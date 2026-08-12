#!/usr/bin/env python3
"""Probe C2-copilot-remote: does copilot's `tools:` hold on a REMOTE server — §8's pattern?

OPT-IN. Needs `copilot` on PATH and spends model calls.

WHY IT IS A SEPARATE PROBE FROM `probe_copilot_gating.py`. That one measured a stdio server and
found the filter ENFORCED. This asks the same question of the shape the harness actually exists
to run: **remote `url` + `Authorization: Bearer …` + a `tools:` allowlist** (§8). Those are two
different code paths inside a CLI — claude's own `--allowedTools` behaves differently on MCP
tools than the flag name suggests (§6-C2), and a filter that holds over a spawned subprocess is
not thereby a filter that holds over an HTTP client. **Assuming the stdio result carries is the
same inference this document has already been wrong about twice**, so it is measured instead.

WHAT IS OBSERVED, from where. `fixtures/http_mcp_server.py` writes one receipt per request
carrying the HTTP verb, the path, every header as received, and — for `tools/call` — the tool
NAME. So the arrival of an off-list call is a server-side fact read from a file this process
opens afterwards, never the model's account of itself. The fixture is REUSED through
`probe_remote_mcp.py`'s `start_fixture`, not reimplemented: that function already carries the
reap-on-every-failure-path fix, and a second copy is a second thing to fix (§4).

THE THREE FACTS THIS SETTLES, and they are separable:

  1. does the declared `Authorization` header reach the server at all (probe #1 established
     this for claude; copilot is a different client and inherits nothing);
  2. does an off-list `tools/call` arrive when `tools:` names only the other tool;
  3. does the on-list tool actually work UNDER THE ALLOWLIST — without which "nothing arrived"
     is equally true of a `tools:` that suppresses the server wholesale rather than filtering
     it. This was claimed here and not measured: the prompt named only the off-list tool, so
     `called(echo)=False` in both arms and the verdict read ENFORCED off it. The gated arm now
     carries a claim of each sign, and `SUPPRESSES_ALL` is the verdict when only one holds.

    python tools/probe_copilot_remote_gating.py        # prints the tally either way
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid

HARNESS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HARNESS, "tools"))

from probe_remote_mcp import start_fixture           # noqa: E402 — after the path bootstrap

sys.path.insert(0, os.path.join(HARNESS, "fixtures"))
from echo_mcp_server import IDENTITY_GENERATE        # noqa: E402 — imported, never retyped

ALLOWED = "echo"
OFF_LIST = "add"
DEADLINE = 240.0

ENFORCED = "ENFORCED"
LEAKED = "LEAKED"
UNMEASURED = "UNMEASURED"
SUPPRESSES_ALL = "SUPPRESSES_ALL"         # nothing arrived gated, ALLOWED included: an off switch
ANSWER_LOST = "ANSWER_LOST"               # the allowed call arrived; its reply never came back
INSTRUMENT_FAILED = "INSTRUMENT_FAILED"

# BOTH REMOTE TRANSPORTS THE SCHEMA ADMITS, because a claim covers what it measured and the
# harness's `url:` server may be either. §4's rule about a promise stated wider than its
# mechanism, arriving in a measurement: the first version of this probe measured Streamable
# HTTP and the result was written up as "both transports" — true of stdio and http, and silent
# about the third thing copilot exposes. `sse` here is copilot's own discriminator as
# `probe_copilot_config.py` reads it back; a wrong spelling produces a server nothing ever
# reaches, which `server_ran` reports as INSTRUMENT_FAILED rather than as a filter result.
TRANSPORTS = (("http", "streamable"), ("sse", "sse"))


def read_receipts(path: str) -> list[dict]:
    """Well-formed records only; a truncated final line is an ordinary ending, not a crash."""
    out: list[dict] = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except OSError:
        return []
    return out


def server_ran(records: list[dict]) -> bool:
    """The fixture announces `listening` before it serves anything. Without this, every
    negative below is equally true of a server that never started."""
    return any(r.get("kind") == "listening" for r in records)


def called(records: list[dict], tool: str) -> bool:
    return any(r.get("kind") == "rpc" and r.get("method") == "tools/call"
               and r.get("tool") == tool for r in records)


def credential_arrived(records: list[dict], sentinel: str) -> bool:
    """Whether the declared bearer token reached the server, on EVERY request that carried
    headers — not on at least one. A client that sends it once and drops it thereafter is a
    different animal from one that sends it always, and probe #1 already had to repair exactly
    that weakening in its version-header check (review, PR #106)."""
    seen = [r for r in records if r.get("kind") == "request"]
    if not seen:
        return False
    # THE WHOLE VALUE, not a substring of it. `sentinel in value` is satisfied by
    # `Bearer <sentinel>-altered` and by anything else that merely CONTAINS the token, so a
    # client that appended, wrapped or re-encoded the declared header would have passed while
    # the server received something the harness never declared. What §8 needs to know is that
    # the header arrived intact, which is an equality (review, PR #110).
    expected = f"Bearer {sentinel}"
    return all((r.get("headers") or {}).get("authorization", "") == expected for r in seen)


def minted_identity(records: list[dict]) -> str:
    """The marker the SERVER minted, read out of its own startup receipt — see the stdio
    probe. This transport never had the leak that one did, since `start_fixture` sets the knob
    on the FIXTURE's process and copilot cannot see it; both use the minted form anyway, so
    there is one mechanism to reason about rather than two with different guarantees."""
    for record in records:
        if record.get("kind") == "listening":
            ident = record.get("identity")
            return ident if isinstance(ident, str) else ""
    return ""


def classify(gated: list[dict], control: list[dict], answered: bool) -> tuple[str, str]:
    """(verdict, reason) — a named function so every branch is drivable on synthetic rows.

    `answered` is REQUIRED and has no default: the only default that would keep older calls
    working is the permissive one, and a caller that forgot it would be handed ENFORCED.
    """
    if not server_ran(control) or not server_ran(gated):
        return INSTRUMENT_FAILED, "the fixture did not start in one or both arms"
    # BOTH TOOLS UNGATED, because the gated arm is read for two facts of opposite sign.
    if not called(control, OFF_LIST) or not called(control, ALLOWED):
        return UNMEASURED, (f"the CONTROL called {OFF_LIST}={called(control, OFF_LIST)} "
                            f"{ALLOWED}={called(control, ALLOWED)} over HTTP; whichever it "
                            f"skipped, the gated arm's reading for that tool is the model's "
                            f"doing rather than the filter's")
    if called(gated, OFF_LIST):
        return LEAKED, (f"{OFF_LIST!r} reached the REMOTE server despite `tools: "
                        f"[{ALLOWED!r}]` — §8's pattern is not gated on copilot, and the "
                        f"stdio result did not carry")
    if not called(gated, ALLOWED):
        return SUPPRESSES_ALL, (f"neither tool reached the REMOTE server under `tools: "
                                f"[{ALLOWED!r}]`, though the control called both over HTTP. "
                                f"§8's pattern is not usable: the allowlist is an off switch")
    # ARRIVING IS NOT WORKING. Every clause above is read from receipts, which record a request
    # coming IN and can say nothing about its answer going OUT — so a client that forwards the
    # call and drops the response satisfies all of them. The marker is opaque, reaches the
    # server in its environment, is never in the prompt, and returns only inside the tool's
    # result: the model repeating it is the round trip closing.
    if not answered:
        return ANSWER_LOST, (f"{ALLOWED!r} reached the REMOTE server under the allowlist and "
                             f"its reply never came back: the opaque marker the tool returns "
                             f"is absent from the run's output, so §8's pattern gates a call "
                             f"that produces nothing")
    return ENFORCED, (f"{OFF_LIST!r} arrived ungated and did NOT arrive under `tools: "
                      f"[{ALLOWED!r}]`, while {ALLOWED!r} DID arrive in that same run AND its "
                      f"opaque reply came back — §8's pattern is enforced and usable on "
                      f"copilot today")


def mcp_config(path: str, url: str, sentinel: str, kind: str,
               *, tools: list[str] | None) -> str:
    """The remote entry in copilot's own spelling — `type`/`url`/`headers`/`tools`, as
    measured by `probe_copilot_config.py` rather than taken from documentation. `type` in
    particular is a key nothing in §3 had, and copilot writes `http` for the Streamable shape
    and `sse` for the legacy one — `kind` is which of those this arm is measuring."""
    server: dict = {"type": kind, "url": url,
                    "headers": {"Authorization": f"Bearer {sentinel}"}}
    if tools is not None:
        server["tools"] = list(tools)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"mcpServers": {"echo": server}}, handle)
    return path


