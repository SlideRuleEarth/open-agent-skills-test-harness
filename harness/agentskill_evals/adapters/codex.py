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
from typing import Any, Mapping, Optional

from ..schema import EventKind, NormalizedEvent
from .base import (Adapter, ParseOutput, ProbeResult, RunOptions, VersionProvenance,
                   extract_command, extract_path, iter_jsonl, try_load_json,
                   warn_unknown_usage)


_KNOWN_USAGE_KEYS = {"input_tokens", "output_tokens", "reasoning_tokens", "total_tokens"}

# --- CLI version provenance (see base.VersionProvenance) ---------------------------
#
# 0.140.0: the build every finding in this file was established against; confirmed the
#          installed build on 2026-07-21 (`codex --version` -> codex-cli 0.140.0, and a
#          rollout written by a live run recorded cli_version 0.140.0).
_VERIFIED_VERSIONS = ("0.140.0",)
_VERIFIED_ON = "2026-07-21"

# Why there is no version to read, established by experiment on 2026-07-21 rather than
# assumed — and worth stating precisely, because it is a trade-off this harness CHOSE
# rather than a limitation codex imposes:
#
#   * `codex exec --json` names no version anywhere in its stream. A complete successful
#     run emits exactly four events — thread.started (thread_id only), turn.started,
#     item.completed, turn.completed — and none carries one.
#   * codex DOES record it, in the rollout file's `session_meta.cli_version`, keyed by an
#     `id` equal to the stream's thread_id. That would qualify: it is written by the run
#     being judged, so it has the same epistemics as any other run telemetry.
#   * But `--ephemeral`, which this adapter passes deliberately for isolation, suppresses
#     the rollout entirely. Verified both directions: with `--ephemeral` no rollout file
#     appears for the run's thread_id; without it one does, carrying cli_version.
#
# So the version is purchasable only by giving up the isolation `--ephemeral` buys, and
# that is the wrong trade — hermeticity is the property under test, provenance is the
# audit trail for it. A `codex --version` probe is not an alternative: the standing rule
# is that a fact learned by executing the program again may not clear a security decision,
# and since a probe could only ever be allowed to WARN, it buys nothing over this.
_VERSION_UNREADABLE = (
    "`codex exec --json` emits no version in its stream, and the one place codex records "
    "it (session_meta.cli_version in the rollout file) is suppressed by the `--ephemeral` "
    "flag this adapter passes for isolation"
)

_PROVENANCE = VersionProvenance(
    agent="codex",
    verified=_VERIFIED_VERSIONS,
    verified_on=_VERIFIED_ON,
    unreadable=_VERSION_UNREADABLE,
    analysis="MCP hermeticity analysis",
    clear_hint=(
        "There is nothing to clear per-run. Check `codex --version` against the list "
        "above out of band; if it differs, re-establish the findings in adapters/codex.py "
        "(each is marked with the version it was verified on) and update "
        "_VERIFIED_VERSIONS."),
)

# What `codex mcp add` accepts as a server name (verified 0.140.0: "use letters, numbers,
# '-', '_'") — exactly the TOML bare-key charset, so any addable name works in an unquoted
# `-c mcp_servers.<name>...` dotted path.
_BARE_KEY_RE = re.compile(r"[A-Za-z0-9_-]+\Z")

# extra_args tokens that reach past the verified MCP-off state (see build_argv): `-c`/
# `--config` overrides appended after the kill-switch outrank it (verified 0.140.0: the
# later of two duplicate `-c mcp_servers.<n>.enabled=` overrides wins), `--profile`/`-p`
# and `--cd`/`-C` change WHICH configuration codex resolves (per-profile config files;
# trusted-project discovery follows the effective cwd), and `--enable` re-opens feature
# channels (`plugins` is its own MCP channel, invisible to `mcp list` enumeration). The
# long forms are matched exact or `--flag=value`; the short forms exact or with an
# attached value (`-cKEY=VAL` parses, verified 0.140.0).
_CONFIG_CHANNEL_LONG = ("--config", "--profile", "--cd", "--enable")
_CONFIG_CHANNEL_SHORT = ("-c", "-p", "-C")


# A `-c mcp_servers.<name>.enabled=false` disable, in either spelling the adapter can
# emit: a bare TOML key, or the quoted form used for names outside [A-Za-z0-9_-] (which
# makes codex refuse its config outright — fail closed — but still has to be parsed back
# out of argv here, or the post-run comparison would read it as "never disabled" and fail
# the run for the wrong reason).
_DISABLE_RE = re.compile(
    r"^mcp_servers\.(?:\"([^\"]+)\"|([A-Za-z0-9_-]+))\.enabled=false$")


