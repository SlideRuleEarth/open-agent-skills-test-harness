"""Declared MCP servers: schema, `${VAR}` interpolation, and the secrets registry.

Separate from ``spec.py`` because three different layers need these types and none of
them should own the others: the loader parses them, ``validate_spec`` reports on them,
and each adapter renders them into its own native config shape (DESIGN_MCP_Support.md §4).

The parse/validate split follows the rest of the harness. ``parse_mcp_servers`` raises on
STRUCTURE — a shape the rest of the code could not interpret without guessing — while
``validate_mcp_servers`` returns errors for everything a human should be told about all at
once, before any tokens are spent. A missing ``${VAR}`` is deliberately in the second
group: raising at load time would abort discovery of every OTHER eval in the directory
over one unset variable.

Secrets never reach YAML. ``${VAR}`` names an environment variable of the harness process,
resolved at run time, and every value substituted this way is returned in a redaction set
so the runner can scrub it from artifacts (§5.3) — including the case no CLI flag covers,
a tool RESULT echoing a token back into the transcript.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

# Names ride into tool identifiers (`mcp__<server>__<tool>`), argv, and TOML dotted paths,
# so the accepted set is the intersection of what all four CLIs take: codex's `mcp add`
# restricts to this charset, and anything outside it either needs TOML quoting the `-c`
# parser cannot express (§2) or is not addressable on a claude `--disallowedTools` entry.
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# `${VAR}`. Deliberately not `$VAR`: a bare-word form would make every literal `$` in a
# header or URL a potential interpolation site, and silently substituting where the author
# meant a literal is the failure mode this whole field exists to avoid.
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

_STDIO_KEYS = {"command", "args", "env", "tools"}
_REMOTE_KEYS = {"url", "transport", "headers", "tools"}
_ALL_KEYS = _STDIO_KEYS | _REMOTE_KEYS

# Native per-server filter spellings. Claude ACCEPTS these in an `--mcp-config` server
# object and silently ignores them (measured 2.1.113, §6-C2); codex spells the same idea
# `enabled_tools`/`disabled_tools`. Writing one through would be a filter that quietly does
# nothing, so they are refused in favour of the portable `tools:` field.
_NATIVE_FILTER_KEYS = {"allowedTools", "enabledTools", "enabled_tools", "disabled_tools",
                       "allowed_tools", "disabledTools"}

_TRANSPORTS = {"http", "sse"}

# A redaction set built from very short values would rewrite unrelated text all over the
# artifacts — a one-character secret would blank out every occurrence of that character.
# Below this length the value is left un-redacted and validation says so out loud, which is
# the honest trade: a warning the author can act on beats artifacts that silently rot.
MIN_REDACTABLE_LEN = 6

REDACTED = "«redacted»"


@dataclass(frozen=True)
class MCPServer:
    """One declared server, as written (pre-interpolation) or resolved (post).

    ``env``/``headers`` values may contain `${VAR}` in the parsed form and never do in the
    resolved form — `resolve_mcp_servers` is the only thing that closes that gap, and it
    returns the substituted values alongside so they can be scrubbed from artifacts.
    """
    name: str
    command: Optional[str] = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: Optional[str] = None
    transport: str = "http"
    headers: dict[str, str] = field(default_factory=dict)
    tools: Optional[list[str]] = None   # None = no filter; [] = an explicit empty allowlist

    @property
    def is_stdio(self) -> bool:
        return self.command is not None


def parse_mcp_servers(raw: Any, *, where: str) -> dict[str, MCPServer]:
    """Structural parse. Raises ValueError on a shape nothing downstream could interpret.

    Everything checkable without the environment is checked here, so a typo is a load
    error rather than a confusing per-adapter failure mid-run.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"{where}: `mcp_servers` must be a mapping of name -> server, got "
            f"{type(raw).__name__}")

    out: dict[str, MCPServer] = {}
    for name, body in raw.items():
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise ValueError(
                f"{where}: MCP server name {name!r} must match [A-Za-z0-9_-]+ — the name "
                "becomes part of a tool identifier and a config key in every runner")
        if "__" in name:
            # `mcp__a__b__c` cannot be split back into server and tool unambiguously, and
            # that split is exactly what the post-run allowlist check in §6-C2 performs.
            raise ValueError(
                f"{where}: MCP server name {name!r} contains '__', which is the separator "
                "in claude's `mcp__<server>__<tool>` tool names — the server and tool "
                "could not be told apart afterwards")
        if not isinstance(body, dict):
            raise ValueError(
                f"{where}: MCP server {name!r} must be a mapping, got "
                f"{type(body).__name__}")

        native = sorted(set(body) & _NATIVE_FILTER_KEYS)
        if native:
            raise ValueError(
                f"{where}: MCP server {name!r} sets {', '.join(native)} — that is a "
                "CLI-native filter spelling, which claude accepts and silently IGNORES "
                "(and codex spells differently again). Use the portable `tools:` list.")
        unknown = sorted(set(body) - _ALL_KEYS)
        if unknown:
            raise ValueError(
                f"{where}: MCP server {name!r} has unknown key(s): {', '.join(unknown)}. "
                f"Known keys: {', '.join(sorted(_ALL_KEYS))}")

        has_command = "command" in body
        has_url = "url" in body
        if has_command == has_url:
            raise ValueError(
                f"{where}: MCP server {name!r} must set exactly one of `command:` (stdio) "
                "or `url:` (remote)" + (" — it sets both" if has_command else " — it sets neither"))

        cross = sorted(set(body) & ((_REMOTE_KEYS - _STDIO_KEYS) if has_command
                                    else (_STDIO_KEYS - _REMOTE_KEYS)))
        if cross:
            kind = "stdio" if has_command else "remote"
            raise ValueError(
                f"{where}: MCP server {name!r} is {kind} but sets "
                f"{', '.join(cross)}, which belongs to the other transport")

        transport = body.get("transport", "http")
        if has_url and transport not in _TRANSPORTS:
            raise ValueError(
                f"{where}: MCP server {name!r} has transport {transport!r}; "
                f"expected one of {', '.join(sorted(_TRANSPORTS))}")

        out[name] = MCPServer(
            name=name,
            command=_str_or_none(body.get("command"), f"{where}: {name}.command"),
            args=_str_list(body.get("args"), f"{where}: {name}.args"),
            env=_str_map(body.get("env"), f"{where}: {name}.env"),
            url=_str_or_none(body.get("url"), f"{where}: {name}.url"),
            transport=str(transport),
            headers=_str_map(body.get("headers"), f"{where}: {name}.headers"),
            tools=(_str_list(body["tools"], f"{where}: {name}.tools")
                   if "tools" in body else None),
        )
    return out


