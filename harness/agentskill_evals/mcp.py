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

import json
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


def interpolated_refs(servers: Optional[dict[str, MCPServer]]) -> list[str]:
    """Which declared fields carry a `${VAR}`, i.e. will hold a credential once resolved.

    A property of the DECLARATION, so it needs no environment and cannot be confused with
    `resolve_mcp_servers`'s redaction set. That set deliberately omits values shorter than
    MIN_REDACTABLE_LEN — redacting a three-character string would rewrite unrelated text
    everywhere — and a short credential is still a credential. So `bool(secrets)` answers
    "what can be scrubbed", never "does this cell handle secrets": review found the harness
    failing a cell for credentials it could not have had, because the only question being
    asked was whether `mcp_servers` was present at all. Anything reasoning about exposure
    asks here; only the redaction pass may ask there.

    Covers exactly the fields `resolve_mcp_servers` substitutes into — `env`, `url`,
    `headers` — and deliberately not `command`/`args`, which are never interpolated so that
    a variable cannot choose what program runs.
    """
    found: list[str] = []
    for name, s in (servers or {}).items():
        fields = [(f"{name}.url", s.url or "")]
        fields += [(f"{name}.env.{k}", v) for k, v in s.env.items()]
        fields += [(f"{name}.headers.{k}", v) for k, v in s.headers.items()]
        found += [label for label, text in fields if _VAR_RE.search(text or "")]
    return sorted(found)


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

def _surface_forms(value: str) -> list[str]:
    """Every spelling one secret can wear in an artifact.

    Found in review: searching artifacts for the RAW value misses the escaped one, and
    almost every artifact this harness writes is JSON. A token containing `"`, `\\`, a
    control character, or any non-ASCII byte is re-spelled by the encoder before it lands
    — `a"b` is stored `a\\"b`, `ö` is stored `\\u00f6` — so the raw needle is simply not
    present in the haystack and the scrub passes over a credential sitting in plain view.

    Both encoder settings are generated because both reach disk: this harness writes with
    the `ensure_ascii=True` default, while a CLI's own JSONL stream may use either.
    """
    forms = {value}
    for ensure_ascii in (True, False):
        encoded = json.dumps(value, ensure_ascii=ensure_ascii)[1:-1]
        if encoded:
            forms.add(encoded)
    return list(forms)


def redact(text: str, secrets) -> str:
    """Replace every occurrence of every secret, in each form it can appear in.

    Longest first — across the expanded form set, not just the raw values — so a value
    that contains another is not left half-rewritten by the shorter one's replacement.
    """
    if not text or not secrets:
        return text
    forms: set[str] = set()
    for value in secrets:
        if value:
            forms.update(_surface_forms(value))
    for form in sorted(forms, key=len, reverse=True):
        text = text.replace(form, REDACTED)
    return text


def redact_obj(obj, secrets):
    """Scrub a structure before it is serialized, rather than after.

    ``_rwj`` used to redact the serialized JSON, which made the escaping bug above a leak
    in every structured artifact at once. Walking the object first means the comparison
    happens against the value the author actually supplied, with no encoder in between.
    Keys are scrubbed as well as values: a credential can be a dict key (an env map keyed
    by token, a header name) as easily as a leaf.

    Leaves that are neither str/list/dict/tuple still matter, because ``_write_json``
    serializes them with ``default=str`` — so an object whose ``str()`` exposes a secret
    is replaced by that redacted string rather than passed through to the encoder.
    """
    if not secrets:
        return obj
    if isinstance(obj, str):
        return redact(obj, secrets)
    if isinstance(obj, dict):
        return {redact_obj(k, secrets): redact_obj(v, secrets) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(x, secrets) for x in obj]
    if isinstance(obj, (int, float, bool, type(None))):
        return obj
    rendered = str(obj)
    scrubbed = redact(rendered, secrets)
    return scrubbed if scrubbed != rendered else obj


def redact_bytes(raw: bytes, secrets) -> bytes:
    """Byte-level scrub, for files this harness did not author.

    The agent's workspace is archived into the artifact tree verbatim, so a credential an
    MCP result echoed back can arrive on disk inside a file the agent wrote. Those files
    have no guaranteed encoding and may be binary, so the substitution is done on bytes:
    decoding first would throw away the very files most likely to be skipped silently.
    """
    if not raw or not secrets:
        return raw
    forms: set[bytes] = set()
    for value in secrets:
        if value:
            for form in _surface_forms(value):
                forms.add(form.encode("utf-8", "surrogatepass"))
    replacement = REDACTED.encode("utf-8")
    for form in sorted(forms, key=len, reverse=True):
        raw = raw.replace(form, replacement)
    return raw
