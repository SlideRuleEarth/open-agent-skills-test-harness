"""Per-cell HOME isolation for hermetic skill visibility.

Agents discover skills from the project *and* the user's global skills folders, so
globally-installed skills leak into a run regardless of what an eval provisions. To make
a run see only the skills it declares — while keeping the agent's own *vendor* skills and
all of its auth/config — we run each cell against a temporary HOME that is a **symlink
overlay** of the real one:

  * every top-level HOME entry (`.gitconfig`, `.ssh`, `.config`, `.claude/plugins`, …) is a
    single wholesale symlink to the real one, so logins/settings/toolchains keep working;
  * only each *global skills directory* is rebuilt as a real dir that symlinks through every
    entry **except** this repo's skills (preserving vendor skills such as codex's `.system`),
    then symlinks in the cell's *declared* skills.

Net effect inside a skills dir: ``vendor/other ∪ declared`` — undeclared repo skills are gone.
Placing the declared skills here (in addition to the workspace) makes discovery work whether a
surface reads project-local skills, user-global skills, or merges them at any precedence.

The overlay is built from cheap symlinks (no copying), so a 35 MB codex log dir costs nothing.
``shutil.rmtree`` removes the overlay by unlinking the symlinks — it never recurses into the
real HOME.
"""

from __future__ import annotations

import os
from typing import Iterable, Optional

# Sentinel marking a node in the path tree as a *skills directory* leaf (built specially)
# rather than an ancestor directory to recurse into.
_SKILLS_LEAF = object()


def build_isolated_home(
    dest_home: str,
    skills_subpaths: Iterable[str],
    repo_skill_names: Iterable[str],
    declared_skill_dirs: Iterable[str],
    real_home: Optional[str] = None,
) -> str:
    """Build a symlink overlay of ``real_home`` at ``dest_home`` with masked skills dirs.

    Args:
      dest_home: a fresh (ideally empty) directory to populate; returned for convenience.
      skills_subpaths: HOME-relative global skills dirs to mask, e.g. ``.claude/skills`` or
        ``.gemini/config/skills`` (arbitrary nesting; missing ones are created empty).
      repo_skill_names: skill names this repo can provision — removed from the masked dirs.
      declared_skill_dirs: absolute skill source dirs to symlink into each masked dir.
      real_home: the HOME to mirror (defaults to ``~``).

    Raises OSError if a symlink can't be created (e.g. Windows without privilege); the caller
    should fall back to a non-isolated run.
    """
    real_home = os.path.abspath(real_home or os.path.expanduser("~"))
    repo_skills = set(repo_skill_names or ())
    declared = [os.path.abspath(d) for d in (declared_skill_dirs or ()) if os.path.isdir(d)]

    # Build a nested tree of "special" paths: name -> subtree(dict) | _SKILLS_LEAF.
    tree: dict = {}
    for sub in skills_subpaths or ():
        parts = [p for p in str(sub).replace("\\", "/").split("/") if p and p != "."]
        if not parts:
            continue
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = _SKILLS_LEAF

    os.makedirs(dest_home, exist_ok=True)
    _overlay(real_home, dest_home, tree, repo_skills, declared)
    return dest_home


def _overlay(real_dir: str, dst_dir: str, tree: dict,
             repo_skills: set, declared: list) -> None:
    os.makedirs(dst_dir, exist_ok=True)
    special = set(tree)
    # 1) wholesale-symlink every real entry that isn't a special (skills/ancestor) path.
    if os.path.isdir(real_dir):
        for name in os.listdir(real_dir):
            if name in special:
                continue
            os.symlink(os.path.join(real_dir, name), os.path.join(dst_dir, name))
    # 2) handle special children, even if absent in the real dir (create the empty chain).
    for name, node in tree.items():
        real_child = os.path.join(real_dir, name)
        dst_child = os.path.join(dst_dir, name)
        if node is _SKILLS_LEAF:
            _build_skills_dir(real_child, dst_child, repo_skills, declared)
        else:
            _overlay(real_child, dst_child, node, repo_skills, declared)


def _build_skills_dir(real_skills: str, dst_skills: str,
                      repo_skills: set, declared: list) -> None:
    """Rebuild one skills dir: vendor/other entries passed through, repo skills dropped,
    declared skills added."""
    os.makedirs(dst_skills, exist_ok=True)
    placed: set = set()
    if os.path.isdir(real_skills):
        for name in os.listdir(real_skills):
            if name in repo_skills:
                continue  # drop this repo's skills; declared ones are re-added below
            os.symlink(os.path.join(real_skills, name), os.path.join(dst_skills, name))
            placed.add(name)
    for src in declared:
        name = os.path.basename(os.path.normpath(src))
        if name in placed:
            continue
        os.symlink(src, os.path.join(dst_skills, name))
        placed.add(name)
