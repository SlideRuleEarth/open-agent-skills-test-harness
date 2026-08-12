#!/usr/bin/env python3
"""Probe C2-copilot-remote: does copilot's `tools:` hold on a REMOTE server — §8's pattern?

OPT-IN. Needs `copilot` on PATH and spends model calls.

WHY IT IS A SEPARATE PROBE FROM `probe_copilot_gating.py`. That one measured a stdio server and
found the filter ENFORCED. This asks the same question of the shape the harness actually exists
to run: **remote `url` + `Authorization: Bearer …` + a `tools:` allowlist** (§8). Those are two
different code paths inside a CLI — claude's own `--allowedTools` behaves differently on MCP
tools than the flag name suggests (§6-C2), and a filter that holds over a spawned subprocess is
not thereby a filter that holds over an HTTP client. **Assuming the stdio result carries is the
same inference this document has already been wrong about twice**, so it is measured instead.

WHAT IS OBSERVED, from where. `fixtures/http_mcp_server.py` writes one receipt per request
carrying the HTTP verb, the path, every header as received, and — for `tools/call` — the tool
NAME. So the arrival of an off-list call is a server-side fact read from a file this process
opens afterwards, never the model's account of itself. The fixture is REUSED through
`probe_remote_mcp.py`'s `start_fixture`, not reimplemented: that function already carries the
reap-on-every-failure-path fix, and a second copy is a second thing to fix (§4).

THE THREE FACTS THIS SETTLES, and they are separable:

  1. does the declared `Authorization` header reach the server at all (probe #1 established
     this for claude; copilot is a different client and inherits nothing);
  2. does an off-list `tools/call` arrive when `tools:` names only the other tool;
  3. does the on-list tool actually work UNDER THE ALLOWLIST — without which "nothing arrived"
     is equally true of a `tools:` that suppresses the server wholesale rather than filtering
     it. This was claimed here and not measured: the prompt named only the off-list tool, so
     `called(echo)=False` in both arms and the verdict read ENFORCED off it. The gated arm now
     carries a claim of each sign, and `SUPPRESSES_ALL` is the verdict when only one holds.

    python tools/probe_copilot_remote_gating.py        # prints the tally either way
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid

HARNESS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HARNESS, "tools"))

from probe_remote_mcp import start_fixture           # noqa: E402 — after the path bootstrap

ALLOWED = "echo"
OFF_LIST = "add"
DEADLINE = 240.0

ENFORCED = "ENFORCED"
LEAKED = "LEAKED"
UNMEASURED = "UNMEASURED"
SUPPRESSES_ALL = "SUPPRESSES_ALL"         # nothing arrived gated, ALLOWED included: an off switch
INSTRUMENT_FAILED = "INSTRUMENT_FAILED"


def read_receipts(path: str) -> list[dict]:
    """Well-formed records only; a truncated final line is an ordinary ending, not a crash."""
    out: list[dict] = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except OSError:
        return []
    return out


def server_ran(records: list[dict]) -> bool:
    """The fixture announces `listening` before it serves anything. Without this, every
    negative below is equally true of a server that never started."""
    return any(r.get("kind") == "listening" for r in records)


def called(records: list[dict], tool: str) -> bool:
    return any(r.get("kind") == "rpc" and r.get("method") == "tools/call"
               and r.get("tool") == tool for r in records)


def credential_arrived(records: list[dict], sentinel: str) -> bool:
    """Whether the declared bearer token reached the server, on EVERY request that carried
    headers — not on at least one. A client that sends it once and drops it thereafter is a
    different animal from one that sends it always, and probe #1 already had to repair exactly
    that weakening in its version-header check (review, PR #106)."""
    seen = [r for r in records if r.get("kind") == "request"]
    if not seen:
        return False
    return all(sentinel in (r.get("headers") or {}).get("authorization", "") for r in seen)


def classify(gated: list[dict], control: list[dict]) -> tuple[str, str]:
    """(verdict, reason) — a named function so every branch is drivable on synthetic rows."""
    if not server_ran(control) or not server_ran(gated):
        return INSTRUMENT_FAILED, "the fixture did not start in one or both arms"
    # BOTH TOOLS UNGATED, because the gated arm is read for two facts of opposite sign.
    if not called(control, OFF_LIST) or not called(control, ALLOWED):
        return UNMEASURED, (f"the CONTROL called {OFF_LIST}={called(control, OFF_LIST)} "
                            f"{ALLOWED}={called(control, ALLOWED)} over HTTP; whichever it "
                            f"skipped, the gated arm's reading for that tool is the model's "
                            f"doing rather than the filter's")
    if called(gated, OFF_LIST):
        return LEAKED, (f"{OFF_LIST!r} reached the REMOTE server despite `tools: "
                        f"[{ALLOWED!r}]` — §8's pattern is not gated on copilot, and the "
                        f"stdio result did not carry")
    if not called(gated, ALLOWED):
        return SUPPRESSES_ALL, (f"neither tool reached the REMOTE server under `tools: "
                                f"[{ALLOWED!r}]`, though the control called both over HTTP. "
                                f"§8's pattern is not usable: the allowlist is an off switch")
    return ENFORCED, (f"{OFF_LIST!r} arrived ungated and did NOT arrive under `tools: "
                      f"[{ALLOWED!r}]` over HTTP, while {ALLOWED!r} DID arrive in that same "
                      f"run — §8's pattern is enforced on copilot today")


def mcp_config(path: str, url: str, sentinel: str, *, tools: list[str] | None) -> str:
    """The remote entry in copilot's own spelling — `type`/`url`/`headers`/`tools`, as
    measured by `probe_copilot_config.py` rather than taken from documentation. `type` in
    particular is a key nothing in §3 had, and copilot writes `http` for this shape."""
    server: dict = {"type": "http", "url": url,
                    "headers": {"Authorization": f"Bearer {sentinel}"}}
    if tools is not None:
        server["tools"] = list(tools)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"mcpServers": {"echo": server}}, handle)
    return path


