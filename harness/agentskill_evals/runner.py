"""The runner: execute a matrix of (target × eval) for a single agent, assert, and report.
A target is a model id plus an optional pinned reasoning effort (see spec.ModelTarget), so
one run can compare e.g. claude-haiku-4.5@high against claude-opus-4.6@low.

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
from .adapters.base import RunOptions
from .assertions import AssertionContext, AssertionResult, run_assertion
from .exec import execute
from .isolation import build_isolated_home
from .judge import Judge
from .progress import Progress
from .schema import EventKind, RunResult
from .spec import EvalSpec, ModelTarget, repo_root_for, skill_names
from .workspace_view import (
    REPORT_MAX_INLINE_BYTES,
    file_tree,
    inline_files,
    leaked_skill_reads,
    seeded_relpaths,
    writes_outside_workspace,
)


@dataclass
class CellResult:
    agent: str
    model: Optional[str]
    eval_name: str
    skill: Optional[str]
    passed: bool
    run_result: RunResult
    # The target column's pinned effort (None = unpinned) — part of the cell's matrix
    # identity, so the same model can appear twice at different efforts.
    reasoning_effort: Optional[str] = None
    # What the run actually used after resolution (CLI --reasoning-effort > target > spec).
    effective_effort: Optional[str] = None
    assertions: list[AssertionResult] = field(default_factory=list)
    judge_run_result: Optional[RunResult] = None
    artifacts_dir: str = ""
    isolated: bool = False
    ungraded: bool = False
    isolation_leaks: list[str] = field(default_factory=list)
    scenario_path: Optional[str] = None
    # workspace-relative paths seeded before the run (fixture + files:) — inputs the report
    # annotates so they aren't mistaken for model output
    seeded_paths: list[str] = field(default_factory=list)

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

    @property
    def target_label(self) -> str:
        """Human-readable column identity: `model@effort` when an effort is pinned."""
        base = self.model or "default"
        return f"{base}@{self.reasoning_effort}" if self.reasoning_effort else base


class Runner:
    def __init__(
        self,
        agent: str,
        *,
        targets: Optional[list[ModelTarget]] = None,
        models: Optional[list[Optional[str]]] = None,
        artifacts_root: str,
        run_id: str,
        skills_root: str,
        judge: Optional[Judge] = None,
        provision: bool = True,
        auto_approve: bool = True,
        reasoning_effort: Optional[str] = None,
        jobs: int = 1,
        isolated: bool = True,
        progress: Optional[Progress] = None,
        command: str = "",
    ):
        if targets is None:
            if models is None:
                raise TypeError("Runner() requires targets= (or the deprecated models=)")
            # Deprecated pre-#67 alias: a list of model ids/None — each entry becomes an
            # effort-less column, so existing programmatic callers keep working.
            targets = [m if isinstance(m, ModelTarget) else ModelTarget(m) for m in models]
        self.agent = agent
        self.adapter = get_adapter(agent)
        self.targets = targets
        self.artifacts_root = artifacts_root
        self.run_id = run_id
        self.skills_root = skills_root
        self.judge = judge
        self.provision = provision
        self.command = command
        self.auto_approve = auto_approve
        # Run-level override (from --reasoning-effort); per cell it beats both a target's
        # pinned effort and the spec's own `reasoning_effort:` — same CLI-flag > file
        # precedence as every other run knob.
        self.reasoning_effort = reasoning_effort
        self.jobs = max(1, jobs)
        self.isolated = isolated
        self.progress = progress
        self._repo_skill_names = set(skill_names(skills_root))
        # for masking stale global symlinks (retired names) that still point into this checkout
        self._repo_root = repo_root_for(skills_root)
        self.run_dir = os.path.join(artifacts_root, run_id)

    @property
    def models(self) -> list[Optional[str]]:
        """Deprecated pre-#67 view of the target columns: model ids only (effort dropped)."""
        return [t.model for t in self.targets]

    # --- public -------------------------------------------------------------

    def run(self, specs: list[EvalSpec]) -> list[CellResult]:
        os.makedirs(self.run_dir, exist_ok=True)
        cells = [
            (target, spec)
            for spec in specs
            if self._eligible(spec)
            for target in self.targets
        ]

        results: list[CellResult] = []
        if self.jobs == 1:
            for i, (target, spec) in enumerate(cells, 1):
                results.append(self._run_cell(target, spec, cell_idx=i, total=len(cells)))
        else:
            with ThreadPoolExecutor(max_workers=self.jobs) as pool:
                # Each future must carry its OWN cell_idx/total — without it, _run_cell defaults
                # to cell_idx=0 for every parallel cell (all phase updates show "cell 0/...",
                # indistinguishable), and `if p and cell_idx:` inside _run_cell_body is falsy for
                # 0, so its own internal p.done() call never fires either. The (now removed) fix
                # of calling p.done() here instead used as_completed()'s completion ORDER as the
                # cell number, not the cell's actual identity.
                futs = {
                    pool.submit(self._run_cell, t, s, cell_idx=i, total=len(cells)): (t, s)
                    for i, (t, s) in enumerate(cells, 1)
                }
                for fut in as_completed(futs):
                    results.append(fut.result())

        results.sort(key=lambda c: (c.eval_name, c.model or "", c.reasoning_effort or ""))
        self._write_summary(results, specs)
        return results

    # --- internals ----------------------------------------------------------

    def _eligible(self, spec: EvalSpec) -> bool:
        return spec.agents is None or self.agent in spec.agents

    def _run_cell(self, target: ModelTarget, spec: EvalSpec,
                  cell_idx: int = 0, total: int = 0) -> CellResult:
        cell_dir = os.path.join(
            self.run_dir, _target_seg(target),
            _safe(spec.skill_name or "_"), _safe(spec.name)
        )
        workspace = os.path.join(cell_dir, "workspace")
        _prepare_workspace(workspace)

        # The directory the agent actually runs in. Under isolation this is a tempdir with NO
        # path relationship to this repo's checkout — not `<repo>/artifacts/.../workspace`,
        # which put every eval's cwd two `list_dir`/`cd ..` hops from the repo's own undeclared
        # skill directories. HOME-overlay isolation (below) only masks global skill-discovery
        # paths; it did nothing to stop a general-purpose file-browsing agent (antigravity's
        # list_dir/view_file/shell on arbitrary absolute paths, not just its own cwd) from
        # walking up and reading them directly, which is exactly what happened in run
        # 20260707-072933_scen_SimpleATL06PromptGrandMesa. Moved into `workspace` (inside
        # cell_dir) once the run + assertions are done, so artifacts/report are unaffected.
        exec_ws = workspace
        if self.isolated:
            exec_ws = tempfile.mkdtemp(prefix="ase-ws-")

        # A cell that raises (a buggy assertion, the judge choking on a malformed response, an
        # OSError mid-move, ...) must not: (a) leak the exec_ws tempdir forever, or (b) abort
        # every other cell in the batch — `run()` has no try/except of its own around this call,
        # so this is the only backstop.
        try:
            return self._run_cell_body(target, spec, cell_idx, cell_dir, workspace, exec_ws)
        except Exception as exc:
            return self._failed_cell(target, spec, cell_idx, cell_dir, exec_ws, exc)
        finally:
            # A no-op on the success path AND the usual crash path: `shutil.move` (here or in
            # _failed_cell) already relocated exec_ws to `workspace`, so there's nothing left
            # at the old path to remove. This only fires when even that preservation failed.
            if exec_ws != workspace and os.path.isdir(exec_ws):
                shutil.rmtree(exec_ws, ignore_errors=True)

    def _failed_cell(self, target: ModelTarget, spec: EvalSpec, cell_idx: int,
                     cell_dir: str, exec_ws: str, exc: Exception) -> CellResult:
        """Best-effort CellResult for a cell that raised instead of completing normally — keeps
        one broken eval from aborting the whole run() batch, and still records something on disk
        (report.md/assertions.json) rather than leaving the cell's artifacts dir silently empty."""
        print(f"warning: [{self.agent}] cell {spec.name!r} crashed: {exc}", file=sys.stderr)
        rr = RunResult(agent=self.agent, eval_name=spec.name, prompt="", workdir=exec_ws,
                       error=f"{type(exc).__name__}: {exc}")
        cell = CellResult(agent=self.agent, model=target.model, eval_name=spec.name,
                          skill=spec.skill_name, passed=False, run_result=rr,
                          reasoning_effort=target.reasoning_effort,
                          artifacts_dir=cell_dir,
                          scenario_path=getattr(spec, "source_path", None))
        # Preserve whatever the run produced before crashing: move exec_ws into
        # cell_dir/workspace (as the success path does) instead of letting _run_cell's
        # finally delete it — partial output is exactly the evidence needed to debug the
        # crash, and report.md below can then still show it.
        workspace = os.path.join(cell_dir, "workspace")
        try:
            if exec_ws != workspace and os.path.isdir(exec_ws):
                if os.path.isdir(workspace) and not os.listdir(workspace):
                    os.rmdir(workspace)
                if not os.path.lexists(workspace):
                    shutil.move(exec_ws, workspace)
        except OSError:
            pass  # best-effort — the finally in _run_cell still cleans up the tempdir
        try:
            self._write_cell_json(cell_dir, cell)
            _write(os.path.join(cell_dir, "report.md"), render_report(cell))
        except Exception:
            pass  # best-effort only — don't let artifact-writing mask the real error above
        if self.progress and cell_idx:
            self.progress.done(cell=cell_idx, passed=False, cost="")
        return cell

    def _run_cell_body(self, target: ModelTarget, spec: EvalSpec, cell_idx: int,
                       cell_dir: str, workspace: str, exec_ws: str) -> CellResult:
        adapter = self.adapter
        p = self.progress
        model = target.model
        model_label = target.label

        def _phase(phase: str):
            if p:
                p.update(cell=cell_idx, phase=phase,
                         eval_name=spec.name, model=model_label)

        # 1) provision skills + 2) seed files
        _phase("provisioning workspace")
        declared_dirs = self._skill_dirs(spec)
        if self.provision and declared_dirs:
            adapter.provision_skills(exec_ws, declared_dirs)
        self._seed_files(exec_ws, spec)

        # 3) render prompt (fill {skill}/{skills} for this adapter)
        prompt = self._render_prompt(spec)

        # 4) isolate HOME so the model sees only the provisioned skills (+ the surface's
        #    vendor skills), not this repo's globally-installed skills.
        iso_home = None
        iso_env: dict[str, str] = {}
        # Distinct from `self.isolated` (the run-level config flag, which also gates the exec_ws
        # relocation above): this tracks whether THIS cell's HOME-overlay skill masking actually
        # succeeded, which can independently fail (e.g. no symlink privileges) and fall back.
        home_isolated = False
        # Config-file masks neutralize per-user config the overlay's wholesale symlinks would
        # otherwise pass through — today that's MCP server configs ("{}" = declare no
        # servers), keeping runs hermetically MCP-off (DESIGN_MCP_Support.md, Phase 0).
        cfg_masks = {p: "{}" for p in getattr(adapter, "isolation_config_masks", [])}
        if self.isolated and (adapter.global_skills_subpaths or cfg_masks):
            iso_home = tempfile.mkdtemp(prefix="ase-home-")
            seed_dirs = declared_dirs if self.provision else []
            try:
                build_isolated_home(
                    iso_home, adapter.global_skills_subpaths, self._repo_skill_names,
                    seed_dirs, os.path.expanduser("~"),
                    plugin_registry_subpaths=getattr(adapter, "global_plugin_registry_subpaths", []),
                    repo_root=self._repo_root,
                    config_file_masks=cfg_masks,
                )
                cfg_root = None
                for var, skills_sub in getattr(adapter, "isolation_config_homes", []):
                    custom = os.environ.get(var)
                    if custom and os.path.isdir(custom):
                        if cfg_root is None:
                            cfg_root = tempfile.mkdtemp(prefix="cfg-", dir=iso_home)
                        mirror = os.path.join(cfg_root, _safe(var))
                        build_isolated_home(mirror, [skills_sub], self._repo_skill_names,
                                            seed_dirs, custom, repo_root=self._repo_root)
                        iso_env[var] = mirror
                home_isolated = True
            except OSError as exc:
                print(f"warning: [{self.agent}] skill isolation unavailable ({exc}); "
                      "running non-isolated.", file=sys.stderr)
                shutil.rmtree(iso_home, ignore_errors=True)
                iso_home = None
                iso_env = {}
            except Exception:
                # Anything other than OSError building the isolated home must not leak the
                # tempdir either — clean up, then re-raise so _run_cell's crash-safety wrapper
                # records a failed cell instead of aborting the whole batch (it has no way to
                # reach this function-local iso_home itself).
                shutil.rmtree(iso_home, ignore_errors=True)
                raise

        # 5) run
        _phase(f"running agent ({self.agent}/{model_label})")
        effort = (self.reasoning_effort or target.reasoning_effort
                  or spec.reasoning_effort)
        if not adapter.supports_reasoning_effort:
            # This runner has no effort control (cmd_run warns up front): resolve to None so
            # neither RunOptions nor the recorded effective_effort claims a thinking budget
            # the CLI silently ignored — that would corrupt cross-effort comparisons.
            effort = None
        opts = RunOptions(
            model=model,
            auto_approve=self.auto_approve,
            reasoning_effort=effort,
            output_schema=spec.output_schema,
            home=iso_home,
            isolation_env=iso_env,
        )
        try:
            ex = execute(
                adapter, prompt, opts,
                cwd=exec_ws, timeout=spec.timeout_sec,
                env_overrides=spec.env, agent_name=self.agent, eval_name=spec.name,
            )
        finally:
            if iso_home:
                shutil.rmtree(iso_home, ignore_errors=True)
        rr = ex.result

        if model and rr.error and _looks_like_model_error(rr.error, ex.stderr, model):
            rr.error = f"model {model!r} rejected by {self.agent} — check models.yaml: {rr.error}"

        # Residual safety net: even with exec_ws relocated outside the repo tree, a run could
        # still reach an undeclared skill some other way (e.g. searching the real disk by name).
        # Catch that from the trace so a leak is never silently reported as `isolated: true`.
        leaks: list[str] = []
        if home_isolated:
            leaks = leaked_skill_reads(
                rr, exec_ws, self.skills_root, self._repo_skill_names, set(spec.skills)
            )
            if leaks:
                home_isolated = False

        # 6) artifacts. NOTE: rr.workdir (serialized into result.json below) still names the
        # ephemeral exec_ws tempdir, not its final `workspace` location — that's the directory
        # the run actually executed in, before its contents were moved into the artifacts dir
        # below; the tempdir itself is gone by the time anyone reads result.json.
        _phase("writing artifacts")
        self._write_artifacts(cell_dir, ex.stdout, ex.stderr, rr)

        # 7) assertions
        _phase("running assertions")
        effective = spec.effective_assertions()
        skipped_judge = self.judge is None and any(
            c.get("type") == "llm_judge" for c in effective)
        # skill_dirs: every location this adapter can discover a provisioned skill from —
        # isolation copies declared skills into the global dirs too, so a read via e.g.
        # ~/.codex/skills/<skill>/ must still count as the skill being triggered.
        ctx = AssertionContext(
            spec=spec, judge=self.judge, skills_subdir=adapter.skills_subdir,
            skill_dirs=[adapter.skills_subdir, *(adapter.global_skills_subpaths or [])])
        checks = []
        for cfg in effective:
            if self.judge is None and cfg.get("type") == "llm_judge":
                continue
            if cfg.get("type") == "llm_judge":
                _phase("running judge")
            checks.append(run_assertion(cfg, rr, exec_ws, spec, ctx))
        clean = rr.error is None and not rr.timed_out
        ungraded = clean and not checks and skipped_judge
        passed = False if ungraded else (
            (clean and all(c.passed for c in checks)) if checks else clean)

        # Move the tempdir exec workspace into cell_dir/workspace for artifacts/report — `rmdir`
        # is safe since `_prepare_workspace` left it empty and nothing else wrote into it while
        # isolated; `move` renames when possible (same filesystem) instead of a copy+delete pass.
        # Assertion details recorded paths under the (about to vanish) exec_ws tempdir — remap
        # them onto the final workspace location so assertions.json points at real files.
        if self.isolated:
            os.rmdir(workspace)
            shutil.move(exec_ws, workspace)
            for c in checks:
                c.details = _remap_paths(c.details, exec_ws, workspace)

        cell = CellResult(
            agent=self.agent, model=model, eval_name=spec.name, skill=spec.skill_name,
            passed=passed, run_result=rr, assertions=checks, artifacts_dir=cell_dir,
            reasoning_effort=target.reasoning_effort, effective_effort=effort,
            isolated=home_isolated, ungraded=ungraded, isolation_leaks=leaks,
            scenario_path=getattr(spec, "source_path", None),
            seeded_paths=sorted(seeded_relpaths(spec)),
        )
        # Attach the judge's run BEFORE rendering, so report.md's verdict line shows the
        # combined agent+judge cost (previously only summary.* had it).
        if ctx.judge_exec is not None:
            cell.judge_run_result = ctx.judge_exec.result
        self._write_cell_json(cell_dir, cell)
        _write(os.path.join(cell_dir, "report.md"), render_report(cell))

        # 8) judge artifacts — same detail level as the agent, prefixed judge_*
        if ctx.judge_exec is not None:
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

    def _seed_files(self, dest_dir: str, spec: EvalSpec) -> None:
        """Seed `spec`'s fixture/files into `dest_dir` — named generically, not `workspace`,
        since the caller passes whatever directory the agent will actually run in (`exec_ws`
        under isolation), distinct from `_run_cell`'s own `workspace` variable."""
        fixture = spec.resolved_fixture()
        if fixture:
            if os.path.isdir(fixture):
                shutil.copytree(fixture, dest_dir, dirs_exist_ok=True)
            else:
                # validate_spec blocks this in cmd_run; this backstop covers programmatic use
                print(f"warning: fixture {fixture!r} does not exist — skipping",
                      file=sys.stderr)
        for src, dest_rel in spec.resolved_files():
            if not os.path.isfile(src):
                print(f"warning: seed file {src!r} does not exist — skipping",
                      file=sys.stderr)
                continue
            dest = os.path.realpath(os.path.join(dest_dir, dest_rel))
            if not dest.startswith(os.path.realpath(dest_dir) + os.sep):
                print(f"warning: seed file {dest_rel!r} escapes workspace, skipping",
                      file=sys.stderr)
                continue
            os.makedirs(os.path.dirname(dest) or dest_dir, exist_ok=True)
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
                "reasoning_effort": cell.reasoning_effort,
                "effective_effort": cell.effective_effort,
                "eval": cell.eval_name,
                "skill": cell.skill,
                "isolated": cell.isolated,
                "isolation_leaks": cell.isolation_leaks,
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

        # The LAST llm_judge assertion, to match ctx.judge_exec — _llm_judge overwrites
        # judge_exec on each run, so with several llm_judge assertions the saved exec trace
        # belongs to the final one; picking the first would pair a verdict with the wrong
        # transcript.
        judge_assertion = next(
            (a for a in reversed(cell.assertions) if a.type == "llm_judge"), None)
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
            "command": self.command,
            "agent": self.agent,
            "models": [t.model for t in self.targets if t.model is not None] or ["default"],
            "targets": [{"model": t.model, "reasoning_effort": t.reasoning_effort}
                        for t in self.targets],
            # Keep the legacy `isolated` key as the requested run mode. Per-cell `isolated`
            # records whether skill isolation actually held after fallback/leak detection.
            "isolated": self.isolated,
            "isolation_requested": self.isolated,
            "all_cells_isolated": all(c.isolated for c in results) if results else self.isolated,
            "n_evals": len(specs),
            "n_cells": len(results),
            "n_passed": sum(1 for c in results if c.passed),
            "judge_agent": self.judge.agent if self.judge else None,
            "judge_model": self.judge.model if self.judge else None,
            "cells": [
                {
                    "agent": c.agent, "model": c.model,
                    "reasoning_effort": c.reasoning_effort,
                    "effective_effort": c.effective_effort,
                    "eval": c.eval_name, "skill": c.skill,
                    "isolated": c.isolated, "isolation_leaks": c.isolation_leaks,
                    "ungraded": c.ungraded,
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
               render_markdown(results, self.agent, self.targets,
                               run_dir=self.run_dir, command=self.command))


# ---------------------------------------------------------------------------
# Reporting helpers (also used by the CLI for stdout)
# ---------------------------------------------------------------------------

def _cell_key(c: CellResult) -> tuple:
    """Matrix identity of a cell: eval row × (model, pinned effort) column."""
    return (c.eval_name, c.model, c.reasoning_effort)


def _as_targets(targets) -> list[ModelTarget]:
    """Accept the deprecated pre-#67 column list (model ids/None) alongside ModelTargets,
    so external render_matrix/render_markdown callers keep working."""
    return [t if isinstance(t, ModelTarget) else ModelTarget(t) for t in targets]


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
                  targets: list[ModelTarget]) -> str:
    """Eval × target (model@effort) pass/fail grid for the terminal."""
    targets = _as_targets(targets)
    by_key = {_cell_key(c): c for c in results}
    evals = sorted({c.eval_name for c in results})
    labels = [t.label for t in targets]

    eval_w = max([len("EVAL")] + [len(e) for e in evals]) + 2
    col_w = max([14] + [len(l) + 2 for l in labels]) if labels else 14
    header = "EVAL".ljust(eval_w) + "".join(l.center(col_w) for l in labels)
    lines = [f"agent: {agent}", header, "-" * len(header)]
    for ev in evals:
        row = ev.ljust(eval_w)
        for t in targets:
            row += _cell_text(by_key.get((ev, t.model, t.reasoning_effort))).center(col_w)
        lines.append(row)

    lines += ["", "pass rate:"]
    for t in targets:
        cells = [c for c in (by_key.get((ev, t.model, t.reasoning_effort)) for ev in evals)
                 if c is not None]
        graded = [c for c in cells if not c.ungraded]
        npass = sum(1 for c in graded if c.passed)
        ung = len(cells) - len(graded)
        extra = f"   ({ung} ungraded)" if ung else ""
        lines.append(f"  {t.label:<28} {npass}/{len(graded)}{extra}")
    return "\n".join(lines)


