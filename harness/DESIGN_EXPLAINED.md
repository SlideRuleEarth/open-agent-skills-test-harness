# The Test Harness, Explained

*A guided tour for someone new to this codebase (and to testing AI agents in general).*

This document assumes you can read Python and have used a terminal, but **not** that you
know what an "agent CLI," "hermetic run," or "LLM judge" is. Every term is defined the
first time it shows up. When you want the precise, engineer-facing reference instead,
read [README.md](README.md); for the shortest plain-language version, [FAQ.md](FAQ.md).

---

## 1. What is this thing, in one sentence?

**It's an automated test runner for "Agent Skills" — like `pytest`, but instead of
testing your functions, it tests whether an AI coding assistant does the right thing when
given a skill.**

That sentence has two unfamiliar pieces. Let's unpack them.

### What's an "Agent Skill"?

Modern AI coding tools — Claude Code, OpenAI's Codex CLI, GitHub Copilot, Google's
AntiGravity — can be extended with **skills**. A skill is just a folder with a
`SKILL.md` file (plus optional helper scripts and reference docs) that teaches the
assistant how to do a specific job: "when the user asks for a SlideRule data pipeline,
here's exactly how to structure it." Think of a skill as a **plugin made of
instructions** rather than code.

### Why do skills need a *test* runner?

Because a skill is instructions for an AI, and AIs are non-deterministic — the same skill
can produce good behavior or bad behavior, and it can behave differently on different
models and different tools. So the questions you actually want answered are:

- Did the skill make the assistant **do the right thing**? (Create the right file? Run
  the right command? Produce a correct answer?)
- Does it work **across tools** — the same skill on Claude Code *and* Codex *and*
  Copilot?
- Did adding the skill actually **help**, versus running without it? (A/B testing.)

Doing that by hand — running an agent, eyeballing the output, repeating for every
tool — is slow and unreliable. This harness automates the whole loop.

> **Why it exists at all:** Anthropic's own Agent Skills best-practices doc says there
> "is not currently a built-in way to run these evaluations." This is that missing
> runner.

---

## 2. The mental model

If you've used a unit-test framework, this maps cleanly onto ideas you already know:

| Unit testing (pytest) | This harness |
| --- | --- |
| A test function | An **eval** (a YAML file describing one test) |
| The function under test | An **AI agent running a skill** |
| `assert x == 5` | An **assertion** (`file_exists`, `ran_command`, …) |
| Running the test | Launching a real agent CLI in a clean sandbox |
| Test output: pass/fail | A **pass/fail matrix** + saved artifacts |
| A fixture | Seeded files / a starting workspace |

The one genuinely new idea is that the "thing under test" is a **live AI process** that
costs money and time to run, produces free-form output, and could wander off and touch
files it shouldn't. Most of the harness's cleverness is about taming that.

---

## 3. Vocabulary (read this once, refer back as needed)

| Term | Plain meaning |
| --- | --- |
| **Skill** | A `SKILL.md` folder that teaches an agent how to do a task. |
| **Agent CLI** | A command-line AI coding tool: `claude`, `codex`, `copilot`, `agy`. |
| **Runner** | The harness's name for one agent CLI it can drive. "Run this on the `claude` runner." |
| **Adapter** | The small piece of code that knows how to talk to *one* runner (see §6). |
| **Eval** | One test, written as YAML: a prompt + skills + how to grade it. Lives in a skill's `evals/` folder. |
| **Scenario** | Like an eval, but ad-hoc and self-contained (it names its own target runner/model). Run by file path. |
| **Cell** | One box in the results grid: *one eval × one model*. The unit of work. |
| **Matrix** | The grid of all cells in a run (evals down the side, models across the top). |
| **Workspace** | The throwaway directory the agent works in for one cell. |
| **Isolated / hermetic** | The run is sealed off so the agent sees only the skills you gave it — not other skills installed on your machine. |
| **Assertion** | A deterministic (no-AI) check: does this file exist? Was this command run? |
| **Rubric** | Plain-English behaviors graded by a second AI (the "judge"). |
| **LLM judge** | A separate AI run whose only job is to read the output and grade the rubric pass/fail. |
| **MCP** | "Model Context Protocol" — a way to give an agent extra tools (a server it can call). Mostly relevant to advanced evals. |
| **Artifacts** | Everything saved to disk after a run: raw output, normalized events, verdicts, the final workspace. |

