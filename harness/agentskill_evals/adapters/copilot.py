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

VERSION PROVENANCE. The findings recorded throughout this module are dated to the build
they were established on — most say "1.0.64", the build the original analysis read
(installed CLI + its app.js bundle, 2026-07-17). The authoritative, queryable record is
``_VERIFIED_VERSIONS``, currently ``1.0.64`` and ``1.0.72``; a run on anything else warns
(see ``_check_cli_version``) and a build known to be broken fails closed by name.

Do not read a "1.0.64" comment as "this is the shipping version" — the CLI updates itself
by rewriting its executable IN PLACE, and this module was running against 1.0.72 four days
after being verified against 1.0.64, with nothing in the code aware of the gap. That is
the failure this provenance block exists to prevent.
"""

from __future__ import annotations

import json
import ntpath
import os
import re
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

# --- Custom agents: an MCP channel --disable-mcp-server cannot be AIMED at ------------
#
# copilot 1.0.64 discovers custom-agent definitions (markdown with frontmatter) from
# four sources: <config home>/agents, the .github/agents and .claude/agents convention
# dirs walked UP from the working directory to a boundary that is the git root when git
# discovery finds a repo and the OS home ONLY when it does not (an either/or, not the
# nearer of the two — read out of the 1.0.64 bundle: `boundary = found ? gitRoot :
# os.homedir()`, so a home NESTED IN a repo is walked straight past), installed
# plugins, and — when the working directory sits in a git repo with a GitHub remote
# and auth is available — a REMOTE org/enterprise listing fetched from the Copilot API. Local files
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
# _mcp_disable_args on what it can't: any discoverable local agent file, and any run
# whose enumerable config does not provably opt out of remote discovery.
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


def _custom_agent_files(home: str, cwd: Optional[str]) -> list[str]:
    """Every LOCAL custom-agent definition file discoverable for a run:
    ``<home>/agents`` plus the ``.github/agents`` / ``.claude/agents`` convention
    dirs of the PHYSICAL run cwd (``os.path.realpath`` — a cwd symlinked into a tree
    finds the dirs copilot's own physical walk would) and EVERY ancestor up to the
    filesystem root. No boundary is applied, because none can be computed soundly.

    copilot stops that walk at a boundary its own loader picks per run: ``boundary =
    gitDiscovery(cwd).found ? gitRoot : os.homedir()`` (1.0.64 bundle). Neither arm is
    usable here.

    * The git root is unusable because learning it means EXECUTING git, and copilot
      executes git AGAIN when it launches — two independent executions that a stateful
      or slow wrapper, a different resolved binary, or a differing timeout can answer
      differently. Narrowing a security scan on the first execution's answer would let
      copilot walk FARTHER than the harness looked and start local agent MCP servers
      the harness never saw. This is the same two-execution gap the Windows ODR gate
      refuses to bet on (_assert_odr_gate_off), and it gets the same answer: don't bet.
    * The OS home is unusable because the boundary is an EITHER/OR, not the nearer of
      the two. When the child's home sits INSIDE a repository — ``HOME=/repo/home`` with
      cwd ``/repo/home/ws`` — copilot's boundary is ``/repo`` and its walk goes straight
      past the home to read ``/repo/.github/agents``. Stopping the harness at the home
      would miss exactly that file, and ``defaultLocalOnly`` does not save the run: it
      suppresses only REMOTE agents, so the missed local agent still brings its own
      mcp-servers up. Deciding whether the home is the real boundary means deciding
      whether git discovery succeeds — the execution above, again.

    So the walk runs to the root. The cost is over-detection: a ``.github/agents`` above
    the child's home, which copilot would never read, fails runs closed. That is the
    direction this scan is required to err in — unlike the workspace MCP-config walk,
    where an over-approximation just adds harmless disables, a MISSED agent file is a
    silent MCP leak."""
    dirs = [os.path.join(home, "agents")]
    if cwd:
        d = os.path.realpath(cwd)
        while True:
            for rel in _WORKSPACE_AGENT_DIRS:
                dirs.append(os.path.join(d, *rel.split("/")))
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
    files: list[str] = []
    for dpath in dirs:
        files.extend(_agent_definition_files(dpath))
    return files


def _disabled_server_names(argv: list[str]) -> set[str]:
    """The MCP server names an argv actually disables. Both spellings are read
    (``--disable-mcp-server x`` and ``--disable-mcp-server=x``): the harness only emits
    the two-token form, but this also parses argv it did not build (verify_post_run
    re-reads the argv that was launched)."""
    names: set[str] = set()
    for i, tok in enumerate(argv):
        if tok == "--disable-mcp-server" and i + 1 < len(argv):
            names.add(argv[i + 1])
        elif tok.startswith("--disable-mcp-server="):
            names.add(tok.split("=", 1)[1])
    return names


# MCP host statuses that mean copilot did NOT bring the server up. 1.0.64's enum is
# connected | failed | needs-auth | pending | disabled | not_configured; everything
# outside this set counts as brought-up, including `failed` (a stdio server that failed
# to initialize still had its command spawned) and any status a later version invents.
_INERT_MCP_STATUSES = {"disabled", "not_configured"}


# --- Version provenance ---------------------------------------------------------------
#
# The CLI builds this adapter's MCP analysis has actually been checked against, as DATA
# rather than the prose scattered through this module. The prose comments below record
# individual findings and stay dated to the build they were made on; this constant is the
# single queryable source of truth, the one the drift warning cites, and the one to update
# when a new build is cleared.
#
# It exists because provenance kept in prose rots silently: this module was written and
# verified against 1.0.64 on 2026-07-17 and, four days later, was running against 1.0.72 —
# copilot's updater having rewritten the executable in place, which is the very mechanism
# _mcp_witness documents as making the version unpinnable. Nobody was warned, because
# nothing in the code knew what version it had been verified against.
#
# 1.0.64: original analysis (installed CLI + its app.js bundle).
# 1.0.72: re-verified 2026-07-21 — the witness contract confirmed live (a real run emits
#         session.mcp_servers_loaded naming github-mcp-server as `disabled`), and every
#         channel marker in _MCP_CHANNEL_MARKERS confirmed still present in the bundle.
_VERIFIED_VERSIONS = ("1.0.64", "1.0.72")
_VERIFIED_ON = "2026-07-21"

# Builds found to actively BREAK a hermeticity assumption, mapped to what broke. Empty is
# the normal state. This is the tier the runtime contract cannot cover: a build that
# breaks, say, plugin masking leaves the MCP witness perfectly intact, so no runtime check
# fires — the failure has to be recorded here once a human finds it, and then it fails
# closed by name instead of waiting for a violation that never comes.
_DENIED_VERSIONS: dict[str, str] = {}

# The MCP/config discovery channels this adapter neutralizes, each with a string that must
# be present in the CLI bundle for the corresponding defence to still mean anything. This
# is the inventory `verify-copilot-channels` audits a new build against; a marker that has
# GONE means the assumption behind that defence no longer holds and the finding needs
# re-establishing. See cmd_verify_copilot_channels for the (important) limits of that
# audit — above all that it cannot detect a channel a new build ADDED.
_MCP_CHANNEL_MARKERS: tuple[tuple[str, str], ...] = (
    ("github-mcp-server", "the built-in server, and the witness sentinel"),
    ("playwright", "feature-gated built-in server disabled by name"),
    ("bluebird", "feature-gated built-in server disabled by name"),
    ("computer-use", "staff-gated built-in server disabled by name"),
    ("defaultLocalOnly", "the ONLY off-switch for remote custom-agent discovery"),
    (".github/mcp.json", "workspace MCP config candidate"),
    ("mcp-servers", "custom-agent frontmatter key that declares MCP servers"),
    ("enabledMcpServers", "config key that switches on feature-gated built-ins"),
    ("installedPlugins", "config key whose cache_path loads plugins past the mask"),
    ("session.mcp_servers_loaded", "the MCP witness event the post-run audit reads"),
    ("not_configured", "inert-status value the witness classifies against"),
)

# A version directory: `1.0.72`, or a prerelease like `1.0.73-beta.1`. The suffix is
# matched rather than rejected so a prerelease build is REPORTED as the thing it is —
# `1.0.73-beta.1` is not in _VERIFIED_VERSIONS and warns, whereas failing to match at all
# reads as "no version found" and warns about the wrong thing (or, in the audit below,
# silently skips a bundle that the loader would happily run).
_VERSION_DIR = r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z][0-9A-Za-z.+-]*)?"

# An app root is `<cache>/pkg/<platform>/<version>/`, and the built-in skills live
# directly under it in `builtin/` — verified against a captured 1.0.72 run:
# `~/Library/Caches/copilot/pkg/darwin-arm64/1.0.72/builtin/<name>/SKILL.md`.
#
# The `builtin/` is part of the pattern, not decoration. Without it this matches any
# version-shaped `pkg/*/` component ANYWHERE in the path, and the first such component
# is not necessarily the app root: a cache root that itself sits under a path of that
# shape (`$COPILOT_CACHE_HOME` is an ordinary directory anyone can place) puts a decoy
# ahead of the real one, and the reported version is then the outer directory's name
# rather than the code that ran — a build could report itself as a VERIFIED version that
# way. Anchoring on the segment the CLI's own layout guarantees pins the match to the
# real root; ``finditer``'s last hit is taken as well, so the deepest such root wins even
# if the anchor itself were ever nested.
_APP_ROOT_VERSION_RE = re.compile(
    r"[/\\]pkg[/\\][^/\\]+[/\\](" + _VERSION_DIR + r")[/\\]builtin[/\\]")


def _app_root_version(path: str) -> Optional[str]:
    """The app-root version *path* was emitted out of, or None if it names no app root."""
    last = None
    for m in _APP_ROOT_VERSION_RE.finditer(path):
        last = m.group(1)
    return last


def _stream_cli_version(stdout: str) -> Optional[str]:
    """The CLI version that actually EXECUTED, read out of the child's own stream.

    Same epistemics as the MCP witness, and for the same reason: the evidence comes from
    the run being judged, so it needs no second execution and cannot disagree with what
    ran. A preflight ``copilot --version`` can — it resolves its app.js by scanning
    writable cache roots that this adapter's ``--no-auto-update`` argv deliberately
    bypasses, so the probe and the run can execute different code (see _mcp_witness).

    Read ONLY from ``source == "builtin"`` skill paths in ``session.skills_loaded``, never
    by scanning stdout at large and never from the other skill sources. Built-in skill
    paths are structural data the CLI emits about its own app root. The rest of the same
    stream is not: assistant message content is model-controlled, so a broad regex would
    let a model's prose ("I am running pkg/darwin-arm64/9.9.9/") forge the version that
    silences the drift warning — and the SAME event also lists ``source: "project"``
    skills whose paths are workspace-controlled (verified live: they arrive as
    ``<workspace>/.agents/skills/<name>/SKILL.md``). A repo laid out as
    ``.../pkg/x/9.9.9/SKILL.md`` would inject a second, bogus version, and because
    disagreement resolves to None, that alone would silently disarm the denylist. The
    workspace does not get a vote on which build the harness thinks it ran.

    None when no app-root path appears, when paths disagree, or when the telemetry is
    malformed — all of them mean the version is unknown, which warns rather than fails
    (absence of evidence about the version is not evidence of a leak; the contract check
    is what actually gates). Malformed telemetry must reach that same warning rather than
    raise: this is called from ``verify_post_run``, where an exception is reported as an
    MCP hermeticity failure, and a mistyped ``data.skills`` is not one.
    """
    seen: set[str] = set()
    for obj in iter_jsonl(stdout):
        if not isinstance(obj, dict) or obj.get("type") != "session.skills_loaded":
            continue
        data = obj.get("data")
        if not isinstance(data, dict):
            continue
        skills = data.get("skills")
        if not isinstance(skills, list):
            continue
        for skill in skills:
            if not isinstance(skill, dict) or skill.get("source") != "builtin":
                continue
            path = skill.get("path")
            if isinstance(path, str):
                found = _app_root_version(path)
                if found:
                    seen.add(found)
    return seen.pop() if len(seen) == 1 else None


# Versions already warned about, so the drift notice fires once per process rather than
# once per cell — a warning repeated on every run of a matrix is one nobody reads.
_WARNED_VERSIONS: set[str] = set()


def _check_cli_version_denied(version: Optional[str]) -> None:
    """Fail a run that turns out to have executed a build KNOWN to break a hermeticity
    assumption.

    POST-RUN DETECTION, NOT PREVENTION, and the distinction is not pedantic: by the time
    this fires the CLI has already run to completion. If the denylisted defect is that a
    build silently loads an MCP server, that server has already been reachable for the
    whole run and whatever it did is already done. What this buys is that the run is
    reported as FAILED rather than passing — the result never enters the record as
    evidence — not that the build was kept away from anything.

    It cannot be moved earlier, and that is a property of copilot rather than a shortcut
    taken here. The executing version is knowable only from the run's own stream: the npm
    metadata names a version the executable no longer is (verified: package.json said
    1.0.63 while the SEA had been rewritten in place), no file the harness can read states
    what the binary will resolve, and the branch's standing rule bars clearing a security
    decision with a fact learned by executing the same program a second time — a preflight
    probe resolves its app root by a different path than the real argv, so it can honestly
    report a version the run does not use. A pre-launch gate here would therefore be
    guessing with more ceremony.

    It still runs BEFORE the contract evidence, because it covers precisely what that
    evidence cannot: a defect that leaves the MCP witness perfectly intact (broken plugin
    masking, say) fires no runtime check at all. Once a human finds such a build it is
    recorded in ``_DENIED_VERSIONS`` and refused by name, rather than waiting for a
    violation that never comes. Keeping the tiers honest about *when* they act is what
    stops "the denylist covers it" from being read as "that build cannot run here".
    """
    if version is not None and version in _DENIED_VERSIONS:
        raise RuntimeError(
            f"this run executed copilot {version}, which is on this adapter's denylist: "
            f"{_DENIED_VERSIONS[version]}. That defect leaves the MCP witness intact, so "
            "no runtime check catches it — the run is failed by version instead. Note "
            "this is detection AFTER the fact: the version is only readable from the "
            "run's own output, so the CLI has already executed and any side effect of "
            "that defect has already happened; what is prevented is the RESULT counting. "
            "Pin a different CLI build before running again."
        )


def _warn_cli_version_drift(version: Optional[str], *, agent: str = "copilot",
                            witnessed: bool = False) -> None:
    """Warn once per version that a run executed a build the analysis has not been checked
    against. A WARNING, not a failure — and deliberately so.

    What a newer build can do, and what nothing at runtime can see, is introduce a
    discovery channel that was never enumerated because no code was ever written to
    enumerate it. You cannot detect the absence of a check you never wrote. The message
    says that explicitly, because the danger is a green run being read as covering it.

    *witnessed* decides which half of that it is entitled to claim. A run that did not
    complete is excused from producing a witness, so this is reached with no MCP evidence
    at all — and "the runtime witness held, so the channels this adapter knows about were
    disabled and none loaded" would then be describing a check that never ran. A security
    notice that overstates its own evidence is worse than none: it is the sentence a
    reader would use to justify shipping.

    Once per version per process, not per cell: a warning repeated across every cell of a
    model matrix is one that gets filtered out, which defeats the purpose.
    """
    if version is not None and version in _VERIFIED_VERSIONS:
        return
    key = version or ""
    if key in _WARNED_VERSIONS:
        return
    _WARNED_VERSIONS.add(key)
    ran = f"CLI {version}" if version else "a CLI whose version could not be determined"
    established = (
        "  The runtime witness held, so the channels this adapter KNOWS ABOUT were "
        "disabled and none loaded. That does NOT cover a discovery channel ADDED after "
        f"{_VERIFIED_VERSIONS[-1]}: a new one would never have been enumerated, and no "
        "runtime check can see a check that was never written.\n"
        if witnessed else
        "  This run produced no MCP witness at all — it did not complete normally, which "
        "is allowed but proves nothing about its MCP host. So the channels this adapter "
        "knows about were NOT confirmed inert here, and a channel ADDED after "
        f"{_VERIFIED_VERSIONS[-1]} would be invisible on top of that.\n")
    print(
        f"warning: [{agent}] this run executed {ran}; the MCP hermeticity analysis was "
        f"verified against {'/'.join(_VERIFIED_VERSIONS)} ({_VERIFIED_ON}).\n"
        + established +
        "  To clear it: `agentskill-evals verify-copilot-channels` (audits the CLI "
        "bundle against the known channel inventory), then add the version to "
        "_VERIFIED_VERSIONS in adapters/copilot.py.",
        file=sys.stderr)


def _bundle_search_roots(env_map: Optional[Mapping[str, str]] = None) -> list[str]:
    """The ``pkg`` roots a copilot app bundle can live under.

    Transcribed from the root list in 1.0.72's own loader rather than inferred:
    ``$COPILOT_CACHE_HOME/pkg``, a per-platform default, ``${XDG_CACHE_HOME:-~/.cache}/
    copilot/pkg``, ``$COPILOT_HOME/pkg`` and ``~/.copilot/pkg`` — where the per-platform
    default is ``~/Library/Caches/copilot`` on darwin, ``${LOCALAPPDATA:-~/.cache}/
    copilot`` on Windows and the XDG one elsewhere. Every branch of that is covered here,
    and the two XDG-conditional ones are covered unconditionally, which makes this list a
    superset — the safe direction.

    A root missing from this list is a bundle the audit reports nothing about, which is
    indistinguishable from a bundle it cleared — so the list errs toward scanning roots
    that may not exist (``_safe_listdir`` swallows those) rather than toward missing one
    that does. ``$XDG_CACHE_HOME`` in particular is not a synonym for ``~/.cache``: where
    it is set to something else, ``~/.cache/copilot`` is the wrong directory entirely."""
    env_map = env_map if env_map is not None else os.environ
    home = os.path.expanduser("~")
    xdg = env_map.get("XDG_CACHE_HOME")
    bases = [env_map.get("COPILOT_CACHE_HOME"), env_map.get("COPILOT_HOME"),
             os.path.join(xdg, "copilot") if xdg else None,
             os.path.join(home, ".cache", "copilot"), os.path.join(home, ".copilot")]
    if sys.platform == "darwin":
        bases.append(os.path.join(home, "Library", "Caches", "copilot"))
    local_appdata = env_map.get("LOCALAPPDATA")
    if local_appdata:
        bases.append(os.path.join(local_appdata, "copilot"))
    roots: list[str] = []
    for b in bases:
        if b:
            p = os.path.join(b, "pkg")
            if p not in roots:
                roots.append(p)
    return roots


def find_cli_bundles(env_map: Optional[Mapping[str, str]] = None
                     ) -> list[tuple[str, str]]:
    """Every discoverable ``(version, app.js path)``, newest-sorting last.

    These are the bundles the loader can pick from, which is not necessarily the code a
    given run executes: this adapter's ``--no-auto-update`` argv pins the run to the
    binary's own app root. The audit is therefore about a BUILD, not about a past run —
    for what actually executed, read the version out of the run's own stream
    (``_stream_cli_version``).

    CANDIDACY IS THE LOADER'S, NOT A TIDIER ONE. Read out of the 1.0.72 bundle's own
    ``index.js``: it lists each ``<root>/{universal,<platform>}`` directory, keeps every
    entry whose ``app.js`` is merely R_OK-readable — no name test of any kind — sorts them
    by a PREFIX parse (``/^(\\d+)\\.(\\d+)\\.(\\d+)/``) and imports the first. So
    ``1.0.73foo`` and ``1.0.73-`` parse as 1.0.73 and can outrank the running build, and
    ``nonsense`` is a candidate the ``--prefer-version`` path (banned from scenario argv,
    but present in the CLI) selects by exact name. Requiring a well-formed semver
    directory here would have skipped all three — reporting nothing about a bundle that
    can execute, which in an audit's output is indistinguishable from having cleared it.
    The name filter is therefore gone entirely and ``_version_sort_key`` reproduces the
    loader's ordering rather than a stricter semver one.

    ``app.js`` is the candidacy file because it is the one that has to exist for code to
    run: the outer SEA loader looks for ``index.js``, but that shim then resolves
    ``<its own dir>/app.js``, so an ``index.js`` without a sibling ``app.js`` fails to
    import rather than executing. It is also the file the markers live in.

    Directories other than ``universal``/``<platform>`` are scanned even though the loader
    ignores them — a superset is the safe direction for an audit."""
    found: list[tuple[str, str]] = []
    for root in _bundle_search_roots(env_map):
        for platform_dir in sorted(_safe_listdir(root)):
            pdir = os.path.join(root, platform_dir)
            for ver in sorted(_safe_listdir(pdir)):
                app = os.path.join(pdir, ver, "app.js")
                if os.path.isfile(app):
                    found.append((ver, app))
    found.sort(key=lambda vp: _version_sort_key(vp[0]))
    return found


def _version_sort_key(version: str) -> tuple[int, tuple[int, ...], int, str]:
    """Newest-last ordering, reproducing the comparator in the CLI's own loader.

    Ported from 1.0.72's ``index.js``/``sea-loader.js`` rather than from the semver spec,
    because the question this answers is "which of these would the loader pick", and it
    picks by its own rules: a leading ``\\d+.\\d+.\\d+`` PREFIX (so ``1.0.73foo`` is
    1.0.73), a name that does not parse at all sorting below every one that does, a name
    containing ``-`` treated as a prerelease and sorting before its release
    (``1.0.73-beta.1`` < ``1.0.73``, and equally ``1.0.73-`` < ``1.0.73``), and the raw
    name as the final tiebreak. Ordering matters because callers report the newest bundle
    as the interesting one."""
    m = re.match(r"(\d+)\.(\d+)\.(\d+)", version)
    if not m:
        return (0, (0, 0, 0), 0, version)
    return (1, tuple(int(g) for g in m.groups()), 0 if "-" in version else 1, version)


def _safe_listdir(path: str) -> list[str]:
    try:
        return os.listdir(path)
    except OSError:
        return []


def audit_channel_markers(app_js: str) -> dict[str, bool]:
    """Which ``_MCP_CHANNEL_MARKERS`` are still present in one CLI bundle.

    Substring search over the raw bundle, streamed in chunks with an overlap so a marker
    straddling a chunk boundary is not missed. Deliberately crude: the bundle is minified
    JS, so anything cleverer would be brittle, and the question being asked is only
    "does this string still occur anywhere" — a marker that has VANISHED is the signal."""
    needles = [m.encode() for m, _why in _MCP_CHANNEL_MARKERS]
    hits = [False] * len(needles)
    longest = max(len(n) for n in needles)
    with open(app_js, "rb") as f:
        tail = b""
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            buf = tail + chunk
            for i, needle in enumerate(needles):
                if not hits[i] and needle in buf:
                    hits[i] = True
            if all(hits):
                break
            tail = buf[-longest:]
    return {m: hits[i] for i, (m, _why) in enumerate(_MCP_CHANNEL_MARKERS)}


# The built-in server a hermetic invocation always CONFIGURES and always disables:
# --disable-builtin-mcps covers it and _BUILTIN_MCP_SERVERS names it on every argv.
# 1.0.64 therefore lists it in session.mcp_servers_loaded on every run under the
# harness's flags — verified live, as the sole entry, `{"name": "github-mcp-server",
# "status": "disabled"}`. That makes it the witness's SENTINEL: a well-formed event that
# does not name it is not describing the MCP host this adapter was built against, so an
# empty or truncated server list can no longer pass for "nothing was loaded".
_WITNESS_SENTINEL = "github-mcp-server"


def _mcp_witness(stdout: str,
                 exit_code: Optional[int]) -> tuple[Optional[str], list[str], bool]:
    """Read copilot's MCP witness out of its own event stream.

    Returns ``(contract_violation, live_servers, witnessed)`` — the violation being
    ``None`` when the stream still speaks the contract this audit depends on,
    ``live_servers`` the servers copilot said it brought up as ``name (status)``, and
    ``witnessed`` whether a conforming witness was actually SEEN.

    ``witnessed`` is not the negation of the violation. A run that did not complete is
    excused from producing a witness (a child killed on timeout emits none), so it can
    return no violation and no witness at all — nothing was proven either way, and
    anything downstream that describes what this run established has to say so rather
    than infer a clean bill from a silent ``None``.

    ``--output-format json`` streams two ephemeral events describing the MCP host:
    ``session.mcp_servers_loaded`` (emitted right after initializeMcpHost(), one entry per
    CONFIGURED server with its status — a disabled one is LISTED as ``disabled``, not
    omitted) and ``session.mcp_server_status_changed`` (a later transition, e.g. a server
    that connects mid-run). Both are read here; ``parse`` skips them only because they are
    ephemeral streaming fragments, not because they are absent.

    This is the one piece of evidence a launch-window leak cannot take back. Every other
    check the adapter makes reads the filesystem, so a change reverted before the child
    exits reads back clean — but copilot has already said, in its own captured output,
    which servers it started. Verified against 1.0.64: with the harness's flags the sole
    entry reports ``github-mcp-server``/``disabled`` and never transitions, while dropping
    --disable-builtin-mcps yields ``connected`` plus a status_changed event.

    THE CONTRACT IS CHECKED, NOT THE VERSION, and the check has to be strict in three
    separate ways, because every weakening of it degrades silently into "no servers
    found" — which is indistinguishable from a clean run:

    * WHEN the witness is required. Inferring "the session finished" from copilot's own
      ``result`` event makes the gate self-defeating: a build that renamed BOTH events
      would emit neither, be judged incomplete, and pass. So a normal zero exit is what
      demands the witness — a fact about the process, not about the stream, and one no
      rename can erase. (``result`` still counts, so a nonzero exit that nonetheless
      reported a completed session is held to the contract too.) A child killed on
      timeout exits -9 and is not blamed for a report it never had the chance to make;
      the state re-check covers exactly that case.
    * WHAT SHAPE the payload has. ``data`` must be an object and ``data.servers`` a list
      of entries each carrying a non-empty string ``name`` and ``status``. A renamed
      field (``mcpServers``), a null, a string, or one malformed entry means the event is
      no longer the event this reader understands — that is a contract violation, not an
      empty server list.
    * WHAT IT NAMES. A well-formed but EMPTY list would still satisfy a
      presence-only check while proving nothing, so the built-in sentinel
      (``_WITNESS_SENTINEL``) must appear. Only its presence is required here, not its
      status: a sentinel reported live is a genuine leak and is reported as such by the
      caller, with the message that actually fits.

    Why not a version NUMBER instead — the obvious alternative, refusing builds this
    adapter was not verified against? Because it cannot be built soundly here, for two
    independently fatal reasons, both established against the installed CLI rather than
    reasoned about:

    * The version reported is not the version that runs. The npm package advertises
      1.0.63 in its metadata while the executable it resolves to (a ~150 MB
      single-executable-application under ``node_modules/@github/copilot-darwin-arm64``)
      had been REWRITTEN IN PLACE by copilot's own updater; the run it performs loads its
      app root out of ``~/Library/Caches/copilot/pkg/<platform>/<version>/`` (visible in
      the child's own ``session.skills_loaded`` paths). No file the harness can read
      states the version that will execute.
    * A preflight probe and the real run resolve their code DIFFERENTLY. Absent
      ``--no-auto-update``, the loader scans writable cache roots
      (``$COPILOT_CACHE_HOME/pkg``, ``$COPILOT_HOME/pkg``, ``~/.cache/copilot/pkg``,
      ``~/.copilot/pkg``) and imports the app.js under the HIGHEST version-shaped
      directory name — verified by planting a ``9.9.9/app.js`` in a cache root and
      watching copilot execute it. A bare ``copilot --version`` therefore goes through
      that scan; this adapter's argv, which carries ``--no-auto-update``, does not, and
      the two can execute different code. That flag is in ``_HERMETIC`` for what looked
      like a bandwidth reason and turns out to be load-bearing: it pins the run to the
      binary's own app root, out of reach of anything writable that a launch-window
      neighbour could plant. (The same plant is NOT loaded once the flag is present.)

    So the version is unreadable and unpinnable from outside, and the branch's standing
    rule applies anyway: a fact learned by executing a program copilot independently
    executes again may not CLEAR a security decision. The contract check needs no second
    execution — its evidence comes from the very run being judged.
    """
    live: dict[str, str] = {}
    violations: list[str] = []
    completed = exit_code == 0
    loaded_ok = sentinel_seen = False

    def note(name: str, status: str) -> None:
        if status not in _INERT_MCP_STATUSES:
            live[name] = status

    def entry(name: object, status: object, where: str) -> bool:
        if (not isinstance(name, str) or not name
                or not isinstance(status, str) or not status):
            violations.append(
                f"{where} carries a malformed entry (name={name!r}, status={status!r}): "
                "each entry must name a server and its status as non-empty strings")
            return False
        note(name, status)
        return True

    for obj in iter_jsonl(stdout):
        # iter_jsonl yields any well-formed JSON VALUE, not just objects: a bare `42` or
        # a list on its own line parses fine and has no .get(). Skipping those is not
        # leniency about the contract — the events below are still required to be
        # well-formed, and a run that produced none of them still fails. It is about
        # WHICH failure gets reported: an AttributeError here escapes verify_post_run and
        # is announced as an MCP hermeticity failure, so one stray line would mask a
        # perfectly good witness later in the same stream (reproduced: `42` ahead of a
        # valid session.mcp_servers_loaded).
        if not isinstance(obj, dict):
            continue
        etype = obj.get("type")
        data = obj.get("data")
        if etype == "result":
            completed = True
        elif etype == "session.mcp_servers_loaded":
            if not isinstance(data, dict):
                violations.append(
                    "session.mcp_servers_loaded carries no 'data' object "
                    f"(got {type(data).__name__})")
                continue
            servers = data.get("servers")
            if not isinstance(servers, list):
                violations.append(
                    "session.mcp_servers_loaded has no 'data.servers' LIST (got "
                    f"{type(servers).__name__}; keys present: {sorted(data)}) — a "
                    "renamed or retyped field reads as 'no servers', which is exactly "
                    "what a clean run looks like")
                continue
            well_formed = True
            for srv in servers:
                if isinstance(srv, dict):
                    ok = entry(srv.get("name"), srv.get("status"),
                               "session.mcp_servers_loaded.data.servers")
                    if ok and srv.get("name") == _WITNESS_SENTINEL:
                        sentinel_seen = True
                else:
                    ok = entry(None, None, "session.mcp_servers_loaded.data.servers")
                well_formed = well_formed and ok
            loaded_ok = loaded_ok or well_formed
        elif etype == "session.mcp_server_status_changed":
            if not isinstance(data, dict):
                violations.append(
                    "session.mcp_server_status_changed carries no 'data' object "
                    f"(got {type(data).__name__})")
                continue
            entry(data.get("serverName"), data.get("status"),
                  "session.mcp_server_status_changed.data")

    witnessed = loaded_ok and sentinel_seen
    if violations:
        return violations[0], _fmt_live(live), witnessed
    if completed and not loaded_ok:
        return ("no well-formed session.mcp_servers_loaded event reached the harness",
                _fmt_live(live), witnessed)
    if completed and not sentinel_seen:
        return (f"session.mcp_servers_loaded never named the built-in "
                f"{_WITNESS_SENTINEL!r}, which a hermetic invocation always configures "
                "and disables — so the event is not describing the MCP host this "
                "adapter was verified against", _fmt_live(live), witnessed)
    return None, _fmt_live(live), witnessed


def _fmt_live(live: Mapping[str, str]) -> list[str]:
    return [f"{n} ({live[n]})" for n in sorted(live)]


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


# The two files copilot resolves user settings from. `customAgents` is a USER setting,
# and 1.0.64 MIGRATES it between them at startup: a value written into config.json is
# applied for that run and then moved into settings.json (verified live — the harness's
# own injected opt-out came back out of config.json and appeared, intact, in a
# settings.json copilot created). Reading only config.json therefore sees the opt-out
# vanish after the first run that touches the home: harmless under isolation, which
# rebuilds the home each time, but it strands NON-isolated users in a loop where they set
# the documented key, copilot relocates it, and every later run fails closed telling them
# to set the key they already set. Both files are read for that reason.
_COPILOT_SETTINGS_FILES = ("config.json", "settings.json")


def _remote_agents_opted_out(home: str) -> bool:
    """True iff the settings copilot will read provably disable remote custom-agent
    discovery: ``customAgents.defaultLocalOnly`` (documented in ``copilot help
    config``), the ONLY off-switch — the 1.0.64 loader short-circuits before its
    org/enterprise listing when it is set; no flag or env var exists. The isolation
    sanitizer injects it, so masked-home runs always pass this test; a missing or
    unreadable config proves nothing and reads as not-opted-out.

    Both of copilot's settings files count (see _COPILOT_SETTINGS_FILES), and a
    DISAGREEMENT between them fails closed: which one wins is decided inside a native
    module (``stateMigrateSettingsFromConfig``), not in readable JS, so the harness
    declines to guess a precedence it cannot verify. Opted out means set somewhere and
    contradicted nowhere.

    A file CONTRADICTS the opt-out whenever it carries a ``customAgents`` key that does
    not itself resolve to ``defaultLocalOnly is True`` — including the empty object and
    any non-dict value. Requiring the subkey to be PRESENT before counting the file was
    the earlier rule and it had a hole: ``config.json: {"customAgents": {}}`` alongside
    ``settings.json: {"customAgents": {"defaultLocalOnly": true}}`` read as opted out
    here, while the startup migration moves config.json's whole ``customAgents`` object
    across and the surviving value is ``{}`` — remote discovery back on, preflight
    satisfied. The migration transports the KEY, so the key's presence is what has to be
    judged, not the subkey's.

    That strictness costs real runs nothing, which was checked rather than assumed: a live
    isolated run starts with the sanitizer's opt-out in BOTH files and ends with the key
    REMOVED from config.json (not left as an empty object) and intact in settings.json —
    so the migration never produces the contradicting shape this now rejects. Worth
    re-checking on a CLI upgrade: a migration that emptied the key instead of deleting it
    would fail every isolated run closed, loudly, which is the direction to fail but a
    bad afternoon.
    """
    seen: list[bool] = []
    for name in _COPILOT_SETTINGS_FILES:
        data = _load_copilot_config(os.path.join(home, name))
        if not isinstance(data, dict) or "customAgents" not in data:
            continue  # says nothing about custom agents; the other file may still speak
        agents_cfg = data["customAgents"]
        seen.append(isinstance(agents_cfg, dict)
                    and agents_cfg.get("defaultLocalOnly") is True)
    return bool(seen) and all(seen)


# extra_args tokens that reopen MCP/configuration channels after the disable set is
# computed (all verified against the installed 1.0.64: --help lists the first three,
# --config-dir and --prefer-version are in the bundle but hidden from help):
# --additional-mcp-config merges MORE servers into the session past the disable set;
# --agent selects a custom agent whose frontmatter mcp-servers the disable set cannot
# NAME (see the custom-agents comment above); --plugin-dir loads a plugin — whose
# definition can declare mcpServers — from an arbitrary dir; --config-dir repoints the whole config
# home away from the one this adapter enumerated; --prefer-version can select a
# DIFFERENT cached CLI version, past which the 1.0.64-specific safety assumptions this
# adapter computed no longer hold; -C changes copilot's working directory BEFORE any
# discovery, invalidating the cwd the workspace/agent walks used. Long forms match exact
# or --flag=value; -C matches its own token AND any combined short-option cluster
# carrying it (see _config_channel_token).
#
# --output-format is in the list for a different reason: it does not add a server, it
# blinds the WITNESS. build_argv sets `--output-format json` and verify_post_run reads
# copilot's session.mcp_servers_loaded events out of that stream — the only evidence a
# launch-window leak cannot revert. extra_args ride at the end of argv and copilot's
# parser takes the LAST value for a repeated option, so a trailing `--output-format text`
# silently wins (verified live on the installed build: the run printed plain prose and
# emitted zero JSON events, and verify_post_run's stream check then sees an empty stream
# and passes). `-s/--silent` was tested the same way and does NOT suppress the events, so
# it stays allowed.
_CONFIG_CHANNEL_LONG = ("--additional-mcp-config", "--agent", "--plugin-dir",
                        "--config-dir", "--prefer-version", "--output-format")


def _config_channel_token(extra_args: list[str]) -> Optional[str]:
    """The first extra_args token that opens a copilot configuration channel — or
    suppresses the output channel the post-run audit reads — or None. A token that merely
    LOOKS like one (e.g. a value following some unrelated flag) is reported too — that
    false positive fails closed, the safe direction.

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
    """Sanitizing mask for BOTH of copilot's user-settings files, ~/.copilot/config.json
    and ~/.copilot/settings.json (see _COPILOT_SETTINGS_FILES): keep everything (auth
    tokens, settings), drop the plugin registrations and the built-in-server enablement
    list (``_COPILOT_CONFIG_STRIP_KEYS``), and FORCE
    ``customAgents.defaultLocalOnly`` — the documented setting (``copilot help
    config``) that is the only off-switch for remote custom-agent discovery, whose
    org/enterprise listings can carry MCP servers whose names --disable-mcp-server
    has no way to learn (see the custom-agents comment at the top of this module).
    Either file is JSONC — line/inline/block comments and trailing commas, the
    live-verified grammar — handled by ``_load_copilot_config``. Unreadable/unparseable →
    the same neutral-but-hermetic shape (fail closed: no plugins, no remote agents; auth
    loss surfaces loudly rather than servers silently loading). Both files get this same
    treatment because copilot MOVES user settings between them at startup: masking one
    would let the other reintroduce exactly the keys stripped here."""
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
    # Custom agents are one more channel, and the one --disable-mcp-server cannot be
    # AIMED at: startServerOnce does honor the disable set, but agent-declared server
    # NAMES are unenumerable — local frontmatter plus REMOTE org/enterprise listings the
    # harness cannot read (see the module comment). So agents/ is masked to an empty
    # dir, the sanitized config.json gets customAgents.defaultLocalOnly forced (kills
    # remote agent discovery), and _mcp_disable_args fails closed on any local agent
    # file the masks can't reach (workspace convention dirs, non-isolated homes).
    # Every one of those layers reads state BEFORE launch, while copilot reads it again
    # for itself AFTER launch — and execs git in between. verify_post_run closes the
    # report on that window once the child has exited, from both sides: it re-runs the
    # enumeration (catching a change that outlived the run) and reads copilot's own
    # session.mcp_servers_loaded stream (catching one that was reverted before exit,
    # which no amount of re-reading the filesystem could). It cannot stop a server that
    # started, but a run that passes never had its premise broken underneath it.
    # settings.json is masked with the SAME sanitizer as config.json because 1.0.64 moves
    # user settings between the two files at startup (verified live: an injected
    # customAgents opt-out left config.json and reappeared in a settings.json copilot
    # created). Whatever `enabledPlugins` / `enabledMcpServers` / `customAgents` the strip
    # removes from one file would otherwise ride back in through the other, which the
    # overlay symlinks wholesale unless it is declared here.
    isolation_config_masks = {".copilot/mcp-config.json": '{"mcpServers": {}}',
                              ".copilot/installed-plugins": None,
                              ".copilot/agents": None,
                              ".copilot/config.json": _sanitized_copilot_config,
                              ".copilot/settings.json": _sanitized_copilot_config}
    # COPILOT_HOME replaces ~/.copilot wholesale (verified in 1.0.64's bundle) — without
    # mirroring it, a set var would bypass the masks above.
    isolation_config_homes = [("COPILOT_HOME", ".copilot", None)]
    # `--reasoning-effort <level>` (verified 2026-07-08: choices none|low|medium|high|
    # xhigh|max — the harness only passes the typed cross-runner subset low|medium|high).
    supports_reasoning_effort = True

    # Hermetic flags — memory is already off in -p mode; these block the
    # remaining state channels (custom instructions / AGENTS.md, built-in
    # MCP servers, remote control, auto-update downloads).
    # --no-auto-update carries more weight than its name suggests and must not be
    # dropped: without it copilot's loader picks the app.js it executes by scanning
    # WRITABLE cache roots ($COPILOT_CACHE_HOME/pkg, $COPILOT_HOME/pkg,
    # ~/.cache/copilot/pkg, ~/.copilot/pkg) and importing whichever directory name sorts
    # highest under its own prefix-parse — a planted 9.9.9/app.js runs, as arbitrary code
    # inside the child, ahead of every MCP flag on this argv. With the flag the run is
    # pinned to the binary's own app root and the plant is ignored (both halves verified
    # live; the selection rule itself is read out of the bundle in find_cli_bundles). See
    # _mcp_witness for why the resulting build still cannot be identified by version, and
    # _BUILD_REDIRECT_VARS below for the env var that bypasses this flag entirely.
    _HERMETIC = [
        "--no-custom-instructions",
        "--disable-builtin-mcps",
        "--no-remote",
        "--no-auto-update",
    ]

    # ...and the flag is not sufficient on its own. Reading 1.0.72's loader to check the
    # claim above turned up an env var that is consulted BEFORE any of this argv is
    # examined: COPILOT_CLI_DIST_DIR imports `<its value>/index.js` directly, with no
    # version floor, no cache-root constraint and no interaction with --no-auto-update at
    # all. An ambient value in the developer's shell — or a scenario `env:` override —
    # would run arbitrary code as the agent, and _stream_cli_version would then be
    # reporting the provenance of a build nobody chose. Cleared for every copilot launch,
    # not just isolated ones, since the var is read from the child's environment however
    # it got there. (COPILOT_CLI_VERSION and COPILOT_AUTO_UPDATE were checked at the same
    # time and are inert under this argv: both only feed the rescan that --no-auto-update
    # already switches off, and COPILOT_AUTO_UPDATE can only disable, never enable.)
    _BUILD_REDIRECT_VARS = ("COPILOT_CLI_DIST_DIR",)

    def env(self, base_env: dict[str, str], opts: RunOptions) -> dict[str, str]:
        env = dict(super().env(base_env, opts))
        for var in self._BUILD_REDIRECT_VARS:
            env.pop(var, None)
        return env

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
        be disabled here at all: startServerOnce would honor the disable set, but their
        NAMES are unenumerable — local frontmatter plus REMOTE org/enterprise listings
        the harness cannot read (module comment) — so any discoverable local agent
        file, and any run whose config doesn't provably opt out of remote agent
        discovery (customAgents.defaultLocalOnly — injected into the sanitized
        config.json, so masked-home runs pass), fail closed instead. The opt-out is
        required unconditionally rather than only for a git-repo cwd: deciding that
        would take a git execution the harness cannot bind to copilot's own. On win32 the
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
        # Custom agents (module comment): the harness cannot enumerate their
        # mcp-server names, so presence of any LOCAL agent definition — or the
        # possibility of a REMOTE org/enterprise listing — fails closed before
        # anything is enumerated.
        agent_files = _custom_agent_files(home, cwd)
        if agent_files:
            shown = ", ".join(agent_files[:4]) + (", ..." if len(agent_files) > 4
                                                  else "")
            raise RuntimeError(
                f"custom-agent definition file(s) [{shown}] are discoverable by "
                "copilot (under <config home>/agents, or a .github/agents / "
                ".claude/agents dir on the walk from the run cwd up to the "
                "filesystem root — copilot's own walk stops at the git root, or at "
                "the OS home when the cwd is in no repo, but which of those applies "
                "can only be learned by running git, so the harness scans wider): "
                "agent frontmatter can declare mcp-servers whose names the "
                "harness can't enumerate (local frontmatter plus unreadable REMOTE "
                "org/enterprise listings), so it has nothing reliable to add to "
                "--disable-mcp-server even though copilot's startServerOnce would "
                "honor that set, with no flag to disable custom agents — MCP "
                "hermeticity can't be enforced; failing closed. Remove or relocate "
                "the agent files for harness runs (isolation already masks "
                "<home>/agents to an empty dir)."
            )
        # The REMOTE org/enterprise listing is unlocked by a git-repo cwd — but whether
        # THIS cwd is one is not a question the harness may answer for itself. Finding
        # out means executing git, and copilot executes git again at launch: two
        # independent executions a stateful wrapper, a slow one, or a differently
        # resolved binary can answer differently, and the answer here would CLEAR a
        # security gate. Same two-execution gap as the Windows ODR command, same
        # verdict (_assert_odr_gate_off): don't bet on it. The opt-out is therefore
        # required UNCONDITIONALLY — it is a fact the harness reads directly out of the
        # config copilot will read, and it is what actually disables the listing.
        if not _remote_agents_opted_out(home):
            raise RuntimeError(
                "the config copilot will read does not set "
                "customAgents.defaultLocalOnly: for a GitHub-remoted repo with "
                "auth, copilot lists org/enterprise custom agents remotely, and "
                "those can carry mcp-servers whose names the harness cannot "
                "enumerate (1.0.64 bundle) with no flag to disable the listing — MCP "
                "hermeticity can't be verified; failing closed. The opt-out is "
                "required for every run, not just an in-repo one: proving this cwd is "
                "outside a repository would mean running git here while copilot runs "
                "it again at launch, and two independent executions can disagree — a "
                "gap no cwd/env matching closes. Isolated runs get the opt-out "
                "injected into the sanitized config.json automatically; for a "
                'non-isolated run set {"customAgents": {"defaultLocalOnly": true}} '
                "in the real config.json."
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
        # _CONFIG_CHANNEL_LONG and the combined-short-cluster rule in
        # _config_channel_token). Standard runner cells never populate
        # extra_args; a programmatic caller passing one of these fails closed here
        # (exec.execute() records a failed run), checked FIRST so a doomed
        # invocation never spends the enumeration.
        bad = _config_channel_token(opts.extra_args)
        if bad is not None:
            raise RuntimeError(
                f"extra_args token {bad!r} opens a copilot configuration channel, or "
                "closes the output channel the post-run audit reads "
                "(--additional-mcp-config injects MCP servers past the disable "
                "set, --agent selects a custom agent whose mcp-server NAMES the "
                "harness cannot enumerate, --plugin-dir loads plugin-declared "
                "servers, --config-dir repoints the enumerated config home, "
                "--prefer-version runs a different cached CLI build than the one "
                "this enumeration is built on, --output-format overrides the JSON "
                "stream verify_post_run reads its evidence from, -C moves discovery "
                "to another cwd — the last also in combined short clusters "
                "like -sC/tmp) — the run "
                "would not be provably MCP-hermetic; failing closed."
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

    def verify_post_run(self, argv: list[str], opts: RunOptions, *, cwd: str,
                        stdout: str = "", stderr: str = "",
                        exit_code: Optional[int] = None) -> None:
        """Audit the finished run two ways: what copilot SAID it loaded, and whether the
        state the disable set was computed from still says the same thing.

        _mcp_disable_args reads the agent dirs and MCP configs BEFORE launch; copilot
        reads them again FOR ITSELF at startup. Between those two reads copilot runs
        code: 1.0.64 execs ``git rev-parse`` (resolved on the CHILD's PATH) to pick its
        convention-dir boundary, and only then globs the agent dirs and loads
        mcp-config.json. Whatever answers for ``git`` there can create an agent file or
        add a server entry in that window — copilot discovers it, and the disable set
        computed a moment earlier does not name it.

        Neither escape the original finding suggests is available. The discovery paths
        cannot be made immutable: they are every ancestor of the cwd up to ``/`` plus a
        config home the child owns (isolation masks ``<home>/agents`` to an empty dir,
        but the child runs as the same uid and can write into it, and can chmod back
        anything the harness locks). And copilot's git cannot be bound to a binary the
        harness trusts, because the harness has no more trustworthy git to bind it to —
        it would resolve the same PATH and get the same answer.

        So the window is audited rather than closed, from two directions:

        1. THE CHILD'S OWN REPORT (_mcp_witness). Copilot streams
           session.mcp_servers_loaded / session.mcp_server_status_changed naming every
           configured server and its status. Any server not reported inert was brought
           up, and no later edit to the filesystem retracts that. This is what catches
           the ABA shape the state re-check structurally cannot: plant an agent file or
           a config entry, let copilot load it and start the server, restore the original
           bytes before exit. The re-read below then sees exactly the clean state it saw
           before — but copilot has already testified, in the output the harness
           captured, that the server was live.
        2. THE STATE, RE-READ. Catches a change that outlived the run, and covers the
           case report (1) cannot make: a child killed on timeout, or one that died
           before its MCP host initialized, emits no such event, and absence of the event
           is not evidence of absence of a server.

        Neither is sufficient alone; a leak now has to survive both, which means being
        reverted before exit AND never appearing in copilot's own event stream. A shim
        answering for ``git`` outside the process can do the first and not the second.

        Direction (1) is only as good as the stream contract it reads, and the CLI version
        that contract belongs to can neither be read nor pinned from outside the run (see
        _mcp_witness). So the CONTRACT is checked instead of the version, and checked
        strictly: any run that finished normally — a zero exit, which no event rename can
        disguise — must produce a well-formed ``session.mcp_servers_loaded`` naming the
        built-in sentinel. A witness that is absent, reshaped, or renamed degrades into
        "no servers found", which is indistinguishable from a clean run, so it is refused
        before either direction is consulted.

        Re-executing the enumeration is sound in the direction it is used — unlike the
        git preflight this branch refuses (which would CLEAR a gate on the first of two
        executions), a second scan can only ADD failures, never bless a run.

        The whole enumeration is re-run, not just the agent scan: the same window is
        open on mcp-config.json and the workspace configs, where a late entry is a
        directly-named server the launched argv never disabled.

        Known false positive: an eval whose own task writes a custom-agent definition
        into the workspace tree fails here. That file appeared after copilot's glob and
        so never loaded — but the harness cannot date it against a glob it did not
        observe, and this scan errs toward the loud answer like every other one on this
        path. Relocate such fixtures outside the discovery tree.
        """
        # A build KNOWN to break an assumption is refused first: that is the one tier the
        # runtime contract cannot reach, since such a defect leaves the witness intact.
        # Everything after this point is contract evidence, which is version-independent.
        version = _stream_cli_version(stdout)
        _check_cli_version_denied(version)
        broken, live, witnessed = _mcp_witness(stdout, exit_code)
        if broken is not None:
            raise RuntimeError(
                f"copilot's MCP witness does not hold: {broken}. The run finished "
                "normally (zero exit, or its own 'result' event), and that stream is "
                "where the ABA-immune half of this audit gets its evidence — a hermetic "
                "run on a build this adapter understands always reports its MCP host and "
                "names the built-in server there. A witness that is missing, reshaped, or "
                "renamed yields 'no servers found', which reads exactly like a clean run, "
                "so it is refused instead: the version this contract belongs to can "
                "neither be read nor pinned from outside (see _mcp_witness). The run's "
                "hermeticity is unwitnessed rather than confirmed; failing closed."
            )
        if live:
            raise RuntimeError(
                f"copilot's own event stream reports MCP server(s) {', '.join(live)} as "
                "brought up during this run, but a hermetic invocation leaves every "
                "configured server 'disabled'. The state on disk may read clean now — a "
                "config or agent file planted inside the launch window and reverted "
                "before exit would — but the run itself was not MCP-hermetic."
            )
        try:
            after = _disabled_server_names(
                self._mcp_disable_args(cwd, env=opts.effective_env))
        except RuntimeError as exc:
            raise RuntimeError(
                "the invocation was built while MCP hermeticity was enforceable, but "
                f"re-checking after the run it no longer is: {exc}"
            ) from None
        new = sorted(after - _disabled_server_names(argv))
        if new:
            raise RuntimeError(
                f"MCP server(s) {', '.join(new)} are configured now but were not when "
                "the invocation was built, so the launched --disable-mcp-server set "
                "does not name them: copilot reads its config at startup, after it "
                "execs git for its agent-dir boundary, so a server added in that "
                "window loads un-disabled. This run is not provably MCP-hermetic."
            )
        # Last, and only on a run that cleared every gate above: say so if the build is
        # one the analysis has never been checked against. Warning here rather than
        # earlier keeps a genuine hermeticity failure from being buried under a version
        # notice, and keeps the notice honest — it describes what THIS run does and does
        # not cover, which is why `witnessed` goes with it: clearing every gate is not
        # the same as having produced evidence, since a run that never completed is
        # excused from the witness entirely.
        _warn_cli_version_drift(version, agent=self.name, witnessed=witnessed)

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
            if not isinstance(obj, dict):   # a bare JSON scalar/list is not an event
                continue
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
