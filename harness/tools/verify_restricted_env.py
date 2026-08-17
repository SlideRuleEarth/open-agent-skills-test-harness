#!/usr/bin/env python3
"""Drive restricted_env.sh's FAILURE paths, which are the ones that matter.

The happy path of that script is self-checking: if the denials do not take, the suites it
runs say so. Its failure paths are not. Six rounds of external review found five separate
fail-OPEN defects in it — a construction step whose failure let a later step run
unrestricted, a leak on a second allocation, a signal handler that cleaned up and returned,
a signal set that was "the ones I tested", a status collision — and every one of them
reported green while being broken. That is the class this file exists for:

    any step whose failure lets a later step run in a state it claims not to be in.

Five sections, five different questions:

  A  CONSTRUCTION. Each step is made to fail in turn. The phases must never be reached, the
     status must be non-zero, and nothing may be left behind. An always-fail stub is not
     enough on its own — it can only ever exercise the FIRST allocation, which is how the
     second-allocation leak survived a round of "verification".

  B  SIGNALS. Every catchable terminating signal, delivered to the process GROUP (what a
     keyboard signal does; signalling the script's pid alone misses its subshells), under
     every shell on this machine. EXIT-trap-runs-anyway is NOT a property of `sh`, so this
     is measured per shell rather than assumed. The contract is read out of the script: no
     signal it TRAPS may leak. What remains is REPORTED, not asserted -- which fault signals
     a given shell runs the EXIT trap for is a fact about that shell, not something this repo
     can hold it to, and asserting the residual was a subset of FAULT could not fail.

  C  PINS. The trap set and the per-phase expectations are read out of the script and checked
     against the distinction the documentation claims, in BOTH directions — every sent signal
     trapped, no fault signal trapped, and every evidence literal still DEMANDED by a judge
     call rather than merely assigned to a variable. A requirement deleted upstream must redden
     a check here, not silently shrink the covered set.

  D  THE PRODUCTION PATH. Default mode — no `--payload`, no test-only switch — against a stub
     interpreter reached through the script's own path logic. One requirement is broken at a
     time, in one phase, and the exact PROBLEM line is demanded along with the absence of any
     other: batched failures cannot show that an individual guard discriminates.

  E  MUTATIONS. The script is mutated nine ways on every run — each requirement deleted, each
     expected status weakened, a handler stopped from exiting — and C and D must redden for
     each. Two of the nine were green when they were written, so this is not a formality.

ONE DELIBERATE COPY, AND EVERYTHING ELSE READ FROM THE SOURCE. The trap set, the construction
steps, the phase list and the per-phase expectations are read out of restricted_env.sh itself;
a duplicate that can drift silently is the defect this repo has spent the most rounds on.

`EXPECTED_CONTRACT` is the exception, and it is deliberate: section D generates its cases FROM
the script's `judge` calls, so a requirement deleted upstream would delete the very case that
should have caught it — a universal quantified over a set the subject controls. Stating the
contract independently is the structural clause ahead of that universal, which means changing
what the script demands SHOULD require saying so in two places. Section E is what keeps the
copy honest: it performs those deletions and requires the red.

Two negative controls run first, because a leak detector that cannot see a leak reports a
clean sweep exactly like a clean sweep: a planted sandbox must be DETECTED, and the script's
pre-review trap shape (INT/TERM only) must be seen LEAKING under SIGQUIT. If either fails,
every result below it is meaningless and the run says so.

    python tools/verify_restricted_env.py    # ~2 minutes; exits non-zero on any failure
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
# Overridable ONLY so section E can point a child run at a mutated copy. Unset everywhere
# else, including in every run a human starts.
SCRIPT = pathlib.Path(os.environ.get(
    "VRE_SCRIPT_UNDER_TEST", ROOT / "harness" / "tools" / "restricted_env.sh"))
DOC = ROOT / "harness" / "TODO_Contained_HOME.md"

# Catchable signals whose default action terminates. KILL/STOP are absent because nobody can
# catch them; CHLD/CONT/URG/WINCH/INFO/TSTP/TTIN/TTOU because they do not terminate.
SENT = ["HUP", "INT", "QUIT", "PIPE", "TERM", "ABRT", "ALRM",
        "USR1", "USR2", "XCPU", "XFSZ", "VTALRM", "PROF"]
FAULT = ["ILL", "TRAP", "EMT", "FPE", "BUS", "SEGV", "SYS"]

SHELLS = [s for s in ("/bin/sh", "/bin/bash", "/bin/dash", "/bin/zsh", "/bin/ksh")
          if os.path.exists(s)]

checks = 0
fails: list[str] = []
skipped: list[tuple[str, str]] = []


def check(label: str, ok: bool, detail: object = None) -> bool:
    global checks
    checks += 1
    if ok:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}" + (f"   <- {detail!r}" if detail is not None else ""))
        fails.append(label)
    return ok


def skip(section: str, reason: object) -> None:
    print(f"  SKIP {section}  <- {reason}")
    skipped.append((section, str(reason)[:300]))


# --------------------------------------------------------------------------- reading the script

def script_traps(text: str) -> dict[str, str]:
    """{signal: handler} for every `trap` line in the script, EXIT excluded."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"^trap\s+'([^']*)'\s+([A-Z0-9 ]+?)\s*(?:#.*)?$", line)
        if not m:
            continue
        handler, names = m.group(1), m.group(2).split()
        for name in names:
            if name != "EXIT":
                out[name] = handler
    return out


