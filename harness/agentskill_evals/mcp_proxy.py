"""C3 — the harness-owned filtering proxy: DECISIONS.

`DESIGN_MCP_Support.md` §10 is the specification; this module is its decision layer, and
nothing here does I/O. The transport half — spawning the declared server, the two pumps, the
signal handlers, the coordinated shutdown of §10.5, the audit log — lands beside it and calls
`decide()` once per line.

WHY THE SPLIT, since it is not the obvious shape for a program whose whole job is to sit in a
pipe. `tools/mutate_mcp.py` proves arms by running the SELFTEST, so logic reachable only by
driving the real program over real pipes gets no mutation coverage at all — the gap already
recorded for `fixtures/`. This is code in the request path of every gated cell, where §10.5's
standing rule is that a bug is a silently wrong eval rather than a loud failure, so every
decision that could be silently wrong is a pure function an arm can call and a mutation can
break. What is left in the I/O layer is reads, writes, spawning and shutdown SEQUENCING —
things a wire-level driver can observe.

WHERE THE SPLIT IS DRAWN, which review had to correct once. An earlier cut of this module
exported only predicates and formatters and left the combining to the I/O half, which put the
load-bearing step — "may this line be forwarded?" — back on the side no mutation can reach.
An arm calling `refusal_for()` proves a message is well worded; it proves nothing about
whether an off-list call is ever refused. `decide()` is therefore the entry point, it returns
an ACTION rather than advice, and the I/O layer's whole contract is to perform that action
and nothing else.

THE ONE RULE EVERYTHING HERE SERVES (§10.5):

    The proxy never degrades. Anything it cannot handle with certainty becomes a failed
    cell, never unfiltered traffic.

So `decide()` has a THIRD outcome besides forward and refuse — `Fail`, carrying an `Anomaly`
— and callers are expected to treat it as terminal. There is deliberately no "warn and
continue" return anywhere in this file.

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
  * No "no allowlist" mode. `allowed` is always a real set: a server that declares no `tools:`
    is not proxied at all (§10.1), so there is no configuration in which this code is asked to
    pass everything, and none in which a bug could quietly become one.
"""
from __future__ import annotations

import json
import math
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
# `clientInfo` and `logLevel` are optional and carry no such obligation. `subscriptionId`
# correlates every message on a `subscriptions/listen` stream, including the response that
# closes it.
VER_KEY = "io.modelcontextprotocol/protocolVersion"
CAP_KEY = "io.modelcontextprotocol/clientCapabilities"
SUB_KEY = "io.modelcontextprotocol/subscriptionId"

# Methods that exist only in the modern revision. A CATEGORY, not a special case (§10.2):
# naming one and not the other is exactly how the probe shim came to answer a bare
# `subscriptions/listen` under legacy semantics that have no such method. `subscriptions/
# listen` is 2026-07-28's replacement for legacy `resources/subscribe`.
MODERN_ONLY_METHODS = ("server/discover", "subscriptions/listen")

# The only requests a legacy client may send before its `initialize` has been answered:
# "the client SHOULD NOT send requests other than pings before the server has responded to
# the initialize request". Everything else needs an era that was actually established —
# without modern `_meta` and without a negotiated version, NO implemented version governs the
# request, which is the version gate defeated by simply never handshaking (review, PR #100).
PRE_INITIALIZE_METHODS = ("initialize", "ping")

# JSON-RPC error codes the proxy itself originates. -32601 for an off-list tool is
# deliberate: from the client's side the tool genuinely does not exist on this connection,
# which is what the allowlist means, and inventing a private code would tell a CLI nothing it
# could act on.
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602

# Direction labels. A response travels the OPPOSITE way from the request it answers, which is
# why these are named rather than spelled inline at every call site.
C2S = "c2s"
S2C = "s2c"
_ORIGIN_OF = {C2S: S2C, S2C: C2S}


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
DUPLICATE_ID = "duplicate_request_id"            # a second live request on an id still open
UNIMPLEMENTED_VERSION = "unimplemented_version"  # well-formed, modern, unread revision
MISSING_META = "missing_meta"                    # modern request without a required key
ERA_METHOD_MISMATCH = "era_method_mismatch"      # method belongs to the other era
NO_ERA_ESTABLISHED = "no_era_established"        # bare request, no version governs it
SERVER_REQUEST_IN_MODERN = "server_request_in_modern_era"
TOOL_BEARING_SAMPLING = "tool_bearing_sampling"  # a definition outside tools/list (§10.6)
BAD_INPUT_REQUESTS = "bad_input_requests"        # InputRequiredResult not shaped as a map
BAD_TOOLS_RESULT = "bad_tools_result"            # tools/list result not shaped as expected
BAD_SUBSCRIPTION_CLOSURE = "bad_subscription_closure"
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