def _str_or_none(value, where: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{where} must be a non-empty string, got {value!r}")
    return value


def _str_list(value, where: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{where} must be a list, got {type(value).__name__}")
    out = []
    for item in value:
        # bool before int: `args: [true]` is far more likely a YAML accident than intent.
        if isinstance(item, bool) or not isinstance(item, (str, int, float)):
            raise ValueError(f"{where} entries must be strings, got {item!r}")
        out.append(str(item))
    return out


def _str_map(value, where: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be a mapping, got {type(value).__name__}")
    out = {}
    for k, v in value.items():
        if isinstance(v, bool) or not isinstance(v, (str, int, float)):
            raise ValueError(f"{where}.{k} must be a string, got {v!r}")
        out[str(k)] = str(v)
    return out


# ---------------------------------------------------------------------------
# Interpolation
# ---------------------------------------------------------------------------

def referenced_vars(servers: dict[str, MCPServer]) -> set[str]:
    """Every `${VAR}` named anywhere in the declared servers."""
    names: set[str] = set()
    for s in servers.values():
        for text in _interpolatable(s):
            names.update(_VAR_RE.findall(text))
    return names


def _interpolatable(s: MCPServer) -> list[str]:
    """The fields `${VAR}` is honoured in — env values, header values, and the URL.

    Deliberately NOT `command` or `args`: substituting into an executable path or its
    arguments would turn an environment variable into a way to choose what program runs,
    which is a different and much larger power than supplying a credential.
    """
    return [*s.env.values(), *s.headers.values(), *( [s.url] if s.url else [] )]


def validate_mcp_servers(servers: dict[str, MCPServer], *,
                         env: Optional[dict] = None) -> tuple[list[str], list[str]]:
    """Adapter-independent checks. Returns (errors, warnings)."""
    environ = os.environ if env is None else env
    errors: list[str] = []
    warnings: list[str] = []

    for name in sorted(referenced_vars(servers)):
        if name not in environ:
            errors.append(
                f"`mcp_servers` references ${{{name}}} but that variable is not set in "
                "the environment — export it before running (values are never committed "
                "to the eval file)")
        elif len(str(environ[name])) < MIN_REDACTABLE_LEN:
            warnings.append(
                f"${{{name}}} resolves to fewer than {MIN_REDACTABLE_LEN} characters, so "
                "it will NOT be redacted from artifacts — a value that short would rewrite "
                "unrelated text everywhere it happens to occur")

    for s in servers.values():
        if s.tools is not None and not s.tools:
            warnings.append(
                f"MCP server {s.name!r} has an empty `tools: []` — every tool is filtered "
                "out, so the server is reachable but unusable; omit `tools:` to allow all")
        if s.tools:
            dupes = sorted({t for t in s.tools if s.tools.count(t) > 1})
            if dupes:
                warnings.append(
                    f"MCP server {s.name!r} lists tool(s) {', '.join(dupes)} more than once")

    return errors, warnings


def resolve_mcp_servers(servers: dict[str, MCPServer], *,
                        env: Optional[dict] = None,
                        base_dir: Optional[str] = None
                        ) -> tuple[dict[str, MCPServer], set[str]]:
    """Substitute `${VAR}`, absolutize scenario-relative paths, collect the secrets.

    Returns (resolved servers, values to redact). Raises KeyError naming the variable if
    one is unset — `validate_mcp_servers` reports that path cleanly, so reaching the raise
    means validation was skipped.

    Path handling mirrors `files:` (spec.resolved_files): the agent runs in a tempdir
    workspace, so a scenario-relative `fixtures/echo_mcp_server.py` would otherwise not
    exist from the child's cwd. Absolutization is SELECTIVE — a bare `python3` stays a PATH
    lookup, and an argument that does not name a real file under the scenario dir is left
    alone rather than being rewritten into a path that happens to collide.
    """
    environ = os.environ if env is None else env
    secrets: set[str] = set()

    def sub(text: str) -> str:
        def repl(m: re.Match) -> str:
            var = m.group(1)
            if var not in environ:
                raise KeyError(
                    f"mcp_servers references ${{{var}}} but it is not set in the environment")
            value = str(environ[var])
            if len(value) >= MIN_REDACTABLE_LEN:
                secrets.add(value)
            return value
        return _VAR_RE.sub(repl, text)

    out: dict[str, MCPServer] = {}
    for name, s in servers.items():
        out[name] = MCPServer(
            name=s.name,
            command=_abs_command(s.command, base_dir),
            args=[_abs_arg(a, base_dir) for a in s.args],
            env={k: sub(v) for k, v in s.env.items()},
            url=sub(s.url) if s.url else None,
            transport=s.transport,
            headers={k: sub(v) for k, v in s.headers.items()},
            tools=list(s.tools) if s.tools is not None else None,
        )
    return out, secrets


def _abs_command(command: Optional[str], base_dir: Optional[str]) -> Optional[str]:
    """Absolutize only a path-shaped command naming a real file under the scenario dir.

    `python3` must stay a PATH lookup — rewriting it would break every host whose
    interpreter is somewhere else.
    """
    if not command or not base_dir or os.path.isabs(command):
        return command
    if os.sep not in command and (os.altsep or os.sep) not in command:
        return command
    candidate = os.path.join(base_dir, command)
    return candidate if os.path.isfile(candidate) else command


def _abs_arg(arg: str, base_dir: Optional[str]) -> str:
    if not base_dir or os.path.isabs(arg) or arg.startswith("-"):
        return arg
    candidate = os.path.join(base_dir, arg)
    return candidate if os.path.exists(candidate) else arg


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

def redact(text: str, secrets) -> str:
    """Replace every occurrence of every secret. Longest first, so a value that contains
    another is not left half-rewritten by the shorter one's replacement."""
    if not text or not secrets:
        return text
    for value in sorted(secrets, key=len, reverse=True):
        if value:
            text = text.replace(value, REDACTED)
    return text
