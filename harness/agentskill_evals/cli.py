"""Command-line interface.

    agentskill-evals run --agent claude --skill sliderule-pipeline-direct-request
    agentskill-evals list-agents-configured-models
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
from .progress import Progress
from .runner import Runner, _cell_text, _safe, render_matrix
from .spec import (
    REASONING_EFFORT_LEVELS,
    SKILLS_SUBDIR,
    ModelTarget,
    _load_raw,
    discover_specs,
    load_scenario,
    load_spec,
    parse_model_target,
    repo_root_for,
    skill_names,
    validate_spec,
)

DEFAULT_MAX_CELLS = 25
DEFAULT_JOBS = 1


def _default_skills_root() -> str:
    return os.getcwd()


def _has_skill_dirs(path: str) -> bool:
    """True if `path` directly contains at least one `<name>/SKILL.md`."""
    try:
        return any(
            os.path.isfile(os.path.join(path, name, "SKILL.md"))
            for name in os.listdir(path)
        )
    except OSError:
        return False


def _resolve_skills_root(raw: str | None) -> str:
    """Resolve the effective skills root (skills are its immediate subdirs).

    If `raw` itself holds the skill folders it's used as-is; otherwise, when it
    has a `skills_examples/` subdir that does, we descend into it. This keeps
    the documented `--skills-root .` / `--skills-root ..` invocations working now
    that the example skills live under `skills_examples/`.
    """
    base = os.path.abspath(raw or _default_skills_root())
    if _has_skill_dirs(base):
        return base
    nested = os.path.join(base, SKILLS_SUBDIR)
    if _has_skill_dirs(nested):
        return nested
    return base


def _default_config_path(skills_root: str) -> str:
    """Locate models.yaml — the repo-root single source of truth.

    Prefer `<skills_root>/models.yaml`, but when skills live under
    `skills_examples/` the config still sits at the repo root (the parent), so
    fall back there.
    """
    here = os.path.join(skills_root, "models.yaml")
    if os.path.isfile(here):
        return here
    parent = os.path.join(os.path.dirname(os.path.abspath(skills_root)), "models.yaml")
    if os.path.isfile(parent):
        return parent
    return here


def _canonical_agent(name: str) -> str:
    try:
        return get_adapter(name).name
    except KeyError:
        return name.strip().lower()


def _dedup(seq) -> list:
    return list(dict.fromkeys(seq))


def _default_run_id(args, scenario, specs) -> str:
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    if scenario:
        return f"{ts}_scen_{_safe(scenario.spec.name)}"
    if args.skill:
        return f"{ts}_skill_{_safe(args.skill)}"
    if args.evals:
        if len(args.evals) == 1:
            name = os.path.splitext(os.path.basename(args.evals[0]))[0]
            return f"{ts}_eval_{_safe(name)}"
        return f"{ts}_evals_{len(args.evals)}"
    return ts


def _duplicate_names(specs) -> dict[str, list[str]]:
    """Eval names shared by more than one discovered spec. Duplicates silently collide:
    same artifacts cell dir (the second wipes the first's workspace) and same key in the
    results matrix (one row shown, pass rates undercount) — so cmd_run blocks on them."""
    by_name: dict[str, list[str]] = {}
    for s in specs:
        by_name.setdefault(s.name, []).append(s.source_path or "?")
    return {n: paths for n, paths in by_name.items() if len(paths) > 1}


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
    if jblk.get("timeout") is not None:
        try:
            judge["timeout"] = int(jblk["timeout"])
        except (TypeError, ValueError):
            warnings.append(
                f"judge.timeout must be an integer (seconds), got {jblk['timeout']!r} — ignored")
    return ModelsConfig(models, defaults, judge, warnings)


def _resolve_targets(agent: str, cli_targets: list[ModelTarget] | None,
                     cfg: ModelsConfig, all_models: bool) -> list[ModelTarget]:
    """Precedence: CLI --model / scenario target > (--all-models ? full list) > default >
    [default target]. A target that pins only an effort (no model id) gets the models.yaml
    default model, preserving how a model-less scenario resolves."""
    if cli_targets:
        if all_models:
            print("warning: --model and --all-models both given — --model wins, "
                  "--all-models is ignored", file=sys.stderr)
        return _dedup(ModelTarget(t.model or cfg.default(agent), t.reasoning_effort)
                      for t in cli_targets)
    if all_models and cfg.models(agent):
        return [ModelTarget(m) for m in cfg.models(agent)]
    if cfg.default(agent):
        return [ModelTarget(cfg.default(agent))]
    return [ModelTarget()]


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def _is_yaml_error(exc: Exception) -> bool:
    """True if `exc` is PyYAML's own parse-error type — not a subclass of RuntimeError/ValueError/
    OSError, so it slips past the usual load-failure catches below and would otherwise surface as
    a raw traceback for a plain YAML syntax error instead of the clean `error: ...` message every
    other load failure gets. Guarded import: if PyYAML isn't installed, _load_raw already raises
    a friendly RuntimeError before ever calling yaml.safe_load, so this can't fire either way."""
    try:
        import yaml  # type: ignore
    except ModuleNotFoundError:
        return False
    return isinstance(exc, yaml.YAMLError)


def _discover(args, skills_root: str):
    try:
        return discover_specs(skills_root=skills_root, skill=args.skill, paths=args.evals)
    except (RuntimeError, ValueError, FileNotFoundError, OSError) as exc:
        # ValueError covers both a spec's own structural errors (missing `prompt`, bad `skills`
        # shape) and malformed JSON (json.JSONDecodeError is a ValueError subclass) — without it,
        # only RuntimeError/yaml-error was caught and either of those cases raised a raw
        # traceback instead of this function's clean `error: ...` exit.
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
    except Exception as exc:
        if not _is_yaml_error(exc):
            raise
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)


