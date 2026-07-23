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
import os
from typing import Any, Optional

from ..notices import warn
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


def _mcp_witness(stdout: str,
                 exit_code: int) -> tuple[Optional[str], list[str], bool, dict]:
    """Check the run's own account of its MCP host.

    Returns (violation, live, witnessed, statuses).

    ``live`` is every server the run reports, DECLARED OR NOT — deciding which of those
    were supposed to be there belongs to the caller, which is the only layer that knows
    what the scenario asked for. Filtering here would make the witness an accomplice to
    the policy it exists to check.

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
    statuses: dict[str, Optional[str]] = {}
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
            # `status` was being discarded, so a server reported `{"name": "echo",
            # "status": "failed"}` counted as successfully present and passed verification
            # without even the missing-server warning (found in review). Recorded per name,
            # strictest reading wins: once a server is seen in a non-connected state that
            # sticks, because a stream that reports the same server both ways has not
            # established that the scenario got the tool surface it asked for.
            status = s.get("status") if isinstance(s, dict) else None
            status = str(status) if status is not None else None
            if name not in statuses or statuses[name] == "connected":
                statuses[name] = status
    if violation is not None:
        # Report the violation, but hand back whatever servers WERE named: a stream that
        # both reshaped one event and loaded a server in another should not lose the
        # second fact to the first.
        return (violation, live, False, statuses)
    if witnessed:
        return (None, live, True, statuses)
    if exit_code == 0:
        return ("the run completed but emitted no system/init event", [], False, {})
    return (None, [], False, {})


class ClaudeAdapter(Adapter):
    name = "claude"
    binary = "claude"
    global_skills_subpaths = [".claude/skills"]
    # CLAUDE_CONFIG_DIR overrides ~/.claude (skills under it). Under isolation it's mirrored +
    # repointed (custom config dir kept, skills masked), else cleared to the isolated home.
    isolation_config_homes = [("CLAUDE_CONFIG_DIR", ".claude", "skills")]
    # Nothing. Measured against 2.1.113 on macOS, 2026-07-23: an empty HOME with only the
    # masked skills dir runs, emits its `system`/`init` event with `claude_code_version`
    # intact (so version provenance still reads the executing build), and answers.
    #
    # The reason it costs nothing is worth writing down, because it is macOS-specific and
    # will not hold for every adapter. claude's HOME-side auth is the login KEYCHAIN, at
    # ~/Library/Keychains — redirect HOME and it reports "Not logged in", and the only way to
    # symlink it back is an outward symlink, which a contained home cannot have. The keychain
    # is also uncopyable in practice: it is every password on the machine, and a copy is not
    # auto-unlocked, so a headless run would block on a password prompt. So auth arrives
    # instead as CLAUDE_CODE_OAUTH_TOKEN in the environment (verified: authenticates against
    # a wholly empty home), which the operator exports like any other harness credential and
    # which base.env() already passes through. The harness deliberately does NOT read the
    # keychain itself — acquiring that capability silently is not something a test harness
    # should do, and the token never touches disk this way.
    contained_home_subpaths: list[str] = []

    supports_output_schema = True
    # `--effort <level>` (verified 2026-07-08: choices low|medium|high|xhigh|max — the
    # harness only passes the typed cross-runner subset low|medium|high).
    supports_reasoning_effort = True

    # Declared servers ride in on `--mcp-config` (stdio shape verified live, 2.1.113).
    supports_mcp_injection = True
    # Per-server `tools:` is REFUSED here rather than half-enforced. The only claude
    # mechanism that gates MCP tools is `--disallowedTools` on the complement of the
    # allowlist (`--allowedTools` does nothing under --dangerously-skip-permissions —
    # measured, DESIGN_MCP_Support.md §6-C2), and computing a complement requires the
    # server's full tool list, which is knowable only by starting the server and asking it
    # — a SECOND server instance that can answer differently from the one claude launches.
    # That gap is the design's C3 (a harness-owned filtering proxy), deliberately not built
    # yet, so the honest state is "no enforcement mechanism implemented" and the validator
    # refuses `tools:` instead of accepting an allowlist that would not apply.
    mcp_tool_filter = "unbuilt"

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

    def mcp_servers_seen(self, argv: list[str]) -> Optional[list[str]]:
        """``[]`` — this run could not have had MCP servers — or None if argv stops saying so.

        Unlike codex and copilot, which neutralize servers by NAME and so report the disable
        set, claude's whole MCP argument is one flag: ``--strict-mcp-config`` restricts the
        run to servers from ``--mcp-config``, and this adapter never passes one. That is a
        POSITIVE claim about the configuration, not an absence of information, so it is
        reported as the empty set rather than as unknown — and the distinction decides
        whether a matrix reads `verified` or `unverified`, since an axis nobody can read
        must not be counted as agreement.

        Both halves are checked on the argv actually used, not assumed from build_argv:
        ``extra_args`` rides at the end verbatim, so a programmatic caller can append
        ``--mcp-config``. Then servers may exist and their names live in a JSON file this
        cannot resolve — unknown, so None. Same if ``--strict-mcp-config`` is gone.

        This is a comparability report, never a safety decision: hermeticity is decided by
        the run's own init event in `verify_post_run`, which sees servers from any source,
        including ones argv never mentions.
        """
        if "--strict-mcp-config" not in argv:
            return None
        if any(a == "--mcp-config" or a.startswith("--mcp-config=") for a in argv):
            return None
        return []

    def _write_mcp_config(self, opts: RunOptions) -> str:
        """Materialize `<scratch>/mcp.json` and return its path.

        A FILE, not `--mcp-config '<inline json>'`: argv is archived verbatim into
        result.json, so an inline config would publish every resolved credential into the
        artifacts. The file lives in the runner's per-cell scratch dir — outside the
        workspace, which is archived and inlined into report.md — and is created 0600 so it
        is not readable by other users for the seconds it exists.

        Written on every build_argv call rather than cached, because build_argv is the only
        hook that runs after the runner has created the scratch dir and before the child
        starts, and a stale file from a previous cell would silently outrank the current
        scenario's servers.
        """
        if not opts.mcp_scratch_dir:
            raise RuntimeError(
                "claude: mcp_servers were declared but no scratch dir was provided — "
                "refusing to write MCP config with resolved secrets into the workspace, "
                "which is archived into artifacts and inlined into report.md.")
        servers: dict[str, Any] = {}
        for name, s in opts.mcp_servers.items():
            if s.is_stdio:
                entry: dict[str, Any] = {"command": s.command}
                if s.args:
                    entry["args"] = list(s.args)
                if s.env:
                    entry["env"] = dict(s.env)
            else:
                # `type` is claude's transport discriminator; `http` and `sse` are the two
                # documented values (§2 — this half is still INFERRED, unlike the stdio
                # shape which is verified live against fixtures/echo_mcp_server.py).
                entry = {"type": s.transport, "url": s.url}
                if s.headers:
                    entry["headers"] = dict(s.headers)
            servers[name] = entry

        path = os.path.join(opts.mcp_scratch_dir, "mcp.json")
        # Create with 0600 from the start — writing then chmod'ing would leave a window
        # where the credentials are world-readable.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"mcpServers": servers}, fh)
        return path

    def validate_mcp_support(self, mcp_servers: dict) -> tuple[list[str], list[str]]:
        errors, warnings = super().validate_mcp_support(mcp_servers)
        # Point at the reason and the escape hatch rather than the internal state name —
        # "unbuilt" tells the scenario author nothing actionable.
        errors = [e + (" (claude's only MCP tool filter is deny-the-complement, which "
                       "needs a tool list this harness cannot obtain without a second, "
                       "independently answerable server instance — see C3 in "
                       "DESIGN_MCP_Support.md)"
                       if "tools:" in e and "not implemented" in e else "")
                  for e in errors]
        return errors, warnings

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
        if opts.mcp_servers:
            # --strict-mcp-config is already in _HERMETIC, so these become the ONLY servers
            # the run can reach — the opt-in is hermetic for free (§5.1).
            argv += ["--mcp-config", self._write_mcp_config(opts)]
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
        broken, live, witnessed, statuses = _mcp_witness(stdout, exit_code)
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
        # `opts` is None on direct calls (selftest, out-of-tree callers). Absent options
        # mean nothing was declared, which is the STRICT reading: every reported server is
        # then undeclared and fails the run. Defaulting the other way would let a missing
        # argument silently permit any server at all.
        declared = set(getattr(opts, "mcp_servers", None) or {})
        undeclared = sorted(s for s in live if s not in declared)
        if undeclared:
            expected = (f"only the declared server(s) {', '.join(sorted(declared))}"
                        if declared else
                        "that list empty, since --strict-mcp-config was passed with no "
                        "--mcp-config")
            raise RuntimeError(
                f"claude reports MCP server(s) {', '.join(undeclared)} loaded during this "
                f"run, but this invocation should have had {expected}. Either "
                "--strict-mcp-config no longer governs every server source, or something "
                "in this invocation supplied one. The state on disk may read clean now — a "
                "config planted inside the launch window and reverted before exit would — "
                "but the run itself was not MCP-hermetic."
            )
        # A DECLARED server that is missing from the witness is not a hermeticity failure
        # (nothing leaked) and is not silently fine either: the scenario asked for a tool
        # surface it did not get, so assertions about it will fail confusingly. Surfaced as
        # a warning rather than a raise, because verify_post_run's raises all mean "this
        # run was not hermetic" and widening that would blur what a failure here means.
        missing = sorted(declared - set(live))
        if missing and witnessed:
            warn(f"warning: [claude] declared MCP server(s) {', '.join(missing)} were not "
                 "reported by the run — the scenario ran without them; check the server "
                 "command and its startup output.")
        # Being NAMED in the witness is not the same as being usable. A server reported
        # `{"name": "echo", "status": "failed"}` used to clear this check silently — it was
        # present, so it was not "missing", and its status was discarded. That is the same
        # confusing outcome as a missing server (assertions about tools that never existed)
        # and it gets the same warning. Unknown states warn too rather than being assumed
        # good: a status this adapter does not recognise is not evidence of health.
        #
        # Through `warn`, not `print`. The message's own claim — that assertions will fail
        # "for a reason the results will not show" — was true of the message as well when it
        # went only to the harness process's stderr, which nothing archives. It now lands on
        # RunResult.warnings, so the cell that fails confusingly carries its own explanation.
        unhealthy = sorted(
            (name, statuses.get(name)) for name in declared & set(live)
            if statuses.get(name) != "connected"
        )
        if unhealthy and witnessed:
            detail = ", ".join(f"{n} ({s or 'no status reported'})" for n, s in unhealthy)
            warn(f"warning: [claude] declared MCP server(s) {detail} were reported by the "
                 "run but not as connected — their tools were most likely unavailable, so "
                 "assertions about them will fail for a reason the results would otherwise "
                 "not show.")
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
            # Same reader the hermeticity check uses, so the recorded build is the one the
            # verification was reasoning about rather than a second, possibly different,
            # determination of it.
            cli_version=_stream_cli_version(stdout),
        )
