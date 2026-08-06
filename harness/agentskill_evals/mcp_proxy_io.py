"""C3 — the harness-owned filtering proxy: the I/O HALF, and the program.

`DESIGN_MCP_Support.md` §10.3 and §10.5 are the specification. Everything that DECIDES lives in
`mcp_proxy.py` and everything that JUDGES lives in `mcp_audit.py`; what is left here is reads,
writes, spawning and shutdown sequencing — the things a wire-level driver can observe and a
mutation cannot reach. `tools/verify_mcp_proxy.py` is that driver, and it is not optional
coverage: nothing in this file is proven by the selftest.

    agent CLI  --stdio-->  this program  --stdio-->  the declared MCP server

THE ONE RULE (§10.5): the proxy never degrades. Anything it cannot handle with certainty
becomes a failed cell, never unfiltered traffic. So there is no "warn and continue" path
anywhere below: every `Fail` from `decide()` latches a `protocol_anomaly` trigger and starts
the teardown, and every teardown fault is recorded rather than swallowed.

THE SHUTDOWN IS THE HARD PART, and the naive form of it is worse than not having one. Unlike
the probe shim this proxy has a CHILD — the real MCP server, holding the interpolated
credentials — so a handler that logs a terminator and calls `os._exit(0)` neither terminates
nor reaps it. The server outlives the run, still holding a credential, while the audit log
certifies that the instance ended cleanly: a false clean verdict produced by the very mechanism
meant to prevent one. §10.5's six steps are therefore in a fixed order, and each of them
records whether it kept its promise, because a step that ran no code raises nothing to notice.

THE PROXY IS NOT THE SERVER'S PARENT — THE GUARDIAN IS, and that inversion is what makes the
containment claim structural rather than careful. The guardian is established BEFORE the
credential-bearing child exists, it is the thing that starts it, and a guardian that cannot be
established is `spawn_failed` with no server ever running. Because it is the parent it can keep
the child unreaped, which is the only way any process here can show that a process-group id
still names the group it created rather than one the kernel has since handed to a stranger. So
every real signal to that group is sent by the guardian, on the proxy's order, and the proxy's
own fallback exists for exactly one ending — the guardian dying mid-run — and says out loud
that it is the weaker claim (review, PR #103).

WHAT IS DELIBERATELY NOT WRITTEN TO THE AUDIT LOG: argv, the environment, the config, and the
detail text of any anomaly. The config carries interpolated credentials (§10.3) and an anomaly
detail can quote a wire message the server chose the contents of. The log carries tool names,
enumerated reasons and pids; diagnosis goes to stderr, which is the CLI's problem and not an
archived artifact. The one thing this program must never do is put a secret somewhere the
scrubber has to catch it.
"""
from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field

if __package__:
    from . import mcp_audit as audit
    from . import mcp_proxy as proxy
else:
    # LAUNCHED BY ABSOLUTE PATH, which is how the CLI's MCP config spawns it (§10.3). The
    # interpreter is `sys.executable` — the harness's own, and an absolute path outside HOME so
    # a contained or masked home does not affect it — but that only settles which Python runs,
    # not whether `agentskill_evals` is importable, and running a file by path puts the file's
    # own directory on `sys.path` rather than the package's parent. Bootstrapping from
    # `__file__` is deliberate: the alternative is a `PYTHONPATH` that has to survive four
    # different CLIs' handling of an MCP server's `env` map, and a proxy that fails to import
    # is indistinguishable from a server that would not start.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from agentskill_evals import mcp_audit as audit  # noqa: E402 — see above
    from agentskill_evals import mcp_proxy as proxy  # noqa: E402

# Read ONCE at startup, from env vars the driver sets (§10.9). Nothing below re-reads them: a
# fault point that could be armed mid-run would be a fault point whose configuration the start
# record does not describe.
FAULT_ENV = "ASE_MCP_FAULT_SUPPRESS"     # PRESENCE arms it; the value names the facts
INHERIT_ENV = "ASE_MCP_INHERIT_FDS"      # fds to hand the child and then close (§10.9)
GRACE_ENV = "ASE_MCP_GRACE"              # seconds; scales every bound below
GUARDIAN_ENV = "ASE_MCP_GUARDIAN"        # how to break the guardian, for §10.9's cases

DEFAULT_GRACE = 5.0

GUARDIAN_FLAG = "--guardian"
# The four ways §10.9 breaks the guardian live in `mcp_audit`, with the rest of the record's
# vocabulary, because the start record has to carry which one was armed (§10.5.1).
GUARDIAN_MODES = audit.GUARDIAN_MODES
# How long the guard loop waits before re-asking whether the proxy is still its parent. There is
# deliberately NO ceiling on the loop itself. An earlier version gave up after an hour and
# exited, which recreates the orphan it exists to prevent — a long-lived session outlives the
# ceiling and the credential-bearing child is then unguarded (review, PR #103). The wait ends on
# facts instead: an order, the lifeline's EOF, or a reparented `getppid()`.
GUARDIAN_POLL = 0.2

# The orders, one byte each, written down the lifeline. Every order is answered by exactly one
# report line, so the proxy's account of what happened to the group is the guardian's, not its
# own guess. EOF is not an order: it is the proxy having died.
ORDER_TERM = b"T"                        # deliver SIGTERM to the child's group
ORDER_KILL = b"K"                        # deliver SIGKILL to the child's group
ORDER_RELEASE = b"R"                     # reap the child, release the pin, confirm the group
ORDER_STAND_DOWN = b"."                  # leave the group alone and exit — §10.9's control only

# What the guardian says about the group after the reap. `gone` is the only one that settles
# `group_terminated` as `done`.
GROUP_GONE = "gone"
GROUP_PRESENT = "present"

_READ_CHUNK = 65536

# The client's own descriptors, by number rather than through `sys.stdin`/`sys.stdout`. Those
# are buffered wrappers this program must not use — a line left in a userspace buffer is a line
# the peer never saw — and `sys.stdin` is not even guaranteed to exist: CPython sets it to None
# for a descriptor it cannot wrap for reading, so `sys.stdin.fileno()` turns an unusual stdin
# into an `AttributeError` before the start record is written.
CLIENT_IN = 0
CLIENT_OUT = 1


class _DrainFailed(Exception):
    """The drain hit a read error and already recorded it. Unwind without re-diagnosing.

    Without this the `shutdown_anomaly` catch-all would fire on top of the `shutdown_read_failed`
    the drain just recorded, and the fact would carry two causes — which §10.5.1 rejects as two
    incompatible accounts of one step, correctly.
    """


class ConfigError(Exception):
    """The config could not be read or does not describe a server. OUTSIDE the boundary.

    §10.5 puts the instance boundary before the spawn attempt so that `spawn_failed` is an
    ordinary ending with a record like any other. What stays outside it is narrower: a config
    that cannot be read, and an audit log that cannot be opened — and that gap is closed from
    the other side, since for a gated server a log that does not exist fails the cell.
    """


@dataclass(frozen=True)
class Config:
    """What the proxy was told to front, read from a file whose path is its sole argument."""

    server: str
    command: str
    args: tuple[str, ...]
    env: dict[str, str]
    cwd: str | None
    allowed: frozenset[str]
    audit_path: str


def _require(raw: dict, key: str, kind: type, what: str):
    if key not in raw:
        raise ConfigError(f"config has no {key!r}: {what}")
    value = raw[key]
    if not isinstance(value, kind) or (kind is str and not value):
        raise ConfigError(f"config {key!r} is {value!r}, not {what}")
    return value


