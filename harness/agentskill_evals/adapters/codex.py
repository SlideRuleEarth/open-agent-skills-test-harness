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
import os
import re
import subprocess
import sys
from typing import Any, Optional

from ..schema import EventKind, NormalizedEvent
from .base import Adapter, ParseOutput, ProbeResult, RunOptions, extract_command, extract_path, iter_jsonl, try_load_json, warn_unknown_usage

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover — 3.10 fallback is regex-based
    tomllib = None


_KNOWN_USAGE_KEYS = {"input_tokens", "output_tokens", "reasoning_tokens", "total_tokens"}

# What `codex mcp add` accepts as a server name (verified 0.140.0: "use letters, numbers,
# '-', '_'") — exactly the TOML bare-key charset, so any addable name works in an unquoted
# `-c mcp_servers.<name>...` dotted path.
_BARE_KEY_RE = re.compile(r"[A-Za-z0-9_-]+\Z")

# For the Python 3.10 config-scan fallback (no tomllib): a dotted TOML key path (bare,
# basic-quoted, or literal-quoted segments) and one key of an inline table.
_KEY_PATH_RE = re.compile(
    r'((?:"(?:[^"\\]|\\.)*"|\'[^\']*\'|[A-Za-z0-9_-]+)'
    r'(?:\s*\.\s*(?:"(?:[^"\\]|\\.)*"|\'[^\']*\'|[A-Za-z0-9_-]+))*)\s*=')
_INLINE_ONEKEY_RE = re.compile(r'\s*("(?:[^"\\]|\\.)*"|\'[^\']*\'|[A-Za-z0-9_-]+)\s*=')


