"""The runner: execute a matrix of (model × eval) for a single agent, assert, and report.

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
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Optional

from .adapters import get_adapter
from .adapters.base import RunOptions
from .assertions import AssertionContext, AssertionResult, run_assertion
from .exec import execute
from .isolation import build_isolated_home
from .judge import Judge
from .progress import Progress
from .schema import EventKind, RunResult
from .spec import EvalSpec, skill_names
from .workspace_view import file_tree, inline_files, writes_outside_workspace


@dataclass
class CellResult:
    agent: str
    model: Optional[str]
    eval_name: str
    skill: Optional[str]
    passed: bool
    run_result: RunResult
    assertions: list[AssertionResult] = field(default_factory=list)
    judge_run_result: Optional[RunResult] = None
    artifacts_dir: str = ""
    isolated: bool = False
    ungraded: bool = False

    @property
    def n_pass(self) -> int:
        return sum(1 for a in self.assertions if a.passed)

    @property
    def n_total(self) -> int:
        return len(self.assertions)

    @property
    def det_assertions(self) -> list[AssertionResult]:
        return [a for a in self.assertions if a.kind != "judge"]

    @property
    def judge_assertions(self) -> list[AssertionResult]:
        return [a for a in self.assertions if a.kind == "judge"]

    @property
    def cost_str(self) -> str:
        agent = self.run_result.cost_str
        judge = self.judge_run_result.cost_str if self.judge_run_result else ""
        if agent and judge:
            return f"agent {agent} + judge {judge}"
        return agent

    @property
    def model_label(self) -> str:
        return _model_seg(self.model)


class Runner:
    def __init__(
        self,
        agent: str,
        *,
        models: list[Optional[str]],
        artifacts_root: str,
        run_id: str,
        skills_root: str,
        judge: Optional[Judge] = None,
        provision: bool = True,
        auto_approve: bool = True,
        jobs: int = 1,
        isolated: bool = True,
        progress: Optional[Progress] = None,
    ):
        self.agent = agent
        self.adapter = get_adapter(agent)
        self.models = models
        self.artifacts_root = artifacts_root
        self.run_id = run_id
        self.skills_root = skills_root
        self.judge = judge
        self.provision = provision
        self.auto_approve = auto_approve
        self.jobs = max(1, jobs)
        self.isolated = isolated
        self.progress = progress
        self._repo_skill_names = set(skill_names(skills_root))
        self.run_dir = os.path.join(artifacts_root, run_id)

    # --- public -------------------------------------------------------------

    def run(self, specs: list[EvalSpec]) -> list[CellResult]:
        os.makedirs(self.run_dir, exist_ok=True)
        cells = [
            (model, spec)
            for spec in specs
            if self._eligible(spec)
            for model in self.models
        ]

        results: list[CellResult] = []
        if self.jobs == 1:
            for i, (model, spec) in enumerate(cells, 1):
                results.append(self._run_cell(model, spec, cell_idx=i, total=len(cells)))
        else:
            with ThreadPoolExecutor(max_workers=self.jobs) as pool:
                futs = {pool.submit(self._run_cell, m, s): (m, s) for m, s in cells}
                for i, fut in enumerate(as_completed(futs), 1):
                    r = fut.result()
                    if self.progress:
                        self.progress.done(cell=i, passed=r.passed if not r.ungraded else None,
                                           cost=r.cost_str)
                    results.append(r)

        results.sort(key=lambda c: (c.eval_name, c.model or ""))
        self._write_summary(results, specs)
        return results

    # --- internals ----------------------------------------------------------

    def _eligible(self, spec: EvalSpec) -> bool:
        return spec.agents is None or self.agent in spec.agents

    def _run_cell(self, model: Optional[str], spec: EvalSpec,
                  cell_idx: int = 0, total: int = 0) -> CellResult:
        adapter = self.adapter
        p = self.progress
        model_label = model or "default"

        def _phase(phase: str):
            if p:
                p.update(cell=cell_idx, phase=phase,
                         eval_name=spec.name, model=model_label)

        cell_dir = os.path.join(
            self.run_dir, _model_seg(model),
            _safe(spec.skill_name or "_"), _safe(spec.name)
        )
        workspace = os.path.join(cell_dir, "workspace")
        _prepare_workspace(workspace)

        # 0) git-root boundary — agents walk up from cwd looking for .git to discover
        #    project-level skills. Without this, the workspace (inside the repo tree) leaks
        #    all repo-root skills. `git init` stops the walk at the workspace.
        if self.isolated:
            subprocess.run(["git", "init", workspace], capture_output=True)

        # 1) provision skills + 2) seed files
        _phase("provisioning workspace")
        declared_dirs = self._skill_dirs(spec)
        if self.provision and declared_dirs:
            adapter.provision_skills(workspace, declared_dirs)
        self._seed_files(workspace, spec)

        # 3) render prompt (fill {skill}/{skills} for this adapter)
        prompt = self._render_prompt(spec)

        # 4) isolate HOME so the model sees only the provisioned skills (+ the surface's
        #    vendor skills), not this repo's globally-installed skills.
        iso_home = None
        iso_env: dict[str, str] = {}
        isolated = False
        if self.isolated and adapter.global_skills_subpaths:
            iso_home = tempfile.mkdtemp(prefix="ase-home-")
            seed_dirs = declared_dirs if self.provision else []
            try:
                build_isolated_home(
                    iso_home, adapter.global_skills_subpaths, self._repo_skill_names,
                    seed_dirs, os.path.expanduser("~"),
                )
                cfg_root = None
                for var, skills_sub in getattr(adapter, "isolation_config_homes", []):
                    custom = os.environ.get(var)
                    if custom and os.path.isdir(custom):
                        if cfg_root is None:
                            cfg_root = tempfile.mkdtemp(prefix="cfg-", dir=iso_home)
                        mirror = os.path.join(cfg_root, _safe(var))
                        build_isolated_home(mirror, [skills_sub], self._repo_skill_names,
                                            seed_dirs, custom)
                        iso_env[var] = mirror
                isolated = True
            except OSError as exc:
                print(f"warning: [{self.agent}] skill isolation unavailable ({exc}); "
                      "running non-isolated.", file=sys.stderr)
                shutil.rmtree(iso_home, ignore_errors=True)
                iso_home = None
                iso_env = {}

        # 5) run
        _phase(f"running agent ({self.agent}/{model_label})")
        opts = RunOptions(
            model=model,
            auto_approve=self.auto_approve,
            output_schema=spec.output_schema,
            home=iso_home,
            isolation_env=iso_env,
        )
        try:
            ex = execute(
                adapter, prompt, opts,
                cwd=workspace, timeout=spec.timeout_sec,
                env_overrides=spec.env, agent_name=self.agent, eval_name=spec.name,
            )
        finally:
            if iso_home:
                shutil.rmtree(iso_home, ignore_errors=True)
        rr = ex.result

        if model and rr.error and _looks_like_model_error(rr.error, ex.stderr):
            rr.error = f"model {model!r} rejected by {self.agent} — check models.yaml: {rr.error}"

        # 6) artifacts
        _phase("writing artifacts")
        self._write_artifacts(cell_dir, ex.stdout, ex.stderr, rr)

        # 7) assertions
        _phase("running assertions")
        effective = spec.effective_assertions()
        skipped_judge = self.judge is None and any(
            c.get("type") == "llm_judge" for c in effective)
        ctx = AssertionContext(spec=spec, judge=self.judge,
                               skills_subdir=adapter.skills_subdir)
        checks = []
        for cfg in effective:
            if self.judge is None and cfg.get("type") == "llm_judge":
                continue
            if cfg.get("type") == "llm_judge":
                _phase("running judge")
            checks.append(run_assertion(cfg, rr, workspace, spec, ctx))
        clean = rr.error is None and not rr.timed_out
        ungraded = clean and not checks and skipped_judge
        passed = False if ungraded else (
            (clean and all(c.passed for c in checks)) if checks else clean)

        cell = CellResult(
            agent=self.agent, model=model, eval_name=spec.name, skill=spec.skill_name,
            passed=passed, run_result=rr, assertions=checks, artifacts_dir=cell_dir,
            isolated=isolated, ungraded=ungraded,
        )
        self._write_cell_json(cell_dir, cell)
        _write(os.path.join(cell_dir, "report.md"), render_report(cell))

        # 8) judge artifacts — same detail level as the agent, prefixed judge_*
        if ctx.judge_exec is not None:
            cell.judge_run_result = ctx.judge_exec.result
            self._write_judge_artifacts(cell_dir, cell, ctx.judge_exec)

        if p and cell_idx:
            p.done(cell=cell_idx, passed=passed if not ungraded else None,
                   cost=cell.cost_str)
        return cell

    def _skill_dirs(self, spec: EvalSpec) -> list[str]:
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

    def _render_prompt(self, spec: EvalSpec) -> str:
        prompt = spec.rendered_prompt()
        primary = spec.skills[0] if spec.skills else (spec.skill_name or "")
        if primary:
            prompt = prompt.replace("{skill}", self.adapter.format_skill(primary))
        if spec.skills:
            joined = ", ".join(self.adapter.format_skill(s) for s in spec.skills)
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

    def _write_judge_artifacts(self, cell_dir: str, cell: CellResult, judge_ex) -> None:
        """Write judge_* artifacts — same format as the agent's, so a human can
        audit the judge's reasoning at the same level of detail."""
        jrr = judge_ex.result
        _write(os.path.join(cell_dir, "judge_stdout.jsonl"), judge_ex.stdout)
        _write(os.path.join(cell_dir, "judge_stderr.txt"), judge_ex.stderr)
        _write_json(os.path.join(cell_dir, "judge_events.json"),
                     [e.to_dict() for e in jrr.events])
        _write_json(os.path.join(cell_dir, "judge_result.json"), jrr.to_dict())

        judge_assertion = next((a for a in cell.assertions if a.type == "llm_judge"), None)
        judge_cell = CellResult(
            agent=jrr.agent,
            model=jrr.resolved_model or cell.model,
            eval_name=cell.eval_name,
            skill=cell.skill,
            passed=judge_assertion.passed if judge_assertion else False,
            run_result=jrr,
            assertions=[judge_assertion] if judge_assertion else [],
            artifacts_dir=cell_dir,
        )
        _write(os.path.join(cell_dir, "judge_report.md"), render_report(judge_cell))

    def _write_summary(self, results: list[CellResult], specs: list[EvalSpec]) -> None:
        summary = {
            "run_id": self.run_id,
            "agent": self.agent,
            "models": [m for m in self.models if m is not None] or ["default"],
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
                    "premium_requests": c.run_result.premium_requests,
                    "judge_cost_usd": c.judge_run_result.cost_usd if c.judge_run_result else None,
                    "judge_premium_requests": c.judge_run_result.premium_requests if c.judge_run_result else None,
                    "artifacts": os.path.relpath(c.artifacts_dir, self.run_dir),
                }
                for c in results
            ],
        }
        _write_json(os.path.join(self.run_dir, "summary.json"), summary)
        _write(os.path.join(self.run_dir, "summary.md"),
               render_markdown(results, self.agent, self.models, run_dir=self.run_dir))


