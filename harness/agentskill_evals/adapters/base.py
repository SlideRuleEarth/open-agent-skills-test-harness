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
import re
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

from ..isolation import build_mcp_masked_home
from ..schema import NormalizedEvent


@dataclass
class RunOptions:
    """Per-invocation knobs the runner passes to an adapter.

    These are agent-agnostic; each adapter translates the ones it supports into
    native flags and ignores the rest.
    """

    model: Optional[str] = None
    auto_approve: bool = True            # allow file/command execution without prompts
    reasoning_effort: Optional[str] = None  # "low" | "medium" | "high" — thinking budget;
                                            # mapped to a native flag only where the runner
                                            # has one (see supports_reasoning_effort)
    output_schema: Optional[dict] = None  # JSON Schema for the final structured answer
    allowed_tools: Optional[list[str]] = None
    disable_tools: bool = False          # run reasoning-only (used by the judge)
    extra_args: list[str] = field(default_factory=list)  # raw flags appended verbatim
    home: Optional[str] = None           # isolated HOME for this run (see isolation.py); None = real HOME
    isolation_env: dict = field(default_factory=dict)  # config-home vars repointed at isolated mirrors


@dataclass
class ProbeResult:
    """Outcome of probing a single model."""

    accepted: bool
    cost_usd: Optional[float] = None
    premium_requests: Optional[float] = None

    @property
    def cost_str(self) -> str:
        parts = []
        if self.cost_usd is not None:
            parts.append(f"${self.cost_usd:.4f}")
        if self.premium_requests is not None:
            parts.append(f"{self.premium_requests}req")
        return " / ".join(parts) if parts else ""


@dataclass
class ParseOutput:
    """What an adapter's parse() returns."""

    events: list[NormalizedEvent] = field(default_factory=list)
    final_text: str = ""
    structured_output: Optional[Any] = None
    cost_usd: Optional[float] = None
    premium_requests: Optional[float] = None
    duration_ms: Optional[int] = None
    resolved_model: Optional[str] = None


