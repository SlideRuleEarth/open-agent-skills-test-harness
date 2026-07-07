"""Shared, agent-agnostic views over a run's workspace — the file tree and
inlined file contents — used by BOTH the judge (to grade) and the per-cell
report (to show the human everything the model produced).

The judge takes a deliberately small slice (a few files, truncated) to keep its
prompt cheap; the report shows everything the model produced, in full. Both ride
the same walk so they never diverge on what counts as "the model's output"
(e.g. both exclude the provisioned skill dirs and VCS/build noise).
"""

from __future__ import annotations

import os
import shlex
from typing import Any, Iterable, Iterator, Optional

# Budgets for the judge's compact view (the report passes None == no cap).
JUDGE_MAX_FILES = 60
JUDGE_MAX_INLINE_FILES = 5
JUDGE_MAX_INLINE_BYTES = 1500

_TEXT_EXT = {".md", ".txt", ".py", ".json", ".yaml", ".yml", ".toml", ".cfg",
             ".ini", ".js", ".ts", ".html", ".css", ".sh", ".csv"}

# Provisioned skills are inputs, not model output; .git/node_modules/etc. are noise.
_SKILL_DIRS = (".claude", ".agents", ".antigravity", ".codex")
_SKIP_DIRS = {".git", "node_modules", "__pycache__"}


def _iter_files(workdir: str) -> Iterator[tuple[str, str]]:
    """Yield (abspath, relpath) for files the model could have produced —
    excluding VCS/build noise and the provisioned skill dirs."""
    for root, dirs, files in os.walk(workdir):
        dirs[:] = [d for d in sorted(dirs) if d not in _SKIP_DIRS]
        rel_root = os.path.relpath(root, workdir)
        if rel_root.startswith(_SKILL_DIRS):
            continue
        for f in sorted(files):
            path = os.path.join(root, f)
            yield path, os.path.relpath(path, workdir)


def _is_under(base: str, path: str) -> bool:
    """True if the absolute `path` resolves inside the absolute `base` directory."""
    try:
        return os.path.commonpath([base, path]) == base
    except ValueError:        # different drives, etc. → treat as outside
        return False


def resolve_trace_path(path: str, workdir: str) -> str:
    """Resolve a tool-trace path to an absolute path. The agent ran with cwd == the workspace, so a
    RELATIVE trace path is relative to the WORKSPACE — never the harness process cwd (resolving it
    against the process cwd is how an agent's relative ``README.md`` was wrongly matched to the
    repo's ``harness/README.md``). An ABSOLUTE trace path is kept as-is, so an agent that wrote to a
    mangled absolute path outside the workspace is still detected."""
    if os.path.isabs(path):
        return path
    return os.path.join(workdir, path)


def writes_outside_workspace(result: Any, workdir: str) -> list[str]:
    """Absolute paths the run created that landed OUTSIDE the workspace (e.g. the model wrote to an
    absolute path with a mangled run-id). Surfacing them lets the judge grade — and the report show —
    the artifact the run actually produced, not just whatever happened to land in the workspace."""
    wd = os.path.abspath(workdir)
    out: list[str] = []
    seen: set[str] = set()
    for p in result.file_paths_touched():
        if not p:
            continue
        ap = os.path.abspath(resolve_trace_path(p, workdir))
        if ap in seen:
            continue
        if not _is_under(wd, ap) and os.path.isfile(ap):
            seen.add(ap)
            out.append(ap)
    return out


def leaked_skill_reads(
    result: Any, workdir: str, repo_root: str,
    repo_skill_names: Iterable[str], declared_names: Iterable[str],
) -> list[str]:
    """Absolute paths (or referenced script paths) this run touched that reach an UNDECLARED
    skill through the real, on-disk repo checkout rather than the provisioned workspace copy.

    Even with the eval workspace relocated to a tempdir outside this repo's working tree
    (``runner.py``), a run could still reach an undeclared skill some other way (e.g. searching
    the real disk by name) — this is the residual safety net that catches it from the trace, so
    a leak is never silently reported as ``isolated: true``.
    """
    leaked_names = set(repo_skill_names) - set(declared_names)
    if not leaked_names:
        return []
    root = os.path.abspath(repo_root)
    wd = os.path.abspath(workdir)
    hits: list[str] = []
    seen: set[str] = set()

    def _flag(candidate: str) -> None:
        if not candidate or candidate in seen:
            return
        ap = os.path.abspath(candidate)
        if not _is_under(root, ap) or _is_under(wd, ap):
            return
        rel_parts = os.path.relpath(ap, root).split(os.sep)
        if rel_parts and rel_parts[0] in leaked_names:
            seen.add(candidate)
            hits.append(ap)

    for p in result.file_paths_touched():
        _flag(resolve_trace_path(p, workdir))

    markers = [os.path.join(root, name) for name in leaked_names]
    for cmd in result.commands():
        try:
            tokens = shlex.split(cmd)
        except ValueError:        # unbalanced quotes, etc. — fall back to whitespace split
            tokens = cmd.split()
        for tok in tokens:
            for marker in markers:
                if tok.startswith(marker):
                    _flag(tok)

    return hits


def file_tree(workdir: str, extra: list[str] = (), max_files: Optional[int] = None) -> str:
    """A flat listing of every file under `workdir` (skill dirs / noise excluded), plus any
    `extra` paths written outside it. `max_files=None` lists everything (the report); the judge
    passes a cap."""
    lines: list[str] = []
    count = 0
    truncated = False
    for _abs, rel in _iter_files(workdir):
        if max_files is not None and count >= max_files:
            truncated = True
            break
        lines.append(f"  {rel}")
        count += 1
    if truncated:
        lines.append(f"  ... (+ more, truncated at {max_files})")
    for ap in extra:
        lines.append(f"  {ap}   [written OUTSIDE the workspace by this run]")
    return "\n".join(lines) if lines else "  (workspace empty)"


def inline_files(workdir: str, extra: list[str] = (), max_files: Optional[int] = None,
                 max_bytes: Optional[int] = None) -> str:
    """Inline the contents of text files under `workdir` (and `extra`). With max_files/max_bytes
    None (the report) every text file is inlined in full; the judge passes small caps to keep its
    prompt cheap. Non-text files are skipped (they appear in `file_tree`)."""
    chunks: list[str] = []
    used = 0

    def _maybe(path: str, label: str) -> bool:
        """Return False to stop the walk (budget exhausted)."""
        nonlocal used
        if max_files is not None and used >= max_files:
            return False
        if os.path.splitext(path)[1].lower() not in _TEXT_EXT:
            return True   # binary: skip contents, keep walking
        try:
            if max_bytes is not None and os.path.getsize(path) > max_bytes:
                return True
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                body = fh.read()
        except OSError:
            return True
        chunks.append(f"--- {label} ---\n{body}")
        used += 1
        return True

    for ap, rel in _iter_files(workdir):
        if not _maybe(ap, rel):
            break
    else:
        for ap in extra:
            if not _maybe(ap, f"{ap}  [outside workspace]"):
                break
    return "\n\n".join(chunks)