def run_arm(workdir: str, url: str, sentinel: str, kind: str, *, tools: list[str] | None):
    receipts_before = None
    config = mcp_config(os.path.join(workdir, f"cfg-{uuid.uuid4().hex}.json"), url, sentinel,
                        kind, tools=tools)
    # BOTH TOOLS, ALLOWED FIRST — see `classify`. Naming only the off-list one leaves the
    # allowed one uncalled in every arm, which is exactly what an off switch looks like.
    # The reply is quoted VERBATIM so the opaque marker survives into the output, which is
    # what turns "the call arrived" into "the call arrived and answered".
    prompt = (f"Do both of these, in order, using the `echo` MCP server. "
              f"First: call the {ALLOWED} tool with the text HELLO, and quote the tool's "
              f"reply back VERBATIM, exactly as it returned it. "
              f"Second: call the {OFF_LIST} tool to add 2 and 3. "
              f"Report both results. If one of them is unavailable, say exactly "
              f"NO_SUCH_TOOL:<name> for that one and still do the other.")
    argv = ["copilot", "-p", prompt,
            "--no-custom-instructions", "--disable-builtin-mcps", "--no-remote",
            "--additional-mcp-config", f"@{config}",
            "--output-format", "json", "--allow-all"]
    try:
        done = subprocess.run(argv, cwd=workdir, capture_output=True, text=True,
                              timeout=DEADLINE)
        return (done.stdout or "") + (done.stderr or ""), receipts_before
    except FileNotFoundError:
        return "copilot is not on PATH", receipts_before
    except subprocess.TimeoutExpired:
        return f"copilot exceeded {DEADLINE}s", receipts_before


