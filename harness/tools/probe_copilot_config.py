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
import shutil
import subprocess
import sys
import tempfile

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


def remote_shape(body: Any) -> str:
    """Whether an entry is really the remote form, or a remote request filed as a local one.

    Its own function because the failure it names is silent: `copilot mcp add name -- --url X`
    produces a perfectly well-formed entry whose `command` is `--url`, and a probe reading only
    "did a record appear" reports that as a measured remote spelling. The discriminator is the
    transport field the CLI writes, checked against the presence of a command.
    """
    if not isinstance(body, dict):
        return "absent"
    kind, url, command = body.get("type"), body.get("url"), body.get("command")
    if command is not None and url is None:
        return f"LOCAL (type={kind!r}, command={command!r}) — the remote add did not take"
    if url is None:
        return f"no url and no command (type={kind!r}) — unreadable"
    return f"remote: type={kind!r}, url key present, headers={'headers' in body}"


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


def main() -> int:
    home = tempfile.mkdtemp(prefix="probe-copilot-home-")
    try:
        rc, out = add_server(home, "probe_stdio",
                             ["--", sys.executable, "-c", "pass"])
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
        rc2, out2 = add_server(home, "probe_remote",
                               ["--transport", "http", "https://example.invalid/mcp",
                                "--header", "Authorization: Bearer PROBE_SENTINEL",
                                "--env", "PROBE_KEY=PROBE_VALUE", "--tools", "echo,add"])
        print(f"`copilot mcp add` (remote attempt): rc={rc2}")
        remote_path = find_config(home)
        if rc2 == 0 and remote_path:
            try:
                with open(remote_path, encoding="utf-8") as handle:
                    after = json.load(handle)
                body = (after.get("mcpServers") or {}).get("probe_remote")
                print(f"  probe_remote entry: {json.dumps(body)[:400]}")
                print(f"  remote shape: {remote_shape(body)}")
            except (OSError, ValueError) as exc:
                print(f"  unreadable after the remote add: {exc}")
        else:
            print("  " + out2.strip()[:500].replace("\n", "\n  "))
            print("  -> remote spelling UNMEASURED by this route; `copilot mcp add --help` "
                  "is the next step and the gating probe does not depend on it")
        return 1 if (differs or surprises) else 0
    finally:
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
