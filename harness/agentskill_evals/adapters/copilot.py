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

Verified against copilot 1.0.64 on 2026-07-17 (installed CLI + its app.js bundle).
"""

from __future__ import annotations

import json
import ntpath
import os
import sys
from typing import Any, Mapping, Optional

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

# Workspace-level MCP config candidates. This is a deliberate CONSERVATIVE SUPERSET
# of what copilot 1.0.64 discovers, not CLI parity: the bundle's candidate list is
# only .mcp.json and .github/mcp.json (`.vscode/mcp.json` was removed in 1.0.64),
# the FIRST existing candidate per directory wins, and the walk runs from the cwd to
# the git root (repo-less runs read the cwd only). The harness checks every
# candidate — the removed .vscode file and legacy "servers" key spelling included —
# in the cwd and EVERY ancestor, because over-enumerating only adds harmless
# disables (--disable-mcp-server tolerates names copilot doesn't load, verified
# 1.0.64) while under-enumerating would leave a server live.
_WORKSPACE_MCP_FILES = (".mcp.json", ".github/mcp.json", ".vscode/mcp.json")

# Built-in / feature-gated in-process MCP servers named in the 1.0.64 bundle.
# --disable-builtin-mcps' own help covers only github-mcp-server, and the
# staff-feature-gated computer-use can be switched on via config
# `enabledMcpServers` — so each name is ALSO disabled explicitly on every argv
# (over-disabling is a no-op for servers that never load).
_BUILTIN_MCP_SERVERS = ("github-mcp-server", "playwright", "bluebird",
                        "computer-use")

# --- Custom agents: an MCP channel --disable-mcp-server does NOT reach ----------------
#
# copilot 1.0.64 discovers custom-agent definitions (markdown with frontmatter) from
# four sources: <config home>/agents, the .github/agents and .claude/agents convention
# dirs walked from the working directory up to the git root, installed plugins, and —
# when the working directory sits in a git repo with a GitHub remote and auth is
# available — a REMOTE org/enterprise listing fetched from the Copilot API. Local files
# are collected by a recursive **/*.md glob (symlinks followed, dot-entries skipped),
# and both local frontmatter (`mcp-servers`) and remote listing entries can declare MCP
# servers (all read out of the 1.0.64 bundle: the custom-agents loader and its remote
# branch). copilot's startServerOnce DOES consult the session disabledMcpServers set,
# but the harness can't populate it for this channel: the agent-declared server NAMES
# live inside agent frontmatter and — worse — inside REMOTE org/enterprise listings the
# harness cannot read at all, so it has nothing reliable to add to --disable-mcp-server
# (parsing local frontmatter would still miss every remote entry). Disabling by name
# therefore cannot cover this channel — and no flag disables custom agents
# (--no-custom-instructions does not; verified against 1.0.64 --help). What does exist:
# the documented config setting `customAgents.defaultLocalOnly` (see `copilot help
# config`) short-circuits the loader BEFORE the remote listing. Consequently the
# harness neutralizes on disk what it can (isolation masks <home>/agents to an empty
# dir and forces defaultLocalOnly into the sanitized config.json) and FAILS CLOSED in
# _mcp_disable_args on what it can't: any discoverable local agent file, or a git-repo
# cwd whose enumerable config does not provably opt out of remote discovery.
_WORKSPACE_AGENT_DIRS = (".github/agents", ".claude/agents")


def _agent_definition_files(root: str) -> list[str]:
    """Custom-agent definition files under one agents dir: every *.md at any depth.
    copilot's loader globs **/*.md under each source dir (symlinks followed, dot
    entries skipped, case-sensitive); this walk is a SUPERSET — dot entries and
    case-insensitive ``.md`` included — since over-detection only fail-closes more,
    never less. An absent root contributes nothing (copilot's glob catch treats it
    as empty); an unreadable one raises — absence can't be proven there."""
    out: list[str] = []
    seen: set[str] = set()
    stack = [root]
    while stack:
        d = stack.pop()
        real = os.path.realpath(d)
        if real in seen:            # symlink cycles terminate (copilot's glob
            continue                # dedupes realpaths the same way)
        seen.add(real)
        try:
            entries = list(os.scandir(d))
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as exc:
            raise RuntimeError(
                f"custom-agent dir {d!r} exists but can't be read ({exc}) — whether "
                "copilot would discover agent files there can't be verified; "
                "failing closed."
            )
        for ent in entries:
            try:
                is_dir = ent.is_dir()
            except OSError:
                is_dir = False
            if is_dir:
                stack.append(ent.path)
            elif ent.name.lower().endswith(".md"):
                out.append(ent.path)
    return sorted(out)


# Env vars that redirect git's own repository discovery. copilot finds its repo by
# running `git rev-parse --show-toplevel` with the CHILD's environment, so any of
# these being set means the repo (and its GitHub remote) can live somewhere no
# .git-entry walk from the cwd would find.
_GIT_ENV_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR")


def _git_env_repo(env_map: Mapping[str, str]) -> bool:
    """True when the child env redirects git discovery (see _GIT_ENV_VARS) — the
    harness then treats a repo as POSSIBLE with an unknowable root."""
    return any(env_map.get(v) for v in _GIT_ENV_VARS)


def _is_git_root(d: str) -> bool:
    """True when git would treat ``d`` as a repository root — the semantics copilot's
    ``git rev-parse`` uses, not a bare "a ``.git`` name exists" test:

    * ``d/.git`` is a FILE → a gitlink/worktree pointer (``gitdir: ...``); git treats
      ``d`` as a work tree. Counted (even a bogus pointer is git's to reject, and the
      fail-closed direction is safe).
    * ``d/.git`` is a DIRECTORY → a real repo only if it has a ``HEAD`` (git always
      writes one). An EMPTY or partial ``.git/`` is NOT a repository — git ignores it
      and keeps walking UP to the real parent repo — so counting it would stop the
      walk early and MISS what copilot reads above it. HEAD-gated to match git.
    """
    dotgit = os.path.join(d, ".git")
    if os.path.isfile(dotgit):
        return True
    return os.path.isdir(dotgit) and os.path.isfile(os.path.join(dotgit, "HEAD"))


def _child_os_home(env_map: Mapping[str, str]) -> Optional[str]:
    """The child's OS home directory, realpath-resolved, or None when unknowable —
    the boundary copilot's convention-dir walk stops at for a REPO-LESS run (Node's
    ``os.homedir()``: ``$HOME`` on POSIX, ``%USERPROFILE%`` on win32; an empty value
    falls back to the account home, i.e. ``expanduser('~')``). None → the walk runs to
    the filesystem root (over-approximation, the safe direction)."""
    key = "USERPROFILE" if sys.platform == "win32" else "HOME"
    h = env_map.get(key) or os.path.expanduser("~")
    if not h or h == "~":
        return None
    try:
        return os.path.realpath(h)
    except OSError:
        return None


