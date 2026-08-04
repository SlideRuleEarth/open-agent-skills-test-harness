#!/usr/bin/env python3
"""Drive `agentskill_evals/mcp_proxy_io.py` over real pipes and check what it did (§10.9).

The proxy is a program with a defined wire protocol on both sides, so it is testable without
any agent CLI: a scripted client on one side, a fixture server on the other, and the audit log
in between. That log is the point. Everything the decision layer does has a selftest arm and a
mutation behind it; NOTHING in the I/O half does, because `mutate_mcp.py` proves arms by
running a suite and this code is only reachable by running the real program. This file is the
whole of that coverage.

WHAT IT DRIVES, and each group is a clause of §10.9:

  1. The wire rules — the filtered list, the refused call, verbatim pass-through, and the
     notification rule in BOTH halves: forwarded to the server, and never answered.
  2. Every trigger and every cleanup outcome of §10.5.1, each on demand, because a reason
     nobody can produce is a reason nobody has tested.
  3. That the two axes stay INDEPENDENT — one anomalous trigger through a clean teardown, and
     one clean trigger through an anomalous one. That is the pair a single-slot `reason` cannot
     tell apart, so it is the pair that proves the two-axis model is doing work.
  4. The endings with no reason to record: a `SIGKILL` to the proxy, and a teardown that died
     before step 6. Both assert start-present, terminator-ABSENT, verdict-anomalous.
  5. The clean path with a credential-bearing helper, checked from OUTSIDE by two liveness
     pipes — the only case that can tell a proxy which tears the process group down from one
     that merely claims to. Every other assertion here passes just as well against a proxy
     whose step 4 is a no-op.
  6. The fault point itself: armed and wired to suppress nothing must STILL fail the instance,
     because otherwise a hook that silently never fires produces a passing run.

WHAT IT DELIBERATELY DOES NOT DRIVE. The structural validator is pinned on synthetic RECORDS,
not on runs, and those live in the selftest (`audit.*` and `audit_log.*` arms) where a mutation
can prove each one can fail. Driving them here as well would be a second copy of a rule with
nothing proving the copy still agrees with the original — including the two absence cases that
are statements about a FILE rather than about a process: a crash after the start record, and a
truncated final line.

    python tools/verify_mcp_proxy.py     # exits non-zero on any failure
"""
from __future__ import annotations

import json
import os
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

HARNESS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HARNESS)

from agentskill_evals import mcp_audit as A       # noqa: E402 — after the path bootstrap
from agentskill_evals import mcp_proxy_io as IO   # noqa: E402

PROXY = os.path.join(HARNESS, "agentskill_evals", "mcp_proxy_io.py")
ECHO = os.path.join(HARNESS, "fixtures", "echo_mcp_server.py")
TARGET = os.path.join(HARNESS, "fixtures", "proxy_target_server.py")

# Every bound in the proxy scales off this, so a case that has to reach step 3's SIGKILL costs
# under two seconds. Short enough to run the whole file in a few seconds; long enough that a
# loaded machine does not turn a pass into a flake.
GRACE = 0.5
DEADLINE = 30.0

fails: list[str] = []
ran = 0
# What the runs above ACTUALLY produced, accumulated from the records rather than listed by
# hand. A totality check written as a literal set is a copy of the enumeration that can agree
# with itself while nothing drove any of it — it proves the list has not grown, which is not
# what its label would claim.
driven_triggers: set[str] = set()
driven_outcomes: set[str] = set()


def check(label, cond, detail="") -> bool:
    global ran
    ran += 1
    print(f"  {'ok  ' if cond else 'FAIL'} {label}{'' if cond else '  <- ' + str(detail)[:400]}")
    if not cond:
        fails.append(label)
    return bool(cond)


def section(name: str) -> None:
    print(f"\n{name}")


# ---------------------------------------------------------------------------------------
# The liveness channels (§10.9)
# ---------------------------------------------------------------------------------------


class Channel:
    """One pipe, created here, whose write end is passed down to a chosen depth.

    EOF IS THE EVIDENCE, and it is monotone: once true it stays true, and before it is true
    something is demonstrably alive. Deliberately not `kill(pid, 0)` polling, which is a
    point-in-time question that races the proxy's own exit and can be answered by a recycled
    pid. The driver never writes; it holds the read end and closes its writer as soon as the
    proxy is spawned, because holding one means its EOF can never arrive and the positive case
    would hang rather than pass.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.reader, self.writer = os.pipe()
        self._buffer = b""

    def close_writer(self) -> None:
        if self.writer is not None:
            os.close(self.writer)
            self.writer = None

    def record(self, timeout: float = DEADLINE) -> dict | None:
        """The holder's nonce-bound announcement, or None if it never arrived."""
        deadline = time.monotonic() + timeout
        sel = selectors.DefaultSelector()
        sel.register(self.reader, selectors.EVENT_READ)
        try:
            while b"\n" not in self._buffer:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not sel.select(remaining):
                    return None
                chunk = os.read(self.reader, 4096)
                if not chunk:
                    return None          # PREMATURE EOF: nothing ever held this descriptor
                self._buffer += chunk
        finally:
            sel.close()
        line, self._buffer = self._buffer.split(b"\n", 1)
        try:
            return json.loads(line)
        except ValueError:
            return None

    def at_eof(self, timeout: float) -> bool:
        sel = selectors.DefaultSelector()
        sel.register(self.reader, selectors.EVENT_READ)
        try:
            if not sel.select(timeout):
                return False             # not readable: someone still holds the writer
            return os.read(self.reader, 4096) == b""
        finally:
            sel.close()

    def close(self) -> None:
        self.close_writer()
        try:
            os.close(self.reader)
        except OSError:
            pass


