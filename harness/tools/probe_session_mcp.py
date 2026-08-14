#!/usr/bin/env python3
"""§9 probe C3-4 — does a real remote MCP server issue a session, and will it release one?

WHY THIS FILE EXISTS. §10.10 takes a position — **a retained session is not clean** — whose
cost is named but not measured: a conformant server that simply does not offer client-driven
termination makes every gated run against it fail. §9 records that deciding this by reasoning
means choosing between "fails every run against a common server shape" and "certifies a live
credential-bearing session as cleaned up", with no evidence about which case is real. This is
the instrument that supplies the evidence.

IT IS THE FIRST C3 PROBE WHOSE SUBJECT IS A SERVER RATHER THAN A CLI. Nothing here runs an
agent CLI, spends a model call, or needs a credential against the default target — it speaks
Streamable HTTP directly, because the questions are about what the *server* does.

    python3 tools/probe_session_mcp.py                       # the default target, full run
    python3 tools/probe_session_mcp.py --url URL --sessions 5
    python3 tools/probe_session_mcp.py --skip-survival       # everything but the timed hold
    SLIDERULE_TOKEN=... python3 tools/probe_session_mcp.py \
        --url URL --header-env 'Authorization=SLIDERULE_TOKEN'

**NO HEADER VALUE IS READ FROM THE COMMAND LINE.** `--header-env NAME=VAR` names the variable
holding the value, so only the variable's NAME reaches argv; the variable must hold the
**complete** header value including any scheme (`TOK='Bearer eyJ…'`, not the bare token —
nothing here synthesizes a scheme, because guessing one would send `Bearer <something-else>`
for anybody not using it). `--header` always refuses, and exists only so the error names the
flag that works.

The property is STRUCTURAL rather than enumerated, and that is the second correction it took.
This file first recommended `--header 'Authorization: Bearer …'`, which §10.7 already treats as
world-readable and which this repo's own containment sweep can read via `ps -eo command=`. The
fix after that was a denylist of credential-bearing header names — which passed
`X-SlideRule-Token`, `X-Goog-Api-Key` and `Api-Key` straight through, the motivating server's
own header shape among them (external review, both rounds). **A list of which names are secret
can always be one name short, and a guarantee resting on such a list is not one**; refusing
every command-line value is the version that can actually be checked.

**A READING IS ABOUT THE SERVER IT WAS TAKEN FROM.** C3-4 is named for the SlideRule server and
§8's motivating pattern is a bearer token against it; the default target here is NASA's public
Earthdata endpoint, which is a *different server* and needs no credential. So this answers "does
a real conformant server release a session" and leaves "does SlideRule" open. The report names
its target, its `serverInfo` and the date for that reason, and `--url` exists so the same
procedure runs against the other one when access arrives rather than being rewritten for it.

WHAT IT ASSERTS, one numbered failure mode each, in §9's question order:

  0. THE HANDSHAKE COMPLETES — `initialize`, a response correlated by id, then
     `notifications/initialized`. Numbered zero because everything below is a reading taken
     through it: a probe that skipped the notification measured a server's tolerance of an
     incomplete handshake rather than the lifecycle the bridge executes, which is a different
     server behaviour wearing the same numbers.
  1. Q1 — the PROTOCOL REVISION and binding, classified against the era constants imported from
     `mcp_proxy`. A modern (`2026-07-28`) server would mean the session machinery **does not
     apply**, because modern removed protocol-level sessions; that is a design finding rather
     than a parameter, which is why §9 asks it first though the probe is named for Q3.
  2. Q2 — whether an `Mcp-Session-Id` is issued at all. If not, `session_released` is
     `not_applicable` for this server and Q3/Q4 are moot **for it**, not for the design.
  3. Q3 — the RELEASE, over N sessions under stated conditions. The finding is "N of N answered
     alike", which is a statement about the sample; §9 is explicit that no finite probe
     establishes a universal. **Disagreement inside the sample is the interesting result** and
     settles the design question immediately — a server that releases only some sessions is one
     where the verdict needs a per-run record rather than a constant.
  4. Q4 — the IDLE LIFETIME against a window W, reported censored. See the cohort note below.
  5. CLEANUP — every session id this run learned of was released and OBSERVED gone. Asserted
     rather than attempted, and asserted last so it covers the sessions the questions above
     left behind, including the ones they could not read.

THREE THINGS THAT MAKE THIS A MEASUREMENT RATHER THAN A SEQUENCE OF REQUESTS:

**Liveness is tri-state, never a bool.** `session_alive` answers ALIVE, DEAD or UNREADABLE. A
connection reset is not a released session — it is the absence of a reading — and collapsing it
into "dead" would publish the instrument's own failure as the server's cleanest possible
behaviour. That is C3-1's lesson exactly ("a failed read is not a closed stdin"), where
swallowing an `OSError` into an empty chunk would have reported an instrument failure as a
clean shutdown, and it is the same shape a third time.

**And ALIVE is a fact about MCP, not about HTTP.** A 2xx says the transport worked; it does not
say the session did. The first cut read the status line alone, so an empty 200, a CDN
interstitial, or an SSE stream carrying only a priming event all answered ALIVE — and the
binding explicitly permits a server to send events before the response to the request just
sent, so "the first thing in the body" is not "the answer" either (external review). A verdict
now requires a JSON-RPC response **correlated by the id just sent**, with a fresh id per
request so nothing earlier can satisfy it; anything else 2xx is UNREADABLE, never DEAD.

**Every session id is released, including the ones that cannot be read.** A probe about session
cleanliness that leaks sessions is making the mess it exists to measure. `SessionLedger`
records each id the moment it is issued — before any question is asked of it, so a raise
between issue and use cannot lose it — and `main`'s `finally` releases everything outstanding
and *verifies* each one went, because a `DELETE` returning 200 is the server's claim and
whether the session is gone is an observation. Distinguishing those two is the entire subject
of this probe, so its own cleanup is held to the same standard.

**The 404 has to be attributable to the session.** A 404 can come from a path, a proxy or a CDN
— the default target sits behind CloudFront — so "the session is gone" needs the *same request*
to have succeeded while the session was alive. Every release therefore reads liveness twice,
before and after, with only the session state differing between them; the before-reading is the
positive control that makes the after-reading informative, on §4's rule that an assertion which
can only pass proves nothing. A fabricated session id is exercised too, and *recorded rather
than asserted*: it says what the server does with a session it never issued, which is what
separates "404 means this session was released" from "404 means any session id at all".

**AN IDLE SESSION'S LIFETIME CANNOT BE MEASURED BY POLLING IT** — polling is not idleness, and a
session kept alive by the instrument that is watching it is the instrument reporting on itself.
So Q4 runs COHORTS: one session per horizon, each touched exactly twice — opened, then observed
once at its own horizon — and no session is ever asked twice whether it is alive. A poll loop
over one session would have produced a number, and the number would have measured keep-alive
traffic. This is the C3-3 question ("is the quantity observable from where the instrument
stands at all") asked before the reading rather than after three rounds of making it careful.

W IS DERIVED, NOT PICKED. §9 says to set it from the longest eval this harness would actually
run, so it is read from `EvalSpec.timeout_sec`'s default — a bridge session lives for one proxy
instance, which is one cell, which that field bounds. Restating it as a literal here would let
the harness's cap move while the probe went on measuring against the old one.

AND THE ANSWER IS REPORTED CENSORED, in those words. A session still alive at W establishes
`lifetime > W`, censored — never "it does not expire". §9 asks for the reporting rule to be
written before the measurement because "no expiry observed" read as "no expiry" is the C3-3
mistake, and `survival_phrase` is where that rule lives so it can be checked rather than
remembered.
"""
from __future__ import annotations

