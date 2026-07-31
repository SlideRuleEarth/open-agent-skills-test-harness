#!/usr/bin/env python3
"""Probe C3-2 — does this CLI PIPELINE requests behind `initialize`?

    python3 harness/tools/probe_mcp_pipelining.py [claude|codex|copilot|agy] ...

THE QUESTION, and why it is worth a live run. `DESIGN_MCP_Support.md` §10.2 gates a bare
request on an era actually being established. A proxy is structurally more exposed to
pipelining than the server it fronts: the server ANSWERS `initialize` before reading the
next line, so by then its state is settled, while a proxy only forwards and therefore meets
a pipelined request with the negotiation still in flight. If a CLI pipelines and the proxy
refuses, that cell fails and nothing else in the fleet objects — the worst failure shape
§10.5 names, correct by the specification and broken in practice.

WHY NO EARLIER LOG COULD ANSWER IT. Every C3-0/C3-1 run used the same shim, and the shim
answers `initialize` immediately, so a pipelined request is read after the era exists and
looks exactly like a well-behaved one. Timestamps do not separate them either: both show
the next line arriving right after the response goes out. The instrument had to change —
`PROBE_MCP_INIT_DELAY_MS` holds the response and watches the pipe — which is C3-1's lesson
applied again: fix the instrument before trusting the reading.

COST, because three of these are real model calls. `claude mcp list` spawns stdio servers
for a health check and reaches a full handshake for free. codex, copilot and agy have no
equivalent — their `mcp list` reads configuration without connecting — so each needs one
cheap non-interactive run. The prompt is irrelevant to the measurement: the handshake
happens at client startup, before the model is reached.

ISOLATION. Every CLI gets `build_isolated_home()` with the ordinary symlink overlay, so
keychain and token auth still work, plus a materialized config file declaring the probe
server. Nothing writes to the real HOME, and no `mcp add` is ever run.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentskill_evals.isolation import build_isolated_home  # noqa: E402

HARNESS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHIM = os.path.join(HARNESS, "fixtures", "probe_era_mcp_server.py")
# The harness's own interpreter: the one guaranteed to exist at an absolute path outside
# HOME, so a masked or contained home cannot affect it (§10.3 makes the same choice).
PYTHON = sys.executable
HELD_MS = 1200
PROMPT = "Reply with exactly: OK"


def _server_env(log: str) -> dict:
    return {"PROBE_MCP_LOG": log, "PROBE_MCP_INIT_DELAY_MS": str(HELD_MS)}


def _stdio_json(log: str, *, type_key: bool = False) -> dict:
    entry = {"command": PYTHON, "args": [SHIM], "env": _server_env(log)}
    if type_key:
        entry["type"] = "local"
        entry["tools"] = ["*"]
    return {"mcpServers": {"probe": entry}}


def _toml(log: str) -> str:
    env = ", ".join(f'{k} = "{v}"' for k, v in _server_env(log).items())
    return ("[mcp_servers.probe]\n"
            f'command = "{PYTHON}"\n'
            f'args = ["{SHIM}"]\n'
            f"env = {{ {env} }}\n")


# Each entry: the config file to materialize in the isolated home, its content, and the argv.
# `home_env` names the variable that points the CLI at the isolated home — codex reads
# CODEX_HOME rather than HOME for its own state, and got that wrong once already (#88).
def _plan(cli: str, log: str, ws: str, home: str) -> dict:
    if cli == "claude":
        # `.mcp.json` in the WORKSPACE, plus `mcp list`, which "spawns stdio servers for
        # health checks" — a complete handshake with no model call.
        with open(os.path.join(ws, ".mcp.json"), "w") as fh:
            json.dump(_stdio_json(log), fh)
        return {"argv": ["claude", "mcp", "list"], "mask": None, "model": None}
    if cli == "codex":
        return {"argv": ["codex", "exec", "--skip-git-repo-check",
                         "-m", "gpt-5.4-mini", PROMPT],
                "mask": (".codex/config.toml", _toml(log)),
                "env": {"CODEX_HOME": os.path.join(home, ".codex")},
                "model": "gpt-5.4-mini"}
    if cli == "copilot":
        return {"argv": ["copilot", "-p", PROMPT, "--model", "claude-haiku-4.5",
                         "--allow-all-tools", "--disable-builtin-mcps"],
                "mask": (".copilot/mcp-config.json",
                         json.dumps(_stdio_json(log, type_key=True))),
                "model": "claude-haiku-4.5"}
    if cli == "agy":
        return {"argv": ["agy", "-p", PROMPT, "--model", "gemini-3.5-flash-low",
                         "--dangerously-skip-permissions"],
                "mask": (".gemini/config/mcp_config.json", json.dumps(_stdio_json(log))),
                "model": "gemini-3.5-flash-low"}
    raise SystemExit(f"unknown CLI {cli!r}")


def _read_log(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except (json.JSONDecodeError, ValueError):
                    pass
    return out


def probe(cli: str, *, timeout: int, verbose: bool) -> dict:
    home = tempfile.mkdtemp(prefix=f"pipeline-home-{cli}-")
    ws = tempfile.mkdtemp(prefix=f"pipeline-ws-{cli}-")
    log = os.path.join(tempfile.mkdtemp(prefix=f"pipeline-log-{cli}-"), "probe.jsonl")
    plan = _plan(cli, log, ws, home)
    masks = {plan["mask"][0]: plan["mask"][1]} if plan["mask"] else {}
    build_isolated_home(home, [], [], [], os.path.expanduser("~"),
                        config_file_masks=masks)

    env = dict(os.environ, HOME=home, **plan.get("env", {}))
    started = time.monotonic()
    try:
        proc = subprocess.run(plan["argv"], cwd=ws, env=env, capture_output=True,
                              text=True, timeout=timeout)
        rc, out, err = proc.returncode, proc.stdout, proc.stderr
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        rc, out, err, timed_out = None, exc.stdout or "", exc.stderr or "", True
    elapsed = time.monotonic() - started

    recs = _read_log(log)
    hit = next((r for r in recs if r["event"] == "pipelining"), None)
    era = next((r for r in recs if r["event"] == "era"), None)
    # `connected` means AN ERA WAS OBSERVED, not "the log exists". The shim writes `start`
    # before reading a byte, so a CLI that spawns the server and dies would otherwise count as
    # connected — and then, having sent no `initialize`, be classified as modern with nothing
    # to pipeline behind. A false negative reported as a clean result, which is the one
    # outcome a probe must not produce (review, PR #100).
    result = {
        "cli": cli,
        "model": plan["model"],
        "connected": era is not None,
        "spawned": bool(recs),
        "pipelining": hit,
        "era": era,
        "rc": rc,
        "timed_out": timed_out,
        "elapsed_s": round(elapsed, 1),
    }
    if verbose:
        print(f"--- {cli} argv: {' '.join(plan['argv'])}")
        for r in recs:
            print("   ", json.dumps(r, sort_keys=True)[:200])
        if err.strip():
            print("    stderr:", err.strip()[:400])
        if out.strip():
            print("    stdout:", out.strip()[:400])
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("clis", nargs="*", default=["claude", "codex", "copilot", "agy"],
                    help="which CLIs to probe (default: all four)")
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(SHIM):
        raise SystemExit(f"shim not found: {SHIM}")

    print(f"C3-2 — pipelining, holding each `initialize` response for {HELD_MS} ms\n")
    rows = []
    for cli in args.clis:
        r = probe(cli, timeout=args.timeout, verbose=args.verbose)
        rows.append(r)
        if not r["connected"]:
            verdict = ("NO ERA OBSERVED — the CLI never handshook"
                       + (" (server was spawned)" if r["spawned"] else " (server never ran)"))
        elif r["era"]["era"] == "modern" and r["pipelining"] is None:
            # A modern client sends no `initialize` at all, so the window never opens. That
            # is an ANSWER, not a gap: there is no pre-negotiation state to pipeline behind.
            # It is claimed only for an era actually OBSERVED as modern — "no pipelining
            # record" on its own would say the same thing about a CLI that died first.
            verdict = "n/a — no `initialize` (modern era, per-request metadata)"
        elif r["pipelining"] is None:
            verdict = (f"NOT MEASURED — era {r['era']['era']} was observed but the "
                       f"`initialize` window never ran")
        elif r["pipelining"]["pipelined"]:
            verdict = (f"PIPELINES — {r['pipelining']['count']} request(s) arrived while the "
                       f"response was held: {r['pipelining']['methods']}")
        else:
            verdict = "waits for the response before sending anything else"
        era = f"{r['era']['era']}/{r['era']['version']}" if r["era"] else "-"
        print(f"{cli:8} {era:22} rc={str(r['rc']):5} {r['elapsed_s']:>6}s  {verdict}")

    print()
    # Unmeasured means BOTH "never handshook" and "handshook in the legacy era but the
    # window never ran". Only an observed modern era licenses "n/a".
    unknown = [r["cli"] for r in rows
               if not r["connected"]
               or (r["pipelining"] is None and r["era"]["era"] != "modern")]
    pipelines = [r["cli"] for r in rows
                 if r["pipelining"] and r["pipelining"]["pipelined"]]
    if pipelines:
        print(f"AT LEAST ONE CLI PIPELINES: {', '.join(pipelines)}. §10.2's rule lets a "
              f"request behind an unanswered `initialize` through, so this is supported — "
              f"but it is now load-bearing rather than defensive, and must stay that way.")
    else:
        print("No CLI pipelined. §10.2's allowance for a pending handshake is therefore "
              "defensive rather than load-bearing — keep it: SHOULD NOT is not MUST NOT, "
              "and this is one CLI release away from changing.")
    if unknown:
        print(f"NOT MEASURED (no handshake): {', '.join(unknown)} — an unanswered question, "
              f"not a negative result.")
    return 0 if not unknown else 1


if __name__ == "__main__":
    sys.exit(main())