---

## 4. Anatomy of a test (the YAML you write)

Here's a trimmed eval. Every field maps to a question about the test:

```yaml
name: scaffold-readme                    # what's this test called?
description: Agent should create a README with a title and Usage section.
skills: [scaffold-readme]                # which skill(s) to give the agent
prompt: |                                # what to ask the agent
  Scaffold a README for "Acme Widgets" using the {skill} skill.

# Deterministic checks — no AI involved, all must pass:
assertions:
  - {type: file_exists, path: README.md, matches: "(?s)^#\\s.+##\\s*Usage"}
  - {type: file_absent, path: package.json}

# Behaviors graded by the AI judge — one pass/fail verdict each:
rubric:
  - The README has a top-level title naming the project "Acme Widgets".
  - The README has a Usage section containing a fenced code block.
```

A few things worth knowing:

- **`{skill}` and `{skills}` are placeholders.** The harness fills them in per-tool —
  `/scaffold-readme` for Claude, `$scaffold-readme` for Codex — because each tool
  references skills differently. You write it once.
- **`assertions` are cheap and exact; `rubric` items are flexible but cost an AI call.**
  Use assertions for anything you can check mechanically (a file, a command, an exit
  code). Use the rubric for fuzzy "did it actually do the right thing" judgments.
- The full field list (seed files, fixtures, timeouts, tags, per-model reasoning effort,
  output schemas) is documented in [`../scenarios/example_full_schema.yaml`](../scenarios/example_full_schema.yaml),
  which annotates *every* field.

---

## 5. What happens when you run one cell

This is the heart of the harness. When you run an eval against one model, the runner does
this, in order:

```
  YAML eval ──┐
              ▼
   1. Parse & validate the spec            (spec.py)
   2. Build a clean, isolated workspace    (runner.py + isolation.py)
        • a temp dir with no link to this repo   ← blocks project-local skills
        • a masked HOME so global skills are hidden ← blocks global skills
        • copy ONLY the declared skills in
   3. Ask the adapter to build the command (adapters/<tool>.py → build_argv)
   4. Launch the real agent CLI as a subprocess, in that workspace   (exec.py)
        • capture stdout / stderr / exit code
   5. Adapter parses the tool's raw output into a common shape   (adapters/<tool>.py → parse)
        → a list of NormalizedEvent + a RunResult   (schema.py)
   6. Grade it:
        • run every deterministic assertion   (assertions.py)
        • if there's a rubric, run the LLM judge   (judge.py)
   7. Save everything to artifacts/<run_id>/…   and delete the sandbox
```

Some of those steps deserve a beginner-level note:

- **"Subprocess"** (step 4) just means the harness launches the agent CLI as a separate
  program — the same as typing `claude -p "…"` yourself — and reads back whatever it
  prints. The agent is a black box that the harness feeds a prompt and observes.
- **"Isolated workspace"** (step 2) is the safety wrapper. More on it in §7 — it's one of
  the two big ideas in this codebase.
- **"Common shape"** (step 5) is the *other* big idea. Read §6 next.

A full run repeats this for every cell in the matrix (every eval × every model), then
prints a grid and an exit code your CI can read (`0` = all passed, `1` = something
failed, etc.).

---

## 6. Big idea #1 — the adapter pattern (one shape, many tools)

Every agent CLI speaks a **different dialect**. Ask each one to run a prompt and emit
structured output, and you get wildly different formats:

- **Claude Code** streams JSON lines: `system`, `assistant`, `user`, `result` events,
  with tool calls buried in `tool_use` blocks.
