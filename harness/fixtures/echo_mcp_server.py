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

DUAL-ERA, over a bounded set of revisions. It implements the `initialize` handshake for the
two legacy versions the shipped fleet was measured speaking (`2025-11-25`, `2025-06-18`)
and per-request `_meta` for exactly `2026-07-28` — not "2026-07-28 and later", since a
revision nobody has read cannot be served conformantly. Anything outside those sets is
refused, modern with `-32022` and its supported list. Probe C3-0 measured the fleet as
split across both eras (§9), so a legacy-only fixture cannot serve all four CLIs.

Era comes from the request, with ONE piece of state. A modern request is judged entirely on
its own `_meta`, because modern MCP is stateless. Legacy is the exception the spec carves
out: `initialize` selects legacy semantics for the process, and the fixture remembers that,
because bare later messages carry no metadata to re-derive it from. The corollary is the
one an earlier revision of this file got wrong — **absence of modern metadata is not
legacy.** Before an `initialize`, a bare request establishes nothing and is refused.

It is LENIENT ABOUT INPUT AND STRICT ABOUT OUTPUT, and the asymmetry is deliberate — this
is a test double, not a measuring instrument. Where `probe_era_mcp_server.py` rejects a
client for any missing required `_meta` field because catching that is its entire job, this
fixture tolerates a missing `clientCapabilities`: the version still identifies the era, so
it can still answer conformantly, and a CLI quirk should not surface as a scenario failure
attributed to the wrong thing. The tolerance runs ONE WAY ONLY — version present and
capabilities absent is served; capabilities present and version absent is a broken *modern*
request and is refused, not quietly downgraded to legacy. That leniency stops precisely at
the protocol version.
Absent or malformed, there is no way to know which shape of reply would be correct, so
guessing would produce exactly the malformed server this fixture must not be — C3-1
established that cost the expensive way, when agy's measured shutdown path changed once the
probe stopped being one.

Two environment knobs, both off by default so the shape every existing check asserts is the
one it gets: `ECHO_MCP_SERVER_NAME` sets the advertised `serverInfo.name`, and
`ECHO_MCP_IDENTITY=<marker>` puts that marker in front of `echo`'s reply. The second exists
because the first is invisible in a RESULT, and it takes a value rather than a flag so the
marker can be one nothing else in the run knows — see `IDENTITY` below. `ECHO_MCP_IDENTITY=
@generate` mints the marker HERE instead, after the CLI has started this process, and reports
it in the `listening` receipt: a marker the driver chose has to travel through the CLI to get
here, so the CLI holds it before any tool runs.

No third-party imports, by rule: this runs as a subprocess of an agent CLI, inside a
per-cell tempdir, on whatever interpreter `command:` resolves to. A dependency here would
be a dependency of every scenario that uses it.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid

# The legacy revisions this fixture actually implements — the two the shipped fleet was
# measured speaking (§9: claude and copilot 2025-11-25, codex 2025-06-18). An earlier
# version MIRRORED whatever the client asked for, so that a protocol bump in any CLI would
# not turn into a mysterious scenario failure. That reasoning was wrong in a way worth
# recording: mirroring `2099-01-01` claims to implement a revision this file has never
# heard of, and a client that believes the claim gets a 2025-shaped answer to a 2099
# request. Selecting from a set it really implements is what the legacy lifecycle asks
# for, and a version mismatch is then visible instead of silently wrong. Extend this tuple
# when a CLI moves.
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18")

# Modern MCP (revision 2026-07-28) dropped the `initialize` handshake for per-request
# `_meta`. Probe C3-0 measured the shipped fleet as SPLIT — claude and copilot on
# 2025-11-25, codex on 2025-06-18, agy already modern (DESIGN_MCP_Support.md §9) — so a
# legacy-only fixture would simply fail to serve agy, and any scenario reaching it under
# Phase 3 would look like a harness bug rather than a missing mode.
MODERN_VERSION = "2026-07-28"
VER_KEY = "io.modelcontextprotocol/protocolVersion"
CAP_KEY = "io.modelcontextprotocol/clientCapabilities"
# Methods that exist only in the modern revision, so a request for one carrying no modern
# `_meta` is unknown rather than servable.
MODERN_ONLY_METHODS = ("server/discover", "subscriptions/listen")

