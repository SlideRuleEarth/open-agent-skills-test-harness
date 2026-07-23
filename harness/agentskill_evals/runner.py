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

import glob
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Optional

from .adapters import get_adapter
from .adapters.base import RunOptions
from .assertions import AssertionContext, AssertionResult, run_assertion
from .exec import execute
from .isolation import (build_isolated_home, config_home_entries, home_write_escapes,
                        reroot_config_masks)
from .judge import Judge
from .mcp import REDACTED, interpolated_refs, redact, redact_bytes, redact_obj
from .notices import warn
from .progress import Progress
from .schema import EventKind, RunResult
from . import xattrs
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
    # Class-level default so the redacting artifact writers work on a Runner built without
    # __init__ — selftest constructs partial runners to exercise the writers in isolation,
    # and an artifact writer that raises AttributeError on "no secrets to redact" would be
    # failing at the safest possible moment.
    _secrets: tuple[str, ...] = ()
    # Union of every cell's secrets, for the artifacts written AFTER the per-cell registry
    # is cleared. See `_run_secrets` in __init__.
    _run_secrets: tuple[str, ...] = ()

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
        # Values interpolated into THIS cell's MCP config, scrubbed from every artifact this
        # runner writes. Cell-scoped (set in _run_cell_body, cleared in _run_cell's finally)
        # rather than run-scoped, and never stored on CellResult/RunResult — those are
        # serialized, which would archive the very strings this exists to keep out.
        self._secrets: tuple[str, ...] = ()
        # The run-scoped union of the above. Needed because the run summary is written long
        # after the last cell cleared `_secrets`, and it AGGREGATES cells — `RunResult.error`
        # carries a tail of the child's stdout/stderr, so summary.json and summary.md can
        # republish a credential that every per-cell artifact correctly scrubbed. Review
        # reproduced the leak through both files.
        #
        # Union, not per-cell: a summary row mixes cells, so scrubbing it against one cell's
        # set would leave the others exposed. The cost is over-redaction — cell A's token
        # blanked out of cell B's row — which is the harmless direction.
        self._run_secrets: tuple[str, ...] = ()

    @property
    def models(self) -> list[Optional[str]]:
        """Deprecated pre-#67 view of the target columns: model ids only (effort dropped)."""
        return [t.model for t in self.targets]

    # --- public -------------------------------------------------------------

    def run(self, specs: list[EvalSpec]) -> list[CellResult]:
        os.makedirs(self.run_dir, exist_ok=True)
        if self.jobs > 1 and not getattr(self.adapter, "parallel_safe_config", False):
            # An earlier revision of this guard refused only NON-isolated parallel runs, on
            # the premise that an isolated cell's private home made concurrency safe. That
            # premise was wrong, and review caught it: an isolated home is a symlink
            # OVERLAY, not a copy. `isolation._overlay` wholesale-symlinks every entry it
            # is not explicitly told to mask, so two isolated cells' `.codex/config.toml`
            # are two paths to one real file — verified by writing through one overlay and
            # reading the change back through another, with the write also landing in the
            # user's real home. Only `isolation_config_masks` entries are materialized, and
            # no adapter masks its whole config home.
            #
            # So isolation is the wrong thing to gate on. What matters is whether the
            # adapter's mutable configuration is materialized PER CELL, which is what
            # `parallel_safe_config` declares. Today no adapter can claim it, so this
            # refuses `--jobs > 1` outright rather than pretending one flag combination is
            # the dangerous one.
            #
            # Refused rather than warned because the failure mode is a wrong ANSWER, not a
            # crash: cell A's agent (or the CLI's own startup bookkeeping) writes config
            # that cell B reads mid-launch, and the resulting nondeterminism gets attributed
            # to the model. `--jobs 1` is the whole workaround, and it is the default.
            raise RuntimeError(
                f"refusing to run {self.jobs} cells in parallel: the {self.agent} adapter "
                "does not materialize its CLI configuration per cell, so concurrent cells "
                "share it. Isolation does NOT fix this — an isolated home is a symlink "
                "overlay, so every config file it does not explicitly mask is a symlink to "
                "the one real file, and a write through one cell's overlay is visible to "
                "every other cell (and to your real home). Concurrent cells would corrupt "
                "each other's results nondeterministically, in a way that looks like a "
                "model problem. Use --jobs 1 (the default). Parallelism can be re-enabled "
                "for a runner once its mutable config is materialized per cell — set "
                "parallel_safe_config on the adapter then."
            )
        if not self.isolated and (getattr(self.adapter, "isolation_config_masks", None)
                                  or getattr(self.adapter, "plugin_registry_config_masks", None)):
            # This runner's MCP-off guarantee lives in the isolation overlay's config masks
            # (it has no complete CLI-level kill-switch) — surface the exposure once, up
            # front, instead of letting a non-isolated run silently load real MCP servers.
            print(f"warning: [{self.agent}] running with isolation off — the user's real "
                  f"MCP configuration (user config/plugins) is NOT masked on this runner; "
                  f"MCP hermeticity is not guaranteed for this run.", file=sys.stderr)
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

        # The directory the agent actually runs in: a tempdir with NO path relationship to
        # this repo's checkout — not `<repo>/artifacts/.../workspace`, which put every eval's
        # cwd two `list_dir`/`cd ..` hops from the repo's own undeclared skill directories.
        # HOME-overlay isolation (below) only masks global skill-discovery paths; it did
        # nothing to stop a general-purpose file-browsing agent (antigravity's
        # list_dir/view_file/shell on arbitrary absolute paths, not just its own cwd) from
        # walking up and reading them directly, which is exactly what happened in run
        # 20260707-072933_scen_SimpleATL06PromptGrandMesa. Moved into `workspace` (inside
        # cell_dir) once the run + assertions are done, so artifacts/report are unaffected.
        #
        # Unconditional, NOT gated on `self.isolated`, which was the third review round's
        # find: a cwd is a place the agent can write, and `..` from a cwd inside the artifact
        # tree IS the artifact tree. A `--no-isolated` cell wrote `../agent-created.txt` and
        # published the raw secret in cell_dir, where the workspace scrub never looks because
        # only `workspace` is archived. `--no-isolated` means "see my real HOME and installed
        # skills"; it never meant "run inside the results directory".
        #
        # The extra nesting level is the point: `..` from exec_ws is `exec_root`, which is
        # the harness's own tempdir, is never archived, and is deleted whole.
        exec_root = tempfile.mkdtemp(prefix="ase-ws-")
        exec_ws = os.path.join(exec_root, "workspace")
        os.makedirs(exec_ws)
        # Registered with the frame that owns the try/except/finally, not held in the body
        # that creates them: a raise below unwinds the body's locals, and a credential
        # directory nothing can still name is a credential directory nothing will remove.
        cleanup = _CellCleanup(self.agent)
        cleanup.own("the agent's execution directory", exec_root)

        # A cell that raises (a buggy assertion, the judge choking on a malformed response, an
        # OSError mid-move, ...) must not: (a) leak the exec_ws tempdir forever, or (b) abort
        # every other cell in the batch — `run()` has no try/except of its own around this call,
        # so this is the only backstop.
        try:
            return self._run_cell_body(target, spec, cell_idx, cell_dir, workspace, exec_ws,
                                       exec_root, cleanup)
        except Exception as exc:
            return self._failed_cell(target, spec, cell_idx, cell_dir, exec_ws, exc, cleanup)
        finally:
            # Backstop. Both paths above purge what they own and RECORD the failures, before
            # they write artifacts, so a failure lands on the result somebody will actually
            # read. This fires only when neither got that far — _failed_cell raising, say —
            # and it is the last moment anything knows these directories existed, so it says
            # what it knows out loud rather than discarding it.
            cleanup.purge_all()
            cleanup.flush()
            # Cleared per cell, not per run: these are this scenario's resolved credentials
            # and nothing after this point may still be redacting against a stale set — a
            # secret carried into the NEXT cell would scrub text there for no reason, and
            # one carried past the last cell would outlive the artifacts it protects.
            self._secrets = ()

    def _failed_cell(self, target: ModelTarget, spec: EvalSpec, cell_idx: int,
                     cell_dir: str, exec_ws: str, exc: Exception,
                     cleanup: "_CellCleanup") -> CellResult:
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
        # Same scrub as the success path, and needed for the same reason: this preserves the
        # agent's output into the artifact tree, so a credential it wrote to a file is
        # archived here too. `_secrets` is still populated — _run_cell clears it in a finally
        # that runs after this. Ordered before report.md below, which INLINES workspace files.
        # Same purge as the success path, and needed more here: this cell crashed, so it may
        # well have crashed BEFORE the body reached its own cleanup, leaving a credentials
        # directory exactly where the agent left it. `purge_all` rather than a named path
        # because THIS frame cannot know how far the body got — the MCP scratch dir may be
        # unremoved, already removed, or removed-and-failed, and only `cleanup` can tell the
        # difference. `take()` collects the failures from both this sweep and any the body
        # recorded before it raised, which is the whole reason they are not body locals.
        cleanup.purge_all()
        # Re-scrubbed because the body may have raised before it got here; `note` dedupes
        # and, crucially, does not replace — if the body already scrubbed, this rescan finds
        # a clean tree and says nothing, and the finding it made is still in `cleanup`.
        cleanup.note(_scrub_and_note(workspace, self._secrets))
        pending = cleanup.pending()
        _record_notes(rr, pending)
        try:
            # Refreshed only if step 6 got as far as writing one: this path must not invent
            # an artifact the successful path would have, but must not leave a stale one
            # either. Inside the same best-effort block as the writes below — artifact
            # bookkeeping must never mask the crash that brought us here.
            if os.path.isfile(os.path.join(cell_dir, "result.json")):
                self._rwj(os.path.join(cell_dir, "result.json"), rr.to_dict())
            self._write_cell_json(cell_dir, cell)
            self._rw(os.path.join(cell_dir, "report.md"), render_report(cell))
            # Only now, with the writes returned: this whole block is best-effort, and
            # acknowledging before it would forget a note precisely when it failed to land.
            cleanup.acknowledge(pending)
        except Exception:
            pass  # best-effort only — don't let artifact-writing mask the real error above
        if self.progress and cell_idx:
            self.progress.done(cell=cell_idx, passed=False, cost="")
        return cell

    def _run_cell_body(self, target: ModelTarget, spec: EvalSpec, cell_idx: int,
                       cell_dir: str, workspace: str, exec_ws: str,
                       exec_root: str, cleanup: "_CellCleanup") -> CellResult:
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
        # 2b) neutralize seeded MCP configs a CLI discovers from the workspace itself
        # (e.g. agy's .agents/mcp_config.json) — the HOME overlay can't reach these, and
        # runners with workspace_config_masks have no CLI-level disable either.
        for rel in _apply_workspace_config_masks(adapter, exec_ws):
            print(f"warning: [{self.agent}] {spec.name!r}: seeded workspace file {rel!r} "
                  f"is an MCP config this runner would load — neutralized (MCP is "
                  f"hermetically off; no `mcp_servers:` support yet).", file=sys.stderr)

        # 3) render prompt (fill {skill}/{skills} for this adapter)
        prompt = self._render_prompt(spec)

        # 4) isolate HOME so the model sees only the provisioned skills (+ the surface's
        #    vendor skills), not this repo's globally-installed skills.
        iso_home = None
        iso_env: dict[str, str] = {}
        # Distinct from `self.isolated` (the run-level config flag; the exec_ws relocation above
        # is not gated on it): this tracks whether THIS cell's HOME-overlay skill masking actually
        # succeeded, which can independently fail (e.g. no symlink privileges) and fall back.
        home_isolated = False
        # Config masks neutralize per-user config the overlay's wholesale symlinks would
        # otherwise pass through — today that's MCP server configs ("{}" = declare no
        # servers; None = empty dir), keeping runs hermetically MCP-off
        # (DESIGN_MCP_Support.md, Phase 0).
        cfg_masks = dict(getattr(adapter, "isolation_config_masks", {}) or {})
        plugin_cfg_masks = dict(getattr(adapter, "plugin_registry_config_masks", {}) or {})
        if self.isolated and (adapter.global_skills_subpaths or cfg_masks or plugin_cfg_masks):
            iso_home = tempfile.mkdtemp(prefix="ase-home-")
            # Registered at creation, like the scratch dir and for the same reason: what
            # follows — the overlay build, `_phase`, the effort resolution — can raise, and
            # until this line the only thing that knew the directory existed was a local in
            # the frame the raise unwinds. Non-fatal: it holds config masks and symlinks
            # into the real home, so a stubborn one is a leaked tempdir, not a leaked secret.
            cleanup.own("the isolated HOME", iso_home, fatal=False)
            seed_dirs = declared_dirs if self.provision else []
            try:
                build_isolated_home(
                    iso_home, adapter.global_skills_subpaths, self._repo_skill_names,
                    seed_dirs, os.path.expanduser("~"),
                    plugin_registry_subpaths=getattr(adapter, "global_plugin_registry_subpaths", []),
                    repo_root=self._repo_root,
                    config_file_masks=cfg_masks,
                    plugin_config_masks=plugin_cfg_masks,
                )
                cfg_root = None
                for var, replaces, skills_sub in config_home_entries(adapter):
                    custom = os.environ.get(var)
                    if custom and os.path.isdir(custom):
                        if cfg_root is None:
                            cfg_root = tempfile.mkdtemp(prefix="cfg-", dir=iso_home)
                        mirror = os.path.join(cfg_root, _safe(var))
                        # the custom home stands in for one HOME subdir (e.g. $COPILOT_HOME
                        # for ~/.copilot), so the masks under that subdir apply inside it —
                        # otherwise pointing the var elsewhere would bypass them.
                        build_isolated_home(mirror, [skills_sub] if skills_sub else [],
                                            self._repo_skill_names,
                                            seed_dirs, custom, repo_root=self._repo_root,
                                            config_file_masks=reroot_config_masks(
                                                cfg_masks, replaces))
                        iso_env[var] = mirror
                home_isolated = True
            except OSError as exc:
                if cfg_masks or plugin_cfg_masks:
                    # This runner's MCP hermeticity lives in the overlay (no CLI-level
                    # kill-switch) — running against the real HOME would load the user's
                    # real MCP servers, so fail this cell instead of falling open. An
                    # explicit `isolated: false` run is the documented opt-out.
                    cleanup.purge(iso_home)
                    raise RuntimeError(
                        f"HOME-overlay isolation failed ({exc}) and {self.agent} depends "
                        f"on it to keep MCP hermetically off — failing closed rather than "
                        f"running with the user's real MCP servers loaded. Run with "
                        f"isolated: false to explicitly accept a non-hermetic run."
                    ) from exc
                print(f"warning: [{self.agent}] skill isolation unavailable ({exc}); "
                      "running non-isolated.", file=sys.stderr)
                cleanup.purge(iso_home)
                iso_home = None
                iso_env = {}
            except Exception:
                # Anything other than OSError building the isolated home must not leak the
                # tempdir either — clean up, then re-raise so _run_cell's crash-safety wrapper
                # records a failed cell instead of aborting the whole batch (it has no way to
                # reach this function-local iso_home itself).
                cleanup.purge(iso_home)
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
        # Declared MCP servers, resolved per cell. The scratch dir holds CLI config files
        # carrying the interpolated credentials, so it lives OUTSIDE the workspace (which is
        # archived into artifacts and inlined into report.md) and is removed in the same
        # finally as the isolated home, on every exit path including a timeout.
        mcp_scratch = None
        mcp_resolved: dict = {}
        # The try starts HERE, not at `execute`: everything between resolving the credentials
        # and launching the agent can raise (a `${VAR}` that no longer resolves, an adapter
        # whose RunOptions no longer match), and each of those raises used to escape the only
        # code that removes the scratch dir and the isolated home.
        try:
            if spec.mcp_servers:
                mcp_resolved, secrets = spec.resolved_mcp_servers()
                self._secrets = tuple(secrets)
                self._run_secrets = tuple(dict.fromkeys(self._run_secrets + self._secrets))
                mcp_scratch = tempfile.mkdtemp(prefix="ase-mcp-")
                # Registered the moment it exists rather than once it holds something: the
                # window between the two is where the credentials get written into it.
                # `${VAR}` in the DECLARATION, not `bool(secrets)`: the redaction set omits
                # values under MIN_REDACTABLE_LEN, and a short credential is still a
                # credential. Review failed a cell for credentials it could not have had
                # because the only question asked was whether `mcp_servers` was present.
                interpolated = interpolated_refs(spec.mcp_servers)
                cleanup.own("the MCP scratch directory", mcp_scratch,
                            tail=_CREDENTIAL_TAIL if interpolated else _CONFIG_TAIL)
                if interpolated:
                    _refuse_uncontained_home(iso_home, spec.name, interpolated)
                    # This cell HAS credentials, which changes what the isolated HOME is.
                    # It was registered as a leaked-tempdir risk because the harness built
                    # it out of masks and symlinks — but it is `$HOME` for a child that can
                    # write, and review's agent copied its resolved token straight into it,
                    # then watched a failed removal report that no credentials were
                    # present. What a writable directory contains is decided after the
                    # harness stops looking, so from here it is the scratch dir's equal.
                    cleanup.own("the isolated HOME", iso_home, tail=_EXPOSED_TAIL)
            opts = RunOptions(
                model=model,
                auto_approve=self.auto_approve,
                reasoning_effort=effort,
                output_schema=spec.output_schema,
                home=iso_home,
                isolation_env=iso_env,
                mcp_servers=mcp_resolved or None,
                mcp_scratch_dir=mcp_scratch,
            )
            ex = execute(
                adapter, prompt, opts,
                cwd=exec_ws, timeout=spec.timeout_sec,
                env_overrides=spec.env, agent_name=self.agent, eval_name=spec.name,
            )
        finally:
            # Verified removal, not best-effort: `mcp.json` in here holds the interpolated
            # credentials, and this is the last code that will ever look at the directory.
            # The failure goes to `cleanup` rather than a local: an exception from `execute`
            # leaves this function, and a note in a local leaves with it — which is exactly
            # how a failed scratch removal came to be reported as nothing at all.
            cleanup.purge(mcp_scratch)
            # The isolated HOME carries config masks and symlinks into the real home, not
            # resolved secrets, so a stubborn one warns instead of failing the cell — the
            # `fatal=False` it was registered with. It gets the same verified removal and
            # the same durable record all the same: "best effort" was never the intent
            # here, it was just what `ignore_errors=True` happened to mean.
            cleanup.purge(iso_home)
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
        # is safe since `_prepare_workspace` left it empty and nothing else wrote into it (the
        # agent has never had a path into cell_dir); `move` renames when possible (same
        # filesystem) instead of a copy+delete pass. Assertion details recorded paths under the
        # (about to vanish) exec_ws tempdir — remap them onto the final workspace location so
        # assertions.json points at real files.
        os.rmdir(workspace)
        shutil.move(exec_ws, workspace)
        for c in checks:
            c.details = _remap_paths(c.details, exec_ws, workspace)
        # The workspace is the one artifact this runner does not WRITE, so it never met
        # `_rw`/`_rwj` and review found it kept credentials the rest of the tree had
        # scrubbed: an MCP result can echo a token back and the agent can save it to a file.
        #
        # Outside the isolation branch on purpose. `workspace` is the final artifact
        # location either way — under isolation the tempdir was just moved onto it, and
        # without isolation the agent wrote into it directly — so scoping this to isolated
        # runs would leave the non-isolated ones, where the agent's output lands in the
        # artifact tree with no move at all, unscrubbed.
        #
        # Runs after grading, never before: assertions above ran against `exec_ws`, so what
        # was graded is exactly what the agent produced, unmodified.
        # exec_root can only go once exec_ws has been moved out of it. Whatever is left is
        # what the agent wrote ABOVE its cwd: never archived, never scrubbed, and — since a
        # cell only has secrets when it declared MCP servers — potentially a copy of one.
        cleanup.purge(exec_root)
        error_before = rr.error
        # The scrub could not certify part of the tree, so it deleted that part — or, worse,
        # could not delete it either; or a directory holding this cell's credentials outlived
        # the cell. Say so loudly and fail: a cell that had to destroy evidence to stay safe
        # has not produced a trustworthy result, and a silent deletion would be worse than
        # either outcome it is choosing between. Ordered before the artifact writes below, so
        # the sentence is on disk and not only in memory, and while `_secrets` still redacts
        # what gets written.
        #
        # `pending`, not a drain: this collects the scratch-dir failure recorded back at
        # `execute`'s finally along with this one, and forgetting them here would put them in
        # a local again — one frame later than the bug this replaced, and just as lossy if a
        # write below raises. They are acknowledged at the return, not here.
        #
        # The scrub's verdict goes through `cleanup` too. It reads like a value that can be
        # recomputed, and it cannot: the scrub DELETES what it could not certify, so a raise
        # after this line reached `_failed_cell`, which rescanned a tree that was clean by
        # then and turned an evidence deletion into a silent one.
        cleanup.note(_scrub_and_note(workspace, self._secrets))
        pending = cleanup.pending()
        if _record_notes(rr, pending):
            passed = False
        if rr.error != error_before or pending:
            # `result.json` was serialized back at step 6, before the workspace could be
            # moved and long before any of this was knowable. Re-write it: `error` is the
            # field tooling greps, and a finding that reaches only report.md is a finding
            # half the readers never see — the same durability gap `notices.py` closed for
            # warnings, in the one artifact that is written twice.
            self._rwj(os.path.join(cell_dir, "result.json"), rr.to_dict())

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
        self._rw(os.path.join(cell_dir, "report.md"), render_report(cell))

        # 8) judge artifacts — same detail level as the agent, prefixed judge_*
        if ctx.judge_exec is not None:
            self._write_judge_artifacts(cell_dir, cell, ctx.judge_exec)

        if p and cell_idx:
            p.done(cell=cell_idx, passed=passed if not ungraded else None,
                   cost=cell.cost_str)
        # Last statement before the return, not merely after the writes that carry the
        # notes. Everything above can still raise — the judge artifacts, `progress.done` —
        # and a raise reaches `_failed_cell`, which rebuilds the result from scratch and
        # REWRITES `result.json` from it: anything acknowledged early is not just missing
        # from the new record, it is erased from the old one. The only moment that is safe
        # is the one where nothing is left to go wrong.
        cleanup.acknowledge(pending)
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

    def _rw(self, path: str, text: str) -> None:
        """Write text with this cell's MCP secrets scrubbed.

        Every artifact write in this class goes through here or `_rwj`. Redacting at the
        writers rather than at each producer is deliberate: the transcript can carry a
        credential that no flag could have kept out — a tool RESULT echoing a token back,
        or the model quoting one — so the scrub has to sit at the last point before disk,
        where it covers producers nobody thought about.
        """
        _write(path, redact(text, self._secrets) if self._secrets else text)

    def _rwj(self, path: str, obj) -> None:
        # Walks the structure and scrubs each string, so a secret is caught wherever it
        # sits — argv entries, nested event payloads, assertion messages — without this
        # needing to know the shape of any of them.
        #
        # This used to redact the SERIALIZED form instead, which review showed was a leak:
        # the encoder re-spells a value containing a quote, a backslash, a control
        # character, or any non-ASCII byte, so the raw secret was no longer present to
        # match. Comparing before serialization removes the encoder from the path.
        _write_json(path, redact_obj(obj, self._secrets) if self._secrets else obj)

    def _write_artifacts(self, cell_dir: str, stdout: str, stderr: str, rr: RunResult) -> None:
        os.makedirs(cell_dir, exist_ok=True)
        self._rw(os.path.join(cell_dir, "stdout.jsonl"), stdout)
        self._rw(os.path.join(cell_dir, "stderr.txt"), stderr)
        rr.stdout_path = os.path.join(cell_dir, "stdout.jsonl")
        rr.stderr_path = os.path.join(cell_dir, "stderr.txt")
        self._rwj(os.path.join(cell_dir, "events.json"), [e.to_dict() for e in rr.events])
        self._rwj(os.path.join(cell_dir, "result.json"), rr.to_dict())

    def _write_cell_json(self, cell_dir: str, cell: CellResult) -> None:
        self._rwj(
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
        self._rw(os.path.join(cell_dir, "judge_stdout.jsonl"), judge_ex.stdout)
        self._rw(os.path.join(cell_dir, "judge_stderr.txt"), judge_ex.stderr)
        self._rwj(os.path.join(cell_dir, "judge_events.json"),
                  [e.to_dict() for e in jrr.events])
        self._rwj(os.path.join(cell_dir, "judge_result.json"), jrr.to_dict())

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
        self._rw(os.path.join(cell_dir, "judge_report.md"), render_report(judge_cell))

    def _consistency(self, results: list[CellResult]) -> dict:
        """Did every cell in this matrix run under the same conditions?

        A matrix's whole purpose is comparison — "model A scored better than model B" is
        the claim the artifact makes. That claim silently requires the cells to differ
        ONLY in the thing being compared. Three things can drift underneath it without
        failing a single cell:

        * **CLI version.** The failure that started this line of work: copilot rewrote its
          own executable from 1.0.64 to 1.0.72 in four days. A long matrix, or two runs a
          day apart, can straddle an auto-update, and every cell still passes. The
          difference then gets attributed to the model.
        * **MCP server set.** Cells that disabled different servers ran against different
          configurations.
        * **Isolation.** A cell that fell back to non-isolated saw skills its siblings did
          not (``CellResult.isolated`` already records the ACHIEVED state, which is why
          this reads that rather than the requested flag).

        Reported, never enforced, and deliberately so: by the time this runs the cells have
        executed and been paid for, so failing them would destroy results that are still
        perfectly readable once the reader knows they are not comparable. What is prevented
        is the silent part.

        Unknown is not agreement, on EVERY axis. A ``cli_version`` of None means the runner
        does not state it (codex, antigravity); an ``mcp_servers_seen`` of None means the
        runner does not express its servers where this can read them (antigravity's file
        masks). A matrix of all-unknown is not "consistent" — it is unverifiable, which is
        reported as its own state so a green consistency line never implies a check that
        could not run. ``verified`` therefore requires every axis to be positively known
        AND uniform: an adapter that can prove it ran no MCP servers says so with ``[]``,
        and one that cannot say anything gets ``unverified``, not a green line resting on
        one axis while another was never read.
        """
        def _spread(values):
            """Distinct values, with None folded into an `unknown` count rather than
            treated as a value — otherwise one unreadable cell reads as a version change.

            Sorting is over the values as given (strings or tuples), so callers keep
            whatever structure they passed in; nothing is stringified here.
            """
            known = sorted({v for v in values if v is not None})
            return known, sum(1 for v in values if v is None)

        versions, versions_unknown = _spread([c.run_result.cli_version for c in results])
        servers_raw = []
        for c in results:
            try:
                seen = self.adapter.mcp_servers_seen(c.run_result.argv)
            except Exception:  # pragma: no cover — a reporting path must never fail a run
                seen = None
            # Kept as a TUPLE, never joined into a string. Server names are arbitrary JSON
            # object keys for copilot, so a name may itself contain the separator: joining
            # on "," makes {"a,b"} and {"a","b"} the same value and reports two genuinely
            # different configurations as consistent. Tuples compare structurally and are
            # emitted below as JSON arrays.
            servers_raw.append(None if seen is None else tuple(seen))
        servers, servers_unknown = _spread(servers_raw)
        isolation = sorted({bool(c.isolated) for c in results})

        drift = []
        if len(versions) > 1:
            drift.append(f"CLI version varied across cells: {', '.join(versions)}")
        if len(servers) > 1:
            drift.append("MCP server set varied across cells: "
                         + "; ".join("[" + (", ".join(s) if s else "none") + "]"
                                     for s in servers))
        if len(isolation) > 1:
            drift.append("isolation varied across cells: some ran isolated, some did not")

        # Per-axis verification. "Exactly one known value AND no unknown cells" is the
        # only shape that means the axis was actually compared; `len(...) <= 1` would
        # accept an axis where every cell was unreadable, which is the mistake below.
        cli_verified = len(versions) == 1 and versions_unknown == 0
        mcp_verified = len(servers) == 1 and servers_unknown == 0
        isolation_verified = len(isolation) == 1  # always readable; empty matrix aside
        # TRI-STATE, not a boolean. A boolean `consistent` reads true whenever nothing
        # DIFFERED — including when nothing could be compared at all, which is every codex
        # and antigravity matrix. Automation would then take a green field as proof of a
        # check that never ran, which is precisely the failure this whole line of work
        # keeps finding. The nuance cannot live only in a secondary field that careful
        # readers consult; the primary one has to carry it.
        #
        # And it has to carry it for EVERY axis, not just the one that motivated the state.
        # A first cut gated `verified` on cli_verified alone, so a claude matrix with a
        # known uniform version and an entirely unread MCP axis reported "verified" beside
        # `mcp_server_set_unknown_cells: 2` — reconstructing, one field over, exactly the
        # misleading green this tri-state exists to remove (found in review).
        if drift:
            comparability = "drift"
        elif cli_verified and mcp_verified and isolation_verified:
            comparability = "verified"
        else:
            comparability = "unverified"

        return {
            # "verified"  — every axis was positively read on every cell, and agreed
            # "unverified" — nothing differed, but at least one axis could not be read, so
            #                sameness was never established (codex, antigravity)
            # "drift"     — cells demonstrably ran under different conditions
            "comparability": comparability,
            "drift": drift,
            "cli_versions": versions,
            "cli_version_unknown_cells": versions_unknown,
            "cli_version_verified": cli_verified,
            # JSON arrays, one per distinct set — never a joined string, since a server
            # name can contain any separator (see _spread's caller).
            "mcp_server_sets": [list(s) for s in servers],
            "mcp_server_set_unknown_cells": servers_unknown,
            "mcp_server_set_verified": mcp_verified,
            "isolation_uniform": len(isolation) <= 1,
        }

    def _warn_inconsistent(self, consistency: dict) -> None:
        """Say it on stderr as well as in the artifact. A drift recorded only in
        summary.json is one nobody sees until they are already arguing about the numbers."""
        if consistency["drift"]:
            print(f"warning: [{self.agent}] this matrix did NOT run under uniform "
                  "conditions, so its cells are not strictly comparable:", file=sys.stderr)
            for d in consistency["drift"]:
                print(f"  - {d}", file=sys.stderr)
            print("  The per-cell results are still valid individually; what is not "
                  "supported is reading the DIFFERENCE between them as caused by the thing "
                  "the matrix varies (model, effort, skill). See summary.json "
                  "`consistency`.", file=sys.stderr)

    def _write_summary(self, results: list[CellResult], specs: list[EvalSpec]) -> None:
        consistency = self._consistency(results)
        self._warn_inconsistent(consistency)
        summary = {
            "run_id": self.run_id,
            # Whether this matrix's cells are comparable to each other at all — see
            # _consistency. Recorded before the results themselves because it qualifies
            # every number below it.
            "consistency": consistency,
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
                    # None where the runner's telemetry does not state it (codex, agy).
                    "cli_version": c.run_result.cli_version,
                    "ungraded": c.ungraded,
                    "passed": c.passed, "n_pass": c.n_pass, "n_total": c.n_total,
                    "error": c.run_result.error, "timed_out": c.run_result.timed_out,
                    "warnings": c.run_result.warnings,
                    "cost_usd": c.run_result.cost_usd,
                    "premium_requests": c.run_result.premium_requests,
                    "judge_cost_usd": c.judge_run_result.cost_usd if c.judge_run_result else None,
                    "judge_premium_requests": c.judge_run_result.premium_requests if c.judge_run_result else None,
                    "artifacts": os.path.relpath(c.artifacts_dir, self.run_dir),
                }
                for c in results
            ],
        }
        # Scrubbed against the RUN-scoped union, not `_secrets`: by the time this runs every
        # cell has cleared its own registry, so the per-cell writers would be redacting
        # against an empty set here. Both files aggregate `RunResult.error`, which carries a
        # tail of child output.
        _write_json(os.path.join(self.run_dir, "summary.json"),
                    redact_obj(summary, self._run_secrets))
        _write(os.path.join(self.run_dir, "summary.md"),
               redact(render_markdown(results, self.agent, self.targets,
                                      run_dir=self.run_dir, command=self.command),
                      self._run_secrets))


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
    for w in rr.warnings:
        # Above the transcript, not buried under it: these exist to explain a verdict the
        # reader is looking at right now (a server that never connected, a build the
        # analysis was not verified against).
        out.append(f"- **warning:** {w.strip()}")
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