def _nearest_git_root(cwd: str) -> Optional[str]:
    """The first ancestor of the PHYSICAL ``cwd`` (inclusive) that git would treat as a
    repository root (see ``_is_git_root``), or None. copilot resolves its repo with
    ``git rev-parse --show-toplevel`` in the child context, which operates on the
    symlink-resolved path and applies git's own root semantics — so this walks
    ``os.path.realpath(cwd)`` (a cwd symlinked into a repo subtree finds the physical
    repo's ``.git``, which an ``abspath`` lexical walk would miss) and HEAD-gates ``.git``
    dirs (an empty nested ``.git/`` is not a boundary; git walks past it). The residual
    approximation is safe in the fail-closed direction, and the inverse — a repo git
    finds via ``GIT_DIR`` & co that no ``.git`` entry reveals — is covered by
    ``_git_env_repo``."""
    d = os.path.realpath(cwd)
    while True:
        if _is_git_root(d):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def _custom_agent_files(home: str, cwd: Optional[str],
                        env_map: Mapping[str, str]) -> list[str]:
    """Every LOCAL custom-agent definition file discoverable for a run:
    ``<home>/agents`` plus the ``.github/agents`` / ``.claude/agents`` convention
    dirs of the PHYSICAL run cwd and its ancestors up to copilot's own walk boundary —
    the nearest git root (inclusive), or, for a repo-less run, the child's OS home
    (inclusive). The cwd is symlink-resolved (``os.path.realpath``) so a cwd symlinked
    into a repo subtree finds the agent dirs git/copilot would; the git boundary is
    HEAD-gated (``_is_git_root`` — an empty nested ``.git/`` is NOT a boundary, git
    walks past it). Only when the child env redirects git discovery
    (``_git_env_repo`` — GIT_DIR & co) is the real boundary unknowable; the walk then
    runs to the filesystem root (conservative). Unlike the workspace MCP-config walk —
    where an over-approximation just adds harmless disables — agent detection FAILS
    runs closed, so bounding at copilot's boundary (git root / home) instead of always
    over-walking to the filesystem root keeps a ``.claude/agents`` ABOVE the repo or
    the home — which copilot never reads — from killing otherwise-valid runs."""
    boundary_unknown = _git_env_repo(env_map)
    child_home = None if boundary_unknown else _child_os_home(env_map)
    dirs = [os.path.join(home, "agents")]
    if cwd:
        d = os.path.realpath(cwd)
        while True:
            for rel in _WORKSPACE_AGENT_DIRS:
                dirs.append(os.path.join(d, *rel.split("/")))
            if not boundary_unknown and (_is_git_root(d) or d == child_home):
                break
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
    files: list[str] = []
    for dpath in dirs:
        files.extend(_agent_definition_files(dpath))
    return files


def _jsonc_strip(text: str) -> str:
    """Reduce copilot's accepted JSONC to strict JSON: replace ``//`` line comments
    (full-line AND inline) and ``/* */`` block comments with a SINGLE SPACE, and drop
    trailing commas before a closing ``}``/``]`` — all string-aware, so ``//`` or
    ``/*`` or ``,]`` INSIDE a string value stays data. A comment becomes whitespace,
    not nothing: it is a token separator, so ``tr/*x*/ue`` must NOT collapse into
    ``true`` (copilot's parser reduces that malformed value to ``{}`` — reading it as
    ``true`` here would let a comment fabricate an opt-out). An UNTERMINATED block
    comment (no closing ``*/`` before EOF) raises ValueError, exactly as copilot's own
    parser errors on it. This is the grammar copilot 1.0.64 actually accepts,
    live-verified via ``copilot mcp list`` against fixture configs: all three comment
    styles and trailing commas parse; JSON5 forms (unquoted keys, single quotes) do
    NOT — copilot errors out on them, so this transform deliberately leaves them to
    fail json.loads."""
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] not in "\r\n":
                i += 1
            out.append(" ")          # comment → token-separating whitespace
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            if i + 1 >= n:           # no closing */ before EOF
                raise ValueError("unterminated block comment in JSONC")
            i += 2
            out.append(" ")          # comment → token-separating whitespace
            continue
        if c == ",":
            # trailing comma iff only whitespace/comments sit between it and }/]
            j = i + 1
            while j < n:
                cj = text[j]
                if cj in " \t\r\n":
                    j += 1
                elif cj == "/" and j + 1 < n and text[j + 1] == "/":
                    while j < n and text[j] not in "\r\n":
                        j += 1
                elif cj == "/" and j + 1 < n and text[j + 1] == "*":
                    j += 2
                    while j + 1 < n and not (text[j] == "*" and text[j + 1] == "/"):
                        j += 1
                    j += 2
                else:
                    break
            if j < n and text[j] in "}]":
                i += 1              # drop the comma; the gap is consumed next passes
                continue
        out.append(c)
        i += 1
    return "".join(out)


def _jsonc_loads(text: str) -> Any:
    """json.loads over copilot-compatible JSONC (see _jsonc_strip). A single leading
    UTF-8 BOM is consumed first — copilot accepts a BOM-prefixed config while
    ``json.loads`` rejects one, so a valid BOM-prefixed mcp-config.json / config.json
    must not fail closed (or, for config.json, discard auth). Raises ValueError
    exactly where copilot's own parser errors out."""
    if text[:1] == chr(0xFEFF):     # one leading UTF-8 BOM
        text = text[1:]
    return json.loads(_jsonc_strip(text))