- **Codex** streams `item.started` / `item.completed` events with types like
  `command_execution` and `file_change`.
- **AntiGravity** prints a *single* JSON blob, and its tool-by-tool trace isn't even in
  stdout — it's written to a log file on disk that you have to find by an id.

If the assertions and the judge had to understand all three, adding a fourth tool would
mean rewriting everything. So the harness draws a line:

```
  claude output ─┐
  codex output  ─┤→  [ each adapter's parse() ]  →  NormalizedEvent stream + RunResult
  agy output    ─┘                                  (one common shape — schema.py)
                                                          │
                          everything downstream (assertions, judge, reports)
                          only ever sees THIS common shape
```

An **adapter** (in [`agentskill_evals/adapters/`](agentskill_evals/adapters/)) is the
*only* tool-specific code in the whole harness. Each one answers three questions for its
CLI:

1. **`build_argv()`** — how do I invoke this tool for a given prompt? (Which flags?)
2. **`format_skill()`** — how does this tool reference a skill? (`/name` vs `$name`)
3. **`parse()`** — how do I turn this tool's raw output into the common shape?

The "common shape" is two dataclasses in [`schema.py`](agentskill_evals/schema.py):

- **`NormalizedEvent`** — one thing that happened, tagged with a kind
  (`TOOL_CALL`, `AGENT_MESSAGE`, `FILE_CHANGE`, `RESULT`, …) plus fields like
  `tool_name`, `command`, `path`.
- **`RunResult`** — the outcome of the whole run: the final answer text, every command
  the agent ran, cost, duration, exit code, and so on.

**Why this matters:** adding a new tool is *one small adapter*, not a rewrite. That's the
payoff of the normalization layer, and it's the single most important design decision in
the project.

---

## 7. Big idea #2 — isolation (the clean room)

Here's a subtle trap. You want to test "does skill X work?" But agent CLIs **auto-discover
skills** from places on your machine:

- **Global:** `~/.claude/skills/`, `~/.agents/skills/`, etc. in your home directory.
- **Project-local:** `.claude/skills/`, `.agents/skills/` at the top of the current git
  repo.

If this repo's skills are installed in those places (they often are, for development), an
agent could pick them up **even when your eval didn't ask for them** — and you'd get a
false "it worked!" that was really the ambient install. Your test would be lying.

So before each cell, the harness builds a **clean room** with two layers
([`isolation.py`](agentskill_evals/isolation.py) + [`runner.py`](agentskill_evals/runner.py)):

1. **A masked HOME.** The agent runs with its `HOME` pointed at a temporary directory
   that *mirrors* your real home — auth and config still work — but **hides this repo's
   skills** while keeping the tool's own built-in/vendor skills. (Under the hood this uses
   symlinks: everything is passed through by link *except* the skill folders, which are
   masked, and the declared skills, which are copied in fresh.)
2. **A relocated workspace.** The agent works in a temp directory that has **no path
   relationship to this repo at all** — so even an agent that walks up looking for a git
   root, or just lists a parent directory, finds nothing of ours to latch onto.

A few honest caveats the code is explicit about:

- **It's not an OS-level jail.** An agent that deliberately searches the *entire disk*
  (`find / -name …`) can still find the real checkout, because it genuinely exists
  somewhere. Closing that fully would need a container or VM per cell. The project accepts
  that residual risk and instead ships an **after-the-fact detector**
  (`leaked_skill_reads()` in [`workspace_view.py`](agentskill_evals/workspace_view.py))
  that catches a leak if it ever actually happens and marks that cell `isolated: false`
  rather than silently passing.
- You can turn isolation off with `--no-isolated` to test against your real setup on
  purpose.

There's a more advanced version of this called a **contained HOME**, used when an eval
has to hand the agent a real credential (e.g. an API token for an MCP tool). A plain
masked HOME uses pass-through symlinks, and a token written through one could escape to
the real home. A contained HOME instead *copies* only what's needed and has no outward
links, so nothing the agent writes can leak. If you're curious, that whole design is in
[`TODO_Contained_HOME.md`](TODO_Contained_HOME.md) — but you don't need it to understand
the basics.

