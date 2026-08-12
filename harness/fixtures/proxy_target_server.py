#!/usr/bin/env python3
"""A deliberately awkward MCP server, for driving the PROXY's teardown paths (§10.9).

`echo_mcp_server.py` is a well-behaved test double and stays that way — it is what the
`mcp_echo_*` scenarios run against, and 278 checks in `verify_mcp_fixtures.py` pin its
conformance. This is the opposite: a server that on request refuses to exit when its stdin
closes, writes on the way out, spawns a helper into its own process group, or lets that helper
escape the group entirely. Each of those exists because it is the only way to reach one of
§10.5.1's cleanup outcomes against the real program.

TWO ROLES, ONE FILE. `--helper` runs the credential-bearing helper the positive teardown case
needs. Splitting it into a second file would put the two halves of one topology in two places
that can drift; the roles share the announcement format and nothing else.

THE ANNOUNCEMENT IS THE WHOLE POINT OF THE HELPER, and it is nonce-bound (§10.9). The driver
generates the nonce per case and this process echoes it back, so a process left over from an
earlier case cannot answer for this one — the same reason instance ids exist in the audit log.
After announcing, the holder writes nothing more and holds its writer for the rest of its life,
which is what makes EOF on that pipe mean the holder is gone.

Everything is driven by environment variables because the proxy passes the config's `env` map
to its child verbatim, which is the only channel the driver has to this process.

    PT_NONCE          echoed in every announcement; required with either FD below
    PT_CHILD_FD       announce here and hold it — the CHILD liveness channel
    PT_HELPER_FD      spawn a helper that announces here and holds it — the HELPER channel
    PT_HELPER_ESCAPE  "1": the helper calls setsid, leaving the child's process group
    PT_HELPER_STDOUT  "1": the helper inherits this server's stdout, so it stays open after
                      this server exits — which is how the proxy's bounded drain is driven
    PT_LINGER         seconds to stay alive after stdin EOF, never exiting on its own
    PT_IGNORE_TERM    "1": ignore SIGTERM, so the proxy's escalation has to reach SIGKILL
    PT_CLOSE_STDIN    "1": close stdin at startup and never read it, so the proxy's next
                      write to this server fails
    PT_CLOSE_STDOUT   "1": close stdout when stdin reaches EOF, so the proxy's drain sees EOF
                      while this server is still ALIVE
    PT_NOTIFY_ECHO    "1": answer a NOTIFICATION with a notification of its own, which is how
                      the driver sees that one was forwarded rather than answered
    PT_FAREWELL       a JSON line to write when stdin reaches EOF, i.e. during the proxy's
                      step-2 drain rather than during ordinary forwarding
    PT_REPORT_ENV     comma-separated variable NAMES, each optionally ending in `*` to mean
                      "every name with this prefix"; the announcement reports which of them
                      this process can see. A prefix is what lets the driver ask "did ANY of
                      the proxy's own knobs arrive" without naming them — a question phrased
                      as a list of names cannot notice a name that left the list
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

# Far longer than any case, and far shorter than forever. "Must not exit on its own" (§10.9) is
# a requirement against the BOUNDED WAIT — a helper that outlived a crashed driver would be a
# leaked process holding a pipe, which is the thing these cases exist to detect.
HELPER_CEILING = 120.0

TOOLS = [
    {"name": "alpha", "description": "on-list", "inputSchema": {"type": "object"}},
    {"name": "beta", "description": "off-list", "inputSchema": {"type": "object"}},
]


def announce(fd: int, role: str) -> None:
    """Nonce-bound readiness, then silence for the rest of this process's life."""
    # NAMES IN, NAMES OUT — never values, and never the whole environment. What the driver
    # needs to know is whether a variable ARRIVED, and a child that shipped its environment
    # down a pipe would be putting the interpolated secrets of `env:` into an artifact for the
    # scrubber to catch, which is the one thing this program must never do.
    seen = []
    for pattern in (p for p in os.environ.get("PT_REPORT_ENV", "").split(",") if p):
        if pattern.endswith("*"):
            seen += [name for name in os.environ if name.startswith(pattern[:-1])]
        elif pattern in os.environ:
            seen.append(pattern)
    record = {"role": role, "nonce": os.environ.get("PT_NONCE", ""),
              "pid": os.getpid(), "pgid": os.getpgid(0), "env_seen": sorted(set(seen))}
    os.write(fd, (json.dumps(record) + "\n").encode("utf-8"))