def _load_copilot_config(path: str) -> Optional[Any]:
    """``config.json`` parsed with copilot's JSONC tolerance — line/inline/block
    comments and trailing commas (the same live-verified grammar the user
    mcp-config.json gets, see _jsonc_strip) — or None when unreadable/unparseable
    (copilot's config loader treats an unparseable file as empty)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return None
    try:
        return _jsonc_loads(raw)
    except ValueError:
        return None


def _remote_agents_opted_out(home: str) -> bool:
    """True iff the config copilot will read provably disables remote custom-agent
    discovery: ``customAgents.defaultLocalOnly`` (documented in ``copilot help
    config``), the ONLY off-switch — the 1.0.64 loader short-circuits before its
    org/enterprise listing when it is set; no flag or env var exists. The isolation
    sanitizer injects it, so masked-home runs always pass this test; a missing or
    unreadable config proves nothing and reads as not-opted-out."""
    data = _load_copilot_config(os.path.join(home, "config.json"))
    agents_cfg = data.get("customAgents") if isinstance(data, dict) else None
    return isinstance(agents_cfg, dict) and agents_cfg.get("defaultLocalOnly") is True


# extra_args tokens that reopen MCP/configuration channels after the disable set is
# computed (all verified against the installed 1.0.64: --help lists the first three,
# --config-dir and --prefer-version are in the bundle but hidden from help):
# --additional-mcp-config merges MORE servers into the session past the disable set;
# --agent selects a custom agent whose frontmatter mcp-servers start OUTSIDE that set
# (see the custom-agents comment above); --plugin-dir loads a plugin — whose definition
# can declare mcpServers — from an arbitrary dir; --config-dir repoints the whole config
# home away from the one this adapter enumerated; --prefer-version can select a
# DIFFERENT cached CLI version, past which the 1.0.64-specific safety assumptions this
# adapter computed no longer hold; -C changes copilot's working directory BEFORE any
# discovery, invalidating the cwd the workspace/agent walks used. Long forms match exact
# or --flag=value; -C matches its own token AND any combined short-option cluster
# carrying it (see _config_channel_token).
_CONFIG_CHANNEL_LONG = ("--additional-mcp-config", "--agent", "--plugin-dir",
                        "--config-dir", "--prefer-version")


def _config_channel_token(extra_args: list[str]) -> Optional[str]:
    """The first extra_args token that opens a copilot configuration channel, or None.
    A token that merely LOOKS like one (e.g. a value following some unrelated flag) is
    reported too — that false positive fails closed, the safe direction.

    The cwd flag ``-C`` is matched inside combined SHORT-option clusters, not only as a
    leading ``-C``: copilot 1.0.64 accepts ``-sC/tmp`` (== ``-s -C /tmp``), where ``C``
    consumes the rest as the new cwd, so a rule keyed on a leading ``-C`` would miss it
    and let copilot change directory AFTER the workspace/agent walks ran. Any
    single-dash token (not ``--`` long-opt, not a bare ``-``) containing ``C`` is
    rejected; a stray ``C`` that is really a value character over-matches into a
    fail-closed, the safe direction."""
    for tok in extra_args:
        if any(tok == f or tok.startswith(f + "=") for f in _CONFIG_CHANNEL_LONG):
            return tok
        if (tok.startswith("-") and not tok.startswith("--")
                and len(tok) > 1 and "C" in tok[1:]):
            return tok
    return None

# --- Windows ODR (On-Device Registry) MCP discovery -----------------------------------
#
# On win32, copilot additionally discovers MCP servers through the Windows MCP registry:
# it reads the string value "Command" under HKLM\SOFTWARE\Microsoft\Windows\
# CurrentVersion\Mcp, executes `<that command> mcp list` itself, parses the JSON
# `servers` array from its stdout, and registers each named server (1.0.64 bundle, the
# "ODR load"/"ODR convert" pipeline); no flag, env var, or setting turns the mechanism
# off. The harness therefore treats a POPULATED gate as un-hermetic and FAILS CLOSED:
# earlier revisions executed the registry command too and pre-disabled the resolved
# names, but copilot runs the command a SECOND, independent time — a stateful or
# time-varying command can hand the two processes different listings, leaving a server
# enabled that the harness never saw, and no cwd/env/bitness matching closes that gap.
# What remains is gate DETECTION: the value is read with KEY_QUERY_VALUE|
# KEY_WOW64_64KEY — the query-only access level copilot's RegGetValueW uses (NOT the
# broader KEY_READ, which a query-only ACL would deny, making the harness reject a host
# copilot reads fine) plus the 64-bit view flag matching copilot's read (a 32-bit
# default would read WOW6432Node, where an absent key would fake the gate "off") — and
# blank-vs-populated is judged with the same ECMAScript trim()+falsy test copilot
# applies. A 32-bit harness process still fails closed outright in _mcp_disable_args
# (WOW64 file-system redirection desyncs the config FILES it reads from 64-bit
# copilot's view).
_ODR_REGISTRY_SUBKEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Mcp"

# ECMAScript whitespace — the exact set /\s/ tests AND String.prototype.trim() strips
# (identical by spec: WhiteSpace ∪ LineTerminator; verified identical by exhaustive
# code-point sweep under node v24, the runtime copilot ships on). It differs from
# Python's str whitespace in both directions: U+001C–U+001F and U+0085 are
# Python-space but NOT JS-space, while U+FEFF is JS-space but NOT Python-space. The
# bundle judges the registry value with `value?.trim()` then a falsy test, so the
# gate-on/gate-off decision must use this set: with str.strip(), a value of only
# U+FEFF would read gate-ON here while copilot reads it blank (gate off), and one
# of only U+001C the reverse.
_JS_WS = ("\t\n\x0b\x0c\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006"
          "\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff")


def _js_trim(s: str) -> str:
    """String.prototype.trim() — strips exactly the ECMAScript whitespace set from
    both ends (str.strip(chars) with the explicit set; NOT str.strip(), whose
    Python set diverges — see _JS_WS)."""
    return s.strip(_JS_WS)


def _odr_registry_command() -> Optional[str]:
    """The ODR command line from the registry, or None when the gate is off (non-win32,
    key/value absent). An unreadable key with the gate possibly on raises RuntimeError.

    The key is opened with ``KEY_QUERY_VALUE | KEY_WOW64_64KEY`` — the QUERY-ONLY
    access level copilot's native helper uses (it calls ``RegGetValueW``, which opens
    with ``KEY_QUERY_VALUE``), NOT the broader ``KEY_READ`` (which also demands
    ENUMERATE_SUB_KEYS/NOTIFY/READ_CONTROL): a host whose ACL grants copilot query-only
    access would make a ``KEY_READ`` open fail and the harness reject the host where
    copilot reads the gate fine. The ``KEY_WOW64_64KEY`` view flag matches copilot's
    64-bit-view read so this reads the 64-BIT registry view no matter the harness's own
    bitness — a 32-bit process's DEFAULT view is the redirected WOW6432Node one, where
    the key can be absent (the gate would read "off") while the 64-bit view copilot
    queries is populated; 32-bit Windows, with its single view, ignores the flag.
    (copilot's ``RegGetValueW`` also pins ``REG_SZ``/``REG_EXPAND_SZ`` with
    no-expansion; ``QueryValueEx`` here does not auto-expand ``REG_EXPAND_SZ`` either,
    so the raw value matches.) Exercised cross-host in the selftest via a stubbed
    ``winreg``."""
    if sys.platform != "win32":
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _ODR_REGISTRY_SUBKEY, 0,
                            winreg.KEY_QUERY_VALUE | winreg.KEY_WOW64_64KEY) as key:
            value, _type = winreg.QueryValueEx(key, "Command")
    except FileNotFoundError:
        return None  # no key/value → copilot skips ODR entirely
    except OSError as exc:
        raise RuntimeError(
            f"cannot read the Windows MCP registry key (HKLM\\{_ODR_REGISTRY_SUBKEY}): "
            f"{exc} — its servers can't be enumerated, failing closed."
        )
    # JS-trim, not str.strip(): the bundle does `value?.trim()` then a falsy test, so
    # a value of only U+FEFF reads as gate-OFF exactly as it does for copilot, while
    # one of only U+001C (Python-blank, JS-nonblank) keeps the gate ON — this returns
    # that command and _assert_odr_gate_off then fails the run closed, the conservative
    # side of the same no-servers outcome (copilot itself would fail to launch it).
    if not isinstance(value, str) or not _js_trim(value):
        return None  # empty command → copilot logs "command line not found" and skips
    return _js_trim(value)


def _mcp_server_names(path: str) -> list[str]:
    """Server names declared in one WORKSPACE MCP config — ``{"mcpServers": {...}}``
    or the legacy ``{"servers": {...}}`` spelling. STRICT JSON on purpose:
    workspace files do not get the user file's JSONC (comment/trailing-comma)
    tolerance (live-verified 1.0.64 — a trailing comma makes ``copilot mcp list``
    silently ignore the workspace file), and an unreadable/invalid file → [] is exact
    parity with copilot's own warn-and-treat-as-empty workspace loader. Read with
    ``utf-8-sig`` so a leading BOM is consumed rather than failing the parse — a
    BOM-prefixed workspace file left unparsed would MISS its servers (over-enumeration
    here is harmless, under-enumeration leaks)."""
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
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


def _user_mcp_server_names(path: str) -> list[str]:
    """Server names from the USER mcp-config.json (``$COPILOT_HOME`` else
    ``~/.copilot``). copilot 1.0.64 parses THIS file as JSONC — line, inline, and
    block comments plus trailing commas, all live-verified via ``copilot mcp
    list`` (three JSONC-declared fixture servers listed) — so the strict parser
    the workspace files get would silently MISS servers a JSONC-only user config
    declares. A file that exists but doesn't parse (or can't be read) fails
    closed: live-verified, copilot itself errors out on such a file ('Failed to
    read configuration ... mcpServers: Required' for garbage and for the JSON5
    forms its parser rejects), and enumerating nothing where copilot would load
    something is exactly the hole Phase 0 forbids. Only an ABSENT file is no
    servers."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise RuntimeError(
            f"user MCP config {path!r} exists but can't be read ({exc}) — its "
            "servers can't be enumerated; failing closed."
        )
    try:
        data = _jsonc_loads(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"user MCP config {path!r} exists but does not parse as the JSONC "
            f"copilot accepts ({exc}) — its servers can't be enumerated (copilot "
            "1.0.64 itself errors out on such a file); failing closed."
        )
    names: list[str] = []
    if isinstance(data, dict):
        block = data.get("mcpServers")
        if isinstance(block, dict):
            names.extend(str(k) for k in block)
    return names


