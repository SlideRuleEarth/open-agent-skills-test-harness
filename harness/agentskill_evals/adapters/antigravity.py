"""AntiGravity (Google) adapter.

Verified against agy 1.0.9:
    agy -p "<prompt>" [--dangerously-skip-permissions] [--model MODEL]

  * Binary is `agy`, not `antigravity`. `-p` / `--print` / `--prompt` run a
    single prompt non-interactively.
  * Auto-approve tool/file actions: `--dangerously-skip-permissions` (set when
    opts.auto_approve). Older docs' `/goal` prefix is NOT used by this build.
  * Model flag is `--model` (not `-m`).
  * agy 1.0.9 has NO `--output-format` flag — output is plain text, so we don't
    request one by default and parse() falls back to treating stdout as the
    final answer. There is no structured event/tool trace, so prefer filesystem
    and llm_judge assertions over tool-trace ones for AntiGravity. (parse() still
    handles JSONL / single-JSON defensively in case a future build emits them,
    and you can opt in via opts.output_format if a build adds the flag.)
  * First run triggers an interactive Google Sign-In (OAuth) in the browser.
"""

from __future__ import annotations

import subprocess
from typing import Any, Optional

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

import re

_PAREN_RE = re.compile(r"\s*\(([^)]+)\)\s*$")


def _display_to_model_id(display: str) -> str:
    """'Gemini 3.5 Flash (Medium)' -> 'gemini-3.5-flash-medium'."""
    m = _PAREN_RE.search(display)
    tier = ""
    if m:
        tier = "-" + m.group(1).strip().lower()
        display = display[: m.start()]
    return display.strip().lower().replace(" ", "-") + tier


class AntigravityAdapter(Adapter):
    name = "antigravity"
    binary = "agy"
    skills_subdir = ".antigravity/skills"  # best-effort guess; verify per build
    # Global skills dirs agy/gemini discovers (mirrors the root Makefile's GLOBAL_SKILL_DIRS).
    global_skills_subpaths = [
        ".gemini/config/skills",
        ".gemini/antigravity-ide/skills",
        ".antigravity/skills",
    ]

    has_model_list = True

    def discover_models(self) -> Optional[list[str]]:
        """Return model IDs from ``agy models``.

        ``agy models`` prints display names like "Gemini 3.5 Flash (Medium)".
        We normalise to the kebab-case ``--model`` IDs the CLI also accepts:
        ``gemini-3.5-flash-medium``.
        """
        try:
            r = subprocess.run(
                [self.binary, "models"], capture_output=True, text=True, timeout=10,
            )
            if r.returncode != 0:
                return None
            return [
                _display_to_model_id(line.strip())
                for line in r.stdout.splitlines()
                if line.strip()
            ]
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return None

    def _probe_argv(self, model: str):
        return [self.binary, "-p", "say ok", "--dangerously-skip-permissions",
                "--model", model]

    def format_skill(self, skill: str) -> str:
        return f"/{skill}"

    def build_argv(self, prompt: str, opts: RunOptions) -> list[str]:
        argv = [self.binary, "-p", prompt]
        if opts.auto_approve:
            argv += ["--dangerously-skip-permissions"]
        if opts.model:
            argv += ["--model", opts.model]
        # agy 1.0.9 has no --output-format (output is plain text); only pass it
        # if a caller explicitly opts in for a build that supports it.
        if opts.output_format:
            argv += ["--output-format", opts.output_format]
        argv += opts.extra_args
        return argv

    def parse(self, stdout: str, stderr: str, exit_code: int) -> ParseOutput:
        # 1) Try JSONL (stream-style) — generic field mapping.
        jsonl_objs = list(iter_jsonl(stdout))
        if len(jsonl_objs) > 1 or (jsonl_objs and _looks_like_event(jsonl_objs[0])):
            return _parse_generic_events(jsonl_objs, skills_subdir=self.skills_subdir)

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


def _parse_generic_events(objs: list[dict], skills_subdir: str = "") -> ParseOutput:
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
            cmd = extract_command(args)
            path = extract_path(args)
            if name and name.lower() == "skill" and not path and skills_subdir:
                skill_name = args.get("skill") or ""
                if skill_name:
                    path = f"{skills_subdir}/{skill_name}/SKILL.md"
            events.append(
                NormalizedEvent(
                    EventKind.TOOL_CALL,
                    raw=obj,
                    tool_name=name,
                    command=cmd,
                    path=path,
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