class ScrubFailed(RuntimeError):
    """An uncertifiable artifact could not be removed, so the secret is still on disk.

    Distinct from the `lost` list, which reports artifacts that WERE removed: this is the
    one outcome the scrub cannot make safe, and the cell says so in those words rather than
    reporting a deletion that did not happen.
    """


def _shown(paths: list[str]) -> str:
    return ", ".join(paths[:5]) + (f" (+{len(paths) - 5} more)" if len(paths) > 5 else "")


def _scrub_note(lost: list[str]) -> str:
    """The one sentence a deleted artifact gets, on the result that had to delete it."""
    return (f"secret scrub could not certify {_shown(lost)} — removed from the archived "
            f"workspace rather than published unchecked")


def _scrub_and_note(workspace: str, secrets) -> str:
    """Scrub the archived workspace; return the sentence, if any, the cell has to carry."""
    try:
        lost = _scrub_tree(workspace, secrets)
    except ScrubFailed as exc:
        return str(exc)
    return _scrub_note(lost) if lost else ""


def _spells_secret(rel: str, secrets) -> bool:
    """True when a path, ASSEMBLED from its components, reads back as a secret.

    A secret containing a separator — `tenant/key`, an `https://user:pw@host` URL — is
    spelled out by a directory and its child while every individual component looks
    clean, so a per-component check certifies a tree whose `find` output is the
    credential.
    """
    return redact(rel, secrets) != rel