def _win_fully_qualified(path: str) -> bool:
    """Windows semantics for "needs no current-directory OR current-drive context to
    resolve" — i.e. what the OS treats as an absolute path — AND, for the device-
    namespace forms, "the child path ``ntpath.join`` builds OPENS the same file
    copilot's path resolver opens". (1.0.64 resolves paths in NATIVE code, no longer
    Node's ``path.win32`` itself, but with the same join/normalization semantics —
    "Node's join"/"Node inserts" here and below are shorthand for that shared
    behavior.) Under an exact ``\\\\?\\`` prefix (whose open SKIPS Win32
    normalization) that requires byte-identical join strings; under ``\\\\.\\`` (always
    normalized on open, in both processes) the joins need only reconverge at open — the
    accepted ``\\\\.\\C:\\a\\..\\b`` keeps its ``..`` in the harness's join where the
    resolver collapses it, and Windows normalization makes both name the same file. Uses
    ``ntpath`` explicitly so the predicate is correct (and unit-testable) on any host,
    not just win32:

    * a lettered drive WITH a root — ``C:\\x`` — is fully qualified;
    * a COMPLETE UNC share — ``\\\\server\\share`` and anything below it — is fully
      qualified (``ntpath.splitdrive`` returns the whole ``\\\\server\\share`` as the
      drive even with an empty tail, yet Python 3.10's ``ntpath.isabs`` wrongly reports
      such a bare share ROOT as relative, fixed in 3.11 — hence this predicate rather
      than ``isabs``). An INCOMPLETE one — separator-only ``\\\\`` or server-only
      ``\\\\server`` / ``\\\\server\\`` — is NOT: no UNC volume exists without BOTH
      server and share, Node joins a bare ``\\\\`` into ``\\mcp-config.json`` (resolved
      from the CHILD's current-drive root — e.g. ``D:\\mcp-config.json``, a real file
      copilot loads) while the harness's join makes a three-separator or
      incomplete-UNC string that names nothing, and 3.10/3.11 join ``\\\\server\\``
      with a DOUBLED separator. A terminal-COLON share root (``\\\\srv\\share:``) is
      also rejected: splitdrive returns it whole as a drive ending in ``:``, so
      ``ntpath.join`` GLUES the child name on (``\\\\srv\\share:mcp-config.json``, all
      supported Pythons) where Node inserts the separator. Fail closed on all of
      these, on every version;
    * a DEVICE-NAMESPACE path — ``\\\\?\\...`` (question) / ``\\\\.\\...`` (dot). Windows
      skips path normalization ONLY after an EXACT literal ``\\\\?\\`` prefix, so what
      qualifies differs by namespace:

        - under ``\\\\?\\`` only an EXACT literal, already-CANONICAL prefix qualifies — a
          rooted drive form (``\\\\?\\C:\\x``), a volume-GUID root
          (``\\\\?\\Volume{...}``), or a complete extended-UNC share root
          (``\\\\?\\UNC\\server\\share``), each optionally with ONE trailing separator.
          copilot's path resolver canonicalizes the home (folding ``/``→``\\``, collapsing
          ``.``/``..``/duplicate separators) and folds a forward-slash ``//?/`` spelling to
          a literal ``\\\\?\\`` one — whose open then SKIPS normalization — while the
          harness keeps its spelling, which Windows DOES normalize (trailing periods
          trimmed, ``..`` collapsed); the two would then read different files. So a
          noncanonical literal ``\\\\?\\`` (a ``/``, or a ``.``/``..``/internal-or-repeated-
          empty segment) and ANY nonliteral ``//?/`` spelling fail closed;
        - under ``\\\\.\\`` Windows normalizes on open in BOTH processes (it does not
          skip), so a noncanonical or forward-slash spelling reconverges — no canonicality
          is required.

      An UNROOTED drive-LETTER form — bare ``\\\\?\\C:`` / ``\\\\.\\C:`` or drive-relative
      ``\\\\?\\C:.copilot`` — is rejected in BOTH namespaces: ``ntpath.join`` drops the
      separator after the ``:`` (``ntpath.join(r"\\\\?\\C:", "f")`` → ``\\\\?\\C:f``, no
      such path — Node joins ``\\\\?\\C:\\f``) or glues on a tail copilot resolves against
      ``C:``'s own current directory, naming a config copilot never reads.

      A BARE namespace root — ``\\\\?\\`` / ``\\\\.\\`` alone — or an INCOMPLETE
      extended-UNC root — ``\\\\?\\UNC`` or ``\\\\?\\UNC\\server``, with or without a
      trailing separator — is likewise rejected. On Python 3.10/3.11 that is a PROVEN
      join divergence: they join the bare roots with a DOUBLED separator
      (``\\\\?\\\\mcp-config.json``) and 3.11 does the same under the
      trailing-separator UNC forms (the separator lands inside splitdrive's drive and
      join appends another), while Node produces the single-separator spelling — which
      under an exact ``\\\\?\\`` names a local DOS-device alias copilot could open but
      the harness never enumerates. On the versions where the joins coincide (3.12+),
      the rejection is CONSERVATIVE — one uniform version-stable rule instead of a
      per-version accept, at no cost, since a home at a bare device-namespace or
      share-less extended-UNC root is no real profile directory.

      A TERMINAL-COLON body end is rejected in BOTH namespaces — one test, two cases.
      A whole-drive colon root (``\\\\?\\foo:`` / ``\\\\.\\foo:`` on every version;
      ``\\\\?\\UNC\\srv\\share:`` on 3.11+) is a PROVEN divergence: splitdrive
      returns it whole as a drive ending in ``:``, so ``ntpath.join`` GLUES the child
      name on (``\\\\?\\foo:mcp-config.json``) where Node inserts the separator
      (``\\\\?\\foo:\\mcp-config.json``) — and no normalization ever reconverges a
      colon glue, so even the ``\\\\.\\`` namespace diverges. A colon-terminal body
      PAST a rooted drive (``\\\\?\\C:\\dir:`` — splitdrive: drive ``\\\\?\\C:``,
      rooted tail, every version) joins with an inserted separator exactly like Node
      (verified 3.10–3.14) and is rejected CONSERVATIVELY: a terminal-colon component
      is NTFS stream syntax, never a directory, so the uniform test loses nothing.
      (A colon segment with a ROOTED tail below it — ``\\\\?\\foo:\\x`` — joins
      identically to Node on every version and stays acceptable.)
    * a driveless path — rooted ``\\x`` or plain ``x`` — is NOT (it needs the current
      drive), and a drive-RELATIVE one — bare ``C:`` or ``C:x`` — is NOT (it resolves
      against that drive's own current directory).
    """
    drive, tail = ntpath.splitdrive(path)
    if not drive:
        return False                       # driveless: \x or x — needs the current drive
    if drive[0] in "\\/":
        d = drive.replace("/", "\\")
        if d[:4] in ("\\\\?\\", "\\\\.\\"):
            # Post-prefix content with every separator folded to "\": the slice is stable
            # regardless of where splitdrive draws the drive/tail line (3.10 vs 3.11+
            # split extended-UNC and device roots differently).
            body = path.replace("/", "\\")[4:]
            # A BARE namespace root (\\?\ or \\.\ alone): 3.10/3.11 joins DOUBLE the
            # separator (\\?\\mcp-config.json) where Node's single-separator join names
            # a local DOS-device alias the harness never enumerates — PROVEN divergence
            # there; on 3.12+ the joins coincide and the rejection is CONSERVATIVE (one
            # uniform version-stable rule — no real profile lives at a namespace root).
            if not body:
                return False
            # UNROOTED drive-LETTER form (bare \\?\C: / \\.\C: or drive-relative
            # \\?\C:.copilot): ntpath.join drops the separator after the ":"
            # (\\?\C:<name>) or glues on a tail copilot resolves against C:'s own current
            # directory — never a path copilot reads. Rejected for BOTH namespaces (a
            # join artifact, not a normalization one).
            if (len(body) >= 2 and body[0].isalpha() and body[1] == ":"
                    and (len(body) == 2 or body[2] != "\\")):
                return False
            # A TERMINAL-COLON body end — one test, two cases. A whole-drive colon
            # root (\\?\foo:, \\.\foo: on every version; \\?\UNC\srv\share: on 3.11+)
            # is a PROVEN divergence: splitdrive returns it whole as a drive ending in
            # ":", so ntpath.join GLUES the child name on (\\?\foo:mcp-config.json)
            # where Node inserts the separator, and no normalization reconverges a
            # colon glue — BOTH namespaces diverge. A colon-terminal body past a
            # rooted drive (\\?\C:\dir:, rooted-tail splitdrive on every version)
            # joins exactly like Node and is rejected CONSERVATIVELY — a terminal-
            # colon component is NTFS stream syntax, never a directory, so the
            # uniform test loses nothing. (A colon segment with a ROOTED tail below
            # it, e.g. \\?\foo:\x, joins identically to Node and passes.)
            if body[-1] == ":":
                return False
            # Normalization divergence is a \\?\-ONLY hazard: Windows skips normalization
            # only after an EXACT literal \\?\ prefix. copilot's path resolver folds a
            # forward-slash //?/ prefix to a literal \\?\ one (whose open then skips
            # normalization) and canonicalizes the string, while the harness keeps its
            # spelling — so under \\?\ a nonliteral //?/ prefix, OR a noncanonical literal
            # one (a "/", or a "."/".."/internal-or-repeated-empty segment; one trailing
            # separator is fine), makes the two read different files. \\.\ never skips
            # normalization, so Windows normalizes it identically in both and it
            # reconverges — no canonicality required there.
            if d[:4] == "\\\\?\\":
                if path[:4] != "\\\\?\\" or "/" in path:
                    return False
                segs = body.split("\\")
                if segs[-1] == "":          # body is non-empty, so segs never is
                    segs = segs[:-1]        # one trailing separator is join-stable
                if any(s in ("", ".", "..") for s in segs):
                    return False
                # An INCOMPLETE extended-UNC root (\\?\UNC or \\?\UNC\srv, trailing
                # separator or not): 3.11 keeps that separator inside splitdrive's
                # drive and join DOUBLES it (\\?\UNC\srv\\mcp-config.json — Node:
                # \\?\UNC\srv\mcp-config.json), and no share — so no config — exists
                # above server+share anyway. Fail closed on every version.
                if segs[0].upper() == "UNC" and len(segs) < 3:
                    return False
            return True
        # Ordinary UNC: only a COMPLETE \\server\share root (and below) qualifies.
        # 3.11+ splitdrive returns separator-only (\\) and server-only (\\server)
        # forms as a "drive" too, and 3.10 does the same for \\server\ — but no UNC
        # volume exists without BOTH server and share: Node joins a bare \\ into
        # \mcp-config.json, resolved from the CHILD's current-drive root (a real
        # file copilot loads) while the harness's three-separator/incomplete-UNC
        # join names nothing, and \\server\ joins with a DOUBLED separator on
        # 3.10/3.11. Fail closed below server+share.
        if len([p for p in d[2:].split("\\") if p]) < 2:
            return False
        # A terminal-COLON share root (\\srv\share:) GLUES in ntpath.join — the
        # whole root is a drive ending in ":", so no separator is inserted
        # (\\srv\share:mcp-config.json, all supported Pythons) where Node inserts
        # one. Any non-empty tail joins like Node everywhere and passes — a colon
        # root with a rooted tail (\\srv\share:\sub) and a tail merely ENDING in
        # ":" (\\srv\share\dir: — inserted separator, verified 3.10–3.14) alike.
        return bool(tail) or not d.endswith(":")
    return tail[:1] in ("\\", "/")          # lettered X: — qualified only when rooted


