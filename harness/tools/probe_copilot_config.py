#!/usr/bin/env python3
"""Probe §9 #3: what does copilot ACTUALLY write into `mcp-config.json`?

OPT-IN, and unlike its sibling this one **spends no model call** — `copilot mcp add` edits a
config file and exits. Needs `copilot` on PATH.

WHY IT IS A PROBE AND NOT A READING OF THE DOCS. Every adapter in this harness writes a config
some CLI has to parse, and a key spelled the way the documentation says rather than the way the
binary reads produces a server that silently never starts — which looks exactly like a server
that started and had nothing to say. Phase 1b already paid ten days for that once (§4). The
cheap defence is to let the CLI write the file itself, in a throwaway `COPILOT_HOME`, and read
back what it chose.

WHAT IT ESTABLISHES, at the width the instrument allows: the key names copilot emits **for the
options it was asked to set**. It is silent about keys it was never asked for — an absence here
is "not exercised", never "not supported", and `unset` is reported as its own value for exactly
that reason. In particular a `tools` allowlist is only reported if `copilot mcp add` has a flag
for it; if it does not, this probe says so and `probe_copilot_gating.py` is what settles whether
the array works when written by hand.

NOTHING IS WRITTEN OUTSIDE THE THROWAWAY HOME. `COPILOT_HOME` is pointed at a temp dir and the
real one is never read or touched, which is the same containment §5 requires of every runner.

    python tools/probe_copilot_config.py
"""
from __future__ import annotations

from typing import Any

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

_VERSION_RE = re.compile(r"\d+\.\d+\.\d+")

DEADLINE = 60.0
CONFIG_NAME = "mcp-config.json"

# What the adapter intends to write (DESIGN_MCP_Support.md §3). Each is reported as
# `confirmed`, `differs:<what copilot used>`, or `unexercised` — three states, because
# "copilot did not write this key" and "copilot wrote it differently" lead to different work.
EXPECTED = {
    "servers_container": "mcpServers",
    "command": "command",
    "args": "args",
    "env": "env",
    "tools": "tools",
    "url": "url",
    "headers": "headers",
    # MEASURED INTO THE SET, which is what this probe is for. `type` was the key nothing in §3
    # had, and it was reported as a surprise on every run — which meant `surprises` was NEVER
    # empty, so the exit status was 1 whatever else happened and the remote-shape verdict could
    # not move it. A finding that cannot change is not a finding; it is a constant. Now that
    # 1.0.79 has been measured writing `local`/`http`/`sse` here, it is a key the adapter must
    # write, and the remaining surprises are the ones nobody has seen yet (review, PR #110).
    # §3 owes a line for it, and that is adapter work rather than probe work.
    "type": "type",
}

CONFIRMED = "confirmed"
UNEXERCISED = "unexercised"


def find_config(home: str) -> str | None:
    """The config copilot wrote, wherever under the throwaway home it put it.

    SEARCHED RATHER THAN ASSUMED. The path is part of what this probe is measuring: an adapter
    that writes to the documented location while the binary reads another is the same silent
    failure as a misspelled key, and asserting the path here would hide it.
    """
    for root, _dirs, files in os.walk(home):
        if CONFIG_NAME in files:
            return os.path.join(root, CONFIG_NAME)
    return None


def unexpected_keys(config: dict, expected: dict) -> list[str]:
    """Keys copilot wrote that the adapter has no plan for.

    THE SET IS CLOSED IN BOTH DIRECTIONS, which is the half that is easy to leave out and the
    half that found something: the first run of this probe reported "keys that DIFFER: none"
    while copilot was writing `"type": "local"`, a transport discriminator nothing in §3
    mentions. Checking only the keys you already thought of cannot report the key you did not
    — the same rule `mcp_audit` states for completion facts, arriving in an instrument.
    """
    container = next((k for k in config if isinstance(config.get(k), dict) and config[k]), None)
    if container is None:
        return []
    body = next((v for v in config[container].values() if isinstance(v, dict)), {})
    known = {v for k, v in expected.items() if k != "servers_container"}
    return sorted(k for k in body if k not in known)


