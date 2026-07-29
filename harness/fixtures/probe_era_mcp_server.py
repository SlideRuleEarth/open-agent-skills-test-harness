#!/usr/bin/env python3
"""A stdio MCP server that measures its CLIENT, for probes C3-0 and C3-1.

`echo_mcp_server.py` is an instrument for testing the harness. This one is an instrument
for testing the four agent CLIs: it answers just enough protocol to keep a client talking,
and records what that client did. Two questions, one process (DESIGN_MCP_Support.md §9):

  C3-0  Which protocol era and EXACT version does the client speak? Revision 2026-07-28
        dropped the `initialize` handshake for per-request `_meta`, so this is no longer
        answerable by assumption. §10.2 gates on the exact versions the proxy implements,
        which makes the version string the load-bearing output — "modern" alone would
        leave that gate unimplementable.

  C3-1  How does the client shut a stdio server down? §10.5 fails a cell that has no clean
        terminator, and that rule is only workable if clients close stdin and wait. The
        spec says they SHOULD, but sanctions escalating to SIGTERM/SIGKILL, so what each
        CLI actually does is a measurement rather than a reading.

It answers BOTH eras by default. A shim that spoke only one would hang the other client at
its first request, and a timeout is indistinguishable from a finding — the measurement
would be of this file rather than of the CLI.

Protocol: JSON-RPC 2.0 over stdio, ONE message per line. stdout carries protocol traffic
and NOTHING else; the log goes to its own file, and diagnostics to stderr.

No third-party imports, by rule — this runs as a subprocess of an agent CLI on whatever
interpreter `command:` resolves to.

Environment:
  PROBE_MCP_LOG            path to the JSONL log (falls back to stderr)
  PROBE_MCP_MODE           dual (default) | modern | legacy — which eras to serve, so the
                           dual-era FALLBACK path can be measured and not just the happy one
  PROBE_MCP_IGNORE_SIGTERM 1 to swallow SIGTERM, to see whether a client escalates to KILL
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time

MODE = os.environ.get("PROBE_MCP_MODE", "dual")
IGNORE_SIGTERM = os.environ.get("PROBE_MCP_IGNORE_SIGTERM") == "1"
MODERN_VERSION = "2026-07-28"
LEGACY_FALLBACK = "2025-06-18"

_started = time.monotonic()
_log_fh = None
_era_recorded = False


def _log(event: str, **fields) -> None:
    """Append one record. Flushed on every write, because the interesting outcome is a
    process that dies without warning: anything still in a userspace buffer when SIGKILL
    lands is exactly the evidence C3-1 needs."""
    rec = {"t": round(time.monotonic() - _started, 6), "event": event, **fields}
    line = json.dumps(rec, sort_keys=True)
    fh = _log_fh or sys.stderr
    fh.write(line + "\n")
    fh.flush()


def _send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _result(req_id, result: dict) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


TOOLS = [{
    "name": "probe_noop",
    "description": "Does nothing. Present only so a client has a tool to list and call.",
    "inputSchema": {"type": "object", "properties": {}},
}]


def _modern_version(msg: dict):
    """The modern era is declared by METADATA, not by method name (§10.2). `server/discover`
    is optional for clients, so a modern client may open with an ordinary `tools/list`;
    keying off the method would misclassify it as legacy."""
    params = msg.get("params")
    if not isinstance(params, dict):
        return None
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        return None
    v = meta.get("io.modelcontextprotocol/protocolVersion")
    return v if isinstance(v, str) else None


def _record_era(msg: dict, method) -> None:
    """First determination wins and is recorded once — era is a property of the server
    process, not of an individual request."""
    global _era_recorded
    if _era_recorded:
        return
    modern = _modern_version(msg)
    if modern is not None:
        _era_recorded = True
        _log("era", era="modern", version=modern, decided_by="_meta", first_method=method)
    elif method == "initialize":
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        v = params.get("protocolVersion")
        _era_recorded = True
        _log("era", era="legacy", version=v if isinstance(v, str) else None,
             decided_by="initialize", first_method=method)


def _discover() -> dict:
    return {
        "resultType": "complete",
        "supportedVersions": [MODERN_VERSION],
        # Capabilities, deliberately NOT tool definitions. §10.6 rejected scanning results
        # for tool-shaped objects; this is the shape that an over-broad check trips on.
        "capabilities": {"tools": {}},
        "_meta": {"io.modelcontextprotocol/serverInfo": {
            "name": "probe-era", "version": "1.0.0"}},
    }


def _initialize(params: dict) -> dict:
    requested = params.get("protocolVersion")
    return {
        "protocolVersion": requested if isinstance(requested, str) else LEGACY_FALLBACK,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": "probe-era", "version": "1.0.0"},
    }


def _on_signal(signum, _frame):
    name = signal.Signals(signum).name
    if signum == signal.SIGTERM and IGNORE_SIGTERM:
        _log("signal", signal=name, action="ignored")
        return
    _log("signal", signal=name, action="exiting")
    _log("terminator", reason="signal", signal=name)
    os._exit(0)


def main() -> int:
    _log("start", pid=os.getpid(), mode=MODE, ignore_sigterm=IGNORE_SIGTERM)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):  # not all signals settable everywhere
            pass

    while True:
        # readline() rather than `for line in sys.stdin`: iteration reads ahead into a
        # buffer, which would smear the timing this probe exists to measure.
        line = sys.stdin.readline()
        if line == "":
            _log("stdin_eof")
            break
        line = line.strip()
        if not line:
            continue

        _log("rx", raw=line[:4096])
        try:
            msg = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            _error(None, -32700, "parse error")
            continue
        if not isinstance(msg, dict):
            _error(None, -32600, "invalid request")
            continue

        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        _record_era(msg, method)

        if req_id is None:
            continue  # notification: never answer

        if method == "server/discover":
            # In `legacy` mode this is refused, which is what a legacy server does and what
            # a dual-era client must fall back from. That fallback is a distinct code path
            # with its own failure modes (§10.9), so it needs to be reachable on purpose.
            if MODE == "legacy":
                _error(req_id, -32601, "method not found: server/discover")
            else:
                _result(req_id, _discover())
        elif method == "initialize":
            if MODE == "modern":
                _error(req_id, -32601, "method not found: initialize")
            else:
                _result(req_id, _initialize(params))
        elif method == "tools/list":
            _result(req_id, {"tools": TOOLS})
        elif method == "tools/call":
            _result(req_id, {"content": [{"type": "text", "text": "ok"}], "isError": False})
        elif method == "ping":
            _result(req_id, {})
        else:
            _error(req_id, -32601, f"method not found: {method}")

    _log("terminator", reason="stdin_eof")
    return 0


if __name__ == "__main__":
    path = os.environ.get("PROBE_MCP_LOG")
    if path:
        _log_fh = open(path, "a", encoding="utf-8")  # noqa: SIM115 — lives for the process
    try:
        sys.exit(main())
    except (BrokenPipeError, KeyboardInterrupt):
        _log("terminator", reason="broken_pipe")
        sys.exit(0)
