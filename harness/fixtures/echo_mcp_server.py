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


def _result(req_id, result: dict) -> None:
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

        if method == "initialize":
            _result(req_id, _initialize(params))
        elif method == "tools/list":
            _result(req_id, {"tools": TOOLS})
        elif method == "tools/call":
            _result(req_id, _call_tool(params))
        elif method == "ping":
            _result(req_id, {})
        else:
            _error(req_id, -32601, f"method not found: {method}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (BrokenPipeError, KeyboardInterrupt):
        # The client hung up. Normal shutdown, not a failure worth a traceback on stderr —
        # which some CLIs surface as a server error.
        sys.exit(0)