def settled(verdict: str) -> bool:
    """Whether this transport's arms ANSWERED the question, either way."""
    return verdict in (ENFORCED, LEAKED, SUPPRESSES_ALL, ANSWER_LOST)


def certifies_native(verdict: str, bearer_ok: bool, version_ok: bool) -> bool:
    """Whether ONE transport supports declaring `native` — a conjunction over every fact.

    THE NAME AND THE MEANING USED TO DISAGREE, which is exactly the defect this file keeps
    finding in itself. The exit status was `settled and bearer_ok`, under a comment saying a
    measured SSE failure could not ride out under a green Streamable result — but `LEAKED` is
    settled, so `http: ENFORCED` plus `sse: LEAKED` exited 0 and the PR body's claim was false
    of its own code. Certification is `ENFORCED`, an intact bearer, and a readable version;
    everything else is a settled answer that says NOT to declare `native` (review, PR #110).
    """
    return verdict == ENFORCED and bearer_ok and version_ok


def version_verdict(rc: int, out: str, err: str) -> tuple[str, bool]:
    """(text, usable) for a `copilot --version` result.

    PURE, so §E19 can drive every branch without a copilot install — and SEPARATE from the
    subprocess call for the same reason every other classifier here is. A measurement whose
    write-up is qualified "at 1.0.79" is worth nothing if the run could not read a version:
    this used to return a string like "(version unreadable: ...)" and `main` carried on, so
    the probe would print a verdict attributed to a build it never identified (review, PR #110).
    """
    text = (out or "").strip() or (err or "").strip()
    if rc != 0:
        return (text or f"exit {rc}"), False
    return (text, bool(text))


