# A degraded verdict — 🟡 beside ✅ and ❌

**Goal:** a cell that *ran and graded* but did not have the premises the scenario described stops
reporting as a clean pass. Today it reports green with an explanation in prose, and prose is not a
field anything reads.

This is a runner/reporting change, not adapter work. Every agent inherits it. It has **two customers
already in the tree** before any new detection code is written, which is the argument for building the
lane rather than special-casing MCP.

`harness/DESIGN_MCP_Support.md` is authoritative for the MCP half. Counts live only in
`TODO_Contained_HOME.md` §4 — do not restate them here.

---

## 0. Why, and why it is not an MCP feature

Two conditions in the shipped code mean *"this run graded fine, but it was not the experiment the
scenario described"*, and both resolve to **green**:

- **A declared MCP server that never connected.** claude warns
  ([claude.py:707](agentskill_evals/adapters/claude.py#L707), [:727](agentskill_evals/adapters/claude.py#L727));
  the warning reaches **two durable locations — `report.md` and `summary.json`'s `cells[].warnings` —
  and no others**, plus an echo to the harness's stderr that nothing archives (`execute()` captures the
  *child's*). `cells[].warnings` is machine-readable and per-cell; what it is not is **typed**, so
  finding this class of finding means substring-matching prose. If the scenario asserts on that surface
  the cell fails on the assertion — loudly, if confusingly. If it does not, the cell passes.
- **An isolation leak.** The run read undeclared skills from the real repo checkout, bypassing
  HOME-based isolation. `passed` is computed at [runner.py:718](agentskill_evals/runner.py#L718)
  **without consulting `isolation_leaks` at all**; the leak is recorded in `assertions.json` and
  `summary.json` and rendered as a blockquote in `report.md`
  ([runner.py:1462](agentskill_evals/runner.py#L1462)). It has never influenced a verdict.

The second one matters most for sequencing: it is already *detected*, already *recorded*, and simply
discarded at the verdict. So the lane can be proven end to end on a real condition without writing any
new failure detection — see slice 2.

---

## 1. THE OPEN DECISION — what CI does with a degraded cell

Yellow is unambiguous to a human reading the matrix under every option. **CI reads one integer**, so
the only real question is what that integer is. There is no arrangement that is simultaneously "obvious
in the matrix" and "green in CI" and "blocks the pipeline" — pick two.

| | exit status | consequence |
| --- | --- | --- |
| **D1** | `0`, with `--fail-on-degraded` to opt in | non-breaking. Existing pipelines stay green, which is the same false-green this change exists to end — until someone sets the flag |
| **D2** | a distinct **`4`** | matches the house precedent exactly (below). Any CI doing `rc != 0` treats it as a failure — which is the intent, but it *is* a behaviour change for existing pipelines |
| **D3** | `0`, no flag, reporting only | cheapest; purely a legibility change. Honest, but automation learns nothing |

**Recommendation: D2.** The precedent is already in the tree and its reasoning is verbatim the argument
for this whole document — [cli.py:710](agentskill_evals/cli.py#L710):

> Nothing was graded … exit 3 so CI can tell "no verdict" apart from a real failure (1) without falsely
> claiming success (0).

"Without falsely claiming success" is the requirement. Exit 3 earned its own code for a *rarer* case
than this one. D1 is the fallback if breaking `rc != 0` pipelines is unacceptable; note that D1 and D2
differ only in the default, so D1 now does not foreclose D2 later.

**Not in question:** a degraded cell stays in the denominator. See §5's first risk.

---

## 2. What already exists (grounded — do not re-derive)

The tri-state is half-built. Four precedents, all shipped:

- **A third exit code.** `0` pass / `1` fail / `3` no verdict — [cli.py:710](agentskill_evals/cli.py#L710).
- **Display lanes beyond ✅/❌.** [`_cell_mark`](agentskill_evals/runner.py#L1302) checks `run_result.error`
  *first* and renders `⚠️ <error>`, then `⚪ ungraded`, and only then ✅/❌. `_cell_text` mirrors it with
  `ERR` / `SKIP` / `PASS` / `FAIL`.
- **A tri-state at the progress layer.** `Progress.done(passed: bool | None)` renders `✓` / `✗` / `·`
  ([progress.py:88](agentskill_evals/progress.py#L88)), and the runner already passes `None` for ungraded
  cells ([runner.py:798](agentskill_evals/runner.py#L798)).
- **`ungraded`** — a per-cell boolean that is *not* pass and *not* fail
  ([runner.py:717](agentskill_evals/runner.py#L717)).

**`ungraded` is the shape to copy and the field to leave alone.** It removes the cell from the
denominator (`graded = [c for c in results if not c.ungraded]`,
[cli.py:660](agentskill_evals/cli.py#L660)) because a rubric-only eval with no judge produced no verdict
to count. A degraded cell *did* produce one. Reusing the field would silently shrink the denominator and
make the pass rate improve as things get worse.

---

## 3. The verdict sites — the actual work

`degraded` is genuinely a **second axis**, not a third value of `passed`: a cell can pass its assertions
while degraded, or fail them while degraded. That is the same two-axis shape the MCP set/health
reporting already uses, and it is why a separate field is right here.

The failure mode to avoid is the one the repo has a rule about: **a new boolean that only one call site
checks**, leaving every other caller publishing its old conclusion. So the deliverable of slice 1 is not
the field — it is a single `cell_verdict(cell)` function that every row below calls.

| site | today | needs |
| --- | --- | --- |
| [runner.py:1299](agentskill_evals/runner.py#L1299) `_cell_text` | `PASS`/`FAIL` | `DEGR` lane |
| [runner.py:1310](agentskill_evals/runner.py#L1310) `_cell_mark` | `✅`/`❌` | `🟡` lane |
| [runner.py:1336](agentskill_evals/runner.py#L1336), [:1378](agentskill_evals/runner.py#L1378) | summary.md tallies | degraded count beside pass rate |
| [runner.py:1210](agentskill_evals/runner.py#L1210) | `n_passed` | `n_degraded` beside it |
| [runner.py:1230](agentskill_evals/runner.py#L1230) | summary.json `cells[].passed` | `degraded` beside it |
| [runner.py:902](agentskill_evals/runner.py#L902) | assertions.json `passed` | `degraded` beside it |
| [runner.py:798](agentskill_evals/runner.py#L798) | `progress.done(passed=…)` | already tri-state; feed it |
| [cli.py:661](agentskill_evals/cli.py#L661) | terminal tally | `(n degraded)` |
| [cli.py:689](agentskill_evals/cli.py#L689) | `--verbose` failure list | degraded cells listed with their reason |
| [cli.py:701](agentskill_evals/cli.py#L701) | `--reports fail` selection | **include degraded**, or the yellow cell has no report to read |
| [cli.py:708](agentskill_evals/cli.py#L708) | `failed` → exit status | per §1 |

The last two rows are the ones easiest to miss and the most user-visible: a degraded cell whose report is
never rendered, or a matrix showing 🟡 while the process exits 0, are both the matrix and the exit status
disagreeing about the same run.

---

## 4. Build order

### Slice 1 — the lane, with no producers

`CellResult.degraded`, the `cell_verdict()` function, every row in §3 converted, exit status per §1.
**No condition sets the flag yet**, so behaviour is unchanged and the slice is provable on synthetic
`CellResult`s without running an agent.

The assertion that must be able to fail here: a test that constructs a degraded cell and checks *each*
render site — a check that only exercises `_cell_mark` passes while `summary.json` still says `passed:
true`. Include the exit status in the same arm as the matrix text, since their agreement is the property
under test, not two separate ones.

### Slice 2 — `isolation_leaks` becomes the first producer

No new detection: the leaks are already computed and discarded. One line at
[runner.py:718](agentskill_evals/runner.py#L718)'s verdict, and the blockquote at
[runner.py:1462](agentskill_evals/runner.py#L1462) keeps its detail.

**This is deliberately first.** It proves the lane on a real, already-shipped condition, and its negative
control is free — an isolated run with no leaks must stay ✅, which is the check that the lane is not
simply degrading everything.

### Slice 3 — the MCP shortfall becomes the second producer

claude's two `warn` calls keep warning **and** mark the cell degraded. The warning text is the detail;
`degraded` is the verdict. copilot inherits this when Phase 2's witness slice lands — at which point
`TODO_Phase2_Copilot.md` §1 is no longer a choice between a silent pass and a hard failure.

Note this does **not** need the exception-channel rework a hard failure would have required: nothing
raises, so `verify_post_run`'s "everything raised here means the run was not hermetic" contract is
untouched, and no accumulate-then-raise ordering problem appears.

### Slice 4 — documentation and schema

`DESIGN_MCP_Support.md` §8's warn-vs-raise resolution gains the third answer;
`TODO_Phase2_Copilot.md` §1 records that degraded supersedes its A/C framing; the summary.json shape is
documented wherever consumers are told what to read.

---

## 5. Risks

1. **The denominator.** Degraded cells stay in `graded`. The tell that this went wrong is a pass rate
   that *improves* when a server breaks.
2. **Verdict/exit-status divergence.** The single reason for `cell_verdict()`. Any site in §3 left
   reading `.passed` directly is a place the matrix and the exit code can disagree.
3. **Scope creep.** Every existing warning will look like a candidate for degraded. It is not:
   degraded means *the run's premises differed from the scenario's declaration*, not *something was
   worth mentioning*. Version-drift warnings, for instance, are about the harness's knowledge, not the
   run's premises, and stay warnings.
4. **`summary.json` consumers.** Adding fields is safe; changing what `passed` means is not. `passed`
   keeps its meaning exactly, and `degraded` sits beside it.

---

## 6. Interaction with Phase 2

`TODO_Phase2_Copilot.md` §1 chooses **A** (warn, matching claude) "for now", recording that A is
defensible *because* Phase 2 slice 3's live acceptance case catches broken injection. It also already
carries this document as row **D**, described there as superseding the A/C trade rather than sitting
inside it — so the cross-reference runs both ways and neither file should be edited on the assumption
that the other still frames it as a three-way choice.

What a degraded verdict changes: the shortfall gets a **typed** field — the distinction that matters,
since `cells[].warnings` is already machine-readable and merely untyped — and a visible lane, without
conflating "the run was too empty" with "the run was too permissive" on one error channel, which was C's
blocker. §1 should be revisited, not rewritten pre-emptively, once slice 1 here lands.