# ---------------------------------------------------------------------------------------
# Running one instance
# ---------------------------------------------------------------------------------------


def _readable(fd: int, timeout: float) -> bool:
    sel = selectors.DefaultSelector()
    sel.register(fd, selectors.EVENT_READ)
    try:
        return bool(sel.select(timeout))
    finally:
        sel.close()


class Result:
    def __init__(self, returncode, replies, log, allowed, server, stderr="") -> None:
        self.returncode = returncode
        self.replies = replies
        self.log = log
        self.stderr = stderr
        self.verdict = A.log_verdict(log, server=server, allowed=frozenset(allowed))
        self.instances, self.parse_problems = A.parse_log(log)

    @property
    def only(self):
        return self.instances[0] if self.instances else None

    @property
    def terminator(self) -> dict:
        return (self.only.terminator or {}) if self.only else {}

    @property
    def triggers(self) -> list[str]:
        return [t.get("reason") for t in self.terminator.get("triggers", [])]

    @property
    def outcomes(self) -> list[str]:
        return [o.get("kind") for o in self.terminator.get("outcomes", [])]

    @property
    def facts(self) -> dict:
        return self.terminator.get("facts", {})

    def state(self, fact: str) -> str | None:
        return (self.facts.get(fact) or {}).get("state")

    def observe(self):
        """Fold this run's record into the fleet-wide totality sets, and return self."""
        driven_triggers.update(t for t in self.triggers if t)
        driven_outcomes.update(o for o in self.outcomes if o)
        return self

    def events(self, kind: str) -> list[dict]:
        return [e for e in (self.only.events if self.only else ()) if e.get("event") == kind]

    def __repr__(self) -> str:
        return (f"rc={self.returncode} triggers={self.triggers} outcomes={self.outcomes} "
                f"facts={ {k: v.get('state') for k, v in self.facts.items()} } "
                f"problems={self.verdict.problems or [i.problems for i in self.verdict.instances]}"
                f" stderr={self.stderr.strip()[-200:]!r}")