# ---------------------------------------------------------------------------
# Reporting helpers (also used by the CLI for stdout)
# ---------------------------------------------------------------------------

def _model_label(model: Optional[str]) -> str:
    return model if model else "default"


def _judge_score(c: CellResult) -> Optional[tuple[int, int]]:
    """(passed, total) rubric items from the judge, or None if no judge ran."""
    for a in c.judge_assertions:
        items = (a.details or {}).get("items", [])
        if items:
            return sum(1 for it in items if it.get("pass")), len(items)
    return None


def _score_parts(c: CellResult) -> str:
    """Format split scores: 'det 2/4  rubric 3/3', or omit a section when empty."""
    parts = []
    det = c.det_assertions
    if det:
        det_pass = sum(1 for a in det if a.passed)
        parts.append(f"det {det_pass}/{len(det)}")
    js = _judge_score(c)
    if js is not None:
        parts.append(f"rubric {js[0]}/{js[1]}")
    return "  ".join(parts) if parts else f"{c.n_pass}/{c.n_total}"


def _cell_text(c: Optional[CellResult]) -> str:
    if c is None:
        return "-"
    if c.run_result.error:
        return "ERR"
    if c.ungraded:
        return "SKIP"
    cost = f"  ({c.cost_str})" if c.cost_str else ""
    return f"{'PASS' if c.passed else 'FAIL'}  {_score_parts(c)}{cost}"


