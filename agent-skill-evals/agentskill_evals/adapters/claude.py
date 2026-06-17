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
from typing import Any

from ..schema import EventKind, NormalizedEvent
from .base import Adapter, ParseOutput, RunOptions, extract_command, extract_path, iter_jsonl

# Claude tool names that mutate files (so we can also tag them as file touches).
_FILE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "Create"}
_SHELL_TOOLS = {"Bash", "BashOutput"}


class ClaudeAdapter(Adapter):
    name = "claude"
    binary = "claude"

    def format_skill(self, skill: str) -> str:
        return f"/{skill}"

    def build_argv(self, prompt: str, opts: RunOptions) -> list[str]:
        argv = [
            self.binary,
            "-p",
            prompt,
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

    def parse(self, stdout: str, stderr: str, exit_code: int) -> ParseOutput:
        events: list[NormalizedEvent] = []
        final_text = ""
        structured: Any = None
        cost = None
        dur = None
        last_assistant_text = ""

        for obj in iter_jsonl(stdout):
            etype = obj.get("type")

            if etype == "system" and obj.get("subtype") == "init":
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
                        cmd = extract_command(inp) if name in _SHELL_TOOLS else None
                        path = extract_path(inp) if name in _FILE_TOOLS else None
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
                        events.append(
                            NormalizedEvent(
                                EventKind.TOOL_RESULT,
                                raw=block,
                                is_error=bool(block.get("is_error")),
                            )
                        )

            elif etype == "result":
                result_text = obj.get("result")
                if isinstance(result_text, str):
                    final_text = result_text
                cost = obj.get("total_cost_usd", cost)
                dur = obj.get("duration_ms", dur)
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
        )