def cli_version() -> tuple[str, bool]:
    """`copilot --version`, and whether it is usable. See `version_verdict`."""
    try:
        done = subprocess.run(["copilot", "--version"], capture_output=True, text=True,
                              timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"could not run `copilot --version`: {exc!r}", False
    return version_verdict(done.returncode, done.stdout, done.stderr)


def measure(workdir: str, kind: str, endpoint: str, sentinel: str):
    """One transport, both arms. Returns (verdict, reason, results, bearer_ok, answered)."""
    # ONE RECEIPTS FILE PER ARM, because the fixture appends and two arms sharing a file
    # would let the control's `add` satisfy the gated arm's check — the two runs agreeing
    # with each other rather than each being measured (§4).
    results = {}
    for label, tools in (("control", None), ("gated", [ALLOWED])):
        receipts = os.path.join(workdir, f"receipts-{kind}-{label}.jsonl")
        # `(proc, info)` on success and `(None, reason)` on failure — the announcement is
        # already parsed there, so this does not re-parse it. Re-deriving a contract the
        # helper already establishes is how the two copies drift.
        proc, info = start_fixture(receipts, IDENTITY_GENERATE)
        if proc is None:
            return INSTRUMENT_FAILED, f"fixture: {info}", {}, False, False
        try:
            out, _ = run_arm(workdir, info[endpoint], sentinel, kind, tools=tools)
        finally:
            proc.kill()
            proc.wait(timeout=15)
        results[label] = (read_receipts(receipts), out)
    gated_out = results["gated"][1]
    marker = minted_identity(results["gated"][0])
    answered = bool(marker) and marker in (gated_out or "")
    verdict, reason = classify(results["gated"][0], results["control"][0], answered)
    bearer_ok = all(credential_arrived(recs, sentinel) for recs, _out in results.values())
    return verdict, reason, results, bearer_ok, answered


def main() -> int:
    workdir = tempfile.mkdtemp(prefix="probe-copilot-remote-")
    sentinel = uuid.uuid4().hex
    try:
        version, version_ok = cli_version()
        print(f"copilot: {version}")
        if not version_ok:
            print("  VERSION UNREADABLE: this result is qualified by a copilot build it could "
                  "not identify, so it certifies nothing whatever the filter did")
        # EVERY TRANSPORT THE SCHEMA ADMITS, and the exit status is a conjunction of
        # CERTIFICATIONS over them — not of "settled", which is the bug review found: `LEAKED`
        # is settled, so an SSE leak beside a green Streamable result exited 0 under a comment
        # promising it could not. Exit 0 now means every transport measured ENFORCED with an
        # intact bearer and a readable version.
        ok = True
        for kind, endpoint in TRANSPORTS:
            verdict, reason, results, bearer_ok, answered = measure(
                workdir, kind, endpoint, sentinel)
            print(f"probe C2-copilot-remote [{kind}]: {verdict}")
            print(f"  {reason}")
            for label, (recs, _out) in results.items():
                print(f"  {label:<8} server_ran={server_ran(recs)} "
                      f"called({OFF_LIST})={called(recs, OFF_LIST)} "
                      f"called({ALLOWED})={called(recs, ALLOWED)} "
                      f"bearer_intact_on_every_request={credential_arrived(recs, sentinel)} "
                      f"records={len(recs)}")
            if results:
                print(f"  gated reply_came_back={answered}")
            # FACT 1 IS ASSERTED, NOT MERELY PRINTED — the same defect as the on-list tool, one
            # field over. `credential_arrived` appeared only in the tally, so a copilot that
            # dropped the declared `Authorization` header would have produced a green ENFORCED
            # and exit 0 while the credential half of §8's pattern silently failed. A fact this
            # file says it settles has to be able to fail it.
            if not bearer_ok:
                print("  FACT 1 UNPROVEN: the declared bearer did not arrive INTACT on every "
                      "request in both arms, so §8's credential path is not established by "
                      "this run whatever the filter did")
            good = certifies_native(verdict, bearer_ok, version_ok)
            if verdict != ENFORCED or not bearer_ok:
                for label, (_recs, out) in results.items():
                    print(f"  --- {label} ---\n  "
                          + (out or "").strip()[:1000].replace("\n", "\n  "))
            if settled(verdict) and not good:
                print(f"  SETTLED, AND THE ANSWER IS NO for {kind}: {verdict} is a definite "
                      f"result and it says §8's pattern cannot be declared `native` here")
            ok = ok and good
        return 0 if ok else 1
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
