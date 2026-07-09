# open-agent-skills-test-harness

A cross-agent **test harness for evaluating [Agent Skills](https://platform.claude.com/docs/en/agents-and-tools/agent-skills)** across models and runtimes. Write one eval, run it against Claude Code, Codex, GitHub Copilot, and AntiGravity, and grade the result with deterministic assertions and an LLM judge. The repo also ships a couple of **example skills** plus tooling to install and export any skills you keep here.

**Two parts:**

1. **Harness** ([`harness/`](harness/)) — a CLI tool (`agentskill-evals`) that evaluates skills across multiple agent CLIs (Claude Code, GitHub Copilot, OpenAI Codex, AntiGravity). It provisions skills into isolated workspaces, runs a prompt through an agent, then grades the result with deterministic assertions (file exists, skill triggered, etc.) and an LLM judge (rubric-based). Evals live per-skill in `<skill>/evals/*.yaml`; scenarios in `scenarios/` combine multiple skills with a pinned runner/model. **This is the durable purpose of the repo.**

2. **Example skills** (under [`skills_examples/`](skills_examples/)) — a small, illustrative set used to exercise and demonstrate the harness. Each contains a `SKILL.md` and follows the cross-agent conventions (`.claude/skills/`, `.agents/skills/`, `.antigravity/skills/`) so it works with any platform that supports the open Agent Skills standard.

**Key design choices:** an adapter abstraction normalizes each CLI's output into a common event stream; workspace isolation (HOME overlay + git boundary) ensures agents see only provisioned skills; the judge runs through the same adapter machinery so any agent can grade any other.

## Example skills in this repo

The bundled skills are **examples** — this repo is a harness, not a skills library. They currently target [SlideRule Earth](https://slideruleearth.io) (a NASA ICESat-2/GEDI cloud-processing service):

- [sliderule-pipeline-direct_request](skills_examples/sliderule-pipeline-direct_request/) — Directives for orchestrating SlideRule analyses as single-script pipelines
- [sliderule-region-picker](skills_examples/sliderule-region-picker/) — Interactive map for defining geographic regions

> **Just want to use these skills?** Install them below and you're done — the harness is optional.
>
> **Want to test your _own_ skill?** You don't need to add it to this repo or install anything into `.claude/skills` — point the harness at any directory of skills with `--skills-root`. See [Bring your own skills](harness/README.md#bring-your-own-skills-external-skills-root).

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
# each entry: sliderule-pipeline-direct_request -> /path/to/open-agent-skills-test-harness/skills_examples/sliderule-pipeline-direct_request
```

`ln -sfn` (used by the targets) replaces existing symlinks, so the targets are safe to re-run after adding skills. They won't overwrite a real directory — if you have a non-symlink copy of a skill installed, remove it first.

### Adding a new surface

A "surface" is any tool that consumes the skills (Claude Code, Codex, AntiGravity, CoPilot, other aggregators…). Adding one has up to two parts, and most surfaces only need the first:

1. **Install support — always.** The linking is data-driven: add the surface's `<platform>/skills` directory to `PROJECT_SKILL_DIRS` (and its per-user dir to `GLOBAL_SKILL_DIRS`) in the `Makefile`, re-run `make link-project` / `make link-global`, and **add a row to the install table above** with that surface's specific install instructions. New surfaces always get installation instructions.
2. **Test-runner support — when you want to *test* through that surface.** A surface becomes a *test runner* in the eval harness by getting an adapter — see [Adding a new runner](harness/README.md#adding-a-new-runner). Two good reasons to add one: it reaches a **model** no existing runner covers, or it's **your actual setup** and you want the evals to reflect exactly how your surface runs the skills — the runner's scaffolding (skill injection, subagents, prompt execution) does influence behavior (see [Testing the skills across models](#testing-the-skills-across-models)). If neither applies — an aggregator that just re-runs models you don't use and that are already covered — step 1 is enough.

### How discovery works

Claude Code (and the Agent SDK) discover project-level skills from `.claude/skills/` in the working directory, every parent up to the repo root, and nested subdirectories on demand — so the committed `.claude/skills/` is found automatically when you work in this repo. You can also point an external Claude session at the repo with `--add-dir /path/to/open-agent-skills-test-harness` (a `.claude/skills/` inside an added directory is loaded automatically). Precedence: enterprise > personal (`~/.claude`) > project (`.claude`) > plugins.

## Testing the skills across models

Installation and testing are two different concerns:

- **Installation is per *surface*** (Claude Code, Codex, AntiGravity, CoPilot, other aggregators…) — covered above; every surface gets its own install instructions.
- **Test coverage is per *model*.** How well a skill works is dominated by the LLM, so the eval harness tests each skill across **models**, not across every surface. The runner's scaffolding does contribute — each CLI differs in how it injects/triggers skills, what tools and subagents it uses, and how it decomposes a prompt — so the same model can behave somewhat differently under different surfaces. By default we accept that variance: each model is tested through one designated runner, and that runner+model pair is the *tested configuration* — re-running an already-covered model through another surface isn't counted as new *model* coverage. But it can still be worth testing: if a particular surface (e.g. CoPilot, or any aggregator) is *your* setup, add it as a runner and evaluate through it directly so results reflect exactly how your surface runs the skills.

The harness, the model matrix (`models.yaml`), the cost guardrails, and how to add/retire models live in **[harness/](harness/README.md)**.

## Non-macOS users

The skills are plain text and Python and run anywhere; only the install mechanics differ.

### Linux

Identical to macOS. The `make link-project` / `make link-global` targets work verbatim.

### Windows

The `make` targets need a Unix shell (Git Bash or WSL) — run them there. In plain `cmd`/PowerShell, replicate the links manually into each agent's profile dir (`%USERPROFILE%\.claude\skills\`, `%USERPROFILE%\.agents\skills\`, `%USERPROFILE%\.gemini\config\skills\`, `%USERPROFILE%\.gemini\antigravity-ide\skills\`). The example below uses `.claude`; repeat for the others. Pick one:

- **Directory junctions (recommended — no admin needed).** The closest equivalent to the symlink "single source of truth" model. In `cmd`:

  ```cmd
  mklink /J "%USERPROFILE%\.claude\skills\sliderule-pipeline-direct_request" "C:\path\to\open-agent-skills-test-harness\skills_examples\sliderule-pipeline-direct_request"
  ```

  Junctions work for directories without elevation, and a `git pull` in the repo updates every consumer.

- **PowerShell symlinks (need admin or Developer Mode).**

  ```powershell
  $repo = "C:\path\to\open-agent-skills-test-harness"
  foreach ($s in "sliderule-pipeline-direct_request","sliderule-region-picker") {
    New-Item -ItemType SymbolicLink -Path "$env:USERPROFILE\.claude\skills\$s" -Target "$repo\skills_examples\$s" -Force
  }
  ```

- **Plain copy (works anywhere, loses auto-update).** Copy the folders, or use Codex's `skill-installer` (below) — you then re-copy to update.

> Symlink/junction following in the skills directory works in practice but is undocumented; on Windows, prefer junctions.

### WSL

If you run Claude Code or Codex *inside* WSL, treat it as Linux and use the macOS/Linux instructions — but note WSL has its own `$HOME`, separate from your Windows user profile, so install into the WSL home.

## Using the skills with Codex

Codex uses the cross-agent `.agents/skills/` convention — `$REPO_ROOT/.agents/skills/` for a project and `~/.agents/skills/` globally — and discovers these skills in **three** ways.

### Project-level (committed)

`make link-project` populates `.agents/skills/` in this repo, and those symlinks are committed — so a fresh clone already exposes every skill in this repo to Codex with no setup.

### Per-project trusted scan (no symlinks)

When Codex runs with its working directory at or under a **trusted** project, it also scans the tree for `SKILL.md` files (including the ones under `skills_examples/`) and registers each skill for the session. Mark the repo trusted in `~/.codex/config.toml`:

```toml
[projects."/path/to/open-agent-skills-test-harness"]
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
make export-sliderule-pipeline-direct_request
```

(`make export` just runs `export.py`; see `python export.py -h` for its options, including a custom output directory.)

### 2. Upload the zips

Upload each zip from `exports/` through the app's skill-management settings, then repeat for the skills you want. Exact navigation varies by app — e.g. on [Claude.ai](https://claude.ai) the skills live under **Customize → Skills**: click **+** → **+ Create skill** and upload a zip (use the entry's **⋮** menu → **Replace** to update an existing skill). Custom uploads generally require a paid plan with skills enabled (and may be admin-gated on team/enterprise tiers).

Uploaded skills are then offered to the agent across your chats, the same way the runtime-installed skills work locally.

### Updating

A zip is a point-in-time snapshot, not a live link — there's no auto-update equivalent to the symlink model used by the CLI runtimes. After changing a skill, re-run `make export` and re-upload the new zip to pick up the changes.