import argparse
import dataclasses
import itertools
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.dirname(HERE)
sys.path.insert(0, HARNESS)

# IMPORTED, NOT RESTATED (§4). The era constants are the ones §10.2's gate actually enforces,
# so a probe that re-derived them could report a server "in the allowlist" that the proxy would
# refuse. The same argument covers W below: it is the harness's own cell cap, read from the
# field that sets it.
from agentskill_evals.mcp_proxy import (  # noqa: E402 — after the path bootstrap
    ERROR, IMPLEMENTED_VERSIONS, LEGACY_VERSIONS, MODERN_VERSIONS, RESULT,
    classify_envelope, request_id_key,
)
from agentskill_evals.spec import EvalSpec  # noqa: E402

DEFAULT_URL = "https://cmr.earthdata.nasa.gov/mcp/v1"
CLIENT_NAME = "sliderule-harness-probe-c3-4"
TIMEOUT = 30.0

# Liveness — tri-state on purpose; see the module docstring.
ALIVE = "ALIVE"
DEAD = "DEAD"
UNREADABLE = "UNREADABLE"

# Era, as this probe classifies a server's declared protocolVersion.
ERA_LEGACY = "legacy"
ERA_MODERN = "modern"
ERA_UNKNOWN = "unknown"      # outside §10.2's allowlist — an anomaly, not a third era
ERA_NONE = "no-handshake"    # nothing came back; not a reading

# Release dispositions.
RELEASED = "released"                # asked, accepted, and the session is gone afterwards
DECLINED = "declined"                # the server answers 405 — it does not offer termination
RETAINED = "retained"                # asked, and the session still answers: §10.10's anomaly
NOT_APPLICABLE = "not_applicable"    # no session was issued, so there is nothing to release
INDETERMINATE = "indeterminate"      # the instrument could not read it; NOT a server behaviour

def _cell_cap() -> int:
    """W's default: the harness's own per-cell timeout, read from the field that sets it.

    A bridge session lives for one proxy instance, which is one cell, which `timeout_sec`
    bounds — so this is the longest eval this harness would actually run, which is what §9
    asks W to be set from. Read rather than restated so the two cannot drift apart.
    """
    for f in dataclasses.fields(EvalSpec):
        if f.name == "timeout_sec":
            return int(f.default)
    raise RuntimeError("EvalSpec has no timeout_sec — W's derivation has lost its source")


W_DEFAULT = _cell_cap()

# Request ids, unique for the life of the process. Correlation is only as good as the id being
# unrepeatable: a fixed id lets a response to an earlier question satisfy a later one.
_IDS = itertools.count(1)

findings: list[str] = []


def check(label: str, ok: bool, detail="") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + ("" if ok else f"  <- {str(detail)[:400]}"))
    if not ok:
        findings.append(label)


# ---------------------------------------------------------------------------
# Transport. Small on purpose: the subject is the server, not this client.
# ---------------------------------------------------------------------------

class Reply:
    """One HTTP exchange, including the ones that never reached the server.

    `status is None` means no answer came back at all — kept distinct from every status code
    because "the request failed" and "the server said 404" are different facts, and the whole
    tri-state above collapses if they are merged here.
    """

    def __init__(self, status=None, headers=None, events=(), error=None, raw=""):
        self.status = status
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.events = tuple(events or ())
        self.error = error
        self.raw = raw

    @property
    def body(self):
        """The first parsed event, for diagnostics ONLY.

        Never for deciding anything: an SSE stream may carry priming events and unrelated
        server messages before the response to the request just sent, so "the first thing in
        the body" is not "the answer to my question". `rpc_response` is what a verdict reads.
        """
        return self.events[0] if self.events else None

    def rpc(self, want_id):
        return rpc_response(self.events, want_id)

    def __repr__(self) -> str:
        return f"<Reply {self.status} err={self.error!r} events={len(self.events)}>"


def parse_events(content_type: str, raw: str) -> tuple:
    """EVERY JSON object in the body, in order — not the first one.

    The default target answers ordinary POSTs with `text/event-stream`, which the Streamable
    HTTP binding permits and which a reader assuming `application/json` would fail to parse,
    reporting a malformed server where there is a conformant one. That much was always handled
    here. What was NOT is that the binding also permits a server to send priming events and
    unrelated messages on that stream *before* the response to the request just sent — so
    returning the first `data:` line returns whatever happened to arrive first, and a probe
    that then called the session ALIVE would be reading an unrelated event as its answer
    (external review). Correlation by request id is the only thing that makes a reply a reply,
    so this returns them all and `rpc_response` picks.
    """
    raw = (raw or "").strip()
    if not raw:
        return ()
    if "text/event-stream" in (content_type or "") or raw.startswith("event:"):
        out = []
        for line in raw.splitlines():
            if line.startswith("data:"):
                try:
                    out.append(json.loads(line[5:].strip()))
                except ValueError:
                    continue          # a malformed event is not the whole stream's fault
        return tuple(out)
    try:
        return (json.loads(raw),)
    except ValueError:
        return ()


