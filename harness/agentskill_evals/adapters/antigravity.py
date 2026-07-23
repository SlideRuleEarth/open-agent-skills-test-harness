"""AntiGravity (Google) adapter.

Invocation:
    agy -p "<prompt>" --output-format json [--add-dir DIR] \\
        [--dangerously-skip-permissions] [--model MODEL]

Version provenance is _VERIFIED_VERSIONS below, not this header — the findings in this
file were established at different times against different builds, and a single "verified
against X" line at the top could only ever be wrong about most of them. (It was: it said
1.0.16 while the comments below cited 1.1.1, which is the prose-rot failure this
adapter's provenance constant exists to stop.) Each finding names the build it was
checked on, inline, where it can be re-checked.

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
    VersionProvenance,
    extract_command,
    extract_path,
    iter_jsonl,
    snake_case_keys,
    try_load_json,
    warn_unknown_usage,
)

# --- CLI version provenance (see base.VersionProvenance) ---------------------------
#
# 1.1.1: the build the MCP/customization-channel inventory below was established against
#        — the four customization roots and the plugin mcp_config.json channel, each
#        confirmed with a live sentinel stdio server. That inventory is what this
#        adapter's hermeticity argument rests on, so it is what the constant tracks.
#
# NOT 1.1.2, which is what was installed on the dev host on 2026-07-21. The invocation
# contract was re-checked there and holds (`agy --help` still documents -p/--print,
# --add-dir, --dangerously-skip-permissions, --model), but the channel inventory was NOT
# re-established, and that is the part a new build can quietly invalidate. Listing 1.1.2
# here because it "seems fine" would be the constant blessing an unknown state — the
# exact prose-rot failure it replaced. So a 1.1.2 run warns, correctly: the drift is real
# and un-re-verified, and the warning is how it stays visible until someone re-runs the
# sentinel tests.
_VERIFIED_VERSIONS = ("1.1.1",)
_VERIFIED_ON = "2026-07-21"

# Why the executing version cannot be read from the run, checked on 2026-07-21:
#
#   * The `--output-format json` result object carries no version — its keys are exactly
#     _KNOWN_RESULT_KEYS below.
#   * Neither does the transcript this adapter already reads off disk
#     (transcript_full.jsonl steps carry step_index/source/type/status/created_at/content).
#
# There IS a candidate: `~/.gemini/antigravity-cli/cli.log` opens with a "Language server
# version: <v>" banner the run itself writes. It is not used, for two reasons that would
# each have to be settled first. It sits at a fixed shared path, so attributing a line to
# THIS run depends on the isolated HOME actually containing it rather than the real one —
# unverified. And it is a free-form log the agent's own activity also writes into, so a
# naive scan would be reading a channel that model-controlled text can reach, which is the
# forgery hazard the copilot adapter had to design around. Until both are settled, "we do
# not know" is the honest answer.
#
# An `agy --version` probe is not the answer either: the standing rule is that a fact
# learned by executing the program again may not CLEAR a security decision, and a probe
# resolves its own code path — it can truthfully report a build the run never used.
_VERSION_UNREADABLE = (
    "neither agy's `--output-format json` result nor the transcript it writes to disk "
    "states the executing version"
)

_PROVENANCE = VersionProvenance(
    agent="antigravity",
    verified=_VERIFIED_VERSIONS,
    verified_on=_VERIFIED_ON,
    unreadable=_VERSION_UNREADABLE,
    analysis="MCP/customization-channel hermeticity analysis",
    clear_hint=(
        "There is nothing to clear per-run. Check `agy --version` against the list above "
        "out of band; if it differs, re-establish the customization-root and plugin "
        "mcp_config channel findings in adapters/antigravity.py with live sentinel "
        "servers, then update _VERIFIED_VERSIONS."),
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
    # agy has no MCP flags at all — server discovery is purely file-based, so isolation
    # masks every discovery file (DESIGN_MCP_Support.md, Phase 0): the global
    # mcp_config.json (which the overlay would otherwise pass through as a symlink —
    # .gemini/config/ is a real dir in the overlay, being an ancestor of the skills leaf
    # above), and plugins.json, which can register EXTERNAL plugin directories by absolute
    # path — their mcp_config.json files live outside every masked tree, so the
    # registration itself has to go.
    isolation_config_masks = {".gemini/config/mcp_config.json": '{"mcpServers": {}}',
                              ".gemini/config/plugins.json": "{}"}
    # Plugins in the registry are an MCP channel of their own — "MCP Servers defined in
    # plugins/<name>/mcp_config.json" (agy 1.1.1 embedded plugin docs) — so the registry
    # overlay materializes that file inside every plugin. With no flag-level kill-switch,
    # isolation is the ONLY MCP-off mechanism on this runner: non-isolated runs can't be
    # made hermetic (the runner warns) and an overlay failure fails closed.
    plugin_registry_config_masks = {"mcp_config.json": '{"mcpServers": {}}'}
    # agy also discovers MCP configs from the RUN WORKSPACE via --add-dir — outside the
    # HOME overlay's reach, so the runner neutralizes any *seeded* ones. It recognizes
    # FOUR customization roots (verified 1.1.1: a sentinel stdio server in each launched
    # at session start): .agents/, .agent/, _agents/, and _agent/. Per root, three
    # channels: the root's own mcp_config.json, every in-root plugin's
    # plugins/<name>/mcp_config.json, and plugins.json — which registers EXTERNAL plugin
    # directories by path (entries/inherits schema, per the embedded json_configs.md;
    # verified 1.1.1: an external dir registered from a workspace plugins.json launched
    # its MCP server), so the registration file itself must be neutralized. Ancestors of
    # the workspace are NOT scanned (verified 1.1.1), and a config the agent itself
    # writes mid-run is out of scope (discovery happens at session start). The plugins
    # channel is masked with BOTH a `plugins/*/` and a `plugins/.*/` glob: Python's glob
    # excludes dot-leading names from `*`, but agy discovers DOT-prefixed plugin dirs too
    # (verified: a `plugins/.hidden/mcp_config.json` launched its server), so the
    # dot-inclusive companion pattern is required to reach them (glob's `.*` never matches
    # `.`/`..` — scandir-based — so it's exactly the hidden-name complement of `*`).
    workspace_config_masks = {
        f"{root}/{rel}": content
        for root in (".agents", ".agent", "_agents", "_agent")
        for rel, content in (("mcp_config.json", '{"mcpServers": {}}'),
                             ("plugins/*/mcp_config.json", '{"mcpServers": {}}'),
                             ("plugins/.*/mcp_config.json", '{"mcpServers": {}}'),
                             ("plugins.json", "{}"))
    }

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

    def _probe_argv(self, model: str, *, cwd: Optional[str] = None,
                    env: Optional[dict] = None):
        # --add-dir: without it a bare `agy -p` operates against the fixed, shared
        # ~/.gemini/antigravity-cli/scratch dir and leaves state there (see module
        # docstring). The anchor must be the probe's fresh PRIVATE workspace
        # (probe_model creates one and passes it as cwd) — never a shared dir like the
        # system temp root, where anyone's planted .agents/mcp_config.json (or .agent/,
        # _agents/, _agent/, their plugins/, plugins.json) would load as workspace MCP
        # config; /tmp is world-writable on Linux. agy does not scan the anchor's
        # ancestors (verified 1.1.1), so a fresh empty 0700 dir is hermetic.
        # --output-format json keeps the probe's output shape consistent with real runs.
        anchor = os.path.abspath(cwd) if cwd else tempfile.mkdtemp(prefix="ase-probe-agy-")
        return [self.binary, "-p", "say ok", "--dangerously-skip-permissions",
                "--output-format", "json", "--add-dir", anchor,
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

    def verify_post_run(self, argv: list[str], opts: RunOptions, *, cwd: str,
                        stdout: str = "", stderr: str = "", exit_code: int = 0) -> None:
        """Record that this run's agy build is unidentifiable, and against which build the
        channel inventory was actually established.

        No denylist check, and none is possible: every version-keyed tier needs a version.
        VersionProvenance refuses to hold a denylist next to ``unreadable`` for that
        reason. The hermeticity argument itself rests on the isolation overlay masking the
        customization roots, which is evidence about this run's filesystem rather than
        about which build read it.
        """
        _PROVENANCE.warn_drift(None)

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
