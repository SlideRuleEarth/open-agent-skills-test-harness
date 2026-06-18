"""Process execution: run one adapter's CLI and normalize the result.

Shared by the runner (running evals) and the judge (grading). Keeps all the
subprocess/timeout/error handling in one place so adapters stay pure.
"""

from __future__ import annotations

import os
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
    argv = adapter.build_argv(prompt, opts)
    env = adapter.env(dict(os.environ), opts)
    if env_overrides:
        env.update(env_overrides)

    rr = RunResult(
        agent=agent_name or adapter.name,
        eval_name=eval_name,
        prompt=prompt,
        workdir=cwd,
        argv=argv,
    )

    if not adapter.is_available():
        rr.error = f"{adapter.binary!r} not found on PATH"
        return ExecResult(rr, "", "")

    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        stdout, stderr, code = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as exc:
        # On TimeoutExpired the partial stdout/stderr can come back as bytes even with
        # text=True, so decode BEFORE appending the timeout note (else: can't concat str+bytes).
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", "replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        stderr += f"\n[timeout after {timeout}s]"
        rr.timed_out = True
        rr.error = f"timed out after {timeout}s"
        code = -9
    except FileNotFoundError:
        rr.error = f"{adapter.binary!r} not found on PATH"
        return ExecResult(rr, "", "")
    except Exception as exc:  # pragma: no cover - defensive
        rr.error = f"exec failed: {exc}"
        return ExecResult(rr, "", "")

    rr.exit_code = code
    try:
        parsed = adapter.parse(stdout, stderr, code)
        rr.events = parsed.events
        rr.final_text = parsed.final_text
        rr.structured_output = parsed.structured_output
        rr.cost_usd = parsed.cost_usd
        rr.duration_ms = parsed.duration_ms
    except Exception as exc:  # parsing must never crash a run
        rr.error = (rr.error + "; " if rr.error else "") + f"parse failed: {exc}"

    # A nonzero exit is a failed run — surface it so the cell is marked failed and
    # the model-rejection annotation (runner) can fire on bad model ids.
    if code != 0 and not rr.error and not rr.timed_out:
        tail = _tail(stderr) or _tail(stdout) or "(no stderr)"
        rr.error = f"{adapter.binary} exited with code {code}: {tail}"

    return ExecResult(rr, stdout, stderr)


def _tail(text: str | None, limit: int = 400) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    return text[-limit:].replace("\n", " ⏎ ")