def _cell_mark(c: Optional[CellResult]) -> str:
    if c is None:
        return "–"
    if c.run_result.error:
        return f"⚠️ {c.run_result.error}"
    if c.ungraded:
        return "⚪ ungraded"
    cost = f"  ({c.cost_str})" if c.cost_str else ""
    return f"{'✅' if c.passed else '❌'} {_score_parts(c)}{cost}"


def render_matrix(results: list[CellResult], agent: str,
                  models: list[Optional[str]]) -> str:
    """Eval × model pass/fail grid for the terminal."""
    by_key = {(c.eval_name, c.model): c for c in results}
    evals = sorted({c.eval_name for c in results})
    labels = [_model_label(m) for m in models]

    eval_w = max([len("EVAL")] + [len(e) for e in evals]) + 2
    col_w = max([14] + [len(l) + 2 for l in labels]) if labels else 14
    header = "EVAL".ljust(eval_w) + "".join(l.center(col_w) for l in labels)
    lines = [f"agent: {agent}", header, "-" * len(header)]
    for ev in evals:
        row = ev.ljust(eval_w)
        for m in models:
            row += _cell_text(by_key.get((ev, m))).center(col_w)
        lines.append(row)

    lines += ["", "pass rate:"]
    for m in models:
        cells = [c for c in (by_key.get((ev, m)) for ev in evals) if c is not None]
        graded = [c for c in cells if not c.ungraded]
        npass = sum(1 for c in graded if c.passed)
        ung = len(cells) - len(graded)
        extra = f"   ({ung} ungraded)" if ung else ""
        lines.append(f"  {_model_label(m):<28} {npass}/{len(graded)}{extra}")
    return "\n".join(lines)


