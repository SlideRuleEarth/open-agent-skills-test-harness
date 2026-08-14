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
    python3 tools/probe_session_mcp.py --header 'Authorization: Bearer ...'

**A READING IS ABOUT THE SERVER IT WAS TAKEN FROM.** C3-4 is named for the SlideRule server and
§8's motivating pattern is a bearer token against it; the default target here is NASA's public
Earthdata endpoint, which is a *different server* and needs no credential. So this answers "does
a real conformant server release a session" and leaves "does SlideRule" open. The report names
its target, its `serverInfo` and the date for that reason, and `--url` exists so the same
procedure runs against the other one when access arrives rather than being rewritten for it.

WHAT IT ASSERTS, one numbered failure mode each, in §9's question order:

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

THREE THINGS THAT MAKE THIS A MEASUREMENT RATHER THAN A SEQUENCE OF REQUESTS:

**Liveness is tri-state, never a bool.** `session_alive` answers ALIVE, DEAD or UNREADABLE. A
connection reset is not a released session — it is the absence of a reading — and collapsing it
into "dead" would publish the instrument's own failure as the server's cleanest possible
behaviour. That is C3-1's lesson exactly ("a failed read is not a closed stdin"), where
swallowing an `OSError` into an empty chunk would have reported an instrument failure as a
clean shutdown, and it is the same shape a third time.

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
    IMPLEMENTED_VERSIONS, LEGACY_VERSIONS, MODERN_VERSIONS,
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

    def __init__(self, status=None, headers=None, body=None, error=None, raw=""):
        self.status = status
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.body = body
        self.error = error
        self.raw = raw

    def __repr__(self) -> str:
        return f"<Reply {self.status} err={self.error!r} body={str(self.body)[:120]}>"


def parse_body(content_type: str, raw: str):
    """JSON, whether it arrived as JSON or framed as SSE.

    The default target answers ordinary POSTs with `text/event-stream`, which the Streamable
    HTTP binding permits and which a reader assuming `application/json` would fail to parse —
    reporting a malformed server where there is a conformant one. So the framing is handled
    here rather than assumed anywhere upstream.
    """
    raw = raw.strip()
    if not raw:
        return None
    if "text/event-stream" in (content_type or "") or raw.startswith("event:"):
        for line in raw.splitlines():
            if line.startswith("data:"):
                try:
                    return json.loads(line[5:].strip())
                except ValueError:
                    return None
        return None
    try:
        return json.loads(raw)
    except ValueError:
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
                         parse_body(resp.headers.get("content-type", ""), raw), raw=raw)
    except urllib.error.HTTPError as exc:
        raw = (exc.read() or b"").decode(errors="replace")
        # A STATUS, NOT AN ERROR. `urllib` raises on 4xx/5xx, and treating that exception as a
        # transport failure would file every 404 — the exact answer this probe is looking for —
        # as INDETERMINATE, which is how a server that releases cleanly gets reported as
        # unmeasurable.
        return Reply(exc.code, dict(exc.headers or {}),
                     parse_body((exc.headers or {}).get("content-type", ""), raw), raw=raw)
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


def classify_liveness(reply: Reply) -> str:
    """ALIVE, DEAD or UNREADABLE for one session-bearing request.

    The middle case is the whole point. `reply.status is None` means nothing came back, and a
    5xx is the server failing rather than the session being gone; neither is evidence about the
    session, and calling either DEAD would credit the server with a release it never performed.
    """
    if reply.status is None:
        return UNREADABLE
    if reply.status == 404:
        return DEAD
    if 200 <= reply.status < 300:
        return ALIVE
    if reply.status >= 500:
        return UNREADABLE
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

def open_session(url: str, headers=None) -> dict:
    """One initialize handshake. Returns everything the later questions read."""
    reply = _send(url, "POST", {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": LEGACY_VERSIONS[0], "capabilities": {},
                   "clientInfo": {"name": CLIENT_NAME, "version": "1"}}}, headers=headers)
    result = (reply.body or {}).get("result") or {}
    return {"reply": reply, "sid": reply.headers.get("mcp-session-id"),
            "version": result.get("protocolVersion"),
            "server_info": result.get("serverInfo") or {},
            "capabilities": result.get("capabilities") or {},
            "framing": reply.headers.get("content-type", "")}


def session_alive(url: str, sid: str, version: str, headers=None) -> tuple[str, Reply]:
    """Is this session still usable? The same request shape every time it is asked.

    Deliberately `tools/list`: a read-only method every MCP server implements, so a failure is
    about the session rather than about what the server can do.
    """
    reply = _send(url, "POST", {"jsonrpc": "2.0", "id": 99, "method": "tools/list"},
                  sid=sid, version=version, headers=headers)
    return classify_liveness(reply), reply


def release_session(url: str, sid: str, version: str, headers=None) -> Reply:
    return _send(url, "DELETE", None, sid=sid, version=version, headers=headers)


# ---------------------------------------------------------------------------
# Q1 + Q2 — the handshake, and what it settles before anything else runs.
# ---------------------------------------------------------------------------

def probe_handshake(url: str, headers=None) -> dict:
    print("\nQ1/Q2: what the server speaks, and whether it issues a session")
    s = open_session(url, headers)
    reply = s["reply"]
    check("the endpoint answers an initialize at all — the instrument's positive fact, "
          "without which every silence below is unreadable",
          reply.status is not None and reply.body is not None,
          reply.error or reply.raw[:200])
    if reply.body is None:
        return s
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

