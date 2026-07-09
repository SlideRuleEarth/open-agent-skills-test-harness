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

# Budgets for the judge's compact view (the report passes None == no file-count cap).
JUDGE_MAX_FILES = 60
JUDGE_MAX_INLINE_FILES = 5
JUDGE_MAX_INLINE_BYTES = 1500
# The report inlines every text file, but per-file only up to this many bytes (with a
# truncation note) — a run that legitimately produces a multi-MB CSV/JSON export must not
# balloon report.md; the full file is still in workspace/.
REPORT_MAX_INLINE_BYTES = 200_000

_TEXT_EXT = {".md", ".txt", ".py", ".json", ".yaml", ".yml", ".toml", ".cfg",
             ".ini", ".js", ".ts", ".html", ".css", ".sh", ".csv"}

# Provisioned skills are inputs, not model output; .git/node_modules/etc. are noise.
_SKILL_DIRS = (".claude", ".agents", ".antigravity", ".codex")
_SKIP_DIRS = {".git", "node_modules", "__pycache__"}


def _is_skill_dir(rel_root: str) -> bool:
    """True if `rel_root` IS one of the provisioned skill dirs, or a path underneath one — a path
    segment match, not a bare string prefix (a real dir named e.g. `.codexnotes` must NOT match
    `.codex`, or the model's actual output would silently vanish from the report)."""
    return any(rel_root == d or rel_root.startswith(d + os.sep) for d in _SKILL_DIRS)


def _iter_files(workdir: str) -> Iterator[tuple[str, str]]:
    """Yield (abspath, relpath) for files the model could have produced —
    excluding VCS/build noise and the provisioned skill dirs."""
    for root, dirs, files in os.walk(workdir):
        dirs[:] = [d for d in sorted(dirs) if d not in _SKIP_DIRS]
        rel_root = os.path.relpath(root, workdir)
        if _is_skill_dir(rel_root):
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


def seeded_relpaths(spec: Any) -> set[str]:
    """Workspace-relative paths that were seeded into the workspace BEFORE the run (the
    fixture tree plus each `files:` destination) — inputs, not model output. Both the judge
    and the report annotate these so a seeded file is never credited as work the model did."""
    seeded: set[str] = set()
    if spec is None:
        return seeded
    try:
        fixture = spec.resolved_fixture()
    except Exception:
        fixture = None
    if fixture and os.path.isdir(fixture):
        for root, _dirs, files in os.walk(fixture):
            for f in files:
                seeded.add(os.path.relpath(os.path.join(root, f), fixture))
    try:
        for _src, dest in spec.resolved_files():
            seeded.add(dest)
    except Exception:
        pass
    return seeded


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
    the artifact the run actually produced, not just whatever happened to land in the workspace.

    Uses ``realpath`` (not ``abspath``) on both sides of the containment check: a symlink inside the
    workspace pointing outside it (e.g. `workspace/link -> /elsewhere`) must resolve to its real
    target, or a write through that link would be wrongly counted as "inside"."""
    wd = os.path.realpath(workdir)
    out: list[str] = []
    seen: set[str] = set()
    for p in result.file_paths_touched():
        if not p:
            continue
        ap = os.path.realpath(resolve_trace_path(p, workdir))
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
    the real disk by name, or a symlink planted inside the workspace pointing at the repo) — this
    is the residual safety net that catches it from the trace, so a leak is never silently
    reported as ``isolated: true``.

    Uses ``realpath`` (not ``abspath``): an agent that symlinks a workspace-local name at an
    undeclared skill (e.g. ``ln -s <repo>/sliderule-pipeline-direct_request evil`` then reads ``evil/SKILL.md``) would
    otherwise look textually "inside the workspace" and never get flagged at all.
    """
    leaked_names = set(repo_skill_names) - set(declared_names)
    if not leaked_names:
        return []
    root = os.path.realpath(repo_root)
    wd = os.path.realpath(workdir)
    hits: list[str] = []
    seen: set[str] = set()

    def _flag(candidate: str) -> None:
        if not candidate or candidate in seen:
            return
        ap = os.path.realpath(candidate)
        if not _is_under(root, ap) or _is_under(wd, ap):
            return
        rel_parts = os.path.relpath(ap, root).split(os.sep)
        if rel_parts and rel_parts[0] in leaked_names:
            seen.add(candidate)
            hits.append(ap)

    for p in result.file_paths_touched():
        _flag(resolve_trace_path(p, workdir))

    # Markers match literal text in a raw shell command string, so they must use the SAME
    # (unresolved) form the agent would actually have typed — not the realpath'd `root` above,
    # which e.g. on macOS resolves /var -> /private/var and would never textually match a
    # command referencing the ordinary /var path. `_flag` still realpath-resolves the matched
    # token for the actual containment decision.
    raw_root = os.path.abspath(repo_root)
    markers = [os.path.join(raw_root, name) for name in leaked_names]
    for cmd in result.commands():
        try:
            tokens = shlex.split(cmd)
        except ValueError:        # unbalanced quotes, etc. — fall back to whitespace split
            tokens = cmd.split()
        for tok in tokens:
            for marker in markers:
                # `in`, not `startswith`: the marker can be embedded mid-token (e.g.
                # --script=/repo/sliderule-pipeline-direct_request/scripts/tool.py or --path=/repo/... ), not just
                # at the start. Flag from where the marker begins, not the whole raw token (a
                # leading flag like "--script=" isn't a path itself, so realpath-ing it whole
                # would resolve it relative to cwd instead of recognizing the embedded absolute
                # path).
                idx = tok.find(marker)
                if idx != -1:
                    _flag(tok[idx:])

    return hits


