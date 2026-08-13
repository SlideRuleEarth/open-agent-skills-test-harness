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

Fixtures carry no selftest arms, so this lives in tools/ alongside the other runnable
verifiers rather than in the arm count. They ARE mutation targets now, though — `mutate_mcp.py`
routes any mutation of `fixtures/` or `tools/` to this file as the suite that must catch it,
under the `F*` heading. Until that existed, everything below was named and unproven: an
assertion nothing has ever been seen to break is a claim about the author's intent, not about
the fixture. Adding a check here is therefore only half the work; the other half is a mutation
that reddens it.

Drives the shim over pipes — no agent CLI, no network, a few seconds. Everything runs
behind a DEADLINE and the child is always reaped: a shim that stops answering must fail a
check, not hang the verifier, since a hang is the failure mode a broken instrument is
most likely to produce.

    python tools/verify_mcp_fixtures.py   # exits non-zero on any failure
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import pathlib
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

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
print("E14. C3-2: the probe's own CLASSIFICATION, which is the result a decision rests on")
# E13 above checks the shim's timing path. That is a different instrument from the probe that
# READS it, and the probe's `elif` chain — which decides whether a CLI's answer is "n/a", an
# unanswered question, or a measurement — had no check at all, so every one of E13's checks
# could stay green over a probe that reported a CLI that died as "modern n/a" (review, PR
# #100). Synthetic rows, because the point is the classification, not another live run.
sys.path.insert(0, HERE)
import probe_mcp_pipelining as PIPE  # noqa: E402


def _row(cli, *, connected=True, era=None, version="2025-11-25", pipelined=None, spawned=True):
    return {"cli": cli, "connected": connected, "spawned": spawned,
            "era": {"event": "era", "era": era, "version": version} if era else None,
            "pipelining": None if pipelined is None else
            {"event": "pipelining", "pipelined": pipelined,
             "count": 1 if pipelined else 0,
             "methods": ["tools/list"] if pipelined else [], "initialize_id": 1}}


_dead = _row("dead", connected=False)
_modern = _row("agy", era="modern", version="2026-07-28")
_gap = _row("codex", era="legacy")
_pipes = _row("copilot", era="legacy", pipelined=True)
_waits = _row("claude", era="legacy", pipelined=False)
check("an observed MODERN era is `n/a` — there is no `initialize` to hide behind",
      PIPE.classify(_modern) == PIPE.NOT_APPLICABLE, PIPE.classify(_modern))
check("a CLI that never handshook is NOT `n/a`",
      PIPE.classify(_dead) == PIPE.NO_ERA, PIPE.classify(_dead))
check("a legacy era whose window never ran is UNMEASURED, not a negative result",
      PIPE.classify(_gap) == PIPE.UNMEASURED, PIPE.classify(_gap))
check("pipelining and waiting are both measurements",
      (PIPE.classify(_pipes), PIPE.classify(_waits)) == (PIPE.PIPELINES, PIPE.WAITS))
check("BOTH ways of missing an answer count as unmeasured",
      PIPE.unmeasured([_dead, _modern, _gap, _pipes, _waits]) == ["dead", "codex"],
      PIPE.unmeasured([_dead, _modern, _gap, _pipes, _waits]))
check("a fully answered fleet is not reported as unmeasured",
      PIPE.unmeasured([_modern, _pipes, _waits]) == [])
# A BROKEN INSTRUMENT IS AN UNANSWERED ROW, and it is the one that LOOKS answered: the shim
# can log an era and a pipelining record before its reader dies. Handling it only in the exit
# status let the tool print the complete-fleet claim and then exit 1 — the same fleet-wide
# conclusion from an incomplete run as before, through a door the earlier fix did not cover
# (review, PR #100). So it is classified, not merely counted.
_broken = _row("broken", era="legacy", pipelined=False)
_broken["reader_failed"] = True
check("a row whose shim broke is classified as instrument-failed, not as a measurement",
      PIPE.classify(_broken) == PIPE.INSTRUMENT_FAILED, PIPE.classify(_broken))
check("...even though it looks answered on every other axis",
      _broken["connected"] and _broken["pipelining"] is not None
      and PIPE.classify({**_broken, "reader_failed": False}) == PIPE.WAITS)
check("...so it counts as unmeasured",
      PIPE.unmeasured([_waits, _broken]) == ["broken"], PIPE.unmeasured([_waits, _broken]))
_broken_sum = " ".join(PIPE.summary([_modern, _pipes, _waits, _broken]))
check("...and the fleet-wide negative CANNOT be printed over it",
      "across the whole fleet" not in _broken_sum
      and "costs the fleet nothing" not in _broken_sum, _broken_sum)
check("...with the cause named, not just the absence",
      "reader failed" in _broken_sum and "broken" in _broken_sum, _broken_sum)
check("...and the per-row verdict says so too",
      "INSTRUMENT FAILED" in PIPE.verdict(_broken), PIPE.verdict(_broken))
# The clean-run claim must still be reachable, or this check would pass on a tool that never
# concludes anything.
check("a fleet with no broken instrument still earns the complete claim",
      "across the whole fleet" in " ".join(PIPE.summary([_modern, _waits])))
# The summary is guidance a reader ACTS on, so it has to describe the design that exists:
# §10.2 refuses a request behind an unanswered handshake, in either outcome. An earlier
# version said pending traffic "is supported ... keep it", which was the rejected design.
_clean = " ".join(PIPE.summary([_modern, _waits]))
_dirty = " ".join(PIPE.summary([_pipes, _waits]))
_partial = " ".join(PIPE.summary([_dead, _gap, _waits]))
_nothing = " ".join(PIPE.summary([_dead, _gap]))
check("the clean outcome prices the REFUSAL rather than blessing pending traffic",
      "refus" in _clean and "allow" not in _clean and "supported" not in _clean, _clean)
check("the pipelining outcome says those cells fail, and names defer-and-replay",
      "refus" in _dirty and "FAIL" in _dirty and "defer" in _dirty, _dirty)
check("the unmeasured line distinguishes the two reasons",
      "never handshook" in _nothing and "window never ran" in _nothing, _nothing)
check("...and says nothing at all when there is nothing missing",
      "NOT MEASURED" not in _clean, _clean)
# A NEGATIVE CONCLUSION NEEDS EVERY ROW. "No CLI pipelined ... costs the fleet nothing" was
# printed whenever no row was positive — true of a run where every CLI failed to connect, and
# contradicted two lines later by the list of what was not measured (review, PR #100).
check("an incomplete run does NOT print the fleet-wide negative",
      "costs the fleet nothing" not in _partial and "No CLI pipelined" not in _partial,
      _partial)
check("...it says only what it measured, and calls the fleet cost unknown",
      "No MEASURED CLI pipelined" in _partial and "UNKNOWN" in _partial, _partial)
check("a run where NOTHING answered claims nothing either",
      "costs the fleet nothing" not in _nothing and "2 of 2" in _nothing, _nothing)
check("only a complete run earns the fleet-wide claim",
      "across the whole fleet" in _clean and "costs the fleet nothing" in _clean, _clean)
# C3-3 rides on the same run: §10.4 spends a request id once it reaches the server, refusing a
# reuse the spec permits, and that strictness needs a price. The probe must measure the
# identity the PROXY enforces, and must not overstate what a short run establishes.
sys.path.insert(0, os.path.dirname(HERE))
from agentskill_evals.mcp_proxy import request_id_key  # noqa: E402
from agentskill_evals.mcp_proxy import valid_request_id as P_valid  # noqa: E402
from agentskill_evals.mcp_proxy import classify_envelope  # noqa: E402

sys.path.insert(0, FIXTURES)
import probe_era_mcp_server as SHIM_MOD  # noqa: E402
# The HTTP fixture serves the stdio one's tools by IMPORTING them; this reads the same
# module so "they match" is checked against the definition rather than against a copy.
import echo_mcp_server as ECHO_MOD  # noqa: E402
# The endpoint paths come FROM the fixture: a verifier that retyped them would still
# pass if the fixture moved its endpoint, which is the one thing these path checks are for.
from http_mcp_server import PATH_SSE as PATH_SSE_T  # noqa: E402
from http_mcp_server import PATH_STREAMABLE as PATH_STREAMABLE_T  # noqa: E402

# The shim cannot import the proxy — CLIs spawn it with only the stdlib reachable — so it
# carries its own copy of the rule. A copy that could drift silently is worse than the
# duplication, so the two are checked against the cases that distinguish them.
for _v in (1, 1.0, 0, -0.0, "1", "a", 2**70, -5):
    check(f"the shim identifies id {_v!r} exactly as the proxy does",
          tuple(SHIM_MOD._id_key(_v)) == request_id_key(_v),
          (SHIM_MOD._id_key(_v), request_id_key(_v)))
check("...so the pairs the proxy calls equal are equal in the shim too",
      tuple(SHIM_MOD._id_key(1)) == tuple(SHIM_MOD._id_key(1.0))
      and tuple(SHIM_MOD._id_key(0)) == tuple(SHIM_MOD._id_key(-0.0))
      and tuple(SHIM_MOD._id_key(1)) != tuple(SHIM_MOD._id_key("1")))
def _tl(*pairs):
    """A timeline: ('req', v) / ('resp', v), with v canonicalized as the proxy would."""
    return [(kind, request_id_key(v)) for kind, v in pairs]


check("a monotonic run has no repeats at all",
      PIPE.id_findings(_tl(("req", 0), ("resp", 0), ("req", 1), ("resp", 1)))
      == {"requests": 2, "live_duplicates": [], "post_response_reuse": [],
          "truncated": False})
# THE DISTINCTION THAT WAS MISSING. A repeat while the first is unanswered is a live
# duplicate, which JSON-RPC forbids and the proxy refuses as `duplicate_request_id`; only a
# repeat AFTER the response prices §10.4's stricter rule (review, PR #100).
check("a repeat AFTER the response classifies as post-response reuse",
      PIPE.id_findings(_tl(("req", 1), ("resp", 1),
                           ("req", 1)))["post_response_reuse"] == [request_id_key(1)])
check("a repeat BEFORE the response is a live duplicate, not that reuse",
      PIPE.id_findings(_tl(("req", 1), ("req", 1),
                           ("resp", 1)))["post_response_reuse"] == [])
_dup = PIPE.id_findings(_tl(("req", 1), ("req", 1), ("resp", 1)))
check("...and it is still reported, as its own finding",
      _dup["live_duplicates"] == [request_id_key(1)] and _dup["post_response_reuse"] == [],
      _dup)
check("the two never trade places",
      PIPE.id_findings(_tl(("req", 1), ("resp", 1), ("req", 1)))["live_duplicates"] == [])
# THE PROXY STOPS AT A DUPLICATE, so the probe must too: `Fail` is terminal, the connection is
# torn down, and nothing after it would ever reach the rule being priced (review, PR #100).
_after_dup = PIPE.id_findings(_tl(("req", 1), ("req", 1), ("resp", 1), ("req", 1)))
check("evidence after a live duplicate does not price the stricter rule",
      _after_dup["post_response_reuse"] == [] and _after_dup["truncated"] is True, _after_dup)
check("...and the run is not credited with requests it never got to make",
      _after_dup["requests"] == 2, _after_dup)
check("a run with no duplicate is not marked truncated",
      PIPE.id_findings(_tl(("req", 1), ("resp", 1), ("req", 1)))["truncated"] is False)
_dirty_sum = " ".join(PIPE.id_summary(
    [{"cli": "a", "id_timeline": _tl(("req", 1), ("req", 1), ("resp", 1), ("req", 1))}]))
check("...and the summary reports one repeat, not two",
      "(1)" in _dirty_sum and "IDS ARE REUSED" not in _dirty_sum, _dirty_sum)
# THE FALSE NEGATIVE from the round before: `repr` dedup called these distinct, so a CLI whose
# reuse the proxy would refuse was reported as not reusing at all.
check("`1` then `1.0` is one id, because the proxy says it is",
      PIPE.id_findings(_tl(("req", 1), ("resp", 1),
                           ("req", 1.0)))["post_response_reuse"] == [request_id_key(1)])
check("`0` then `-0.0` is one id too",
      PIPE.id_findings(_tl(("req", 0), ("resp", 0),
                           ("req", -0.0)))["post_response_reuse"] == [request_id_key(0)])
check("...but `1` and `\"1\"` stay different ids, as JSON-RPC says",
      PIPE.id_findings(_tl(("req", 1), ("resp", 1),
                           ("req", "1")))["post_response_reuse"] == [])
check("the timeline is read from structured events, not the truncated `raw`",
      PIPE.id_timeline([{"event": "request_id", "id_key": ["n", 1], "method": "ping"},
                        {"event": "rx", "raw": '{"id":9,"method":"ping"}'},
                        {"event": "response_id", "id_key": ["n", 1]},
                        {"event": "era", "era": "legacy"}])
      == [("req", ("n", 1)), ("resp", ("n", 1))])
# C3-3 CONCLUDES NOTHING FROM THIS RUN, IN EITHER DIRECTION. The negative never could —
# allocation is not exercised once per connection, so no run length bounds the next id. The
# POSITIVE cannot either: classifying a repeat needs the order of an arrival against a
# response, and no observer inside the server establishes that. Stopping the process, writing
# a second request on a live id and resuming produced `req, resp, req` in 1 run of 20, which
# is a live duplicate reported as legal reuse (review, PR #100).
_clean_ids = " ".join(PIPE.id_summary([{"cli": "a", "id_timeline": _tl(
    ("req", 0), ("resp", 0), ("req", 1), ("resp", 1))}]))
_reuse_ids = " ".join(PIPE.id_summary([{"cli": "a", "id_timeline": _tl(
    ("req", 0), ("resp", 0), ("req", 0))}]))
_dup_ids = " ".join(PIPE.id_summary([{"cli": "a", "id_timeline": _tl(
    ("req", 1), ("req", 1), ("resp", 1))}]))
for _name, _text in (("a clean run", _clean_ids), ("an apparent reuse", _reuse_ids),
                     ("a live duplicate", _dup_ids)):
    check(f"{_name} is reported INCONCLUSIVE", "INCONCLUSIVE" in _text, _text)
    check(f"...and {_name} never claims to price or clear the rule",
          "costs the fleet nothing" not in _text and "the spec permits" not in _text
          and "FAIL" not in _text, _text)
check("the reason is named, not just the verdict",
      "cannot be established from inside the server" in _clean_ids, _clean_ids)