SERVER_NAME = os.environ.get("ECHO_MCP_SERVER_NAME", "echo")

# Opt-in: when set, `echo` prefixes its reply with this value, putting the instance's
# identity in the RESULT rather than only in `serverInfo`. Without it, two instances are
# indistinguishable by their answers — echo is verbatim, so one process can serve two
# aliases and produce exactly the output a correctly-routed pair would, leaving a
# multi-server scenario unable to tell routing from a collision (review, PR #99).
#
# It carries a VALUE rather than being a boolean over SERVER_NAME, and that is the whole
# point. The consumer is a scenario asserting on the agent's final text, and an agent is
# told the server names in its prompt: a marker it could reconstruct from what it was
# already given proves nothing, because a model handed a bare `wolverine-11` will label it
# `alpha:wolverine-11` unprompted. So the scenario supplies an OPAQUE marker that appears
# nowhere in the prompt, and the only way it reaches the answer is a tool result (review,
# second round). Off by default: the verbatim contract is what every other check and both
# `mcp_echo_*` scenarios assert against.
IDENTITY = os.environ.get("ECHO_MCP_IDENTITY") or ""
# `@generate` MEANS THE SERVER MINTS IT, and that is a different security property from a
# marker the driver chose. A supplied marker has to reach this process somehow, and every route
# runs through the CLI under test — its config file, or its environment — so the CLI holds a
# copy before any tool is called, and a diagnostic dump or an `env` in a shell tool satisfies
# "the marker appeared in the output" with nothing having returned. Minted here, after the CLI
# has started us, the value exists in this process and in the replies `echo` emits, and NOWHERE
# ELSE: the receipts carry only its digest, because that file's path is in the CLI's own config
# and the file lands in the CLI's working directory (review, PR #110, four rounds on this one
# clause, each moving the marker one hop rather than changing what could reach it).
IDENTITY_GENERATE = "@generate"
if IDENTITY == IDENTITY_GENERATE:
    IDENTITY = uuid.uuid4().hex


def identity_digest() -> str:
    """sha256 of the marker, or "" when there is no marker. Never the marker itself."""
    return hashlib.sha256(IDENTITY.encode("utf-8")).hexdigest() if IDENTITY else ""

# Opt-in receipts, for the one question no reply can answer: what did the CLIENT actually
# send? `IDENTITY` proves an answer travelled back; this proves a request arrived. Measuring
# whether a CLI's own `tools:` filter is a real boundary needs exactly that and nothing else —
# §6-C2 measured claude's flag NOT stopping the call, which is the finding C3 exists because
# of, and it is a fact about arrival rather than about the model's account of itself.
#
# ONE LINE OF JSON PER REQUEST, FLUSHED, and a `listening` record at startup — the same shape
# as `http_mcp_server.py`, deliberately, because an observation channel that was never
# connected reports the same silence as one reporting a clean run (§4). The startup record is
# what lets a reader tell "the filter stopped it" from "the server never ran".
#
# It records the METHOD and, for `tools/call`, the tool name. Not the arguments: they are
# chosen by a model and this file is archived.
RECEIPTS = os.environ.get("ECHO_MCP_RECEIPTS") or ""


def _receipt(**fields) -> None:
    """Append one record. Never raises: an instrument that can kill the subject it observes
    turns a measurement into a different measurement."""
    if not RECEIPTS:
        return
    try:
        with open(RECEIPTS, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(fields) + "\n")
            handle.flush()
    except OSError:
        pass

# Set by an accepted `initialize`, and the only state carried across requests. Modern
# supplies its context per request; legacy semantics exist only once initialize selects
# them, which is why "no modern metadata" cannot be read as "legacy".
_legacy = None

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


_ABSENT = object()


