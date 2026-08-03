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
  PROBE_MCP_INIT_DELAY_MS  hold the `initialize` RESPONSE this long and record what arrives
                           meanwhile — C3-2, the pipelining measurement (0 = off)

C3-2 AND WHY IT NEEDED AN INSTRUMENT CHANGE. "Does this client pipeline ordinary requests
behind `initialize` without waiting for the response?" cannot be read off any earlier log
from this shim, and the reason is structural rather than an oversight: a server ANSWERS
`initialize` before it reads the next line, so by the time a pipelined request is read the
era is established and nothing looks unusual. Arrival timestamps do not separate the two
cases either — both show the next line being read right after the response goes out. The
only way to see it is to hold the response and watch the pipe, which is what
`PROBE_MCP_INIT_DELAY_MS` does. It matters because a PROXY is more exposed than the server
it fronts: it reads lines serially without answering them, so a pipelined request reaches
its era check with the negotiation still in flight (`DESIGN_MCP_Support.md` §10.2).
"""
from __future__ import annotations

import collections
import json
import os
import signal
import sys
import threading
import time

MODE = os.environ.get("PROBE_MCP_MODE", "dual")
IGNORE_SIGTERM = os.environ.get("PROBE_MCP_IGNORE_SIGTERM") == "1"
INIT_DELAY_MS = int(os.environ.get("PROBE_MCP_INIT_DELAY_MS") or 0)
MODERN_VERSION = "2026-07-28"
# The legacy versions this shim actually implements. The legacy lifecycle says a server
# returns the REQUESTED version only when it supports it, and otherwise answers with one it
# does support — echoing anything back would turn C3-0's load-bearing version column into a
# recording of the client's own guess. Both measured legacy versions are here (§9).
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18")
SUB_ID_KEY = "io.modelcontextprotocol/subscriptionId"
# Methods that exist ONLY in the modern revision, so a request for one carrying no modern
# `_meta` is unknown rather than servable. `subscriptions/listen` belongs here for the same
# reason `server/discover` does: it was introduced in 2026-07-28 and REPLACED the legacy
# `resources/subscribe`, so serving it under legacy semantics would answer a method that
# revision does not have. Keeping this as a set rather than a special case is the point —
# the first version of this gate named one method and silently let the other through.
MODERN_ONLY_METHODS = ("server/discover", "subscriptions/listen")

_started = time.monotonic()
_log_fd = None
# PROTOCOL STATE. Modern is stateless; LEGACY retains what its `initialize` negotiated.
# A boolean was not enough: later legacy messages carry no metadata at all, so the
# negotiated VERSION is the only thing that says which legacy revision to read them under
# — and this shim implements two. Anything reading later bare traffic needs this, which is
# why the proxy must keep the same state rather than a flag (§10.2).
# None until `initialize`; then {"version", "capabilities", "clientInfo"}.
_legacy = None
# TELEMETRY — what C3-0 reports. Deliberately NOT protocol state: a single `_era` used as
# both made dispatch order-dependent, so a modern request followed by an accepted
# `initialize` left legacy disabled and refused the bare requests that followed it.
_first_era = None
_seen_eras = set()
_subscriptions = {}       # listen-request id -> (acknowledged filter, modern flag)
# Id keys ADMITTED to the C3-3 timeline. A response is only recorded for one of these, so a
# reply to a refused frame cannot mark an id answered that never arrived. See `_send`.
_admitted = set()


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


def _valid_request_id(req_id) -> bool:
    """Whether this is a usable `RequestId`, by the PROXY's rule — see `_id_key` on drift.

    C3-3's timeline may only contain ids the proxy would actually correlate on, and letting
    anything else in broke the measurement two ways: a list id is unhashable, so the reader
    crashed rather than reporting, and `true` followed by `1` manufactured a reuse out of
    nothing, because Python aliases `True` to `1` while JSON-RPC does not (review, PR #100).
    Malformed traffic is logged as its own event instead, which is a finding about the client
    rather than an input to the price of §10.4's rule.
    """
    if isinstance(req_id, bool) or req_id is None:
        return False
    if isinstance(req_id, str):
        return True
    if isinstance(req_id, int):
        return True
    return isinstance(req_id, float) and not (req_id != req_id or req_id in (
        float("inf"), float("-inf")))


def _envelope_shape(msg):
    """Which JSON-RPC shape the PROXY would call this, or `None` if it would refuse it.

    Mirrors `agentskill_evals.mcp_proxy.classify_envelope` — ALL FOUR SHAPES, not just the
    request branch — and is checked against it in `verify_mcp_fixtures.py` for the same reason
    `_id_key` is: this file cannot import the proxy, so the copy must be pinned rather than
    trusted.

    THE POINT IS THE `None`, as much as the shapes. The proxy fails the connection on any
    envelope it cannot classify, so every one of those is a terminal marker in the C3-3
    timeline. Two earlier versions were narrower and each let a proxy-fatal frame read as
    ordinary traffic: checking only the id admitted a JSON-RPC 1.0 request, and checking only
    request-shaped frames left batches and malformed NOTIFICATIONS — which carry no id at all
    — producing no marker, so the timeline said the conversation continued past a message the
    proxy would have died on (review, PR #100).
    """
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return None
    has_method, has_id = "method" in msg, "id" in msg
    has_result, has_error = "result" in msg, "error" in msg
    if has_result and has_error:
        return None
    if has_method and (has_result or has_error):
        return None
    if has_method:
        if not isinstance(msg["method"], str) or not msg["method"]:
            return None
        if "params" in msg and not isinstance(msg["params"], dict):
            return None
        if has_id:
            return "request" if _valid_request_id(msg["id"]) else None
        return "notification"
    if has_result:
        if not has_id or not _valid_request_id(msg["id"]):
            return None
        return "result" if isinstance(msg["result"], dict) else None
    if has_error:
        err = msg["error"]
        if not isinstance(err, dict) or not isinstance(err.get("code"), int) \
                or isinstance(err.get("code"), bool) or not isinstance(err.get("message"), str):
            return None
        if has_id and not _valid_request_id(msg["id"]):
            return None
        return "error"
    return None


def _id_key(req_id):
    """A request id's IDENTITY, for the C3-3 reading: domain marker plus value.

    Deliberately identical to `agentskill_evals.mcp_proxy.request_id_key`, and deliberately
    NOT imported from it: this file is a fixture that CLIs spawn directly, in a masked HOME
    with an unrelated cwd, so it must run with nothing but the standard library on the path.
    Duplication of a two-line rule is the lesser evil — but a duplicate that could drift is
    not, so `verify_mcp_fixtures.py` asserts the two agree on the cases that distinguish them
    (`1` vs `"1"`, `1` vs `1.0`, `0` vs `-0.0`).

    Logged as a JSON array, which reparses to a list whose comparison and hash match the
    proxy's tuple once the reader converts it — `("n", 1) == ("n", 1.0)` in Python, which is
    the point of the numeric domain.
    """
    return ["s", req_id] if isinstance(req_id, str) else ["n", req_id]


def _send(msg: dict) -> None:
    # An id is ANSWERED at the instant its response DEPARTS, and C3-3 needs that instant
    # relative to the arrivals `_announce` records: a repeat before it is a live duplicate,
    # which JSON-RPC already forbids and the proxy refuses as `duplicate_request_id`, while a
    # repeat after it is the post-response reuse the spec PERMITS and §10.4 refuses. Only the
    # second one prices that rule, and a probe that cannot tell them apart reported the first
    # as though it did (review, PR #100).
    #
    # LOGGED AFTER THE WRITE SUCCEEDS, which is not a detail: C3-1 established that agy is
    # gone by the time the graceful closure is written, so this call raises `BrokenPipeError`
    # and nothing departs. Logging first recorded an answer for a response no client ever saw,
    # and a subsequent request on that id would then read as post-response reuse against a
    # response that does not exist (review, PR #100).
    os.write(1, (json.dumps(msg) + "\n").encode())
    # ONLY FOR AN ID THAT ENTERED THE TIMELINE. This shim answers malformed requests, as a
    # conformant server should — with an error — but that response must not be recorded,
    # because the request it answers was refused admission. Recording it marked the id
    # "answered" with no matching arrival, so a single later valid request on that id read as
    # post-response reuse: a repeat manufactured out of one request (review, PR #100).
    if ("result" in msg or "error" in msg) and _valid_request_id(msg.get("id")) \
            and tuple(_id_key(msg["id"])) in _admitted:
        _log("response_id", id_key=_id_key(msg["id"]))


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


def _observe(msg: dict, method, *, version=None, requested=None) -> None:
    """Record what era an ACCEPTED request spoke. Telemetry only — it gates nothing.

    `version` overrides what the request claimed, and legacy `initialize` passes the
    SELECTED version here. Recording the requested one would put the client's guess in
    C3-0's load-bearing column: ask for `2099-01-01`, get `2025-11-25` in the reply, and
    the table would still have said 2099. What the two parties ended up speaking is the
    measurement; what one of them opened with is context, kept as `requested`.

    The first observation is what C3-0's table reports. Later *distinct* ones are logged
    too rather than dropped: a client mixing eras is a finding, and a recorder that kept
    only the first would hide exactly that."""
    global _first_era
    era, claimed, how = _attempt(msg, method)
    if era is None:
        return
    version = claimed if version is None else version
    extra = {} if requested is None else {"requested": requested}
    key = (era, version)
    if _first_era is None:
        _first_era = key
        _seen_eras.add(key)
        _log("era", era=era, version=version, decided_by=how, first_method=method, **extra)
    elif key not in _seen_eras:
        _seen_eras.add(key)
        _log("era_also", era=era, version=version, decided_by=how, method=method, **extra)


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
        if MODE == "legacy":
            # FIRST, before any modern-specific check. A legacy-only server must never
            # emit a RECOGNIZED MODERN ERROR: the dual-era probe treats one as proof the
            # server is modern and explicitly does not fall back, so answering -32022 here
            # would misidentify this server and strand the client.
            _log("violation", why="modern request to a legacy-only server", method=method)
            _error(req_id, -32601, f"method not found: {method}")
            return True
        missing = [k for k in (VER_KEY, CAP_KEY) if k not in meta]
        if missing:
            # Both protocolVersion and clientCapabilities are required on every modern
            # request, and the spec names the code: "A request missing any required field
            # is malformed; the server MUST reject it with -32602".
            _log("violation", why="modern request missing required _meta", method=method,
                 missing=missing)
            _error(req_id, -32602, f"missing required _meta: {', '.join(missing)}")
            return True
        claimed, caps = meta[VER_KEY], meta[CAP_KEY]
        if not isinstance(claimed, str) or not isinstance(caps, dict):
            # Present but the wrong type is malformed, not unsupported. Answering -32022
            # for a numeric version would assert the server understood a version request
            # it never actually received.
            _log("violation", why="modern _meta field of the wrong type", method=method,
                 version_type=type(claimed).__name__, caps_type=type(caps).__name__)
            _error(req_id, -32602, "malformed _meta: protocolVersion must be a string and "
                                   "clientCapabilities an object")
            return True
        if claimed != MODERN_VERSION:
            _log("violation", why="unsupported protocol version", method=method,
                 claimed=claimed)
            _send({"jsonrpc": "2.0", "id": req_id, "error": {
                "code": -32022, "message": "Unsupported protocol version",
                "data": {"supported": [MODERN_VERSION], "requested": claimed}}})
            return True
        if method == "initialize":
            # `initialize` does not exist in the modern revision. Serving it because the
            # method name looks familiar would derive the era from the method, which is
            # the rule this file exists to enforce the other way round.
            _log("violation", why="initialize carrying modern metadata", method=method)
            _error(req_id, -32601, "method not found: initialize")
            return True
        return False

    if method == "initialize":
        if MODE == "modern":
            _log("violation", why="initialize to a modern-only server", method=method)
            _error(req_id, -32601, "method not found: initialize")
            return True
        if _legacy is not None:
            # Legacy initialization happens once per connection. Accepting a second one
            # would let a client renegotiate the version mid-stream — and every bare
            # message already sent would retroactively belong to a different revision
            # than the one it was read under.
            _log("violation", why="initialize after legacy is already initialized",
                 method=method, negotiated=_legacy["version"])
            _error(req_id, -32600, "already initialized")
            return True
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        bad = [k for k, t in (("protocolVersion", str), ("capabilities", dict),
                              ("clientInfo", dict)) if not isinstance(params.get(k), t)]
        if bad:
            _log("violation", why="legacy initialize missing required params",
                 method=method, missing=bad)
            _error(req_id, -32602, f"initialize requires {', '.join(bad)}")
            return True
        return False

    if method in MODERN_ONLY_METHODS:
        # A modern-only method arriving with no modern metadata. A legacy server has never
        # heard of it, and answering on the strength of the familiar name would be reading
        # the era off the method — the inversion this whole function exists to prevent.
        _log("violation", why="modern-only method without modern metadata", method=method)
        _error(req_id, -32601, f"method not found: {method}")
        return True

    # Neither modern metadata nor `initialize`: legal only once `initialize` has selected
    # legacy semantics for this process.
    if _legacy is None:
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


def _initialize(params: dict):
    """Select a version, store the legacy state it establishes, and return (payload,
    selected, requested). Selection happens BEFORE the caller records the era, so the
    telemetry carries what was agreed rather than what was asked for."""
    global _legacy
    requested = params.get("protocolVersion")
    selected = requested if requested in LEGACY_VERSIONS else LEGACY_VERSIONS[0]
    _legacy = {"version": selected,
               "capabilities": params.get("capabilities"),
               "clientInfo": params.get("clientInfo")}
    _log("legacy_initialize", requested=requested, selected=selected,
         supported=list(LEGACY_VERSIONS), downgraded=selected != requested)
    return {
        "protocolVersion": selected,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": "probe-era", "version": "1.0.0"},
    }, selected, requested


def _open_subscription(req_id, params: dict, modern: bool) -> None:
    """Acknowledge first, per spec: `notifications/subscriptions/acknowledged` MUST be the
    first message CARRYING THIS SUBSCRIPTION'S ID. Ordering is scoped per subscription and
    not per channel — on stdio, other subscriptions' messages may legitimately interleave
    ahead of it. The subscription id IS the listen request's id. The request itself stays
    open; its JSON-RPC response is the graceful-closure signal, sent at shutdown, not now."""
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


_STDIN = 0
_rx_lock = threading.Lock()
_rx_ready = threading.Condition(_rx_lock)
_rx_lines: collections.deque = collections.deque()   # complete lines, in ARRIVAL order
_rx_partial = b""
_rx_eof = False
_rx_failed = False   # the reader DIED; distinct from the client closing stdin


def _announce(raw: bytes) -> None:
    """Log one arrival, at the moment its bytes land. Called only from the reader thread.

    THE ORDER IS THE WHOLE POINT. §10.4's spent-id rule is a statement about the order messages
    cross the stream, because that is all a proxy sitting in the stream can see — and this shim
    is standing where the proxy would stand.

    ONLY A CONTINUOUS READER CAN OBSERVE THAT ORDER, which took two attempts to get right. The
    first logged arrivals from the main loop, i.e. in PROCESSING order. The second moved the
    logging into `_fill` — better, but `_fill` was still called on demand, so outside the held
    `initialize` window the shim answered the current request before reading again: two
    requests that both crossed the wire before the response was written were logged as
    `req, resp, req`, which reads as post-response reuse when it is a live duplicate (review,
    PR #100). Nothing that reads on demand can see when bytes it has not asked for arrived. So
    input is drained continuously by `_reader`, and the main loop consumes from the queue it
    fills.

    MALFORMED TRAFFIC DOES NOT ENTER C3-3, and "malformed" means the PROXY'S whole envelope
    taxonomy — see `_envelope_shape`. Narrower versions kept letting a proxy-fatal frame read
    as ordinary traffic: checking only the id admitted a JSON-RPC 1.0 request, and checking
    only request-shaped frames left batches and malformed NOTIFICATIONS unmarked. Before those,
    a list id crashed the reader while `true` followed by `1` manufactured a reuse Python sees
    and JSON-RPC does not (review, PR #100). Anything rejected is logged as its own event —
    a finding about the client, and a terminal marker, since the proxy dies there.
    """
    if not raw.strip():
        return
    try:
        msg = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        _log("request_id_malformed", why="unparseable")
        return
    if isinstance(msg, list):
        # A JSON-RPC BATCH. MCP forbids it on stdio and the proxy fails on it, so it is
        # terminal here too — it was producing no marker at all, being neither parseable-
        # request-shaped nor unparseable (review, PR #100).
        _log("request_id_malformed", why="batch", count=len(msg))
        return
    shape = _envelope_shape(msg)
    if shape is None:
        # ANY envelope the proxy would refuse, whatever its shape. A malformed NOTIFICATION
        # carries no id and so slipped past a request-shaped check, while the proxy dies on it
        # exactly as it dies on a malformed request.
        _log("request_id_malformed", why="envelope",
             id_repr=repr(msg.get("id")) if isinstance(msg, dict) else None,
             method=str(msg.get("method"))[:120] if isinstance(msg, dict) else None,
             jsonrpc=repr(msg.get("jsonrpc")) if isinstance(msg, dict) else None)
    elif shape == "request":
        _admitted.add(tuple(_id_key(msg["id"])))
        _log("request_id", id_key=_id_key(msg["id"]), method=msg["method"])
    # A well-formed notification or response is ordinary traffic with no place in an id
    # timeline, and no finding either.


def _reader() -> None:
    """Drain stdin forever, announcing each line as it arrives. Daemon thread.

    A thread rather than `select` in the main loop because the main loop is not always in a
    position to select: it spends time formatting and writing responses, and any bytes that
    arrive during that window are invisible until it next asks for them. That window is
    exactly where a live duplicate and a post-response reuse become indistinguishable.
    """
    global _rx_partial, _rx_eof, _rx_failed
    while True:
        try:
            chunk = os.read(_STDIN, 65536)
        except (OSError, ValueError) as exc:
            # A FAILED READ IS NOT A CLOSED STDIN. Swallowing it into `chunk = b""` made the
            # main loop log `stdin_eof` and a clean terminator, so an instrument failure would
            # have been published as "this CLI shut the server down cleanly" — the exact
            # conclusion C3-1 exists to draw, drawn from a bug in the thing drawing it
            # (review, PR #100).
            _log("reader_error", error=type(exc).__name__, detail=str(exc)[:200])
            with _rx_ready:
                _rx_failed = True
                _rx_eof = True
                _rx_ready.notify_all()
            return
        if not chunk:
            with _rx_ready:
                # A trailing fragment at EOF is all of that line there will ever be.
                if _rx_partial.strip():
                    _announce(_rx_partial)
                    _rx_lines.append(_rx_partial)
                    _rx_partial = b""
                _rx_eof = True
                _rx_ready.notify_all()
            return
        with _rx_ready:
            _rx_partial += chunk
            parts = _rx_partial.split(b"\n")
            _rx_partial = parts.pop()
            for raw in parts:
                _announce(raw)
                _rx_lines.append(raw)
            _rx_ready.notify_all()


def _readline() -> str:
    """One line from stdin, or "" at EOF.

    READS FROM THE ARRIVAL QUEUE, not the fd. `sys.stdin` was wrong here for a reason worth
    keeping: a TextIOWrapper (and the BufferedReader under it) pulls a chunk rather than a
    line, so a pipelined request could sit in Python's buffer while `select` reported the pipe
    quiet — which would make the C3-2 measurement report "no pipelining" for a client that
    pipelines. The reader thread owns the only buffer, so "has anything arrived?" stays
    answerable, and now it is answerable at the instant of arrival rather than on demand.
    """
    with _rx_ready:
        while not _rx_lines and not _rx_eof:
            _rx_ready.wait()
        if _rx_lines:
            return _rx_lines.popleft().decode("utf-8", "replace") + "\n"
        return ""


def _buffered_lines() -> list[str]:
    """Complete lines already queued, without consuming them."""
    with _rx_lock:
        return [ln.decode("utf-8", "replace") for ln in _rx_lines if ln.strip()]


def _measure_pipelining(req_id) -> None:
    """C3-2: hold the `initialize` response and record whatever the client sends meanwhile.

    A client that waits sends nothing here. A client that pipelines has already written its
    next request, so it lands in the queue during the window. This is the only vantage point
    from which the two differ — see the module docstring.

    THE QUEUE IS READ ONCE, AT THE CLOSE, and everything still in it counts. The main loop has
    popped exactly the `initialize` line by now, so anything else sitting there was written by
    the client before this response was — which is the definition of pipelining. An earlier
    version of this function snapshotted the queue length at the window's open and counted only
    what arrived after, which is right for an on-demand reader and wrong for a continuous one:
    a client that pipelines aggressively has its next request queued before the window even
    opens, and that is the strongest possible evidence, not a reason to discard it.
    """
    deadline = time.monotonic() + INIT_DELAY_MS / 1000.0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(remaining, 0.01))
    arrived = _buffered_lines()
    methods = []
    for raw in arrived:
        try:
            methods.append(json.loads(raw).get("method"))
        except (json.JSONDecodeError, ValueError):
            methods.append("<unparseable>")
    _log("pipelining", initialize_id=req_id, held_ms=INIT_DELAY_MS,
         pipelined=bool(arrived), count=len(arrived), methods=methods)


def main() -> int:
    _log("start", pid=os.getpid(), mode=MODE, ignore_sigterm=IGNORE_SIGTERM,
         init_delay_ms=INIT_DELAY_MS)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):  # not all signals settable everywhere
            pass
    # Daemon, so a shim that is killed or exits via os._exit() in a signal handler is never
    # held open by it — C3-1's whole subject is how this process dies.
    threading.Thread(target=_reader, daemon=True).start()

    while True:
        # Our own raw reader rather than `sys.stdin`: every layer above the fd reads ahead
        # into a buffer this file cannot inspect, and C3-2 is precisely a question about what
        # is in that buffer.
        line = _readline()
        if line == "":
            # C3-1 reads the terminator as a verdict about the CLIENT, so an instrument failure
            # must never wear the clean-shutdown label (review, PR #100).
            _log("reader_failed" if _rx_failed else "stdin_eof")
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
        # The C3-3 `request_id` record is NOT written here. It is written by `_announce` when
        # the line arrives, which is as close to wire order as this process can get; this loop
        # runs in processing order, which is further away still — see `_announce`.
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

        # Everything below is REACHABLE ONLY IF _reject() passed it, so the mode and era
        # rules live there and nowhere else. The MODE checks that used to sit in these two
        # branches are gone rather than kept "for safety": they were unreachable, and the
        # same rule stated in two places is the kind that drifts apart silently.
        if method == "server/discover":
            _observe(msg, method)
            _result(req_id, _discover(), modern=is_modern, cacheable=True)
        elif method == "initialize":
            payload, selected, requested = _initialize(params)
            _observe(msg, method, version=selected, requested=requested)
            # C3-2 runs BETWEEN deciding the answer and sending it — that window is the whole
            # measurement, and it is the only place a client's pipelining is observable.
            if INIT_DELAY_MS > 0:
                _measure_pipelining(req_id)
            _result(req_id, payload, modern=False)
        elif method == "subscriptions/listen":
            _observe(msg, method)
            _open_subscription(req_id, params, is_modern)
        elif method == "tools/list":
            _observe(msg, method)
            _result(req_id, {"tools": TOOLS}, modern=is_modern, cacheable=True)
        elif method == "tools/call":
            _observe(msg, method)
            _result(req_id, {"content": [{"type": "text", "text": "ok"}], "isError": False},
                    modern=is_modern)
        elif method == "ping":
            _observe(msg, method)
            _result(req_id, {}, modern=is_modern)
        else:
            _log("refused", method=method, mode=MODE)
            _error(req_id, -32601, f"method not found: {method}")

    reason = "reader_failed" if _rx_failed else "stdin_eof"
    _close_subscriptions(reason)
    _log("terminator", reason=reason)
    # A NON-ZERO EXIT, not just a differently-worded log line. C3-1 reads the terminator as a
    # verdict about the client, and anything driving this shim — the verifier, a probe, a CI
    # step — checks the exit status first. Logging the failure and then exiting 0 leaves the
    # instrument saying "clean" in the one place most callers look (review, PR #100).
    return 1 if _rx_failed else 0


if __name__ == "__main__":
    path = os.environ.get("PROBE_MCP_LOG")
    if path:
        _log_fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        sys.exit(main())
    except (BrokenPipeError, KeyboardInterrupt):
        _log("terminator", reason="broken_pipe")
        sys.exit(0)