---

## 8. How grading works (two very different judges)

A cell is graded two ways, and it's worth understanding the split:

**Deterministic assertions** ([`assertions.py`](agentskill_evals/assertions.py)) — no AI,
exact, cheap, repeatable. They inspect the workspace and the normalized events:

- Filesystem: `file_exists`, `file_absent`, `dir_exists`.
- Tool trace: `ran_command`, `used_tool`, `tool_count`.
- Skill interaction: `skill_triggered`, `skill_reference_read`, `skill_script_executed`, …
- Process/output: `exit_code`, `no_error`, `final_contains`, `output_matches_schema`.

**The LLM judge** ([`judge.py`](agentskill_evals/judge.py)) — a *second* AI run whose only
job is to read the agent's output and grade each plain-English `rubric` item pass/fail.
Use it for things you can't check mechanically ("the README's tone is appropriate").

Two things a beginner should know about the judge:

- **It costs money and can be skipped.** With `--no-judge`, rubric checks are *skipped*,
  not failed — the cell is graded on its deterministic assertions only.
- **It can be fooled.** Because the judge reads the agent's output verbatim, a sneaky
  agent could try to write "all rubric items pass" into its answer to steer the verdict
  (this is called *prompt injection*). The harness saves the judge's full prompt and
  reasoning as artifacts so a suspicious pass can be inspected. Rule of thumb: trust
  assertions first; treat a surprising judge pass with suspicion.

---

## 9. Why "matrix" and why the cost guardrails

The same skill can behave very differently on different **models**, so model is a
first-class axis. A single run is a grid:

```
                claude-haiku    claude-opus     (models across the top)
  eval-01          PASS            PASS
  eval-02          FAIL            PASS
  (evals down the side)
```

Every cell is a **full agent run plus a judge call**, and the axes multiply
(`evals × models`). That gets expensive fast, so the harness is deliberately
hard to fire off by accident:

- `run` **requires** you to name a runner (`--agent`) and a scope
  (`--skill`/`--evals`/`--config`). There's no "run everything" button.
- A scoped run uses only the **cheapest** model by default; the full set needs
  `--all-models`.
- There's a hard `--max-cells` ceiling (default 25) and a confirmation prompt for any
  multi-cell run.
- `--dry-run` shows you exactly what *would* run — including which skills the model will
  see — and spends nothing.

Models aren't hardcoded anywhere in the code; they live in one file, **`models.yaml`** at
the repo root, which is the single source of truth for "which models exist per runner."

---

## 10. How the harness tests *itself*

This is a nice bit of meta-engineering, and it's why the project trusts its own changes.

- **The self-test** ([`selftest.py`](agentskill_evals/selftest.py), run with
  `python3 -m agentskill_evals selftest`). It needs **no agent CLIs and no network** — it
  feeds each adapter a *captured sample* of that tool's real output and checks the
  `parse()` produces the right normalized shape, plus hundreds of logic checks
  ("arms") for isolation, redaction, cleanup, and more. It runs in a few seconds and is
  the project's main safety net. Each check is designed so a crash counts as a *failure*,
  never a silent skip.

- **Mutation testing** ([`tools/mutate_mcp.py`](tools/mutate_mcp.py)). This tests the
  *tests*. It deliberately breaks the production code in hundreds of small, specific ways
  ("what if this security check were deleted?") and confirms that a **named** check
  catches each break — that one, not just "something went red". A test that nothing can
  break is decorative; mutation testing proves each check actually earns its keep. Run it
  with `make mutation` (§11).

  It drives **three** suites, not one, and which suite runs is decided by which file the
  mutation edits: most of `agentskill_evals/` is proven by the self-test, the stdio
  fixtures by `verify_mcp_fixtures.py`, and the MCP proxy by `verify_mcp_proxy.py` — the
  proxy being production code that no self-test arm can reach, because proving it needs
  real pipes and a real child process. The totals are reported separately and deliberately
  never summed: only one of the three measures coverage of production code, and adding
  them would claim more than exists.

