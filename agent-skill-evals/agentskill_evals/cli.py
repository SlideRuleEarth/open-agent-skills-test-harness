"""Command-line interface.

    python -m agentskill_evals run        # run the matrix
    python -m agentskill_evals list-agents
    python -m agentskill_evals list-evals
    python -m agentskill_evals migrate     # upgrade legacy evals to canonical format
    python -m agentskill_evals selftest    # parser tests, no CLIs required
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
from .runner import Runner, render_matrix
from .spec import _load_raw, discover_specs, load_scenario, load_spec, skill_names

# Built-in run defaults. The CLI flags default to None (sentinel) so a scenario file's
# value can win when no flag is given; these constants are the final fallback.
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


def _split_models(s: str) -> list[str]:
    return [m.strip() for m in s.split(",") if m.strip()]


def _dedup(seq) -> list:
    """Order-preserving de-duplication (a duplicate target is a duplicate paid cell)."""
    return list(dict.fromkeys(seq))


def _parse_models(pairs: list[str], agents: list[str]) -> dict[str, list[str]]:
    """--model accepts `agent=m1,m2` (repeatable) or a bare `m1,m2` for all agents.
    Returns a per-agent list; repeated flags accumulate."""
    out: dict[str, list[str]] = {}
    for p in pairs or []:
        if "=" in p:
            a, m = p.split("=", 1)
            out.setdefault(_canonical_agent(a), []).extend(_split_models(m))
        else:
            for a in agents:
                out.setdefault(a, []).extend(_split_models(p))
    return out


# ---------------------------------------------------------------------------
# models.yaml — the single source of truth for which models the harness tests
# ---------------------------------------------------------------------------

class ModelsConfig:
    """Parsed `models.yaml`. Accessors are by canonical agent name."""

    def __init__(self, models: dict[str, list[str]], defaults: dict[str, str],
                 judge: dict, warnings: list[str], load_error: str | None = None):
        self._models = models
        self._defaults = defaults
        self.judge = judge          # {"agent": ..., "model": ...} (either key optional)
        self.warnings = warnings
        # set when a config file was present but could not be read/parsed — fatal for
        # `run` (would silently fall back to each CLI's own, possibly pricier, default),
        # warning-only for `list-agents`. A genuinely absent file leaves this None.
        self.load_error = load_error

    def models(self, agent: str) -> list[str]:
        return list(self._models.get(agent, []))

    def default(self, agent: str):
        return self._defaults.get(agent)


def _load_models_config(path) -> ModelsConfig:
    """Read the grouped `agents:` schema; validate without crashing.

        agents:
          <runner>:
            default: <cheapest id>
            models: [<id>, ...]
        judge:
          agent: <runner>
          model: <id>
    """
    if not path or not os.path.isfile(path):
        return ModelsConfig({}, {}, {}, [])
    warnings: list[str] = []
    try:
        raw = _load_raw(path)            # may raise (no PyYAML, or not a mapping)
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


def _resolve_models(agents: list[str], cli_map: dict[str, list[str]],
                    cfg: ModelsConfig, all_models: bool) -> dict[str, list]:
    """Per-agent precedence: CLI --model > (--all-models ? full list) > default > [None]."""
    out: dict[str, list] = {}
    for a in agents:
        if cli_map.get(a):
            vals = list(cli_map[a])
        elif all_models and cfg.models(a):
            vals = cfg.models(a)
        elif cfg.default(a):
            vals = [cfg.default(a)]
        else:
            vals = [None]
        out[a] = _dedup(vals)   # a repeated model id would schedule the same cell twice
    return out


def _target_labels(agents: list[str], model_map: dict[str, list]) -> list[str]:
    return [f"{a}:{m or 'default'}" for a in agents for m in model_map[a]]


def _cost_line(n_cells: int, has_judge: bool) -> str:
    if has_judge:
        return (f"~{n_cells} agent runs + ~{n_cells} judge calls"
                f"        (≈{2 * n_cells} paid LLM calls)")
    return f"~{n_cells} agent runs        (≈{n_cells} paid LLM calls)"


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def _discover(args, skills_root: str):
    """discover_specs, but turn a missing-PyYAML RuntimeError into a clean message
    and exit 2 instead of dumping a traceback."""
    try:
        return discover_specs(skills_root=skills_root, skill=args.skill, paths=args.evals)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)


def _load_scenario(path: str):
    """load_scenario, but turn loader errors (missing PyYAML, bad target/skills, missing
    file) into a clean message + exit 2 instead of a traceback."""
    try:
        return load_scenario(path)
    except (RuntimeError, ValueError, FileNotFoundError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)


def _print_skill_visibility(specs, agents, model_map, isolated, skills_root, provision) -> None:
    """Print, per target, which skills the model will see — computed from the filesystem,
    no agent run. Detailed for a single eval/scenario; a summary for the matrix."""
    repo = set(skill_names(skills_root))
    real_home = os.path.expanduser("~")
    single = specs[0] if len(specs) == 1 else None
    print("Skills visible to the model" + (f" — eval '{single.name}'" if single else "") + ":")
    if not provision:
        print("  (--no-provision: declared skills are NOT seeded — the model sees only "
              "what's already installed)")
    for a in agents:
        adapter = get_adapter(a)
        # with provisioning off, no declared skills are seeded into the workspace or home
        declared = set(single.skills) if (single and provision) else set()
        for m in model_map[a]:
            vis = resolve_visible_skills(adapter, declared, repo, isolated, real_home)
            tag = "isolated" if isolated else "NOT isolated"
            print(f"  {a}:{m or 'default'} ({tag}):")
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
        specs = _discover(args, skills_root)
        if args.tag:
            specs = [s for s in specs if set(args.tag) & set(s.tags)]
        if not specs:
            print("no evals found. Looked under per-skill evals/ dirs in "
                  f"{skills_root!r} (use --skill / --evals to target).", file=sys.stderr)
            return 2
    ov = scenario.overrides if scenario else {}

    # fail early if a declared skill isn't a real skill (a dir with a SKILL.md) under
    # --skills-root. A bare directory check would accept non-skill folders (e.g.
    # agent-skill-evals); a typo or wrong --skills-root would otherwise be silently dropped
    # at provision time, so the model runs with fewer skills than the plan/preview claims.
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

    # agents: explicit, else the scenario's target runner, else all installed
    # (dedup aliases so a runner isn't run twice)
    if args.agents:
        agents = _dedup(_canonical_agent(a) for a in args.agents)
        unknown_agents = [a for a in agents if a not in adapter_names()]
        if unknown_agents:
            print(f"error: unknown runner(s): {', '.join(sorted(set(unknown_agents)))}. "
                  f"Known runners: {', '.join(adapter_names())}.", file=sys.stderr)
            return 2
    elif scenario:
        agents = [scenario_runner]
    else:
        agents = [a.name for a in all_adapters() if a.is_available()]
        if not agents:
            print("no agent CLIs found on PATH. Install one or pass --agents.", file=sys.stderr)
            return 2

    # warn about unavailable agents
    for a in agents:
        if not get_adapter(a).is_available():
            print(f"warning: runner {a!r} not on PATH — its cells will be marked ERR.",
                  file=sys.stderr)

    # models.yaml (single source of truth) → per-agent model lists.
    # A present-but-unparseable config is fatal: silently falling back to each CLI's
    # own default would break the "plain run uses the cheapest model" cost guarantee.
    cfg = _load_models_config(args.models_config or _default_config_path(skills_root))
    if cfg.load_error:
        print(f"error: could not load models.yaml ({cfg.load_error}).\n"
              "  It is the source of truth for which models run; a plain run uses the\n"
              "  cheapest model from it. Fix the file, pass --models-config PATH, or set\n"
              "  the model explicitly with --model <runner>=<id>.", file=sys.stderr)
        return 2
    for w in cfg.warnings:
        print(f"warning: models.yaml: {w}", file=sys.stderr)
    # --model is repeatable AND space-separated → flatten the list-of-lists.
    model_pairs = [p for group in (args.model or []) for p in group]
    cli_map = _parse_models(model_pairs, agents)
    unknown = [a for a in cli_map if a not in agents]
    if unknown:
        print(f"error: --model names unknown or unselected runner(s): "
              f"{', '.join(sorted(unknown))}. Selected runners: {', '.join(agents)}.",
              file=sys.stderr)
        return 2
    # a scenario's target.model behaves like `--model runner=model` (overridable by an
    # explicit --model / --all-models).
    if scenario and scenario.model and scenario_runner not in cli_map and not args.all_models:
        cli_map[scenario_runner] = [scenario.model]
    model_map = _resolve_models(agents, cli_map, cfg, args.all_models)

    # judge: --judge-agent > models.yaml judge.agent > claude;
    #        --judge-model > models.yaml judge.model > the judge agent's cheapest default
    #        (a scenario's `judge: false` disables it; --no-judge always disables)
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

    # ---- plan + cost guardrails (before building the Runner) ----------------
    # resolve run knobs: CLI flag > scenario override > built-in default
    isolated = (not args.no_isolated) and (ov.get("isolated") is not False)
    provision = not args.no_provision

    # an isolated run manages HOME/XDG and each runner's config-home vars, so an eval's `env:`
    # entry for one is overridden — warn rather than silently ignore it.
    if isolated:
        managed = {"HOME", "USERPROFILE", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                   "XDG_CACHE_HOME", "XDG_STATE_HOME"}
        for a in agents:
            managed.update(v for v, _ in getattr(get_adapter(a), "isolation_config_homes", []))
        for s in specs:
            clash = sorted(set(s.env) & managed)
            if clash:
                print(f"warning: eval {s.name!r} sets env {', '.join(clash)} — managed by "
                      "isolation and ignored; pass --no-isolated to honor them.",
                      file=sys.stderr)
    max_cells = args.max_cells if args.max_cells is not None else int(ov.get("max_cells", DEFAULT_MAX_CELLS))
    jobs = args.jobs if args.jobs is not None else int(ov.get("jobs", DEFAULT_JOBS))
    n_cells = sum(len(model_map[a]) for s in specs for a in agents
                  if (s.agents is None or a in s.agents))
    labels = _target_labels(agents, model_map)
    judge_label = f"{judge.agent}/{judge.model or 'default'}" if judge else "off"
    n_llm = n_cells * (2 if judge else 1)

    run_id = args.run_id or _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = os.path.join(os.path.abspath(args.artifacts), run_id)

    print(f"Plan: {n_cells} cell(s) — {len(specs)} eval(s) × {len(labels)} target(s) "
          f"[{', '.join(labels)}]")
    print(f"      judge: {judge_label}   isolated: {'on' if isolated else 'off'}   "
          f"≈{n_llm} LLM calls   artifacts: {run_dir}\n")

    if args.dry_run:
        _print_skill_visibility(specs, agents, model_map, isolated, skills_root, provision)
        print("(dry run — nothing executed)")
        return 0

    if n_cells > max_cells:
        scope = f"{len(specs)} evals × {len(labels)} runner/model targets"
        print(
            f"✗ Refusing to run: {n_cells} cells exceeds the --max-cells ceiling of "
            f"{max_cells}.\n"
            f"  Scope:  {scope}        (judge: {judge_label})\n"
            f"  Cost:   {_cost_line(n_cells, bool(judge))}\n\n"
            "  Narrow the run (recommended):\n"
            "    --skill <name> | --evals <file>     fewer evals\n"
            "    --agents claude                     fewer runners\n"
            "    --model claude=claude-haiku-4-5     specific model(s); drop --all-models\n"
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
                "  or narrow scope (--skill/--evals/--agents/--model, --no-judge). "
                "--dry-run to preview.",
                file=sys.stderr,
            )
            return 2
        print(f"⚠ This spends real API/usage budget: ≈{n_llm} paid LLM calls across "
              f"{n_cells} cells.")
        if input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
            print("aborted.")
            return 1

    runner = Runner(
        agents,
        artifacts_root=os.path.abspath(args.artifacts),
        run_id=run_id,
        skills_root=skills_root,
        judge=judge,
        provision=provision,
        auto_approve=not args.no_auto_approve,
        jobs=jobs,
        model_map=model_map,
        isolated=isolated,
    )

    results = runner.run(specs)

    print(render_matrix(results, agents, model_map))
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

    # success only if something was graded and nothing failed (an all-ungraded run
    # verified nothing, so it is not a pass).
    failed = [c for c in graded if not c.passed]
    return 0 if graded and not failed else 1


def cmd_list_agents(args) -> int:
    skills_root = os.path.abspath(getattr(args, "skills_root", None) or _default_skills_root())
    cfg = _load_models_config(getattr(args, "models_config", None)
                              or _default_config_path(skills_root))
    if cfg.load_error:   # warning-only here (read-only listing); fatal only for `run`
        print(f"warning: models.yaml could not be loaded ({cfg.load_error}); "
              "showing each runner's CLI default.", file=sys.stderr)
    for w in cfg.warnings:
        print(f"warning: models.yaml: {w}", file=sys.stderr)

    # a runner is the harness used to reach a model; models come from models.yaml
    print(f"{'RUNNER':<14}{'BINARY':<10}{'AVAILABLE':<11}{'DEFAULT MODEL':<20}MODELS")
    for a in all_adapters():
        models = cfg.models(a.name)
        dflt = cfg.default(a.name) or "(cli default)"
        models_str = ", ".join(models) if models else "(cli default)"
        avail = "yes" if a.is_available() else "no"
        print(f"{a.name:<14}{a.binary:<10}{avail:<11}{dflt:<20}{models_str}")
    print("\nmodels come from models.yaml (the single source of truth). "
          "A runner is the harness used to reach a model — see the README.")
    return 0


def cmd_list_evals(args) -> int:
    skills_root = os.path.abspath(args.skills_root)
    specs = _discover(args, skills_root)
    if not specs:
        print("no evals found.", file=sys.stderr)
        return 2
    # size the columns to the data so long eval names don't collide with CHECKS
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
    """Audit skill visibility: the provisionable superset, plus per-runner global skills split
    into repo (masked under isolation) vs vendor (kept), with drift warnings."""
    skills_root = os.path.abspath(args.skills_root)
    superset = skill_names(skills_root)
    repo = set(superset)
    real_home = os.path.expanduser("~")

    print(f"provisionable skills (superset) under {skills_root}  ({len(superset)}):")
    print("  " + (", ".join(superset) if superset else "(none)"))

    print("\nper-runner global skills — what an un-isolated run also sees:")
    for a in all_adapters():
        # nothing declared → every repo skill found globally is in `masked`
        vis = resolve_visible_skills(a, (), repo, isolated=True, real_home=real_home)
        masked, vendor = vis["masked"], vis["vendor"]
        dirs = a.global_skills_subpaths or []
        print(f"\n{a.name}:")
        print("  dirs: " + (", ".join("~/" + d for d in dirs) if dirs else "(none)"))
        print(f"  repo skills (masked under isolation): {', '.join(masked) or '(none)'}")
        print(f"  vendor/other (kept):                  {', '.join(vendor) or '(none)'}")
        present = set(masked)
        missing = sorted(repo - present)
        if present and missing:   # partial install → likely a stale `make link-global`
            print(f"  ⚠ drift: global is missing {', '.join(missing)} "
                  "(run `make link-global` to refresh)")

    print("\nWith isolation ON (default) a run sees only the skills it provisions + vendor "
          "skills; with --no-isolated it also sees the repo skills above.")
    print("Note: skills bundled inside a CLI's package or plugins aren't listed here.")
    return 0


def cmd_migrate(args) -> int:
    skills_root = os.path.abspath(args.skills_root)
    specs = _discover(args, skills_root)
    if not specs:
        print("no evals found to migrate.", file=sys.stderr)
        return 2

    try:
        import yaml  # type: ignore
        have_yaml = True
    except ModuleNotFoundError:
        have_yaml = False

    fmt = args.to
    if fmt == "yaml" and not have_yaml:
        print("PyYAML not installed; falling back to --to json.", file=sys.stderr)
        fmt = "json"

    for s in specs:
        canonical = s.to_canonical_dict()
        src = s.source_path
        base, _ = os.path.splitext(src)
        dest = f"{base}.{ 'yaml' if fmt == 'yaml' else 'json' }"
        if fmt == "yaml":
            text = yaml.safe_dump(canonical, sort_keys=False, width=100, allow_unicode=True)
        else:
            import json
            text = json.dumps(canonical, indent=2, ensure_ascii=False) + "\n"

        if not args.write:
            print(f"--- {os.path.relpath(dest, skills_root)} (dry run) ---")
            print(text)
            continue

        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(text)
        action = f"wrote {os.path.relpath(dest, skills_root)}"
        if args.replace and os.path.abspath(dest) != os.path.abspath(src):
            os.remove(src)
            action += f"  (removed {os.path.basename(src)})"
        print(action)

    if not args.write:
        print("\n(dry run — re-run with --write to apply, --replace to delete originals)")
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

    sp = sub.add_parser("run", help="run the agent × eval matrix (or a scenario via --config)")
    add_discovery(sp)
    sp.add_argument("--config", help="run a scenario file: a combination eval (skills provisioned "
                    "together + a target). See scenarios/. CLI flags override file values.")
    sp.add_argument("--agents", nargs="*", help=f"agents to test (default: all installed). "
                    f"Known: {', '.join(adapter_names())}")
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
    sp.add_argument("--model", nargs="*", action="append", default=None,
                    help="model override: `runner=m1,m2` (repeatable, and/or space-separated) "
                         "or bare `m1,m2` for all; overrides models.yaml")
    sp.add_argument("--models-config", help="models.yaml path (default: <skills-root>/models.yaml)")
    sp.add_argument("--all-models", action="store_true",
                    help="run each runner's full models.yaml list (default: just the cheapest)")
    sp.add_argument("--max-cells", type=int, default=None,
                    help="hard ceiling: refuse runs larger than this (default 25; -y can't lift it)")
    sp.add_argument("-y", "--yes", action="store_true",
                    help="skip the multi-cell confirmation (still bounded by --max-cells)")
    sp.add_argument("--dry-run", action="store_true",
                    help="print the resolved plan + cell count and exit without running")
    sp.add_argument("--tag", nargs="*", help="only evals with one of these tags")
    sp.add_argument("-v", "--verbose", action="store_true", help="print failing assertions")
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

    sp = sub.add_parser("migrate", help="rewrite evals into the canonical format")
    add_discovery(sp)
    sp.add_argument("--to", choices=["yaml", "json"], default="yaml", help="target format")
    sp.add_argument("--write", action="store_true", help="apply changes (default: dry run)")
    sp.add_argument("--replace", action="store_true",
                    help="delete the original file when the extension changes")
    sp.set_defaults(func=cmd_migrate)

    sp = sub.add_parser("selftest", help="test the adapter parsers against bundled fixtures")
    sp.add_argument("-v", "--verbose", action="store_true")
    sp.set_defaults(func=cmd_selftest)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