def _copilot_home(env_map: Mapping[str, str], cwd: Optional[str] = None) -> str:
    """The directory copilot reads its user config (``mcp-config.json``, ``config.json``)
    from: ``$COPILOT_HOME`` when non-empty (an EMPTY value is unset to copilot too —
    verified 1.0.64: ``COPILOT_HOME=""`` falls back to ``$HOME/.copilot``), else
    ``<home>/.copilot`` where ``<home>`` is copilot's Node ``os.homedir()``.

    ``os.homedir()`` is ``$HOME`` on POSIX and ``%USERPROFILE%`` on Windows — a stray
    ``HOME`` on win32 must not redirect it. The two platforms treat a missing/empty base
    differently, mirrored here from Node/libuv:

    * POSIX: a PRESENT-BUT-EMPTY ``$HOME`` is preserved — Node's ``homedir()`` returns
      ``""`` for it, making the ``.copilot`` join relative (verified 1.0.64: ``copilot mcp
      list`` with ``HOME=""`` reads ``<its cwd>/.copilot``); ``os.path.expanduser`` is the
      fallback only when ``$HOME`` is absent.
    * win32: an ABSENT ``%USERPROFILE%`` makes libuv's ``uv_os_homedir`` fall back to
      ``GetUserProfileDirectoryW`` — the real profile directory, resolved from the process
      token, not this env. A PRESENT but empty/<3-char value is different: ``uv_os_homedir``
      returns ``UV_ENOENT`` as an ERROR (no fallback), which Node's ``os.homedir()``
      surfaces as a thrown system error. Neither is ever a cwd-relative path (Windows has
      no POSIX empty-``HOME`` behaviour), and neither names a config this harness could
      enumerate — so both fail closed. The RAW value is also vetted BEFORE the
      ``.copilot`` join: one that ``ntpath.splitdrive`` returns whole as a drive ending
      in ``:`` (``\\\\?\\foo:``, ``\\\\srv\\share:`` — and ``\\\\?\\UNC\\srv\\share:``
      on the versions whose splitdrive absorbs it whole) makes ``ntpath.join`` GLUE the
      name on (``\\\\?\\foo:.copilot``) where Node inserts the separator, and the glued
      string would then pass the fully-qualified predicate — fail closed instead.

    Resolution of a non-fully-qualified home: a RELATIVE home resolves against copilot's
    own process cwd (verified 1.0.64: ``COPILOT_HOME=relhome`` reads ``<its cwd>/relhome``),
    so it is anchored to the CHILD's ``cwd`` here (``cwd=None`` = the child inherits the
    harness's cwd, where the two coincide). On win32 a ROOTED-BUT-DRIVELESS home
    (``\\config``) is anchored the same way — the join takes the child cwd's drive (ntpath
    keeps the root, supplies the drive), where copilot resolves it. But a DRIVE-RELATIVE
    home (bare ``C:`` or ``C:x``) is rejected outright: it resolves against that drive's
    per-drive current directory, which nobody here can know and which copilot does NOT
    equate to the child cwd (verified: ``COPILOT_HOME="D:"`` persists to ``D:\\`` root, not
    ``<cwd>``), so the config it loads can't be named — fail closed. A bare device-
    namespace drive (``\\\\?\\C:`` / ``\\\\.\\C:``), a bare or incomplete namespace root
    (``\\\\?\\``, ``\\\\.\\``, ``\\\\?\\UNC\\server\\`` — some Pythons join them with a
    DOUBLED separator where Node's single-separator spelling names an object the harness
    never enumerates), an INCOMPLETE ordinary UNC root (``\\\\``, ``\\\\srv``,
    ``\\\\srv\\`` — Node resolves a bare ``\\\\`` from the CHILD's current-drive root,
    e.g. ``D:\\mcp-config.json``, while the harness's join names nothing; caught here by
    the two-separator prefix even on 3.10, where splitdrive returns an EMPTY drive for
    ``\\\\``/``\\\\srv`` and the plain drive test would fall through to cwd anchoring),
    a terminal-colon root (``\\\\?\\foo:``, ``\\\\srv\\share:`` — ``ntpath.join`` GLUES
    the child name on where Node inserts the separator), and an unsafe ``\\\\?\\``
    device path — a nonliteral ``//?/`` spelling, or a noncanonical literal one (a
    ``/`` or a ``.``/``..``/internal-or-repeated-empty segment) that copilot's Node
    resolver canonicalizes but ``ntpath.join`` does not — are rejected the same way; an
    otherwise-noncanonical ``\\\\.\\`` device path is exempt (Windows normalizes it
    identically in both processes). See ``_win_fully_qualified``."""
    explicit = env_map.get("COPILOT_HOME")
    if explicit:
        home = explicit
    elif sys.platform == "win32":  # pragma: no cover — win32 only
        base = env_map.get("USERPROFILE")
        if base is None or len(base) < 3:
            raise RuntimeError(
                f"copilot home base USERPROFILE={base!r}: on win32 libuv's homedir() "
                "resolves an ABSENT value via GetUserProfileDirectoryW (the real "
                "profile dir, from the process token, not this env) and ERRORS on a "
                "present-but-<3-char one (Node os.homedir() throws); either way the "
                "config copilot loads can't be named from the env, failing closed."
            )
        # Vet the RAW base BEFORE the join: when splitdrive returns the whole value as
        # a drive ending in ":" (\\?\foo:, \\.\foo:, \\srv\share: on every version;
        # \\?\UNC\srv\share: on 3.11+, whose splitdrive absorbs it whole — 3.10 keeps
        # a rooted tail and INSERTS like Node), ntpath.join GLUES ".copilot" straight
        # on (\\?\foo:.copilot) where Node inserts the separator (\\?\foo:\.copilot).
        # The glued string is itself a device path the fully-qualified predicate
        # accepts, so without this pre-join test the harness would enumerate a
        # different config than copilot loads.
        b_drive, b_tail = ntpath.splitdrive(base)
        if b_drive.endswith(":") and not b_tail:
            raise RuntimeError(
                f"copilot home base USERPROFILE={base!r} is one whole splitdrive "
                "\"drive\" ending in ':' — ntpath.join GLUES '.copilot' straight onto "
                "it (\\\\?\\foo:.copilot) where copilot's path resolver inserts the "
                "separator (\\\\?\\foo:\\.copilot), and no normalization reconverges "
                "a colon glue — the harness would enumerate a different config than "
                "copilot loads; failing closed."
            )
        home = os.path.join(base, ".copilot")
    else:
        base = env_map.get("HOME")
        if base is None:
            base = os.path.expanduser("~")
        home = os.path.join(base, ".copilot")  # empty $HOME preserved → relative join
    fully_qualified = _win_fully_qualified if sys.platform == "win32" else os.path.isabs
    if fully_qualified(home):
        return home
    if sys.platform == "win32" and (
            ntpath.splitdrive(home)[0]
            # 3.10 splitdrive returns an EMPTY drive for \\ and \\srv — without this
            # prefix test they would fall through to cwd anchoring, which copilot
            # (resolving them in the UNC namespace / at the child drive's root)
            # does not do.
            or home.replace("/", "\\").startswith("\\\\")):
        raise RuntimeError(
            f"copilot home {home!r} is drive-relative, a bare or incomplete UNC or "
            "device-namespace root or drive, a terminal-colon root, or an unsafe "
            "\\\\?\\ device path on win32 — a "
            "drive-relative path resolves against that drive's own current directory "
            "(unknowable here, and copilot would not resolve it against the child cwd); "
            "an incomplete UNC root (\\\\, \\\\srv, \\\\srv\\) is resolved by copilot "
            "in the UNC namespace or from the child drive's ROOT while the harness's "
            "join names something else; joining under a bare \\\\?\\X: drops the "
            "separator, a bare/incomplete "
            "namespace root (\\\\?\\, \\\\?\\UNC\\srv\\) can DOUBLE it vs Node's join, "
            "and a terminal-colon root (\\\\?\\foo:, \\\\srv\\share:) GLUES the child "
            "name on where Node inserts the separator; "
            "and under \\\\?\\ (which Windows opens without "
            "normalizing) a nonliteral //?/ spelling or a noncanonical literal one (a "
            "'/', or a '.'/'..'/internal-or-repeated-empty segment) is canonicalized by "
            "copilot's path resolver but not by ntpath.join, so the two would read "
            "different files; the config copilot loads can't be named, failing closed."
        )
    anchor = os.path.abspath(cwd or os.getcwd())
    resolved = os.path.normpath(os.path.join(anchor, home))
    if not fully_qualified(resolved):
        raise RuntimeError(
            f"copilot home {home!r} does not resolve to a fully-qualified path against "
            f"the child cwd {anchor!r} — the config copilot would load cannot be named, "
            "failing closed rather than enumerating the wrong one."
        )
    return resolved