def _disabled_server_names(argv: list[str]) -> set[str]:
    """The MCP server names an argv actually disables.

    Both `-c VALUE` and the attached `-cVALUE` spelling are read (codex accepts both,
    verified 0.140.0), as are `--config VALUE` and `--config=VALUE`. This adapter only
    ever emits the two-token `-c` form, but the comparison this feeds is a security check:
    reading fewer spellings than codex accepts would silently under-count what was
    disabled, and under-counting here means failing runs that were fine — or, if the
    asymmetry ever ran the other way, passing runs that were not.
    """
    names: set[str] = set()
    pending = False
    for tok in argv:
        value = None
        if pending:
            value, pending = tok, False
        elif tok in ("-c", "--config"):
            pending = True
            continue
        elif tok.startswith("-c") and len(tok) > 2:
            value = tok[2:]
        elif tok.startswith("--config="):
            value = tok[len("--config="):]
        if value is None:
            continue
        m = _DISABLE_RE.match(value)
        if m:
            names.add(m.group(1) or m.group(2))
    return names


def _mcp_tool_calls(stdout: str) -> list[str]:
    """MCP tool calls codex reported making, as ``server/tool``.

    Verified live against codex 0.140.0 with a sentinel stdio server: the item shape is
    ``{"type": "mcp_tool_call", "server": ..., "tool": ..., "status": ...}``, emitted on
    both ``item.started`` and ``item.completed``. Taken from the run's own stream, so
    unlike anything read off disk afterwards it cannot be retracted by a later edit.

    **Presence proves a leak; absence proves nothing.** codex emits no event when it
    *starts* an MCP server — the same live test showed it launching the sentinel and
    exchanging ``initialize``/``tools/list`` with it while the stream stayed silent — so a
    server that ran for the whole session but was never called is invisible here. This is
    strictly weaker than copilot's ``session.mcp_servers_loaded`` witness, and is why the
    re-enumeration half of ``verify_post_run`` carries the load rather than being a
    belt-and-braces addition.

    A call that FAILED still counts: the sentinel's call came back "user cancelled MCP
    tool call" (the approval policy refused it) and that is still proof the server was
    live and reachable. Never raises — malformed telemetry is not a leak report.
    """
    found: list[str] = []
    for obj in iter_jsonl(stdout):
        if not isinstance(obj, dict):
            continue
        item = obj.get("item")
        if not isinstance(item, dict):
            continue
        if (item.get("type") or item.get("item_type")) != "mcp_tool_call":
            continue
        label = f"{item.get('server') or '?'}/{item.get('tool') or '?'}"
        if label not in found:
            found.append(label)
    return found