check("...and the sample it saw is still reported",
      "2 request(s)" in _clean_ids and "a=2" in _clean_ids, _clean_ids)
check("repeats are surfaced for a human, as UNCLASSIFIED",
      "UNCLASSIFIED" in _reuse_ids and "UNCLASSIFIED" in _dup_ids, (_reuse_ids, _dup_ids))
check("a clean run has nothing to surface",
      "UNCLASSIFIED" not in _clean_ids, _clean_ids)
# MALFORMED TRAFFIC MUST REACH THE REPORT. It was written to the raw log and dropped there:
# `probe()` carried only the timeline, so without -v a malformed client produced no finding.
_mal = " ".join(PIPE.id_summary([{"cli": "a", "id_timeline": _tl(("req", 1), ("resp", 1)),
                                  "id_anomalies": [{"event": "request_id_malformed"}]}]))
check("malformed requests are named in the summary",
      "MALFORMED" in _mal and "terminates the connection" in _mal, _mal)
check("...and a run without them says nothing about them",
      "MALFORMED" not in _clean_ids, _clean_ids)

print()
print("E15. C3-3: the shim logs a request id for EVERY request, whatever its size")
# The reading is only as good as the record. `rx` truncates at 4096 characters as a
# diagnostic, and the first version of this measurement reparsed that — so a request longer
# than the cut simply vanished, and a probe that silently omits requests cannot answer a
# question about reuse (review, PR #100). Driven live, because the defect lives in the shim's
# logging path and a unit test of the reader would not see it.
_big = "x" * 9000
_recs_ids = run([legacy_init(1),
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": "echo", "arguments": {"text": _big}}},
                 {"jsonrpc": "2.0", "id": "tail", "method": "ping"}])[2]
_ids = PIPE.request_ids(PIPE.id_timeline(_recs_ids))
_raw_ids = [r for r in _recs_ids if r.get("event") == "rx" and len(r.get("raw", "")) >= 4096]
check("a request far longer than the `raw` cut still yields its id",
      _ids == [("n", 1), ("n", 2), ("s", "tail")], _ids)
check("...and the `raw` record really was truncated, so the case was exercised",
      len(_raw_ids) == 1 and len(_raw_ids[0]["raw"]) == 4096, [len(r["raw"]) for r in _raw_ids])
check("notifications carry no id and produce no record",
      PIPE.request_ids(PIPE.id_timeline(run(
          [legacy_init(1),
           {"jsonrpc": "2.0", "method": "notifications/initialized"}])[2])) == [("n", 1)])

# SEQUENTIAL POST-RESPONSE REUSE, driven live. `run()` writes everything up front, so it
# cannot produce this: the second `ping(1)` has to go out AFTER the first is answered, or the
# shim sees a live duplicate and the two cases become indistinguishable — which was the whole
# finding. Driven by hand, like the pipelining window above.
def _sequenced(second_after_response: bool):
    """Two requests on id 1; the second either waits for the answer or races it."""
    tmp = tempfile.mkdtemp(prefix="verify-ids-")
    log = os.path.join(tmp, "probe.jsonl")
    env = dict(os.environ, PROBE_MCP_LOG=log, PROBE_MCP_MODE="dual")
    if not second_after_response:
        # The reviewer's reproduction: hold the `initialize` answer and pipeline behind it.
        env["PROBE_MCP_INIT_DELAY_MS"] = "400"
    p = subprocess.Popen([sys.executable, SHIM], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    try:
        p.stdin.write((json.dumps(legacy_init(1)) + "\n").encode())
        p.stdin.flush()
        if second_after_response:
            p.stdout.readline()          # WAIT for the answer, then reuse the id
        p.stdin.write((json.dumps({"jsonrpc": "2.0", "id": 1,
                                   "method": "ping"}) + "\n").encode())
        p.stdin.flush()
        p.stdin.close()
        p.wait(timeout=DEADLINE)
    finally:
        if p.poll() is None:
            p.kill()
    return PIPE.id_findings(PIPE.id_timeline(
        [json.loads(ln) for ln in open(log) if ln.strip()]))


_after = _sequenced(True)
_racing = _sequenced(False)
check("id 1 reused AFTER its answer is post-response reuse",
      _after["post_response_reuse"] == [("n", 1)] and _after["live_duplicates"] == [], _after)
check("id 1 pipelined behind a HELD answer is a live duplicate, not that reuse",
      _racing["live_duplicates"] == [("n", 1)]
      and _racing["post_response_reuse"] == [], _racing)
check("both runs really did send two requests, so neither verdict is an empty sample",
      _after["requests"] == 2 and _racing["requests"] == 2, (_after, _racing))

# THE ORDINARY CASE, which is where a reader that only fills on demand goes wrong. Two
# requests on id 1 both crossing the wire before the server answers, with NO held window: the
# shim must still see `req, req, resp`. It saw `req, resp, req` while `_announce` ran off
# `_fill`, because the main loop answers the first request before asking for more input —
# so a live duplicate was logged as post-response reuse (review, PR #100). E15 previously
# covered only the initialize window, where `_measure_pipelining` happens to drain
# continuously, which is exactly why this was invisible.
_recs_dup = run([legacy_init(1), {"jsonrpc": "2.0", "id": 1, "method": "ping"}])[2]
_found_dup = PIPE.id_findings(PIPE.id_timeline(_recs_dup))
check("a live duplicate OUTSIDE the initialize window is still a live duplicate",
      _found_dup["live_duplicates"] == [("n", 1)]
      and _found_dup["post_response_reuse"] == [], _found_dup)

# A RESPONSE THAT NEVER DEPARTED IS NOT AN ANSWER. C3-1 established that agy is already gone
# when the graceful closure is written, so the write raises and nothing reaches the client;
# logging the answer first recorded one anyway (review, PR #100).
_gone = run([legacy_init(1),
             {"jsonrpc": "2.0", "id": 2, "method": "subscriptions/listen",
              "params": {**MODERN_META,
                         "notifications": {"toolsListChanged": True}}}],
            close_reader=True)[2]
_answered_ids = [r for r in _gone if r.get("event") == "response_id"]
_broken = [r for r in _gone if r.get("event") == "terminator"
           and r.get("reason") == "broken_pipe"]
check("a departed reader means the closure is NOT recorded as answered",
      not _broken or all(tuple(r["id_key"]) != ("n", 2) for r in _answered_ids),
      (_answered_ids, _broken))

# MALFORMED IDS STAY OUT OF THE TIMELINE. A list id is unhashable and crashed the reader; a
# `true` followed by `1` manufactured a reuse, because Python aliases them and JSON-RPC does
# not (review, PR #100).
def _raw_bytes(payload: bytes):
    """Write these exact bytes to the shim and return its log."""
    tmp = tempfile.mkdtemp(prefix="verify-badid-")
    log = os.path.join(tmp, "probe.jsonl")
    p = subprocess.Popen([sys.executable, SHIM], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         env=dict(os.environ, PROBE_MCP_LOG=log, PROBE_MCP_MODE="dual"))
    try:
        p.communicate(payload, timeout=DEADLINE)
    except subprocess.TimeoutExpired:
        p.kill()
    return [json.loads(ln) for ln in open(log) if ln.strip()]


def _raw_drive(msgs):
    """Write these messages and return the log. `run()` cannot: it keys pending ids in a set,
    so a list id raises `unhashable` in the DRIVER before the shim ever sees it."""
    return _raw_bytes(b"".join((json.dumps(m) + "\n").encode() for m in msgs))


_bad = _raw_drive([{"jsonrpc": "2.0", "id": [], "method": "ping"},
                   {"jsonrpc": "2.0", "id": True, "method": "ping"},
                   {"jsonrpc": "2.0", "id": 1, "method": "ping"}])
_bad_tl = PIPE.id_timeline(_bad)
check("a list id never enters the timeline",
      PIPE.request_ids(_bad_tl) == [("n", 1)], PIPE.request_ids(_bad_tl))
check("...so the reader does not crash on it, and `true` does not alias `1` into a reuse",
      PIPE.id_findings(_bad_tl)["post_response_reuse"] == []
      and PIPE.id_findings(_bad_tl)["live_duplicates"] == [], PIPE.id_findings(_bad_tl))
check("...and the malformed traffic is still reported, as its own event",
      len([r for r in _bad if r.get("event") == "request_id_malformed"]) == 2,
      [r for r in _bad if r.get("event") == "request_id_malformed"])
# A VALID ID INSIDE A MALFORMED ENVELOPE. A JSON-RPC 1.0 frame carries a perfectly good id, so
# validating only the id let it in; the proxy terminates on that frame as MALFORMED and never
# reaches any id rule, so a later valid request on the same id read as legal reuse. Worse, the
# shim ANSWERS the bad frame — as a conformant server should — and recording that response
# marked the id answered with no arrival, manufacturing a repeat out of one request (review,
# PR #100).
_v1 = _raw_drive([{"id": 1, "method": "ping", "params": {}},          # no "jsonrpc": "2.0"
                  {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}])
_v1_tl = PIPE.id_timeline(_v1)
check("a JSON-RPC 1.0 frame is refused admission and marks the run terminal",
      _v1_tl[0] == ("bad", None)
      and PIPE.id_findings(_v1_tl)["truncated"] is True, _v1_tl)
check("...and nothing after it is classified, so no reuse is manufactured",
      PIPE.id_findings(_v1_tl)["post_response_reuse"] == []
      and PIPE.id_findings(_v1_tl)["requests"] == 0, PIPE.id_findings(_v1_tl))
check("...and the response to the refused frame is not recorded as an answer",
      [k for k, _ in PIPE.id_timeline(
          _raw_drive([{"id": 1, "method": "ping", "params": {}}]))] == ["bad"],
      PIPE.id_timeline(_raw_drive([{"id": 1, "method": "ping", "params": {}}])))
# ALL FOUR SHAPES, not just the request branch. The proxy fails on any envelope it cannot
# classify, so each of those is terminal in the timeline — and a malformed NOTIFICATION has no
# id at all, which is how it slipped past a request-shaped check (review, PR #100).
for _env in ({"jsonrpc": "1.0", "id": 1, "method": "ping"},
             {"jsonrpc": "2.0", "id": 1, "method": ""},
             {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": []},
             {"jsonrpc": "2.0", "id": 1, "method": "ping", "result": {}},
             {"jsonrpc": "2.0", "id": 1, "method": "ping"},
             {"jsonrpc": "2.0", "method": "notifications/initialized"},
             {"jsonrpc": "1.0", "method": "notifications/initialized"},
             {"jsonrpc": "2.0", "method": "x", "params": []},
             {"jsonrpc": "2.0", "id": 1, "result": {}},
             {"jsonrpc": "2.0", "id": 1, "result": []},
             {"jsonrpc": "2.0", "id": 1, "error": {"code": 1, "message": "m"}},
             {"jsonrpc": "2.0", "id": 1, "error": {"code": "1", "message": "m"}},
             {"jsonrpc": "2.0", "id": 1}):
    _proxy = classify_envelope(_env)
    _want = None if not isinstance(_proxy, str) else _proxy
    check(f"the shim's envelope verdict matches the proxy's for {_env}",
          SHIM_MOD._envelope_shape(_env) == _want, (SHIM_MOD._envelope_shape(_env), _proxy))

# A FAILED READ IS NOT A CLOSED STDIN. `_reader` swallowed every OSError into `chunk = b""`,
# so the main loop logged `stdin_eof` and a clean terminator — C3-1 would have published an
# instrument failure as "this CLI shut the server down cleanly" (review, PR #100). Driven with
# a WRITE-only fd as stdin, so `os.read` really raises.
_rfd, _wfd = os.pipe()
_failtmp = tempfile.mkdtemp(prefix="verify-readerfail-")
_faillog = os.path.join(_failtmp, "probe.jsonl")
_fp = subprocess.Popen([sys.executable, SHIM], stdin=_wfd, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL,
                       env=dict(os.environ, PROBE_MCP_LOG=_faillog, PROBE_MCP_MODE="dual"))
os.close(_rfd)
os.close(_wfd)
_fp.wait(timeout=DEADLINE)
_frecs = [json.loads(ln) for ln in open(_faillog) if ln.strip()]
check("a failed read is logged as a reader error, not silence",
      any(r["event"] == "reader_error" for r in _frecs), [r["event"] for r in _frecs])
check("...and the terminator says so instead of claiming a clean stdin close",
      [r.get("reason") for r in _frecs if r["event"] == "terminator"] == ["reader_failed"],
      _frecs)
# THE EXIT STATUS TOO. A log line the caller has to go and read is not a report: everything
# driving this shim checks the status first, so logging the failure and exiting 0 leaves the
# instrument saying "clean" where most callers look (review, PR #100).
check("...and the shim exits non-zero", _fp.returncode != 0, _fp.returncode)
check("...and a genuine stdin close still exits 0 with a clean terminator",
      [r.get("reason") for r in run([legacy_init(1)])[2]
       if r["event"] == "terminator"] == ["stdin_eof"])
_okrun = subprocess.run([sys.executable, SHIM], input=b"", capture_output=True,
                        env=dict(os.environ, PROBE_MCP_MODE="dual"), timeout=DEADLINE)
check("...confirmed: a clean EOF is exit 0", _okrun.returncode == 0, _okrun.returncode)
# The PROBE carries the instrument's health beside the reading, and FAILS THE RUN on it.
_answered = {"cli": "a", "connected": True, "era": {"era": "legacy", "version": "2025-11-25"},
             "pipelining": {"pipelined": False, "count": 0, "methods": []},
             "id_anomalies": [], "reader_failed": False}
check("a fully answered run exits 0", PIPE.run_failed([_answered]) is False)
check("...a dead reader fails it",
      PIPE.run_failed([{**_answered, "reader_failed": True}]) is True)
check("...malformed traffic fails it",
      PIPE.run_failed([{**_answered, "id_anomalies": [{"event": "request_id_malformed"}]}])
      is True)
check("...and the shim's own log is what tells the probe",
      any(r["event"] in ("reader_error", "reader_failed") for r in _frecs), _frecs)

# BATCHES AND MALFORMED NOTIFICATIONS are proxy-terminal too, and produced no marker at all —
# a batch is neither request-shaped nor unparseable, and a malformed notification has no id.
_batch_tl = PIPE.id_timeline(_raw_drive([[{"jsonrpc": "2.0", "id": 1, "method": "ping"}],
                                         {"jsonrpc": "2.0", "id": 2, "method": "ping"}]))
check("a JSON-RPC batch is a terminal marker",
      _batch_tl and _batch_tl[0] == ("bad", None)
      and PIPE.id_findings(_batch_tl)["truncated"] is True, _batch_tl)
_note_tl = PIPE.id_timeline(_raw_drive([{"jsonrpc": "1.0", "method": "notifications/x"},
                                        {"jsonrpc": "2.0", "id": 2, "method": "ping"}]))
check("a malformed NOTIFICATION is a terminal marker, id or no id",
      _note_tl and _note_tl[0] == ("bad", None)
      and PIPE.id_findings(_note_tl)["truncated"] is True, _note_tl)
check("...but a well-formed notification is neither a marker nor a finding",
      PIPE.id_timeline(_raw_drive([{"jsonrpc": "2.0", "method": "notifications/initialized"},
                                   {"jsonrpc": "2.0", "id": 2, "method": "ping"}]))[0]
      == ("req", ("n", 2)))
check("...and unparseable input is still terminal",
      PIPE.id_timeline(_raw_bytes(b"not json\n"))[:1] == [("bad", None)])

check("the shim's RequestId rule matches the proxy's",
      all(SHIM_MOD._valid_request_id(v) == P_valid(v)
          for v in (1, 1.0, 0, -0.0, "1", "", True, False, None, 2**70, -5, 1.5)),
      [(v, SHIM_MOD._valid_request_id(v), P_valid(v))
       for v in (1, 1.0, 0, -0.0, "1", "", True, False, None, 2**70, -5, 1.5)])

print()
print("E16. http_mcp_server.py: the two REMOTE transports, and the header question §9 "
      "probe #1 could not answer without a server")

# WHY THIS FIXTURE IS DRIVEN HERE and not left to the live probe: the live probe costs an API
# call and needs `claude` installed, so it runs when someone asks (`tools/probe_remote_mcp.py`).
# This runs in every suite, offline, and is what keeps the fixture trustworthy in between —
# the C3-1 lesson, where an instrument's own defect changed a published measurement (§9).
#
# IT NEEDS TO BIND A TCP SOCKET, which §4 warns about: an instrument that needs a privilege is
# an instrument some reviewer cannot run, and a sandbox denying `bind()` is exactly where the
# proxy verifier's loopback case had to be redesigned. This one cannot be redesigned out of it
# — HTTP is the thing under test — so the obligation is different: it must FAIL BY NAME, with
# the fixture's own stderr attached, never hang and never crash the suite (review, PR #106).


class _Remote:
    """The fixture under a context manager, because a leaked HTTP server is a leaked port.

    Reaped on every exit path, INCLUDING a failed `__enter__` — which never reaches `__exit__`,
    so a process started there and abandoned is leaked with no one to notice. §4's incident was
    a test tool leaving processes behind; a server holding a port is the version of that which
    fails the next run rather than this one.
    """

    def __enter__(self):
        self.receipts = tempfile.mkdtemp(prefix="http-mcp-")
        self.path = os.path.join(self.receipts, "receipts.jsonl")
        self.info, self.failure = {}, ""
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(FIXTURES, "http_mcp_server.py")],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            # Cleared then set, for the reason `run()` gives: an ambient export must not be
            # able to change what these checks measure. An allowlisted origin in the
            # environment would silently disarm the Origin checks below.
            env={**os.environ, "HTTP_MCP_RECEIPTS": self.path,
                 "HTTP_MCP_ALLOWED_ORIGINS": ""})
        q = queue.Queue()
        threading.Thread(target=_pump, args=(self.proc.stdout, q), daemon=True).start()
        line = _readline(q, time.monotonic() + DEADLINE)
        if not line:
            self.proc.kill()
            self.proc.wait(timeout=DEADLINE)
            err = (self.proc.stderr.read() or b"").decode(errors="replace").strip()
            shutil.rmtree(self.receipts, ignore_errors=True)
            # THE LAST LINE, not a tail of bytes. A traceback's final line is the exception,
            # and `check()` truncates the detail to 300 characters — so a 400-byte tail gets
            # cut in the printer and shows the traceback's MIDDLE, which names a frame instead
            # of a cause. The whole reason for keeping stderr is to say "Operation not
            # permitted" out loud; a diagnostic that survives to the terminal in a useless
            # form is the same defect as no diagnostic (review, PR #106).
            lines = [x for x in err.splitlines() if x.strip()]
            self.failure = ("the fixture announced no port within the deadline"
                            + (f"; it died with: {lines[-1].strip()}" if lines
                               else ", and said nothing on stderr either"))
        else:
            try:
                self.info = json.loads(line)
            except ValueError:
                self.failure = f"the port announcement was not JSON: {line[:200]!r}"
        return self

    def __exit__(self, *exc):
        self.proc.kill()
        self.proc.wait(timeout=DEADLINE)
        shutil.rmtree(self.receipts, ignore_errors=True)

    @property
    def up(self) -> bool:
        return bool(self.info.get("port"))

    def url(self, path=""):
        return f"http://127.0.0.1:{self.info['port']}{path}" if path else self.info["streamable"]

    def rpc(self, msg, headers=None, url=None):
        """One JSON-RPC POST. Returns (status, headers, body) — or the HTTP error's status."""
        req = urllib.request.Request(
            url or self.info["streamable"], data=json.dumps(msg).encode(),
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=DEADLINE) as r:
                body = r.read()
                return r.status, dict(r.headers), (json.loads(body) if body else None)
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), None

    def get(self, path, headers=None):
        req = urllib.request.Request(self.url(path), headers=headers or {}, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=DEADLINE) as r:
                return r.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def rows(self, kind=None):
        if not os.path.exists(self.path):
            return []
        out = [json.loads(x) for x in open(self.path, encoding="utf-8") if x.strip()]
        return [r for r in out if kind is None or r["kind"] == kind]

    def promise_a_body(self, headers: str, declared: int = 50_000_000, bound=5.0):
        """Send a request that DECLARES a large body and never sends it. Returns
        (status_line, elapsed, closed) — with `status_line` empty if nothing came back.

        HAND-WRITTEN OVER A RAW SOCKET because `urllib` cannot express this: it writes the
        body it declares. The case is the whole question of ordering — a server that validates
        `Origin` only after reading the body will sit in `rfile.read(declared)` waiting for
        bytes that never arrive, so the discriminator is whether an answer comes back AT ALL
        within a bound, not what the answer says (review, PR #106).
        """
        sock = socket.create_connection(("127.0.0.1", self.info["port"]), timeout=bound)
        try:
            started = time.monotonic()
            sock.sendall(headers.encode())          # headers and the blank line; NO body
            got, closed = b"", False
            try:
                while b"\r\n\r\n" not in got:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    got += chunk
                # A refusal must also END the connection: an unread body has desynchronized
                # it, so anything after this response would be read as a new request.
                closed = sock.recv(4096) == b""
            except (TimeoutError, OSError):
                pass
            elapsed = time.monotonic() - started
            return got.split(b"\r\n", 1)[0].decode(errors="replace"), elapsed, closed
        finally:
            sock.close()


