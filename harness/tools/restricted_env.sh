#!/bin/sh
# Reproduce a capability-restricted environment and run the suites inside it.
#
# The verifier's third result state (`skip()`) exists because some environments deny `ps` or
# `bind()`, and a section that CANNOT RUN is neither a pass nor a failure. This script is how
# that claim is exercised on a machine where those capabilities are available: it denies them
# on purpose, then runs the suites under each denial and under both.
#
#   harness/tools/restricted_env.sh                  the three verifier runs and the selftest
#   harness/tools/restricted_env.sh --payload CMD    run CMD in each phase instead (the tests)
#   harness/tools/restricted_env.sh --help
#
# EXIT STATUS MEANS "THIS WAS A REPRODUCTION", WHICH IS A CLAIM ABOUT THE SUITE RESULTS AND
# NOT MERELY ABOUT REACHING THEM. 0 means every phase produced the status AND the evidence a
# denied run must produce; 1 means it did not; 2 is usage; and >= 128 means A SIGNAL ENDED THE
# RUN -- 129/130/131/141/143 for HUP/INT/QUIT/PIPE/TERM, and exactly 128 for every other signal
# handled below, which is NOT 128+n (external review). The grouped handlers do not compute
# 128+n because signal NUMBERS are not portable: USR1 is 30 on macOS and the BSDs and 10 on
# Linux, so a hardcoded table would be quietly wrong somewhere. The five spelled out have had
# the same numbers everywhere for decades; the rest only promise ">= 128, ended by a signal".
#
# The first version of this script discarded the phase statuses and always exited 0, on the
# theory that the suites' own reports were the output and folding "the suite skipped sections"
# into a failure would hide the interesting case. That reasoning produced a script which
# reported SUCCESS while every suite result was wrong -- driving all four phases to exit 7
# still returned 0 (external review). It is the same fail-open class the rest of this file
# exists to remove, authored deliberately this time: a denial that silently does not take
# yields UNRESTRICTED green verifier runs and a green reproduction on top of them.
#
# So each phase declares what it must produce, and the evidence is the point: a status alone
# cannot tell "denied" from "the suite failed for another reason", and it cannot tell the
# ps-denied phase from the bind-denied one. The skip REASONS can, and unlike a skip count they
# do not drift when a section is added.
#
# Rationale for every guard below lives in harness/TODO_Contained_HOME.md section 4; the
# failure paths are driven by harness/tools/verify_restricted_env.py, which reads THIS FILE
# rather than a copy of it.

# The header block IS the help text, printed by walking it rather than by slicing fixed line
# numbers: the header has grown three times in review, and a hardcoded range silently truncates
# mid-sentence when it does.
usage() {
    awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"
}

