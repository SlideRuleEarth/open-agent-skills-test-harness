#!/usr/bin/env python3
"""Verify fixtures/probe_era_mcp_server.py by driving it as a scripted client.

The C3-0/C3-1 results table in DESIGN_MCP_Support.md §9 is only as trustworthy as the
instrument that produced it: if the shim misreads an era, every row is wrong and nothing
downstream would notice. This is that instrument's own check, kept in the repo so the
measurement is reproducible rather than resting on a scratch file that no longer exists.

Fixtures carry no selftest arms and are not mutation targets, so this lives in tools/
alongside the other runnable verifiers rather than in the arm count.

Drives the shim over pipes with a scripted client — no agent CLI, no network, no cost,
about two seconds. Covers what the results depend on: era decided by `_meta` rather than
method name (the case a modern client that skips `server/discover` would break), the
dual-era fallback path, notification silence, and all three shutdown outcomes including
the SIGKILL case where the ABSENCE of a terminator is the finding.

    python tools/verify_probe_shim.py     # exits non-zero on any failure
"""
import json, os, signal, subprocess, sys, tempfile, time

SHIM = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'fixtures', 'probe_era_mcp_server.py')

def run(msgs, mode="dual", kill=None, ignore_sigterm=False):
    log = tempfile.mktemp(suffix=".jsonl")
    env = dict(os.environ, PROBE_MCP_LOG=log, PROBE_MCP_MODE=mode)
    if ignore_sigterm:
        env["PROBE_MCP_IGNORE_SIGTERM"] = "1"
    p = subprocess.Popen([sys.executable, SHIM], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True)
    replies = []
    for m in msgs:
        p.stdin.write(json.dumps(m) + "\n"); p.stdin.flush()
        if m.get("id") is not None:
            replies.append(json.loads(p.stdout.readline()))
    if kill:
        time.sleep(0.15)
        p.send_signal(kill)
        time.sleep(0.25)
        if p.poll() is None:
            p.kill()
    else:
        p.stdin.close()
    p.wait(timeout=5)
    recs = [json.loads(l) for l in open(log) if l.strip()]
    return replies, recs

def ev(recs, name):
    return [r for r in recs if r["event"] == name]

fails = []
def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}{'' if cond else '  <- ' + detail}")
    if not cond: fails.append(label)

MODERN_META = {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                         "io.modelcontextprotocol/clientCapabilities": {}}}

print("1. modern client that SKIPS server/discover (the P2 case)")
r, recs = run([{"jsonrpc":"2.0","id":1,"method":"tools/list","params":dict(MODERN_META)}])
e = ev(recs, "era")
check("era decided", len(e) == 1, str(e))
check("era == modern", e and e[0]["era"] == "modern", str(e))
check("exact version captured", e and e[0]["version"] == "2026-07-28", str(e))
check("decided by _meta not method", e and e[0]["decided_by"] == "_meta", str(e))
check("first_method is tools/list", e and e[0]["first_method"] == "tools/list", str(e))
check("tools/list answered", r and "tools" in r[0].get("result", {}), str(r))

print("2. legacy client")
r, recs = run([{"jsonrpc":"2.0","id":1,"method":"initialize",
                "params":{"protocolVersion":"2025-06-18"}}])
e = ev(recs, "era")
check("era == legacy", e and e[0]["era"] == "legacy", str(e))
check("legacy version captured", e and e[0]["version"] == "2025-06-18", str(e))
check("initialize answered", r and "protocolVersion" in r[0].get("result", {}), str(r))

print("3. dual-era probe: server/discover in dual mode")
r, recs = run([{"jsonrpc":"2.0","id":1,"method":"server/discover","params":dict(MODERN_META)}])
res = r[0].get("result", {})
check("DiscoverResult returned", res.get("resultType") == "complete", str(r))
check("supportedVersions advertised", res.get("supportedVersions") == ["2026-07-28"], str(r))
check("capabilities not tool defs", res.get("capabilities") == {"tools": {}}, str(r))

print("4. legacy-mode fallback path: server/discover must be refused")
r, recs = run([{"jsonrpc":"2.0","id":1,"method":"server/discover","params":dict(MODERN_META)},
               {"jsonrpc":"2.0","id":2,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}],
              mode="legacy")
check("discover refused", "error" in r[0], str(r[0]))
check("then initialize works", "result" in r[1], str(r[1]))

print("5. modern-mode: initialize must be refused")
r, recs = run([{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}], mode="modern")
check("initialize refused", "error" in r[0], str(r[0]))

print("6. notification is never answered")
r, recs = run([{"jsonrpc":"2.0","method":"notifications/initialized","params":dict(MODERN_META)},
               {"jsonrpc":"2.0","id":1,"method":"ping","params":dict(MODERN_META)}])
check("exactly one reply (ping only)", len(r) == 1, str(r))
check("ping answered", r and r[0].get("id") == 1, str(r))

print("7. C3-1: stdin close -> clean terminator")
r, recs = run([{"jsonrpc":"2.0","id":1,"method":"ping","params":dict(MODERN_META)}])
check("stdin_eof logged", len(ev(recs, "stdin_eof")) == 1, str(recs))
t = ev(recs, "terminator")
check("terminator written", len(t) == 1, str(recs))
check("terminator reason == stdin_eof", t and t[0]["reason"] == "stdin_eof", str(t))

print("8. C3-1: SIGTERM -> terminator names the signal")
r, recs = run([{"jsonrpc":"2.0","id":1,"method":"ping","params":dict(MODERN_META)}],
              kill=signal.SIGTERM)
s = ev(recs, "signal"); t = ev(recs, "terminator")
check("signal logged", s and s[0]["signal"] == "SIGTERM", str(recs))
check("terminator written", len(t) == 1, str(recs))
check("terminator reason == signal", t and t[0]["reason"] == "signal", str(t))

print("9. C3-1: SIGKILL -> NO terminator (absence is the answer)")
r, recs = run([{"jsonrpc":"2.0","id":1,"method":"ping","params":dict(MODERN_META)}],
              kill=signal.SIGKILL)
check("start was logged", len(ev(recs, "start")) == 1, str(recs))
check("no terminator", len(ev(recs, "terminator")) == 0, str(recs))

print("10. escalation probe: SIGTERM ignored, process survives to be killed")
r, recs = run([{"jsonrpc":"2.0","id":1,"method":"ping","params":dict(MODERN_META)}],
              kill=signal.SIGTERM, ignore_sigterm=True)
s = ev(recs, "signal")
check("SIGTERM logged as ignored", s and s[0]["action"] == "ignored", str(recs))
check("no terminator (was SIGKILLed)", len(ev(recs, "terminator")) == 0, str(recs))

print()
print("FAILED: " + ", ".join(fails) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
