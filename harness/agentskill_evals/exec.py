"""Process execution: run one adapter's CLI and normalize the result.

Shared by the runner (running evals) and the judge (grading). Keeps all the
subprocess/timeout/error handling in one place so adapters stay pure.
"""

from __future__ import annotations

import ctypes
import dataclasses
import os
import signal
import subprocess
import sys
from ctypes import wintypes
from dataclasses import dataclass
from typing import Optional

from .adapters.base import Adapter, RunOptions
from .schema import RunResult


@dataclass
class ExecResult:
    result: RunResult
    stdout: str
    stderr: str


class ChildSpawned(Exception):
    """Raised in place of any error that happens AFTER the child process started.

    Spawning failures and post-spawn failures demand opposite handling, and both used to
    arrive as a bare ``OSError``. If ``Popen`` itself fails there is no child, nothing
    ran, and nothing needs auditing. If ``communicate()`` fails there IS a child — it may
    have brought an MCP server up before the pipe broke — so the run still has to go
    through ``verify_post_run``. Collapsing the two let a post-spawn ``OSError`` skip the
    audit on a child that had genuinely run.

    Carries whatever output was recovered, so the caller can audit the child's own
    account of itself rather than only its absence.
    """

    def __init__(self, exc: BaseException, stdout: str = "", stderr: str = ""):
        super().__init__(str(exc))
        self.stdout = stdout
        self.stderr = stderr


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

    # A failure to SPAWN returns here: no child ran, so there is nothing to audit. A
    # failure AFTER the child started (ChildSpawned) must not take that exit — the child
    # may have brought an MCP server up before the pipe broke, so it falls through to
    # parse and, crucially, to verify_post_run like any other run that happened.
    child_error = ""
    try:
        stdout, stderr, code, timed_out = run_captured(
            argv, cwd=cwd, env=env, timeout=timeout)
    except ChildSpawned as exc:
        stdout, stderr, code, timed_out = exc.stdout, exc.stderr, -1, False
        child_error = f"exec failed after the child started: {exc}"
    except FileNotFoundError:
        rr.error = f"{adapter.binary!r} not found on PATH"
        return ExecResult(rr, "", "")
    except Exception as exc:  # pragma: no cover - defensive
        rr.error = f"exec failed: {exc}"
        return ExecResult(rr, "", "")
    if child_error:
        rr.error = child_error
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
        adapter._verify_post_run_compat(argv, opts, cwd=cwd, stdout=stdout,
                                        stderr=stderr, exit_code=code)
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

    The group is swept on EVERY return, not only on timeout — see ``_ProcessTree.close``.
    A clean exit says the agent finished, never that its descendants did.

    Raises ``ChildSpawned`` if the tree could not be contained at launch — which on
    Windows is EVERY run, because assignment-after-CreateProcess cannot contain a
    grandchild spawned in the launch window (see ``_ProcessTree.contained``). A child the
    harness cannot guarantee it can kill is a failed run, not a quietly weaker one, and
    "the harness has no Windows support yet" is a far better thing to ship than a
    hermeticity guarantee that is unsound on one platform and says nothing about it.
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
        start_new_session=_HAS_KILLPG,
    )
    tree = _ProcessTree(proc)   # captured AT LAUNCH — see the class docstring
    try:
        # Fail closed on a tree that cannot be contained. Degrading to "kill the one
        # process we know about" is how a leaked MCP server survives its own run, and it
        # does so silently — the run reports success. The child has already started, so
        # this is a ChildSpawned like any other post-spawn failure: the caller still
        # parses and audits whatever it managed to say. Raised OUTSIDE the handlers
        # below so it is not re-wrapped as an unexpected post-spawn error.
        if not tree.contained:
            tree.kill()
            raise ChildSpawned(RuntimeError(
                f"this run's process tree cannot be contained, so a grandchild the agent "
                f"leaves behind — an MCP server, most of all — could outlive the run "
                f"while it reported success: {tree.why_uncontained}"))
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            return (stdout or ""), (stderr or ""), proc.returncode, False
        except subprocess.TimeoutExpired:
            tree.kill()
            try:
                # The tree is dead, so the pipes close and this returns quickly with all
                # output accumulated so far (communicate() retried after a timeout loses
                # none).
                stdout, stderr = proc.communicate(timeout=10)
            except (subprocess.TimeoutExpired, ValueError, OSError):  # pragma: no cover
                proc.kill()
                stdout, stderr = "", ""
            return (stdout or ""), (stderr or ""), -9, True
        except BaseException as exc:
            # Never leave the tree running because the wait was interrupted
            # (KeyboardInterrupt included) — the orphan outlives the harness otherwise.
            tree.kill()
            # A post-spawn failure is reported as ChildSpawned so the caller can tell it
            # from "never launched" and still audit the child that DID run; control-flow
            # BaseExceptions (KeyboardInterrupt, SystemExit) propagate untouched.
            if isinstance(exc, Exception):
                raise ChildSpawned(exc) from exc
            raise
    finally:
        tree.close()