PAYLOAD=""
while [ $# -gt 0 ]; do
    case "$1" in
        --payload)
            [ $# -ge 2 ] || { echo "restricted_env: --payload needs an argument" >&2; exit 2; }
            PAYLOAD="$2"
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            echo "restricted_env: unknown argument: $1" >&2
            exit 2
            ;;
    esac
done

CDPATH=''   # a set CDPATH makes `cd` resolve against surprising directories and echo the result
here=$(cd -P -- "$(dirname -- "$0")" && pwd) || exit 1
repo=$(cd -P -- "$here/../.." && pwd) || exit 1
cd "$repo" || exit 1

PY="$repo/harness/.venv/bin/python"
if [ -z "$PAYLOAD" ] && [ ! -x "$PY" ]; then
    echo "restricted_env: $PY is missing — run 'cd harness && make dev' first" >&2
    exit 1
fi

# ONE PARENT DIRECTORY, so there is a single allocation to guard and a single thing to remove.
# Two independent `mktemp -d` calls meant the second could fail AFTER the first succeeded, and
# the `exit` on that guard walked straight past the `rm -rf` at the bottom. That leak was only
# reachable by a stub failing the SECOND call, which an always-fail stub cannot produce — the
# hole and the test that missed it had the same shape (external review).
sandbox=$(mktemp -d) || exit 1
[ -n "$sandbox" ] && [ -d "$sandbox" ] || exit 1

# CLEANUP ON EVERY EXIT PATH THE SHELL CAN CATCH, INSTALLED BEFORE ANYTHING IS PUT IN IT. A
# `rm -rf` at the bottom runs only when the bottom is reached; a trap runs when the guards below
# fire too. Positional cleanup is what made the leak above possible at all.
#
# THE SIGNAL HANDLERS MUST *EXIT*, NOT MERELY CLEAN UP. A handler that runs `rm -rf` and RETURNS
# leaves the shell to carry on with the next command: the sandbox is gone, so PATH and PYTHONPATH
# name nothing, and the remaining phases execute with NO DENIAL IN PLACE — reporting a full green
# pass as though the restricted environment had been exercised. Interruption is fail-OPEN unless
# the handler ends the run (external review). `exit` from a handler still fires the EXIT trap.
#
# SENT signals are trapped; signals raised because the SHELL ITSELF FAULTED are not. An `XCPU`
# from a ulimit or an `ALRM` from a timeout wrapper arrives at a healthy shell that can be
# trusted to run `rm -rf`; ILL/TRAP/EMT/FPE/BUS/SEGV/SYS mean the interpreter is broken, and a
# destructive command issued from a faulted process is a worse bargain than a leaked 0700
# directory holding a fake `ps`. Those seven, plus the untrappable KILL, are the residual leak
# surface — measured per shell in verify_restricted_env.py, not asserted here.
#
# None of this can be left to the shell: EXIT-trap-runs-anyway is not a property of `sh`. Of the
# twenty catchable terminating signals, dash runs the EXIT trap for NONE, zsh for two, bash for
# eighteen, ksh for nineteen.
trap 'rm -rf "$sandbox"' EXIT
trap 'exit 129' HUP     # terminal closed, ssh dropped
trap 'exit 130' INT     # Ctrl-C
trap 'exit 131' QUIT    # Ctrl-\ quit from the keyboard (never end a comment with a backslash)
trap 'exit 141' PIPE    # output piped into something that exits first
trap 'exit 143' TERM    # kill, CI cancellation
# The rest of the sent set: a resource limit (XCPU, XFSZ), a timeout wrapper (ALRM), an abort,
# or a human with the wrong pid. ONE HANDLER, ONE STATUS: 128 exactly, NOT 128+n, because these
# signals' numbers differ across platforms — USR1 is 30 on macOS and the BSDs, 10 on Linux — so
# a hardcoded per-signal table would be quietly wrong somewhere. The five above keep 128+n only
# because their numbers have been the same everywhere for decades.
#
# 128 rather than 1 ON PURPOSE: 1 is this script's own "the expectations were not met" status,
# so reusing it for a signal would make a run KILLED halfway through indistinguishable from one
# that finished and found the denial had not taken. Those are opposite problems — an
# interruption, and a false reproduction.
trap 'exit 128' ABRT ALRM USR1 USR2 XCPU XFSZ VTALRM PROF

mkdir "$sandbox/nops" "$sandbox/nobind" || exit 1

# PRIVATE DIRECTORIES, because both are WRITTEN THROUGH. A predictable /tmp/<known-name> that
# already exists — a directory someone else owns, or a symlink — redirects the redirection into
# whatever it points at. `mktemp -d` creates 0700 in the PLATFORM-SELECTED temporary location,
# which is not necessarily $TMPDIR and on macOS is not: it uses the per-user Darwin temp dir and
# ignores TMPDIR outright. Do not read the sandbox's location off an environment variable; ask
# the shell where it went. The paths are quoted everywhere for the same reason (external review).
#
# Deny `ps`: the fake must be the ONLY thing on PATH, since execvp keeps searching after EACCES.
# Hence the absolute interpreter below — PATH no longer resolves `python3` either.
printf '#!/bin/sh\nexit 0\n' > "$sandbox/nops/ps" || exit 1
chmod 0644 "$sandbox/nops/ps" || exit 1

# Deny bind(), inherited by every child through PYTHONPATH.
cat > "$sandbox/nobind/sitecustomize.py" <<'PYDENY' || exit 1
import socket
def _denied(self, addr):
    raise PermissionError(1, "Operation not permitted")
socket.socket.bind = _denied
PYDENY

# THE FIXTURES ARE CHECKED, NOT ONLY THE COMMANDS THAT WROTE THEM. Every construction step above
# carries `|| exit 1`, and that is still not the property the phases depend on: what they need is
# that `ps` EXISTS and is NOT executable, and that `sitecustomize.py` exists and is non-empty. A
# missing or truncated denial file does not error — it makes the phase below silently
# UNRESTRICTED, which publishes a false negative instead of a failure (external review).
[ -s "$sandbox/nops/ps" ] || exit 1
[ ! -x "$sandbox/nops/ps" ] || exit 1
[ -s "$sandbox/nobind/sitecustomize.py" ] || exit 1

# A phase runs the default command, or --payload instead. The tests drive THIS script with a
# cheap payload rather than a copy of its skeleton, so what they exercise is what ships.
# /bin/sh BY ABSOLUTE PATH, for the same reason the interpreter below is absolute: in the
# `ps`-denied phases PATH is the stub directory and NOTHING ELSE, so a bare `sh` is not
# resolvable and the payload dies 127 without ever running. Found by running it.
exec_run() {
    if [ -n "$PAYLOAD" ]; then
        /bin/sh -c "$PAYLOAD"
    else
        "$@"
    fi
}

# Each phase runs in its own subshell so its denial cannot outlive it. `VAR=value func` is NOT
# used: POSIX leaves it to the shell whether such an assignment persists after a FUNCTION
# returns, and a PATH that leaked into the next phase would silently un-deny it.
#
# Output is captured and then echoed rather than streamed, because the STATUS has to survive:
# `cmd | tee` reports tee's status in a POSIX shell, and this script now depends on the real one.
log="$sandbox/phase.log"
problems=0

# judge <label> <actual> <expected> [evidence...]
# Evidence is matched with `grep -F`: these are literal strings the suites print, not patterns.
judge() {
    label=$1
    actual=$2
    expected=$3
    shift 3
    printf 'PHASE %-22s exit=%s\n' "$label" "$actual"
    if [ -n "$PAYLOAD" ]; then
        return 0    # payload mode: the caller supplied the command, so the caller judges it
    fi
    if [ "$actual" != "$expected" ]; then
        printf 'PROBLEM %s exited %s, expected %s\n' "$label" "$actual" "$expected"
        problems=$((problems + 1))
    fi
    for want in "$@"; do
        if ! grep -qF "$want" "$log"; then
            printf 'PROBLEM %s never reported: %s\n' "$label" "$want"
            problems=$((problems + 1))
        fi
    done
}

PS_DENIED="the process observer is unavailable here"
BIND_DENIED="a loopback listener cannot be bound here"

# shellcheck disable=SC2123  # overwriting PATH is the whole point; it is subshell-scoped
(
    PATH="$sandbox/nops"; export PATH
    exec_run "$PY" harness/tools/verify_mcp_fixtures.py
) > "$log" 2>&1; status=$?
cat "$log"
judge "ps-denied" "$status" 1 "INCOMPLETE" "$PS_DENIED"

(
    PYTHONPATH="$sandbox/nobind"; export PYTHONPATH
    exec_run "$PY" harness/tools/verify_mcp_fixtures.py
) > "$log" 2>&1; status=$?
cat "$log"
judge "bind-denied" "$status" 1 "INCOMPLETE" "$BIND_DENIED"

# shellcheck disable=SC2123  # as above — the denial is the subject, not an accident
(
    PATH="$sandbox/nops"; export PATH
    PYTHONPATH="$sandbox/nobind"; export PYTHONPATH
    exec_run "$PY" harness/tools/verify_mcp_fixtures.py
) > "$log" 2>&1; status=$?
cat "$log"
# BOTH reasons, because one of them is what a half-applied denial looks like.
judge "both-denied" "$status" 1 "INCOMPLETE" "$PS_DENIED" "$BIND_DENIED"

# The selftest separately, for the arm the `mkfifo` replacement is for. The bind() case belongs
# on the VERIFIER too and not only here: E16's two transports are what it exercises, and pointing
# it at the selftest alone demonstrates the FIFO replacement and nothing else (external review).
(
    PYTHONPATH="$sandbox/nobind"; export PYTHONPATH
    exec_run "$PY" -m agentskill_evals.cli selftest
) > "$log" 2>&1; status=$?
cat "$log"
judge "selftest-bind-denied" "$status" 0 "SELFTEST PASSED"

if [ -n "$PAYLOAD" ]; then
    echo "payload mode: no expectations applied — this run is NOT a reproduction"
    exit 0
fi
if [ "$problems" -gt 0 ]; then
    echo "$problems expectation(s) NOT met — this was not a reproduction"
    exit 1
fi
echo "every phase produced the expected status and the evidence that the denial took"
exit 0
