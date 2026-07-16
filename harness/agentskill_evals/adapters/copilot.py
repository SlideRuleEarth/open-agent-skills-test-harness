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

Verified against copilot 1.0.63 on 2026-06-22.
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
# packages/remotes) are just disabled no-ops.
_ODR_REGISTRY_SUBKEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Mcp"
_ODR_NAME_BAD_CHARS = re.compile(r"[^0-9a-zA-Z_./@-]")


def _odr_registry_command() -> Optional[str]:
    """The ODR command line from the registry, or None when the gate is off (non-win32,
    key/value absent). An unreadable key with the gate possibly on raises RuntimeError."""
    if sys.platform != "win32":
        return None
    import winreg  # pragma: no cover — win32 only
    try:  # pragma: no cover
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _ODR_REGISTRY_SUBKEY) as key:
            value, _type = winreg.QueryValueEx(key, "Command")
    except FileNotFoundError:  # pragma: no cover
        return None  # no key/value → copilot skips ODR entirely
    except OSError as exc:  # pragma: no cover
        raise RuntimeError(
            f"cannot read the Windows MCP registry key (HKLM\\{_ODR_REGISTRY_SUBKEY}): "
            f"{exc} — its servers can't be enumerated, failing closed."
        )
    if not isinstance(value, str) or not value.strip():  # pragma: no cover
        return None  # empty command → copilot logs "command line not found" and skips
    return value.strip()  # pragma: no cover


def _split_odr_command(cmdline: str) -> tuple[str, list[str]]:
    """Split the registry command line the way copilot does (1.0.64 bundle): a leading
    double-quoted token or the first whitespace-delimited token is the executable; the
    rest splits on unquoted whitespace with bare quote toggling (no escapes)."""
    s = cmdline.strip()
    if not s:
        raise ValueError("empty ODR command line")
    if s.startswith('"'):
        end = s.find('"', 1)
        if end < 0:
            raise ValueError("unterminated quote in ODR command line")
        exe, rest = s[1:end], s[end + 1:].strip()
    else:
        parts = s.split(None, 1)
        exe, rest = parts[0], (parts[1].strip() if len(parts) > 1 else "")
    args: list[str] = []
    buf, in_quote = "", False
    for ch in rest:
        if ch == '"':
            in_quote = not in_quote
            continue
        if not in_quote and ch.isspace():
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
    resolve" — i.e. what the OS treats as an absolute path. Uses ``ntpath`` explicitly so
    the predicate is correct (and unit-testable) on any host, not just win32:

    * a lettered drive WITH a root — ``C:\\x`` — is fully qualified;
    * a complete UNC share — ``\\\\server\\share`` and anything below it — is fully
      qualified (``ntpath.splitdrive`` returns the whole ``\\\\server\\share`` as the
      drive even with an empty tail, yet Python 3.10's ``ntpath.isabs`` wrongly reports
      such a bare share ROOT as relative, fixed in 3.11 — hence this predicate rather
      than ``isabs``);
    * a DEVICE-NAMESPACE path — ``\\\\?\\...`` / ``\\\\.\\...`` — is fully qualified with
      a rooted tail (``\\\\?\\C:\\x``), and so is a complete EXTENDED UNC share root
      (``\\\\?\\UNC\\server\\share``): Python 3.11+ ``splitdrive`` returns the whole
      share root as one "drive" with an empty tail (3.10 splits it as drive
      ``\\\\?\\UNC`` + rooted tail, taking the rooted-tail arm), and ``ntpath.join``
      inserts the separator under it exactly like Node — verified identical on
      3.10–3.14. A bare drive-LETTER device form (``\\\\?\\C:``) is NOT qualified:
      ``splitdrive`` hands it back as a complete "drive" too, but joining under it
      drops the separator (``ntpath.join(r"\\\\?\\C:", "f")`` → ``\\\\?\\C:f``, no such
      path — Node joins ``\\\\?\\C:\\f``), so treating it as qualified would enumerate
      a config file copilot never reads;
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
            if tail[:1] in ("\\", "/"):
                return True                # rooted under a device drive: \\?\C:\x
            # 3.11+ splitdrive returns a complete extended UNC share root as ONE
            # drive with an empty tail; join still inserts the separator (the
            # drive doesn't end in ":"), matching Node — accept exactly that.
            parts = [p for p in d[4:].split("\\") if p]
            return not tail and len(parts) >= 3 and parts[0].upper() == "UNC"
        return True                        # UNC share root \\server\share (and below)
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
      enumerate — so both fail closed.

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
    namespace drive (``\\\\?\\C:``) is rejected the same way — joins under it name paths
    copilot never reads (see ``_win_fully_qualified``)."""
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
        home = os.path.join(base, ".copilot")
    else:
        base = env_map.get("HOME")
        if base is None:
            base = os.path.expanduser("~")
        home = os.path.join(base, ".copilot")  # empty $HOME preserved → relative join
    fully_qualified = _win_fully_qualified if sys.platform == "win32" else os.path.isabs
    if fully_qualified(home):
        return home
    if sys.platform == "win32" and ntpath.splitdrive(home)[0]:  # pragma: no cover — win32
        raise RuntimeError(
            f"copilot home {home!r} is drive-relative or a bare device-namespace drive "
            "on win32 — a drive-relative path resolves against that drive's own current "
            "directory (unknowable here, and copilot would not resolve it against the "
            "child cwd), and joining under a bare \\\\?\\X: drops the separator, naming "
            "a path copilot never reads; the config it loads can't be named, failing "
            "closed."
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
    """Sanitizing mask for ~/.copilot/config.json: keep everything (auth tokens, settings)
    but drop the plugin registrations. The file is JSONC-ish — full-line ``//`` comments
    above/inside the JSON — so strip those before parsing. Unreadable/unparseable → "{}"
    (fail closed: no plugins can load; auth loss surfaces loudly rather than servers
    silently loading)."""
    try:
        with open(real_path, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return "{}"
    text = "\n".join(ln for ln in raw.splitlines() if not ln.lstrip().startswith("//"))
    try:
        data = json.loads(text)
    except ValueError:
        return "{}"
    if isinstance(data, dict):
        for key in _COPILOT_PLUGIN_STATE_KEYS:
            data.pop(key, None)
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
    isolation_config_masks = {".copilot/mcp-config.json": '{"mcpServers": {}}',
                              ".copilot/installed-plugins": None,
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
        and remain a documented gap for non-isolated runs."""
        env_map: Mapping[str, str] = env if env is not None else os.environ
        home = _copilot_home(env_map, cwd)
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
        into a failed run; probe_model into an unavailable model)."""
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
