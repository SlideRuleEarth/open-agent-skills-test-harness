"""C3 — the harness-owned filtering proxy: DECISIONS.

`DESIGN_MCP_Support.md` §10 is the specification; this module is its decision layer, and
nothing here does I/O. The transport half — spawning the declared server, the two pumps, the
signal handlers, the coordinated shutdown of §10.5, the audit log — lands beside it and calls
into these functions.

WHY THE SPLIT, since it is not the obvious shape for a program whose whole job is to sit in a
pipe. `tools/mutate_mcp.py` proves arms by running the SELFTEST, so logic reachable only by
driving the real program over real pipes gets no mutation coverage at all — the gap already
recorded for `fixtures/`. This is code in the request path of every gated cell, where §10.5's
standing rule is that a bug is a silently wrong eval rather than a loud failure, so every
decision that could be silently wrong is a pure function an arm can call and a mutation can
break. What is left in the I/O layer is sequencing, which a wire-level driver can observe.

THE ONE RULE EVERYTHING HERE SERVES (§10.5):

    The proxy never degrades. Anything it cannot handle with certainty becomes a failed
    cell, never unfiltered traffic.

So every function below is written to answer "may this be forwarded?" with a THIRD option
besides yes and no — `Anomaly` — and callers are expected to treat that as terminal. There is
deliberately no "warn and continue" return anywhere in this file.

WHAT IS NOT HERE, on purpose:

  * No tool-shape scanning. Tool definitions are checked at their DEFINED LOCATIONS per
    implemented version (§10.6) — `result.tools` on a `tools/list` response, and `params.tools`
    on sampling — because `Result` carries an index signature (`[key: string]: unknown`) and a
    structural scan is unsound in both directions: it fails legitimate extension payloads, and
    it misses any future revision that conveys definitions in a new shape. The thing that
    actually closes the future-channel hole is the version gate below, not a heuristic.
  * No caching of a filtered tool list. `toolsListChanged` lets a server prompt
    re-enumeration whenever it likes and copilot issues `tools/list` twice a session, so every
    response is filtered on its own merits (§10.4).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------------------

# The versions this proxy IMPLEMENTS — not "the versions that look modern". Probe C3-0
# measured the shipped fleet split three ways (§9): claude and copilot on 2025-11-25, codex
# on 2025-06-18, agy already on the modern 2026-07-28. Anything outside this set is an
# anomaly, and that gate — not any payload inspection — is what makes a future revision fail
# CLOSED on arrival. A revision that adds a new tool-bearing channel is refused because
# nobody has read it, which is the honest reason.
MODERN_VERSIONS = ("2026-07-28",)
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18")
IMPLEMENTED_VERSIONS = MODERN_VERSIONS + LEGACY_VERSIONS

# Reserved `_meta` keys. Modern MCP dropped the `initialize` handshake: protocol version and
# client capabilities ride on EVERY request. Both are required — the spec says a request
# missing either "is malformed; the server MUST reject it with -32602" — so a proxy policing
# only the version would forward traffic a conforming server is obliged to refuse.
# `clientInfo` and `logLevel` are optional and carry no such obligation.
VER_KEY = "io.modelcontextprotocol/protocolVersion"
CAP_KEY = "io.modelcontextprotocol/clientCapabilities"

# Methods that exist only in the modern revision. A CATEGORY, not a special case (§10.2):
# naming one and not the other is exactly how the probe shim came to answer a bare
# `subscriptions/listen` under legacy semantics that have no such method. `subscriptions/
# listen` is 2026-07-28's replacement for legacy `resources/subscribe`.
MODERN_ONLY_METHODS = ("server/discover", "subscriptions/listen")

# JSON-RPC error codes the proxy itself originates. -32601 for an off-list tool is
# deliberate: from the client's side the tool genuinely does not exist on this connection,
# which is what the allowlist means, and inventing a private code would tell a CLI nothing it
# could act on.
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602


# ---------------------------------------------------------------------------------------
# Anomalies
# ---------------------------------------------------------------------------------------

# Every way the proxy can refuse to be certain, as named kinds rather than free text. They
# are separate because the audit log has to say WHICH — §10.4's worked example is an `id`-less
# error response, which is schema-valid (the schema declares `id?: RequestId`) and so must not
# be diagnosed as a malformed envelope: doing that sends whoever reads the log hunting a
# framing bug that does not exist, while hiding a genuine server-side error the server was
# trying to report. Same verdict, different diagnosis.
UNPARSEABLE = "unparseable"                      # not JSON at all
MALFORMED = "malformed"                          # JSON, but not a legal MCP envelope
BATCH = "batch"                                  # a JSON-RPC array; illegal on MCP stdio
UNCORRELATED = "uncorrelated"                    # response to an id never requested this way
UNIMPLEMENTED_VERSION = "unimplemented_version"  # well-formed, modern, unread revision
MISSING_META = "missing_meta"                    # modern request without a required key
SERVER_REQUEST_IN_MODERN = "server_request_in_modern_era"
TOOL_BEARING_SAMPLING = "tool_bearing_sampling"  # a definition outside tools/list (§10.6)
BAD_TOOLS_RESULT = "bad_tools_result"            # tools/list result not shaped as expected
SECOND_INITIALIZE = "second_initialize"          # lifecycle violation, not renegotiation


@dataclass(frozen=True)
class Anomaly:
    """A refusal to be certain. Terminal by contract: the caller stops forwarding.

    Carries `kind` for the audit log's diagnosis and `detail` for a human. It is a VALUE
    rather than an exception because most of these arise inside decisions that also have
    ordinary answers, and a function returning `Anomaly | Something` forces the caller to
    look — where an exception can be swallowed by a `except Exception` three frames up, which
    is exactly how a proxy would come to degrade instead of failing.
    """

    kind: str
    detail: str = ""

    def __bool__(self) -> bool:
        # Deliberately truthy — `if isinstance(x, Anomaly)` is the intended test, and a
        # falsy anomaly would make `if not result:` read as "no anomaly" for the one value
        # that most needs noticing.
        return True


# ---------------------------------------------------------------------------------------
# Envelope validation (§10.4)
# ---------------------------------------------------------------------------------------

REQUEST = "request"
NOTIFICATION = "notification"
RESULT = "result"
ERROR = "error"


def parse_line(line: str) -> dict | Anomaly:
    """One wire line to a JSON object, or an anomaly.

    Two refusals that are not the same thing. Unparseable bytes are `UNPARSEABLE`. A JSON
    ARRAY is `BATCH` and is refused as a conformance matter, not a v1 shortcut: MCP's stdio
    binding says "Each message is a single JSON-RPC request, notification, or response", so a
    batch is not a legal message on this transport at all. Anything else non-object is
    malformed.
    """
    try:
        msg = json.loads(line)
    except (json.JSONDecodeError, ValueError) as exc:
        return Anomaly(UNPARSEABLE, f"line is not JSON: {exc}")
    if isinstance(msg, list):
        return Anomaly(BATCH, "JSON-RPC batch: an array is not a legal MCP stdio message")
    if not isinstance(msg, dict):
        return Anomaly(MALFORMED, f"top-level {type(msg).__name__}, not an object")
    return msg


def classify_envelope(msg: dict) -> str | Anomaly:
    """Which of the four JSON-RPC shapes this is — or why it is none of them.

    PARSEABLE JSON IS NOT A VALID MCP MESSAGE, and the difference is load-bearing. The naive
    test — "no `id` means notification" — is the bug this function exists to prevent: a
    malformed RESPONSE that lost its `id` matches it, and would be forwarded verbatim down
    the never-answer path, unfiltered and unrecorded. So the shape is established positively,
    by requiring EXACTLY ONE of the four forms.

    `error` responses are the subtle case: the schema declares `id?: RequestId`, so an
    `id`-less error is schema-valid and must classify as an error rather than as malformed.
    Whether it can be correlated is a separate question, answered by the caller (§10.4) — and
    answered with a different anomaly kind, `UNCORRELATED`, precisely so the log distinguishes
    "the server sent a broken frame" from "the server reported an error the proxy cannot
    account for".
    """
    if msg.get("jsonrpc") != "2.0":
        return Anomaly(MALFORMED, f"jsonrpc field is {msg.get('jsonrpc')!r}, not '2.0'")

    has_method = "method" in msg
    has_id = "id" in msg
    has_result = "result" in msg
    has_error = "error" in msg

    if has_result and has_error:
        return Anomaly(MALFORMED, "carries both `result` and `error`")
    if has_method and (has_result or has_error):
        return Anomaly(MALFORMED, "carries `method` alongside a response payload")

    if has_method:
        if not isinstance(msg["method"], str) or not msg["method"]:
            return Anomaly(MALFORMED, "`method` is not a non-empty string")
        # A null id is not an absent id. JSON-RPC 2.0 reserves `"id": null` for a response to
        # a request whose id could not be determined; as a REQUEST it is malformed, and
        # reading it as a notification would put it on the never-answer path.
        if has_id:
            return (Anomaly(MALFORMED, "request `id` is null")
                    if msg["id"] is None else REQUEST)
        return NOTIFICATION

    if has_result:
        return RESULT if has_id else Anomaly(MALFORMED, "result response with no `id`")
    if has_error:
        if not isinstance(msg["error"], dict):
            return Anomaly(MALFORMED, "`error` is not an object")
        return ERROR

    return Anomaly(MALFORMED, "none of method/result/error present")


# ---------------------------------------------------------------------------------------
# Protocol era (§10.2)
# ---------------------------------------------------------------------------------------

_ABSENT = object()


def _meta_of(msg: dict) -> dict:
    params = msg.get("params")
    if not isinstance(params, dict):
        return {}
    meta = params.get("_meta")
    return meta if isinstance(meta, dict) else {}


def has_modern_intent(msg: dict) -> bool:
    """Whether this request is TRYING to be modern.

    EITHER reserved key establishes intent, not just the version. A request carrying
    `clientCapabilities` and no `protocolVersion` is a broken MODERN request — the spec
    obliges a server to reject it with -32602 — and reading it as "not modern, therefore
    legacy" would quietly downgrade it onto the bare-request path. That laundering is a real
    defect the echo fixture shipped with and review caught: absence of modern metadata is not
    legacy, and partial modern metadata is not absence.
    """
    meta = _meta_of(msg)
    return VER_KEY in meta or CAP_KEY in meta


def modern_version(msg: dict) -> str | None | Anomaly:
    """The implemented modern version this request declares.

    Returns None when the request makes no modern claim at all — the caller then reads it
    under whatever legacy state is in force, which is the ONLY thing that may be inferred
    from an absence.

    Everything else fails closed, per request rather than per session. Modern is stateless —
    "servers MUST NOT rely on prior requests over the same connection to establish context" —
    so there is no negotiated state a later request could be judged against, and an OPENING
    request that escaped validation would set the very terms it should have been judged by.
    """
    if not has_modern_intent(msg):
        return None
    meta = _meta_of(msg)
    missing = [k for k in (VER_KEY, CAP_KEY) if k not in meta]
    if missing:
        return Anomaly(MISSING_META,
                       f"modern request missing required _meta {', '.join(missing)}")
    claimed = meta.get(VER_KEY, _ABSENT)
    caps = meta.get(CAP_KEY, _ABSENT)
    # Present-but-wrong-type is malformed, and `null` is not absence: a JSON null passes an
    # `in` test and would sail through a check written as `meta.get(VER_KEY) or ...`.
    if not isinstance(claimed, str) or not claimed:
        return Anomaly(MISSING_META, f"{VER_KEY} is {claimed!r}, not a version string")
    if not isinstance(caps, dict):
        return Anomaly(MISSING_META, f"{CAP_KEY} is {caps!r}, not an object")
    if claimed not in MODERN_VERSIONS:
        # Well-formed, modern-shaped, and unread. This is the gate that stands in for every
        # future revision, and it must stay an anomaly rather than becoming a warning:
        # recording a version while forwarding its traffic is documentation of a failure.
        return Anomaly(UNIMPLEMENTED_VERSION,
                       f"request declares protocol version {claimed!r}; this proxy "
                       f"implements {', '.join(IMPLEMENTED_VERSIONS)}")
    return claimed


def legacy_version(msg: dict) -> str | Anomaly:
    """The version an `initialize` request selects, or why it cannot be honoured.

    The NEGOTIATED VERSION is what gets stored, never a boolean. Later legacy messages carry
    no metadata whatsoever, so the version is the only thing that says which legacy revision
    to read them under — and the fleet spans two of them, which disagree. A proxy that
    recorded merely "legacy is on" could not interpret the traffic that follows.
    """
    params = msg.get("params")
    if not isinstance(params, dict):
        return Anomaly(MALFORMED, "initialize with no params object")
    claimed = params.get("protocolVersion", _ABSENT)
    if not isinstance(claimed, str) or not claimed:
        return Anomaly(MALFORMED, f"initialize protocolVersion is {claimed!r}")
    if claimed not in LEGACY_VERSIONS:
        return Anomaly(UNIMPLEMENTED_VERSION,
                       f"initialize declares {claimed!r}; this proxy implements "
                       f"{', '.join(LEGACY_VERSIONS)} for the legacy era")
    return claimed


def method_matches_era(method: str, *, modern: bool) -> bool:
    """Whether a method may be honoured under the era the REQUEST established.

    THE METHOD MUST MATCH THE REQUEST, NOT THE REVERSE. Reading the era off a familiar method
    name is the exact inversion §10.2 forbids: `initialize` carrying modern `_meta` is an
    unknown method, and a modern-only method arriving with no modern metadata is unknown to
    the legacy semantics in force. Both directions are checked here so neither can be
    answered on the strength of its name.
    """
    if modern:
        return method != "initialize"
    return method not in MODERN_ONLY_METHODS


# ---------------------------------------------------------------------------------------
# Tool filtering (§10.4, §10.6)
# ---------------------------------------------------------------------------------------

def filter_tools_result(result: Any, allowed: frozenset[str]) -> tuple[dict, list[str]]:
    """Strip off-list tools from a `tools/list` result. Returns (result, removed names).

    Raises nothing and returns no anomaly for an unexpected shape — the caller checks that
    first with `tools_result_ok`, because "is this the shape I can filter" and "filter it"
    are different questions and merging them produced a function that silently returned the
    input unchanged when it could not understand it. Forwarding unfiltered on a shape you did
    not recognise is precisely the degradation this design forbids.

    The result is REBUILT rather than mutated: the caller is holding the parsed message it
    will forward, and a filter that edited in place would leave no way to record what the
    server actually said (§10.5 wants the filtered advertisement logged as an expected event,
    not erased).
    """
    tools = result["tools"]
    kept, removed = [], []
    for tool in tools:
        (kept if tool.get("name") in allowed else removed).append(tool)
    out = dict(result)
    out["tools"] = kept
    return out, [t.get("name") for t in removed]


def tools_result_ok(result: Any) -> bool:
    """Whether a `tools/list` result is shaped so the allowlist can actually be applied.

    Every tool must be an object with a string `name`, because a nameless entry cannot be
    compared against the allowlist and therefore cannot be certainly kept OR certainly
    dropped. Keeping it would forward an unnamed advertisement; dropping it would silently
    remove something the server meant. Neither is a decision this proxy is entitled to make,
    so the shape is an anomaly instead.
    """
    if not isinstance(result, dict):
        return False
    tools = result.get("tools")
    if not isinstance(tools, list):
        return False
    return all(isinstance(t, dict) and isinstance(t.get("name"), str) for t in tools)


def sampling_carries_tools(msg: dict) -> bool:
    """A tool definition at a DEFINED LOCATION outside `tools/list` (§10.6).

    Sampling is the concrete counterexample to "`tools:` gates every route to the model": a
    server may put its own `tools` array in a `sampling/createMessage`, scoped to that request
    and not corresponding to anything registered. Legacy carries it as a server-originated
    request; modern carries it inside `InputRequiredResult.inputRequests`.

    This checks the two named locations, and nothing else — it is not a scan. A `tools` key
    that is not a non-empty list is not a definition; the empty list in particular is what a
    capability FLAG looks like, and tripping on it would fail every modern handshake.
    """
    if msg.get("method") == "sampling/createMessage":
        params = msg.get("params")
        if isinstance(params, dict) and _nonempty_list(params.get("tools")):
            return True
    result = msg.get("result")
    if isinstance(result, dict):
        for req in result.get("inputRequests") or []:
            if not isinstance(req, dict):
                continue
            if req.get("method") != "sampling/createMessage":
                continue
            params = req.get("params")
            if isinstance(params, dict) and _nonempty_list(params.get("tools")):
                return True
    return False


def _nonempty_list(value: Any) -> bool:
    return isinstance(value, list) and bool(value)


# ---------------------------------------------------------------------------------------
# Correlation and protocol state (§10.2, §10.4)
# ---------------------------------------------------------------------------------------

class InFlight:
    """Outstanding requests, ONE MAP PER DIRECTION.

    Under legacy, both sides originate requests, so an `id` on a server→client request is not
    an unrequested `id`. A single shared map would either collide two legitimate conversations
    that happen to pick the same id — the two sides number independently — or report one
    direction's response as uncorrelated.

    The map exists so responses can be correlated, which is what makes `tools/list` filtering
    possible at all (a response says only which id it answers, never which method). Pagination
    therefore needs no special case: every page's response correlates to a `tools/list` and is
    filtered on its own merits.
    """

    def __init__(self) -> None:
        self._by_direction: dict[str, dict[Any, str]] = {"c2s": {}, "s2c": {}}

    def _key(self, req_id: Any) -> Any:
        # JSON-RPC ids may be a string or a number, and `1` and `"1"` are different ids. They
        # are also both valid dict keys, but `True == 1` in Python, so a bare id would let a
        # boolean id (illegal, but arriving from a peer this proxy does not control) alias a
        # numeric one. Keying on (type name, value) keeps them apart without rejecting either.
        return (type(req_id).__name__, req_id)

    def record(self, direction: str, req_id: Any, method: str) -> None:
        self._by_direction[direction][self._key(req_id)] = method

    def method_for(self, direction: str, req_id: Any) -> str | None:
        return self._by_direction[direction].get(self._key(req_id))

    def retire(self, direction: str, req_id: Any) -> str | None:
        """Drop an entry and return the method it held.

        Retirement makes the id REUSABLE, which is not a nicety: the subscription id IS the
        JSON-RPC request id, and a client that cancels a subscription on id 1 may legitimately
        use id 1 for the next `ping`. An entry left resident forever would then correlate a
        fresh response against stale state — or, worse, make the proxy treat a legitimate
        response as unrequested and fail a clean cell.
        """
        return self._by_direction[direction].pop(self._key(req_id), None)

    def open_ids(self, direction: str) -> list:
        return [key[1] for key in self._by_direction[direction]]


def cancelled_id(msg: dict) -> Any:
    """The request id a `notifications/cancelled` names, or `_ABSENT`.

    Notifications are forwarded verbatim, as all notifications are — but this one must also be
    OBSERVED on the way through, because it is one of the two orderly ends of a subscription
    (§10.4). The design's earlier claim that notifications need no special treatment is wrong
    for exactly this one.
    """
    if msg.get("method") != "notifications/cancelled":
        return _ABSENT
    params = msg.get("params")
    if not isinstance(params, dict) or "requestId" not in params:
        return _ABSENT
    return params["requestId"]


def is_graceful_closure(method: str | None, msg: dict) -> bool:
    """Whether this response is a subscription's terminal message rather than a reply.

    `subscriptions/listen` gets no immediate answer: its JSON-RPC response IS the closure
    signal, an empty result on the original id. C3-0 found agy opening one with
    `notifications: {toolsListChanged: true}` and streaming through notifications correlated
    by subscription id — so an implementation that treated an unanswered request as a leak,
    a timeout, or an anomaly would fail every agy cell.
    """
    if method != "subscriptions/listen":
        return False
    result = msg.get("result")
    return isinstance(result, dict) and not result


class ProtocolState:
    """What an accepted `initialize` selected, for the life of the stdio process.

    NOT A BOOLEAN, and not a mirror of the modern era. Modern is stateless — it supplies its
    context per request, so "the connection is modern" is not a state this may hold, and
    keeping one flag for both roles makes dispatch order-dependent: a modern request followed
    by an accepted `initialize` would leave legacy disabled and refuse every bare request
    after it.

    `observed` is TELEMETRY and gates nothing (§10.7). It is plural and says *observed* rather
    than "negotiated" because a modern client declares its version per request and may
    legitimately speak more than one era on one connection; a recorder keeping only the first
    would hide precisely the mixed-era client worth knowing about.
    """

    def __init__(self) -> None:
        self.legacy_version: str | None = None
        self.legacy_capabilities: dict | None = None
        self.observed: list[str] = []

    def observe(self, version: str) -> None:
        if version not in self.observed:
            self.observed.append(version)

    def accept_initialize(self, version: str, capabilities: Any) -> None | Anomaly:
        """Select legacy semantics, or refuse a second attempt.

        A second `initialize` is a LIFECYCLE VIOLATION, not a renegotiation. Accepting one
        would retroactively move every message already exchanged to a revision it was not read
        under — the proxy would have filtered and forwarded traffic under one set of rules and
        then claimed another applied.
        """
        if self.legacy_version is not None:
            return Anomaly(SECOND_INITIALIZE,
                           f"second `initialize` (already negotiated {self.legacy_version}); "
                           f"the lifecycle allows one per connection")
        self.legacy_version = version
        self.legacy_capabilities = capabilities if isinstance(capabilities, dict) else {}
        self.observe(version)
        return None


def refusal_for(req_id: Any, name: str, server: str) -> dict:
    """The error the client gets for an off-list `tools/call`.

    The call never reaches the server, so this is the proxy speaking — and it says the tool
    is not found rather than inventing a policy code, because on this connection that is
    simply true: the allowlist IS the tool surface. The message names the harness so nobody
    debugs a server that never saw the request.
    """
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {
            "code": ERR_METHOD_NOT_FOUND,
            "message": (f"tool {name!r} is not in the declared `tools:` allowlist for MCP "
                        f"server {server!r} — refused by the agentskill-evals proxy; the "
                        f"server was not contacted"),
        },
    }