_HAS_KILLPG = hasattr(os, "killpg")

# Windows Job Object constants (winnt.h). Only these two are needed: the extended-limit
# info class to request kill-on-close, and the flag itself.
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000


def _win32_kernel32():  # pragma: no cover — win32 only; stubbed in the selftest
    """The kernel32 handle ``_win32_assign_job`` calls through. A seam, not a wrapper:
    it is the single point the selftest replaces to exercise the job-object call
    sequence on a non-Windows host, exactly as ``winreg`` is stubbed for the ODR read."""
    import ctypes
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _win32_assign_job(proc: subprocess.Popen) -> Optional[int]:
    """Put *proc* into a fresh Windows Job Object; return the job handle, or None.

    A Job Object is the only Windows mechanism that reliably kills a process TREE, and
    the tree is what has to die: an agent CLI's MCP server is a grandchild. The
    alternatives both fail on the case that motivates this — a parent that has already
    exited. ``proc.kill()`` reaches exactly one process, and ``taskkill /T`` walks live
    parent/child links, which are precisely what an exited parent no longer provides.
    Every process created by a job member joins the job automatically, so
    ``TerminateJobObject`` reaches the grandchildren no matter who is still alive.

    EVERY step is checked, and None means the caller must fail the run rather than
    proceed with weaker containment (see ``_ProcessTree.contained``). That includes the
    kill-on-close limit: it is what kills the tree if the harness itself is killed before
    it can terminate the job, so a job that will not accept it does not contain the tree
    under the failure it exists for. A modern Windows that refuses any of these is
    anomalous enough to stop for.

    ``IsProcessInJob`` re-reads the outcome from the kernel rather than trusting the
    ``AssignProcessToJobObject`` return alone. It is the one check that catches an
    assignment that "succeeded" against a process which had already exited — the exact
    shape that made the POSIX path fail open (see ``_ProcessTree``).

    THE LAUNCH RACE IS REAL AND IS NOT CLOSED HERE, WHICH IS WHY WINDOWS RUNS FAIL.
    Assignment happens immediately after ``Popen`` returns, so a child that spawns a
    grandchild in that window leaves that grandchild outside the job — permanently, since
    Windows only associates a member's FUTURE children. Microsoft's own answer is to
    create the process already in the job via ``PROC_THREAD_ATTRIBUTE_JOB_LIST``, which
    stdlib ``Popen`` cannot express: ``STARTUPINFO.lpAttributeList`` is honoured for
    ``handle_list`` and nothing else, and ``CREATE_SUSPENDED`` is no help because Popen
    closes the child's thread handle and leaves no supported way to resume it. Closing
    this needs either a bespoke ``CreateProcess`` or a job-resident launcher shim; both
    are Windows-only code that cannot be verified without a Windows host.

    So this job does NOT make a run containable — ``_ProcessTree.contained`` is False on
    win32 and ``run_captured`` refuses the run. What the job is still worth is the kill:
    it is the only mechanism that reaches a grandchild whose parent has already exited, so
    it is what sweeps the child that did start before the run is failed. Keeping the call
    sequence correct and tested also means the follow-up that adds job-at-creation changes
    one predicate rather than starting from nothing.

    NOT verified against a live Windows host: none was available. The ctypes call
    sequence is exercised cross-host in the selftest against a stubbed kernel32, the
    same way copilot's ODR registry read is exercised against a stubbed ``winreg``.
    """
    handle = getattr(proc, "_handle", None)
    if handle is None:
        return None
    job = None
    try:  # pragma: no cover — win32 only
        k32 = _win32_kernel32()
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        k32.SetInformationJobObject.restype = wintypes.BOOL
        k32.AssignProcessToJobObject.restype = wintypes.BOOL
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.IsProcessInJob.restype = wintypes.BOOL

        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None
        if not k32.SetInformationJobObject(
                wintypes.HANDLE(job), _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(_kill_on_close_limits()),
                ctypes.sizeof(_JOBOBJECT_EXTENDED_LIMIT_INFORMATION)):
            return _win32_close(k32, job)
        if not k32.AssignProcessToJobObject(wintypes.HANDLE(job),
                                            wintypes.HANDLE(int(handle))):
            return _win32_close(k32, job)
        member = wintypes.BOOL(0)
        if not k32.IsProcessInJob(wintypes.HANDLE(int(handle)), wintypes.HANDLE(job),
                                  ctypes.byref(member)) or not member.value:
            return _win32_close(k32, job)
        return int(job)
    except Exception:
        if job:  # pragma: no cover — win32 only
            try:
                _win32_close(_win32_kernel32(), job)
            except Exception:
                pass
        return None