def dig(obj, *path, default=None):
    """Walk `path` through nested dicts/lists, returning `default` at the first miss.

    THE CHECKS BELOW ASSERT THINGS ABOUT A POSSIBLY-BROKEN FIXTURE, so indexing its replies
    directly is where a defect turns into a traceback instead of a red check — which is the
    exact failure the startup path was fixed for, one level in. It surfaced as a mutation that
    denied every request: `initialize` returned no body, `_init["result"]` raised, and the
    suite reported ONE failure where the block should have reported several (review, PR #106).
    A verifier that crashes on the defect it exists to detect reports less than one that says
    nothing.
    """
    cur = obj
    for key in path:
        try:
            cur = cur[key]
        except (KeyError, IndexError, TypeError):
            return default
    return cur


with _Remote() as _rm:
    # THE INSTRUMENT'S OWN LIVENESS IS CHECK ONE and everything else is inside its `if`. Not a
    # skip — this reddens — because a suite that quietly ran fewer checks reports the same
    # "ALL PASS" as one that ran them all. (The third result state that would let this be an
    # honest SKIP is a backlog item, not something to invent here.)
    check("the fixture announces the port it actually bound",
          _rm.up and _rm.info.get("streamable", "").endswith("/mcp"),
          _rm.failure or _rm.info)

    if _rm.up:
        # THE WITNESS SAYS SOMETHING POSITIVE BEFORE ITS SILENCE IS EVIDENCE. Every header
        # check below reads "no such header in any row" as a finding; an unwired receipts
        # file, a server that never started, and a server that received nothing all produce
        # exactly that. The startup row separates them (§4).
        check("...and the receipts witness records its own startup, so silence is readable",
              [r["kind"] for r in _rm.rows()][:1] == ["listening"], _rm.rows())

        _s, _h, _init = _rm.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                 "params": {"protocolVersion": "2025-11-25",
                                            "capabilities": {}}},
                                {"Authorization": "Bearer FIXTURE_SENTINEL"})
        check("streamable HTTP answers `initialize` in the POST's own response",
              _s == 200 and dig(_init, "result", "protocolVersion") == "2025-11-25", (_s, _init))
        # NO SESSION IS ADVERTISED, because none is kept. Issuing an id nothing checks is
        # worse than issuing none: it tells a client the id is honoured, and this fixture
        # answered a wrong id exactly like a right one (review, PR #106).
        check("...and advertises NO session id, because it keeps no session state",
              "Mcp-Session-Id" not in _h, sorted(_h))

        _s, _, _tl = _rm.rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        check("...and serves the SAME tools as the stdio fixture, because it imports them",
              [t.get("name") for t in (dig(_tl, "result", "tools", default=[]) or [])]
              == [t["name"] for t in ECHO_MOD.TOOLS], _tl)

        _s, _, _call = _rm.rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                "params": {"name": "echo", "arguments": {"text": "over http"}}})
        check("...and calls a tool",
              dig(_call, "result", "content", 0, "text") == "over http", _call)

        # THE QUESTION THE FIXTURE EXISTS FOR, answered from the only place it is answerable:
        # what the server RECEIVED, not what the client believes it sent.
        _reqs = _rm.rows("request")
        _auth = [r["headers"].get("authorization") for r in _reqs]
        check("a declared header ARRIVES, with its value intact — §9 probe #1's real question",
              "Bearer FIXTURE_SENTINEL" in _auth, _auth)
        check("...and a request sent without one records its absence, so the check can fail",
              None in _auth, _auth)

        # THE OTHER HALF OF THE WITNESS, and the half a LIVE probe cannot do without. Its
        # "the model called the tool" check reads the model's final text, and the text it
        # looks for was handed to the model in the prompt — so a client that advertised the
        # tools and never invoked one passed by repeating itself (review, PR #106). Only the
        # server can distinguish those, and only if it records the call rather than just the
        # HTTP request carrying it. `id` 3 is the `tools/call` above.
        _rpc = [{k: r.get(k) for k in ("method", "id", "tool")} for r in _rm.rows("rpc")]
        check("the witness records the CALL ITSELF, so 'the tool ran' and 'the model repeated "
              "its prompt' stop looking alike",
              {"method": "tools/call", "id": 3, "tool": "echo"} in _rpc, _rpc)
        # Without this the field could be the constant "echo" and the check above would still
        # pass — an identifier that names everything identifies nothing.
        _named = {r["tool"] for r in _rpc if r["method"] == "tools/list"}
        check("...and a message that is NOT a tool call records no tool name, so the field "
              "tells them apart",
              _named == {None}, _rpc)

        # THE HEADER ROW MUST NAME THE MESSAGE IT CARRIED, or every header question can only be
        # asked of the run as a whole — and one of them is per-message, since `initialize`
        # precedes the negotiation `MCP-Protocol-Version` reports. Correlation by adjacency
        # against the `rpc` rows would be unsound here: this is a threading server (PR #106).
        _carried = [(r.get("rpc"), r["headers"].get("authorization")) for r in _reqs]
        check("a request row names the message it carried, on the same row as its headers",
              ("initialize", "Bearer FIXTURE_SENTINEL") in _carried, _carried)
        check("...and a request carrying a DIFFERENT method records that one, so the field is "
              "not the same word every time",
              ("tools/call", None) in _carried, _carried)

        check("an unroutable method is an error, not a hang",
              dig(_rm.rpc({"jsonrpc": "2.0", "id": 4, "method": "no/such"}),
                  2, "error", "code") == -32601)
        _s, _, _body = _rm.rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
        check("...and a NOTIFICATION is accepted with no answer at all",
              _s == 202 and _body is None, (_s, _body))

        # ORIGIN VALIDATION IS A TRANSPORT-LEVEL MUST and this server is the case it was
        # written for: it listens on loopback while a model runs, so any page the user's
        # browser is on can reach it cross-origin unless the server refuses. A fixture more
        # permissive than the spec teaches the harness that a permissive server is normal.
        check("a cross-origin POST is refused 403 — the transport's DNS-rebinding MUST",
              _rm.rpc({"jsonrpc": "2.0", "id": 5, "method": "ping"},
                      {"Origin": "https://evil.example"})[0] == 403)
        check("...and a cross-origin GET on the SSE endpoint is refused too, not just POST",
              _rm.get(PATH_SSE_T, {"Origin": "https://evil.example"}) == 403)
        # The control: without it, a fixture that refused EVERYTHING would score full marks on
        # both checks above — "rejects everything passes" is the defect §10.9 spends a case on.
        check("...while a request with no Origin at all is served, as a non-browser client",
              _rm.rpc({"jsonrpc": "2.0", "id": 6, "method": "ping"})[0] == 200)

        # THE REFUSAL MUST COST THE REFUSED CALLER MORE THAN IT COSTS THIS SERVER, and that is
        # a property of ORDER, which the three checks above cannot see: they send bodies, so a
        # server reading the body first still answers 403 and passes all of them. A revision
        # that read first — to put the message's method on the receipt row — let a rejected
        # cross-origin caller name a `Content-Length` of its choosing, or pin the handler open
        # by declaring a body and sending none (review, PR #106).
        _line, _secs, _closed = _rm.promise_a_body(
            "POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nOrigin: https://evil.example\r\n"
            "Content-Type: application/json\r\nContent-Length: 50000000\r\n\r\n")
        # THE SOCKET'S DEADLINE IS THE BOUND, so arriving at all is the whole assertion — a
        # server that reads the body first is still blocked in `rfile.read` when that deadline
        # expires, and returns no status line to start with. A separate `elapsed < 4.0` check
        # stood here briefly and was deleted: it could not fail for the reason it named, only
        # for one it did not — the host pausing this process — which is a false failure with no
        # coverage attached (review, PR #106). Elapsed stays as DETAIL, where it explains a red
        # check without being able to cause one.
        check("a cross-origin POST is refused WITHOUT its body — 403 arrives though the "
              "declared 50MB never does",
              _line.startswith("HTTP/1.1 403"), (_line, f"{_secs:.2f}s"))
        check("...and the connection is CLOSED, since an unread body has desynchronized it",
              _closed, (_line, _closed))
        # `_record` on the POST refusal path is what keeps this true; without it the credential
        # question goes unanswered for exactly the requests most worth asking it about.
        #
        # THE VERB IS PART OF THE ASSERTION, and leaving it out made this pass over its own
        # mutation. `do_GET` records before it refuses, by a different line — so a cross-origin
        # GET above put an `evil.example` row in the file, and a check that asked only "is there
        # such a row" was answered by a request it was not about. §4's recurring one, in the
        # check written to close a finding about ordering (F35, caught by its own mutation).
        check("...while the refused POST is still RECORDED, so a credential sent to a rejected "
              "origin is not invisible",
              any(r["method"] == "POST" and r["headers"].get("origin") == "https://evil.example"
                  for r in _rm.rows("request")),
              [(r["method"], r["headers"].get("origin")) for r in _rm.rows("request")])

        # THE BINDING'S OTHER MUST, found by asking what else the Origin argument covered
        # rather than by it being reported: `2025-11-25` requires 400 for a protocol version
        # the server does not support, and the scope note used to file that header under
        # MODERN additions while claiming legacy conformance (review, PR #106).
        check("an unsupported MCP-Protocol-Version is refused 400 — the binding's other MUST",
              _rm.rpc({"jsonrpc": "2.0", "id": 11, "method": "ping"},
                      {"MCP-Protocol-Version": "1999-01-01"})[0] == 400)
        # THE CONTROL, and simultaneously the pin: `_initialize` selects out of this same
        # tuple, so a version list narrower than it would 400 a version the server itself had
        # just negotiated. Driving EVERY member is what makes a later narrowing fail here
        # instead of intermittently against a real client.
        _served = {v: _rm.rpc({"jsonrpc": "2.0", "id": 12, "method": "ping"},
                              {"MCP-Protocol-Version": v})[0] for v in ECHO_MOD.LEGACY_VERSIONS}
        check("...while EVERY version `initialize` can negotiate is served, so the set it is "
              "checked against cannot narrow away from the set it is chosen from",
              bool(_served) and set(_served.values()) == {200}, _served)
        check("...and an absent header is served too, because `initialize` itself carries none",
              _rm.rpc({"jsonrpc": "2.0", "id": 13, "method": "ping"})[0] == 200)

        # EXACTLY THE CONFIGURED PATH. `/definitely-not-mcp` used to be served, so a config
        # whose URL had been mangled still reached a working server and the probe that was
        # meant to prove the URL correct proved nothing about it (review, PR #106).
        check("a POST to a path that is not the endpoint is 404, not quietly served",
              _rm.rpc({"jsonrpc": "2.0", "id": 7, "method": "ping"},
                      url=_rm.url("/definitely-not-mcp"))[0] == 404)
        check("...and a prefix of the endpoint is not the endpoint either",
              _rm.rpc({"jsonrpc": "2.0", "id": 8, "method": "ping"},
                      url=_rm.url("/mcp-anything"))[0] == 404)

        # The streamable endpoint declines a server->client stream, which the transport
        # permits explicitly. Asserted rather than assumed: a fixture that opened an idle
        # stream would add a shutdown path nothing here drives.
        check("GET on the streamable endpoint is refused 405, as the transport allows",
              _rm.get(PATH_STREAMABLE_T) == 405)

