# sliderule-skills

A set of open-standard Agent Skills for working with [SlideRule Earth](https://slideruleearth.io). Each top-level folder is a self-contained skill that can be used by any platform that supports the open Agent Skills standard (Claude Code, the Claude Agent SDK, etc.).

## Skills in this repo

- [sliderule-api](sliderule-api/) — Query NASA ICESat-2 and GEDI data via the SlideRule HTTP API
- [sliderule-docsearch](sliderule-docsearch/) — Semantic search of the SlideRule documentation
- [sliderule-examples](sliderule-examples/) — Worked examples from the SlideRule Python client notebooks
- [sliderule-openapi](sliderule-openapi/) — OpenAPI specs for the SlideRule endpoints
- [sliderule-params](sliderule-params/) — Reference for SlideRule request parameters
- [sliderule-pipeline](sliderule-pipeline/) — Directives for orchestrating SlideRule analyses as single-script pipelines
- [sliderule-region-picker](sliderule-region-picker/) — Interactive map for defining geographic regions
- [nsidc-reference](nsidc-reference/) — Reference for NASA NSIDC and ORNL DAAC data products

## Installing the skills (one source of truth)

These are runtime-level skills, not editor plugins — the agent runtime (Claude Code, the Claude Agent SDK, Codex) scans a skills directory at startup regardless of whether you launch it from a terminal, an IDE extension, or over SSH. VSCode is irrelevant; "not using VSCode" changes nothing about installation.

To make this repo the single source of truth, symlink each skill folder into the directory that the runtime scans. Updating the repo then updates every consumer:

| Runtime | Global skills directory |
| --- | --- |
| Claude Code | `~/.claude/skills/` |
| Claude Agent SDK | `~/.claude/skills/` (same as Claude Code — see note) |
| Codex | `~/.codex/skills/` |

> **Note:** The Claude Agent SDK does **not** use a separate `~/.agents/skills/` directory. It reads the same personal `~/.claude/skills/` and project-level `.claude/skills/` locations as the Claude Code CLI (controlled by the SDK's `settingSources`), so installing into `~/.claude/skills/` covers both. Symlink-following into these directories works in practice but is not officially documented — treat it as best-effort rather than a guarantee.

### 1. Create the target directories

```bash
mkdir -p ~/.claude/skills ~/.codex/skills
```

### 2. Symlink each skill

From inside this repo:

```bash
REPO="$(pwd)"
for skill in sliderule-api sliderule-docsearch sliderule-examples sliderule-openapi sliderule-params sliderule-pipeline sliderule-region-picker nsidc-reference; do
  ln -sfn "$REPO/$skill" "$HOME/.claude/skills/$skill"
  ln -sfn "$REPO/$skill" "$HOME/.codex/skills/$skill"
done
```

`ln -sfn` replaces any existing symlink at the target path, so this is safe to re-run after adding new skills. It will refuse to overwrite a real directory — if you already have a non-symlink copy of a skill installed, remove or rename it first.

### 3. Verify

```bash
ls -l ~/.claude/skills ~/.codex/skills
```

Each entry should show an arrow pointing back to this repo, e.g. `sliderule-api -> /path/to/sliderule-skills/sliderule-api`.

### Installing a single skill

If you only want one skill:

```bash
ln -sfn "$(pwd)/sliderule-api" ~/.claude/skills/sliderule-api
ln -sfn "$(pwd)/sliderule-api" ~/.codex/skills/sliderule-api
```

### Uninstalling

Symlinks can be removed without affecting the repo:

```bash
rm ~/.claude/skills/sliderule-api ~/.codex/skills/sliderule-api
```

### Without a global install (project-level)

You don't have to install globally. Claude Code (and the Agent SDK) also discover skills from a project-level `.claude/skills/` directory — in the working directory, every parent up to the repository root, and nested subdirectories on demand. Two ways to use this:

- Run Claude Code from a directory that has the repo's skills under `.claude/skills/`, or
- Point Claude Code at this repo with `--add-dir /path/to/sliderule-skills` — a `.claude/skills/` inside an added directory is loaded automatically.

Precedence is enterprise > personal (`~/.claude`) > project (`.claude`) > plugins.

## Non-macOS users

The skills are plain text and Python and run anywhere; only the install mechanics differ.

### Linux

Identical to macOS. The `mkdir`/`ln -sfn` instructions above work verbatim.

### Windows

The skills directories live under your user profile — `%USERPROFILE%\.claude\skills\` and `%USERPROFILE%\.codex\skills\` — but the Bash `ln` loop won't run in `cmd`/PowerShell. Pick one:

- **Directory junctions (recommended — no admin needed).** The closest equivalent to the symlink "single source of truth" model. In `cmd`:

  ```cmd
  mklink /J "%USERPROFILE%\.claude\skills\sliderule-api" "C:\path\to\sliderule-skills\sliderule-api"
  ```

  Junctions work for directories without elevation, and a `git pull` in the repo updates every consumer.

- **PowerShell symlinks (need admin or Developer Mode).**

  ```powershell
  $repo = "C:\path\to\sliderule-skills"
  foreach ($s in "sliderule-api","sliderule-docsearch","sliderule-examples","sliderule-openapi","sliderule-params","sliderule-pipeline","sliderule-region-picker","nsidc-reference") {
    New-Item -ItemType SymbolicLink -Path "$env:USERPROFILE\.claude\skills\$s" -Target "$repo\$s" -Force
  }
  ```

- **Plain copy (works anywhere, loses auto-update).** Copy the folders, or use Codex's `skill-installer` (below) — you then re-copy to update.

> Symlink/junction following in the skills directory works in practice but is undocumented; on Windows, prefer junctions.

### WSL

If you run Claude Code or Codex *inside* WSL, treat it as Linux and use the macOS/Linux instructions — but note WSL has its own `$HOME`, separate from your Windows user profile, so install into the WSL home.

## Using the skills with Codex

Codex supports the same open Agent Skills standard and discovers these skills in **two** ways.

### Per-project (no install)

When Codex runs with its working directory at or under a **trusted** project, it scans that project tree for `*/SKILL.md` and registers each skill for the session — so simply running `codex` inside a clone of this repo exposes all the `sliderule-*` skills with no install step. Mark the repo trusted in `~/.codex/config.toml`:

```toml
[projects."/path/to/sliderule-skills"]
trust_level = "trusted"
```

These skills are then visible only while working in that repo.

### Global (every project)

To make the skills available from any working directory, get them into `~/.codex/skills/` — either with the symlink loop above, or by asking Codex to use its built-in **`skill-installer`** skill to install from this repo's path. User skills sit alongside the built-in `.system/` skills in that directory.

## Using the skills in a hosted agent app (e.g. Claude.ai)

Hosted agent apps don't scan a local skills directory — there's no filesystem to symlink into. Instead they typically take each skill as an uploaded zip through a skills/capabilities setting. This repo ships the tooling to produce those zips.

### 1. Build the zips

From inside this repo:

```bash
make export
```

This writes one `<skill>.zip` per skill into `exports/`, each containing the skill's top-level folder and its `SKILL.md` — the layout these apps expect. To build a single skill:

```bash
make export-sliderule-api
```

(`make export` just runs `export.py`; see `python export.py -h` for its options, including a custom output directory.)

### 2. Upload the zips

Upload each zip from `exports/` through the app's skill-management settings, then repeat for the skills you want. Exact navigation varies by app — e.g. on [Claude.ai](https://claude.ai) the skills live under **Settings → Capabilities**: open the **Skills** section, choose **Upload skill**, and pick a zip. Custom uploads generally require a paid plan with skills enabled (and may be admin-gated on team/enterprise tiers).

Uploaded skills are then offered to the agent across your chats, the same way the runtime-installed skills work locally.

### Code execution

Several of these skills (e.g. `sliderule-docsearch`, `sliderule-openapi`) run Python helper scripts, so a skill only works end-to-end where the agent can execute code in a sandbox — e.g. Claude.ai runs skill scripts in its hosted sandbox when code execution is enabled. Without that, only the prose in each `SKILL.md` is available, not the script-backed lookups.

### Updating

A zip is a point-in-time snapshot, not a live link — there's no auto-update equivalent to the symlink model used by the CLI runtimes. After changing a skill, re-run `make export` and re-upload the new zip to pick up the changes.