class Adapter(ABC):
    name: str = "base"
    binary: str = ""
    # Where this agent discovers project-local skills, relative to the workspace.
    skills_subdir: str = ".claude/skills"
    # HOME-relative global skills dirs this agent discovers (masked under isolation so a
    # run sees only the skills it provisions). Empty = isolation has nothing to mask.
    global_skills_subpaths: list[str] = []
    # HOME-relative plugin-registry dirs (see isolation.py) whose entries are plugin
    # packages that can each carry a nested skills/ — a second, independent skill-discovery
    # channel some CLIs support (e.g. AntiGravity's `.gemini/config/plugins`).
    global_plugin_registry_subpaths: list[str] = []
    # Env vars that redirect this agent's config/home away from $HOME, as (env var,
    # HOME-relative dir it stands in for, skills-subdir within that home or None) — e.g.
    # ("CODEX_HOME", ".codex", "skills"). Under isolation a *set* one is mirrored into the
    # isolated home (skills + config masks applied, masks re-rooted via the stand-in dir)
    # and repointed, so custom config homes keep their auth/config; if it can't be mirrored
    # it is cleared so it can't read the real skills/config and bypass isolation.
    isolation_config_homes: list[tuple[str, str, Optional[str]]] = []
    # HOME-relative config paths the isolation overlay materializes instead of symlinking,
    # mapped to neutral content ("{}" = declare no MCP servers; None = an empty directory) —
    # the wholesale HOME symlinks otherwise pass the user's real MCP-server config straight
    # into "hermetic" runs (e.g. ~/.copilot/mcp-config.json). Applied by the runner's
    # isolation overlay AND by the mask-only overlay probes/judge runs get
    # (isolation.build_mcp_masked_home). Adapters with a working flag-level kill-switch
    # (claude --strict-mcp-config, codex per-server disables) don't need one.
    isolation_config_masks: dict[str, Optional[str]] = {}
    # File names materialized (with the given content) inside every plugin of every
    # global_plugin_registry_subpaths dir — plugins can carry their own MCP configs
    # (e.g. agy's plugins/<name>/mcp_config.json), a server-discovery channel of their own.
    plugin_registry_config_masks: dict[str, str] = {}

    supports_output_schema: bool = False
    # True if build_argv maps RunOptions.reasoning_effort onto a native flag/config of this
    # CLI. When False the option is silently ignored here — the CLI layer warns the user up
    # front (cmd_run) so a run never half-applies an effort across a comparison matrix.
    supports_reasoning_effort: bool = False

    # --- discovery ----------------------------------------------------------

    def is_available(self) -> bool:
        """True if the agent's CLI is on PATH."""
        return bool(self.binary) and shutil.which(self.binary) is not None

    def resolved_binary(self) -> Optional[str]:
        return shutil.which(self.binary) if self.binary else None

    has_model_list: bool = False

    def discover_models(self) -> Optional[list[str]]:
        """Probe the CLI for its available models.

        Returns a list of model id strings, or None if this adapter cannot
        discover models (the default).  Subclasses override with CLI-specific
        logic and set ``has_model_list = True``.
        """
        return None

    def probe_model(self, model: str, timeout: int = 30) -> ProbeResult:
        """Probe whether the CLI accepts *model*.

        Returns a ProbeResult with acceptance status and cost information
        extracted from the CLI's output. Probes inherit the real environment except for
        MCP hermeticity: an adapter with config masks gets a throwaway mask-only HOME
        overlay (auth/config/skills pass through; MCP configs neutralized), so probing
        models never launches the user's real MCP servers.
        """
        argv = self._probe_argv(model)
        if not argv:
            return ProbeResult(accepted=True)
        env = None
        masked_home, iso_env = None, {}
        try:
            masked_home, iso_env = build_mcp_masked_home(self)
        except OSError as exc:
            import sys
            print(f"warning: [{self.name}] could not build the MCP-masking overlay for a "
                  f"model probe ({exc}); probing with the real HOME.", file=sys.stderr)
        if masked_home:
            env = self.env(dict(os.environ),
                           RunOptions(home=masked_home, isolation_env=iso_env))
        try:
            r = subprocess.run(
                argv, capture_output=True, text=True, env=env,
                timeout=timeout, stdin=subprocess.DEVNULL,
            )
            combined = r.stderr + r.stdout
            lower = combined.lower()
            if "not available" in lower or "invalid model" in lower or "unknown model" in lower:
                return ProbeResult(accepted=False)
            if r.returncode != 0:
                return ProbeResult(accepted=False)
            return self._parse_probe_cost(combined)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return ProbeResult(accepted=False)
        finally:
            if masked_home:
                shutil.rmtree(masked_home, ignore_errors=True)

    def _parse_probe_cost(self, output: str) -> ProbeResult:
        """Extract cost info from probe output. Override per adapter."""
        return ProbeResult(accepted=True)

    def _probe_argv(self, model: str) -> Optional[list[str]]:
        """Return the argv to probe whether *model* is accepted.

        Override per adapter.  Return None to skip probing.
        """
        return None

    # --- skill provisioning -------------------------------------------------

    def provision_skills(self, workspace: str, skill_dirs: list[str]) -> list[str]:
        """Copy skill directories into the workspace so this agent discovers them.

        Returns the destination paths created. A *copy* (not a symlink) keeps a run
        side-effect-free: if the agent writes inside a provisioned skill dir, it mutates the
        throwaway workspace copy, never the original skill source. Override per adapter if an
        agent only supports a global skills location.
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
            shutil.copytree(src, dest, dirs_exist_ok=True)
            installed.append(dest)
        return installed

    # --- prompt construction ------------------------------------------------

    def format_skill(self, skill: str) -> str:
        """How a prompt references a skill in this agent. Default: slash form."""
        return f"/{skill}"

    # --- invocation ---------------------------------------------------------

    @abstractmethod
    def build_argv(self, prompt: str, opts: RunOptions, *, cwd: str) -> list[str]:
        """Return the full argv (including the binary) to run this prompt.

        ``cwd`` is the workspace the subprocess will be launched with (same value passed
        as ``subprocess.run(..., cwd=cwd)``). Most adapters ignore it because their CLI
        already scopes itself to the process's actual working directory; an adapter whose
        CLI resolves its own project root independently of cwd (see AntiGravity) uses it
        to pin the run back to the workspace explicitly.
        """
        raise NotImplementedError

    def env(self, base_env: dict[str, str], opts: RunOptions) -> dict[str, str]:
        """Mutate/extend the subprocess environment.

        Default: pass through, except when ``opts.home`` is set (isolated run) — then point
        HOME (and Windows' USERPROFILE) at the isolated home and drop XDG overrides so they
        re-derive under it. Config-home vars (``isolation_config_homes``, e.g. CODEX_HOME) are
        repointed at their isolated mirror when ``opts.isolation_env`` provides one (so a custom
        config home keeps its auth/config with skills masked), otherwise cleared so they can't
        read the real config — and its skills — and bypass the isolated home. The isolated home
        mirrors the real one, so auth/config still work.
        """
        if not opts.home:
            return base_env
        env = dict(base_env)
        env["HOME"] = opts.home
        env["USERPROFILE"] = opts.home
        for k in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
            env.pop(k, None)
        for var, _replaces, _skills_sub in self.isolation_config_homes:
            if var in (opts.isolation_env or {}):
                env[var] = opts.isolation_env[var]   # repoint at the isolated mirror
            else:
                env.pop(var, None)                   # unmirrored → fall back to the isolated HOME
        return env

    # --- output normalization ----------------------------------------------

    @abstractmethod
    def parse(self, stdout: str, stderr: str, exit_code: int,
               *, opts: Optional[RunOptions] = None) -> ParseOutput:
        """Translate raw agent output into the normalized shape.

        ``opts`` is the same RunOptions the run was built from (``opts.home`` in
        particular) — most adapters get everything they need from stdout/stderr, but one
        whose CLI writes richer structured data to disk (keyed by an id in stdout) rather
        than to the stream itself needs it to locate that side-channel.
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def warn_unknown_usage(agent_name: str, usage: dict, known_keys: set[str]) -> None:
    """Warn on stderr if a usage/result dict contains keys we don't capture."""
    unknown = set(usage) - known_keys
    if unknown:
        import sys
        print(f"warning: {agent_name} reported unknown usage/billing fields: "
              f"{sorted(unknown)} — check if new billing metrics need capturing",
              file=sys.stderr)


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
_COMMAND_KEYS = ("command", "cmd", "script", "shell", "code", "args", "input", "command_line")
_PATH_KEYS = ("file_path", "path", "filepath", "file", "filename", "target_file", "directory_path")


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


_CAMEL_BOUNDARY_1 = re.compile(r"(.)([A-Z][a-z]+)")
_CAMEL_BOUNDARY_2 = re.compile(r"([a-z0-9])([A-Z])")


def _camel_to_snake(name: str) -> str:
    """``TargetFile`` -> ``target_file``, ``URLPath`` -> ``url_path`` (not ``u_r_l_path`` — a
    naive "insert _ before every capital" rule mangles runs of capitals in an acronym; splitting
    only at a lower/acronym-to-new-word boundary keeps the acronym intact as one segment)."""
    s = _CAMEL_BOUNDARY_1.sub(r"\1_\2", name)
    return _CAMEL_BOUNDARY_2.sub(r"\1_\2", s).lower()


def snake_case_keys(obj: Any) -> Any:
    """Rewrite a dict's PascalCase/camelCase keys to snake_case (e.g. ``TargetFile`` ->
    ``target_file``) so tool args from CLIs with a different naming convention still match
    ``extract_command``/``extract_path``'s key lists. Non-dicts pass through unchanged."""
    if not isinstance(obj, dict):
        return obj
    return {_camel_to_snake(k): v for k, v in obj.items()}