# THE LEGACY SSE TRANSPORT IS A DIFFERENT PROTOCOL, not a different spelling, and this is the
# check that says so: the reply to a POST does NOT come back in that POST's response. A fixture
# that answered in place would satisfy a client that had never implemented SSE.
with _Remote() as _rm2:
    check("the SSE fixture starts too", _rm2.up, _rm2.failure)
    if _rm2.up:
        _events, _endpoint = [], []

        def _read_stream(rm=None, events=None, endpoint=None):
            req = urllib.request.Request(rm.info["sse"], headers={"Accept": "text/event-stream"})
            name = None
            with urllib.request.urlopen(req, timeout=DEADLINE) as r:
                for raw in r:
                    line = raw.decode().rstrip("\n")
                    if line.startswith("event: "):
                        name = line[7:]
                    elif line.startswith("data: "):
                        if name == "endpoint":
                            endpoint.append(line[6:])
                            name = None
                        else:
                            events.append(json.loads(line[6:]))
                            return

        _t = threading.Thread(target=_read_stream,
                              kwargs={"rm": _rm2, "events": _events, "endpoint": _endpoint},
                              daemon=True)
        _t.start()
        _deadline = time.monotonic() + DEADLINE
        while not _endpoint and time.monotonic() < _deadline:
            time.sleep(0.05)
        check("the SSE stream's FIRST event names the endpoint to POST to",
              bool(_endpoint) and _endpoint[0].startswith("/messages?sessionId="), _endpoint)
        if _endpoint:
            _s, _, _body = _rm2.rpc({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                     "params": {"name": "add", "arguments": {"a": 2, "b": 3}}},
                                    url=_rm2.url(_endpoint[0]))
            check("...a POST there is answered 202, with the reply NOT in its response",
                  _s == 202 and _body is None, (_s, _body))
            _t.join(timeout=DEADLINE)
            check("...and the reply arrives on the stream the client is still holding",
                  dig(_events, 0, "result", "content", 0, "text") == "5", _events)
        # A POST naming a session nobody opened has nowhere to deliver a reply, and 202 would
        # strand it silently.
        check("...while a POST for an unopened session is 404, not a silently stranded 202",
              _rm2.rpc({"jsonrpc": "2.0", "id": 10, "method": "ping"},
                       url=_rm2.url("/messages?sessionId=nosuch"))[0] == 404)
        check("...and a prefix of the SSE path is not the SSE path",
              _rm2.get("/sse-anything") == 405)

print()
print("E17. mutate_mcp.py's own readers, driven without paying for a 63-minute run")

# WHY THE RUNNER IS UNDER TEST HERE. It is the instrument every "N/N caught" claim rests on,
# and until now nothing executed its suite-reading code except a full run — so a change to the
# record those readers share broke `run()` for BOTH verifier suites, shipped, and was found by
# hand-driving the tool rather than by any check (review, PR #106). An instrument exercised
# only by its slowest path is one whose breakage is discovered late by construction; §4 makes
# this point about probes and it is no weaker about the thing that scores them.
#
# `mutate_mcp` is importable from here — same directory — so these drive the real functions.
# A check that restated their logic could not disagree with them, which is the whole failure
# mode being closed (§4's duplicated-rule rule, satisfied by import).
import mutate_mcp as MUT  # noqa: E402 — a section-local import, beside the checks that use it


def survives(fn, *a, **k):
    """`fn`'s value, or the exception it raised — never the exception itself.

    `dig` one level out, and for the same reason. The defect these checks exist to catch
    RAISES rather than returning something wrong: the mis-unpack was a `ValueError`, and a bad
    parser is a `re.error`. Evaluated inside a `check(...)` argument either of those takes the
    verifier down and reports nothing at all, which is strictly less than a red line saying
    which reader broke.
    """
    try:
        return fn(*a, **k)
    except Exception as exc:                                   # noqa: BLE001 — any of them
        return exc


def field(suite, name):
    """One named field of a suite record, or None where the record has no such field.

    `getattr` with a default rather than an attribute access, so a record that is a bare tuple
    again — the shape the named one replaced — reads as a MISSING field and reddens a check,
    instead of raising `AttributeError` out of a `check(...)` argument.
    """
    return getattr(MUT._SUITES[suite], name, None)


_ARGVS = {s: survives(MUT.command_for, pathlib.Path("/x"), s) for s in MUT._SUITES}
check("every declared suite yields a runnable command, which is the line that broke",
      bool(_ARGVS) and all(isinstance(c, list) and len(c) > 1
                           and c[1:] == list(field(s, "argv") or [])
                           for s, c in _ARGVS.items()), _ARGVS)
# The three fields have three different readers, and a record one of them cannot read is the
# defect above — so name them rather than trusting the record's arity. Parenthesized: as
# `A and B or C` this is true by precedence whenever a labels parser is a string, which is
# every real entry, and the interesting term would never be reached (§4).
check("...and each names a FAILED-check parser, with a full-label parser or an explicit None",
      bool(MUT._SUITES) and all(          # the structural clause: `all` over {} is true
          isinstance(field(s, "failed"), str)
          and (field(s, "labels") is None or isinstance(field(s, "labels"), str))
          for s in MUT._SUITES), MUT._SUITES)
# `None` is legitimate for the selftest, which prints section headings rather than a line per
# arm. It is a SILENT DISARM for any suite that does print one, because the arm guard simply
# skips a suite declaring none. Both of these print a line per check — this file is one of
# them, four lines up.
check("...and the two suites that print a line per check both declare one, since a `None` "
      "there disarms the arm guard without saying so",
      all(isinstance(field(s, "labels"), str) for s in ("fixtures", "proxy")), MUT._SUITES)

# THE PARSERS, ON THIS SUITE'S REAL OUTPUT FORMAT — which is the format printed a few lines
# above, so a change to `check()` moves the sample and the parser together.
_sample = "  ok   a passing label\n  FAIL a failing label  <- detail here\nALL PASS\n"
check("the FAILED parser reads a red check out of this verifier's own format",
      survives(MUT.failed_checks, "fixtures", _sample) == ["a failing label"], _sample)
check("...and the label parser reads BOTH lines, since arms name passing checks too",
      survives(re.findall, MUT._ALL_LABELS, _sample, re.MULTILINE)
      == ["a passing label", "a failing label"], _sample)
# THE POSITIVE FACT BEHIND THE FAIL-CLOSED BRANCH. The arm guard used to skip itself when the
# parse came back empty, which cleared every arm at once if a verifier's output format moved.
# Making it refuse instead is only worth something if an empty parse is REACHABLE — a parser
# that matched every line would turn the new branch into unreachable code that reads like a
# safeguard (review, PR #106).
check("...and finds nothing in output that is not a check, so the empty parse it now refuses "
      "on is a state that can really occur",
      survives(re.findall, MUT._ALL_LABELS, "E1. a section heading\n\nALL PASS\n",
               re.MULTILINE) == [],
      "otherwise the guard's fail-closed branch is unreachable")

# THE ANCHOR VALIDATOR, driven on a synthetic tree rather than on this one. A stale anchor is
# an entry that cannot be applied, and the runner used to discover that only on reaching it —
# an hour in, for a list this long. Driven here on three trees whose right answers are known:
# present once, absent, and present twice. NOT pointed at the real `harness/`, deliberately:
# under an applied mutation a target legitimately no longer contains its own anchor, so a check
# that read the live tree would go red for whichever mutation was in flight.
_anchor_tmp = tempfile.mkdtemp(prefix="verify-anchors-")
try:
    _synth = pathlib.Path(_anchor_tmp) / "t.py"
    _entry = [("X1-example", "t.py", "needle", "haystack", "some arm")]
    _synth.write_text("before\nneedle\nafter\n")
    check("an anchor present exactly once is not reported stale",
          MUT.stale_anchors(_anchor_tmp, _entry) == [], MUT.stale_anchors(_anchor_tmp, _entry))
    # BOTH WAYS OF NOT BEING APPLICABLE. Zero occurrences is the rewritten line; two is the
    # anchor that would match the wrong site, which §4 already records as having injected an
    # `IndentationError` reported as "failed, but NOT via" rather than as a defect found.
    _synth.write_text("before\nafter\n")
    check("...an anchor whose text no longer exists is reported, with its count",
          MUT.stale_anchors(_anchor_tmp, _entry) == [("X1-example", "t.py", 0)],
          MUT.stale_anchors(_anchor_tmp, _entry))
    _synth.write_text("needle\nneedle\n")
    check("...and so is one that matches twice, which would mutate the wrong site",
          MUT.stale_anchors(_anchor_tmp, _entry) == [("X1-example", "t.py", 2)],
          MUT.stale_anchors(_anchor_tmp, _entry))
    # A TUPLE `find` IS TWO ANCHORS, and a defect defended in two places needs both removed —
    # so a validator reading only the first would clear an entry that cannot be applied.
    _pair = [("X2-pair", "t.py", ("needle", "gone"), ("a", "b"), "some arm")]
    check("...and a two-part anchor is checked in both parts, not just the first",
          MUT.stale_anchors(_anchor_tmp, _pair) == [("X2-pair", "t.py", 2),
                                                    ("X2-pair", "t.py", 0)],
          MUT.stale_anchors(_anchor_tmp, _pair))
finally:
    shutil.rmtree(_anchor_tmp, ignore_errors=True)

# THE VERDICT, ON SYNTHETIC RUNS. It used to be an `elif` chain inside `main()`, reachable only
# by paying for a suite — so the branch nobody arranged was the branch nobody checked. Every
# case below is a record the chain would have had to be given a real hung or passing suite to
# produce.


def _outcome(rc, out, wall=1.0, cpu=1.0):
    return MUT.Outcome(returncode=rc, output=out, wall=wall, cpu=cpu)


check("a suite that goes red on the named arm is CAUGHT",
      MUT.verdict(_outcome(1, "FAIL the arm  <- why"), "the arm", ["the arm"]) == MUT.CAUGHT,
      "the one verdict that counts towards a coverage claim")
check("...one that goes red on something else is NOT the same answer",
      MUT.verdict(_outcome(1, "x"), "the arm", ["another check"]) == MUT.NOT_VIA,
      "'some arm failed' is a different claim from 'the arm that names this defect failed', "
      "and collapsing the two is how a mutation that has come unhooked reads as coverage")
check("...one that still passes with the defect present is MISSED",
      MUT.verdict(_outcome(0, "ALL PASS"), "the arm", []) == MUT.MISSED,
      "an arm nothing can break is decorative, which is the whole point of the suite")