# How many times the assembled-path pass may rename before it gives up and deletes. Each
# round removes one secret spelling, and a workspace declaring more than a handful of
# secrets that also collide with its own directory names is not converging on anything.
_SCRUB_ROUNDS = 32


def _make_traversable(root: str) -> None:
    """Restore owner access on directories under *root* so nothing hides from the scrub.

    ``os.walk`` hands an unreadable directory to ``onerror`` and then yields nothing for
    it, which is indistinguishable from an empty one — review showed a `chmod 000` subtree
    being skipped in silence while the scrub reported success. The harness owns this tree
    (it created the workspace; the agent wrote into it as the same user), so these
    permissions are ours to restore. Whatever cannot be restored is still caught by the
    walk's ``onerror`` and removed rather than published unchecked.
    """
    pending = [root]
    while pending:
        d = pending.pop()
        try:
            st = os.lstat(d)
        except OSError:
            continue
        if stat.S_ISLNK(st.st_mode):
            continue  # never chase a link out of the tree to widen permissions
        mode = stat.S_IMODE(st.st_mode)
        want = mode | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
        if want != mode:
            try:
                os.chmod(d, want)
            except OSError:
                continue
        try:
            with os.scandir(d) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(entry.path)
        except OSError:
            continue


def _scrub_file(path: str, secrets) -> None:
    """Rewrite one regular file — bytes AND attributes — onto a fresh inode.

    Writing in place mutates the INODE, and every hardlink to a file shares its inode:
    review demonstrated the scrub reaching outside the artifact tree to overwrite an
    external file the agent had linked to, and then, a round later, the permission repair
    doing the same thing to that file's MODE. So nothing is written through this name at
    all. The replacement is built beside the original — content redacted, attributes copied
    across redacted — and renamed over it. The artifact tree gets its own inode; the agent's
    link target is left byte-for-byte and bit-for-bit as it was.

    A file carrying nothing is not touched, so mtimes, modes and attributes all survive the
    ordinary no-leak run.
    """
    st = os.lstat(path)
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except PermissionError:
        if st.st_nlink > 1:
            # Widening the mode to read it is a change visible through every OTHER name for
            # this inode, i.e. outside the tree. Stay out: an unreadable hardlink is
            # uncertifiable, and dropping our name for it leaves the rest untouched.
            raise
        os.chmod(path, stat.S_IMODE(st.st_mode) | stat.S_IRUSR | stat.S_IWUSR)
        with open(path, "rb") as fh:
            raw = fh.read()
    attrs = [(n, xattrs.getxattr(path, n)) for n in xattrs.listxattr(path)]
    clean = [(redact_bytes(n, secrets), redact_bytes(v, secrets)) for n, v in attrs]
    scrubbed = redact_bytes(raw, secrets)
    if scrubbed == raw and clean == attrs:
        return  # untouched: original mtime, mode and metadata survive the common run
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".scrub-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(scrubbed)
        for name, value in clean:
            xattrs.setxattr(tmp, name, value)
        os.chmod(tmp, stat.S_IMODE(st.st_mode))  # the mode it had BEFORE any read repair
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _scrub_link(path: str, secrets) -> None:
    """Rewrite one symlink — target AND attributes — onto a fresh inode.

    A symlink is not the unshareable object the previous round assumed it was:
    `os.link(src, dst, follow_symlinks=False)` works on macOS, and review used it to
    hardlink an EXTERNAL symlink into the workspace and watch an in-place `setxattr` rewrite
    the outside name's metadata. So a link that needs changing is replaced rather than
    edited — same reasoning as ``_scrub_file`` and the same result: our name gets a new
    inode, every other name keeps its own. The replacement carries no attributes it was not
    given, which is the strongest form of scrubbed metadata can be.

    The link is still never FOLLOWED — its target may sit outside the artifact tree, and
    rewriting through one would let a link the agent created redirect this scrub into an
    arbitrary file. But the target STRING lives in the tree and `readlink` reads it straight
    back, so a blandly named link pointing at `/tmp/<token>` publishes the credential as
    plainly as a file containing it.
    """
    target = os.readlink(path)
    attrs = [(n, xattrs.getxattr(path, n)) for n in xattrs.listxattr(path)]
    clean = redact(target, secrets)
    clean_attrs = [(redact_bytes(n, secrets), redact_bytes(v, secrets)) for n, v in attrs]
    if clean == target and clean_attrs == attrs:
        return  # untouched: the common no-leak run leaves the link exactly as it found it
    parent = os.path.dirname(path) or "."
    base = os.path.basename(path)
    n = 0
    tmp = os.path.join(parent, f".scrub-{base}")
    while os.path.lexists(tmp):
        n += 1
        tmp = os.path.join(parent, f".scrub-{n}-{base}")
    try:
        os.symlink(clean, tmp)
        for name, value in clean_attrs:
            xattrs.setxattr(tmp, name, value)
        os.replace(tmp, path)  # renames the LINK; never follows it
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _scrub_xattrs(path: str, secrets) -> None:
    """Scrub a directory's attributes in place, there being no rename-over trick for one.

    Directories are the one entry kind left that is written through rather than replaced.
    An unprivileged agent cannot hardlink a directory on either platform this harness runs
    on, so the shared-inode problem that forced files and symlinks onto fresh inodes does
    not arise here — and `st_nlink` could not detect it if it did, since every directory has
    at least two links (`.` and its parent's entry). Names are scrubbed as well as values:
    an attribute name is attacker-chosen text too.
    """
    for name in xattrs.listxattr(path):
        value = xattrs.getxattr(path, name)
        clean_name = redact_bytes(name, secrets)
        clean_value = redact_bytes(value, secrets)
        if (clean_name, clean_value) == (name, value):
            continue
        xattrs.setxattr(path, clean_name, clean_value)
        if clean_name != name:
            xattrs.removexattr(path, name)


