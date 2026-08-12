#!/usr/bin/env python3
"""A zero-dependency HTTP MCP server, for the half of §9 probe #1 nothing has measured.

WHAT THIS ANSWERS. `claude._write_mcp_config` writes `{"type": "http"|"sse", "url",
"headers"}` for a remote server, and until now that shape was INFERRED. Two of the three
questions can be answered without a server at all — `claude mcp add --transport http` writes
that exact shape, and `--mcp-config` lists such a server in its init event — but both stop at
"the config was understood". The third cannot: **do the declared `headers` actually reach the
server?** That is the one with credentials behind it, because §8's SlideRule pattern is a
bearer token in exactly that field, and a token that never arrives is a broken run that looks
like an empty result.

Answering it needs something on the other end that can say what it received. Hence a server
rather than a mock: the same argument `echo_mcp_server.py` opens with, one transport over.

THE POSITIVE CONTROL IS THE POINT. Pointed at an unreachable host, claude reports
`status: "failed"` — which is also what a config it could not parse would produce. A server
that actually connects is what makes that reading mean something, and without one the probe
cannot distinguish "understood, unreachable" from "not understood". §4 spends a rule on this:
a channel nothing was ever passed to reports the same silence as a channel whose holder never
started.

TWO TRANSPORTS, because the adapter emits two `type` values and they are different protocols
rather than different spellings:

  * `http`  — Streamable HTTP. One endpoint. The client POSTs a JSON-RPC message and the
    response comes back in that same HTTP response, either as `application/json` or as an SSE
    stream. A `GET` may open a server→client stream; a server that does not need one MUST
    answer 405, which this does, because a fixture that opened an idle stream would be adding
    a code path nothing in the harness exercises.
  * `sse`   — the older two-channel transport. `GET` opens an event stream whose FIRST event
    names a second URL, and the client POSTs to that one while replies arrive on the stream
    it is still holding. The asymmetry is the whole difference: under `http` a reply is the
    POST's own response, under `sse` the POST is answered `202 Accepted` and the reply
    arrives somewhere else entirely.

THE HTTP BINDING IS DELIBERATELY LEGACY-ONLY, AND THIS IS THE SECOND ATTEMPT AT SAYING SO
ACCURATELY. The first said the transport "implements the `2025-11-25` binding" and filed
`MCP-Protocol-Version` under the things modern HTTP adds. Both halves were wrong in the same
direction: that header is required BY `2025-11-25`, on every request after initialization, with
a 400 owed for a value the server does not support — so a sentence written to disclaim modern
work was quietly claiming legacy conformance this file did not have (review, PR #106). An
overstated scope note is worse than none, because it is what a later reader trusts INSTEAD of
reading the code.

WHAT IS SERVED, precisely: Streamable HTTP as specified in `2025-11-25`, on `/mcp`; and the
deprecated `2024-11-05` HTTP+SSE pair on `/sse` + `/messages`, which that same revision keeps
for backwards compatibility and which `claude._write_mcp_config` still emits as `"type":
"sse"`. The server-side MUSTs of the former are swept rather than sampled, and listed here so
the claim is checkable against the code instead of taken: bind to loopback only; validate
`Origin`; answer 400 to an unsupported `MCP-Protocol-Version`; answer 202 and no body to a POST
carrying only notifications; answer JSON or an SSE stream to a POST carrying requests; answer
405 to a GET this server will not open a stream for.

VALIDATION HAPPENS BEFORE THE BODY IS READ, and that ordering is part of the `Origin` defence
rather than an implementation detail of it. A refusal that reads first lets the caller it is
refusing choose how much this server reads, or hold a handler open indefinitely by declaring
a body and never sending it — so `_refusal_for` decides from the request line and headers
alone, and `_refuse` answers and closes without touching the body. Every check that sends a
body is blind to this, which is how a revision that read first passed all three `Origin`
checks; the case that sees it is a hand-written request over a raw socket (review, PR #106).

WHAT IS NOT SERVED, deliberately, each because nothing in the harness reaches it yet: modern
Streamable HTTP (`2026-07-28`) and its per-request metadata — the imported protocol logic is
dual-era and WILL answer a modern message, which is precisely why the TRANSPORT gap has to be
stated rather than inferred from the replies; sessions (below); stream resumability and
`Last-Event-ID`; and authentication, which is a SHOULD this fixture inverts on purpose, its
whole job being to RECORD the credential rather than to check it. A modern HTTP arm is real
work, it is not done, and anything reading this file as evidence about modern HTTP would be
reading it wrong.

SESSIONS ARE NOT IMPLEMENTED, AND SO NONE IS ADVERTISED. The transport makes `Mcp-Session-Id`
optional; a server that keeps no session state is conformant without it. What is not
conformant is issuing one and ignoring it, which is what an earlier revision did.

THE PROTOCOL LOGIC IS IMPORTED, NOT RESTATED. `initialize`, `server/discover`, `tools/list`,
`tools/call`, the era rules and the modern result shaping all come from `echo_mcp_server.py`,
which is a sibling in this directory and therefore importable. §4's rule is that a duplicated
rule must be pinned to its original and an imported one cannot disagree at all — and the era
rules are exactly the kind that drift silently, since a wrong one still produces a well-formed
reply. What lives here is transport and nothing else, which is also what makes this fixture
evidence about transport: if a scenario behaves differently over HTTP than over stdio, the
difference cannot be the protocol layer, because it is the same code.

THE RECEIPTS FILE IS A WITNESS, AND IT IS WIRED BEFORE IT IS TRUSTED. `HTTP_MCP_RECEIPTS`
names a path this server appends one JSON object per line to: a `listening` record at startup
and one `request` record per HTTP request, carrying the method, the path, and EVERY header as
received. The startup record is not decoration — the question this fixture exists to answer is
answered by the ABSENCE of an `authorization` header, and an absent file, an unstarted server
and a server that received nothing all look identical from outside. A positive record at
startup separates them, so "no authorization header in any request record" is a finding rather
than an unread channel (§4).

HEADERS ARE RECORDED VERBATIM AND THIS FILE IS THEREFORE CREDENTIAL-BEARING. It is written
0600 into whatever path the caller names, and the caller is responsible for putting that path
somewhere the harness does not archive. The alternative — recording only header NAMES — was
rejected because "the header arrived" and "the header arrived with the right value" are
different findings, and a probe that cannot tell them apart would pass a CLI that forwards the
key while mangling the value.

No third-party imports, by rule: this runs wherever the fixture directory happens to be, on
whatever interpreter starts it.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The protocol half, imported rather than restated — see the module docstring.
import echo_mcp_server as echo  # noqa: E402 — the path insert above has to come first

RECEIPTS_ENV = "HTTP_MCP_RECEIPTS"
PORT_ENV = "HTTP_MCP_PORT"           # 0 (the default) means "pick a free one and say which"
ORIGINS_ENV = "HTTP_MCP_ALLOWED_ORIGINS"   # comma-separated; empty means "no Origin allowed"

# ORIGIN VALIDATION IS A TRANSPORT-LEVEL **MUST**, and this server is the case the requirement
# was written for: it listens on loopback while a model is running, so any page the user's
# browser is on can POST to it cross-origin unless the server refuses. The attack is DNS
# rebinding — a hostile page resolves its own name to 127.0.0.1 and then speaks to whatever is
# listening, and "it is only bound to localhost" is precisely the false comfort the rule
# exists to remove.
#
# A fixture that skipped this would be non-conformant in the direction that matters: every
# check here is evidence about what a REAL server does, so a fixture more permissive than the
# spec teaches the harness that a permissive server is normal (review, PR #106).
#
# Default is DENY: a request with no `Origin` at all is fine — that is what a non-browser
# client sends, and it is every client this fixture serves — but a present `Origin` must be on
# the allowlist, which is empty unless the caller names one.
ALLOWED_ORIGINS = frozenset(
    o.strip() for o in (os.environ.get(ORIGINS_ENV) or "").split(",") if o.strip())

# The two endpoint paths. `/mcp` is the streamable-HTTP endpoint and the only one that
# transport uses. `/sse` and `/messages` are the legacy pair, and they are SEPARATE paths
# because that transport's first act is to tell the client a second URL — pointing both at
# one path would make a fixture that cannot distinguish the two transports, which is the
# distinction it exists to test.
PATH_STREAMABLE = "/mcp"
PATH_SSE = "/sse"
PATH_MESSAGES = "/messages"

# THE PROTOCOL-VERSION HEADER IS THE BINDING'S OTHER MUST, and it is implemented here for the
# reason the `Origin` block above gives rather than for a reason of its own. That argument — a
# fixture more permissive than the spec teaches the harness that a permissive server is normal
# — quantifies over every server-side MUST in the binding, not over the one that happened to be
# reported, so honouring it meant sweeping the rest of them. This is what the sweep found
# missing: `2025-11-25` requires the client to send `MCP-Protocol-Version` on every request
# after initialization, and requires the server to answer 400 when the value is one it does not
# support (review, PR #106).
#
# THE SUPPORTED SET IS IMPORTED, AND THAT IS LOAD-BEARING RATHER THAN TIDY. `echo._initialize`
# picks the negotiated version out of `echo.LEGACY_VERSIONS`; any set narrower than that one
# lets this server 400 a version it negotiated itself one request earlier, which presents as an
# intermittent client bug. Importing is what makes the two unable to disagree (§4), and the
# check driving every member of the tuple is what makes a later narrowing fail loudly.
#
# ABSENT IS ALLOWED, and is not a gap. The transport tells a server to assume `2025-03-26` when
# the header is missing; more to the point, the `initialize` request that PRECEDES negotiation
# legitimately carries none, and a server keeping no session state cannot tell that request
# from a client that simply omits the header. Refusing would break the handshake the header is
# defined relative to.
SUPPORTED_VERSIONS = echo.LEGACY_VERSIONS


class Receipts:
    """What actually arrived, written where a different process can read it.

    Locked because `ThreadingHTTPServer` handles requests on separate threads and two
    interleaved partial lines would be an unparseable record — the failure mode where the
    witness's own defect reads as the absence of evidence.
    """

    def __init__(self, path: str | None) -> None:
        self.path = path
        self._lock = threading.Lock()

    def write(self, kind: str, **fields) -> None:
        if not self.path:
            return
        line = json.dumps({"kind": kind, **fields}, separators=(",", ":")) + "\n"
        with self._lock:
            # 0600 from the start rather than written-then-chmod'ed: the headers recorded
            # here include whatever `Authorization` the client sent, so a world-readable
            # window is a credential exposure however brief (the same rule the adapter's
            # scratch config follows).
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(line)


RECEIPTS = Receipts(os.environ.get(RECEIPTS_ENV) or None)


def dispatch(msg: dict) -> dict | None:
    """One JSON-RPC message in, the message to send back, or None for a notification.

    TRANSPORT-FREE ON PURPOSE. Both transports below call this and differ only in where the
    answer goes, which is what keeps "the reply is the POST's response" and "the reply
    arrives on a stream the client is already holding" from becoming two protocol
    implementations that can disagree.
    """
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    # THE WITNESS RECORDS THE MESSAGE, not only the HTTP request that carried it. The header
    # record answers "did the credential arrive"; this answers "was the tool actually INVOKED",
    # which is a different question and the one a LIVE probe cannot ask of itself. A model that
    # never calls the tool and simply repeats its prompt back produces the same final text as
    # one that did, so an assertion over that text is satisfiable without a tool call ever
    # happening (review, PR #106). Only the server can say otherwise.
    #
    # Written HERE rather than in either transport, because both call this: where the reply
    # goes is what the two transports disagree about, and the fact that the call arrived is
    # not. The tool NAME is recorded, and only for `tools/call`, so the field distinguishes a
    # tool call from every other message rather than being a constant that would satisfy the
    # check without carrying anything.
    RECEIPTS.write("rpc", method=method, id=req_id,
                   tool=params.get("name") if method == "tools/call" else None)
    if method is None or req_id is None:
        return None                      # a notification, or a response; nothing to answer
    modern = echo._modern_intent(msg)
    if method == "initialize":
        return echo.result_envelope(req_id, echo._initialize(params))
    if method == "server/discover":
        return echo.result_envelope(req_id, echo._discover(), modern=modern, cacheable=True)
    if method == "tools/list":
        return echo.result_envelope(req_id, {"tools": echo.TOOLS},
                                    modern=modern, cacheable=True)
    if method == "tools/call":
        return echo.result_envelope(req_id, echo._call_tool(params), modern=modern)
    if method == "ping":
        return echo.result_envelope(req_id, {}, modern=modern)
    return echo.error_envelope(req_id, -32601, f"method not found: {method}")


def _sse_frame(payload: dict, *, event: str | None = None) -> bytes:
    """One Server-Sent Event. `data:` last, and the blank line is what ends the event."""
    head = f"event: {event}\n" if event else ""
    return (head + "data: " + json.dumps(payload, separators=(",", ":")) + "\n\n").encode()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"        # keep-alive, which both transports assume

    # -- plumbing ---------------------------------------------------------------------------

    def log_message(self, fmt, *args):
        """Silence. The receipts file is the record; stderr belongs to whoever started us."""

    def _record(self, msg: dict | None = None) -> None:
        RECEIPTS.write("request", method=self.command, path=self.path,
                       # THE MESSAGE THIS REQUEST CARRIED, on the SAME ROW as its headers.
                       # Without it the header questions can only be asked of the run as a
                       # whole, and at least one of them is per-message: `initialize` precedes
                       # the negotiation `MCP-Protocol-Version` reports, so "did the header
                       # arrive" has a different right answer for it than for every request
                       # after it. The `rpc` rows written by `dispatch` carry the method too,
                       # but pairing them with these by ADJACENCY is unsound — this is a
                       # `ThreadingHTTPServer`, and two requests in flight interleave their
                       # rows. Correlated by construction is the only kind that holds (review,
                       # PR #106). `None` for a GET, which carries no message.
                       rpc=msg.get("method") if isinstance(msg, dict) else None,
                       # EVERY header, lowercased for lookup but otherwise verbatim. The
                       # question is whether `authorization` arrives AND with what value, so
                       # recording names only would answer half of it.
                       headers={k.lower(): v for k, v in self.headers.items()})

    def _body(self) -> dict | None:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return None
        try:
            return json.loads(self.rfile.read(length))
        except ValueError:
            return None

    def _json(self, code: int, payload: dict, extra: dict | None = None) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _empty(self, code: int) -> None:
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- streamable HTTP --------------------------------------------------------------------

    def _origin_ok(self) -> bool:
        """Absent is fine; present must be allowlisted. See ALLOWED_ORIGINS above."""
        origin = self.headers.get("Origin")
        return origin is None or origin in ALLOWED_ORIGINS

    def _version_ok(self) -> bool:
        """Absent is fine; present must name a version this server implements. See above."""
        claimed = self.headers.get("MCP-Protocol-Version")
        return claimed is None or claimed in SUPPORTED_VERSIONS

    def _refusal_for(self, path: str) -> int | None:
        """The status this request is refused with, or None to accept it.

        DECIDED FROM THE REQUEST LINE AND HEADERS ALONE, which is the whole point of it being
        a function: everything answerable without the body is listed here, so it can be
        answered BEFORE the body is read. A revision that read the body first — to put the
        message's method on the receipt row — bought that convenience with the ordering, and
        the ordering was the defence: a rejected cross-origin caller could then make this
        server read a `Content-Length` of its choosing, or hold the handler open forever by
        declaring a body and sending none, before collecting its 403 (review, PR #106).
        Origin validation exists to spend nothing on a caller that failed it, and the comment
        justifying the reorder listed what it bought while never mentioning what it cost.
        """
        if not self._origin_ok():
            return 403
        # EXACTLY THE CONFIGURED PATHS, matched whole. `startswith` accepted `/mcp-anything`
        # and, worse, a catch-all accepted `/definitely-not-mcp` — so a config whose URL had
        # been mangled or rewritten still reached a working server, and the probe that was
        # supposed to prove the URL correct proved nothing about it (review, PR #106).
        if path not in (PATH_STREAMABLE, PATH_MESSAGES):
            return 404
        # The version header belongs to the Streamable HTTP binding; `/messages` is the
        # `2024-11-05` transport, which predates it and whose clients need not send one.
        if path == PATH_STREAMABLE and not self._version_ok():
            return 400
        return None

    def _refuse(self, code: int) -> None:
        """Answer, and CLOSE, WITHOUT having read the body.

        Closing is not tidiness — it is the only other way to end a refusal. An unread body
        desynchronizes a keep-alive connection, so a server that refuses must either read what
        it declined to look at or stop talking, and for a request refused on `Origin` reading
        is precisely what must not happen. `send_header("Connection", "close")` both tells the
        client and sets `close_connection` in `http.server`, so the two cannot disagree.
        """
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def do_POST(self) -> None:                                   # noqa: N802 — BaseHTTPRequestHandler
        # THE ORDER HERE IS A SECURITY PROPERTY, not a style: every decision that can be made
        # from the request line and headers is made before a byte of the body is read. See
        # `_refusal_for`. The query string is split off first because `/messages` carries one.
        path = self.path.split("?", 1)[0]
        refusal = self._refusal_for(path)
        if refusal is not None:
            # Recorded anyway, with no message, because "did the credential arrive" must stay
            # answerable for a request that was REFUSED — a token sent to a rejected origin
            # still left the client.
            self._record(None)
            self._refuse(refusal)
            return
        msg = self._body()
        self._record(msg)
        if msg is None:
            self._empty(400)
            return
        if path == PATH_MESSAGES:
            # LEGACY SSE: the reply does NOT belong to this response. It goes to the stream
            # the client opened with GET /sse and is still holding; this POST is answered
            # `202 Accepted` and nothing else. Getting this wrong by answering here would
            # produce a fixture that works with a client which does not implement the
            # transport it claims to.
            reply = dispatch(msg)
            session = _query_session(self.path)
            stream = STREAMS.get(session)
            if stream is None:
                self._empty(404)
                return
            if reply is not None:
                stream.send(_sse_frame(reply))
            self._empty(202)
            return
        # NO SECOND PATH OR VERSION CHECK HERE. Both moved into `_refusal_for`, and leaving a
        # copy behind would be one rule implemented in two places — the drift §4 spends a rule
        # on. Reaching this line already means the path is `/mcp` and the version is one this
        # server implements.
        #
        # STREAMABLE HTTP: the reply is this response's body.
        reply = dispatch(msg)
        if reply is None:
            self._empty(202)             # a notification carries no answer
            return
        # NO `Mcp-Session-Id`. An earlier version minted one on the initialize reply because
        # that is where the transport puts it — but this fixture keeps no session state, so a
        # later request with no id, or with a wrong one, was answered exactly like a correct
        # one. Issuing an identifier nothing checks is worse than issuing none: it teaches a
        # client that the id is honoured, and the fixture would pass a client that never echoed
        # it (review, PR #106). Sessions are optional in the transport; not having them is a
        # conformant choice, and pretending to have them is not.
        self._json(200, reply)

    def do_GET(self) -> None:                                    # noqa: N802
        self._record()
        if not self._origin_ok():
            self._refuse(403)                # a GET carries no body, but it ends the same way
            return
        if self.path.split("?", 1)[0] != PATH_SSE:
            # The streamable endpoint MAY decline to open a server→client stream, and 405 is
            # the documented way to say so. A fixture that opened an idle stream instead
            # would add a path nothing here exercises and a shutdown case to go with it.
            self._empty(405)
            return
        session = uuid.uuid4().hex
        stream = Stream(self.wfile)
        STREAMS[session] = stream
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        # THE FIRST EVENT NAMES THE SECOND URL. That is the legacy transport's whole
        # handshake: without it the client has nowhere to POST, and with a wrong one it
        # POSTs into a 404 that looks exactly like a server that is not answering.
        stream.send(f"event: endpoint\ndata: {PATH_MESSAGES}?sessionId={session}\n\n".encode())
        try:
            stream.wait()
        finally:
            STREAMS.pop(session, None)


class Stream:
    """One held-open SSE response, writable from the thread handling a different request."""

    def __init__(self, wfile) -> None:
        self._wfile = wfile
        self._lock = threading.Lock()
        self._closed = threading.Event()

    def send(self, raw: bytes) -> None:
        with self._lock:
            try:
                self._wfile.write(raw)
                self._wfile.flush()
            except OSError:
                self._closed.set()       # the client hung up; ordinary, not a failure

    def wait(self) -> None:
        self._closed.wait()


STREAMS: dict[str, Stream] = {}


def _query_session(path: str) -> str:
    _, _, query = path.partition("?")
    for part in query.split("&"):
        key, _, value = part.partition("=")
        if key == "sessionId":
            return value
    return ""


def main() -> int:
    port = int(os.environ.get(PORT_ENV) or 0)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    actual = server.server_address[1]
    # THE IDENTITY IS ECHO'S, and it is reported here for the same reason the stdio fixture
    # reports its own: under `@generate` the marker is minted inside this process, so the
    # receipts file is the only route back to the driver that does not pass through the client.
    RECEIPTS.write("listening", port=actual,
                   streamable=f"http://127.0.0.1:{actual}{PATH_STREAMABLE}",
                   sse=f"http://127.0.0.1:{actual}{PATH_SSE}",
                   identity=echo.IDENTITY)
    # STDOUT, ONE LINE, FLUSHED: the caller needs the port before it can write a config
    # naming it, and an ephemeral port is the only kind that cannot collide with whatever
    # else is running on a developer machine or a CI box.
    print(json.dumps({"port": actual,
                      "streamable": f"http://127.0.0.1:{actual}{PATH_STREAMABLE}",
                      "sse": f"http://127.0.0.1:{actual}{PATH_SSE}"}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
