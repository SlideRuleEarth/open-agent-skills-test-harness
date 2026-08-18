#!/usr/bin/env python3
"""Probe: does `--disable-mcp-server` reach a PLUGIN-declared MCP server, and under what name?

§9 probe #3's second unanswered half, and Phase 2 slice 1's question 3. It is a Phase 0
hermeticity question rather than a Phase 2 blocker — but it tests a claim that is currently
LOAD-BEARING IN SHIPPED CODE. `adapters/copilot.py` says, beside the decision that makes
copilot's MCP-off guarantee overlay-dependent:

    No flag disables copilot's plugin-declared MCP servers, and their names live inside
    plugin definitions the harness cannot enumerate

Two claims in one sentence, and they are not equally true. This probe separates them.

WHY A PLANTED PLUGIN, AND WHY THE CONTROL IS THE WHOLE MEASUREMENT. The question is whether a
flag SUPPRESSES something, so the reading is an absence — and an absence is worthless unless
the same setup produces the thing when the flag is gone. A plugin whose manifest schema is
wrong loads nothing, and then every `--disable-mcp-server` arm reports "the server is not
running" and the probe would publish UNREACHABLE: a false negative about a hermeticity
control, drawn from an instrument that was never connected. So the control arm runs first,
with no flag, and its server must appear `connected` AND be attributed to the plugin. Anything
else is `INSTRUMENT_FAILED` and no arm below it is read.

THE NAMING IS A SEPARATE QUESTION FROM THE REACHING, and asking only one hides the other. A
probe that tried the bare server key alone and saw it work would never learn that the
plugin-qualified spellings do NOT; a probe that tried only a qualified spelling would report
`UNREACHABLE` for a flag that works. So every candidate spelling is tried and each is reported.

RUNS AGAINST THE REAL HOME, deliberately. A throwaway `COPILOT_HOME` has no credentials and
copilot refuses to start — the contained-HOME problem this repo has a document about. Nothing
here writes to the user's config: `--plugin-dir` loads a plugin from a directory without
installing it. The user's own MCP servers may therefore appear in the witness beside ours;
every classifier below matches OUR server by name and source, never "the only server present".

    python tools/probe_copilot_plugin_mcp.py      # control + one arm per candidate spelling
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

HARNESS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HARNESS)
ECHO = os.path.join(HARNESS, "fixtures", "echo_mcp_server.py")

from agentskill_evals.adapters.copilot import _stream_cli_version  # noqa: E402 — run's own witness

PLUGIN_NAME = "probeplugmike"
SERVER_NAME = "probesrvoscar"
DEADLINE = 180.0

# EVERY SPELLING A READER MIGHT REACH FOR, because the answer is the point. `<plugin>/<server>`
# and the bare plugin name are the two a caller would guess from the event's own
# `sourcePlugin`/`pluginName` fields, and finding that they do NOT work is as much a result as
# finding the one that does.
CANDIDATES = (SERVER_NAME, f"{PLUGIN_NAME}/{SERVER_NAME}", PLUGIN_NAME,
              f"{PLUGIN_NAME}:{SERVER_NAME}")

# --- verdicts -------------------------------------------------------------------------
REACHES = "REACHES"                       # some spelling disabled it — the flag DOES reach
UNREACHABLE = "UNREACHABLE"               # no spelling disabled it
INSTRUMENT_FAILED = "INSTRUMENT_FAILED"   # the control never brought the plugin server up

# The statuses copilot uses. `disabled` is the one the harness's own `_INERT_MCP_STATUSES`
# already treats as inert, which is why the flag WORKING looks identical to the server never
# having been declared unless the entry is read rather than merely counted.
CONNECTED = "connected"
DISABLED = "disabled"


def write_plugin(root: str, receipts: str) -> str:
    """Plant a plugin declaring one stdio MCP server. Returns the plugin dir.

    The manifest shape is `plugin.json` with `name`/`version`/`mcpServers` — CONFIRMED against
    copilot's own reader rather than taken from documentation: `copilot plugin list
    --plugin-dir <dir>` lists the plugin, and the control arm below proves the server inside it
    is actually brought up. A wrong shape would be silent in exactly the direction that
    produces a false negative here, which is why neither check is optional.
    """
    os.makedirs(root, exist_ok=True)
    manifest = {
        "name": PLUGIN_NAME,
        "version": "1.0.0",
        "description": "harness probe: a plugin that declares one MCP server",
        "mcpServers": {
            SERVER_NAME: {"type": "local", "command": sys.executable, "args": [ECHO],
                          "env": {"ECHO_MCP_RECEIPTS": receipts}},
        },
    }
    with open(os.path.join(root, "plugin.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)
    return root


def parse_events(stream: str) -> list[dict]:
    """Every JSON object on its own line; non-JSON lines skipped rather than ending the read."""
    out = []
    for line in (stream or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def server_entry(events: list[dict], name: str) -> dict | None:
    """OUR server's entry from `session.mcp_servers_loaded`, or None.

    Matched by name against the entries rather than by position: the user's real home is in
    play, so "the only server listed" is not a safe reading and would break the moment this
    ran on a machine with any MCP server configured.
    """
    for obj in events:
        if obj.get("type") != "session.mcp_servers_loaded":
            continue
        data = obj.get("data")
        if not isinstance(data, dict):
            continue
        servers = data.get("servers")
        if not isinstance(servers, list):
            continue
        for srv in servers:
            if isinstance(srv, dict) and srv.get("name") == name:
                return srv
    return None


def attributed_to_plugin(entry: dict | None) -> bool:
    """Whether the witness itself says this server came from our plugin.

    THIS IS A SECOND FINDING, not a detail of the first. The event carries `source`,
    `sourcePlugin` and `pluginName`, which means a plugin-declared server is IDENTIFIABLE from
    the run's own stream — the harness's post-run audit reads that same event and today keeps
    none of those fields. It does not make the names enumerable BEFORE the run, which is the
    half of the shipped claim that survives; it does mean "the harness cannot tell a
    plugin server from any other" is not true after one.
    """
    if not isinstance(entry, dict):
        return False
    return (entry.get("source") == "plugin"
            and (entry.get("sourcePlugin") == PLUGIN_NAME
                 or entry.get("pluginName") == PLUGIN_NAME))


def control_ok(entry: dict | None) -> tuple[bool, str]:
    """Did the control arm actually bring our plugin's server up? The gate on everything else."""
    if entry is None:
        return False, ("the control run never listed our server at all — the planted plugin "
                       "was not loaded, so no `--disable-mcp-server` arm below could mean "
                       "anything")
    if entry.get("status") != CONNECTED:
        return False, (f"the control run listed our server as {entry.get('status')!r} rather "
                       f"than {CONNECTED!r}, so it was never running to be disabled")
    if not attributed_to_plugin(entry):
        return False, (f"the control's entry is not attributed to our plugin ({entry!r}) — it "
                       f"may be a same-named server from another source, which would make "
                       f"every arm below measure the wrong thing")
    return True, f"the plugin's server came up {CONNECTED!r} and is attributed to the plugin"


def disabled_by(entry: dict | None) -> bool:
    """Whether an arm's entry shows OUR server suppressed.

    `disabled` is required rather than "absent": a server missing from the list entirely is
    also what a plugin that failed to load looks like, and treating absence as success would
    let a broken arm certify the flag.
    """
    return isinstance(entry, dict) and entry.get("status") == DISABLED


# --- per-arm states -------------------------------------------------------------------
# EVERY CANDIDATE IS ITS OWN MEASUREMENT, and the first version of this probe treated only the
# CONTROL that way. An arm whose entry was missing, or came from another source, or carried a
# status nobody predicted, was read as "that spelling did not suppress it" — so an arm that
# crashed, and an arm that proved the flag does nothing, produced the same word. A reviewer
# reproduced exit 0 from a `disabled` entry sourced from `"user"` with the other three arms
# completely empty (review, PR #121). An arm now either states a fact about the flag or states
# that it failed, and there is no third reading.
ARM_DISABLED = "ARM_DISABLED"       # attributed to our plugin AND reported `disabled`
ARM_CONNECTED = "ARM_CONNECTED"     # attributed to our plugin AND still `connected`
ARM_FAILED = "ARM_FAILED"           # this arm measured nothing about the flag


def arm_state(entry: dict | None) -> tuple[str, str]:
    """(state, why) for one candidate spelling.

    ATTRIBUTION IS REQUIRED ON EVERY ARM, not just the control — the probe runs against the
    real home, so a same-named server from another source is a live possibility, and an arm
    reading one of those is measuring somebody else's config. The `disabled` entry keeps
    `source`/`sourcePlugin`/`pluginName` (measured, copilot 1.0.80), so requiring attribution
    here rejects the impostor without rejecting the true positive.

    The status set is CLOSED. A status nobody predicted is a failed arm rather than a quiet
    "not disabled", because the alternative is publishing a negative about a hermeticity
    control on the strength of a word this probe does not understand.
    """
    if entry is None:
        return ARM_FAILED, ("this arm's stream never listed our server at all — it may have "
                            "crashed, timed out, or failed to load the plugin, and none of "
                            "those is a fact about the flag")
    if not attributed_to_plugin(entry):
        return ARM_FAILED, (f"this arm's entry is not attributed to our plugin ({entry!r}), so "
                            f"it describes some other server that happens to share the name")
    status = entry.get("status")
    if status == DISABLED:
        return ARM_DISABLED, f"our plugin's server was reported {DISABLED!r}"
    if status == CONNECTED:
        return ARM_CONNECTED, f"our plugin's server was still {CONNECTED!r}"
    return ARM_FAILED, (f"our plugin's server carried the unexpected status {status!r}, which "
                        f"this probe cannot read as either suppressed or running")


def classify(control: dict | None,
             arms: dict[str, dict | None]) -> tuple[str, str, list[str], dict[str, str]]:
    """(verdict, why, the spellings that worked,every arm's state) — control first, always.

    STRUCTURAL CLAUSE AHEAD OF THE QUANTIFIER. `arms` being empty, or every arm reporting
    nothing, is indistinguishable from a flag that works unless the control established that
    there was something to suppress. So the control is asked before any arm is read.

    THEN EVERY ARM MUST HAVE SPOKEN. `working` and the "did not work" list are both
    comprehensions over `arms`, and a failed arm silently joins the second — which is how a run
    with three empty arms reported which spellings the flag does not reach. One failed arm
    makes the whole naming measurement incomplete, so it is reported as an instrument failure
    rather than folded into either answer.
    """
    ok, why = control_ok(control)
    if not ok:
        return INSTRUMENT_FAILED, why, [], {}
    if not arms:
        return INSTRUMENT_FAILED, "no candidate spelling was tried", [], {}
    states = {name: arm_state(entry) for name, entry in arms.items()}
    broken = {name: why for name, (state, why) in states.items() if state == ARM_FAILED}
    flat = {name: state for name, (state, _) in states.items()}
    if broken:
        return INSTRUMENT_FAILED, (
            "the control was sound but "
            + "; ".join(f"the {name!r} arm measured nothing ({why})"
                        for name, why in sorted(broken.items()))
            + " — with any arm unread the set of spellings that do NOT work is unknown, and "
              "that set is half the question"), [], flat
    working = [name for name, (state, _) in states.items() if state == ARM_DISABLED]
    if working:
        return REACHES, (f"`--disable-mcp-server` DOES reach a plugin-declared server, "
                         f"addressed as {working!r}; the spellings that did NOT work are "
                         f"{[n for n in arms if n not in working]!r}"), working, flat
    return UNREACHABLE, (f"no candidate spelling suppressed it — tried {sorted(arms)!r} and the "
                         f"server stayed {CONNECTED!r} under every one"), [], flat


def run_arm(workdir: str, plugin_dir: str, disable: str | None) -> str:
    """One copilot run. The prompt is trivial: the MCP host initializes before the model acts,
    so the witness arrives whatever the model says, and a bigger prompt only costs more."""
    argv = ["copilot", "-p", "Say OK and stop.",
            "--no-custom-instructions", "--disable-builtin-mcps", "--no-remote",
            "--plugin-dir", plugin_dir,
            "--output-format", "json", "--allow-all"]
    if disable is not None:
        argv += ["--disable-mcp-server", disable]
    try:
        done = subprocess.run(argv, cwd=workdir, capture_output=True, text=True,
                              timeout=DEADLINE)
    except FileNotFoundError:
        return ""
    except subprocess.TimeoutExpired:
        return ""
    return (done.stdout or "") + (done.stderr or "")


def agreed_version(streams: list[str]) -> tuple[str, bool]:
    """(text, usable) over EVERY arm LAUNCHED — one witness each, and the set a singleton.

    Launched, not "that ran": an arm that produced nothing has no witness, and that is the arm
    whose build is least accounted for. Filtering the empties out first is what let three
    silent arms ride through version agreement (review, PR #121).
    """
    if not streams:
        return "(no runs to witness)", False
    found = [_stream_cli_version(s or "") for s in streams]
    if any(f is None for f in found):
        return f"an executed arm produced no in-band version witness ({[f or '-' for f in found]})", False
    if len(set(found)) != 1:
        return f"the arms did not run the same build: {sorted(str(f) for f in set(found))}", False
    return found[0], True


def main() -> int:
    workdir = tempfile.mkdtemp(prefix="probe-copilot-plugin-")
    try:
        plugin_dir = write_plugin(os.path.join(workdir, "plug"),
                                  os.path.join(workdir, "receipts.jsonl"))
        control_stream = run_arm(workdir, plugin_dir, None)
        streams = [control_stream]
        control = server_entry(parse_events(control_stream), SERVER_NAME)
        print(f"control entry: {control!r}")
        ok, why = control_ok(control)
        print(f"control: {'OK' if ok else 'FAILED'} — {why}\n")

        arms: dict[str, dict | None] = {}
        if ok:
            for name in CANDIDATES:
                out = run_arm(workdir, plugin_dir, name)
                # EVERY ARM'S STREAM, INCLUDING AN EMPTY ONE. Dropping the empties made an arm
                # that never ran invisible to version agreement, and an arm that never ran is
                # precisely the one whose build is least accounted for (review, PR #121).
                streams.append(out)
                entry = server_entry(parse_events(out), SERVER_NAME)
                arms[name] = entry
                print(f"  --disable-mcp-server {name!r:<32} -> {entry!r}")

        verdict, reason, working, states = classify(control, arms)
        version, version_ok = agreed_version(streams)
        print(f"\nper-arm : {states or '(none)'}")
        print(f"verdict : {verdict}\n  {reason}")
        print(f"witness attributes the server to its plugin: {attributed_to_plugin(control)}")
        print(f"copilot : {version} ({'usable' if version_ok else 'UNVERIFIED'})")

        # A SETTLED ANSWER EITHER WAY EXITS 0 ONLY IF IT IS VERSION-QUALIFIED. `UNREACHABLE` is
        # a real finding, not a failure — but an unqualified one is a claim about no build.
        settled = verdict in (REACHES, UNREACHABLE)
        return 0 if (settled and version_ok) else 1
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