def _relax_dir(path: str) -> None:
    """Give the owner write+execute on a directory, so its entries can be removed."""
    try:
        st = os.lstat(path)
    except OSError:
        return
    if stat.S_ISLNK(st.st_mode):
        return
    for attempt in (lambda: os.chflags(path, 0),
                    lambda: os.chmod(path, stat.S_IMODE(st.st_mode) | stat.S_IRWXU)):
        try:
            attempt()
        except (AttributeError, OSError, NotImplementedError):
            pass


def _remove(path: str) -> bool:
    """Delete *path*, and report whether it is actually gone.

    The old code called `unlink`, swallowed the failure and reported the path as lost
    anyway: review set `chflags uchg` on a file and got back a `lost` entry for an artifact
    that was still sitting there with the raw secret in it. So the result is now the answer
    to a question — `lexists` — and not the absence of an exception.

    Escalation runs outward-in. Unlinking needs write+execute on the PARENT, which is a
    directory inside the artifact tree and cannot be hardlinked elsewhere, so that is tried
    first. Only if that is not enough do we clear flags on the entry itself, which for a
    hardlinked file is visible outside the tree — a trade we make only against publishing
    the secret, and only once the cheaper fix has already failed.
    """
    def _attempt() -> None:
        try:
            if os.path.islink(path) or not os.path.isdir(path):
                os.unlink(path)
            else:
                shutil.rmtree(path)
        except OSError:
            pass

    _attempt()
    if not os.path.lexists(path):
        return True
    _relax_dir(os.path.dirname(path) or ".")
    _attempt()
    if not os.path.lexists(path):
        return True
    try:
        os.lchflags(path, 0)
    except (AttributeError, OSError):
        pass
    if os.path.isdir(path) and not os.path.islink(path):
        # `rmtree` needs read+execute on every directory it descends, and a `chmod 000`
        # one cannot even be listed — `os.walk` yields nothing for it, so relaxing what the
        # walk reports would never reach the directory doing the refusing. `_make_traversable`
        # chmods first and scans after, which is the order that gets in.
        _make_traversable(path)
        for sub, dirnames, filenames in os.walk(path):
            for name in dirnames + filenames:
                try:
                    os.lchflags(os.path.join(sub, name), 0)
                except (AttributeError, OSError):
                    pass
    _attempt()
    return not os.path.lexists(path)


