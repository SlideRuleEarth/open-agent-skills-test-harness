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
  4. The tools are advertised as `mcp__<server>__<tool>`, and the model INVOKES one — read
     from the fixture's receipts.
  5. The tool's ANSWER gets back to the model, proved by an opaque per-run marker that THIS
     SCRIPT generates and hands to the fixture privately, in its environment, for `echo` to
     prefix its reply with. What makes it evidence is that it appears nowhere in the prompt —
     not who generated it, which an earlier wording got backwards (review, PR #106). (4) and
     (5) were once one assertion over the model's final text, and that text was supplied BY
     the prompt: a client that advertised the tools and never called one passed it by
     repeating itself.
  6. EVERY request the fixture received carried the declared `Authorization` header, with the
     sentinel value intact. Read from the fixture's receipts — the server's account of what
     arrived, not the client's account of what it sent, because a claim and the thing it
     claims about must not have the same author.
  7. The `MCP-Protocol-Version` the client declares is one the fixture implements. This is
     what keeps the fixture's 400-on-unsupported path safe: it refuses a version outside
     `echo.LEGACY_VERSIONS`, and that refusal stops being correct the moment a CLI moves to a
     revision outside it. §9 records `2025-11-25` for claude; this is what remeasures it.

THE SENTINEL AND THE MARKER are generated per run and both are fake. The sentinel reaches disk
in TWO files, not one: the receipts, and `mcp.json` — which is where a bearer token has to be
for the CLI to send it at all. An earlier version of this paragraph named only the receipts,
which made the file's own security note its least accurate line (review, PR #106). Both files
live under a `0700` temp dir this script removes on every exit path. The marker never reaches
disk here at all; it is handed to the fixture in its environment. The point of the sentinel is
that a REAL credential would travel exactly this path.
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
sys.path.insert(0, FIXTURES)

# Imported for the ONE tuple this file asserts against — the versions the fixture implements,
# which is also the set it now answers 400 outside of. Restating it here would let this probe
# pass while the fixture refused every request a real client sent (§4: where import is
# possible, import). The config shape a few lines down is restated for the opposite reason,
# and the two are not in tension: that one is checking the ADAPTER against the CLI, so
# importing the adapter's writer would make it agree by construction.
import echo_mcp_server as ECHO  # noqa: E402 — the path insert above has to come first
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


def start_fixture(receipts: str, marker: str):
    """The fixture, or a named reason it could not start. Never an unbounded wait, and never a
    running server the caller has no handle to kill.

    EVERY FAILURE PATH REAPS, INCLUDING THE PARSING ONES. An earlier version parsed the
    announcement in its `return` statement, so a first line that was not JSON raised out of
    here with the child still running and the caller's `proc` still `None` — leaving its
    `finally` nothing to kill. This is the verifier's startup defect, which was fixed there and
    left standing in the copy meant to be REUSED (review, PR #106); a leaked HTTP server holds
    its port, so the leak fails the next run rather than this one.
    """
    proc = subprocess.Popen(
        [sys.executable, FIXTURE], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, "HTTP_MCP_RECEIPTS": receipts, "HTTP_MCP_ALLOWED_ORIGINS": "",
             # The opaque marker `echo` prefixes its reply with. It travels in the ENVIRONMENT
             # and not in the prompt, which is the whole of what makes it evidence.
             "ECHO_MCP_IDENTITY": marker})

    def reaped(reason: str):
        """Kill FIRST and read stderr after: a live child's stderr can block forever."""
        proc.kill()
        proc.wait(timeout=15)
        said = [x for x in (proc.stderr.read() or b"").decode(errors="replace").splitlines()
                if x.strip()]
        return None, reason + (f"; its last word on stderr: {said[-1].strip()}" if said else "")

    q: queue.Queue = queue.Queue()
    threading.Thread(target=_pump, args=(proc.stdout, q), daemon=True).start()
    try:
        line = q.get(timeout=15)
    except queue.Empty:
        line = None
    if not line:
        return reaped("the fixture announced no port within the deadline")
    try:
        info = json.loads(line)
    except ValueError:
        return reaped(f"the fixture's first line was not a port announcement: {line[:200]!r}")
    if not (isinstance(info, dict) and info.get("streamable") and info.get("sse")):
        return reaped(f"the port announcement named no endpoints: {str(info)[:200]}")
    return proc, info


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
    marker = f"MARK-{uuid.uuid4().hex[:12]}"
    proc = None
    try:
        proc, info = start_fixture(receipts, marker)
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

        # "Verbatim" matters: the tool's reply is prefixed with the marker, and a model that
        # tidies the prefix away would fail the round-trip check for a reason that is about
        # presentation rather than transport.
        run = run_claude(cfg, "Call the echo tool with the text 'probe one'. Then reply with "
                              "its output, verbatim and with nothing else.", tmp)
        servers = run["init"].get("mcp_servers") or []
        status = {s.get("name"): s.get("status") for s in servers}
        check(f"{transport}: the config is ACCEPTED — the server appears in the init event",
              "probe" in status, run)
        check(f"{transport}: ...and reaches `connected` — the positive control",
              status.get("probe") == "connected", status)
        tools = [t for t in (run["init"].get("tools") or []) if t.startswith("mcp__probe__")]
        check(f"{transport}: ...its tools are advertised as mcp__<server>__<tool>",
              sorted(tools) == ["mcp__probe__add", "mcp__probe__echo"], tools)
        # THAT A TOOL RAN IS THE SERVER'S FACT, NOT THE MODEL'S. The assertion this replaces
        # read the model's final text for the string the tool was asked to echo — and that
        # string came from the prompt, so a client which advertised the tools and never
        # invoked one satisfied it by repeating itself (review, PR #106).
        calls = [r for r in receipts_rows(receipts, "rpc") if r.get("method") == "tools/call"]
        check(f"{transport}: ...the model INVOKES one — from the server's record of the call, "
              f"not the model's account of itself",
              any(c.get("tool") == "echo" for c in calls), calls)
        # AND THE ANSWER TRAVELS BACK, which the receipts cannot witness: the server can say
        # what it wrote, not what was read. What proves it is a value the model had no other
        # way to hold — the per-run marker above, handed to the fixture in its environment and
        # prefixed onto `echo`'s reply by `ECHO_MCP_IDENTITY`, absent from the prompt and from
        # everything else the model was given. That mechanism exists one layer up for
        # precisely this argument (see echo_mcp_server.py).
        check(f"{transport}: ...and its ANSWER reaches the model — carrying a per-run marker "
              f"the prompt never contained, so only a tool result could have supplied it",
              marker in (run["result"] or ""), run["result"])

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

        # THE VERSION THE CLIENT DECLARES, asked PER MESSAGE. The first version of this dropped
        # the requests with no header and then required only that one remained — under which a
        # client sending the header once and omitting it on every later request passed, while
        # §9 went on publishing that every post-handshake request carries it (review, PR #106).
        # An assertion weaker than the sentence it is supposed to protect is not protecting it.
        #
        # `initialize` is the one message legitimately without it: it PRECEDES the negotiation
        # the header reports. That is why the fixture records the JSON-RPC method beside the
        # headers — the exemption has to be identified, not assumed to be "whichever one was
        # missing".
        msgs = [r for r in reqs if r.get("rpc")]           # a GET on /sse carries none
        after = [r for r in msgs if r["rpc"] != "initialize"]
        carried = [r for r in after if r["headers"].get("mcp-protocol-version")]
        bare = sorted({r["rpc"] for r in msgs if not r["headers"].get("mcp-protocol-version")})
        seen = {r["headers"].get("mcp-protocol-version") for r in msgs} - {None}
        # PRINTED, PASS OR FAIL. §9 publishes a count, and `check()` shows its detail only when
        # a check goes red — so on a green run the published number was unrecoverable, the
        # receipts having been deleted on the way out (review, PR #106).
        print(f"       {transport}: MCP-Protocol-Version on {len(carried)}/{len(msgs)} messages"
              f" — absent on {bare or 'nothing'}, values {sorted(seen) or 'none'}")

        check(f"{transport}: every protocol version it declares is one the fixture implements",
              bool(seen) and seen <= set(ECHO.LEGACY_VERSIONS),
              {"seen": sorted(seen), "supported": ECHO.LEGACY_VERSIONS})
        if transport == "http":
            # STREAMABLE HTTP ONLY, and the split is the transport's, not a convenience: this
            # header belongs to that binding, and `/sse` + `/messages` is the `2024-11-05`
            # transport, which predates it. The fixture enforces the 400 on `/mcp` alone for
            # the same reason, so requiring it of `sse` here would assert something no
            # specification asks that transport for. The sse tally is reported above instead.
            check(f"{transport}: ...on EVERY post-handshake message, which is the published "
                  f"§9 result — not merely on one of them",
                  bool(after) and len(carried) == len(after),
                  {"post-handshake": [r["rpc"] for r in after], "carried": len(carried)})
            check(f"{transport}: ...and the only message without one is the `initialize` that "
                  f"precedes the negotiation",
                  set(bare) <= {"initialize"}, bare)
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