def run(*, command=None, args=None, tools=("echo",), env=None, fault=None, send=(),
        signal_after=None, kill_after=None, close_stdout=False, stdin_fd=None,
        channels=(), server="echo", settle=0.3, grace=GRACE, warmup=0.0, reply_per_send=True,
        after_send=None):
    """One proxy instance, driven to completion, with its audit log read back.

    Everything runs behind a DEADLINE and the proxy is always reaped: a proxy that stops
    answering must fail a check rather than hang the verifier, since a hang is what a broken
    shutdown sequence is most likely to produce.
    """
    tmp = tempfile.mkdtemp(prefix="ase-mcp-proxy-")
    try:
        log_path = os.path.join(tmp, f"{server}.audit.jsonl")
        cfg_path = os.path.join(tmp, "proxy.json")
        with open(cfg_path, "w", encoding="utf-8") as handle:
            json.dump({"server": server, "command": command or sys.executable,
                       "args": list(args if args is not None else [ECHO]),
                       "env": dict(env or {}), "tools": list(tools),
                       "audit_log": log_path}, handle)

        child_env = dict(os.environ)
        child_env[IO.GRACE_ENV] = str(grace)
        child_env.pop(IO.FAULT_ENV, None)
        if fault is not None:
            child_env[IO.FAULT_ENV] = fault
        writers = [c.writer for c in channels]
        if writers:
            child_env[IO.INHERIT_ENV] = ",".join(str(w) for w in writers)

        # STDERR TO A FILE, not a pipe. The child inherits it and so can anything the child
        # spawns, so a pipe here is held open by every process in the group — and reading it
        # would block until the last of them exits, which in the control case is precisely the
        # process the case requires to still be running.
        err_path = os.path.join(tmp, "stderr.txt")
        err_handle = open(err_path, "w", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, PROXY, cfg_path],
            stdin=stdin_fd if stdin_fd is not None else subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=err_handle,
            env=child_env, pass_fds=tuple(writers))
        err_handle.close()
        # The driver's own copies, closed the moment the proxy has them. Each channel now has a
        # sole writer somewhere below, which is what makes an observation about a channel an
        # observation about a process rather than about the inheritance path.
        for chan in channels:
            chan.close_writer()
        if close_stdout:
            # BEFORE anything is sent. Closing it later lets the reply land in the pipe buffer
            # and succeed, so the write the case is about never fails — the arm would then be
            # measuring the buffer rather than the departed client.
            proc.stdout.close()
        time.sleep(warmup)

        replies = []
        try:
            # A case that supplied its own `stdin_fd` has no pipe to write down, and reaching
            # `None.write` there would report an AttributeError from the driver in place of
            # whatever the case was about.
            for msg in (send if proc.stdin is not None else ()):
                payload = msg if isinstance(msg, (str, bytes)) else json.dumps(msg)
                if isinstance(payload, str):
                    payload = payload.encode("utf-8")
                proc.stdin.write(payload + b"\n")
                proc.stdin.flush()
                if not close_stdout and reply_per_send:
                    # BOUNDED. A bare `readline()` blocks forever against a proxy that neither
                    # answers nor exits, and `proc.wait(timeout=...)` below is no help because
                    # it has not been reached yet — so the file's claim to run everything behind
                    # a deadline would be false exactly where a broken shutdown puts it to the
                    # test. A reply that never comes is a case failing on its assertions, which
                    # is a result; a driver that hangs is not.
                    line = b""
                    if _readable(proc.stdout.fileno(), DEADLINE):
                        line = proc.stdout.readline()
                    if line:
                        replies.append(line.decode("utf-8", "replace").strip())
        except OSError:
            pass                      # the proxy went away mid-conversation; the log says why

        time.sleep(settle)
        if after_send is not None:
            after_send(proc)
        if kill_after is not None:
            time.sleep(kill_after)
            proc.kill()
        elif signal_after is not None:
            proc.send_signal(signal_after)
        elif proc.stdin is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass
        try:
            returncode = proc.wait(timeout=DEADLINE)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            returncode = "HUNG"
        if proc.stdout and not proc.stdout.closed:
            rest = proc.stdout.read()
            replies += [ln for ln in rest.decode("utf-8", "replace").splitlines() if ln.strip()]
        with open(err_path, encoding="utf-8") as handle:
            stderr = handle.read()

        try:
            with open(log_path, encoding="utf-8") as handle:
                log = handle.read()
        except FileNotFoundError:
            # No log at all. For a gated server that fails the cell from the other side, and
            # here it means the proxy died before the boundary — which is a finding, not a
            # crash in the verifier.
            log = ""
        return Result(returncode, replies, log, tools, server, stderr).observe()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def reply_to(replies, req_id):
    for line in replies:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if isinstance(msg, dict) and msg.get("id") == req_id:
            return msg
    return None


def req(mid, method, **params):
    return {"jsonrpc": "2.0", "id": mid, "method": method, "params": params}


INIT = req(1, "initialize", protocolVersion="2025-11-25", capabilities={},
           clientInfo={"name": "verify", "version": "1"})


# ---------------------------------------------------------------------------------------
# 1. The wire rules
# ---------------------------------------------------------------------------------------
section("wire rules (§10.4):")

wire = run(send=[INIT, req(2, "tools/list"), req(3, "ping"),
                 req(4, "tools/call", name="echo", arguments={"text": "hi"}),
                 req(5, "tools/call", name="add", arguments={"a": 1, "b": 2})])

listed = reply_to(wire.replies, 2) or {}
check("an off-list tool is stripped from the advertisement the client sees",
      [t["name"] for t in (listed.get("result") or {}).get("tools", [])] == ["echo"], wire.replies)
check("...and the audit log records the filtering as the expected event it is",
      [(e.get("forwarded"), e.get("removed")) for e in wire.events(A.TOOLS_ADVERTISED)]
      == [(["echo"], ["add"])], wire.events(A.TOOLS_ADVERTISED))

refused = reply_to(wire.replies, 5) or {}
check("an off-list `tools/call` is answered by the proxy and never reaches the server",
      refused.get("error", {}).get("code") == -32601
      and "allowlist" in refused.get("error", {}).get("message", "")
      and [e.get("tool") for e in wire.events(A.CALL_REFUSED)] == ["add"], refused)
check("...while an on-list call is forwarded and recorded as forwarded",
      (reply_to(wire.replies, 4) or {}).get("result", {}).get("content")
      == [{"type": "text", "text": "hi"}]
      and [e.get("tool") for e in wire.events(A.CALL_FORWARDED)] == ["echo"], wire.replies)
check("a non-tool method passes through verbatim",
      (reply_to(wire.replies, 3) or {}).get("result") == {}, wire.replies)
