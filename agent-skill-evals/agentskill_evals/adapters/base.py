"""Adapter base class + shared parsing helpers.

An adapter is the *only* agent-specific code in the harness. It answers three
questions for one CLI:

  1. build_argv()    -> how do I invoke this agent non-interactively for a prompt?
  2. format_skill()  -> how does a prompt reference a skill in this agent?
  3. parse()         -> how do I turn this agent's raw output into NormalizedEvents?

Everything else (running the process, assertions, judging, reporting) is shared.
"""

from __future__ import annotations

import json
import os
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

from ..schema import NormalizedEvent


@dataclass
class RunOptions:
    """Per-invocation knobs the runner passes to an adapter.

    These are agent-agnostic; each adapter translates the ones it supports into
    native flags and ignores the rest.
    """

    model: Optional[str] = None
    auto_approve: bool = True            # allow file/command execution without prompts
    output_schema: Optional[dict] = None  # JSON Schema for the final structured answer
    allowed_tools: Optional[list[str]] = None
    disable_tools: bool = False          # run reasoning-only (used by the judge)
    extra_args: list[str] = field(default_factory=list)  # raw flags appended verbatim
    output_format: Optional[str] = None  # adapter-specific override (e.g. "json"/"stream-json")
    home: Optional[str] = None           # isolated HOME for this run (see isolation.py); None = real HOME


@dataclass
class ParseOutput:
    """What an adapter's parse() returns."""

    events: list[NormalizedEvent] = field(default_factory=list)
    final_text: str = ""
    structured_output: Optional[Any] = None
    cost_usd: Optional[float] = None
    duration_ms: Optional[int] = None


class Adapter(ABC):
    name: str = "base"
    binary: str = ""
    # Where this agent discovers project-local skills, relative to the workspace.
    skills_subdir: str = ".claude/skills"
    # HOME-relative global skills dirs this agent discovers (masked under isolation so a
    # run sees only the skills it provisions). Empty = isolation has nothing to mask.
    global_skills_subpaths: list[str] = []
    # Env vars that redirect this agent's config/home away from $HOME (e.g. CODEX_HOME).
    # Cleared under isolation so the agent can't read the real config — and its skills —
    # and bypass the isolated home.
    isolation_clear_env: list[str] = []

    # --- discovery ----------------------------------------------------------

    def is_available(self) -> bool:
        """True if the agent's CLI is on PATH."""
        return bool(self.binary) and shutil.which(self.binary) is not None

    def resolved_binary(self) -> Optional[str]:
        return shutil.which(self.binary) if self.binary else None

    # --- skill provisioning -------------------------------------------------

    def provision_skills(self, workspace: str, skill_dirs: list[str]) -> list[str]:
        """Copy skill directories into the workspace so this agent discovers them.

        Returns the destination paths created. Keeping skills inside the
        per-run workspace makes runs hermetic and side-effect free. Override
        per adapter if an agent only supports a global skills location.
        """
        installed: list[str] = []
        if not skill_dirs:
            return installed
        dest_root = os.path.join(workspace, self.skills_subdir)
        os.makedirs(dest_root, exist_ok=True)
        for src in skill_dirs:
            if not os.path.isdir(src):
                continue
            src = os.path.abspath(src)
            dest = os.path.join(dest_root, os.path.basename(os.path.normpath(src)))
            if os.path.lexists(dest):
                continue
            # Symlink keeps the workspace small and the skill read-only; fall
            # back to a copy where symlinks aren't available (e.g. Windows).
            try:
                os.symlink(src, dest, target_is_directory=True)
            except (OSError, NotImplementedError):
                shutil.copytree(src, dest, dirs_exist_ok=True)
            installed.append(dest)
        return installed

    # --- prompt construction ------------------------------------------------

    def format_skill(self, skill: str) -> str:
        """How a prompt references a skill in this agent. Default: slash form."""
        return f"/{skill}"

    # --- invocation ---------------------------------------------------------

    @abstractmethod
    def build_argv(self, prompt: str, opts: RunOptions) -> list[str]:
        """Return the full argv (including the binary) to run this prompt."""
        raise NotImplementedError

    def env(self, base_env: dict[str, str], opts: RunOptions) -> dict[str, str]:
        """Mutate/extend the subprocess environment.

        Default: pass through, except when ``opts.home`` is set (isolated run) — then point
        HOME (and Windows' USERPROFILE) at the isolated home, drop XDG overrides so they
        re-derive under it, and clear any config-home overrides (``isolation_clear_env``, e.g.
        CODEX_HOME) that would otherwise let the agent read the real config and bypass the
        isolated home. The isolated home mirrors the real one, so auth/config still work.
        """
        if not opts.home:
            return base_env
        env = dict(base_env)
        env["HOME"] = opts.home
        env["USERPROFILE"] = opts.home
        for k in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
            env.pop(k, None)
        for k in self.isolation_clear_env:
            env.pop(k, None)
        return env

    # --- output normalization ----------------------------------------------

    @abstractmethod
    def parse(self, stdout: str, stderr: str, exit_code: int) -> ParseOutput:
        """Translate raw agent output into the normalized shape."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def iter_jsonl(text: str):
    """Yield parsed JSON objects from JSONL text, skipping blank/non-JSON lines.

    Agents sometimes interleave plain-text warnings on stdout; we skip those
    rather than crash, but still surface them via `yield_other` callers can use.
    """
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue


def try_load_json(text: str) -> Optional[Any]:
    """Best-effort: parse `text` as a single JSON value.

    Falls back to extracting the last balanced {...} or [...] block, which
    handles agents that print a banner before the JSON result.
    """
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    # fall back: find the last top-level JSON object/array
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except (json.JSONDecodeError, ValueError):
                continue
    return None


# Keys commonly used by various agents to carry a shell command inside a tool
# call's argument object. Checked in order.
_COMMAND_KEYS = ("command", "cmd", "script", "shell", "code", "args", "input")
_PATH_KEYS = ("file_path", "path", "filepath", "file", "filename", "target_file")


def extract_command(obj: Any) -> Optional[str]:
    """Pull a shell-command string out of a tool-call argument object."""
    if isinstance(obj, str):
        return obj
    if not isinstance(obj, dict):
        return None
    for k in _COMMAND_KEYS:
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, list) and all(isinstance(x, str) for x in v):
            return " ".join(v)
    return None


def extract_path(obj: Any) -> Optional[str]:
    """Pull a file path out of a tool-call argument object."""
    if not isinstance(obj, dict):
        return None
    for k in _PATH_KEYS:
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return None