# Config keys the sanitizer drops. Plugin state lives in ~/.copilot/config.json:
# "installedPlugins" records carry an absolute cache_path the loader follows even
# when installed-plugins/ is masked empty (verified 1.0.64 — an empty mirrored
# plugin dir still exposed a plugin's MCP server). "enabledMcpServers" switches on
# feature-gated built-in servers (the staff-gated computer-use in 1.0.64) that
# --disable-builtin-mcps does not cover — those are also disabled by name via
# _BUILTIN_MCP_SERVERS, this strip is the config-side half.
_COPILOT_CONFIG_STRIP_KEYS = ("installedPlugins", "enabledPlugins",
                              "enabledMcpServers")


def _sanitized_copilot_config(real_path: str) -> str:
    """Sanitizing mask for ~/.copilot/config.json: keep everything (auth tokens,
    settings), drop the plugin registrations and the built-in-server enablement
    list (``_COPILOT_CONFIG_STRIP_KEYS``), and FORCE
    ``customAgents.defaultLocalOnly`` — the documented setting (``copilot help
    config``) that is the only off-switch for remote custom-agent discovery, whose
    org/enterprise listings can carry MCP servers outside the --disable-mcp-server
    set (see the custom-agents comment at the top of this module). The file is
    JSONC — line/inline/block comments and trailing commas, the live-verified
    grammar — handled by ``_load_copilot_config``. Unreadable/unparseable → the
    same neutral-but-hermetic shape (fail closed: no plugins, no remote agents;
    auth loss surfaces loudly rather than servers silently loading)."""
    data = _load_copilot_config(real_path)
    if not isinstance(data, dict):
        data = {}
    for key in _COPILOT_CONFIG_STRIP_KEYS:
        data.pop(key, None)
    agents_cfg = data.get("customAgents")
    if not isinstance(agents_cfg, dict):
        agents_cfg = {}
    agents_cfg["defaultLocalOnly"] = True
    data["customAgents"] = agents_cfg
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
    # _mcp_disable_args additionally disables every *enumerable* server by name on argv
    # — the built-in/feature-gated names unconditionally (--disable-builtin-mcps'
    # help names only github-mcp-server; see _BUILTIN_MCP_SERVERS), the user config
    # with copilot's live-verified JSONC grammar, and the workspace configs — which
    # also covers probes, judge runs, and non-isolated runs (where plugins remain a
    # documented gap). On Windows the same argv layer FAILS CLOSED whenever the ODR
    # registry gate (HKLM\...\CurrentVersion\Mcp) is populated: copilot executes the
    # registry command itself, so pre-enumerating its listing cannot be sound (see
    # _assert_odr_gate_off).
    # Custom agents are one more channel — their frontmatter mcp-servers start OUTSIDE
    # the --disable-mcp-server set (see the module comment): agents/ is masked to an
    # empty dir, the sanitized config.json gets customAgents.defaultLocalOnly forced
    # (kills remote agent discovery), and _mcp_disable_args fails closed on any local
    # agent file the masks can't reach (workspace convention dirs, non-isolated homes).
    isolation_config_masks = {".copilot/mcp-config.json": '{"mcpServers": {}}',
                              ".copilot/installed-plugins": None,
                              ".copilot/agents": None,
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

    def _probe_argv(self, model: str, *, cwd: Optional[str] = None,
                    env: Optional[dict] = None):
        return [self.binary, "-p", "say ok", *self._HERMETIC,
                *self._mcp_disable_args(cwd or os.getcwd(), env=env),
                "--model", model, "--output-format", "json", "--allow-all"]

    def _mcp_disable_args(self, cwd: Optional[str],
                          env: Optional[Mapping[str, str]] = None) -> list[str]:
        """``--disable-mcp-server <name>`` for every enumerable server: the built-in /
        feature-gated in-process servers (``_BUILTIN_MCP_SERVERS`` — unconditionally,
        since --disable-builtin-mcps names only github-mcp-server in 1.0.64), the user
        config ($COPILOT_HOME else ~/.copilot, mcp-config.json, parsed as copilot's
        JSONC), and the workspace configs copilot discovers from the run cwd upward. On
        Windows the ODR registry gate is NOT enumerated — a populated gate fails the
        whole invocation closed instead (``_assert_odr_gate_off``: copilot runs the
        registry command itself, so a harness re-execution can't prove it sees the same
        listing). Resolution uses the *child's* env (``env``; exec.execute() passes the
        exact subprocess environment) so a scenario's ``env: {COPILOT_HOME: ...}``
        override or an isolated run's repointed HOME enumerates the config the child
        will actually read — a relative home anchoring to the child's ``cwd``, where
        copilot itself resolves it (see _copilot_home). Riding on argv makes this cover
        every invocation the same
        way — cells (including a scenario-seeded workspace config), model probes, judge
        runs, and non-isolated runs — with the isolation masks as the second layer.
        Plugin-declared servers can't be enumerated here (their names live inside each
        plugin's definition); those are covered by the installed-plugins isolation mask
        and remain a documented gap for non-isolated runs. Custom-agent servers can't
        be DISABLED here at all — a selected agent's mcp-servers start outside the
        --disable-mcp-server set (module comment) — so any discoverable local agent
        file, and a git-repo cwd whose config doesn't provably opt out of remote agent
        discovery (customAgents.defaultLocalOnly — injected into the sanitized
        config.json, so masked-home runs pass), fail closed instead. On win32 the
        harness must itself be a 64-BIT process; a 32-bit one fails closed up front
        (WOW64 redirection — see the inline comment)."""
        # A 32-bit harness process on win32 shares neither filesystem nor default-
        # registry views with copilot's 64-bit process: the WOW64 redirector remaps
        # C:\Windows\System32 file reads to SysWOW64, and the default registry view to
        # WOW6432Node. The registry READ is pinned to the 64-bit view (KEY_WOW64_64KEY
        # — _odr_registry_command), but the config FILES this method reads have no such
        # per-call escape, so nothing enumerated below would be provably what copilot
        # sees — fail closed before enumerating anything. (The ODR command is copilot's
        # to launch, not the harness's; the harness only reads the gate value.)
        if sys.platform == "win32" and sys.maxsize <= 2**32:
            raise RuntimeError(
                "the harness is running as a 32-bit process on win32: WOW64 "
                "redirection gives it different filesystem (System32 → SysWOW64) and "
                "default-registry views than copilot's 64-bit process, so the config "
                "files it reads are not provably the ones copilot sees — MCP "
                "hermeticity can't be verified; failing closed. Run the harness under "
                "a 64-bit Python."
            )
        env_map: Mapping[str, str] = env if env is not None else os.environ
        home = _copilot_home(env_map, cwd)
        # Custom agents (module comment): their mcp-servers start OUTSIDE the
        # --disable-mcp-server set and no flag disables agents, so presence of any
        # LOCAL agent definition — or the possibility of a REMOTE org/enterprise
        # listing (git-repo cwd, config not provably opted out) — fails closed
        # before anything is enumerated.
        agent_files = _custom_agent_files(home, cwd, env_map)
        if agent_files:
            shown = ", ".join(agent_files[:4]) + (", ..." if len(agent_files) > 4
                                                  else "")
            raise RuntimeError(
                f"custom-agent definition file(s) [{shown}] are discoverable by "
                "copilot (under <config home>/agents, or a .github/agents / "
                ".claude/agents dir between the run cwd and its git root): agent "
                "frontmatter can declare mcp-servers whose names the harness can't "
                "enumerate (local frontmatter plus unreadable REMOTE org/enterprise "
                "listings), so it has nothing reliable to add to --disable-mcp-server "
                "even though copilot's startServerOnce would honor that set, with no "
                "flag to disable custom agents — MCP hermeticity can't be enforced; "
                "failing closed. Remove or relocate the agent files for harness "
                "runs (isolation already masks <home>/agents to an empty dir)."
            )
        if (cwd and (_git_env_repo(env_map) or _nearest_git_root(cwd))
                and not _remote_agents_opted_out(home)):
            raise RuntimeError(
                f"the run cwd {cwd!r} sits inside a git repository (a .git entry, "
                "or GIT_DIR/GIT_WORK_TREE/GIT_COMMON_DIR in the child env) and the "
                "config copilot will read does not set "
                "customAgents.defaultLocalOnly: for a GitHub-remoted repo with "
                "auth, copilot lists org/enterprise custom agents remotely, and "
                "those can carry mcp-servers that start OUTSIDE the "
                "--disable-mcp-server set (1.0.64 bundle) with no flag to disable "
                "the listing — MCP hermeticity can't be verified; failing closed. "
                "Isolated runs get the opt-out injected into the sanitized "
                'config.json automatically; for a non-isolated run set '
                '{"customAgents": {"defaultLocalOnly": true}} in the real '
                "config.json."
            )
        # Built-in / feature-gated in-process servers are seeded unconditionally:
        # --disable-builtin-mcps documents only github-mcp-server in 1.0.64, while
        # the bundle special-cases github-mcp-server, playwright, bluebird, AND the
        # staff-feature-gated computer-use — which config `enabledMcpServers`
        # (sanitized away under isolation) can switch on. Explicit names cost
        # nothing: --disable-mcp-server tolerates servers that never load.
        names = list(_BUILTIN_MCP_SERVERS)
        names.extend(_user_mcp_server_names(os.path.join(home, "mcp-config.json")))
        if cwd:
            d = os.path.realpath(cwd)   # physical path: a symlinked cwd resolves to the
            while True:                 # real repo tree copilot's discovery walks
                for rel in _WORKSPACE_MCP_FILES:
                    names.extend(_mcp_server_names(os.path.join(d, *rel.split("/"))))
                parent = os.path.dirname(d)
                if parent == d:
                    break
                d = parent
        self._assert_odr_gate_off()
        args: list[str] = []
        for name in dict.fromkeys(names):  # de-dupe, keep order
            args += ["--disable-mcp-server", name]
        return args

    def _assert_odr_gate_off(self) -> None:
        """Fail closed when the Windows ODR registry gate is ON (no-op when off:
        non-win32, or key/value absent/blank under copilot's own trim+falsy test).

        copilot executes the registry-advertised command ITSELF to discover MCP
        servers. Earlier revisions executed it here too and pre-disabled the
        resolved names, but the two executions are independent: a stateful or
        time-varying command can hand the harness listing A and copilot listing B,
        leaving B's servers enabled — no cwd/env/bitness matching closes a
        two-execution gap, and nothing prevents or intercepts copilot's own ODR
        load (no flag, env var, or setting — 1.0.64 bundle). So a populated gate
        means MCP hermeticity cannot be proven for ANY invocation on this host:
        RuntimeError (exec.execute() turns that into a failed run; probe_model
        into an unavailable model)."""
        cmdline = _odr_registry_command()
        if not cmdline:
            return
        raise RuntimeError(
            "the Windows MCP registry gate is ON (HKLM\\SOFTWARE\\Microsoft\\"
            f"Windows\\CurrentVersion\\Mcp advertises the ODR command {cmdline!r}): "
            "copilot executes that command itself to discover MCP servers, and a "
            "second, independent execution by the harness could be handed a "
            "DIFFERENT listing (stateful or time-varying commands), so "
            "pre-enumerating names to disable cannot prove hermeticity — and no "
            "flag, env var, or setting prevents copilot's own ODR load; failing "
            "closed. Clear the registry value (or run on a host without it) for "
            "harness runs."
        )

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
        # extra_args ride at the END of argv (below), appended verbatim — a
        # configuration-channel token there would inject MCP servers or reroute
        # config discovery AFTER _mcp_disable_args computed its disable set (see
        # _CONFIG_CHANNEL_LONG/_SHORT). Standard runner cells never populate
        # extra_args; a programmatic caller passing one of these fails closed here
        # (exec.execute() records a failed run), checked FIRST so a doomed
        # invocation never spends the enumeration.
        bad = _config_channel_token(opts.extra_args)
        if bad is not None:
            raise RuntimeError(
                f"extra_args token {bad!r} opens a copilot configuration channel "
                "(--additional-mcp-config injects MCP servers past the disable "
                "set, --agent selects a custom agent whose mcp-servers start "
                "outside it, --plugin-dir loads plugin-declared servers, "
                "--config-dir repoints the enumerated config home, -C moves "
                "discovery to another cwd) — the run would not be provably "
                "MCP-hermetic; failing closed."
            )
        argv = [self.binary, "-p", prompt, *self._HERMETIC,
                *self._mcp_disable_args(cwd, env=opts.effective_env),
                "--output-format", "json"]
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
