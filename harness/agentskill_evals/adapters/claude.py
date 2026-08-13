"""Claude Code adapter.

Invocation:
    claude -p "<prompt>" --output-format stream-json --verbose \
           [--dangerously-skip-permissions] [--model M] [--json-schema '<schema>']

`--verbose` is REQUIRED with stream-json in print mode. Output is JSONL of
Anthropic SDK message objects:

    {"type":"system","subtype":"init", ...}
    {"type":"assistant","message":{"content":[{"type":"text"...},
                                              {"type":"tool_use","name":"Bash",
                                               "input":{"command":"..."}}]}}
    {"type":"user","message":{"content":[{"type":"tool_result", ...}]}}
    {"type":"result","subtype":"success","result":"...","total_cost_usd":...,
     "duration_ms":..., "is_error":false}
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from .. import mcp_audit
from ..mcp import _NAME_RE
from ..notices import warn
from ..schema import EventKind, NormalizedEvent
from .base import (Adapter, MCPOffMechanism, ParseOutput, ProbeResult, RunOptions,
                   VersionProvenance, extract_command, extract_path, iter_jsonl, warn_unknown_usage)

# The proxy, as a path rather than a module name: it is spawned by the CLI, not imported here.
# Derived from this file's location so a harness running out of any checkout finds its own —
# hard-coding it would make a second checkout gate traffic through the first one's proxy.
_PROXY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mcp_proxy_io.py")

_FILE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "Create"}
_READ_TOOLS = {"Read", "View"}
_SHELL_TOOLS = {"Bash", "BashOutput"}
# Keys observed on the `result` event as of claude CLI mid-2026 (verified 2026-07-08 against
# a captured run). warn_unknown_usage compares ALL result keys, so an incomplete list here
# prints a spurious warning on EVERY run — keep this in sync when the CLI adds fields, and
# only then decide whether the new field carries billing data worth capturing.
_KNOWN_RESULT_KEYS = {
    "type", "subtype", "result", "is_error",
    "total_cost_usd", "duration_ms", "duration_api_ms", "structured_output",
    "session_id", "model", "modelUsage", "usage", "num_turns",
    "permission_denials", "stop_reason", "terminal_reason", "api_error_status",
    "fast_mode_state", "uuid",
    # 2.1.231 added three latency measures to every result event. They are recorded here
    # rather than left to warn on every run BECAUSE they were read: all three are timing
    # telemetry (time to first token, the same on the stream, and time to the request),
    # and none of them carries cost, usage, turn or error semantics this adapter reads.
    # That review is the whole purpose of `warn_unknown_usage` — the set exists to make a
    # shape change visible once, not to be silenced. Captured from a complete 2.1.231
    # result event, not from a truncated one: the earlier reading came off the tail of a
    # run and could not have seen a key that sorts before it.
    "ttft_ms", "ttft_stream_ms", "time_to_request_ms",
}


# --- CLI version provenance (see base.VersionProvenance) ---------------------------
#
# 2.1.113: verified 2026-07-21. Three things were actually checked, because a constant
#          that blesses an unknown state is worse than no constant:
#            * `--strict-mcp-config` still exists and still means what the argument here
#              rests on — `--help` describes it as "Only use MCP servers from
#              --mcp-config, ignoring all other MCP configurations". That single flag
#              carries this adapter's whole MCP argument, so it is the marker worth
#              auditing; there is no per-version channel inventory to go stale because
#              this adapter never enumerates channels.
#            * The witness below holds live: six captured 2.1.113 runs all report
#              `mcp_servers: []` in their init event.
#            * The parser contract: the `result` events of those same six runs carry no
#              key outside _KNOWN_RESULT_KEYS.
# 2.1.231: verified 2026-08-13, after the fleet was brought up to date. Four things were
#          checked, and one of them CHANGED — which is the reason this list is a list of
#          audited builds rather than a floor:
#            * `--allowedTools` still does nothing to MCP tools under
#              --dangerously-skip-permissions (§6-C2), measured against the echo fixture
#              with a CONTROL arm, because "the off-list tool never arrived" is equally
#              explained by a model that never tried. Both arms: `add` reached the server.
#            * `--strict-mcp-config` still excludes everything outside --mcp-config, and
#              this is now MEASURED rather than reviewed: without the flag the init event
#              named 7 servers (the declared one plus six real user connectors), with it
#              exactly 1. The witness text below was written for an EMPTY list, which
#              could not distinguish a working flag from a build that grew a source
#              outside its reach; a populated control arm can, for the sources it names.
#              It still cannot speak for a source that appears in NEITHER arm.
#            * The parser contract: a complete result event carries three keys 2.1.113
#              did not (`ttft_ms`, `ttft_stream_ms`, `time_to_request_ms`), all timing
#              telemetry, now in _KNOWN_RESULT_KEYS above with the reasoning.
#            * The MCP protocol era is unchanged (`2025-11-25`, legacy, decided by
#              `initialize`) — but the stdio SHUTDOWN changed: 2.1.113 closed stdin and
#              2.1.231 sends SIGINT (C3-1, DESIGN_MCP_Support.md §9). The proxy already
#              handles it, because #96 turned that probe into a requirement for SIGTERM
#              *and* SIGINT handlers rather than for the one behaviour it observed.
_VERIFIED_VERSIONS = ("2.1.113", "2.1.231")
_VERIFIED_ON = "2026-08-13"

# Builds found to actively break an assumption. Empty is the normal state.
_DENIED_VERSIONS: dict[str, str] = {}

_PROVENANCE = VersionProvenance(
    agent="claude",
    verified=_VERIFIED_VERSIONS,
    verified_on=_VERIFIED_ON,
    denied=_DENIED_VERSIONS,
    analysis="MCP hermeticity + parser analysis",
    witness_held=(
        "  The runtime witness held: the CLI reported an empty MCP server list, so "
        "--strict-mcp-config did what this adapter relies on it for. That does NOT cover "
        "a discovery channel the flag stopped governing: if a newer build grew a server "
        "source outside --mcp-config's reach, the list would be empty for the wrong "
        "reason and look identical here."),
    witness_absent=(
        "  This run reported no MCP server list at all — it did not complete far enough "
        "to emit its init event, which is allowed but proves nothing about its MCP host. "
        "So --strict-mcp-config was NOT confirmed effective here."),
    clear_hint=(
        "To clear it: confirm `claude --help` still documents --strict-mcp-config as "
        "ignoring all MCP configuration outside --mcp-config, then add the version to "
        "_VERIFIED_VERSIONS in adapters/claude.py."),
)


def _stream_cli_version(stdout: str) -> str | None:
    """The CLI version that actually EXECUTED, read out of the child's own stream.

    Claude states this directly: the ``system``/``init`` event carries
    ``claude_code_version`` as a first-class scalar the CLI writes about itself. That is
    the whole reason this is trustworthy — it needs no second execution, so it cannot
    disagree with what ran, unlike a preflight ``claude --version`` which resolves its own
    code path and can honestly report a build the real invocation never used.

    It is also structural, not prose: nothing a model emits and nothing the workspace
    contains reaches this field, so it cannot be forged by an assistant message or by a
    repo laid out to look like a version string. (Copilot has to reconstruct its version
    from skill paths and pays for that in care; here there is nothing to reconstruct.)

    EVERY init event is read, not just the first. Stopping at the first was a real defect
    (found in review): a stream carrying a second init event could state a different
    version, and taking the leading one would report whichever build the stream *opened*
    with rather than resolving the disagreement. Distinct versions therefore collapse to
    None — the same rule copilot applies to its app-root paths — because a stream that
    tells two stories about what ran has not established either.

    Returns None when the event is absent, malformed, or self-contradictory: all of them
    mean the version is unknown, which warns, and additionally FAILS the run when the
    adapter has a non-empty denylist that the unknown version cannot be excluded from
    (see VersionProvenance.check_denied). Must not raise: this runs inside verify_post_run,
    where anything raised is reported as an MCP hermeticity failure, and malformed
    telemetry is not one.
    """
    seen: set[str] = set()
    for obj in iter_jsonl(stdout):
        if not isinstance(obj, dict):
            continue
        if obj.get("type") != "system" or obj.get("subtype") != "init":
            continue
        version = obj.get("claude_code_version")
        if isinstance(version, str) and version:
            seen.add(version)
    return seen.pop() if len(seen) == 1 else None


def _mcp_witness(stdout: str,
                 exit_code: int) -> tuple[str | None, list[str], bool, dict]:
    """Check the run's own account of its MCP host.

    Returns (violation, live, witnessed, statuses).

    ``live`` is every server the run reports, DECLARED OR NOT — deciding which of those
    were supposed to be there belongs to the caller, which is the only layer that knows
    what the scenario asked for. Filtering here would make the witness an accomplice to
    the policy it exists to check.

    The init event lists ``mcp_servers``; under ``--strict-mcp-config`` with no
    ``--mcp-config`` passed, a hermetic run reports that list empty. Reading it from the
    run being judged is what makes this immune to the ABA problem that any
    inspect-the-disk-afterwards check has: a config planted inside the launch window and
    reverted before exit leaves the filesystem looking clean, but the CLI already loaded
    it and says so here.

    A run that did not complete normally is EXCUSED (witnessed=False) rather than failed:
    a crash before the init event is not evidence of a leak. That distinction is why
    `witnessed` is threaded into the drift warning — claiming the witness held on a run
    that never produced one would be the notice inventing its own evidence.

    EVERY init event is examined and their server lists are UNIONED, rather than trusting
    the first. Returning at the first one was a real defect (found in review): a stream
    whose opening init reported an empty list and whose second reported a live server
    passed verification, because the evidence that mattered arrived after the check had
    already made up its mind. An adapter that reads only the start of a stream can be
    told anything by the rest of it, so a server named anywhere counts as loaded, and a
    reshaped list anywhere is a violation.
    """
    violation: str | None = None
    live: list[str] = []
    statuses: dict[str, str | None] = {}
    witnessed = False
    for obj in iter_jsonl(stdout):
        if not isinstance(obj, dict):
            continue
        if obj.get("type") != "system" or obj.get("subtype") != "init":
            continue
        servers = obj.get("mcp_servers")
        if not isinstance(servers, list):
            # The field this contract is read from is gone or reshaped. On a run that
            # otherwise completed, that is a contract violation rather than a clean
            # result: "no servers found" and "the field moved" are indistinguishable
            # outcomes, and only one of them is safe.
            if violation is None:
                violation = "an init event carries no `mcp_servers` list"
            continue
        witnessed = True
        for s in servers:
            name = str(s.get("name") if isinstance(s, dict) else s)
            if name not in live:
                live.append(name)
            # `status` was being discarded, so a server reported `{"name": "echo",
            # "status": "failed"}` counted as successfully present and passed verification
            # without even the missing-server warning (found in review). Recorded per name,
            # strictest reading wins: once a server is seen in a non-connected state that
            # sticks, because a stream that reports the same server both ways has not
            # established that the scenario got the tool surface it asked for.
            status = s.get("status") if isinstance(s, dict) else None
            status = str(status) if status is not None else None
            if name not in statuses or statuses[name] == "connected":
                statuses[name] = status
    if violation is not None:
        # Report the violation, but hand back whatever servers WERE named: a stream that
        # both reshaped one event and loaded a server in another should not lose the
        # second fact to the first.
        return (violation, live, False, statuses)
    if witnessed:
        return (None, live, True, statuses)
    if exit_code == 0:
        return ("the run completed but emitted no system/init event", [], False, {})
    return (None, [], False, {})


def _witnessed_servers(stdout: str, exit_code: int) -> tuple | None:
    """The servers this run reported hosting, as sorted ``(name, status)`` pairs.

    ``None`` whenever the run did not actually witness its MCP host — it crashed before
    the init event, or the event was reshaped so the field could not be read. That is the
    whole point of the return type: `()` says "the run reported hosting nothing", None says
    "the run did not report", and a matrix may only be called comparable on the first.
    Collapsing them would let a crashed cell contribute agreement it never established,
    which is the same defect the tri-state `comparability` exists to prevent.

    Pairs rather than names because being NAMED is not being USABLE. A cell where `echo` is
    `connected` and a sibling where `echo` is `failed` ran against different tool surfaces,
    and a comparison over names alone would report that matrix as verified.

    A violation is deliberately not raised here: this runs on the reporting path (`parse`),
    where the rule is that malformed telemetry is an UNKNOWN rather than a failure. The
    identical evidence is read again by `verify_post_run`, which is the layer allowed to
    fail the run — and does, on exactly the reshaped event that makes this return None.
    """
    violation, live, witnessed, statuses = _mcp_witness(stdout, exit_code)
    if violation is not None or not witnessed:
        return None
    # Keyed on the NAME alone: a status is `str | None`, and sorting raw pairs would compare
    # those against each other the moment two names collided — `None < "connected"` is a
    # TypeError, i.e. a crash on the reporting path. `live` is deduped today so it cannot
    # happen, which is exactly the kind of guarantee that quietly stops holding.
    return tuple(sorted(((name, statuses.get(name)) for name in live), key=lambda p: p[0]))


class ClaudeAdapter(Adapter):
    name = "claude"
    binary = "claude"
    global_skills_subpaths = [".claude/skills"]
    # CLAUDE_CONFIG_DIR overrides ~/.claude (skills under it). Under isolation it's mirrored +
    # repointed (custom config dir kept, skills masked), else cleared to the isolated home.
    isolation_config_homes = [("CLAUDE_CONFIG_DIR", ".claude", "skills")]
    # Nothing. Measured against 2.1.113 on macOS, 2026-07-23: an empty HOME with only the
    # masked skills dir runs, emits its `system`/`init` event with `claude_code_version`
    # intact (so version provenance still reads the executing build), and answers.
    #
    # The reason it costs nothing is worth writing down, because it is macOS-specific and
    # will not hold for every adapter. claude's HOME-side auth is the login KEYCHAIN, at
    # ~/Library/Keychains — redirect HOME and it reports "Not logged in", and the only way to
    # symlink it back is an outward symlink, which a contained home cannot have. The keychain
    # is also uncopyable in practice: it is every password on the machine, and a copy is not
    # auto-unlocked, so a headless run would block on a password prompt. So auth arrives
    # instead as CLAUDE_CODE_OAUTH_TOKEN in the environment (verified: authenticates against
    # a wholly empty home), which the operator exports like any other harness credential and
    # which base.env() already passes through. The harness deliberately does NOT read the
    # keychain itself — acquiring that capability silently is not something a test harness
    # should do, and the token never touches disk this way.
    contained_home_subpaths: list[str] = []
    # The token that authenticates the contained (or any) HOME arrives here, and env() passes
    # it to the child. Register it for redaction so a run that echoes it — claude logging its
    # environment, a tool dumping env — cannot archive it verbatim. It is the credential the
    # contained-HOME design deliberately introduced into the child environment; leaving it out
    # of the scrub set would undo the containment the design bought.
    credential_env_vars = ["CLAUDE_CODE_OAUTH_TOKEN"]
    # And in a CONTAINED home it is the only route, which is the measured half of the comment
    # above rather than an inference from the empty surface: the keychain needs
    # ~/Library/Keychains, an outward symlink a contained home cannot have, and nothing else
    # under HOME carries a login. Confirmed from the failing direction on 2026-07-30 —
    # scenarios/mcp_echo_cred.yaml with the variable unset spends the cell and comes back
    # `exited with code 1` carrying "Not logged in · Please run /login"; with it set, the same
    # cell passes. That run is what this preflight was written from.
    contained_home_required_credential_env_vars = ["CLAUDE_CODE_OAUTH_TOKEN"]

    supports_output_schema = True
    # `--effort <level>` (verified 2026-07-08: choices low|medium|high|xhigh|max — the
    # harness only passes the typed cross-runner subset low|medium|high).
    supports_reasoning_effort = True

    # Declared servers ride in on `--mcp-config` (stdio shape verified live, 2.1.113).
    supports_mcp_injection = True
    # `--strict-mcp-config` ("Only use MCP servers from --mcp-config, ignoring all other MCP
    # configurations") is a CLI flag, so it holds whatever HOME the child is handed — this
    # adapter declares no config masks and needs none. A declared server set is therefore
    # still the whole server set under `isolated: false`, which is why such a run is allowed
    # rather than refused (see Adapter.mcp_off_mechanism).
    #
    # This is a REVIEWED ASSERTION about that flag, not a verified one. _PROVENANCE is an
    # audit trail plus a drift warning: a build outside _VERIFIED_VERSIONS warns and still
    # runs, and the witness text above says outright that an empty server list cannot
    # distinguish "the flag worked" from "a newer build grew a server source outside the
    # flag's reach". Nothing here would catch a wrong value; what the provenance machinery
    # buys is that an unaudited build is visible, and that re-establishing the claim has a
    # documented procedure (clear_hint).
    mcp_off_mechanism = MCPOffMechanism.CLI
    # Per-server `tools:` is enforced BY THE HARNESS, not by claude. No claude mechanism can
    # do it: `--allowedTools` does nothing to MCP tools under --dangerously-skip-permissions
    # (measured, DESIGN_MCP_Support.md §6-C2), and the only alternative — `--disallowedTools`
    # over the complement of the allowlist — needs the server's full tool list, knowable only
    # by starting the server and asking it, which is a SECOND instance free to answer
    # differently from the one claude launches. So the filter moved to where the traffic is:
    # `mcp_proxy_io.py` sits between the CLI and the declared server, and the CLI is handed
    # the proxy as if it were the server (§10). Nothing here trusts claude to filter anything.
    #
    # This field read `"unbuilt"` from #84 until the C3 adapter integration, and the validator
    # refused every gated server for that whole time — the honest state while the mechanism
    # the allowlist would need did not exist. Which is why `tool_filter_for` below exists: the
    # blanket refusal was also what kept REMOTE servers out, and flipping this constant took
    # that refusal away for a case the proxy still cannot serve.
    mcp_tool_filter = "proxy"

    def tool_filter_for(self, server: Any) -> tuple[str, str | None]:
        """`proxy`, but only for a server the proxy can actually be put in front of.

        `mcp_proxy_io.py` is spawned AS the server and speaks JSON-RPC over pipes in both
        directions; it holds no HTTP or SSE client, so a remote server gives it nothing to
        connect to. The transport bridge that would (stdio in, HTTP/SSE out) is the next piece
        of work and is not built — DESIGN §10's first cut is stdio only.

        Answering `proxy` here regardless would be this class asserting a filter that is not
        there, and the failure it produced was not even a loud one: the run reached a proxy
        config whose `command` was `null`, spending a model call to arrive at a missing audit
        log — a cell that fails for a reason bearing no resemblance to the cause (PR #107).
        """
        if not server.is_stdio:
            return "unbuilt", (
                "the harness's filtering proxy speaks stdio to the declared server and this "
                "one is remote (`url:`), so there is nothing for it to connect to — the "
                "transport bridge that would give it one is not built yet (DESIGN §10)")
        return self.mcp_tool_filter, None

    # TODO: Claude Code has no `list-models` command yet (feature request pending).
    # When one ships, add has_model_list = True and a discover_models() override
    # like Codex and AntiGravity have — then probing falls back to free discovery.

    # Hermetic flags — no memory, no hooks, no MCP, no saved sessions, no
    # user/project settings leaking in.  Avoids --bare because it also blocks
    # keychain/OAuth auth (requires ANTHROPIC_API_KEY).
    _HERMETIC = [
        "--no-session-persistence",
        "--strict-mcp-config",
        "--settings", '{"autoMemory": false, "hooks": {}}',
        "--setting-sources", "",
    ]

    def _probe_argv(self, model: str):
        return [self.binary, "-p", "say ok", *self._HERMETIC,
                "--output-format", "stream-json",
                "--verbose", "--model", model, "--dangerously-skip-permissions"]

    def _parse_probe_cost(self, output: str) -> ProbeResult:
        for line in output.splitlines():
            try:
                obj = json.loads(line.strip())
            except (json.JSONDecodeError, ValueError):
                continue
            if obj.get("type") == "result":
                cost = obj.get("total_cost_usd")
                return ProbeResult(accepted=True,
                                   cost_usd=float(cost) if cost is not None else None)
        return ProbeResult(accepted=True)

    def format_skill(self, skill: str) -> str:
        return f"/{skill}"

    def mcp_servers_seen(self, argv: list[str]) -> list[str] | None:
        """``[]`` — this run could not have had MCP servers — or None if argv stops saying so.

        Unlike codex and copilot, which neutralize servers by NAME and so report the disable
        set, claude's whole MCP argument is one flag: ``--strict-mcp-config`` restricts the
        run to servers from ``--mcp-config``, and this adapter never passes one. That is a
        POSITIVE claim about the configuration, not an absence of information, so it is
        reported as the empty set rather than as unknown — and the distinction decides
        whether a matrix reads `verified` or `unverified`, since an axis nobody can read
        must not be counted as agreement.

        Both halves are checked on the argv actually used, not assumed from build_argv:
        ``extra_args`` rides at the end verbatim, so a programmatic caller can append
        ``--mcp-config``. Then servers may exist and their names live in a JSON file this
        cannot resolve — unknown, so None. Same if ``--strict-mcp-config`` is gone.

        This is a comparability report, never a safety decision: hermeticity is decided by
        the run's own init event in `verify_post_run`, which sees servers from any source,
        including ones argv never mentions.
        """
        if "--strict-mcp-config" not in argv:
            return None
        if any(a == "--mcp-config" or a.startswith("--mcp-config=") for a in argv):
            return None
        return []

    def _write_mcp_config(self, opts: RunOptions) -> str:
        """Materialize `<scratch>/mcp.json` and return its path.

        A FILE, not `--mcp-config '<inline json>'`: argv is archived verbatim into
        result.json, so an inline config would publish every resolved credential into the
        artifacts. The file lives in the runner's per-cell scratch dir — outside the
        workspace, which is archived and inlined into report.md — and is created 0600 so it
        is not readable by other users for the seconds it exists.

        Written on every build_argv call rather than cached, because build_argv is the only
        hook that runs after the runner has created the scratch dir and before the child
        starts, and a stale file from a previous cell would silently outrank the current
        scenario's servers.
        """
        if not opts.mcp_scratch_dir:
            raise RuntimeError(
                "claude: mcp_servers were declared but no scratch dir was provided — "
                "refusing to write MCP config with resolved secrets into the workspace, "
                "which is archived into artifacts and inlined into report.md.")
        servers: dict[str, Any] = {}
        for name, s in opts.mcp_servers.items():
            if s.tools is not None:
                # GATED: claude is handed the PROXY as if it were the server, and never learns
                # the real command. The allowlist is applied to the wire by a program this
                # harness owns (§10), which is the only arrangement in which "the agent could
                # only call these tools" is a statement about what happened rather than about
                # what a CLI flag was asked to do.
                entry = self._write_proxy_config(name, s, opts.mcp_scratch_dir)
            elif s.is_stdio:
                entry = {"command": s.command}
                if s.args:
                    entry["args"] = list(s.args)
                if s.env:
                    entry["env"] = dict(s.env)
            else:
                # `type` is claude's transport discriminator; `http` and `sse` are the two
                # documented values (§2 — verified live against fixtures/http_mcp_server.py
                # by §9 probe #1: the shape is accepted and declared headers arrive).
                entry = {"type": s.transport, "url": s.url}
                if s.headers:
                    entry["headers"] = dict(s.headers)
            servers[name] = entry

        path = os.path.join(opts.mcp_scratch_dir, "mcp.json")
        # Create with 0600 from the start — writing then chmod'ing would leave a window
        # where the credentials are world-readable.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"mcpServers": servers}, fh)
        return path

    @staticmethod
    def audit_log_path(scratch_dir: str, name: str) -> str:
        """Where the proxy for `name` writes its audit log, and where `verify_post_run` reads
        it. ONE function, because a writer and a reader that each build the path themselves
        agree until one of them is edited — and a log the reader cannot find is `no_instances`,
        which is a FAILED cell reported as an unproven one (§4)."""
        return os.path.join(scratch_dir, f"mcp-audit-{name}.jsonl")

    def _write_proxy_config(self, name: str, s: Any, scratch_dir: str) -> dict[str, Any]:
        """Materialize the proxy's own config for one gated server; return the `mcpServers`
        entry that launches it.

        THE CREDENTIALS MOVE OUT OF `mcp.json` AND INTO THIS FILE, which is the same class of
        secret in the same 0600 scratch dir — but the entry claude reads now names only two
        paths, so a gated server's interpolated `env` is one file further from anything the
        CLI echoes back. Both files die with the scratch dir.

        THE SERVER NAME BECOMES PART OF A FILENAME, so it is checked against the schema's own
        regex rather than trusted. `mcp._NAME_RE` is IMPORTED, not restated: a copy would be a
        second opinion about what a name may contain, and the one that matters is the one the
        parser enforced (§4). Today it admits no separator and no `..`, so this cannot escape
        the scratch dir — the check is what keeps that true if the schema ever widens.

        THE TRANSPORT IS RE-ASSERTED HERE, not assumed from `validate_mcp_support` having run.
        That validator is the friendly refusal, reached before tokens are spent and skippable
        by any caller that builds argv directly; this is the layer nothing routes around. What
        it prevents is specific and was observed: a remote server produced a config with
        `"command": null` and no trace of the `url` and `headers` it was actually declared
        with, which is a launch failure two processes away from anything naming the cause.
        """
        if not s.is_stdio:
            raise RuntimeError(
                f"claude: MCP server {name!r} sets `tools:` but is remote (`url:`), and the "
                "filtering proxy speaks stdio — there is nothing for it to connect to. "
                "`validate_mcp_support` refuses this before a run starts, so reaching here "
                "means validation was skipped. Refusing rather than writing a proxy config "
                "with no command, which drops the declared url and headers and fails at "
                "launch instead of here.")
        if not _NAME_RE.match(name):
            raise RuntimeError(
                f"claude: MCP server name {name!r} is not one the schema admits, and it is "
                "about to become a filename in the scratch dir. Refusing rather than writing "
                "a path this name could have chosen.")
        cfg_path = os.path.join(scratch_dir, f"mcp-proxy-{name}.json")
        config = {
            "server": name,
            "command": s.command,
            "args": list(s.args or ()),
            "env": dict(s.env or {}),
            "tools": sorted(s.tools),
            "audit_log": self.audit_log_path(scratch_dir, name),
        }
        # 0600 from the start, for the reason `mcp.json` is: this one carries the interpolated
        # `env`, so a writable-then-chmod'ed file has a window where it is world-readable.
        fd = os.open(cfg_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(config, fh)
        # `sys.executable`, not "python": the proxy is part of this harness and must run on
        # the interpreter the harness is running on, whatever venv that is. It is spawned as a
        # plain script rather than `-m`, which is what `mcp_proxy_io.py`'s own sys.path
        # bootstrap is for and what `tools/verify_mcp_proxy.py` already drives it as.
        return {"command": sys.executable, "args": [_PROXY_PATH, cfg_path]}

    def gating_failure(self, opts: RunOptions) -> str | None:
        """The reason this run's tool gating cannot be called clean, or None.

        A FUNCTION, not an `if` inside `verify_post_run`, so it can be driven on a written log
        rather than only by a live claude — the same argument §4 makes about a probe's
        classifier. `log_verdict` never raises, and neither does this: a traceback out of
        `verify_post_run` is not a failed cell, it is an ABSENT verdict, which is the outcome
        the whole audit exists to make impossible.

        FAIL-CLOSED ON A MISSING LOG, and that is the case worth stating. A gated server whose
        proxy wrote nothing means the gating never happened — indistinguishable, from here,
        from a proxy that was never started at all. `log_verdict` reports it as `no_instances`
        rather than as a vacuous pass for exactly that reason (§10.5.1), and this must not
        soften it back into one.
        """
        scratch = getattr(opts, "mcp_scratch_dir", None)
        gated = {n: s for n, s in (getattr(opts, "mcp_servers", None) or {}).items()
                 if getattr(s, "tools", None) is not None}
        if not gated:
            return None
        if not scratch:
            return ("claude: MCP server(s) " + ", ".join(sorted(gated)) + " declared `tools:`, "
                    "but this run has no scratch dir, so the proxy's audit log cannot be "
                    "found. The allowlist cannot be shown to have applied; failing closed.")
        for name in sorted(gated):
            try:
                with open(self.audit_log_path(scratch, name), encoding="utf-8") as handle:
                    text = handle.read()
            except OSError:
                text = ""          # judged as `no_instances` below, not excused
            verdict = mcp_audit.log_verdict(
                text, server=name, allowed=frozenset(gated[name].tools))
            if not verdict.clean:
                detail = ", ".join(verdict.problems) or ", ".join(
                    str(v.anomalous or v.problems) for v in verdict.unclean)
                return (f"claude: the tool gating for MCP server {name!r} did not hold: "
                        f"{detail}. The proxy is what applies `tools:`, so a run whose audit "
                        "log is missing, unreadable or anomalous is a run whose allowlist "
                        "cannot be shown to have been enforced — which is a failed cell, not "
                        "a warning. If this cell also failed for another reason, that reason "
                        "came first and this check is reporting only what the log can show.")
        return None

    def validate_mcp_support(self, mcp_servers: dict) -> tuple[list[str], list[str]]:
        errors, warnings = super().validate_mcp_support(mcp_servers)
        # Point at the reason and the escape hatch rather than the internal state name —
        # "unbuilt" tells the scenario author nothing actionable.
        errors = [e + (" (claude's only MCP tool filter is deny-the-complement, which "
                       "needs a tool list this harness cannot obtain without a second, "
                       "independently answerable server instance — see C3 in "
                       "DESIGN_MCP_Support.md)"
                       if "tools:" in e and "not implemented" in e else "")
                  for e in errors]
        return errors, warnings

    def build_argv(self, prompt: str, opts: RunOptions, *, cwd: str) -> list[str]:
        argv = [
            self.binary,
            "-p",
            prompt,
            *self._HERMETIC,
            "--output-format",
            "stream-json",
            "--verbose",  # mandatory with stream-json in -p
        ]
        if opts.auto_approve:
            argv += ["--dangerously-skip-permissions"]
        if opts.model:
            argv += ["--model", opts.model]
        if opts.reasoning_effort:
            argv += ["--effort", opts.reasoning_effort]
        if opts.output_schema:
            argv += ["--json-schema", json.dumps(opts.output_schema)]
        if opts.allowed_tools:
            argv += ["--allowedTools", ",".join(opts.allowed_tools)]
        if opts.disable_tools:
            argv += ["--tools", ""]  # reasoning-only (judge mode)
        if opts.mcp_servers:
            # --strict-mcp-config is already in _HERMETIC, so these become the ONLY servers
            # the run can reach — the opt-in is hermetic for free (§5.1).
            argv += ["--mcp-config", self._write_mcp_config(opts)]
        argv += opts.extra_args
        return argv

    def verify_post_run(self, argv: list[str], opts: RunOptions, *, cwd: str,
                        stdout: str = "", stderr: str = "", exit_code: int = 0) -> None:
        """Confirm from the run's own stream that it was MCP-hermetic, and record which
        build produced that evidence.

        Ordered the same way as copilot's, and for the same reasons. The denylist is
        REPORTED first because it covers exactly what the runtime evidence cannot: a
        defect that leaves the witness perfectly intact fires no runtime check at all, so
        a denial must not be masked by a contract failure found on the same run. The drift
        warning runs LAST, only on a run that cleared every gate, so a genuine hermeticity
        failure is never buried under a version notice.

        The witness is COMPUTED before the denylist check but RAISED after it. That split
        looks fussy and is load-bearing: check_denied needs to know whether the run got far
        enough to be judged (an unknown version fails closed on a completed run once
        anything is denylisted, but must not on a crash), while the reporting order above
        still has to put a denial ahead of a contract failure.
        """
        version = _stream_cli_version(stdout)
        broken, live, witnessed, statuses = _mcp_witness(stdout, exit_code)
        _PROVENANCE.check_denied(version, completed=witnessed)
        if broken is not None:
            raise RuntimeError(
                f"claude's MCP witness does not hold: {broken}. The run finished normally, "
                "and that stream is where the ABA-immune half of this audit gets its "
                "evidence — a hermetic run on a build this adapter understands always "
                "reports its MCP server list. A witness that is missing or reshaped yields "
                "'no servers found', which reads exactly like a clean run, so it is "
                "refused instead: the run's hermeticity is unwitnessed rather than "
                "confirmed; failing closed."
            )
        # `opts` is None on direct calls (selftest, out-of-tree callers). Absent options
        # mean nothing was declared, which is the STRICT reading: every reported server is
        # then undeclared and fails the run. Defaulting the other way would let a missing
        # argument silently permit any server at all.
        declared = set(getattr(opts, "mcp_servers", None) or {})
        undeclared = sorted(s for s in live if s not in declared)
        if undeclared:
            expected = (f"only the declared server(s) {', '.join(sorted(declared))}"
                        if declared else
                        "that list empty, since --strict-mcp-config was passed with no "
                        "--mcp-config")
            raise RuntimeError(
                f"claude reports MCP server(s) {', '.join(undeclared)} loaded during this "
                f"run, but this invocation should have had {expected}. Either "
                "--strict-mcp-config no longer governs every server source, or something "
                "in this invocation supplied one. The state on disk may read clean now — a "
                "config planted inside the launch window and reverted before exit would — "
                "but the run itself was not MCP-hermetic."
            )
        # THE GATING, judged from the proxy's own log. Placed after the undeclared-server
        # check and before the warnings, because the two raises answer different questions and
        # one is broader: `undeclared` asks whether something got IN that was never declared,
        # this asks whether what was declared stayed inside its allowlist. A leak is the wider
        # failure and is reported first. Both are raises rather than warnings — an allowlist
        # that cannot be shown to have applied is the case `tools:` exists to prevent.
        gating = self.gating_failure(opts)
        if gating:
            raise RuntimeError(gating)
        # A DECLARED server that is missing from the witness is not a hermeticity failure
        # (nothing leaked) and is not silently fine either: the scenario asked for a tool
        # surface it did not get, so assertions about it will fail confusingly. Surfaced as
        # a warning rather than a raise, because verify_post_run's raises all mean "this
        # run was not hermetic" and widening that would blur what a failure here means.
        missing = sorted(declared - set(live))
        if missing and witnessed:
            warn(f"warning: [claude] declared MCP server(s) {', '.join(missing)} were not "
                 "reported by the run — the scenario ran without them; check the server "
                 "command and its startup output.")
        # Being NAMED in the witness is not the same as being usable. A server reported
        # `{"name": "echo", "status": "failed"}` used to clear this check silently — it was
        # present, so it was not "missing", and its status was discarded. That is the same
        # confusing outcome as a missing server (assertions about tools that never existed)
        # and it gets the same warning. Unknown states warn too rather than being assumed
        # good: a status this adapter does not recognise is not evidence of health.
        #
        # Through `warn`, not `print`. The message's own claim — that assertions will fail
        # "for a reason the results will not show" — was true of the message as well when it
        # went only to the harness process's stderr, which nothing archives. It now lands on
        # RunResult.warnings, so the cell that fails confusingly carries its own explanation.
        unhealthy = sorted(
            (name, statuses.get(name)) for name in declared & set(live)
            if statuses.get(name) != "connected"
        )
        if unhealthy and witnessed:
            detail = ", ".join(f"{n} ({s or 'no status reported'})" for n, s in unhealthy)
            warn(f"warning: [claude] declared MCP server(s) {detail} were reported by the "
                 "run but not as connected — their tools were most likely unavailable, so "
                 "assertions about them will fail for a reason the results would otherwise "
                 "not show.")
        _PROVENANCE.warn_drift(version, witnessed=witnessed)

    def parse(self, stdout: str, stderr: str, exit_code: int,
               *, opts: RunOptions | None = None) -> ParseOutput:
        events: list[NormalizedEvent] = []
        final_text = ""
        structured: Any = None
        cost = None
        dur = None
        resolved_model: str | None = None
        last_assistant_text = ""

        for obj in iter_jsonl(stdout):
            etype = obj.get("type")

            if etype == "system" and obj.get("subtype") == "init":
                resolved_model = obj.get("model") or resolved_model
                events.append(NormalizedEvent(EventKind.SESSION_START, raw=obj))

            elif etype == "assistant":
                content = (obj.get("message") or {}).get("content") or []
                for block in content:
                    btype = block.get("type")
                    if btype == "text":
                        txt = block.get("text", "")
                        last_assistant_text = txt
                        events.append(
                            NormalizedEvent(EventKind.AGENT_MESSAGE, raw=block, text=txt)
                        )
                    elif btype == "tool_use":
                        name = block.get("name")
                        inp = block.get("input") or {}
                        # StructuredOutput is the --json-schema delivery mechanism,
                        # not a real tool the skill invoked: capture it, don't trace it.
                        if name == "StructuredOutput":
                            structured = inp
                            continue
                        cmd = None
                        path = None
                        if name == "Skill":
                            skill_name = inp.get("skill") or ""
                            if skill_name:
                                path = f"{self.skills_subdir}/{skill_name}/SKILL.md"
                        elif name in _SHELL_TOOLS:
                            cmd = extract_command(inp)
                        elif name in (_FILE_TOOLS | _READ_TOOLS):
                            path = extract_path(inp)
                        else:
                            # Glob/Grep/LS/WebFetch/etc. all take a `path`-shaped argument
                            # for an arbitrary absolute location, not just cwd — leaving
                            # these unhandled would silently drop that leak signal from
                            # leaked_skill_reads() (see workspace_view.py).
                            cmd = extract_command(inp)
                            if not cmd:
                                path = extract_path(inp)
                        events.append(
                            NormalizedEvent(
                                EventKind.TOOL_CALL,
                                raw=block,
                                tool_name=name,
                                command=cmd,
                                path=path,
                            )
                        )

            elif etype == "user":
                content = (obj.get("message") or {}).get("content") or []
                for block in content:
                    if block.get("type") == "tool_result":
                        result_content = block.get("content")
                        text = ""
                        if isinstance(result_content, str):
                            text = result_content
                        elif isinstance(result_content, list):
                            text = "\n".join(
                                p.get("text", "") for p in result_content
                                if isinstance(p, dict) and p.get("type") == "text"
                            )
                        events.append(
                            NormalizedEvent(
                                EventKind.TOOL_RESULT,
                                raw=block,
                                text=text,
                                is_error=bool(block.get("is_error")),
                            )
                        )

            elif etype == "result":
                warn_unknown_usage("claude", obj, _KNOWN_RESULT_KEYS)
                result_text = obj.get("result")
                if isinstance(result_text, str):
                    final_text = result_text
                cost = obj.get("total_cost_usd", cost)
                dur = obj.get("duration_ms", dur)
                resolved_model = obj.get("model") or resolved_model
                # With --json-schema, the validated object is delivered in a
                # dedicated `structured_output` field (the `result` string is
                # just the assistant's closing text). Fall back to parsing the
                # result string only if that field is absent.
                if obj.get("structured_output") is not None:
                    structured = obj["structured_output"]
                elif structured is None and isinstance(result_text, str):
                    try:
                        structured = json.loads(result_text)
                    except (json.JSONDecodeError, ValueError):
                        structured = None
                events.append(
                    NormalizedEvent(
                        EventKind.RESULT,
                        raw=obj,
                        text=final_text,
                        is_error=bool(obj.get("is_error")),
                    )
                )

        if not final_text:
            final_text = last_assistant_text

        return ParseOutput(
            events=events,
            final_text=final_text,
            structured_output=structured,
            cost_usd=cost,
            duration_ms=dur,
            resolved_model=resolved_model,
            # Same reader the hermeticity check uses, so the recorded build is the one the
            # verification was reasoning about rather than a second, possibly different,
            # determination of it.
            cli_version=_stream_cli_version(stdout),
            # Read from the SAME init event and the same helper as the kill-switch witness,
            # for the same reason as cli_version above: what gets reported is the thing the
            # verification reasoned about, not a second determination of it that could
            # disagree. Comparability only — `verify_post_run` remains the sole consumer
            # that can fail a run on this evidence.
            mcp_servers_witnessed=_witnessed_servers(stdout, exit_code),
        )