class CodexAdapter(Adapter):
    name = "codex"
    binary = "codex"
    skills_subdir = ".agents/skills"  # Codex reads $REPO_ROOT/.agents/skills (cross-agent convention)
    # Global skills dirs codex discovers (the .system vendor bundle in ~/.codex/skills is kept).
    global_skills_subpaths = [".codex/skills", ".agents/skills"]
    # CODEX_HOME overrides ~/.codex (skills under $CODEX_HOME/skills). Under isolation it's
    # mirrored + repointed (custom home kept, skills masked), else cleared to the isolated home.
    isolation_config_homes = [("CODEX_HOME", ".codex", "skills")]
    # No dedicated flag; the config.toml key `model_reasoning_effort` (settable per-run via
    # `-c`) reaches the API as `reasoning.effort` (verified 2026-07-08: the API echoes
    # supported values none|minimal|low|medium|high|xhigh on a bad one).
    supports_reasoning_effort = True
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
                "--disable", "plugins",
                "-c", "memories.use_memories=false",
                "-c", "memories.generate_memories=false",
                *self._mcp_disable_args(),
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

    # --- MCP hermeticity ------------------------------------------------------

    def _mcp_disable_args(self) -> list[str]:
        """`-c mcp_servers.<name>.enabled=false` for every server codex would actually
        load. Verified on 0.140.0: `-c mcp_servers={}` deep-merges with config.toml
        instead of replacing it (the server stays enabled), while the per-server `enabled`
        override does disable it — so hermeticity requires enumerating names. Enumeration
        must match codex's *effective* view exactly: disabling a name codex doesn't load
        creates an incomplete top-level entry and the run dies with "invalid transport"
        (verified). Riding on argv (not the isolation overlay) makes this cover every
        invocation the same way: cells (isolated or not), model probes, and judge runs."""
        args: list[str] = []
        for name in self._configured_mcp_server_names():
            if _BARE_KEY_RE.match(name):
                args += ["-c", f"mcp_servers.{name}.enabled=false"]
            else:
                # Only reachable via a hand-edited config.toml — `codex mcp add` rejects
                # anything outside [A-Za-z0-9_-]. The -c parser can't address quoted key
                # segments, and passing this form makes codex refuse to load its config at
                # all: the run errors out (fail closed) instead of silently executing with
                # the server live.
                print(f"warning: [codex] MCP server name {name!r} can't be disabled via "
                      f"-c (non-bare TOML key); the run will fail closed rather than load "
                      f"it — rename the server in config.toml.", file=sys.stderr)
                args += ["-c", f'mcp_servers."{name}".enabled=false']
        return args

    # (home, config.toml mtime_ns) -> names; adapters are singletons, so this caps the
    # `codex mcp list` spawns at one per config state instead of one per argv build.
    _mcp_names_cache: Optional[tuple] = None

    def _configured_mcp_server_names(self) -> list[str]:
        """Server names codex will load from *configuration* (its plugin channel is closed
        separately by `--disable plugins`, and plugin-provided servers must NOT be disabled
        by name — they have no config.toml entry, so a `-c` disable would create an
        incomplete one and break the run). Primary source: `codex --disable plugins mcp
        list --json`, codex's own effective-config resolution (profiles and future config
        layers included). Fallback when the CLI can't answer: parse the config ourselves
        (base table + the config-selected active profile only)."""
        home = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
        try:
            mtime = os.stat(os.path.join(home, "config.toml")).st_mtime_ns
        except OSError:
            mtime = None
        key = (home, mtime)
        if self._mcp_names_cache and self._mcp_names_cache[0] == key:
            return self._mcp_names_cache[1]
        names = self._mcp_names_via_cli()
        if names is None:
            names = _parse_config_mcp_names(home)
        self._mcp_names_cache = (key, names)
        return names

    def _mcp_names_via_cli(self) -> Optional[list[str]]:
        """Ask codex itself which MCP servers its effective config defines. None (fall
        back to parsing) when the binary is missing or errors out."""
        try:
            r = subprocess.run(
                [self.binary, "--disable", "plugins", "mcp", "list", "--json"],
                capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL,
            )
            if r.returncode != 0:
                return None
            data = json.loads(r.stdout)
            return sorted({str(s["name"]) for s in data
                           if isinstance(s, dict) and s.get("name")})
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError,
                ValueError, KeyError):
            return None

    def build_argv(self, prompt: str, opts: RunOptions, *, cwd: str) -> list[str]:
        argv = [self.binary]
        if opts.auto_approve:
            # Non-interactive parity with the deprecated `--full-auto`: never prompt for
            # approval AND allow workspace writes. `-a/--ask-for-approval` and
            # `-s/--sandbox` are top-level options, so they precede the `exec` subcommand.
            argv += ["--ask-for-approval", "never", "--sandbox", "workspace-write"]
        argv += ["exec", "--ephemeral", "--disable", "memories",
                 # Plugins are their own MCP channel (a plugin can ship .mcp.json servers
                 # that never appear in config.toml); the feature switch removes the whole
                 # channel — plugin-provided skills go with it, documented trade-off.
                 "--disable", "plugins",
                 "-c", "memories.use_memories=false",
                 "-c", "memories.generate_memories=false",
                 # MCP kill-switch (DESIGN_MCP_Support.md, Phase 0): the isolated HOME
                 # symlinks ~/.codex wholesale, so any [mcp_servers.*] in the user's real
                 # config.toml loads in every run — and `-c mcp_servers={}` does NOT clear
                 # it (verified 0.140.0: -c deep-merges with the persisted table). Each
                 # configured server is disabled by name instead.
                 *self._mcp_disable_args(),
                 "--json"]
        if opts.model:
            argv += ["-m", opts.model]
        if opts.reasoning_effort:
            # Quoted so the value part parses as a TOML string (see `-c` help text).
            argv += ["-c", f'model_reasoning_effort="{opts.reasoning_effort}"']
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


def _unquote_toml_key(key: str) -> str:
    """Strip the quotes (and unescape) of a TOML basic/literal-string key; bare keys pass
    through."""
    if len(key) >= 2 and key.startswith('"') and key.endswith('"'):
        return re.sub(r'\\(.)', r'\1', key[1:-1])
    if len(key) >= 2 and key.startswith("'") and key.endswith("'"):
        return key[1:-1]
    return key