def render_markdown(results: list[CellResult], agent: str,
                    models: list[Optional[str]],
                    run_dir: Optional[str] = None) -> str:
    by_key = {(c.eval_name, c.model): c for c in results}
    evals = sorted({c.eval_name for c in results})
    labels = [_model_label(m) for m in models]

    def _linked_mark(c: Optional[CellResult]) -> str:
        mark = _cell_mark(c)
        if run_dir and c is not None and c.artifacts_dir:
            rel = os.path.relpath(os.path.join(c.artifacts_dir, "report.md"), run_dir)
            return f"[{mark}]({rel})"
        return mark

    lines = [f"# Eval results — {agent}", "",
             "Each cell links to a per-cell `report.md` — the prompt the model was given, "
             "its complete response (full transcript), and every file it produced.", "",
             "| eval | " + " | ".join(labels) + " |",
             "|" + "---|" * (len(labels) + 1)]
    for ev in evals:
        cells = [_linked_mark(by_key.get((ev, m))) for m in models]
        lines.append(f"| {ev} | " + " | ".join(cells) + " |")

    lines += ["", "## Pass rate", "", "| model | pass rate |", "|---|---|"]
    for m in models:
        cells = [c for c in (by_key.get((ev, m)) for ev in evals) if c is not None]
        graded = [c for c in cells if not c.ungraded]
        npass = sum(1 for c in graded if c.passed)
        ung = len(cells) - len(graded)
        rate = f"{npass}/{len(graded)}" + (f" ({ung} ungraded)" if ung else "")
        lines.append(f"| {_model_label(m)} | {rate} |")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Per-cell readable report
# ---------------------------------------------------------------------------

_TOOL_RESULT_CLIP = 4000


def _clip(text: str, limit: int) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [clipped {len(text) - limit} chars — full output in events.json / stdout.jsonl]"


