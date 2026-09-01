# A degraded verdict — 🟡 beside ✅ and ❌

> **PARKED (2026-09-01) — designed, never built.** No code was written for any slice. There is
> no 🟡 lane: a cell whose declared MCP server never connected still reports green, with the
> explanation in `report.md` and `summary.json`'s `cells[].warnings` exactly as §0 describes.
> Parked alongside the rest of the MCP workstream (`DESIGN_MCP_Support.md`'s status block).
>
> The lane itself was never MCP-specific, and §0's argument for building it rather than
> special-casing MCP still stands if anyone picks it up.

**Goal:** a cell whose run did not have the premises the scenario described stops reporting as a clean
pass. Today it reports green with an explanation in prose, and prose is not a field anything reads.

Note the scope deliberately says *ran*, not *ran and graded*: degradation is a claim about the run's
**premises**, so an `ungraded` cell can be degraded too, and the two axes are orthogonal throughout
(§1).

The **lane** is runner/reporting infrastructure and every agent inherits it — but connecting a producer
to it is adapter work where the producer lives in an adapter, which is exactly slice 3 (claude's
emissions, and copilot's when Phase 2's witness slice lands). Slices 1 and 2 touch no adapter at all.
It has **two customers already in the tree** before any new detection code is written, which is the
argument for building the lane rather than special-casing MCP.

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

## 1. DECIDED — a degraded run exits `4`

**Resolved 2026-08-17: option D2.** Yellow is unambiguous to a human reading the matrix under every
option; **CI reads one integer**, so the only real question was what that integer is. There is no
arrangement that is simultaneously "obvious in the matrix", "green in CI" and "blocks the pipeline" —
the choice was which two.

| | exit status | verdict |
| --- | --- | --- |
| **D1** | `0`, with `--fail-on-degraded` to opt in | non-breaking, but existing pipelines stay green — the same false-green this change exists to end, until someone sets the flag. **Not taken**; it differs from D2 only in the default, so it remains the fallback if the behaviour change proves unacceptable |
| **D2** | a distinct **`4`** | **CHOSEN.** Matches the house precedent exactly. Any CI doing `rc != 0` treats it as a failure — which is the intent, and it *is* a behaviour change for existing pipelines |
| **D3** | `0`, no flag, reporting only | cheapest, purely a legibility change; automation learns nothing. **Rejected** |

The precedent is already in the tree and its reasoning is verbatim the argument for this whole document
— [cli.py:710](agentskill_evals/cli.py#L710):

> Nothing was graded … exit 3 so CI can tell "no verdict" apart from a real failure (1) without falsely
> claiming success (0).

"Without falsely claiming success" is the requirement, and **exit 3 earned its own code for a rarer case
than this one**.

**What D2 costs, stated plainly:** a pipeline that today treats any nonzero status as failure will start
failing on degraded runs. That is the intended effect rather than a side effect — a degraded run *is* a
run whose premises differed from what the scenario declared — but it is a change to observable behaviour
and must not be discovered by whoever owns the pipeline.

**The notice ships with slice 2, not slice 4.** Slice 1 has no producers, so `4` is unreachable and
nothing changes; **slice 2 is the first slice that can emit one**, which is the moment a green pipeline
can turn red. Documenting it in slice 4 would put the warning after the behaviour by two slices unless
they landed atomically, which nothing guarantees. So slice 2 carries the release note as a deliverable,
and does not land without it.

### Precedence — per cell and per run

Degraded is a **second axis** (§3), so `failed + degraded` is a real state and the two axes must be
ordered explicitly. Left unspecified, an implementation is free to render 🟡 over a genuine assertion
failure or return the degraded code where it owes a failure — hiding a red behind a yellow, which is
strictly worse than the false-green this document exists to end.

This ordering was written before §1 was settled and deliberately did not depend on it — only the
degraded-only code was ever in question. It is unchanged by the decision.

**Per cell — the first match wins, and it extends the chain `_cell_mark` already applies:**

| order | state | renders |
| --- | --- | --- |
| 1 | `run_result.error` | `⚠️ <error>` |
| 2 | `ungraded` | `⚪ ungraded` — **annotated as degraded when it is**, on the same principle as row 3 |
| 3 | **failed** (degraded or not) | `❌` — **annotated as degraded when it is**, never replaced by 🟡 |
| 4 | **degraded** and passed | `🟡` |
| 5 | passed | `✅` |

**Per run — the exit status is the most severe cell, not the last one computed:**

| condition | exit |
| --- | --- |
| any graded cell failed or errored | **`1`** |
| nothing was graded | **`3`** (unchanged) — `3` outranks `4`; see the cross-product note below |
| every graded cell passed, **any cell degraded — graded or not** | **`4`** (§1) |
| every graded cell passed, no cell degraded | **`0`** |

**Ordinary failure always wins visibly and always exits `1`.** Degradation annotates a failure, it never
outranks one: the reason a cell failed is the thing the reader came for, and a run that lost a real
failure into the yellow lane would have made the reporting worse than it was.

The precedence itself is what slice 1's arms must exercise — a `failed + degraded` cell asserted at every
render site *and* against the exit status, since "does 🟡 hide ❌" is exactly the question a check on
either one alone cannot answer.

**Not in question:** a **graded** degraded cell stays in the denominator. See §5's first risk — and the
cross-product note immediately below for why that qualifier is load-bearing.

### `ungraded` + `degraded` — reachable, and both axes are kept

This combination is **not** hypothetical and must not be left to an implementer to discover. `ungraded`
is computed at [runner.py:717](agentskill_evals/runner.py#L717) and `isolation_leaks` at
[runner.py:685](agentskill_evals/runner.py#L685), **independently** — so a rubric-only cell with no judge
can leak, and slice 2 will produce exactly that: `ungraded=True` with a degradation record.

**The invariant: the two axes are orthogonal and both are preserved.** An ungraded cell may be degraded;
nothing suppresses the record. Suppressing it would be the worse error, because an isolation leak is a
fact about what the run *did*, and it does not stop being true because no assertion graded it.

- **Rendering** — `⚪ ungraded`, annotated with the degradation, exactly as a failed cell is annotated
  rather than replaced. The finding is never invisible.
- **Denominator** — `ungraded` governs membership, and wins. §1's rule above is about **graded** cells:
  a cell that produced no verdict has nothing to count, whether or not it also degraded.
- **Exit status** — **`3` outranks `4`.** `4` is a claim about cells that *passed*, and a run that graded
  nothing cannot make it; `3` says the run yielded no verdict, which is the stronger and more honest
  statement about what the run is worth. The degradation still reaches the matrix and the artifacts, so
  nothing is lost — only the exit code, which can carry one fact, carries the more severe one.

**One sub-case the table states outright, because it is where an implementer would guess:** a run with
*some* graded cells that all passed, plus an ungraded cell that degraded, exits **`4`**, not `0`. The
degraded axis is a claim about the run's **premises**, not about grading, so a degradation anywhere in
the run counts — the cell being outside the denominator affects the pass *rate*, never whether the
finding happened.

That last point is the general rule for this table: the exit status reports the **most severe** thing
true of the run, and severity is ordered by how little the run established, not by how recently a fact
was learned.

---

## 2. What already exists (grounded — do not re-derive)

The tri-state is half-built. Four precedents, all shipped:

- **A third exit code.** `0` pass / `1` fail / `3` no verdict — [cli.py:710](agentskill_evals/cli.py#L710).
- **Display lanes beyond ✅/❌.** [`_cell_mark`](agentskill_evals/runner.py#L1302) checks `run_result.error`
  *first* and renders `⚠️ <error>`, then `⚪ ungraded`, and only then ✅/❌. `_cell_text` mirrors it with
  `ERR` / `SKIP` / `PASS` / `FAIL`.
- **A tri-state at the progress layer — precedent, but *not* spare capacity.** `Progress.done(passed:
  bool | None)` renders `✓` / `✗` / `·` ([progress.py:88](agentskill_evals/progress.py#L88)) and the
  runner passes `None` for ungraded cells ([runner.py:798](agentskill_evals/runner.py#L798)). **All three
  values are already taken** — `True` pass, `False` fail, `None` ungraded — so there is nothing free to
  spell degraded with. This one shows the shape is accepted here; it does not donate a slot, and slice 1
  must **extend the API** rather than overload `None` or `False`.
- **`ungraded`** — a per-cell boolean that is *not* pass and *not* fail
  ([runner.py:717](agentskill_evals/runner.py#L717)).

**`ungraded` is the shape to copy and the field to leave alone.** It removes the cell from the
denominator (`graded = [c for c in results if not c.ungraded]`,
[cli.py:660](agentskill_evals/cli.py#L660)) because a rubric-only eval with no judge produced no verdict
to count. A **graded** degraded cell did produce one, and that is the case this rule is about — an
ungraded cell may also be degraded (§1), and there `ungraded` wins the denominator question because
there is still no verdict to count. Reusing the field would silently shrink the denominator and make
the pass rate improve as things get worse.

---

## 3. The verdict sites — the actual work

`degraded` is genuinely a **second axis**, not a third value of `passed`: a cell can pass its assertions
while degraded, or fail them while degraded. That is the same two-axis shape the MCP set/health
reporting already uses, and it is why a separate field is right here.

The failure mode to avoid is the one the repo has a rule about: **a new boolean that only one call site
checks**, leaving every other caller publishing its old conclusion. So the deliverable of slice 1 is not
the field — it is a single `cell_verdict(cell)` function that every row below calls.

### The producer path, which does not exist yet

**A `degraded` boolean alone is not enough, and settling for one would make slice 3 substring-match its
own English.** There is no typed channel from an adapter to the verdict today. An adapter has exactly
two ways to tell the runner anything non-fatal:

- **raise** — `verify_post_run` is typed `-> None` ([base.py:691](agentskill_evals/adapters/base.py#L691)),
  so its only outward signal is an exception, which `execute()` turns into `rr.error`;
- **a warning string** — `notices.warn()` into a thread-local sink that `execute()` drains onto
  `rr.warnings` ([exec.py:234](agentskill_evals/exec.py#L234)).

`CellResult` is then constructed *afterwards*, in the runner. So "claude noticed a declared server never
connected" reaches the verdict only as prose, and a `degraded` flag would have to be inferred by matching
that prose — reproducing, one layer down, the exact untyped-string problem this document exists to fix.

**Changing `verify_post_run`'s return type does not solve it**, and this is the constraint that decides
the design: the two producers live in **different layers**. Slice 3's is adapter-level (inside
`verify_post_run`); slice 2's is **runner-level** — `isolation_leaks` is computed by the runner and never
passes through an adapter at all. A carrier reachable only from `verify_post_run` covers one and not the
other.

**So slice 1 defines a typed degradation record, and it is a data-model decision, not plumbing:**

- a `Degradation` carrying a **`kind`** (a stable slug — `mcp_server_unavailable`,
  `isolation_leak`) **and the human string**, so the warning text stays the detail and the kind is what a
  consumer filters or aggregates on;
- adapters emit it through the channel that already exists — a typed sibling of `notices.warn()`,
  collected by the same thread-local sink, so it **joins the existing path** rather than adding a parallel
  one, and keeps echoing to the operator exactly as now;
- the runner collects its own findings into the same list, which is what lets `isolation_leaks` be slice
  2's producer without an adapter round trip;
- `cell_verdict()` reads that one list. `degraded` is then a *derived* property — `bool(degradations)` —
  rather than a second thing that can disagree with it.

Getting this wrong is expensive later and cheap now, which is why it belongs in slice 1 even though
nothing produces a record until slice 2.

| site | today | needs |
| --- | --- | --- |
| [runner.py:1299](agentskill_evals/runner.py#L1299) `_cell_text` | `PASS`/`FAIL` | `DEGR` lane |
| [runner.py:1310](agentskill_evals/runner.py#L1310) `_cell_mark` | `✅`/`❌` | `🟡` lane |
| [runner.py:1336](agentskill_evals/runner.py#L1336), [:1378](agentskill_evals/runner.py#L1378) | summary.md tallies | degraded count beside pass rate |
| [runner.py:1210](agentskill_evals/runner.py#L1210) | `n_passed` | `n_degraded` beside it |
| [runner.py:1230](agentskill_evals/runner.py#L1230) | summary.json `cells[].passed` | **`degradations: [{kind, message}]`** *and* the derived `degraded` |
| [runner.py:902](agentskill_evals/runner.py#L902) | assertions.json `passed` | **`degradations: [{kind, message}]`** *and* the derived `degraded` |
| [runner.py:798](agentskill_evals/runner.py#L798) | `progress.done(passed=…)` | **extend the API** — its three values are already spent (§2); take the verdict type, not a fourth bool |
| [cli.py:661](agentskill_evals/cli.py#L661) | terminal tally | `(n degraded)` |
| [cli.py:689](agentskill_evals/cli.py#L689) | `--verbose` failure list | degraded cells listed with their reason |
| [cli.py:701](agentskill_evals/cli.py#L701) | `--reports fail` selection | **include degraded**, or the yellow cell has no report to read |
| [cli.py:708](agentskill_evals/cli.py#L708) | `failed` → exit status | the per-run table in §1 — `1` outranks `4` |

The last two rows are the ones easiest to miss and the most user-visible: a degraded cell whose report is
never rendered, or a matrix showing 🟡 while the process exits 0, are both the matrix and the exit status
disagreeing about the same run.

---

## 4. Build order

### Slice 1 — the lane, with no producers

Four deliverables, and the first is the one that is expensive to change later:

1. **the typed `Degradation` record and its collection path** (§3) — the data model, decided before
   anything produces one;
2. **`cell_verdict()`** and the per-cell/per-run precedence in §1;
3. **every row in §3 converted**, including the `Progress.done` extension its three spent values force;
4. **the exit status** — `4` for degraded-only, per §1.

**No condition emits a record yet**, so behaviour is unchanged and the whole slice is provable on
synthetic `CellResult`s without running an agent.

Assertions that must be able to fail here:

- a degraded cell checked at **every** render site *and* against the exit status in the same arm — a
  check that only exercises `_cell_mark` passes while `summary.json` still says `passed: true`, and
  their agreement is the property under test rather than two separate ones;
- a **`failed + degraded`** cell, asserting ❌ survives and 🟡 does not replace it, and that the run exits
  `1` rather than `4`. "Does yellow hide red" is unanswerable from either axis alone;
- an **`ungraded + degraded`** cell — reachable via slice 2, so not hypothetical — asserting the record
  survives on an ungraded cell, that the cell stays *out* of the denominator, and that the run exits
  `3` rather than `4`;
- a degradation record whose **`kind` is read without touching its message**, since the point of the
  type is that no consumer has to parse the prose;
- a record **round-tripped through `assertions.json` and `summary.json`** and read back by `kind` — the
  serialization is where a typed record would otherwise decay into the derived boolean and take the
  whole point of §3's producer path with it.

### Slice 2 — `isolation_leaks` becomes the first producer

No new detection: the leaks are already computed and discarded. The runner emits an `isolation_leak`
record beside its existing `CellResult.isolation_leaks`, and the blockquote at
[runner.py:1462](agentskill_evals/runner.py#L1462) keeps its detail.

It is also the slice that **proves the record is reachable from the runner layer**, which is the half of
§3's producer path an adapter-side carrier would have missed — and the slice that first produces an
`ungraded + degraded` cell in the wild, since a rubric-only cell with no judge can leak (§1).

**It carries the exit-`4` release note** (§1). This is the first slice at which a previously-green
pipeline can go red, so the notice ships here or the warning arrives after the behaviour.

**This is deliberately first.** It proves the lane on a real, already-shipped condition, and its negative
control is free — an isolated run with no leaks must stay ✅, which is the check that the lane is not
simply degrading everything.

### Slice 3 — the MCP shortfall becomes the second producer

claude's two `warn` calls become typed emissions carrying `kind="mcp_server_unavailable"` **and the same
message**, so the operator echo and both durable locations are unchanged and the verdict now has a type
to read. The warning text stays the detail; the `kind` is what the verdict and any consumer act on —
**at no point does anything match the English**, which is the requirement §3's producer path exists to
meet. copilot inherits this when Phase 2's witness slice lands, at which point `TODO_Phase2_Copilot.md`
§1 is no longer a choice between a silent pass and a hard failure.

Note this does **not** need the exception-channel rework a hard failure would have required: nothing
raises, so `verify_post_run`'s "everything raised here means the run was not hermetic" contract is
untouched, and no accumulate-then-raise ordering problem appears.

### Slice 4 — documentation and schema

`DESIGN_MCP_Support.md` §8's warn-vs-raise resolution gains the third answer;
`TODO_Phase2_Copilot.md` §1 records that degraded supersedes its A/C framing; the `summary.json` and
`assertions.json` shapes — including `degradations: [{kind, message}]` — are documented wherever
consumers are told what to read.

**The exit-`4` release note is not here**; it ships with slice 2, the first slice that can emit a
degradation (§1). What remains for this slice is the reference documentation, which can trail the
behaviour without anyone's pipeline breaking on it.

---

## 5. Risks

1. **The denominator.** **Graded** degraded cells stay in `graded`; an `ungraded` degraded cell does not,
   because `ungraded` governs membership (§1). The tell that this went wrong is a pass rate that
   *improves* when a server breaks.
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