def _meta_of(msg: dict) -> dict:
    params = msg.get("params")
    if not isinstance(params, dict):
        return {}
    meta = params.get("_meta")
    return meta if isinstance(meta, dict) else {}


# IMPORTED BY `http_mcp_server.py`, along with `_initialize`, `_discover`, `_call_tool`,
# `TOOLS`, `result_envelope` and `error_envelope`. The underscore says "private to the
# stdio transport", which stopped being true when a sibling started serving the same
# protocol over HTTP — they are renamed at the cost of that fixture, and the coupling is
# deliberate: one definition of the era rules means the two transports cannot disagree
# about what a conformant reply is (§4's duplicated-rule rule, satisfied by import).
def _modern_intent(msg: dict) -> bool:
    """Whether this request is TRYING to be modern — the question that must be asked before
    "is it well-formed", because otherwise a malformed modern request is indistinguishable
    from a legacy one.

    EITHER reserved key establishes the intent. The modern schema requires both, so a
    request carrying `clientCapabilities` and no `protocolVersion` is a broken modern
    request, not a legacy one; judging solely on the version key let exactly that shape be
    served as legacy once an `initialize` had made the bare path legal.

    Note `_meta` alone proves nothing: codex and copilot both send a legacy
    `_meta.progressToken` (§9), which is why this tests the two reserved keys by name."""
    meta = _meta_of(msg)
    return VER_KEY in meta or CAP_KEY in meta


def _claimed_version(msg: dict):
    """The protocol version this request declares, or `_ABSENT` if it declares none.

    A sentinel rather than `None`, because `None` is a value the client can actually send:
    `"protocolVersion": null` is a *present but malformed* field, and returning `None` for
    it made it indistinguishable from an absent one."""
    return _meta_of(msg).get(VER_KEY, _ABSENT)


def _reject(req_id, msg: dict, method) -> bool:
    """Answer anything this fixture cannot serve conformantly. True if already answered.

    This is where "lenient about input" stops, and the boundary is not a matter of taste:
    **an absent protocol version cannot be tolerated, because without one there is no way
    to know which conforming output to produce.** A missing `clientCapabilities` is
    different — the version still identifies the era — so that stays tolerated, which is
    the leniency a test double can actually afford.

    An earlier version read "no modern metadata" as "legacy" and served it. Legacy
    semantics do not exist until `initialize` selects them, so that let a bare `tools/list`
    open a session, answered `server/discover` in a legacy shape that revision has no such
    result for, and served a modern-metadata `initialize` as a legacy handshake — the three
    era inversions §10.2 forbids, all from one missing distinction."""
    if _modern_intent(msg):
        claimed = _claimed_version(msg)
        if claimed is _ABSENT:
            # Recognisably modern, and missing a field the schema requires. Serving it as
            # legacy would answer a broken modern request in the wrong era's shape.
            _error(req_id, -32602, f"missing required _meta: {VER_KEY}")
            return True
        if not isinstance(claimed, str):
            _error(req_id, -32602, "malformed _meta: protocolVersion must be a string")
            return True
        if claimed != MODERN_VERSION:
            _send({"jsonrpc": "2.0", "id": req_id, "error": {
                "code": -32022, "message": "Unsupported protocol version",
                "data": {"supported": [MODERN_VERSION], "requested": claimed}}})
            return True
        if method == "initialize":
            _error(req_id, -32601, "method not found: initialize")
            return True
        return False

    if method == "initialize":
        if _legacy is not None:
            _error(req_id, -32600, "already initialized")
            return True
        return False

    if method in MODERN_ONLY_METHODS:
        _error(req_id, -32601, f"method not found: {method}")
        return True

    if _legacy is None:
        _error(req_id, -32602, "no protocol era established: send initialize or modern _meta")
        return True
    return False


