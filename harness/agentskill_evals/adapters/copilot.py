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
import re
import subprocess
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

# Workspace-level MCP config files copilot discovers, checked in the run cwd and every
# ancestor (copilot walks up, like git-root discovery — verified in the 1.0.64 bundle).
_WORKSPACE_MCP_FILES = (".mcp.json", ".github/mcp.json", ".vscode/mcp.json")

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
# branch). Crucially, a selected agent's servers are started per name by the session's
# initializeMcpHost via mcpHost.startServer(name, config) WITHOUT consulting the
# disabledMcpServers set (that set only filters the base config's startServers), so
# disabling by name cannot cover this channel — and no flag disables custom agents
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


def _nearest_git_root(cwd: str) -> Optional[str]:
    """The first ancestor of ``cwd`` (inclusive) containing a ``.git`` entry — dir or
    file (worktrees use a file) — or None. This is the boundary copilot's convention
    walk stops at, and the trigger for its remote custom-agents listing (a git repo
    is a precondition; the harness doesn't parse remotes — git config resolution
    spans include directives and worktree indirection it can't replicate provably,
    so ANY repo counts, the conservative direction)."""
    d = os.path.abspath(cwd)
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def _custom_agent_files(home: str, cwd: Optional[str]) -> list[str]:
    """Every LOCAL custom-agent definition file discoverable for a run:
    ``<home>/agents`` plus the ``.github/agents`` / ``.claude/agents`` convention
    dirs of the run cwd and its ancestors up to the nearest git root (inclusive) —
    copilot's own walk boundary. Without a git root every ancestor is checked (the
    bundle's boundary resolution for repo-less dirs isn't provable, so the walk is
    conservative there). Unlike the workspace MCP-config walk — where an
    over-approximation just adds harmless disables — agent detection FAILS runs
    closed, so this walk mirrors copilot's boundary instead of over-walking to the
    filesystem root (a ~/.claude/agents outside the repo must not kill in-repo
    runs copilot would never read it for)."""
    dirs = [os.path.join(home, "agents")]
    if cwd:
        d = os.path.abspath(cwd)
        while True:
            for rel in _WORKSPACE_AGENT_DIRS:
                dirs.append(os.path.join(d, *rel.split("/")))
            if os.path.exists(os.path.join(d, ".git")):
                break
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
    files: list[str] = []
    for dpath in dirs:
        files.extend(_agent_definition_files(dpath))
    return files


