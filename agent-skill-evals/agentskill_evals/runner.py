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

import json
import os
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Optional

from .adapters import get_adapter
from .adapters.base import Adapter, RunOptions
from .assertions import AssertionContext, AssertionResult, run_assertion
from .exec import execute
from .judge import Judge
from .schema import RunResult
from .spec import EvalSpec


@dataclass
class CellResult:
    agent: str
    eval_name: str
    skill: Optional[str]
    passed: bool
    run_result: RunResult
    assertions: list[AssertionResult] = field(default_factory=list)
    artifacts_dir: str = ""

    @property
    def n_pass(self) -> int:
        return sum(1 for a in self.assertions if a.passed)

    @property
    def n_total(self) -> int:
        return len(self.assertions)


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
        model_map: Optional[dict[str, str]] = None,
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
        self.run_dir = os.path.join(artifacts_root, run_id)

    # --- public -------------------------------------------------------------

    def run(self, specs: list[EvalSpec]) -> list[CellResult]:
        os.makedirs(self.run_dir, exist_ok=True)
        cells = [
            (agent, spec)
            for spec in specs
            for agent in self.agents
            if self._eligible(agent, spec)
        ]

        results: list[CellResult] = []
        if self.jobs == 1:
            for agent, spec in cells:
                results.append(self._run_cell(agent, spec))
        else:
            with ThreadPoolExecutor(max_workers=self.jobs) as pool:
                futs = {pool.submit(self._run_cell, a, s): (a, s) for a, s in cells}
                for fut in as_completed(futs):
                    results.append(fut.result())

        results.sort(key=lambda c: (c.eval_name, c.agent))
        self._write_summary(results, specs)
        return results

    # --- internals ----------------------------------------------------------

    def _eligible(self, agent: str, spec: EvalSpec) -> bool:
        return spec.agents is None or agent in spec.agents

    def _run_cell(self, agent: str, spec: EvalSpec) -> CellResult:
        adapter = self.adapters[agent]
        cell_dir = os.path.join(self.run_dir, agent, _safe(spec.skill_name or "_"), _safe(spec.name))
        workspace = os.path.join(cell_dir, "workspace")
        os.makedirs(workspace, exist_ok=True)

        # 1) provision skills + 2) seed files
        if self.provision:
            self._provision(adapter, workspace, spec)
        self._seed_files(workspace, spec)

        # 3) render prompt (fill {skill}/{skills} for this adapter)
        prompt = self._render_prompt(adapter, spec)

        # 4) run
        opts = RunOptions(
            model=self.model_map.get(agent),
            auto_approve=self.auto_approve,
            output_schema=spec.output_schema,
        )
        ex = execute(
            adapter, prompt, opts,
            cwd=workspace, timeout=spec.timeout_sec,
            env_overrides=spec.env, agent_name=agent, eval_name=spec.name,
        )
        rr = ex.result

        # 5) artifacts
        self._write_artifacts(cell_dir, ex.stdout, ex.stderr, rr)

        # 6) assertions
        ctx = AssertionContext(spec=spec, judge=self.judge)
        checks = [
            run_assertion(cfg, rr, workspace, spec, ctx)
            for cfg in spec.effective_assertions()
        ]
        clean = rr.error is None and not rr.timed_out
        # With assertions: clean run AND every check passes. Without any
        # assertions: pass == the agent ran cleanly.
        passed = (clean and all(c.passed for c in checks)) if checks else clean

        cell = CellResult(
            agent=agent, eval_name=spec.name, skill=spec.skill_name,
            passed=passed, run_result=rr, assertions=checks, artifacts_dir=cell_dir,
        )
        self._write_cell_json(cell_dir, cell)
        return cell

    def _provision(self, adapter: Adapter, workspace: str, spec: EvalSpec) -> None:
        skill_dirs = []
        for name in spec.skills:
            d = os.path.join(self.skills_root, name)
            if os.path.isdir(d):
                skill_dirs.append(d)
        if skill_dirs:
            adapter.provision_skills(workspace, skill_dirs)

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
                "eval": cell.eval_name,
                "skill": cell.skill,
                "passed": cell.passed,
                "assertions": [
                    {"type": a.type, "passed": a.passed, "kind": a.kind,
                     "message": a.message, "details": a.details}
                    for a in cell.assertions
                ],
            },
        )

    def _write_summary(self, results: list[CellResult], specs: list[EvalSpec]) -> None:
        summary = {
            "run_id": self.run_id,
            "agents": self.agents,
            "n_evals": len(specs),
            "n_cells": len(results),
            "n_passed": sum(1 for c in results if c.passed),
            "judge_agent": self.judge.agent if self.judge else None,
            "cells": [
                {
                    "agent": c.agent, "eval": c.eval_name, "skill": c.skill,
                    "passed": c.passed, "n_pass": c.n_pass, "n_total": c.n_total,
                    "error": c.run_result.error, "timed_out": c.run_result.timed_out,
                    "cost_usd": c.run_result.cost_usd,
                    "artifacts": os.path.relpath(c.artifacts_dir, self.run_dir),
                }
                for c in results
            ],
        }
        _write_json(os.path.join(self.run_dir, "summary.json"), summary)
        _write(os.path.join(self.run_dir, "summary.md"), render_markdown(results, self.agents))


# ---------------------------------------------------------------------------
# Reporting helpers (also used by the CLI for stdout)
# ---------------------------------------------------------------------------

def render_matrix(results: list[CellResult], agents: list[str]) -> str:
    """A compact eval × agent pass/fail grid for the terminal."""
    by_eval: dict[str, dict[str, CellResult]] = {}
    for c in results:
        by_eval.setdefault(c.eval_name, {})[c.agent] = c
    eval_w = max([len("EVAL")] + [len(e) for e in by_eval]) + 2
    header = "EVAL".ljust(eval_w) + "".join(a[:10].center(12) for a in agents)
    lines = [header, "-" * len(header)]
    for ev in sorted(by_eval):
        row = ev.ljust(eval_w)
        for a in agents:
            c = by_eval[ev].get(a)
            if c is None:
                cell = "-"
            elif c.run_result.error:
                cell = "ERR"
            elif c.passed:
                cell = f"PASS {c.n_pass}/{c.n_total}"
            else:
                cell = f"FAIL {c.n_pass}/{c.n_total}"
            row += cell.center(12)
        lines.append(row)
    return "\n".join(lines)


def render_markdown(results: list[CellResult], agents: list[str]) -> str:
    by_eval: dict[str, dict[str, CellResult]] = {}
    for c in results:
        by_eval.setdefault(c.eval_name, {})[c.agent] = c
    lines = ["# Eval results", "", "| eval | " + " | ".join(agents) + " |",
             "|" + "---|" * (len(agents) + 1)]
    for ev in sorted(by_eval):
        cells = []
        for a in agents:
            c = by_eval[ev].get(a)
            if c is None:
                cells.append("–")
            elif c.run_result.error:
                cells.append(f"⚠️ {c.run_result.error}")
            else:
                mark = "✅" if c.passed else "❌"
                cells.append(f"{mark} {c.n_pass}/{c.n_total}")
        lines.append(f"| {ev} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# small fs helpers
# ---------------------------------------------------------------------------

def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text or "")


def _write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, default=str)