def _win32_close_handle(k32, job: int) -> bool:  # pragma: no cover — win32 only
    """``CloseHandle``, reporting whether the kernel actually accepted it.

    A false return means the handle is still open, which matters here for one specific
    reason: a job handle that will not close is a job whose kill-on-close backstop can
    never fire. The callers that can act on that do (see ``_ProcessTree.close``); the
    abandon paths cannot, and only leak a handle in a process that is already failing."""
    try:
        k32.CloseHandle.restype = wintypes.BOOL
        return bool(k32.CloseHandle(wintypes.HANDLE(job)))
    except Exception:
        return False


def _win32_close(k32, job: int) -> None:  # pragma: no cover — win32 only
    """Close a job handle and report the failure as None, so every abandon path in
    ``_win32_assign_job`` is one statement and none of them can leak the handle."""
    _win32_close_handle(k32, job)
    return None


def _win32_sweep_job(job: int) -> bool:  # pragma: no cover — win32 only
    """Terminate everything in *job*, then release the handle. True only if BOTH worked.

    Order matters: terminate first, close second. Closing a kill-on-close job is *also* a
    kill, but only if the close succeeds, so a sweep built on it silently does nothing on
    the one failure it would need to survive. Terminating first makes the close pure
    hygiene — and it is still checked, because a handle that would not close is evidence
    the kernel disagrees about the state of this job, which is not a thing to shrug at
    while claiming the tree is dead."""
    try:
        k32 = _win32_kernel32()
    except Exception:
        return False
    try:
        k32.TerminateJobObject.restype = wintypes.BOOL
        terminated = bool(k32.TerminateJobObject(wintypes.HANDLE(job), 1))
    except Exception:
        terminated = False
    closed = _win32_close_handle(k32, job)   # attempted even after a failed terminate
    return terminated and closed


