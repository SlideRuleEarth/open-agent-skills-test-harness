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

DEFAULT_GRACE = 5.0

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

    @property
    def targets(self) -> frozenset[str]:
        return frozenset(self.modes)

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> Fault:
        env = os.environ if environ is None else environ
        if FAULT_ENV not in env:
            return cls()
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
        return cls(armed=True, modes=modes)

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
        self.child: subprocess.Popen | None = None
        self.child_pgid: int | None = None
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
            print(f"mcp-proxy: fault point aborting before {fact}", file=sys.stderr)
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
            started["fault_point"] = {"suppresses": sorted(self.fault.targets)}
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
                    print(f"mcp-proxy: pump failed: {exc!r}", file=sys.stderr)
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
        """§10.5 step 0. A failure here is an ordinary ending with a record like any other."""
        env = dict(os.environ)
        env.update(self.cfg.env)
        # The proxy's own control vars are not the child's business, and one of them names
        # descriptors the child is about to be handed by inheritance rather than by name.
        for key in (FAULT_ENV, INHERIT_ENV, GRACE_ENV):
            env.pop(key, None)
        inherit = _inherit_fds()
        try:
            self.child = subprocess.Popen(          # noqa: S603 — the declared server (§10.3)
                [self.cfg.command, *self.cfg.args],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                env=env, cwd=self.cfg.cwd, close_fds=True, pass_fds=inherit,
                # ITS OWN PROCESS GROUP, and this is a launch decision that exists only to make
                # step 4 possible: a stdio server may spawn helpers, they inherit the
                # interpolated environment, and a surviving grandchild is a live credential the
                # run no longer accounts for. Terminating the child alone would leave it.
                start_new_session=True)
        except OSError as exc:
            print(f"mcp-proxy: cannot start {self.cfg.command!r}: {exc}", file=sys.stderr)
            self._trigger(audit.SPAWN_FAILED)
            return False
        finally:
            # The driver's writers, closed the moment they have been handed on. Holding one
            # would mean its EOF could never arrive, and §10.9's positive case would hang
            # rather than pass — the proxy is on the inheritance path of both channels.
            for fd in inherit:
                try:
                    os.close(fd)
                except OSError:
                    pass
        try:
            self.child_pgid = os.getpgid(self.child.pid)
        except OSError:
            # The child is gone already. Not a spawn failure — it started — so this is the
            # `child_exit` ending, reached before the first byte rather than mid-request.
            self.child_pgid = self.child.pid
            self._trigger(audit.CHILD_EXIT)
            self.sink.write(audit.LINE_SPAWN, child_pid=self.child.pid,
                            child_pgid=self.child_pgid)
            return False
        # Facts the start record cannot hold, since it precedes the spawn. They are what lets
        # §10.9's teardown case check the proxy's account of the group against the group a
        # surviving process reports for itself, rather than discovering processes from outside
        # and deciding for itself which belonged to the run.
        self.sink.write(audit.LINE_SPAWN, child_pid=self.child.pid, child_pgid=self.child_pgid)
        return True

    # -- the pumps --------------------------------------------------------------------------

    def _pump(self, wake_r: int) -> None:
        """Both directions and the signal channel, until something latches a trigger."""
        sel = selectors.DefaultSelector()
        try:
            sel.register(CLIENT_IN, selectors.EVENT_READ, proxy.C2S)
            sel.register(self.child.stdout.fileno(), selectors.EVENT_READ, proxy.S2C)
            sel.register(wake_r, selectors.EVENT_READ, "signal")
            while not self.triggers:
                for key, _events in sel.select():
                    if key.data == "signal":
                        self._on_signal(wake_r)
                    else:
                        self._on_readable(key.data, sel)
                    if self.triggers:
                        break
        finally:
            sel.close()

    def _on_signal(self, wake_r: int) -> None:
        """`set_wakeup_fd` writes the signal NUMBER, so the trigger says which arrived."""
        try:
            raw = os.read(wake_r, _READ_CHUNK)
        except OSError:
            raw = b""
        for number in raw:
            self._trigger(audit.SIGNAL_INT if number == signal.SIGINT else audit.SIGNAL_TERM)

    def _on_readable(self, direction: str, sel: selectors.BaseSelector) -> None:
        source = CLIENT_IN if direction == proxy.C2S else self.child.stdout.fileno()
        try:
            chunk = os.read(source, _READ_CHUNK)
        except OSError as exc:
            # NOT EOF. An instrument failure must never wear the clean-shutdown label: a
            # swallowed `OSError` presenting as a quiet end of stream is the defect §4 has
            # already caught in the wild.
            print(f"mcp-proxy: read failed on {direction}: {exc}", file=sys.stderr)
            self._trigger(audit.READ_FAILED)
            return
        if not chunk:
            sel.unregister(source)
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

    def _consume(self, direction: str, chunk: bytes, *, shutting_down: bool) -> None:
        """Frame the bytes into lines and put each one through `decide()`.

        §10.4's rules still apply to what the DRAIN forwards — the shutdown is not a licence to
        pump bytes unfiltered — so a malformed or off-list frame arriving on the way out is a
        genuine second trigger, and an anomalous one.
        """
        self._buffers[direction] += chunk
        while b"\n" in self._buffers[direction]:
            line, self._buffers[direction] = self._buffers[direction].split(b"\n", 1)
            if not line.strip():
                continue
            self._act(direction, line.decode("utf-8", "replace"), shutting_down=shutting_down)
            if self.triggers and not shutting_down:
                return

    def _act(self, direction: str, line: str, *, shutting_down: bool) -> None:
        action = proxy.decide(line, direction=direction, allowed=self.cfg.allowed,
                              state=self.state, inflight=self.inflight, server=self.cfg.server)
        if isinstance(action, proxy.Fail):
            # The KIND only. The detail names methods and ids and can quote a message whose
            # contents the server chose, and this file goes into the archived artifacts.
            print(f"mcp-proxy: anomaly {action.anomaly.kind}: {action.anomaly.detail}",
                  file=sys.stderr)
            self._trigger(audit.PROTOCOL_ANOMALY, anomaly=action.anomaly.kind)
            return
        if isinstance(action, proxy.Drop):
            self.sink.write(audit.LINE_EVENT, event=audit.MESSAGE_DROPPED,
                            reason=action.reason)
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
            _write_all(CLIENT_OUT if to_client else self.child.stdin.fileno(),
                       payload)
        except OSError as exc:
            if shutting_down:
                # C3-1 measured this against agy: the client closes stdin and stops reading at
                # once, so the graceful-closure write raises EPIPE against a CONFORMING peer.
                # Record it, swallow it, continue — an exception escaping here would skip every
                # step below and orphan the child.
                self._outcome(audit.SHUTDOWN_WRITE_FAILED)
                return
            print(f"mcp-proxy: write failed: {exc}", file=sys.stderr)
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
        """
        self._step(audit.INTAKE_CLOSED, self._close_intake)                  # step 1
        if self.child is None:
            # `spawn_failed`: there is no child, so four facts are legitimately inapplicable —
            # the one shape a lazily written validator rejects along with the malformed ones.
            for fact in audit.FACTS[1:]:
                self._not_applicable(fact)
            return
        self._step(audit.CHILD_STDIN_CLOSED, self._close_child_stdin)        # step 2a
        self._step(audit.DRAIN_ENDED, self._drain)                           # steps 2b and 3
        self._step(audit.GROUP_TERMINATED, self._terminate_group)            # step 4
        self._step(audit.CHILD_REAPED, self._reap_child)                     # step 5

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
            print(f"mcp-proxy: teardown step {fact} raised: {exc!r}", file=sys.stderr)
            self._outcome(audit.SHUTDOWN_ANOMALY, fact=fact, exception=repr(exc))
            self._failed(fact, audit.SHUTDOWN_ANOMALY)
            return False
        return True

    def _close_intake(self) -> None:
        """Step 1 — no client traffic accepted after this point. It always applies."""
        self._done(audit.INTAKE_CLOSED)

    def _close_child_stdin(self) -> None:
        """Step 2 — the portable graceful signal (§10.2)."""
        try:
            self.child.stdin.close()
        except OSError:
            pass                     # already gone; the promise the fact makes is still kept
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
        for escalation in (signal.SIGTERM, signal.SIGKILL):
            self._signal_group(escalation)
            if escalation == signal.SIGKILL:
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
            sel.register(self.child.stdout.fileno(), selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                if not sel.select(min(remaining, 0.05)):
                    continue
                try:
                    chunk = os.read(self.child.stdout.fileno(), _READ_CHUNK)
                except OSError as exc:
                    # Same argument as `read_failed`, and the one §4 has already caught in the
                    # wild: a swallowed `OSError` that presents as a clean end of stream.
                    print(f"mcp-proxy: drain read failed: {exc}", file=sys.stderr)
                    self._outcome(audit.SHUTDOWN_READ_FAILED)
                    self._failed(audit.DRAIN_ENDED, audit.SHUTDOWN_READ_FAILED)
                    raise _DrainFailed from exc
                if not chunk:
                    self._child_eof = True
                    return True
                self._consume(proxy.S2C, chunk, shutting_down=True)
        finally:
            sel.close()

    def _terminate_group(self) -> None:
        """Step 4 — the GROUP, not just the child, and while the child is still unreaped.

        A stdio server may spawn helpers, and they inherit the interpolated environment — so a
        surviving grandchild is a live credential the run no longer accounts for. `ESRCH` is
        success: the group is gone, which is the goal.

        THE GROUP IS NOT CHECKED FOR EMPTINESS AFTERWARDS, and that is deliberate rather than an
        omission. This proxy is holding the child as an unreaped zombie precisely so the pgid
        still names a group it created, and a zombie is a member — so "the group is empty"
        cannot be true here, and a check written that way would report
        `shutdown_group_kill_failed` on every clean run. What step 4 promises is that `SIGKILL`
        reached the group, which is a kernel guarantee about every live member. That nothing
        survived is checked from OUTSIDE, by §10.9's liveness channels, because a completion
        fact is the proxy's own claim and this is the one that most needs a second author.

        `EPERM` IS SUCCESS HERE, which is not a swallowed error. macOS returns it from `killpg`
        when every member of the group is a zombie — measured, not assumed — where Linux
        returns 0 for the same group, so the errno is a platform difference about a group with
        nothing live in it rather than a permission problem. A group with a LIVE member always
        returns 0, because the child and anything it spawned run as this proxy's own uid.
        Reading it as a failure fails every clean shutdown on half the supported platforms,
        which §10.5 weighs exactly as heavily as forwarding a definition.
        """
        if self.child_pgid == os.getpgid(0):
            # `start_new_session=True` did not take effect, so this group is the proxy's own and
            # signalling it would reach the proxy, whatever spawned it, and every sibling in the
            # session. A cleanup must not reach further than what it created (§4), so it
            # refuses rather than guessing.
            print(f"mcp-proxy: refusing to signal group {self.child_pgid}, which is not the "
                  f"child's own", file=sys.stderr)
            self._outcome(audit.SHUTDOWN_GROUP_KILL_FAILED)
            self._failed(audit.GROUP_TERMINATED, audit.SHUTDOWN_GROUP_KILL_FAILED)
            return
        for escalation in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(self.child_pgid, escalation)
            except ProcessLookupError:
                self._done(audit.GROUP_TERMINATED)       # ESRCH: the group is gone
                return
            except PermissionError:
                self._done(audit.GROUP_TERMINATED)       # EPERM: nothing live in it (macOS)
                return
            except OSError as exc:
                # A surviving grandchild is a live credential the run no longer accounts for,
                # so a group teardown that errored for any other reason is an anomaly.
                print(f"mcp-proxy: group kill failed: {exc}", file=sys.stderr)
                self._outcome(audit.SHUTDOWN_GROUP_KILL_FAILED)
                self._failed(audit.GROUP_TERMINATED, audit.SHUTDOWN_GROUP_KILL_FAILED)
                return
            if escalation == signal.SIGTERM:
                time.sleep(min(self.grace, 0.2))
        self._done(audit.GROUP_TERMINATED)

    def _signal_group(self, sig: int) -> None:
        try:
            os.killpg(self.child_pgid, sig)
        except OSError:
            pass                     # step 4 is where a failing group signal is reported

    def _reap_child(self) -> None:
        """Step 5 — reap, and record the exit status alongside the fact.

        The status is DATA and deliberately not a verdict input: MCP specifies no exit code for
        a server that saw its stdin close, so failing on a non-zero one would fail conforming
        servers. What the terminator promises is that the child was reaped, not that it was
        happy about it — and a status is something only a reaper can hold, so recording one
        without having reaped would be a lie rather than an omission.
        """
        try:
            self.child.wait(timeout=self.grace)
        except subprocess.TimeoutExpired:
            # Unreapable even after a group-wide `SIGKILL`. The terminator's central promise is
            # exactly this, so it cannot be the part that is allowed to fail quietly.
            self._outcome(audit.SHUTDOWN_REAP_FAILED)
            self._failed(audit.CHILD_REAPED, audit.SHUTDOWN_REAP_FAILED)
            return
        self._done(audit.CHILD_REAPED)
        self.child_status = self.child.returncode

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
            assembled["fault_point"] = {"suppresses": sorted(self.fault.targets)}
        found = audit.verdict(assembled)
        if not found.clean:
            print(f"mcp-proxy: instance {self.sink.instance_id} ended anomalously: "
                  f"{found.anomalous or found.problems}", file=sys.stderr)
        return 0 if found.clean else 1


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: mcp_proxy_io.py <config.json>", file=sys.stderr)
        return 2
    try:
        cfg = load_config(args[0])
        fault = Fault.from_env()
        sink = AuditSink(cfg.audit_path, uuid.uuid4().hex)
    except ConfigError as exc:
        # OUTSIDE the instance boundary, so nothing was logged and nothing was spawned. Closed
        # from the other side: for a gated server, an audit log that does not exist fails the
        # cell, because a gated server whose proxy never ran means the gating never happened.
        print(f"mcp-proxy: {exc}", file=sys.stderr)
        return 2
    try:
        return Instance(cfg, sink, fault, grace=_grace()).run()
    finally:
        sink.close()


if __name__ == "__main__":                                   # pragma: no cover — the program
    sys.exit(main())