def _load_scenario(path: str):
    try:
        return load_scenario(path)
    except (RuntimeError, ValueError, FileNotFoundError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
    except Exception as exc:
        if not _is_yaml_error(exc):
            raise
        print(f"error: {path}: {exc}", file=sys.stderr)
        raise SystemExit(2)


def _print_skill_visibility(specs, agent, isolated, skills_root, provision) -> None:
    repo = set(skill_names(skills_root))
    real_home = os.path.expanduser("~")
    single = specs[0] if len(specs) == 1 else None
    print("Skills visible to the model" + (f" — eval '{single.name}'" if single else "") + ":")
    if not provision:
        print("  (--no-provision: declared skills are NOT seeded — the model sees only "
              "what's already installed)")
    adapter = get_adapter(agent)
    declared = set(single.skills) if (single and provision) else set()
    vis = resolve_visible_skills(adapter, declared, repo, isolated, real_home,
                                 repo_root=repo_root_for(skills_root))
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
    print(f"      vendor kept:  {vend}  + the agent's built-in skills (not listed)")
    if isolated:
        print(f"      masked:       {', '.join(vis['masked']) or '(none)'}")
    else:
        print(f"      also visible: {', '.join(vis['also_visible']) or '(none)'}")
    print()


def cmd_run(args) -> int:
    skills_root = _resolve_skills_root(args.skills_root)

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

    # duplicate eval names collide in artifacts dirs and the results matrix — block early
    dupes = _duplicate_names(specs)
    if dupes:
        print("error: duplicate eval name(s) — rename so results don't collide:",
              file=sys.stderr)
        for n, paths in sorted(dupes.items()):
            print(f"    {n!r}:", file=sys.stderr)
            for p in paths:
                print(f"        {p}", file=sys.stderr)
        return 2

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
              "  Use `list-agents-configured-models` to see availability and configured models.",
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

    cli_targets = None
    if args.model:
        try:
            cli_targets = [parse_model_target(tok, "--model")
                           for tok in args.model.split(",") if tok.strip()]
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if scenario and not cli_targets and not args.all_models:
        pinned = [t for t in scenario.targets
                  if t.model is not None or t.reasoning_effort is not None]
        if pinned:
            cli_targets = pinned
    targets = _resolve_targets(agent, cli_targets, cfg, args.all_models)

    # judge
    # `judge:` in a scenario can be true/false OR a mapping {agent, model}.
    ov_judge = ov.get("judge")
    ov_judge_cfg: dict = {}
    if isinstance(ov_judge, dict):
        ov_judge_cfg = ov_judge
        do_judge = (not args.no_judge)
    else:
        do_judge = (not args.no_judge) and (ov_judge is not False)
    judge = None
    if do_judge:
        judge_agent = (args.judge_agent
                       or ov_judge_cfg.get("agent")
                       or cfg.judge.get("agent")
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
            judge_model = (args.judge_model
                           or ov_judge_cfg.get("model")
                           or cfg.judge.get("model")
                           or cfg.default(judge_agent))
            judge_timeout = (args.judge_timeout
                             or ov_judge_cfg.get("timeout")
                             or cfg.judge.get("timeout")
                             or 240)
            try:
                judge_timeout = int(judge_timeout)
            except (TypeError, ValueError):
                print(f"warning: judge timeout {judge_timeout!r} is not an integer — "
                      "using 240s", file=sys.stderr)
                judge_timeout = 240
            judge = Judge(agent=judge_agent, model=judge_model, timeout=judge_timeout)
            if not judge.available():
                print(f"warning: judge runner {judge_agent!r} not on PATH — "
                      "llm_judge checks will fail.", file=sys.stderr)
        elif any(s.rubric for s in specs):
            print("note: evals have rubrics but no judge available; "
                  "pass --judge-agent or install claude.", file=sys.stderr)
    if not do_judge and any(s.rubric for s in specs):
        print("note: rubric grading is off — only deterministic assertions are graded "
              "(llm_judge checks are skipped).", file=sys.stderr)

    # pre-flight validation
    has_errors = False
    for s in specs:
        vr = validate_spec(s, available_skills=valid_skills, judge_enabled=do_judge)
        label = f"{s.name}" + (f" ({s.source_path})" if s.source_path else "")
        for e in vr.errors:
            print(f"error: {label}: {e}", file=sys.stderr)
            has_errors = True
        for w in vr.warnings:
            print(f"warning: {label}: {w}", file=sys.stderr)
    if has_errors:
        print("\nFix the errors above before running — they will always fail and waste tokens.",
              file=sys.stderr)
        return 2

    # plan + cost guardrails
    isolated = (not args.no_isolated) and (ov.get("isolated") is not False)
    provision = not args.no_provision

    # reasoning effort: CLI flag > per-target pin > per-spec `reasoning_effort:`. Warn up
    # front when the chosen runner can't honor it — the run proceeds with the runner's
    # default effort.
    if not get_adapter(agent).supports_reasoning_effort:
        wanted = (args.reasoning_effort
                  or next((t.reasoning_effort for t in targets if t.reasoning_effort), None)
                  or next((s.reasoning_effort for s in specs if s.reasoning_effort), None))
        if wanted:
            print(f"warning: runner {agent!r} has no reasoning-effort control — "
                  f"`reasoning_effort: {wanted}` is ignored (on antigravity pick a tiered "
                  "model id instead, e.g. gemini-3.5-flash-medium).", file=sys.stderr)
    elif args.reasoning_effort and any(t.reasoning_effort for t in targets):
        # A global flag on top of a per-model comparison collapses the comparison —
        # say so instead of silently running every column at the same effort.
        print(f"note: --reasoning-effort {args.reasoning_effort} overrides the per-model "
              "efforts pinned in the target — every cell runs at that effort.",
              file=sys.stderr)

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
    n_cells = n_eligible * len(targets)
    model_labels = [t.label for t in targets]
    judge_label = f"{judge.agent}/{judge.model or 'default'}" if judge else "off"
    n_llm = n_cells * (2 if judge else 1)

    run_id = args.run_id or _default_run_id(args, scenario, specs)
    run_dir = os.path.join(os.path.abspath(args.artifacts), run_id)

    # Only shown when set somewhere — an unset effort keeps the plan line unchanged. A runner
    # without an effort control shows nothing: the run proceeds at its own default (warned
    # above), so the plan must not claim a budget that won't be applied.
    efforts: list[str] = []
    if get_adapter(agent).supports_reasoning_effort:
        efforts = sorted({e for e in ((args.reasoning_effort or t.reasoning_effort
                                       or s.reasoning_effort)
                                      for t in targets for s in specs) if e})
    effort_label = f"   effort: {'/'.join(efforts)}" if efforts else ""

    print(f"Plan: {n_cells} cell(s) — {n_eligible} eval(s) × {agent} "
          f"[{', '.join(model_labels)}]")
    print(f"      judge: {judge_label}   isolated: {'on' if isolated else 'off'}"
          f"{effort_label}   ≈{n_llm} LLM calls   artifacts: {run_dir}\n")

    if args.dry_run:
        _print_skill_visibility(specs, agent, isolated, skills_root, provision)
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

    # build a concise command string for the summary heading
    cmd_parts = ["run"]
    if args.config:
        cmd_parts.append(f"--config {os.path.basename(args.config)}")
    elif args.skill:
        cmd_parts.append(f"--skill {args.skill}")
    elif args.evals:
        cmd_parts.append(f"--evals {' '.join(os.path.basename(e) for e in args.evals)}")
    cmd_parts.append(f"--agent {agent}")
    if cli_targets:
        cmd_parts.append(f"--model {','.join(t.label for t in cli_targets)}")
    if args.reasoning_effort:
        cmd_parts.append(f"--reasoning-effort {args.reasoning_effort}")
    command = " ".join(cmd_parts)

    with Progress(total_cells=n_cells) as progress:
        runner = Runner(
            agent,
            targets=targets,
            artifacts_root=os.path.abspath(args.artifacts),
            run_id=run_id,
            skills_root=skills_root,
            judge=judge,
            provision=provision,
            auto_approve=not args.no_auto_approve,
            reasoning_effort=args.reasoning_effort,
            jobs=jobs,
            isolated=isolated,
            progress=progress,
            command=command,
        )

        results = runner.run(specs)

    print(render_matrix(results, agent, targets))
    graded = [c for c in results if not c.ungraded]
    n_pass = sum(1 for c in graded if c.passed)
    ung = len(results) - len(graded)
    line = f"\n{n_pass}/{len(graded)} cells passed"
    if ung:
        line += f"   ({ung} ungraded — rubric-only evals with no judge)"

    # cost total — agent and judge separated
    def _sum_cost(rrs):
        usd = sum(r.cost_usd for r in rrs if r and r.cost_usd)
        req = sum(r.premium_requests for r in rrs if r and r.premium_requests)
        parts = []
        if usd:
            parts.append(f"${usd:.4f}")
        if req:
            parts.append(f"{req:.2f}req")
        return " / ".join(parts)

    agent_cost = _sum_cost(c.run_result for c in results)
    judge_cost = _sum_cost(c.judge_run_result for c in results if c.judge_run_result)
    if agent_cost and judge_cost:
        line += f"   cost: agent {agent_cost} + judge {judge_cost}"
    elif agent_cost:
        line += f"   cost: {agent_cost}"

    print(f"{line}   (details: {runner.run_dir}/summary.md)")

    if args.verbose:
        for c in results:
            if not c.passed and not c.ungraded:
                print(f"\n✗ {c.eval_name} [{c.agent}/{c.target_label}]"
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
                print(f"  {_cell_text(c):<10} {c.eval_name} [{c.target_label}]")
                print(f"    → {os.path.join(c.artifacts_dir, 'report.md')}")

    failed = [c for c in graded if not c.passed]
    if not graded:
        # Nothing was graded (rubric-only evals with the judge off, or no eligible cells) —
        # exit 3 so CI can tell "no verdict" apart from a real failure (1) without falsely
        # claiming success (0).
        print("\nnote: no cells were graded — exiting 3 (no verdict; not a failure).",
              file=sys.stderr)
        return 3
    return 0 if not failed else 1


def cmd_list_configured_agents(args) -> int:
    skills_root = _resolve_skills_root(getattr(args, "skills_root", None))
    config_path = os.path.abspath(
        getattr(args, "models_config", None) or _default_config_path(skills_root))
    cfg = _load_models_config(config_path)
    if cfg.load_error:
        print(f"warning: could not load {config_path} ({cfg.load_error}); "
              "showing each runner's CLI default.", file=sys.stderr)
    for w in cfg.warnings:
        print(f"warning: models.yaml: {w}", file=sys.stderr)

    print(f"config: {config_path}\n")
    all_defaults = [cfg.default(a.name) or "(cli default)" for a in all_adapters()]
    dflt_w = max(len("DEFAULT MODEL"), max(len(d) for d in all_defaults)) + 2
    print(f"{'RUNNER':<14}{'BINARY':<10}{'INSTALLED':<11}{'DEFAULT MODEL':<{dflt_w}}CONFIGURED MODELS")
    for a, dflt in zip(all_adapters(), all_defaults):
        models = cfg.models(a.name)
        models_str = ", ".join(models) if models else "(cli default)"
        avail = "yes" if a.is_available() else "no"
        print(f"{a.name:<14}{a.binary:<10}{avail:<11}{dflt:<{dflt_w}}{models_str}")
    print("\nThis shows what models.yaml declares. Use `list-agents-available-models` to probe the CLIs.")
    return 0


def cmd_list_available_agents(args) -> int:
    skills_root = _resolve_skills_root(getattr(args, "skills_root", None))
    config_path = os.path.abspath(
        getattr(args, "models_config", None) or _default_config_path(skills_root))
    cfg = _load_models_config(config_path)

    installed = [a for a in all_adapters() if a.is_available()]
    if not installed:
        print("No agent CLIs found on PATH.", file=sys.stderr)
        return 2

    configured_counts = {a.name: len(cfg.models(a.name)) for a in installed}
    total_probes = sum(configured_counts.values())
    runners_str = ", ".join(f"{a.name} ({configured_counts[a.name]})" for a in installed)

    skip_confirm = getattr(args, "yes", False)
    if not skip_confirm:
        print("This will probe each installed CLI to verify which models are accepted.")
        print(f"Each probe sends a trivial prompt ('say ok') to the model — this has a small cost.\n")
        has_list_cmd = [a.name for a in installed if a.has_model_list]
        print(f"Runners to probe: {runners_str}")
        print(f"Total probes: {total_probes} configured model(s)")
        print(f"Runners with a model-list command (free discovery): "
              + (", ".join(has_list_cmd) if has_list_cmd else "(none)"))
        print()
        try:
            answer = input("Proceed? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\naborted.", file=sys.stderr)
            return 1
        if answer not in ("y", "yes"):
            print("aborted.")
            return 1
        print()

    discrepancies: list[str] = []

    for a in installed:
        configured = cfg.models(a.name)
        configured_set = set(configured) if configured else set()

        # 1. discover_models() — free list from CLIs that support it
        discovered = a.discover_models()

        # 2. probe each configured model
        ok, rejected = [], []
        total_cost_usd = 0.0
        total_premium_req = 0.0
        if configured:
            for m in configured:
                print(f"  {a.name}: probing {m} ...", end="", flush=True)
                result = a.probe_model(m)
                if result.accepted:
                    ok.append(m)
                    cost_tag = f" ({result.cost_str})" if result.cost_str else ""
                    print(f" ok{cost_tag}")
                    if result.cost_usd:
                        total_cost_usd += result.cost_usd
                    if result.premium_requests:
                        total_premium_req += result.premium_requests
                else:
                    rejected.append(m)
                    print(" REJECTED")

        if rejected:
            discrepancies.append(
                f"  {a.name}: configured but rejected by CLI: {', '.join(rejected)}")

        # 3. report discovered models not in config
        if discovered:
            extra = [m for m in discovered if m not in configured_set]
            if extra:
                discrepancies.append(
                    f"  {a.name}: available in CLI but not in models.yaml: {', '.join(extra)}")
            print(f"\n  {a.name} — {len(discovered)} model(s) from `{a.binary} models`:")
            for m in discovered:
                tag = "  <- not in models.yaml" if m not in configured_set else ""
                print(f"    {m}{tag}")
        else:
            n = len(ok) + len(rejected)
            print(f"\n  {a.name} — no model-list command; probed {n} configured model(s)")

        cost_parts = []
        if total_cost_usd > 0:
            cost_parts.append(f"${total_cost_usd:.4f}")
        if total_premium_req > 0:
            cost_parts.append(f"{total_premium_req:.2f} premium requests")
        if cost_parts:
            print(f"  probe cost for {a.name}: {' / '.join(cost_parts)}")
        print()

    # -- summary ------------------------------------------------------------
    print(f"config: {config_path}")
    if discrepancies:
        print("\n=== discrepancies ===")
        for d in discrepancies:
            print(d)
        print(f"\nUpdate {config_path} to match, then re-run.")
    else:
        print("\nNo discrepancies — models.yaml has complete coverage of all installed CLIs.")

    return 0


def cmd_list_evals(args) -> int:
    skills_root = _resolve_skills_root(args.skills_root)
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
    skills_root = _resolve_skills_root(args.skills_root)
    superset = skill_names(skills_root)
    repo = set(superset)
    real_home = os.path.expanduser("~")

    print(f"provisionable skills (superset) under {skills_root}  ({len(superset)}):")
    print("  " + (", ".join(superset) if superset else "(none)"))

    print("\nper-runner global skills — what an un-isolated run also sees:")
    for a in all_adapters():
        vis = resolve_visible_skills(a, (), repo, isolated=True, real_home=real_home,
                                     repo_root=repo_root_for(skills_root))
        masked, vendor = vis["masked"], vis["vendor"]
        dirs = list(a.global_skills_subpaths or []) + list(
            getattr(a, "global_plugin_registry_subpaths", []) or [])
        print(f"\n{a.name}:")
        print("  dirs: " + (", ".join("~/" + d for d in dirs) if dirs else "(none)"))
        print(f"  repo skills (masked under isolation): {', '.join(masked) or '(none)'}")
        print(f"  vendor/other (kept):                  {', '.join(vendor) or '(none)'}")
        present = set(masked)
        missing_skills = sorted(repo - present)
        if present and missing_skills:
            print(f"  ⚠ drift: global is missing {', '.join(missing_skills)} "
                  "(run `make link-global` to refresh)")

    print("\nWith isolation ON (default) normal skill discovery sees only the skills a run "
          "provisions + vendor skills; with --no-isolated it also sees the repo skills above.")
    print("Note: skills bundled inside a CLI's own package aren't listed here; skills nested\n"
          "in a tracked plugin registry (see 'dirs' above) are, folded into vendor/masked.")
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
                        help="dir containing skill folders (each with an evals/); "
                             "auto-descends into skills_examples/ if present. Default: cwd")
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
    sp.add_argument("--judge-timeout", type=int, default=None,
                    help="seconds before a judge run is killed (default 240; also settable "
                         "as judge.timeout in models.yaml or a scenario's judge: block)")
    sp.add_argument("--no-judge", action="store_true", help="disable LLM-judge rubric grading")
    sp.add_argument("--no-provision", action="store_true", help="don't copy skills into workspaces")
    sp.add_argument("--no-isolated", action="store_true",
                    help="also expose your globally-installed repo skills (default: each run is "
                         "isolated so normal skill discovery sees only provisioned skills + the "
                         "agent's vendor skills)")
    sp.add_argument("--no-auto-approve", action="store_true",
                    help="don't auto-approve tool/file actions")
    sp.add_argument("--model", help="model(s) to run, comma-separated; overrides models.yaml. "
                    "Append @low|@medium|@high to pin a per-model reasoning effort so one "
                    "run can compare efforts, e.g. "
                    "--model claude-haiku-4.5@high,claude-opus-4.6@low")
    sp.add_argument("--reasoning-effort", choices=list(REASONING_EFFORT_LEVELS),
                    help="thinking/reasoning budget for ALL agent runs; overrides per-model "
                         "@effort pins and an eval/scenario's `reasoning_effort:`. Mapped to "
                         "the runner's native control (claude --effort, codex "
                         "model_reasoning_effort, copilot --reasoning-effort); ignored with "
                         "a warning by runners without one (antigravity encodes effort in "
                         "the model id tier instead)")
    sp.add_argument("--models-config", help="models.yaml path (default: models.yaml under the skills-root, else the repo root)")
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

    sp = sub.add_parser("list-agents-configured-models",
                        help="show runners and their configured models from models.yaml")
    sp.add_argument("--skills-root", default=_default_skills_root(),
                    help="dir to look for models.yaml (default: cwd)")
    sp.add_argument("--models-config", help="models.yaml path (default: models.yaml under the skills-root, else the repo root)")
    sp.set_defaults(func=cmd_list_configured_agents)

    sp = sub.add_parser("list-agents-available-models",
                        help="probe installed CLIs to discover available models and check config")
    sp.add_argument("--skills-root", default=_default_skills_root(),
                    help="dir to look for models.yaml (default: cwd)")
    sp.add_argument("--models-config", help="models.yaml path (default: models.yaml under the skills-root, else the repo root)")
    sp.add_argument("-y", "--yes", action="store_true",
                    help="skip the confirmation prompt")
    sp.set_defaults(func=cmd_list_available_agents)

    sp = sub.add_parser("list-evals", help="discover and list evals")
    add_discovery(sp)
    sp.set_defaults(func=cmd_list_evals)

    sp = sub.add_parser("list-skills",
                        help="audit skill visibility (superset + per-runner masked/kept + drift)")
    sp.add_argument("--skills-root", default=_default_skills_root(),
                    help="dir containing skill folders; auto-descends into "
                         "skills_examples/ if present (default: cwd)")
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
