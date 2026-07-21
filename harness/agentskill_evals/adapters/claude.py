"""Claude Code adapter.

Invocation:
    claude -p "<prompt>" --output-format stream-json --verbose \
           [--dangerously-skip-permissions] [--model M] [--json-schema '<schema>']

`--verbose` is REQUIRED with stream-json in print mode. Output is JSONL of
Anthropic SDK message objects:

    {"type":"system","subtype":"init", ...}
    {"type":"assistant","message":{"content":[{"type":"text"...},
                                              {"type":"tool_use","name":"Bash",
                                               "input":{"command":"..."}}]}}
    {"type":"user","message":{"content":[{"type":"tool_result", ...}]}}
    {"type":"result","subtype":"success","result":"...","total_cost_usd":...,
     "duration_ms":..., "is_error":false}
"""

from __future__ import annotations

import json
from typing import Any, Optional

from ..schema import EventKind, NormalizedEvent
from .base import (Adapter, ParseOutput, ProbeResult, RunOptions, VersionProvenance,
                   extract_command, extract_path, iter_jsonl, warn_unknown_usage)

_FILE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "Create"}
_READ_TOOLS = {"Read", "View"}
_SHELL_TOOLS = {"Bash", "BashOutput"}
# Keys observed on the `result` event as of claude CLI mid-2026 (verified 2026-07-08 against
# a captured run). warn_unknown_usage compares ALL result keys, so an incomplete list here
# prints a spurious warning on EVERY run — keep this in sync when the CLI adds fields, and
# only then decide whether the new field carries billing data worth capturing.
_KNOWN_RESULT_KEYS = {
    "type", "subtype", "result", "is_error",
    "total_cost_usd", "duration_ms", "duration_api_ms", "structured_output",
    "session_id", "model", "modelUsage", "usage", "num_turns",
    "permission_denials", "stop_reason", "terminal_reason", "api_error_status",
    "fast_mode_state", "uuid",
}


# --- CLI version provenance (see base.VersionProvenance) ---------------------------
#
# 2.1.113: verified 2026-07-21. Three things were actually checked, because a constant
#          that blesses an unknown state is worse than no constant:
#            * `--strict-mcp-config` still exists and still means what the argument here
#              rests on — `--help` describes it as "Only use MCP servers from
#              --mcp-config, ignoring all other MCP configurations". That single flag
#              carries this adapter's whole MCP argument, so it is the marker worth
#              auditing; there is no per-version channel inventory to go stale because
#              this adapter never enumerates channels.
#            * The witness below holds live: six captured 2.1.113 runs all report
#              `mcp_servers: []` in their init event.
#            * The parser contract: the `result` events of those same six runs carry no
#              key outside _KNOWN_RESULT_KEYS.
_VERIFIED_VERSIONS = ("2.1.113",)
_VERIFIED_ON = "2026-07-21"

# Builds found to actively break an assumption. Empty is the normal state.
_DENIED_VERSIONS: dict[str, str] = {}

_PROVENANCE = VersionProvenance(
    agent="claude",
    verified=_VERIFIED_VERSIONS,
    verified_on=_VERIFIED_ON,
    denied=_DENIED_VERSIONS,
    analysis="MCP hermeticity + parser analysis",
    witness_held=(
        "  The runtime witness held: the CLI reported an empty MCP server list, so "
        "--strict-mcp-config did what this adapter relies on it for. That does NOT cover "
        "a discovery channel the flag stopped governing: if a newer build grew a server "
        "source outside --mcp-config's reach, the list would be empty for the wrong "
        "reason and look identical here."),
    witness_absent=(
        "  This run reported no MCP server list at all — it did not complete far enough "
        "to emit its init event, which is allowed but proves nothing about its MCP host. "
        "So --strict-mcp-config was NOT confirmed effective here."),
    clear_hint=(
        "To clear it: confirm `claude --help` still documents --strict-mcp-config as "
        "ignoring all MCP configuration outside --mcp-config, then add the version to "
        "_VERIFIED_VERSIONS in adapters/claude.py."),
)


