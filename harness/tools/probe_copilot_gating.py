#!/usr/bin/env python3
"""Probe C2-copilot: is copilot's per-server `tools:` a REAL boundary, or advisory?

OPT-IN. Needs `copilot` on PATH and spends model calls. Nothing in the verification block
runs it; `verify_mcp_fixtures.py` drives its classifiers offline, because "nothing routine
runs it" is exactly how a fix lands in one copy and not the other (§4).

WHY THIS PROBE EXISTS AND WHY ITS ANSWER IS WORTH A MODEL CALL. The identical question was
asked of claude in DESIGN_MCP_Support.md §6-C2 and the answer inverted the expectation:
`--allowedTools` does NOTHING to MCP tools under `--dangerously-skip-permissions`. That
single measurement is the entire reason C3 — a harness-owned filtering proxy — exists. copilot
advertises a per-server `tools` array in its own MCP config, which is a different CLI's
different mechanism, and it deserves the same measurement rather than the same assumption. If
it holds, copilot needs no proxy: `mcp_tool_filter = "native"`, and §8's remote pattern is
reachable without the transport bridge. If it does not, copilot joins claude behind C3.

WHAT IS OBSERVED, AND FROM WHERE. The only fact this instrument can establish is **whether an
off-list `tools/call` ARRIVED AT THE SERVER**, read from receipts the server writes to a file
this process reads afterwards. That is deliberate and it is the lesson C3-3 paid for: a
measurement is worth what its vantage point can see, and the model's own account of what tools
it had is not evidence about a filter.

**IT DOES NOT DISTINGUISH ADVERTISEMENT-FILTERING FROM CALL-BLOCKING, and does not need to.**
If copilot strips `add` from `tools/list` before the model sees it, no call is made; if copilot
lets the model try and refuses the call, none arrives either. Both are hard boundaries, which
is the property under test.

TWO THINGS MUST BE SEPARATED FROM THAT, and the first version of this probe only separated one.
*No filter at all* is what the CONTROL is for — the same scenario with `tools:` omitted, in
which `add` must arrive; without it, "no `add` at the server" is satisfied by a model that
simply never tried. *An off switch* is the other, and it went unnoticed through a full green
run: if `tools:` suppresses the server's tools wholesale, `add` does not arrive either, and
every observable is identical to a working filter for as long as nothing asks for the ALLOWED
tool. The original prompt named only the off-list tool, so `called(echo)=False` in every arm of
both probes and the verdict read ENFORCED off it. The prompt now asks for both, and the gated
arm carries a claim of each sign: `echo` must arrive, `add` must not. An allowlist that admits
nothing is not a boundary the harness can declare `native` — it is a server that does not work.

WHAT THE EXIT STATUS MEANS, and it is the strong claim rather than the weak one. **0 means
this run supports declaring `mcp_tool_filter = "native"`**: the filter held, the allowed tool
answered, and the run identified the build it measured. `LEAKED`, `SUPPRESSES_ALL` and
`ANSWER_LOST` are definite ANSWERS — the findings this probe exists to produce — and they exit
1, because a caller reading only the status must never read "LEAKED" as permission. `UNMEASURED`
and `INSTRUMENT_FAILED` are runs that settled nothing and exit 1 as well; the printed verdict is
what distinguishes them. An earlier revision wired the status to "the question was settled" and
described it as certification, which are not the same set (review, PR #110).

    python tools/probe_copilot_gating.py            # both arms; prints the tally either way
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

HARNESS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HARNESS)
ECHO = os.path.join(HARNESS, "fixtures", "echo_mcp_server.py")
sys.path.insert(0, os.path.join(HARNESS, "fixtures"))

# IMPORTED, NOT RETYPED. §4: where import is possible, import — a probe holding its own copy of
# the sentinel would keep asking for `@generate` after the fixture renamed it, and the server
# would then treat the sentinel as a literal marker, which is the failure mode this whole
# clause exists to prevent.
from echo_mcp_server import IDENTITY_GENERATE  # noqa: E402 — after the path insert

from agentskill_evals.adapters.copilot import _stream_cli_version  # noqa: E402 — the run's own witness

_VERSION_RE = re.compile(r"\d+\.\d+\.\d+")

ALLOWED = "echo"
OFF_LIST = "add"
DEADLINE = 180.0

# Verdicts. Named rather than spelled at the one site that tests them, for the reason
# `ENFORCING_TOOL_FILTERS` is: two readers must agree about a vocabulary.
ENFORCED = "ENFORCED"          # ungated call arrived; gated call did not
LEAKED = "LEAKED"              # gated call arrived — the filter is advisory, as claude's was
UNMEASURED = "UNMEASURED"      # the control never called it, so the gated arm proves nothing
SUPPRESSES_ALL = "SUPPRESSES_ALL"         # nothing arrived gated, ALLOWED included: an off switch
ANSWER_LOST = "ANSWER_LOST"               # the allowed call arrived; its reply never came back
INSTRUMENT_FAILED = "INSTRUMENT_FAILED"   # a server that never ran, or receipts that never came


def read_receipts(path: str) -> list[dict]:
    """Every well-formed record, and nothing else.

    A malformed line is SKIPPED rather than fatal, and that is not leniency: this file is
    appended to by a subprocess that may be killed mid-write, so a truncated final line is an
    ordinary ending. What must not happen is a traceback standing in for a measurement.
    """
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
    """The premise every negative below rests on. A `listening` record is written before the
    first request is read, so its absence means the server never started — under which
    'the off-list call never arrived' is true and means nothing."""
    return any(r.get("kind") == "listening" for r in records)


def called(records: list[dict], tool: str) -> bool:
    return any(r.get("kind") == "request" and r.get("method") == "tools/call"
               and r.get("tool") == tool for r in records)


_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
# The minted marker is `uuid4().hex`: 32 lowercase hex characters. Candidates are pulled out of
# the transcript by that shape and HASHED, so the driver recognises the marker without ever
# holding it and the CLI cannot manufacture it from anything it can read.
_CANDIDATE_RE = re.compile(r"[0-9a-f]{32}")


def minted_digest(records: list[dict]) -> str:
    """The sha256 the SERVER reported for its minted marker, or "" if there is none.

    A DIGEST RATHER THAN THE MARKER, because the receipts path is in the config the CLI reads
    and the file lands in the CLI's working directory under `--allow-all`. Minting moved the
    marker out of the config; it left it readable one hop away, so a file-read tool could put
    it in the transcript with no reply having returned. sha256 is not invertible, so this
    channel can carry recognition without carrying the secret (review, PR #110).

    SHAPE-VALIDATED, which is the other half. This used to accept any string, so a receipt
    reporting the literal `@generate` sentinel — the exact state a broken mint produces — was
    accepted as a marker, and a transcript containing that word scored ENFORCED. The offline
    verifier caught the mutation; the measurement executable itself failed open.
    """
    for record in records:
        if record.get("kind") == "listening":
            digest = record.get("identity_digest")
            return digest if isinstance(digest, str) and _DIGEST_RE.match(digest) else ""
    return ""


def reply_carried_marker(transcript: str, digest: str) -> bool:
    """Whether the transcript contains a token whose sha256 is `digest`.

    The empty digest is never satisfied: a server that minted nothing cannot have answered.
    """
    if not digest:
        return False
    return any(hashlib.sha256(tok.encode("utf-8")).hexdigest() == digest
               for tok in set(_CANDIDATE_RE.findall(transcript or "")))


def control_verdict(control: list[dict]) -> tuple[str, str] | None:
    """The verdict the CONTROL alone already decides, or None if the gated arm is needed.

    ONE AUTHORITY, TWO CALLERS. `classify` consults it first, and `main` consults it to skip
    the second model call — the short-circuit its comment promised and never had, which spent
    a call whose result could not change the answer. Duplicating the condition in `main`
    instead would be a second copy of the rule that decides what a run means.
    """
    if not server_ran(control):
        return INSTRUMENT_FAILED, ("the echo server did not start in the control arm, so an "
                                   "absent call says nothing about a filter")
    # BOTH TOOLS MUST HAVE BEEN EXERCISED UNGATED, not just the off-list one. The gated arm is
    # read for two facts of opposite sign, so the control has to establish that the model
    # reaches for each of them when nothing is stopping it.
    if not called(control, OFF_LIST) or not called(control, ALLOWED):
        return UNMEASURED, (f"the CONTROL called {OFF_LIST}={called(control, OFF_LIST)} "
                            f"{ALLOWED}={called(control, ALLOWED)}; whichever it skipped, the "
                            f"gated arm's reading for that tool is the model's choice rather "
                            f"than the filter's doing")
    return None


def classify(gated: list[dict], control: list[dict], answered: bool) -> tuple[str, str]:
    """(verdict, one-line reason) from the two arms' receipts.

    `answered` IS REQUIRED, with no default. A default would have to be `True` to keep the
    existing calls working, which is the permissive value — a caller that forgot it would get
    ENFORCED for free, and the clause added to close a hole would open it one level up.

    A FUNCTION, not an `elif` chain inside `main()`, so it can be driven on synthetic rows —
    §4's rule for probes, and the reason C3-2's classifier has its own checks. Every branch
    below is reachable from a hand-written pair of record lists.
    """
    decided = control_verdict(control)
    if decided is not None:
        return decided
    if not server_ran(gated):
        return INSTRUMENT_FAILED, ("the echo server did not start in the gated arm, so an "
                                   "absent call says nothing about a filter")
    if called(gated, OFF_LIST):
        return LEAKED, (f"{OFF_LIST!r} reached the server despite `tools: [{ALLOWED!r}]` — the "
                        f"filter is advisory, exactly as claude's `--allowedTools` measured "
                        f"(§6-C2), and copilot needs C3's proxy like claude does")
    # THE HYPOTHESIS THE FIRST VERSION COULD NOT ELIMINATE. Every observable of a CLI that
    # suppresses the whole server whenever `tools:` is present is identical to one that filters
    # correctly, as long as nothing asks for the ALLOWED tool — and the first prompt asked only
    # for the off-list one, so both probes reported ENFORCED off `called(echo)=False` in every
    # arm. An allowlist that admits nothing is not a boundary the harness can declare `native`;
    # it is a server that does not work (review of my own instrument, 2026-08-12).
    if not called(gated, ALLOWED):
        return SUPPRESSES_ALL, (f"neither tool reached the server under `tools: [{ALLOWED!r}]`, "
                                f"though the control called both. `tools:` is acting as an off "
                                f"switch rather than a filter, so it cannot back `native`")
    # ARRIVING IS NOT WORKING, and the difference is the whole of what a scenario depends on.
    # Everything above is read from the server's receipts, which say a request came IN; none of
    # them can see whether its answer came back OUT. A client that forwards the call and then
    # drops, truncates or suppresses the response satisfies every clause so far, and a harness
    # that declared `native` on that strength would gate correctly onto a tool that returns
    # nothing. The marker is minted BY THE SERVER, is never in the prompt, never in the config,
    # and never in the receipts — those carry only its digest — so the model reproducing a
    # token that hashes to it is the round trip completing, and no file the CLI can read is a
    # second route to the same value.
    if not answered:
        return ANSWER_LOST, (f"{ALLOWED!r} reached the server under the allowlist and its reply "
                             f"never came back: the run's output does not contain the opaque "
                             f"marker the tool returns, so the call is gated but not usable")
    return ENFORCED, (f"{OFF_LIST!r} arrived ungated and did NOT arrive under "
                      f"`tools: [{ALLOWED!r}]`, while {ALLOWED!r} DID arrive in that same run "
                      f"AND its opaque reply came back — a filter rather than an off switch, "
                      f"and the allowed tool is usable, so copilot can be `native`")


def mcp_config(path: str, receipts: str, *, tools: list[str] | None) -> str:
    """copilot's MCP config for one echo server, with or without the allowlist under test.

    The KEY SPELLING IS THE OTHER OPEN PROBE (§9 #3) and this file does not settle it: it
    writes the shape `DESIGN_MCP_Support.md` §3 records, and a wrong key would show up as a
    server that never starts — which `server_ran` reports as INSTRUMENT_FAILED rather than as
    a filter result. Run `probe_copilot_config.py` first; that is what it is for.
    """
    # NO MARKER TRAVELS THROUGH HERE — only the instruction to MINT one. Two earlier versions
    # of this line were wrong in the same direction: the marker in this file, which copilot
    # reads, and then the marker in copilot's own environment, which copilot and any shell tool
    # it launches can dump. Both put the value where "it appeared in the output" is satisfiable
    # without a reply. `@generate` leaves the marker in the server's memory, its receipts file
    # and its replies, and nowhere copilot can passively reach (review, PR #110).
    server: dict = {"command": sys.executable, "args": [ECHO],
                    "env": {"ECHO_MCP_RECEIPTS": receipts,
                            "ECHO_MCP_IDENTITY": IDENTITY_GENERATE}}
    if tools is not None:
        server["tools"] = list(tools)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"mcpServers": {"echo": server}}, handle)
    return path


def run_arm(workdir: str, *, tools: list[str] | None) -> tuple[list[dict], str]:
    """One copilot run. Returns (receipts, stdout+stderr) — the transcript for diagnosis only.

    The PROMPT names BOTH tools explicitly, in the order that matters. A probe that asked
    politely and let the model decide would measure the model's inclination, not the filter,
    and a negative would then be indistinguishable from a model that saw no reason to call
    anything. Naming only the off-list tool — which is what this asked first — leaves the
    allowed one uncalled in every arm, and an allowlist that admits nothing then reads exactly
    like one that works. The ALLOWED tool goes first so it is reached before the model meets
    the unavailable one, and the instruction to carry on is what stops a refusal there from
    ending the run before the second half of the measurement exists.
    """
    receipts = os.path.join(workdir, f"receipts-{uuid.uuid4().hex}.jsonl")
    config = mcp_config(os.path.join(workdir, "mcp-config.json"), receipts, tools=tools)
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
        transcript = (done.stdout or "") + (done.stderr or "")
    except FileNotFoundError:
        return [], "copilot is not on PATH"
    except subprocess.TimeoutExpired:
        transcript = f"copilot exceeded {DEADLINE}s"
    return read_receipts(receipts), transcript


def settled(verdict: str) -> bool:
    """Whether the run ANSWERED the question, either way. `LEAKED`, `SUPPRESSES_ALL` and
    `ANSWER_LOST` are three definite ways the answer is "copilot cannot be `native`";
    `UNMEASURED` and `INSTRUMENT_FAILED` are runs that answered nothing."""
    return verdict in (ENFORCED, LEAKED, SUPPRESSES_ALL, ANSWER_LOST)


def certifies_native(verdict: str, version_ok: bool) -> bool:
    """Whether this run supports declaring `mcp_tool_filter = "native"` — the EXIT STATUS.

    TWO DIFFERENT QUESTIONS, and conflating them is what review caught: `settled` was wired to
    the exit status under a comment claiming the run "certified" the filter, so a `LEAKED`
    result — the finding this probe exists to catch — exited 0 alongside an `ENFORCED` one. A
    caller acting on the status would have declared `native` on the strength of a measurement
    that said the opposite. Exit 0 here means the strong thing, and a settled non-`ENFORCED`
    verdict is printed loudly and exits 1 (review, PR #110).

    The VERSION joins it because the result is version-qualified: a run that could not say
    which build it measured certifies nothing, whatever the filter did.
    """
    return verdict == ENFORCED and version_ok


def version_verdict(rc: int, out: str, err: str) -> tuple[str, bool]:
    """(text, usable) for a `copilot --version` result — the PREFLIGHT reading.

    Kept for probes that never run a model session, and NOT used to gate a measurement: a
    preflight is a different execution from the run, and copilot's launcher can resolve a
    different cached app.js between the two (see `adapters/copilot.py:_stream_cli_version`,
    which is why that adapter reads the version out of the run's own stream). Gating on this
    would identify a build that may not be the one that answered (review, PR #110).

    SHAPE-CHECKED, because `rc == 0` with any text on stdout used to count: a run whose only
    output was a warning returned "usable" while naming no version at all.
    """
    text = (out or "").strip() or (err or "").strip()
    if rc != 0:
        return (text or f"exit {rc}"), False
    return (text, bool(_VERSION_RE.search(text)))


def agreed_version(streams: list[str]) -> tuple[str, bool]:
    """(text, usable) over EVERY arm that actually ran.

    ONE WITNESS PER EXECUTED ARM, AND THEY MUST AGREE. Reading the version from one arm and
    deciding on another is not a version check: the stdio probe read `control_out` and then
    launched the gated arm separately, so a control at 1.0.79 and a gated run at 9.9.9 exited 0
    reporting 1.0.79 — driven by review, exactly so. Concatenating the streams instead, as the
    remote probe did, is the one-sided form of the same hole: one witnessed arm plus one arm
    with no witness at all reads as witnessed.

    So: every stream must yield a witness, and the set must be a singleton. Absence in ANY
    executed arm is unverified, because the arm with no witness is the one that could have been
    a different build (review, PR #110).
    """
    if not streams:
        return "(no runs to witness)", False
    found = [_stream_cli_version(s or "") for s in streams]
    if any(f is None for f in found):
        return (f"an executed arm produced no in-band version witness "
                f"({[f or '-' for f in found]})"), False
    if len(set(found)) != 1:
        return (f"the arms did not run the same build: "
                f"{sorted(str(f) for f in set(found))}"), False
    return found[0], True


def cli_version() -> tuple[str, bool]:
    """`copilot --version`, and whether it is usable. See `version_verdict`."""
    try:
        done = subprocess.run(["copilot", "--version"], capture_output=True, text=True,
                              timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"could not run `copilot --version`: {exc!r}", False
    return version_verdict(done.returncode, done.stdout, done.stderr)


def main() -> int:
    workdir = tempfile.mkdtemp(prefix="probe-copilot-gating-")
    try:
        # THE CONTROL RUNS FIRST, and if it already decides the verdict the gated arm is not
        # worth a second model call — which this said and did not do until review pointed at
        # the line (PR #110).
        control, control_out = run_arm(workdir, tools=None)
        decided = control_verdict(control)
        if decided is not None:
            verdict, reason = decided
            version, version_ok = agreed_version([control_out])
            print(f"copilot: {version}")
            print(f"probe C2-copilot: {verdict}\n  {reason}")
            print(f"  control: server_ran={server_ran(control)} "
                  f"called({OFF_LIST})={called(control, OFF_LIST)} "
                  f"called({ALLOWED})={called(control, ALLOWED)} records={len(control)}")
            print("  the gated arm was NOT run: its reading could not change this verdict")
            print("  --- control transcript ---")
            print("  " + (control_out or "").strip()[:1200].replace("\n", "\n  "))
            return 1
        gated, gated_out = run_arm(workdir, tools=[ALLOWED])
        # EVERY ARM THAT RAN, and they must agree — the gated arm is the one the verdict is
        # read from, so versioning the control alone identified the wrong execution.
        version, version_ok = agreed_version([control_out, gated_out])
        print(f"copilot: {version}")
        # THE ROUND TRIP, read from the GATED arm because that is the run whose usability is
        # in question. What comes back out of the receipts is a DIGEST; the marker itself never
        # leaves the server except in a reply, so `answered` asks whether the transcript holds
        # a token that hashes to it — a fact about the reply rather than about anything copilot
        # could read.
        answered = reply_carried_marker(gated_out, minted_digest(gated))
        verdict, reason = classify(gated, control, answered)

        # PRINTED ON EVERY RUN, pass or fail. A green line shows no detail, the receipts are
        # deleted on the way out, and this result is version-qualified — a claim with no way
        # to see what it rested on expires silently (review, PR #106).
        print(f"probe C2-copilot: {verdict}")
        print(f"  {reason}")
        print(f"  control: server_ran={server_ran(control)} "
              f"called({OFF_LIST})={called(control, OFF_LIST)} "
              f"called({ALLOWED})={called(control, ALLOWED)} records={len(control)}")
        print(f"  gated:   server_ran={server_ran(gated)} "
              f"called({OFF_LIST})={called(gated, OFF_LIST)} "
              f"called({ALLOWED})={called(gated, ALLOWED)} "
              f"reply_came_back={answered} records={len(gated)}")
        if verdict != ENFORCED:
            print("  --- control transcript ---")
            print("  " + (control_out or "").strip()[:1200].replace("\n", "\n  "))
            print("  --- gated transcript ---")
            print("  " + (gated_out or "").strip()[:1200].replace("\n", "\n  "))
        if not version_ok:
            print("  VERSION UNREADABLE: this result is qualified by a copilot build it could "
                  "not identify, so it certifies nothing whatever the filter did")
        elif settled(verdict) and verdict != ENFORCED:
            print(f"  SETTLED, AND THE ANSWER IS NO: {verdict} is a definite result and it "
                  f"says copilot cannot be declared `native` on this evidence")
        # EXIT 0 IS THE STRONG CLAIM — see `certifies_native`. A settled-but-negative verdict is
        # a finding, printed above and exiting 1, because a caller reading only the status must
        # never read "LEAKED" as permission.
        return 0 if certifies_native(verdict, version_ok) else 1
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
