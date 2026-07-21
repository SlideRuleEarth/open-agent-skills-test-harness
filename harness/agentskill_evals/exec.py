"""Process execution: run one adapter's CLI and normalize the result.

Shared by the runner (running evals) and the judge (grading). Keeps all the
subprocess/timeout/error handling in one place so adapters stay pure.
"""

from __future__ import annotations

import dataclasses
import os
import signal
import subprocess
import sys
from dataclasses import dataclass

from .adapters.base import Adapter, RunOptions
from .schema import RunResult


@dataclass
class ExecResult:
    result: RunResult
    stdout: str
    stderr: str


def execute(
    adapter: Adapter,
    prompt: str,
    opts: RunOptions,
    *,
    cwd: str,
    timeout: int,
    env_overrides: dict[str, str] | None = None,
    agent_name: str | None = None,
    eval_name: str = "",
) -> ExecResult:
    # Apply the eval/scenario env first, then let adapter.env() layer isolation on top — so an
    # isolated run's HOME / XDG / config-home vars can't be overridden by an eval's `env:`.
    base = dict(os.environ)
    if env_overrides:
        base.update(env_overrides)
    # ...then fold case-colliding keys on Windows BEFORE isolation/enumeration see them:
    # process.env is case-insensitive there, so a scenario `env: {copilot_home: ...}` that
    # dodges isolation's case-sensitive `COPILOT_HOME` pop would still be read by the child
    # as COPILOT_HOME, escaping the overlay (see _fold_env_keys_case_insensitive).
    if sys.platform == "win32":  # pragma: no cover — win32 only
        base = _fold_env_keys_case_insensitive(base)
    env = adapter.env(base, opts)

    rr = RunResult(
        agent=agent_name or adapter.name,
        eval_name=eval_name,
        prompt=prompt,
        workdir=cwd,
    )

    # env is computed BEFORE argv on purpose: an adapter whose argv depends on ambient
    # state (codex enumerates MCP servers to disable them by name) must see the child's
    # exact context — same cwd, same env (a scenario's `env: {CODEX_HOME: ...}` override,
    # an isolated run's repointed HOME) — or it enumerates the wrong config. An argv
    # construction failure is a failed run (fail closed), not a crash: an adapter raises
    # when it can't guarantee a hermetic invocation (e.g. MCP servers it can't enumerate).
    opts = dataclasses.replace(opts, effective_env=env)
    try:
        argv = adapter.build_argv(prompt, opts, cwd=cwd)
    except Exception as exc:
        rr.error = f"could not construct a hermetic invocation: {exc}"
        return ExecResult(rr, "", "")
    rr.argv = argv

    if not adapter.is_available():
        rr.error = f"{adapter.binary!r} not found on PATH"
        return ExecResult(rr, "", "")

    try:
        stdout, stderr, code, timed_out = run_captured(
            argv, cwd=cwd, env=env, timeout=timeout)
    except FileNotFoundError:
        rr.error = f"{adapter.binary!r} not found on PATH"
        return ExecResult(rr, "", "")
    except Exception as exc:  # pragma: no cover - defensive
        rr.error = f"exec failed: {exc}"
        return ExecResult(rr, "", "")
    if timed_out:
        stderr += f"\n[timeout after {timeout}s]"
        rr.timed_out = True
        rr.error = f"timed out after {timeout}s"

    rr.exit_code = code
    try:
        parsed = adapter.parse(stdout, stderr, code, opts=opts)
        rr.events = parsed.events
        rr.final_text = parsed.final_text
        rr.structured_output = parsed.structured_output
        rr.cost_usd = parsed.cost_usd
        rr.premium_requests = parsed.premium_requests
        rr.duration_ms = parsed.duration_ms
        rr.resolved_model = parsed.resolved_model
    except Exception as exc:  # parsing must never crash a run
        rr.error = (rr.error + "; " if rr.error else "") + f"parse failed: {exc}"

    # A nonzero exit is a failed run — surface it so the cell is marked failed and
    # the model-rejection annotation (runner) can fire on bad model ids.
    if code != 0 and not rr.error and not rr.timed_out:
        tail = _tail(stderr) or _tail(stdout) or "(no stderr)"
        rr.error = f"{adapter.binary} exited with code {code}: {tail}"

    # build_argv decided hermeticity by READING state the child then read again for
    # itself; the launch window sits between those two reads, and the child runs code
    # inside it (copilot execs `git rev-parse` before it globs custom-agent dirs). So the
    # state is re-read now that the child is gone, and an adapter that finds it moved
    # fails the run — see Adapter.verify_post_run. The child's output goes with it: state
    # that was changed and then changed BACK inside the window re-reads clean, and only
    # the child's own report of the servers it brought up still shows it. Detection, not
    # prevention: a server started inside the window already ran. It is appended, never
    # substituted, so a timeout or a nonzero exit keeps its own diagnosis alongside.
    try:
        adapter.verify_post_run(argv, opts, cwd=cwd, stdout=stdout, stderr=stderr)
    except Exception as exc:
        rr.error = ((rr.error + "; " if rr.error else "")
                    + f"MCP hermeticity was not confirmed after the run: {exc}")

    return ExecResult(rr, stdout, stderr)


