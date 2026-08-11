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
is the property under test. What must be separated from them is *no filter at all*, and that is
what the CONTROL is for — the same scenario with `tools:` omitted, in which `add` must arrive.
Without that control, "no `add` at the server" is satisfied by a model that simply never tried,
and this file would report a working filter for a CLI that has none (§4).

THE THIRD RESULT IS NOT A FAILURE. A run in which the model never calls `add` even ungated
answers nothing about the filter, and saying so is the point: `UNMEASURED` is a verdict here,
not an error, and the exit status distinguishes it from a filter that leaked.

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

ALLOWED = "echo"
OFF_LIST = "add"
DEADLINE = 180.0

# Verdicts. Named rather than spelled at the one site that tests them, for the reason
# `ENFORCING_TOOL_FILTERS` is: two readers must agree about a vocabulary.
ENFORCED = "ENFORCED"          # ungated call arrived; gated call did not
LEAKED = "LEAKED"              # gated call arrived — the filter is advisory, as claude's was
UNMEASURED = "UNMEASURED"      # the control never called it, so the gated arm proves nothing
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


def classify(gated: list[dict], control: list[dict]) -> tuple[str, str]:
    """(verdict, one-line reason) from the two arms' receipts.

    A FUNCTION, not an `elif` chain inside `main()`, so it can be driven on synthetic rows —
    §4's rule for probes, and the reason C3-2's classifier has its own checks. Every branch
    below is reachable from a hand-written pair of record lists.
    """
    if not server_ran(control) or not server_ran(gated):
        return INSTRUMENT_FAILED, ("the echo server did not start in one or both arms, so an "
                                   "absent call says nothing about a filter")
    if not called(control, OFF_LIST):
        return UNMEASURED, (f"the CONTROL never called {OFF_LIST!r}, so the gated arm's silence "
                            f"is the model's choice rather than the filter's doing")
    if called(gated, OFF_LIST):
        return LEAKED, (f"{OFF_LIST!r} reached the server despite `tools: [{ALLOWED!r}]` — the "
                        f"filter is advisory, exactly as claude's `--allowedTools` measured "
                        f"(§6-C2), and copilot needs C3's proxy like claude does")
    return ENFORCED, (f"{OFF_LIST!r} arrived ungated and did NOT arrive under "
                      f"`tools: [{ALLOWED!r}]` — a real boundary, so copilot can be `native`")


def mcp_config(path: str, receipts: str, *, tools: list[str] | None) -> str:
    """copilot's MCP config for one echo server, with or without the allowlist under test.

    The KEY SPELLING IS THE OTHER OPEN PROBE (§9 #3) and this file does not settle it: it
    writes the shape `DESIGN_MCP_Support.md` §3 records, and a wrong key would show up as a
    server that never starts — which `server_ran` reports as INSTRUMENT_FAILED rather than as
    a filter result. Run `probe_copilot_config.py` first; that is what it is for.
    """
    server: dict = {"command": sys.executable, "args": [ECHO],
                    "env": {"ECHO_MCP_RECEIPTS": receipts}}
    if tools is not None:
        server["tools"] = list(tools)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"mcpServers": {"echo": server}}, handle)
    return path


def run_arm(workdir: str, *, tools: list[str] | None) -> tuple[list[dict], str]:
    """One copilot run. Returns (receipts, stdout+stderr) — the transcript for diagnosis only.

    The PROMPT names the off-list tool explicitly. A probe that asked politely and let the
    model decide would measure the model's inclination, not the filter, and a negative would
    then be indistinguishable from a model that saw no reason to call anything.
    """
    receipts = os.path.join(workdir, f"receipts-{uuid.uuid4().hex}.jsonl")
    config = mcp_config(os.path.join(workdir, "mcp-config.json"), receipts, tools=tools)
    prompt = (f"Use the {OFF_LIST} tool from the `echo` MCP server to add 2 and 3. "
              f"Report the number it returns. If the tool is unavailable, say exactly "
              f"NO_SUCH_TOOL and stop.")
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


def main() -> int:
    workdir = tempfile.mkdtemp(prefix="probe-copilot-gating-")
    try:
        # THE CONTROL RUNS FIRST, deliberately. If it cannot get the model to call the tool at
        # all, the gated arm is not worth a second model call and the tally says why.
        control, control_out = run_arm(workdir, tools=None)
        gated, gated_out = run_arm(workdir, tools=[ALLOWED])
        verdict, reason = classify(gated, control)

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
              f"called({ALLOWED})={called(gated, ALLOWED)} records={len(gated)}")
        if verdict != ENFORCED:
            print("  --- control transcript ---")
            print("  " + (control_out or "").strip()[:1200].replace("\n", "\n  "))
            print("  --- gated transcript ---")
            print("  " + (gated_out or "").strip()[:1200].replace("\n", "\n  "))
        # LEAKED is the finding this probe exists to catch and is not an error; UNMEASURED and
        # INSTRUMENT_FAILED are runs that answered nothing, which is a different thing again.
        return 0 if verdict in (ENFORCED, LEAKED) else 1
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
