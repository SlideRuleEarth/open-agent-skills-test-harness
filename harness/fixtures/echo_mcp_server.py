#!/usr/bin/env python3
"""A zero-dependency stdio MCP server, for testing the harness against a REAL server.

Why this exists rather than a mock: every MCP fact this harness rests on was established
by running a CLI against a live server and watching what it did (codex starting a server
process at all, copilot's `session.mcp_servers_loaded` shapes, claude's empty
`mcp_servers` init list). A mock inside the harness can only confirm what the harness
already believes. This is the thing on the other end of the pipe.

Three jobs:
  1. The instrument for the open verification probes (DESIGN_MCP_Support.md §9) — notably
     whether claude's `--allowedTools` actually GATES MCP tools under
     `--dangerously-skip-permissions` or merely advises. Two tools exist precisely so one
     can be allowed and the other not.
  2. The source of offline parser goldens in selftest.py — each CLI's own event shape for
     an MCP tool call, captured once from a real exchange.
  3. The server behind the `mcp_echo_smoke.yaml` scenario, so CI never depends on a remote
     server being up, credentialed, or unchanged.

Protocol: JSON-RPC 2.0 over stdio, ONE message per line (MCP's stdio transport is
newline-delimited JSON, not LSP-style Content-Length framing). stdout carries protocol
traffic and NOTHING else — any diagnostic goes to stderr, because a stray print would be
parsed as a message and desync the peer.

DUAL-ERA. It serves both the `initialize` handshake (legacy, 2025-11-25 and earlier) and
per-request `_meta` (modern, 2026-07-28+), deciding per REQUEST from that request's own
metadata rather than holding a session era. Probe C3-0 measured the shipped fleet as split
across both, so a legacy-only fixture cannot serve all four CLIs.

It is LENIENT ABOUT INPUT AND STRICT ABOUT OUTPUT, and the asymmetry is deliberate — this
is a test double, not a measuring instrument. `probe_era_mcp_server.py` rejects a client
that omits a required `_meta` field, because catching that is its entire job; this fixture
answers anyway, because a scenario here is testing the harness and a CLI quirk should not
surface as a scenario failure attributed to the wrong thing. Its own replies stay
conforming regardless: a malformed server changes client behaviour, which C3-1 established
the expensive way when agy's measured shutdown path changed once the probe stopped being
malformed. The version-mirroring in `_initialize` is the same principle, already applied.

No third-party imports, by rule: this runs as a subprocess of an agent CLI, inside a
per-cell tempdir, on whatever interpreter `command:` resolves to. A dependency here would
be a dependency of every scenario that uses it.
"""
from __future__ import annotations

import json
import os
import sys

# Mirrored back to whatever the client asks for (see _initialize). Used only when the
# client omits the field.
_FALLBACK_PROTOCOL = "2025-06-18"

# Modern MCP (revision 2026-07-28) dropped the `initialize` handshake for per-request
# `_meta`. Probe C3-0 measured the shipped fleet as SPLIT — claude and copilot on
# 2025-11-25, codex on 2025-06-18, agy already modern (DESIGN_MCP_Support.md §9) — so a
# legacy-only fixture would simply fail to serve agy, and any scenario reaching it under
# Phase 3 would look like a harness bug rather than a missing mode.
MODERN_VERSION = "2026-07-28"
VER_KEY = "io.modelcontextprotocol/protocolVersion"

SERVER_NAME = os.environ.get("ECHO_MCP_SERVER_NAME", "echo")

TOOLS = [
    {
        "name": "echo",
        "description": (
            "Return the given text verbatim. Use this to prove a tool call reached the "
            "server."),
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "text to echo back"}},
            "required": ["text"],
        },
    },
    {
        "name": "add",
        "description": "Add two numbers and return the sum.",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
        },
    },
]


def _send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _modern_request(msg: dict) -> bool:
    """Whether THIS request is modern, read off its own `_meta`.

    Per request, not per session: modern MCP is stateless — "servers MUST NOT rely on
    prior requests over the same connection to establish context" — so there is no
    connection-wide era to consult. Reading it off the method name instead would misclassify
    a modern client that skips `server/discover`, which the spec explicitly permits."""
    params = msg.get("params")
    if not isinstance(params, dict):
        return False
    meta = params.get("_meta")
    return isinstance(meta, dict) and isinstance(meta.get(VER_KEY), str)


