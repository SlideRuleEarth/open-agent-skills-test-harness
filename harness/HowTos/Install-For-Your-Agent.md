# Make the skills work with your agent (AntiGravity, Copilot, or another app)

You just cloned this repo and want your agent — AntiGravity, Claude Code, Codex, GitHub Copilot,
or something else — to actually see the `sliderule-*` skills in your **day-to-day sessions**. This
is an **installation** step, separate from testing.

> **You do NOT need any of this to run the evals.** The harness provisions each eval's declared
> skills itself — copied into an isolated, throwaway workspace — so a fresh clone plus the
> [harness CLI](../README.md#install) and your agent's own CLI is enough to test. Skills you
> already have installed globally aren't touched (and this repo's, if installed, are masked
> during runs so they can't contaminate results). The targets below are only for *using* the
> skills in normal agent sessions.

## How it works

This repo is the **single source of truth**. Each agent runtime scans a *different* skills
directory, so the [`Makefile`](../../Makefile) points them all at the real skill folders here via
symlinks. Update the repo → every consumer updates.

| Runtime | Project-level (committed, in-repo) | Global (per-user) |
| --- | --- | --- |
| Claude Code / Agent SDK | `.claude/skills/` | `~/.claude/skills/` |
| Codex | `.agents/skills/` | `~/.agents/skills/` |
| GitHub Copilot CLI | `.agents/skills/` | `~/.agents/skills/` |
| AntiGravity CLI (`agy`) | `.antigravity/skills/` | `~/.gemini/config/skills/` |
| AntiGravity IDE | `.antigravity/skills/` | `~/.gemini/antigravity-ide/skills/` |

## Option A — work *inside* the cloned repo (usually already done)

The project-level symlinks (`.claude/skills/`, `.agents/skills/`, `.antigravity/skills/`) are
**committed**, so a fresh clone already exposes every skill. Just run your agent from the repo root
(or point it here) — AntiGravity picks them up from `.antigravity/skills/`, Claude Code from
`.claude/skills/`, Codex and the Copilot CLI from `.agents/skills/`.

If the links didn't survive the clone (e.g. a Windows checkout without symlink support), rebuild
them:

```bash
make relink-project     # unlink-project + link-project
```

## Option B — use the skills from *any* directory (global install)

To reach the skills outside this repo, symlink them into your per-user agent dirs:

```bash
make link-global
```

That populates `~/.claude/skills`, `~/.agents/skills`, **`~/.gemini/config/skills`** (AntiGravity
CLI `agy`), and **`~/.gemini/antigravity-ide/skills`** (AntiGravity IDE). These are absolute
symlinks into this checkout (not committed), so a later `git pull` here updates every consumer.
Verify:

```bash
ls -l ~/.gemini/config/skills ~/.gemini/antigravity-ide/skills
# each entry: sliderule-pipeline-direct-request -> /path/to/open-agent-skills-test-harness/skills_examples/sliderule-pipeline-direct-request
```

`make unlink-global` removes them. `make link-global` uses `ln -sfn`, so it's safe to re-run after
adding skills — but it won't clobber a *real* directory; if you have a non-symlink copy of a skill
installed, remove that first.

> AntiGravity has **two** homes — `~/.gemini/config/skills` for the `agy` CLI and
> `~/.gemini/antigravity-ide/skills` for the IDE. `make link-global` writes both, so it doesn't
> matter which one you use. (Symlink-following into these dirs works in practice but isn't
> officially documented — treat it as best-effort.)

> The **GitHub Copilot CLI** shares the cross-agent `.agents/skills` convention with Codex, so
> `make link-global` already covers it via `~/.agents/skills` — there is no separate
> `~/.copilot/skills` directory to wire up. Run `copilot` from any directory and the globally
> linked skills are visible; inside this repo, the committed `.agents/skills/` links serve it
> project-level. (As with the others, this discovery path is convention-based rather than a
> documented guarantee — it's what the harness's copilot adapter relies on and works with the
> current CLI.)

## Option C — a different app that isn't wired up yet

If your agent scans some other directory, teach the Makefile about it once:

1. Add its **project** skills dir (`<platform>/skills`) to `PROJECT_SKILL_DIRS` and its **per-user**
   dir to `GLOBAL_SKILL_DIRS` in the [`Makefile`](../../Makefile).
2. Re-run `make link-project` and/or `make link-global`.
3. Add a row to the install table in the [root README](../../README.md) so the next person knows the
   path.

The linking is data-driven, so that's all it takes — no per-skill edits.

## Not on macOS?

The skills are plain text + Python and run anywhere; only the link mechanics differ. Linux is
identical to macOS. On Windows use directory junctions (`mklink /J`, no admin needed) or PowerShell
symlinks; under WSL treat it as Linux but install into the WSL `$HOME`. The
[root README](../../README.md) has the exact Windows/WSL commands.

See the [root README](../../README.md) for the canonical install reference, [FAQ.md](../FAQ.md) for
skill visibility during tests, and [README.md](../README.md) for the harness.
