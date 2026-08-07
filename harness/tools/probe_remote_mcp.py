#!/usr/bin/env python3
"""§9 probe #1, as a runnable procedure rather than a paragraph someone once ran.

WHY THIS FILE EXISTS. The §9 result for probe #1 is version-qualified — "claude 2.1.113
accepts the remote config shape and forwards the declared headers" — and a version-qualified
claim that cannot be remeasured expires silently the next time the CLI updates. The fixture
verifier proves the FIXTURE against `urllib`; nothing in it runs claude, drives the
unreachable control, or asserts that every live request carried the sentinel. Without those,
the published result rests on a scratch file that no longer exists, which is the state §9's
own C3-0/C3-1 entries were rescued from (review, PR #106).

OPT-IN, because it costs an API call and needs `claude` on PATH. Nothing runs it
automatically; it is the recipe, kept executable so it cannot drift from the claim it backs.

    python3 tools/probe_remote_mcp.py                 # both transports + the control
    python3 tools/probe_remote_mcp.py --transport sse  # one of them

WHAT IT ASSERTS, and each of these is a separate failure mode:

  1. `--mcp-config` ACCEPTS the shape `claude._write_mcp_config` writes. Read from the init
     event's `mcp_servers`, not inferred from the run succeeding.
  2. The server reaches `status: "connected"` — the POSITIVE CONTROL. Without it,
     `status: "failed"` from the unreachable case below is not evidence of anything, since an
     unparseable config produces exactly the same word (§4).
  3. An UNREACHABLE url reports `failed` — the negative control, which is what makes (2)
     informative rather than tautological.
  4. The tools are advertised as `mcp__<server>__<tool>` and the model calls one.
  5. EVERY request the fixture received carried the declared `Authorization` header, with the
     sentinel value intact. Read from the fixture's receipts — the server's account of what
     arrived, not the client's account of what it sent, because a claim and the thing it
     claims about must not have the same author.

The sentinel is generated per run and never written to disk outside the receipts file, which
lives in a temp dir this script removes. It is a fake token; the point is that a REAL one
would follow the same path.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(os.path.dirname(HERE), "fixtures")
FIXTURE = os.path.join(FIXTURES, "http_mcp_server.py")
DEADLINE = 60.0

findings: list[str] = []


def check(label: str, ok: bool, detail="") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + ("" if ok else f"  <- {str(detail)[:400]}"))
    if not ok:
        findings.append(label)


def _pump(fh, q):
    for line in fh:
        q.put(line)
    q.put(None)


def start_fixture(receipts: str):
    """The fixture, or a named reason it could not start. Never an unbounded wait."""
    proc = subprocess.Popen([sys.executable, FIXTURE], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            env={**os.environ, "HTTP_MCP_RECEIPTS": receipts,
                                 "HTTP_MCP_ALLOWED_ORIGINS": ""})
    q: queue.Queue = queue.Queue()
    threading.Thread(target=_pump, args=(proc.stdout, q), daemon=True).start()
    try:
        line = q.get(timeout=15)
    except queue.Empty:
        line = None
    if not line:
        proc.kill()
        proc.wait(timeout=15)
        err = (proc.stderr.read() or b"").decode(errors="replace").strip().splitlines()
        return None, (f"the fixture died with: {err[-1].strip()}" if err
                      else "the fixture announced no port and said nothing")
    return proc, json.loads(line)


def run_claude(config_path: str, prompt: str, cwd: str) -> dict:
    """One claude run, returning the init event and the result. Bounded, and reaped."""
    argv = ["claude", "-p", prompt, "--mcp-config", config_path, "--strict-mcp-config",
            "--output-format", "stream-json", "--verbose", "--model", "claude-haiku-4-5",
            "--dangerously-skip-permissions"]
    out = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=DEADLINE)
    init, result = {}, None
    for line in out.stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "system" and ev.get("subtype") == "init":
            init = ev
        elif ev.get("type") == "result":
            result = ev.get("result")
    return {"init": init, "result": result, "rc": out.returncode,
            "stderr": out.stderr[-400:]}


def receipts_rows(path: str, kind=None) -> list[dict]:
    if not os.path.exists(path):
        return []
    rows = [json.loads(x) for x in open(path, encoding="utf-8") if x.strip()]
    return [r for r in rows if kind is None or r["kind"] == kind]


def probe_transport(transport: str) -> None:
    print(f"\n{transport}: a live claude run against the local fixture")
    tmp = tempfile.mkdtemp(prefix=f"probe1-{transport}-")
    receipts = os.path.join(tmp, "receipts.jsonl")
    sentinel = f"PROBE1-{uuid.uuid4().hex[:12]}"
    proc = None
    try:
        proc, info = start_fixture(receipts)
        if proc is None:
            check(f"{transport}: the fixture starts", False, info)
            return
        url = info["streamable"] if transport == "http" else info["sse"]
        # EXACTLY the shape claude._write_mcp_config emits. Restated here rather than imported
        # because the point is to check the ADAPTER's shape against the CLI — importing the
        # adapter's writer would make the probe agree with it by construction.
        cfg = os.path.join(tmp, "mcp.json")
        with open(cfg, "w", encoding="utf-8") as fh:
            json.dump({"mcpServers": {"probe": {
                "type": transport, "url": url,
                "headers": {"Authorization": f"Bearer {sentinel}"}}}}, fh)

        run = run_claude(cfg, "Call the echo tool with the text 'probe one'. "
                              "Then reply with just its output.", tmp)
        servers = run["init"].get("mcp_servers") or []
        status = {s.get("name"): s.get("status") for s in servers}
        check(f"{transport}: the config is ACCEPTED — the server appears in the init event",
              "probe" in status, run)
        check(f"{transport}: ...and reaches `connected` — the positive control",
              status.get("probe") == "connected", status)
        tools = [t for t in (run["init"].get("tools") or []) if t.startswith("mcp__probe__")]
        check(f"{transport}: ...its tools are advertised as mcp__<server>__<tool>",
              sorted(tools) == ["mcp__probe__add", "mcp__probe__echo"], tools)
        check(f"{transport}: ...and the model calls one and gets its answer",
              (run["result"] or "").strip().strip('"').endswith("probe one"), run["result"])

        # THE HEADER QUESTION, from the server's own account. `all`, not `any`: one request
        # carrying the token while others do not is a client that drops it on retry, which a
        # real credential would surface as an intermittent auth failure.
        reqs = receipts_rows(receipts, "request")
        auth = [r["headers"].get("authorization") for r in reqs]
        check(f"{transport}: the witness recorded its own startup, so its silence is readable",
              bool(receipts_rows(receipts, "listening")), receipts)
        check(f"{transport}: EVERY request carried the declared bearer token, intact",
              bool(auth) and all(a == f"Bearer {sentinel}" for a in auth),
              [a if a is None else a[:20] + "..." for a in auth])
    finally:
        if proc is not None:
            proc.kill()
            proc.wait(timeout=15)
        shutil.rmtree(tmp, ignore_errors=True)


def probe_unreachable() -> None:
    """The NEGATIVE control: `failed` must be producible, or `connected` proves nothing."""
    print("\ncontrol: an unreachable url must report `failed`, not `connected`")
    tmp = tempfile.mkdtemp(prefix="probe1-control-")
    try:
        cfg = os.path.join(tmp, "mcp.json")
        with open(cfg, "w", encoding="utf-8") as fh:
            json.dump({"mcpServers": {"probe": {
                "type": "http", "url": "https://example.invalid/mcp",
                "headers": {"Authorization": "Bearer UNUSED"}}}}, fh)
        run = run_claude(cfg, "reply with exactly: ok", tmp)
        status = {s.get("name"): s.get("status") for s in (run["init"].get("mcp_servers") or [])}
        check("an unreachable server is listed but NOT connected", status.get("probe") == "failed",
              status)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--transport", choices=["http", "sse", "both"], default="both")
    args = ap.parse_args()
    if shutil.which("claude") is None:
        print("claude is not on PATH — this probe drives the real CLI on purpose.")
        return 2
    ver = subprocess.run(["claude", "--version"], capture_output=True, text=True)
    print(f"claude: {ver.stdout.strip() or '(version unreadable)'}")
    print("A version-qualified result is only as good as the version it names; §9 records "
          "this one.\n")
    for t in (["http", "sse"] if args.transport == "both" else [args.transport]):
        probe_transport(t)
    probe_unreachable()
    print()
    print("FAILED: " + ", ".join(findings) if findings else "ALL PASS")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