check("...and the whole exchange still ends clean",
      wire.verdict.clean and wire.returncode == 0, wire)

# THE NOTIFICATION RULE HAS TWO HALVES and only one of them is visible from the client alone.
# "Never answered" is observable here; "forwarded verbatim" needs the server to say it saw one,
# which is what `PT_NOTIFY_ECHO` is for. Checking only the half that is easy would pass against
# a proxy that dropped every notification on the floor.
noted = run(args=[TARGET], tools=("alpha",), env={"PT_NOTIFY_ECHO": "1"}, server="target",
            send=[{"jsonrpc": "2.0", "method": "notifications/initialized"},
                  req(2, "ping")])
notifications = [json.loads(r) for r in noted.replies
                 if r.startswith("{") and "notifications/seen" in r]
check("a notification is FORWARDED to the server",
      len(notifications) == 1 and notifications[0].get("method") == "notifications/seen",
      noted.replies)
check("...and is never answered — the reply the client gets is to its own request",
      [json.loads(r).get("id") for r in noted.replies if json.loads(r).get("id") is not None]
      == [2] and noted.verdict.clean, noted.replies)

# The envelope cases a "no `id` means notification" reading would wave through.
for label, line in (("a JSON-RPC batch array", '[{"jsonrpc":"2.0","id":1,"method":"ping"}]'),
                    ("an error response with no `id`",
                     '{"jsonrpc":"2.0","error":{"code":-1,"message":"x"}}'),
                    ("a message carrying both `result` and `error`",
                     '{"jsonrpc":"2.0","id":9,"result":{},"error":{"code":-1,"message":"x"}}'),
                    ("unparseable JSON", "not json at all")):
    bad = run(send=[line])
    check(f"{label} is an anomaly, not traffic",
          bad.triggers[:1] == [A.PROTOCOL_ANOMALY] and not bad.verdict.clean
          and bad.returncode == 1, bad)


# ---------------------------------------------------------------------------------------
# 2. Every trigger, on demand (§10.5.1 axis 1)
# ---------------------------------------------------------------------------------------
section("every trigger, driven against the real program (§10.5.1):")

eof = run(send=[INIT])
check(f"{A.CLIENT_EOF}: the client closes stdin",
      eof.triggers == [A.CLIENT_EOF] and eof.verdict.clean, eof)

for name, sig in ((A.SIGNAL_TERM, signal.SIGTERM), (A.SIGNAL_INT, signal.SIGINT)):
    signalled = run(send=[INIT], signal_after=sig)
    check(f"{name}: the client signals, and the handler still writes a terminator",
          signalled.triggers == [name] and signalled.verdict.clean, signalled)

spawn_failed = run(command="/nonexistent/mcp-server")
check(f"{A.SPAWN_FAILED}: an ordinary ending with a record like any other",
      spawn_failed.triggers == [A.SPAWN_FAILED]
      and spawn_failed.only.spawn is None
      and all(spawn_failed.state(f) == A.NOT_APPLICABLE for f in A.FACTS[1:])
      and spawn_failed.state(A.INTAKE_CLOSED) == A.DONE
      and not spawn_failed.verdict.clean
      and spawn_failed.verdict.instances[0].anomalous == (A.SPAWN_FAILED,), spawn_failed)

exited = run(args=["-c", "raise SystemExit(3)"])
check(f"{A.CHILD_EXIT}: a server that exits while the connection is live",
      exited.triggers[:1] == [A.CHILD_EXIT] and not exited.verdict.clean, exited)

write_failed = run(args=[TARGET], tools=("alpha",), server="target", close_stdout=True,
                   send=[req(1, "ping")])
check(f"{A.CLIENT_WRITE_FAILED}: a write to a departed client DURING forwarding",
      write_failed.triggers[:1] == [A.CLIENT_WRITE_FAILED]
      and not write_failed.verdict.clean, write_failed)

child_write_failed = run(args=[TARGET], tools=("alpha",), server="target", warmup=0.6,
                         reply_per_send=False,
                         env={"PT_CLOSE_STDIN": "1", "PT_LINGER": "20"}, send=[req(1, "ping")])
check(f"{A.CHILD_WRITE_FAILED}: the server stopped reading and what we sent did not arrive",
      child_write_failed.triggers[:1] == [A.CHILD_WRITE_FAILED]
      and not child_write_failed.verdict.clean, child_write_failed)