def remote_shape(body: Any, want_type: str = "http") -> tuple[bool, str]:
    """(is_the_remote_shape, description) for one entry.

    Its own function because the failure it names is silent: `copilot mcp add name -- --url X`
    produces a perfectly well-formed entry whose `command` is `--url`, and a probe reading only
    "did a record appear" reports that as a measured remote spelling. The discriminator is the
    transport field the CLI writes, checked against the presence of a command.

    A BOOLEAN AS WELL AS PROSE, because the prose was all there was and `main` only printed it
    — the same advertised-fact-with-no-verdict defect the gating probes had twice. And the
    check is now the FULL shape the gating probe will go on to write by hand: the declared
    `type`, a `url`, the `headers` the credential travels in, and the `tools` allowlist that is
    the entire subject of these probes. Confirming `url` alone would leave the other three
    resting on documentation (review, PR #110).
    """
    if not isinstance(body, dict):
        return False, "absent"
    kind, url, command = body.get("type"), body.get("url"), body.get("command")
    if command is not None and url is None:
        return False, f"LOCAL (type={kind!r}, command={command!r}) — the remote add did not take"
    if url is None:
        return False, f"no url and no command (type={kind!r}) — unreadable"
    missing = [k for k in ("headers", "tools") if k not in body]
    if kind != want_type:
        return False, (f"url present but type={kind!r}, not {want_type!r} — the transport "
                       f"discriminator is what the gating probe writes, so a different one "
                       f"means it is writing a config copilot does not produce")
    if missing:
        return False, (f"remote type={kind!r} with a url, but no {missing} — the credential "
                       f"and the allowlist are the two things §8's pattern is made of")
    # PRESENCE IS NOT SHAPE, and checking only presence passed `headers: []` and
    # `tools: "wrong"` — two values §8's pattern cannot be built from, filed as confirmation
    # that it can (review, PR #110). What the gating probes write by hand is a header MAPPING
    # carrying a bearer and a LIST of tool names, so that is what has to come back.
    headers, tools = body.get("headers"), body.get("tools")
    if not isinstance(headers, dict):
        return False, f"headers is {type(headers).__name__}, not a mapping: {headers!r}"
    auth = next((v for k, v in headers.items() if k.lower() == "authorization"), None)
    if not (isinstance(auth, str) and auth.startswith("Bearer ") and auth[7:].strip()):
        return False, (f"headers carries no usable `Authorization: Bearer <token>` — the "
                       f"credential half of §8's pattern is the whole reason for `headers`: "
                       f"{sorted(headers)!r}")
    if not (isinstance(tools, list) and tools
            and all(isinstance(t, str) and t for t in tools)):
        return False, (f"tools is {tools!r}, not a non-empty list of tool names — an allowlist "
                       f"that is not a list of names is not the thing under test")
    return True, (f"remote: type={kind!r}, url present, bearer in headers, tools present "
                  f"({tools!r})")


def classify_keys(config: dict, expected: dict) -> dict[str, str]:
    """Per intended key: confirmed, `differs:<found>`, or unexercised.

    A NAMED FUNCTION over parsed input, so `verify_mcp_fixtures.py` can drive every branch on
    synthetic configs without a copilot install — §4's rule that a probe's classification is
    under test like anything else.
    """
    container = expected["servers_container"]
    found_container = next((k for k in config if isinstance(config.get(k), dict)
                            and config[k]), None)
    out: dict[str, str] = {}
    if found_container is None:
        return {name: UNEXERCISED for name in expected}
    out["servers_container"] = (CONFIRMED if found_container == container
                                else f"differs:{found_container}")
    servers = config[found_container]
    body = next((v for v in servers.values() if isinstance(v, dict)), {})
    for name, key in expected.items():
        if name == "servers_container":
            continue
        out[name] = CONFIRMED if key in body else UNEXERCISED
    return out