def rpc_response(events, want_id):
    """The JSON-RPC response to `want_id`, or None — the only thing a verdict may read.

    **BOTH HALVES ARE IMPORTED RATHER THAN RE-DERIVED** (§4: where import is possible, import).
    The first cut hand-rolled them and got both wrong in ways that only a written-down rule
    catches (external review):

      * *Shape.* It required only that `jsonrpc` be PRESENT, so `"jsonrpc": "1.0"` passed, and
        an envelope carrying **both** `result` and `error` passed. `classify_envelope` is the
        function that already decides this, checks the fields the schema makes mandatory, and
        is what the proxy itself enforces — so a probe re-deriving it could accept a frame the
        proxy would refuse, and report a live session on it.
      * *Identity.* It compared with `==`, under which **`True == 1` and `False == 0`** in
        Python — so a response id of `true` answered a request on id `1`. `request_id_key` is
        the repo's own answer, and it is public precisely so a probe measures the identity the
        proxy enforces rather than one adjacent to it. It also makes `1` and `1.0` the same id,
        which is correct: a peer may echo either spelling of one JSON number.

    A `result` and an `error` both count as the session answering — "method not found" is a
    server talking to us, which is what liveness asks. An `error` with no `id` is schema-valid
    but uncorrelatable, so it is not an answer to us; `any_error_message` reads those instead.
    """
    want = request_id_key(want_id)
    for ev in events or ():
        if not isinstance(ev, dict):
            continue
        if classify_envelope(ev) not in (RESULT, ERROR):
            continue                       # malformed, a request, or a notification
        if "id" not in ev or request_id_key(ev["id"]) != want:
            continue
        return ev
    return None


def any_error_message(events):
    """The first `error.message` anywhere in the body, whatever id it carries.

    Deliberately NOT id-correlated: a server rejecting a dead session answers out-of-band —
    the live target replies `{"id": "server-error", ...}` to a request whose id was an integer
    — so requiring correlation here would discard exactly the message worth reading. It feeds
    reporting and the 404-discrimination qualifier, never a liveness verdict.
    """
    for ev in events or ():
        if isinstance(ev, dict) and isinstance(ev.get("error"), dict):
            message = ev["error"].get("message")
            if message:
                return message
    return None


