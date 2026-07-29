#!/usr/bin/env python3
"""Verify fixtures/probe_era_mcp_server.py by driving it as a scripted client.

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

    python tools/verify_probe_shim.py     # exits non-zero on any failure
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
SHIM = os.path.join(os.path.dirname(HERE), "fixtures", "probe_era_mcp_server.py")
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
    for line in fh:
        q.put(line)
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


def run(msgs, *, mode="dual", kill=None, ignore_sigterm=False, cancel=None):
    """Send `msgs`, collect replies, then shut down via `kill` (a signal) or stdin close.

    `subscriptions/listen` deliberately gets no immediate reply — its JSON-RPC response IS
    the graceful-closure signal — so expected replies are computed per method rather than
    per id. Waiting on it would hang, which is exactly the bug this rewrite fixes."""
    tmp = tempfile.mkdtemp(prefix="verify-shim-")
    log = os.path.join(tmp, "probe.jsonl")
    env = dict(os.environ, PROBE_MCP_LOG=log, PROBE_MCP_MODE=mode)
    if ignore_sigterm:
        env["PROBE_MCP_IGNORE_SIGTERM"] = "1"

    deadline = time.monotonic() + DEADLINE
    responses, notifications = [], []
    p = subprocess.Popen([sys.executable, SHIM], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    q = queue.Queue()
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

        while pending:
            line = _readline(q, deadline)
            if not line:
                break
            msg = json.loads(line)
            if msg.get("id") is None:
                notifications.append(msg)
            else:
                responses.append(msg)
                pending.discard(msg["id"])

        if kill:
            time.sleep(0.15)
            p.send_signal(kill)
        else:
            p.stdin.close()
            while True:  # drain graceful-closure responses until EOF
                line = _readline(q, deadline)
                if line is None or line == "":
                    break
                msg = json.loads(line)
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
    return responses, notifications, recs


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


print("1. modern client that SKIPS server/discover (the P2 case)")
r, n, recs = run([modern("tools/list", 1)])
e = ev(recs, "era")
check("era decided", len(e) == 1, e)
check("era == modern", e and e[0]["era"] == "modern", e)
check("exact version captured", e and e[0]["version"] == "2026-07-28", e)
check("decided by _meta not method", e and e[0]["decided_by"] == "_meta", e)
check("tools/list answered", r and "tools" in r[0].get("result", {}), r)

print("2. modern results are CONFORMING (P1: resultType + caching hints)")
r, n, recs = run([modern("server/discover", 1), modern("tools/list", 2),
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
r, n, recs = run([{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": "2025-06-18"}},
                  {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}])
e = ev(recs, "era")
check("era == legacy", e and e[0]["era"] == "legacy", e)
check("legacy version captured", e and e[0]["version"] == "2025-06-18", e)
by = {m["id"]: m.get("result", {}) for m in r}
check("initialize answered", "protocolVersion" in by.get(1, {}), by.get(1))
check("no resultType leaked into legacy", "resultType" not in by.get(2, {}), by.get(2))

print("4. forced fallback: legacy mode must log LEGACY, not the refused modern attempt")
r, n, recs = run([modern("server/discover", 1),
                  {"jsonrpc": "2.0", "id": 2, "method": "initialize",
                   "params": {"protocolVersion": "2025-06-18"}}], mode="legacy")
by = {m["id"]: m for m in r}
check("discover refused", "error" in by.get(1, {}), by.get(1))
check("then initialize works", "result" in by.get(2, {}), by.get(2))
e = ev(recs, "era")
check("exactly one era record", len(e) == 1, e)
check("negotiated era is legacy", e and e[0]["era"] == "legacy", e)
check("refusal was logged", len(ev(recs, "refused")) == 1, recs)

print("5. forced fallback the other way: modern mode must log MODERN")
r, n, recs = run([{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                  modern("tools/list", 2)], mode="modern")
by = {m["id"]: m for m in r}
check("initialize refused", "error" in by.get(1, {}), by.get(1))
check("then modern request works", "result" in by.get(2, {}), by.get(2))
e = ev(recs, "era")
check("exactly one era record", len(e) == 1, e)
check("negotiated era is modern", e and e[0]["era"] == "modern", e)

print("6. subscriptions/listen: ack first, no immediate response, graceful closure at EOF")
r, n, recs = run([modern("subscriptions/listen", 1, notifications={"toolsListChanged": True}),
                  modern("tools/list", 2)])
acks = [m for m in n if m.get("method") == "notifications/subscriptions/acknowledged"]
check("acknowledgment sent", len(acks) == 1, n)
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
r, n, recs = run([modern("subscriptions/listen", 1, notifications={"toolsListChanged": True})],
                 cancel=[{"jsonrpc": "2.0", "method": "notifications/cancelled",
                          "params": dict(MODERN_META, requestId=1)},
                         modern("ping", 1)])
check("cancellation observed", len(ev(recs, "subscription_cancelled")) == 1, recs)
check("no graceful closure after cancel", len(ev(recs, "subscription_close")) == 0, recs)
pings = [m for m in r if m.get("id") == 1 and "result" in m]
check("id 1 reusable after cancel", len(pings) >= 1, r)

print("8. notification is never answered")
r, n, recs = run([{"jsonrpc": "2.0", "method": "notifications/initialized",
                   "params": dict(MODERN_META)}, modern("ping", 1)])
check("exactly one response (ping only)", len(r) == 1, r)
check("no stray notifications", n == [], n)

print("9. C3-1: stdin close -> clean terminator")
r, n, recs = run([modern("ping", 1)])
check("stdin_eof logged", len(ev(recs, "stdin_eof")) == 1, recs)
t = ev(recs, "terminator")
check("terminator reason == stdin_eof", len(t) == 1 and t[0]["reason"] == "stdin_eof", t)

print("10. C3-1: SIGTERM -> terminator names the signal")
r, n, recs = run([modern("ping", 1)], kill=signal.SIGTERM)
s, t = ev(recs, "signal"), ev(recs, "terminator")
check("signal logged", s and s[0]["signal"] == "SIGTERM", recs)
check("terminator reason == signal", len(t) == 1 and t[0]["reason"] == "signal", t)

print("11. C3-1: SIGKILL -> NO terminator (absence is the answer)")
r, n, recs = run([modern("ping", 1)], kill=signal.SIGKILL)
check("start was logged", len(ev(recs, "start")) == 1, recs)
check("no terminator", len(ev(recs, "terminator")) == 0, recs)

print("12. escalation probe: SIGTERM ignored, process survives to be killed")
r, n, recs = run([modern("ping", 1)], kill=signal.SIGTERM, ignore_sigterm=True)
s = ev(recs, "signal")
check("SIGTERM logged as ignored", s and s[0]["action"] == "ignored", recs)
check("no terminator (was killed)", len(ev(recs, "terminator")) == 0, recs)

print()
print("FAILED: " + ", ".join(fails) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
