#!/usr/bin/env python3
"""Probe: what does copilot SAY it did? — Phase 2 slice 1, questions 1, 2 and 4.

WHY A FOURTH COPILOT PROBE. The three shipped ones read what copilot *sent*: the two gating
probes read server-side receipts, and `probe_copilot_config.py` reads the config copilot wrote
for itself. Every remaining Phase 2 unknown is on the other side of the process — copilot's own
account of the run, in its `--output-format json` stream — and no amount of care with receipts
reaches it. Three questions live there and one run answers all of them:

  1. **The MCP tool-name format in copilot's own events.** claude spells it
     `mcp__<server>__<tool>`; agy's is *inferred* as `mcp_<server>_<tool>` from binary strings;
     copilot's is unmeasured. Phase 2's parser and the portable `used_mcp_tool` assertion both
     need it, and both would be built to a guess without this.
  2. **What `session.mcp_servers_loaded` reports for a DECLARED server** — which spelling it
     names, and the status string a HEALTHY one carries. Phase 2 slice 2 rewrites the
     hermeticity witness to permit the declared set, and it cannot be designed without knowing
     which status means healthy: `_INERT_MCP_STATUSES` currently treats everything except
     `disabled`/`not_configured` as brought-up, and slice 2 has to split that set.
  3. **What `--secret-env-vars` actually does to an MCP-bearing run.** §8 lists it as
     belt-and-braces. Nothing has measured whether it redacts the *value* wherever the value
     appears, or only where the variable itself is echoed — and belt-and-braces that only holds
     in the case you did not need it is worth knowing about before it is relied on.

THE CONFIG KEY AND THE ADVERTISED NAME ARE DELIBERATELY DIFFERENT, and question 2 is
unanswerable without that. A server whose `mcpServers` key matches its own
`serverInfo.name` cannot distinguish "the event reports the key the harness chose" from "the
event reports the name the server claimed" — the two readings produce identical output, and
Phase 2 would pick one by coin flip. So the config key is `CONFIG_KEY` and the fixture
advertises `ADVERTISED_NAME`, and the classifier reports WHICH it saw.

THE SECRET ARM HAS A POSITIVE CONTROL, for the reason §4 spends a rule on: a run where the
sentinel never reached the output at all produces exactly the same silence as redaction that
works. So the control arm runs the identical prompt with the flag ABSENT and must find the
sentinel; only then does its absence under the flag mean anything. Without that, `REDACTS` is
a claim about a channel nobody proved was connected.

WHAT THIS PROBE DOES NOT ANSWER. Whether `--disable-mcp-server` reaches plugin-declared
servers (§9 probe #3's other half) — that needs an installed plugin, not an injected config,
and is a Phase 0 hermeticity question rather than a Phase 2 blocker.

    python tools/probe_copilot_events.py        # two arms; prints every reading either way

EXIT STATUS is a conjunction over the three questions plus the version witness, never a lookup
on the last one read: a run that answered two questions and left the third UNMEASURED has not
established what Phase 2 needs, and must not exit 0 on the strength of the two.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import uuid

HARNESS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HARNESS)
ECHO = os.path.join(HARNESS, "fixtures", "echo_mcp_server.py")

from agentskill_evals.adapters.copilot import _stream_cli_version  # noqa: E402 — run's own witness

# The two names are different ON PURPOSE — see the module docstring. Neither is a word the
# model would produce on its own, so a match is the event echoing our input rather than
# coincidence.
CONFIG_KEY = "cfgkeyzulu"
ADVERTISED_NAME = "advnamequebec"
TOOL = "echo"
DEADLINE = 180.0

# --- verdicts ------------------------------------------------------------------------
# Named rather than spelled at the site that tests them: two readers must agree about a
# vocabulary, and `UNMEASURED` in particular has to be distinguishable from a negative.
CLAUDE_STYLE = "CLAUDE_STYLE"        # mcp__<server>__<tool>
AGY_STYLE = "AGY_STYLE"              # mcp_<server>_<tool>
DOTTED = "DOTTED"                    # <server>.<tool>
BARE = "BARE"                        # <tool>, with no server component at all
OTHER = "OTHER"                      # a real name in none of the above shapes
UNMEASURED = "UNMEASURED"            # no MCP tool call reached the stream: NOT a format finding

REPORTS_CONFIG_KEY = "REPORTS_CONFIG_KEY"
REPORTS_ADVERTISED = "REPORTS_ADVERTISED"
REPORTS_NEITHER = "REPORTS_NEITHER"

REDACTS = "REDACTS"                  # sentinel present in control, absent under the flag
NO_REDACTION = "NO_REDACTION"        # sentinel present in both
CONTROL_FAILED = "CONTROL_FAILED"    # sentinel never reached the control's output


def parse_events(stream: str) -> list[dict]:
    """Every JSON object on its own line, non-JSON lines skipped.

    A PARSER, NOT A FILTER: copilot interleaves human-readable lines with the JSON stream on
    some paths, and a probe that treated the first unparseable line as the end of the stream
    would report "no events" for a run that emitted plenty — which is indistinguishable from a
    run that never started. The adapter's own witness skips them one at a time for exactly this
    reason.
    """
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


def loaded_servers(events: list[dict]) -> list[tuple[str | None, str | None]]:
    """Every (name, status) copilot reported, from BOTH witness events.

    `session.mcp_servers_loaded` carries `data.servers[]` with `name`/`status`;
    `session.mcp_server_status_changed` carries `data.serverName`/`data.status`. Reading only
    the first would miss a server that arrived healthy and then failed, and a later transition
    is exactly the case Phase 2 slice 2 has to classify. Field spellings are the adapter's
    (`_mcp_witness`), which is where they were verified.
    """
    seen: list[tuple[str | None, str | None]] = []
    for obj in events:
        etype, data = obj.get("type"), obj.get("data")
        if etype == "session.mcp_servers_loaded" and isinstance(data, dict):
            servers = data.get("servers")
            if isinstance(servers, list):
                for srv in servers:
                    if isinstance(srv, dict):
                        seen.append((srv.get("name"), srv.get("status")))
        elif etype == "session.mcp_server_status_changed" and isinstance(data, dict):
            seen.append((data.get("serverName"), data.get("status")))
    return seen


def declared_spelling(seen: list[tuple[str | None, str | None]]) -> tuple[str, str | None]:
    """(which spelling the witness used, the status it carried) for OUR declared server.

    Returns `REPORTS_NEITHER` with a None status when the run named neither spelling, which is
    the honest reading of a stream that never mentioned our server — not a claim that the
    server was absent, since a stream that never arrived says the same thing. `main` gates on
    the stream having been produced at all before treating this as an answer.
    """
    for name, status in seen:
        if name == CONFIG_KEY:
            return REPORTS_CONFIG_KEY, status
    for name, status in seen:
        if name == ADVERTISED_NAME:
            return REPORTS_ADVERTISED, status
    return REPORTS_NEITHER, None


def tool_names(events: list[dict]) -> list[str]:
    """Every tool name copilot said it executed, from the execution event and the request.

    TWO SOURCES, because they can disagree and the disagreement matters: `tool.execution_start`
    is copilot's own record of what it ran, while `assistant.message.data.toolRequests[]` is
    what the model asked for. If the canonical spelling differs between them, Phase 2's parser
    has to know which one `used_mcp_tool` should match.
    """
    names: list[str] = []
    for obj in events:
        data = obj.get("data")
        if not isinstance(data, dict):
            continue
        if obj.get("type") == "tool.execution_start":
            nm = data.get("toolName")
            if isinstance(nm, str):
                names.append(nm)
        elif obj.get("type") == "assistant.message":
            for req in data.get("toolRequests") or []:
                if isinstance(req, dict):
                    nm = req.get("toolName") or req.get("name")
                    if isinstance(nm, str):
                        names.append(nm)
    return names


def mcp_tool_name(names: list[str]) -> str | None:
    """The one name among `names` that refers to OUR tool on OUR server, or None.

    Matched by containing both components rather than by an assumed separator — the separator
    is the very thing being measured, so a predicate spelling it would only ever confirm the
    guess it was written with. Built-in tools (`shell`, `view`) are excluded by the same test:
    they carry neither component.
    """
    for nm in names:
        if TOOL in nm and (CONFIG_KEY in nm or ADVERTISED_NAME in nm):
            return nm
    return None


def name_format(name: str | None) -> str:
    """Classify the observed MCP tool name into a known shape.

    `UNMEASURED` for None is the load-bearing case: no MCP tool call in the stream means the
    model never called one, which says nothing whatever about how copilot spells them. Reading
    that as `BARE` — "no prefix seen" — would be a fleet-wide negative drawn from an unanswered
    row, which §4 names as its own error.
    """
    if name is None:
        return UNMEASURED
    for server in (CONFIG_KEY, ADVERTISED_NAME):
        if name == f"mcp__{server}__{TOOL}":
            return CLAUDE_STYLE
        if name == f"mcp_{server}_{TOOL}":
            return AGY_STYLE
        if name == f"{server}.{TOOL}":
            return DOTTED
    if name == TOOL:
        return BARE
    return OTHER


def secret_verdict(control_stream: str, secret_stream: str, sentinel: str) -> tuple[str, str]:
    """(verdict, why) for `--secret-env-vars`, with its control read FIRST.

    THE CONTROL IS THE MEASUREMENT. Absence of the sentinel under the flag is evidence of
    redaction only if the same run without the flag produced it; otherwise the sentinel simply
    never travelled, and `REDACTS` would be a claim about a channel nobody connected. So a
    control that does not carry the sentinel returns `CONTROL_FAILED` — not a weaker positive.
    """
    if sentinel not in control_stream:
        return CONTROL_FAILED, ("the sentinel never reached the control run's output, so its "
                                "absence under --secret-env-vars measures nothing: there was "
                                "no value in the channel to redact")
    if sentinel in secret_stream:
        return NO_REDACTION, ("the sentinel appears in the output WITH --secret-env-vars naming "
                              "its variable — the flag did not redact the value where it landed")
    return REDACTS, ("the sentinel reached the control's output and is absent under "
                     "--secret-env-vars, so the flag redacted the value itself")


def mcp_config(path: str, sentinel: str) -> str:
    """One stdio echo server under `CONFIG_KEY`, advertising `ADVERTISED_NAME`.

    NO `type` KEY, deliberately: the stdio gating probe omits it too and its servers start, so
    this arm inherits a shape already measured to work rather than introducing an untested
    variable into a run whose subject is something else.
    """
    server = {"command": sys.executable, "args": [ECHO],
              "env": {"ECHO_MCP_SERVER_NAME": ADVERTISED_NAME,
                      "ECHO_MCP_IDENTITY": sentinel}}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"mcpServers": {CONFIG_KEY: server}}, handle)
    return path


def run_arm(workdir: str, sentinel: str, *, redact: bool) -> str:
    """One copilot run. Returns stdout+stderr — the stream is the whole measurement here.

    The prompt names the server and tool explicitly and asks for the reply VERBATIM: the
    identity marker rides back in the tool's answer, which is what puts the sentinel into
    copilot's own output and gives the secret arm something to redact.
    """
    config = mcp_config(os.path.join(workdir, f"cfg-{uuid.uuid4().hex}.json"), sentinel)
    prompt = (f"Use the `{CONFIG_KEY}` MCP server: call its {TOOL} tool with the text HELLO, "
              f"and quote the tool's reply back VERBATIM, exactly as it returned it.")
    argv = ["copilot", "-p", prompt,
            "--no-custom-instructions", "--disable-builtin-mcps", "--no-remote",
            "--additional-mcp-config", f"@{config}",
            "--output-format", "json", "--allow-all"]
    if redact:
        argv += ["--secret-env-vars", "PROBE_SECRET"]
    env = dict(os.environ)
    env["PROBE_SECRET"] = sentinel
    try:
        done = subprocess.run(argv, cwd=workdir, capture_output=True, text=True,
                              timeout=DEADLINE, env=env)
    except FileNotFoundError:
        return ""
    except subprocess.TimeoutExpired:
        return ""
    return (done.stdout or "") + (done.stderr or "")


def agreed_version(streams: list[str]) -> tuple[str, bool]:
    """(text, usable) over EVERY arm that ran — the same rule the gating probes settled on.

    One witness per executed arm, and the set must be a singleton. An arm with no witness is
    precisely the arm that could have executed a different build, so absence anywhere is
    unverified rather than tolerated.
    """
    if not streams:
        return "(no runs to witness)", False
    found = [_stream_cli_version(s or "") for s in streams]
    if any(f is None for f in found):
        return f"an executed arm produced no in-band version witness ({[f or '-' for f in found]})", False
    if len(set(found)) != 1:
        return f"the arms did not run the same build: {sorted(str(f) for f in set(found))}", False
    return found[0], True


def answered(fmt: str, spelling: str, secret: str) -> bool:
    """Whether all three questions were ANSWERED — a conjunction, never a lookup.

    Each term is a separate question, and a run that settled two of them has not established
    what Phase 2 needs. `REPORTS_NEITHER` is unanswered rather than a negative finding, for the
    same reason `UNMEASURED` is: a stream that never named our server and a stream that never
    arrived are the same bytes.
    """
    return (fmt != UNMEASURED
            and spelling != REPORTS_NEITHER
            and secret in (REDACTS, NO_REDACTION))


def main() -> int:
    sentinel = f"SENT-{uuid.uuid4().hex}"
    workdir = tempfile.mkdtemp(prefix="probe-copilot-events-")
    print(f"config key   : {CONFIG_KEY}")
    print(f"advertised   : {ADVERTISED_NAME}")
    print(f"sentinel     : {sentinel}\n")

    control = run_arm(workdir, sentinel, redact=False)
    secret = run_arm(workdir, sentinel, redact=True)
    streams = [s for s in (control, secret) if s]
    if not streams:
        print("INSTRUMENT_FAILED: no arm produced any output (is `copilot` on PATH?)")
        return 1

    version, version_ok = agreed_version(streams)
    # BOTH ARMS POOLED. Reading one arm and falling back to the other would report
    # `UNMEASURED` whenever the arm consulted happened not to call the tool, while the arm
    # beside it carried the answer — an absence produced by the reader's choice rather than by
    # the run. `seen` is printed whole below, so a disagreement between the arms stays visible
    # rather than being hidden by the first-match classifiers.
    events = parse_events(control) + parse_events(secret)
    seen = loaded_servers(events)
    spelling, status = declared_spelling(seen)
    names = tool_names(events)
    observed = mcp_tool_name(names)
    fmt = name_format(observed)
    verdict, why = secret_verdict(control, secret, sentinel)

    print(f"version          : {version} ({'usable' if version_ok else 'UNVERIFIED'})")
    print(f"servers reported : {seen or '(none)'}")
    print(f"Q2 name spelling : {spelling}   status={status!r}")
    print(f"tool names seen  : {names or '(none)'}")
    print(f"Q1 tool format   : {fmt}   observed={observed!r}")
    print(f"Q4 secret        : {verdict}\n    {why}")

    ok = answered(fmt, spelling, verdict) and version_ok
    print(f"\n{'ANSWERED' if ok else 'NOT FULLY ANSWERED'} — "
          f"artifacts under {workdir}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
