#!/usr/bin/env python3
"""Verify the stdio MCP fixtures by driving them as a scripted client.

Two servers, one driver: `probe_era_mcp_server.py` (the C3-0/C3-1 measuring instrument)
and `echo_mcp_server.py` (the test double behind the mcp_echo_* scenarios). They are
checked together because they share a wire protocol and the same conformance obligations;
two copies of the driver would let them drift apart on exactly the details that matter.

The C3-0/C3-1 results table in DESIGN_MCP_Support.md §9 is only as trustworthy as the
instrument that produced it: if the shim misreads an era, every row is wrong and nothing
downstream would notice. This is that instrument's own check, kept in the repo so the
measurement is reproducible rather than resting on a scratch file that no longer exists.

Fixtures carry no selftest arms and are not mutation targets, so this lives in tools/
alongside the other runnable verifiers rather than in the arm count.

Drives the shim over pipes — no agent CLI, no network, a few seconds. Everything runs
behind a DEADLINE and the child is always reaped: a shim that stops answering must fail a
check, not hang the verifier, since a hang is the failure mode a broken instrument is
most likely to produce.

    python tools/verify_mcp_fixtures.py   # exits non-zero on any failure
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(os.path.dirname(HERE), "fixtures")
SHIM = os.path.join(FIXTURES, "probe_era_mcp_server.py")
ECHO = os.path.join(FIXTURES, "echo_mcp_server.py")
DEADLINE = 10.0

MODERN_META = {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                         "io.modelcontextprotocol/clientCapabilities": {}}}


def _pump(fh, q):
    """Read lines on a background thread and hand them over. Deliberately NOT select() on
    the pipe: `readline()` fills an 8 KiB userspace buffer, so a single syscall can pull
    several messages in at once and leave the kernel pipe empty — select() then reports
    "nothing to read" while a complete line is already sitting in the buffer. That is a
    hang, not a slow read, and it is exactly the failure this verifier is supposed to
    detect rather than suffer."""
    try:
        for line in fh:
            q.put(line)
    except (OSError, ValueError):
        pass  # the departed-reader scenario closes this pipe underneath us, on purpose
    q.put(None)


def _readline(q, deadline):
    """One line, "" at EOF, None once the deadline passes."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    try:
        line = q.get(timeout=remaining)
    except queue.Empty:
        return None
    return "" if line is None else line.decode()