def run_helper() -> int:
    fd = int(os.environ["PT_HELPER_FD"])
    if os.environ.get("PT_HELPER_ESCAPE") == "1":
        # OUT of the child's process group, so step 4's group signal does not reach it. That is
        # not a helper misbehaving: it is the case where something the proxy cannot terminate is
        # holding the child's stdout, which is what the bounded drain has to report.
        os.setsid()
    announce(fd, "helper")
    deadline = time.monotonic() + HELPER_CEILING
    while time.monotonic() < deadline:
        # Default SIGTERM disposition, on purpose: a group signal MUST kill this, and nothing
        # else may. It never reads stdin, so the client's stdin closing cannot end it either.
        time.sleep(0.05)
    return 0


def spawn_helper() -> None:
    fd = int(os.environ["PT_HELPER_FD"])
    stdout = sys.stdout if os.environ.get("PT_HELPER_STDOUT") == "1" else subprocess.DEVNULL
    # STDERR IS NOT INHERITED, and that is not tidiness. This server's stderr is the proxy's,
    # which is a pipe the driver holds — so a helper that inherited it would keep that pipe
    # open, and the driver's read of it would block until the helper exits. A liveness case
    # whose instrument deadlocks on the very process it is meant to observe surviving proves
    # nothing at all.
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "--helper"],
                     stdin=subprocess.DEVNULL, stdout=stdout, stderr=subprocess.DEVNULL,
                     pass_fds=(fd,))
    # CLOSED AS SOON AS IT IS HANDED ON. Every stage of the inheritance path closes its copy so
    # each channel has a SOLE WRITER — hold one here and the helper channel's EOF would be
    # reporting this server's exit rather than the helper's.
    os.close(fd)


def send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def answer(req_id, method: str, params: dict) -> None:
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": req_id, "result": {
            "protocolVersion": params.get("protocolVersion", "2025-11-25"),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "proxy-target", "version": "1.0.0"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}})
    elif method == "ping":
        send({"jsonrpc": "2.0", "id": req_id, "result": {}})
    elif method == "tools/call":
        send({"jsonrpc": "2.0", "id": req_id, "result": {
            "content": [{"type": "text", "text": params.get("name", "")}], "isError": False}})
    else:
        send({"jsonrpc": "2.0", "id": req_id,
              "error": {"code": -32601, "message": f"method not found: {method}"}})


def main() -> int:
    if "--helper" in sys.argv[1:]:
        return run_helper()

    if os.environ.get("PT_IGNORE_TERM") == "1":
        # So the proxy's step-3 escalation has to reach SIGKILL, which is the only way to
        # produce `shutdown_child_killed` — an outcome §10.5.1 classifies CLEAN, so the case is
        # as much about the proxy NOT failing the cell as about it recording the kill.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)

    if "PT_CHILD_FD" in os.environ:
        # The independent report. The proxy's spawn record is the proxy's account of what it
        # did, and these cases are about not taking the proxy's word for anything — so the
        # group id is read from inside the group by the process that is in it.
        announce(int(os.environ["PT_CHILD_FD"]), "child")
    if "PT_HELPER_FD" in os.environ:
        spawn_helper()

    if os.environ.get("PT_CLOSE_STDIN") == "1":
        # No reader on the other end of the proxy's pipe, so its next write raises EPIPE while
        # the connection is still LIVE — `child_write_failed`, not the shutdown-phase outcome.
        os.close(0)
    else:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("id") is None:
                if os.environ.get("PT_NOTIFY_ECHO") == "1":
                    send({"jsonrpc": "2.0", "method": "notifications/seen"})
                continue       # a notification is never ANSWERED, id or no id
            answer(msg["id"], msg.get("method") or "",
                   msg.get("params") if isinstance(msg.get("params"), dict) else {})

    farewell = os.environ.get("PT_FAREWELL")
    if farewell:
        # Written AFTER stdin reached EOF, so the proxy is already in step 2's drain. That is
        # the only phase in which a write to a departed client is `shutdown_write_failed`
        # (clean) rather than `client_write_failed` (an anomaly) — the same errno, two reasons,
        # and the phase is part of the reason rather than a flag a caller applies.
        try:
            sys.stdout.write(farewell.rstrip("\n") + "\n")
            sys.stdout.flush()
        except OSError:
            pass
    if os.environ.get("PT_CLOSE_STDOUT") == "1":
        # ALIVE, but with nothing more to say. That combination is what §10.9's teardown control
        # needs: the proxy's drain reaches EOF and settles `drain_ended` cleanly, while the
        # child and its group are still there for the suppressed steps 3-5 to have not touched.
        sys.stdout.close()
        os.close(1)
    linger = float(os.environ.get("PT_LINGER") or 0)
    if linger:
        # The spec only SHOULDs a prompt exit after stdin closes, so this is a conforming-enough
        # server that makes the proxy escalate — and forced termination is CLEAN in §10.5.1.
        time.sleep(linger)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (BrokenPipeError, KeyboardInterrupt):
        sys.exit(0)