def _config_channel_token(extra_args: list[str]) -> Optional[str]:
    """The first extra_args token that opens a codex configuration channel, or None.
    A token that merely LOOKS like one (e.g. a value following some unrelated flag) is
    reported too — that false positive fails closed, the safe direction."""
    for tok in extra_args:
        if any(tok == f or tok.startswith(f + "=") for f in _CONFIG_CHANNEL_LONG):
            return tok
        if any(tok.startswith(f) for f in _CONFIG_CHANNEL_SHORT):
            return tok
    return None


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

    def _probe_argv(self, model: str, *, cwd: Optional[str] = None,
                    env: Optional[dict] = None):
        return [self.binary, "--ask-for-approval", "never", "--sandbox", "read-only",
                "exec", "--ephemeral", "--disable", "memories",
                "--disable", "plugins",
                "-c", "memories.use_memories=false",
                "-c", "memories.generate_memories=false",
                *self._mcp_disable_args(cwd=cwd, env=env),
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

    def _mcp_disable_args(self, cwd: Optional[str] = None,
                          env: Optional[Mapping[str, str]] = None) -> list[str]:
        """`-c mcp_servers.<name>.enabled=false` for every server codex would actually
        load. Verified on 0.140.0: `-c mcp_servers={}` deep-merges with config.toml
        instead of replacing it (the server stays enabled), while the per-server `enabled`
        override does disable it — so hermeticity requires enumerating names. Enumeration
        must match codex's *effective* view exactly: disabling a name codex doesn't load
        creates an incomplete top-level entry and the run dies with "invalid transport"
        (verified) — and that effective view depends on the run's cwd and env, not the
        harness's: a *trusted* project's `.codex/config.toml` (found via the git root
        above cwd) contributes servers, and $CODEX_HOME moves the global config (verified
        0.140.0) — so callers pass the child's exact cwd/env (exec.execute() and
        probe_model() both do). Riding on argv (not the isolation overlay) makes this
        cover every invocation the same way: cells (isolated or not), model probes, and
        judge runs. A `-c enabled=false` (and `--disable plugins`) can still be OUTRANKED
        by a higher-precedence managed/MDM configuration layer, so once the overrides are
        built they are POST-VERIFIED against codex's own effective view — with the
        overrides applied, in the same cwd/env — and the run fails closed if any server is
        still enabled (see _verify_all_mcp_disabled)."""
        args: list[str] = []
        names = self._configured_mcp_server_names(cwd=cwd, env=env)
        for name in names:
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
        if names:
            # A managed/MDM config layer can outrank the -c overrides above (and
            # `--disable plugins`); the only trustworthy check is codex's own post-override
            # view, so confirm every server is actually disabled — fail closed otherwise.
            self._verify_all_mcp_disabled(args, cwd=cwd, env=env)
        return args

    def _verify_all_mcp_disabled(self, disable_args: list[str], cwd: Optional[str] = None,
                                 env: Optional[Mapping[str, str]] = None) -> None:
        """Re-run codex's own effective-config enumeration WITH the generated
        `-c ...enabled=false` overrides and `--disable plugins` applied, in the child's
        exact cwd and env, and confirm no server is still enabled. A higher-precedence
        managed/MDM configuration can override a `-c` value (and the plugins feature
        flag), so the pre-override enumeration alone doesn't prove hermeticity — only
        codex's post-override view does. Any server still reported enabled, or any failure
        to obtain that view (binary missing, non-zero exit, timeout, unrecognized shape),
        raises RuntimeError so the invocation fails closed rather than launching an
        un-disabled server."""
        try:
            r = subprocess.run(
                [self.binary, *disable_args, "--disable", "plugins",
                 "mcp", "list", "--json"],
                capture_output=True, text=True, encoding="utf-8", timeout=15,
                stdin=subprocess.DEVNULL, cwd=cwd,
                env=dict(env) if env is not None else None,
            )
            if r.returncode != 0:
                raise ValueError(f"`codex mcp list` exited with code {r.returncode}")
            data = json.loads(r.stdout)
        except (subprocess.TimeoutExpired, FileNotFoundError, NotADirectoryError,
                OSError, ValueError) as exc:
            raise RuntimeError(
                "codex could not be re-checked after applying the MCP kill-switch "
                f"overrides ({exc}) — failing closed rather than running without "
                "confirming every server is disabled."
            )
        still_enabled = _enabled_mcp_names(data)
        if still_enabled is None:
            raise RuntimeError(
                "codex's post-override `mcp list --json` returned an unrecognized shape — "
                "cannot confirm its MCP servers are disabled, failing closed."
            )
        if still_enabled:
            raise RuntimeError(
                "these MCP servers are still enabled after the kill-switch overrides "
                f"({', '.join(sorted(still_enabled))}) — a higher-precedence managed/MDM "
                "configuration is overriding `-c ...enabled=false`; failing closed rather "
                "than running with them live."
            )

    def _configured_mcp_server_names(self, cwd: Optional[str] = None,
                                     env: Optional[Mapping[str, str]] = None) -> list[str]:
        """Server names codex will load from *configuration* (its plugin channel is closed
        separately by `--disable plugins`, and plugin-provided servers must NOT be disabled
        by name — they have no config.toml entry, so a `-c` disable would create an
        incomplete one and break the run). The sole source is codex itself:
        `codex --disable plugins mcp list --json`, run with the child's exact cwd and env,
        which is the only view that matches codex's effective resolution (trusted-project
        configs, $CODEX_HOME, and the system/managed layers all included). There is no
        offline fallback and no caching: a hand-parsed subset of one config.toml misses
        the system/managed layers codex itself reads (so a later successful run could
        still launch an un-disabled server), and the effective view depends on the full
        execution context (resolved binary/PATH, git-root state, managed config) — more
        than any cache key could capture — so a stale entry could disable the wrong set.
        Any failure to positively enumerate therefore FAILS CLOSED (RuntimeError)."""
        names = self._mcp_names_via_cli(cwd=cwd, env=env)
        if names is None:
            raise RuntimeError(
                "codex could not enumerate its MCP servers "
                "(`codex --disable plugins mcp list --json` did not return a usable "
                "list) — failing closed rather than running without a verified server "
                "set (an offline config parse would miss the system/managed layers codex "
                "itself reads)."
            )
        return names

    def _mcp_names_via_cli(self, cwd: Optional[str] = None,
                           env: Optional[Mapping[str, str]] = None) -> Optional[list[str]]:
        """Ask codex itself which MCP servers its effective config defines — from the
        child's exact cwd and env, so trusted-project configs and $CODEX_HOME resolve the
        same way they will in the run. None (fall back) when the binary is missing,
        errors out, or answers in a shape we don't positively recognize."""
        try:
            r = subprocess.run(
                [self.binary, "--disable", "plugins", "mcp", "list", "--json"],
                capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL,
                cwd=cwd, env=dict(env) if env is not None else None,
            )
            if r.returncode != 0:
                return None
            data = json.loads(r.stdout)
        except (subprocess.TimeoutExpired, FileNotFoundError, NotADirectoryError,
                OSError, ValueError):
            return None
        return _validate_mcp_list_json(data)

    def build_argv(self, prompt: str, opts: RunOptions, *, cwd: str) -> list[str]:
        # extra_args ride at the END of argv (below), after the MCP kill-switch overrides
        # are built and POST-VERIFIED — a config-channel token there would outrank or
        # sidestep the verified MCP-off state (see _CONFIG_CHANNEL_LONG/_SHORT). Standard
        # runner cells never populate extra_args; a programmatic caller passing one of
        # these fails closed here (exec.execute() records a failed run), checked first so
        # a doomed invocation never spends the enumeration/post-verify subprocesses.
        bad = _config_channel_token(opts.extra_args)
        if bad is not None:
            raise RuntimeError(
                f"extra_args token {bad!r} opens a codex configuration channel "
                "(-c/--config, --profile/-p, --cd/-C, --enable) after the MCP "
                "kill-switch overrides were verified — failing closed rather than "
                "running with an unverifiable MCP state."
            )
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
                 # configured server is disabled by name instead, enumerated from the
                 # child's own cwd/env (opts.effective_env, set by exec.execute()) so
                 # trusted-project configs and env-overridden CODEX_HOMEs resolve
                 # exactly as they will in the run.
                 *self._mcp_disable_args(cwd=cwd, env=opts.effective_env),
                 "--json"]
        if opts.model:
            argv += ["-m", opts.model]
        if opts.reasoning_effort:
            # Quoted so the value part parses as a TOML string (see `-c` help text).
            argv += ["-c", f'model_reasoning_effort="{opts.reasoning_effort}"']
        argv += opts.extra_args
        argv += [prompt]  # prompt is positional and must come last
        return argv

    def verify_post_run(self, argv: list[str], opts: RunOptions, *, cwd: str,
                        stdout: str = "", stderr: str = "", exit_code: int = 0) -> None:
        """Narrow the launch window on codex's MCP kill switch, then record that this run's
        build is unidentifiable.

        NARROW, not close. **There is a residual case this cannot detect, by construction**
        — see "The hole that stays open" below. Read that before quoting this method as
        evidence a codex run was MCP-hermetic.

        The gap this exists for: everything codex verified before now ran from
        ``build_argv`` — ``_verify_all_mcp_disabled`` is a post-*enumeration*, pre-*launch*
        check, despite its name. Between building argv and codex actually reading its
        config at startup, a server added to config.toml (or to a trusted project's
        `.codex/config.toml`, or via `$CODEX_HOME`) is a server the launched
        ``-c ...enabled=false`` set does not name, so it loads live. copilot has closed
        this window since Phase 0; codex never did.

        Two checks:

        1. **Stream evidence** (survives a revert, presence-only). A file edit undone
           before exit re-reads as clean, so re-enumeration alone can be defeated by a
           change that does not outlive the run. Testimony the CLI already emitted cannot
           be retracted that way. codex's testimony is far weaker than copilot's: copilot
           emits ``session.mcp_servers_loaded`` naming every server it brought up, so a
           clean run is positively witnessed, whereas codex emits **nothing at all when a
           server starts** — verified live against a sentinel stdio server, which codex
           launched and handed ``initialize``/``tools/list`` without a single stream event.
           The only MCP trace is an ``mcp_tool_call`` item, which appears **only if the
           model actually called a tool**. Presence proves a leak; absence proves nothing.

        2. **Re-enumeration.** The whole disable set is rebuilt from codex's own effective
           view, in the child's exact cwd/env, and any server configured now that argv did
           not disable fails the run. Blind to a change reverted before it runs.

        **The hole that stays open.** These two do NOT cover each other's blind spots —
        that is true of copilot, and porting the claim here was wrong. A server added after
        argv was built, started by codex, and removed again before this method
        re-enumerates is invisible to *both* checks whenever the model never calls one of
        its tools: check 1 sees nothing because no tool call happened and startup emits no
        event, and check 2 sees the restored config. Since it is exactly the *idle* server
        that goes unseen, and since a server does not need to be called to act — the
        sentinel received ``initialize``/``tools/list`` and could have done anything at
        startup, running as the agent for the whole session — this residue is not a corner
        case that only matters to a pedant.

        What it would take to actually close it: give the child a *materialized* private
        config rather than one it shares with the host, so there is no file an outside
        writer can add a server to mid-run. That is a real change to codex's isolation
        story (``$CODEX_HOME`` currently mirrors the user's, which is what keeps auth
        working) and is deliberately not attempted here. Until then this is detection with
        a known blind spot, and ``DESIGN_MCP_Support.md`` §1 states the narrowed guarantee.

        Detection, not prevention, and the same live test shows why that is not a
        formality: ``--ask-for-approval never`` cancelled the sentinel's tool *call* but
        did nothing about its *startup* — the server process ran as the agent for the whole
        session. By the time either check fires, that has already happened; what is
        prevented is the result counting.
        """
        if opts is None:  # pragma: no cover — programming error, not a runtime state
            raise RuntimeError(
                "codex's post-run MCP re-check needs the RunOptions the invocation was "
                "built from: its enumeration only matches codex's effective view when it "
                "runs in the child's exact cwd and env (opts.effective_env). Failing "
                "closed rather than re-checking against the harness's own environment, "
                "which can resolve a different set of servers entirely."
            )
        called = _mcp_tool_calls(stdout)
        if called:
            raise RuntimeError(
                f"codex's own event stream reports MCP tool call(s) {', '.join(called)} "
                "during this run, but a hermetic invocation disables every configured "
                "server before launch. The config on disk may read clean now — a server "
                "added inside the launch window and removed before exit would — but the "
                "run itself was not MCP-hermetic, and this is testimony the CLI already "
                "emitted, which a later edit cannot retract."
            )
        try:
            after = _disabled_server_names(
                self._mcp_disable_args(cwd=cwd, env=opts.effective_env))
        except RuntimeError as exc:
            raise RuntimeError(
                "the invocation was built while MCP hermeticity was enforceable, but "
                f"re-checking after the run it no longer is: {exc}"
            ) from None
        new = sorted(after - _disabled_server_names(argv))
        if new:
            raise RuntimeError(
                f"MCP server(s) {', '.join(new)} are configured now but were not when the "
                "invocation was built, so the launched `-c mcp_servers.<name>.enabled="
                "false` set does not name them: codex reads its config at startup, after "
                "the harness enumerated, so a server added in that window loads "
                "un-disabled. This run is not provably MCP-hermetic."
            )
        _PROVENANCE.warn_drift(None)

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


