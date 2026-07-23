"""Per-cell HOME isolation for hermetic skill visibility.

Agents discover skills from two locations:

  1. **Global (HOME-based):** ``~/.claude/skills/``, ``~/.agents/skills/``, etc.
  2. **Project-local (git-root-based):** ``.claude/skills/``, ``.agents/skills/``, etc.
     at the git repository root, found by walking up from cwd.

This module handles layer 1 (global skills). The runner handles layer 2 by running each
cell's workspace in a tempdir with no path relationship to this repo's checkout (see
``runner.py``), so there's no real repo root above it for an agent to walk up into.

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

A **plugin registry** (e.g. AntiGravity's ``.gemini/config/plugins/<name>/skills/``) is a third
skill-discovery location some CLIs support: each entry under it is a whole plugin package, one
level of which (``<plugin>/skills/``) can itself carry this repo's skills — e.g. after
``agy plugin import claude``. It's *not* a plain skills dir (its siblings are plugin.json,
metadata, etc., not skills), so it gets its own leaf type (``_PLUGINS_LEAF``): every plugin
passes through untouched except its nested ``skills/``, which is mask-only (repo skills
dropped, nothing re-added — a declared skill is already injected once via the primary skills
dir above; duplicating it into every unrelated vendor plugin too would be redundant clutter).

A **config-file mask** is a third leaf type: a HOME-relative *file* (e.g.
``.copilot/mcp-config.json``) materialized as a real file with harness-supplied content
instead of symlinked — or, with content ``None``, an empty real *directory* (e.g. copilot's
``installed-plugins/``, whose entries can each carry MCP servers). The wholesale
pass-through symlinks are exactly how per-user config the harness must neutralize — today,
MCP server configs — would leak into "hermetic" runs; masking the file with ``{}`` (declare
no servers) closes that channel while the rest of the CLI's config keeps passing through
(see DESIGN_MCP_Support.md, Phase 0). Plugin registries have the same problem one level
down (``plugins/<name>/mcp_config.json`` in agy): ``plugin_config_masks`` materializes
those names inside every plugin while the rest of the plugin passes through.

The overlay is built from cheap symlinks — only the small *declared* skills are copied — so a
35 MB codex log dir costs nothing, and an agent writing inside a declared skill mutates the copy,
never its source. ``shutil.rmtree`` removes the overlay by unlinking the symlinks (and deleting
the copies) — it never recurses into the real HOME.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from typing import Any, Iterable, Mapping, Optional

# Sentinels marking a node in the path tree as a leaf (built specially) rather than an
# ancestor directory to recurse into: a plain *skills directory*, or a *plugin registry*
# (one level deeper — see module docstring).
_SKILLS_LEAF = object()
_PLUGINS_LEAF = object()


class _FileMaskLeaf:
    """Leaf for a *config-file mask* (see module docstring): the node is a single file,
    written with this content instead of symlinked to the real HOME's copy — or, with
    content ``None``, an empty real directory. Content may also be a callable
    ``(real_path) -> str`` for masks that *sanitize* the real file rather than replace it
    outright (e.g. copilot's config.json, which holds auth alongside the plugin
    registrations that must be dropped)."""
    __slots__ = ("content",)

    def __init__(self, content):
        self.content = content


def config_home_entries(adapter: Any) -> list[tuple]:
    """Normalized ``isolation_config_homes`` entries as (env var, HOME subdir it stands in
    for or None, skills-subdir or None). Accepts the pre-Phase-0 out-of-tree 2-tuple form
    ``(var, skills_sub)`` — those adapters predate config masks, so no stand-in dir."""
    entries = []
    for entry in getattr(adapter, "isolation_config_homes", []) or []:
        entry = tuple(entry)
        if len(entry) == 2:
            entries.append((entry[0], None, entry[1]))
        else:
            entries.append(entry[:3])
    return entries


def _is_stale_repo_link(path: str, repo_root: Optional[str]) -> bool:
    """True for a skills-dir entry left behind by an install of this repo that the name-based
    mask can't catch: a symlink into ``repo_root`` under a name no longer in the skill superset
    (the skill was renamed/removed), or a broken symlink (its checkout moved or was deleted).
    Regular files/dirs and symlinks resolving elsewhere are vendor skills — never touched."""
    if not os.path.islink(path):
        return False
    target = os.path.realpath(path)
    if not os.path.exists(target):
        return True
    if repo_root:
        root = os.path.realpath(repo_root)
        return target == root or target.startswith(root + os.sep)
    return False


def build_isolated_home(
    dest_home: str,
    skills_subpaths: Iterable[str],
    repo_skill_names: Iterable[str],
    declared_skill_dirs: Iterable[str],
    real_home: Optional[str] = None,
    plugin_registry_subpaths: Iterable[str] = (),
    repo_root: Optional[str] = None,
    config_file_masks: Optional[Mapping[str, Optional[str]]] = None,
    plugin_config_masks: Optional[Mapping[str, str]] = None,
) -> str:
    """Build a symlink overlay of ``real_home`` at ``dest_home`` with masked skills dirs.

    Args:
      dest_home: a fresh (ideally empty) directory to populate; returned for convenience.
      skills_subpaths: HOME-relative global skills dirs to mask, e.g. ``.claude/skills`` or
        ``.gemini/config/skills`` (arbitrary nesting; missing ones are created empty).
      repo_skill_names: skill names this repo can provision — removed from the masked dirs.
      declared_skill_dirs: absolute skill source dirs to symlink into each masked dir.
      real_home: the HOME to mirror (defaults to ``~``).
      plugin_registry_subpaths: HOME-relative plugin-registry dirs (e.g.
        ``.gemini/config/plugins``) whose entries are plugin packages, each optionally
        holding a nested ``skills/`` — mask-only, unlike skills_subpaths (see module
        docstring).
      repo_root: this repo's checkout root. Skills-dir symlinks resolving under it are
        masked even when their name is no longer in ``repo_skill_names`` — stale installs
        of renamed/removed skills (broken symlinks are masked too). None disables the
        target-based check; the name-based mask still applies.
      config_file_masks: HOME-relative config *file* paths mapped to the content to
        materialize them with instead of symlinking the real file — written even when the
        real file doesn't exist, so a CLI can never fall back to non-empty defaults (see
        module docstring). Content forms: a string (neutral replacement), ``None`` (mask
        as an empty real *directory*), or a callable ``(real_path) -> str`` (sanitize the
        real file's content). Paths must be relative and ``..``-free (ValueError
        otherwise) — a traversing path would write outside the overlay.
      plugin_config_masks: file names materialized (with the given content) inside every
        plugin of every ``plugin_registry_subpaths`` dir, e.g. ``{"mcp_config.json": "{}"}``
        — per-plugin MCP configs are a server-discovery channel of their own.

    Raises OSError if a symlink can't be created (e.g. Windows without privilege); the caller
    should fall back to a non-isolated run.
    """
    real_home = os.path.abspath(real_home or os.path.expanduser("~"))
    repo_skills = set(repo_skill_names or ())
    declared = [os.path.abspath(d) for d in (declared_skill_dirs or ()) if os.path.isdir(d)]
    plugin_masks = dict(plugin_config_masks or {})

    # Build a nested tree of "special" paths:
    # name -> subtree(dict) | _SKILLS_LEAF | _PLUGINS_LEAF | _FileMaskLeaf.
    tree: dict = {}
    _insert_leaf(tree, skills_subpaths, _SKILLS_LEAF)
    _insert_leaf(tree, plugin_registry_subpaths, _PLUGINS_LEAF)
    for sub, content in (config_file_masks or {}).items():
        _validate_mask_subpath(sub)
        _insert_leaf(tree, [sub], _FileMaskLeaf(content))

    os.makedirs(dest_home, exist_ok=True)
    _overlay(real_home, dest_home, tree, repo_skills, declared, repo_root, plugin_masks)
    return dest_home


def home_write_escapes(home: Optional[str]) -> list[str]:
    """Overlay paths a write would travel to land in the REAL home.

    The overlay masks what the model can READ. It was never a boundary on what the model can
    WRITE: step 1 of `_overlay` passes every unmasked real-HOME entry through as a symlink,
    so `$HOME/.cache/x` IS `~/.cache/x`. Review wrote a token through one of those and
    watched the overlay's removal succeed while the token stayed in the real home — outside
    every directory this harness deletes and outside the workspace it scrubs.

    Returns HOME-relative paths of EVERY symlink resolving outside *home*, sorted —
    whatever it points at. The first version reported only directory symlinks, reasoning
    that a file symlink can be clobbered but not used to plant a new file. Clobbering is the
    leak: review symlinked a `state.json`, the gate reported nothing, and writing through it
    replaced the real file's contents with the token. A dangling symlink is worse still — it
    has no target to inspect and a write CREATES one outside. "What kind of thing is at the
    other end" is not the question; "does this name lead out of the tree we can account for"
    is, and it has the same answer for all three.

    Walks the overlay, never through it (``followlinks=False``): the cost is the materialized
    part of the tree, not the real home hanging off its symlinks.
    """
    if not home or not os.path.isdir(home):
        return []
    root = os.path.abspath(home)
    inside = root + os.sep
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in list(dirnames) + list(filenames):
            path = os.path.join(dirpath, name)
            if not os.path.islink(path):
                continue
            # realpath, not lstat: a dangling link still resolves to the path a write would
            # create, and that path is exactly what needs to be inside the overlay.
            target = os.path.realpath(path)
            if target != root and not target.startswith(inside):
                found.append(os.path.relpath(path, root))
    return sorted(found)


def _validate_mask_subpath(sub: str) -> None:
    """Masks are opened for writing (O_TRUNC) at the joined path — an absolute,
    drive-anchored, or ``..``-traversing subpath would clobber a file outside the overlay.
    Checked platform-independently (``C:evil.json`` is drive-relative on Windows even
    though POSIX ``isabs`` says no). Empty and dot-only paths (``""``, ``"."``, ``"./"``)
    are rejected too: they name no file, so ``_insert_leaf`` would silently discard the
    mask — an adapter typo would quietly leave the real config live."""
    s = str(sub)
    parts = s.replace("\\", "/").split("/")
    if (os.path.isabs(s) or s.startswith(("/", "\\")) or os.path.splitdrive(s)[0]
            or (len(s) >= 2 and s[0].isalpha() and s[1] == ":") or ".." in parts):
        raise ValueError(f"config mask path must be HOME-relative without '..': {sub!r}")
    if not [p for p in parts if p not in ("", ".")]:
        raise ValueError(f"config mask path names no file: {sub!r}")


def _insert_leaf(tree: dict, subpaths: Iterable[str], leaf: object) -> None:
    """Split each of ``subpaths`` into path segments and mark its terminal node as ``leaf``,
    building out any intermediate ancestors as plain subtrees along the way."""
    for sub in subpaths or ():
        parts = [p for p in str(sub).replace("\\", "/").split("/") if p and p != "."]
        if not parts:
            continue
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = leaf


def _overlay(real_dir: str, dst_dir: str, tree: dict,
             repo_skills: set, declared: list, repo_root: Optional[str],
             plugin_masks: Optional[dict] = None) -> None:
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
            _build_skills_dir(real_child, dst_child, repo_skills, declared, repo_root)
        elif node is _PLUGINS_LEAF:
            _mask_plugin_registry_dir(real_child, dst_child, repo_skills, repo_root,
                                      plugin_masks)
        elif isinstance(node, _FileMaskLeaf):
            _write_mask_file(dst_child, node.content, real_child)
        else:
            _overlay(real_child, dst_child, node, repo_skills, declared, repo_root,
                     plugin_masks)


def resolve_visible_skills(
    adapter: Any,
    declared_names: Iterable[str],
    repo_skill_names: Iterable[str],
    isolated: bool,
    real_home: Optional[str] = None,
    repo_root: Optional[str] = None,
) -> dict:
    """What skills the model would see, computed from the filesystem (no agent run).

    Reads the adapter's global skills dirs and classifies their entries against this repo's
    skills. Returns sorted lists:
      provisioned   — the declared skills (always visible, from the workspace);
      vendor        — non-repo entries in the global dirs (kept even under isolation);
      masked        — repo skills present globally but not declared (hidden when isolated);
                      includes stale installs — symlinks into `repo_root` under retired
                      names, and broken symlinks;
      also_visible  — same set, shown when NOT isolated (they leak in).
    Skills bundled inside a CLI package live outside these dirs and aren't listed; skills
    nested in a plugin registry (``global_plugin_registry_subpaths``) are.
    """
    real_home = os.path.abspath(real_home or os.path.expanduser("~"))
    declared = set(declared_names or ())
    repo = set(repo_skill_names or ())
    vendor: set = set()
    leaked_repo: set = set()   # repo skills found globally but not declared

    def _classify(parent_dir: str, names: Iterable[str]) -> None:
        for name in names:
            if name in repo or _is_stale_repo_link(os.path.join(parent_dir, name), repo_root):
                if name not in declared:
                    leaked_repo.add(name)
            else:
                vendor.add(name)

    scan_dirs = [os.path.join(real_home, sub)
                 for sub in getattr(adapter, "global_skills_subpaths", []) or []]
    # a custom config home (e.g. $CODEX_HOME) holds skills outside the HOME-relative dirs
    for var, _replaces, skills_sub in config_home_entries(adapter):
        custom = os.environ.get(var)
        if custom and skills_sub:
            scan_dirs.append(os.path.join(custom, skills_sub))
    for d in scan_dirs:
        if os.path.isdir(d):
            _classify(d, os.listdir(d))

    # plugin registries nest skills one level deeper, under each plugin's own skills/.
    for sub in getattr(adapter, "global_plugin_registry_subpaths", []) or []:
        registry = os.path.join(real_home, sub)
        if not os.path.isdir(registry):
            continue
        for plugin_name in os.listdir(registry):
            plugin_skills = os.path.join(registry, plugin_name, "skills")
            if os.path.isdir(plugin_skills):
                _classify(plugin_skills, os.listdir(plugin_skills))

    return {
        "provisioned": sorted(declared),
        "vendor": sorted(vendor),
        "masked": sorted(leaked_repo) if isolated else [],
        "also_visible": [] if isolated else sorted(leaked_repo),
    }


def _build_skills_dir(real_skills: str, dst_skills: str,
                      repo_skills: set, declared: list, repo_root: Optional[str]) -> None:
    """Rebuild one skills dir: vendor/other entries passed through, repo skills dropped
    (by name, or by symlink target for stale installs), declared skills added."""
    os.makedirs(dst_skills, exist_ok=True)
    placed: set = set()
    if os.path.isdir(real_skills):
        for name in os.listdir(real_skills):
            if name in repo_skills:
                continue  # drop this repo's skills; declared ones are re-added below
            if _is_stale_repo_link(os.path.join(real_skills, name), repo_root):
                continue  # stale install of a renamed/removed repo skill
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


def _write_mask_file(path: str, content, real_path: Optional[str] = None) -> None:
    """Materialize one config mask: a real file with the supplied content — for ``None``,
    an empty real directory; for a callable, the string it derives from the real file at
    ``real_path`` (sanitizing masks) — never a symlink to the real HOME's copy. 0600/0700
    because the same mechanism that neutralizes with ``{}`` today will materialize declared
    MCP configs — which can carry credentials — in a later phase (DESIGN_MCP_Support.md §4)."""
    if content is None:
        os.makedirs(path, mode=0o700, exist_ok=True)
        return
    if callable(content):
        content = content(real_path or path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)


def _mask_plugin_registry_dir(real_dir: str, dst_dir: str, repo_skills: set,
                              repo_root: Optional[str],
                              plugin_masks: Optional[dict] = None) -> None:
    """Rebuild one plugin-registry dir: each child is a whole plugin package, passed through
    untouched (plugin.json, metadata, …) except its nested ``skills/``, where this repo's own
    skills are dropped, and any ``plugin_masks`` names (e.g. the plugin's own
    ``mcp_config.json``), materialized with the mask content instead of symlinked. Skills
    handling is mask-only — declared skills aren't re-added here; they're already injected
    once via the primary skills dir, so duplicating them into every unrelated vendor
    plugin's skills/ would just be clutter."""
    plugin_masks = plugin_masks or {}
    os.makedirs(dst_dir, exist_ok=True)
    if not os.path.isdir(real_dir):
        return
    for plugin_name in os.listdir(real_dir):
        real_plugin = os.path.join(real_dir, plugin_name)
        dst_plugin = os.path.join(dst_dir, plugin_name)
        if not os.path.isdir(real_plugin):
            os.symlink(real_plugin, dst_plugin)
            continue
        os.makedirs(dst_plugin, exist_ok=True)
        for name in os.listdir(real_plugin):
            if name == "skills":
                continue
            if name in plugin_masks:
                _write_mask_file(os.path.join(dst_plugin, name), plugin_masks[name])
                continue
            os.symlink(os.path.join(real_plugin, name), os.path.join(dst_plugin, name))
        real_skills = os.path.join(real_plugin, "skills")
        if os.path.isdir(real_skills):
            # mask-only: an empty `declared` makes _build_skills_dir's re-add step a no-op,
            # leaving just its drop-repo-skills/symlink-the-rest behavior.
            _build_skills_dir(real_skills, os.path.join(dst_plugin, "skills"), repo_skills,
                              [], repo_root)