def probe_release(url: str, n: int, version: str, headers=None) -> list[dict]:
    print(f"\nQ3: does it accept client-driven termination? {n} sessions, stated sample")
    rows = []
    for i in range(n):
        s = open_session(url, headers)
        if not s["sid"]:
            rows.append({"i": i, "disposition": NOT_APPLICABLE, "before": None, "after": None})
            continue
        before, _ = session_alive(url, s["sid"], version, headers)
        delete = release_session(url, s["sid"], version, headers)
        after, after_reply = session_alive(url, s["sid"], version, headers)
        rows.append({"i": i, "sid": s["sid"], "before": before, "after": after,
                     "delete_status": delete.status,
                     "disposition": classify_release(delete, before, after),
                     "after_message": ((after_reply.body or {}).get("error") or {}).get("message")})
        print(f"       session {i}: alive_before={before} DELETE={delete.status} "
              f"alive_after={after} -> {rows[-1]['disposition']}")

    # STRUCTURAL CLAUSE AHEAD OF EVERY UNIVERSAL (§4). Without this, a run in which no session
    # was ever opened satisfies the two `all()`s below and publishes uniform agreement.
    check(f"Q3: {n} sessions were actually opened and read — the sample exists",
          len(rows) == n and all(r.get("sid") for r in rows), rows)
    check("Q3: every session was demonstrably ALIVE before it was asked to terminate — the "
          "positive control, without which `gone afterwards` is not attributable to the DELETE",
          bool(rows) and all(r["before"] == ALIVE for r in rows),
          [r["before"] for r in rows])
    dispositions = [r["disposition"] for r in rows]
    phrase, uniform = sample_verdict(dispositions)
    print(f"       SAMPLE: {phrase}")
    check("Q3: the sample is uniform — a split sample is the interesting result and means the "
          "verdict must be recorded per run rather than fixed as a constant",
          uniform, dispositions)
    check("Q3: no session's reading was INDETERMINATE — an unreadable session is the "
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
    message = ((reply.body or {}).get("error") or {}).get("message")
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

def probe_survival(url: str, horizons, w: int, version: str, headers=None) -> list[dict]:
    print(f"\nQ4: idle lifetime against W={w}s — one session per horizon, each observed ONCE")
    print("       (a session polled repeatedly is not idle; see the module docstring)")
    cohorts = []
    for h in horizons:
        s = open_session(url, headers)
        cohorts.append({"horizon": h, "sid": s["sid"], "opened_at": time.time(),
                        "state": None, "phrase": None})
    check("Q4: every cohort session was opened — an unopened cohort observes nothing, and a "
          "silent nothing is indistinguishable from an expiry",
          bool(cohorts) and all(c["sid"] for c in cohorts),
          [(c["horizon"], bool(c["sid"])) for c in cohorts])
    if not all(c["sid"] for c in cohorts):
        return cohorts

    results: dict[int, dict] = {}

    def observe(c):
        # Sleep to this cohort's OWN horizon, measured from when it was opened, then read once.
        delay = c["opened_at"] + c["horizon"] - time.time()
        if delay > 0:
            time.sleep(delay)
        state, reply = session_alive(url, c["sid"], version, headers)
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
    # Release the cohorts rather than leaving live sessions behind. A probe about session
    # cleanliness that leaks sessions would be making the mess it exists to measure.
    for c in cohorts:
        if c["state"] == ALIVE:
            release_session(url, c["sid"], version, headers)
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
                    help="extra request header, repeatable (e.g. a bearer token)")
    args = ap.parse_args()

    headers = {}
    for raw in args.header:
        name, _, value = raw.partition(":")
        if not value.strip():
            print(f"--header needs 'Name: value', got {raw!r}")
            return 2
        headers[name.strip()] = value.strip()

    w = args.window
    horizons = ([int(x) for x in args.horizons.split(",") if x.strip()] if args.horizons
                else sorted({60, max(60, w // 2), w}))

    print(f"C3-4 against {args.url}")
    print(f"date: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print("A reading is about the server it was taken from; §9 records which one and when.\n")

    s = probe_handshake(args.url, headers)
    if s["reply"].body is None:
        print("\nFAILED: the endpoint did not complete a handshake — nothing below is readable")
        return 1
    version = s["version"]
    if not s["sid"]:
        print("\nNo session id issued: `session_released` is `not_applicable` for THIS server. "
              "Q3 and Q4 are moot for it, and unchanged for the design.")
    else:
        rows = probe_release(args.url, args.sessions, version, headers)
        probe_unknown_session(args.url, version,
                              [r.get("after_message") for r in rows], headers)
        if args.skip_survival:
            print(f"\nQ4: SKIPPED by --skip-survival. W={w}s is unmeasured, which is not the "
                  f"same as unbounded.")
        else:
            eta = time.strftime('%H:%M:%S', time.localtime(time.time() + max(horizons)))
            print(f"\nQ4 holds sessions idle for up to {max(horizons)}s — done about {eta}.")
            probe_survival(args.url, horizons, w, version, headers)

    print()
    print("FAILED: " + ", ".join(findings) if findings else "ALL PASS")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
