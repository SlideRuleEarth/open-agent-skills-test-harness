#!/usr/bin/env python3
"""Probe C3-2 — does this CLI PIPELINE requests behind `initialize`?

    python3 harness/tools/probe_mcp_pipelining.py [claude|codex|copilot|agy] ...

THE QUESTION, and why it is worth a live run. `DESIGN_MCP_Support.md` §10.2 REFUSES a bare
request until an era is actually established, and a pending `initialize` does not establish
one. A proxy is structurally more exposed to pipelining than the server it fronts: the server
ANSWERS `initialize` before reading the next line, so by then its state is settled, while a
proxy only forwards and therefore meets a pipelined request with the negotiation still in
flight. If a CLI pipelines, that cell fails and nothing else in the fleet objects — the worst
failure shape §10.5 names, correct by the specification and broken in practice. This probe is
what prices that refusal: it is the measurement, not the argument.

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
    timeline = id_timeline(recs)
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
        # C3-3 rides along free: the shim already records arrivals and departures, so the id
        # timeline is a READING of this same run rather than another one. See `id_findings`.
        "id_timeline": timeline,
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


# The five outcomes one CLI can have. They are NAMED and derived by a function rather than
# decided inline while printing, because the classification is the probe's actual result — the
# reading a design decision gets justified by — and an inline `elif` chain in `main()` cannot be
# checked by anything. `verify_mcp_fixtures.py` E14 drives these on synthetic rows; E13 checks
# the shim's timing path, which is a different instrument and was the only one covered (review,
# PR #100).
NO_ERA = "no_era"                # never handshook: the question was not put
NOT_APPLICABLE = "n/a"           # modern era OBSERVED, so there is no `initialize` to hide behind
UNMEASURED = "unmeasured"        # an era was observed, but the delay window never ran
PIPELINES = "pipelines"          # requests arrived while the response was held
WAITS = "waits"                  # nothing arrived until the response went out


# How many requests one connection must carry before "no reuse observed" is worth anything.
# NOT a threshold that makes C3-3 a priced trade — see `id_summary`. It only separates runs
# that could have shown reuse from runs that structurally could not.
ID_SAMPLE_FLOOR = 2


def id_timeline(recs: list[dict]) -> list[tuple[str, tuple]]:
    """Request arrivals and response departures, IN WIRE ORDER, as the proxy would see them.

    §10.4 spends a request id for the connection once it has reached the server, which refuses
    a client behaviour the spec permits: reuse after a response. That refusal needs a price,
    and the price is "does any shipped CLI reuse ids". The shim records both halves, so this
    run already contains the reading and no separate probe has to be paid for.

    Read from the shim's structured events, NOT by reparsing its `rx` record. `rx` truncates at
    4096 characters as a diagnostic, so reparsing it drops any longer request entirely — and a
    measurement that silently omits requests cannot answer a question about reuse (review, PR
    #100). Ids arrive already canonicalized to `mcp_proxy.request_id_key`'s identity.
    """
    kinds = {"request_id": "req", "response_id": "resp"}
    return [(kinds[r["event"]], tuple(r["id_key"])) for r in recs
            if r.get("event") in kinds and isinstance(r.get("id_key"), list)]


def id_findings(timeline: list[tuple[str, tuple]]) -> dict:
    """Separate the two ways an id can repeat. THEY ARE DIFFERENT FINDINGS.

    A repeat while the first request is still UNANSWERED is a live duplicate, which JSON-RPC
    forbids outright — "the request ID MUST NOT match the ID of any other request the sender
    has issued and not yet received a response for" — and which the proxy refuses as
    `duplicate_request_id`. It says nothing about the stricter rule.

    A repeat AFTER the response went out is the post-response reuse the spec permits and §10.4
    refuses. That, and only that, prices the rule.

    Conflating them meant a client that pipelined `ping(1)` behind a held `initialize(1)` was
    reported as proof that §10.4 "refuses a client behaviour the spec permits" — when the
    proxy would have refused it anyway, for violating JSON-RPC (review, PR #100).

    CLASSIFICATION STOPS AT THE FIRST LIVE DUPLICATE, because the proxy does. A duplicate is
    `Fail`, and `Fail` is terminal (§10.5) — the connection is torn down, so nothing after it
    would ever have been seen by the rule being priced. Counting a later reuse anyway reported
    that §10.4 "causes failure" using traffic that never reaches §10.4 (review, PR #100).
    """
    live: set = set()
    answered: set = set()
    duplicates: list = []
    reuse: list = []
    requests = 0
    for kind, key in timeline:
        if kind == "req":
            requests += 1
            if key in live:
                duplicates.append(key)
                break            # terminal: the proxy fails the connection here
            if key in answered:
                reuse.append(key)
            live.add(key)
        else:
            live.discard(key)
            answered.add(key)
    return {"requests": requests, "live_duplicates": duplicates,
            "post_response_reuse": reuse,
            # True when the run was cut short: whatever came after is unobservable through a
            # proxy, so this row can neither price the rule nor be counted as clean past here.
            "truncated": bool(duplicates)}


def request_ids(timeline: list[tuple[str, tuple]]) -> list:
    """The arrival-ordered request ids from a timeline, for display."""
    return [key for kind, key in timeline if kind == "req"]


def reuses_ids(row: dict) -> bool | None:
    """Did this CLI reuse an id AFTER it was answered? `None` when the run cannot say.

    `True` is a real answer — the exact behaviour §10.4 refuses and the spec allows. `False`
    means only "not among the requests this run happened to contain", which is why
    `id_summary` never turns it into a fleet-wide price. A live duplicate is neither: it is
    reported separately, because the proxy rejects it on grounds that have nothing to do with
    the rule being priced.
    """
    found = id_findings(row.get("id_timeline") or [])
    if found["requests"] < ID_SAMPLE_FLOOR:
        return None
    return bool(found["post_response_reuse"])


def classify(row: dict) -> str:
    if not row["connected"]:
        return NO_ERA
    if row["pipelining"] is None:
        # "No pipelining record" alone would say the same thing about a CLI that died before
        # handshaking, so `n/a` is claimed only for an era actually observed as MODERN — a
        # modern client sends no `initialize`, so the window cannot open and that is an ANSWER.
        return NOT_APPLICABLE if row["era"]["era"] == "modern" else UNMEASURED
    return PIPELINES if row["pipelining"]["pipelined"] else WAITS


def verdict(row: dict) -> str:
    kind = classify(row)
    if kind == NO_ERA:
        return ("NO ERA OBSERVED — the CLI never handshook"
                + (" (server was spawned)" if row["spawned"] else " (server never ran)"))
    if kind == NOT_APPLICABLE:
        return "n/a — no `initialize` (modern era, per-request metadata)"
    if kind == UNMEASURED:
        return (f"NOT MEASURED — era {row['era']['era']} was observed but the "
                f"`initialize` window never ran")
    if kind == PIPELINES:
        return (f"PIPELINES — {row['pipelining']['count']} request(s) arrived while the "
                f"response was held: {row['pipelining']['methods']}")
    return "waits for the response before sending anything else"


def unmeasured(rows: list[dict]) -> list[str]:
    """The CLIs whose answer is missing — BOTH ways of missing it.

    A CLI that never handshook, and one that handshook in a legacy era where the window never
    ran, are equally unanswered. Only an observed modern era licenses `n/a`.
    """
    return [r["cli"] for r in rows if classify(r) in (NO_ERA, UNMEASURED)]


def summary(rows: list[dict]) -> list[str]:
    """The closing paragraphs. This is what a reader acts on, so it states the design as it
    IS — §10.2 refuses a request behind an unanswered `initialize` — and it never states a
    FLEET-WIDE conclusion that the rows do not support.

    A negative claim needs every row answered. An earlier version printed "No CLI pipelined ...
    costs the fleet nothing" whenever no row was positive, which is true of a run where every
    single CLI failed to connect; it then contradicted itself two lines later with the list of
    what had not been measured (review, PR #100). Absence of a positive result is not a
    negative result, which is the same distinction `classify` draws per row, applied to the
    fleet.
    """
    out = []
    pipelines = [r["cli"] for r in rows if classify(r) == PIPELINES]
    missing = unmeasured(rows)
    if pipelines:
        out.append(f"AT LEAST ONE CLI PIPELINES: {', '.join(pipelines)}. §10.2 refuses a "
                   f"request sent behind an unanswered `initialize`, so those cells FAIL "
                   f"today. The answer is the defer-and-replay action §10.2 names — hold the "
                   f"pipelined request until the negotiation completes — not reopening the "
                   f"gate: a pending negotiation cannot govern the traffic it would admit.")
    elif missing:
        out.append(f"No MEASURED CLI pipelined, and that is all this run says: "
                   f"{len(missing)} of {len(rows)} did not answer the question. Whether "
                   f"§10.2's refusal of a request behind a pending handshake costs the fleet "
                   f"anything is UNKNOWN until they do — an unmeasured CLI is exactly where a "
                   f"'correct by the specification, broken in practice' failure hides (§10.5).")
    else:
        out.append("No CLI pipelined, across the whole fleet. §10.2's refusal of a request "
                   "behind a pending handshake therefore costs the fleet nothing — which is "
                   "what makes it a priced trade rather than a gamble, not what makes it safe. "
                   "SHOULD NOT is not MUST NOT, and one CLI release turns this into the "
                   "defer-and-replay action.")
    if missing:
        why = {r["cli"]: ("never handshook" if classify(r) == NO_ERA
                          else f"{r['era']['era']} era observed, but the window never ran")
               for r in rows if r["cli"] in missing}
        out.append("NOT MEASURED: " + ", ".join(f"{c} ({why[c]})" for c in missing)
                   + " — an unanswered question, not a negative result.")
    out.extend(id_summary(rows))
    return out


def id_summary(rows: list[dict]) -> list[str]:
    """C3-3's paragraph. A POSITIVE result is conclusive; a negative one never is.

    This asymmetry is the finding, not a hedge. Pipelining is exercised exactly once per
    connection — the `initialize` window either has traffic in it or does not — so one clean
    run per CLI settles C3-2. ALLOCATION IS NOT LIKE THAT: a run that sends ids 0 and 1 says
    nothing about what the fourth request will use, and a cell runs far longer than a probe
    does. An earlier version treated any two distinct ids as a completed answer and went on to
    claim the rule "costs the fleet nothing", which is a conclusion about sessions it never
    observed (review, PR #100).

    So the negative branch reports the SAMPLE and calls the rule unpriced, every time. Pricing
    it needs a workload that actually stresses the allocator — many `tools/call`s on one
    connection — which is a probe of its own, named in §9.
    """
    found = {r["cli"]: id_findings(r.get("id_timeline") or []) for r in rows}
    reusing = [c for c, f in found.items() if f["post_response_reuse"]]
    duplicating = [c for c, f in found.items() if f["live_duplicates"]]
    out = []
    if reusing:
        out.append(f"C3-3 — IDS ARE REUSED AFTER BEING ANSWERED by: {', '.join(reusing)}. "
                   f"§10.4 spends an id once it reaches the server, so those cells FAIL. That "
                   f"is the rule refusing a client behaviour the spec permits, and it is the "
                   f"fleet declining to pay for it. A positive result needs no sample size.")
    else:
        counts = ", ".join(f"{c}={f['requests']}" for c, f in found.items())
        total = sum(f["requests"] for f in found.values())
        out.append(f"C3-3 — no post-response reuse OBSERVED, over {total} request(s) total "
                   f"({counts}). This does NOT price §10.4's spent-id rule and must not be "
                   f"read as doing so: allocation is not exercised once per connection the way "
                   f"pipelining is, so a short run says nothing about the id the next request "
                   f"will carry. The rule stays a deliberate strictness until a stress "
                   f"workload measures it (§9).")
    if duplicating:
        # Reported, but NOT as a price for the spent-id rule: this is the weaker rule, and the
        # proxy already refuses it as `duplicate_request_id` on JSON-RPC's own terms. The run
        # also ENDS there as far as any proxied cell is concerned, so the rest of the log is
        # not evidence about anything.
        out.append(f"C3-3, separately — LIVE DUPLICATE ids from: {', '.join(duplicating)}: an "
                   f"id repeated while the first request was still unanswered. JSON-RPC "
                   f"forbids that outright, so the proxy refuses it as `duplicate_request_id` "
                   f"regardless of §10.4's stricter rule — a finding about the CLI, not a "
                   f"price for that rule. Those cells fail at that message, so nothing later "
                   f"in the run is evidence either way and it is not counted.")
    return out


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
        era = f"{r['era']['era']}/{r['era']['version']}" if r["era"] else "-"
        print(f"{cli:8} {era:22} rc={str(r['rc']):5} {r['elapsed_s']:>6}s  {verdict(r)}")
        reuse = {True: "REUSES IDS", False: "no reuse in this sample",
                 None: "too few requests to tell"}[reuses_ids(r)]
        shown = [k[1] for k in request_ids(r["id_timeline"])]
        print(f"{'':8} ids: {shown} — {reuse}")

    print()
    for paragraph in summary(rows):
        print(paragraph)
    return 1 if unmeasured(rows) else 0


if __name__ == "__main__":
    sys.exit(main())
