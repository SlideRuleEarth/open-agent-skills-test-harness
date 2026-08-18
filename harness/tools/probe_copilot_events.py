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

THE SECRET ARM RESTS ON TWO WITNESSES, NOT ONE, and the first version of it had only the
weaker (review, PR #120). A control that runs the same prompt with the flag ABSENT and finds
the sentinel proves the value CAN travel — but it proves it about the control, and `REDACTS`
is a claim about the *other* arm. An arm that crashed, timed out, or simply never called the
tool produces the identical silence, and the probe would read the absence of the exchange as
the absence of the value. So the secret arm carries its own POSITIVE witness, authored by the
fixture rather than by the process under test: its server's receipts must show the `tools/call`
that carries the marker, and the `listening` row must carry the digest of THIS run's sentinel —
so "the value was in the channel" and "the channel was used" are both facts about the arm the
verdict is about. Nothing the CLI emits can forge either: the receipts are written by a
different process, and the marker itself never appears there, only its sha256.

AND THE RECEIPTS ARE ONLY HALF OF IT, because they end at the wire. They say the fixture put a
reply carrying the value onto it; they cannot say copilot ever emitted a result for that call.
An arm with an execution and no completion event has nothing to redact and nothing to have
redacted, and reading its silence as redaction is the same absence-as-evidence error one layer
further out (review, PR #120). So each arm is read from BOTH authors — its fixture's receipts
and copilot's own correlated `tool.execution_complete` — and the treatment arm is held to
exactly what the control is.

WHAT THIS PROBE DOES NOT ANSWER. Whether `--disable-mcp-server` reaches plugin-declared
servers (§9 probe #3's other half) — that needs an installed plugin, not an injected config,
and is answered by `probe_copilot_plugin_mcp.py`.

    python tools/probe_copilot_events.py        # two arms; prints every reading either way

EXIT STATUS is a conjunction over the three questions plus the version witness, never a lookup
on the last one read: a run that answered two questions and left the third UNMEASURED has not
established what Phase 2 needs, and must not exit 0 on the strength of the two.
"""
from __future__ import annotations

import hashlib
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
HYPHEN = "HYPHEN"                    # <server>-<tool>
BARE = "BARE"                        # <tool>, with no server component at all
OTHER = "OTHER"                      # a real name in none of the above shapes
UNMEASURED = "UNMEASURED"            # no MCP tool call reached the stream: NOT a format finding
AMBIGUOUS = "AMBIGUOUS"              # the sources disagreed: there is no ONE canonical spelling
ONE_SOURCE_ONLY = "ONE_SOURCE_ONLY"  # only one of the two sources spoke: agreement UNOBSERVED

REPORTS_CONFIG_KEY = "REPORTS_CONFIG_KEY"
REPORTS_ADVERTISED = "REPORTS_ADVERTISED"
REPORTS_NEITHER = "REPORTS_NEITHER"
REPORTS_BOTH = "REPORTS_BOTH"        # the run used BOTH spellings: there is no single answer

REDACTS = "REDACTS"                  # sentinel present in control, absent under the flag
NO_REDACTION = "NO_REDACTION"        # sentinel present in both
CONTROL_FAILED = "CONTROL_FAILED"    # the control's own tool RESULT never carried the sentinel
CONTROL_INCOMPLETE = "CONTROL_INCOMPLETE"         # the control never ran the exchange either
SECRET_ARM_INCOMPLETE = "SECRET_ARM_INCOMPLETE"   # the arm under the flag never made the call

# What became of OUR tool's result in one arm's stream. THREE STATES, BECAUSE TWO OF THEM WERE
# ONE BOOLEAN: "the result came back without the value" and "no result of ours came back at
# all" are different facts, and only the first is evidence about redaction (review, PR #120).
RESULT_CARRIED = "RESULT_CARRIED"    # a result of our tool reached the output, WITH the value
RESULT_CLEAN = "RESULT_CLEAN"        # a result of our tool reached the output, without it
RESULT_ABSENT = "RESULT_ABSENT"      # no result of our tool reached the output at all

# Question 2 is "which spelling AND what status", so the status needs a verdict of its own.
STATUS_MEASURED = "STATUS_MEASURED"          # a real status, from an arm proven to have served
STATUS_ABSENT = "STATUS_ABSENT"              # our server was named and carried no status
STATUS_UNWITNESSED = "STATUS_UNWITNESSED"    # a status, from an arm that never proved it worked

# The two sources a tool name can come from, kept apart because they can disagree.
EXECUTION = "execution"              # tool.execution_start.data.toolName — what copilot RAN
REQUEST = "request"                  # assistant.message.data.toolRequests[] — what it ASKED for
EXPECTED_SOURCES = (EXECUTION, REQUEST)

# How the two sources stood, which is a separate question from what they said.
AGREED = "AGREED"                    # both spoke, and they said the same thing
DISAGREED = "DISAGREED"              # more than one distinct name for our tool
PARTIAL = "PARTIAL"                  # at least one expected source said nothing
SILENT = "SILENT"                    # neither named our tool: nothing to agree about


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
    """Every (name, status) copilot reported, IN STREAM ORDER, from BOTH witness events.

    `session.mcp_servers_loaded` carries `data.servers[]` with `name`/`status`;
    `session.mcp_server_status_changed` carries `data.serverName`/`data.status`. Reading only
    the first would miss a server that arrived healthy and then failed, and a later transition
    is exactly the case Phase 2 slice 2 has to classify. Field spellings are the adapter's
    (`_mcp_witness`), which is where they were verified.

    ORDER IS PART OF THE READING, not an accident of iteration — `effective_status` below is
    the consumer that depends on it.
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


def statuses_for(seen: list[tuple[str | None, str | None]], name: str) -> list[str | None]:
    """Every status `name` carried, in order. Shared with the remote-gating probe, which
    imports it rather than keeping a second reader of the same two events."""
    return [status for nm, status in seen if nm == name]


def effective_status(statuses: list[str | None]) -> str | None:
    """The status our server ENDED on, which is the only one that describes the run.

    THE LAST, NOT THE FIRST. `loaded_servers` was written to see a server that came up healthy
    and then failed, and the first consumer of it then took the first match and reported
    `connected` for exactly that server — the reader fixed, the conclusion not (review,
    PR #120). Slice 2 splits `_INERT_MCP_STATUSES` on this value, so a stale healthy status
    here becomes a hermeticity witness that permits a server nothing is talking to.
    """
    return statuses[-1] if statuses else None


def declared_spelling(
        seen: list[tuple[str | None, str | None]]) -> tuple[str, list[str | None]]:
    """(which spelling the witness used, EVERY status it carried) for OUR declared server.

    BOTH SPELLINGS APPEARING IS A FINDING, not a tie to be broken. The first version checked
    the config key first and returned on a match, so a run whose `mcp_servers_loaded` used the
    key and whose later `mcp_server_status_changed` used the advertised name reported one
    spelling confidently AND silently dropped the final status — the two defects compounding,
    since the status that went missing is the one slice 2 reads (review, PR #120). Whichever
    is true of copilot, a design cannot be built on "the key, except when it is the other one",
    so `REPORTS_BOTH` says the contract is not one thing and `answered()` refuses it.

    Returns `REPORTS_NEITHER` with no statuses when the run named neither spelling, which is
    the honest reading of a stream that never mentioned our server — not a claim that the
    server was absent, since a stream that never arrived says the same thing. `main` gates on
    the stream having been produced at all before treating this as an answer.
    """
    by_key = statuses_for(seen, CONFIG_KEY)
    by_advertised = statuses_for(seen, ADVERTISED_NAME)
    if by_key and by_advertised:
        # IN STREAM ORDER, over both names. Concatenating one name's sequence onto the
        # other's would invent an ordering across two identities; this is the run as it
        # happened, which is what a reader diagnosing the ambiguity needs.
        return REPORTS_BOTH, [st for nm, st in seen
                              if nm in (CONFIG_KEY, ADVERTISED_NAME)]
    if by_key:
        return REPORTS_CONFIG_KEY, by_key
    if by_advertised:
        return REPORTS_ADVERTISED, by_advertised
    return REPORTS_NEITHER, []


def tool_names(events: list[dict]) -> list[tuple[str, str]]:
    """Every (source, tool name) copilot reported, from the execution event and the request.

    TWO SOURCES, AND THEY STAY APART: `tool.execution_start` is copilot's own record of what it
    ran, while `assistant.message.data.toolRequests[]` is what the model asked for. The first
    version of this returned bare strings, so a disagreement between the two — the case the
    docstring said mattered — was flattened into a list and then resolved by taking whichever
    came first (review, PR #120). If the canonical spelling differs between them, Phase 2's
    parser has to know which one `used_mcp_tool` should match, and that is a question the probe
    must ASK rather than answer by iteration order.
    """
    names: list[tuple[str, str]] = []
    for obj in events:
        data = obj.get("data")
        if not isinstance(data, dict):
            continue
        if obj.get("type") == "tool.execution_start":
            nm = data.get("toolName")
            if isinstance(nm, str):
                names.append((EXECUTION, nm))
        elif obj.get("type") == "assistant.message":
            for req in data.get("toolRequests") or []:
                if isinstance(req, dict):
                    nm = req.get("toolName") or req.get("name")
                    if isinstance(nm, str):
                        names.append((REQUEST, nm))
    return names


FIELDS_PRESENT = "FIELDS_PRESENT"    # copilot names server and tool in SEPARATE fields
FIELDS_ABSENT = "FIELDS_ABSENT"      # only the composite name is on offer
FIELDS_UNMEASURED = "FIELDS_UNMEASURED"   # no MCP execution event at all: nothing to read


def mcp_fields(events: list[dict]) -> list[tuple[str | None, str | None]]:
    """(mcpServerName, mcpToolName) from every `tool.execution_start`, in stream order.

    A SECOND, BETTER ANSWER TO QUESTION 1, found by looking at the event rather than at the
    string it contains. copilot 1.0.80 puts the server and the tool in their own fields beside
    the composite `toolName`, which means slice 4's `used_mcp_tool` can match on the two facts
    it actually cares about instead of splitting a name on a separator whose escaping rules
    nobody has measured — a server or tool whose own name contains the separator breaks the
    split and cannot break these. Entries are reported with `None`s intact so a build that
    stops emitting them is visible as a change rather than as an empty list.
    """
    out: list[tuple[str | None, str | None]] = []
    for obj in events:
        data = obj.get("data")
        if obj.get("type") == "tool.execution_start" and isinstance(data, dict):
            if "mcpServerName" in data or "mcpToolName" in data:
                out.append((data.get("mcpServerName"), data.get("mcpToolName")))
    return out


def fields_verdict(pairs: list[tuple[str | None, str | None]],
                   executions: int) -> tuple[str, str]:
    """(verdict, why) for "can slice 4 read the server and tool without parsing a name?".

    `executions` — how many `tool.execution_start` events the stream carried at all — is the
    structural clause: with none, an empty `pairs` says nothing about whether copilot emits
    the fields, and `FIELDS_ABSENT` would be a fleet-wide negative drawn from a row nobody
    answered.
    """
    if not executions:
        return FIELDS_UNMEASURED, ("no tool executed in this run, so whether the execution "
                                   "event carries the fields was not observed")
    ours = [(srv, tool) for srv, tool in pairs
            if tool == TOOL and srv in (CONFIG_KEY, ADVERTISED_NAME)]
    if not ours:
        return FIELDS_ABSENT, (f"{executions} tool execution(s) ran and none named our server "
                               f"and tool in `mcpServerName`/`mcpToolName`: slice 4 has only "
                               f"the composite name to work with")
    return FIELDS_PRESENT, (f"copilot names the server and the tool in their own fields "
                            f"({ours[0]!r}), so `used_mcp_tool` need not parse the composite")


def executions(events: list[dict]) -> int:
    """How many `tool.execution_start` events the stream carried — the structural clause for
    `fields_verdict`, counted separately so it cannot be inferred from what it gates."""
    return sum(1 for obj in events if obj.get("type") == "tool.execution_start")


def is_our_tool(name: str) -> bool:
    """Whether `name` refers to OUR tool on OUR server.

    Matched by containing both components rather than by an assumed separator — the separator
    is the very thing being measured, so a predicate spelling it would only ever confirm the
    guess it was written with. Built-in tools (`shell`, `view`) are excluded by the same test:
    they carry neither component.
    """
    return TOOL in name and (CONFIG_KEY in name or ADVERTISED_NAME in name)


def mcp_names_by_source(pairs: list[tuple[str, str]]) -> dict[str, list[str]]:
    """{source: the distinct names it used for OUR tool}, in stream order.

    Sources with nothing to say are absent rather than present-and-empty, so a caller cannot
    mistake "this source named no MCP tool" for "this source agreed with the other one".
    """
    by_source: dict[str, list[str]] = {}
    for source, name in pairs:
        if is_our_tool(name) and name not in by_source.setdefault(source, []):
            by_source[source].append(name)
    return {src: names for src, names in by_source.items() if names}


def sources_verdict(by_source: dict[str, list[str]]) -> str:
    """How the two sources stood: AGREED, DISAGREED, PARTIAL or SILENT.

    AGREEMENT NEEDS TWO PARTIES, and the first version of this did not require them. It asked
    only whether the set of names had one element, so a run where the model's request event
    never named our tool — or a build that stops emitting one of the events — read as
    "agreed", and the probe exited ANSWERED while the recorded conclusion said "both sources,
    identically" (review, PR #120). One source saying one thing is not two sources saying the
    same thing; it is one observation and an unobserved one, and `PARTIAL` is what that is.

    `EXPECTED_SOURCES` is the structural clause: agreement is judged against the sources that
    are supposed to speak, never against however many happened to.
    """
    distinct = {name for names in by_source.values() for name in names}
    if not distinct:
        return SILENT
    if len(distinct) > 1:
        return DISAGREED
    if any(not by_source.get(src) for src in EXPECTED_SOURCES):
        return PARTIAL
    return AGREED


def canonical_name(by_source: dict[str, list[str]]) -> str | None:
    """The single name our tool was called by, or None when there is not exactly one."""
    distinct = {name for names in by_source.values() for name in names}
    return distinct.pop() if len(distinct) == 1 else None


def mcp_call_ids(events: list[dict]) -> set[str]:
    """The `toolCallId`s copilot assigned to executions of OUR tool.

    The correlation key the result event needs. `tool.execution_complete` carries no tool name
    at all — only `toolCallId`, `result` and telemetry (measured, 1.0.80) — so attributing a
    result to our tool means matching the id copilot itself minted on the start event.
    """
    ids: set[str] = set()
    for obj in events:
        if obj.get("type") != "tool.execution_start":
            continue
        data = obj.get("data")
        if not isinstance(data, dict):
            continue
        cid = data.get("toolCallId")
        if not isinstance(cid, str) or not cid:
            continue
        name = data.get("toolName")
        if (data.get("mcpToolName") == TOOL
                and data.get("mcpServerName") in (CONFIG_KEY, ADVERTISED_NAME)) or (
                isinstance(name, str) and is_our_tool(name)):
            ids.add(cid)
    return ids


def tool_result_state(events: list[dict], sentinel: str) -> str:
    """Whether a RESULT of one of OUR tool's executions reached the output, and what it carried.

    ATTRIBUTED, NOT SEARCHED. "The sentinel appears somewhere in this arm's stream" is a weaker
    fact than it looks: the marker is also in an env var and in the config file this probe
    writes, and the run has `--allow-all`, so a diagnostic echo or a file read satisfies it
    without any tool reply having travelled — and `REDACTS` would then be resting on a route
    the secret arm's absence says nothing about (review, PR #120). This asks the narrower
    question the verdict actually claims: did the value come back *in our tool's result*.

    THREE STATES, BECAUSE THE ABSENT CASE WAS NOT A CLEAN ONE (review, PR #120). A boolean
    made "our tool's result carried no sentinel" and "our tool's result never appeared"
    indistinguishable, and the secret arm read the pair of them as redaction: a run whose reply
    the fixture wrote and copilot never emitted certified `REDACTS`, on the strength of an
    output that was never produced. The fixture's receipts prove the value went ONTO the wire;
    only this says whether anything came back OFF it.

    Searched over the SERIALIZED result rather than one field, because 1.0.80 renders the same
    text under `content`, `detailedContent` and `contents[].text`, and pinning one spelling
    would go quiet on a rendering change while the value sat plainly in the other two.

    The structural clause is `ids`: with no execution of our tool there is no result of ours to
    read, and every `tool.execution_complete` in the stream belongs to something else — which
    is `RESULT_ABSENT`, never `RESULT_CLEAN`.
    """
    ids = mcp_call_ids(events)
    if not ids:
        return RESULT_ABSENT
    state = RESULT_ABSENT
    for obj in events:
        if obj.get("type") != "tool.execution_complete":
            continue
        data = obj.get("data")
        if not isinstance(data, dict) or data.get("toolCallId") not in ids:
            continue
        if sentinel in json.dumps(data.get("result")):
            return RESULT_CARRIED
        state = RESULT_CLEAN
    return state


def healthy_status_verdict(status: str | None, *, served: bool) -> tuple[str, str]:
    """(verdict, why) for the half of question 2 that is about the STATUS.

    TWO WAYS OF NOT HAVING MEASURED IT, and `answered()` used to see neither. The spelling and
    the status are separate deliverables — slice 2 splits `_INERT_MCP_STATUSES` on the status —
    but only the spelling reached the exit predicate, so a witness naming our server with no
    `status` field at all exited ANSWERED having measured half the question (review, PR #120).

    The second way is subtler and is the same trap this file keeps finding. The question is
    what status a *healthy* server reports; reading whatever status appeared and calling it the
    healthy one assumes the thing being measured. A server that failed to start reports
    `failed`, and nothing in the status itself says which case this is. So health is
    established from OUTSIDE copilot's account — the fixture's own receipts, which show it
    answered our tool with a reply carrying this run's marker — and without that the status is
    a real reading of an unknown condition, not the answer to this question.
    """
    if not isinstance(status, str) or not status:
        return STATUS_ABSENT, ("the witness named our server and carried no status, so the "
                               "value slice 2 has to split its allowlist on was not observed")
    if not served:
        return STATUS_UNWITNESSED, (
            f"the witness reported {status!r}, but nothing outside copilot's own account shows "
            f"this arm's server ever answered our tool — so this is the status of a condition "
            f"the run did not establish, and calling it the HEALTHY status assumes the answer")
    return STATUS_MEASURED, (f"the server answered our tool — proven by its own fixture, not by "
                             f"the CLI — and copilot reported it {status!r}")


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
        if name == f"{server}-{TOOL}":
            return HYPHEN
    if name == TOOL:
        return BARE
    return OTHER


def format_reading(by_source: dict[str, list[str]]) -> tuple[str, str | None]:
    """(verdict, the observed name) for question 1 — the classifier plus the agreement test.

    THREE WAYS OF NOT HAVING AN ANSWER, kept apart because they call for different next steps.
    `UNMEASURED`: the run never exercised the tool. `AMBIGUOUS`: it did, and produced two
    spellings that have to be reconciled before a parser is written. `ONE_SOURCE_ONLY`: one
    source spoke and the other did not, so what the probe has is a name and no agreement —
    and it reports the name, because which source fell silent is the diagnostic.
    """
    state = sources_verdict(by_source)
    if state == SILENT:
        return UNMEASURED, None
    if state == DISAGREED:
        return AMBIGUOUS, None
    if state == PARTIAL:
        return ONE_SOURCE_ONLY, canonical_name(by_source)
    return name_format(canonical_name(by_source)), canonical_name(by_source)


# --- the secret arm's own witnesses, authored by the fixture ---------------------------
def read_receipts(path: str) -> list[dict]:
    """Well-formed records only; a truncated final line is an ordinary ending, not a crash."""
    out = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except OSError:
        return []
    return out


def requested_tool(records: list[dict]) -> bool:
    """Whether a `tools/call` for OUR tool ARRIVED at the fixture in this arm.

    Arrival only. The fixture writes this row before `_reject` and before any answer, on
    purpose — a filter measurement needs what the client SENT, and a refused request still
    arrived. Reported for diagnosis; never the basis of a claim about what came back.
    """
    return any(r.get("kind") == "request" and r.get("method") == "tools/call"
               and r.get("tool") == TOOL for r in records)


def answered_with_marker(records: list[dict]) -> bool:
    """Whether the fixture ANSWERED our tool in this arm with a reply carrying its marker.

    THE REQUEST ROW WAS THE WRONG WITNESS, and this is the repair (review, PR #120). It is
    written before the server decides whether to answer at all: a call refused on protocol
    grounds, or one whose reply never flushed, leaves exactly the same row as a served one. A
    probe reading "the value is absent from the output" off that row certifies redaction for a
    reply that was never produced — the same defect as the empty arm, one layer in.

    The `served` row exists only past a successful `_result`, and it carries
    `carried_identity`: whether the reply the fixture just put on the wire actually began with
    this process's marker. Both clauses are required, and neither is the CLI's account of
    itself.

    NO `is_error` CLAUSE, deliberately. It was here and it could not fail: only the fixture's
    SUCCESS path prefixes the marker, so `carried_identity` already implies a non-error reply,
    and no arm could distinguish a predicate carrying the clause from one without it. An
    assertion that cannot fail is one §4 spends a rule on, so it is gone and the implication it
    was standing in for is asserted directly — §E21 drives an error reply and requires
    `carried_identity` false on it, which is what makes dropping the clause safe rather than
    merely tidy.
    """
    return any(r.get("kind") == "served" and r.get("method") == "tools/call"
               and r.get("tool") == TOOL and r.get("carried_identity") is True
               for r in records)


def held_sentinel(records: list[dict], sentinel: str) -> bool:
    """Whether the fixture that served this arm was carrying THIS run's marker.

    A DIGEST, because the receipts file lands where the CLI can read it under `--allow-all`
    and the plain marker there would be a second route to the value (PR #110). sha256 is not
    invertible, so the driver can still recognise it and the CLI cannot mint it.
    """
    want = hashlib.sha256(sentinel.encode("utf-8")).hexdigest()
    return any(r.get("kind") == "listening" and r.get("identity_digest") == want
               for r in records)


def arm_exchanged(records: list[dict], sentinel: str) -> bool:
    """Whether this arm ran the exchange the secret question is about.

    Two independent facts, both required: the server that answered was holding THIS run's
    marker (`held_sentinel`, from the digest on the startup row), and it actually produced a
    reply carrying that marker (`answered_with_marker`, from a row written after the reply
    flushed). Either alone is satisfied by a run that could not have put the value into the
    output, which is exactly what the absence downstream is being read as evidence of.
    """
    return held_sentinel(records, sentinel) and answered_with_marker(records)


def secret_verdict(secret_stream: str, sentinel: str, *, control_exchanged: bool,
                   control_result: str, secret_exchanged: bool,
                   secret_result: str) -> tuple[str, str]:
    """(verdict, why) for `--secret-env-vars`, with every structural gate read FIRST.

    BOTH ARMS MUST HAVE RUN THE SAME EXCHANGE, and the control's half arrived last (review,
    PR #120). The secret arm's gate came first: absence of the sentinel under the flag is
    evidence only if THAT arm actually ran the exchange, since otherwise the value never
    travelled in that run. But the control was still being asked only whether the sentinel
    appeared *somewhere* in its output — and the marker also sits in an env var and in the
    config file this probe writes, under `--allow-all`. So a control that echoed a config and
    never called anything, paired with a secret arm that completed the exchange and whose reply
    simply did not render, produced `REDACTS`. The comparison is only a comparison if both arms
    are established to have done the same thing.

    Each arm therefore carries the same two witnesses, from the same two authors: its fixture's
    receipts (a different process) say the tool was answered with this run's marker, and
    copilot's own result event says whether that reply reached the output. THE SECOND ONE WAS
    ASKED OF THE CONTROL ALONE, which is the same imbalance one round later (review, PR #120):
    the secret arm had to prove the reply was WRITTEN, not that anything came back. A run with
    an execution and no completion event — killed mid-call, or one whose result copilot never
    emitted — has nothing to redact and nothing to have redacted, and it read as `REDACTS`.

    So the secret arm's result is read in three states rather than searched for a substring.
    `RESULT_ABSENT` is incomplete: no output to judge. `RESULT_CARRIED` and a sentinel anywhere
    else in the stream are both `NO_REDACTION` — the flag is a claim about the OUTPUT, so the
    route the value leaked by is diagnosis, not a different verdict. Only a result that came
    back without it, in an arm whose fixture proved it went out with it, is redaction.
    """
    if not control_exchanged:
        return CONTROL_INCOMPLETE, ("the control arm never completed the exchange either — its "
                                    "fixture did not record answering our tool with this run's "
                                    "marker — so the two arms did not do the same thing and "
                                    "the difference between them is not about the flag")
    if control_result != RESULT_CARRIED:
        return CONTROL_FAILED, (
            "the control's own tool RESULT never carried the sentinel"
            + (" — no result of our tool reached its output at all"
               if control_result == RESULT_ABSENT else
               " — its result came back without the value, with no flag set")
            + ", so the value does not reach the output by that route and its absence under "
              "--secret-env-vars measures nothing")
    if not secret_exchanged:
        return SECRET_ARM_INCOMPLETE, ("the arm under --secret-env-vars never completed the "
                                       "exchange that carries the value — its fixture did not "
                                       "record both this run's marker and a call to the tool "
                                       "that returns it — so the sentinel's absence from its "
                                       "output is the absence of the CALL, not of the value")
    if secret_result == RESULT_ABSENT:
        return SECRET_ARM_INCOMPLETE, ("the arm under --secret-env-vars answered our tool and "
                                       "then emitted no result for it at all, so nothing came "
                                       "back to be redacted — the fixture proves the value went "
                                       "onto the wire and nothing shows what copilot did with "
                                       "it, which is not the same as showing it removed it")
    if sentinel in secret_stream:
        return NO_REDACTION, (
            "the sentinel appears in the output WITH --secret-env-vars naming its variable"
            + (" — in our tool's own result" if secret_result == RESULT_CARRIED else
               " — outside our tool's result, which came back without it")
            + ": the flag did not redact the value where it landed")
    return REDACTS, ("both arms answered our tool with this run's marker, the control's tool "
                     "RESULT carried it into the output, the secret arm's result came back too "
                     "— and the value is absent from that arm's output entirely, so the flag "
                     "redacted the value itself")


def mcp_config(path: str, sentinel: str, receipts: str) -> str:
    """One stdio echo server under `CONFIG_KEY`, advertising `ADVERTISED_NAME`.

    NO `type` KEY, deliberately: the stdio gating probe omits it too and its servers start, so
    this arm inherits a shape already measured to work rather than introducing an untested
    variable into a run whose subject is something else.
    """
    server = {"command": sys.executable, "args": [ECHO],
              "env": {"ECHO_MCP_SERVER_NAME": ADVERTISED_NAME,
                      "ECHO_MCP_IDENTITY": sentinel,
                      "ECHO_MCP_RECEIPTS": receipts}}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"mcpServers": {CONFIG_KEY: server}}, handle)
    return path


def run_arm(workdir: str, sentinel: str, *, redact: bool) -> tuple[str, list[dict]]:
    """One copilot run. Returns (stdout+stderr, the fixture's receipts for THIS arm).

    ONE RECEIPTS FILE PER ARM: the fixture appends, and two arms sharing a file would let the
    control's call satisfy the secret arm's structural gate — the two runs agreeing with each
    other rather than each being measured (§4).

    The prompt names the server and tool explicitly and asks for the reply VERBATIM: the
    identity marker rides back in the tool's answer, which is what puts the sentinel into
    copilot's own output and gives the secret arm something to redact.
    """
    tag = uuid.uuid4().hex
    receipts = os.path.join(workdir, f"receipts-{'secret' if redact else 'control'}-{tag}.jsonl")
    config = mcp_config(os.path.join(workdir, f"cfg-{tag}.json"), sentinel, receipts)
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
        stream = (done.stdout or "") + (done.stderr or "")
    except FileNotFoundError:
        stream = ""
    except subprocess.TimeoutExpired:
        stream = ""
    return stream, read_receipts(receipts)


def agreed_version(streams: list[str]) -> tuple[str, bool]:
    """(text, usable) over EVERY arm the probe launched — the same rule the gating probes
    settled on, and now over the arms rather than over the ones that answered.

    One witness per arm, and the set must be a singleton. An arm with no witness is precisely
    the arm that could have executed a different build — or no build at all — so absence
    anywhere is unverified rather than tolerated. Filtering the empty ones out first is what
    let a vanished secret arm ride through version agreement (review, PR #120).
    """
    if not streams:
        return "(no runs to witness)", False
    found = [_stream_cli_version(s or "") for s in streams]
    if any(f is None for f in found):
        return f"an executed arm produced no in-band version witness ({[f or '-' for f in found]})", False
    if len(set(found)) != 1:
        return f"the arms did not run the same build: {sorted(str(f) for f in set(found))}", False
    return found[0], True


def answered(fmt: str, spelling: str, status_state: str, secret: str) -> bool:
    """Whether all three questions were ANSWERED — a conjunction, never a lookup.

    Each term is a separate question, and a run that settled two of them has not established
    what Phase 2 needs. `REPORTS_NEITHER` is unanswered rather than a negative finding, for the
    same reason `UNMEASURED` is: a stream that never named our server and a stream that never
    arrived are the same bytes. `AMBIGUOUS` joins them: two spellings for one tool is a
    question with two answers, which is not an answer. So do `ONE_SOURCE_ONLY` — a name with
    no agreement observed — and `REPORTS_BOTH`, where the run used both server spellings and
    the contract is therefore not one thing.

    THE STATUS IS ITS OWN TERM, because question 2 asks two things and only one of them used to
    reach here: a run naming our server with no status at all answered the spelling and left
    the value slice 2 splits its allowlist on unmeasured.
    """
    return (fmt not in (UNMEASURED, AMBIGUOUS, ONE_SOURCE_ONLY)
            and spelling not in (REPORTS_NEITHER, REPORTS_BOTH)
            and status_state == STATUS_MEASURED
            and secret in (REDACTS, NO_REDACTION))


def main() -> int:
    sentinel = f"SENT-{uuid.uuid4().hex}"
    workdir = tempfile.mkdtemp(prefix="probe-copilot-events-")
    print(f"config key   : {CONFIG_KEY}")
    print(f"advertised   : {ADVERTISED_NAME}")
    print(f"sentinel     : {sentinel}\n")

    control, control_receipts = run_arm(workdir, sentinel, redact=False)
    secret, secret_receipts = run_arm(workdir, sentinel, redact=True)
    if not (control or secret):
        print("INSTRUMENT_FAILED: no arm produced any output (is `copilot` on PATH?)")
        return 1

    # EVERY ARM LAUNCHED, not every arm that spoke. An arm that produced nothing is the one
    # whose build is least accounted for.
    version, version_ok = agreed_version([control, secret])

    # Q1 POOLS BOTH ARMS; Q2 DOES NOT. Whether the model calls a tool is its own decision, so
    # reading one arm and falling back would report `UNMEASURED` whenever the arm consulted
    # happened not to call it while the arm beside it carried the answer. Server loading is
    # NOT the model's decision — the MCP host initializes before the model acts — so question 2
    # is read from the control arm, one run, where a status SEQUENCE has a meaning that
    # concatenating two runs' streams would destroy. The secret arm's reading is printed
    # beside it so a disagreement stays visible.
    ev_control, ev_secret = parse_events(control), parse_events(secret)
    by_source = mcp_names_by_source(tool_names(ev_control) + tool_names(ev_secret))
    fmt, observed = format_reading(by_source)
    fields, fields_why = fields_verdict(mcp_fields(ev_control) + mcp_fields(ev_secret),
                                        executions(ev_control) + executions(ev_secret))

    seen = loaded_servers(ev_control)
    spelling, statuses = declared_spelling(seen)
    status = effective_status(statuses)
    seen_secret = loaded_servers(ev_secret)
    spelling_secret, statuses_secret = declared_spelling(seen_secret)

    # ONE FACT, TWO QUESTIONS. Whether the CONTROL arm's server actually answered our tool is
    # what makes its status the HEALTHY one (question 2) and what makes its output a
    # comparison for the secret arm (question 4). It was computed for neither and printed for
    # both (review, PR #120).
    control_exchanged = arm_exchanged(control_receipts, sentinel)
    status_state, status_why = healthy_status_verdict(status, served=control_exchanged)

    verdict, why = secret_verdict(
        secret, sentinel,
        control_exchanged=control_exchanged,
        control_result=tool_result_state(ev_control, sentinel),
        secret_exchanged=arm_exchanged(secret_receipts, sentinel),
        secret_result=tool_result_state(ev_secret, sentinel))

    print(f"version            : {version} ({'usable' if version_ok else 'UNVERIFIED'})")
    print(f"servers (control)  : {seen or '(none)'}")
    print(f"servers (secret)   : {seen_secret or '(none)'}")
    print(f"Q2 name spelling   : {spelling}   statuses={statuses}   effective={status!r}")
    print(f"Q2 healthy status  : {status_state}\n    {status_why}")
    print(f"   secret arm said : {spelling_secret}   statuses={statuses_secret}"
          f"{'   (DISAGREES with the control)' if spelling_secret != spelling else ''}")
    print(f"tool names by source: {by_source or '(none)'}")
    print(f"Q1 tool format     : {fmt}   observed={observed!r}")
    print(f"Q1 structured      : {fields}\n    {fields_why}")
    print(f"Q4 secret          : {verdict}\n    {why}")
    for label, rows, evs in (("control", control_receipts, ev_control),
                             ("secret ", secret_receipts, ev_secret)):
        print(f"   {label} receipts: arrived={requested_tool(rows)} "
              f"answered_with_marker={answered_with_marker(rows)} "
              f"server_held_marker={held_sentinel(rows, sentinel)} "
              f"tool_result={tool_result_state(evs, sentinel)}")

    # THE FOOTER PROMISED A DIRECTORY AND NOTHING WAS EVER PUT IN IT. These readings are the
    # slice's deliverable and they get quoted into a design document, so the stream they came
    # from has to outlive the print: a reading nobody can re-derive is a claim, not a
    # measurement.
    for label, text in (("control", control), ("secret", secret)):
        with open(os.path.join(workdir, f"{label}.stream"), "w", encoding="utf-8") as fh:
            fh.write(text)

    ok = answered(fmt, spelling, status_state, verdict) and version_ok
    print(f"\n{'ANSWERED' if ok else 'NOT FULLY ANSWERED'} — "
          f"raw streams and receipts under {workdir}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
