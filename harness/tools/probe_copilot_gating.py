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

THE ANSWER THAT IS NOT A FAILURE. A run in which the model never calls a tool even ungated
answers nothing about the filter, and saying so is the point: `UNMEASURED` is a verdict here,
not an error. `LEAKED` and `SUPPRESSES_ALL` are the two ways the answer can be "copilot cannot
be `native`" — definite results, exit 0 — and the exit status separates all of those from a run
that measured nothing.

    python tools/probe_copilot_gating.py            # both arms; prints the tally either way
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
ECHO = os.path.join(HARNESS, "fixtures", "echo_mcp_server.py")
sys.path.insert(0, os.path.join(HARNESS, "fixtures"))

# IMPORTED, NOT RETYPED. §4: where import is possible, import — a probe holding its own copy of
# the sentinel would keep asking for `@generate` after the fixture renamed it, and the server
# would then treat the sentinel as a literal marker, which is the failure mode this whole
# clause exists to prevent.
from echo_mcp_server import IDENTITY_GENERATE  # noqa: E402 — after the path insert

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


def minted_identity(records: list[dict]) -> str:
    """The marker the SERVER minted, read out of its own startup receipt.

    The driver never chooses it. Every value the driver could choose has to travel to the
    server through the CLI under test — its config or its environment — which leaves the CLI
    holding a copy before any tool runs, and "the marker appeared in the output" then has a
    route that is not a reply (review, PR #110, twice).
    """
    for record in records:
        if record.get("kind") == "listening":
            ident = record.get("identity")
            return ident if isinstance(ident, str) else ""
    return ""


def classify(gated: list[dict], control: list[dict], answered: bool) -> tuple[str, str]:
    """(verdict, one-line reason) from the two arms' receipts.

    `answered` IS REQUIRED, with no default. A default would have to be `True` to keep the
    existing calls working, which is the permissive value — a caller that forgot it would get
    ENFORCED for free, and the clause added to close a hole would open it one level up.

    A FUNCTION, not an `elif` chain inside `main()`, so it can be driven on synthetic rows —
    §4's rule for probes, and the reason C3-2's classifier has its own checks. Every branch
    below is reachable from a hand-written pair of record lists.
    """
    if not server_ran(control) or not server_ran(gated):
        return INSTRUMENT_FAILED, ("the echo server did not start in one or both arms, so an "
                                   "absent call says nothing about a filter")
    # BOTH TOOLS MUST HAVE BEEN EXERCISED UNGATED, not just the off-list one. The gated arm is
    # read for two facts of opposite sign, so the control has to establish that the model
    # reaches for each of them when nothing is stopping it.
    if not called(control, OFF_LIST) or not called(control, ALLOWED):
        return UNMEASURED, (f"the CONTROL called {OFF_LIST}={called(control, OFF_LIST)} "
                            f"{ALLOWED}={called(control, ALLOWED)}; whichever it skipped, the "
                            f"gated arm's reading for that tool is the model's choice rather "
                            f"than the filter's doing")
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
    # nothing. The marker is opaque, travels to the server in its ENVIRONMENT, is never in the
    # prompt, and comes back only inside the tool's result — so the model repeating it is the
    # round trip completing (review, third instance of this family in one file).
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


def main() -> int:
    workdir = tempfile.mkdtemp(prefix="probe-copilot-gating-")
    try:
        version, version_ok = cli_version()
        print(f"copilot: {version}")
        # THE CONTROL RUNS FIRST, deliberately. If it cannot get the model to call the tool at
        # all, the gated arm is not worth a second model call and the tally says why.
        control, control_out = run_arm(workdir, tools=None)
        gated, gated_out = run_arm(workdir, tools=[ALLOWED])
        # THE ROUND TRIP, read from the GATED arm because that is the run whose usability is
        # in question. The marker is MINTED BY THE SERVER and read back out of its receipts —
        # never chosen here — so copilot's only route to it is a tool reply, and `answered` is
        # a fact about that reply rather than about anything copilot could dump.
        marker = minted_identity(gated)
        answered = bool(marker) and marker in (gated_out or "")
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