def read_failed_case():
    """A read error on the client's stdin that is NOT an end of stream.

    A LOOPBACK SOCKET RESET, because the obvious candidates do not work. A directory descriptor
    makes CPython refuse to start at all (`<stdin> is a directory, cannot continue`), so the
    proxy never reaches its own boundary; a pty whose master is closed reads as plain EOF on
    macOS, which is the very thing this case has to be distinguishable from. `SO_LINGER {1, 0}`
    turns the driver's close into an RST, and the proxy's next read gets `ECONNRESET`.

    The distinction is the whole case: an instrument failure must never wear the clean-shutdown
    label, and a swallowed `OSError` presenting as a quiet end of stream is the defect §4 has
    already caught in the wild.
    """
    import socket
    import struct

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    near = socket.create_connection(listener.getsockname())
    far, _ = listener.accept()
    listener.close()
    near.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    try:
        return run(stdin_fd=far.fileno(), settle=0.0, after_send=lambda _proc: near.close())
    finally:
        far.close()
        near.close()


read_failed = read_failed_case()
check(f"{A.READ_FAILED}: a read error is not an end of stream",
      read_failed.triggers[:1] == [A.READ_FAILED] and not read_failed.verdict.clean,
      read_failed)

def _close_then_signal(proc):
    """Stdin first, the signal second, and the second one lands during the TEARDOWN.

    That is the whole case: `client_eof` has already latched, so anything after it is a
    runner-up, and the sweep that collects it runs after §10.5's six steps rather than inside
    the pump. The fixture is configured to make the teardown take about a second — a server
    that ignores SIGTERM and never closes stdout — so the signal cannot race past it.
    """
    proc.stdin.close()
    time.sleep(GRACE / 3)
    proc.send_signal(signal.SIGTERM)


# TWO CLEAN TRIGGERS MUST NOT COMPOSE INTO A FAILURE. `client_eof` then `signal_term` is a CLI
# closing stdin and then signalling — ordinary behaviour on half the fleet — so the runner-up is
# recorded behind the latch and classified by the same `is_clean`. The sweep that collects it
# runs AFTER the teardown, which is the only place a signal arriving mid-shutdown can be seen.
both = run(args=[TARGET], tools=("alpha",), server="target", send=[req(1, "ping")],
           env={"PT_IGNORE_TERM": "1", "PT_LINGER": "20"}, after_send=_close_then_signal)
check("a client that closes stdin and THEN signals records both, and stays clean",
      both.triggers == [A.CLIENT_EOF, A.SIGNAL_TERM] and both.verdict.clean, both)

anomaly = run(send=['{"jsonrpc":"2.0","id":1,"method":"tools/list"}'])
check(f"{A.PROTOCOL_ANOMALY}: it carries WHICH anomaly, not just that there was one",
      anomaly.triggers == [A.PROTOCOL_ANOMALY]
      and anomaly.terminator["triggers"][0].get("anomaly") == "no_era_established", anomaly)

check("every trigger in the enumeration was actually produced by a run above",
      driven_triggers == set(A.TRIGGERS),
      f"missing={sorted(set(A.TRIGGERS) - driven_triggers)} "
      f"unenumerated={sorted(driven_triggers - set(A.TRIGGERS))}")


# ---------------------------------------------------------------------------------------
# 3. Every cleanup outcome, on demand (§10.5.1 axis 2)
# ---------------------------------------------------------------------------------------
section("every cleanup outcome, driven against the real program (§10.5.1):")

farewell = run(args=[TARGET], tools=("alpha",), server="target", close_stdout=True,
               env={"PT_FAREWELL": '{"jsonrpc":"2.0","method":"notifications/bye"}'})
check(f"{A.SHUTDOWN_WRITE_FAILED}: measured against agy — recorded, swallowed, and CLEAN",
      farewell.outcomes == [A.SHUTDOWN_WRITE_FAILED]
      and farewell.state(A.DRAIN_ENDED) == A.DONE
      and farewell.verdict.clean, farewell)

killed = run(args=[TARGET], tools=("alpha",), server="target",
             env={"PT_IGNORE_TERM": "1", "PT_LINGER": "20"})
check(f"{A.SHUTDOWN_CHILD_KILLED}: forced termination is the standard escalation, so CLEAN",
      A.SHUTDOWN_CHILD_KILLED in killed.outcomes and killed.verdict.clean
      and killed.state(A.CHILD_REAPED) == A.DONE, killed)

# A helper OUTSIDE the child's process group, holding the child's stdout. The group signal
# cannot reach it, so the drain never sees EOF — an unaccounted-for process holding a pipe from
# a credential-bearing server, which is exactly what a clean verdict must not certify.
stuck_channel = Channel("stuck")
try:
    stuck = run(args=[TARGET], tools=("alpha",), server="target", channels=(stuck_channel,),
                env={"PT_HELPER_FD": str(stuck_channel.writer), "PT_NONCE": "stuck",
                     "PT_HELPER_ESCAPE": "1", "PT_HELPER_STDOUT": "1"})
finally:
    stuck_channel.close()
_stuck_anomaly = [o for o in stuck.terminator.get("outcomes", [])
                  if o.get("kind") == A.SHUTDOWN_ANOMALY]