_CREDENTIAL_TAIL = ("it holds this cell's resolved credentials; delete it before sharing "
                    "this machine's state")
_TEMPDIR_TAIL = ("no resolved credentials are in it — config masks and symlinks into the "
                 "real home — but nothing will clean it up now")
# The harness built it, but the agent could write to it, so its final contents are the
# agent's business and not the harness's. Said as "could have" rather than "holds": the
# claim being made is about reach, which is what an operator has to act on.
_EXPOSED_TAIL = ("the agent had it as $HOME and could have written this cell's resolved "
                 "credentials into it; delete it before sharing this machine's state")
_CONFIG_TAIL = ("this cell interpolated no ${VAR}, so no credential is in it — but it holds "
                "MCP configuration the harness wrote outside the workspace")


def _refuse_uncontained_home(home: Optional[str], eval_name: str, refs: list[str]) -> None:
    """Fail a credential-bearing cell whose $HOME has write paths into the real home.

    The isolated HOME is a symlink overlay: it masks what the model can READ, and passes
    every unmasked real-home entry through as a symlink. So a model that has just been given
    a token — an MCP tool result can hand it straight back — can write it to
    `$HOME/<anything>/token` and have it land in the real home, outside every directory this
    runner deletes and outside the workspace it scrubs. Review demonstrated exactly that, and
    the overlay's removal still reported success: deleting a symlink certifies nothing about
    its target.

    Refused rather than warned, and refused rather than run-and-scrubbed, because there is
    nothing to scrub — the harness does not know which of the real home's directories were
    written to and will not go looking through the user's home. `isolated: false` is not the
    escape hatch either: that gives the agent the real home with no overlay at all.

    The check is structural, not a blanket ban, so it lifts itself once the writable HOME
    state is materialized (DESIGN_MCP_Support.md §5.3) instead of needing this code deleted.
    """
    if not home:
        raise RuntimeError(
            f"{eval_name!r} interpolates a credential into `mcp_servers` "
            f"({', '.join(refs)}) but this cell has no isolated HOME, so the agent runs "
            f"against the real one: anything it writes there outlives the run and the "
            f"harness cannot certify otherwise. Refusing rather than reporting a contained "
            f"run — remove the `${{VAR}}` or run with isolation available.")
    escapes = home_write_escapes(home)
    if not escapes:
        return
    raise RuntimeError(
        f"{eval_name!r} interpolates a credential into `mcp_servers` ({', '.join(refs)}) "
        f"and its isolated HOME is a symlink overlay: {len(escapes)} of its entries "
        f"({_shown(escapes[:3])}) resolve to directories in the real home, so a token the "
        f"agent writes through one lands outside every directory this run deletes and "
        f"outside the workspace it scrubs. Removing the overlay cannot certify those "
        f"targets. Refusing the run rather than reporting it as contained.")