def run(msgs, *, mode="dual", kill=None, ignore_sigterm=False, cancel=None,
        close_reader=False, server=SHIM, extra_env=None):
    """Send `msgs`, collect replies, then shut down via `kill` (a signal) or stdin close.

    `subscriptions/listen` deliberately gets no immediate reply — its JSON-RPC response IS
    the graceful-closure signal — so expected replies are computed per method rather than
    per id. Waiting on it would hang, which is exactly the bug this rewrite fixes.

    Returns (responses, notifications, records, stream). `stream` is every message in WIRE
    ORDER: the split views lose ordering, and "the acknowledgment comes first" is a claim
    about order, so asserting it against the split lists would pass for a late ack.

    `close_reader` shuts the client's read side before shutdown, reproducing the departed
    reader that produced agy's `broken_pipe` terminator in §9."""
    tmp = tempfile.mkdtemp(prefix="verify-shim-")
    log = os.path.join(tmp, "probe.jsonl")
    env = dict(os.environ, PROBE_MCP_LOG=log, PROBE_MCP_MODE=mode)
    if ignore_sigterm:
        env["PROBE_MCP_IGNORE_SIGTERM"] = "1"
    # The echo fixture's own knobs. Cleared first and then set only from `extra_env`, so an
    # ambient export cannot change what these checks measure — `env` starts as a copy of
    # os.environ, and a shell with ECHO_MCP_IDENTITY set would otherwise redden every
    # verbatim-echo assertion below with no indication why.
    for knob in ("ECHO_MCP_SERVER_NAME", "ECHO_MCP_IDENTITY"):
        env.pop(knob, None)
    env.update(extra_env or {})

    deadline = time.monotonic() + DEADLINE
    responses, notifications, stream = [], [], []
    p = subprocess.Popen([sys.executable, server], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    q = queue.Queue()
    # No pump in the departed-reader scenario: closing the pipe out from under a thread
    # that is blocked reading it is a race, and it hangs rather than failing. Nothing is
    # read there anyway — the whole point is that the client stopped listening.
    if not close_reader:
        threading.Thread(target=_pump, args=(p.stdout, q), daemon=True).start()
    try:
        pending = set()
        for m in msgs:
            p.stdin.write((json.dumps(m) + "\n").encode())
            p.stdin.flush()
            if m.get("id") is not None and m.get("method") != "subscriptions/listen":
                pending.add(m["id"])
        for m in (cancel or []):
            p.stdin.write((json.dumps(m) + "\n").encode())
            p.stdin.flush()

        while pending and not close_reader:
            line = _readline(q, deadline)
            if not line:
                break
            msg = json.loads(line)
            stream.append(msg)
            if msg.get("id") is None:
                notifications.append(msg)
            else:
                responses.append(msg)
                pending.discard(msg["id"])

        if close_reader:
            # Depart before the server finishes talking. Its next write — the graceful
            # closure — then lands on a pipe with no reader.
            time.sleep(0.2)
            p.stdout.close()

        if kill:
            time.sleep(0.15)
            p.send_signal(kill)
        else:
            p.stdin.close()
            while not close_reader:  # drain graceful-closure responses until EOF
                line = _readline(q, deadline)
                if line is None or line == "":
                    break
                msg = json.loads(line)
                stream.append(msg)
                (notifications if msg.get("id") is None else responses).append(msg)

        try:
            # A signalled child needs only a moment to prove whether it honoured the
            # signal; waiting out the full deadline on the ignore-SIGTERM case would add
            # ten seconds to every run to learn nothing more.
            p.wait(timeout=1.5 if kill else max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
    finally:
        if p.poll() is None:
            p.kill()
            p.wait(timeout=5)
        for fh in (p.stdin, p.stdout, p.stderr):
            try:
                fh.close()
            except (OSError, ValueError):
                pass
        recs = []
        if os.path.exists(log):
            recs = [json.loads(l) for l in open(log) if l.strip()]
        shutil.rmtree(tmp, ignore_errors=True)
    return responses, notifications, recs, stream


def ev(recs, name):
    return [r for r in recs if r["event"] == name]


fails = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}{'' if cond else '  <- ' + str(detail)[:300]}")
    if not cond:
        fails.append(label)


def modern(method, mid, **extra):
    return {"jsonrpc": "2.0", "id": mid, "method": method,
            "params": dict(MODERN_META, **extra)}


def modern_noc(method, mid, **extra):
    """Modern `_meta` with the version but NO clientCapabilities — the one direction the
    echo fixture tolerates by design."""
    return {"jsonrpc": "2.0", "id": mid, "method": method, "params": dict(
        {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}, **extra)}


def legacy_init(mid, version="2025-11-25"):
    """A COMPLETE legacy initialize. The legacy lifecycle requires protocolVersion,
    capabilities and clientInfo, and the shim now enforces that — so scenarios cannot get
    away with the partial params they used to send."""
    return {"jsonrpc": "2.0", "id": mid, "method": "initialize", "params": {
        "protocolVersion": version, "capabilities": {},
        "clientInfo": {"name": "verify-probe-shim", "version": "1.0"}}}


print("1. modern client that SKIPS server/discover (the P2 case)")
r, n, recs, st = run([modern("tools/list", 1)])
e = ev(recs, "era")
check("era decided", len(e) == 1, e)
check("era == modern", e and e[0]["era"] == "modern", e)
check("exact version captured", e and e[0]["version"] == "2026-07-28", e)
check("decided by _meta not method", e and e[0]["decided_by"] == "_meta", e)
check("tools/list answered", r and "tools" in r[0].get("result", {}), r)

print("2. modern results are CONFORMING (P1: resultType + caching hints)")
r, n, recs, st = run([modern("server/discover", 1), modern("tools/list", 2),
                  modern("ping", 3), modern("tools/call", 4, name="probe_noop")])
by = {m["id"]: m.get("result", {}) for m in r}
check("discover has resultType", by.get(1, {}).get("resultType") == "complete", by.get(1))
check("discover has ttlMs", "ttlMs" in by.get(1, {}), by.get(1))
check("discover has cacheScope", by.get(1, {}).get("cacheScope") == "public", by.get(1))
check("tools/list has resultType", by.get(2, {}).get("resultType") == "complete", by.get(2))
check("tools/list has ttlMs", "ttlMs" in by.get(2, {}), by.get(2))
check("tools/list has cacheScope", by.get(2, {}).get("cacheScope") == "public", by.get(2))
check("ping has resultType", by.get(3, {}).get("resultType") == "complete", by.get(3))
check("ping has NO caching hints", "ttlMs" not in by.get(3, {}), by.get(3))
check("tools/call has resultType", by.get(4, {}).get("resultType") == "complete", by.get(4))

print("3. legacy client, and legacy results are NOT modern-shaped")
r, n, recs, st = run([legacy_init(1, "2025-06-18"),
                  {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}])
e = ev(recs, "era")
check("era == legacy", e and e[0]["era"] == "legacy", e)
check("legacy version captured", e and e[0]["version"] == "2025-06-18", e)
by = {m["id"]: m.get("result", {}) for m in r}
check("initialize answered", "protocolVersion" in by.get(1, {}), by.get(1))
check("no resultType leaked into legacy", "resultType" not in by.get(2, {}), by.get(2))

print("4. forced fallback: legacy mode must log LEGACY, not the refused modern attempt")
r, n, recs, st = run([modern("server/discover", 1),
                  legacy_init(2, "2025-06-18")], mode="legacy")
by = {m["id"]: m for m in r}
check("discover refused", "error" in by.get(1, {}), by.get(1))
check("then initialize works", "result" in by.get(2, {}), by.get(2))
e = ev(recs, "era")
check("exactly one era record", len(e) == 1, e)
check("negotiated era is legacy", e and e[0]["era"] == "legacy", e)
check("refusal was logged", len(ev(recs, "violation")) + len(ev(recs, "refused")) == 1, recs)

print("5. forced fallback the other way: modern mode must log MODERN")
r, n, recs, st = run([{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                  modern("tools/list", 2)], mode="modern")
by = {m["id"]: m for m in r}
check("initialize refused", "error" in by.get(1, {}), by.get(1))
check("then modern request works", "result" in by.get(2, {}), by.get(2))
e = ev(recs, "era")
check("exactly one era record", len(e) == 1, e)
check("negotiated era is modern", e and e[0]["era"] == "modern", e)

print("6. subscriptions/listen: ack first, no immediate response, graceful closure at EOF")
r, n, recs, st = run([modern("subscriptions/listen", 1, notifications={"toolsListChanged": True}),
                  modern("tools/list", 2)])
acks = [m for m in n if m.get("method") == "notifications/subscriptions/acknowledged"]
check("acknowledgment sent", len(acks) == 1, n)
# Ordering is per SUBSCRIPTION, not per channel: on stdio every subscription shares one
# pipe, and the spec explicitly permits other subscriptions' messages to interleave ahead
# of an acknowledgment. Asserting "first on the wire" would be a stricter rule than the
# protocol has, and would fail a conforming server that happened to interleave.
def sub_stream(stream, sub_id):
    out = []
    for m in stream:
        meta = (m.get("params") or m.get("result") or {}).get("_meta") or {}
        if meta.get("io.modelcontextprotocol/subscriptionId") == sub_id:
            out.append(m)
    return out


s1 = sub_stream(st, 1)
check("acknowledgment is first WITHIN subscription 1",
      s1 and s1[0].get("method") == "notifications/subscriptions/acknowledged",
      [m.get("method") or m.get("id") for m in s1])
check("ack carries subscriptionId == request id",
      acks and acks[0]["params"]["_meta"]["io.modelcontextprotocol/subscriptionId"] == 1, acks)
check("ack reflects agreed filter",
      acks and acks[0]["params"]["notifications"] == {"toolsListChanged": True}, acks)
closing = [m for m in r if m.get("id") == 1]
check("graceful closure response sent", len(closing) == 1, r)
check("closure carries subscriptionId", closing and
      closing[0]["result"]["_meta"]["io.modelcontextprotocol/subscriptionId"] == 1, closing)
check("subscription open+close logged",
      len(ev(recs, "subscription_open")) == 1 and len(ev(recs, "subscription_close")) == 1, recs)

print("7. cancellation retires the subscription, and its id is then reusable")
r, n, recs, st = run([modern("subscriptions/listen", 1, notifications={"toolsListChanged": True})],
                 cancel=[{"jsonrpc": "2.0", "method": "notifications/cancelled",
                          "params": dict(MODERN_META, requestId=1)},
                         modern("ping", 1)])
check("cancellation observed", len(ev(recs, "subscription_cancelled")) == 1, recs)
check("no graceful closure after cancel", len(ev(recs, "subscription_close")) == 0, recs)
pings = [m for m in r if m.get("id") == 1 and "result" in m]
check("id 1 reusable after cancel", len(pings) >= 1, r)

print("8. notification is never answered")
r, n, recs, st = run([{"jsonrpc": "2.0", "method": "notifications/initialized",
                   "params": dict(MODERN_META)}, modern("ping", 1)])
check("exactly one response (ping only)", len(r) == 1, r)
check("no stray notifications", n == [], n)

print("9. C3-1: stdin close -> clean terminator")
r, n, recs, st = run([modern("ping", 1)])
check("stdin_eof logged", len(ev(recs, "stdin_eof")) == 1, recs)
t = ev(recs, "terminator")
check("terminator reason == stdin_eof", len(t) == 1 and t[0]["reason"] == "stdin_eof", t)

print("10. C3-1: SIGTERM -> terminator names the signal")
r, n, recs, st = run([modern("ping", 1)], kill=signal.SIGTERM)
s, t = ev(recs, "signal"), ev(recs, "terminator")
check("signal logged", s and s[0]["signal"] == "SIGTERM", recs)
check("terminator reason == signal", len(t) == 1 and t[0]["reason"] == "signal", t)

print("11. C3-1: SIGKILL -> NO terminator (absence is the answer)")
r, n, recs, st = run([modern("ping", 1)], kill=signal.SIGKILL)
check("start was logged", len(ev(recs, "start")) == 1, recs)
check("no terminator", len(ev(recs, "terminator")) == 0, recs)

print("12. escalation probe: SIGTERM ignored, process survives to be killed")
r, n, recs, st = run([modern("ping", 1)], kill=signal.SIGTERM, ignore_sigterm=True)
s = ev(recs, "signal")
check("SIGTERM logged as ignored", s and s[0]["action"] == "ignored", recs)
check("no terminator (was killed)", len(ev(recs, "terminator")) == 0, recs)

print("13. departed reader: graceful-closure write gets EPIPE (pins the agy §9 result)")
r, n, recs, st = run([modern("subscriptions/listen", 1, notifications={"toolsListChanged": True}),
                      modern("tools/list", 2)], close_reader=True)
t = ev(recs, "terminator")
check("subscription was open", len(ev(recs, "subscription_open")) == 1, recs)
check("closure was attempted before the write failed",
      len(ev(recs, "subscription_close")) == 1, recs)
check("terminator reason == broken_pipe",
      len(t) == 1 and t[0]["reason"] == "broken_pipe", t)

print("14. the OPENING request is validated too, not just later ones")
# Scenario 15 below establishes a valid era first, so it structurally cannot catch an
# opener that sets the very terms it should have been checked against.
r, n, recs, st = run([{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {
    "_meta": {"io.modelcontextprotocol/protocolVersion": "2099-01-01",
              "io.modelcontextprotocol/clientCapabilities": {}}}},
    modern("tools/list", 2)])
by = {m["id"]: m for m in r}
check("unsupported opening version rejected", "error" in by.get(1, {}), by.get(1))
check("rejected with -32022", by.get(1, {}).get("error", {}).get("code") == -32022, by.get(1))
e = ev(recs, "era")
check("rejected opener established NO era", len(e) == 1, e)
check("the accepted retry set the era", e and e[0]["version"] == "2026-07-28", e)
check("retry answered", "result" in by.get(2, {}), by.get(2))

print("15. an opener with no metadata at all establishes nothing")
r, n, recs, st = run([{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                      modern("tools/list", 2)])
by = {m["id"]: m for m in r}
check("bare opener rejected", "error" in by.get(1, {}), by.get(1))
check("no era recorded for it", len(ev(recs, "era")) == 1, ev(recs, "era"))
check("and it was not served in legacy shape", "result" not in by.get(1, {}), by.get(1))

print("16. clientCapabilities is required, not just protocolVersion")
r, n, recs, st = run([{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {
    "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {
        "_meta": {"io.modelcontextprotocol/clientCapabilities": {}}}},
    modern("tools/list", 3)])
by = {m["id"]: m for m in r}
check("missing capabilities rejected", "error" in by.get(1, {}), by.get(1))
check("... with -32602", by.get(1, {}).get("error", {}).get("code") == -32602, by.get(1))
check("missing version rejected", "error" in by.get(2, {}), by.get(2))
check("... also -32602", by.get(2, {}).get("error", {}).get("code") == -32602, by.get(2))
check("well-formed modern request still works", "result" in by.get(3, {}), by.get(3))

print("17. negotiated era is ENFORCED: modern-only mode refuses initialize")
r, n, recs, st = run([modern("tools/list", 1),
                      {"jsonrpc": "2.0", "id": 2, "method": "initialize",
                       "params": {"protocolVersion": "2025-06-18"}},
                      modern("ping", 3)], mode="modern")
by = {m["id"]: m for m in r}
check("era is modern", ev(recs, "era")[0]["era"] == "modern", recs)
check("initialize rejected", "error" in by.get(2, {}), by.get(2))
check("violation logged", len(ev(recs, "violation")) == 1, recs)
check("session still usable afterwards", "result" in by.get(3, {}), by.get(3))

print("18. ... and an UNSUPPORTED version is refused wherever it appears")
# Not a "switch": nothing is stateful. Another version this shim implemented would be
# judged on its own merits, request by request.
r, n, recs, st = run([modern("tools/list", 1),
                      {"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}},
                      {"jsonrpc": "2.0", "id": 3, "method": "ping", "params": {"_meta": {
                          "io.modelcontextprotocol/protocolVersion": "2099-01-01",
                          "io.modelcontextprotocol/clientCapabilities": {}}}}])
by = {m["id"]: m for m in r}
check("bare request after modern rejected", "error" in by.get(2, {}), by.get(2))
check("version switch rejected", "error" in by.get(3, {}), by.get(3))
check("version switch uses -32022",
      by.get(3, {}).get("error", {}).get("code") == -32022, by.get(3))
check("supported list advertised",
      by.get(3, {}).get("error", {}).get("data", {}).get("supported") == ["2026-07-28"],
      by.get(3))
check("both violations logged", len(ev(recs, "violation")) == 2, recs)

print("19. ... and a legacy-only server refuses modern metadata")
r, n, recs, st = run([legacy_init(1, "2025-06-18"),
                      modern("tools/list", 2),
                      {"jsonrpc": "2.0", "id": 3, "method": "ping", "params": {}}],
                     mode="legacy")
by = {m["id"]: m for m in r}
check("era is legacy", ev(recs, "era")[0]["era"] == "legacy", recs)
check("modern request rejected", "error" in by.get(2, {}), by.get(2))
check("violation logged", len(ev(recs, "violation")) == 1, recs)
check("plain legacy request still works", "result" in by.get(3, {}), by.get(3))

print("20. dual mode is ORDER-INDEPENDENT: modern first, then initialize, then bare")
r, n, recs, st = run([modern("tools/list", 1), legacy_init(2),
                      {"jsonrpc": "2.0", "id": 3, "method": "ping", "params": {}}])
by = {m["id"]: m for m in r}
check("modern request served", "result" in by.get(1, {}), by.get(1))
check("initialize accepted", "result" in by.get(2, {}), by.get(2))
check("bare request now legal", "result" in by.get(3, {}), by.get(3))
check("both eras recorded", len(ev(recs, "era")) == 1 and len(ev(recs, "era_also")) == 1,
      recs)

print("21. ... and the other order, which must behave the same")
r, n, recs, st = run([legacy_init(1), modern("tools/list", 2),
                      {"jsonrpc": "2.0", "id": 3, "method": "ping", "params": {}}])
by = {m["id"]: m for m in r}
check("initialize accepted", "result" in by.get(1, {}), by.get(1))
check("modern request served", "result" in by.get(2, {}), by.get(2))
check("bare request still legal", "result" in by.get(3, {}), by.get(3))
check("modern reply is modern-shaped",
      by.get(2, {}).get("result", {}).get("resultType") == "complete", by.get(2))

print("22. the method must match the REQUEST's era, not the other way round")
r, n, recs, st = run([modern("initialize", 1),
                      legacy_init(2),
                      {"jsonrpc": "2.0", "id": 3, "method": "server/discover",
                       "params": {}}])
by = {m["id"]: m for m in r}
check("modern-metadata initialize is unknown", "error" in by.get(1, {}), by.get(1))
check("... with -32601", by.get(1, {}).get("error", {}).get("code") == -32601, by.get(1))
check("bare server/discover after legacy init is unknown", "error" in by.get(3, {}),
      by.get(3))
check("... and NOT a DiscoverResult", "result" not in by.get(3, {}), by.get(3))

print("22b. ... and that gate covers EVERY modern-only method, not just discover")
# subscriptions/listen was introduced in 2026-07-28 and REPLACED the legacy
# resources/subscribe. The first version of this gate named server/discover alone and let
# this one through, serving a modern-only method under legacy semantics.
r, n, recs, st = run([legacy_init(1),
                      {"jsonrpc": "2.0", "id": 2, "method": "subscriptions/listen",
                       "params": {"notifications": {"toolsListChanged": True}}}])
by = {m["id"]: m for m in r}
check("bare subscriptions/listen is unknown", "error" in by.get(2, {}), by.get(2))
check("... with -32601", by.get(2, {}).get("error", {}).get("code") == -32601, by.get(2))
check("no acknowledgment emitted",
      not [m for m in n if str(m.get("method", "")).startswith("notifications/subscriptions")], n)
check("no subscription opened", len(ev(recs, "subscription_open")) == 0, recs)
check("no graceful closure either", len(ev(recs, "subscription_close")) == 0, recs)

print("23. a legacy-only server must never emit a recognized MODERN error")
r, n, recs, st = run([{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {
    "_meta": {"io.modelcontextprotocol/protocolVersion": "2099-01-01",
              "io.modelcontextprotocol/clientCapabilities": {}}}}], mode="legacy")
code = r and r[0].get("error", {}).get("code")
check("rejected", r and "error" in r[0], r)
check("NOT -32022 (that would identify it as modern)", code != -32022, r)
check("plain method-not-found instead", code == -32601, r)

print("24. presence is not validity")
r, n, recs, st = run([{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {
    "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
              "io.modelcontextprotocol/clientCapabilities": None}}},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {
        "_meta": {"io.modelcontextprotocol/protocolVersion": 20260728,
                  "io.modelcontextprotocol/clientCapabilities": {}}}},
    modern("tools/list", 3)])
by = {m["id"]: m for m in r}
check("null clientCapabilities rejected", "error" in by.get(1, {}), by.get(1))
check("... with -32602", by.get(1, {}).get("error", {}).get("code") == -32602, by.get(1))
check("numeric protocolVersion rejected", "error" in by.get(2, {}), by.get(2))
check("... as malformed, not unsupported",
      by.get(2, {}).get("error", {}).get("code") == -32602, by.get(2))
check("well-formed request still works", "result" in by.get(3, {}), by.get(3))

print("25. legacy initialize selects a SUPPORTED version rather than echoing")
r, n, recs, st = run([legacy_init(1, "2099-01-01")])
sel = r and r[0].get("result", {}).get("protocolVersion")
check("did not echo the unsupported version", sel != "2099-01-01", r)
check("selected one it implements", sel in ("2025-11-25", "2025-06-18"), r)
li = ev(recs, "legacy_initialize")
check("requested vs selected recorded", li and li[0]["requested"] == "2099-01-01"
      and li[0]["selected"] == sel, li)
check("downgrade flagged", li and li[0]["downgraded"] is True, li)
# The PRIMARY record is what C3-0's table reads. Asserting only the secondary
# `legacy_initialize` event let a wrong `era.version` pass unnoticed.
e = ev(recs, "era")
check("era.version is the SELECTED version", e and e[0]["version"] == sel, e)
check("era.version is not the client's guess", e and e[0]["version"] != "2099-01-01", e)
check("era retains what was requested", e and e[0].get("requested") == "2099-01-01", e)

print("26. ... and echoes a version it does support")
r, n, recs, st = run([legacy_init(1, "2025-06-18")])
check("supported request honoured",
      r and r[0].get("result", {}).get("protocolVersion") == "2025-06-18", r)
check("not flagged as a downgrade",
      ev(recs, "legacy_initialize")[0]["downgraded"] is False, recs)

print("27. legacy initialization happens ONCE; a second is a lifecycle violation")
r, n, recs, st = run([legacy_init(1, "2025-06-18"), legacy_init(2, "2025-11-25"),
                      {"jsonrpc": "2.0", "id": 3, "method": "ping", "params": {}}])
by = {m["id"]: m for m in r}
check("first initialize accepted", "result" in by.get(1, {}), by.get(1))
check("second initialize rejected", "error" in by.get(2, {}), by.get(2))
check("renegotiation did not happen",
      len(ev(recs, "legacy_initialize")) == 1, ev(recs, "legacy_initialize"))
check("and was not recorded as another era", len(ev(recs, "era_also")) == 0, recs)
check("violation logged", any(v.get("why", "").startswith("initialize after legacy")
                              for v in ev(recs, "violation")), ev(recs, "violation"))
check("session still usable on the negotiated version", "result" in by.get(3, {}), by.get(3))

print("28. legacy initialize requires its own params")
r, n, recs, st = run([{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-11-25"}},
                      legacy_init(2)])
by = {m["id"]: m for m in r}
check("initialize without capabilities/clientInfo rejected", "error" in by.get(1, {}),
      by.get(1))
check("... with -32602", by.get(1, {}).get("error", {}).get("code") == -32602, by.get(1))
check("complete initialize accepted", "result" in by.get(2, {}), by.get(2))



# ---------------------------------------------------------------------------
# fixtures/echo_mcp_server.py — the test double behind the mcp_echo_* scenarios.
# Two live scenarios depend on its LEGACY behaviour, so those arms are regression
# tests first and conformance tests second.
# ---------------------------------------------------------------------------

def echo(msgs, **kw):
    return run(msgs, server=ECHO, **kw)


print("E1. legacy path is UNCHANGED (two scenarios depend on it)")
r, n, recs, st = echo([{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-11-25"}},
                       {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                       {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                           "name": "echo", "arguments": {"text": "hi"}}}])
by = {m["id"]: m.get("result", {}) for m in r}
check("initialize selects the requested supported version",
      by.get(1, {}).get("protocolVersion") == "2025-11-25", by.get(1))
check("both tools advertised",
      [t["name"] for t in by.get(2, {}).get("tools", [])] == ["echo", "add"], by.get(2))
check("echo returns its text",
      by.get(3, {}).get("content", [{}])[0].get("text") == "hi", by.get(3))
check("no resultType leaked into legacy", "resultType" not in by.get(2, {}), by.get(2))
check("no caching hints leaked into legacy", "ttlMs" not in by.get(2, {}), by.get(2))

print("E2. modern path: server/discover, conforming and cacheable")
r, n, recs, st = echo([modern("server/discover", 1)])
res = r[0].get("result", {}) if r else {}
check("DiscoverResult returned", res.get("resultType") == "complete", res)
check("supportedVersions advertised", res.get("supportedVersions") == ["2026-07-28"], res)
check("caching hints present", res.get("ttlMs") == 0 and res.get("cacheScope") == "public", res)
check("advertises capabilities, NOT tool definitions",
      res.get("capabilities") == {"tools": {}} and "tools" not in
      [k for k in res if k != "capabilities"], res)

print("E3. modern path: same tools, modern shape")
r, n, recs, st = echo([modern("tools/list", 1), modern("ping", 2),
                       modern("tools/call", 3, name="echo", arguments={"text": "hi"})])
by = {m["id"]: m.get("result", {}) for m in r}
check("tool set is identical across eras",
      [t["name"] for t in by.get(1, {}).get("tools", [])] == ["echo", "add"], by.get(1))
check("tools/list has resultType", by.get(1, {}).get("resultType") == "complete", by.get(1))
check("tools/list has caching hints", "ttlMs" in by.get(1, {}), by.get(1))
check("ping has resultType", by.get(2, {}).get("resultType") == "complete", by.get(2))
check("ping has NO caching hints", "ttlMs" not in by.get(2, {}), by.get(2))
check("tools/call has resultType", by.get(3, {}).get("resultType") == "complete", by.get(3))
check("echo still echoes under modern",
      by.get(3, {}).get("content", [{}])[0].get("text") == "hi", by.get(3))

print("E4. era is per REQUEST, so a mixed connection is served correctly either way")
r, n, recs, st = echo([{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-11-25"}},
                       {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                       modern("tools/list", 3)])
by = {m["id"]: m.get("result", {}) for m in r}
check("bare request stays legacy-shaped", "resultType" not in by.get(2, {}), by.get(2))
check("modern request is modern-shaped",
      by.get(3, {}).get("resultType") == "complete", by.get(3))

print("E5. an opener with no protocol version establishes nothing")
# The echo half used to exercise only valid inputs, which is why two era inversions lived
# here while the probe half already pinned the same rules.
r, n, recs, st = echo([{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                       {"jsonrpc": "2.0", "id": 2, "method": "initialize",
                        "params": {"protocolVersion": "2025-11-25"}},
                       {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}}])
by = {m["id"]: m for m in r}
check("bare opener rejected", "error" in by.get(1, {}), by.get(1))
check("... and NOT served in legacy shape", "result" not in by.get(1, {}), by.get(1))
check("initialize accepted", "result" in by.get(2, {}), by.get(2))
check("bare request legal once legacy is established", "result" in by.get(3, {}), by.get(3))

print("E6. the method must match the request's era")
r, n, recs, st = echo([{"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {}},
                       modern("initialize", 2),
                       {"jsonrpc": "2.0", "id": 3, "method": "initialize",
                        "params": {"protocolVersion": "2025-11-25"}},
                       {"jsonrpc": "2.0", "id": 4, "method": "initialize",
                        "params": {"protocolVersion": "2025-11-25"}}])
by = {m["id"]: m for m in r}
check("bare server/discover is unknown", "error" in by.get(1, {}), by.get(1))
check("... NOT a legacy-shaped DiscoverResult", "result" not in by.get(1, {}), by.get(1))
check("modern-metadata initialize is unknown", "error" in by.get(2, {}), by.get(2))
check("... with -32601", by.get(2, {}).get("error", {}).get("code") == -32601, by.get(2))
check("second initialize rejected", "error" in by.get(4, {}), by.get(4))

print("E7. unsupported versions are refused, not impersonated")
r, n, recs, st = echo([{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {
    "_meta": {"io.modelcontextprotocol/protocolVersion": "2099-01-01"}}},
    {"jsonrpc": "2.0", "id": 2, "method": "initialize",
     "params": {"protocolVersion": "2099-01-01"}}])
by = {m["id"]: m for m in r}
check("unsupported modern version refused", "error" in by.get(1, {}), by.get(1))
check("... with -32022", by.get(1, {}).get("error", {}).get("code") == -32022, by.get(1))
check("... advertising what it does support",
      by.get(1, {}).get("error", {}).get("data", {}).get("supported") == ["2026-07-28"],
      by.get(1))
sel = by.get(2, {}).get("result", {}).get("protocolVersion")
check("legacy did not mirror an unknown revision", sel != "2099-01-01", by.get(2))
check("legacy selected one it implements", sel in ("2025-11-25", "2025-06-18"), by.get(2))

print("E8. missing clientCapabilities is still tolerated (the leniency that survives)")
r, n, recs, st = echo([{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {
    "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}}])
check("served despite absent capabilities", r and "result" in r[0], r)
check("and served in modern shape",
      r and r[0]["result"].get("resultType") == "complete", r)

print("E9. an explicit null version is malformed, not absent")
# The laundering state specifically: AFTER a valid legacy initialization, where the
# absent-version path is legal and would otherwise swallow a present-but-null field.
r, n, recs, st = echo([{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-11-25"}},
                       {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {
                           "_meta": {"io.modelcontextprotocol/protocolVersion": None}}},
                       {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}}])
by = {m["id"]: m for m in r}
check("legacy initialization accepted", "result" in by.get(1, {}), by.get(1))
check("null version rejected even with legacy in force", "error" in by.get(2, {}), by.get(2))
check("... with -32602 (malformed, not unsupported)",
      by.get(2, {}).get("error", {}).get("code") == -32602, by.get(2))
check("a genuinely bare request is still legal", "result" in by.get(3, {}), by.get(3))

r, n, recs, st = echo([{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-11-25",
    "_meta": {"io.modelcontextprotocol/protocolVersion": None}}}])
check("null-version initialize is not a legacy handshake", r and "error" in r[0], r)
check("... also -32602", r and r[0].get("error", {}).get("code") == -32602, r)

print("E10. capabilities without a version is a BROKEN modern request, not a legacy one")
# Again exercised AFTER legacy initialization: before it, the request fails merely because
# no era exists, which would pass against the bug rather than catching it.
r, n, recs, st = echo([{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-11-25"}},
                       {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {
                           "_meta": {"io.modelcontextprotocol/clientCapabilities": {}}}},
                       {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}}])
by = {m["id"]: m for m in r}
check("legacy initialization accepted", "result" in by.get(1, {}), by.get(1))
check("capabilities-only request rejected", "error" in by.get(2, {}), by.get(2))
check("... with -32602", by.get(2, {}).get("error", {}).get("code") == -32602, by.get(2))
check("... and NOT served as a legacy tool list", "result" not in by.get(2, {}), by.get(2))
check("a genuinely bare request is still legal", "result" in by.get(3, {}), by.get(3))
# The tolerance runs one way only: version present, capabilities absent, still served.
r, n, recs, st = echo([modern_noc("tools/list", 1)])
check("version without capabilities is still tolerated", r and "result" in r[0], r)
check("... and served in modern shape",
      r and r[0]["result"].get("resultType") == "complete", r)

print("E11. subscriptions/listen is declined, not faked")
r, n, recs, st = echo([modern("subscriptions/listen", 1,
                              notifications={"toolsListChanged": True})])
check("method not found", r and "error" in r[0], r)
check("... with -32601", r and r[0]["error"]["code"] == -32601, r)
check("no acknowledgment emitted", n == [], n)

# The knob regress_mcp_two_servers.yaml rests on. Its whole job is to make one instance's
# reply distinguishable from another's, so the checks are: the identity reaches the RESULT,
# it tracks SERVER_NAME rather than being a constant, and it is genuinely opt-in — the
# default must stay verbatim, because two live scenarios and every E-check above assert
# that shape.
print("E12. the identity marker is opt-in, opaque, and names the instance that answered")
_call = {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "echo", "arguments": {"text": "wolverine-11"}}}
_init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-11-25"}}


def _echo_text(**kw):
    r, _n, _recs, _st = echo([_init, _call], **kw)
    got = {m["id"]: m.get("result", {}) for m in r}
    return got.get(3, {}).get("content", [{}])[0].get("text")


check("default is verbatim (the contract two scenarios depend on)",
      _echo_text() == "wolverine-11", "identity leaked into the default reply")
check("a named server without the marker is STILL verbatim",
      _echo_text(extra_env={"ECHO_MCP_SERVER_NAME": "alpha"}) == "wolverine-11",
      "serverInfo alone must not change results — that is why the marker exists")
check("with the marker, the reply carries it",
      _echo_text(extra_env={"ECHO_MCP_IDENTITY": "kestrel-9f3a"}) == "kestrel-9f3a:wolverine-11",
      "identity must reach the result, not only serverInfo")
check("...and it is the instance's OWN marker, not a constant",
      _echo_text(extra_env={"ECHO_MCP_IDENTITY": "quarry-7b1c"}) == "quarry-7b1c:wolverine-11",
      "a fixed prefix would let one process satisfy a two-server routing assertion")
# The marker is INDEPENDENT of the advertised name. A scenario needs to assert on something
# the agent cannot reconstruct from its prompt, and the prompt necessarily contains the
# server aliases — so a marker derived from SERVER_NAME would be guessable and prove
# nothing (review, second round). This is the check that would fail if it were re-derived.
check("the marker is not derived from the server name",
      _echo_text(extra_env={"ECHO_MCP_SERVER_NAME": "alpha",
                            "ECHO_MCP_IDENTITY": "kestrel-9f3a"}) == "kestrel-9f3a:wolverine-11",
      "SERVER_NAME must not reach the prefix — a guessable marker is not evidence")
check("an empty marker is off, not an empty prefix",
      _echo_text(extra_env={"ECHO_MCP_IDENTITY": ""}) == "wolverine-11",
      "an unset-looking value must not produce ':wolverine-11'")
# Modern era too: the scenario pins claude (legacy today), but the fleet is split and agy
# is already modern, so a knob that worked on only one era would fail on the runner that
# most needs it.
r, _n, _recs, _st = echo([modern("tools/call", 5, name="echo",
                                 arguments={"text": "marmot-22"})],
                         extra_env={"ECHO_MCP_IDENTITY": "quarry-7b1c"})
check("identity works in the modern era as well",
      r and r[0].get("result", {}).get("content", [{}])[0].get("text")
      == "quarry-7b1c:marmot-22", r)

print()
print("E13. C3-2: the `initialize` delay window measures PIPELINING, in both directions")
# The instrument for probe C3-2 (§9) needs its own regression, and it needs to be checked
# BOTH ways: an instrument that reports "pipelined" for everything is as useless as one that
# reports it for nothing, and the second failure is the plausible one — it is what a buffered
# read path produces, since a pipelined line sits in Python's buffer while `select` reports
# the pipe quiet. `run()` above cannot be used here: it writes every message up front, which
# is pipelining by construction, so the timing has to be driven by hand.


def _init_window(*, pipelined: bool, held_ms: int = 400, delay: bool = True):
    """Drive one handshake with the response held, and return the `pipelining` record."""
    tmp = tempfile.mkdtemp(prefix="verify-window-")
    log = os.path.join(tmp, "probe.jsonl")
    env = dict(os.environ, PROBE_MCP_LOG=log, PROBE_MCP_MODE="dual")
    if delay:
        env["PROBE_MCP_INIT_DELAY_MS"] = str(held_ms)
    else:
        env.pop("PROBE_MCP_INIT_DELAY_MS", None)
    p = subprocess.Popen([sys.executable, SHIM], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    nxt = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    try:
        p.stdin.write((json.dumps(legacy_init(1)) + "\n").encode())
        p.stdin.flush()
        if not pipelined:
            p.stdout.readline()          # WAIT for the response, as a conforming client does
        p.stdin.write((nxt + "\n").encode())
        p.stdin.flush()
        p.stdin.close()
        p.wait(timeout=DEADLINE)
    finally:
        if p.poll() is None:
            p.kill()
    recs = [json.loads(ln) for ln in open(log) if ln.strip()]
    return next((r for r in recs if r["event"] == "pipelining"), None)


_pipe = _init_window(pipelined=True)
_wait = _init_window(pipelined=False)
_off = _init_window(pipelined=True, delay=False)
check("a client that pipelines is DETECTED", _pipe and _pipe["pipelined"] is True, _pipe)
check("...and what it sent is named", _pipe and _pipe["methods"] == ["tools/list"], _pipe)
check("a client that WAITS is not reported as pipelining",
      _wait and _wait["pipelined"] is False and _wait["count"] == 0, _wait)
check("the window names the initialize it held", _pipe and _pipe["initialize_id"] == 1, _pipe)
check("the held duration is recorded", _pipe and _pipe["held_ms"] == 400, _pipe)
# Off by default, or every other measurement in this file pays for it — and a shim that
# always paused would be measuring its own delay rather than the client.
check("the window is OFF unless asked for", _off is None, _off)

print()
print("FAILED: " + ", ".join(fails) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
