#!/usr/bin/env python3
"""Empirically map an adapter's CONTAINED-HOME surface (see isolation.py's contained mode).

`Adapter.contained_home_subpaths` is `None` for every adapter but claude, and `None` means
"not mapped" — it fails closed, so credential-bearing cells on that adapter stay refused.
Filling it in is empirical by construction: the failure mode of contained mode is the CLI
erroring because it needed something nobody declared, and the only way to learn what it
needs is to run it against a home that has nothing else.

This drives exactly the harness's own launch path — `build_isolated_home(contained_subpaths=)`
for the home, `adapter.env()` for the environment, `adapter.build_argv()` for the argv,
`exec.run_captured()` for the spawn — so a surface that works here is a surface that works
in a cell, rather than one that works in a hand-rolled approximation of a cell.

    probe_contained_home.py codex                       # the empty surface: what breaks?
    probe_contained_home.py codex --subpaths .codex/auth.json
    probe_contained_home.py copilot --subpaths ''       # explicit empty
    probe_contained_home.py codex --overlay             # control: the historical overlay

CREDENTIAL SAFETY. Naming a subpath COPIES that path out of the real home. Two consequences
worth understanding before typing one:

  * the copy is a second location holding a long-lived credential, for the life of the probe
    (`--keep` leaves it on disk; without it the tree is removed on every exit path); and
  * the child can WRITE to its copy, and a CLI that refreshes a rotating token writes the new
    one there — where this script then deletes it — while the real store keeps the old one.
    If the issuer treats refresh tokens as single-use, that logs the real CLI out. `--rotation-
    check` hashes each copied file before and after and reports any that the child rewrote,
    which is how you find out whether a given CLI does this before it costs you a login.

Output is redacted: every string of >= 8 chars found in a copied JSON credential file is
scrubbed from the transcript before printing, same discipline as the runner's registry.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from agentskill_evals import exec as ase_exec           # noqa: E402
from agentskill_evals.adapters import get_adapter        # noqa: E402
from agentskill_evals.adapters.base import RunOptions    # noqa: E402
from agentskill_evals.isolation import (                 # noqa: E402
    build_isolated_home,
    home_write_escapes,
)

# Short enough to catch an account id, long enough not to rewrite ordinary prose. The runner's
# own floor is higher (MIN_REDACTABLE_LEN) because it must not corrupt a model's answer; this
# transcript is read by a human looking for an error message, so err toward over-scrubbing.
_MIN_REDACT = 8

# A prompt with a verifiable answer and no tool use: the question under test is whether the CLI
# can authenticate and reach the model from a home containing only the declared surface, not
# whether it can edit files. Tool use would add its own failure modes on top.
_PROMPT = "Reply with exactly the word CONTAINED and nothing else."
_EXPECT = "CONTAINED"


def _secrets_in(path: str) -> list[str]:
    """Every string value >= _MIN_REDACT chars reachable in a JSON file, for redaction.

    Not "the fields we think are secret": a credential store's schema is the CLI's business
    and it changes. Anything long enough to be a token gets scrubbed whether or not this
    script recognizes the key it sits under.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            doc = json.load(fh)
    except Exception:
        return []
    out: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str) and len(node) >= _MIN_REDACT:
            out.append(node)

    walk(doc)
    return out


def _redact(text: str, secrets: list[str]) -> str:
    # Longest first, so a token that contains a shorter one does not get half-scrubbed into a
    # string that no longer matches the longer needle.
    for secret in sorted(set(secrets), key=len, reverse=True):
        text = text.replace(secret, "«REDACTED»")
    return text


def _digest(path: str) -> str | None:
    """Content hash of a file, or None if it is not a regular readable file."""
    if not os.path.isfile(path):
        return None
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def _tree(root: str, limit: int = 60) -> list[str]:
    """The materialized home, as HOME-relative paths, marking symlinks."""
    seen: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in sorted(dirnames) + sorted(filenames):
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root)
            seen.append(rel + (" -> " + os.readlink(path) if os.path.islink(path) else ""))
            if len(seen) >= limit:
                return seen + ["… (truncated)"]
    return seen