def result_envelope(req_id, result: dict, *, modern: bool = False,
                    cacheable: bool = False) -> dict:
    """The result message, shaped for the era of the request being answered. PURE.

    Modern results MUST carry `resultType`, and the operations the caching spec lists —
    `server/discover` and `tools/list` among them — MUST additionally carry `ttlMs` and
    `cacheScope`. `ttlMs: 0` means "immediately stale", which is the right answer for a
    fixture: a client caching this tool list across a scenario would be answering from
    memory rather than from the server the scenario is exercising.

    SPLIT FROM THE SENDING, and public, so `http_mcp_server.py` can answer identically over
    a different transport by IMPORTING this rather than restating it. The era rules above
    are protocol, not transport — a second copy of them could drift from this one silently,
    and then the two fixtures would disagree about what a conformant modern result is while
    both looked right in isolation (§4's duplicated-rule rule; import where import is
    possible, which between siblings in this directory it is).
    """
    if modern:
        out = {"resultType": "complete"}
        out.update(result)
        if cacheable:
            out["ttlMs"] = 0
            out["cacheScope"] = "public"
        result = out
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def error_envelope(req_id, code: int, message: str) -> dict:
    """The error message. PURE, and public for the same reason as `result_envelope`."""
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _result(req_id, result: dict, *, modern: bool = False,
            cacheable: bool = False) -> None:
    _send(result_envelope(req_id, result, modern=modern, cacheable=cacheable))


def _error(req_id, code: int, message: str) -> None:
    _send(error_envelope(req_id, code, message))


def _text(s: str, *, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": s}], "isError": is_error}


def _initialize(params: dict) -> dict:
    """Select a legacy version this fixture implements, and record that legacy is now in
    force. The legacy lifecycle returns the requested version only when it is supported;
    otherwise the server answers with one it does support and the client decides."""
    global _legacy
    requested = params.get("protocolVersion")
    selected = requested if requested in LEGACY_VERSIONS else LEGACY_VERSIONS[0]
    _legacy = {"version": selected, "capabilities": params.get("capabilities")}
    return {
        "protocolVersion": selected,
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
        # The prefix binds identity to payload in ONE reply, which is the property a routing
        # test needs: one process holds one IDENTITY, so it cannot produce two different
        # markers however many aliases are pointed at it.
        return _text(f"{IDENTITY}:{text}" if IDENTITY else text)
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
    # THE POSITIVE FACT, written before a single request is read. Absence of a `tools/call`
    # receipt is the whole finding a gating measurement rests on, and absence is also what a
    # server that never started produces — so the reader checks this record first and calls
    # the run unmeasured without it, rather than reading silence as a filter working.
    # A DIGEST, NEVER THE MARKER. The receipts path is handed to the CLI in its own config —
    # it has to be, that is how the server is told where to write — and the file lands in the
    # CLI's working directory under `--allow-all`, so a shell or file-read tool can print it.
    # Minting moved the secret out of the config and left it readable one hop away: the plain
    # marker in this record is a second route to the value the round-trip clause exists to
    # prove only a REPLY can carry. A sha256 is not invertible, so the driver can still
    # recognise the marker when it sees it and the CLI cannot produce it from this file
    # (review, PR #110). The marker itself now exists only in this process's memory and in the
    # replies `echo` emits.
    _receipt(kind="listening", server=SERVER_NAME, tools=[t["name"] for t in TOOLS],
             identity_digest=identity_digest())
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

        # BEFORE `_reject`, and before the notification check below. What a measurement of a
        # client's filter needs is what the client SENT, which is not the same set as what
        # this server chose to answer — a request refused here still arrived, and a filter
        # that let it through has already failed whether or not it got a reply.
        _receipt(kind="request", method=method if isinstance(method, str) else None,
                 tool=(params.get("name") if method == "tools/call"
                       and isinstance(params.get("name"), str) else None))

        # A NOTIFICATION carries no id and must never be answered — `notifications/
        # initialized` is the one every client sends, and replying to it is a protocol
        # violation that some clients treat as fatal.
        if req_id is None:
            continue

        if _reject(req_id, msg, method):
            continue
        # `_reject` has already guaranteed that modern intent implies a supported
        # version, so intent is the whole test by the time we get here.
        modern = _modern_intent(msg)

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