def add_server(home: str, name: str, argv_tail: list[str]) -> tuple[int, str]:
    """One `copilot mcp add`, entirely inside the throwaway home."""
    env = dict(os.environ)
    env["COPILOT_HOME"] = home
    try:
        done = subprocess.run(["copilot", "mcp", "add", name, *argv_tail],
                              capture_output=True, text=True, timeout=DEADLINE, env=env)
    except FileNotFoundError:
        return 127, "copilot is not on PATH"
    except subprocess.TimeoutExpired:
        return 124, f"copilot exceeded {DEADLINE}s"
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def exit_code(differs: list, surprises: list, remote_ok: bool, version_ok: bool) -> int:
    """The probe's verdict over its three findings, as a function rather than an expression.

    EXTRACTED SO IT CAN BE DRIVEN. `remote_shape` was already a named function and already had
    a mutation, and neither established that `main` ACTED on what it returned — the exit status
    read the stdio half alone, so a remote add that silently filed a LOCAL entry left the probe
    green. Testing a classifier proves the classifier; the verdict is a separate claim and needs
    a separate one (review, PR #110).

    A CONJUNCTION OVER EVERY FINDING, not a lookup on the most recent: a differing key, a key
    nobody planned for, an unconfirmed remote shape and an unidentifiable build are four
    independent ways this probe's answer is not the one the design assumed, and all four are
    true of the same run.

    `version_ok` JOINS THEM for the reason the others do. And note what had to change for
    `remote_ok` to matter at all: `type` was a permanent member of `surprises`, so this
    returned 1 on every real run and no other axis could move it — a conjunction is only a
    conjunction while each term can be false (review, PR #110).
    """
    return 1 if (differs or surprises or not remote_ok or not version_ok) else 0


def version_verdict(rc: int, out: str, err: str) -> tuple[str, bool]:
    """(text, usable) for a `copilot --version` result — and it is a PREFLIGHT reading.

    THIS PROBE CANNOT DO BETTER, and that is worth stating rather than papering over. Its two
    siblings recover the version from the run's own stream, because copilot's launcher can
    resolve different cached code between two executions. `copilot mcp add` emits no such
    stream, so the spellings this probe reports are attributed to a build it identified in a
    DIFFERENT execution. It reports them as UNVERIFIED for that reason; the alternative — a
    version this file simply asserts — would be the stronger-sounding and weaker claim.

    PURE, so §E19 can drive every branch without a copilot install — and SEPARATE from the
    subprocess call for the same reason every other classifier here is. A measurement whose
    write-up is qualified "at 1.0.79" is worth nothing if the run could not read a version:
    this used to return a string like "(version unreadable: ...)" and `main` carried on, so
    the probe would print a verdict attributed to a build it never identified (review, PR #110).
    """
    text = (out or "").strip() or (err or "").strip()
    if rc != 0:
        return (text or f"exit {rc}"), False
    # SHAPE-CHECKED: `rc == 0` with any text used to count, so a run whose only output was a
    # warning returned "usable" while naming no version (review, PR #110).
    return (text, bool(_VERSION_RE.search(text)))


