"""AntiGravity (Google) adapter.

Invocation:
    agy -p "<prompt>" [--output-format json] [-m MODEL]

Notes / caveats (AntiGravity's headless interface is young and under-documented
as of this writing):
  * The binary is `agy`, not `antigravity`.
  * `--output-format json` exists but is version-dependent — some builds don't
    recognize it. We request it by default but parse defensively: JSONL first,
    then a single JSON object, then fall back to treating stdout as the final
    answer text.
  * `/goal <prompt>` runs a task to completion without pausing for plan
    approval; we prepend it when auto_approve is set. There is no documented
    `--yolo` equivalent.
  * The structured event schema is undocumented, so tool-trace extraction is
    BEST-EFFORT: we map common field names (type/tool/tool_use/command/...).
    If your build doesn't emit structured tool events, prefer filesystem and
    llm_judge assertions over tool-trace assertions for AntiGravity.

Everything here is intentionally forgiving; tighten it once a build's real
schema is known.
"""

from __future__ import annotations

from typing import Any

from ..schema import EventKind, NormalizedEvent
from .base import (
    Adapter,
    ParseOutput,
    RunOptions,
    extract_command,
    extract_path,
    iter_jsonl,
    try_load_json,
)


class AntigravityAdapter(Adapter):
    name = "antigravity"
    binary = "agy"
    skills_subdir = ".antigravity/skills"  # best-effort guess; verify per build

    def format_skill(self, skill: str) -> str:
        return f"/{skill}"

    def build_argv(self, prompt: str, opts: RunOptions) -> list[str]:
        # /goal runs to completion without approval pauses.
        effective_prompt = f"/goal {prompt}" if opts.auto_approve else prompt
        argv = [self.binary, "-p", effective_prompt]
        # output_format default "json"; pass "" / None to omit (some builds reject it)
        fmt = opts.output_format if opts.output_format is not None else "json"
        if fmt:
            argv += ["--output-format", fmt]
        if opts.model:
            argv += ["-m", opts.model]
        argv += opts.extra_args
        return argv

    def parse(self, stdout: str, stderr: str, exit_code: int) -> ParseOutput:
        # 1) Try JSONL (stream-style) — generic field mapping.
        jsonl_objs = list(iter_jsonl(stdout))
        if len(jsonl_objs) > 1 or (jsonl_objs and _looks_like_event(jsonl_objs[0])):
            return _parse_generic_events(jsonl_objs)

        # 2) Try a single JSON object.
        single = try_load_json(stdout)
        if isinstance(single, dict):
            text = (
                single.get("response")
                or single.get("result")
                or single.get("output")
                or single.get("text")
                or ""
            )
            events = [NormalizedEvent(EventKind.RESULT, raw=single, text=text)]
            return ParseOutput(
                events=events,
                final_text=text if isinstance(text, str) else str(text),
                structured_output=try_load_json(text) if isinstance(text, str) else single,
            )

        # 3) Fall back: raw stdout is the answer.
        text = (stdout or "").strip()
        return ParseOutput(
            events=[NormalizedEvent(EventKind.RESULT, raw={"raw": text}, text=text)],
            final_text=text,
            structured_output=None,
        )


def _looks_like_event(obj: Any) -> bool:
    return isinstance(obj, dict) and any(
        k in obj for k in ("type", "event", "kind", "tool", "tool_use", "role")
    )


def _parse_generic_events(objs: list[dict]) -> ParseOutput:
    """Map an unknown JSONL event stream onto normalized events by field-sniffing."""
    events: list[NormalizedEvent] = []
    final_text = ""
    assistant_buf: list[str] = []

    for obj in objs:
        etype = str(obj.get("type") or obj.get("event") or obj.get("kind") or "").lower()
        name = obj.get("name") or obj.get("tool") or obj.get("tool_name")
        args = obj.get("args") or obj.get("input") or obj.get("arguments") or obj
        text = obj.get("text") or obj.get("content") or obj.get("message")

        if "init" in etype or "start" in etype or "session" in etype:
            events.append(NormalizedEvent(EventKind.SESSION_START, raw=obj))
        elif "tool" in etype and "result" in etype:
            events.append(NormalizedEvent(EventKind.TOOL_RESULT, raw=obj))
        elif "tool" in etype or name:
            events.append(
                NormalizedEvent(
                    EventKind.TOOL_CALL,
                    raw=obj,
                    tool_name=name,
                    command=extract_command(args),
                    path=extract_path(args),
                )
            )
        elif "result" in etype or "final" in etype or "done" in etype:
            if isinstance(text, str):
                final_text = text
            events.append(NormalizedEvent(EventKind.RESULT, raw=obj, text=text))
        elif "message" in etype or "assistant" in etype or text:
            if isinstance(text, str):
                assistant_buf.append(text)
                events.append(NormalizedEvent(EventKind.AGENT_MESSAGE, raw=obj, text=text))
        else:
            events.append(NormalizedEvent(EventKind.OTHER, raw=obj))

    if not final_text:
        final_text = "\n".join(assistant_buf)
    return ParseOutput(
        events=events,
        final_text=final_text,
        structured_output=try_load_json(final_text) if final_text else None,
    )