def _split_toml_key(path: str) -> list[str]:
    """``'mcp_servers."weird name.v2".env'`` → ``['mcp_servers', 'weird name.v2', 'env']``
    — a dot-splitter that respects quoted segments (a naive ``split('.')`` would shear a
    quoted name containing dots)."""
    parts: list[str] = []
    i, n = 0, len(path)
    while i < n:
        while i < n and path[i] in " \t":
            i += 1
        if i >= n:
            break
        if path[i] in "\"'":
            quote = path[i]
            buf = []
            j = i + 1
            while j < n:
                c = path[j]
                if quote == '"' and c == "\\" and j + 1 < n:
                    buf.append(path[j + 1])
                    j += 2
                    continue
                if c == quote:
                    break
                buf.append(c)
                j += 1
            parts.append("".join(buf))
            i = j + 1
        else:
            j = i
            while j < n and path[j] not in ". \t":
                j += 1
            parts.append(path[i:j])
            i = j
        while i < n and path[i] in " \t":
            i += 1
        if i < n and path[i] == ".":
            i += 1
    return parts


def _parse_config_mcp_names(home: str) -> list[str]:
    """Fallback enumerator when `codex mcp list` can't answer: server names in the
    *top-level* ``mcp_servers`` table of config.toml — and nothing else. Profiles don't
    contribute: 0.140.0 rejects the legacy ``profile = "x"`` config key outright ("no
    longer supported; use --profile"), the harness never passes ``--profile``, and
    ``[profiles.*]`` tables are ignored when inactive (all verified) — while disabling a
    name codex doesn't load breaks the run (see _mcp_disable_args). Best-effort:
    unreadable/unparseable config yields [] (codex itself fails on such a config
    anyway)."""
    try:
        with open(os.path.join(home, "config.toml"), "rb") as f:
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    if tomllib is not None:
        try:
            servers = tomllib.loads(text).get("mcp_servers")
        except tomllib.TOMLDecodeError:
            return []
        return sorted(str(k) for k in servers) if isinstance(servers, dict) else []
    return sorted(_scan_toml_mcp_names(text))  # pragma: no cover — Python 3.10 path,
                                               # forced in selftest by nulling tomllib


def _collect_scanned_path(path: list[str], line: str, names: set) -> None:
    """Given a fully-qualified key path (table header + assignment key), record the
    top-level server name it addresses (``mcp_servers.<name>…``). A path ending AT
    ``mcp_servers`` with an inline-table value contributes that table's keys."""
    if path[:1] != ["mcp_servers"]:
        return
    if len(path) > 1:
        names.add(path[1])
        return
    # `mcp_servers = { alpha = {...}, beta = {...} }` — single-line inline table
    brace = line.find("{")
    if brace != -1:
        names.update(_inline_table_keys(line[brace + 1:]))


def _inline_table_keys(body: str) -> list[str]:
    """Top-level keys of a TOML inline table (text after its opening brace) — depth-tracked
    so keys of nested tables (an ``env = {…}`` inside a server) don't read as server
    names."""
    keys: list[str] = []
    depth, i, n = 1, 0, len(body)
    at_key = True  # positioned where a key may start: table start, or after a depth-1 comma
    while i < n and depth > 0:
        if at_key and depth == 1:
            m = _INLINE_ONEKEY_RE.match(body, i)
            if m:
                keys.append(_unquote_toml_key(m.group(1)))
                i = m.end()
                at_key = False
                continue
        c = body[i]
        if c in "\"'":
            quote = c
            i += 1
            while i < n:
                if quote == '"' and body[i] == "\\":
                    i += 2
                    continue
                if body[i] == quote:
                    break
                i += 1
        elif c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
        elif c == "," and depth == 1:
            at_key = True
        i += 1
    return keys


def _scan_toml_mcp_names(text: str) -> set[str]:
    """Line-based TOML scan for the no-tomllib fallback: tracks the current table header
    and resolves each assignment's dotted key against it, so ``[mcp_servers.a]`` headers,
    top-level ``mcp_servers.a = {…}`` dotted assignments (valid TOML codex loads — a bare
    header regex misses it), and inline tables all land, while the same shapes under a
    ``[profiles.<p>]`` section (not loaded without --profile) don't."""
    names: set[str] = set()
    current: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            header = line.lstrip("[").split("]")[0].strip()
            current = _split_toml_key(header)
            _collect_scanned_path(current, "", names)
            continue
        m = _KEY_PATH_RE.match(line)
        if m:
            _collect_scanned_path(current + _split_toml_key(m.group(1)), line, names)
    return names


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
