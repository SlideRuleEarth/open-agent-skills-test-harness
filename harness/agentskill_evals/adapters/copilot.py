"""GitHub Copilot CLI adapter.

Invocation:
    copilot -p "<prompt>" --output-format json --allow-all [--model MODEL]

  * Binary is `copilot`.  `-p`/`--prompt` runs a single prompt non-interactively.
  * Auto-approve: `--allow-all` enables all permissions (tools + paths + URLs).
    `--allow-all-tools` alone is required for non-interactive mode; `--allow-all`
    also unlocks paths and URLs.
  * Model flag is `--model` (not `-m`).
  * `--output-format json` emits JSONL (one JSON object per line).

Output is JSONL of session/assistant/tool events:

    {"type":"session.skills_loaded","data":{...},...,"ephemeral":true}
    {"type":"user.message","data":{"content":"..."},...}
    {"type":"assistant.message","data":{"model":"...","content":"...","toolRequests":[
        {"toolCallId":"...","name":"shell","arguments":{"command":"npm install"}},
        {"toolCallId":"...","name":"view","arguments":{"path":"..."}}],...}}
    {"type":"tool.execution_start","data":{"toolCallId":"...","toolName":"shell",...}}
    {"type":"tool.execution_complete","data":{"toolCallId":"...","success":true,
        "result":{"content":"..."},...}}
    {"type":"assistant.turn_end","data":{"turnId":"0"}}
    {"type":"result","exitCode":0,"usage":{"premiumRequests":1,
        "totalApiDurationMs":...,"sessionDurationMs":...}}

Ephemeral events (`session.*`, `assistant.message_start/delta`,
`assistant.reasoning_delta`) are streaming fragments — we skip them and parse
only the non-ephemeral `assistant.message`, `tool.*`, and `result` events.

Verified against copilot 1.0.63 on 2026-06-22.
"""

from __future__ import annotations

from typing import Any

from ..schema import EventKind, NormalizedEvent
from .base import (
    Adapter,
    ParseOutput,
    ProbeResult,
    RunOptions,
    extract_command,
    extract_path,
    iter_jsonl,
    try_load_json,
)

_SHELL_TOOLS = {"shell", "bash", "run_command"}
_FILE_TOOLS = {"write", "edit", "create", "multi_edit"}
_VIEW_TOOLS = {"view", "read"}