def _purge(label: str, path: Optional[str], tail: str = _CREDENTIAL_TAIL) -> str:
    """Remove a directory that held credentials; return a sentence if it is still there.

    `shutil.rmtree(..., ignore_errors=True)` answers "did this raise", which is not the
    question — review chmod-ed an exec tempdir to 000 and watched both of its raw-secret
    files survive a cell that reported nothing about them. ``_remove`` is the same
    outward-in escalation the workspace quarantine uses, and its result is `lexists`: an
    answer about the directory rather than about the call.

    The failure is returned rather than raised so the caller can put it on the cell's own
    result, which is where somebody will actually read it — and can do so while
    ``_secrets`` is still populated, before the redaction registry that protects the
    artifacts is torn down.
    """
    if not path or not os.path.lexists(path):
        return ""
    if _remove(path):
        return ""
    return f"{label} could not be removed and is still on disk: {path} — {tail}"


class _CellCleanup:
    """One cell's credential directories, and what removing them found.

    Owned by `_run_cell` — the frame with the try/except/finally — and not by the body that
    creates them, because an exception in the body transfers control OUT of the body: a
    directory only a body local knew about becomes unreachable, and a note only a body local
    held is gone. Review made `execute()` raise while the scratch removal failed, and watched
    the raw `mcp.json` survive a cell whose recorded error was just the crash. Creating these
    directories is the body's job; outliving it is not.

    Each site still removes its own at the moment that is right for it — the scratch dir the
    instant the agent exits, `exec_root` only once the workspace has been moved out of it —
    and whoever runs last sweeps whatever is left.
    """

    def __init__(self, agent: str) -> None:
        self._agent = agent
        self._owned: list[tuple[str, str, bool, str]] = []
        self._notes: list[tuple[bool, str]] = []

    def own(self, label: str, path: Optional[str], *, fatal: bool = True,
            tail: Optional[str] = None) -> None:
        """Register a directory as this cell's to remove; `None` is a no-op.

        `fatal` says what a failure to remove it MEANS. The credential directories fail the
        cell: a resolved `${VAR}` outliving the run that resolved it is not a result anyone
        should trust. A directory holding only harness-built masks and symlinks is a leaked
        temp directory rather than a leaked secret, and lands on `warnings` instead. The
        distinction is a flag and not two classes because both need the same registration,
        the same verified removal, and the same guarantee that the answer outlives the frame
        that asked for it.

        Re-registering a path REPLACES its severity, which is the point rather than a
        convenience: what a directory contains is not fixed at creation. The isolated HOME
        is `$HOME` for a child that can write, so the harness knows its initial contents and
        not its final ones — review copied a resolved token into it and watched a failed
        removal reported as holding no credentials. Anything the agent can write to is
        credential-bearing from the moment this cell has credentials.
        """
        if not path:
            return
        self._owned = [e for e in self._owned if e[1] != path]
        self._owned.append(
            (label, path, fatal, tail or (_CREDENTIAL_TAIL if fatal else _TEMPDIR_TAIL)))

    def note(self, text: str, *, fatal: bool = True) -> None:
        """Record a finding that is not about a directory this object removes.

        The workspace scrub's verdict travels the same way for the same reason: it was a
        body local, so a raise after an uncertifiable file had already been DELETED reached
        `_failed_cell`, which rescanned a now-clean tree, found nothing to say, and turned an
        evidence deletion into a silent one. A finding that cannot be recomputed must not be
        held anywhere that a raise can discard.
        """
        if text and (fatal, text) not in self._notes:
            self._notes.append((fatal, text))

    def purge(self, path: Optional[str]) -> None:
        """Remove one registered directory, keeping any failure; `None` is a no-op.

        Deliberately NOT a "purge everything" default: the callers pass a variable that is
        `None` whenever the cell declared no MCP servers, and a `None`-means-all sentinel
        would have swept `exec_root` while the agent's workspace was still inside it.
        """
        for entry in [e for e in self._owned if path and e[1] == path]:
            self._owned.remove(entry)
            label, owned, fatal, tail = entry
            self.note(_purge(label, owned, tail), fatal=fatal)

    def purge_all(self) -> None:
        """Remove every directory still registered, most recently created first."""
        for _, path, _, _ in list(reversed(self._owned)):
            self.purge(path)

    def pending(self) -> list[tuple[bool, str]]:
        """The failures recorded so far, WITHOUT forgetting them.

        Reading used to drain, which put the note in a local again — one frame later than
        before, and just as lossy: review made the `result.json` rewrite raise between the
        read and the write, and the scratch-dir failure vanished from a cell that then
        reported only the write error. A note is owed to a reader until it reaches one, so
        handing it out and forgetting it are separate steps (see `acknowledge`).
        """
        return list(self._notes)

    def acknowledge(self, notes: list[tuple[bool, str]]) -> None:
        """Forget failures that are now ON DISK — never merely handed to something.

        Call this after the artifact writes that carry them have returned, not before. What
        is still here when `flush` runs is exactly what no artifact ever recorded.
        """
        for note in notes:
            if note in self._notes:
                self._notes.remove(note)

    def flush(self) -> None:
        """Last stop: echo whatever never reached an artifact, and forget it.

        `_run_cell`'s finally is the final moment anything knows these directories existed.
        Reaching here means both the success and failure paths were unable to write the
        finding down, so stderr is all that is left — better than the silence that a
        drain-on-read gave when the write behind it failed.
        """
        notes, self._notes = self._notes, []
        for _, note in notes:
            warn(f"warning: [{self._agent}] {note}")


