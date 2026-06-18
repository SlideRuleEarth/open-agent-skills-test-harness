# agent-skill-evals — FAQ

A short, plain-language FAQ for the skill test harness.

## How do I check whether a skill actually helps on a given prompt?

Run a simple A/B test: take one prompt and run it twice on the same model — once with the skill
provisioned and once without (`--no-provision`) — then compare the graded results. Because
isolation is on by default, the "without" run sees no repo skills at all, so the skill is the only
variable.

The step-by-step walkthrough — writing the scenario, the neutral-prompt gotcha, previewing with
`--dry-run`, and reading the two result sets — is in **[Simple-A-B-Test.md](HowTos/Simple-A-B-Test.md)**.

## Which skills can the model actually see during a test?

**Short answer:** by default, only the skills that test provisions for itself — plus the
agent's own built-in skills. Each test runs in an isolated home directory that hides the
SlideRule skills you've installed globally, so the model sees exactly the set the test
declares (and nothing else from this repo).

**What the harness does for each test**

1. Creates a fresh, throwaway workspace just for that one run.
2. Adds the test's declared skills into that workspace.
3. Runs the agent against a private home that mirrors your real one — so your logins, settings,
   and the agent's own bundled skills keep working — but with this repo's global skills hidden.
4. So the model sees the provisioned skills plus the agent's vendor skills, and no other repo
   skills you happen to have installed.

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
- There's also a switch (`--no-provision`) to skip provisioning entirely, to rely solely on
  your global install.
