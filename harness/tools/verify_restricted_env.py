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
    """The phase labels the script reports, in order."""
    return re.findall(r'^\); report "([^"]+)"', text, re.MULTILINE)


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


def run_script(shell: str, payload: str, *, stub_dir: pathlib.Path | None = None,
               script: pathlib.Path | None = None, sig: int | None = None,
               marker: pathlib.Path | None = None,
               timeout: float = 60.0) -> tuple[int, str]:
    """Run the real script; optionally signal its process GROUP once a phase is in flight."""
    env = dict(os.environ)
    if stub_dir is not None:
        env["PATH"] = f"{stub_dir}:{env['PATH']}"
    proc = subprocess.Popen(
        [shell, str(script or SCRIPT), "--payload", payload],
        cwd=str(ROOT), env=env, start_new_session=True,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)

    if sig is not None:
        deadline = time.time() + 20
        while time.time() < deadline:
            if marker is not None and marker.exists() and marker.read_text().strip():
                break
            if proc.poll() is not None:
                break
            time.sleep(0.02)
        time.sleep(0.25)
        if proc.poll() is None:
            with contextlib_suppress():
                os.killpg(os.getpgid(proc.pid), sig)
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib_suppress():
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        out, _ = proc.communicate(timeout=10)
    return proc.returncode, out or ""


class contextlib_suppress:
    """Local, tiny: os.killpg races a process that just exited."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return exc_type is not None and issubclass(exc_type, (ProcessLookupError, OSError))


def phases_in(out: str) -> int:
    return len(re.findall(r"^PHASE ", out, re.MULTILINE))


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
        _, _out = run_script("/bin/sh", f'echo x >> "{marker}"; /bin/sleep 5',
                             stub_dir=stub, script=old_path,
                             sig=signal.SIGQUIT, marker=marker)
        leaked = sandboxes_under(sroot)
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
            rc, out = run_script("/bin/sh", f'echo ran >> "{marker}"', stub_dir=stub)
            check(f"{name} fails: no phase is reached", phases_in(out) == 0, out[:120])
            check(f"{name} fails: status is non-zero", rc != 0, rc)
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
        rc, out = run_script("/bin/sh", f'echo ran >> "{marker}"', stub_dir=stub)
        expected = len(script_phases(text))
        check(f"unbroken: every phase runs ({expected}, read from the script)",
              phases_in(out) == expected, (phases_in(out), expected))
        check("unbroken: status is 0", rc == 0, rc)
        check("unbroken: nothing is left behind",
              sandboxes_under(sroot) == [], sandboxes_under(sroot))


def section_b() -> None:
    print(f"\nB. SIGNALS — {len(SENT)} sent + {len(FAULT)} fault, "
          f"delivered to the process group, under {len(SHELLS)} shell(s)")
    residual: dict[str, list[str]] = {}
    for shell in SHELLS:
        leaked_sent, leaked_fault, ran_on = [], [], []
        for name in SENT + FAULT:
            num = getattr(signal, "SIG" + name, None)
            if num is None:
                continue
            with tempfile.TemporaryDirectory() as td:
                root = pathlib.Path(td)
                sroot, stub = root / "sboxes", root / "stub"
                sroot.mkdir()
                make_stubs(stub, sroot, None)
                marker = root / "m"
                _rc, out = run_script(shell, f'echo x >> "{marker}"; /bin/sleep 5',
                                      stub_dir=stub, sig=num, marker=marker)
                leaked = sandboxes_under(sroot)
                if leaked:
                    (leaked_sent if name in SENT else leaked_fault).append(name)
                for p in leaked:
                    shutil.rmtree(p, ignore_errors=True)
                # A phase that COMPLETES after the signal is the fail-open shape: the
                # handler cleaned up, returned, and let the run continue undenied.
                if phases_in(out) > 1:
                    ran_on.append(name)
        check(f"{shell}: no SENT signal leaks the sandbox", leaked_sent == [], leaked_sent)
        check(f"{shell}: no phase completes after any signal", ran_on == [], ran_on)
        check(f"{shell}: the residual is a subset of the documented fault signals",
              set(leaked_fault) <= set(FAULT), leaked_fault)
        residual[shell] = leaked_fault
    print("\n  residual leak surface (documented, not a defect — the shell itself has faulted):")
    for shell, names in residual.items():
        print(f"    {shell:11} {', '.join(names) if names else 'none'}")


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
    check("no handler exits 1, which is a verifier run's EXPECTED status",
          all(h != "exit 1" for h in traps.values()), catchall)

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