def load_config(path: str) -> Config:
    """The whole configuration, or a refusal naming what is wrong with it.

    `tools` is REQUIRED and may not be empty. There is no "no allowlist" mode anywhere in C3: a
    server that declares no `tools:` is not proxied at all (§10.1), so there is no configuration
    in which this program is asked to pass everything — and none in which a missing key could
    quietly become one.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except OSError as exc:
        raise ConfigError(f"cannot read config {path!r}: {exc}") from exc
    except ValueError as exc:
        raise ConfigError(f"config {path!r} is not JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"config {path!r} is {type(raw).__name__}, not an object")

    args = _require(raw, "args", list, "a list of arguments")
    if not all(isinstance(a, str) for a in args):
        raise ConfigError(f"config 'args' is {args!r}, not a list of strings")
    env = raw.get("env") or {}
    if not isinstance(env, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        raise ConfigError(f"config 'env' is {env!r}, not a map of strings")
    tools = _require(raw, "tools", list, "the declared `tools:` allowlist")
    if not tools or not all(isinstance(t, str) and t for t in tools):
        raise ConfigError(f"config 'tools' is {tools!r}, not a non-empty list of tool names")
    cwd = raw.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        raise ConfigError(f"config 'cwd' is {cwd!r}, not a path")

    return Config(server=_require(raw, "server", str, "the gated server's name"),
                  command=_require(raw, "command", str, "the declared server's command"),
                  args=tuple(args), env=env, cwd=cwd, allowed=frozenset(tools),
                  audit_path=_require(raw, "audit_log", str, "the audit log's path"))


class AuditSink:
    """Append-only, and FLUSHED ON EVERY WRITE.

    Not a performance detail. A `SIGKILL` arriving between two records must not erase the
    evidence of what came before it — the absence rule reads a start record with no terminator
    as an anomaly, and that only means anything if the start record actually reached the file.
    Buffering would make a killed proxy indistinguishable from one that never ran, which is the
    one distinction §10.5 needs the log to carry.

    Append mode because the client may RESTART the proxy: the file spans instances, every
    record carries an instance id, and the verdict is per instance (§10.5).
    """

    def __init__(self, path: str, instance_id: str) -> None:
        try:
            self._handle = open(path, "a", buffering=1, encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"cannot open audit log {path!r}: {exc}") from exc
        self.instance_id = instance_id

    def write(self, kind: str, **fields) -> None:
        record = {"instance": self.instance_id, "kind": kind, "ts": time.time(), **fields}
        self._handle.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")
        self._handle.flush()

    def close(self) -> None:
        try:
            self._handle.close()
        except OSError:
            pass


SUPPRESS = "suppress"                    # skip the step; the fact reads `fault_point_fired`
FAIL = "fail"                            # skip it and record the step's OWN typed outcome
ABORT = "abort"                          # vanish before step 6, so no terminator is written
FAULT_MODES = (SUPPRESS, FAIL, ABORT)


@dataclass(frozen=True)
class Fault:
    """The fault point of §10.9, read once at startup and recorded in the start record.

    ARMED IS NOT FIRED, and the two are separate on purpose. Arming is `fault_point_configured`
    and is ALWAYS anomalous on its own — that is the clause which stops a hook that was armed
    and silently never fired from producing a passing run. Firing is evidence: it says which
    step was actually suppressed, and it is the only thing a suppressed step's `failed` may name
    as its cause.

    THREE MODES, because "a reason nobody can produce on demand is a reason nobody has tested"
    (§10.9) and three cleanup outcomes cannot be produced any other way. A read error on a pipe
    this process created, a child that survives a group `SIGKILL`, and a `killpg` that fails for
    a reason other than the group being gone are not arrangeable from outside the proxy at all.

      `fact`        SUPPRESS — the step does not run and the record says exactly that:
                    `fault_point_fired` for the fact, and `failed(fault_point_fired)` on it.
      `fact=fail`   FAIL — the step does not run and the record carries the step's OWN typed
                    outcome, with NO firing. That is not a cosmetic difference: §10.5.1 requires
                    exactly one cause, and a record holding both would say the step was
                    attempted and failed AND that it never ran.
      `fact=abort`  ABORT — the process exits at that point, writing no terminator. §10.9's
                    "teardown that died before reaching step 6", made deterministic instead of
                    raced against a signal.

    None of the three can make a run pass: the instance is anomalous from the ARMING, before
    any of them does anything.
    """

    armed: bool = False
    modes: dict[str, str] = field(default_factory=dict)
    guardian: str = ""                   # §10.9's guardian injection, or "" for none

    @property
    def targets(self) -> frozenset[str]:
        return frozenset(self.modes)

    def record(self) -> dict:
        """What the START record carries. Both injections, in one place, because both are it.

        The guardian knob used to be an env var nothing wrote down, which made an injected
        guardian failure indistinguishable in the audit from a real one (review, PR #103) — a
        fault point with no provenance, which is the one thing `fault_point_configured` exists
        to prevent. It rides in the same map, so no consumer needs a second clause and none can
        omit one.
        """
        found = {"suppresses": sorted(self.targets)}
        if self.guardian:
            found["guardian"] = self.guardian
        return found

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> Fault:
        env = os.environ if environ is None else environ
        guardian = env.get(GUARDIAN_ENV, "")
        if guardian and guardian not in GUARDIAN_MODES:
            raise ConfigError(f"{GUARDIAN_ENV}={guardian!r} names no guardian injection from "
                              f"{sorted(GUARDIAN_MODES)}")
        if FAULT_ENV not in env:
            # ARMED BY EITHER, because either makes the run a test rather than a run. A guardian
            # injection with no suppression list is the arm-only case one axis over: it
            # suppresses nothing, records itself, and cannot produce a passing instance.
            return cls(armed=bool(guardian), guardian=guardian)
        # PRESENT and empty is arm-only mode: configured, recorded, wired to suppress nothing,
        # which is its entire purpose. Reading an empty value as "no fault point" would pass
        # exactly the case §10.9 arms it for.
        modes: dict[str, str] = {}
        for part in env[FAULT_ENV].split(","):
            if not part:
                continue
            fact, _, mode = part.partition("=")
            mode = mode or SUPPRESS
            if fact not in audit.FACTS or mode not in FAULT_MODES:
                raise ConfigError(f"{FAULT_ENV} entry {part!r} names no completion fact "
                                  f"and mode from {FAULT_MODES}")
            if mode == FAIL and audit.typed_outcome(fact) is None:
                # Two facts have no typed outcome, so there is nothing for this mode to record
                # — and inventing `shutdown_anomaly` here would fabricate an exception that
                # never happened, in a record whose whole purpose is to be believed.
                raise ConfigError(f"{FAULT_ENV}: {fact!r} has no typed cleanup outcome, so "
                                  f"{FAIL!r} has nothing to record; use {SUPPRESS!r}")
            modes[fact] = mode
        return cls(armed=True, modes=modes, guardian=guardian)

    def mode_for(self, fact: str) -> str | None:
        return self.modes.get(fact) if self.armed else None


def _grace(environ: dict[str, str] | None = None) -> float:
    env = os.environ if environ is None else environ
    try:
        value = float(env.get(GRACE_ENV, DEFAULT_GRACE))
    except ValueError:
        return DEFAULT_GRACE
    return value if value > 0 else DEFAULT_GRACE


def _inherit_fds(environ: dict[str, str] | None = None) -> tuple[int, ...]:
    """Descriptors to hand the child and then close (§10.9's liveness channels).

    Test-only plumbing, and it is in the program rather than in the driver because inheritance
    is the thing being tested: the driver cannot pass a descriptor to a process it did not
    spawn. The proxy closing its own copies is not bookkeeping either — with the handoff done
    each channel has a SOLE WRITER, and that is what makes an observation about a channel an
    observation about a process.
    """
    env = os.environ if environ is None else environ
    found = []
    for part in env.get(INHERIT_ENV, "").split(","):
        if part.strip().isdigit():
            found.append(int(part))
    return tuple(found)


def _write_all(fd: int, payload: bytes) -> None:
    """Every byte, or an `OSError`. A partial write on a pipe is a truncated JSON-RPC line."""
    while payload:
        payload = payload[os.write(fd, payload):]


def _note(message: str) -> None:
    """A diagnostic that cannot fail the thing it describes.

    STDERR BELONGS TO THE CLI, AND THE CLI MAY HAVE CLOSED IT. `print` then raises
    `BrokenPipeError`, and in the guardian that exception aborted the sweep — because the
    message came BEFORE the signal, a dead diagnostic channel left a credential-bearing process
    group alive (review, PR #103). A diagnostic is never load-bearing, so every stderr write in
    this file goes through here, and the guardian now signals first and says so afterwards.

    `ValueError` as well as `OSError`, because a closed file object raises that instead.
    """
    try:
        print(message, file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


def _close(fd: int | None) -> None:
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass


def _readable(fd: int, timeout: float) -> bool:
    sel = selectors.DefaultSelector()
    try:
        sel.register(fd, selectors.EVENT_READ)
        return bool(sel.select(timeout))
    finally:
        sel.close()


def _is_own_group(pgid: int) -> bool:
    """Signalling this would reach the caller, whatever spawned it, and every sibling.

    Which means `start_new_session=True` did not take effect. A cleanup must not reach further
    than what it created (§4), so both signalling sites — the guardian and the proxy's fallback
    — ask this before they deliver anything, and both refuse rather than guess.
    """
    return pgid == os.getpgid(0)


# THERE IS NO FALLBACK SIGNALLER, and the missing function is the point. An earlier version had
# one: with the guardian dead, the proxy asked `getpgid(child_pid)` and signalled if the answer
# was still the recorded pgid. That check is not an identity — a reaped pid can be reused, and
# because `start_new_session=True` makes the child its own group leader, `getpgid(pid) == pid`
# degenerates to "some group leader has this number". Its own docstring said so, and it was used
# to authorize `SIGKILL` anyway: the naked-pgid hazard narrowed to one path rather than removed
# (review, PR #103). A loud anomaly does not authorize signalling an uncertain identity. So the
# `guardian_lost` ending records that the group could not be accounted for and signals NOTHING;
# §10.6 records the resulting leak as a limit of the design rather than hiding it in a branch.
# What would lift it is another non-reusable handle on the process — `pidfd_open` on Linux,
# `EVFILT_PROC` on the BSDs — and neither is portable to both platforms this harness supports.


def probe_group_empty(pgid: int, grace: float) -> bool:
    """Whether the group is EMPTY, asked with a signal that delivers nothing.

    `killpg(pgid, 0)` answering `ESRCH` is direct evidence, where an errno from a real delivery
    is an inference: `EPERM` means the members present could not be signalled, which a sandbox
    restriction and a differently credentialed descendant produce as readily as the all-zombie
    group that was measured (review, PR #103). Anything but `ESRCH` therefore means something is
    still there, and the bounded retry is for the one benign case — a helper that has died and
    is briefly a zombie of its own until init reaps it.

    Because it sends NOTHING, this is the one group operation that needs no pin: the worst a
    recycled pgid can produce here is a false `shutdown_group_kill_failed`, which is a failed
    cell rather than a signal to a stranger.
    """
    deadline = time.monotonic() + grace
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            pass                         # present but unsignallable; still present
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


class Guardian:
    """The child's PARENT: established before the child exists, and the holder of its pin.

    WHY IT IS THE PARENT AND NOT A BYSTANDER. Three separate properties fall out of that one
    decision, and none of them is reachable by a process that merely knows a number:

      * IT CANNOT BE LATE. The credential-bearing server is started BY the guardian, so there is
        no window in which the child exists and nothing is watching it. A guardian spawned after
        the child leaves exactly that window, and a `SIGKILL` inside it orphans the server no
        matter how careful the code on either side is (review, PR #103).
      * IT CANNOT BE ABSENT. No guardian means no spawn: `_establish_guardian` failing is
        `spawn_failed`, an ordinary anomalous ending with a record like any other, and the
        server never runs. "Best effort" was the wrong shape for a containment mechanism.
      * IT HOLDS THE IDENTITY. The group is named by the child's pid, and the kernel may reuse
        that number once the last member is reaped. Keeping the child unreaped is what makes
        every signal this process sends provably reach the group it created rather than a
        stranger's, and the argument needs nothing about how pids are allocated: a pgid names at
        most one EXISTING group, a group exists while it has a member, and an unreaped child is
        one. It is also why the guardian NEVER signals after it has reaped — releasing the pin
        ends its licence to act, so `release()` is the last thing it does.

    THE HANDSHAKE IS IN TWO PHASES, and the split is what makes `spawn_failed` TRUE rather than
    merely written. Phase one authenticates this process and NOTHING ELSE: it reports its own
    pid, which the proxy checks against the one it spawned, and it does not yet hold a command
    to run. Phase two is the launch order, which the proxy sends only once phase one has been
    accepted. One phase was not enough — the guardian spawned the server and reported in one
    step, so a report the proxy REJECTED still left a child that had run: under the imposter
    injection a `/usr/bin/touch` child created its marker in 6 of 20 runs while every audit said
    `spawn_failed` and recorded no spawn at all (review, PR #103). A partition the record
    asserts has to be a partition the code enforces, and the enforcement is that the guardian
    cannot start what it has not been told.

    IT ONLY EVER STANDS DOWN ON PURPOSE. The default at every ending — an order, an EOF — is to
    terminate the group; the single exception is §10.9's retention control, which is armed by a
    fault point and therefore cannot occur in a run that could have passed. An earlier version
    stood down whenever the proxy's teardown had merely RUN, so a teardown that ran and FAILED
    left the survivors it had just failed to kill (review, PR #103). The record preserves the
    evidence either way; cleanup does not erase it.
    """

    def __init__(self, setup: dict) -> None:
        self.stdin_fd = int(setup["stdin_fd"])
        self.stdout_fd = int(setup["stdout_fd"])
        self.lifeline = int(setup["lifeline_fd"])
        self.report_fd = int(setup["report_fd"])
        self.grace = float(setup["grace"])
        self.knob = setup.get("guardian") or ""
        self.child: subprocess.Popen | None = None
        self.child_pgid: int | None = None

    # -- talking to the proxy -----------------------------------------------------------------

    def report(self, **fields) -> None:
        """One line per order, and never anything else. A failed write means the proxy is gone,
        which the guard loop is about to discover for itself."""
        try:
            _write_all(self.report_fd, (json.dumps(fields) + "\n").encode("utf-8"))
        except OSError:
            pass

    def _phase(self, name: str) -> None:
        """UNDER AN INJECTION ONLY: say which phase this process reached.

        §10.9 needs to tell "the guardian stopped at authentication" from "the guardian was
        handed a command anyway", and the end-to-end witness for the second — whether the child
        got far enough to leave a mark before the sweep reached it — is a race the driver loses
        about nineteen times in twenty, measured. This is the same fact stated by the process it
        is about, on a channel the driver already reads, and it is silent in any run that is not
        already a fault injection.
        """
        if self.knob:
            _note(f"mcp-proxy guardian: phase {name}")

    def authenticate(self) -> bool:
        """Phase one: say who this process is. No child exists, and none can until this passes.

        §10.9's `silent` returns without reporting and §10.9's `imposter` reports a pid that is
        not this process's. Both must end with no server having run at all, which is a property
        of WHERE they are rather than of what the proxy does with them: there is nothing to
        clean up on either path, because there is nothing yet.
        """
        if self.knob == audit.GUARDIAN_SILENT:
            return False
        mine = os.getpid() + 1 if self.knob == audit.GUARDIAN_IMPOSTER else os.getpid()
        self.report(guardian_pid=mine)
        return True

    # -- the child ----------------------------------------------------------------------------

    def spawn(self, launch: dict) -> bool:
        """Phase two: start the declared server, in its own session, and report what was started.

        The command arrives HERE, in the order that follows a successful phase one, and not in
        the setup this process was constructed from. That is the enforcement: a guardian the
        proxy has not accepted has nothing to run.
        """
        inherit = tuple(launch["inherit"])
        try:
            self.child = subprocess.Popen(      # noqa: S603 — the declared server (§10.3)
                [launch["command"], *launch["args"]],
                stdin=self.stdin_fd, stdout=self.stdout_fd,
                env=dict(launch["env"]), cwd=launch["cwd"], close_fds=True, pass_fds=inherit,
                # ITS OWN PROCESS GROUP, which is what gives step 4 something to signal: a stdio
                # server may spawn helpers, they inherit the interpolated environment, and a
                # surviving grandchild is a live credential the run no longer accounts for.
                start_new_session=True)
        except OSError as exc:
            self.report(error=f"cannot start {launch['command']!r}: {exc}")
            return False
        finally:
            # EVERY DESCRIPTOR HANDED ON IS CLOSED HERE. The proxy holds the other end of both
            # stdio pipes and reads the child's stdout to EOF, so a copy left open in this
            # process is an EOF that never arrives — a proxy that hangs in its own drain. The
            # same discipline gives each §10.9 liveness channel a SOLE writer.
            for fd in (self.stdin_fd, self.stdout_fd, *inherit):
                _close(fd)
        try:
            self.child_pgid = os.getpgid(self.child.pid)
        except OSError:
            self.child_pgid = self.child.pid
        self.report(child_pid=self.child.pid, child_pgid=self.child_pgid)
        return True

    # -- the group ----------------------------------------------------------------------------

    def _signal(self, sig: int) -> str | None:
        """Deliver one signal to the child's group. An error string, or None.

        `ESRCH` and `EPERM` are both None, and neither of them is a verdict: the group being
        gone is the goal, and an `EPERM` says only that what is there could not be signalled by
        this process. What the group actually contains is settled by `probe_group_empty`, which
        sends nothing and can therefore be believed.
        """
        if _is_own_group(self.child_pgid):
            return f"group {self.child_pgid} is this process's own, not the child's"
        try:
            os.killpg(self.child_pgid, sig)
        except (ProcessLookupError, PermissionError):
            return None
        except OSError as exc:
            return str(exc)
        return None

    def deliver(self, sig: int) -> None:
        error = self._signal(sig)
        self.report(signalled=error is None, error=error)

    def release(self) -> bool:
        """Reap the child, release the pin, and say what became of the group. True iff reaped.

        THE SWEEP COMES FIRST, unconditionally. Reaping is what ends this process's licence to
        signal, so anything still in the group has to be dealt with while the pin is still held
        — including the case where the proxy's own step 4 never delivered, which is exactly the
        failure the guardian exists to survive. On a shutdown that already worked it is a
        `killpg` against a group holding nothing but our own zombie, which costs one syscall.

        A REAP THAT TIMES OUT KEEPS THE PIN, so this returns False and the guard loop carries on
        watching. There is no state in which this process has released the pin and is still
        pretending to guard something.
        """
        self._signal(signal.SIGKILL)
        try:
            self.child.wait(timeout=self.grace)
        except subprocess.TimeoutExpired:
            self.report(reaped=False, error="the child outlived a group SIGKILL")
            return False
        self.report(reaped=True, status=self.child.returncode,
                    group=(GROUP_GONE if probe_group_empty(self.child_pgid, self.grace)
                           else GROUP_PRESENT))
        return True

    def sweep(self) -> None:
        """The proxy died without releasing the pin. Terminate the group and reap.

        THE SIGNAL COMES FIRST AND THE DIAGNOSTIC AFTER IT. This is the one place in the program
        where the order of those two was load-bearing and got it wrong: stderr is inherited from
        the proxy and therefore from the CLI, so once the CLI has closed that pipe a `print`
        here raised `BrokenPipeError` and the guardian exited without signalling anything — a
        credential-bearing group left alive by its own log line (review, PR #103). `_note` makes
        every diagnostic in this file non-fatal; putting the sweep ahead of it makes the
        ordering say so too, so a reader does not have to trust the helper to see why it is safe.
        """
        self._signal(signal.SIGTERM)
        time.sleep(min(self.grace, 0.2))
        self._signal(signal.SIGKILL)
        _note(f"mcp-proxy guardian: the proxy is gone and its teardown did not finish; "
              f"terminated group {self.child_pgid}")
        try:
            self.child.wait(timeout=self.grace)
        except subprocess.TimeoutExpired:
            _note(f"mcp-proxy guardian: child {self.child.pid} outlived a group SIGKILL")

    # -- the loop -----------------------------------------------------------------------------

    def obey(self, order: bytes) -> bool:
        """Carry out one order. True iff the pin is now released and there is nothing to guard."""
        if order == ORDER_TERM:
            self.deliver(signal.SIGTERM)
        elif order == ORDER_KILL:
            self.deliver(signal.SIGKILL)
        elif order == ORDER_RELEASE:
            return self.release()
        else:
            self.report(signalled=False, error=f"unknown order {order!r}")
        return False

    def guard(self) -> None:
        """Wait for orders until the pin is released, the proxy stands us down, or it dies.

        THE LIFELINE'S EOF IS THE WHOLE DEATH DETECTOR, and it is the kernel's own statement
        rather than a poll: the proxy holds the only write end — it is not in the guardian's
        `pass_fds`, so `close_fds` closes it here, and the child is spawned from this process
        with an explicit `pass_fds` that does not carry it either — so an EOF means no writer is
        left anywhere. A second channel was tried, `getppid()` no longer being the proxy, and
        removing it changed the outcome of nothing: its mutation reported MISSED because every
        ending that reparents this process also closes that descriptor. A redundant check whose
        failure is unobservable is not defence in depth, it is a line nothing proves — so what
        guards the premise instead is the sole-writer discipline above, which is a property of
        two `pass_fds` lists that a reader can check.

        AND THERE IS NO CEILING. An earlier version gave up after an hour and exited, which
        recreates the orphan for any session that runs longer than one (review, PR #103). The
        wait ends on facts: an order, or the EOF.
        """
        while True:
            if not _readable(self.lifeline, GUARDIAN_POLL):
                continue
            try:
                order = os.read(self.lifeline, 1)
            except OSError:
                order = b""
            if not order:
                break                    # EOF: the proxy is gone and never released the pin
            if order == ORDER_STAND_DOWN:
                return                   # §10.9's retention control, and nothing else
            if self.obey(order):
                return                   # released: this process may no longer signal anything
        self.sweep()

    def run(self, orders: _Orders) -> int:
        """The two phases, in order, and the guard loop that follows them."""
        if not self.authenticate():
            return 3                     # §10.9's `silent`: no report, and nothing was started
        self._phase("authenticated")
        launch = orders.line()
        if launch is None:
            # The proxy did not accept phase one — or died during it. Either way this process
            # has no command, so there is nothing to start and nothing to clean up.
            return 3
        self._phase("launched")
        if not self.spawn(launch):
            return 3
        if self.knob == audit.GUARDIAN_LATE:
            # §10.9: a guardian that dies once the child exists. The proxy sees the report pipe
            # reach EOF and latches `guardian_lost`, which is terminal precisely because nothing
            # is left that can prove the group's identity.
            return 4
        self.guard()
        return 0


class _Orders:
    """The setup line and then the launch line, read from one inherited descriptor.

    TWO LINES ON ONE PIPE rather than two pipes, because the second must be impossible to read
    before the first: the proxy writes it only after phase one is accepted, so a guardian that
    was rejected sees EOF here and exits with no command. Framed, since neither end may assume
    the other's write is a single read.

    THE ORDERS COME DOWN A PIPE, not on the command line and not through the environment,
    because the launch line carries the interpolated `env` (§10.3). `argv` is world-readable
    through `ps` and is recorded in `result.json`, which is the reason mutation M10 exists.
    """

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self.buffer = b""
        self.done = False

    def line(self) -> dict | None:
        while b"\n" not in self.buffer:
            if self.done:
                return None
            try:
                chunk = os.read(self.fd, _READ_CHUNK)
            except OSError:
                return None
            if not chunk:
                self.done = True
                return None
            self.buffer += chunk
        raw, self.buffer = self.buffer.split(b"\n", 1)
        try:
            found = json.loads(raw)
        except ValueError:
            return None
        return found if isinstance(found, dict) else None

    def close(self) -> None:
        _close(self.fd)


def run_guardian(order_fd: int) -> int:
    """Read the setup line off the inherited descriptor, then be the guardian."""
    orders = _Orders(order_fd)
    try:
        setup = orders.line()
        if setup is None:
            return 2
        return Guardian(setup).run(orders)
    except (KeyError, TypeError, ValueError) as exc:
        _note(f"mcp-proxy guardian: malformed order: {exc!r}")
        return 2
    finally:
        orders.close()


class Instance:
    """One proxy instance: start record, spawn, two pumps, §10.5's teardown, terminator."""

    def __init__(self, cfg: Config, sink: AuditSink, fault: Fault, *,
                 grace: float = DEFAULT_GRACE) -> None:
        self.cfg = cfg
        self.sink = sink
        self.fault = fault
        self.grace = grace
        self.triggers: list[dict] = []
        self.outcomes: list[dict] = []
        self.facts: dict[str, dict] = {}
        self.fired: list[str] = []
        self.state = proxy.ProtocolState()
        self.inflight = proxy.InFlight()
        # The child is the GUARDIAN's process, not this one's: what the proxy holds of it is two
        # descriptors and two numbers, and everything else about it is asked for by order.
        self.child_pid: int | None = None
        self.child_pgid: int | None = None
        self.child_in: int | None = None       # write end of the child's stdin
        self.child_out: int | None = None      # read end of the child's stdout
        self.guardian: subprocess.Popen | None = None
        self.guardian_pid: int | None = None   # as the GUARDIAN reported it, then cross-checked
        self._lifeline: int | None = None      # orders out; its EOF is what fires the guardian
        self._report: int | None = None        # reports in; its EOF is the guardian's death
        self._report_buf = b""
        self._release_report: dict | None = None
        self.child_status: int | None = None
        self._buffers = {proxy.C2S: b"", proxy.S2C: b""}
        self._client_eof = False
        self._child_eof = False

    # -- the record -------------------------------------------------------------------------

    def _trigger(self, reason: str, **payload) -> None:
        """Append. The FIRST is the latch and the rest are runners-up, classified identically.

        `client_eof` then `signal_term` is a CLI closing stdin and then signalling, and two
        individually clean triggers must not compose into a failure — which is the whole reason
        this is a list rather than a slot (§10.5.1).
        """
        self.triggers.append({"reason": reason, **payload})

    def _outcome(self, kind: str, **payload) -> None:
        self.outcomes.append({"kind": kind, **payload})

    def _injected(self, fact: str) -> bool:
        """Whether the fault point stops this step from running, recording what it did.

        The step is SKIPPED, not relabelled. §10.9's control leaves the child and its group
        alive on purpose, which is only a demonstration if nothing killed them — and the record
        then says so, because the fact reads `failed(fault_point_fired)` and the pairing rule
        requires the matching fired record in both directions.
        """
        mode = self.fault.mode_for(fact)
        if mode is None:
            return False
        if mode == ABORT:
            # No terminator, on purpose. The absence rule is what has to catch this, and it is
            # the one ending that cannot be made to describe itself.
            _note(f"mcp-proxy: fault point aborting before {fact}")
            sys.stderr.flush()
            os._exit(70)
        if mode == FAIL:
            typed = audit.typed_outcome(fact)
            self._outcome(typed)
            self._failed(fact, typed)
            return True
        self.fired.append(fact)
        self._failed(fact, audit.FAULT_POINT_FIRED)
        return True

    def _done(self, fact: str) -> None:
        self.facts[fact] = {"state": audit.DONE}

    def _not_applicable(self, fact: str) -> None:
        self.facts[fact] = {"state": audit.NOT_APPLICABLE}

    def _failed(self, fact: str, cause: str) -> None:
        self.facts[fact] = {"state": audit.FAILED, "cause": cause}

    # -- the run ----------------------------------------------------------------------------

    def run(self) -> int:
        """Start record, spawn, pump, tear down, terminator. Returns the process exit code."""
        started = {"server": self.cfg.server, "pid": os.getpid()}
        if self.fault.armed:
            started["fault_point"] = self.fault.record()
        # BEFORE THE SPAWN, and therefore before a single byte is forwarded. That is what makes
        # the two cases partition cleanly: an instance that logged a start must also log a
        # terminator, and an instance that died before logging anything spawned nothing and
        # forwarded nothing, so its absence from the log is not a gap in the evidence.
        self.sink.write(audit.LINE_START, **started)

        wake_r, wake_w = os.pipe()
        # BOTH ends non-blocking. `set_wakeup_fd` requires it of the writer; the reader needs it
        # because the runners-up sweep after the teardown reads a pipe that is usually empty,
        # and a blocking read there hangs the proxy after every clean shutdown — a terminator
        # that is never written, which the absence rule then reports as an anomaly for a run
        # that did everything right.
        os.set_blocking(wake_w, False)
        os.set_blocking(wake_r, False)
        previous = signal.set_wakeup_fd(wake_w)
        # The handler does NOTHING. Re-entrant buffered JSON and file I/O from a signal handler
        # can interleave with a write already in progress and corrupt the very record the
        # verdict depends on; what is needed from the handler is only that the default
        # disposition — terminate without running any handler — does not apply, because a proxy
        # that omitted these would write no terminator on half the shipped fleet (C3-1).
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda _sig, _frame: None)
        try:
            if self._spawn():
                try:
                    self._pump(wake_r)
                except BaseException as exc:                  # noqa: BLE001 — see below
                    # An exception escaping the pumps must not skip the teardown: that would
                    # orphan the child, which is the failure the whole sequence exists to
                    # prevent. `BaseException` is deliberate — a `KeyboardInterrupt` landing
                    # here still leaves a live credential-bearing child if the teardown is
                    # skipped.
                    _note(f"mcp-proxy: pump failed: {exc!r}")
                    self._trigger(audit.READ_FAILED)
            self._teardown()
            # RUNNERS-UP, collected after the teardown rather than during the pump. `client_eof`
            # followed by `signal_term` is a CLI closing stdin and then signalling — ordinary
            # behaviour, two clean triggers, and a signal arriving mid-teardown that went
            # unrecorded would leave the log unable to say that is what happened.
            self._on_signal(wake_r)
        finally:
            signal.set_wakeup_fd(previous)
            for fd in (wake_r, wake_w):
                try:
                    os.close(fd)
                except OSError:
                    pass
        return self._finish()

    def _spawn(self) -> bool:
        """§10.5 step 0 — establish the guardian, which is what starts the server.

        THE ORDER IS THE WHOLE OF IT. The credential-bearing child is the guardian's own child,
        so containment cannot be late, cannot be absent, and cannot be a best effort: a guardian
        that will not start means no server starts, which is `spawn_failed` — an ordinary
        anomalous ending with a record like any other, because the instance boundary sits before
        the spawn attempt (§10.5). Both halves of "the guardian must be ready before the server
        can receive credentials, and inability to establish it must fail closed" are then
        properties of the structure rather than of the care taken at one call site.
        """
        ready = self._establish_guardian()
        if ready is None:
            self._trigger(audit.SPAWN_FAILED)
            return False
        self.guardian_pid = ready["guardian_pid"]
        self.child_pid, self.child_pgid = ready["child_pid"], ready["child_pgid"]
        # Facts the start record cannot hold, since it precedes the spawn. They are what lets
        # §10.9's teardown case check the proxy's account of the group against the group a
        # surviving process reports for itself, rather than discovering processes from outside
        # and deciding for itself which belonged to the run. `guardian_pid` is REQUIRED, and its
        # presence is what the reader takes as the readiness evidence: it was reported by the
        # guardian about itself and cross-checked below, so it cannot be written by a proxy that
        # never got one (review, PR #103).
        self.sink.write(audit.LINE_SPAWN, child_pid=self.child_pid,
                        child_pgid=self.child_pgid, guardian_pid=self.guardian_pid)
        return True

    def _establish_guardian(self) -> dict | None:
        """Authenticate the guardian, THEN give it the launch order, and read what it started.

        TWO PHASES, AND THE ORDER IS THE GUARANTEE. Phase one is a handshake and nothing else:
        `Popen` succeeding says a fork happened, so what has to be established is that our code
        ran in that process — the guardian reports its OWN pid and this checks it against the
        one that was spawned. Only then does the launch order go down the pipe, which is what
        makes `spawn_failed` mean no server ran: a guardian that fails phase one has never been
        told what to run. With one phase it had, and the audit said otherwise (review, PR #103).
        """
        env = dict(os.environ)
        env.update(self.cfg.env)
        # The proxy's own control vars are not the child's business, and one of them names
        # descriptors the child is about to be handed by inheritance rather than by name.
        for key in (FAULT_ENV, INHERIT_ENV, GRACE_ENV, GUARDIAN_ENV):
            env.pop(key, None)
        inherit = _inherit_fds()
        child_in_r, child_in_w = os.pipe()
        child_out_r, child_out_w = os.pipe()
        lifeline_r, lifeline_w = os.pipe()
        report_r, report_w = os.pipe()
        order_r, order_w = os.pipe()
        handed = (child_in_r, child_out_w, lifeline_r, report_w, order_r)
        ours = (child_in_w, child_out_r, lifeline_w, report_r, order_w)
        # §10.9's `missing`: a program that is not there, so this is a real `OSError` out of the
        # real call rather than an exception a test asked the code to raise.
        program = (os.path.join(os.path.dirname(sys.executable), "no-such-interpreter")
                   if self.fault.guardian == audit.GUARDIAN_MISSING else sys.executable)
        try:
            self.guardian = subprocess.Popen(   # noqa: S603 — this module, by absolute path
                [program, os.path.abspath(__file__), GUARDIAN_FLAG, str(order_r)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                close_fds=True, pass_fds=handed + inherit,
                # ITS OWN SESSION, so a group signal aimed at the proxy — which is what half the
                # fleet sends (C3-1) — does not take out the thing whose whole job is to outlive
                # the proxy's death. Its stderr is the proxy's, inherited and passed on to the
                # child, so a server's diagnostics still land where they did before.
                start_new_session=True)
        except OSError as exc:
            _note(f"mcp-proxy: no guardian, so no server: {exc}")
            self.guardian = None
            # OUR ends only. The `finally` below closes the handed ones on every path, and
            # closing a descriptor twice is not free: between the two closes the number can have
            # been handed to something else, and the second close would take that with it.
            for fd in ours:
                _close(fd)
            return None
        finally:
            # Every descriptor the guardian now owns, and the driver's writers with them. Each
            # channel has a SOLE writer one level down, which is what makes an observation about
            # a channel an observation about a process rather than about the inheritance path.
            for fd in handed + inherit:
                _close(fd)
        self.child_in, self.child_out = child_in_w, child_out_r
        self._lifeline, self._report = lifeline_w, report_r

        # PHASE ONE. The setup carries descriptors and bounds — nothing that could start a
        # process — so a guardian that never gets past here has nothing it could have run.
        setup = {"stdin_fd": child_in_r, "stdout_fd": child_out_w, "lifeline_fd": lifeline_r,
                 "report_fd": report_w, "grace": self.grace, "guardian": self.fault.guardian}
        if not self._order(order_w, setup):
            return None
        ready = self._read_report(time.monotonic() + self.grace)
        # `is_json_int` rather than `isinstance(..., int)`, because `True` passes the latter —
        # the reader's own predicate, imported so the writer cannot check something narrower
        # than what the log is checked against.
        if ready is None or not audit.is_json_int(ready.get("guardian_pid")) \
                or ready["guardian_pid"] != self.guardian.pid:
            _note(f"mcp-proxy: the guardian did not authenticate: {ready!r}")
            _close(order_w)              # EOF on the order pipe: it never learns the command
            return None

        # PHASE TWO, and only now does a command exist anywhere below this process.
        launch = {"command": self.cfg.command, "args": list(self.cfg.args), "env": env,
                  "cwd": self.cfg.cwd, "inherit": list(inherit)}
        if not self._order(order_w, launch):
            return None
        _close(order_w)
        started = self._read_report(time.monotonic() + self.grace)
        if started is None or "error" in started:
            _note(f"mcp-proxy: the guardian did not start the server: "
                  f"{(started or {}).get('error', 'no report')}")
            return None
        if not audit.is_json_int(started.get("child_pid")) \
                or not audit.is_json_int(started.get("child_pgid")):
            _note(f"mcp-proxy: unusable spawn report {started!r}")
            return None
        return {"guardian_pid": ready["guardian_pid"], "child_pid": started["child_pid"],
                "child_pgid": started["child_pgid"]}

    @staticmethod
    def _order(fd: int, payload: dict) -> bool:
        """One newline-framed order down the pipe the guardian reads."""
        try:
            _write_all(fd, (json.dumps(payload) + "\n").encode("utf-8"))
        except OSError as exc:
            _note(f"mcp-proxy: cannot brief the guardian: {exc}")
            _close(fd)
            return False
        return True

    def _read_report(self, deadline: float) -> dict | None:
        """One report line, or None — which is the guardian having died or gone quiet.

        A report that never comes is NOT distinguished here from one that arrives malformed:
        both mean the proxy has no account of what happened to the group, and every caller
        records the same typed failure for the step it was asking about.
        """
        while b"\n" not in self._report_buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or self._report is None:
                return None
            if not _readable(self._report, remaining):
                return None
            try:
                chunk = os.read(self._report, _READ_CHUNK)
            except OSError:
                chunk = b""
            if not chunk:
                return None              # EOF: the guardian is gone
            self._report_buf += chunk
        line, self._report_buf = self._report_buf.split(b"\n", 1)
        try:
            found = json.loads(line)
        except ValueError:
            return None
        return found if isinstance(found, dict) else None

    def _ask(self, order: bytes) -> dict | None:
        """One order down the lifeline, one report back. None if the guardian did not answer."""
        if self._lifeline is None or self.guardian is None:
            return None
        try:
            _write_all(self._lifeline, order)
        except OSError:
            return None                  # the guardian is gone; the caller records the failure
        return self._read_report(time.monotonic() + self.grace)

    def _guardian_lost(self) -> bool:
        """Whether the guardian is gone — which is what ends the proxy's licence to be sure."""
        return self.guardian is None or self.guardian.poll() is not None

    def _release_guardian(self) -> None:
        """Close the lifeline, having first told the guardian if it is to do NOTHING.

        THE STAND-DOWN IS THE EXCEPTION AND THE DEFAULT IS CLEANUP. It is sent only when a fault
        point actually fired on one of the two facts whose suppression leaves the group alive on
        purpose (§10.9's retention control) — and firing implies arming, which is anomalous on
        its own, so no run that could have passed ever sends one. Every other ending, including
        a teardown that ran and failed, leaves the guardian to find the EOF and terminate the
        group: the audit record already holds the evidence of the failure, and cleaning up after
        it does not erase that (review, PR #103).
        """
        retained = [f for f in (audit.GROUP_TERMINATED, audit.CHILD_REAPED) if f in self.fired]
        if retained and self._lifeline is not None:
            _note(f"mcp-proxy: standing the guardian down; {', '.join(retained)} suppressed")
            try:
                _write_all(self._lifeline, ORDER_STAND_DOWN)
            except OSError:
                pass                     # already gone, and it acts on nothing when it is
        _close(self._lifeline)
        self._lifeline = None

    # -- the pumps --------------------------------------------------------------------------

    def _pump(self, wake_r: int) -> None:
        """Both directions, the signal channel and the guardian, until a trigger latches.

        THE GUARDIAN IS WATCHED FROM HERE, and that is not symmetry for its own sake. It is the
        child's parent, so a guardian that dies mid-run leaves a live credential-bearing server
        with no process holding its identity — the reap will not be ours to make and the group's
        pin is gone. That is a terminal condition rather than a degraded mode: the report pipe
        reaching EOF latches `guardian_lost`, forwarding stops, and the teardown falls back to
        the weaker identity check `group_identity` describes.
        """
        sel = selectors.DefaultSelector()
        try:
            sel.register(CLIENT_IN, selectors.EVENT_READ, proxy.C2S)
            sel.register(self.child_out, selectors.EVENT_READ, proxy.S2C)
            sel.register(wake_r, selectors.EVENT_READ, "signal")
            sel.register(self._report, selectors.EVENT_READ, "guardian")
            while not self.triggers:
                for key, _events in sel.select():
                    if key.data == "signal":
                        self._on_signal(wake_r)
                    elif key.data == "guardian":
                        self._on_guardian(sel)
                    else:
                        self._on_readable(key.data, sel)
                    if self.triggers:
                        break
        finally:
            sel.close()

    def _on_guardian(self, sel: selectors.BaseSelector) -> None:
        """The guardian says nothing unprompted, so anything here is its death or a bug."""
        try:
            chunk = os.read(self._report, _READ_CHUNK)
        except OSError:
            chunk = b""
        if chunk:
            # Unsolicited, which no order-and-report exchange produces. Keep it for whichever
            # step asks next rather than discarding a line the teardown is about to need.
            self._report_buf += chunk
            return
        sel.unregister(self._report)
        _note("mcp-proxy: the guardian is gone; the child has no pin")
        self._trigger(audit.GUARDIAN_LOST)

    def _on_signal(self, wake_r: int) -> None:
        """`set_wakeup_fd` writes the signal NUMBER, so the trigger says which arrived."""
        try:
            raw = os.read(wake_r, _READ_CHUNK)
        except OSError:
            raw = b""
        for number in raw:
            self._trigger(audit.SIGNAL_INT if number == signal.SIGINT else audit.SIGNAL_TERM)

    def _on_readable(self, direction: str, sel: selectors.BaseSelector) -> None:
        source = CLIENT_IN if direction == proxy.C2S else self.child_out
        try:
            chunk = os.read(source, _READ_CHUNK)
        except OSError as exc:
            # NOT EOF. An instrument failure must never wear the clean-shutdown label: a
            # swallowed `OSError` presenting as a quiet end of stream is the defect §4 has
            # already caught in the wild.
            _note(f"mcp-proxy: read failed on {direction}: {exc}")
            self._trigger(audit.READ_FAILED)
            return
        if not chunk:
            sel.unregister(source)
            # A HALF-WRITTEN LINE AT EOF IS A MESSAGE THAT WAS NEVER FRAMED, and dropping it
            # certifies a stream that ended mid-message as one that ended. It is latched
            # BEFORE the EOF trigger so the latch names the framing fault rather than the
            # clean close that followed it (review, PR #103).
            self._flush_residue(direction)
            if direction == proxy.C2S:
                self._client_eof = True
                self._trigger(audit.CLIENT_EOF)
            else:
                self._child_eof = True
                # The spec tells clients to RESTART a server that exits unexpectedly, so this
                # is the case most easily mistaken for normality — and it is not covered by the
                # spawn-failure or terminator checks.
                self._trigger(audit.CHILD_EXIT)
            return
        self._consume(direction, chunk, shutting_down=False)

    def _flush_residue(self, direction: str) -> None:
        """Whatever is left in the frame buffer when the stream ends. It is never nothing."""
        residue, self._buffers[direction] = self._buffers[direction], b""
        if residue:
            _note(f"mcp-proxy: {len(residue)} unterminated bytes at EOF on {direction}")
            self._trigger(audit.PROTOCOL_ANOMALY, anomaly=proxy.UNPARSEABLE)

    def _consume(self, direction: str, chunk: bytes, *, shutting_down: bool) -> None:
        """Frame the bytes into lines and put each one through `decide()`.

        §10.4's rules still apply to what the DRAIN forwards — the shutdown is not a licence to
        pump bytes unfiltered — so a malformed or off-list frame arriving on the way out is a
        genuine second trigger, and an anomalous one.

        NOTHING IS SKIPPED AND NOTHING IS NORMALIZED. An empty line is not a legal MCP message
        — the stdio binding says each message is a single request, notification or response —
        and bytes that are not UTF-8 are not a message either. Both used to be waved through
        here, one by a `strip()` test and the other by `errors="replace"`, which silently
        rewrote a peer's bytes into different bytes and forwarded them (review, PR #103).
        `decide()` already classifies an empty string as unparseable, so the fix for the first
        is to stop intercepting it: one owner for what a legal line is.
        """
        self._buffers[direction] += chunk
        while b"\n" in self._buffers[direction]:
            line, self._buffers[direction] = self._buffers[direction].split(b"\n", 1)
            try:
                text = line.decode("utf-8")
            except UnicodeDecodeError as exc:
                _note(f"mcp-proxy: undecodable line on {direction}: {exc}")
                self._trigger(audit.PROTOCOL_ANOMALY, anomaly=proxy.UNPARSEABLE)
                return
            # A COUNT, not a truthiness test. During the drain there is ALREADY a trigger — the
            # one that started the teardown — so `if self.triggers` stops nothing, and a
            # malformed frame arriving on the way out was recorded as an anomaly and then
            # followed by more forwarding. What has to stop the loop is a trigger this line
            # added, which is the difference between "something is wrong" and "this went
            # wrong here" (review, PR #103).
            before = len(self.triggers)
            self._act(direction, text, shutting_down=shutting_down)
            if len(self.triggers) > before:
                return

    def _act(self, direction: str, line: str, *, shutting_down: bool) -> None:
        action = proxy.decide(line, direction=direction, allowed=self.cfg.allowed,
                              state=self.state, inflight=self.inflight, server=self.cfg.server)
        if isinstance(action, proxy.Fail):
            # The KIND only. The detail names methods and ids and can quote a message whose
            # contents the server chose, and this file goes into the archived artifacts.
            _note(f"mcp-proxy: anomaly {action.anomaly.kind}: {action.anomaly.detail}")
            self._trigger(audit.PROTOCOL_ANOMALY, anomaly=action.anomaly.kind)
            return
        if isinstance(action, proxy.Drop):
            # THE CODE, never the prose. `action.detail` names the id and direction, and an id
            # is an arbitrary value the peer chose — a string of any length and content — while
            # this file is an archived artifact whose contract is enumerated reasons rather
            # than message detail (review, PR #103).
            _note(f"mcp-proxy: dropped: {action.detail}")
            self.sink.write(audit.LINE_EVENT, event=audit.MESSAGE_DROPPED, reason=action.code)
            return
        if isinstance(action, proxy.Refuse):
            self.sink.write(audit.LINE_EVENT, event=audit.CALL_REFUSED, tool=action.tool)
            self._send(direction, action.msg, back=True, shutting_down=shutting_down)
            return
        if isinstance(action, proxy.Filtered):
            # `.get` rather than `[...]`, even though `tools_result_ok` ran before the filter:
            # a `KeyError` here would escape the pump and be recorded as `read_failed`, which
            # is a misdiagnosis rather than a crash — the worst of the three outcomes, since
            # the log would name a pipe fault for a bug in this function.
            result = action.msg.get("result") if isinstance(action.msg.get("result"), dict) else {}
            kept = [t.get("name") for t in (result.get("tools") or []) if isinstance(t, dict)]
            self.sink.write(audit.LINE_EVENT, event=audit.TOOLS_ADVERTISED,
                            forwarded=[n for n in kept if isinstance(n, str)],
                            removed=list(action.removed))
            self._send(direction, action.msg, back=False, shutting_down=shutting_down)
            return
        tool = self._forwarded_call(direction, action.msg)
        if tool is not None:
            self.sink.write(audit.LINE_EVENT, event=audit.CALL_FORWARDED, tool=tool)
        self._send(direction, action.msg, back=False, shutting_down=shutting_down)

    @staticmethod
    def _forwarded_call(direction: str, msg: dict) -> str | None:
        """§10.7's per-call telemetry: the tool that actually reached the server.

        Read off the message rather than re-decided. `decide()` already refused every off-list
        name and already required `params.name` to be a non-empty string on a `tools/call`, so
        anything arriving here as a forwarded call is on the list by construction — and the
        reader checks that claim against the allowlist independently.
        """
        if direction != proxy.C2S or msg.get("method") != "tools/call":
            return None
        name = (msg.get("params") or {}).get("name")
        return name if isinstance(name, str) else None

    def _send(self, direction: str, msg: dict, *, back: bool, shutting_down: bool) -> None:
        """Write a message on. `back` means to whoever sent it — a refusal, never the peer."""
        to_client = (direction == proxy.S2C) != back
        payload = (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            target = CLIENT_OUT if to_client else self.child_in
            if target is None:
                # Step 2a has already closed it, and the NUMBER is not kept: an fd the kernel
                # has since handed to something else would take this line silently. A write to a
                # descriptor that is gone is a write failure like any other.
                raise BrokenPipeError("the child's stdin is closed")
            _write_all(target, payload)
        except OSError as exc:
            if shutting_down:
                # C3-1 measured this against agy: the client closes stdin and stops reading at
                # once, so the graceful-closure write raises EPIPE against a CONFORMING peer.
                # Record it, swallow it, continue — an exception escaping here would skip every
                # step below and orphan the child.
                self._outcome(audit.SHUTDOWN_WRITE_FAILED)
                return
            _note(f"mcp-proxy: write failed: {exc}")
            self._trigger(audit.CLIENT_WRITE_FAILED if to_client
                          else audit.CHILD_WRITE_FAILED)

    # -- §10.5's six steps ------------------------------------------------------------------

    def _teardown(self) -> None:
        """The order is fixed, and a clean terminator asserts every promise in it was kept.

        WHY THE REAP IS LAST, which is the one part of this order that is not obvious. The
        process group is named by the child's pid, and that pid stops being unique the moment
        the child is reaped: after `wait()` returns, `killpg(pgid, ...)` names a group the
        kernel is free to have given to something else. Keeping the child as an unreaped zombie
        through step 4 is what makes the group provably still ours when it is signalled — the
        zombie is a member, so the group cannot be recycled out from under it. Step 5 then reaps
        and records, at which point nothing is signalled again.

        That constraint is also why step 3 does not call `wait()`. `os.waitid(..., WNOWAIT)` —
        the POSIX way to wait for an exit without consuming it — is not available on macOS, so
        step 3 bounds its wait on the child's stdout having reached EOF, which the drain already
        establishes, and escalates on the timer when it has not. Correctness does not rest on
        that reading: step 4 delivers `SIGKILL` to the group unconditionally, which is a kernel
        guarantee about every live member rather than an inference about one, and step 5's
        bounded reap is what reports a child that outlived even that.

        WHO ACTUALLY DOES STEPS 4 AND 5 IS THE GUARDIAN, on order. It is the child's parent, so
        it is the only process here that holds the pin the paragraph above is about — the proxy
        asking the kernel to signal a number it merely remembers is the thing that has no such
        guarantee (review, PR #103). What the proxy keeps is the sequence, the bounds and the
        record; what it delegates is every real signal, plus the reap that ends the pin.
        """
        self._step(audit.INTAKE_CLOSED, self._close_intake)                  # step 1
        if self.child_pid is None:
            # `spawn_failed`: there is no child, so four facts are legitimately inapplicable —
            # the one shape a lazily written validator rejects along with the malformed ones.
            for fact in audit.FACTS[1:]:
                self._not_applicable(fact)
            self._release_guardian()
            return
        self._step(audit.CHILD_STDIN_CLOSED, self._close_child_stdin)        # step 2a
        self._step(audit.DRAIN_ENDED, self._drain)                           # steps 2b and 3
        delivered = self._step(audit.GROUP_TERMINATED, self._terminate_group)   # step 4
        self._step(audit.CHILD_REAPED, self._reap_child)                        # step 5
        # Step 4's fact is settled here, once the reap has removed the one member the guardian
        # was deliberately keeping alive. `delivered` is false when the fault point claimed the
        # step or when delivery itself errored, and in both cases the fact is already written.
        if delivered and audit.GROUP_TERMINATED not in self.facts:
            self._guard(audit.GROUP_TERMINATED, self._confirm_group_gone)
        # LAST, and outside every step: the guardian is released once the record of what the
        # teardown managed is complete, so nothing above can be reordered ahead of it.
        self._release_guardian()

    def _step(self, fact: str, run) -> bool:
        """One step, skipped if the fault point claims it and guarded either way."""
        if self._injected(fact):
            return False
        return self._guard(fact, run)

    def _guard(self, fact: str, run) -> bool:
        """Run one part of one step, and never let an exception skip the rest of the sequence.

        The catch-all is `shutdown_anomaly` keyed to the EXACT fact — never to the step. A step
        number is coarser than the thing it identifies (step 2 owns both `child_stdin_closed`
        and `drain_ended`), so a step-keyed anomaly would license `failed` on either of them, or
        on both, having arisen from one operation. A pairing key one level coarser than what it
        pairs is not a weaker check; it is a check that passes for the wrong reason.
        """
        try:
            run()
        except _DrainFailed:
            return False
        except BaseException as exc:                          # noqa: BLE001 — that is the point
            _note(f"mcp-proxy: teardown step {fact} raised: {exc!r}")
            self._outcome(audit.SHUTDOWN_ANOMALY, fact=fact, exception=repr(exc))
            self._failed(fact, audit.SHUTDOWN_ANOMALY)
            return False
        return True

    def _close_intake(self) -> None:
        """Step 1 — no client traffic accepted after this point. It always applies."""
        self._done(audit.INTAKE_CLOSED)

    def _close_child_stdin(self) -> None:
        """Step 2 — the portable graceful signal (§10.2)."""
        _close(self.child_in)
        self.child_in = None
        self._done(audit.CHILD_STDIN_CLOSED)

    def _drain(self) -> None:
        """Steps 2b and 3 — drain the child's stdout, escalating on the bounded timer.

        THE PROXY NEVER SYNTHESIZES A CLOSURE. The graceful-closure result for an open
        `subscriptions/listen` is the SERVER's statement about the server's subscription, so the
        real one is forwarded and none is manufactured: asserting in the server's voice that a
        subscription closed cleanly is a thing the proxy cannot know, and is the same
        fabrication §10.4 refuses everywhere else.

        THE DRAIN AND THE ESCALATION ARE ONE WAIT, not two bounded independently. They are the
        same question — has the child finished? — asked of the same descriptor, and a drain with
        its own separate deadline reports a server that merely needed `SIGKILL` as a
        `shutdown_anomaly`, when §10.5.1 classifies forced termination as CLEAN. That would fail
        a conforming cell, which §10.5 weighs exactly as heavily as forwarding a definition.

        AND IT IS STILL BOUNDED, because the child's stdout can be held open by something the
        group signal does not reach — a helper the server put in a different process group. A
        drain that never reaches EOF is a `shutdown_anomaly` rather than a hang: an
        unaccounted-for process holding a pipe from a credential-bearing server is exactly what
        a clean verdict must not certify.
        """
        if self._drain_until(time.monotonic() + self.grace):
            self._done(audit.DRAIN_ENDED)
            return
        if self.fault.mode_for(audit.CHILD_REAPED) is not None:
            # §10.9's control leaves the child and its group alive ON PURPOSE, and step 3's
            # escalation would kill exactly what the case exists to observe. Gating on the
            # arming rather than on the fixture's manners is the point: "a proxy that skipped
            # step 4 would pass on the helper's good manners" is the failure mode being avoided.
            raise TimeoutError("step 3 is suppressed, and the child has not finished")
        for order in (ORDER_TERM, ORDER_KILL):
            self._deliver(order)
            if order == ORDER_KILL:
                # The spec makes forced termination the standard escalation and only SHOULDs a
                # prompt exit, so this is worth recording and not worth failing on. The
                # terminator's promise — child reaped, group gone — still holds.
                self._outcome(audit.SHUTDOWN_CHILD_KILLED)
            if self._drain_until(time.monotonic() + self.grace):
                self._done(audit.DRAIN_ENDED)
                return
        raise TimeoutError(
            f"the child's stdout was still open after a group SIGKILL, so something outside "
            f"the child's process group is holding it {self.grace:.1f}s later")

    def _drain_until(self, deadline: float) -> bool:
        """Pump the child's stdout until EOF or the deadline. True iff EOF was reached."""
        if self._child_eof:
            return True
        sel = selectors.DefaultSelector()
        try:
            sel.register(self.child_out, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                if not sel.select(min(remaining, 0.05)):
                    continue
                try:
                    chunk = os.read(self.child_out, _READ_CHUNK)
                except OSError as exc:
                    # Same argument as `read_failed`, and the one §4 has already caught in the
                    # wild: a swallowed `OSError` that presents as a clean end of stream.
                    _note(f"mcp-proxy: drain read failed: {exc}")
                    self._outcome(audit.SHUTDOWN_READ_FAILED)
                    self._failed(audit.DRAIN_ENDED, audit.SHUTDOWN_READ_FAILED)
                    raise _DrainFailed from exc
                if not chunk:
                    self._child_eof = True
                    return True
                self._consume(proxy.S2C, chunk, shutting_down=True)
        finally:
            sel.close()

    def _deliver(self, order: bytes) -> str | None:
        """One signal to the child's group, BY ORDER and only ever by order.

        ONE RULE, ONE ROUTE: a real signal is sent only by a process that can show the group id
        has not been recycled, and the only such process is the guardian, which holds the child
        unreaped. So this method carries an order and an answer, and when there is nobody to
        carry it to it reports a failure rather than doing the work itself. That is not a gap —
        it is the honest end of the design (§10.6), because no portable handle on a process
        survives its parent's death.

        THE ERRNO IS NOT THE EVIDENCE, on the guardian's side either. `EPERM` was once read as
        success on the strength of a measurement — macOS returns it for a group whose members
        are all zombies, where Linux returns 0 — but that establishes ONE cause of `EPERM`, not
        an equivalence. `kill(2)` defines it as the inability to signal group members, which a
        sandbox restriction or a differently credentialed descendant also produces, and either
        would leave a live member while the branch certified success (review, PR #103). So
        neither `EPERM` nor `ESRCH` decides anything: emptiness is checked positively, by a
        probe that sends nothing.
        """
        report = None if self._guardian_lost() else self._ask(order)
        if report is None:
            return (f"the guardian is gone, so nothing holds the identity of group "
                    f"{self.child_pgid}; a remembered pgid could name a stranger")
        return None if report.get("signalled") else str(report.get("error"))

    def _terminate_group(self) -> None:
        """Step 4 — the GROUP, not just the child, and while the child is still unreaped.

        A stdio server may spawn helpers, and they inherit the interpolated environment — so a
        surviving grandchild is a live credential the run no longer accounts for.

        DELIVERY HAPPENS HERE; THE VERDICT IS SETTLED AFTER THE REAP. Signalling while the child
        is unreaped is what makes the pgid provably still ours — an unreaped member holds the id
        against reuse — but it is also what makes the group impossible to observe as empty,
        since that member is in it. So this step delivers `SIGTERM` then `SIGKILL` and records
        only errors it can attribute; `_confirm_group_gone` runs after step 5 and decides.
        """
        for order in (ORDER_TERM, ORDER_KILL):
            error = self._deliver(order)
            if error is not None:
                _note(f"mcp-proxy: group kill failed: {error}")
                self._outcome(audit.SHUTDOWN_GROUP_KILL_FAILED)
                self._failed(audit.GROUP_TERMINATED, audit.SHUTDOWN_GROUP_KILL_FAILED)
                return
            if order == ORDER_TERM:
                time.sleep(min(self.grace, 0.2))

    def _reap_child(self) -> None:
        """Step 5 — the guardian reaps, releases the pin, and reports; this records what it said.

        THE REAPER IS THE GUARDIAN, so `child_status` is the guardian's statement rather than
        this process's. The rule it was written for survives the move — only a process that
        reaped a child can hold its exit status, so a `done` without one claims a reap while
        lacking the one piece of evidence a reaper necessarily has — because the proxy has
        nowhere else to obtain a status either, and the fact and the number arrive together in
        the same report or not at all.

        THE ORDER IS ALSO WHAT MAKES THE SWEEP HAPPEN. `Guardian.release` terminates the group
        before it reaps, unconditionally, because reaping is what ends its licence to signal —
        so the one ordering the proxy must never get wrong is asking for the reap before it has
        finished with the group, and it cannot: there is nothing left it could ask for after.
        """
        self._release_report = report = self._ask(ORDER_RELEASE)
        if report is None or not report.get("reaped"):
            # Unreapable even after a group-wide `SIGKILL`, or nobody left to do the reaping.
            # The terminator's central promise is exactly this, so it cannot be the part that is
            # allowed to fail quietly.
            _note(f"mcp-proxy: the child was not reaped: "
                  f"{(report or {}).get('error', 'the guardian did not answer')}")
            self._outcome(audit.SHUTDOWN_REAP_FAILED)
            self._failed(audit.CHILD_REAPED, audit.SHUTDOWN_REAP_FAILED)
            return
        self._done(audit.CHILD_REAPED)
        status = report.get("status")
        # The status is DATA and deliberately not a verdict input: MCP specifies no exit code
        # for a server that saw its stdin close, so failing on a non-zero one would fail
        # conforming servers. A non-integer is dropped rather than written, since the reader
        # requires an integer beside a `done` and a record it rejects says less than one that
        # says the reap failed.
        if audit.is_json_int(status):
            self.child_status = status

    def _confirm_group_gone(self) -> None:
        """The positive half of step 4, settled AFTER step 5's reap. Emptiness is the finding.

        Once the pinning member has been reaped the group is empty if and only if it no longer
        exists, and the guardian answers that in the same breath as the reap — the tightest
        window available, since it is the process the reap happened in. `probe_group_empty` is
        what it uses and what this falls back to when there is no report to read, which is the
        `guardian_lost` ending: a probe that sends nothing is the one group operation that
        stays sound without a pin.
        """
        report = self._release_report
        empty = (report.get("group") == GROUP_GONE if report is not None
                 else probe_group_empty(self.child_pgid, self.grace))
        if empty:
            self._done(audit.GROUP_TERMINATED)
            return
        _note(f"mcp-proxy: process group {self.child_pgid} still exists after SIGKILL")
        self._outcome(audit.SHUTDOWN_GROUP_KILL_FAILED)
        self._failed(audit.GROUP_TERMINATED, audit.SHUTDOWN_GROUP_KILL_FAILED)

    # -- step 6 -----------------------------------------------------------------------------

    def _finish(self) -> int:
        """Write the terminator LAST, then exit on the verdict of what it says.

        The verdict computed here is the proxy's own reading of its own record, used for the
        exit status and nothing else. It is NOT the check: a proxy broken enough to write a
        malformed terminator is precisely the wrong process to ask whether it did, and the rule
        that a claim and the thing it claims about must not have the same author applies here
        first. `verify_post_run` re-reads the file.
        """
        terminator = {
            "observed": list(self.state.observed),
            "triggers": self.triggers,
            "outcomes": self.outcomes,
            "facts": self.facts,
        }
        if self.fired:
            terminator["fired"] = [{"fact": f} for f in self.fired]
        if self.child_status is not None:
            terminator["child_status"] = self.child_status
        self.sink.write(audit.LINE_TERMINATOR, **terminator)

        assembled = dict(terminator)
        if self.fault.armed:
            assembled["fault_point"] = self.fault.record()
        found = audit.verdict(assembled)
        if not found.clean:
            _note(f"mcp-proxy: instance {self.sink.instance_id} ended anomalously: "
                  f"{found.anomalous or found.problems}")
        return 0 if found.clean else 1


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == [GUARDIAN_FLAG]:
        if len(args) != 2 or not args[1].isdigit():
            _note(f"usage: mcp_proxy_io.py {GUARDIAN_FLAG} <launch-order-fd>")
            return 2
        return run_guardian(int(args[1]))
    if len(args) != 1:
        _note("usage: mcp_proxy_io.py <config.json>")
        return 2
    try:
        cfg = load_config(args[0])
        fault = Fault.from_env()
        sink = AuditSink(cfg.audit_path, uuid.uuid4().hex)
    except ConfigError as exc:
        # OUTSIDE the instance boundary, so nothing was logged and nothing was spawned. Closed
        # from the other side: for a gated server, an audit log that does not exist fails the
        # cell, because a gated server whose proxy never ran means the gating never happened.
        _note(f"mcp-proxy: {exc}")
        return 2
    try:
        return Instance(cfg, sink, fault, grace=_grace()).run()
    finally:
        sink.close()


if __name__ == "__main__":                                   # pragma: no cover — the program
    sys.exit(main())