def _send(url: str, method: str, payload=None, sid=None, version=None, headers=None) -> Reply:
    body = json.dumps(payload).encode() if payload is not None else None
    hdrs = {"Accept": "application/json, text/event-stream"}
    if body is not None:
        hdrs["Content-Type"] = "application/json"
    if sid:
        hdrs["Mcp-Session-Id"] = sid
    if version:
        hdrs["MCP-Protocol-Version"] = version
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode(errors="replace")
            return Reply(resp.status, dict(resp.headers),
                         parse_events(resp.headers.get("content-type", ""), raw), raw=raw)
    except urllib.error.HTTPError as exc:
        raw = (exc.read() or b"").decode(errors="replace")
        # A STATUS, NOT AN ERROR. `urllib` raises on 4xx/5xx, and treating that exception as a
        # transport failure would file every 404 — the exact answer this probe is looking for —
        # as INDETERMINATE, which is how a server that releases cleanly gets reported as
        # unmeasurable.
        return Reply(exc.code, dict(exc.headers or {}),
                     parse_events((exc.headers or {}).get("content-type", ""), raw), raw=raw)
    except Exception as exc:                                   # noqa: BLE001 — deliberate
        return Reply(error=f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Classification. Named functions, drivable on synthetic rows (CLAUDE.md).
# ---------------------------------------------------------------------------

def classify_era(version) -> str:
    """Which era a declared `protocolVersion` puts the server in.

    UNKNOWN is not a third era — it is §10.2's anomaly, a version outside the allowlist the
    proxy enforces. It is distinguished from ERA_NONE because "the server said something we do
    not implement" and "the server said nothing" license different actions.
    """
    if not version or not isinstance(version, str):
        return ERA_NONE
    if version in MODERN_VERSIONS:
        return ERA_MODERN
    if version in LEGACY_VERSIONS:
        return ERA_LEGACY
    return ERA_UNKNOWN


def sessions_apply(era: str, session_id) -> bool:
    """Whether protocol-level sessions are even in scope for this server.

    Modern removed them, so a modern server with no session id is conformant rather than
    deficient — and a modern server that *issues* one is the anomaly worth seeing, which is why
    this is a conjunction over both facts rather than a lookup on the era alone.
    """
    return era in (ERA_LEGACY, ERA_UNKNOWN) and bool(session_id)


def classify_liveness(reply: Reply, want_id) -> str:
    """ALIVE, DEAD or UNREADABLE for one session-bearing request.

    The middle case is the whole point. `reply.status is None` means nothing came back, and a
    5xx is the server failing rather than the session being gone; neither is evidence about the
    session, and calling either DEAD would credit the server with a release it never performed.

    **A 2xx IS A TRANSPORT FACT, NOT AN MCP FACT** (external review). This returned ALIVE for
    any 2xx — including an empty body, an HTML interstitial from a CDN, and an SSE stream whose
    only event was a priming ping — because the status line was all it read. The question the
    probe is actually asking is "does the SESSION still work", and the only evidence for that
    is the server answering *this request*: a JSON-RPC response correlated by the id just sent.
    Anything else 2xx is the absence of a reading, which is what UNREADABLE is for, and routing
    it there rather than to DEAD keeps the asymmetry the rest of this file depends on — an
    unreadable session is never counted as a released one.
    """
    if reply.status is None:
        return UNREADABLE
    if reply.status == 404:
        return DEAD
    if 200 <= reply.status < 300:
        return ALIVE if rpc_response(reply.events, want_id) is not None else UNREADABLE
    return UNREADABLE


def classify_release(delete: Reply, before: str, after: str) -> str:
    """What one session's release attempt established.

    Ordered so that the instrument's own failures cannot be read as server behaviour: if the
    session was not demonstrably ALIVE first, nothing after it is interpretable, because the
    "gone" reading has no control to be gone relative to.
    """
    if before != ALIVE:
        return INDETERMINATE
    if delete.status == 405:
        # The binding lets a server decline client-driven termination outright. This is the
        # case §10.10 named as possibly fatal, and it is a *server answer*, not a failure.
        return DECLINED
    if delete.status is None:
        return INDETERMINATE
    if after == DEAD:
        return RELEASED
    if after == ALIVE:
        return RETAINED
    return INDETERMINATE


def sample_verdict(dispositions) -> tuple[str, bool]:
    """The finding a sample of N licenses, and whether it is uniform.

    STRUCTURAL CLAUSE FIRST (§4): an empty sample is not agreement. `all()` over a list nothing
    was put into is true, and this function exists to be called where that would otherwise be
    the published result of a run in which no session was ever opened.
    """
    outcomes = list(dispositions)
    if not outcomes:
        return "no sessions were opened, so the sample establishes nothing", False
    kinds = sorted(set(outcomes))
    n = len(outcomes)
    if len(kinds) == 1:
        return f"{n} of {n} answered alike: {kinds[0]}", True
    tally = ", ".join(f"{k}={outcomes.count(k)}" for k in kinds)
    return (f"the sample DISAGREES over {n} sessions ({tally}) — the verdict needs a per-run "
            f"record rather than a constant"), False


def survival_phrase(horizon_s: int, state: str, w: int) -> str:
    """How one cohort's single observation is allowed to be reported.

    §9 asks for this rule in writing before the measurement, because "no expiry observed" read
    as "no expiry" is the C3-3 mistake. A surviving session yields a CENSORED bound and says so
    in the word "censored"; it never says the session does not expire, which is a claim about
    every time after W that no observation at W can support.
    """
    if state == ALIVE:
        bound = f"lifetime > {horizon_s}s, censored"
        return (f"{bound} (window W={w}s)" if horizon_s >= w
                else f"{bound} — short of W={w}s")
    if state == DEAD:
        return f"expired at or before {horizon_s}s of idleness"
    return f"UNREADABLE at {horizon_s}s — no observation, not a survival"


# ---------------------------------------------------------------------------
# The exchanges.
# ---------------------------------------------------------------------------

class SessionLedger:
    """Every session id this probe has LEARNED OF, and what became of it.

    §10.10 phrases the bridge's guarantee as "nothing this instance *learned of* is still
    alive" rather than "nothing it created", because an id can arrive for a session the process
    never went on to use. The same width applies to the instrument: **a probe about session
    cleanliness that leaks sessions is making the mess it exists to measure** (external
    review). The first cut leaked two ways — the handshake session was never released at all,
    and Q4 released only the cohorts it could still read, so an UNREADABLE session (which may
    well be alive; that is the whole point of the tri-state) was left running.

    Registration happens the moment an id is seen, BEFORE any question is asked of it, so an
    exception between issue and use cannot lose it. That ordering is the same one §10.10
    requires of the bridge's connect record, and for the same reason.

    THE NEGOTIATED VERSION IS PART OF THE PROVENANCE, not a run-wide constant (external
    review). Cleanup used the FIRST handshake's revision for every session, so a session that
    negotiated a different one was sent a `DELETE` under a revision it never agreed to and
    could survive — with its correct revision sitting in a variable one frame away. **A session
    id without the revision it was issued under is not enough to release it**, so the ledger
    carries both and `release_all` reads the pair.
    """

    def __init__(self):
        self.issued: list[str] = []
        self.versions: dict[str, str | None] = {}
        self.released: set[str] = set()

    def note(self, sid, version=None):
        if not sid:
            return sid
        if sid not in self.issued:
            self.issued.append(sid)
            self.versions[sid] = version
        elif version is not None:
            self.versions[sid] = version
        return sid

    def version_for(self, sid):
        return self.versions.get(sid)

    def mark_released(self, sid) -> None:
        if sid:
            self.released.add(sid)

    def outstanding(self) -> list[str]:
        return [s for s in self.issued if s not in self.released]


def collect_headers(header_args, header_env_args, environ) -> tuple[dict, list[str]]:
    """Extra request headers. EVERY value comes from the environment; none from argv.

    **A denylist of credential-bearing header names cannot establish "credentials never reach
    argv"** (external review). The first cut refused `Authorization`, `Cookie` and a few
    others — and passed `X-SlideRule-Token`, `X-Goog-Api-Key` and `Api-Key` straight through
    with their values, which is the motivating server's own header shape among them. The list
    can always be one name short, and the name it is short of is the one nobody thought of;
    a guarantee that depends on enumerating every future credential header is not a guarantee.

    So the property is made STRUCTURAL rather than enumerated: **no header value is ever read
    from the command line at all.** `--header-env NAME=VAR` names the variable holding the
    value, so only the variable's NAME reaches argv. §10.7 already treats argv as
    world-readable and this repo's own containment sweep runs `ps -eo command=`, so the
    exposure was demonstrated by neighbouring code rather than argued.

    `--header` still exists, and always refuses, pointing at the flag that works — a directed
    error for anyone porting a `curl` line, which is better than argparse's "unrecognized
    argument" and costs no exception to the rule above.

    THE VARIABLE HOLDS THE COMPLETE HEADER VALUE, scheme included: `Authorization=TOK` with
    `TOK='Bearer eyJ…'`, not the bare token. This function does not synthesize a scheme,
    because guessing one would silently send `Bearer <basic-credential>` for anybody using a
    different scheme.
    """
    headers, errors = {}, []
    for raw in header_args or ():
        name = (raw.partition(":")[0] or raw).strip() or "that header"
        errors.append(
            f"--header will not carry a value from the command line ({name!r}): argv is "
            f"world-readable, and a list of which header names are secret can always be one "
            f"name short. Use --header-env '{name}=VAR_NAME', with the variable holding the "
            f"COMPLETE value including any scheme, e.g. VAR_NAME='Bearer eyJ…'.")
    for raw in header_env_args or ():
        name, sep, var = raw.partition("=")
        name, var = name.strip(), var.strip()
        if not sep or not name or not var:
            errors.append(f"--header-env needs 'Name=ENV_VAR', got {raw!r}")
            continue
        if var not in environ:
            # NAMED, not silently skipped: a credential header that quietly fails to be set
            # produces an auth failure three layers away from its cause.
            errors.append(f"--header-env {name!r} names ${var}, which is not set")
            continue
        if not environ[var]:
            errors.append(f"--header-env {name!r} names ${var}, which is set but empty")
            continue
        headers[name] = environ[var]
    return headers, errors


def handshake_complete(session) -> bool:
    """Did THIS session's handshake finish — a successful `InitializeResult` that declared a
    protocol version, followed by an accepted `notifications/initialized`?

    **Asked of every session, not only the first** (external review). `probe_handshake` checked
    the one session it opened and every later `open_session` was read for its `sid` alone — so
    a sampled session whose handshake never completed was measured as though it had, and a row
    with `initialized=False` was published as `released` with no finding. A measurement taken
    through a handshake that did not happen is not a weaker measurement; it is a measurement of
    something else, and the sample cannot tell the two apart afterwards.

    **A CORRELATED RESPONSE IS NOT A SUCCESSFUL ONE** (external review, second round). This
    asked only that the response *exist*, and `rpc_response` returns errors too — deliberately,
    because for LIVENESS an error is the server talking to us. For a HANDSHAKE it is the
    opposite: `initialize` answered `-32602` is a session that never began, and the first cut
    read `handshake_complete=True` with `version=None` for exactly that. The two questions want
    opposite things from the same envelope, which is why this is a separate predicate rather
    than a reuse of the liveness one.
    """
    response = session.get("response")
    return (isinstance(response, dict) and isinstance(response.get("result"), dict)
            and bool(session.get("version")) and bool(session.get("initialized")))


def session_eligible(session, expected_version) -> tuple[bool, str]:
    """May this session's readings enter the measurement — and if not, in what words?

    ONE PREDICATE, CONSULTED BEFORE THE MEASUREMENT RATHER THAN REPORTED AFTER IT (external
    review). The previous cut checked handshakes and revisions in `check()` calls that ran once
    the sample was already taken: Q3 queried and deleted using the FIRST session's revision,
    then noticed the mismatch afterwards, and the "excluded" it printed was not excluded from
    anything — `sample_verdict` still saw the row. A gate that runs after the thing it gates is
    a report, and §4's rule is that a new fact about whether something is trustworthy joins the
    existing predicate rather than becoming a parallel flag one caller reads.

    The revision is part of it because a session that negotiated a different one is not a
    weaker sample of the same quantity — it is a sample of a different one, and averaging the
    two produces a number that describes neither.
    """
    response = session.get("response")
    if response is None:
        return False, "no correlated initialize response"
    if isinstance(response.get("error"), dict):
        err = response["error"]
        return False, (f"initialize returned an error: {err.get('code')!r} "
                       f"{str(err.get('message'))[:60]!r}")
    if not isinstance(response.get("result"), dict):
        return False, "initialize response carried no result object"
    if not session.get("version"):
        return False, "the initialize result declared no protocolVersion"
    if not session.get("initialized"):
        return False, "notifications/initialized was not accepted"
    if not session.get("sid"):
        return False, "no session id was issued"
    if expected_version is not None and session["version"] != expected_version:
        return False, (f"negotiated {session['version']!r}, not the run's "
                       f"{expected_version!r} — a different revision is a different quantity")
    return True, ""


def initialized_accepted(ack) -> bool:
    """Was `notifications/initialized` accepted?

    A notification has no response by definition, so the only evidence available is the
    transport status — 2xx, in practice 202. That is a weaker fact than a correlated response
    and is treated as one: it says the server took the message, not that it processed it.
    """
    return ack is not None and ack.status is not None and 200 <= ack.status < 300


def open_session(url: str, ledger: SessionLedger, headers=None) -> dict:
    """One COMPLETE initialize handshake: `initialize`, then `notifications/initialized`.

    **The notification is not optional and its absence was a real defect** (external review).
    The `2025-11-25` lifecycle says the client MUST send it before normal operations, so a
    probe that skipped it was measuring a server's tolerance of an incomplete handshake rather
    than the lifecycle the bridge will actually execute. The live target tolerated it, which is
    exactly why nothing here noticed: a server that did not would have failed loudly, and one
    that does leaves the measurement quietly describing a state no real client is ever in.
    """
    rid = next(_IDS)
    reply = _send(url, "POST", {
        "jsonrpc": "2.0", "id": rid, "method": "initialize",
        "params": {"protocolVersion": LEGACY_VERSIONS[0], "capabilities": {},
                   "clientInfo": {"name": CLIENT_NAME, "version": "1"}}}, headers=headers)
    response = reply.rpc(rid)
    result = response.get("result") if isinstance(response, dict) else None
    result = result if isinstance(result, dict) else {}
    version = result.get("protocolVersion")
    version = version if isinstance(version, str) and version else None
    # NOTED BEFORE ANYTHING ELSE HAPPENS, and noted WITH ITS REVISION — a failed handshake may
    # still have been issued an id, and an id whose revision is unknown is one cleanup cannot
    # address correctly. See SessionLedger.
    sid = ledger.note(reply.headers.get("mcp-session-id"), version)
    # THE NOTIFICATION FOLLOWS A SUCCESSFUL INITIALIZE, NOT MERELY A CORRELATED ONE. Sending it
    # after an error announces normal operation for a session that never began — and then the
    # server's 202 made the handshake look complete (external review).
    ack = None
    if version:
        ack = _send(url, "POST", {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    sid=sid, version=version, headers=headers)
    return {"reply": reply, "sid": sid, "version": version,
            "response": response, "ack": ack, "initialized": initialized_accepted(ack),
            "error": response.get("error") if isinstance(response, dict) else None,
            "server_info": result.get("serverInfo") or {},
            "capabilities": result.get("capabilities") or {},
            "framing": reply.headers.get("content-type", "")}


def session_alive(url: str, sid: str, version: str, headers=None) -> tuple[str, Reply]:
    """Is this session still usable? The same request shape every time it is asked.

    Deliberately `tools/list`: a read-only method every MCP server implements, so a failure is
    about the session rather than about what the server can do. **The id is fresh every time**
    — a constant would let a replayed, cached or interleaved response from an earlier question
    satisfy the correlation, which is the loophole `rpc_response` exists to close.
    """
    rid = next(_IDS)
    reply = _send(url, "POST", {"jsonrpc": "2.0", "id": rid, "method": "tools/list"},
                  sid=sid, version=version, headers=headers)
    return classify_liveness(reply, rid), reply


def release_session(url: str, sid: str, version: str, headers=None) -> Reply:
    return _send(url, "DELETE", None, sid=sid, version=version, headers=headers)


def release_all(url: str, version: str, ledger: SessionLedger, headers=None) -> list[dict]:
    """Release every outstanding session and CHECK each one went. Fail-closed.

    A `DELETE` returning 200 is the server's CLAIM; whether the session is gone is an
    observation, and distinguishing those two is the entire subject of this probe — so the
    cleanup is held to the same standard as the measurement rather than trusted because it is
    only cleanup. A session that will not go, or cannot be read, is reported.
    """
    rows = []
    for sid in ledger.outstanding():
        # EACH SESSION'S OWN REVISION, from the ledger. `version` is the run's, and using it
        # here sent a `DELETE` under a revision the session never agreed to — leaving alive
        # precisely the session whose correct revision was known (external review).
        per_session = ledger.version_for(sid) or version
        delete = release_session(url, sid, per_session, headers)
        state, reply = session_alive(url, sid, per_session, headers)
        if state == DEAD:
            ledger.mark_released(sid)
        rows.append({"sid": sid, "delete_status": delete.status, "after": state,
                     "message": any_error_message(reply.events)})
    return rows


def cleanup_report(url: str, version, ledger: SessionLedger, headers=None) -> list[dict]:
    """Release everything outstanding, say what happened, and FAIL if anything survived.

    Fail-closed, and loudly: this runs in `main`'s `finally`, so it is also what happens after
    an exception, which is the case no per-function cleanup covers. Against a credential-
    bearing server a surviving session is a live authenticated handle nobody is holding — which
    is precisely the condition §10.10 refuses to call clean, so an instrument that left one
    behind while measuring that refusal would be indefensible.
    """
    print("\ncleanup: every session this run learned of")
    if not ledger.issued:
        # PRINTED, NOT SILENTLY PASSED. `not outstanding` is trivially true over an empty
        # ledger, which is §4's "an assertion that passes because nothing was recorded" — so
        # the zero is stated rather than asserted over, and the run says which case it was.
        print("       no sessions were issued, so there is nothing to release and this run "
              "establishes nothing about cleanup")
        return []
    rows = release_all(url, version, ledger, headers) if ledger.outstanding() else []
    for r in rows:
        print(f"       {r['sid'][:12]}…  DELETE={r['delete_status']}  after={r['after']}"
              + (f"  {str(r['message'])[:48]}" if r["message"] else ""))
    left = ledger.outstanding()
    print(f"       {len(ledger.released)}/{len(ledger.issued)} released and observed gone")
    check("cleanup: every session this run learned of was released and OBSERVED gone — a probe "
          "about session cleanliness must not leak the thing it measures",
          not left,
          {"still outstanding": [s[:12] + "…" for s in left],
           "issued": len(ledger.issued), "released": len(ledger.released)})
    return rows


# ---------------------------------------------------------------------------
# Q1 + Q2 — the handshake, and what it settles before anything else runs.
# ---------------------------------------------------------------------------

def probe_handshake(url: str, ledger: SessionLedger, headers=None) -> dict:
    print("\nQ1/Q2: what the server speaks, and whether it issues a session")
    s = open_session(url, ledger, headers)
    reply = s["reply"]
    # A CORRELATED RESPONSE, not merely a body. `reply.body is not None` was satisfied by any
    # JSON at all — including an SSE priming event — which is the same defect `classify_liveness`
    # carried, one function over. Both are fixed by asking `rpc()` rather than reading position.
    check("the endpoint answers the initialize we sent — a JSON-RPC response correlated by id, "
          "which is the instrument's positive fact; without it every silence below is unreadable",
          reply.status is not None and s["response"] is not None,
          reply.error or reply.raw[:200])
    if s["response"] is None:
        return s
    check("...and the handshake COMPLETES — `notifications/initialized` accepted, which the "
          "2025-11-25 lifecycle requires before normal operation, and which the bridge will send",
          s["initialized"],
          {"ack_status": getattr(s["ack"], "status", None),
           "ack_error": getattr(s["ack"], "error", None)})
    era = classify_era(s["version"])
    s["era"] = era
    info = s["server_info"]
    print(f"       serverInfo: {info.get('name')!r} version {info.get('version')!r}")
    print(f"       protocolVersion: {s['version']!r} -> era {era}")
    print(f"       POST framing: {s['framing']!r}")
    print(f"       session id issued: {'yes' if s['sid'] else 'no'}")
    check("Q1: the declared protocol version is one §10.2's gate implements — outside the "
          "allowlist is an anomaly the proxy would refuse, not a server we can bridge to",
          s["version"] in IMPLEMENTED_VERSIONS,
          {"declared": s["version"], "allowlist": IMPLEMENTED_VERSIONS})
    # NOT AN ASSERTION — a finding. A modern server would mean §10.10's session machinery does
    # not apply to it, which is a design result rather than a failure of anything.
    if era == ERA_MODERN:
        print("       FINDING: modern era — protocol-level sessions were removed, so Q3/Q4 do "
              "not apply to this server and §10.10's bridge does not serve it as designed.")
    check("Q2: sessions are in scope for this server, and the era agrees with the evidence — "
          "a legacy server issuing an id, or a modern one issuing none",
          sessions_apply(era, s["sid"]) or (era == ERA_MODERN and not s["sid"]),
          {"era": era, "session_id": bool(s["sid"])})
    return s


# ---------------------------------------------------------------------------
# Q3 — the release, over a stated sample.
# ---------------------------------------------------------------------------

def probe_release(url: str, n: int, version: str, ledger: SessionLedger,
                  headers=None) -> list[dict]:
    print(f"\nQ3: does it accept client-driven termination? {n} sessions, stated sample")
    rows = []
    for i in range(n):
        s = open_session(url, ledger, headers)
        # THE GATE RUNS BEFORE THE MEASUREMENT, which is the whole of the fix. It used to run
        # after: the row was queried and deleted under the RUN's revision, and the mismatch was
        # noticed by a `check()` once the sample had already been taken (external review).
        eligible, why = session_eligible(s, version)
        if not eligible:
            rows.append({"i": i, "sid": s["sid"], "disposition": INDETERMINATE,
                         "before": None, "after": None,
                         "handshake": handshake_complete(s), "eligible": False,
                         "why": why, "version": s["version"]})
            print(f"       session {i}: NOT MEASURABLE — {why}")
            print(f"                  -> {INDETERMINATE}, excluded from the sample")
            continue
        # THIS SESSION'S OWN REVISION on this session's requests. Identical to `version` while
        # the gate holds, and written this way so it stays correct if the gate ever loosens.
        vs = s["version"]
        before, _ = session_alive(url, s["sid"], vs, headers)
        delete = release_session(url, s["sid"], vs, headers)
        after, after_reply = session_alive(url, s["sid"], vs, headers)
        if after == DEAD:
            ledger.mark_released(s["sid"])
        rows.append({"i": i, "sid": s["sid"], "before": before, "after": after,
                     "delete_status": delete.status, "handshake": True, "eligible": True,
                     "why": "", "version": vs,
                     "disposition": classify_release(delete, before, after),
                     "after_message": any_error_message(after_reply.events)})
        print(f"       session {i}: alive_before={before} DELETE={delete.status} "
              f"alive_after={after} -> {rows[-1]['disposition']}")

    # STRUCTURAL CLAUSE AHEAD OF EVERY UNIVERSAL (§4). Without this, a run in which no session
    # was ever opened satisfies the two `all()`s below and publishes uniform agreement.
    check(f"Q3: {n} sessions were actually opened and read — the sample exists",
          len(rows) == n and all(r.get("sid") for r in rows), rows)
    # THE SAMPLE IS THE ELIGIBLE ROWS, and this is where "excluded" becomes true rather than
    # printed. The previous cut computed the verdict over EVERY row, so two failed handshakes
    # published `2 of 2 answered alike: indeterminate` — an agreement among rows that had
    # measured nothing, next to a line claiming they were excluded (external review).
    measurable = [r for r in rows if r.get("eligible")]
    excluded = [r for r in rows if not r.get("eligible")]
    check("Q3: EVERY sampled session was measurable — a row taken through an incomplete "
          "handshake, or a different revision, is a reading of another quantity entirely",
          bool(rows) and not excluded,
          [(r["i"], r.get("why")) for r in excluded])
    # Kept explicit rather than left implied by the gate: this is the sentence §9 publishes,
    # and a check that restates it fails loudly if the gate is ever loosened underneath it.
    _versions = {r.get("version") for r in measurable}
    check("Q3: ...and every MEASURED session negotiated the same protocol revision, so the "
          "sample is one measurement rather than several averaged together",
          len(_versions) == 1 and _versions == {version}, sorted(map(str, _versions)))
    check("Q3: every measured session was demonstrably ALIVE before it was asked to terminate "
          "— the positive control, without which `gone afterwards` is not attributable to it",
          bool(measurable) and all(r["before"] == ALIVE for r in measurable),
          [r["before"] for r in measurable])
    dispositions = [r["disposition"] for r in measurable]
    phrase, uniform = sample_verdict(dispositions)
    print(f"       SAMPLE: {phrase} — over {len(measurable)} of {n} sessions"
          + (f", {len(excluded)} excluded" if excluded else ""))
    check("Q3: the sample is uniform — a split sample is the interesting result and means the "
          "verdict must be recorded per run rather than fixed as a constant",
          uniform, dispositions)
    check("Q3: no measured session's reading was INDETERMINATE — an unreadable session is the "
          "instrument failing, and it must not be counted as a server behaviour",
          bool(dispositions) and INDETERMINATE not in dispositions, dispositions)
    return rows


def discriminates_release(released_messages, unknown_message) -> bool:
    """Does the server's 404 distinguish 'this session was released' from 'never heard of it'?

    Purely a qualifier on how much the Q3 reading says, and never an assertion: a server is
    under no obligation to tell the two apart, and one that answers both the same way is still
    conformant. But when it DOES tell them apart, the post-DELETE 404 is attributable to the
    release rather than to a generic unknown-session path, which is a stronger reading of the
    same measurement — so it is worth naming rather than leaving in a transcript.
    """
    said = {m for m in released_messages if m}
    return bool(said) and bool(unknown_message) and unknown_message not in said


def probe_unknown_session(url: str, version: str, released_messages, headers=None) -> None:
    """What a session id the server never issued gets — RECORDED, not asserted.

    This is what separates "404 means this session was released" from "404 means any session
    id at all". It is not a pass/fail because either answer is conformant; it is printed
    because the Q3 reading is weaker if the server 404s indiscriminately, and a reader of the
    result deserves to know which.
    """
    print("\ncontrol: a session id the server never issued")
    fake = uuid.uuid4().hex
    state, reply = session_alive(url, fake, version, headers)
    message = any_error_message(reply.events)
    print(f"       fabricated session -> {state} (http {reply.status}) {str(message)[:80]}")
    said = sorted({m for m in released_messages if m})
    print(f"       a RELEASED session said -> {said or '(nothing recorded)'}")
    if discriminates_release(released_messages, message):
        print("       the server DISTINGUISHES the two, so a post-DELETE 404 is attributable "
              "to the release rather than to a generic unknown-session path")
    else:
        print("       the server does NOT distinguish them: a post-DELETE 404 says only that "
              "the id is unusable, which is the weaker of the two readings")


# ---------------------------------------------------------------------------
# Q4 — idle lifetime, by cohorts. One session per horizon, observed once.
# ---------------------------------------------------------------------------

def probe_survival(url: str, horizons, w: int, version: str, ledger: SessionLedger,
                   headers=None) -> list[dict]:
    print(f"\nQ4: idle lifetime against W={w}s — one session per horizon, each observed ONCE")
    print("       (a session polled repeatedly is not idle; see the module docstring)")
    cohorts = []
    for h in horizons:
        s = open_session(url, ledger, headers)
        eligible, why = session_eligible(s, version)
        cohorts.append({"horizon": h, "sid": s["sid"], "opened_at": time.time(),
                        "handshake": handshake_complete(s), "version": s["version"],
                        "eligible": eligible, "why": why, "state": None, "phrase": None})
    check("Q4: every cohort session was opened — an unopened cohort observes nothing, and a "
          "silent nothing is indistinguishable from an expiry",
          bool(cohorts) and all(c["sid"] for c in cohorts),
          [(c["horizon"], bool(c["sid"])) for c in cohorts])
    # A COHORT IS A SESSION IN NORMAL OPERATION, or it is not a cohort. An idle-lifetime
    # reading taken on a half-initialized session, or on one that negotiated a different
    # revision, measures the server's treatment of THAT — a different quantity wearing this
    # one's units. ONE PREDICATE, the same one Q3 uses.
    _ineligible = [c for c in cohorts if not c["eligible"]]
    check("Q4: every cohort completed its handshake and negotiated the same revision — an "
          "idle session that never entered normal operation is a different quantity",
          bool(cohorts) and not _ineligible,
          [(c["horizon"], c["why"]) for c in _ineligible])
    # THE EARLY RETURN CARRIES THE SAME PREDICATE. It used to test `sid and handshake`, which
    # omitted the revision — so a cohort that negotiated a different one went on to be observed
    # and reported as a survival (external review).
    if _ineligible:
        for c in _ineligible:
            print(f"       idle {c['horizon']:>4}s: NOT MEASURABLE — {c['why']}")
        return cohorts

    results: dict[int, dict] = {}

    def observe(c):
        # Sleep to this cohort's OWN horizon, measured from when it was opened, then read once.
        delay = c["opened_at"] + c["horizon"] - time.time()
        if delay > 0:
            time.sleep(delay)
        # THIS COHORT'S OWN REVISION, for the same reason Q3 uses the row's.
        state, reply = session_alive(url, c["sid"], c["version"], headers)
        c["state"] = state
        c["phrase"] = survival_phrase(c["horizon"], state, w)
        c["http"] = reply.status
        results[c["horizon"]] = c

    threads = [threading.Thread(target=observe, args=(c,), daemon=True) for c in cohorts]
    for t in threads:
        t.start()
    # Bounded join: the horizon plus the request timeout plus slack. A cohort thread that never
    # returns must not hang the probe — it must be reported as the missing observation it is.
    deadline = time.time() + max(horizons) + TIMEOUT + 30
    for t in threads:
        t.join(timeout=max(0.0, deadline - time.time()))

    for c in sorted(cohorts, key=lambda x: x["horizon"]):
        said = c["phrase"] or "NO OBSERVATION — the cohort thread did not report"
        print(f"       idle {c['horizon']:>4}s: {said}")
    check("Q4: every cohort produced an observation — a thread that never reported is a "
          "missing reading, and absence of a positive result is not a negative result",
          len(results) == len(cohorts) and all(c["state"] for c in cohorts),
          [(c["horizon"], c["state"]) for c in cohorts])
    survivors = [c for c in cohorts if c["state"] == ALIVE]
    if survivors:
        longest = max(c["horizon"] for c in survivors)
        print(f"       BOUND: idle sessions survive at least {longest}s — "
              f"lifetime > {longest}s, CENSORED. This is not 'they do not expire'.")
    # NO CLEANUP HERE. It used to release only the cohorts still reading ALIVE, which left an
    # UNREADABLE one running — and UNREADABLE explicitly does not mean gone; that asymmetry is
    # the point of the tri-state (external review). Every session is in the ledger and every
    # outstanding one is released in `main`'s `finally`, so cleanup happens once, covers the
    # sessions this function could not read, and covers the ones it never reached because
    # something raised.
    return cohorts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--sessions", type=int, default=5,
                    help="N for Q3's stated sample (default 5)")
    ap.add_argument("--window", type=int, default=W_DEFAULT,
                    help=f"W for Q4, seconds (default {W_DEFAULT}, the harness cell cap)")
    ap.add_argument("--horizons", default="",
                    help="comma-separated idle horizons; default 60,W/2,W")
    ap.add_argument("--skip-survival", action="store_true",
                    help="Q1-Q3 only — skips the timed hold, which takes W seconds")
    ap.add_argument("--header", action="append", default=[], metavar="'Name: value'",
                    help="ALWAYS REFUSED — no header value is read from the command line. "
                         "Kept so the error names --header-env instead of argparse's")
    ap.add_argument("--header-env", action="append", default=[], metavar="'Name=ENV_VAR'",
                    help="header whose VALUE is read from the named environment variable, "
                         "which must hold the COMPLETE value including any scheme (e.g. "
                         "'Bearer eyJ...'). Only the variable name reaches argv")
    args = ap.parse_args()

    headers, header_errors = collect_headers(args.header, args.header_env, os.environ)
    if header_errors:
        for message in header_errors:
            print(message)
        return 2

    w = args.window
    horizons = ([int(x) for x in args.horizons.split(",") if x.strip()] if args.horizons
                else sorted({60, max(60, w // 2), w}))

    print(f"C3-4 against {args.url}")
    print(f"date: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print("A reading is about the server it was taken from; §9 records which one and when.\n")

    # ONE LEDGER FOR THE RUN, and one release point. Every id is registered as it is issued and
    # everything outstanding is released in the `finally` below — including on the paths that
    # raise, which is the half no per-function cleanup can cover.
    ledger = SessionLedger()
    version = None
    try:
        s = probe_handshake(args.url, ledger, headers)
        if s["response"] is None:
            print("\nFAILED: the endpoint did not complete a handshake — nothing below is "
                  "readable")
            return 1
        version = s["version"]
        if not s["sid"]:
            print("\nNo session id issued: `session_released` is `not_applicable` for THIS "
                  "server. Q3 and Q4 are moot for it, and unchanged for the design.")
        else:
            rows = probe_release(args.url, args.sessions, version, ledger, headers)
            probe_unknown_session(args.url, version,
                                  [r.get("after_message") for r in rows], headers)
            if args.skip_survival:
                print(f"\nQ4: SKIPPED by --skip-survival. W={w}s is unmeasured, which is not "
                      f"the same as unbounded.")
            else:
                eta = time.strftime('%H:%M:%S', time.localtime(time.time() + max(horizons)))
                print(f"\nQ4 holds sessions idle for up to {max(horizons)}s — done about {eta}.")
                probe_survival(args.url, horizons, w, version, ledger, headers)
    finally:
        cleanup_report(args.url, version, ledger, headers)

    print()
    print("FAILED: " + ", ".join(findings) if findings else "ALL PASS")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
