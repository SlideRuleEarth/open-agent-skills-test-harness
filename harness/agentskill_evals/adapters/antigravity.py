"""AntiGravity (Google) adapter.

Verified against agy 1.0.16:
    agy -p "<prompt>" --output-format json [--add-dir DIR] \\
        [--dangerously-skip-permissions] [--model MODEL]

  * Binary is `agy`, not `antigravity`. `-p` / `--print` / `--prompt` run a
    single prompt non-interactively.
  * Auto-approve tool/file actions: `--dangerously-skip-permissions` (set when
    opts.auto_approve). Older docs' `/goal` prefix is NOT used by this build.
  * Model flag is `--model` (not `-m`).
  * `--output-format json` IS supported (undocumented — absent from `--help`,
    added sometime between 1.0.10 and 1.0.16) and returns one JSON object:
    `{"conversation_id","status","response","duration_seconds","num_turns","usage"}`.
    Values other than "json" (e.g. "stream-json", "jsonl") are silently ignored and
    fall back to plain text, so this is the one literal string worth passing.
  * Print mode does NOT scope itself to the process's cwd by default — confirmed on
    1.0.16 that a bare `agy -p` operates against a fixed, persistent
    `~/.gemini/antigravity-cli/scratch` directory regardless of subprocess `cwd`, and
    that state written there leaks across unrelated invocations. `--add-dir <abspath>`
    (not a relative path — verified `--add-dir .` still resolves to `scratch`) pins the
    run to the real workspace; `--new-project` also fixes it but permanently registers a
    new entry in the real (unmasked) `~/.gemini/config/projects/`, so `--add-dir` is the
    one that doesn't leave lasting state. This matches the 1.0.12 changelog: "updated the
    project resolution logic to default regardless of the active workspace."
  * No stdout tool-call stream even with `--output-format json` — the `response` field is
    just the final answer. The real step-by-step trace (tool_calls with args, in
    PascalCase keys like `TargetFile`/`DirectoryPath`/`CommandLine`) lives on disk at
    `~/.gemini/antigravity-cli/brain/<conversation_id>/.system_generated/logs/
    transcript_full.jsonl`, keyed by the `conversation_id` the JSON result hands back —
    see `_read_transcript_events`. Plain-text/legacy JSONL parsing is kept as a fallback
    for older or misconfigured builds (tool-trace extraction degrades gracefully then).
  * A plugin registry (`~/.gemini/config/plugins/<name>/skills/...`) is a second,
    independent channel this CLI discovers skills from, separate from the
    `global_skills_subpaths` below — `agy plugin import claude` (or similar) can mirror
    this repo's own skills in there, invisibly bypassing per-eval skill declaration.
    `global_plugin_registry_subpaths` tells the isolation layer to mask it too.
  * First run triggers an interactive Google Sign-In (OAuth) in the browser.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from typing import Any, Optional

from ..schema import EventKind, NormalizedEvent
from .base import (
    Adapter,
    ParseOutput,
    RunOptions,
    extract_command,
    extract_path,
    iter_jsonl,
    snake_case_keys,
    try_load_json,
    warn_unknown_usage,
)

_PAREN_RE = re.compile(r"\s*\(([^)]+)\)\s*$")
# What a normalized model id must look like. `agy models` output is display names, one per
# line, but any banner/header line ("Available models:", blank-ish separators) would
# otherwise get mangled into a bogus id and offered for probing.
_MODEL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]*$")

# Top-level keys of the `--output-format json` result object we've seen.
_KNOWN_RESULT_KEYS = {
    "conversation_id", "status", "response", "duration_seconds", "num_turns", "usage",
}
_KNOWN_USAGE_KEYS = {"input_tokens", "output_tokens", "thinking_tokens", "total_tokens"}

# transcript_full.jsonl step `type`s that carry conversation bookkeeping rather than
# agent-visible behavior — skipped when building events.
_TRANSCRIPT_SKIP_TYPES = {"USER_INPUT", "CONVERSATION_HISTORY", "CHECKPOINT"}


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
    # Plugin registries: each immediate child is a plugin dir that may itself contain a
    # nested skills/ folder (`<plugin>/skills/<skill>/SKILL.md`) — a second skill-discovery
    # channel independent of global_skills_subpaths above (see module docstring).
    global_plugin_registry_subpaths = [".gemini/config/plugins"]
    # agy has no MCP flags at all — server discovery is purely file-based via this config
    # file, which the overlay would otherwise pass through as a symlink to the user's real
    # one (.gemini/config/ is a real dir in the overlay, being an ancestor of the skills
    # leaf above), so isolation materializes it as `{}` (DESIGN_MCP_Support.md, Phase 0).
    isolation_config_masks = [".gemini/config/mcp_config.json"]

    # supports_reasoning_effort stays False: agy has no effort flag — thinking budget is
    # encoded in the model id's tier suffix instead (e.g. gemini-3.5-flash-medium, see
    # _display_to_model_id). RunOptions.reasoning_effort is ignored here (cmd_run warns);
    # pick a tiered model id to control effort on this runner.

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
            ids = [
                _display_to_model_id(line.strip())
                for line in r.stdout.splitlines()
                if line.strip()
            ]
            # drop anything that doesn't normalize to a plausible model id (headers etc.)
            return [m for m in ids if _MODEL_ID_RE.match(m)]
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return None

    def _probe_argv(self, model: str):
        # --add-dir: without it a bare `agy -p` operates against the fixed, shared
        # ~/.gemini/antigravity-cli/scratch dir and leaves state there (see module
        # docstring); the system temp dir is a harmless, non-registering anchor for a
        # trivial "say ok" probe. --output-format json keeps the probe's output shape
        # consistent with real runs.
        return [self.binary, "-p", "say ok", "--dangerously-skip-permissions",
                "--output-format", "json", "--add-dir", tempfile.gettempdir(),
                "--model", model]

    def format_skill(self, skill: str) -> str:
        return f"/{skill}"

    def build_argv(self, prompt: str, opts: RunOptions, *, cwd: str) -> list[str]:
        # --add-dir needs an absolute path — a relative "." still resolves against agy's
        # own default scratch dir, not the subprocess cwd (verified on 1.0.16).
        argv = [self.binary, "-p", prompt, "--output-format", "json",
                "--add-dir", os.path.abspath(cwd)]
        if opts.auto_approve:
            argv += ["--dangerously-skip-permissions"]
        if opts.model:
            argv += ["--model", opts.model]
        argv += opts.extra_args
        return argv

    def parse(self, stdout: str, stderr: str, exit_code: int,
               *, opts: Optional[RunOptions] = None) -> ParseOutput:
        # 1) The `--output-format json` shape we now always request: one object with a
        #    conversation_id we can use to pull the real tool-call trace off disk.
        single = try_load_json(stdout)
        if isinstance(single, dict) and "conversation_id" in single:
            return self._parse_json_result(single, opts)

        # 2) Legacy JSONL (stream-style) — generic field mapping. Kept for older/misconfigured
        #    builds; a current agy never emits this from -p.
        jsonl_objs = list(iter_jsonl(stdout))
        if len(jsonl_objs) > 1 or (jsonl_objs and _looks_like_event(jsonl_objs[0])):
            return _parse_generic_events(jsonl_objs, skills_subdir=self.skills_subdir)

        # 3) A single JSON object without conversation_id (older/partial builds).
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

        # 4) Fall back: raw stdout is the answer (plain-text build, no --output-format).
        text = (stdout or "").strip()
        return ParseOutput(
            events=[NormalizedEvent(EventKind.RESULT, raw={"raw": text}, text=text)],
            final_text=text,
            structured_output=None,
        )

    def _parse_json_result(self, obj: dict, opts: Optional[RunOptions]) -> ParseOutput:
        """Parse the `--output-format json` result object and enrich it with the real
        tool-call trace read from the on-disk transcript (see module docstring)."""
        warn_unknown_usage("antigravity", obj, _KNOWN_RESULT_KEYS)
        usage = obj.get("usage")
        if isinstance(usage, dict):
            warn_unknown_usage("antigravity", usage, _KNOWN_USAGE_KEYS)

        response = obj.get("response") or ""
        status = obj.get("status")
        duration_ms = None
        dur_s = obj.get("duration_seconds")
        if isinstance(dur_s, (int, float)):
            duration_ms = int(dur_s * 1000)

        events = _read_transcript_events(obj.get("conversation_id"), opts, self.skills_subdir)
        events.append(
            NormalizedEvent(
                EventKind.RESULT, raw=obj, text=response,
                is_error=bool(status) and status != "SUCCESS",
            )
        )
        return ParseOutput(
            events=events,
            final_text=response if isinstance(response, str) else str(response),
            structured_output=try_load_json(response) if isinstance(response, str) else None,
            duration_ms=duration_ms,
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
        elif "error" in etype:
            events.append(
                NormalizedEvent(EventKind.ERROR, raw=obj,
                                text=text if isinstance(text, str) else str(obj), is_error=True)
            )
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
            usage = obj.get("usage")
            if isinstance(usage, dict) and usage:
                warn_unknown_usage("antigravity", usage, set())
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


def _events_from_transcript(steps: list[dict], skills_subdir: str = "") -> list[NormalizedEvent]:
    """Map transcript_full.jsonl steps onto normalized events.

    Steps are heterogeneous by design (see module docstring): a PLANNER_RESPONSE step can
    carry `thinking`, `tool_calls`, and/or final `content` independently, and a tool's
    completion is reported as a *separate*, later step whose `type` names the tool
    (`LIST_DIRECTORY`, `RUN_COMMAND`, `CODE_ACTION`, …). Those result-step types aren't
    enumerable ahead of time, so any MODEL-sourced step with `content` that isn't itself a
    PLANNER_RESPONSE is treated as a tool result.
    """
    events: list[NormalizedEvent] = []
    if steps:
        events.append(NormalizedEvent(EventKind.SESSION_START, raw=steps[0]))

    for step in steps:
        stype = str(step.get("type") or "").upper()
        if stype in _TRANSCRIPT_SKIP_TYPES:
            continue

        emitted = False
        thinking = step.get("thinking")
        if isinstance(thinking, str) and thinking.strip():
            events.append(NormalizedEvent(EventKind.REASONING, raw=step, text=thinking))
            emitted = True

        for tc in step.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            name = tc.get("name")
            # agy's tool args are PascalCase (TargetFile, DirectoryPath, CommandLine, …);
            # normalize before delegating to the shared, snake_case-keyed extractors.
            args = snake_case_keys(tc.get("args") or {})
            path = extract_path(args)
            if name and str(name).lower() == "skill" and not path and skills_subdir:
                skill_name = args.get("skill") or ""
                if skill_name:
                    path = f"{skills_subdir}/{skill_name}/SKILL.md"
            events.append(
                NormalizedEvent(
                    EventKind.TOOL_CALL, raw=tc, tool_name=name,
                    command=extract_command(args), path=path,
                )
            )
            emitted = True

        content = step.get("content")
        if stype == "ERROR_MESSAGE":
            events.append(
                NormalizedEvent(
                    EventKind.ERROR, raw=step,
                    text=content if isinstance(content, str) else None,
                    is_error=True,
                )
            )
            emitted = True
        elif stype == "PLANNER_RESPONSE":
            if isinstance(content, str) and content.strip():
                events.append(NormalizedEvent(EventKind.AGENT_MESSAGE, raw=step, text=content))
                emitted = True
        elif step.get("source") == "MODEL" and isinstance(content, str) and content.strip():
            events.append(NormalizedEvent(EventKind.TOOL_RESULT, raw=step, text=content))
            emitted = True

        if not emitted:
            events.append(NormalizedEvent(EventKind.OTHER, raw=step))

    return events


def _read_transcript_events(
    conversation_id: Optional[str], opts: Optional[RunOptions], skills_subdir: str = "",
) -> list[NormalizedEvent]:
    """Read agy's on-disk transcript for *conversation_id* and normalize its steps.

    This is the only source of tool-call detail: `--output-format json`'s stdout carries
    just the final answer (see module docstring). Best-effort — a missing/unreadable
    transcript (older build, moved HOME, …) degrades to no tool-trace events rather than
    failing the run, same spirit as the plain-text fallback in parse() itself.
    """
    if not conversation_id:
        return []
    home = (opts.home if opts else None) or os.path.expanduser("~")
    path = os.path.join(
        home, ".gemini", "antigravity-cli", "brain", conversation_id,
        ".system_generated", "logs", "transcript_full.jsonl",
    )
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return []
    return _events_from_transcript(list(iter_jsonl(text)), skills_subdir)
