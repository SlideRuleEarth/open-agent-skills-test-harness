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

# The two endpoint paths. `/mcp` is the streamable-HTTP endpoint and the only one that
# transport uses. `/sse` and `/messages` are the legacy pair, and they are SEPARATE paths
# because that transport's first act is to tell the client a second URL — pointing both at
# one path would make a fixture that cannot distinguish the two transports, which is the
# distinction it exists to test.
PATH_STREAMABLE = "/mcp"
PATH_SSE = "/sse"
PATH_MESSAGES = "/messages"


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
    if method is None or req_id is None:
        return None                      # a notification, or a response; nothing to answer
    modern = echo._modern_intent(msg)
    params = msg.get("params") or {}
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

    def _record(self) -> None:
        RECEIPTS.write("request", method=self.command, path=self.path,
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

    def do_POST(self) -> None:                                   # noqa: N802 — BaseHTTPRequestHandler
        self._record()
        msg = self._body()
        if msg is None:
            self._empty(400)
            return
        if self.path.startswith(PATH_MESSAGES):
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
        # STREAMABLE HTTP: the reply is this response's body.
        reply = dispatch(msg)
        if reply is None:
            self._empty(202)             # a notification carries no answer
            return
        # `Mcp-Session-Id` on the initialize reply only, which is where the spec puts it.
        extra = ({"Mcp-Session-Id": uuid.uuid4().hex}
                 if msg.get("method") == "initialize" else None)
        self._json(200, reply, extra)

    def do_GET(self) -> None:                                    # noqa: N802
        self._record()
        if not self.path.startswith(PATH_SSE):
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
    RECEIPTS.write("listening", port=actual,
                   streamable=f"http://127.0.0.1:{actual}{PATH_STREAMABLE}",
                   sse=f"http://127.0.0.1:{actual}{PATH_SSE}")
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
