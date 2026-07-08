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
from .base import Adapter, ParseOutput, ProbeResult, RunOptions, extract_command, extract_path, iter_jsonl, warn_unknown_usage

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


class ClaudeAdapter(Adapter):
    name = "claude"
    binary = "claude"
    global_skills_subpaths = [".claude/skills"]
    # CLAUDE_CONFIG_DIR overrides ~/.claude (skills under it). Under isolation it's mirrored +
    # repointed (custom config dir kept, skills masked), else cleared to the isolated home.
    isolation_config_homes = [("CLAUDE_CONFIG_DIR", "skills")]

    supports_output_schema = True

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
        if opts.output_schema:
            argv += ["--json-schema", json.dumps(opts.output_schema)]
        if opts.allowed_tools:
            argv += ["--allowedTools", ",".join(opts.allowed_tools)]
        if opts.disable_tools:
            argv += ["--tools", ""]  # reasoning-only (judge mode)
        argv += opts.extra_args
        return argv

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
