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
        terminator, and that rule is only workable if clients close stdin, or signal in a
        way the server can catch. What each CLI does is a measurement, not a reading.

It answers BOTH eras. A shim that spoke only one would hang the other client at its first
request, and a timeout is indistinguishable from a finding.

CONFORMANCE MATTERS HERE, and an earlier revision of this file got it wrong. A client is
entitled to a well-formed peer, and a tolerant client papering over a malformed server
turns C3-1 into a measurement of that tolerance rather than of the shutdown path. So
modern responses carry `resultType`, cacheable results (`server/discover`, `tools/list`)
carry the `ttlMs`/`cacheScope` hints the spec makes MUST, and `subscriptions/listen`
opens a real stream — acknowledgment first, graceful closure at the end — instead of
being refused.

Protocol: JSON-RPC 2.0 over stdio, ONE message per line. stdout carries protocol traffic
and NOTHING else; the log goes to its own fd, and diagnostics to stderr.

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
SUB_ID_KEY = "io.modelcontextprotocol/subscriptionId"

_started = time.monotonic()
_log_fd = None
_era = None               # negotiated era, set only when a request is ACCEPTED
_version = None           # the exact negotiated version, enforced on later requests
_subscriptions = {}       # listen-request id -> acknowledged notification filter


def _log(event: str, **fields) -> None:
    """Append one record with a single os.write().

    Raw fd rather than a buffered file object, for two reasons that both matter to C3-1:
    a small O_APPEND write is atomic and needs no flush, so a kill arriving mid-run cannot
    erase what came before it; and it is safe to call from a signal handler, which buffered
    JSON/file I/O is not."""
    rec = {"t": round(time.monotonic() - _started, 6), "event": event, **fields}
    data = (json.dumps(rec, sort_keys=True) + "\n").encode()
    if _log_fd is not None:
        os.write(_log_fd, data)
    else:
        os.write(2, data)


def _send(msg: dict) -> None:
    os.write(1, (json.dumps(msg) + "\n").encode())


def _result(req_id, payload: dict, *, modern: bool, cacheable: bool = False,
            sub_id=None) -> None:
    """Send a result, shaped for the negotiated era.

    Modern results MUST carry `resultType`; the operations listed as cacheable in the spec
    (`server/discover`, `tools/list`, and the list/read family) MUST additionally carry
    `ttlMs` and `cacheScope`. ttlMs=0 says "immediately stale", which is the honest answer
    for a probe and keeps a client from caching a tool list across the measurement."""
    if modern:
        out = {"resultType": "complete"}
        out.update(payload)
        if cacheable:
            out["ttlMs"] = 0
            out["cacheScope"] = "public"
        if sub_id is not None:
            meta = dict(out.get("_meta") or {})
            meta[SUB_ID_KEY] = sub_id
            out["_meta"] = meta
        payload = out
    _send({"jsonrpc": "2.0", "id": req_id, "result": payload})