def cli_version() -> tuple[str, bool]:
    """`copilot --version`, and whether it is usable. See `version_verdict`."""
    try:
        done = subprocess.run(["copilot", "--version"], capture_output=True, text=True,
                              timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"could not run `copilot --version`: {exc!r}", False
    return version_verdict(done.returncode, done.stdout, done.stderr)


def main() -> int:
    home = tempfile.mkdtemp(prefix="probe-copilot-home-")
    try:
        version, version_ok = cli_version()
        print(f"copilot: {version}  (UNVERIFIED — read from a separate execution; "
              f"`copilot mcp add` emits no in-band witness)")
        if not version_ok:
            print("  VERSION UNREADABLE: no version at all, so the spellings below name no "
                  "build even weakly")
        # AN ENV VAR IS SET ON THE STDIO ADD, so `env` is exercised rather than reported
        # `unexercised` — the PR body claimed every listed spelling was confirmed while this
        # run never gave copilot an env var to write (review, PR #110).
        rc, out = add_server(home, "probe_stdio",
                             ["--env", "PROBE_KEY=PROBE_VALUE",
                              "--", sys.executable, "-c", "pass"])
        print(f"`copilot mcp add` (stdio): rc={rc}")
        if rc != 0:
            print("  " + out.strip()[:800].replace("\n", "\n  "))
            print("  the subcommand's own spelling may differ; `copilot mcp add --help` is "
                  "the next step, and this probe reports rather than guesses")
            return 1
        path = find_config(home)
        if path is None:
            print(f"  no {CONFIG_NAME} anywhere under COPILOT_HOME — the file's LOCATION is "
                  f"part of the finding, so this is a result rather than a crash")
            return 1
        try:
            with open(path, encoding="utf-8") as handle:
                config = json.load(handle)
        except (OSError, ValueError) as exc:
            print(f"  {path} is not readable JSON: {exc}")
            return 1
        print(f"  wrote {os.path.relpath(path, home)}")
        verdicts = classify_keys(config, EXPECTED)
        for name in EXPECTED:
            print(f"    {name:<18} {verdicts.get(name, UNEXERCISED)}")
        print(f"  raw: {json.dumps(config)[:600]}")
        # `unexercised` is not a failure: this run set a stdio command and nothing else, so
        # `url`, `headers` and `tools` are expected to be absent unless the subcommand offers
        # flags for them. Reporting them as their own state is what keeps a later reader from
        # recording "copilot has no `tools` key" on the strength of a run that never asked.
        differs = sorted(k for k, v in verdicts.items() if v.startswith("differs:"))
        surprises = unexpected_keys(config, EXPECTED)
        print(f"  keys that DIFFER from what the adapter intends: {differs or 'none'}")
        print(f"  keys copilot wrote that the adapter does NOT know about: "
              f"{surprises or 'none'}")

        # A SECOND SERVER, remote, because the whole point of §8's pattern is a `url` and the
        # stdio add above cannot say how copilot spells one. Reported separately and never
        # fatal: a subcommand with no remote flag is a finding about the subcommand, not a
        # broken probe, and `probe_copilot_gating.py` can still write a config by hand.
        # `--transport http <name> <url>`, and the URL is POSITIONAL — read from
        # `copilot mcp add --help` rather than guessed. The first version of this call passed
        # `-- --url <url>`, which the subcommand cheerfully accepted as a *command* named
        # `--url`, producing a `"type": "local"` entry that looked like a remote one had been
        # written. An instrument that files a malformed request under a plausible answer is
        # worse than one that fails (§4), which is what `remote_shape` below is for.
        # BOTH REMOTE TRANSPORTS, because the schema admits both and the gating probe writes
        # whichever the harness's server needs. Measuring only Streamable HTTP and reporting
        # "the remote spelling" is §4's promise-wider-than-the-mechanism, in an instrument.
        remote_ok, remote_seen = True, []
        for kind in ("http", "sse"):
            rc2, out2 = add_server(home, f"probe_remote_{kind}",
                                   ["--transport", kind, "https://example.invalid/mcp",
                                    "--header", "Authorization: Bearer PROBE_SENTINEL",
                                    "--env", "PROBE_KEY=PROBE_VALUE", "--tools", "echo,add"])
            print(f"`copilot mcp add --transport {kind}`: rc={rc2}")
            remote_path = find_config(home)
            if rc2 != 0 or not remote_path:
                print("  " + out2.strip()[:500].replace("\n", "\n  "))
                print(f"  -> the {kind} spelling is UNMEASURED by this route, which is a "
                      f"finding about the subcommand and not a measurement")
                remote_ok = False
                continue
            try:
                with open(remote_path, encoding="utf-8") as handle:
                    after = json.load(handle)
                body = (after.get("mcpServers") or {}).get(f"probe_remote_{kind}")
            except (OSError, ValueError) as exc:
                print(f"  unreadable after the {kind} add: {exc}")
                remote_ok = False
                continue
            shaped, why = remote_shape(body, want_type=kind)
            print(f"  entry: {json.dumps(body)[:400]}")
            print(f"  shape: {why}")
            remote_seen.append(kind)
            remote_ok = remote_ok and shaped
            # THE KEYS THE GATING PROBE WRITES BY HAND, checked against what copilot writes
            # for itself. Those two disagreeing is precisely how a gating run measures a
            # config the CLI would never produce and reports the result as copilot's.
            for surprise in unexpected_keys({"mcpServers": {"x": body or {}}}, EXPECTED):
                print(f"  key copilot wrote that the adapter does NOT know about: {surprise}")
                surprises = sorted(set(surprises) | {surprise})
        # `remote_ok` JOINS THE VERDICT rather than being printed beside it. `remote_shape`
        # was prose in a `print` and the exit status read only the stdio half, so a remote add
        # that silently filed a LOCAL entry left this probe green — the same defect the two
        # gating probes carried, in the third file (review, PR #110).
        if not remote_ok:
            print("  REMOTE SPELLING UNCONFIRMED: the entries above are not the shape §8's "
                  "pattern needs, so the gating probes' hand-written config is not backed by "
                  "anything copilot produced")
        return exit_code(differs, surprises, remote_ok, version_ok)
    finally:
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
