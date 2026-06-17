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
from .judge import Judge
from .runner import Runner, render_matrix
from .spec import discover_specs, load_spec


def _default_skills_root() -> str:
    return os.getcwd()


def _parse_models(pairs: list[str], agents: list[str]) -> dict[str, str]:
    """--model accepts `agent=model` (repeatable) or a bare `model` for all."""
    out: dict[str, str] = {}
    for p in pairs or []:
        if "=" in p:
            a, m = p.split("=", 1)
            out[a.strip()] = m.strip()
        else:
            for a in agents:
                out[a] = p.strip()
    return out


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_run(args) -> int:
    skills_root = os.path.abspath(args.skills_root)
    specs = discover_specs(skills_root=skills_root, skill=args.skill, paths=args.evals)
    if args.tag:
        specs = [s for s in specs if set(args.tag) & set(s.tags)]
    if not specs:
        print("no evals found. Looked under per-skill evals/ dirs in "
              f"{skills_root!r} (use --skill / --evals to target).", file=sys.stderr)
        return 2

    # agents: explicit, else all installed
    if args.agents:
        agents = args.agents
    else:
        agents = [a.name for a in all_adapters() if a.is_available()]
        if not agents:
            print("no agent CLIs found on PATH. Install one or pass --agents.", file=sys.stderr)
            return 2

    # warn about unavailable agents
    for a in agents:
        if not get_adapter(a).is_available():
            print(f"warning: agent {a!r} not on PATH — its cells will be marked ERR.",
                  file=sys.stderr)

    # judge
    judge = None
    if not args.no_judge:
        judge_agent = args.judge_agent or ("claude" if get_adapter("claude").is_available() else None)
        if judge_agent:
            judge = Judge(agent=judge_agent, model=args.judge_model)
            if not judge.available():
                print(f"warning: judge agent {judge_agent!r} not on PATH — "
                      "llm_judge checks will fail.", file=sys.stderr)
        elif any(s.rubric for s in specs):
            print("note: evals have rubrics but no judge available; "
                  "pass --judge-agent or install claude.", file=sys.stderr)

    run_id = args.run_id or _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    runner = Runner(
        agents,
        artifacts_root=os.path.abspath(args.artifacts),
        run_id=run_id,
        skills_root=skills_root,
        judge=judge,
        provision=not args.no_provision,
        auto_approve=not args.no_auto_approve,
        jobs=args.jobs,
        model_map=_parse_models(args.model, agents),
    )

    print(f"running {len(specs)} eval(s) × {len(agents)} agent(s) = "
          f"{sum(1 for s in specs for a in agents if s.agents is None or a in s.agents)} cells")
    print(f"agents: {', '.join(agents)}    judge: {judge.agent if judge else 'off'}")
    print(f"artifacts: {runner.run_dir}\n")

    results = runner.run(specs)

    print(render_matrix(results, agents))
    n_pass = sum(1 for c in results if c.passed)
    print(f"\n{n_pass}/{len(results)} cells passed   (details: {runner.run_dir}/summary.md)")

    if args.verbose:
        for c in results:
            if not c.passed:
                print(f"\n✗ {c.eval_name} [{c.agent}]"
                      + (f"  ERROR: {c.run_result.error}" if c.run_result.error else ""))
                for a in c.assertions:
                    if not a.passed:
                        print(f"    - {a.type}: {a.message}")

    return 0 if n_pass == len(results) else 1


def cmd_list_agents(args) -> int:
    print(f"{'AGENT':<14}{'BINARY':<12}{'AVAILABLE':<10}SKILLS DIR")
    for a in all_adapters():
        print(f"{a.name:<14}{a.binary:<12}{'yes' if a.is_available() else 'no':<10}{a.skills_subdir}")
    return 0


def cmd_list_evals(args) -> int:
    skills_root = os.path.abspath(args.skills_root)
    specs = discover_specs(skills_root=skills_root, skill=args.skill, paths=args.evals)
    if not specs:
        print("no evals found.", file=sys.stderr)
        return 2
    print(f"{'SKILL':<26}{'EVAL':<34}{'CHECKS':<8}AGENTS")
    for s in specs:
        n = len(s.effective_assertions())
        agents = ",".join(s.agents) if s.agents else "all"
        print(f"{(s.skill_name or '-'):<26}{s.name:<34}{n:<8}{agents}")
    print(f"\n{len(specs)} eval(s)")
    return 0


def cmd_migrate(args) -> int:
    skills_root = os.path.abspath(args.skills_root)
    specs = discover_specs(skills_root=skills_root, skill=args.skill, paths=args.evals)
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

    sp = sub.add_parser("run", help="run the agent × eval matrix")
    add_discovery(sp)
    sp.add_argument("--agents", nargs="*", help=f"agents to test (default: all installed). "
                    f"Known: {', '.join(adapter_names())}")
    sp.add_argument("--artifacts", default="artifacts", help="artifacts root dir")
    sp.add_argument("--run-id", help="name this run (default: timestamp)")
    sp.add_argument("--jobs", type=int, default=1, help="parallel cells (default 1)")
    sp.add_argument("--judge-agent", help="agent to grade rubrics (default: claude if installed)")
    sp.add_argument("--judge-model", help="model override for the judge")
    sp.add_argument("--no-judge", action="store_true", help="disable LLM-judge rubric grading")
    sp.add_argument("--no-provision", action="store_true", help="don't copy skills into workspaces")
    sp.add_argument("--no-auto-approve", action="store_true",
                    help="don't auto-approve tool/file actions")
    sp.add_argument("--model", nargs="*", default=[],
                    help="model override: `agent=model` (repeatable) or bare `model` for all")
    sp.add_argument("--tag", nargs="*", help="only evals with one of these tags")
    sp.add_argument("-v", "--verbose", action="store_true", help="print failing assertions")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("list-agents", help="show adapters and availability")
    sp.set_defaults(func=cmd_list_agents)

    sp = sub.add_parser("list-evals", help="discover and list evals")
    add_discovery(sp)
    sp.set_defaults(func=cmd_list_evals)

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
