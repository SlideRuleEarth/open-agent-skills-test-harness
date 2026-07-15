"""Process execution: run one adapter's CLI and normalize the result.

Shared by the runner (running evals) and the judge (grading). Keeps all the
subprocess/timeout/error handling in one place so adapters stay pure.
"""

from __future__ import annotations

import dataclasses
import os
import signal
import subprocess
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
        # start_new_session puts the agent in its OWN process group: agent CLIs spawn
        # children (shell tool commands, MCP servers), and subprocess.run's timeout kill
        # only reaches the direct child — orphaned grandchildren would keep burning API
        # budget and keep writing into the workspace while the runner relocates it.
        # Killing the whole group on timeout reaps them too.
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
    except FileNotFoundError:
        rr.error = f"{adapter.binary!r} not found on PATH"
        return ExecResult(rr, "", "")
    except Exception as exc:  # pragma: no cover - defensive
        rr.error = f"exec failed: {exc}"
        return ExecResult(rr, "", "")

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        try:
            # The group is dead, so the pipes close and this returns quickly with all
            # output accumulated so far (communicate() retried after a timeout loses none).
            stdout, stderr = proc.communicate(timeout=10)
        except (subprocess.TimeoutExpired, ValueError, OSError):  # pragma: no cover
            proc.kill()
            stdout, stderr = "", ""
        stdout = stdout or ""
        stderr = (stderr or "") + f"\n[timeout after {timeout}s]"
        rr.timed_out = True
        rr.error = f"timed out after {timeout}s"
        code = -9
    except Exception as exc:  # pragma: no cover - defensive
        _kill_process_group(proc)
        rr.error = f"exec failed: {exc}"
        return ExecResult(rr, "", "")

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

    return ExecResult(rr, stdout, stderr)


def _kill_process_group(proc: subprocess.Popen) -> None:
    """SIGKILL the agent's whole process group (POSIX); fall back to killing just the
    direct child where process groups aren't available (Windows) or the group is gone."""
    if hasattr(os, "killpg"):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
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