def _record_notes(rr: RunResult, notes: list[tuple[bool, str]]) -> bool:
    """Put cleanup failures on a result; return True if any of them must fail the cell.

    Fatal ones join `error`, the field tooling greps. The rest join `warnings`, which
    `notices.py` already routes to cell.json, report.md and summary.json — so a leaked
    temp directory is still named somewhere durable without pretending the cell's output
    is untrustworthy.
    """
    fatal_seen = False
    for fatal, note in notes:
        if fatal:
            rr.error = (rr.error + "; " if rr.error else "") + note
            fatal_seen = True
        elif note not in rr.warnings:
            rr.warnings.append(note)
    return fatal_seen


def _all_offending_paths(root: str, secrets) -> list[str]:
    """Every path under *root* that, ASSEMBLED, reads back as a secret — shallowest first."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            if _spells_secret(rel, secrets):
                found.append((rel.count("/"), path))
    return [path for _, path in sorted(found)]


def _first_offending_path(root: str, secrets) -> Optional[str]:
    """The SHALLOWEST offending path, which is the one worth renaming.

    Shallowest rather than deepest because renaming one component fixes every path below
    it: for a secret `tenant/key`, renaming `key` cleans the whole subtree in one step,
    where starting at the leaves would mangle every descendant on the way up.
    """
    found = _all_offending_paths(root, secrets)
    return found[0] if found else None


def _scrub_tree(root: str, secrets) -> list[str]:
    """Rewrite any archived workspace entry that carries a declared secret.

    Returns the paths, relative to *root*, that could NOT be certified clean. Each one has
    already been REMOVED from the tree: an artifact the harness cannot prove is scrubbed
    must not be published, and the caller fails the cell so that deletion is never silent.
    An empty list is a positive statement — every byte, every symlink target and every
    assembled path under `root` was examined.

    Bottom-up, so a directory is renamed only after its contents are done. Every entry is
    classified by ``lstat``, because "not a directory" is not the same claim as "a readable
    regular file" — review hung the whole scrub on a FIFO that `open(...).read()` waited on
    forever. Five things get scrubbed, because review found a leak in each:

    * **Contents**, onto a fresh inode (see ``_scrub_file``).
    * **Extended attributes**, which no read of a file's bytes will ever show.
    * **Symlink targets**, also onto a fresh inode (see ``_scrub_link``). The link is never
      followed, but its target STRING is archived and `readlink` reads it straight back.
    * **Names**, one component at a time, and then
    * **assembled paths**, to a fixed point; see ``_spells_secret``.

    Special files (FIFO, socket, device) are removed rather than read: none of them holds
    archivable bytes, and none of them can be certified.
    """
    if not secrets:
        return []
    try:
        st = os.lstat(root)
    except FileNotFoundError:
        return []  # nothing was archived, so there is nothing to certify
    except OSError:
        # Absence and REFUSAL are different answers, and a bare `except OSError: return []`
        # gave them the same one: review chmod-ed the cell directory to 000 and got a clean
        # bill of health over a workspace still holding the secret. The parent is `cell_dir`
        # — created by this harness, in the artifact tree, ours to repair — so try that once.
        _relax_dir(os.path.dirname(root) or ".")
        try:
            st = os.lstat(root)
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise ScrubFailed(
                f"secret scrub could not even inspect {root} ({exc.strerror}) — the archived "
                f"workspace cannot be certified free of declared secrets, and cannot be "
                f"removed either; delete it before sharing this run") from exc
    if not stat.S_ISDIR(st.st_mode):
        # `os.path.isdir` FOLLOWS symlinks, and review used that to aim the entire scrub at
        # an external directory: every file under it came back rewritten to «redacted». A
        # workspace that is not itself a real directory is not a tree to walk — the link is
        # dropped whole, and an empty directory left in its place so the artifact keeps the
        # shape the report writer downstream expects.
        _remove(root)
        os.makedirs(root, exist_ok=True)
        return ["<workspace root: not a directory>"]
    _make_traversable(root)
    lost: set[str] = set()
    stuck: set[str] = set()
    blocked: list[str] = []

    def _rel(path: str) -> str:
        return os.path.relpath(path, root).replace(os.sep, "/")

    def _give_up(path: str) -> None:
        """Delete what could not be certified, and remember it for the caller."""
        lost.add(_rel(path))
        if not _remove(path):
            stuck.add(_rel(path))

    def _rename(dirpath: str, name: str, new: str) -> None:
        dst = os.path.join(dirpath, new)
        n = 1
        while os.path.lexists(dst):
            dst = os.path.join(dirpath, f"{new}.{n}")
            n += 1
        try:
            os.rename(os.path.join(dirpath, name), dst)
        except OSError:
            _give_up(os.path.join(dirpath, name))

    # The root carries metadata like any other directory, and it is the one entry the walk
    # below never visits — nor could the quarantine remove it if it failed.
    try:
        _scrub_xattrs(root, secrets)
    except OSError:
        stuck.add("<workspace root: extended attributes>")

    walk = os.walk(root, topdown=False,
                   onerror=lambda exc: blocked.append(getattr(exc, "filename", "") or root))
    for dirpath, dirnames, filenames in walk:
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            try:
                mode = os.lstat(path).st_mode
                if stat.S_ISLNK(mode):
                    _scrub_link(path, secrets)
                elif stat.S_ISDIR(mode):
                    _scrub_xattrs(path, secrets)
                elif stat.S_ISREG(mode):
                    _scrub_file(path, secrets)
                else:
                    _give_up(path)  # FIFO, socket, device: unreadable by construction
            except OSError:
                _give_up(path)
        for name in dirnames + filenames:
            new = redact(name, secrets)
            if new != name:
                _rename(dirpath, name, new)

    # Assembled paths, to a fixed point. A component that is clean on its own can still
    # complete a secret together with its parent — and review showed that fixing that on the
    # way past is not enough: with two secrets declared, renaming a parent to remove the
    # first CREATED the second across the new parent and an untouched child, which the walk
    # had already gone by. `secretAtailpart/childsecret` became `«redacted»tailpart/
    # childsecret`, spelling `tailpart/childsecret` and reported clean. So the tree is
    # re-examined until nothing spells anything, rather than judged once per component.
    for _ in range(_SCRUB_ROUNDS):
        offender = _first_offending_path(root, secrets)
        if offender is None:
            break
        parent, name = os.path.split(offender)
        _rename(parent, name, REDACTED)
    else:
        # Renaming is not converging — a secret that contains the redaction marker itself
        # would do that. Stop rewriting and start deleting, from a snapshot rather than a
        # re-query: an offender `_remove` cannot delete would otherwise be handed back on
        # every pass forever.
        for offender in _all_offending_paths(root, secrets):
            if os.path.lexists(offender):
                _give_up(offender)

    for path in blocked:
        if os.path.lexists(path):
            _give_up(path)
    if stuck:
        raise ScrubFailed(
            f"secret scrub could not certify {_shown(sorted(stuck))} AND could not remove "
            f"it — the archived workspace still contains a declared secret; delete "
            f"{root} before sharing this run")
    return sorted(lost)


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


def _apply_workspace_config_masks(adapter, workspace: str) -> list[str]:
    """Overwrite seeded workspace files matching the adapter's ``workspace_config_masks``
    globs with their neutral content — MCP configs some CLIs discover from the run
    workspace itself (agy's ``.agents/mcp_config.json``), which no HOME overlay or CLI
    flag reaches. Only *existing* files are touched: pre-creating a config would pollute
    the workspace the agent sees and the archived artifacts. A seeded symlink resolving
    outside the workspace is *neutralized*, not skipped: every escaping symlink component
    is unlinked (never followed for the write — the outside target must not be touched)
    and the path is rebuilt as a real file with the neutral content, so the CLI can't
    read the outside config through the link. Returns the workspace-relative paths
    neutralized."""
    masked: list[str] = []
    ws_real = os.path.realpath(workspace)
    for pattern, content in (getattr(adapter, "workspace_config_masks", {}) or {}).items():
        full_pattern = os.path.join(glob.escape(workspace), *pattern.split("/"))
        for path in glob.glob(full_pattern):
            if not os.path.isfile(path):
                continue
            if not _unlink_escaping_symlinks(workspace, ws_real, path):
                continue  # couldn't make the path safely writable — leave it unmatched
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
            masked.append(os.path.relpath(path, workspace))
    return sorted(masked)


def _unlink_escaping_symlinks(workspace: str, ws_real: str, path: str) -> bool:
    """Make ``path`` (a glob match under ``workspace``) safe to rewrite in place: unlink
    every symlink component under the workspace that resolves outside it (the final file
    or any intermediate directory), so a subsequent write lands inside the workspace
    instead of following a seeded link out. Symlinks resolving *within* the workspace
    are left alone. Returns False when containment can't be established."""
    rel = os.path.relpath(path, workspace)
    if rel == os.curdir or rel.startswith(os.pardir + os.sep) or rel == os.pardir:
        return False
    parts = rel.split(os.sep)
    for _ in range(len(parts) + 1):
        cur = workspace
        escaping = None
        for part in parts:
            cur = os.path.join(cur, part)
            if os.path.islink(cur):
                real = os.path.realpath(cur)
                if real != ws_real and not real.startswith(ws_real + os.sep):
                    escaping = cur
                    break
        if escaping is None:
            real = os.path.realpath(path)
            return real == ws_real or real.startswith(ws_real + os.sep)
        os.unlink(escaping)
    return False  # pragma: no cover — more escapes than path components can't happen