def valid_request_id(value: Any) -> bool:
    """Whether this is a legal MCP `RequestId`: a string or a number, and nothing else.

    Not a pedantic schema check. Correlation keys on the id, so a list or an object id
    reaches `dict.__setitem__` and raises `TypeError: unhashable` — inside a pump, several
    frames from anything that knows what to do about it. Rejecting it here makes it an
    ordinary malformed envelope with an audit-log entry instead of a crash.

    A STRING OR A NUMBER, not a string or an integer. The schema — declared the source of
    truth — says `RequestId = string | number`, and JSON-RPC says fractional parts "SHOULD
    NOT" be used, which is discouragement rather than prohibition. `1.5` is therefore a valid
    if unfashionable id, and refusing it would fail a conforming client over style (review,
    PR #100; the prose on the basic page says "integer" and the first version followed it).

    Booleans are excluded EXPLICITLY because `isinstance(True, int)` is true in Python: a
    boolean is not a legal id, and it is also the one value that would silently alias `1`.
    Non-finite floats are excluded because `NaN` and `Infinity` are not JSON at all (RFC
    8259) — Python's parser accepts them anyway, so they arrive here — and because a `NaN` id
    never compares equal to itself, so it could never be retired or correlated.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, str) or isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _error_object_ok(err: Any) -> bool:
    """`error` must carry an integer `code` and a string `message`; `data` is free.

    An error response missing either is not something to forward on the strength of having an
    `error` key — the client would be handed a reply it cannot interpret, in answer to a
    request the proxy did forward.
    """
    if not isinstance(err, dict):
        return False
    code = err.get("code")
    if not isinstance(code, int) or isinstance(code, bool):
        return False
    return isinstance(err.get("message"), str)


def classify_envelope(msg: dict) -> str | Anomaly:
    """Which of the four JSON-RPC shapes this is — or why it is none of them.

    PARSEABLE JSON IS NOT A VALID MCP MESSAGE, and the difference is load-bearing. The naive
    test — "no `id` means notification" — is the bug this function exists to prevent: a
    malformed RESPONSE that lost its `id` matches it, and would be forwarded verbatim down
    the never-answer path, unfiltered and unrecorded. So the shape is established positively,
    by requiring EXACTLY ONE of the four forms and by checking the fields the schema makes
    mandatory: `id` is a string or integer, `params` and `result` are objects, and an `error`
    carries an integer `code` and a string `message`.

    `error` responses are the subtle case: the schema declares `id?: RequestId`, so an
    `id`-less error is schema-valid and must classify as an error rather than as malformed.
    Whether it can be correlated is a separate question, answered by `decide()` — and
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
        if "params" in msg and not isinstance(msg["params"], dict):
            return Anomaly(MALFORMED, f"`params` is {type(msg['params']).__name__}, "
                                      f"not an object")
        # A null id is not an absent id, and neither is a boolean or a list. JSON-RPC 2.0
        # reserves `"id": null` for a response to a request whose id could not be determined;
        # as a REQUEST it is malformed, and reading it as a notification would put it on the
        # never-answer path.
        if has_id:
            return (REQUEST if valid_request_id(msg["id"])
                    else Anomaly(MALFORMED, f"request `id` is {msg['id']!r}, "
                                            f"not a string or number"))
        return NOTIFICATION

    if has_result:
        if not has_id:
            return Anomaly(MALFORMED, "result response with no `id`")
        if not valid_request_id(msg["id"]):
            return Anomaly(MALFORMED, f"response `id` is {msg['id']!r}, "
                                      f"not a string or number")
        if not isinstance(msg["result"], dict):
            return Anomaly(MALFORMED, f"`result` is {type(msg['result']).__name__}, "
                                      f"not an object")
        return RESULT
    if has_error:
        if not _error_object_ok(msg["error"]):
            return Anomaly(MALFORMED, "`error` is not an object with an integer `code` "
                                      "and a string `message`")
        if has_id and not valid_request_id(msg["id"]):
            return Anomaly(MALFORMED, f"error `id` is {msg['id']!r}, "
                                      f"not a string or number")
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


def proposed_version(msg: dict) -> str | Anomaly:
    """The version an `initialize` REQUEST proposes. Shape only — deliberately not gated.

    A proposal governs nothing. The lifecycle says the client sends the latest version it
    supports and "if the server supports the requested protocol version, it MUST respond with
    the same version. Otherwise, the server MUST respond with another protocol version it
    supports" — so what the following traffic is read under is the SERVER's answer, and
    `negotiated_version` is where the implemented-version gate lives.

    Gating here as well would fail closed on a conversation that was about to succeed: a
    client proposing a legacy revision this proxy has not read, answered by a server offering
    one it has, is an ordinary negotiation and not an anomaly. Review caught this modelled the
    wrong side of the handshake.
    """
    params = msg.get("params")
    if not isinstance(params, dict):
        return Anomaly(MALFORMED, "initialize with no params object")
    claimed = params.get("protocolVersion", _ABSENT)
    if not isinstance(claimed, str) or not claimed:
        return Anomaly(MALFORMED, f"initialize protocolVersion is {claimed!r}")
    return claimed