# The winnt.h layouts SetInformationJobObject expects. Written with EXPLICIT widths
# rather than `wintypes` aliases: `wintypes.DWORD` is `c_ulong`, which is 4 bytes on
# Windows (LLP64) but 8 on an LP64 host, so an alias-built struct would silently take a
# different shape off Windows — 72 bytes instead of 64 — and the cross-host selftest
# would then be validating a layout Windows never sees. `c_uint32` is 4 everywhere and
# identical to Windows' DWORD; `c_size_t` matches ULONG_PTR on the host that matters.
# Defining them at module scope is safe on every platform (these are plain ctypes types),
# which is what keeps the job-object call sequence testable off Windows at all.
class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(_name, ctypes.c_uint64) for _name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),              # DWORD
                ("MinimumWorkingSetSize", ctypes.c_size_t),   # ULONG_PTR
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),                # ULONG_PTR
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32)]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


def _kill_on_close_limits() -> _JOBOBJECT_EXTENDED_LIMIT_INFORMATION:
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    return info


class _ProcessTree:
    """Kill handle for every process a launch creates, not just the direct child.

    The identity of the tree is captured AT LAUNCH, and that is the whole point. By the
    time the kill is needed the direct child may be gone: an agent CLI can start an MCP
    server, exit, and leave the server holding the captured stdout pipe open —
    ``communicate()`` then blocks on that pipe until the timeout, having already reaped
    the leader via its own ``poll()``. Deriving the target at kill time from the leader
    (``os.getpgid(proc.pid)``) raises ``ProcessLookupError`` in exactly that case and
    silently degrades to killing a process that is already dead, while the server keeps
    running. Reproduced before this change: a 1s timeout returned after 10.1s with the
    grandchild not merely alive but running to completion.

    POSIX: ``start_new_session`` makes the child a session and process-group leader, so
    the group id IS its pid — known without a syscall and with no window in which it can
    be wrong. A pid that is in use as a process-group id is not recycled while the group
    still has members, so a stored pgid either names that same group or names nothing
    (``ESRCH``); it cannot drift onto an unrelated process.

    Windows: a Job Object, since there are no process groups to kill (see
    ``_win32_assign_job``). If neither mechanism is available the tree is NOT contained,
    and ``run_captured`` fails the run rather than falling back to killing the direct
    child. That fallback was the pre-existing behaviour and it is fail-open: it reaches
    exactly the one process that is least likely to be the problem, while reporting
    success.
    """

    def __init__(self, proc: subprocess.Popen):
        self._proc = proc
        # start_new_session was passed iff _HAS_KILLPG, so the pgid is the child's pid
        # exactly then. Never re-derived later; see the class docstring.
        self._pgid: Optional[int] = proc.pid if _HAS_KILLPG else None
        self._job: Optional[int] = (_win32_assign_job(proc)
                                    if sys.platform == "win32" else None)

    @property
    def contained(self) -> bool:
        """Whether a kill here reaches EVERY process this launch created — including one
        the child spawned before the harness got a chance to act.

        WINDOWS IS NEVER CONTAINED, and that is not a stub: it is what the mechanism
        actually provides. The job is assigned after ``CreateProcess`` has already
        returned, so a grandchild spawned inside that window is not a job member and
        ``TerminateJobObject`` does not reach it. Windows associates *future* children of a
        member, never past ones. So the job contains the tree in every case except the one
        that needs no window at all to happen: an MCP server started as the first thing the
        child does. Reporting that as containment would be the same fail-open this class
        exists to remove, one platform over.

        Closing it needs job-at-creation (``PROC_THREAD_ATTRIBUTE_JOB_LIST``, unreachable
        through stdlib ``Popen`` — see ``_win32_assign_job``) or a job-resident launcher
        shim, plus a Windows host to verify on. Until then the honest answer is that the
        harness cannot make its process-tree guarantee on Windows, so it refuses to claim
        it: ``run_captured`` fails the run. The job is still created and still terminated,
        because it is the best sweep available for the child that did start."""
        if sys.platform == "win32":  # pragma: no cover — win32 only
            return False
        return self._pgid is not None

    @property
    def why_uncontained(self) -> str:
        if sys.platform == "win32":  # pragma: no cover — win32 only
            return ("Windows containment is not sound: the Job Object can only be assigned "
                    "after CreateProcess returns, so a grandchild spawned in that window "
                    "is never a job member and survives TerminateJobObject")
        return "this platform has neither process groups nor Job Objects"

    def kill(self) -> None:
        """SIGKILL the whole process group (POSIX) or terminate the whole job (Windows).

        Falls back to the direct child only when there is nothing better — an uncontained
        launch, which ``run_captured`` is separately failing the run over. Killing one
        process is not containment; it is the last thing worth trying on the way out.

        Never the HARNESS's own group. With ``start_new_session`` that cannot happen —
        the stored pgid is the child's own pid — but the failure mode is severe enough to
        guard rather than assume: a group SIGKILL that reached this process would take
        down the whole run (and, under the self-test, the shell that launched it —
        observed while mutation-testing exactly that change)."""
        if self._job is not None:  # pragma: no cover — win32 only
            try:
                k32 = _win32_kernel32()
                k32.TerminateJobObject.restype = wintypes.BOOL
                if k32.TerminateJobObject(wintypes.HANDLE(self._job), 1):
                    return
            except Exception:
                pass    # fall through: a job that will not terminate is not a reason to
                        # skip every other kill available
        if self._kill_group():
            return
        try:
            self._proc.kill()
        except OSError:  # pragma: no cover — already gone
            pass

    def _kill_group(self) -> bool:
        """SIGKILL the stored process group; True if the signal was delivered.

        False covers both "no group to signal" and "the group is already gone" (ESRCH),
        which are indistinguishable here and want the same handling from every caller."""
        if self._pgid is None:
            return False
        try:
            if self._pgid == os.getpgid(0):  # pragma: no cover — guard, see kill()
                return False
            os.killpg(self._pgid, signal.SIGKILL)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False

    def close(self) -> None:
        """Sweep the tree, then release the job handle. Runs on EVERY return.

        A normal exit is evidence that the AGENT finished, never that its descendants
        did. The shape that motivates the timeout kill does not need a timeout to happen:
        a CLI that starts an MCP server, redirects its stdio away from the captured
        pipes, and exits 0 returns here in milliseconds with the server still running —
        measured at 0.03s, with the grandchild outliving the run. The pipes stay open in
        the timeout case and hide it; close them and the leak is silent and fast.

        Sweeping a reaped leader's group is safe for the same reason the stored pgid is:
        a pid in use as a process-group id is not recycled while the group has members,
        so this either reaches that group or reaches nothing.

        On Windows the sweep is an EXPLICIT ``TerminateJobObject``, not the kill-on-close
        limit. Kill-on-close is a backstop for the harness dying unexpectedly; leaning on
        it here would make the sweep depend on a ``CloseHandle`` that is not guaranteed to
        succeed, and a failed close would then leave the whole tree running while this
        method returned normally. Terminating first means a failed close leaks a handle
        rather than a process tree — and the close is checked anyway, because the pair
        "terminated, then closed" is the only outcome that actually swept.

        Raises if the Windows sweep failed, which propagates out of ``run_captured``'s
        ``finally`` and fails the run. Replacing a return value that way is deliberate: a
        run whose descendants may still be alive has nothing worth reporting. On POSIX
        there is nothing to raise about — a group that is already gone is the normal case
        and is indistinguishable from one that never existed (both are ``ESRCH``)."""
        if self._pgid is not None:
            self._kill_group()
        job, self._job = self._job, None
        if job is not None and not _win32_sweep_job(job):  # pragma: no cover — win32 only
            raise RuntimeError(
                "the Windows Job Object holding this run's process tree could not be "
                "terminated and released, so descendants of the agent may still be "
                "running; failing the run rather than reporting a result that a leaked "
                "MCP server could have influenced")


def _tail(text: str | None, limit: int = 400) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    return text[-limit:].replace("\n", " ⏎ ")
