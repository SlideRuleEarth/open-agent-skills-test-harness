"""Codex (OpenAI) adapter.

Invocation (auto-approve: approval/sandbox flags are top-level, before `exec`):
    codex --ask-for-approval never --sandbox workspace-write exec --json [-m MODEL] "<prompt>"

Output is JSONL of "item" events:

    {"type":"thread.started","thread_id":"..."}
    {"type":"item.started","item":{"id":"...","type":"command_execution",
                                   "command":"npm install"}}
    {"type":"item.completed","item":{"id":"...","type":"command_execution",
                                     "exit_code":0,"aggregated_output":"..."}}
    {"type":"item.completed","item":{"type":"agent_message","text":"..."}}
    {"type":"turn.completed","usage":{...}}

We dedupe by (item id, kind) so a command that appears in both item.started and
item.completed is only counted once as a TOOL_CALL.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any, Optional

from ..schema import EventKind, NormalizedEvent
from .base import Adapter, ParseOutput, ProbeResult, RunOptions, extract_command, extract_path, iter_jsonl, try_load_json, warn_unknown_usage


_KNOWN_USAGE_KEYS = {"input_tokens", "output_tokens", "reasoning_tokens", "total_tokens"}


class CodexAdapter(Adapter):
    name = "codex"
    binary = "codex"
    skills_subdir = ".agents/skills"  # Codex reads $REPO_ROOT/.agents/skills (cross-agent convention)
    # Global skills dirs codex discovers (the .system vendor bundle in ~/.codex/skills is kept).
    global_skills_subpaths = [".codex/skills", ".agents/skills"]
    # CODEX_HOME overrides ~/.codex (skills under $CODEX_HOME/skills). Under isolation it's
    # mirrored + repointed (custom home kept, skills masked), else cleared to the isolated home.
    isolation_config_homes = [("CODEX_HOME", "skills")]
    has_model_list = True

    def discover_models(self) -> Optional[list[str]]:
        try:
            r = subprocess.run(
                [self.binary, "debug", "models"], capture_output=True, text=True, timeout=10,
            )
            if r.returncode != 0:
                return None
            data = json.loads(r.stdout)
            return [m["slug"] for m in data.get("models", []) if m.get("slug")]
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError,
                json.JSONDecodeError, KeyError):
            return None

    def _probe_argv(self, model: str):
        return [self.binary, "--ask-for-approval", "never", "--sandbox", "read-only",
                "exec", "--ephemeral", "--disable", "memories",
                "-c", "memories.use_memories=false",
                "-c", "memories.generate_memories=false",
                "--json", "-m", model, "say ok"]

    def _parse_probe_cost(self, output: str) -> ProbeResult:
        import json as _json
        for line in output.splitlines():
            try:
                obj = _json.loads(line.strip())
            except (ValueError, _json.JSONDecodeError):
                continue
            if obj.get("type") == "turn.completed":
                usage = obj.get("usage") or {}
                tokens = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
                if tokens:
                    return ProbeResult(accepted=True, cost_usd=None)
        return ProbeResult(accepted=True)

    def format_skill(self, skill: str) -> str:
        # Mirrors the OpenAI example which referenced skills as "$skill-name".
        return f"${skill}"

    def build_argv(self, prompt: str, opts: RunOptions, *, cwd: str) -> list[str]:
        argv = [self.binary]
        if opts.auto_approve:
            # Non-interactive parity with the deprecated `--full-auto`: never prompt for
            # approval AND allow workspace writes. `-a/--ask-for-approval` and
            # `-s/--sandbox` are top-level options, so they precede the `exec` subcommand.
            argv += ["--ask-for-approval", "never", "--sandbox", "workspace-write"]
        argv += ["exec", "--ephemeral", "--disable", "memories",
                 "-c", "memories.use_memories=false",
                 "-c", "memories.generate_memories=false",
                 "--json"]
        if opts.model:
            argv += ["-m", opts.model]
        argv += opts.extra_args
        argv += [prompt]  # prompt is positional and must come last
        return argv

    def parse(self, stdout: str, stderr: str, exit_code: int,
               *, opts: Optional[RunOptions] = None) -> ParseOutput:
        events: list[NormalizedEvent] = []
        final_text = ""
        structured: Any = None
        seen: set[tuple] = set()

        for obj in iter_jsonl(stdout):
            etype = obj.get("type", "")

            if etype in ("thread.started", "session.created"):
                events.append(NormalizedEvent(EventKind.SESSION_START, raw=obj))
                continue

            if etype.startswith("item."):
                item = obj.get("item") or {}
                itype = item.get("type") or item.get("item_type")
                item_id = item.get("id")

                if itype == "command_execution":
                    cmd = item.get("command") or extract_command(item)
                    id_key = ("cmd", item_id)
                    if id_key not in seen:
                        seen.add(id_key)
                        events.append(
                            NormalizedEvent(
                                EventKind.TOOL_CALL, raw=item, tool_name="shell", command=cmd
                            )
                        )
                    if etype == "item.completed":
                        events.append(
                            NormalizedEvent(
                                EventKind.TOOL_RESULT,
                                raw=item,
                                text=item.get("aggregated_output") or "",
                                is_error=bool(item.get("exit_code")),
                            )
                        )

                elif itype == "file_change":
                    for path in _codex_changed_paths(item):
                        events.append(
                            NormalizedEvent(EventKind.FILE_CHANGE, raw=item, path=path)
                        )

                elif itype in ("agent_message", "assistant_message"):
                    if etype == "item.completed":
                        txt = item.get("text") or item.get("content") or ""
                        if isinstance(txt, str) and txt:
                            final_text = txt
                            events.append(
                                NormalizedEvent(EventKind.AGENT_MESSAGE, raw=item, text=txt)
                            )

                elif itype == "reasoning":
                    if etype == "item.completed":
                        events.append(
                            NormalizedEvent(
                                EventKind.REASONING, raw=item, text=item.get("text")
                            )
                        )

                elif itype in ("mcp_tool_call", "tool_call"):
                    if ("tool", item_id) not in seen:
                        seen.add(("tool", item_id))
                        events.append(
                            NormalizedEvent(
                                EventKind.TOOL_CALL,
                                raw=item,
                                tool_name=item.get("tool") or item.get("server") or "tool",
                                command=extract_command(item.get("arguments") or item),
                                path=extract_path(item.get("arguments") or item),
                            )
                        )
                    # Mirror command_execution: the TOOL_CALL is deduped by id (added once, on
                    # whichever of started/completed arrives first), but its completion — success
                    # or failure — must still be surfaced every time, or a failed MCP/tool call is
                    # otherwise invisible (dropped silently once its id is already in `seen`).
                    if etype == "item.completed":
                        is_err = bool(item.get("error")) or item.get("status") in (
                            "failed", "error")
                        result_text = item.get("result") or item.get("output") or item.get("error")
                        events.append(
                            NormalizedEvent(
                                EventKind.TOOL_RESULT,
                                raw=item,
                                text=str(result_text) if result_text else "",
                                is_error=is_err,
                            )
                        )

                else:
                    # An itype we don't specifically recognize (a native tool the CLI added
                    # since this adapter was written) — attempt generic extraction rather
                    # than silently dropping it, so leaked_skill_reads() still has a
                    # command/path to check instead of the event vanishing entirely.
                    cmd = extract_command(item)
                    path = extract_path(item)
                    if (cmd or path) and ("tool", item_id) not in seen:
                        seen.add(("tool", item_id))
                        events.append(
                            NormalizedEvent(
                                EventKind.TOOL_CALL,
                                raw=item,
                                tool_name=itype or "tool",
                                command=cmd,
                                path=path,
                            )
                        )
                continue

            if etype == "turn.completed":
                usage = obj.get("usage") or {}
                if usage:
                    warn_unknown_usage("codex", usage, _KNOWN_USAGE_KEYS)
                events.append(NormalizedEvent(EventKind.RESULT, raw=obj, text=final_text))

            if etype == "error":
                events.append(
                    NormalizedEvent(EventKind.ERROR, raw=obj, text=str(obj), is_error=True)
                )

        if final_text:
            structured = try_load_json(final_text)

        return ParseOutput(events=events, final_text=final_text, structured_output=structured)


def _codex_changed_paths(item: dict) -> list[str]:
    """Codex file_change items vary in shape; pull paths defensively."""
    paths: list[str] = []
    changes = item.get("changes")
    if isinstance(changes, list):
        for c in changes:
            if isinstance(c, dict):
                p = c.get("path") or c.get("file") or c.get("file_path")
                if p:
                    paths.append(p)
            elif isinstance(c, str):
                paths.append(c)
    elif isinstance(changes, dict):
        paths.extend([k for k in changes.keys()])
    single = item.get("path") or item.get("file_path")
    if single:
        paths.append(single)
    return paths