check(f"{A.SHUTDOWN_ANOMALY}: a bounded drain that never reaches EOF says so",
      len(_stuck_anomaly) == 1
      and _stuck_anomaly[0].get("fact") == A.DRAIN_ENDED
      and "TimeoutError" in (_stuck_anomaly[0].get("exception") or "")
      and stuck.state(A.DRAIN_ENDED) == A.FAILED
      and (stuck.facts[A.DRAIN_ENDED] or {}).get("cause") == A.SHUTDOWN_ANOMALY
      and not stuck.verdict.clean, stuck)
check("...and the escalation that ran alongside it is recorded on its own axis entry",
      stuck.outcomes == [A.SHUTDOWN_CHILD_KILLED, A.SHUTDOWN_ANOMALY],
      f"step 3 escalated because stdout never reached EOF, which is true and CLEAN; the "
      f"anomaly is the drain's, and the two do not merge into one: {stuck.outcomes}")

# THE THREE THAT NOTHING OUTSIDE THE PROXY CAN ARRANGE. A read error on a pipe this process
# created, a child that survives a group SIGKILL, and a `killpg` failing for a reason other
# than the group being gone are not reachable from a driver — which is why the fault point
# exists at all (§10.9). `fact=fail` records the step's OWN typed outcome and no firing,
# because a record carrying both would say the step was attempted and failed AND that it never
# ran, and §10.5.1 rejects exactly that.
for fact, outcome in ((A.DRAIN_ENDED, A.SHUTDOWN_READ_FAILED),
                      (A.CHILD_REAPED, A.SHUTDOWN_REAP_FAILED),
                      (A.GROUP_TERMINATED, A.SHUTDOWN_GROUP_KILL_FAILED)):
    injected = run(send=[INIT], fault=f"{fact}={IO.FAIL}")
    check(f"{outcome}: recorded, paired to {fact}, and anomalous",
          injected.outcomes == [outcome]
          and injected.state(fact) == A.FAILED
          and (injected.facts[fact] or {}).get("cause") == outcome
          and not injected.only.terminator.get("fired")
          and not injected.verdict.clean, injected)

check("every cleanup outcome in the enumeration was actually produced by a run above",
      driven_outcomes == set(A.OUTCOMES),
      f"missing={sorted(set(A.OUTCOMES) - driven_outcomes)} "
      f"unenumerated={sorted(driven_outcomes - set(A.OUTCOMES))}")


# ---------------------------------------------------------------------------------------
# 4. The two axes are independent
# ---------------------------------------------------------------------------------------
section("the two axes stay independent (§10.5.1):")

dirty_trigger = anomaly                     # a protocol anomaly, through a CLEAN teardown
clean_trigger = run(send=[INIT], fault=f"{A.DRAIN_ENDED}={IO.FAIL}")
check("an anomalous trigger can end with a teardown that did everything right",
      dirty_trigger.outcomes == []
      and all(dirty_trigger.state(f) == A.DONE for f in A.FACTS)
      and dirty_trigger.triggers == [A.PROTOCOL_ANOMALY], dirty_trigger)
check("...and a clean trigger can end with a teardown that did not",
      clean_trigger.triggers == [A.CLIENT_EOF]
      and clean_trigger.outcomes == [A.SHUTDOWN_READ_FAILED], clean_trigger)
check("...so the two are different endings, which one slot could not tell apart",
      (dirty_trigger.triggers, dirty_trigger.outcomes)
      != (clean_trigger.triggers, clean_trigger.outcomes)
      and not dirty_trigger.verdict.clean and not clean_trigger.verdict.clean,
      (dirty_trigger, clean_trigger))


# ---------------------------------------------------------------------------------------
# 5. The endings with no reason to record (§10.5, the absence rule)
# ---------------------------------------------------------------------------------------
section("what is detected by absence (§10.5):")

for label, kwargs in (("an uncatchable SIGKILL to the proxy", {"kill_after": 0.0}),
                      ("a teardown that died before step 6",
                       {"fault": f"{A.GROUP_TERMINATED}={IO.ABORT}"})):
    gone = run(send=[INIT], **kwargs)
    check(f"{label}: start present, terminator ABSENT, verdict anomalous",
          gone.only is not None and gone.only.start is not None
          and gone.only.terminator is None
          and "terminator_absent" in gone.verdict.instances[0].problems
          and not gone.verdict.clean, gone)

check("...and the start record was flushed before the child was spawned",
      run(send=[INIT], kill_after=0.0).only.start is not None,
      "a buffered log makes a killed proxy indistinguishable from one that never ran")


