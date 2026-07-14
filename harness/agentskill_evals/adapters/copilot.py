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

import json
import os
import sys
from typing import Any, Optional

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
    warn_unknown_usage,
)

_SHELL_TOOLS = {"shell", "bash", "run_command"}
_FILE_TOOLS = {"write", "edit", "create", "multi_edit"}
_VIEW_TOOLS = {"view", "read"}
_KNOWN_USAGE_KEYS = {
    "premiumRequests", "totalApiDurationMs", "sessionDurationMs", "codeChanges",
}

# Workspace-level MCP config files copilot discovers, checked in the run cwd and every
# ancestor (copilot walks up, like git-root discovery — verified in the 1.0.64 bundle).
_WORKSPACE_MCP_FILES = (".mcp.json", ".github/mcp.json", ".vscode/mcp.json")

_warned_windows_registry = False


def _mcp_server_names(path: str) -> list[str]:
    """Server names declared in one MCP config JSON — ``{"mcpServers": {...}}`` (copilot's
    user/workspace format) or ``{"servers": {...}}`` (the .vscode/mcp.json format).
    Unreadable or invalid → [] (copilot would reject such a file too)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    names: list[str] = []
    if isinstance(data, dict):
        for key in ("mcpServers", "servers"):
            block = data.get(key)
            if isinstance(block, dict):
                names.extend(str(k) for k in block)
    return names


# Plugin state lives in ~/.copilot/config.json: "installedPlugins" records carry an
# absolute cache_path the loader follows even when installed-plugins/ is masked empty
# (verified 1.0.64 — an empty mirrored plugin dir still exposed a plugin's MCP server).
_COPILOT_PLUGIN_STATE_KEYS = ("installedPlugins", "enabledPlugins")


def _sanitized_copilot_config(real_path: str) -> str:
    """Sanitizing mask for ~/.copilot/config.json: keep everything (auth tokens, settings)
    but drop the plugin registrations. The file is JSONC-ish — full-line ``//`` comments
    above/inside the JSON — so strip those before parsing. Unreadable/unparseable → "{}"
    (fail closed: no plugins can load; auth loss surfaces loudly rather than servers
    silently loading)."""
    try:
        with open(real_path, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return "{}"
    text = "\n".join(ln for ln in raw.splitlines() if not ln.lstrip().startswith("//"))
    try:
        data = json.loads(text)
    except ValueError:
        return "{}"
    if isinstance(data, dict):
        for key in _COPILOT_PLUGIN_STATE_KEYS:
            data.pop(key, None)
    return json.dumps(data, indent=2)


class CopilotAdapter(Adapter):
    name = "copilot"
    binary = "copilot"
    skills_subdir = ".agents/skills"
    global_skills_subpaths = [".agents/skills"]
    # MCP hermeticity (DESIGN_MCP_Support.md, Phase 0) — copilot has no "ignore user MCP
    # config" flag (`--disable-builtin-mcps` below only covers the bundled GitHub server),
    # and servers come from several places: ~/.copilot/mcp-config.json, installed plugins
    # (each plugin's definition can declare mcpServers), and workspace configs. Isolation
    # masks the plugin channel twice over — installed-plugins/ → empty dir AND config.json
    # sanitized, because plugin records there carry an absolute cache_path that loads the
    # real plugin dir even when installed-plugins/ is empty (verified 1.0.64) — so plugin
    # skills/agents are unavailable in isolated runs. mcp-config.json is replaced with the
    # empty shape copilot actually accepts: bare "{}" fails validation with "mcpServers:
    # Required" (verified 1.0.64), which would kill the run before execution.
    # _mcp_disable_args additionally disables every *enumerable* server by name on argv,
    # which also covers probes, judge runs, and non-isolated runs (where plugins remain a
    # documented gap). Windows-only residual: copilot also discovers ODR servers from the
    # registry (HKLM\...\CurrentVersion\Mcp), which no file mask or enumeration reaches.
    isolation_config_masks = {".copilot/mcp-config.json": '{"mcpServers": {}}',
                              ".copilot/installed-plugins": None,
                              ".copilot/config.json": _sanitized_copilot_config}
    # COPILOT_HOME replaces ~/.copilot wholesale (verified in 1.0.64's bundle) — without
    # mirroring it, a set var would bypass the masks above.
    isolation_config_homes = [("COPILOT_HOME", ".copilot", None)]
    # `--reasoning-effort <level>` (verified 2026-07-08: choices none|low|medium|high|
    # xhigh|max — the harness only passes the typed cross-runner subset low|medium|high).
    supports_reasoning_effort = True

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
                *self._mcp_disable_args(os.getcwd()),
                "--model", model, "--output-format", "json", "--allow-all"]

    def _mcp_disable_args(self, cwd: Optional[str]) -> list[str]:
        """``--disable-mcp-server <name>`` for every enumerable server: the user config
        ($COPILOT_HOME else ~/.copilot, mcp-config.json) plus the workspace configs copilot
        discovers from the run cwd upward. Riding on argv makes this cover every invocation
        the same way — cells (including a scenario-seeded workspace config), model probes,
        judge runs, and non-isolated runs — with the isolation masks as the second layer.
        Plugin-declared servers can't be enumerated here (their names live inside each
        plugin's definition); those are covered by the installed-plugins isolation mask and
        remain a documented gap for non-isolated runs."""
        global _warned_windows_registry
        if sys.platform == "win32" and not _warned_windows_registry:  # pragma: no cover
            _warned_windows_registry = True
            print("warning: [copilot] on Windows, copilot also discovers MCP servers from "
                  "the registry (HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Mcp); "
                  "the harness cannot mask or enumerate that source — those servers stay "
                  "live.", file=sys.stderr)
        home = os.environ.get("COPILOT_HOME") or os.path.join(os.path.expanduser("~"),
                                                              ".copilot")
        names = _mcp_server_names(os.path.join(home, "mcp-config.json"))
        if cwd:
            d = os.path.abspath(cwd)
            while True:
                for rel in _WORKSPACE_MCP_FILES:
                    names.extend(_mcp_server_names(os.path.join(d, *rel.split("/"))))
                parent = os.path.dirname(d)
                if parent == d:
                    break
                d = parent
        args: list[str] = []
        for name in dict.fromkeys(names):  # de-dupe, keep order
            args += ["--disable-mcp-server", name]
        return args

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

    def build_argv(self, prompt: str, opts: RunOptions, *, cwd: str) -> list[str]:
        argv = [self.binary, "-p", prompt, *self._HERMETIC,
                *self._mcp_disable_args(cwd), "--output-format", "json"]
        if opts.auto_approve:
            argv += ["--allow-all"]
        if opts.model:
            argv += ["--model", opts.model]
        if opts.reasoning_effort:
            argv += ["--reasoning-effort", opts.reasoning_effort]
        if opts.disable_tools:
            argv += ["--available-tools", ""]
        argv += opts.extra_args
        return argv

    def parse(self, stdout: str, stderr: str, exit_code: int,
               *, opts: Optional[RunOptions] = None) -> ParseOutput:
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
                            # A file-tool write gets its own FILE_CHANGE event below, not
                            # duplicated here — RunResult.file_paths_touched() reads paths from
                            # BOTH TOOL_CALL and FILE_CHANGE kinds, so putting the same path on
                            # both would double-count a single write (unlike Claude/Codex, which
                            # each report a file write via only one of the two kinds).
                            path=None if name in _FILE_TOOLS else path,
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
                warn_unknown_usage("copilot", usage, _KNOWN_USAGE_KEYS)
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