def _load_copilot_config(path: str) -> Optional[Any]:
    """``config.json`` parsed with copilot's JSONC-ish tolerance (full-line ``//``
    comments stripped), or None when unreadable/unparseable."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return None
    text = "\n".join(ln for ln in raw.splitlines() if not ln.lstrip().startswith("//"))
    try:
        return json.loads(text)
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
# --config-dir is in the bundle but hidden from help): --additional-mcp-config merges
# MORE servers into the session past the disable set; --agent selects a custom agent
# whose frontmatter mcp-servers start OUTSIDE that set (see the custom-agents comment
# above); --plugin-dir loads a plugin — whose definition can declare mcpServers — from
# an arbitrary dir; --config-dir repoints the whole config home away from the one this
# adapter enumerated; -C changes copilot's working directory BEFORE any discovery,
# invalidating the cwd the workspace/agent walks used. Long forms match exact or
# --flag=value; -C exact or with an attached value (-Cdir).
_CONFIG_CHANNEL_LONG = ("--additional-mcp-config", "--agent", "--plugin-dir",
                        "--config-dir")
_CONFIG_CHANNEL_SHORT = ("-C",)


def _config_channel_token(extra_args: list[str]) -> Optional[str]:
    """The first extra_args token that opens a copilot configuration channel, or None.
    A token that merely LOOKS like one (e.g. a value following some unrelated flag) is
    reported too — that false positive fails closed, the safe direction."""
    for tok in extra_args:
        if any(tok == f or tok.startswith(f + "=") for f in _CONFIG_CHANNEL_LONG):
            return tok
        if any(tok.startswith(f) for f in _CONFIG_CHANNEL_SHORT):
            return tok
    return None

# --- Windows ODR (On-Device Registry) MCP discovery -----------------------------------
#
# On win32, copilot additionally discovers MCP servers through the Windows MCP registry:
# it reads the string value "Command" under HKLM\SOFTWARE\Microsoft\Windows\
# CurrentVersion\Mcp, executes `<that command> mcp list` (from its own workspace, with
# its child environment, UTF-8), parses the JSON `servers` array from its stdout, and
# registers each named server — unwrapping `entry.server ?? entry`, then sanitizing the
# name to the charset [0-9a-zA-Z_./@-] per UTF-16 code UNIT (others become "_"; an astral
# char is two surrogate units → "__") and de-colliding duplicates by appending an
# FNV-1a-32 hash of the original name (then _2, _3, ...). All of this is read out of the
# 1.0.64 bundle (app.js, the "ODR load"/"ODR convert" pipeline); no flag, env var, or
# setting turns it off. The helpers below reproduce that resolution exactly — including
# the child cwd/env and per-UTF-16-unit sanitization — so the resolved names can be fed
# to --disable-mcp-server; anything short of a positive enumeration while the registry
# gate is on FAILS CLOSED (RuntimeError) — the alternative is a run with un-disabled
# registry servers live. Over-enumerating is safe: --disable-mcp-server tolerates names
# copilot doesn't load (verified 1.0.64), so servers copilot itself would skip (no usable
# packages/remotes) are just disabled no-ops. Bitness matters twice: the registry read
# carries copilot's exact KEY_READ|KEY_WOW64_64KEY access mask (the 64-bit view from any
# harness bitness — a 32-bit default would read WOW6432Node, where an absent key would
# fake the gate "off"), and a 32-bit harness process fails closed outright in
# _mcp_disable_args, because the WOW64 file-system redirector would still remap a
# System32-homed command executable at launch (System32 → SysWOW64) while 64-bit
# copilot runs the real one.
_ODR_REGISTRY_SUBKEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Mcp"
_ODR_NAME_BAD_CHARS = re.compile(r"[^0-9a-zA-Z_./@-]")

# ECMAScript whitespace — the exact set /\s/ tests AND String.prototype.trim() strips
# (identical by spec: WhiteSpace ∪ LineTerminator; verified identical by exhaustive
# code-point sweep under node v24, the runtime copilot ships on). It differs from
# Python's str whitespace in both directions: U+001C–U+001F and U+0085 are
# Python-space but NOT JS-space (they glue into a JS token), while U+FEFF is JS-space
# but NOT Python-space (a BOM-prefixed registry value trims clean in copilot). The
# bundle's ODR pipeline trims the registry value and splits the command line with
# these semantics, so the harness must use this set — str.split(None)/isspace() would
# hand the same fully-qualified executable different ARGUMENTS than copilot passes,
# enumerating a different listing.
_JS_WS = ("\t\n\x0b\x0c\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006"
          "\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff")
_JS_WS_SET = frozenset(_JS_WS)
# JS LineTerminator — what `.` in the bundle's unquoted-branch regex can NOT match.
_JS_LT = "\n\r\u2028\u2029"
# Mirror of the bundle's /^([^\s]+)\s*(.*)$/ with the JS classes made explicit:
# first token = maximal non-JS-ws run, then a JS-ws run, then a remainder free of
# LineTerminators, anchored to the true end (\Z — JS `$` without /m). A command line
# this can't match (a line terminator interrupting the tail) makes copilot's parser
# throw, which its ODR loader catches and treats as "no ODR servers".
_ODR_UNQUOTED_RE = re.compile(
    "^([^{ws}]+)[{ws}]*([^{lt}]*)\\Z".format(ws=re.escape(_JS_WS),
                                             lt=re.escape(_JS_LT)))


def _js_trim(s: str) -> str:
    """String.prototype.trim() — strips exactly the ECMAScript whitespace set from
    both ends (str.strip(chars) with the explicit set; NOT str.strip(), whose
    Python set diverges — see _JS_WS)."""
    return s.strip(_JS_WS)


def _odr_registry_command() -> Optional[str]:
    """The ODR command line from the registry, or None when the gate is off (non-win32,
    key/value absent). An unreadable key with the gate possibly on raises RuntimeError.

    The key is opened with copilot's exact access mask — ``KEY_READ | KEY_WOW64_64KEY``,
    read out of the 1.0.64 native helper — so this reads the 64-BIT registry view no
    matter the harness's own bitness. A 32-bit process's DEFAULT view is the redirected
    WOW6432Node one, where the key can be absent (the gate would read "off") while the
    64-bit view copilot queries is populated; the explicit view flag removes that
    divergence from the read itself (32-bit Windows, with its single view, ignores the
    flag). Exercised cross-host in the selftest via a stubbed ``winreg``."""
    if sys.platform != "win32":
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _ODR_REGISTRY_SUBKEY, 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
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
    # one of only U+001C (Python-blank, JS-nonblank) keeps the gate ON — copilot then
    # fails to launch it and loads nothing; the harness fails closed downstream on the
    # unqualified executable, the conservative side of the same no-servers outcome.
    if not isinstance(value, str) or not _js_trim(value):
        return None  # empty command → copilot logs "command line not found" and skips
    return _js_trim(value)


def _split_odr_command(cmdline: str) -> tuple[str, list[str]]:
    """Split the registry command line the way copilot does (1.0.64 bundle): a leading
    double-quoted token or the first whitespace-delimited token is the executable; the
    rest splits on unquoted whitespace with bare quote toggling (no escapes).

    "Whitespace" is ECMAScript whitespace throughout (``_JS_WS`` — the class the
    bundle's ``/\\s/`` tests and its ``trim()`` calls strip), NOT Python's: with
    ``str.split(None)``/``isspace()`` a command like ``C:\\odr.exe<U+001C>arg`` would
    hand the harness a different (exe, args) split than copilot's — same executable,
    different listing. The unquoted branch mirrors the bundle's
    ``/^([^\\s]+)\\s*(.*)$/`` exactly (``_ODR_UNQUOTED_RE``): a line terminator
    interrupting the tail makes that regex fail, which copilot's ODR loader catches
    and treats as "no ODR servers" — here it raises instead (the caller fails
    closed: conservative, since copilot provably loads nothing there)."""
    s = _js_trim(cmdline)
    if not s:
        raise ValueError("empty ODR command line")
    if s.startswith('"'):
        end = s.find('"', 1)
        if end < 0:
            raise ValueError("unterminated quote in ODR command line")
        exe, rest = s[1:end], _js_trim(s[end + 1:])
    else:
        m = _ODR_UNQUOTED_RE.match(s)
        if m is None:
            raise ValueError("unparseable ODR command line (line terminator in tail)")
        exe, rest = m.group(1), _js_trim(m.group(2))
    args: list[str] = []
    buf, in_quote = "", False
    for ch in rest:
        if ch == '"':
            in_quote = not in_quote
            continue
        if not in_quote and ch in _JS_WS_SET:
            if buf:
                args.append(buf)
                buf = ""
            continue
        buf += ch
    if buf:
        args.append(buf)
    return exe, args


def _odr_fnv1a32(name: str) -> str:
    """FNV-1a 32-bit over UTF-16 code units, hex — byte-for-byte the collision suffix
    copilot computes (JS charCodeAt + Math.imul, unsigned at the end)."""
    h = 2166136261
    raw = name.encode("utf-16-le")
    for i in range(0, len(raw), 2):
        h ^= raw[i] | (raw[i + 1] << 8)
        h = (h * 16777619) & 0xFFFFFFFF
    return format(h, "x")


def _odr_sanitize_name(name: str) -> str:
    """Sanitize a server name to copilot's charset exactly as the 1.0.64 bundle does — a
    JS ``String.prototype.replace(/[^0-9a-zA-Z_.\\/@-]/g, "_")``, which iterates UTF-16
    code UNITS, not Unicode code points. An astral character (U+10000+) is two surrogate
    units, each outside the charset, so it becomes ``__`` (two underscores) — matching
    copilot; Python's per-code-point ``re.sub`` would emit only one. Every allowed char is
    ASCII (a single unit < 0x80), so a unit is kept iff it's ASCII and in the charset."""
    raw = name.encode("utf-16-le")
    out: list[str] = []
    for i in range(0, len(raw), 2):
        unit = raw[i] | (raw[i + 1] << 8)
        ch = chr(unit)
        out.append(ch if unit < 0x80 and not _ODR_NAME_BAD_CHARS.match(ch) else "_")
    return "".join(out)


def _odr_resolved_names(servers: list) -> list[str]:
    """The server names copilot would register from an ODR ``servers`` listing, in
    listing order: sanitize each entry's name, then de-collide with the FNV-1a suffix
    (of the ORIGINAL name) and _2/_3/... counters — mirroring the bundle's converter.
    Each entry is unwrapped ``entry.server ?? entry`` first (copilot accepts both a bare
    ``{"name": ...}`` and a wrapped ``{"server": {"name": ...}}`` shape; nullish-
    coalescing means only a null/absent ``server`` falls back to the entry itself), and
    its ``name`` is read off that target. Unnamed/non-object entries are skipped there too
    (they don't join the collision set)."""
    seen: set[str] = set()
    out: list[str] = []
    for entry in servers:
        if not isinstance(entry, dict):
            continue
        wrapped = entry.get("server")
        target = wrapped if wrapped is not None else entry
        name = target.get("name") if isinstance(target, dict) else None
        if not isinstance(name, str) or not name:
            continue
        resolved = _odr_sanitize_name(name)
        if resolved in seen:
            hashed = f"{resolved}_{_odr_fnv1a32(name)}"
            if hashed in seen:
                n = 2
                while f"{hashed}_{n}" in seen:
                    n += 1
                hashed = f"{hashed}_{n}"
            resolved = hashed
        seen.add(resolved)
        out.append(resolved)
    return out


def _parse_odr_listing(stdout: str) -> list:
    """The ``servers`` array from the ODR command's stdout — direct JSON, else the
    first-``{``-to-last-``}`` slice (copilot tolerates banners the same way)."""
    text = (stdout or "").strip()
    if not text:
        raise ValueError("empty ODR listing output")
    data: Any = None
    try:
        data = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("ODR listing output is not a JSON object")
    servers = data.get("servers")
    if not isinstance(servers, list):
        raise ValueError("ODR listing output missing 'servers' array")
    return servers


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


def _win_exe_has_extension(exe: str) -> bool:
    """Mirror of libuv's ``name_has_ext`` test (``search_path`` in src/win/process.c,
    verified against v1.51.0): the FIRST dot in the filename portion — everything after
    the last ``\\``, ``/`` or ``:`` — followed by at least one character. Node/libuv only
    tries the EXACT filename first when this holds (Node never sets
    UV_PROCESS_WINDOWS_FILE_PATH_EXACT_NAME); an extensionless name is probed as
    ``<name>.com`` then ``<name>.exe`` instead — never the exact file that CreateProcess
    (which appends nothing to a command containing a path) executes. When it holds and
    the exact file exists, both resolvers pick that same file; when the file is missing,
    the harness's launch fails and the enumeration fails closed. Pure string logic so it
    is unit-testable on any host; only consulted on win32."""
    name = exe.replace("/", "\\").rpartition("\\")[2].rpartition(":")[2]
    dot = name.find(".")
    return dot != -1 and dot != len(name) - 1


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


# Plugin state lives in ~/.copilot/config.json: "installedPlugins" records carry an
# absolute cache_path the loader follows even when installed-plugins/ is masked empty
# (verified 1.0.64 — an empty mirrored plugin dir still exposed a plugin's MCP server).
_COPILOT_PLUGIN_STATE_KEYS = ("installedPlugins", "enabledPlugins")


def _sanitized_copilot_config(real_path: str) -> str:
    """Sanitizing mask for ~/.copilot/config.json: keep everything (auth tokens,
    settings), drop the plugin registrations, and FORCE
    ``customAgents.defaultLocalOnly`` — the documented setting (``copilot help
    config``) that is the only off-switch for remote custom-agent discovery, whose
    org/enterprise listings can carry MCP servers outside the --disable-mcp-server
    set (see the custom-agents comment at the top of this module). The file is
    JSONC-ish — full-line ``//`` comments above/inside the JSON — handled by
    ``_load_copilot_config``. Unreadable/unparseable → the same neutral-but-hermetic
    shape (fail closed: no plugins, no remote agents; auth loss surfaces loudly
    rather than servers silently loading)."""
    data = _load_copilot_config(real_path)
    if not isinstance(data, dict):
        data = {}
    for key in _COPILOT_PLUGIN_STATE_KEYS:
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
    # _mcp_disable_args additionally disables every *enumerable* server by name on argv,
    # which also covers probes, judge runs, and non-isolated runs (where plugins remain a
    # documented gap). On Windows the same argv layer enumerates the ODR registry servers
    # (HKLM\...\CurrentVersion\Mcp) via copilot's own resolution pipeline and disables
    # them too — or fails closed when they can't be enumerated (see _odr_mcp_server_names).
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
        """``--disable-mcp-server <name>`` for every enumerable server: the user config
        ($COPILOT_HOME else ~/.copilot, mcp-config.json), the workspace configs copilot
        discovers from the run cwd upward, and — on Windows — the ODR registry servers
        (enumerated the same way copilot does, or failing closed; see the module-level
        helpers). Resolution uses the *child's* env (``env``; exec.execute() passes the
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
        # C:\Windows\System32 (file reads AND the ODR command launch) to SysWOW64, and
        # the default registry view to WOW6432Node. The registry READ is pinned to the
        # 64-bit view (KEY_WOW64_64KEY — _odr_registry_command), but launches and file
        # reads have no such per-call escape, so nothing enumerated below would be
        # provably what copilot sees — fail closed before enumerating anything.
        if sys.platform == "win32" and sys.maxsize <= 2**32:
            raise RuntimeError(
                "the harness is running as a 32-bit process on win32: WOW64 "
                "redirection gives it different filesystem (System32 → SysWOW64) and "
                "default-registry views than copilot's 64-bit process, so the configs "
                "it reads and the ODR registry command it launches are not provably "
                "the ones copilot sees — MCP hermeticity can't be verified; failing "
                "closed. Run the harness under a 64-bit Python."
            )
        env_map: Mapping[str, str] = env if env is not None else os.environ
        home = _copilot_home(env_map, cwd)
        # Custom agents (module comment): their mcp-servers start OUTSIDE the
        # --disable-mcp-server set and no flag disables agents, so presence of any
        # LOCAL agent definition — or the possibility of a REMOTE org/enterprise
        # listing (git-repo cwd, config not provably opted out) — fails closed
        # before anything is enumerated.
        agent_files = _custom_agent_files(home, cwd)
        if agent_files:
            shown = ", ".join(agent_files[:4]) + (", ..." if len(agent_files) > 4
                                                  else "")
            raise RuntimeError(
                f"custom-agent definition file(s) [{shown}] are discoverable by "
                "copilot (under <config home>/agents, or a .github/agents / "
                ".claude/agents dir between the run cwd and its git root): agent "
                "frontmatter can declare mcp-servers, and a selected agent's "
                "servers are started per name OUTSIDE the --disable-mcp-server set "
                "(1.0.64 bundle: initializeMcpHost calls mcpHost.startServer for "
                "each agent server without consulting disabledMcpServers), with no "
                "flag to disable custom agents — MCP hermeticity can't be enforced; "
                "failing closed. Remove or relocate the agent files for harness "
                "runs (isolation already masks <home>/agents to an empty dir)."
            )
        if cwd and _nearest_git_root(cwd) and not _remote_agents_opted_out(home):
            raise RuntimeError(
                f"the run cwd {cwd!r} sits inside a git repository and the config "
                "copilot will read does not set customAgents.defaultLocalOnly: for "
                "a GitHub-remoted repo with auth, copilot lists org/enterprise "
                "custom agents remotely, and those can carry mcp-servers that "
                "start OUTSIDE the --disable-mcp-server set (1.0.64 bundle) with "
                "no flag to disable the listing — MCP hermeticity can't be "
                "verified; failing closed. Isolated runs get the opt-out injected "
                "into the sanitized config.json automatically; for a non-isolated "
                'run set {"customAgents": {"defaultLocalOnly": true}} in the real '
                "config.json."
            )
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
        names.extend(self._odr_mcp_server_names(cwd=cwd, env=env_map))
        args: list[str] = []
        for name in dict.fromkeys(names):  # de-dupe, keep order
            args += ["--disable-mcp-server", name]
        return args

    def _odr_mcp_server_names(self, cwd: Optional[str] = None,
                              env: Optional[Mapping[str, str]] = None) -> list[str]:
        """Resolved names of the Windows ODR registry servers, [] when the gate is off
        (non-win32 or no registry command). The registry command is run the way copilot
        runs it — from the child's workspace (``cwd``) with the child's environment
        (``env``) and UTF-8 decoding — so a context-sensitive listing or a non-ASCII
        server name resolves to exactly what copilot would register. The command's
        executable must be a FULLY QUALIFIED path: a bare/relative name (``odr.exe``)
        resolves through the launcher's own search rules — Python/CreateProcess searches
        the HARNESS's application dir, cwd, and ambient PATH (the ``env=`` block is not
        consulted), while copilot's Node/libuv searches with the CHILD's PATH — so the
        two can execute DIFFERENT binaries and the harness would disable the wrong
        listing's names while copilot loads the real one. On win32 it must also carry an
        EXTENSION: even for a fully-qualified command, CreateProcess launches the exact
        extensionless file while Node/libuv never tries an extensionless exact name,
        probing ``<name>.com`` then ``<name>.exe`` instead (see
        ``_win_exe_has_extension``) — again two different binaries. While the gate is ON,
        any failure to positively enumerate — unreadable registry, unparseable command
        line, a non-qualified or (win32) extensionless executable, the command
        failing/timing out, output without a ``servers`` array — raises RuntimeError: those servers would otherwise load
        un-disabled, so the invocation fails closed instead (exec.execute() turns that
        into a failed run; probe_model into an unavailable model). Bitness is settled
        before this runs: a 32-bit harness fails closed in ``_mcp_disable_args`` — the
        WOW64 file-system redirector would remap a System32-homed executable to its
        SysWOW64 mirror at the launch below while 64-bit copilot runs the real one."""
        cmdline = _odr_registry_command()
        if not cmdline:
            return []
        try:  # pragma: no cover — needs a live ODR registry (win32); logic selftested via mocks
            exe, args = _split_odr_command(cmdline)
            fully_qualified = (_win_fully_qualified if sys.platform == "win32"
                               else os.path.isabs)
            if not fully_qualified(exe):
                raise RuntimeError(
                    f"the ODR registry command's executable {exe!r} is not a fully-"
                    "qualified path — the harness (CreateProcess search: its own dirs "
                    "and ambient PATH) and copilot (Node/libuv search: the child's "
                    "PATH) could resolve and run DIFFERENT binaries, so this "
                    "enumeration can't be trusted to match what copilot loads; "
                    "failing closed."
                )
            if sys.platform == "win32" and not _win_exe_has_extension(exe):
                raise RuntimeError(
                    f"the ODR registry command's executable {exe!r} has no extension — "
                    "CreateProcess (the harness) launches the exact extensionless file "
                    "when the command carries a path, but copilot's Node/libuv skips an "
                    "extensionless exact name and probes <name>.com then <name>.exe, so "
                    "the two could run DIFFERENT binaries; failing closed."
                )
            # The harness is a 64-bit process here (32-bit failed closed upstream), so
            # CreateProcess resolves a System32-homed exe to the REAL System32 exactly
            # as copilot's 64-bit Node/libuv does — under WOW64 this same launch would
            # silently run the SysWOW64 mirror instead.
            r = subprocess.run([exe, *args, "mcp", "list"], capture_output=True,
                               text=True, encoding="utf-8", timeout=15,
                               stdin=subprocess.DEVNULL, cwd=cwd,
                               env=dict(env) if env is not None else None)
            if r.returncode != 0:
                raise ValueError(f"ODR command exited with code {r.returncode}")
            servers = _parse_odr_listing(r.stdout)
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(
                "the Windows MCP registry (HKLM\\SOFTWARE\\Microsoft\\Windows\\"
                "CurrentVersion\\Mcp) advertises MCP servers but they could not be "
                f"enumerated ({exc}) — failing closed rather than running with "
                "un-disabled registry servers live."
            )
        return _odr_resolved_names(servers)

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