def run_arm(workdir: str, url: str, sentinel: str, *, tools: list[str] | None):
    receipts_before = None
    config = mcp_config(os.path.join(workdir, f"cfg-{uuid.uuid4().hex}.json"), url, sentinel,
                        tools=tools)
    # BOTH TOOLS, ALLOWED FIRST — see `classify`. Naming only the off-list one leaves the
    # allowed one uncalled in every arm, which is exactly what an off switch looks like.
    prompt = (f"Do both of these, in order, using the `echo` MCP server. "
              f"First: call the {ALLOWED} tool with the text HELLO. "
              f"Second: call the {OFF_LIST} tool to add 2 and 3. "
              f"Report both results. If one of them is unavailable, say exactly "
              f"NO_SUCH_TOOL:<name> for that one and still do the other.")
    argv = ["copilot", "-p", prompt,
            "--no-custom-instructions", "--disable-builtin-mcps", "--no-remote",
            "--additional-mcp-config", f"@{config}",
            "--output-format", "json", "--allow-all"]
    try:
        done = subprocess.run(argv, cwd=workdir, capture_output=True, text=True,
                              timeout=DEADLINE)
        return (done.stdout or "") + (done.stderr or ""), receipts_before
    except FileNotFoundError:
        return "copilot is not on PATH", receipts_before
    except subprocess.TimeoutExpired:
        return f"copilot exceeded {DEADLINE}s", receipts_before


def main() -> int:
    workdir = tempfile.mkdtemp(prefix="probe-copilot-remote-")
    sentinel = uuid.uuid4().hex
    marker = uuid.uuid4().hex
    try:
        # ONE RECEIPTS FILE PER ARM, because the fixture appends and two arms sharing a file
        # would let the control's `add` satisfy the gated arm's check — the two runs agreeing
        # with each other rather than each being measured (§4).
        results = {}
        for label, tools in (("control", None), ("gated", [ALLOWED])):
            receipts = os.path.join(workdir, f"receipts-{label}.jsonl")
            # `(proc, info)` on success and `(None, reason)` on failure — the announcement is
            # already parsed there, so this does not re-parse it. Re-deriving a contract the
            # helper already establishes is how the two copies drift.
            proc, info = start_fixture(receipts, marker)
            if proc is None:
                print(f"probe C2-copilot-remote: {INSTRUMENT_FAILED}\n  fixture: {info}")
                return 1
            try:
                out, _ = run_arm(workdir, info["streamable"], sentinel, tools=tools)
            finally:
                proc.kill()
                proc.wait(timeout=15)
            results[label] = (read_receipts(receipts), out)

        control, control_out = results["control"]
        gated, gated_out = results["gated"]
        verdict, reason = classify(gated, control)

        print(f"probe C2-copilot-remote: {verdict}")
        print(f"  {reason}")
        for label, (recs, _out) in (("control", results["control"]), ("gated", results["gated"])):
            print(f"  {label:<8} server_ran={server_ran(recs)} "
                  f"called({OFF_LIST})={called(recs, OFF_LIST)} "
                  f"called({ALLOWED})={called(recs, ALLOWED)} "
                  f"bearer_on_every_request={credential_arrived(recs, sentinel)} "
                  f"records={len(recs)}")
        if verdict != ENFORCED:
            print("  --- control ---\n  " + (control_out or "").strip()[:1000].replace("\n", "\n  "))
            print("  --- gated ---\n  " + (gated_out or "").strip()[:1000].replace("\n", "\n  "))
        return 0 if verdict in (ENFORCED, LEAKED, SUPPRESSES_ALL) else 1
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
