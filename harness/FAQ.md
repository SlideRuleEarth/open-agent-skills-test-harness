# harness — FAQ

A short, plain-language FAQ for the skill test harness.

## How do I check whether a skill actually helps on a given prompt?

Run a simple A/B test: take one prompt and run it twice on the same model — once with the skill
provisioned and once without (`--no-provision`) — then compare the graded results. Because
isolation is on by default, the "without" run does not get repo skills through the harness's
tracked skill-discovery paths, so the provisioned skill is the intended variable.

The step-by-step walkthrough — writing the scenario, the neutral-prompt gotcha, previewing with
`--dry-run`, and reading the two result sets — is in **[Simple-A-B-Test.md](HowTos/Simple-A-B-Test.md)**.

## Can I test a combination of skills?

Yes. List several skills under a scenario's `skills:` block and they're provisioned **together**;
with isolation on (the default) they're the only repo skills exposed through normal skill
discovery, so you're testing exactly that combination working in concert.

The walkthrough — writing the multi-skill scenario, rubrics that check the skills hand off to each
other, and previewing with `--dry-run` — is in
**[Test-Skill-Combinations.md](HowTos/Test-Skill-Combinations.md)**.

## I just cloned this repo — how do I make the skills work with my install of AntiGravity (or another agentic app)?

This repo is the single source of truth; each agent runtime scans a different skills directory, and
the `Makefile` points them all there via symlinks. For a fresh clone the **project-level** links
(including `.antigravity/skills/`) are already committed, so running your agent from the repo
usually just works. To use the skills from **anywhere**, `make link-global` wires them into your
per-user dirs — for AntiGravity that's `~/.gemini/config/skills` (the `agy` CLI) and
`~/.gemini/antigravity-ide/skills` (the IDE). For an app that isn't wired up yet, add its skills
dir to the `Makefile` and re-run.

The full walkthrough — in-repo vs global install, AntiGravity's two homes, adding a brand-new app,
and non-macOS notes — is in **[Install-For-Your-Agent.md](HowTos/Install-For-Your-Agent.md)**.

## What are "scenarios"?

Most evals in this repo are **per-skill** — one skill, auto-discovered from each skill's `evals/`
folder. A **scenario** is the higher-level, *ad-hoc* kind: a single self-describing file that
provisions a **combination of skills together** and pins a **target** (`runner:model`). You run one
explicitly by path — `agentskill-evals run --config <file>` — because scenarios are *not*
auto-discovered. Since runs are isolated by default, a scenario tests exactly the skills it lists
(plus the agent's own vendor skills) and nothing else from this repo; CLI flags override the file
(`CLI > scenario > default`).

They live in [`../scenarios/`](../scenarios/) — see [scenarios/README.md](../scenarios/README.md) for
the file format. For scenarios in action, see the
[combination](HowTos/Test-Skill-Combinations.md) and [A/B](HowTos/Simple-A-B-Test.md) how-tos.

## Which skills can the model actually see during a test?

**Short answer:** by default, normal skill discovery sees only the skills that test provisions for
itself — plus the agent's own built-in skills. Each test runs in an isolated home directory that
hides this repo's skills you've installed globally, so the normal path is exactly the set the
test declares.

**What the harness does for each test**

1. Creates a fresh, throwaway workspace just for that one run.
2. Adds the test's declared skills into that workspace.
3. Runs the agent against a private home that mirrors your real one — so your logins, settings,
   and the agent's own bundled skills keep working — but with this repo's global skills hidden.
4. So normal skill discovery sees the provisioned skills plus the agent's vendor skills, and no
   other repo skills you happen to have installed in the tracked global/project locations.

**Want to test against your real, installed setup instead?** Pass `--no-isolated`. Then the
agent also sees whatever skills you've installed globally — the test's skills *plus* every
other repo skill on your machine. (Handy for reproducing "works on my box" differences.)

**Why isolation is the default**

Agents discover skills from several places — the current project *and* your personal (global)
skills folder. Without isolation, a test that lists one skill could still "see" every other
skill you've installed. For example, this repo can install all of its skills globally (the
"install everywhere" step); an un-isolated test would then run with all of them visible,
muddying what the test actually proves.

**What about the agent's own (vendor) skills?**

Those are kept. Isolation only hides *this repo's* skills from the global folders; the
platform's own skills — the ones your agent ships with — stay available, because a test should
run against the agent's normal baseline, not a stripped-down version of it.

**How do I see what will be visible before spending anything?**

Run with `--dry-run`. It prints a "Skills visible to the model" block per target — what's
provisioned, what vendor skills are kept, and what's masked — with no API calls. You can also
run `agentskill-evals list-skills` to audit the full picture and catch drift (for example, a
global install that's missing a skill).

**Good to know**

- Your logins, settings, and git config still work under isolation — only this repo's global
  skills are masked.
- This is skill-visibility isolation, not an OS-level jail. A deliberate broad-disk search can
  still find the real checkout; if the captured trace shows an undeclared repo-skill read, that
  cell is reported as not isolated.
- `--no-provision` by itself creates a no-repo-skills baseline under isolation. Pair it with
  `--no-isolated` when you intentionally want to rely on your real global install.
