# sliderule-skills

AI agent skills for [SlideRule Earth](https://slideruleearth.io) — a NASA ICESat-2/GEDI cloud processing service — plus a cross-agent test harness to evaluate how well those skills work across different models.

**Two parts:**

1. **Skills** (top-level dirs) — each contains a `SKILL.md` and supporting references/scripts that teach an AI agent how to use SlideRule's API, plan request parameters, search docs, etc. Skills follow cross-agent conventions (`.claude/skills/`, `.agents/skills/`, `.codex/skills/`) and work with any platform that supports the open Agent Skills standard.

2. **Harness** ([`harness/`](harness/)) — a CLI tool (`agentskill-evals`) that evaluates skills across multiple agent CLIs (Claude Code, GitHub Copilot, OpenAI Codex, AntiGravity). It provisions skills into isolated workspaces, runs a prompt through an agent, then grades the result with deterministic assertions (file exists, skill triggered, etc.) and an LLM judge (rubric-based). Evals live per-skill in `<skill>/evals/*.yaml`; scenarios in `scenarios/` combine multiple skills with a pinned runner/model.

**Key design choices:** an adapter abstraction normalizes each CLI's output into a common event stream; workspace isolation (HOME overlay + git boundary) ensures agents see only provisioned skills; the judge runs through the same adapter machinery so any agent can grade any other.

## Skills in this repo

- [sliderule-api](sliderule-api/) — Query NASA ICESat-2 and GEDI data via the SlideRule HTTP API
- [sliderule-docsearch](sliderule-docsearch/) — Semantic search of the SlideRule documentation
- [sliderule-examples](sliderule-examples/) — Worked examples from the SlideRule Python client notebooks
- [sliderule-openapi](sliderule-openapi/) — OpenAPI specs for the SlideRule endpoints
- [sliderule-params](sliderule-params/) — Reference for SlideRule request parameters
- [sliderule-pipeline](sliderule-pipeline/) — Directives for orchestrating SlideRule analyses as single-script pipelines
- [sliderule-region-picker](sliderule-region-picker/) — Interactive map for defining geographic regions
- [nsidc-reference](nsidc-reference/) — Reference for NASA NSIDC and ORNL DAAC data products

> **Just want to use the skills?** Install them below and you're done — the harness is optional.

## Installing the skills (one source of truth)

These are runtime-level skills, not editor plugins — the agent runtime (Claude Code, the Claude Agent SDK, Codex) scans a skills directory at startup regardless of whether you launch it from a terminal, an IDE extension, or over SSH. VSCode is irrelevant; "not using VSCode" changes nothing about installation.

This repo is the single source of truth. Each agent runtime scans a **different** skills directory, so we point them all at the real skill folders here via symlinks — managed by the Makefile. Updating the repo then updates every consumer.

| Runtime | Project-level (committed, in-repo) | Global (per-user) |
| --- | --- | --- |
| Claude Code / Agent SDK | `.claude/skills/` | `~/.claude/skills/` |
| Codex | `.agents/skills/` | `~/.agents/skills/` |
| AntiGravity CLI (`agy`) | `.antigravity/skills/` | `~/.gemini/config/skills/` |
| AntiGravity IDE | `.antigravity/skills/` | `~/.gemini/antigravity-ide/skills/` |

> **Note:** Claude Code (and the Agent SDK) read skills **only** from `.claude/skills/` — never `.agents/skills/`. Codex uses the cross-agent `.agents/skills/` convention (`$REPO_ROOT/.agents/skills/`, then `~/.agents/skills/`). AntiGravity keeps its skills under `~/.gemini/`. Symlink-following into these directories works in practice but isn't officially documented — treat it as best-effort.

### Project-level (committed — the recommended default)

`make link-project` creates **relative** symlinks under `.claude/skills/`, `.agents/skills/`, and `.antigravity/skills/`, each pointing at a skill folder in this repo. Git stores them as symlinks (mode `120000`), so they're committed once and **every clone gets all skills wired up for every agent automatically** — no per-user install.

```bash
make link-project      # create / refresh the in-repo symlinks
make relink-project    # rebuild after adding or removing a skill
make unlink-project    # remove them
```

Running any supported agent from the repo root (or pointing it here) then exposes every skill with zero global setup.

### Global (per-user — available from any directory)

To use the skills outside this repo, symlink them into your per-user agent dirs:

```bash
make link-global       # ~/.claude/skills, ~/.agents/skills, ~/.gemini/config/skills, ~/.gemini/antigravity-ide/skills
make unlink-global     # remove them
```

These are **absolute** symlinks into this checkout and are *not* committed; a `git pull` here then updates every consumer. Verify with:

```bash
ls -l ~/.claude/skills ~/.agents/skills
# each entry: sliderule-api -> /path/to/sliderule-skills/sliderule-api
```

`ln -sfn` (used by the targets) replaces existing symlinks, so the targets are safe to re-run after adding skills. They won't overwrite a real directory — if you have a non-symlink copy of a skill installed, remove it first.

### Adding a new surface

A "surface" is any tool that consumes the skills (Claude Code, Codex, AntiGravity, CoPilot, other aggregators…). Adding one has up to two parts, and most surfaces only need the first:

1. **Install support — always.** The linking is data-driven: add the surface's `<platform>/skills` directory to `PROJECT_SKILL_DIRS` (and its per-user dir to `GLOBAL_SKILL_DIRS`) in the `Makefile`, re-run `make link-project` / `make link-global`, and **add a row to the install table above** with that surface's specific install instructions. New surfaces always get installation instructions.
2. **Test-runner support — only if it adds model coverage.** A surface becomes a *test runner* in the eval harness only when it reaches a model no existing runner covers (see [Testing the skills across models](#testing-the-skills-across-models)). That needs an adapter — see [Adding a new runner](harness/README.md#adding-a-new-runner). Aggregators that only re-run already-covered models (e.g. CoPilot) stop at step 1.

### How discovery works

Claude Code (and the Agent SDK) discover project-level skills from `.claude/skills/` in the working directory, every parent up to the repo root, and nested subdirectories on demand — so the committed `.claude/skills/` is found automatically when you work in this repo. You can also point an external Claude session at the repo with `--add-dir /path/to/sliderule-skills` (a `.claude/skills/` inside an added directory is loaded automatically). Precedence: enterprise > personal (`~/.claude`) > project (`.claude`) > plugins.

## Testing the skills across models

Installation and testing are two different concerns:

- **Installation is per *surface*** (Claude Code, Codex, AntiGravity, CoPilot, other aggregators…) — covered above; every surface gets its own install instructions.
- **Test coverage is per *model*.** How well a skill works is determined by the LLM, not by which tool drives it, so the eval harness tests each skill across **models**, not across every surface. Running an already-covered model through another surface (e.g. CoPilot, or any aggregator) adds no coverage — those are install targets, not test targets.

The harness, the model matrix (`models.yaml`), the cost guardrails, and how to add/retire models live in **[harness/](harness/README.md)**.

## Non-macOS users

The skills are plain text and Python and run anywhere; only the install mechanics differ.

### Linux

Identical to macOS. The `make link-project` / `make link-global` targets work verbatim.

### Windows

The `make` targets need a Unix shell (Git Bash or WSL) — run them there. In plain `cmd`/PowerShell, replicate the links manually into each agent's profile dir (`%USERPROFILE%\.claude\skills\`, `%USERPROFILE%\.agents\skills\`, `%USERPROFILE%\.gemini\config\skills\`, `%USERPROFILE%\.gemini\antigravity-ide\skills\`). The example below uses `.claude`; repeat for the others. Pick one:

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

Codex uses the cross-agent `.agents/skills/` convention — `$REPO_ROOT/.agents/skills/` for a project and `~/.agents/skills/` globally — and discovers these skills in **three** ways.

### Project-level (committed)

`make link-project` populates `.agents/skills/` in this repo, and those symlinks are committed — so a fresh clone already exposes every `sliderule-*` skill to Codex with no setup.

### Per-project trusted scan (no symlinks)

When Codex runs with its working directory at or under a **trusted** project, it also scans the tree for `*/SKILL.md` and registers each skill for the session. Mark the repo trusted in `~/.codex/config.toml`:

```toml
[projects."/path/to/sliderule-skills"]
trust_level = "trusted"
```

### Global (every project)

`make link-global` symlinks the skills into `~/.agents/skills/`. Alternatively, ask Codex to use its built-in **`skill-installer`** skill to install from this repo's path. (Codex's built-in `.system/` skills live separately under `~/.codex/skills/.system/`.)

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

Upload each zip from `exports/` through the app's skill-management settings, then repeat for the skills you want. Exact navigation varies by app — e.g. on [Claude.ai](https://claude.ai) the skills live under **Customize → Skills**: click **+** → **+ Create skill** and upload a zip (use the entry's **⋮** menu → **Replace** to update an existing skill). Custom uploads generally require a paid plan with skills enabled (and may be admin-gated on team/enterprise tiers).

Uploaded skills are then offered to the agent across your chats, the same way the runtime-installed skills work locally.

### Code execution

Several of these skills (e.g. `sliderule-docsearch`, `sliderule-openapi`) run Python helper scripts, so a skill only works end-to-end where the agent can execute code in a sandbox — e.g. Claude.ai runs skill scripts in its hosted sandbox when code execution is enabled. Without that, only the prose in each `SKILL.md` is available, not the script-backed lookups.

### Updating

A zip is a point-in-time snapshot, not a live link — there's no auto-update equivalent to the symlink model used by the CLI runtimes. After changing a skill, re-run `make export` and re-upload the new zip to pick up the changes.