def script_phases(text: str) -> list[str]:
    """The phase labels the script judges, in order.

    Read rather than counted-by-hand, so adding a phase to the script does not quietly leave
    this expecting the old number. When the script changed its reporting from `report` to
    `judge` this returned an empty list and the positive control went red, which is the
    behaviour wanted: a pin that stops matching must FAIL, not shrink to a vacuous zero.
    """
    labels = re.findall(r'^judge "([^"]+)"', text, re.MULTILINE)
    if not labels:
        raise SystemExit(
            "verify_restricted_env: found no phase labels in restricted_env.sh — the pattern "
            "this pin uses no longer matches the script, so every phase-count check below "
            "would be measuring nothing.")
    return labels


def script_expectations(text: str) -> dict[str, tuple[int, list[str]]]:
    """{label: (expected status, [evidence literals])}, parsed from the `judge` calls.

    Resolves "$PS_DENIED" through the script's own assignment, so a requirement is counted
    only where it is USED. Checking that a literal merely appears in the file matched its own
    variable declaration and stayed green when every use was deleted (external review).
    """
    assigns = dict(re.findall(r'^([A-Z_][A-Z_0-9]*)="([^"]*)"$', text, re.MULTILINE))
    out: dict[str, tuple[int, list[str]]] = {}
    for line in text.splitlines():
        m = re.match(r'^judge "([^"]+)" "\$status" (\d+)(.*)$', line)
        if not m:
            continue
        rest = re.sub(r"#.*$", "", m.group(3))
        args = re.findall(r'"([^"]*)"', rest)
        out[m.group(1)] = (int(m.group(2)),
                           [assigns[a[1:]] if a.startswith("$") and a[1:] in assigns else a
                            for a in args])
    if not out:
        raise SystemExit(
            "verify_restricted_env: parsed no `judge` calls out of restricted_env.sh — the "
            "expectation matrix would be empty, which is not the same as passing.")
    unresolved = [e for _s, ev in out.values() for e in ev if e.startswith("$")]
    if unresolved:
        raise SystemExit(f"verify_restricted_env: unresolved evidence variables: {unresolved}")
    return out


# --------------------------------------------------------------------------- driving the script

def _write(path: pathlib.Path, body: str, mode: int = 0o755) -> None:
    path.write_text(body)
    path.chmod(mode)


def make_stubs(stub_dir: pathlib.Path, sandbox_root: pathlib.Path,
               failing: str | None, unwritable: bool = False) -> None:
    """A PATH directory that intercepts the script's construction commands.

    `mktemp` is ALWAYS intercepted so the sandbox lands somewhere this process knows the
    path of: a leak check that watches a directory the sandbox was never created in reports
    every shape as clean, which is how the first version of this control passed while
    measuring nothing. (macOS `mktemp -d` ignores TMPDIR, so pointing TMPDIR at a directory
    of our own does NOT work.)
    """
    stub_dir.mkdir(parents=True, exist_ok=True)
    if failing == "mktemp":
        _write(stub_dir / "mktemp", "#!/bin/sh\nexit 1\n")
    else:
        perm = "0500" if unwritable else "0700"
        _write(stub_dir / "mktemp", (
            "#!/bin/sh\n"
            f'd="{sandbox_root}/sandbox.$$"\n'
            '/bin/mkdir -p "$d" || exit 1\n'
            f'/bin/chmod {perm} "$d" || exit 1\n'
            'printf %s\\\\n "$d"\n'))
    for name in ("mkdir", "chmod", "cat"):
        if failing == name:
            _write(stub_dir / name, "#!/bin/sh\nexit 1\n")