def restart_case():
    """Two instances into ONE log, because unexpected exit is a spec-sanctioned restart trigger.

    The first is killed and the second is clean, which is the pair the no-heal rule is about:
    the file must hold both, and the cell verdict must be the conjunction. A log opened for
    truncation instead of append would leave only the clean one, and the anomalous instance
    would vanish rather than fail — the failure this log exists to catch, erased by the file
    mode it is opened with.
    """
    tmp = tempfile.mkdtemp(prefix="ase-mcp-restart-")
    try:
        log_path = os.path.join(tmp, "echo.audit.jsonl")
        cfg_path = os.path.join(tmp, "proxy.json")
        with open(cfg_path, "w", encoding="utf-8") as handle:
            json.dump({"server": "echo", "command": sys.executable, "args": [ECHO],
                       "env": {}, "tools": ["echo"], "audit_log": log_path}, handle)
        env = dict(os.environ, **{IO.GRACE_ENV: str(GRACE)})
        env.pop(IO.FAULT_ENV, None)
        for kill in (True, False):
            proc = subprocess.Popen([sys.executable, PROXY, cfg_path], stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    env=env)
            time.sleep(0.3)
            if kill:
                proc.kill()
            else:
                proc.stdin.close()
            proc.wait(timeout=DEADLINE)
        with open(log_path, encoding="utf-8") as handle:
            return handle.read()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


_restart_log = restart_case()
_restart = A.log_verdict(_restart_log, server="echo", allowed=frozenset({"echo"}))
_by_id = {v.instance_id: v for v in _restart.instances}
check("a restarted proxy APPENDS, so the killed instance is still in the file",
      len(_restart.instances) == 2
      and sorted(v.clean for v in _restart.instances) == [False, True],
      [(v.instance_id[:8], v.clean, v.problems) for v in _restart.instances])
check("...and one clean restart does not heal the instance before it",
      not _restart.clean and len(_by_id) == 2,
      "the cell verdict is the conjunction over every instance, not the latest verdict")


# ---------------------------------------------------------------------------------------
# 6. The fault point is itself under test (§10.9)
# ---------------------------------------------------------------------------------------
section("the fault point (§10.9):")

arm_only = run(send=[INIT], fault="")
check("armed and wired to suppress nothing STILL fails the instance",
      arm_only.triggers == [A.CLIENT_EOF]
      and arm_only.outcomes == []
      and all(arm_only.state(f) == A.DONE for f in A.FACTS)
      and not arm_only.only.terminator.get("fired")
      and not arm_only.verdict.clean
      and arm_only.verdict.instances[0].anomalous == (A.FAULT_POINT_CONFIGURED,), arm_only)
check("...and it is the ARMING that did it — nothing else in that record is anomalous",
      arm_only.only.start.get("fault_point") == {"suppresses": []}
      and all(A.is_clean(r) for r in arm_only.triggers + arm_only.outcomes),
      arm_only.only.start)


# ---------------------------------------------------------------------------------------
# 7. The clean path, checked from outside (§10.9)
# ---------------------------------------------------------------------------------------
section("the process group really goes away (§10.9):")


def group_case(*, fault=None, extra=None):
    """One run with both liveness channels wired, returning the channels still open."""
    nonce = uuid.uuid4().hex
    child_chan, helper_chan = Channel("child"), Channel("helper")
    env = {"PT_NONCE": nonce, "PT_CHILD_FD": str(child_chan.writer),
           "PT_HELPER_FD": str(helper_chan.writer), **(extra or {})}
    found = run(args=[TARGET], tools=("alpha",), server="target", env=env, fault=fault,
                channels=(child_chan, helper_chan), send=[req(1, "ping")])
    return nonce, child_chan, helper_chan, found


def cross_checked_group(nonce, child_rec, helper_rec, found):
    """The one group id every report agrees on, or None if any of them disagrees.

    THREE REPORTS, cross-checked before any of them is acted on. The child's is the independent
    one — the spawn record is the proxy's account of what it did, and this case is about not
    taking the proxy's word for anything. Disagreement fails rather than being reconciled: the
    control can otherwise be satisfied by the WRONG group entirely, with a mis-grouped helper
    announcing, surviving, being cleaned up successfully, and the child's actual group — the
    only thing step 4 is meant to terminate — never under test at all.
    """
    spawn = (found.only.spawn or {}) if found.only else {}
    if not child_rec or not helper_rec:
        return None
    if child_rec.get("nonce") != nonce or helper_rec.get("nonce") != nonce:
        return None
    if child_rec.get("pid") != child_rec.get("pgid"):
        return None              # `start_new_session=True` makes the child its own group leader
    if {child_rec["pgid"], helper_rec.get("pgid"), spawn.get("child_pgid")} != {child_rec["pgid"]}:
        return None
    return child_rec["pgid"]


nonce, child_chan, helper_chan, positive = group_case()
try:
    child_rec, helper_rec = child_chan.record(), helper_chan.record()
    group = cross_checked_group(nonce, child_rec, helper_rec, positive)
    check("the child, the helper and the spawn record agree on one process group",
          group is not None, (nonce, child_rec, helper_rec,
                              positive.only.spawn if positive.only else None))
    check("an ordinary clean shutdown leaves NOTHING from the instance alive",
          positive.verdict.clean
          and child_chan.at_eof(DEADLINE) and helper_chan.at_eof(DEADLINE),
          positive)