# THE ORDERING, WHICH IS NOT A TIE-BREAK. A timed-out run carries rc 124 and no parsable
# output, so the MISSED and NOT-via branches would both answer it if asked first — and NOT-via
# is what they would say, over an empty list of failures, about a suite that never reported.
check("...and a hung one is TIMEOUT rather than whatever the later branches would say of it",
      MUT.verdict(_outcome(124, MUT._TIMEOUT_OUTPUT), "the arm", []) == MUT.TIMEOUT,
      "no arm got to report, which is not the same fact as every arm passing")
_caught_line = MUT.result_line("M1", "proxy", "the arm", _outcome(1, "x", 42.0, 5.0), ["the arm"])
check("the printed line carries the verdict, the arm and BOTH clocks",
      ("CAUGHT" in _caught_line and "the arm" in _caught_line
       and "42.0s wall" in _caught_line and "5.0s cpu" in _caught_line), _caught_line)
# The line and the verdict are two readers of one run, and they used to be two `elif` chains
# over the same conditions. A line that said CAUGHT while the count said otherwise would make
# the exit status disagree with the output — §4's trustworthy-predicate rule, one level up.
check("...and it says MISSED exactly when the verdict does",
      "MISSED" in MUT.result_line("M1", "proxy", "a", _outcome(0, "ALL PASS"), [])
      and "MISSED" not in _caught_line, _caught_line)
# A SWEEP THAT DID NOT FINISH IS A LEAK, AND HAS TO REACH THE OUTPUT. Whatever is still alive
# was launched out of a tree about to be deleted, and the next mutation to draw that tree runs
# beside it — a fact that reaching only a return value nobody prints is a fact nobody has.
_stuck_line = MUT.result_line("M1", "proxy", "a",
                              MUT.Outcome(124, MUT._TIMEOUT_OUTPUT, 9.0, 1.0, (4242, 4243)), [])
check("...and a timeout whose sweep left something behind names the pids in its line",
      "SURVIVED" in _stuck_line and "4242" in _stuck_line and "4243" in _stuck_line
      and "SURVIVED" not in MUT.result_line("M1", "proxy", "a",
                                            _outcome(124, MUT._TIMEOUT_OUTPUT), []),
      _stuck_line)

# THE ARGUMENT THE PARALLELISM IS ASKED FOR WITH. A typo'd `--job 8` that silently ran serially
# would look exactly like a machine that did not speed up, which is why an unknown argument is
# refused rather than ignored.
check("`--jobs` is 1 unless asked for, in either spelling",
      (survives(MUT.parse_jobs, []) == 1 and survives(MUT.parse_jobs, ["--jobs", "8"]) == 8
       and survives(MUT.parse_jobs, ["--jobs=8"]) == 8), "the only argument this tool takes")
check("...and every way of asking for it wrongly is refused rather than rounded to 1",
      all(isinstance(survives(MUT.parse_jobs, a), ValueError)
          for a in (["--jobs"], ["--jobs", "0"], ["--jobs", "x"], ["--jobs", "-2"],
                    ["-j", "8"], ["--job", "8"])),
      [survives(MUT.parse_jobs, a) for a in (["--jobs"], ["--jobs", "0"], ["--jobs", "x"],
                                             ["--jobs", "-2"], ["-j", "8"], ["--job", "8"])])

# THE SLOWEST-MUTATION WARNING, which §4 reads as the only notice before a defect that loops
# becomes a defect that hangs. It has to rank on CPU: under `--jobs N` the loudest WALL figure
# names whichever mutation was unluckiest with the scheduler, and the proxy suite spends ~40 of
# its ~43 seconds waiting, so wall time there is a statement about sleep.
_recs = [MUT.Record("slow-wall", "M", MUT.CAUGHT, "", wall=90.0, cpu=2.0),
         MUT.Record("slow-cpu", "M", MUT.CAUGHT, "", wall=10.0, cpu=40.0),
         MUT.Record("never-ran", "M", MUT.UNAPPLIED, "", wall=0.0, cpu=0.0)]
check("the slowest mutation is the one that spent the most CPU, not the most wall clock",
      MUT.slowest(_recs).mid == "slow-cpu", [(r.mid, r.wall, r.cpu) for r in _recs])
check("...and a mutation that never ran is not ranked at all, nor mistaken for 'nothing ran'",
      MUT.slowest([_recs[2]]) is None and MUT.slowest([]) is None,
      "0.0s would read as a suite that finished instantly rather than one that never started")

# APPLY / RUN / REVERT, with the run itself faked. The revert moved into a `finally` when the
# trees became shared property: serially, an exception between the two writes ended the run
# anyway, but a tree handed back to the pool still mutated would make every later result a fact
# about two defects at once. Driven by an exception because that is the only way to reach it.
_edit_tmp = tempfile.mkdtemp(prefix="verify-apply-")
try:
    _target = pathlib.Path(_edit_tmp) / "t.py"
    _pristine = "before\nneedle\nafter\n"
    _target.write_text(_pristine)
    _entry = ("X3-example", "t.py", "needle", "haystack", "some arm")
    _real_run, _calls = MUT.run, []

    def _fake_run(cwd, suite):
        _calls.append((str(cwd), suite, (pathlib.Path(cwd) / "t.py").read_text()))
        return _outcome(1, "FAIL some arm  <- why", 3.0, 2.0)

    def _raising_run(cwd, suite):
        raise RuntimeError("the suite could not be started")

    try:
        MUT.run = _fake_run
        _rec = MUT.apply_and_run(pathlib.Path(_edit_tmp), _entry, "fixtures", "F")
        check("the file the suite sees is the MUTATED one, and it is put back afterwards",
              (len(_calls) == 1 and "haystack" in _calls[0][2]
               and _target.read_text() == _pristine),
              (_calls, _target.read_text()))
        check("...and the record carries the verdict, the class and the run's two clocks",
              (_rec.verdict == MUT.CAUGHT and _rec.kind == "F"
               and (_rec.wall, _rec.cpu) == (3.0, 2.0)), _rec)
        MUT.run = _raising_run
        _raised = survives(MUT.apply_and_run, pathlib.Path(_edit_tmp), _entry, "fixtures", "F")
        check("...and it is put back even when the suite could not be run at all, since a "
              "mutated tree goes back into the pool for the next mutation to draw",
              isinstance(_raised, RuntimeError) and _target.read_text() == _pristine,
              (_raised, _target.read_text()))
    finally:
        MUT.run = _real_run
    # Both anchor faults, from the side that runs them. `stale_anchors` refuses the whole list
    # up front; these are what a tree left mutated by a crashed worker would produce, and they
    # must not be reported as a suite that ran and passed.
    #
    # `survives` AND AN `isinstance` AHEAD OF EVERY FIELD, because the interesting failure here
    # RAISES. A guard that stopped refusing would let the call reach the suite runner, which
    # would look for a venv this synthetic tree does not have — and an `OSError` out of a
    # `check(...)` argument ends the verifier without naming the property that went unproven.
    _target.write_text("nothing to find here\n")
    _gone = survives(MUT.apply_and_run, pathlib.Path(_edit_tmp), _entry, "fixtures", "F")
    check("an anchor that no longer matches is UNAPPLIED, and says the suite did not run",
          (isinstance(_gone, MUT.Record) and _gone.verdict == MUT.UNAPPLIED
           and "STALE ANCHOR" in _gone.line and "not run" in _gone.line), _gone)
    _target.write_text("needle\nneedle\n")
    _twice = survives(MUT.apply_and_run, pathlib.Path(_edit_tmp), _entry, "fixtures", "F")
    check("...and one that matches twice is too, rather than mutating whichever site is first",
          (isinstance(_twice, MUT.Record) and _twice.verdict == MUT.UNAPPLIED
           and "AMBIGUOUS ANCHOR" in _twice.line), _twice)
finally:
    shutil.rmtree(_edit_tmp, ignore_errors=True)

# THE CHEAP, CONTAINED CHECKS RUN FIRST, and the ordering is load-bearing rather than
# tidy. These are driven on a synthetic table and cannot leak or signal anything; the
# live probes below start real processes and sweep them. A mutation that breaks the
# enumeration makes the LIVE call raise, so with the order reversed the verifier died
# mid-file and F100 reported `failed, but NOT via ... -> []` — the parse found no red
# check because no check had run yet — while leaking the probes it had already started
# (full run, 2026-08-12). Named arms have to get their line out before the code that
# raises is reached.
# THE ENUMERATION ITSELF, on a table rather than on the machine — where the answers are known
# and where the case that must NEVER occur can be arranged.
_synth_table = {10: (1, "/x/w0/harness/suite.py"), 11: (10, "/x/w0/harness/proxy.py"),
                12: (11, "/x/w0/harness/guardian.py"), 13: (1, "/x/w0/harness/orphan.py"),
                14: (1, "/other/thing.py"), 15: (14, "/other/child.py")}
# `survives` ON THE `None` CASES, because the deletion that makes `None` load-bearing is one
# that RAISES: without the guard, `None in command` is a `TypeError`, which is the whole point
# of choosing `None` over a falsy string — and evaluated inside a `check(...)` argument it would
# end the verifier instead of reddening the line that names the property.
check("the sweep walks the whole chain, not just the immediate children",
      survives(MUT.owned_pids, _synth_table, 10, None) == {10, 11, 12},
      survives(MUT.owned_pids, _synth_table, 10, None))
check("...and the marker adds what the chain no longer reaches, without the chain's help",
      MUT.owned_pids(_synth_table, 10, "/x/w0/harness") == {10, 11, 12, 13}
      and MUT.owned_pids(_synth_table, None, "/x/w0/harness") == {10, 11, 12, 13},
      MUT.owned_pids(_synth_table, None, "/x/w0/harness"))
# THE LANDMINE, AND WHY IT IS A `None` RATHER THAN A FALSY STRING. `"" in command` is true of
# every process on the machine, so a marker arm switched off by falsiness would put one boolean
# between this and SIGKILLing the host — inside the one program whose job is to run versions of
# itself with a line removed. `None` cannot be an `in` operand at all, so the same deletion
# raises and kills nothing. Driven on a synthetic table, where being wrong is free.
check("no marker is `None`, and it sweeps by parentage alone",
      survives(MUT.owned_pids, _synth_table, 14, None) == {14, 15}
      and survives(MUT.owned_pids, _synth_table, None, None) == set(),
      survives(MUT.owned_pids, _synth_table, None, None))
check("...and an empty marker is REFUSED rather than quietly matching every process alive",
      isinstance(survives(MUT.owned_pids, _synth_table, None, ""), ValueError),
      survives(MUT.owned_pids, _synth_table, None, ""))
# THE ONE THAT ACTUALLY HAPPENED, and the reason `-1` is gone from this file. `kill(-1, ...)`
# is POSIX for "every process this user may signal" and `kill(0, ...)` for "this whole process
# group"; neither is a sentinel, and the kernel does not ask whether one was meant. A `-1`
# passed here as a harmless-looking "no parentage root" reached `os.kill` under mutation F98 and
# closed every application on the machine (2026-08-12). Both selectors are now `None`, which is
# not a pid, not an `in` operand, and not something `os.kill` will accept — and the refusals
# below are asked of the exact values that did it.
check("a sweep rooted at a broadcast pid is REFUSED, not performed",
      all(isinstance(survives(MUT.owned_pids, _synth_table, bad, None), ValueError)
          for bad in (-1, 0, -2)),
      [survives(MUT.owned_pids, _synth_table, bad, None) for bad in (-1, 0, -2)])
check("...and no non-process ever reaches the one function that signals",
      all(isinstance(survives(MUT._signal, bad), ValueError)
          for bad in (-1, 0, None, "1234")),
      [survives(MUT._signal, bad) for bad in (-1, 0, None, "1234")])
# AND THE GUARD IS NOT A NO-OP: the same function really does kill a real process, so the
# refusals above are a narrowing of something that works rather than a function that never
# signals anything at all.
_victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
MUT._signal(_victim.pid)
_victim.wait(timeout=10)
check("...while a real pid is still signalled, so those refusals narrow a live mechanism",
      _victim.returncode == -signal.SIGKILL, _victim.returncode)
check("...and the driver is never in its own sweep, on either arm",
      (os.getpid() not in MUT.owned_pids({**_synth_table, os.getpid(): (10, "/x/w0/harness/me")},
                                         10, "/x/w0/harness")),
      "the cost of being wrong about that once is the run killing the process doing the killing")

# THE TWO CLOCKS, AT THE ONE PLACE THEY ARE READ OFF THE KERNEL. `wait4` rather than a delta of
# `getrusage(RUSAGE_CHILDREN)`, because the latter is a running total for the whole process and
# under `--jobs N` a delta around one suite would include whatever the other seven workers
# happened to finish inside the same window. That mistake is invisible at `--jobs 1`, where the
# two agree, which is exactly why it needs a check rather than a reading.
_burn = subprocess.Popen([sys.executable, "-c", "sum(range(4_000_000))"])
_burn_status, _burn_cpu, _burn_left = MUT._await(_burn, 60.0)
check("a child's CPU is measured from the wait that reaps THAT child",
      _burn_status is not None and _burn.returncode == 0 and _burn_cpu > 0.0
      and _burn_left == (),
      f"status={_burn_status} rc={_burn.returncode} cpu={_burn_cpu} left={_burn_left}")
check("...and an exit status is read the way subprocess spells it, signals negative",
      (MUT._exit_code(0) == 0 and MUT._exit_code(3 << 8) == 3
       and MUT._exit_code(signal.SIGKILL) == -signal.SIGKILL),
      f"a suite SIGKILLed at its bound must not read as exit {int(signal.SIGKILL)}, which is "
      f"an ordinary failing run and would be scored as one")
# THE TIMEOUT PATH, WHICH IS THE ONE THAT HAS TO REAP. A waiter that returned without killing
# would leave the suite running beside every mutation after it; one that killed without waiting
# would leave a zombie and, on the next mutation, a `wait4` on a pid nothing can wait for.
_sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
_slept_status, _slept_cpu, _slept_left = MUT._await(_sleeper, 0.5)
check("a child that outlives its bound is reported as such, killed, and reaped",
      (_slept_status is None and _sleeper.returncode == -signal.SIGKILL
       and isinstance(survives(os.waitpid, _sleeper.pid, 0), ChildProcessError)
       and _slept_left == ()),
      f"status={_slept_status} rc={_sleeper.returncode} cpu={_slept_cpu} left={_slept_left}")