- **A shell script, and a verifier built for it** ([`tools/restricted_env.sh`](tools/restricted_env.sh),
  driven by [`tools/verify_restricted_env.py`](tools/verify_restricted_env.py), run with
  `make restricted`). Some of the harness's guarantees are about what happens when the operating
  system says *no* — when `ps` or a network socket is forbidden — and the honest answer there is
  a **third result state**: a check that could not run is neither a pass nor a failure, and must
  not be reported as one. Proving that machinery works means denying those capabilities on
  purpose, which is a job for a shell script. A shell script, though, is the one artifact the
  self-test cannot import and the mutation suite does not target — so it got a verifier of its
  own, which drives its *failure* paths (each construction step failing in turn, every
  terminating signal, the checks it makes on the suites it runs) and then **mutates the script
  on every run** to confirm those checks can still fail. Same idea as mutation testing, carried
  out inside the verifier because the main suite cannot reach the language.

- **A local pre-push git hook** (`.git/hooks/pre-push`). Since GitHub-hosted macOS
  runners aren't available for this org — and the self-test is verified on macOS, where
  its filesystem checks rely on macOS semantics — CI runs *on the developer's machine*
  instead. The hook runs the self-test + a byte-compile + a whitespace check before every
  `git push`, and blocks the push if anything fails (bypass with `git push --no-verify`).
  It's the local stand-in for a cloud CI service.

The layering is worth appreciating: **the harness tests agents; the self-test and the three
verifiers test the harness; and each of those is itself mutation-tested.** Each layer exists
because the one below it can be wrong in a way nothing else would notice. The one seam worth
knowing: `mutate_mcp.py` drives three of those suites, and the fourth — the restricted-environment
verifier — carries its own mutations inline, because its subject is a shell script rather than
Python. Same guarantee, different mechanism, and it is a boundary rather than an oversight.

> **Exact counts live in exactly one place** — the verification block in
> [`TODO_Contained_HOME.md`](TODO_Contained_HOME.md) §4, which lists every command and the
> number each one currently prints. They are deliberately not repeated here or anywhere
> else: a count that lives in two places drifts, and it drifts silently.

---

## 11. Running it yourself

```bash
# one-time setup (creates a local virtualenv and installs the CLI in editable mode)
cd harness && make dev && . .venv/bin/activate

# the self-test — no agent CLIs or API keys needed, runs in seconds
python3 -m agentskill_evals selftest

# lint: the pinned Ruff over the Python, the pinned ShellCheck over the shell, then a parse of
# each script under every shell installed. Both linters come from .venv, never from your PATH.
make lint

# the two wire-level verifiers (real pipes, real child processes) — seconds
make verify

# the restricted-environment script's failure paths, including its own mutations — about 90s
make restricted

# mutation-test the checks themselves — SLOW: it runs a whole suite per mutation, and there
# are hundreds. It prints its own elapsed time at the end, plus the slowest single mutation,
# so read those rather than this note. Run it whenever you add or change a check; it is not
# wired into the pre-push hook.
make mutation

# ...but prefer this. The suite is mostly WAITING (the proxy cases spend their time on
# shutdown grace periods rather than on a core), so running N mutations at once — each in its
# own copy of the tree — is close to an N-fold saving in wall clock. Pick N from your
# performance cores. Nothing that decides a pass/fail moved to make this faster.
python3 -u tools/mutate_mcp.py --jobs 8

# see what would run, and which skills the model would see — spends nothing
agentskill-evals run --agent claude --skill sliderule-region-picker --dry-run

# actually run one skill on the cheapest model, showing failures verbosely
agentskill-evals run --agent claude --skill sliderule-region-picker -v

# run a self-contained scenario file
agentskill-evals run --config scenarios/example_full_schema.yaml --dry-run
```