def render_markdown(results: list[CellResult], agent: str,
                    targets: list[ModelTarget],
                    run_dir: Optional[str] = None,
                    command: str = "") -> str:
    targets = _as_targets(targets)
    by_key = {_cell_key(c): c for c in results}
    evals = sorted({c.eval_name for c in results})
    labels = [t.label for t in targets]

    def _linked_mark(c: Optional[CellResult]) -> str:
        mark = _cell_mark(c)
        if run_dir and c is not None and c.artifacts_dir:
            rel = os.path.relpath(os.path.join(c.artifacts_dir, "report.md"), run_dir)
            return f"[{mark}]({rel})"
        return mark

    heading = f"# Eval results — {agent}"
    if command:
        heading += f" ({command})"
    lines = [heading, "",
             "Each cell links to a per-cell `report.md` — the prompt the model was given, "
             "its complete response (full transcript), and the workspace files after the "
             "run (seeded inputs marked).", "",
             "| eval | " + " | ".join(labels) + " |",
             "|" + "---|" * (len(labels) + 1)]
    for ev in evals:
        cells = [_linked_mark(by_key.get((ev, t.model, t.reasoning_effort)))
                 for t in targets]
        lines.append(f"| {ev} | " + " | ".join(cells) + " |")

    lines += ["", "## Pass rate", "", "| model | pass rate |", "|---|---|"]
    for t in targets:
        cells = [c for c in (by_key.get((ev, t.model, t.reasoning_effort)) for ev in evals)
                 if c is not None]
        graded = [c for c in cells if not c.ungraded]
        npass = sum(1 for c in graded if c.passed)
        ung = len(cells) - len(graded)
        rate = f"{npass}/{len(graded)}" + (f" ({ung} ungraded)" if ung else "")
        lines.append(f"| {t.label} | {rate} |")
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
    title = f"{cell.agent}:{cell.target_label}"
    out: list[str] = [f"# {cell.eval_name} — {title}", ""]

    out.append(f"- **verdict:** {_cell_mark(cell)}")
    if cell.skill:
        out.append(f"- **skill(s):** {cell.skill}")
    if cell.scenario_path:
        # The `name:` inside the file is free text and can drift from the filename — the
        # full path is the one unambiguous pointer back to exactly what was run.
        out.append(f"- **source:** `{cell.scenario_path}`")
    meta = []
    if rr.cost_str:
        meta.append(f"cost {rr.cost_str}")
    if cell.effective_effort:
        meta.append(f"effort {cell.effective_effort}")
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
    if cell.isolation_leaks:
        out += ["", "> ⚠ **Isolation leak:** this run read undeclared skill(s) from the real "
                    "repo checkout, bypassing HOME-based isolation:"]
        for p in cell.isolation_leaks:
            out.append(f"> - `{p}`")

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
    seeded = set(cell.seeded_paths)
    out += ["", "## Files in the workspace after the run", "",
            "```", file_tree(workspace, extra, seeded=seeded), "```"]
    inline = inline_files(workspace, extra,
                          max_bytes=REPORT_MAX_INLINE_BYTES, truncate=True, seeded=seeded)
    if inline.strip():
        out += ["", inline]
    out += ["", "_(Non-text files are listed above but not inlined; files seeded before the "
                "run are marked as inputs.)_"]

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