def sandboxes_under(root: pathlib.Path) -> list[pathlib.Path]:
    return sorted(p for p in root.iterdir() if p.name.startswith("sandbox."))


class Run:
    """One execution of the script, and what is KNOWN about it.

    `started` and `delivered` are the load-bearing fields. Without them a trial where the
    shell died during startup -- before the payload ever ran, so before there was anything to
    interrupt -- is indistinguishable from a trial where the signal arrived and the handler
    cleaned up: both leave no sandbox and no extra phase, which is exactly what the leak and
    fail-open checks look for. Every signal case must therefore assert that the payload was
    reached AND that the signal was actually delivered (external review).
    """

    def __init__(self, rc: int, out: str, started: bool, delivered: bool,
                 denied: str | None = None):
        self.rc, self.out = rc, out
        self.started, self.delivered, self.denied = started, delivered, denied

    @property
    def phases(self) -> int:
        return len(re.findall(r"^PHASE ", self.out, re.MULTILINE))


def run_script(shell: str, payload: str | None = None, *,
               stub_dir: pathlib.Path | None = None,
               script: pathlib.Path | None = None, sig: int | None = None,
               marker: pathlib.Path | None = None,
               env_extra: dict[str, str] | None = None,
               timeout: float = 120.0) -> Run:
    """Run the script; optionally signal its process GROUP once a phase is in flight."""
    env = dict(os.environ)
    if stub_dir is not None:
        env["PATH"] = f"{stub_dir}:{env['PATH']}"
    env.update(env_extra or {})
    argv = [shell, str(script or SCRIPT)]
    if payload is not None:
        argv += ["--payload", payload]
    proc = subprocess.Popen(
        argv, cwd=str(ROOT), env=env, start_new_session=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    started, delivered, denied = sig is None, sig is None, None
    if sig is not None:
        deadline = time.time() + 20
        while time.time() < deadline:
            if marker is not None and marker.exists() and marker.read_text().strip():
                started = True
                break
            if proc.poll() is not None:
                break       # it exited before reaching the payload: NOT a usable trial
            time.sleep(0.02)
        if started:
            time.sleep(0.25)
            try:
                os.killpg(os.getpgid(proc.pid), sig)
                delivered = True
            except PermissionError as exc:
                # Signalling is a CAPABILITY. Denied, this environment cannot answer the
                # question, which is a skip -- not a pass, and not a failure of the script.
                denied = f"killpg denied: {exc}"
            except (ProcessLookupError, OSError):
                # It exited between the poll and the signal, so the trial measured nothing.
                # Left as delivered=False, which FAILS the case rather than passing quietly.
                pass
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        out, _ = proc.communicate(timeout=10)
    return Run(proc.returncode, out or "", started, delivered, denied)


# --------------------------------------------------------------------------- sections

def negative_controls() -> bool:
    """Prove the instrument can report the failures it is about to claim are absent."""
    print("\nN. NEGATIVE CONTROLS — an instrument that cannot fail is not evidence")
    ok = True
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        sroot, stub = root / "sboxes", root / "stub"
        sroot.mkdir()
        planted = sroot / "sandbox.planted"
        planted.mkdir()
        ok &= check("the leak detector SEES a planted sandbox",
                    len(sandboxes_under(sroot)) == 1, sandboxes_under(sroot))
        planted.rmdir()

        # The script as it stood before review: cleanup, but only INT and TERM terminate.
        # DERIVED from the shipping script, not typed out beside it.
        text = SCRIPT.read_text()
        old = re.sub(r"^trap 'exit \d+' (?!INT\b|TERM\b).*$", "", text, flags=re.MULTILINE)
        old_path = root / "old_shape.sh"
        _write(old_path, old)
        ok &= check("the pre-review shape still parses (so the control tests a shape, not a "
                    "syntax error)",
                    subprocess.run(["/bin/sh", "-n", str(old_path)],
                                   capture_output=True).returncode == 0)
        make_stubs(stub, sroot, None)
        marker = root / "m"
        run = run_script("/bin/sh", f'echo x >> "{marker}"; /bin/sleep 5',
                         stub_dir=stub, script=old_path,
                         sig=signal.SIGQUIT, marker=marker)
        leaked = sandboxes_under(sroot)
        ok &= check("...the control trial reached its payload and the signal was DELIVERED",
                    run.started and run.delivered, (run.started, run.delivered, run.denied))
        ok &= check("...and the pre-review shape is CAUGHT leaking under SIGQUIT",
                    len(leaked) == 1, leaked)
        for p in leaked:
            shutil.rmtree(p, ignore_errors=True)
    return ok


def section_a() -> None:
    print("\nA. CONSTRUCTION — each step failing in turn; the phases must never be reached")
    text = SCRIPT.read_text()
    cases = [("mktemp", False), ("mkdir", False), ("chmod", False), ("cat", False),
             ("unwritable sandbox", True)]
    for name, unwritable in cases:
        failing = None if unwritable else name
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            sroot, stub = root / "sboxes", root / "stub"
            sroot.mkdir()
            make_stubs(stub, sroot, failing, unwritable=unwritable)
            marker = root / "m"
            run = run_script("/bin/sh", f'echo ran >> "{marker}"', stub_dir=stub)
            check(f"{name} fails: no phase is reached", run.phases == 0, run.out[:120])
            check(f"{name} fails: status is non-zero", run.rc != 0, run.rc)
            check(f"{name} fails: nothing is left behind",
                  sandboxes_under(sroot) == [], sandboxes_under(sroot))
            for p in sandboxes_under(sroot):
                shutil.rmtree(p, ignore_errors=True)

    # POSITIVE CONTROL: with nothing broken the script must actually REACH its phases, or
    # every "no phase was reached" above is satisfied by a script that never runs at all.
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        sroot, stub = root / "sboxes", root / "stub"
        sroot.mkdir()
        make_stubs(stub, sroot, None)
        marker = root / "m"
        run = run_script("/bin/sh", f'echo ran >> "{marker}"', stub_dir=stub)
        expected = len(script_phases(text))
        check(f"unbroken: every phase runs ({expected}, read from the script)",
              run.phases == expected, (run.phases, expected))
        check("unbroken: status is 0", run.rc == 0, run.rc)
        check("unbroken: nothing is left behind",
              sandboxes_under(sroot) == [], sandboxes_under(sroot))


def section_b() -> None:
    """Every catchable terminating signal, under every shell, against the SHIPPING script.

    The contract is read out of the script rather than from the constants above: a signal the
    script TRAPS must never leak, whatever it is called. Checking instead that the leaks are a
    subset of FAULT was a tautology — entries only ever reached that list when their name had
    already come from FAULT, so it could not fail and inflated the total (external review).
    The residual is REPORTED, not asserted: which fault signals a given shell happens to run
    the EXIT trap for is a fact about that shell, not a contract this repo can hold it to.
    """
    trapped = set(script_traps(SCRIPT.read_text()))
    allsigs = [n for n in SENT + FAULT if getattr(signal, "SIG" + n, None) is not None]
    print(f"\nB. SIGNALS — {len(allsigs)} catchable terminating signals "
          f"({len(trapped & set(allsigs))} trapped by the script), "
          f"process-group delivery, {len(SHELLS)} shell(s)")
    residual: dict[str, list[str]] = {}
    for shell in SHELLS:
        leaked_trapped, ran_on, unusable, denials = [], [], [], []
        leaked_untrapped = []
        for name in allsigs:
            num = getattr(signal, "SIG" + name)
            with tempfile.TemporaryDirectory() as td:
                root = pathlib.Path(td)
                sroot, stub = root / "sboxes", root / "stub"
                sroot.mkdir()
                make_stubs(stub, sroot, None)
                marker = root / "m"
                run = run_script(shell, f'echo x >> "{marker}"; /bin/sleep 5',
                                 stub_dir=stub, sig=num, marker=marker)
                # A TRIAL THAT NEVER HAPPENED MUST NOT COUNT AS A PASS. A shell that dies
                # during startup reaches no payload, receives no signal, leaks nothing and
                # completes no phase — indistinguishable from a clean interruption unless
                # both facts are recorded (external review).
                if run.denied:
                    denials.append(f"{name}: {run.denied}")
                elif not (run.started and run.delivered):
                    unusable.append(f"{name}(started={run.started},sent={run.delivered})")
                leaked = sandboxes_under(sroot)
                if leaked:
                    (leaked_trapped if name in trapped else leaked_untrapped).append(name)
                for path in leaked:
                    shutil.rmtree(path, ignore_errors=True)
                # A phase that COMPLETES after the signal is the fail-open shape: the handler
                # cleaned up, returned, and let the run continue undenied.
                if run.phases > 1:
                    ran_on.append(name)
        if denials:
            skip(f"B/{shell}", f"this environment refuses process-group signals: {denials[0]}")
            continue
        check(f"{shell}: every trial reached the payload AND delivered its signal",
              unusable == [], unusable)
        check(f"{shell}: no signal the script TRAPS leaks the sandbox",
              leaked_trapped == [], leaked_trapped)
        check(f"{shell}: no phase completes after any signal", ran_on == [], ran_on)
        residual[shell] = leaked_untrapped
    print("\n  residual, REPORTED not asserted — untrapped signals whose EXIT trap this shell "
          "skips:")
    for shell, names in residual.items():
        print(f"    {shell:11} {', '.join(names) if names else 'none'}")


# The two literal strings the script greps for as proof a denial took effect.
PS_EVIDENCE = "the process observer is unavailable here"
BIND_EVIDENCE = "a loopback listener cannot be bound here"

# What each phase MUST demand: expected status, and every evidence literal. Checked against the
# script in section C, and the reason a deleted requirement cannot hide -- see the note there.
EXPECTED_CONTRACT = {
    "ps-denied": (1, ["INCOMPLETE", PS_EVIDENCE]),
    "bind-denied": (1, ["INCOMPLETE", BIND_EVIDENCE]),
    "both-denied": (1, ["INCOMPLETE", PS_EVIDENCE, BIND_EVIDENCE]),
    "selftest-bind-denied": (0, ["SELFTEST PASSED"]),
}

# Which single defect suppresses which requirement. Every evidence string the script demands
# must appear here, or section D refuses to run: a requirement this matrix cannot drive is a
# requirement nothing proves is load-bearing.
EVIDENCE_DEFECT = {
    "INCOMPLETE": "no-incomplete",
    PS_EVIDENCE: "no-ps",
    BIND_EVIDENCE: "no-bind",
    "SELFTEST PASSED": "no-banner",
}

# A stand-in for the two suites, selected through the script's OWN path logic: written to
# <fake>/harness/.venv/bin/python, where `$repo/harness/.venv/bin/python` finds it. No
# test-only switch exists in the shipping script -- the production path is what runs.
#
# It identifies its phase the way the script's denials present themselves (argv for the
# selftest, PATH/PYTHONPATH for the three verifier phases) and misbehaves ONLY in the phase
# named by $TARGET, and only in the one way named by $DEFECT. Batching several failures into
# one case meant a nonzero status could not be attributed to the guard under test: deleting
# both `$PS_DENIED` requirements left every case green, because the remaining problems still
# rejected the negative plans (external review).
_STUB_PY = r"""#!/bin/sh
ps_on=no; bind_on=no
case "$PATH" in *nops*) ps_on=yes ;; esac
case "${PYTHONPATH:-}" in *nobind*) bind_on=yes ;; esac
case "$*" in
  *agentskill_evals.cli*) phase="selftest-bind-denied" ;;
  *)
    if [ "$ps_on" = yes ] && [ "$bind_on" = yes ]; then phase="both-denied"
    elif [ "$ps_on" = yes ]; then phase="ps-denied"
    elif [ "$bind_on" = yes ]; then phase="bind-denied"
    else phase="undenied"; fi ;;
esac

defect=""
if [ "$phase" = "${TARGET:-}" ]; then defect="${DEFECT:-}"; fi

# EVIDENCE FIRST, STATUS LAST, so the two defects are INDEPENDENT. Exiting early on the
# status defect also suppressed the output, so one broken requirement produced three
# PROBLEMs and the case could not show that the status guard alone rejects anything.
if [ "$phase" = "selftest-bind-denied" ]; then
    case "$defect" in no-banner) ;; *) echo "SELFTEST PASSED — 579 arms" ;; esac
    case "$defect" in status) exit 3 ;; esac
    exit 0
fi

case "$defect" in no-incomplete) ;; *) echo "INCOMPLETE — some section(s) could not run here" ;; esac
if [ "$ps_on" = yes ] && [ "$defect" != no-ps ]; then
    echo "  - __PS__: \`ps\` did not run"
fi
if [ "$bind_on" = yes ] && [ "$defect" != no-bind ]; then
    echo "  - __BIND__: [Errno 1]"
fi
case "$defect" in status) exit 7 ;; esac
exit 1
"""
STUB_PY = _STUB_PY.replace("__PS__", PS_EVIDENCE).replace("__BIND__", BIND_EVIDENCE)


def _drive_default(target: str | None, defect: str | None) -> Run:
    with tempfile.TemporaryDirectory() as td:
        script = fake_repo(pathlib.Path(td))
        return run_script("/bin/sh", None, script=script,
                          env_extra={"TARGET": target or "", "DEFECT": defect or ""})


def fake_repo(root: pathlib.Path) -> pathlib.Path:
    """A repo-shaped tree holding the REAL script and a stub interpreter."""
    tools = root / "harness" / "tools"
    venv = root / "harness" / ".venv" / "bin"
    tools.mkdir(parents=True)
    venv.mkdir(parents=True)
    shutil.copy2(SCRIPT, tools / SCRIPT.name)
    _write(venv / "python", STUB_PY)
    return tools / SCRIPT.name


def section_d() -> None:
    """DEFAULT MODE: every production guard, driven alone, asserted by name.

    The script's first version discarded phase statuses and always exited 0 (external review).
    The first fix for that was checked with BATCHED failures -- several expectations broken at
    once, asserted only by "nonzero status and some PROBLEM" -- which cannot show that any
    individual guard discriminates: deleting a requirement left every case green because the
    remaining problems still rejected the run. Each case below breaks exactly ONE requirement
    in exactly ONE phase and demands the exact PROBLEM line, and the absence of any other.
    """
    text = SCRIPT.read_text()
    expectations = script_expectations(text)
    print(f"\nD. THE PRODUCTION PATH — {len(expectations)} phases, one broken requirement at a "
          f"time, against a stub interpreter")

    faithful = _drive_default(None, None)
    check("faithful: a denied run that reports what a denied run reports is accepted",
          faithful.rc == 0, (faithful.rc, faithful.out[-200:]))
    check(f"  ...and every phase ran ({len(expectations)}, read from the script), no PROBLEM",
          faithful.phases == len(expectations) and "PROBLEM" not in faithful.out
          and "expected status and the evidence" in faithful.out,
          (faithful.phases, faithful.out[-200:]))

    # STRUCTURAL CLAUSE AHEAD OF THE UNIVERSAL: a requirement this matrix cannot drive would
    # otherwise be silently skipped, and "every guard discriminates" would quantify over
    # whatever happened to be drivable.
    undrivable = [e for _s, ev in expectations.values() for e in ev if e not in EVIDENCE_DEFECT]
    check("every evidence string the script demands can be driven by this matrix",
          undrivable == [], undrivable)

    for label, (want_status, evidence) in expectations.items():
        check(f"  {label}: declares at least one evidence requirement", evidence != [], label)
        wrong = 3 if want_status == 0 else 7
        cases = [("status", f"PROBLEM {label} exited {wrong}, expected {want_status}")]
        cases += [(EVIDENCE_DEFECT[e], f"PROBLEM {label} never reported: {e}")
                  for e in evidence if e in EVIDENCE_DEFECT]
        for defect, want_problem in cases:
            run = _drive_default(label, defect)
            problems = re.findall(r"^PROBLEM .*$", run.out, re.MULTILINE)
            check(f"  {label} / {defect}: rejected", run.rc != 0, (run.rc, run.out[-160:]))
            check(f"  {label} / {defect}: names EXACTLY this problem — {want_problem!r}",
                  problems == [want_problem], problems)


def section_c() -> None:
    print("\nC. PINS — the trap set is read out of the script, and checked BOTH ways")
    text = SCRIPT.read_text()
    traps = script_traps(text)
    check("the script installs an EXIT trap that removes the sandbox",
          bool(re.search(r"^trap 'rm -rf \"\$sandbox\"' EXIT", text, re.MULTILINE)))
    check(f"the trap table is non-empty ({len(traps)} signals), so the checks below quantify "
          "over something",
          len(traps) >= len(SENT), sorted(traps))
    for name in SENT:
        check(f"  SENT {name} is trapped", name in traps, sorted(traps))
        if name in traps:
            check(f"  SENT {name}'s handler EXITS rather than returning",
                  traps[name].startswith("exit "), traps[name])
    for name in FAULT:
        check(f"  FAULT {name} is NOT trapped (a faulted shell must not run rm -rf)",
              name not in traps, traps.get(name))
    # The status collision that would make an interrupted run look like a finished one.
    catchall = {n: h for n, h in traps.items() if n not in ("HUP", "INT", "QUIT", "PIPE", "TERM")}
    check("no signal handler exits 1, which is the script's own \"expectations not met\" "
          "status — an interrupted run and a false reproduction must not look alike",
          all(h != "exit 1" for h in traps.values()), catchall)

    # PIN, BOTH ENDS: still printed by the suite, and still DEMANDED by a judge call. If
    # prints them is reworded, the reproduction stops proving anything -- fail-closed, but the
    # failure would name a phase rather than the cause. This names the cause.
    verifier_src = (ROOT / "harness" / "tools" / "verify_mcp_fixtures.py").read_text()
    expectations = script_expectations(text)
    demanded = {e for _s, ev in expectations.values() for e in ev}
    for literal in (PS_EVIDENCE, BIND_EVIDENCE):
        check(f"  the evidence string is still printed by the suite: {literal!r}",
              literal in verifier_src)
        check("  ...and is still DEMANDED by a judge call, not merely assigned to a variable",
              literal in demanded, sorted(demanded))
    # THE CONTRACT, STATED INDEPENDENTLY OF THE SCRIPT — the structural clause ahead of
    # section D's universal. Section D generates its cases FROM the script's judge calls, so
    # deleting a requirement also deletes the case that would have caught it: quantifying over
    # a set the subject controls. Removing `"INCOMPLETE"` from the ps-denied judge survived the
    # whole matrix for exactly that reason. Stated here, removing any requirement reddens a
    # check instead of quietly shrinking the matrix. It is a deliberate duplicate: changing the
    # script's contract SHOULD require saying so twice.
    for label, want in EXPECTED_CONTRACT.items():
        check(f"  {label} demands exactly {want[0]} + {[e[:22] for e in want[1]]}",
              expectations.get(label) == (want[0], list(want[1])), expectations.get(label))
    check("...and the script declares no phase this contract does not cover",
          set(expectations) == set(EXPECTED_CONTRACT),
          sorted(set(expectations) ^ set(EXPECTED_CONTRACT)))

    doc = DOC.read_text()
    check("the documentation points at the script rather than carrying a second copy",
          "tools/restricted_env.sh" in doc)
    check("...and no fenced block in the doc still builds the sandbox itself",
          not re.search(r"```sh.*?mktemp -d.*?PYDENY.*?```", doc, re.DOTALL))


def _child(script: pathlib.Path, *args: str) -> subprocess.CompletedProcess:
    """This verifier, run against a given copy of the script. Used only by section E."""
    return subprocess.run(
        [sys.executable, str(pathlib.Path(__file__).resolve()), *args],
        cwd=str(ROOT), env=dict(os.environ, VRE_SCRIPT_UNDER_TEST=str(script)),
        capture_output=True, text=True, timeout=300, check=False)


def section_e() -> None:
    """MUTATE THE SCRIPT AND REQUIRE C AND D TO NOTICE.

    Sections C and D assert that the script's guards discriminate. This asserts that those
    assertions can FAIL, by deleting one requirement at a time from a copy of the script and
    demanding a red run each time. Without it, "every guard is discriminating" rests on the
    author's intent -- which is how the batched version of section D passed while deleting both
    `$PS_DENIED` requirements left it green (external review), and how removing `"INCOMPLETE"`
    from one judge call survived the first fix: section D generates its cases FROM the script,
    so a deleted requirement deletes its own test.

    The child runs `--only cd` against the mutated copy, so it cannot recurse into this section.
    """
    print("\nE. MUTATIONS — each deletion must REDDEN sections C and D")
    text = SCRIPT.read_text()

    # TWO CONTROLS BEFORE THE MUTATIONS. "The child exited non-zero" is evidence about the
    # mutation only if the child can exit ZERO — a broken invocation would report every
    # mutation as caught, for the wrong reason. And the flag it runs under has to be honoured:
    # `--only nonsense` used to run C and D and exit 0, so a typo produced a green partial
    # sweep and would have silently reduced what every mutation below proves (external review).
    clean = _child(SCRIPT, "--only", "cd")
    check("  the child PASSES on the unmutated script, so a red below is the mutation talking",
          clean.returncode == 0, (clean.returncode, clean.stdout[-200:]))
    typo = _child(SCRIPT, "--only", "nonsense")
    check("  `--only nonsense` is REJECTED, not run as a green partial sweep",
          typo.returncode == 2, (typo.returncode, typo.stdout[-120:]))
    mutations = [
        ("both uses of $PS_DENIED deleted", lambda s: s.replace(' "$PS_DENIED"', "")),
        ("$BIND_DENIED deleted", lambda s: s.replace(' "$BIND_DENIED"', "")),
        ("the SELFTEST PASSED requirement deleted",
         lambda s: s.replace(' "SELFTEST PASSED"', "")),
        ("INCOMPLETE deleted from ps-denied",
         lambda s: s.replace('judge "ps-denied" "$status" 1 "INCOMPLETE"',
                             'judge "ps-denied" "$status" 1')),
        ("INCOMPLETE deleted from both-denied",
         lambda s: s.replace('judge "both-denied" "$status" 1 "INCOMPLETE"',
                             'judge "both-denied" "$status" 1')),
        ("ps-denied expected status weakened to the failing one",
         lambda s: s.replace('judge "ps-denied" "$status" 1', 'judge "ps-denied" "$status" 7')),
        ("the selftest expected status weakened",
         lambda s: s.replace('judge "selftest-bind-denied" "$status" 0',
                             'judge "selftest-bind-denied" "$status" 1')),
        ("the whole both-denied judge deleted",
         lambda s: re.sub(r'^judge "both-denied".*$', "", s, flags=re.MULTILINE)),
        ("a handler stops exiting", lambda s: s.replace("trap 'exit 130' INT",
                                                        "trap 'rm -rf \"$sandbox\"' INT")),
    ]
    for name, mutate in mutations:
        mutated = mutate(text)
        if mutated == text:
            check(f"  {name}: the mutation still applies to this script", False,
                  "no textual change — the pattern has drifted, so this proves nothing")
            continue
        with tempfile.TemporaryDirectory() as td:
            copy = pathlib.Path(td) / SCRIPT.name
            _write(copy, mutated)
            proc = _child(copy, "--only", "cd")
            check(f"  {name}: caught", proc.returncode != 0,
                  (proc.returncode, proc.stdout[-160:]))


# The accepted `--only` values, as data rather than as a string comparison. `--only cd` runs
# just the two sections section E mutates against, so the child cannot recurse into E and a
# mutation costs seconds rather than a full sweep. ANY OTHER VALUE IS AN ERROR: accepting one
# silently ran C and D and exited 0, which makes a typo look like a pass (external review).
ONLY_SECTIONS = {"cd": (section_c, section_d)}


def main() -> int:
    argv = sys.argv[1:]
    only = None
    if argv:
        if argv[:1] == ["--only"] and len(argv) == 2 and argv[1] in ONLY_SECTIONS:
            only = argv[1]
        else:
            print(f"usage: {sys.argv[0]} [--only {'|'.join(sorted(ONLY_SECTIONS))}]",
                  file=sys.stderr)
            return 2
    if only:
        for section in ONLY_SECTIONS[only]:
            section()
        print(f"\n{checks} checks (--only {only})")
        if fails:
            print("FAILED: " + ", ".join(fails))
        return 1 if fails else 0
    print(f"restricted_env.sh failure paths — shells: {', '.join(SHELLS)}")
    if not SCRIPT.exists():
        print(f"FAILED: {SCRIPT} is missing")
        return 1
    if not negative_controls():
        # Everything below rests on these. Refusing here is the difference between "no leaks
        # were found" and "no leaks could have been found".
        print("\nNEGATIVE CONTROLS FAILED — the rest of this run cannot be trusted; not run.")
        print(f"FAILED: {', '.join(fails)}")
        return 1
    section_a()
    section_b()
    section_c()
    section_d()
    section_e()

    print()
    if skipped:
        print(f"INCOMPLETE — {len(skipped)} section(s) could not run here:")
        for sec, why in skipped:
            print(f"  - {sec}: {why}")
        print("A capability this environment denies is not a passing result.")
    print(f"{checks} checks over {len(SHELLS)} shell(s). A total that DROPS means coverage was "
          "lost, which neither a pass nor a failure reports.")
    if fails:
        print("FAILED: " + ", ".join(fails))
    elif skipped:
        print(f"NO FAILURES, but INCOMPLETE — {len(skipped)} section(s) skipped; not a pass")
    else:
        print("ALL PASS")
    return 1 if (fails or skipped) else 0


if __name__ == "__main__":
    sys.exit(main())