Results print as a grid and are saved under `artifacts/<run_id>/`: a machine-readable
`summary.json`, a rendered `summary.md`, and per-cell folders containing the raw output,
the normalized events, the assertion verdicts, and the final workspace — everything you'd
need to understand *why* a cell passed or failed.

---

## 12. A map of the codebase

Grouped by the job each file does. Start with the **bold** ones.

**Describe the test**
- [`spec.py`](agentskill_evals/spec.py) — parse & validate an eval/scenario (YAML/JSON) into a typed object.
- [`cli.py`](agentskill_evals/cli.py) — the `agentskill-evals` command-line surface (all the flags).

**Talk to each agent (the only tool-specific code)**
- **[`adapters/base.py`](agentskill_evals/adapters/base.py)** — the `Adapter` contract; read this to understand the pattern.
- `adapters/claude.py`, `codex.py`, `copilot.py`, `antigravity.py` — one per tool.

**Run the agent safely**
- **[`runner.py`](agentskill_evals/runner.py)** — the orchestrator: builds the matrix, runs each cell, handles cleanup. The biggest file; the center of gravity.
- [`exec.py`](agentskill_evals/exec.py) — launches the subprocess and builds its exact environment.
- [`isolation.py`](agentskill_evals/isolation.py) — the masked/contained HOME (the clean room).
- [`workspace_view.py`](agentskill_evals/workspace_view.py) — after-the-fact leak detection.
- [`mcp.py`](agentskill_evals/mcp.py) — MCP server config and secret redaction.

**Sit between the agent and an MCP server (the "C3" proxy)**

When an eval says a server may only expose *some* of its tools, the harness cannot ask the
CLI to enforce that — the four CLIs disagree about whether they can, and one cannot at all.
So it puts its own program in the middle: the CLI is told to start *this*, and this starts
the real server. It filters the tool list, refuses off-list calls at the wire, and writes an
audit log that says what it did. Split into three files by how testable each part is:

- [`mcp_proxy.py`](agentskill_evals/mcp_proxy.py) — the **decisions**, as pure functions:
  given a message, forward / filter / refuse. No I/O, so every rule is directly testable.
- [`mcp_audit.py`](agentskill_evals/mcp_audit.py) — the **record and the verdict**: what a
  proxy run must write down, and whether a given log describes a clean one. Written *before*
  the code that produces those records, so the writer had a contract to satisfy.
- [`mcp_proxy_io.py`](agentskill_evals/mcp_proxy_io.py) — the **program**: pipes, signals,
  shutdown, and a *guardian* process that owns the server so a killed proxy cannot leave a
  credential-bearing server running loose. This is the only part that needs real processes
  to test, which is why it has a verifier of its own rather than self-test arms.

This carries real traffic today, for **stdio** servers on `claude`: an eval that declares
`tools:` gets the proxy written into the CLI's MCP config in place of the server it named,
and the cell's verdict includes what the audit log says happened. A **remote** (`url:`)
server with `tools:` is still refused, and the refusal names the transport — the proxy talks
to a child process over pipes, so it has nothing to connect to. The bridge that would give it
one (stdio in, HTTP out) is designed and not built.

Worth knowing that this whole subsystem exists because of *one measurement*: `claude`'s
`--allowedTools` turns out not to gate MCP tools at all. Where a CLI does gate them, the
harness should get out of the way — `copilot`'s own per-server filter was measured working on
every transport it offers, so the plan there is to let copilot do it and write no proxy into
the picture at all.

**Understand the output**
- **[`schema.py`](agentskill_evals/schema.py)** — `NormalizedEvent` + `RunResult`, the common shape.

**Grade it**
- [`assertions.py`](agentskill_evals/assertions.py) — the deterministic checks.
- [`judge.py`](agentskill_evals/judge.py) — the LLM judge.

**Support**
- [`progress.py`](agentskill_evals/progress.py) (live progress), [`notices.py`](agentskill_evals/notices.py) (warnings that survive into artifacts), [`xattrs.py`](agentskill_evals/xattrs.py) (extended-attribute helpers used during scrubbing).