def file_tree(workdir: str, extra: list[str] = (), max_files: Optional[int] = None,
              seeded: Iterable[str] = ()) -> str:
    """A flat listing of every file under `workdir` (skill dirs / noise excluded), plus any
    `extra` paths written outside it. `max_files=None` lists everything (the report); the judge
    passes a cap. Paths in `seeded` (workspace-relative) are annotated as pre-seeded inputs."""
    seeded_set = set(seeded or ())
    lines: list[str] = []
    count = 0
    truncated = False
    for _abs, rel in _iter_files(workdir):
        if max_files is not None and count >= max_files:
            truncated = True
            break
        tag = "   [seeded input, not model output]" if rel in seeded_set else ""
        lines.append(f"  {rel}{tag}")
        count += 1
    if truncated:
        lines.append(f"  ... (+ more, truncated at {max_files})")
    for ap in extra:
        lines.append(f"  {ap}   [written OUTSIDE the workspace by this run]")
    return "\n".join(lines) if lines else "  (workspace empty)"


def inline_files(workdir: str, extra: list[str] = (), max_files: Optional[int] = None,
                 max_bytes: Optional[int] = None, truncate: bool = False,
                 seeded: Iterable[str] = ()) -> str:
    """Inline the contents of text files under `workdir` (and `extra`). With max_files None
    (the report) every text file is inlined; the judge passes small caps to keep its prompt
    cheap. A file over `max_bytes` is skipped by default (judge) or, with `truncate=True`
    (report), inlined up to the cap with a truncation note. Paths in `seeded` are labelled as
    pre-seeded inputs. Non-text files are skipped (they appear in `file_tree`)."""
    seeded_set = set(seeded or ())
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
            size = os.path.getsize(path)
            if max_bytes is not None and size > max_bytes and not truncate:
                return True
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                body = fh.read(max_bytes) if max_bytes is not None else fh.read()
            if max_bytes is not None and size > max_bytes:
                body += (f"\n… [truncated at {max_bytes} bytes of {size} — "
                         "full file in workspace/]")
        except OSError:
            return True
        chunks.append(f"--- {label} ---\n{body}")
        used += 1
        return True

    for ap, rel in _iter_files(workdir):
        label = f"{rel}  [seeded input, not model output]" if rel in seeded_set else rel
        if not _maybe(ap, label):
            break
    else:
        for ap in extra:
            if not _maybe(ap, f"{ap}  [outside workspace]"):
                break
    return "\n\n".join(chunks)