def negotiated_version(result: Any) -> str | Anomaly:
    """The version the server's `initialize` RESULT selects, or why it cannot be honoured.

    THE NEGOTIATED VERSION IS WHAT GETS STORED, never a boolean and never the proposal. Later
    legacy messages carry no metadata whatsoever, so this is the only thing that says which
    legacy revision to read them under — and the fleet spans two of them, which disagree. A
    proxy that recorded merely "legacy is on" could not interpret the traffic that follows.

    This is where the implemented-version gate belongs, because this is the version that
    governs. A server answering with a revision nobody here has read is refused for the same
    reason a modern one is: §10.6's guarantee depends on knowing where tool definitions live.
    """
    if not isinstance(result, dict):
        return Anomaly(MALFORMED, "initialize result is not an object")
    claimed = result.get("protocolVersion", _ABSENT)
    if not isinstance(claimed, str) or not claimed:
        return Anomaly(MALFORMED, f"initialize result protocolVersion is {claimed!r}")
    if claimed not in LEGACY_VERSIONS:
        return Anomaly(UNIMPLEMENTED_VERSION,
                       f"server negotiated {claimed!r}; this proxy implements "
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


def sampling_carries_tools(msg: dict, *, modern: bool) -> bool | Anomaly:
    """A tool definition at a DEFINED LOCATION outside `tools/list` (§10.6).

    Sampling is the concrete counterexample to "`tools:` gates every route to the model": a
    server may put its own `tools` array in a `sampling/createMessage`, scoped to that request
    and not corresponding to anything registered. Legacy carries it as a server-originated
    request; modern carries it inside `InputRequiredResult.inputRequests`.

    DEFINED LOCATIONS ARE PER VERSION, which is why `modern` is a parameter rather than
    something inferred from the payload. `inputRequests` means `InputRequiredResult` only
    under `2026-07-28`; under a legacy revision `Result` is open-ended (`[key: string]:
    unknown`), so a server may legitimately carry a key of that name meaning something else
    entirely, and reading it as MRTR failed a conforming legacy response (review, PR #100).
    The caller passes the version that governs the message — for a response, the one that
    governed the request it answers.

    THE LOCATION IS AN `input_required` RESULT, not a key that happens to be called
    `inputRequests`. `Result` is open-ended and `resultType` is the discriminator that tells a
    client how to parse it, so reading the key under any other type is scanning for a shape —
    the very thing §10.6 rejects, arriving through the back door. An earlier version argued
    that a mislabelled definition is still a definition and skipped the discriminator; that
    argument is the heuristic scan restated, and it diagnosed a `complete` ping result
    carrying unrelated extension data as tool-bearing sampling (review, PR #100). A conforming
    client never acts on `inputRequests` under `complete`, and defending against one that
    ignores the discriminator cannot be done without guessing.

    An `input_required` result may legitimately carry no `inputRequests` at all — servers
    "MUST include at least one of `inputRequests` or `requestState`" — so its absence is
    simply no definition here.

    `inputRequests` IS A MAP, not a list — "keys are server-assigned string identifiers;
    values are request objects". Iterating it directly walks the KEYS, so every request is a
    string, every `isinstance(req, dict)` is false, and a conforming tool-bearing result
    sails through as clean. That was the shape of the first version of this function, and its
    arm repeated the error by building an array; review caught both (`basic/patterns/mrtr`).
    A non-map `inputRequests` is therefore an ANOMALY rather than a False: a shape this
    function cannot read is exactly the case where "no tools here" is a guess.

    This checks the two named locations, and nothing else — it is not a scan. A `tools` key
    that is not a non-empty list is not a definition; the empty list in particular is what a
    capability FLAG looks like, and tripping on it would fail every modern handshake.
    """
    if msg.get("method") == "sampling/createMessage":
        params = msg.get("params")
        if isinstance(params, dict) and _nonempty_list(params.get("tools")):
            return True
    result = msg.get("result")
    if (modern and isinstance(result, dict)
            and result.get("resultType") == "input_required"
            and "inputRequests" in result):
        requests = result["inputRequests"]
        if not isinstance(requests, dict):
            return Anomaly(BAD_INPUT_REQUESTS,
                           f"InputRequiredResult.inputRequests is "
                           f"{type(requests).__name__}, not the map of server-assigned ids "
                           f"the schema defines; its tool-bearing entries cannot be read")
        for key, req in requests.items():
            if not isinstance(req, dict):
                return Anomaly(BAD_INPUT_REQUESTS,
                               f"inputRequests[{key!r}] is {type(req).__name__}, "
                               f"not a request object")
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

    Each entry carries the METHOD AND THE GOVERNING VERSION, because §10.6's defined locations
    are per version and a response carries no version of its own. Storing only the method left
    the reader of a legacy response applying modern `InputRequiredResult` semantics to it
    (review, PR #100).
    """

    def __init__(self) -> None:
        self._by_direction: dict[str, dict[Any, tuple[str, str | None]]] = {C2S: {}, S2C: {}}
        # Ids retired by a CANCELLATION rather than by an answer. See `cancel`.
        self._cancelled: dict[str, set] = {C2S: set(), S2C: set()}

    def _key(self, req_id: Any) -> Any:
        # STRINGS AND NUMBERS ARE DIFFERENT DOMAINS; WITHIN NUMBERS THE VALUE IS THE ID.
        # `1` and `"1"` are different ids per JSON-RPC, so the domain marker keeps them apart.
        # But `1` and `1.0` are the same JSON number, and a peer may echo a response id in
        # either spelling — "It MUST be the same as the value of the id member in the Request
        # Object" is about the value. Keying on Python's type name split them, so a request on
        # `1` answered on `1.0` read as uncorrelated and both could be live at once (review,
        # PR #100). One numeric domain fixes it for free: `1 == 1.0` in Python and their
        # hashes agree, and large integers keep their exact value rather than being floated.
        #
        # Booleans would alias `1` here, and are excluded at the envelope instead — see
        # `valid_request_id`. That is the only place they can enter.
        return ("s", req_id) if isinstance(req_id, str) else ("n", req_id)

    def record(self, direction: str, req_id: Any, method: str,
               version: str | None) -> None | Anomaly:
        """Open an id, or refuse because it is already open in this direction.

        REUSE IS LEGAL ONLY AFTER RETIREMENT — "the request ID MUST NOT match the ID of any
        other request the sender has issued and not yet received a response for". An
        overwrite here is not a bookkeeping detail: record `tools/list` on id 1, then `ping`
        on id 1, and the server's tools response correlates as a `ping` and is forwarded
        UNFILTERED. That is the guarantee this whole module exists to make, defeated by a
        peer that repeats an id, so a live duplicate is an anomaly rather than a replace.

        This is the ONE place that check lives, and `decide()` claims the id here before it
        will produce any message at all — a refusal synthesized on a live id would answer the
        request that id actually belongs to (review, PR #100).
        """
        live = self._by_direction[direction]
        key = self._key(req_id)
        if key in live:
            return Anomaly(DUPLICATE_ID,
                           f"{direction} id {req_id!r} is already open as "
                           f"{live[key][0]!r}; an id may not be reused before it is answered")
        # Reopening an id ends its cancellation grace: from here a response on it answers THIS
        # request, and a straggler for the cancelled one is indistinguishable from it. That is
        # inherent to id reuse in JSON-RPC, not something a proxy can recover.
        self._cancelled[direction].discard(key)
        live[key] = (method, version)
        return None

    def method_for(self, direction: str, req_id: Any) -> str | None:
        entry = self._by_direction[direction].get(self._key(req_id))
        return entry[0] if entry else None

    def version_for(self, direction: str, req_id: Any) -> str | None:
        """The protocol version that governed the request this id opened, if any.

        `None` in the pre-initialization window, where a sanctioned `ping` may go out before
        any version is in force (§10.2). A response the proxy cannot place in a version is a
        response whose defined locations it does not know, so it reads none of them.
        """
        entry = self._by_direction[direction].get(self._key(req_id))
        return entry[1] if entry else None

    def retire(self, direction: str, req_id: Any) -> str | None:
        """Drop an entry and return the method it held.

        Retirement makes the id REUSABLE, which is not a nicety: the subscription id IS the
        JSON-RPC request id, and a client that cancels a subscription on id 1 may legitimately
        use id 1 for the next `ping`. An entry left resident forever would then correlate a
        fresh response against stale state — or, worse, make the proxy treat a legitimate
        response as unrequested and fail a clean cell.
        """
        entry = self._by_direction[direction].pop(self._key(req_id), None)
        return entry[0] if entry else None

    def cancel(self, direction: str, req_id: Any) -> str | None:
        """Retire an id BECAUSE IT WAS CANCELLED, and remember that. Returns its method.

        The memory is not bookkeeping — it is what keeps a documented race from failing a
        clean cell. "Due to network latency, cancellation notifications may arrive after
        request processing has completed, and potentially after a response has already been
        sent", and the client "SHOULD ignore any response to the cancelled request that
        arrives afterward". Without a record of the cancellation that late response is a reply
        to an id nothing is waiting for — which is exactly the `UNCORRELATED` anomaly, and a
        failed cell for a sequence the spec calls normal (review, PR #100).

        Only an id that was actually live is remembered, which also bounds this: one entry per
        request genuinely cancelled, cleared the moment the id is reused. A cancellation
        naming an id nobody issued is ignored, per "invalid cancellation notifications SHOULD
        be ignored: unknown request IDs".
        """
        key = self._key(req_id)
        if key not in self._by_direction[direction]:
            return None
        method = self.retire(direction, req_id)
        self._cancelled[direction].add(key)
        return method

    def was_cancelled(self, direction: str, req_id: Any) -> bool:
        return self._key(req_id) in self._cancelled[direction]

    def open_ids(self, direction: str) -> list:
        return [key[1] for key in self._by_direction[direction]]


def cancelled_id(msg: dict) -> Any:
    """The request id a `notifications/cancelled` names, or `_ABSENT`.

    Notifications are forwarded verbatim, as all notifications are — but this one must also be
    OBSERVED on the way through, because it is one of the two orderly ends of a subscription
    (§10.4). The design's earlier claim that notifications need no special treatment is wrong
    for exactly this one.

    THE NESTED ID IS VALIDATED LIKE ANY OTHER, and this is not symmetry for its own sake: it
    goes on to key a dict, so `requestId: []` reaches `dict.pop` through a tuple and raises
    `TypeError: unhashable` inside a pump — the same crash `valid_request_id` exists to
    prevent on the envelope, arriving by a route that skipped it (review, PR #100). An
    unusable id yields `_ABSENT` and the notification is simply forwarded uninterpreted, which
    is what the spec asks for: "invalid cancellation notifications SHOULD be ignored ...
    malformed notifications". This maintains their fire-and-forget nature.
    """
    if msg.get("method") != "notifications/cancelled":
        return _ABSENT
    params = msg.get("params")
    if not isinstance(params, dict) or not valid_request_id(params.get("requestId")):
        return _ABSENT
    return params["requestId"]


def is_graceful_closure(method: str | None, msg: dict) -> bool:
    """Whether this response is a subscription's terminal message rather than a reply.

    `subscriptions/listen` gets no immediate answer: its JSON-RPC response IS the closure
    signal. C3-0 found agy opening one with `notifications: {toolsListChanged: true}` and
    streaming through notifications correlated by subscription id — so an implementation that
    treated an unanswered request as a leak, a timeout, or an anomaly would fail every agy
    cell.

    THE SHAPE IS NOT `{}`. The subscriptions page calls it "an empty result" in prose and then
    shows what it actually is: `resultType: "complete"` plus
    `_meta.io.modelcontextprotocol/subscriptionId`, whose value "matches the JSON-RPC `id` of
    the originating `subscriptions/listen` request". Requiring a literally empty object
    rejected our own conforming fixture — review caught it, and `verify_mcp_fixtures.py` had
    been asserting the real shape all along.

    The subscription id is checked against the response id because that is the only thing
    distinguishing a closure from a stray complete result, and a mismatch is a confusion no
    proxy should paper over.

    `resultType` is REQUIRED here, not defaulted. The rule that an absent one reads as
    "complete" exists for servers implementing earlier revisions — and no earlier revision
    has `subscriptions/listen`, which arrived in `2026-07-28` as the replacement for
    `resources/subscribe`. A server answering this method is by construction a modern server,
    where `resultType` is mandatory, so defaulting it accepted a closure that cannot exist
    (review, PR #100).
    """
    if method != "subscriptions/listen":
        return False
    result = msg.get("result")
    if not isinstance(result, dict):
        return False
    if result.get("resultType") != "complete":
        return False
    meta = result.get("_meta")
    if not isinstance(meta, dict):
        return False
    return meta.get(SUB_KEY) == msg.get("id")


class ProtocolState:
    """What an `initialize` exchange selected, for the life of the stdio process.

    NOT A BOOLEAN, and not a mirror of the modern era. Modern is stateless — it supplies its
    context per request, so "the connection is modern" is not a state this may hold, and
    keeping one flag for both roles makes dispatch order-dependent: a modern request followed
    by an accepted `initialize` would leave legacy disabled and refuse every bare request
    after it.

    IT IS TWO-PHASE, because the handshake is. The client's capabilities come from the
    REQUEST; the governing version comes from the SERVER's response, which may not be the one
    proposed. Between the two there is a real state — initialization in flight — and it is not
    optional bookkeeping: without it a second `initialize` sent before the first is answered
    passes the "already negotiated?" test and is forwarded, which is the lifecycle violation
    this class exists to refuse.

    `observed` is TELEMETRY and gates nothing (§10.7). It is plural and says *observed* rather
    than "negotiated" because a modern client declares its version per request and may
    legitimately speak more than one era on one connection; a recorder keeping only the first
    would hide precisely the mixed-era client worth knowing about.
    """

    def __init__(self) -> None:
        self.legacy_version: str | None = None
        self.legacy_capabilities: dict | None = None
        self.pending_initialize: str | None = None
        self.observed: list[str] = []

    def observe(self, version: str) -> None:
        if version not in self.observed:
            self.observed.append(version)

    def begin_initialize(self, proposed: str, capabilities: Any) -> None | Anomaly:
        """Record the client's half, or refuse a second attempt.

        A second `initialize` is a LIFECYCLE VIOLATION, not a renegotiation — whether the
        first has been answered or is still in flight. Accepting one would retroactively move
        every message already exchanged to a revision it was not read under: the proxy would
        have filtered and forwarded traffic under one set of rules and then claimed another
        applied.
        """
        if self.legacy_version is not None:
            return Anomaly(SECOND_INITIALIZE,
                           f"second `initialize` (already negotiated {self.legacy_version}); "
                           f"the lifecycle allows one per connection")
        if self.pending_initialize is not None:
            return Anomaly(SECOND_INITIALIZE,
                           f"second `initialize` while one proposing "
                           f"{self.pending_initialize} is still unanswered")
        self.pending_initialize = proposed
        self.legacy_capabilities = capabilities if isinstance(capabilities, dict) else {}
        return None

    def commit_initialize(self, negotiated: str) -> None | Anomaly:
        """Adopt the version the SERVER answered with. Only a validated result gets here."""
        if self.pending_initialize is None:
            return Anomaly(SECOND_INITIALIZE,
                           "initialize result with no initialize request in flight")
        self.pending_initialize = None
        self.legacy_version = negotiated
        self.observe(negotiated)
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


# ---------------------------------------------------------------------------------------
# The decision (§10.4) — what the I/O layer is allowed to do with a line
# ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Forward:
    """Write `msg` on to the peer, unchanged."""

    msg: dict


@dataclass(frozen=True)
class Filtered:
    """Write `msg` on to the peer INSTEAD of what arrived; `removed` names what was stripped.

    Distinct from `Forward` so the audit log can record the filtered advertisement as the
    expected event §10.5 wants recorded, rather than inferring it by diffing.
    """

    msg: dict
    removed: tuple[str, ...]


@dataclass(frozen=True)
class Refuse:
    """Write `msg` back to whoever SENT the line. The peer is never contacted."""

    msg: dict
    tool: str


@dataclass(frozen=True)
class Drop:
    """Write nothing, and carry on. The one non-terminal way a message does not go through.

    Reserved for a message the protocol itself says the receiver should ignore — today only
    the late response to a cancelled request, which is a documented race rather than a fault.
    It is NOT a general "could not handle it" escape: everything the proxy cannot account for
    is a `Fail`, and adding cases here is how a fail-loud proxy becomes a degrading one.
    """

    msg: dict
    reason: str


@dataclass(frozen=True)
class Fail:
    """Stop. Terminal by contract — the cell fails and no further traffic is forwarded."""

    anomaly: Anomaly


Action = Forward | Filtered | Refuse | Drop | Fail


def decide(line: str, *, direction: str, allowed: frozenset[str], state: ProtocolState,
           inflight: InFlight, server: str) -> Action:
    """The whole decision for one wire line. The I/O layer performs the answer and nothing else.

    This is the function the guarantee lives in. Every check above is reachable from here, in
    an order that matters: an envelope is validated before anything reads its fields; the
    method is judged against the era the request declared, then that era is required to be one
    actually in force; the id is CLAIMED before any branch that can produce a message, so a
    refusal can never be synthesized onto an id that already belongs to something else; and a
    `tools/list` response is shape-checked before it is filtered. A refused call releases the
    id again, since nothing goes out and nothing will come back to correlate.

    `state` and `inflight` are mutated — this is a stream, and the correlation that makes
    filtering possible cannot be stateless. Nothing else here is.
    """
    msg = parse_line(line)
    if isinstance(msg, Anomaly):
        return Fail(msg)
    shape = classify_envelope(msg)
    if isinstance(shape, Anomaly):
        return Fail(shape)
    if shape == REQUEST:
        if direction == C2S:
            return _decide_client_request(msg, allowed, state, inflight, server)
        return _decide_server_request(msg, state, inflight)
    if shape == NOTIFICATION:
        cid = cancelled_id(msg)
        if cid is not _ABSENT:
            # Forwarded verbatim, as every notification is — but OBSERVED on the way through,
            # because this is one of the two orderly ends of a subscription and the id it
            # names becomes reusable the moment it passes.
            _cancel_wherever_live(inflight, direction, cid)
        return Forward(msg)
    return _decide_response(msg, shape, direction, allowed, state, inflight)


def _cancel_wherever_live(inflight: InFlight, direction: str, req_id: Any) -> None:
    """Retire a cancelled id in the direction that actually issued the request.

    NOT the direction the notification travelled, which is the obvious reading and is wrong
    for the one case the modern revision requires: "a server MUST send
    `notifications/cancelled` referencing a `subscriptions/listen` request ID when it tears
    down that subscription stream" — and that request is the CLIENT's. Retiring the sender's
    own map there leaves the real subscription resident forever, so reusing its id later fails
    as a duplicate and takes a clean cell with it (review, PR #100).

    Under legacy both sides issue requests and cancel their own, so the sender's own map is
    tried first and the peer's second. An id live in neither is ignored, per "invalid
    cancellation notifications SHOULD be ignored: unknown request IDs".
    """
    for where in (direction, _ORIGIN_OF[direction]):
        if inflight.cancel(where, req_id) is not None:
            return


def _decide_client_request(msg: dict, allowed: frozenset[str], state: ProtocolState,
                           inflight: InFlight, server: str) -> Action:
    method = msg["method"]
    version = modern_version(msg)
    if isinstance(version, Anomaly):
        return Fail(version)
    modern = version is not None
    if modern:
        state.observe(version)
    # The method is judged against the era the REQUEST declared, before anything asks whether
    # that era is in force — a bare `subscriptions/listen` is a modern-only method offered
    # under non-modern metadata whether or not an `initialize` has happened, and that is the
    # more specific diagnosis of the two.
    if not method_matches_era(method, modern=modern):
        # Refused rather than forwarded for the server to reject, because the two eras
        # disagree about what the connection IS. A modern `initialize` that a dual-era server
        # chose to answer would leave the server in legacy and the proxy reading the rest of
        # the stream as modern — and a proxy that has lost track of the era cannot say where
        # tool definitions live (§10.6). No CLI measured in C3-0 sends either.
        return Fail(Anomaly(ERA_METHOD_MISMATCH,
                            f"{method!r} does not belong to the era this request declared "
                            f"(modern={modern}); the method must match the request, not the "
                            f"reverse"))
    if (not modern and state.legacy_version is None and state.pending_initialize is None
            and method not in PRE_INITIALIZE_METHODS):
        # No modern `_meta`, no negotiated version, AND no handshake even attempted: NOTHING
        # governs this request and nothing is going to. Reading it as legacy would be the
        # version gate defeated by simply never handshaking — a client that sends neither
        # would have every request waved through on the strength of an absence, and §10.6's
        # guarantee is stated per implemented version (review, PR #100).
        #
        # A PENDING `initialize` counts, and that is what makes this rule safe to enforce.
        # The lifecycle says a client SHOULD NOT pipeline ordinary requests behind its
        # `initialize` — but SHOULD NOT is not MUST NOT, and a proxy reads lines serially
        # while a server answers them, so a client that pipelines reaches this check with the
        # response still in flight where the server it is talking to would already have
        # negotiated. Refusing there would fail a cell that nothing else objects to, and
        # supporting it any other way needs a defer-and-replay action — a fourth outcome for
        # `decide`, and a materially larger contract (review, PR #100). Nothing escapes:
        # whatever version is negotiated governs the RESPONSE, which is where tool definitions
        # actually live, and a negotiation this proxy cannot implement fails before any
        # response is forwarded.
        return Fail(Anomaly(NO_ERA_ESTABLISHED,
                            f"{method!r} carries no modern `_meta` and no `initialize` has "
                            f"been sent, so no implemented protocol version governs it or is "
                            f"going to; only {', '.join(PRE_INITIALIZE_METHODS)} may precede "
                            f"a handshake"))
    name = None
    if method == "initialize":
        proposed = proposed_version(msg)
        if isinstance(proposed, Anomaly):
            return Fail(proposed)
        began = state.begin_initialize(proposed, (msg.get("params") or {}).get("capabilities"))
        if isinstance(began, Anomaly):
            return Fail(began)
    elif method == "tools/call":
        name = (msg.get("params") or {}).get("name")
        if not isinstance(name, str) or not name:
            return Fail(Anomaly(MALFORMED,
                                f"`tools/call` params.name is {name!r}, not a tool name"))
    # THE ID IS CLAIMED BEFORE ANY MESSAGE IS PRODUCED, refusals included. An off-list call
    # answered on an id that is already live would send the client an error correlated to the
    # request that id really belongs to — its outstanding `tools/list`, say, which then looks
    # answered while the real response is still coming (review, PR #100). So the duplicate
    # check is one step, here, ahead of every branch below it.
    opened = inflight.record(C2S, msg["id"], method,
                             version if modern else state.legacy_version)
    if isinstance(opened, Anomaly):
        return Fail(opened)
    if name is not None and name not in allowed:
        # Not forwarded, so nothing will come back to correlate: release the id the check
        # above claimed. Every MRTR retry lands here too — the allowlist keys on `params.name`
        # per request rather than on position in the stream, and a retry carries a new id.
        inflight.retire(C2S, msg["id"])
        return Refuse(refusal_for(msg["id"], name, server), name)
    return Forward(msg)


def _decide_server_request(msg: dict, state: ProtocolState, inflight: InFlight) -> Action:
    # A server→client REQUEST is legal only under legacy semantics: 2026-07-28 says servers
    # "MUST NOT send JSON-RPC requests", carrying sampling, elicitation and roots inside
    # `InputRequiredResult` responses instead. The test is the LEGACY STATE IN FORCE, not a
    # "connection is modern" flag — modern is stateless, so no such flag may exist.
    if state.legacy_version is None:
        return Fail(Anomaly(SERVER_REQUEST_IN_MODERN,
                            f"server sent a {msg['method']!r} request with no legacy "
                            f"`initialize` in force; the modern revision forbids "
                            f"server-originated requests entirely"))
    # Legacy by the test above, so the legacy carrier is the one that applies: the
    # server-originated `sampling/createMessage` request itself.
    carries = sampling_carries_tools(msg, modern=False)
    if isinstance(carries, Anomaly):
        return Fail(carries)
    if carries:
        return Fail(Anomaly(TOOL_BEARING_SAMPLING,
                            f"{msg['method']!r} carries its own `params.tools`: a tool "
                            f"definition at a defined location outside `tools/list`, which "
                            f"`tools:` does not gate and this proxy will not forward"))
    opened = inflight.record(S2C, msg["id"], msg["method"], state.legacy_version)
    if isinstance(opened, Anomaly):
        return Fail(opened)
    return Forward(msg)


def _decide_response(msg: dict, shape: str, direction: str, allowed: frozenset[str],
                     state: ProtocolState, inflight: InFlight) -> Action:
    if "id" not in msg:
        # Only an ERROR may arrive without one (`id?: RequestId`), so this is schema-valid and
        # NOT malformed — but the proxy forwards only validated requests and can never account
        # for it. Same verdict as a bad frame, different diagnosis, which is the whole point
        # of the kinds (§10.4).
        return Fail(Anomaly(UNCORRELATED,
                            "error response carries no `id`: schema-valid, but it answers "
                            "nothing this proxy forwarded"))
    # A response travels the opposite way from the request it answers.
    origin = _ORIGIN_OF[direction]
    req_id = msg["id"]
    method = inflight.method_for(origin, req_id)
    if method is None:
        if inflight.was_cancelled(origin, req_id):
            # The documented race, not a fault: "cancellation notifications may arrive after
            # request processing has completed, and potentially after a response has already
            # been sent", and the client "SHOULD ignore any response to the cancelled request
            # that arrives afterward". Dropping it is that instruction carried out one hop
            # earlier — and it is safer than forwarding, since the correlation this response
            # would need to be FILTERED went away with the cancellation.
            return Drop(msg, f"late response to cancelled {origin} id {req_id!r}")
        return Fail(Anomaly(UNCORRELATED,
                            f"response to {origin} id {req_id!r}, which was never requested "
                            f"in that direction"))
    if shape == ERROR:
        inflight.retire(origin, req_id)
        return Forward(msg)

    # A response carries no version of its own, so the one that governs it is the one that
    # governed the request it answers — which is why the correlation entry holds it. Under a
    # legacy revision `inputRequests` is not a defined location at all (`Result` is
    # open-ended), and reading it as MRTR failed conforming legacy traffic.
    carries = sampling_carries_tools(
        msg, modern=inflight.version_for(origin, req_id) in MODERN_VERSIONS)
    if isinstance(carries, Anomaly):
        return Fail(carries)
    if carries:
        return Fail(Anomaly(TOOL_BEARING_SAMPLING,
                            f"the response to {method!r} carries a `sampling/createMessage` "
                            f"with its own `params.tools` in `inputRequests`: a definition "
                            f"outside `tools/list`, which `tools:` does not gate"))

    if method == "subscriptions/listen":
        # The only response this request ever gets is its graceful closure, so anything else
        # on that id is a confusion rather than a reply to forward.
        if not is_graceful_closure(method, msg):
            return Fail(Anomaly(BAD_SUBSCRIPTION_CLOSURE,
                                f"response on subscription id {req_id!r} is not the "
                                f"conforming closure (resultType `complete` and a matching "
                                f"{SUB_KEY})"))
        inflight.retire(origin, req_id)
        return Forward(msg)

    if method == "initialize":
        negotiated = negotiated_version(msg["result"])
        if isinstance(negotiated, Anomaly):
            return Fail(negotiated)
        committed = state.commit_initialize(negotiated)
        if isinstance(committed, Anomaly):
            return Fail(committed)
        inflight.retire(origin, req_id)
        return Forward(msg)

    if method == "tools/list":
        result = msg["result"]
        if not tools_result_ok(result):
            return Fail(Anomaly(BAD_TOOLS_RESULT,
                                "`tools/list` result is not a `tools` array of objects with "
                                "string names, so the allowlist cannot be applied to it"))
        kept, removed = filter_tools_result(result, allowed)
        out = dict(msg)
        out["result"] = kept
        inflight.retire(origin, req_id)
        return Filtered(out, tuple(removed))

    inflight.retire(origin, req_id)
    return Forward(msg)
