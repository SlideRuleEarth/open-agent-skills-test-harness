"""The runner: execute a matrix of (agent × eval), assert, and report.

For each cell it:
  1. builds a hermetic workspace (optionally from a fixture),
  2. provisions the eval's skill(s) into the agent's skills dir,
  3. seeds any declared `files`,
  4. renders the prompt (filling {skill}/{skills} per adapter),
  5. runs the agent via exec.execute(),
  6. writes raw + normalized artifacts,
  7. runs every effective assertion (deterministic + rubric/judge),
  8. records a CellResult.

Results stream to artifacts/<run_id>/ and a summary table is returned for the CLI.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Optional

from .adapters import get_adapter
from .adapters.base import Adapter, RunOptions
from .assertions import AssertionContext, AssertionResult, run_assertion
from .exec import execute
from .isolation import build_isolated_home
from .judge import Judge
from .schema import RunResult
from .spec import EvalSpec, skill_names


@dataclass
class CellResult:
    agent: str
    model: Optional[str]      # the model id this cell ran (None == the runner's own default)
    eval_name: str
    skill: Optional[str]
    passed: bool
    run_result: RunResult
    assertions: list[AssertionResult] = field(default_factory=list)
    artifacts_dir: str = ""
    isolated: bool = False     # ran against an isolated HOME (only provisioned + vendor skills visible)
    ungraded: bool = False     # ran clean but nothing graded it (rubric-only eval, judge off)

    @property
    def n_pass(self) -> int:
        return sum(1 for a in self.assertions if a.passed)

    @property
    def n_total(self) -> int:
        return len(self.assertions)

    @property
    def model_label(self) -> str:
        return _model_seg(self.model)


class Runner:
    def __init__(
        self,
        agents: list[str],
        *,
        artifacts_root: str,
        run_id: str,
        skills_root: str,
        judge: Optional[Judge] = None,
        provision: bool = True,
        auto_approve: bool = True,
        jobs: int = 1,
        model_map: Optional[dict[str, list[Optional[str]]]] = None,
        isolated: bool = True,
    ):
        self.agents = agents
        self.adapters: dict[str, Adapter] = {a: get_adapter(a) for a in agents}
        self.artifacts_root = artifacts_root
        self.run_id = run_id
        self.skills_root = skills_root
        self.judge = judge
        self.provision = provision
        self.auto_approve = auto_approve
        self.jobs = max(1, jobs)
        self.model_map = model_map or {}
        self.isolated = isolated
        # the repo's provisionable skills — masked from each runner's global skills dirs
        # under isolation so a cell sees only what it provisions (+ the surface's vendor skills).
        self._repo_skill_names = set(skill_names(skills_root))
        self.run_dir = os.path.join(artifacts_root, run_id)

    # --- public -------------------------------------------------------------

    def run(self, specs: list[EvalSpec]) -> list[CellResult]:
        os.makedirs(self.run_dir, exist_ok=True)
        cells = [
            (agent, model, spec)
            for spec in specs
            for agent in self.agents
            if self._eligible(agent, spec)
            for model in (self.model_map.get(agent) or [None])
        ]

        results: list[CellResult] = []
        if self.jobs == 1:
            for agent, model, spec in cells:
                results.append(self._run_cell(agent, model, spec))
        else:
            with ThreadPoolExecutor(max_workers=self.jobs) as pool:
                futs = {pool.submit(self._run_cell, a, m, s): (a, m, s) for a, m, s in cells}
                for fut in as_completed(futs):
                    results.append(fut.result())

        results.sort(key=lambda c: (c.eval_name, c.agent, c.model or ""))
        self._write_summary(results, specs)
        return results

    # --- internals ----------------------------------------------------------

    def _eligible(self, agent: str, spec: EvalSpec) -> bool:
        return spec.agents is None or agent in spec.agents

    def _run_cell(self, agent: str, model: Optional[str], spec: EvalSpec) -> CellResult:
        adapter = self.adapters[agent]
        cell_dir = os.path.join(
            self.run_dir, agent, _model_seg(model),
            _safe(spec.skill_name or "_"), _safe(spec.name)
        )
        workspace = os.path.join(cell_dir, "workspace")
        os.makedirs(workspace, exist_ok=True)

        # 1) provision skills + 2) seed files
        declared_dirs = self._skill_dirs(spec)
        if self.provision and declared_dirs:
            adapter.provision_skills(workspace, declared_dirs)
        self._seed_files(workspace, spec)

        # 3) render prompt (fill {skill}/{skills} for this adapter)
        prompt = self._render_prompt(adapter, spec)

        # 4) isolate HOME so the model sees only the provisioned skills (+ the surface's
        #    vendor skills), not this repo's globally-installed skills. Mirror the real HOME;
        #    mask only the global skills dirs. On failure, fall back to a non-isolated run.
        iso_home = None
        isolated = False
        if self.isolated and adapter.global_skills_subpaths:
            iso_home = tempfile.mkdtemp(prefix="ase-home-")
            try:
                build_isolated_home(
                    iso_home, adapter.global_skills_subpaths, self._repo_skill_names,
                    declared_dirs if self.provision else [], os.path.expanduser("~"),
                )
                isolated = True
            except OSError as exc:
                print(f"warning: [{agent}] skill isolation unavailable ({exc}); "
                      "running non-isolated.", file=sys.stderr)
                shutil.rmtree(iso_home, ignore_errors=True)
                iso_home = None

        # 5) run
        opts = RunOptions(
            model=model,
            auto_approve=self.auto_approve,
            output_schema=spec.output_schema,
            home=iso_home,
        )
        try:
            ex = execute(
                adapter, prompt, opts,
                cwd=workspace, timeout=spec.timeout_sec,
                env_overrides=spec.env, agent_name=agent, eval_name=spec.name,
            )
        finally:
            if iso_home:
                shutil.rmtree(iso_home, ignore_errors=True)
        rr = ex.result

        # A rolled-off / mistyped model id surfaces as a run error; point the
        # maintainer back to the config rather than leaving a raw CLI stderr.
        if model and rr.error and _looks_like_model_error(rr.error, ex.stderr):
            rr.error = f"model {model!r} rejected by {agent} — check models.yaml: {rr.error}"

        # 6) artifacts
        self._write_artifacts(cell_dir, ex.stdout, ex.stderr, rr)

        # 7) assertions. With no judge active, skip llm_judge checks rather than failing
        #    them — a --no-judge run is graded on its deterministic assertions only.
        effective = spec.effective_assertions()
        skipped_judge = self.judge is None and any(
            c.get("type") == "llm_judge" for c in effective)
        ctx = AssertionContext(spec=spec, judge=self.judge)
        checks = [
            run_assertion(cfg, rr, workspace, spec, ctx)
            for cfg in effective
            if not (self.judge is None and cfg.get("type") == "llm_judge")
        ]
        clean = rr.error is None and not rr.timed_out
        # "ungraded": ran clean but the only checks were judge checks we skipped — nothing
        # actually graded the behavior, so it's neither a pass nor a fail. A genuinely
        # assertion-less eval (no rubric either) stays a clean-run smoke-test pass.
        ungraded = clean and not checks and skipped_judge
        passed = False if ungraded else (
            (clean and all(c.passed for c in checks)) if checks else clean)

        cell = CellResult(
            agent=agent, model=model, eval_name=spec.name, skill=spec.skill_name,
            passed=passed, run_result=rr, assertions=checks, artifacts_dir=cell_dir,
            isolated=isolated, ungraded=ungraded,
        )
        self._write_cell_json(cell_dir, cell)
        return cell

    def _skill_dirs(self, spec: EvalSpec) -> list[str]:
        """Source dirs for the eval's declared skills — real skill dirs (with a SKILL.md)
        under skills_root, so a non-skill folder is never provisioned."""
        dirs = []
        for name in spec.skills:
            d = os.path.join(self.skills_root, name)
            if os.path.isfile(os.path.join(d, "SKILL.md")):
                dirs.append(d)
        return dirs

    def _seed_files(self, workspace: str, spec: EvalSpec) -> None:
        fixture = spec.resolved_fixture()
        if fixture and os.path.isdir(fixture):
            shutil.copytree(fixture, workspace, dirs_exist_ok=True)
        for src, dest_rel in spec.resolved_files():
            if os.path.isfile(src):
                dest = os.path.join(workspace, dest_rel)
                os.makedirs(os.path.dirname(dest) or workspace, exist_ok=True)
                shutil.copy2(src, dest)

    def _render_prompt(self, adapter: Adapter, spec: EvalSpec) -> str:
        prompt = spec.rendered_prompt()
        primary = spec.skills[0] if spec.skills else (spec.skill_name or "")
        if primary:
            prompt = prompt.replace("{skill}", adapter.format_skill(primary))
        if spec.skills:
            joined = ", ".join(adapter.format_skill(s) for s in spec.skills)
            prompt = prompt.replace("{skills}", joined)
        return prompt

    # --- artifact writers ---------------------------------------------------

    def _write_artifacts(self, cell_dir: str, stdout: str, stderr: str, rr: RunResult) -> None:
        os.makedirs(cell_dir, exist_ok=True)
        _write(os.path.join(cell_dir, "stdout.jsonl"), stdout)
        _write(os.path.join(cell_dir, "stderr.txt"), stderr)
        rr.stdout_path = os.path.join(cell_dir, "stdout.jsonl")
        rr.stderr_path = os.path.join(cell_dir, "stderr.txt")
        _write_json(os.path.join(cell_dir, "events.json"), [e.to_dict() for e in rr.events])
        _write_json(os.path.join(cell_dir, "result.json"), rr.to_dict())

    def _write_cell_json(self, cell_dir: str, cell: CellResult) -> None:
        _write_json(
            os.path.join(cell_dir, "assertions.json"),
            {
                "agent": cell.agent,
                "model": cell.model,
                "eval": cell.eval_name,
                "skill": cell.skill,
                "isolated": cell.isolated,
                "ungraded": cell.ungraded,
                "passed": cell.passed,
                "assertions": [
                    {"type": a.type, "passed": a.passed, "kind": a.kind,
                     "message": a.message, "details": a.details}
                    for a in cell.assertions
                ],
            },
        )

    def _write_summary(self, results: list[CellResult], specs: list[EvalSpec]) -> None:
        targets = [
            {"agent": a, "model": m}
            for a in self.agents
            for m in (self.model_map.get(a) or [None])
        ]
        summary = {
            "run_id": self.run_id,
            "agents": self.agents,
            "targets": targets,
            "isolated": self.isolated,
            "n_evals": len(specs),
            "n_cells": len(results),
            "n_passed": sum(1 for c in results if c.passed),
            "judge_agent": self.judge.agent if self.judge else None,
            "judge_model": self.judge.model if self.judge else None,
            "cells": [
                {
                    "agent": c.agent, "model": c.model, "eval": c.eval_name, "skill": c.skill,
                    "isolated": c.isolated, "ungraded": c.ungraded,
                    "passed": c.passed, "n_pass": c.n_pass, "n_total": c.n_total,
                    "error": c.run_result.error, "timed_out": c.run_result.timed_out,
                    "cost_usd": c.run_result.cost_usd,
                    "artifacts": os.path.relpath(c.artifacts_dir, self.run_dir),
                }
                for c in results
            ],
        }
        _write_json(os.path.join(self.run_dir, "summary.json"), summary)
        _write(os.path.join(self.run_dir, "summary.md"),
               render_markdown(results, self.agents, self.model_map))


# ---------------------------------------------------------------------------
# Reporting helpers (also used by the CLI for stdout)
# ---------------------------------------------------------------------------

ModelMap = dict[str, list[Optional[str]]]


def _targets(agents: list[str], model_map: Optional[ModelMap],
             results: list[CellResult]) -> list[tuple[str, Optional[str]]]:
    """Ordered (agent, model) columns. Prefer the declared map; fall back to
    whatever models actually appear in the results (for callers without a map)."""
    out: list[tuple[str, Optional[str]]] = []
    for a in agents:
        models = (model_map or {}).get(a)
        if not models:
            models = []
            for c in results:
                if c.agent == a and c.model not in models:
                    models.append(c.model)
            if not models:
                models = [None]
        for m in models:
            out.append((a, m))
    return out


def _target_label(agent: str, model: Optional[str]) -> str:
    return f"{agent}:{model if model else 'default'}"


def _cell_text(c: Optional[CellResult]) -> str:
    if c is None:
        return "-"
    if c.run_result.error:
        return "ERR"
    if c.ungraded:
        return "SKIP"
    return f"{'PASS' if c.passed else 'FAIL'} {c.n_pass}/{c.n_total}"


def _cell_mark(c: Optional[CellResult]) -> str:
    if c is None:
        return "–"
    if c.run_result.error:
        return f"⚠️ {c.run_result.error}"
    if c.ungraded:
        return "⚪ ungraded"
    return f"{'✅' if c.passed else '❌'} {c.n_pass}/{c.n_total}"


def render_matrix(results: list[CellResult], agents: list[str],
                  model_map: Optional[ModelMap] = None) -> str:
    """A single wide eval × (runner:model) pass/fail grid for the terminal,
    followed by a pass-rate-by-target footer."""
    by_key = {(c.eval_name, c.agent, c.model): c for c in results}
    evals = sorted({c.eval_name for c in results})
    targets = _targets(agents, model_map, results)
    labels = [_target_label(a, m) for a, m in targets]

    eval_w = max([len("EVAL")] + [len(e) for e in evals]) + 2
    col_w = max([14] + [len(l) + 2 for l in labels]) if labels else 14
    header = "EVAL".ljust(eval_w) + "".join(l.center(col_w) for l in labels)
    lines = [header, "-" * len(header)]
    for ev in evals:
        row = ev.ljust(eval_w)
        for a, m in targets:
            row += _cell_text(by_key.get((ev, a, m))).center(col_w)
        lines.append(row)

    lines += ["", "pass rate by target:"]
    for a, m in targets:
        cells = [c for c in (by_key.get((ev, a, m)) for ev in evals) if c is not None]
        graded = [c for c in cells if not c.ungraded]
        npass = sum(1 for c in graded if c.passed)
        ung = len(cells) - len(graded)
        extra = f"   ({ung} ungraded)" if ung else ""
        lines.append(f"  {_target_label(a, m):<28} {npass}/{len(graded)}{extra}")
    return "\n".join(lines)


def render_markdown(results: list[CellResult], agents: list[str],
                    model_map: Optional[ModelMap] = None) -> str:
    by_key = {(c.eval_name, c.agent, c.model): c for c in results}
    evals = sorted({c.eval_name for c in results})
    targets = _targets(agents, model_map, results)
    labels = [_target_label(a, m) for a, m in targets]

    lines = ["# Eval results", "",
             "| eval | " + " | ".join(labels) + " |",
             "|" + "---|" * (len(labels) + 1)]
    for ev in evals:
        cells = [_cell_mark(by_key.get((ev, a, m))) for a, m in targets]
        lines.append(f"| {ev} | " + " | ".join(cells) + " |")

    lines += ["", "## Pass rate by target", "", "| target | pass rate |", "|---|---|"]
    for a, m in targets:
        cells = [c for c in (by_key.get((ev, a, m)) for ev in evals) if c is not None]
        graded = [c for c in cells if not c.ungraded]
        npass = sum(1 for c in graded if c.passed)
        ung = len(cells) - len(graded)
        rate = f"{npass}/{len(graded)}" + (f" ({ung} ungraded)" if ung else "")
        lines.append(f"| {_target_label(a, m)} | {rate} |")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# small fs helpers
# ---------------------------------------------------------------------------

def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)


def _model_seg(model: Optional[str]) -> str:
    """Collision-free filesystem segment for a model id.

    `_safe` collapses '/', ':', and spaces to '_', so distinct ids (e.g. `a/b`,
    `a:b`) could map to the same dir. Append a short hash of the raw id whenever
    sanitizing changed it, so distinct ids never share a directory."""
    if not model:
        return "_default"
    safe = _safe(model)
    if safe != model:
        return f"{safe}-{hashlib.sha1(model.encode('utf-8')).hexdigest()[:6]}"
    return safe


def _looks_like_model_error(error: Optional[str], stderr: Optional[str]) -> bool:
    """Heuristic: did the CLI reject the model id (rolled off / mistyped)?"""
    blob = f"{error or ''} {stderr or ''}".lower()
    return any(k in blob for k in ("model", "not found", "unknown", "invalid", "unsupported"))


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text or "")


def _write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, default=str)