def _stream_cli_version(stdout: str) -> Optional[str]:
    """The CLI version that actually EXECUTED, read out of the child's own stream.

    Claude states this directly: the ``system``/``init`` event carries
    ``claude_code_version`` as a first-class scalar the CLI writes about itself. That is
    the whole reason this is trustworthy — it needs no second execution, so it cannot
    disagree with what ran, unlike a preflight ``claude --version`` which resolves its own
    code path and can honestly report a build the real invocation never used.

    It is also structural, not prose: nothing a model emits and nothing the workspace
    contains reaches this field, so it cannot be forged by an assistant message or by a
    repo laid out to look like a version string. (Copilot has to reconstruct its version
    from skill paths and pays for that in care; here there is nothing to reconstruct.)

    EVERY init event is read, not just the first. Stopping at the first was a real defect
    (found in review): a stream carrying a second init event could state a different
    version, and taking the leading one would report whichever build the stream *opened*
    with rather than resolving the disagreement. Distinct versions therefore collapse to
    None — the same rule copilot applies to its app-root paths — because a stream that
    tells two stories about what ran has not established either.

    Returns None when the event is absent, malformed, or self-contradictory: all of them
    mean the version is unknown, which warns, and additionally FAILS the run when the
    adapter has a non-empty denylist that the unknown version cannot be excluded from
    (see VersionProvenance.check_denied). Must not raise: this runs inside verify_post_run,
    where anything raised is reported as an MCP hermeticity failure, and malformed
    telemetry is not one.
    """
    seen: set[str] = set()
    for obj in iter_jsonl(stdout):
        if not isinstance(obj, dict):
            continue
        if obj.get("type") != "system" or obj.get("subtype") != "init":
            continue
        version = obj.get("claude_code_version")
        if isinstance(version, str) and version:
            seen.add(version)
    return seen.pop() if len(seen) == 1 else None


def _mcp_witness(stdout: str, exit_code: int) -> tuple[Optional[str], list[str], bool]:
    """Check the run's own account of its MCP host. Returns (violation, live, witnessed).

    The init event lists ``mcp_servers``; under ``--strict-mcp-config`` with no
    ``--mcp-config`` passed, a hermetic run reports that list empty. Reading it from the
    run being judged is what makes this immune to the ABA problem that any
    inspect-the-disk-afterwards check has: a config planted inside the launch window and
    reverted before exit leaves the filesystem looking clean, but the CLI already loaded
    it and says so here.

    A run that did not complete normally is EXCUSED (witnessed=False) rather than failed:
    a crash before the init event is not evidence of a leak. That distinction is why
    `witnessed` is threaded into the drift warning — claiming the witness held on a run
    that never produced one would be the notice inventing its own evidence.

    EVERY init event is examined and their server lists are UNIONED, rather than trusting
    the first. Returning at the first one was a real defect (found in review): a stream
    whose opening init reported an empty list and whose second reported a live server
    passed verification, because the evidence that mattered arrived after the check had
    already made up its mind. An adapter that reads only the start of a stream can be
    told anything by the rest of it, so a server named anywhere counts as loaded, and a
    reshaped list anywhere is a violation.
    """
    violation: Optional[str] = None
    live: list[str] = []
    witnessed = False
    for obj in iter_jsonl(stdout):
        if not isinstance(obj, dict):
            continue
        if obj.get("type") != "system" or obj.get("subtype") != "init":
            continue
        servers = obj.get("mcp_servers")
        if not isinstance(servers, list):
            # The field this contract is read from is gone or reshaped. On a run that
            # otherwise completed, that is a contract violation rather than a clean
            # result: "no servers found" and "the field moved" are indistinguishable
            # outcomes, and only one of them is safe.
            if violation is None:
                violation = "an init event carries no `mcp_servers` list"
            continue
        witnessed = True
        for s in servers:
            name = str(s.get("name") if isinstance(s, dict) else s)
            if name not in live:
                live.append(name)
    if violation is not None:
        # Report the violation, but hand back whatever servers WERE named: a stream that
        # both reshaped one event and loaded a server in another should not lose the
        # second fact to the first.
        return (violation, live, False)
    if witnessed:
        return (None, live, True)
    if exit_code == 0:
        return ("the run completed but emitted no system/init event", [], False)
    return (None, [], False)