**Test the harness itself**
- [`selftest.py`](agentskill_evals/selftest.py) — the self-test.
- [`tools/mutate_mcp.py`](tools/mutate_mcp.py) — mutation testing.
- [`tools/probe_contained_home.py`](tools/probe_contained_home.py) — measures what a CLI *actually* needs from your real home, by driving the harness's own launch path against a progressively emptier HOME. This is how each adapter's credential surface was determined; it answers questions no amount of reading the source can.
- [`fixtures/probe_era_mcp_server.py`](fixtures/probe_era_mcp_server.py) — the same idea aimed at the wire: an MCP server that measures the *CLI on the other end* — which version of the protocol it speaks, and how it shuts a server down. This is how we learned the four CLIs are not all speaking the same protocol, which a reasonable person would have assumed.
- [`tools/verify_mcp_fixtures.py`](tools/verify_mcp_fixtures.py) — drives both stdio fixtures against a scripted client, because a measurement is only as good as the thing that took it, and a test double only as good as its resemblance to a real server.
- [`fixtures/http_mcp_server.py`](fixtures/http_mcp_server.py) — the same idea for *remote* servers: a small MCP server that speaks both HTTP flavours, runs offline, and writes down every request it received including the headers. That last part is what makes it evidence — "did the agent's credential actually reach the server?" is a question only the server can answer, and asking the agent is asking the thing under test.
- [`tools/probe_remote_mcp.py`](tools/probe_remote_mcp.py) and the three `tools/probe_copilot_*.py` — **opt-in** probes that need a real CLI installed, and all but one of them spend real API calls, so nothing routine runs them. They exist because a claim like "this CLI's tool allowlist really blocks tools" expires the next time that CLI updates, and a claim nobody can re-run expires silently — so each is a procedure you can run again rather than a paragraph someone wrote once. Their *decision logic* is exercised offline by `verify_mcp_fixtures.py`, because otherwise a fix lands in one copy and not the other.
- [`tools/restricted_env.sh`](tools/restricted_env.sh) and [`tools/verify_restricted_env.py`](tools/verify_restricted_env.py) — the script that reproduces a capability-denied environment, and the verifier that tests *its* failure paths. The script exists because "a denied capability is not a passing result" is a claim you can only check by denying one; the verifier exists because the script's own failure paths are the fail-open kind — a denial that quietly does not take produces an unrestricted run that reports green. It also mutates the script on every run and requires the named check to go red, which is mutation testing done locally for a file the main mutation suite cannot target.
- [`tools/verify_mcp_proxy.py`](tools/verify_mcp_proxy.py) — the third mutation suite: it runs the real proxy over real pipes, with a real child process, and checks what actually happened to that process afterwards. Some things cannot be established from inside the program under test — "the server it was fronting is really gone" is a claim about the operating system, and a program asserting it about itself is the one witness you cannot trust. Its awkward counterpart is [`fixtures/proxy_target_server.py`](fixtures/proxy_target_server.py), a deliberately badly-behaved MCP server that ignores signals, lingers, and spawns helpers that escape their process group.

---

## 13. Where to go next

- [README.md](README.md) — the complete, precise reference (install, every field, every flag, the artifact layout).
- [FAQ.md](FAQ.md) — plain-language answers to common questions.
- [DESIGN_MCP_Support.md](DESIGN_MCP_Support.md) — how giving agents extra tools (MCP) works.
- [TODO_Contained_HOME.md](TODO_Contained_HOME.md) / [TODO_Version_Provenance.md](TODO_Version_Provenance.md) — deep dives on two of the trickier subsystems.

If you only remember three things from this document:

1. **It's a test runner for AI agents** — write an eval, it runs a real agent in a clean
   room, then grades the result.
2. **The adapter pattern** lets one common shape serve many different tools, so adding a
   tool is small.
3. **Isolation** is what makes "test this skill by itself" actually true, and the harness
   is honest about the limits of that isolation.