# THE CONTAINMENT GAP, REPRODUCED AND THEN CLOSED. Killing only the process the waiter holds a
# handle on left every proxy, guardian, fixture server and helper a hung suite had started —
# outliving the `rmtree` of the tree that launched them and running beside the workers still
# going (review, PR #111). The descendant here calls `setsid`, which is not decoration: it is
# what the proxy's guardian does deliberately, and it is the reason a `killpg` aimed at the
# suite is not the fix. `setsid` changes the session, never the parent, so the parentage arm
# still reaches it — provided the table is read BEFORE the first signal.
_SPAWNER = ("import subprocess, sys, time\n"
            "kid = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],\n"
            "                       start_new_session=True)\n"
            "print(kid.pid, flush=True)\n"
            "time.sleep(30)\n")
_tree_probe = subprocess.Popen([sys.executable, "-c", _SPAWNER],
                               stdout=subprocess.PIPE, text=True)
_descendant = int(_tree_probe.stdout.readline().strip())


def _alive(pid):
    """True while `pid` names something that is not a zombie — asked of `ps`, not of a signal.

    `os.kill(pid, 0)` succeeds against a ZOMBIE, which is exactly the state a just-killed
    descendant passes through, so a check written on it would fail intermittently for a
    process that is already dead. The table this asks is the one the sweep itself reads.

    An observer that cannot answer reports "not alive", which is the wrong direction on its
    own — it would make every absence below free. The positive control ahead of them is what
    makes that safe: a broken `ps` fails there, before any absence is read as evidence.
    """
    table = survives(MUT.process_tree)
    return isinstance(table, dict) and pid in table


# THE POSITIVE CONTROL, and it is the whole reason the check below means anything. "The
# descendant is gone" is read off an absence, so it is satisfied by a descendant that never
# started, by one that exited on its own, and by an observer that cannot see processes at all.
check("the descendant of a suite is alive and observable before the bound is reached",
      _alive(_descendant) and _alive(_tree_probe.pid) and _descendant != _tree_probe.pid,
      f"suite={_tree_probe.pid} descendant={_descendant}; without this, the sweep below is "
      f"certified by an instrument that saw nothing in the first place")
_tree_status, _tree_cpu, _tree_left = MUT._await(_tree_probe, 0.5)
for _settle in range(40):                       # init reaps the orphan; it is not instant
    if not _alive(_descendant) and not _alive(_tree_probe.pid):
        break
    time.sleep(0.05)
check("...and a hung suite takes its whole process tree with it, `setsid` and all",
      (_tree_status is None and not _alive(_descendant) and not _alive(_tree_probe.pid)
       and _tree_left == ()),
      f"suite={_tree_probe.pid} alive={_alive(_tree_probe.pid)} descendant={_descendant} "
      f"alive={_alive(_descendant)} leftover={_tree_left} — a survivor here outlives the "
      f"deletion of the work tree it was launched from and runs beside the other workers")
survives(os.waitpid, _tree_probe.pid, os.WNOHANG)
_tree_probe.stdout.close()

# THE MARKER ARM, ON A PROCESS WITH NO PARENTAGE LINK AT ALL. It is what closes the window
# between reading the table and acting on it: a process spawned in that gap is reparented the
# instant the root dies, and only its argv still says whose it was. Driven against a real
# process rather than a table, because the argv `ps` reports is not obviously the argv passed.
_orphan_mark = f"ase-sweep-{uuid.uuid4().hex}"
_orphan = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", _orphan_mark])
check("a process nothing links to us is still ours if its argv names our work tree",
      _alive(_orphan.pid) and _orphan.pid in MUT.owned_pids(MUT.process_tree(), None, _orphan_mark),
      f"{_orphan_mark} -> pid {_orphan.pid}")
_orphan_left = MUT.kill_owned(None, _orphan_mark)
for _settle in range(40):
    if not _alive(_orphan.pid):
        break
    time.sleep(0.05)
check("...and the sweep reaches it by that name alone",
      not _alive(_orphan.pid) and _orphan_left == (),
      f"pid {_orphan.pid} survived a sweep for {_orphan_mark!r}; leftover={_orphan_left}")
survives(os.waitpid, _orphan.pid, os.WNOHANG)


# THE WORK TREE, AND THE ONE THING IT HAS TO DO. The venv is 520MB of a 531MB tree and is now
# SHARED rather than copied — which is safe only because nothing resolves through it: its
# editable install of `agentskill_evals` hardcodes the ORIGINAL tree's path, and what makes a
# mutation bite is the suite running with `cwd` at the work tree, ahead of that finder on
# `sys.meta_path`. This is that argument asked as a question, which is the difference between a
# tree that binds and a run that reports MISSED for all 413 entries an hour later.
_tree_tmp = tempfile.mkdtemp(prefix="verify-worktree-")
try:
    _tree = survives(MUT.make_worktree, MUT.HARNESS, pathlib.Path(_tree_tmp) / "harness")
    _bound = survives(MUT.worktree_binds, _tree) if isinstance(_tree, pathlib.Path) else _tree
    check("a work tree binds its OWN copy of the package, not the original it was copied from",
          (isinstance(_bound, str) and _bound
           and pathlib.Path(_bound).resolve().is_relative_to(_tree.resolve())),
          f"tree={_tree} resolved `agentskill_evals` to {_bound!r}")
finally:
    shutil.rmtree(_tree_tmp, ignore_errors=True)

print()
print("E18. probe_remote_mcp.py's startup path — the copy a fix was left out of")

# WHY THIS IS CHECKED OFFLINE. That probe is opt-in: it needs `claude` on PATH and spends an
# API call, so nothing in the ordinary block runs it, and its startup path had therefore never
# been driven at all. The consequence was reported rather than caught — the verifier's own
# startup was hardened and the probe, which exists to be REUSED, kept the defect: a first line
# that was not JSON raised out of `start_fixture` with the child alive and the caller's handle
# still `None`, leaking a listening server (review, PR #106). The fix belongs to both copies
# and so does the check; that is §4's rule about a duplicated rule, applied to a duplicated FIX.
import probe_remote_mcp as PROBE  # noqa: E402 — section-local, beside the checks that use it


class _Watched:
    """`subprocess`, with a memory of what was started through it.

    THE WITNESS MUST COME FROM OUTSIDE `start_fixture`. What is under test is what it does with
    a handle it does not return: every failure path returns `(None, reason)`, so from the
    caller's side "reaped correctly" and "still running and leaking a port" are the same value.
    A check reading only the return value cannot tell them apart, which is why it gets a
    witness with a different vantage point rather than a cleverer assertion (§4).
    """

    PIPE = subprocess.PIPE

    def __init__(self):
        self.started = []

    def Popen(self, *a, **k):                                  # noqa: N802 — it stands in for one
        proc = subprocess.Popen(*a, **k)
        self.started.append(proc)
        return proc


# Each one is a way a fixture can fail to announce itself. All three sleep afterwards, so a
# process that was NOT reaped is still running when the check reads it — a fake that exited on
# its own would make the reap check pass without anything reaping.
_FAKES = {
    "says something that is not JSON": 'print("definitely not a port", flush=True)',
    "announces no endpoints": "print('{\"port\": 1}', flush=True)",
    "dies before saying anything": ('import sys\nsys.stderr.write("bind: refused\\n")\n'
                                    "raise SystemExit(3)"),
}
_startup_tmp = tempfile.mkdtemp(prefix="probe-startup-")
_real_fixture = PROBE.FIXTURE
for _desc, _body in _FAKES.items():
    _fake = os.path.join(_startup_tmp, "fake.py")
    with open(_fake, "w", encoding="utf-8") as _fh:
        _fh.write(_body + "\nimport time\ntime.sleep(300)\n")
    _watch = _Watched()
    PROBE.subprocess, PROBE.FIXTURE = _watch, _fake
    try:
        _got = survives(PROBE.start_fixture, os.path.join(_startup_tmp, "r.jsonl"), "MARKER")
    finally:
        PROBE.subprocess, PROBE.FIXTURE = subprocess, _real_fixture
    _proc, _why = _got if isinstance(_got, tuple) else (None, _got)
    check(f"a fixture that {_desc} is a NAMED failure, not a traceback",
          _proc is None and isinstance(_why, str) and bool(_why), _got)
    # Read BEFORE the cleanup below, or this asks whether the tidy-up worked.
    _reaped = bool(_watch.started) and all(p.poll() is not None for p in _watch.started)
    # Named per case, not "...and the child is reaped" three times: an arm names a check by its
    # text, and three checks sharing one make the report unable to say which case broke.
    check(f"...and the child of the one that {_desc} is reaped, leaking no listening server",
          _reaped, [(p.pid, p.poll()) for p in _watch.started])
    for _p in _watch.started:                    # so a MUTANT that leaks does not leak for 5m
        _p.kill()
        _p.wait(timeout=DEADLINE)
shutil.rmtree(_startup_tmp, ignore_errors=True)

print()
print("E19. the three copilot probes' classifiers, driven without a copilot install")
# THE VERDICT IS THE PRODUCT. These probes exist to answer one question — can copilot's
# `tools:` back `mcp_tool_filter = "native"` — and an adapter decision is about to rest on the
# word they print. §4's rule for probes applies with the weight of that decision behind it.
#
# WHAT THIS SECTION IS ACTUALLY GUARDING, and it is not the `if` chain. Both gating probes
# printed ENFORCED over a run in which the ALLOWED tool was never called in any arm, because
# the prompt named only the off-list one — so a `tools:` that suppressed the server wholesale
# was indistinguishable from a filter, and the verdict was right by luck. The repair added the
# `SUPPRESSES_ALL` branch; what keeps it honest is that the branch is DRIVEN here, and that
# the readers underneath it are pinned to the fixtures that author the rows they read.
import probe_copilot_config as CFG              # noqa: E402 — after the path bootstrap at E14
import probe_copilot_gating as CG               # noqa: E402
import probe_copilot_remote_gating as CRG       # noqa: E402
import http_mcp_server as HTTPF                 # noqa: E402
import echo_mcp_server as ECHOMOD               # noqa: E402 — the original the probes import