# Per-CLI markers for "this failed because it could not authenticate", as distinct from "this
# failed for some other reason". The bisect has to tell those apart or it will happily blame
# whichever directory the CLI merely needed in order to start up.
_AUTH_MARKERS = {
    "codex": ("401 Unauthorized", "Missing bearer"),
    "copilot": ("No authentication information found",),
    "antigravity": ("Authentication required", "authentication failed or timed out"),
    "claude": ("Invalid API key", "authentication_error", "OAuth token"),
}


def _no_browser_bin(parent: str) -> str:
    """A PATH directory whose `open`/`xdg-open` do nothing, returned for prepending.

    A CLI that cannot authenticate does not simply fail: agy falls back to INTERACTIVE OAuth
    and launches the user's browser at a Google sign-in page. During a bisect that is dozens
    of sign-in tabs appearing on a machine whose owner did not ask for any of them. The
    failure the probe wants to observe is "could not authenticate", and the browser launch is
    a side effect of it, so suppressing the launcher makes the observation cleaner AND stops
    the probe from touching anything outside its own tree. `BROWSER` is set too, for the
    libraries that consult it instead of shelling out to `open`.
    """
    bindir = os.path.join(parent, "no-browser-bin")
    os.makedirs(bindir, exist_ok=True)
    for name in ("open", "xdg-open", "www-browser", "x-www-browser"):
        path = os.path.join(bindir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(path, 0o755)
    return bindir


def _run_once(adapter, args, *, subs: list[str], masks: list[str], overlay: bool,
              verbose: bool = False) -> dict:
    """Build one home, run the CLI against it, and classify the outcome.

    Returns a dict with `verdict` in WORKS / AUTH-FAIL / OTHER-FAIL / CONTAINMENT-BROKEN.
    """
    real_home = os.path.expanduser("~")
    # Redaction needles come from the REAL files, read before anything runs — the copies are
    # identical at that point, and reading the originals means a CLI that rewrites its copy
    # cannot smuggle a value past the scrub by changing it mid-run.
    secrets: list[str] = []
    for sub in subs:
        secrets += _secrets_in(os.path.join(real_home, sub))

    cfg_masks = dict(getattr(adapter, "isolation_config_masks", {}) or {})
    # `None` content materializes the path as an empty real directory — the neutralizing form.
    # Adapter masks win on a collision: they are load-bearing for MCP hermeticity, and a probe
    # that silently unmasked one would be measuring a different configuration than a real cell.
    for m in masks:
        cfg_masks.setdefault(m, None)

    home = tempfile.mkdtemp(prefix="probe-home-")
    ws = tempfile.mkdtemp(prefix="probe-ws-")
    try:
        build_isolated_home(
            home,
            adapter.global_skills_subpaths,
            [],                                  # no repo skills to mask: nothing is provisioned
            [],                                  # no declared skills to seed
            real_home,
            plugin_registry_subpaths=getattr(adapter, "global_plugin_registry_subpaths", []),
            repo_root=None,
            config_file_masks=cfg_masks,
            plugin_config_masks=dict(getattr(adapter, "plugin_registry_config_masks", {}) or {}),
            contained_subpaths=None if overlay else subs,
        )

        escapes = home_write_escapes(home)
        if verbose:
            mode = "OVERLAY (control)" if overlay else "CONTAINED"
            print(f"=== {adapter.name}: {mode}, surface={subs or '[] (nothing copied)'}"
                  + (f", masked={masks}" if masks else ""))
            print(f"home: {home}")
            print(f"home_write_escapes: {len(escapes)}"
                  + (f"  {escapes[:5]}{' …' if len(escapes) > 5 else ''}" if escapes else "  []"))
            print(f"materialized: {_tree(home)}")
        if not overlay and escapes:
            # The lifting condition is structural — if this ever fires under contained mode,
            # containment is broken and no run result below would mean anything.
            if verbose:
                print("CONTAINMENT BROKEN: a contained home must have no outward symlink.")
            return {"verdict": "CONTAINMENT-BROKEN", "escapes": escapes}

        before = ({sub: _digest(os.path.join(home, sub)) for sub in subs}
                  if args.rotation_check else {})

        opts = RunOptions(model=args.model, auto_approve=True, home=home, isolation_env={},
                          extra_args=args.extra_args.split())
        env = adapter.env(dict(os.environ), opts)
        if not args.allow_browser:
            env["PATH"] = _no_browser_bin(ws) + os.pathsep + env.get("PATH", "")
            env["BROWSER"] = "true"
        opts.effective_env = env
        argv = adapter.build_argv(args.prompt, opts, cwd=ws)
        if verbose:
            print(f"argv: {_redact(' '.join(argv), secrets)}")

        stdout, stderr, code, timed_out = ase_exec.run_captured(
            argv, cwd=ws, env=env, timeout=args.timeout)

        parsed_text = ""
        try:
            parsed = adapter.parse(stdout, stderr, code, opts=opts)
            parsed_text = (parsed.final_text or "").strip()
        except Exception as exc:                       # parsing is diagnostic here, not the test
            parsed_text = f"<parse failed: {exc}>"

        ok = (not timed_out) and code == 0 and _EXPECT in parsed_text.upper()
        blob = stdout + stderr
        auth_failed = any(m in blob for m in _AUTH_MARKERS.get(adapter.name, ()))
        verdict = "WORKS" if ok else ("AUTH-FAIL" if auth_failed else "OTHER-FAIL")

        if verbose:
            print(f"exit={code} timed_out={timed_out} verdict={verdict}")
            print(f"answer: {_redact(parsed_text, secrets)[:400]!r}")
            print(f"stdout[:1200]: {_redact(stdout, secrets)[:1200]}")
            print(f"stderr[:1200]: {_redact(stderr, secrets)[:1200]}")

        if args.rotation_check:
            rewritten = [sub for sub in subs
                         if _digest(os.path.join(home, sub)) != before.get(sub)]
            print(f"rotation-check: rewritten-by-child={rewritten or 'none'}")
            if rewritten:
                print("  WARNING: the child rewrote a copied credential. Whatever it wrote "
                      "dies with this tree while the real store keeps the old value — if the "
                      "issuer rotates refresh tokens single-use, the real login is now stale.")
        return {"verdict": verdict, "code": code, "stdout": stdout, "stderr": stderr}
    finally:
        if args.keep and (not subs or args.keep_credentials):
            print(f"kept: {home}  {ws}")
        else:
            shutil.rmtree(home, ignore_errors=True)
            shutil.rmtree(ws, ignore_errors=True)


def _bisect(adapter, args) -> int:
    """Find the real-HOME entry the CLI authenticates from, by masking on the OVERLAY.

    Additive probing (contained mode + a guessed surface) can only confirm a guess. This is
    the search that produces the guess: start from the overlay, which is known to work, and
    take top-level entries away until authentication breaks. What breaks it is what the CLI
    reads its credential from — and it is found rather than assumed, which for two of these
    three CLIs is the difference between a mapped surface and a plausible story.
    """
    real_home = os.path.expanduser("~")
    reserved = set()
    for p in (list(adapter.global_skills_subpaths)
              + list(getattr(adapter, "global_plugin_registry_subpaths", []) or [])
              + list(getattr(adapter, "isolation_config_masks", {}) or {})):
        reserved.add(str(p).replace("\\", "/").split("/")[0])
    candidates = sorted(e for e in os.listdir(real_home) if e not in reserved)

    print(f"=== bisect {adapter.name}: {len(candidates)} top-level candidates "
          f"(reserved by the adapter, never masked: {sorted(reserved)})")

    base = _run_once(adapter, args, subs=[], masks=[], overlay=True)
    print(f"baseline overlay, nothing masked: {base['verdict']}")
    if base["verdict"] != "WORKS":
        print("baseline does not work — nothing to bisect. Fix the run first.")
        return 2

    allm = _run_once(adapter, args, subs=[], masks=candidates, overlay=True)
    print(f"overlay, ALL {len(candidates)} masked: {allm['verdict']}")
    if allm["verdict"] == "WORKS":
        print("masking every top-level entry still authenticates — the credential is NOT "
              "under HOME (keychain, or an absolute path elsewhere). That is itself the "
              "answer: this CLI's contained surface is [] for auth purposes.")
        return 0

    # Invariant: masking `masked` breaks auth; masking nothing does not. Shrink `masked`.
    masked = list(candidates)
    while len(masked) > 1:
        half = len(masked) // 2
        left, right = masked[:half], masked[half:]
        res = _run_once(adapter, args, subs=[], masks=left, overlay=True)
        print(f"  masking {len(left):>3} of {len(masked):>3} -> {res['verdict']}")
        if res["verdict"] != "WORKS":
            masked = left            # breakage reproduces in the left half
            continue
        res = _run_once(adapter, args, subs=[], masks=right, overlay=True)
        print(f"  masking {len(right):>3} of {len(masked):>3} -> {res['verdict']}")
        if res["verdict"] != "WORKS":
            masked = right
            continue
        # Neither half alone breaks it: the CLI reads more than one entry (e.g. a token in one
        # place and a machine id in another). Report the pair rather than pretending it is one.
        print(f"neither half alone breaks auth — the surface is SPLIT across "
              f"{len(masked)} entries: {masked}")
        return 0
    print(f"RESULT: {adapter.name} authenticates from ~/{masked[0]}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("adapter")
    ap.add_argument("--subpaths", default="",
                    help="comma-separated HOME-relative paths to copy in (default: none, "
                         "i.e. the empty surface)")
    ap.add_argument("--overlay", action="store_true",
                    help="control run: build the HISTORICAL symlink overlay instead of a "
                         "contained home, to tell 'contained mode broke it' apart from "
                         "'this CLI/prompt was already broken here'")
    ap.add_argument("--rotation-check", action="store_true",
                    help="hash each copied subpath before and after; report what the child "
                         "rewrote (a rotated credential is destroyed when this tree is removed)")
    ap.add_argument("--mask", default="",
                    help="comma-separated HOME-relative paths to NEUTRALIZE (materialized as "
                         "an empty directory) on top of whichever home is built. Subtractive "
                         "probing: start from a working overlay and take things away.")
    ap.add_argument("--bisect", action="store_true",
                    help="binary-search the real HOME's top-level entries for the one the CLI "
                         "authenticates from, by masking candidates on the OVERLAY until auth "
                         "breaks. Additive probing needs the answer up front; this finds it.")
    ap.add_argument("--extra-args", default="",
                    help="space-separated flags appended to the adapter's argv, for telling a "
                         "containment failure apart from an unrelated CLI gate (e.g. codex's "
                         "--skip-git-repo-check). Adapters that reject config-channel flags "
                         "still reject them here.")
    ap.add_argument("--allow-browser", action="store_true",
                    help="permit the CLI to launch a browser. Off by default: a CLI that "
                         "cannot authenticate falls back to interactive OAuth and opens a "
                         "sign-in tab, which during a bisect means dozens of them.")
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--model", default=None)
    ap.add_argument("--prompt", default=_PROMPT)
    ap.add_argument("--keep", action="store_true",
                    help="leave the home and workspace on disk for inspection. Refused when "
                         "a subpath was copied unless --keep-credentials is also given.")
    ap.add_argument("--keep-credentials", action="store_true",
                    help="with --keep, permit leaving COPIED CREDENTIALS on disk.")
    args = ap.parse_args()

    subs = [s.strip() for s in args.subpaths.split(",") if s.strip()]
    adapter = get_adapter(args.adapter)
    masks = [s.strip() for s in args.mask.split(",") if s.strip()]

    if args.keep and subs and not args.keep_credentials:
        print("refusing --keep with a non-empty surface: the tree holds copies of "
              f"{', '.join(subs)}. Pass --keep-credentials if that is what you want.",
              file=sys.stderr)
        return 2

    if args.bisect:
        return _bisect(adapter, args)

    run = _run_once(adapter, args, subs=subs, masks=masks, overlay=args.overlay, verbose=True)
    return 0 if run["verdict"] == "WORKS" else (3 if run["verdict"] == "CONTAINMENT-BROKEN" else 1)


if __name__ == "__main__":
    raise SystemExit(main())
