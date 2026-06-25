"""Per-cell HOME isolation for hermetic skill visibility.

Agents discover skills from two locations:

  1. **Global (HOME-based):** ``~/.claude/skills/``, ``~/.agents/skills/``, etc.
  2. **Project-local (git-root-based):** ``.claude/skills/``, ``.agents/skills/``, etc.
     at the git repository root, found by walking up from cwd.

This module handles layer 1 (global skills). The runner handles layer 2 by running
``git init`` in each cell's workspace (see ``runner.py``), which creates a ``.git``
boundary that stops agents from walking up to the real repo root.

**Layer 1 — HOME overlay (this module):**

To make a run see only the skills it declares — while keeping the agent's own *vendor* skills
and all of its auth/config — we run each cell against a temporary HOME that is a **symlink
overlay** of the real one:

  * every top-level HOME entry (``.gitconfig``, ``.ssh``, ``.config``, ``.claude/plugins``, …)
    is a single wholesale symlink to the real one, so logins/settings/toolchains keep working;
  * only each *global skills directory* is rebuilt as a real dir that symlinks through every
    entry **except** this repo's skills (preserving vendor skills such as codex's ``.system``),
    then *copies* in the cell's *declared* skills.

Net effect inside a skills dir: ``vendor/other ∪ declared`` — undeclared repo skills are gone.
Placing the declared skills here (in addition to the workspace) makes discovery work whether a
surface reads project-local skills, user-global skills, or merges them at any precedence.

The overlay is built from cheap symlinks — only the small *declared* skills are copied — so a
35 MB codex log dir costs nothing, and an agent writing inside a declared skill mutates the copy,
never its source. ``shutil.rmtree`` removes the overlay by unlinking the symlinks (and deleting
the copies) — it never recurses into the real HOME.
"""

from __future__ import annotations

import os
import shutil
from typing import Any, Iterable, Optional

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


def resolve_visible_skills(
    adapter: Any,
    declared_names: Iterable[str],
    repo_skill_names: Iterable[str],
    isolated: bool,
    real_home: Optional[str] = None,
) -> dict:
    """What skills the model would see, computed from the filesystem (no agent run).

    Reads the adapter's global skills dirs and classifies their entries against this repo's
    skills. Returns sorted lists:
      provisioned   — the declared skills (always visible, from the workspace);
      vendor        — non-repo entries in the global dirs (kept even under isolation);
      masked        — repo skills present globally but not declared (hidden when isolated);
      also_visible  — same set, shown when NOT isolated (they leak in).
    Skills bundled inside a CLI package or plugins live outside these dirs and aren't listed.
    """
    real_home = os.path.abspath(real_home or os.path.expanduser("~"))
    declared = set(declared_names or ())
    repo = set(repo_skill_names or ())
    vendor: set = set()
    leaked_repo: set = set()   # repo skills found globally but not declared
    scan_dirs = [os.path.join(real_home, sub)
                 for sub in getattr(adapter, "global_skills_subpaths", []) or []]
    # a custom config home (e.g. $CODEX_HOME) holds skills outside the HOME-relative dirs
    for var, skills_sub in getattr(adapter, "isolation_config_homes", []) or []:
        custom = os.environ.get(var)
        if custom:
            scan_dirs.append(os.path.join(custom, skills_sub))
    for d in scan_dirs:
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if name in repo:
                if name not in declared:
                    leaked_repo.add(name)
            else:
                vendor.add(name)
    return {
        "provisioned": sorted(declared),
        "vendor": sorted(vendor),
        "masked": sorted(leaked_repo) if isolated else [],
        "also_visible": [] if isolated else sorted(leaked_repo),
    }


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
        # copy (not symlink) declared skills: a write inside one mutates the throwaway copy,
        # never the repo's skill source.
        shutil.copytree(src, os.path.join(dst_skills, name), dirs_exist_ok=True)
        placed.add(name)