_e19 = tempfile.mkdtemp(prefix="verify-copilot-")
try:
    # -- the readers, against rows their own fixtures wrote ---------------------------------
    # NOT HAND-TYPED DICTS, which is the whole point. Each probe's `called`/`server_ran` is a
    # private copy of its fixture's receipt spelling, and the two fixtures do NOT agree: the
    # echo server writes `kind="request"` with the JSON-RPC method, while the HTTP server
    # writes `kind="request"` for the HTTP verb and a separate `kind="rpc"` row for the
    # message. A probe reading the other one's spelling finds nothing, forever, and reports a
    # perfect filter. Synthetic rows would agree with whatever the probe expects and could
    # never catch it — so the fixtures author these.
    _stdio_receipts = os.path.join(_e19, "echo-receipts.jsonl")
    run([{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
          "params": {"name": "echo", "arguments": {"text": "HI"}}}],
        server=ECHO, extra_env={"ECHO_MCP_RECEIPTS": _stdio_receipts})
    _stdio_rows = CG.read_receipts(_stdio_receipts)
    check("the stdio probe's reader agrees with the receipt the echo fixture actually writes",
          CG.server_ran(_stdio_rows) and CG.called(_stdio_rows, "echo"), _stdio_rows)
    check("...and reports nothing for a tool that was never called",
          not CG.called(_stdio_rows, "add"), _stdio_rows)

    # The HTTP fixture's own writer, its own `dispatch`, pointed at a file read back here.
    _http_receipts = os.path.join(_e19, "http-receipts.jsonl")
    HTTPF.RECEIPTS = HTTPF.Receipts(_http_receipts)
    HTTPF.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "echo", "arguments": {"text": "HI"}}})
    _http_rows = CRG.read_receipts(_http_receipts)
    check("the remote probe's reader agrees with the row the HTTP fixture actually writes",
          CRG.called(_http_rows, "echo") and not CRG.called(_http_rows, "add"), _http_rows)
    check("...and the two probes' readers are NOT interchangeable, which is why each is pinned",
          not CRG.called(_stdio_rows, "echo") or not CG.called(_http_rows, "echo"),
          (_stdio_rows, _http_rows))

    # -- `credential_arrived`: every request, not one of them -------------------------------
    _bearer = "sentinel-for-this-check"

    def _hdr(tok):
        return {"kind": "request", "headers": {"authorization": f"Bearer {tok}"}}

    check("the bearer counts only when it is on EVERY request that carried headers",
          CRG.credential_arrived([_hdr(_bearer), _hdr(_bearer)], _bearer)
          and not CRG.credential_arrived([_hdr(_bearer), _hdr("other")], _bearer),
          "a client that sends the credential once and drops it is a different animal")
    check("...and a run with no requests at all does not count as the bearer having arrived",
          not CRG.credential_arrived([{"kind": "listening"}], _bearer))
    # INTACT, NOT MERELY CONTAINING. `sentinel in value` is true of `Bearer <sentinel>-altered`
    # and of anything that wraps or re-encodes the declared header, so containment would pass a
    # client that sent the server something the harness never declared (review, PR #110).
    check("...and a bearer the client altered around the token does not count as arrival",
          not CRG.credential_arrived(
              [{"kind": "request", "headers": {"authorization": f"Bearer {_bearer}-altered"}}],
              _bearer),
          "containment accepts a value that is not the one declared")

    # -- every verdict of both gating classifiers -------------------------------------------
    def _arms(probe, *tools):
        """Receipt rows in `probe`'s own spelling — read off the reader under test, so the
        helper cannot disagree with it about `kind` while the checks below still pass."""
        kind = "rpc" if probe is CRG else "request"
        return [{"kind": "listening"}] + [{"kind": kind, "method": "tools/call", "tool": t}
                                          for t in tools]

    for _p, _name in ((CG, "stdio"), (CRG, "remote")):
        _both, _echo, _add, _none = (_arms(_p, "echo", "add"), _arms(_p, "echo"),
                                     _arms(_p, "add"), _arms(_p))
        check(f"{_name}: the off-list tool blocked while the on-list one arrives is ENFORCED",
              _p.classify(_echo, _both, True)[0] == _p.ENFORCED,
              _p.classify(_echo, _both, True))
        check(f"{_name}: the off-list tool arriving under the allowlist is LEAKED",
              _p.classify(_both, _both, True)[0] == _p.LEAKED,
              _p.classify(_both, _both, True))
        # THE BRANCH THE ORIGINAL PROBES DID NOT HAVE, and the pair is the check: these two
        # arms differ in exactly one fact — whether the ALLOWED tool arrived — and the first
        # version scored both of them ENFORCED. An allowlist admitting nothing is not a
        # boundary; it is a server that does not work.
        check(f"{_name}: NEITHER tool arriving under the allowlist is SUPPRESSES_ALL, not a filter",
              _p.classify(_none, _both, True)[0] == _p.SUPPRESSES_ALL,
              _p.classify(_none, _both, True))
        check(f"{_name}: ...and that verdict differs from ENFORCED by the on-list call alone",
              _p.classify(_none, _both, True)[0] != _p.classify(_echo, _both, True)[0]
              and _none == [r for r in _echo if r.get("tool") != "echo"])
        # ARRIVING IS NOT WORKING, and this pair is that distinction: identical receipts, one
        # bit apart. Everything the classifier reads comes from the server's record of what
        # came IN, so without this clause a client that forwards the call and drops the reply
        # scores ENFORCED and the harness gates onto a tool that returns nothing.
        check(f"{_name}: an on-list call whose reply never came back is ANSWER_LOST, not ENFORCED",
              _p.classify(_echo, _both, False)[0] == _p.ANSWER_LOST,
              _p.classify(_echo, _both, False))
        check(f"{_name}: ...and the receipts alone cannot tell those two apart",
              _p.classify(_echo, _both, False)[0] != _p.classify(_echo, _both, True)[0])
        # THE PERMISSIVE VALUE IS NOT A DEFAULT. A caller that omitted `answered` would be
        # handed ENFORCED, which is the clause opening the hole it was added to close.
        check(f"{_name}: the round-trip fact is required rather than defaulted",
              isinstance(survives(_p.classify, _echo, _both), TypeError),
              survives(_p.classify, _echo, _both))
        # UNMEASURED IN BOTH DIRECTIONS. The gated arm is read for two facts of opposite sign,
        # so a control that skipped either tool leaves the reading for that one to the model.
        check(f"{_name}: a control that never called the off-list tool measures nothing",
              _p.classify(_echo, _echo, True)[0] == _p.UNMEASURED,
              _p.classify(_echo, _echo, True))
        check(f"{_name}: ...and neither does one that never called the on-list tool",
              _p.classify(_add, _add, True)[0] == _p.UNMEASURED,
              _p.classify(_add, _add, True))
        check(f"{_name}: a server that never started is an instrument failure, not a result",
              _p.classify(_echo, [], True)[0] == _p.INSTRUMENT_FAILED
              and _p.classify([], _both, True)[0] == _p.INSTRUMENT_FAILED)

    # -- the marker must be unreachable except through a tool result -----------------------
    # `answered` is "the marker appeared in the CLI's output", which is evidence about a reply
    # only while the CLI has no other copy. Three revisions put one where it could: the config
    # file copilot reads, then copilot's own environment, then the receipts file — whose path
    # is in that same config and which lands in copilot's working directory under `--allow-all`.
    # The receipts now carry a sha256 and never the marker, so recognition travels without the
    # secret, and the plaintext exists only in the server's memory and its replies.
    _cfg_written = CG.mcp_config(os.path.join(_e19, "cfg.json"),
                                 os.path.join(_e19, "receipts.jsonl"), tools=["echo"])
    with open(_cfg_written, encoding="utf-8") as _fh:
        _cfg_text = _fh.read()
    check("the config asks the server to MINT a marker rather than carrying one",
          CG.IDENTITY_GENERATE in _cfg_text
          and '"tools": ["echo"]' in _cfg_text.replace("'", '"'), _cfg_text)
    # ONE IMPLEMENTATION, NOT TWO. These were duplicated, and every offline check drove the
    # stdio copy — so a fix applied to one would have shipped with everything green.
    check("both probes share ONE digest reader, so a fix cannot land in only one of them",
          CRG.minted_digest is CG.minted_digest
          and CRG.reply_carried_marker is CG.reply_carried_marker,
          (CRG.minted_digest, CG.minted_digest))
    check("...and that sentinel is the fixture's own, not a second spelling of it",
          CG.IDENTITY_GENERATE == ECHOMOD.IDENTITY_GENERATE == CRG.IDENTITY_GENERATE,
          (CG.IDENTITY_GENERATE, ECHOMOD.IDENTITY_GENERATE))
    # DRIVEN THROUGH THE FIXTURE ITSELF, so the digest is the server's and not a claim about it.
    _mint_receipts = os.path.join(_e19, "minted.jsonl")
    _replies, _n, _r, _stream = run(
        [legacy_init(1),
         {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
          "params": {"name": "echo", "arguments": {"text": "PAYLOAD"}}}],
        server=ECHO, extra_env={"ECHO_MCP_RECEIPTS": _mint_receipts,
                                "ECHO_MCP_IDENTITY": ECHOMOD.IDENTITY_GENERATE})
    _rows = CG.read_receipts(_mint_receipts)
    _digest = CG.minted_digest(_rows)
    _reply_text = json.dumps(_replies)
    # THE SERVER REALLY MINTED, asserted directly rather than inferred from the marker's shape.
    # Under a broken mint `IDENTITY` stays the sentinel, whose digest is a perfectly well-formed
    # 64-hex string — so every structural check passes and only this one notices.
    check("the digest reported is not the sentinel's, so the server minted rather than echoed",
          bool(_digest) and _digest != hashlib.sha256(
              ECHOMOD.IDENTITY_GENERATE.encode("utf-8")).hexdigest(),
          _digest)
    # THE TOKEN THE REPLY CARRIED, recovered and then looked for in the serialized receipts.
    # Checking that no KEY is named "identity" tests the schema and not the values — a record
    # carrying the right digest plus `"leaked_plaintext": <marker>` passed it (review, PR #110).
    # The marker is whichever candidate in the reply hashes to the reported digest, so this
    # searches for the real value rather than for a field name.
    _marker_in_reply = next((tok for tok in set(CG._CANDIDATE_RE.findall(_reply_text))
                             if hashlib.sha256(tok.encode()).hexdigest() == _digest), "")
    check("the marker is recoverable from the reply, which is what makes the next check real",
          bool(_marker_in_reply), (_digest, _reply_text[:200]))
    check("the receipts carry a DIGEST, and that marker VALUE appears nowhere in them",
          bool(_digest) and len(_digest) == 64 and bool(_marker_in_reply)
          and _marker_in_reply not in json.dumps(_rows),
          _rows)
    # THE PAIR THAT MAKES IT A MEASUREMENT: the reply carries a token matching that digest, and
    # the receipts do not. Without the second half the digest could simply be of something the
    # file already contains.
    check("...and the reply carries a token whose digest is the one reported",
          CG.reply_carried_marker(_reply_text, _digest), (_digest, _reply_text[:300]))
    check("...while the receipts themselves do not satisfy it, so the file is not a route",
          not CG.reply_carried_marker(json.dumps(_rows), _digest), _rows)
    # THE SENTINEL IS NOT A MARKER. A receipt reporting `@generate` is exactly what a broken
    # mint produces, and the live probe used to accept it — a transcript containing that word
    # then scored ENFORCED. Shape-validation is what refuses it.
    # TWO INSTANCES, AND THEY MUST DIFFER. Excluding the sentinel says the value is not THAT
    # constant; it says nothing about any other. `IDENTITY = "a" * 32` satisfies every digest,
    # reply and receipt check above — and a constant in the fixture SOURCE is readable by a CLI
    # that can read files, which is the non-reply route this whole clause exists to close. Only
    # comparing two independent instances distinguishes "minted" from "hard-coded" (review, PR
    # #110). The reply tokens are compared, not just the digests, so a fixture that varied the
    # digest while emitting a constant marker would fail too.
    _second = os.path.join(_e19, "minted2.jsonl")
    _replies2, _n2, _r2, _s2 = run(
        [legacy_init(1),
         {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
          "params": {"name": "echo", "arguments": {"text": "PAYLOAD"}}}],
        server=ECHO, extra_env={"ECHO_MCP_RECEIPTS": _second,
                                "ECHO_MCP_IDENTITY": ECHOMOD.IDENTITY_GENERATE})
    _digest2 = CG.minted_digest(CG.read_receipts(_second))
    _marker2 = next((tok for tok in set(CG._CANDIDATE_RE.findall(json.dumps(_replies2)))
                     if hashlib.sha256(tok.encode()).hexdigest() == _digest2), "")
    check("two `@generate` instances mint DIFFERENT markers, so it is not a constant",
          bool(_marker_in_reply) and bool(_marker2) and _marker_in_reply != _marker2
          and _digest != _digest2,
          (_marker_in_reply, _marker2))
    check("a receipt whose identity is the generation sentinel is not a minted marker",
          CG.minted_digest([{"kind": "listening", "identity_digest": ECHOMOD.IDENTITY_GENERATE}])
          == "" and CG.minted_digest([{"kind": "listening", "identity_digest": "nope"}]) == ""
          and CG.minted_digest([{"kind": "listening", "identity_digest": 7}]) == "",
          "only a 64-hex digest is a digest")
    check("...and an empty digest is never satisfied by any transcript",
          not CG.reply_carried_marker("@generate anything at all", ""),
          "a server that minted nothing cannot have answered")
    # A run that did NOT ask for a marker must not gain one — the knob stays opt-in, or every
    # existing verbatim-echo check changes meaning. `server_ran` first: a fixture that never
    # started writes no receipts, and the claim would pass on that absence.
    _plain = os.path.join(_e19, "plain.jsonl")
    run([{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
          "params": {"name": "echo", "arguments": {"text": "HI"}}}],
        server=ECHO, extra_env={"ECHO_MCP_RECEIPTS": _plain})
    check("...while a server not asked for a marker reports none, so the knob stays opt-in",
          CG.server_ran(CG.read_receipts(_plain))
          and CG.minted_digest(CG.read_receipts(_plain)) == "",
          CG.read_receipts(_plain))

    # -- and the VERDICTS, which are a separate claim from the classifiers -------------------
    # A classifier proven correct says nothing about whether `main` acted on it. `remote_shape`
    # had its own function and its own mutation while the exit status read only the stdio half,
    # so a remote add that filed a LOCAL entry left the probe green — the finding is the shape
    # of the gap, not the helper (review, PR #110).
    #
    # AND THE VERDICT MUST MEAN WHAT ITS NAME SAYS. The exit status was wired to "the question
    # was settled", under comments claiming it certified the filter — so `LEAKED`, the finding
    # these probes exist to catch, exited 0 beside `ENFORCED`. The two are separate functions
    # now, and this is the pair that holds them apart.
    for _p, _n in ((CG, "stdio"), (CRG, "remote")):
        check(f"{_n}: every definite answer counts as the question having been settled",
              all(_p.settled(v) for v in (_p.ENFORCED, _p.LEAKED, _p.SUPPRESSES_ALL,
                                          _p.ANSWER_LOST))
              and not _p.settled(_p.UNMEASURED) and not _p.settled(_p.INSTRUMENT_FAILED))
    # THE DISTINCTION ITSELF: settled and certifying are not the same set, and the gap is
    # exactly the verdicts that say NO.
    check("stdio: a settled negative does NOT certify `native`, which is what exit 0 claims",
          CG.certifies_native(CG.ENFORCED, True)
          and not any(CG.certifies_native(v, True) for v in (CG.LEAKED, CG.SUPPRESSES_ALL,
                                                             CG.ANSWER_LOST))
          and all(CG.settled(v) for v in (CG.LEAKED, CG.SUPPRESSES_ALL, CG.ANSWER_LOST)),
          "LEAKED is an answer; it is not permission")
    check("remote: ...and the same, per transport, over bearer and version too",
          CRG.certifies_native(CRG.ENFORCED, True, True)
          and not CRG.certifies_native(CRG.LEAKED, True, True)
          and not CRG.certifies_native(CRG.ENFORCED, False, True)
          and not CRG.certifies_native(CRG.ENFORCED, True, False),
          "an SSE leak beside a green Streamable result used to exit 0")
    # THE VERSION GATE. A result written up as "at 1.0.79" is worth nothing from a run that
    # could not read a version, and this used to be a string in a `print`.
    for _p, _n in ((CG, "stdio"), (CRG, "remote"), (CFG, "config")):
        check(f"{_n}: a preflight version must LOOK like a version, not merely be output",
              _p.version_verdict(0, "GitHub Copilot CLI 1.0.79", "")[1] is True
              and _p.version_verdict(0, "warning only", "")[1] is False
              and _p.version_verdict(1, "", "boom")[1] is False
              and _p.version_verdict(0, "", "")[1] is False,
              [_p.version_verdict(0, s, e) for s, e in
               (("GitHub Copilot CLI 1.0.79", ""), ("warning only", ""), ("", ""))])
    check("...and the text is carried through so the failure names what it saw",
          CG.version_verdict(1, "", "not found")[0] == "not found"
          and CG.version_verdict(0, "1.0.79", "")[0] == "1.0.79",
          [CG.version_verdict(1, "", "not found"), CG.version_verdict(0, "1.0.79", "")])
    # THE GATING PROBES USE THE RUN'S OWN WITNESS, not a second execution: copilot's launcher
    # can resolve different cached code between two invocations, which is why the adapter reads
    # the version out of `session.skills_loaded` and why that reader is imported rather than
    # reimplemented here.
    _skills = json.dumps({"type": "session.skills_loaded", "data": {"skills": [
        {"source": "builtin", "path": "/x/pkg/darwin-arm64/1.0.79/builtin/s/SKILL.md"}]}})
    _skills99 = _skills.replace("1.0.79", "9.9.9")
    for _p, _n in ((CG, "stdio"), (CRG, "remote")):
        check(f"{_n}: the version is recovered from the run's own stream",
              _p.agreed_version([_skills]) == ("1.0.79", True), _p.agreed_version([_skills]))
        # EVERY EXECUTED ARM MUST WITNESS. One witnessed arm beside one silent arm reads as
        # witnessed under any rule that concatenates or samples — and the silent arm is exactly
        # the one that could have been a different build.
        check(f"{_n}: an executed arm with no witness leaves the run UNVERIFIED",
              _p.agreed_version([_skills, ""])[1] is False
              and _p.agreed_version([""])[1] is False
              and _p.agreed_version([])[1] is False,
              [_p.agreed_version([_skills, ""]), _p.agreed_version([])])
        # ...AND THEY MUST AGREE. The decisive arm is not the one the version came from unless
        # they are the same build: control at 1.0.79 with a gated arm at 9.9.9 exited 0
        # reporting 1.0.79.
        check(f"{_n}: ...and arms that ran different builds do not agree on one",
              _p.agreed_version([_skills, _skills99])[1] is False
              and _p.agreed_version([_skills, _skills])[1] is True,
              _p.agreed_version([_skills, _skills99]))
        # MODEL-CONTROLLED TEXT MUST NOT FORGE IT — the reasoning the adapter already carries,
        # asserted here because these probes now depend on it.
        check(f"{_n}: ...and prose naming an app root does not count as a witness",
              _p.agreed_version(
                  ['{"type":"assistant","text":"pkg/darwin-arm64/9.9.9/builtin/"}'])[1]
              is False, "only builtin skill paths are structural")
    # The config probe's three findings, a conjunction and not a lookup on the last. The
    # VERSION IS DELIBERATELY ABSENT: `copilot mcp add` emits no in-band witness, so a version
    # term there is unverifiable by construction — always-false (exit 1 on every run, the state
    # `type` used to create) or always-true (a check that cannot fail). It reports shape, and
    # the two gating probes carry the version-qualified claims.
    check("the config probe fails on ANY of its three findings, not just the stdio ones",
          CFG.exit_code([], [], True) == 0
          and all(CFG.exit_code(d, s, r) == 1 for d, s, r in
                  ((["k"], [], True), ([], ["k"], True), ([], [], False))),
          [CFG.exit_code(d, s, r) for d, s, r in
           (([], [], True), (["k"], [], True), ([], ["k"], True), ([], [], False))])
    check("...and it takes no version argument at all, so it cannot pretend to be qualified",
          "version" not in inspect.signature(CFG.exit_code).parameters,
          inspect.signature(CFG.exit_code))
    # AND THE STATE THAT MADE `remote_ok` UNREACHABLE. `type` was a permanent surprise, so the
    # exit status was 1 on every real run and no other term could move it — a conjunction whose
    # terms cannot vary is a constant. This is the measured 1.0.79 body; it must surprise nobody.
    check("the discriminator copilot actually writes is a known key, not a permanent surprise",
          CFG.unexpected_keys({"mcpServers": {"e": {"type": "local", "command": "x",
                                                    "args": [], "env": {}, "tools": ["*"]}}},
                              CFG.EXPECTED) == [],
          "while it was unknown, `surprises` was never empty and `remote_ok` could not matter")
    check("...while a key nobody has measured yet is still reported",
          CFG.unexpected_keys({"mcpServers": {"e": {"type": "local", "wat": 1}}},
                              CFG.EXPECTED) == ["wat"])

    # THE CONTROL DECIDES ALONE, or it does not — and `main` reads the same function
    # `classify` does, so the short-circuit cannot drift from the verdict it is short-cutting.
    for _p, _n in ((CG, "stdio"), (CRG, "remote")):
        _both = _arms(_p, "echo", "add")
        check(f"{_n}: a control that exercised both tools does NOT decide alone",
              _p.control_verdict(_both) is None, _p.control_verdict(_both))
        check(f"{_n}: ...a control that never started decides INSTRUMENT_FAILED",
              (_p.control_verdict([]) or ("", ""))[0] == _p.INSTRUMENT_FAILED)
        check(f"{_n}: ...and one that skipped a tool decides UNMEASURED, so no second call",
              (_p.control_verdict(_arms(_p, "echo")) or ("", ""))[0] == _p.UNMEASURED
              and (_p.control_verdict(_arms(_p, "add")) or ("", ""))[0] == _p.UNMEASURED)
        # AND `classify` MUST AGREE WITH IT, since the whole point is one authority.
        check(f"{_n}: ...and classify returns exactly what the control decided",
              _p.classify(_both, _arms(_p, "echo"), True) ==
              _p.control_verdict(_arms(_p, "echo")))

    # -- and the SHORT-CIRCUIT is proved by counting calls, not by reading the condition ----
    # `control_verdict` agreeing with `classify` says the RULE is right; it says nothing about
    # whether `main` obeys it. Removing the actual short-circuit left every check above green
    # and F72 green with it, because nothing here had ever invoked the consumer (review, PR
    # #110). So: a fake runner, and the claim is a CALL COUNT.
    _calls: list = []

    def _fake_arm(_workdir, *, tools):
        _calls.append(tools)
        # `listening` plus whichever tools this canned control "called".
        rows = [{"kind": "listening"}] + [
            {"kind": "request", "method": "tools/call", "tool": name}
            for name in _fake_arm.tools_called]
        return rows, _fake_arm.transcript

    _real_arm, _real_version = CG.run_arm, CG.agreed_version
    try:
        CG.run_arm = _fake_arm
        CG.agreed_version = lambda streams: ("1.0.79", True)
        # A control that skipped the on-list tool decides UNMEASURED on its own.
        _fake_arm.tools_called, _fake_arm.transcript = ["add"], ""
        _calls.clear()
        _rc_short = CG.main()
        check("stdio main does NOT run the gated arm once the control has decided",
              len(_calls) == 1 and _calls == [None] and _rc_short == 1, _calls)
        # ...and the positive control, or the check above is satisfied by a main that never
        # runs anything at all.
        _fake_arm.tools_called, _fake_arm.transcript = ["add", "echo"], ""
        _calls.clear()
        CG.main()
        check("...while a control that decided nothing DOES run it, so the skip is conditional",
              len(_calls) == 2 and _calls[1] == [CG.ALLOWED], _calls)
    finally:
        CG.run_arm, CG.agreed_version = _real_arm, _real_version

    # The remote probe's `measure` has the same consumer, one layer in.
    _rcalls: list = []

    def _fake_start(receipts, _marker):
        rows = [{"kind": "listening"}] + [
            {"kind": "rpc", "method": "tools/call", "tool": name}
            for name in _fake_remote.tools_called]
        with open(receipts, "w", encoding="utf-8") as fh:
            fh.writelines(json.dumps(r) + "\n" for r in rows)

        class _P:
            def kill(self):
                pass

            def wait(self, timeout=None):
                pass
        return _P(), {"streamable": "http://x/mcp", "sse": "http://x/sse"}

    def _fake_remote(_workdir, _url, _sentinel, _kind, *, tools):
        _rcalls.append(tools)
        return "", None

    _real = (CRG.start_fixture, CRG.run_arm)
    _remote_tmp = tempfile.mkdtemp(prefix="verify-shortcircuit-")
    try:
        CRG.start_fixture, CRG.run_arm = _fake_start, _fake_remote
        _fake_remote.tools_called = ["add"]
        _rcalls.clear()
        CRG.measure(_remote_tmp, "http", "streamable", "sentinel")
        check("remote measure does NOT run the gated arm once the control has decided",
              len(_rcalls) == 1 and _rcalls == [None], _rcalls)
        _fake_remote.tools_called = ["add", "echo"]
        _rcalls.clear()
        CRG.measure(_remote_tmp, "http", "streamable", "sentinel")
        check("...while a control that decided nothing DOES run it, so the skip is conditional",
              len(_rcalls) == 2 and _rcalls[1] == [CRG.ALLOWED], _rcalls)
    finally:
        CRG.start_fixture, CRG.run_arm = _real
        shutil.rmtree(_remote_tmp, ignore_errors=True)

    # THE POOLED VERSION, driven through the consumer rather than read off the helper. Each
    # transport agreeing with ITSELF is not one build enforcing both: review drove HTTP at
    # 1.0.79 and SSE at 9.9.9 and got exit 0, two builds reported as one.
    def _streams_for(version):
        line = json.dumps({"type": "session.skills_loaded", "data": {"skills": [
            {"source": "builtin",
             "path": f"/x/pkg/darwin-arm64/{version}/builtin/s/SKILL.md"}]}})
        rows = [{"kind": "listening"}] + [
            {"kind": "rpc", "method": "tools/call", "tool": n} for n in ("echo", "add")]
        return {"control": (rows, line), "gated": (rows, line)}

    _per_transport: list = []

    def _fake_measure(_workdir, kind, _endpoint, _sentinel):
        return (CRG.ENFORCED, "canned", _streams_for(_per_transport.pop(0)), True, True)

    _real_measure = CRG.measure
    _mt = tempfile.mkdtemp(prefix="verify-pooled-")
    try:
        CRG.measure = _fake_measure
        _per_transport[:] = ["1.0.79", "1.0.79"]
        _same = CRG.main()
        _per_transport[:] = ["1.0.79", "9.9.9"]
        _split = CRG.main()
        check("remote: every arm of every transport names ONE build, not one per transport",
              _same == 0 and _split == 1, (_same, _split))
    finally:
        CRG.measure = _real_measure
        shutil.rmtree(_mt, ignore_errors=True)

    # THE TWO VOCABULARIES ARE ONE VOCABULARY, asserted rather than assumed. Neither probe can
    # import the other (each is a standalone opt-in tool), so this is §4's duplicated-rule rule:
    # pin the copy to the original on the cases that distinguish them. A probe that renamed a
    # verdict would otherwise report a word no reader of the other one recognises.
    check("both gating probes spell the shared verdicts identically",
          (CG.ENFORCED, CG.LEAKED, CG.UNMEASURED, CG.SUPPRESSES_ALL, CG.ANSWER_LOST,
           CG.INSTRUMENT_FAILED)
          == (CRG.ENFORCED, CRG.LEAKED, CRG.UNMEASURED, CRG.SUPPRESSES_ALL, CRG.ANSWER_LOST,
              CRG.INSTRUMENT_FAILED))

    # -- `read_receipts`: a partial line is an ending, not a crash --------------------------
    _torn = os.path.join(_e19, "torn.jsonl")
    with open(_torn, "w", encoding="utf-8") as _fh:
        _fh.write('{"kind":"listening"}\n\n{"kind":"request","meth')
    check("a receipts file whose last line was cut mid-write yields the records before it",
          CG.read_receipts(_torn) == [{"kind": "listening"}], CG.read_receipts(_torn))
    check("...and a receipts file that never appeared is empty rather than an exception",
          CG.read_receipts(os.path.join(_e19, "nope.jsonl")) == [])

    # -- probe_copilot_config.py: three states, and the key nobody thought of ---------------
    _intended = {"mcpServers": {"echo": {"command": "python", "args": [], "env": {},
                                         "tools": ["echo"], "url": "u", "headers": {},
                                         "type": "http"}}}
    check("a config in the intended spelling reports every key confirmed",
          set(CFG.classify_keys(_intended, CFG.EXPECTED).values()) == {CFG.CONFIRMED},
          CFG.classify_keys(_intended, CFG.EXPECTED))
    # `differs` AND `unexercised` ARE NOT ONE STATE. "copilot wrote it differently" is an
    # adapter change; "copilot never wrote it" is another probe run. Collapsing them would
    # report work that does not exist, or hide work that does.
    _renamed = {"mcp_servers": _intended["mcpServers"]}
    check("a differently-named container is reported as `differs`, naming what was found",
          CFG.classify_keys(_renamed, CFG.EXPECTED)["servers_container"] == "differs:mcp_servers",
          CFG.classify_keys(_renamed, CFG.EXPECTED))
    check("...while a config with no container at all leaves every key UNEXERCISED",
          set(CFG.classify_keys({}, CFG.EXPECTED).values()) == {CFG.UNEXERCISED})
    check("a key the adapter has no plan for is reported, since that is the one nobody sees",
          CFG.unexpected_keys({"mcpServers": {"e": {"command": "x", "gateway": "z"}}},
                              CFG.EXPECTED) == ["gateway"])
    check("...and a body holding only known keys reports none, so the finding means something",
          CFG.unexpected_keys({"mcpServers": {"e": {"command": "x", "args": []}}},
                              CFG.EXPECTED) == [])
    # THE SILENT ONE: `copilot mcp add name -- --url X` writes a well-formed LOCAL entry whose
    # command is `--url`. A probe reading only "did a record appear" calls that a remote result.
    _full = {"type": "http", "url": "u", "headers": {"Authorization": "Bearer x"},
             "tools": ["echo"]}
    check("a remote add filed as a local entry is named, not counted as the remote spelling",
          CFG.remote_shape({"type": "local", "command": "--url"})[0] is False
          and CFG.remote_shape({"type": "local", "command": "--url"})[1].startswith("LOCAL"),
          CFG.remote_shape({"type": "local", "command": "--url"}))
    check("...and the true remote shape is not mistaken for it",
          CFG.remote_shape(_full) == (True, CFG.remote_shape(_full)[1])
          and CFG.remote_shape(_full)[1].startswith("remote:"), CFG.remote_shape(_full))
    # THE FULL SHAPE, not just a `url`. The gating probes write `type`/`url`/`headers`/`tools`
    # by hand, and this is what says copilot writes the same four — confirming the url alone
    # would leave the credential and the allowlist resting on documentation.
    check("a remote entry missing the credential or the allowlist is not the shape §8 needs",
          all(CFG.remote_shape({k: v for k, v in _full.items() if k != drop})[0] is False
              for drop in ("headers", "tools")),
          [CFG.remote_shape({k: v for k, v in _full.items() if k != d}) for d in
           ("headers", "tools")])
    # PRESENCE IS NOT SHAPE. `headers: []` and `tools: "wrong"` are values §8's pattern cannot
    # be built from, and key-presence alone filed both as confirmation that it can.
    check("a headers value that is not a mapping is not the credential half of §8's pattern",
          CFG.remote_shape({**_full, "headers": []})[0] is False
          and CFG.remote_shape({**_full, "headers": {"X": "y"}})[0] is False
          and CFG.remote_shape({**_full, "headers": {"Authorization": "Basic zzz"}})[0] is False
          and CFG.remote_shape({**_full, "headers": {"authorization": "Bearer t"}})[0] is True,
          [CFG.remote_shape({**_full, "headers": h}) for h in
           ([], {"X": "y"}, {"Authorization": "Basic zzz"}, {"authorization": "Bearer t"})])
    check("...and an allowlist that is not a non-empty list of names is not one either",
          all(CFG.remote_shape({**_full, "tools": v})[0] is False
              for v in ("wrong", [], [""], [1], {"echo": True})),
          [CFG.remote_shape({**_full, "tools": v}) for v in
           ("wrong", [], [""], [1], {"echo": True})])
    check("...and a transport discriminator that is not the one asked for is refused",
          CFG.remote_shape(_full, want_type="sse")[0] is False
          and CFG.remote_shape({**_full, "type": "sse"}, want_type="sse")[0] is True,
          (CFG.remote_shape(_full, want_type="sse"),
           CFG.remote_shape({**_full, "type": "sse"}, want_type="sse")))
finally:
    shutil.rmtree(_e19, ignore_errors=True)

print()
print("FAILED: " + ", ".join(fails) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