def reroot_config_masks(masks: Mapping[str, Optional[str]], replaces: Optional[str]) -> dict:
    """Re-root HOME-relative masks for a custom config home that *stands in for* one HOME
    subdir (e.g. $COPILOT_HOME replaces ``~/.copilot``): ``.copilot/mcp-config.json`` →
    ``mcp-config.json``. Masks outside ``replaces`` don't apply inside that home."""
    if not replaces:
        return {}
    prefix = replaces.rstrip("/") + "/"
    return {p[len(prefix):]: content for p, content in (masks or {}).items()
            if p.startswith(prefix)}


def build_mcp_masked_home(adapter: Any, real_home: Optional[str] = None) -> tuple[Optional[str], dict]:
    """A mask-only HOME overlay for invocations that need MCP hermeticity but not skill
    isolation — model probes and judge runs, which otherwise execute against the real HOME
    and load the user's real MCP servers. Everything passes through (auth, config, skills);
    only the adapter's MCP config masks are applied — in the overlay, in every plugin of its
    plugin registries, and in any *set* custom config home (e.g. $COPILOT_HOME), which is
    mirrored with re-rooted masks so pointing HOME elsewhere can't bypass them.

    Returns ``(home_dir, env_overrides)`` — ``(None, {})`` when the adapter declares no
    masks (nothing to neutralize; run against the real HOME). The caller owns ``home_dir``:
    thread ``env_overrides`` via ``RunOptions.isolation_env`` and delete ``home_dir`` when
    the invocation is done. Raises OSError like ``build_isolated_home`` when the overlay
    can't be built — callers decide whether that fails closed.
    """
    masks = dict(getattr(adapter, "isolation_config_masks", {}) or {})
    plugin_masks = dict(getattr(adapter, "plugin_registry_config_masks", {}) or {})
    registries = (list(getattr(adapter, "global_plugin_registry_subpaths", []) or [])
                  if plugin_masks else [])
    if not masks and not registries:
        return None, {}
    home = tempfile.mkdtemp(prefix="ase-mcpmask-")
    try:
        build_isolated_home(home, [], (), (), real_home,
                            plugin_registry_subpaths=registries,
                            config_file_masks=masks,
                            plugin_config_masks=plugin_masks)
        env: dict = {}
        cfg_root = None
        for var, replaces, _skills_sub in config_home_entries(adapter):
            custom = os.environ.get(var)
            if not custom or not os.path.isdir(custom):
                continue
            # mirror even when no mask re-roots into this home: adapter.env() CLEARS any
            # config-home var absent from isolation_env (so it can't bypass the overlay),
            # which would silently drop the user's custom config/auth.
            if cfg_root is None:
                cfg_root = tempfile.mkdtemp(prefix="cfg-", dir=home)
            mirror = os.path.join(cfg_root, "".join(c if c.isalnum() else "_" for c in var))
            build_isolated_home(mirror, [], (), (), custom,
                                config_file_masks=reroot_config_masks(masks, replaces))
            env[var] = mirror
        return home, env
    except Exception:
        shutil.rmtree(home, ignore_errors=True)
        raise
