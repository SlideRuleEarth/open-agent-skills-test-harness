#!/usr/bin/env python3
"""Drive restricted_env.sh's FAILURE paths, which are the ones that matter.

The happy path of that script is self-checking: if the denials do not take, the suites it
runs say so. Its failure paths are not. Six rounds of external review found five separate
fail-OPEN defects in it — a construction step whose failure let a later step run
unrestricted, a leak on a second allocation, a signal handler that cleaned up and returned,
a signal set that was "the ones I tested", a status collision — and every one of them
reported green while being broken. That is the class this file exists for:

    any step whose failure lets a later step run in a state it claims not to be in.

Three sections, three different questions:

  A  CONSTRUCTION. Each step is made to fail in turn. The phases must never be reached, the
     status must be non-zero, and nothing may be left behind. An always-fail stub is not
     enough on its own — it can only ever exercise the FIRST allocation, which is how the
     second-allocation leak survived a round of "verification".

  B  SIGNALS. Every catchable terminating signal, delivered to the process GROUP (what a
     keyboard signal does; signalling the script's pid alone misses its subshells), under
     every shell on this machine. EXIT-trap-runs-anyway is NOT a property of `sh`, so this
     is measured per shell rather than assumed: no signal that is SENT may leak under any
     shell, and the residual is expected to be exactly the FAULT signals, which the script
     deliberately does not trap.

  C  PINS. The trap set is read out of the script and checked against the distinction the
     documentation claims, in BOTH directions — every sent signal trapped, no fault signal
     trapped. A handler deleted upstream must redden a check here, not silently shrink the
     covered set.

NOTHING HERE IS A COPY. The trap set, the construction steps and the phase list are read
out of restricted_env.sh itself; a duplicated rule that can drift silently is the defect
this repo has spent the most rounds on.

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
SCRIPT = ROOT / "harness" / "tools" / "restricted_env.sh"
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


# The two literal strings the script greps for as proof a denial took effect. Pinned to the
# suite that prints them in section C, so a reworded skip reason is a named failure here rather
# than a reproduction that silently stops proving anything.
PS_EVIDENCE = "the process observer is unavailable here"
BIND_EVIDENCE = "a loopback listener cannot be bound here"

# A stand-in for the two suites, selected through the script's OWN path logic: it is written to
# <fake>/harness/.venv/bin/python and the script's `$repo/harness/.venv/bin/python` finds it.
# No test-only switch in the shipping script -- the production path is what runs.
STUB_PY = rf"""#!/bin/sh
case "$*" in
  *agentskill_evals.cli*)
      case "$PLAN" in
        exit7) exit 7 ;;
        *) echo "SELFTEST PASSED — 579 arms"; exit 0 ;;
      esac ;;
esac
case "$PLAN" in
  exit7) exit 7 ;;
  unrestricted) echo "ALL PASS"; exit 0 ;;
esac
echo "INCOMPLETE — some section(s) could not run here"
case "$PATH" in *nops*) echo "  - {PS_EVIDENCE}: \`ps\` did not run" ;; esac
case "$PLAN" in
  halfapplied) ;;
  *) case "${{PYTHONPATH:-}}" in *nobind*) echo "  - {BIND_EVIDENCE}: [Errno 1]" ;; esac ;;
esac
exit 1
"""


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
    """DEFAULT MODE: the script must refuse to call a broken run a reproduction.

    The first version discarded every phase status and always exited 0 -- all four phases
    driven to exit 7 still returned 0 (external review). A denial that silently does not take
    then yields UNRESTRICTED green verifier runs underneath a green reproduction, which is the
    one outcome this script exists to make impossible.
    """
    print("\nD. THE PRODUCTION PATH — default mode, against a stub interpreter")
    cases = [
        ("faithful", None, 0, "a denied run that reports what a denied run reports"),
        ("exit7", "exit7", 1, "every phase exits 7 (the reported defect)"),
        ("unrestricted", "unrestricted", 1,
         "the denial silently did not take: green suites, no INCOMPLETE"),
        ("halfapplied", "halfapplied", 1,
         "status is right but the bind() reason never appears — only EVIDENCE catches this"),
    ]
    for name, plan, want_rc, why in cases:
        with tempfile.TemporaryDirectory() as td:
            script = fake_repo(pathlib.Path(td))
            run = run_script("/bin/sh", None, script=script,
                             env_extra={"PLAN": plan or "faithful"})
            ok = (run.rc == 0) if want_rc == 0 else (run.rc != 0)
            check(f"{name}: {why}", ok, (run.rc, run.out[-200:]))
            if name == "faithful":
                check("  ...and it ran every phase and said so",
                      run.phases == 4 and "expected status and the evidence" in run.out,
                      (run.phases, run.out[-120:]))
            else:
                check("  ...and it says which expectation failed rather than only exiting",
                      "PROBLEM" in run.out, run.out[-200:])


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

    # PIN: the script greps for these literals as proof the denial took. If the suite that
    # prints them is reworded, the reproduction stops proving anything -- fail-closed, but the
    # failure would name a phase rather than the cause. This names the cause.
    verifier_src = (ROOT / "harness" / "tools" / "verify_mcp_fixtures.py").read_text()
    for literal in (PS_EVIDENCE, BIND_EVIDENCE):
        check(f"  the script's evidence string is still printed by the suite: {literal!r}",
              literal in verifier_src)
        check("  ...and the script actually greps for it", literal in text)

    doc = DOC.read_text()
    check("the documentation points at the script rather than carrying a second copy",
          "tools/restricted_env.sh" in doc)
    check("...and no fenced block in the doc still builds the sandbox itself",
          not re.search(r"```sh.*?mktemp -d.*?PYDENY.*?```", doc, re.DOTALL))


def main() -> int:
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