def _validate_mcp_list_json(data: Any) -> Optional[list[str]]:
    """Strictly validate `codex mcp list --json` output (verified 0.140.0: a JSON array
    of objects, each with a string ``name``). Any other shape — ``null``, an object like
    ``{"servers": [...]}``, an entry without a usable name — returns None so the caller
    FAILS CLOSED instead of trusting schema drift as an authoritative "no servers"."""
    if not isinstance(data, list):
        return None
    names: set[str] = set()
    for s in data:
        if not isinstance(s, dict) or not isinstance(s.get("name"), str) or not s["name"]:
            return None
        names.add(s["name"])
    return sorted(names)


def _enabled_mcp_names(data: Any) -> Optional[set[str]]:
    """Names of servers codex still reports as ENABLED in `mcp list --json` output — an
    entry counts as enabled unless it carries ``"enabled": false`` (a disabled server is
    either omitted from the listing or present with that flag; either way it drops out).
    Returns None for an unrecognized shape (``null``, a wrapped object, an entry without a
    usable name) so the caller can fail closed — an empty set means every server is
    confirmed disabled."""
    if not isinstance(data, list):
        return None
    enabled: set[str] = set()
    for s in data:
        if not isinstance(s, dict) or not isinstance(s.get("name"), str) or not s["name"]:
            return None
        if s.get("enabled") is not False:
            enabled.add(s["name"])
    return enabled


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