def _fold_env_keys_case_insensitive(env: dict[str, str]) -> dict[str, str]:
    """Collapse keys that differ only in case into a single uppercase key.

    Applied to the child env on Windows only (see the ``sys.platform`` gate at the call
    site), but kept platform-independent so it is unit-testable on any host. Windows
    ``process.env`` lookups are case-insensitive (Node documents this), and the env block
    CreateProcess hands a child is effectively case-folded. Python's ``os.environ`` already
    uppercases its keys on Windows, but a scenario's ``env:`` overrides do not — so
    ``env: {copilot_home: C:\\real}`` merges in as a distinct lowercase key. Isolation's
    ``env.pop("COPILOT_HOME")`` (base.py) and copilot's ``env_map.get("COPILOT_HOME")``
    enumeration both match the canonical uppercase name and would MISS it, while the child's
    Node reads it as ``COPILOT_HOME`` and loads the un-isolated home — an isolation escape.
    Folding every key to uppercase (last assignment wins in ``env``'s insertion order, so a
    scenario override applied after ``os.environ`` takes effect, and isolation then
    mirrors/clears it like any ``COPILOT_HOME``) keeps the harness's view of config-home
    vars identical to the child's. Not applied off win32, where environment variables are
    genuinely case-sensitive and the child treats a lowercase key as its own variable."""
    canon: dict[str, str] = {}
    for k, v in env.items():
        canon[k.upper()] = v
    return canon


def run_captured(argv: list[str], *, cwd: str, env: dict[str, str],
                 timeout: int) -> tuple[str, str, int, bool]:
    """Run *argv* to completion in its OWN process group, capturing stdout/stderr.

    Returns ``(stdout, stderr, exit_code, timed_out)``; on timeout the whole group is
    SIGKILLed, the output accumulated so far is returned, and the code is -9. Raises only
    what spawning can raise (FileNotFoundError / OSError) — the caller decides what a
    failure to start means.

    The process group is the point. Agent CLIs spawn children (shell tool commands, MCP
    servers), and the timeout kill that ``subprocess.run`` performs reaches only the
    direct child: orphaned grandchildren keep burning API budget and keep writing into the
    workspace while the runner relocates it — and, for an MCP server, keep RUNNING past
    the run the harness is auditing. Every place the harness launches an agent goes
    through here so no launch path can quietly lack that guarantee; model probes used to
    call ``subprocess.run`` directly and leaked exactly those grandchildren.
    """
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        stdin=subprocess.DEVNULL,  # non-interactive: agents that probe stdin (e.g. codex
                                   # exec) get immediate EOF instead of blocking forever
        start_new_session=hasattr(os, "killpg"),
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return (stdout or ""), (stderr or ""), proc.returncode, False
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        try:
            # The group is dead, so the pipes close and this returns quickly with all
            # output accumulated so far (communicate() retried after a timeout loses none).
            stdout, stderr = proc.communicate(timeout=10)
        except (subprocess.TimeoutExpired, ValueError, OSError):  # pragma: no cover
            proc.kill()
            stdout, stderr = "", ""
        return (stdout or ""), (stderr or ""), -9, True
    except BaseException:  # pragma: no cover - defensive
        # Never leave the group running because the wait was interrupted (KeyboardInterrupt
        # included) — the orphan outlives the harness otherwise.
        _kill_process_group(proc)
        raise


def _kill_process_group(proc: subprocess.Popen) -> None:
    """SIGKILL the agent's whole process group (POSIX); fall back to killing just the
    direct child where process groups aren't available (Windows) or the group is gone.

    Never the HARNESS's own group. That is only possible if the child was spawned without
    ``start_new_session``, which run_captured always passes — but the failure mode is
    severe enough to guard rather than assume: the group SIGKILL would then reach this
    process and everything above it, taking down the whole run (and, under the self-test,
    the shell that launched it — observed while mutation-testing exactly that change).
    Killing just the child is the correct degradation; it is what Windows already does."""
    if hasattr(os, "killpg"):
        try:
            pgid = os.getpgid(proc.pid)
            if pgid != os.getpgid(0):
                os.killpg(pgid, signal.SIGKILL)
                return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.kill()
    except OSError:  # pragma: no cover — already gone
        pass


def _tail(text: str | None, limit: int = 400) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    return text[-limit:].replace("\n", " ⏎ ")
