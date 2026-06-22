"""Command-line interface.

    agentskill-evals run --agent claude --skill sliderule-docsearch
    agentskill-evals list-agents
    agentskill-evals list-evals
    agentskill-evals selftest    # parser tests, no CLIs required
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import sys

from . import __version__
from .adapters import adapter_names, all_adapters, get_adapter
from .isolation import resolve_visible_skills
from .judge import Judge
from .runner import Runner, _cell_text, render_matrix
from .spec import _load_raw, discover_specs, load_scenario, load_spec, skill_names

DEFAULT_MAX_CELLS = 25
DEFAULT_JOBS = 1


def _default_skills_root() -> str:
    return os.getcwd()


def _default_config_path(skills_root: str) -> str:
    return os.path.join(skills_root, "models.yaml")


def _canonical_agent(name: str) -> str:
    try:
        return get_adapter(name).name
    except KeyError:
        return name.strip().lower()


def _dedup(seq) -> list:
    return list(dict.fromkeys(seq))


# ---------------------------------------------------------------------------
# models.yaml
# ---------------------------------------------------------------------------

class ModelsConfig:
    def __init__(self, models: dict[str, list[str]], defaults: dict[str, str],
                 judge: dict, warnings: list[str], load_error: str | None = None):
        self._models = models
        self._defaults = defaults
        self.judge = judge
        self.warnings = warnings
        self.load_error = load_error

    def models(self, agent: str) -> list[str]:
        return list(self._models.get(agent, []))

    def default(self, agent: str):
        return self._defaults.get(agent)


def _load_models_config(path) -> ModelsConfig:
    if not path or not os.path.isfile(path):
        return ModelsConfig({}, {}, {}, [])
    warnings: list[str] = []
    try:
        raw = _load_raw(path)
    except Exception as exc:
        return ModelsConfig({}, {}, {}, [], load_error=f"{path}: {exc}")

    models: dict[str, list[str]] = {}
    defaults: dict[str, str] = {}

    agents_blk = raw.get("agents") or {}
    if not isinstance(agents_blk, dict):
        warnings.append("`agents:` must be a mapping — ignored")
        agents_blk = {}
    for name, blk in agents_blk.items():
        try:
            agent = get_adapter(str(name)).name
        except KeyError:
            warnings.append(f"agent {name!r} has no registered adapter — ignored")
            continue
        if not isinstance(blk, dict):
            warnings.append(f"agent {agent!r}: block must be a mapping — ignored")
            continue
        raw_models = blk.get("models") or []
        if isinstance(raw_models, str):
            raw_models = [raw_models]
        if not isinstance(raw_models, list):
            warnings.append(f"agent {agent!r}: `models:` must be a list — ignored")
            raw_models = []
        seen: list[str] = []
        for m in raw_models:
            m = str(m)
            if m in seen:
                warnings.append(f"agent {agent!r}: model {m!r} listed more than once")
            else:
                seen.append(m)
        if not seen:
            warnings.append(f"agent {agent!r}: no models listed")
        models[agent] = seen
        dflt = blk.get("default")
        if dflt is not None:
            dflt = str(dflt)
            if seen and dflt not in seen:
                warnings.append(
                    f"agent {agent!r}: default {dflt!r} is not in its models {seen}")
            defaults[agent] = dflt
        elif seen:
            warnings.append(
                f"agent {agent!r}: no `default:` — plain runs use the CLI's own default")

    for key in raw:
        if key not in ("agents", "judge"):
            warnings.append(f"unknown top-level key {key!r} ignored")

    judge: dict = {}
    jblk = raw.get("judge") or {}
    if not isinstance(jblk, dict):
        warnings.append("`judge:` must be a mapping — ignored")
        jblk = {}
    jagent = jblk.get("agent")
    if jagent:
        try:
            judge["agent"] = get_adapter(str(jagent)).name
        except KeyError:
            warnings.append(f"judge.agent {jagent!r} has no registered adapter — ignored")
    if jblk.get("model"):
        judge["model"] = str(jblk["model"])
    return ModelsConfig(models, defaults, judge, warnings)


def _resolve_models(agent: str, cli_models: list[str] | None,
                    cfg: ModelsConfig, all_models: bool) -> list:
    """Precedence: CLI --model > (--all-models ? full list) > default > [None]."""
    if cli_models:
        return _dedup(cli_models)
    if all_models and cfg.models(agent):
        return cfg.models(agent)
    if cfg.default(agent):
        return [cfg.default(agent)]
    return [None]


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def _discover(args, skills_root: str):
    try:
        return discover_specs(skills_root=skills_root, skill=args.skill, paths=args.evals)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)


def _load_scenario(path: str):
    try:
        return load_scenario(path)
    except (RuntimeError, ValueError, FileNotFoundError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)


def _print_skill_visibility(specs, agent, models, isolated, skills_root, provision) -> None:
    repo = set(skill_names(skills_root))
    real_home = os.path.expanduser("~")
    single = specs[0] if len(specs) == 1 else None
    print("Skills visible to the model" + (f" — eval '{single.name}'" if single else "") + ":")
    if not provision:
        print("  (--no-provision: declared skills are NOT seeded — the model sees only "
              "what's already installed)")
    adapter = get_adapter(agent)
    declared = set(single.skills) if (single and provision) else set()
    vis = resolve_visible_skills(adapter, declared, repo, isolated, real_home)
    tag = "isolated" if isolated else "NOT isolated"
    print(f"  {agent} ({tag}):")
    if not provision:
        prov = "(none — provisioning off)"
    elif not single:
        prov = "(varies per eval)"
    else:
        prov = ", ".join(vis["provisioned"]) or "(none)"
    print(f"      provisioned:  {prov}")
    vend = ", ".join(vis["vendor"]) or "(none in global dirs)"
    print(f"      vendor kept:  {vend}  + the agent's built-in/plugin skills (not listed)")
    if isolated:
        print(f"      masked:       {', '.join(vis['masked']) or '(none)'}")
    else:
        print(f"      also visible: {', '.join(vis['also_visible']) or '(none)'}")
    print()


def cmd_run(args) -> int:
    skills_root = os.path.abspath(args.skills_root)

    # spec source: a scenario file (--config) OR per-skill discovery
    scenario = None
    scenario_runner = None
    if args.config:
        if args.skill or args.evals:
            print("error: --config can't be combined with --skill/--evals — a scenario "
                  "defines its own eval.", file=sys.stderr)
            return 2
        scenario = _load_scenario(args.config)
        try:
            scenario_runner = get_adapter(scenario.runner).name
        except KeyError:
            print(f"error: unknown runner {scenario.runner!r} in {args.config} target. "
                  f"Known runners: {', '.join(adapter_names())}.", file=sys.stderr)
            return 2
        specs = [scenario.spec]
    else:
        if not args.skill and not args.evals:
            print("error: `run` requires --skill, --evals, or --config to scope what runs.\n"
                  "  Narrow the run:\n"
                  "    --skill <name>          one skill's evals/\n"
                  "    --evals <file> ...      specific eval files\n"
                  "    --config <scenario>     a scenario file\n"
                  "  Use `list-evals --skills-root .` to see what's available.",
                  file=sys.stderr)
            return 2
        specs = _discover(args, skills_root)
        if args.tag:
            specs = [s for s in specs if set(args.tag) & set(s.tags)]
        if not specs:
            print("no evals found. Looked under per-skill evals/ dirs in "
                  f"{skills_root!r} (use --skill / --evals to target).", file=sys.stderr)
            return 2
    ov = scenario.overrides if scenario else {}

    # validate declared skills
    valid_skills = set(skill_names(skills_root))
    missing: dict[str, list[str]] = {}
    for s in specs:
        for name in s.skills:
            if name not in valid_skills:
                missing.setdefault(s.name, []).append(name)
    if missing:
        print(f"error: declared skill(s) not found (no SKILL.md) under --skills-root "
              f"{skills_root!r}:", file=sys.stderr)
        for ev, names in missing.items():
            print(f"    {ev}: {', '.join(names)}", file=sys.stderr)
        avail = ", ".join(skill_names(skills_root)) or "(none found)"
        print(f"  available skills: {avail}\n"
              "  fix the `skills:` list, or point --skills-root at the repo root.",
              file=sys.stderr)
        return 2

    # resolve the single agent: --agent > scenario target > error
    if args.agent:
        agent = _canonical_agent(args.agent)
        if agent not in adapter_names():
            print(f"error: unknown runner {args.agent!r}. "
                  f"Known: {', '.join(adapter_names())}.", file=sys.stderr)
            return 2
    elif scenario:
        agent = scenario_runner
    else:
        print(f"error: --agent is required. Known runners: {', '.join(adapter_names())}.\n"
              "  Use `list-agents` to see availability and configured models.",
              file=sys.stderr)
        return 2

    if not get_adapter(agent).is_available():
        print(f"warning: runner {agent!r} not on PATH — cells will be marked ERR.",
              file=sys.stderr)

    # models.yaml → model list for this agent
    cfg = _load_models_config(args.models_config or _default_config_path(skills_root))
    if cfg.load_error:
        print(f"error: could not load models.yaml ({cfg.load_error}).\n"
              "  Fix the file, pass --models-config PATH, or set the model explicitly "
              "with --model.", file=sys.stderr)
        return 2
    for w in cfg.warnings:
        print(f"warning: models.yaml: {w}", file=sys.stderr)

    cli_models = None
    if args.model:
        cli_models = [m.strip() for m in args.model.split(",") if m.strip()]
    if scenario and scenario.model and not cli_models and not args.all_models:
        cli_models = [scenario.model]
    models = _resolve_models(agent, cli_models, cfg, args.all_models)

    # judge
    do_judge = (not args.no_judge) and (ov.get("judge") is not False)
    judge = None
    if do_judge:
        judge_agent = (args.judge_agent or cfg.judge.get("agent")
                       or ("claude" if get_adapter("claude").is_available() else None))
        if judge_agent:
            try:
                judge_agent = get_adapter(judge_agent).name
            except KeyError:
                print(f"warning: judge agent {judge_agent!r} unknown "
                      f"(known: {', '.join(adapter_names())}) — disabling judge.",
                      file=sys.stderr)
                judge_agent = None
        if judge_agent:
            judge_model = args.judge_model or cfg.judge.get("model") or cfg.default(judge_agent)
            judge = Judge(agent=judge_agent, model=judge_model)
            if not judge.available():
                print(f"warning: judge runner {judge_agent!r} not on PATH — "
                      "llm_judge checks will fail.", file=sys.stderr)
        elif any(s.rubric for s in specs):
            print("note: evals have rubrics but no judge available; "
                  "pass --judge-agent or install claude.", file=sys.stderr)
    if not do_judge and any(s.rubric for s in specs):
        print("note: rubric grading is off — only deterministic assertions are graded "
              "(llm_judge checks are skipped).", file=sys.stderr)

    # plan + cost guardrails
    isolated = (not args.no_isolated) and (ov.get("isolated") is not False)
    provision = not args.no_provision

    if isolated:
        managed = {"HOME", "USERPROFILE", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                   "XDG_CACHE_HOME", "XDG_STATE_HOME"}
        managed.update(v for v, _ in getattr(get_adapter(agent), "isolation_config_homes", []))
        for s in specs:
            clash = sorted(set(s.env) & managed)
            if clash:
                print(f"warning: eval {s.name!r} sets env {', '.join(clash)} — managed by "
                      "isolation and ignored; pass --no-isolated to honor them.",
                      file=sys.stderr)

    max_cells = args.max_cells if args.max_cells is not None else int(ov.get("max_cells", DEFAULT_MAX_CELLS))
    jobs = args.jobs if args.jobs is not None else int(ov.get("jobs", DEFAULT_JOBS))
    n_eligible = sum(1 for s in specs if s.agents is None or agent in s.agents)
    n_cells = n_eligible * len(models)
    model_labels = [m or "default" for m in models]
    judge_label = f"{judge.agent}/{judge.model or 'default'}" if judge else "off"
    n_llm = n_cells * (2 if judge else 1)

    run_id = args.run_id or _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = os.path.join(os.path.abspath(args.artifacts), run_id)

    print(f"Plan: {n_cells} cell(s) — {n_eligible} eval(s) × {agent} "
          f"[{', '.join(model_labels)}]")
    print(f"      judge: {judge_label}   isolated: {'on' if isolated else 'off'}   "
          f"≈{n_llm} LLM calls   artifacts: {run_dir}\n")

    if args.dry_run:
        _print_skill_visibility(specs, agent, models, isolated, skills_root, provision)
        print("(dry run — nothing executed)")
        return 0

    if n_cells > max_cells:
        print(
            f"✗ Refusing to run: {n_cells} cells exceeds the --max-cells ceiling of "
            f"{max_cells}.\n"
            "  Narrow the run:\n"
            "    --skill <name> | --evals <file>     fewer evals\n"
            "    --model <id>                        specific model; drop --all-models\n"
            "    --no-judge                          skip rubric grading\n"
            "    --dry-run                           preview, run nothing\n\n"
            "  Or raise the ceiling deliberately:\n"
            f"    --max-cells {max(n_cells, max_cells * 4)}"
            "                     (you will still be asked to confirm)",
            file=sys.stderr,
        )
        return 2

    if n_cells > 1 and not args.yes:
        if not sys.stdin.isatty():
            print(
                f"✗ Refusing to run {n_cells} cells without confirmation (no TTY to prompt).\n"
                f"  Re-run with -y/--yes to confirm (still capped at --max-cells {max_cells}),\n"
                "  or narrow scope (--skill/--evals/--model, --no-judge). --dry-run to preview.",
                file=sys.stderr,
            )
            return 2
        print(f"⚠ This spends real API/usage budget: ≈{n_llm} paid LLM calls across "
              f"{n_cells} cells.")
        if input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
            print("aborted.")
            return 1

    runner = Runner(
        agent,
        models=models,
        artifacts_root=os.path.abspath(args.artifacts),
        run_id=run_id,
        skills_root=skills_root,
        judge=judge,
        provision=provision,
        auto_approve=not args.no_auto_approve,
        jobs=jobs,
        isolated=isolated,
    )

    results = runner.run(specs)

    print(render_matrix(results, agent, models))
    graded = [c for c in results if not c.ungraded]
    n_pass = sum(1 for c in graded if c.passed)
    ung = len(results) - len(graded)
    line = f"\n{n_pass}/{len(graded)} cells passed"
    if ung:
        line += f"   ({ung} ungraded — rubric-only evals with no judge)"
    print(f"{line}   (details: {runner.run_dir}/summary.md)")

    if args.verbose:
        for c in results:
            if not c.passed and not c.ungraded:
                print(f"\n✗ {c.eval_name} [{c.agent}/{c.model or 'default'}]"
                      + (f"  ERROR: {c.run_result.error}" if c.run_result.error else ""))
                for a in c.assertions:
                    if not a.passed:
                        print(f"    - {a.type}: {a.message}")

    reports_mode = getattr(args, "reports", "fail")
    if judge is None and reports_mode == "fail":
        reports_mode = "all"
    if reports_mode != "none":
        shown = [c for c in results
                 if reports_mode == "all" or not c.passed or c.run_result.error]
        if shown:
            print("\nreports (prompt + complete response + produced files):")
            for c in shown:
                print(f"  {_cell_text(c):<10} {c.eval_name} [{c.model or 'default'}]")
                print(f"    → {os.path.join(c.artifacts_dir, 'report.md')}")

    failed = [c for c in graded if not c.passed]
    return 0 if graded and not failed else 1


def cmd_list_agents(args) -> int:
    skills_root = os.path.abspath(getattr(args, "skills_root", None) or _default_skills_root())
    cfg = _load_models_config(getattr(args, "models_config", None)
                              or _default_config_path(skills_root))
    if cfg.load_error:
        print(f"warning: models.yaml could not be loaded ({cfg.load_error}); "
              "showing each runner's CLI default.", file=sys.stderr)
    for w in cfg.warnings:
        print(f"warning: models.yaml: {w}", file=sys.stderr)

    print(f"{'RUNNER':<14}{'BINARY':<10}{'AVAILABLE':<11}{'DEFAULT MODEL':<20}MODELS")
    for a in all_adapters():
        models = cfg.models(a.name)
        dflt = cfg.default(a.name) or "(cli default)"
        models_str = ", ".join(models) if models else "(cli default)"
        avail = "yes" if a.is_available() else "no"
        print(f"{a.name:<14}{a.binary:<10}{avail:<11}{dflt:<20}{models_str}")
    print("\nEach runner lists the models it supports (overlap across runners is expected).")
    print("Use --agent <runner> with `run` to select one.")
    return 0


def cmd_list_evals(args) -> int:
    skills_root = os.path.abspath(args.skills_root)
    specs = _discover(args, skills_root)
    if not specs:
        print("no evals found.", file=sys.stderr)
        return 2
    skill_w = max([len("SKILL")] + [len(s.skill_name or "-") for s in specs]) + 2
    eval_w = max([len("EVAL")] + [len(s.name) for s in specs]) + 2
    print(f"{'SKILL':<{skill_w}}{'EVAL':<{eval_w}}{'CHECKS':<8}AGENTS")
    for s in specs:
        n = len(s.effective_assertions())
        agents = ",".join(s.agents) if s.agents else "all"
        print(f"{(s.skill_name or '-'):<{skill_w}}{s.name:<{eval_w}}{n:<8}{agents}")
    print(f"\n{len(specs)} eval(s)")
    return 0


def cmd_list_skills(args) -> int:
    skills_root = os.path.abspath(args.skills_root)
    superset = skill_names(skills_root)
    repo = set(superset)
    real_home = os.path.expanduser("~")

    print(f"provisionable skills (superset) under {skills_root}  ({len(superset)}):")
    print("  " + (", ".join(superset) if superset else "(none)"))

    print("\nper-runner global skills — what an un-isolated run also sees:")
    for a in all_adapters():
        vis = resolve_visible_skills(a, (), repo, isolated=True, real_home=real_home)
        masked, vendor = vis["masked"], vis["vendor"]
        dirs = a.global_skills_subpaths or []
        print(f"\n{a.name}:")
        print("  dirs: " + (", ".join("~/" + d for d in dirs) if dirs else "(none)"))
        print(f"  repo skills (masked under isolation): {', '.join(masked) or '(none)'}")
        print(f"  vendor/other (kept):                  {', '.join(vendor) or '(none)'}")
        present = set(masked)
        missing_skills = sorted(repo - present)
        if present and missing_skills:
            print(f"  ⚠ drift: global is missing {', '.join(missing_skills)} "
                  "(run `make link-global` to refresh)")

    print("\nWith isolation ON (default) a run sees only the skills it provisions + vendor "
          "skills; with --no-isolated it also sees the repo skills above.")
    print("Note: skills bundled inside a CLI's package or plugins aren't listed here.")
    return 0


def cmd_selftest(args) -> int:
    from .selftest import run_selftest
    return run_selftest(verbose=args.verbose)


# ---------------------------------------------------------------------------
# arg parsing
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agentskill-evals", description=__doc__)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_discovery(sp):
        sp.add_argument("--skills-root", default=_default_skills_root(),
                        help="dir containing skill folders (each with an evals/). Default: cwd")
        sp.add_argument("--skill", help="only this skill's evals/")
        sp.add_argument("--evals", nargs="*", help="explicit eval files or directories")

    sp = sub.add_parser("run", help="run evals for a single agent (or a scenario via --config)")
    add_discovery(sp)
    sp.add_argument("--config", help="run a scenario file (includes target agent + model). "
                    "See scenarios/. CLI flags override file values.")
    sp.add_argument("--agent", help=f"runner to use. Known: {', '.join(adapter_names())}")
    sp.add_argument("--artifacts", default="artifacts", help="artifacts root dir")
    sp.add_argument("--run-id", help="name this run (default: timestamp)")
    sp.add_argument("--jobs", type=int, default=None, help="parallel cells (default 1)")
    sp.add_argument("--judge-agent", help="agent to grade rubrics (default: claude if installed)")
    sp.add_argument("--judge-model", help="model override for the judge")
    sp.add_argument("--no-judge", action="store_true", help="disable LLM-judge rubric grading")
    sp.add_argument("--no-provision", action="store_true", help="don't copy skills into workspaces")
    sp.add_argument("--no-isolated", action="store_true",
                    help="also expose your globally-installed repo skills (default: each run is "
                         "isolated to only the skills it provisions + the agent's vendor skills)")
    sp.add_argument("--no-auto-approve", action="store_true",
                    help="don't auto-approve tool/file actions")
    sp.add_argument("--model", help="model(s) to run, comma-separated; overrides models.yaml")
    sp.add_argument("--models-config", help="models.yaml path (default: <skills-root>/models.yaml)")
    sp.add_argument("--all-models", action="store_true",
                    help="run this agent's full models.yaml list (default: just the cheapest)")
    sp.add_argument("--max-cells", type=int, default=None,
                    help="hard ceiling: refuse runs larger than this (default 25; -y can't lift it)")
    sp.add_argument("-y", "--yes", action="store_true",
                    help="skip the multi-cell confirmation (still bounded by --max-cells)")
    sp.add_argument("--dry-run", action="store_true",
                    help="print the resolved plan + cell count and exit without running")
    sp.add_argument("--tag", nargs="*", help="only evals with one of these tags")
    sp.add_argument("-v", "--verbose", action="store_true", help="print failing assertions")
    sp.add_argument("--reports", choices=["fail", "all", "none"], default="fail",
                    help="after the run, print paths to the per-cell report.md: "
                         "fail (default), all, none. Implicitly 'all' when --no-judge is set.")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("list-agents", help="show runners, availability, and configured models")
    sp.add_argument("--skills-root", default=_default_skills_root(),
                    help="dir to look for models.yaml (default: cwd)")
    sp.add_argument("--models-config", help="models.yaml path (default: <skills-root>/models.yaml)")
    sp.set_defaults(func=cmd_list_agents)

    sp = sub.add_parser("list-evals", help="discover and list evals")
    add_discovery(sp)
    sp.set_defaults(func=cmd_list_evals)

    sp = sub.add_parser("list-skills",
                        help="audit skill visibility (superset + per-runner masked/kept + drift)")
    sp.add_argument("--skills-root", default=_default_skills_root(),
                    help="dir containing skill folders (default: cwd)")
    sp.set_defaults(func=cmd_list_skills)

    sp = sub.add_parser("selftest", help="test the adapter parsers against bundled fixtures")
    sp.add_argument("-v", "--verbose", action="store_true")
    sp.set_defaults(func=cmd_selftest)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