def _result(req_id, result: dict, *, modern: bool = False,
            cacheable: bool = False) -> None:
    """Send a result, shaped for the era of the request being answered.

    Modern results MUST carry `resultType`, and the operations the caching spec lists —
    `server/discover` and `tools/list` among them — MUST additionally carry `ttlMs` and
    `cacheScope`. `ttlMs: 0` means "immediately stale", which is the right answer for a
    fixture: a client caching this tool list across a scenario would be answering from
    memory rather than from the server the scenario is exercising."""
    if modern:
        out = {"resultType": "complete"}
        out.update(result)
        if cacheable:
            out["ttlMs"] = 0
            out["cacheScope"] = "public"
        result = out
    _send({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def _text(s: str, *, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": s}], "isError": is_error}


def _initialize(params: dict) -> dict:
    # The client's requested version is MIRRORED rather than pinned. A server may answer
    # with a version it prefers, and the client is then free to hang up — which for a test
    # fixture would turn a protocol-revision bump in any of four CLIs into a mysterious
    # scenario failure. Agreeing with the client keeps this instrument measuring the thing
    # under test instead of itself.
    requested = params.get("protocolVersion")
    return {
        "protocolVersion": requested if isinstance(requested, str) else _FALLBACK_PROTOCOL,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": "1.0.0"},
    }


def _discover() -> dict:
    """The modern opener. Servers MUST implement it; clients MAY skip it, which is why the
    era is decided by `_meta` and not by seeing this method arrive.

    It advertises CAPABILITIES, not tool definitions — the tool surface still comes from
    `tools/list`, so this is not a second channel a `tools:` allowlist would have to gate
    (DESIGN_MCP_Support.md §10.6)."""
    return {
        "supportedVersions": [MODERN_VERSION],
        "capabilities": {"tools": {}},
        "_meta": {"io.modelcontextprotocol/serverInfo": {
            "name": SERVER_NAME, "version": "1.0.0"}},
    }


def _call_tool(params: dict) -> dict:
    name = params.get("name")
    args = params.get("arguments") or {}
    if name == "echo":
        text = args.get("text")
        if not isinstance(text, str):
            return _text("echo requires a string 'text' argument", is_error=True)
        return _text(text)
    if name == "add":
        a, b = args.get("a"), args.get("b")
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            return _text("add requires numeric 'a' and 'b' arguments", is_error=True)
        if isinstance(a, bool) or isinstance(b, bool):  # bool is an int in Python
            return _text("add requires numeric 'a' and 'b' arguments", is_error=True)
        return _text(str(a + b))
    # An unknown TOOL is a tool-level error, not a JSON-RPC one: the call was well-formed
    # and the server is answering it. Reporting -32601 here would tell the client the
    # method `tools/call` does not exist.
    return _text(f"unknown tool: {name!r}", is_error=True)


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
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

        # A NOTIFICATION carries no id and must never be answered — `notifications/
        # initialized` is the one every client sends, and replying to it is a protocol
        # violation that some clients treat as fatal.
        if req_id is None:
            continue

        modern = _modern_request(msg)

        if method == "server/discover":
            _result(req_id, _discover(), modern=modern, cacheable=True)
        elif method == "initialize":
            _result(req_id, _initialize(params))
        elif method == "tools/list":
            _result(req_id, {"tools": TOOLS}, modern=modern, cacheable=True)
        elif method == "tools/call":
            _result(req_id, _call_tool(params), modern=modern)
        elif method == "ping":
            _result(req_id, {}, modern=modern)
        else:
            # `subscriptions/listen` lands here on purpose: agy opens one (§9), this
            # fixture has nothing to push, and its `capabilities` never advertise the
            # feature. Method-not-found is the honest answer, and agy was measured
            # carrying on past exactly that reply.
            _error(req_id, -32601, f"method not found: {method}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (BrokenPipeError, KeyboardInterrupt):
        # The client hung up. Normal shutdown, not a failure worth a traceback on stderr —
        # which some CLIs surface as a server error.
        sys.exit(0)