class ClaudeAdapter(Adapter):
    name = "claude"
    binary = "claude"
    global_skills_subpaths = [".claude/skills"]
    # CLAUDE_CONFIG_DIR overrides ~/.claude (skills under it). Under isolation it's mirrored +
    # repointed (custom config dir kept, skills masked), else cleared to the isolated home.
    isolation_config_homes = [("CLAUDE_CONFIG_DIR", ".claude", "skills")]

    supports_output_schema = True
    # `--effort <level>` (verified 2026-07-08: choices low|medium|high|xhigh|max — the
    # harness only passes the typed cross-runner subset low|medium|high).
    supports_reasoning_effort = True

    # TODO: Claude Code has no `list-models` command yet (feature request pending).
    # When one ships, add has_model_list = True and a discover_models() override
    # like Codex and AntiGravity have — then probing falls back to free discovery.

    # Hermetic flags — no memory, no hooks, no MCP, no saved sessions, no
    # user/project settings leaking in.  Avoids --bare because it also blocks
    # keychain/OAuth auth (requires ANTHROPIC_API_KEY).
    _HERMETIC = [
        "--no-session-persistence",
        "--strict-mcp-config",
        "--settings", '{"autoMemory": false, "hooks": {}}',
        "--setting-sources", "",
    ]

    def _probe_argv(self, model: str):
        return [self.binary, "-p", "say ok", *self._HERMETIC,
                "--output-format", "stream-json",
                "--verbose", "--model", model, "--dangerously-skip-permissions"]

    def _parse_probe_cost(self, output: str) -> ProbeResult:
        for line in output.splitlines():
            try:
                obj = json.loads(line.strip())
            except (json.JSONDecodeError, ValueError):
                continue
            if obj.get("type") == "result":
                cost = obj.get("total_cost_usd")
                return ProbeResult(accepted=True,
                                   cost_usd=float(cost) if cost is not None else None)
        return ProbeResult(accepted=True)

    def format_skill(self, skill: str) -> str:
        return f"/{skill}"

    def build_argv(self, prompt: str, opts: RunOptions, *, cwd: str) -> list[str]:
        argv = [
            self.binary,
            "-p",
            prompt,
            *self._HERMETIC,
            "--output-format",
            "stream-json",
            "--verbose",  # mandatory with stream-json in -p
        ]
        if opts.auto_approve:
            argv += ["--dangerously-skip-permissions"]
        if opts.model:
            argv += ["--model", opts.model]
        if opts.reasoning_effort:
            argv += ["--effort", opts.reasoning_effort]
        if opts.output_schema:
            argv += ["--json-schema", json.dumps(opts.output_schema)]
        if opts.allowed_tools:
            argv += ["--allowedTools", ",".join(opts.allowed_tools)]
        if opts.disable_tools:
            argv += ["--tools", ""]  # reasoning-only (judge mode)
        argv += opts.extra_args
        return argv

    def verify_post_run(self, argv: list[str], opts: RunOptions, *, cwd: str,
                        stdout: str = "", stderr: str = "", exit_code: int = 0) -> None:
        """Confirm from the run's own stream that it was MCP-hermetic, and record which
        build produced that evidence.

        Ordered the same way as copilot's, and for the same reasons. The denylist is
        REPORTED first because it covers exactly what the runtime evidence cannot: a
        defect that leaves the witness perfectly intact fires no runtime check at all, so
        a denial must not be masked by a contract failure found on the same run. The drift
        warning runs LAST, only on a run that cleared every gate, so a genuine hermeticity
        failure is never buried under a version notice.

        The witness is COMPUTED before the denylist check but RAISED after it. That split
        looks fussy and is load-bearing: check_denied needs to know whether the run got far
        enough to be judged (an unknown version fails closed on a completed run once
        anything is denylisted, but must not on a crash), while the reporting order above
        still has to put a denial ahead of a contract failure.
        """
        version = _stream_cli_version(stdout)
        broken, live, witnessed = _mcp_witness(stdout, exit_code)
        _PROVENANCE.check_denied(version, completed=witnessed)
        if broken is not None:
            raise RuntimeError(
                f"claude's MCP witness does not hold: {broken}. The run finished normally, "
                "and that stream is where the ABA-immune half of this audit gets its "
                "evidence — a hermetic run on a build this adapter understands always "
                "reports its MCP server list. A witness that is missing or reshaped yields "
                "'no servers found', which reads exactly like a clean run, so it is "
                "refused instead: the run's hermeticity is unwitnessed rather than "
                "confirmed; failing closed."
            )
        if live:
            raise RuntimeError(
                f"claude reports MCP server(s) {', '.join(sorted(live))} loaded during "
                "this run, but --strict-mcp-config with no --mcp-config should leave that "
                "list empty. Either the flag no longer governs every server source, or "
                "something in this invocation supplied one. The state on disk may read "
                "clean now — a config planted inside the launch window and reverted "
                "before exit would — but the run itself was not MCP-hermetic."
            )
        _PROVENANCE.warn_drift(version, witnessed=witnessed)

    def parse(self, stdout: str, stderr: str, exit_code: int,
               *, opts: Optional[RunOptions] = None) -> ParseOutput:
        events: list[NormalizedEvent] = []
        final_text = ""
        structured: Any = None
        cost = None
        dur = None
        resolved_model: Optional[str] = None
        last_assistant_text = ""

        for obj in iter_jsonl(stdout):
            etype = obj.get("type")

            if etype == "system" and obj.get("subtype") == "init":
                resolved_model = obj.get("model") or resolved_model
                events.append(NormalizedEvent(EventKind.SESSION_START, raw=obj))

            elif etype == "assistant":
                content = (obj.get("message") or {}).get("content") or []
                for block in content:
                    btype = block.get("type")
                    if btype == "text":
                        txt = block.get("text", "")
                        last_assistant_text = txt
                        events.append(
                            NormalizedEvent(EventKind.AGENT_MESSAGE, raw=block, text=txt)
                        )
                    elif btype == "tool_use":
                        name = block.get("name")
                        inp = block.get("input") or {}
                        # StructuredOutput is the --json-schema delivery mechanism,
                        # not a real tool the skill invoked: capture it, don't trace it.
                        if name == "StructuredOutput":
                            structured = inp
                            continue
                        cmd = None
                        path = None
                        if name == "Skill":
                            skill_name = inp.get("skill") or ""
                            if skill_name:
                                path = f"{self.skills_subdir}/{skill_name}/SKILL.md"
                        elif name in _SHELL_TOOLS:
                            cmd = extract_command(inp)
                        elif name in (_FILE_TOOLS | _READ_TOOLS):
                            path = extract_path(inp)
                        else:
                            # Glob/Grep/LS/WebFetch/etc. all take a `path`-shaped argument
                            # for an arbitrary absolute location, not just cwd — leaving
                            # these unhandled would silently drop that leak signal from
                            # leaked_skill_reads() (see workspace_view.py).
                            cmd = extract_command(inp)
                            if not cmd:
                                path = extract_path(inp)
                        events.append(
                            NormalizedEvent(
                                EventKind.TOOL_CALL,
                                raw=block,
                                tool_name=name,
                                command=cmd,
                                path=path,
                            )
                        )

            elif etype == "user":
                content = (obj.get("message") or {}).get("content") or []
                for block in content:
                    if block.get("type") == "tool_result":
                        result_content = block.get("content")
                        text = ""
                        if isinstance(result_content, str):
                            text = result_content
                        elif isinstance(result_content, list):
                            text = "\n".join(
                                p.get("text", "") for p in result_content
                                if isinstance(p, dict) and p.get("type") == "text"
                            )
                        events.append(
                            NormalizedEvent(
                                EventKind.TOOL_RESULT,
                                raw=block,
                                text=text,
                                is_error=bool(block.get("is_error")),
                            )
                        )

            elif etype == "result":
                warn_unknown_usage("claude", obj, _KNOWN_RESULT_KEYS)
                result_text = obj.get("result")
                if isinstance(result_text, str):
                    final_text = result_text
                cost = obj.get("total_cost_usd", cost)
                dur = obj.get("duration_ms", dur)
                resolved_model = obj.get("model") or resolved_model
                # With --json-schema, the validated object is delivered in a
                # dedicated `structured_output` field (the `result` string is
                # just the assistant's closing text). Fall back to parsing the
                # result string only if that field is absent.
                if obj.get("structured_output") is not None:
                    structured = obj["structured_output"]
                elif structured is None and isinstance(result_text, str):
                    try:
                        structured = json.loads(result_text)
                    except (json.JSONDecodeError, ValueError):
                        structured = None
                events.append(
                    NormalizedEvent(
                        EventKind.RESULT,
                        raw=obj,
                        text=final_text,
                        is_error=bool(obj.get("is_error")),
                    )
                )

        if not final_text:
            final_text = last_assistant_text

        return ParseOutput(
            events=events,
            final_text=final_text,
            structured_output=structured,
            cost_usd=cost,
            duration_ms=dur,
            resolved_model=resolved_model,
        )