def _error(req_id, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def _notify(method: str, params: dict) -> None:
    _send({"jsonrpc": "2.0", "method": method, "params": params})


TOOLS = [{
    "name": "probe_noop",
    "description": "Does nothing. Present only so a client has a tool to list and call.",
    "inputSchema": {"type": "object", "properties": {}},
}]


VER_KEY = "io.modelcontextprotocol/protocolVersion"
CAP_KEY = "io.modelcontextprotocol/clientCapabilities"


def _meta_of(msg: dict) -> dict:
    params = msg.get("params")
    if not isinstance(params, dict):
        return {}
    meta = params.get("_meta")
    return meta if isinstance(meta, dict) else {}


def _modern_version(msg: dict):
    """The modern era is declared by METADATA, not by method name (§10.2). `server/discover`
    is optional for clients, so a modern client may open with an ordinary `tools/list`;
    keying off the method would misclassify it as legacy."""
    v = _meta_of(msg).get(VER_KEY)
    return v if isinstance(v, str) else None


def _modern_intent(msg: dict) -> bool:
    """Whether this request is TRYING to be modern.

    Not the same as being well-formed: a request carrying `clientCapabilities` but no
    `protocolVersion` is a modern request missing a required field, and must be rejected
    as such rather than quietly served as legacy. Note `_meta` alone proves nothing —
    codex and copilot both send a legacy `_meta.progressToken` (§9)."""
    meta = _meta_of(msg)
    return VER_KEY in meta or CAP_KEY in meta


def _attempt(msg: dict, method):
    """What era this message CLAIMS. Distinct from what gets negotiated: under
    PROBE_MCP_MODE the server refuses one era's opener, and recording the attempt as the
    outcome would log the opposite of what the client actually fell back to."""
    v = _modern_version(msg)
    if v is not None:
        return "modern", v, "_meta"
    if method == "initialize":
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        pv = params.get("protocolVersion")
        return "legacy", pv if isinstance(pv, str) else None, "initialize"
    return None, None, None


def _negotiate(msg: dict, method) -> None:
    """Record the era ONLY on a request the server accepts. Called from each accepting
    branch, after the refusal checks."""
    global _era, _version
    if _era is not None:
        return
    era, version, how = _attempt(msg, method)
    if era is None:
        return
    _era, _version = era, version
    _log("era", era=era, version=version, decided_by=how, first_method=method)


def _reject(req_id, msg: dict, method):
    """Validate EVERY request, including the one that establishes the era.

    Enforcement used to start only after negotiation, which let the opening request set
    the terms it was supposed to be checked against: an opener declaring `2099-01-01` was
    accepted and recorded as the negotiated version, and a bare opener was served in legacy
    shape having established nothing.

    The check is per REQUEST, not per session, because that is what the revision actually
    says: modern is stateless — "servers MUST NOT rely on prior requests over the same
    connection to establish context ... every request supplies this metadata" — while a
    dual-era server selects LEGACY semantics from an `initialize` and keeps them for the
    stdio process. So a bare request is legacy only once `initialize` has selected it, and
    a modern request is judged entirely on its own `_meta`.

    Returns True when the request was rejected and already answered."""
    meta = _meta_of(msg)

    if _modern_intent(msg):
        missing = [k for k in (VER_KEY, CAP_KEY) if k not in meta]
        if missing:
            # Both protocolVersion and clientCapabilities are required on every modern
            # request, and the spec names the code: "A request missing any required field
            # is malformed; the server MUST reject it with -32602".
            _log("violation", why="modern request missing required _meta", method=method,
                 missing=missing)
            _error(req_id, -32602, f"missing required _meta: {', '.join(missing)}")
            return True
        claimed = meta[VER_KEY]
        if claimed != MODERN_VERSION:
            _log("violation", why="unsupported protocol version", method=method,
                 claimed=claimed)
            _send({"jsonrpc": "2.0", "id": req_id, "error": {
                "code": -32022, "message": "Unsupported protocol version",
                "data": {"supported": [MODERN_VERSION], "requested": claimed}}})
            return True
        if MODE == "legacy":
            _log("violation", why="modern request to a legacy-only server", method=method)
            _error(req_id, -32600, "this server implements only initialization-based versions")
            return True
        return False

    if method == "initialize":
        if MODE == "modern":
            _log("violation", why="initialize to a modern-only server", method=method)
            _error(req_id, -32601, "method not found: initialize")
            return True
        return False

    # Neither modern metadata nor `initialize`: legal only after `initialize` selected
    # legacy semantics for this process.
    if _era != "legacy":
        _log("violation", why="request before any era was established", method=method)
        _error(req_id, -32602, "no protocol era established: send initialize or modern _meta")
        return True
    return False


def _discover() -> dict:
    return {
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


def _open_subscription(req_id, params: dict, modern: bool) -> None:
    """Acknowledge first, per spec: `notifications/subscriptions/acknowledged` MUST be the
    first message on the stream, and the subscription id IS the listen request's id. The
    request itself stays open — its JSON-RPC response is the graceful-closure signal, sent
    at shutdown, not now."""
    wanted = params.get("notifications") if isinstance(params.get("notifications"), dict) else {}
    agreed = {k: v for k, v in wanted.items() if k == "toolsListChanged"}
    _subscriptions[req_id] = (agreed, modern)
    _log("subscription_open", id=req_id, requested=wanted, acknowledged=agreed)
    _notify("notifications/subscriptions/acknowledged",
            {"_meta": {SUB_ID_KEY: req_id}, "notifications": agreed})


def _close_subscriptions(reason: str) -> None:
    """Graceful closure: the server SHOULD answer the original listen request with an empty
    result before the stream ends. A client that gets it knows the subscription closed
    cleanly rather than being dropped."""
    for sub_id in list(_subscriptions):
        _, modern = _subscriptions.pop(sub_id)
        _log("subscription_close", id=sub_id, reason=reason)
        _result(sub_id, {}, modern=modern, sub_id=sub_id)


def _on_signal(signum, _frame):
    name = signal.Signals(signum).name
    if signum == signal.SIGTERM and IGNORE_SIGTERM:
        _log("signal", signal=name, action="ignored")
        return
    _log("signal", signal=name, action="exiting")
    _close_subscriptions("signal")
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

        if req_id is None:
            # A notification is never answered — but `notifications/cancelled` still has to
            # be OBSERVED, because it retires a subscription and frees its id for reuse.
            if method == "notifications/cancelled":
                cancelled = params.get("requestId")
                if cancelled in _subscriptions:
                    del _subscriptions[cancelled]
                    _log("subscription_cancelled", id=cancelled)
            continue

        if _reject(req_id, msg, method):
            continue
        is_modern = _modern_intent(msg)

        if method == "server/discover":
            # In `legacy` mode this is refused, which is what a legacy server does and what
            # a dual-era client must fall back from. That fallback is a distinct code path
            # with its own failure modes (§10.9), so it needs to be reachable on purpose.
            if MODE == "legacy":
                _log("refused", method=method, mode=MODE)
                _error(req_id, -32601, "method not found: server/discover")
                continue
            _negotiate(msg, method)
            _result(req_id, _discover(), modern=True, cacheable=True)
        elif method == "initialize":
            if MODE == "modern":
                _log("refused", method=method, mode=MODE)
                _error(req_id, -32601, "method not found: initialize")
                continue
            _negotiate(msg, method)
            _result(req_id, _initialize(params), modern=False)
        elif method == "subscriptions/listen":
            _negotiate(msg, method)
            _open_subscription(req_id, params, is_modern)
        elif method == "tools/list":
            _negotiate(msg, method)
            _result(req_id, {"tools": TOOLS}, modern=is_modern, cacheable=True)
        elif method == "tools/call":
            _negotiate(msg, method)
            _result(req_id, {"content": [{"type": "text", "text": "ok"}], "isError": False},
                    modern=is_modern)
        elif method == "ping":
            _negotiate(msg, method)
            _result(req_id, {}, modern=is_modern)
        else:
            _log("refused", method=method, mode=MODE)
            _error(req_id, -32601, f"method not found: {method}")

    _close_subscriptions("stdin_eof")
    _log("terminator", reason="stdin_eof")
    return 0


if __name__ == "__main__":
    path = os.environ.get("PROBE_MCP_LOG")
    if path:
        _log_fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        sys.exit(main())
    except (BrokenPipeError, KeyboardInterrupt):
        _log("terminator", reason="broken_pipe")
        sys.exit(0)