def _target_seg(target: ModelTarget) -> str:
    """Artifacts dir segment for a matrix column. The effort suffix keeps two runs of the
    SAME model at different efforts from colliding in one cell dir ('@' is fs-safe)."""
    seg = _model_seg(target.model)
    return f"{seg}@{target.reasoning_effort}" if target.reasoning_effort else seg


# Phrases a CLI actually emits when rejecting a model id. Deliberately tight: the old
# any-of ("model", "not found", "unknown", "invalid", "unsupported") heuristic re-labelled
# unrelated failures as "model rejected" whenever stderr merely mentioned the model name or
# said "not found" about something else.
_MODEL_ERR_PHRASES = (
    "invalid model", "unknown model", "model not found", "model_not_found",
    "unsupported model", "no such model", "not a valid model", "model is not available",
    "model not available", "model not supported",
)
_MODEL_ERR_KEYWORDS = ("not found", "unknown", "invalid", "unsupported",
                       "not available", "rejected")


def _looks_like_model_error(error: Optional[str], stderr: Optional[str],
                            model: Optional[str] = None) -> bool:
    blob = f"{error or ''} {stderr or ''}".lower()
    if any(p in blob for p in _MODEL_ERR_PHRASES):
        return True
    # …or the specific model id is named alongside a rejection keyword.
    return bool(model) and model.lower() in blob and any(
        k in blob for k in _MODEL_ERR_KEYWORDS)


def _remap_paths(obj, old: str, new: str):
    """Rewrite the (now deleted) exec_ws tempdir prefix to the final workspace path in
    assertion details, recursively over dicts/lists/strings."""
    if isinstance(obj, str):
        return obj.replace(old, new)
    if isinstance(obj, list):
        return [_remap_paths(x, old, new) for x in obj]
    if isinstance(obj, dict):
        return {k: _remap_paths(v, old, new) for k, v in obj.items()}
    return obj


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