finally:
    child_chan.close()
    helper_chan.close()

# THE COMBINED NEGATIVE CONTROL. Without it the case above passes against a proxy whose step 4
# is a no-op AND against a channel that was never inherited: a broken `pass_fds`, or a holder
# that closes its writer while starting up, hands the driver an immediate EOF and a passing case
# with nothing torn down. The order is fixed — both records, no premature EOF, suppress steps
# 3-5, run the shutdown, wait for the terminator and for the proxy to exit, require BOTH
# channels still open, kill the cross-checked group, require both reach EOF.
#
# THE CHILD HAS TO STAY ALIVE, which is what `PT_CLOSE_STDOUT` and `PT_LINGER` are for. A
# fixture that exits when its stdin closes would EOF the child channel on its own, and the case
# would then be observing the fixture's good manners rather than the suppressed steps — the
# exact substitution §10.9 warns about one level up. Closing stdout while staying alive is what
# lets the drain settle cleanly so the ONLY anomalies in the record are the injected ones.
nonce, child_chan, helper_chan, control = group_case(
    fault=f"{A.CHILD_REAPED},{A.GROUP_TERMINATED}",
    extra={"PT_CLOSE_STDOUT": "1", "PT_LINGER": "20"})
try:
    child_rec, helper_rec = child_chan.record(), helper_chan.record()
    group = cross_checked_group(nonce, child_rec, helper_rec, control)
    # WHY THE RECORDS ARE READ AFTER THE RUN AND NOT BEFORE IT. §10.9 specifies the order as
    # "both records -> no premature EOF -> run the shutdown -> require both still open", and a
    # pipe keeps its data after its writer is gone, so reading the announcements here is not
    # the same observation as reading them earlier — it proves the descriptor once arrived, and
    # nothing about who holds it now. The separate no-premature-EOF step is not missing so much
    # as SUBSUMED: a holder that closed its writer early would leave its channel at EOF by the
    # time the still-open check runs, and that check happens at the strictly later moment, once
    # the proxy and every other ancestor is gone. Non-EOF then has exactly one explanation.
    if check("the control's three reports agree too, and it is a group of our own",
             group is not None and group not in (os.getpgid(0), os.getpgid(os.getppid())),
             (nonce, child_rec, helper_rec)):
        # By now the proxy is gone and no ancestor holds either writer, so two channels still
        # open can only be the child holding one and the helper holding the other — which is
        # simultaneously the ordering requirement, the proof that each descriptor reached its
        # intended holder, and the proof that the child channel really is scoped one level
        # shallower than the helper's.
        still_open = (not child_chan.at_eof(GRACE), not helper_chan.at_eof(GRACE))
        check("with steps 3-5 suppressed, BOTH channels are still open after the proxy exits",
              still_open == (True, True) and control.returncode != "HUNG", still_open)
        check("...and the record says so: both facts `failed`, both firings written",
              control.state(A.CHILD_REAPED) == A.FAILED
              and control.state(A.GROUP_TERMINATED) == A.FAILED
              and (control.facts[A.CHILD_REAPED] or {}).get("cause") == A.FAULT_POINT_FIRED
              and sorted(f["fact"] for f in control.only.terminator.get("fired", []))
              == sorted([A.CHILD_REAPED, A.GROUP_TERMINATED])
              and "child_status" not in control.only.terminator
              and not control.verdict.clean, control)
        # A test for leaked credential-bearing processes must not be the thing that leaks one.
        # And the closing EOF is not ceremony: it separates a real non-EOF from a stuck reader,
        # so the control cannot pass by never observing anything at all.
        try:
            os.killpg(group, signal.SIGKILL)
            swept = child_chan.at_eof(DEADLINE) and helper_chan.at_eof(DEADLINE)
        except OSError as exc:
            swept = f"the vouched-for group was already gone: {exc!r}"
        check("...and killing that one group closes both channels, so the reader was not stuck",
              swept is True,
              f"a non-EOF that never becomes EOF is a reader that never observed anything: "
              f"{swept}")
finally:
    child_chan.close()
    helper_chan.close()

print()
# SELF-REPORTED, for the reason the selftest's arm count is: a total kept by hand in
# `TODO_Contained_HOME.md` §4 was stale for two PRs running, because nothing makes forgetting it
# fail. A count that drops means checks were LOST, which is the one outcome neither a pass nor a
# failure reports — and half of these cases are guarded by an `if check(...)` that skips the
# rest of a group, so the number moving is a real signal rather than bookkeeping.
print("FAILED: " + ", ".join(fails) if fails else f"ALL PASS — {ran} checks")
sys.exit(1 if fails else 0)