class CopilotAdapter(Adapter):
    name = "copilot"
    binary = "copilot"
    skills_subdir = ".agents/skills"
    global_skills_subpaths = [".agents/skills"]

    # Hermetic flags — memory is already off in -p mode; these block the
    # remaining state channels (custom instructions / AGENTS.md, built-in
    # MCP servers, remote control, auto-update downloads).
    _HERMETIC = [
        "--no-custom-instructions",
        "--disable-builtin-mcps",
        "--no-remote",
        "--no-auto-update",
    ]

    def _probe_argv(self, model: str):
        return [self.binary, "-p", "say ok", *self._HERMETIC,
                "--model", model, "--output-format", "json", "--allow-all"]

    def _parse_probe_cost(self, output: str) -> ProbeResult:
        import json as _json
        for line in output.splitlines():
            try:
                obj = _json.loads(line.strip())
            except (ValueError, _json.JSONDecodeError):
                continue
            if obj.get("type") == "result":
                usage = obj.get("usage") or {}
                pr = usage.get("premiumRequests")
                return ProbeResult(accepted=True,
                                   premium_requests=float(pr) if pr is not None else None)
        return ProbeResult(accepted=True)

    def format_skill(self, skill: str) -> str:
        return f"/{skill}"

    def build_argv(self, prompt: str, opts: RunOptions) -> list[str]:
        argv = [self.binary, "-p", prompt, *self._HERMETIC, "--output-format", "json"]
        if opts.auto_approve:
            argv += ["--allow-all"]
        if opts.model:
            argv += ["--model", opts.model]
        if opts.disable_tools:
            argv += ["--available-tools", ""]
        argv += opts.extra_args
        return argv

    def parse(self, stdout: str, stderr: str, exit_code: int) -> ParseOutput:
        events: list[NormalizedEvent] = []
        final_text = ""
        structured: Any = None
        duration_ms = None
        premium_requests = None
        resolved_model = None
        assistant_buf: list[str] = []
        seen_tools: set[str] = set()

        for obj in iter_jsonl(stdout):
            if obj.get("ephemeral"):
                continue

            etype = obj.get("type", "")
            data = obj.get("data") or obj

            if etype == "user.message":
                continue

            if etype == "assistant.turn_start":
                if not events:
                    events.append(NormalizedEvent(EventKind.SESSION_START, raw=obj))
                continue

            if etype == "assistant.message":
                if resolved_model is None:
                    m = data.get("model")
                    if isinstance(m, str) and m:
                        resolved_model = m

                content = data.get("content") or ""
                if isinstance(content, str) and content.strip():
                    assistant_buf.append(content)
                    events.append(
                        NormalizedEvent(EventKind.AGENT_MESSAGE, raw=data, text=content)
                    )

                reasoning = data.get("reasoningText")
                if isinstance(reasoning, str) and reasoning.strip():
                    events.append(
                        NormalizedEvent(EventKind.REASONING, raw=data, text=reasoning)
                    )

                for req in data.get("toolRequests") or []:
                    tc_id = req.get("toolCallId")
                    name = req.get("name") or "tool"
                    args = req.get("arguments") or {}

                    if tc_id and tc_id in seen_tools:
                        continue
                    if tc_id:
                        seen_tools.add(tc_id)

                    if name == "report_intent":
                        continue

                    cmd = None
                    path = None
                    if name == "skill":
                        skill_name = args.get("skill") or ""
                        if skill_name:
                            path = f"{self.skills_subdir}/{skill_name}/SKILL.md"
                    elif name in _SHELL_TOOLS:
                        cmd = extract_command(args)
                    elif name in _FILE_TOOLS:
                        path = extract_path(args)
                    elif name in _VIEW_TOOLS:
                        path = args.get("path") or extract_path(args)
                    else:
                        cmd = extract_command(args)
                        if not cmd:
                            path = extract_path(args)

                    events.append(
                        NormalizedEvent(
                            EventKind.TOOL_CALL,
                            raw=req,
                            tool_name=name,
                            command=cmd,
                            path=path,
                        )
                    )
                    if name in _FILE_TOOLS and path:
                        events.append(
                            NormalizedEvent(EventKind.FILE_CHANGE, raw=req, path=path)
                        )
                continue

            if etype == "tool.execution_complete":
                success = data.get("success", True)
                result = data.get("result") or {}
                result_text = result.get("content") if isinstance(result, dict) else None
                events.append(
                    NormalizedEvent(
                        EventKind.TOOL_RESULT,
                        raw=data,
                        is_error=not success,
                        text=result_text,
                    )
                )
                continue

            if etype == "result":
                usage = obj.get("usage") or {}
                duration_ms = usage.get("sessionDurationMs")
                pr = usage.get("premiumRequests")
                if pr is not None:
                    premium_requests = float(pr)
                final_text = assistant_buf[-1] if assistant_buf else ""
                events.append(
                    NormalizedEvent(EventKind.RESULT, raw=obj, text=final_text)
                )
                continue

            if etype == "error":
                events.append(
                    NormalizedEvent(
                        EventKind.ERROR, raw=obj, text=str(data), is_error=True
                    )
                )

        if not final_text and assistant_buf:
            final_text = assistant_buf[-1]

        if final_text:
            structured = try_load_json(final_text)

        return ParseOutput(
            events=events,
            final_text=final_text,
            structured_output=structured,
            premium_requests=premium_requests,
            duration_ms=duration_ms,
            resolved_model=resolved_model,
        )