def _render_transcript(rr: RunResult) -> str:
    out: list[str] = []
    for e in rr.events:
        k = e.kind
        if k == EventKind.AGENT_MESSAGE:
            if e.text:
                out.append(f"**assistant:**\n\n{e.text}")
        elif k == EventKind.REASONING:
            if e.text:
                out.append(f"**reasoning:**\n\n{e.text}")
        elif k == EventKind.TOOL_CALL:
            label = e.tool_name or "tool"
            if e.command:
                out.append(f"**tool · {label}**\n\n```sh\n{e.command}\n```")
            elif e.path:
                out.append(f"**tool · {label}** → `{e.path}`")
            else:
                out.append(f"**tool · {label}**")
        elif k == EventKind.TOOL_RESULT:
            if e.text:
                out.append(f"**tool result:**\n\n```\n{_clip(e.text, _TOOL_RESULT_CLIP)}\n```")
        elif k == EventKind.FILE_CHANGE:
            if e.path:
                out.append(f"**file changed:** `{e.path}`")
        elif k == EventKind.ERROR:
            if e.text:
                out.append(f"**error:** {e.text}")
    return "\n\n".join(out)


def render_report(cell: CellResult) -> str:
    rr = cell.run_result
    title = f"{cell.agent}:{cell.model or 'default'}"
    out: list[str] = [f"# {cell.eval_name} — {title}", ""]

    out.append(f"- **verdict:** {_cell_mark(cell)}")
    if cell.skill:
        out.append(f"- **skill(s):** {cell.skill}")
    meta = []
    if rr.cost_str:
        meta.append(f"cost {rr.cost_str}")
    if rr.duration_ms is not None:
        meta.append(f"{rr.duration_ms / 1000:.1f}s")
    if rr.resolved_model and rr.resolved_model != cell.model:
        meta.append(f"resolved model: {rr.resolved_model}")
    meta.append(f"isolated: {'yes' if cell.isolated else 'no'}")
    if rr.timed_out:
        meta.append("TIMED OUT")
    out.append(f"- **run:** {', '.join(meta)}")
    if rr.error:
        out.append(f"- **error:** {rr.error}")

    out += ["", "## Prompt given to the model", "", "```", rr.prompt or "(empty)", "```"]

    transcript = _render_transcript(rr)
    out += ["", "## Complete response (transcript)", ""]
    out.append(transcript if transcript.strip()
               else "_(This adapter captured no event trace — see the final answer below.)_")

    out += ["", "## Final answer", "", rr.final_text or "_(empty)_"]
    if rr.structured_output is not None:
        out += ["", "**Structured output:**", "", "```json",
                json.dumps(rr.structured_output, indent=2, default=str), "```"]

    workspace = os.path.join(cell.artifacts_dir, "workspace")
    extra = writes_outside_workspace(rr, workspace)
    out += ["", "## Files the model produced", "",
            "```", file_tree(workspace, extra), "```"]
    inline = inline_files(workspace, extra)
    if inline.strip():
        out += ["", inline]
    out += ["", "_(Non-text files are listed above but not inlined.)_"]

    out += ["", "## Judge verdict", ""]
    judge_a = next((a for a in cell.assertions if a.type == "llm_judge"), None)
    if judge_a is None:
        out.append("_Judge disabled for this run — the rubric was not graded. Read the prompt "
                   "and complete response above and judge for yourself._")
    else:
        det = judge_a.details or {}
        for i, it in enumerate(det.get("items", []), 1):
            out.append(f"{i}. {'✅' if it.get('pass') else '❌'} **{it.get('behavior', '')}**")
            out.append(f"   - {it.get('reason', '')}")
        if det.get("summary"):
            out += ["", f"**Summary:** {det['summary']}"]
        if det.get("judge_error"):
            out += ["", "> ⚠ the judge failed to return a parseable verdict."]

    others = [a for a in cell.assertions if a.type != "llm_judge"]
    if others:
        out += ["", "## Deterministic assertions", ""]
        for a in others:
            out.append(f"- {'✅' if a.passed else '❌'} `{a.type}` — {a.message}")

    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------------------
# small fs helpers
# ---------------------------------------------------------------------------

def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)


def _model_seg(model: Optional[str]) -> str:
    if not model:
        return "_default"
    safe = _safe(model)
    if safe != model:
        return f"{safe}-{hashlib.sha1(model.encode('utf-8')).hexdigest()[:6]}"
    return safe


def _looks_like_model_error(error: Optional[str], stderr: Optional[str]) -> bool:
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


def _prepare_workspace(path: str) -> None:
    """Create a clean per-cell workspace, even when a run_id is reused."""
    if os.path.lexists(path):
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.unlink(path)
    os.makedirs(path, exist_ok=False)
