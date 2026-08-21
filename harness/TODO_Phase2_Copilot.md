# Phase 2 — copilot MCP injection

**Goal, decided 2026-08-17:** copilot reaches §8's motivating pattern — a **remote** MCP server with a
bearer token in `headers` and a per-server `tools:` allowlist that is really enforced — with no proxy
and no transport bridge. Scope is **stdio *and* remote**, not stdio first.

`harness/DESIGN_MCP_Support.md` is authoritative for every fact below; this file is the build order and
the decisions behind it. **§1 closed on 2026-08-17** and blocks nothing; a later option **D** may yet
supersede it without stopping any slice (§1). **What is blocked now is stated once, in §0** — this
sentence used to read "nothing here blocks Phase 2", which was true while §1 was the only open
decision and false the moment a second one was recorded, in the same edit that added §0 to say so
(found in review, 2026-08-20). A file that answers "is anything blocked?" in two places will answer
it differently.
Read §2 (copilot), §5.2, §5.3 and §8 before changing anything. Counts live only in
`TODO_Contained_HOME.md` §4 — do not restate them here.

---

## 0. STATUS — where the build is, and what is next

**Read this first; it is deliberately a short list of facts and a set of pointers.** Everything it would
otherwise restate — the PR table, the slice contents, the status vocabulary, the counts — lives
once, further down or in `TODO_Contained_HOME.md` §4. A status block that copies them is a second
place for them to be wrong.

1. **Slice 1 is done** (PRs #120, #121). All five measurement questions are answered against copilot
   1.0.80, each from the run's own stream. §3's slice 1 has the readings.
2. **No production code has been written for slices 2–4.** Nothing is in flight.
3. **The next change is PR 0**, which is not part of any slice — see §3's table for what it is and
   why it goes first.
4. **No decision is open.** The last one — what the serialized health field means when a server's
   status moves during a run — was settled on 2026-08-20 and refined in review through 2026-08-21:
   health aggregates **every** observation, only *unknown* dominates (by yielding `None`), a cell
   whose surface varied is `"mixed"` and **cannot be verified**, and the field carries a health class
   rather than a status spelling. §3's slice 2, under "Which status wins", has the rule, the
   per-adapter vocabulary, and the claude change it requires (**PR 0b**, which is a shared-reducer
   change and not a reporting-path edit). Slice 2's reducer is unblocked.

The build order, the packaging, and the reasoning behind both are §3. `DESIGN_MCP_Support.md` stays
authoritative for every fact either of them rests on.

---

## 0b. Why copilot, and why now

copilot is the only adapter whose `tools:` is a **hard filter on every transport it offers** — measured
on the wire at 1.0.79 (§2), the opposite of claude's answer to the same question (§6-C2, which is the
entire reason C3 exists). So the pattern this whole design was written for is reachable here through
injection alone. On claude the same pattern still needs §10.10's five bridge slices. Nothing retires
the bridge: it stays the only route on claude, and the only tool gating agy will ever have (§10.1).

The installed CLI **has since moved to 1.0.80** — copilot auto-updates, which is the exact expiry this
document's provenance rules exist for. The behavioural measurements in §2 were taken at **1.0.79** and
carry it from **each run's own stream**; slice 1's own readings carry **1.0.80** the same way. A claim
here is qualified by the build its run witnessed, never by whatever is installed today. It is not a blanket warrant over §2: one class
of fact here is attributable to no build at all, by construction, and reading §2 as uniformly "measured
at 1.0.79" is exactly what would send slice 3 building on a claim nobody made.

---

## 1. DECIDED — a declared server that does not work **warns**, as it does on claude

**Resolved 2026-08-17: option A.** copilot matches claude. The history is kept because the decision was
made twice and reversed once, and the reason it reversed is the useful part.

The first call was *"a declared server reporting `failed` fails the cell"*. That was made without §8's
line on claude's Phase 1, which shipped the **opposite** resolution:

> `_mcp_witness` now permits the *declared* set and only that … A declared server that does **not**
> appear **warns** rather than raises: nothing leaked, but the scenario ran without the surface it
> asked for. So does one that appears in any state other than `connected` … An unrecognised status
> warns too … An *undeclared* server still fails the run whatever its status claims.

Claude's warnings are **recorded as well as printed**: `warn()` echoes to the harness's stderr *and*
appends to `RunResult.warnings` ([notices.py:29](agentskill_evals/notices.py#L29)), and both health calls
take that default. An earlier revision here said "recorded, **not** printed", which inverts what the
module did — the change that made these durable **added** the record, it did not remove the echo. What
matters for §1 is that the echo is **ephemeral**: `execute()` archives the *child's* stderr, never the
harness's own, so only the durable half outlives the run.

That weakens the "false green" argument the fail decision rested on. But only so far, and the limits are
narrower than earlier revisions of this section claimed — both corrections below are load-bearing for §1
and were wrong here:

- The warning survives into **two durable locations — `report.md` and `summary.json`'s
  `cells[].warnings` — and no others.** The remaining per-cell JSONs do *not* carry it:
  `RunResult.to_dict()` ([schema.py:148](agentskill_evals/schema.py#L148)) omits `warnings`, so
  `result.json` lacks it, and `_write_cell_json` emits `assertions.json`, whose keys
  stop at `assertions`. There is no `cell.json` — the method name is a misnomer that
  [runner.py:2077](agentskill_evals/runner.py#L2077) and [selftest.py:12535](agentskill_evals/selftest.py#L12535)
  both repeat in prose.
  **`cells[].warnings` is machine-readable and per-cell** — that much is not the limitation. The
  limitation is that it is an **untyped free-text array**: finding a declared-server shortfall in it means
  substring-matching English, and nothing structural separates that entry from a version-drift warning or
  any other `warn()`. There is no *typed* field naming the shortfall, which is precisely what **D** adds.
- **The health axis does not report the shortfall.** It reports a *health class* (§3's "Which status
  wins" — `"connected"` / `"unhealthy"` / `"mixed"` / `None`; it reported the raw status until
  2026-08-21), and its verdicts compare
  cells to **each other, not to the declaration**: if the server fails to come up in every cell the sets
  agree and `mcp_server_set_verified` stays **true**, while `mcp_server_health_verified` goes false
  when health *differs* between cells — **or when any single cell's health varied within its own run**
  (`"mixed"`, §3; that second clause dates from 2026-08-21 and this line claimed "only … between
  cells" until then). The axes detect **drift, not shortfall**: a server dead in *every* cell is a
  uniform condition and stays verified, which is the whole reason **D** is needed for the shortfall.

So the only thing that compares what ran against what the scenario *declared* is the warning string —
durable in two places, typed in neither, and additionally echoed to a stderr nothing archives.

Four resolutions, three of them considered when this was decided and the fourth raised afterwards.
**B was never acceptable**, and is kept only to stay ruled out:

| | behaviour | verdict |
| --- | --- | --- |
| **A** | copilot **warns**, matching claude | **CHOSEN**, for now. Consistent immediately; a declared-but-dead server still lets the cell pass, carried by **the warning alone** — the health axis records a health class, not the shortfall |
| **B** | copilot **fails**, claude keeps warning | two runners answer "did my declared server work?" differently. A scenario green on claude and red on copilot for a reason that is neither's fault. **Rejected.** |
| **C** | **both fail** — apply the principle everywhere | stricter and still coherent, but it rewrites claude's shipped, reviewed behaviour and widens Phase 2 into Phase 1's code with its own arms and mutations. **Not taken.** Should C ever be revisited it belongs in its own PR, before slice 2, so claude's change is reviewed on claude's own terms. Its real blocker is structural: every raise out of `verify_post_run` is reported as *"MCP hermeticity was not confirmed"* ([exec.py:238](agentskill_evals/exec.py#L238)), so a shortfall would be published as a leak — opposite facts with opposite remedies on one error string. |
| **D** | a **degraded verdict** — 🟡 beside ✅/❌ | **RAISED AFTER THIS WAS DECIDED**, and it supersedes the A/C trade rather than sitting inside it: the shortfall gets a machine-readable field and a visible lane without conflating "the run was too empty" with "the run was too permissive". Planned separately in `TODO_Degraded_Verdict.md`; revisit §1 once its first slice lands. |

**Undeclared servers fail under every option.** That is the kill-switch and it was never in question.
So is an *unavailable* declared set: the witness fails closed there, because a rule that permits the
declared set must never become a way to switch the audit off.

### What A costs, stated plainly

A declared-but-dead server lets the cell pass. The information is not lost, but it is thinner than
"recorded in the artifacts" suggests: **two** durable locations, both free text, no typed field a
consumer could filter or aggregate on, and an axis that reports drift rather than shortfall (see above).
The operator also gets a stderr echo at the moment it happens, which helps whoever is watching and
nobody reading the run afterwards. Nothing *forces* anyone to look, and nothing lets a machine look
without pattern-matching prose. That is the whole of the "false green" objection, and it survives the
decision rather than being answered by it — which is why **D** exists.

**What answers it is not the witness — it is slice 3's acceptance.** The scenario the objection really
fears is *injection silently not working*: the harness writes no usable config, copilot reports the
declared server absent, the witness warns, the cell goes green. A witness cannot close that, because the
witness is downstream of the same broken step. A **live end-to-end case that asserts the bearer arrived
on every receipt and that both gating signs hold** does close it, and slice 3's acceptance now carries
exactly that (added in review, PR #118). A is defensible *because* that case exists; without it, C would
have been the safer call.

### What the decision does and does not unblock

It fixes slice 2's **policy** — every status class now has a verdict, and the change becomes a port of
claude's resolution rather than a new one. It did **not** unblock slice 2's **implementation**, which
needed the status string a healthy injected copilot server actually reports — **now measured at 1.0.80
(slice 1, question 2): `pending` → `connected`, with `failed` and `disabled` the other two words the
fleet's copilot probes have seen.** A says what to do with each class; slice 1 says which string is
which class, and slice 2 can now be written.

**One thing slice 2 must take from how that was measured, not just from what it says — and it is a
place where the obvious lesson is the wrong one.** `pending` is a *transient*: it precedes `connected`
in every healthy run, so a reader that takes the FIRST status a server carried calls a dead server
healthy. The slice 1 probe shipped with exactly that and a review caught it (PR #120).

**Do not port that fix into `_mcp_witness` as it stands.** The two are asking different questions of the
same rows. Phase 0's kill-switch asks *did any server ever come up?* — for which "any non-inert status,
at any point" is the right and fail-closed reading, and it is what the witness does today by
accumulating violations rather than returning the first. Making it read only the LAST status would let a
server that came up `connected` and was later reported `disabled` pass as never having run, which is a
leak the current code catches. Slice 2's new question — *was this DECLARED server healthy throughout?*
— is a different predicate over the same sequence, and it is **not** an end-state reading (this line
asked "did it end up healthy?" until 2026-08-21, which named the very reading the answer rejects): §3's
"Which status wins" settles it as an ordered aggregation over every observation, first
and last alike rejected as lookups on one element. (This paragraph said "the last status" until
2026-08-21; that was the right correction to *first*-wins and the wrong destination.) So the two axes
read the one sequence differently — any-time for the kill-switch, conjunction for health — which is
the point being made here: two predicates over one status sequence, not one predicate to be retuned. Per the repo rule the declared fact still **joins the existing predicate**
rather than arriving as a parallel flag — joining it does not mean overwriting how the existing one
reads its evidence.

One structural consequence, and it is the one to get right. Today copilot's question is binary — inert
or leaked, decided by `_INERT_MCP_STATUSES` alone. Under A it becomes two questions on two axes: *is
this server declared?* and *what is its health?* Per the repo rule, the declared-set fact **joins the
existing predicate** — the one function every caller already reads — rather than arriving as a parallel
flag one call site checks. A second boolean consulted by one caller is how the exit status ends up
disagreeing with the output.

---

## 2. What is already known — and how well

All from `DESIGN_MCP_Support.md` §2 unless noted. **Absence in a probe means *not exercised*, never *not
supported*.** The four classes below are not interchangeable, and which one a fact belongs to decides
whether slice 1 still has to measure it. Strongest first.

### (a) Measured on the wire, version-qualified from the run's own stream

The two gating probes import `_stream_cli_version` **from the adapter** and recover the build from the
same execution that produced the evidence. "At 1.0.79" is a property of that run, not of a preflight.

- **`tools:` is a hard filter on stdio, `http` and `sse`**, with a control arm on each: an off-list call
  never reaches the server, an on-list call does and its answer returns. Read from server-side receipts,
  never the model's account. The gated arm asserts **each sign** — the allowed tool must arrive, the
  off-list one must not — because a `tools:` that suppressed the server wholesale would otherwise look
  identical to a working filter (`SUPPRESSES_ALL` is the verdict for that case).
- The declared `Authorization: Bearer <sentinel>` reached the server on **every** request of both remote
  arms, value intact.
- **An absent `tools` key behaved as "no filter" on all three transports.** This is not an aside — it is
  what every ungated control arm *is*: `mcp_config(…, tools=None)` omits the key and the off-list tool
  arrives. See (b) for why the adapter should nonetheless write `["*"]`.
- **A stdio server with no `type` key started and served tools** — `probe_copilot_gating.py` omits `type`
  in both arms, and those arms are the source of the stdio half of the filter result above.

### (b) Measured *shape*, attributable to no build

`probe_copilot_config.py` points `COPILOT_HOME` at a throwaway dir, makes copilot write its own config
with `copilot mcp add`, and reads back what it chose. `copilot mcp add` emits **no in-band witness**, so
any version this probe reports comes from a *different execution* — the probe says so itself and reports
it UNVERIFIED: it "measures shape, not a build". Treat every fact here as **unversioned**.

- Config key spellings: `mcpServers`, `command`, `args`, `env`, `tools`, `url`, `headers` — across **two
  different adds** (five from a stdio add, `url`/`headers` from the remote ones).
- **copilot writes a `type` discriminator for itself** — `local` for stdio, `http` and `sse` for the
  remote pair. Undocumented; nothing in the harness knew about it.
- **copilot writes `tools: ["*"]` whenever no allowlist is given** — its own spelling of "everything".
- The bearer is stored in that config file **in plaintext** (the probe adds `--header "Authorization:
  Bearer PROBE_SENTINEL"` and reads the token back out), which is what §5.3's
  scratch-dir-outside-the-workspace rule already assumes of every CLI here.

**What (b) licenses, and what it does not.** It licenses *emitting copilot's canonical shape*: writing
`type`, and writing `["*"]` explicitly rather than omitting `tools`, matches what the binary produces for
itself, and depending on an undocumented default instead is a bet a later build can settle silently. It
does **not** license any claim about what omission *does*. Two such claims were previously asserted here
and both are contradicted by (a). The one genuinely open case was **remote `type` omission** — **slice 1
closed it, and moved the fact from (b) to (a)**: at 1.0.80, `http` connects without the key and **`sse`
does not**, copilot reporting the server `failed` against a with-`type` control that reported
`connected`. So the adapter writes `type` because on one transport it is *required*, not only because it
is canonical — the strongest form the reason can take, and one (b) could never have supplied.

### (c) Capability-survey claims — read from the CLI's own help, not exercised here

Real, and recorded as verified in the survey; but nothing in this repo has driven them.

- `--additional-mcp-config <json>` is documented as a JSON string **or `@file`**, repeatable, augmenting
  the user config for the session. **Only `@file` is exercised**, by every probe that injects one — the
  two gating probes and the events probe. Inline JSON, repeatability, and the merge semantics of
  "augments" are unexercised — and slice 3 leans on the last of those holding for the harness's own file.
- ~~`--secret-env-vars <names>` redacts those env values from output.~~ — **exercised and confirmed at
  1.0.80** (slice 1, question 4): the marker reached the control run's output and is absent under the
  flag, in an arm whose fixture receipts prove it made the same call carrying the same marker.
- The empty config shape is `{"mcpServers": {}}`; a bare `{}` fails validation with `mcpServers: Required`
  and kills the session before execution. (Load-bearing for Phase 0's mask, which already ships on it.)

### (d) Current harness code — a statement about today, not a measurement

- `_INERT_MCP_STATUSES = {"disabled", "not_configured"}` — [adapters/copilot.py:228](agentskill_evals/adapters/copilot.py#L228).
  Anything else counts as brought-up, `failed` included, "a spawned process being a spawned process".
  **PR 0** drops `not_configured` from it (§3); slice 2 then reads the set, and does not edit it.

### The constraint that is easy to miss

`build_argv` **hard-rejects `--additional-mcp-config` in `extra_args`** (§2, §8) — along with `--agent`,
`--plugin-dir`, the hidden `--config-dir`/`--prefer-version`, `--output-format` and `-C` in every
spelling. Phase 2 must emit that flag **from the adapter** while keeping the user-supplied rejection
exactly as strict. The reason for the rejection does not weaken when the harness is the one adding it:
a user-supplied copy would merge servers past the enumeration.

Likewise `verify_post_run` re-runs the whole enumeration after the child exits and fails the run **if
the state moved**. The harness's own injected config must be accounted for by that re-read rather than
tripping it.

---

## 3. Build order — four slices, five PRs, one arming

Each slice touches `agentskill_evals/`, so each pays the full gate in `TODO_Contained_HOME.md` §4:
selftest, mutation suite, Ruff.

**Packaging (decided 2026-08-20, in review).** Four slices across five pull requests — two of which
carry no slice at all, being shipped-code corrections this design turned up — and slice 3 splits at
the moment injection becomes reachable:

| PR | contents | `supports_mcp_injection` |
|---|---|---|
| **0** | drop `not_configured` from `_INERT_MCP_STATUSES` (§ slice 2's vocabulary) — no Phase 2 dependency in either direction | stays `False` |
| **0b** | claude adopts the health-class reduction (§ slice 2, "Which status wins"): the **shared reducer** gains the ordered observations its sticky status discards, the reporting reader publishes a class, `runner.py` stops verifying a `"mixed"` cell and reports it as **drift**, and the safety path is held still under a `failed → connected` regression arm — **an arm and an `M`-class mutation per moved value**, with the uniformly-`"unhealthy"` control that keeps this a drift axis; must precede any copilot witness so the field never has two meanings at once | stays `False` |
| 1 | slice 4's **parser** half, then slice **3a** — config writer, `--additional-mcp-config`, `--secret-env-vars`, `${VAR}` interpolation | stays `False` |
| 2 | slice **2** — the witness reducer, the two readers, the declared-set policy | stays `False` |
| 3 | slice **3b** — flip the flag, live acceptance — then slice 4's **`used_mcp_tool`** half | `True` |

**PR 0 is its own PR rather than PR 1's first commit** (decided in review, 2026-08-20). It is a
safety change to shipped code that can newly fail runs, and it depends on nothing in Phase 2 — so it
should not wait on a Phase 2 design discussion, and it should be bisectable on its own. The cost is
honest: a fourth §4 gate for a one-word change, rather than a gate hidden inside a row.

**The gate does not cover PR 0 unless PR 0 brings its own arm** (found in review, 2026-08-20). The
existing witness arms drive `disabled`, `failed` and an invented unknown
(`copilot.post_run_stream_evidence_catches_reverted_leak`, `selftest.py`) — and **never
`not_configured`**. So the suite is green both before and after the only line PR 0 changes, and
would stay green if someone put the word back. A fourth §4 gate that cannot see PR 0's sole
behavioural change is a gate in name. PR 0 therefore ships three things, not one:

1. **The arm.** An undeclared server reported `not_configured` must **fail the run**, alongside the
   existing `disabled` arm, which must keep **passing** in the same check. One direction alone
   cannot tell *the allowlist shrank by one word* from *everything now fails*, and `disabled` is
   already there as that control.
2. **The mutation.** An `M`-class entry that puts `"not_configured"` back into
   `_INERT_MCP_STATUSES`, killed by the arm above. This is the repo's standing requirement rather
   than a new one: a check is only known to work once the failure it exists for has been caused on
   purpose.
3. **The stale comment.** `selftest.py` says *"the allowlist is {disabled, not_configured}"* at the
   arms it is describing. It is a copy of the line PR 0 edits and goes wrong the moment PR 0 lands.

Each behavioural boundary is its own COMMIT inside those PRs — parser, writer, witness, arming — so
a reviewer can read them apart even where they ship together.

**PR 2 does not merge on its own.** Its declared-server policy is unreachable by any live run until
PR 3 flips the flag, so merging it alone would put a branch on `main` that nothing but synthetic
inputs has ever entered — §4's trap, and the reason the split exists in the first place is to avoid
exactly that. PRs 2 and 3 are reviewed as a stack and land together, PR 2 first in the stack, after
PR 3's live acceptance has been the thing that exercised it.

The arming is one commit for a reason: risk 2 below is an interaction that has never executed on any
build, and concentrating it makes it reviewable rather than diffuse.

**But "everything before the arming is inert" is false, and the split it justifies is the wrong
one** (found in review, 2026-08-20). The flag gates `mcp_servers:` at `validate_mcp_support`, so it
makes **injection policy** dormant — the config writer, `tool_filter_for`, the declared-set branch.
It does nothing for the **telemetry and reporting** work, which runs on every ordinary copilot cell
the moment it merges:

| pre-arming work | reachable before 3b? |
|---|---|
| config writer, `--additional-mcp-config`, `${VAR}`, `--secret-env-vars` (3a) | **no** — nothing can declare a server |
| slice 2's declared-set policy branch | **no** — same gate |
| slice 4's parser (PR 1) | **yes** — `parse()` runs for every cell |
| slice 2's reducer + reporting reader (PR 2) | **yes**, and it *replaces a published value* |

The reporting one is the sharp edge. `_consistency` uses the witness when it is not `None` and falls
back to `mcp_servers_seen(argv)` only when it is (`runner.py`). Copilot returns `None` today, so
every copilot cell is currently reported from argv — the **disable set**, with health `()`, "nothing
outstanding". The first PR that populates `mcp_servers_witnessed` switches every copilot matrix onto
the stream reading: the set becomes what copilot says it hosted (the built-in sentinel, `disabled`)
and health stops being `()`. That is the better reading and the reason the field exists, but it is a
**visible change in the published record of every copilot run**, and it lands two PRs before
anything is injected.

So PR 1 and PR 2 each need an **ordinary live run** — a copilot cell declaring no `mcp_servers:` at
all, run end to end. They are not the same test, and conflating them was the first version of this
paragraph:

- **PR 2 — the comparison, and it is real coverage.** `summary.json`'s `mcp_server_sets` /
  `mcp_server_states` / `*_unknown_cells` / `*_verified` diffed against what `main` produces for the
  same scenario. The expected diff is stated above; anything else is a finding. No offline arm
  substitutes, because what is under test is **which of two code paths a real run takes**, and the
  fallback is chosen by a `None` the offline arm would be supplying itself.
- **PR 1 — a REGRESSION SMOKE, and it is not parser coverage.** A cell with no declared servers makes
  no MCP tool call, and none of those `mcp_server_*` fields depends on a normalized tool name: **that
  run passes with the parser's MCP branch deleted** (found in review, 2026-08-20 — §4's rule, a check
  that only exercises the path where the bug cannot appear). What it establishes is the other
  direction, which is still worth establishing: `parse()` did not break the ordinary cell. Parser
  coverage in PR 1 is the **pinned-stream** arms — `tools/pinned/copilot-1.0.80-events.jsonl` — and
  the live assertion on a normalized MCP tool identity belongs to **PR 3's injected acceptance**,
  which is the first run in the whole plan that produces an MCP tool call at all.

Five gates, not three: **PR 0** and **PR 0b** above, then the three slice PRs (~14 min each,
`full-suite-gates-merge-not-commit`). Both of the numberless ones are corrections to shipped code
that the Phase 2 design turned up rather than Phase 2 work, and each pays a gate that can see its
own change — which for PR 0 had to be added on purpose (below), and for PR 0b is the arm over the
trajectories claude's values move on.

### Slice 1 — measure (probes only, no production code)

The shipped copilot probes read two different things, and §2's provenance split is the same distinction:
the **two gating probes** read **server-side receipts** — what copilot *sent* (§2(a)) — while
`probe_copilot_config.py` reads **the config copilot wrote for itself**, never a wire (§2(b)). Four of
the five unknowns are on a side neither reaches: what copilot *says it did*, which one copilot run can
answer together. The fifth is §2(b)'s own limit — a shape read from an execution that carries no version.

1. ~~**MCP tool-name format in copilot's own JSON events.**~~ — **ANSWERED at 1.0.80**
   (`tools/probe_copilot_events.py`): **`<server>-<tool>`**, observed as `cfgkeyzulu-echo`. A hyphen —
   not claude's `mcp__<server>__<tool>`, not agy's inferred `mcp_<server>_<tool>`. The **execution event
   and the model's request agree** — and agreement is *required* now, not inferred: one source saying
   one thing is one observation and one UNOBSERVED source, which the first version scored as agreement
   (review, PR #120). `ONE_SOURCE_ONLY` is a distinct non-answer from `AMBIGUOUS`, and both real lines
   are pinned, because a conclusion that says "both sources" cannot rest on evidence holding one.
   **And a better answer to the same question came with it:** `tool.execution_start.data` carries
   **`mcpServerName` and `mcpToolName` as their own fields**. Slice 4's `used_mcp_tool` should match
   those and never split the composite — a server or tool whose own name contains a hyphen breaks the
   split and cannot break the fields. The composite stays the fallback if a build stops emitting them,
   which is why the probe reports both.
2. ~~**What `session.mcp_servers_loaded` reports for a DECLARED server**~~ — **ANSWERED at 1.0.80**
   (same probe). It names **the config key** (`cfgkeyzulu`), *not* the server's advertised
   `serverInfo.name` (`advnamequebec`) — the two were deliberately different, which is the only reason
   this is an answer rather than a coin flip. A run using **both** spellings is `REPORTS_BOTH` and not
   an answer: the version that broke that tie by priority also silently dropped whichever statuses were
   carried under the losing name, and the status it dropped is the one this row exists to supply.
   **The status is its own term in the exit predicate**, which it was not for two rounds: the question
   asks for the spelling AND the status, and only the spelling was checked, so a witness naming our
   server with no `status` field at all exited ANSWERED having measured half of it. It also has to be
   the status of a server whose health was established *outside* copilot's account — the fixture's own
   receipts, showing it answered our tool with this run's marker. Reading whatever status appeared and
   calling it the healthy one assumes the answer: a server that failed to start reports `failed`, and
   nothing in the status itself says which case you are looking at. A healthy injected server goes **`pending` → `connected`**,
   with a later `session.mcp_server_status_changed` repeating `connected`.
   **The status vocabulary observed across all five probes is `pending`, `connected`, `failed`,
   `disabled`** — the vocabulary slice 2 classifies on (`_INERT_MCP_STATUSES` itself is PR 0's, §3).
   Note `pending` in particular: what was measured is a **leading** `pending → connected`, so a
   reader that takes the server's FIRST status calls every healthy server unhealthy. The probe had
   that bug and the review caught it (PR #120). "Read the last status instead" is what this line
   used to say, and it is not the rule slice 2 implements — see §3's "Which status wins", where
   first and last are both rejected as lookups on one element of a sequence.
3. ~~**Does `--disable-mcp-server` reach plugin-declared servers**, and under what naming~~ —
   **ANSWERED at 1.0.80** (`tools/probe_copilot_plugin_mcp.py`): **yes, by the bare server key**;
   plugin-qualified spellings do nothing. It corrected a claim in shipped code — the flag was never the
   limitation, **unenumerability** is — and it generated the work this row warned about, though not the
   work that was expected: the witness carries `source`/`sourcePlugin`/`pluginName`, which `_mcp_witness`
   discards, so a plugin-declared server is identifiable *after* a run and the harness throws that away.
4. ~~**`--secret-env-vars` actual behaviour**~~ — **ANSWERED at 1.0.80** (same probe): it **REDACTS**.
   A per-run marker rode back in the MCP tool's reply and reached the control run's output; under the
   flag it is absent from a run whose own fixture recorded **answering** that call with a reply that
   carried the marker. So §8's belt-and-braces holds in the case that needed it — the value, wherever
   the value landed, not merely the variable name where it is echoed.
   **The second witness is what makes that a reading**, and it took two review rounds to get right
   (PR #120). The control proves the value *can* travel, but `REDACTS` is a claim about the *other*
   arm, and an arm that crashed or never called the tool produces the identical silence — so the arm
   needs its own positive fact, authored by the fixture rather than by the CLI under test. The first
   attempt used the receipt the fixture already wrote, which turned out to be **the wrong row**: it is
   emitted before `_reject` and before any answer, deliberately, because a measurement of a *filter*
   needs what the client SENT and a refused request still arrived. A call rejected on protocol grounds
   leaves exactly that row, so redaction read off it is a claim about a reply that was never produced.
   The fixture now writes a `served` row past a successful flush, carrying whether that reply began
   with the marker; the filter readers keep the arrival row, and the comment there says why.
   **A third round then found the control weaker than the arm it was controlling for.** It was asked
   only whether the marker appeared *somewhere* in its output — and the marker is also in an env var and
   in the config file this probe writes, on a run with `--allow-all`. So the control now carries the
   same two facts as the secret arm: its fixture's receipts, and copilot's own `tool.execution_complete`
   correlated to our execution by `toolCallId` (the result event carries no tool name). A comparison is
   only a comparison if both arms are established to have done the same thing.
   **A fourth round found the same imbalance one witness later**: the receipts end at the wire. They
   prove the reply went **out** carrying the value; only copilot's result event says anything came
   **back**, and that witness was still being asked of the control alone. A secret arm with an execution
   and no completion event — killed mid-call, or one whose result copilot never emitted — certified
   `REDACTS` from an output that was never produced. Each arm's result is now read as one of five
   `RESULT_*` states, because *came back without the value*, *arrived with nothing in it*, *cannot be
   tied to the marker-bearing reply* and *never arrived* are four different facts and a boolean had made
   them one `False`. The gate is membership in `INSPECTABLE_RESULTS`, so each state added since has
   needed no call site changed. The reading is also sharper for it: the redaction is localized to
   the tool result rather than to the output at large.
   **A fifth round found the third of those still merged into the second**: `RESULT_CLEAN` was assigned
   to any correlated completion, so a failed one carrying no result read as clean. `usable_result`
   requires `success: true` and a structurally usable payload, pinned to the shape 1.0.80 emits — and
   the leak test runs *before* it, since a completion carrying the value is a leak whether or not the
   call succeeded.
   **A ninth round found the same collapse one layer out, in the reasons rather than the verdicts.**
   Three sentences describe what became of a result, and two of them enumerated the states inline and
   ended in an `else` naming one specific observation — so a leak from an arm with no completion at
   all was told *our tool's result came back without it*, and so was a control whose results could
   not be attributed. Every verdict was right, which is why every arm was green: a diagnosis is a
   second assertion and needs driving of its own. `RESULT_ACCOUNTS` now puts each state into words
   once, `result_account()` reads it, and §E21 holds its domain equal to the module's own `RESULT_*`
   vocabulary, read off the module rather than restated.
   **A tenth round found the classifier feeding that table the wrong state.** Attribution cardinality
   was read before *anything came back at all*, so a run with two starts and no completion answered
   `RESULT_UNATTRIBUTED` — a sentence saying results came back, about a stream containing none.
   Existence is now settled over the raw events first; an empty correlated view is absence only when
   every completion carries a usable id to be excluded by; and `RESULT_UNATTRIBUTED`'s account no
   longer names two of the four ways the join fails. §E21 gained **stream-to-state-to-sentence** arms,
   with the state read back from the stream, since a supplied state cannot show the classifier
   supplying the wrong one.
   **An eleventh round found the same gate one join further in.** The empty *correlated* view was
   still decided after cardinality, and borrowed its premise from it, so several identifiable
   executions beside a completion that was provably none of theirs read as ambiguous rather than
   empty. It now states both clauses itself, and §E21's invariant is an equivalence with a witness
   on each side rather than a one-way implication a classifier could satisfy by never producing the
   state.
5. **Remote `type` omission, as *behaviour* rather than shape.** Slice 3 writes `type` and an explicit
   `tools: ["*"]` because §2(b) says copilot writes them for itself — but (b) is unversioned, and the
   probe that produced it *cannot* be versioned: `copilot mcp add` emits no in-band witness, so no
   amount of care makes that execution name its own build. The fix is not a better config probe, it is
   **measuring the same question through a channel that does have a witness**: run a remote gating arm
   with `type` omitted, on the fixture, and read the result from the run's own stream. That converts an
   unversioned shape claim into a versioned behavioural one, and closes the only omission case (a) does
   not already answer — stdio-without-`type` starts, and absent-`tools` is unfiltered, both measured.
   Cheap: one more arm in `probe_copilot_remote_gating.py`, whose `mcp_config` already takes the shape
   as an argument.

   **ANSWERED at 1.0.80, and the answer is TRANSPORT-DEPENDENT** — which is the shape of result a
   one-transport arm would have got wrong, and the reason this probe runs both:

   | transport | `type` omitted | established by |
   |---|---|---|
   | `http` (Streamable) | **starts anyway** — copilot reported `connected`, and `echo` arrived at the server | the connection witness plus the fixture's receipts |
   | `sse` | **does not start** — copilot reported **`failed`**, where the paired with-`type` control reported `connected` | copilot's own status, against a control differing in one key |

   The status vocabulary is split by **what each word licenses**, not by "is it `connected`". Only
   `failed` has been measured to mean the server will not be coming up. `pending` is the transient
   *before* `connected` — a truncated arm ends there and so does a healthy one — and an unmeasured word
   a later build invents is in the same position, so both leave the question `OMISSION_UNMEASURED`
   rather than publishing a negative (review, PR #120). The "listed with the key, absent without it"
   branch needs the bare arm to have published a **readable** inventory as well: an event with the right
   type and unreadable contents says nothing about which servers copilot had, and the entry that could
   not be parsed is the one that might have been ours. An empty list is a real inventory. **And the
   negative is about a NAME, which was being read through the status**: a server listed with no `status`
   field read as `None` — the same answer as a stream that never mentioned it — and was published as
   *not listed* from the inventory that names it. Naming and status are separate readings now
   (`SERVER_NAMED` / `ABSENCE_UNREADABLE` / `ABSENCE_ESTABLISHED`), an unreadable event taints the
   negative instead of yielding to a readable one beside it, and it cannot un-name (review, PR #120).

   **So slice 3 must write `type`, and that is now a measurement rather than a preference.**
   **The arm needed rebuilding to say it** (review, PR #120). The first version read only the fixture's
   receipts, so "no tool call arrived" was the whole evidence for `NEVER_STARTS` — and a turn where the
   model simply never called the tool produces identical receipts. The probe starts that fixture itself,
   so "the server was listening" says nothing about whether copilot understood the entry. It now reads
   **copilot's own connection status**, decided by the MCP host before the model acts, against a
   **paired with-`type` control** that must show what success looks like on this machine, this transport
   and this turn — and when neither witness speaks, the verdict is `OMISSION_UNMEASURED`, which the exit
   status does not accept as a finding.

Per repo policy the probes' **classification lives in named functions**, driven offline on synthetic
rows in `verify_mcp_fixtures.py` (§E19, §E21, §E22), with `F*` mutations. A fleet-wide negative requires
every row answered; absence of a positive is not a negative result.

**Slice 1 is complete: all five questions are answered, every reading version-qualified to copilot
1.0.80 and taken from the run's own stream.** The two verbatim events the readings rest on are kept
under `tools/pinned/copilot-1.0.80-events.jsonl`, so the synthetic streams §E21 drives its classifiers
on are pinned to a shape a real build actually emitted — if copilot changes the events, §E21 reddens
rather than continuing to certify a reader of a stream that no longer exists.

**What the review of this slice was actually about, since it generalizes past copilot.** Every one of
the six findings was the same defect wearing a different coat: *an absence read as a result, from an
instrument whose own participation was never established*. A secret arm that returned nothing certified
redaction; a `type` arm that received no call certified rejection; a candidate spelling whose run
produced no stream certified that the spelling does not work; a status sequence read at its first
element certified a dead server healthy. Each was a check that could not fail on the case it was written
for — §4's rule — and the repair is the same in all four: **name the positive fact the reading requires,
and get it from somewhere the subject does not author.** Fixture receipts for the exchange, copilot's
own connection status for the config, a paired control for the transport, and — for the status
sequence — a reading that is not a single element at all (§3's conjunction; "the last status rather
than the first" is how this line read until 2026-08-21, and it names the fix to that specific probe
bug rather than the rule slice 2 implements). Slices 2–4 read the same event stream and will meet the same trap.

### Slice 2 — the hermeticity witness learns about declared servers

Mirror of claude's Phase 1 change (§8 line 282), adapted to copilot's event shape
(`session.mcp_servers_loaded` + `session.mcp_server_status_changed`, rather than claude's `system`/`init`).

- permit **exactly** the declared set, and only that
- **undeclared + ever non-inert ⇒ fail the run** — the kill-switch, and the qualification is not a
  softening. **"Undeclared + any status" would fail every hermetic run there is**: copilot always
  reports the built-in `github-mcp-server` as `disabled`, that entry is never declared by a
  scenario, and the witness contract *requires* it — a well-formed event that does not name it is
  refused (`_WITNESS_SENTINEL`, `adapters/copilot.py`). The shipped code already reads it this way
  — `note()` records only non-inert statuses and `verify_post_run` raises on that map — so this
  line was the plan disagreeing with the adapter, not a change of policy (found in review,
  2026-08-20).
- declared + healthy ⇒ allowed
- declared + `failed` / unrecognised / absent ⇒ **warn, not raise** (§1, decided — matching claude)

**One reducer, two readers.** Claude's split is the model — `_mcp_witness` may fail the run,
`_witnessed_servers` runs on the reporting path and returns `None` instead of raising (§8's rule:
malformed telemetry is an *unknown* where it is reported and a *failure* where the contract is
judged). Copilot needs more internal evidence than claude's four values, so both readers derive
from **one** reducer over the stream rather than each walking it:

1. the contract violation, if any, and whether the witness existed at all;
2. the servers **ever observed non-inert** — the hermeticity reduction;
3. **every** status observed for each server, **in order** — the health reduction reads them all,
   and the reading is settled under "Which status wins" below: an aggregation over all of them, not
   a lookup on the first or the last, and order-sensitive because one exclusion is positional;
4. the **raw spellings**, for diagnostics only — never the compared field, which carries a health
   class rather than a word;
5. validated plugin attribution, for diagnostics only.

(2) and (3) are different reductions of the same sequence and neither substitutes for the other. The
current `note()` only ever ADDS to `live`, so an inert status never clears a name recorded live
earlier — correct for (2), and unusable for (3), which needs every observation rather than a
survivor. `live[name]` today is the last **non-inert** status, which is neither the last status nor
the sequence.
`_fmt_live` also hands back `"name (status)"` strings, so there is no structured map for the
reporting side to build its pairs from — `(name, health_class)` since the decision below; the
reducer is what supplies both those and the ordered observations they are computed from.

**The status vocabulary, on both axes.** 1.0.64's bundle enumerates
`connected | failed | needs-auth | pending | disabled | not_configured`; slice 1 observed four of
those six on the wire. Those are different classes of evidence, and the difference decides the
table. The observations are §2(a) — behaviour read from a run, version-qualified by that run's own
stream. The enum is **neither** §2(a) nor §2(b): §2(b) is shape read from a command's OUTPUT, and
this is a **string literal read out of a bundle's source**. What a spelling in an enum establishes
is that the spelling exists. It does not establish what the runtime does when it emits it.

| status | evidence | safety (ever non-inert) | declared-server health |
|---|---|---|---|
| `connected` | observed 1.0.80 | non-inert | **known-healthy** |
| `pending` | observed 1.0.80 | non-inert | **excluded when leading**, unknown when it follows a health claim — see "Which status wins" |
| `failed` | observed 1.0.80 | non-inert | **known-unhealthy** |
| `disabled` | observed 1.0.80 | inert | **known-unhealthy** for a DECLARED server — inert on safety is not excluded on health |
| `needs-auth` | bundle enum only | **non-inert** | **unknown**, never healthy |
| `not_configured` | bundle enum only | **non-inert** | **unknown**, never healthy |
| anything else | — | **non-inert** (fail closed) | **unknown**, never healthy |

**`not_configured` comes OUT of `_INERT_MCP_STATUSES`** (decided in review, 2026-08-20; an earlier
revision of this table kept it and was wrong). `_INERT_MCP_STATUSES` is a **fail-open allowlist**:
membership means *this server never started*, which is what excuses an undeclared name from the
kill-switch. Admitting a word to it on the strength of the spelling existing is precisely the rule
this file states two rows down — an unmeasured status is non-inert — applied everywhere except to
the one word already sitting inside the allowlist. `needs-auth` is the control that shows it: same
evidence, same enum, and it is non-inert today, which nobody argues with.

It goes back in when its runtime meaning is established, by a controlled run that produces the
status, or by reading the executable's control flow around where it is emitted — not by the
spelling appearing in a list.

**That one is a change to shipped code, not to this plan.** It is a live fail-open path today,
independent of every Phase 2 slice, so it lands as **PR 0** (§3) — its own change with its own §4
gate, rather than riding into PR 2 with the reducer or hiding as PR 1's first commit. The cost is
stated plainly: a hermetic run that reports a server `not_configured` will now fail closed where it
passed before, and the reason it fails is *nobody has established what that word means* — which is
the honest position and the direction this harness errs in everywhere else.

`needs-auth` gets the **same treatment on both axes**, and an earlier revision of this table did not
give it that (found in review, 2026-08-20): it read `needs-auth` as *unhealthy* while reading
`not_configured` as *unknown*, on identical evidence. The rule two paragraphs up says what an enum
entry establishes — that a spelling exists — and "a server that has not come up" is a reading of the
NAME, which is the one thing an unmeasured word does not license. Both are unknown until measured,
and unknown is already never-healthy, so nothing is lost by saying so accurately.

It also appears **nowhere else in this repo** — no probe, no plan, no table — while being non-inert
today, which is the safe direction by luck rather than by decision. It is written down here so both
axes have a row for it.

An unknown word keeps its **raw spelling** in diagnostics while contributing *unknown* health to
comparability. Losing the word to a bucket is how the next vocabulary change becomes unreadable.

**Two asymmetries with claude, both deliberate. Both are now settled; the second one took three attempts.**

*Inertness.* `DESIGN_MCP_Support.md` §8 line 282 states the rule as "an undeclared server fails the
run whatever its status claims", and that is accurate **for claude**, whose `live` list is every
server the run reports with no status filter at all (`adapters/claude.py`) — nothing is
unconditionally present there, so every reported name is a real question. Copilot cannot read it
that way: its witness contract *requires* an undeclared entry, the built-in sentinel, always
reported `disabled`. So "ever non-inert" is not copilot relaxing claude's rule, it is the same rule
under a stream that always contains one inert entry by construction.

*Which status wins* — **SETTLED 2026-08-20. Neither candidate won, and the first replacement for
them was wrong twice more.**

The two candidates were claude's **first non-`connected` sticks** and copilot's **final status**.
They share a defect: both are **lookups on one element of a sequence**, and `TODO_Contained_HOME.md`
§4 already states the general form — a verdict over several facts is a conjunction over every fact,
never a lookup on whichever arrived last. A server observed twice has a health that is a statement
about both observations. So the reduction is a conjunction.

But a conjunction over *what values*, and *which observations count* — the two questions the first
version of this rule got wrong (review, 2026-08-20):

**(1) The published field is a health CLASS, not a spelling.** The first version
published "the first observed status that was not `connected`", which serializes `needs-auth`,
`not_configured` or any future word **as a status** — and `runner.py` treats every non-`None` status
as **known health**. That converts an unknown into a known state, contradicting this section's own
table two paragraphs up, which gives those words *unknown, never healthy*. So:

> Classify each observation as **known-healthy**, **known-unhealthy**, **excluded** (not yet a health
> claim), or **unknown** (any word whose meaning has not been measured, a missing status, and a
> non-leading `pending`). Then, over the observations that are not excluded:
>
> | what was observed | published |
> |---|---|
> | any **unknown** | **`None`** |
> | nothing at all | **`None`** |
> | all known-healthy | **`"connected"`** |
> | all known-unhealthy | **`"unhealthy"`** |
> | both kinds | **`"mixed"`** |
>
> The compared field therefore carries a **health class from a closed set** — `"connected"` /
> `"unhealthy"` / `"mixed"` / `None` — and never a status spelling.

**Only `unknown` dominates, and it dominates by yielding `None`** (review, 2026-08-21). An earlier
version had known-unhealthy dominate everything, which broke the axis in both directions it is
supposed to protect:

- `failed` and `failed → connected` both published `"unhealthy"`, though only the second offered
  tools for part of the run. Two cells, one of each, compared **equal** and could certify
  `mcp_server_health_verified: true` over surfaces that differed. `"mixed"` exists for exactly that
  distinction — **within-run variation is a fact about the surface**, and this axis is about the
  surface.
- `failed` beside an unknown observation published a **known** `"unhealthy"`, so unreadable evidence
  disappeared behind a readable neighbour. Unknown cannot be outvoted: it means *this cell cannot be
  compared*, which is why it yields `None` and why `runner.py` already lets one unstated status make
  a whole cell's health unknown.

`"mixed"` rather than `"degraded"`: `TODO_Degraded_Verdict.md` uses *degraded* for a run-level
verdict, and one word for two axes is how the next reader gets it wrong.

**`"mixed"` is intrinsically NON-VERIFYING, and that is not a policy choice** (review, 2026-08-21).
Order within a mixed run is a diagnostic rather than a class — but the first version of that sentence
then let two cells, one `connected → failed` and one `failed → connected`, publish the same class and
so satisfy `mcp_health_verified = mcp_set_verified and len(health) == 1 and health_unknown == 0`
(`runner.py`). One ended the run with tools and the other without, and the matrix said **verified**.

The repair is not more trajectory in the field. It is that **the axis's question presupposes each
cell has *a* surface**: "did every cell run against the same tool surface?" is already false once one
cell ran against two. A `"mixed"` cell cannot be the same as another cell in the sense the question
requires — nor, for that matter, as itself. So health verification must fail whenever any cell is
`"mixed"`, however many cells agree on the class.

That makes it a **`runner.py` change, and it lands in the same commit as the reduction that can first
produce the class** (PR 0b, §3) — a window where `"mixed"` exists and still certifies is the defect
itself, not a step toward fixing it. `"mixed"` stays distinct from `None` in the report, because they
say different things: *known to have varied* against *could not be read*. Both refuse to verify; only
one of them is a measurement.

**Non-verifying is not "non-`connected`", and the difference is the whole axis** (review,
2026-08-21). A matrix whose every cell is uniformly `"unhealthy"` is **verified**: the cells agree,
which is the only question this axis asks. `mcp_server_set_verified` already behaves that way — a
server dead in every cell is a uniform condition — and health must match it, or the axis quietly
becomes a health-*success* check and the shortfall reporting **D** exists for is smuggled in through
a comparability field. Only `"mixed"` is excluded, because only `"mixed"` says a single cell held
two surfaces.

**And the top-level `comparability` for a mixed matrix is `"drift"`, not `"unverified"`** (review,
2026-08-21). Left alone, `runner.py`'s control flow would reach `"unverified"` — health verification
false, nothing else wrong — and that field's published contract reads *"nothing differed, but at
least one axis could not be read"*. A `"mixed"` cell was **read**, and what it establishes is that
conditions moved; the neighbouring contract line, *"cells demonstrably ran under different
conditions"*, is the one that fits, once its wording admits **within**-cell as well as
between-cell. So PR 0b appends a **drift entry naming the variation**, which routes through
`if drift:` on its own and leaves `"unverified"` meaning exactly what it means today. Broadening
`"unverified"` instead would merge *no evidence* with *evidence of variation* into one word, which is
the defect this file keeps removing from other fields.

**Raw spellings travel in the diagnostics, never in the compared field.** That is what keeps
"an unknown word keeps its raw spelling" and "unknown contributes unknown health" from being the
contradiction they were.

**A canonical class rather than a deterministic cause, chosen 2026-08-21 in review**, because
"a known-unhealthy observation dominates — publish it" did not say *what* — for `failed → disabled`
it could have meant the first word, the last word, or a class, and those produce different
`mcp_server_states` and different drift verdicts. (The *domination* in that sentence did not survive
either; see above.) Three reasons the class wins:

- **Publishing a cause reintroduces the spelling** into the field the paragraph above just cleared of
  spellings. One or the other, not both.
- **The axis asks about the SURFACE, not the cause.** `_consistency` exists to answer *did these
  cells run against the same tool surface?* A `failed` server and a `disabled` one both offer zero
  tools; two cells differing only in *why* are **equal** on this axis, deliberately, and the
  difference is one diagnostics keeps.
- **The cause is often not established anyway.** Claude's `failed` is emitted for an unreachable host
  *and* for an unparseable config (§9 probe #1), so a "cause" read off it would be a word, not a
  cause.

`"connected"` is kept as the healthy token rather than a matching `"healthy"`: it is both adapters'
measured healthy word **and** the value published today, so the healthy case — every ordinary run —
moves nothing. The asymmetry is deliberate and is the whole reason it is worth having.

**(2) `pending` is excluded POSITIONALLY, not globally.** What slice 1 measured is a **leading**
`pending → connected` startup sequence. It does not establish that a `pending` anywhere in a run can
be erased, and a global exclusion invents a **false green**: `connected → pending` would publish
`connected`, when nothing establishes that a run in that state still had its tool surface. That is
the `not_configured` defect again — a fail-open classification resting on a spelling rather than a
measurement — so it takes the same conservative answer:

> A `pending` observed **before any known health claim for that server** is excluded. A `pending`
> observed **after** one is **unknown**, and takes the unknown branch above.

The alternative is to measure the global semantics, which nothing needs yet; the positional rule
costs an ordered walk the reducer already performs.

**The per-adapter vocabulary, and why it is not the inert set.** Inertness is a **safety** property
(*this server never came up*, so it cannot be a leak); the health classes above are a **health**
property. Two sets over one vocabulary, and `pending` proves they are independent — **non-inert** on
safety (a spawned process is a spawned process, fail closed) and **excluded** on health when leading.
Merging them would delete `disabled` from the health verdict, where it is a real answer for a
declared server.

| adapter | known-healthy | known-unhealthy | excluded (leading only) | everything else |
|---|---|---|---|---|
| copilot | `connected` | `failed`, `disabled` — both measured at 1.0.80 | `pending` | unknown |
| claude | `connected` | **`failed`** — measured at 2.1.113 against an unreachable host, `DESIGN_MCP_Support.md` §9 probe #1 | **empty — no `pending` observed** | unknown |

Claude's `failed` row was **empty in the first version of this table, and that was wrong** (review,
2026-08-21): the measurement exists and is cited in the design doc. It also carries a caveat that
turns out to decide the next question — the same `failed` is produced by an unreachable host *and* by
an unparseable config, so it establishes **that the server did not come up** and not **why**.

Both sets are closed and measured, for the same reason the inert set is: assuming a meaning is
**fail-open on health**.

**"Claude: nothing" was wrong, and this is the third correction — but the second version of the
scope was wrong too** (review, 2026-08-21). Claude tests only `== "connected"` and publishes every
other word raw, so today an unrecognised status is published **as known health**; under the rule
above it publishes `None`. Claude's values do move. What they do **not** include is a missing
status: `_witnessed_servers` already serializes that as `None`, and `selftest.py` already asserts it
("a missing status stays None rather than being invented"). That is a **regression control** for
PR 0b, not a value it changes — and listing it as a changed path would have been a check that passes
on a build where nothing was done.

**PR 0b is not a reporting-path normalization, and calling it one hid a second change** (review,
2026-08-21). `_mcp_witness` keeps **one sticky status per name** — `statuses[name]` is overwritten
only while it still reads `connected` — so the observation sequence is discarded inside the reducer,
and `"mixed"` cannot be computed from what it returns. There are only two ways forward, and one of
them is a defect: parse the stream a second time in the reporting reader, which is the
second-reader divergence this plan spent a whole section avoiding for copilot; or **change the
shared reducer's return contract** to carry the ordered observations, which is what slice 2 already
specifies for copilot's reducer. PR 0b does the latter, and therefore turns claude into the same
shape slice 2 gives copilot: one reducer, two readers over its output.

**That reducer also feeds the safety path**, which is why the scope matters. `verify_post_run` reads
the same return value for its undeclared-server failure and for the declared-but-unhealthy warning
(`statuses.get(name) != "connected"`). Neither may change behaviour here, and the regression arm has
to be **the sequence the refactor endangers, not a generic one** (review, 2026-08-21): the property
claude's sticky reduction is protecting is that **`failed → connected` still warns**. A one-status
`failed` arm passes on a rewritten reader that took the *final* status — the exact mistake available
to someone replacing stickiness with a sequence — so the arm drives `failed → connected`, asserts the
warning's **content** and not merely that something was printed, and is paired with a mutation that
makes the safety reader take the final status.

The values that move on the reporting side:

| trajectory | today | after PR 0b | arm + `M` mutation |
|---|---|---|---|
| an unrecognised **non-empty** status | published raw, read as **known** health | **`None`** | **yes** |
| `failed` | `"failed"` | **`"unhealthy"`** | **yes** |
| `failed` **and** `connected` both observed | `"failed"` — sticky | **`"mixed"`** | **yes** |
| `failed` **and** an unknown status | `"failed"` — sticky, and **known** | **`None`** — unknown is not outvoted | **yes** |
| any cell `"mixed"` | *n/a — the class did not exist* | `mcp_server_health_verified` **false**, however many cells agree | **yes** |
| a uniformly `"mixed"` matrix | *n/a* | `comparability` = **`"drift"`**, with a drift entry naming the variation — never `"unverified"` | **yes** |
| a uniformly `"unhealthy"` matrix | verified — cells agree | **verified, unchanged** | no — **control, and the one that pins the axis** |
| a **missing** status | `None` | `None` — **unchanged** | no — regression control |
| `connected` throughout | `"connected"` | unchanged | no — regression control |
| the declared-but-unhealthy **warning**, driven `failed → connected` | fires, names the state | unchanged | no — regression control |

**Each moved value carries its own arm and its own `M`-class mutation**, and three of the rows above
exist only because an arm set can be complete over the *outputs* and still blind to the *rule*:

- standalone-unknown, standalone-`failed` and healthy-plus-unhealthy are all satisfied by an
  implementation in which unhealthy still overrides unknown — so `failed + unknown → None` needs its
  own arm, and a mutation **restoring that precedence**;
- `"mixed"` failing verification is invisible to any arm that checks the published class, so it is
  asserted on `mcp_server_health_verified` itself, with a mutation dropping the clause — **and beside
  the uniformly-`"unhealthy"` control**, without which an implementation that verifies only
  `"connected"` passes every other arm here while turning a drift axis into a health-success check.
  The paired mutation is the one that broadens the exclusion from `"mixed"` to **every non-connected
  class**, and the control is what kills it;
- the mixed matrix's `comparability` is asserted as **`"drift"`**, since `"unverified"` is what the
  control flow reaches on its own and no arm over `health_verified` alone can tell the two apart.

The existing `M24-server-status-discarded` covers none of them — it forces the status to
`"connected"`, which tests that a status is *preserved rather than overwritten*. The control rows are
named as controls in the same checks, so "everything returns `None` now" cannot pass for a fix.

It lands beside **PR 0** (§3) and **before** any copilot witness, so the field never has two meanings
at once.

**Plugin attribution is diagnostic only.** `mcp_servers_witnessed` stays a **two-element pair**
across adapters — `(name, health_class)` since the health decision above, `(name, status)` before it
— and the pair is what matters here: nothing joins it as a third element. Not for the reason an
earlier revision of this line gave, either. `_consistency` compares
cells **within one adapter** (a `Runner` holds exactly one, `runner.py`), so widening copilot's
tuple would never produce a bad cross-adapter comparison. What it would produce is one **serialized
field name carrying two shapes**: `witness_json` writes it into every run record and `summary.json`,
where the reader is a person or a script that has only the field name to go on. That is reason
enough to keep one shape, and it is a different reason. The `source` / `sourcePlugin` / `pluginName` fields the
witness currently discards are used to turn *"server X was loaded"* into *"server X was loaded,
declared by plugin P"*, under PR #121's rule: `source == "plugin"` **and** both name fields present
**and** equal. Two fields naming one plugin are two witnesses; either-one-matching is not
attribution. Contradictory attribution is reported **as contradictory** — never resolved by picking
a field.

Fails closed if the declared set is unavailable: this must never become a way to *disable* the audit.
Standing rule from §8 applies — *a fact learned from the run may warn; only the runtime contract may
pass*.

Also feeds the **two-axis** comparability reporting (§8): the set and the health are separate verdicts,
`None` ≠ `()`, and health is only compared within a uniform set.

### Slice 3 — injection, split at the arming

**3a builds it; 3b turns it on.** Everything in 3a is unreachable while `supports_mcp_injection` is
`False`, because `validate_mcp_support` refuses `mcp_servers:` for the adapter before anything else
runs — inert by construction, and reviewable without a live run. 3b is the flag, and it is where
the acceptance below is spent.

**3a — the writer** (flag stays `False`): `mcp_tool_filter = "native"`, `tool_filter_for` per
server, the config writer, the argv flag, `--secret-env-vars`, `${VAR}` interpolation.

**3b — the arming** (one commit): `supports_mcp_injection = True` (currently the base default
`False`), plus the three acceptance cases run live.

- config writer emitting **`type`**, `command`/`args`/`env` for stdio, `url`/`headers` for remote, and
  `tools` — never omitted to mean "everything", since copilot spells that `["*"]`
- `--additional-mcp-config @file`, the file written 0600 in the per-cell `ase-mcp-` scratch dir outside
  the archived workspace (§5.3), while `extra_args` keeps rejecting the same flag
- `--secret-env-vars` for declared env credentials
- `${VAR}` in `env`/`headers`/`url` only — **never** `command`/`args` (§8's third refusal: interpolating
  into the executable turns an env var into a way to choose what program runs)
- **Arms the overlay-build guard.** §4/§145: mask-dependent adapters are refused one line earlier today
  and that guard "arms itself the day copilot or agy gains injection". Needs its own arms rather than
  being discovered live.

**Acceptance.** The trap to avoid is accepting slice 3 on evidence that never runs slice 3's code. Every
shipped remote measurement **hand-writes the config dict and invokes copilot directly** — see
`probe_copilot_remote_gating.py`'s `mcp_config`, which builds `{"type", "url", "headers"}` by hand. So a
broken `${VAR}` interpolation, a mis-serialized `headers` mapping, or a secret handled wrongly is
invisible to all of it: the probes prove *copilot* honours a correct config, never that *the adapter
produces one*. NASA is anonymous, so it cannot cover the gap either — it is §8's pattern **minus the
token**, and the token is the half the design exists for.

Three cases, and the middle one is the load-bearing one:

1. **stdio, adapter path** — live against `fixtures/echo_mcp_server.py`.
2. **remote with a PER-RUN CREDENTIAL SENTINEL, adapter path** — live against
   `fixtures/http_mcp_server.py`, which already writes one receipt per request, driven **through the
   adapter/runner from a scenario**. Called a per-run credential sentinel and not a "real credential"
   deliberately: the server is ours, so the value is a token this run generates and exports, and no
   third-party authentication is implied to a reader. It exercises everything the design needs
   exercised — interpolation, exact header delivery, gating, redaction — while introducing no external
   secret and no external service. With:
   - the bearer supplied as `${VAR}` from the environment, never a literal in the scenario — this is the
     only thing that exercises interpolation at all;
   - a native `tools:` allowlist;
   - assert the **exact** `Authorization: Bearer <sentinel>` value on **every** receipt, matching the
     existing probe's `bearer_reached` rather than checking presence;
   - assert **both gating signs** — allowed tool arrives *and* answers, off-list tool does not — because
     one sign alone cannot tell a filter from `SUPPRESSES_ALL`;
   - assert `--secret-env-vars` **redacts the sentinel from the run's output**. Note these last two
     observe different places and so do not conflict: the receipt is what the *server* saw, the redaction
     is what the *harness* published. A test asserting only one of them passes on a build that leaks.
3. **NASA Earthdata** (`https://cmr.earthdata.nasa.gov/mcp/v1`, anonymous — the C3-4 endpoint) as the
   **third-party control**: a real server the harness does not own, proving the shape is not fixture-shaped.

### Slice 4 — parser and the portable assertion

Its two halves land in different PRs, because they depend on different things. The parser needs only
slice 1's measurement, so it goes in PR 1 and gives 3b's live run a correctly-named tool to assert
on. The assertion needs a run that actually uses an MCP tool, so it goes with the arming.

- **PR 1** — normalize copilot's MCP tool names using slice 1's measurement: match `mcpServerName`
  and `mcpToolName`, which `tool.execution_start.data` carries as their own fields, and never split
  the composite — a server or tool whose own name contains a hyphen breaks the split and cannot
  break the fields. The composite `<server>-<tool>` stays the fallback for a build that stops
  emitting them. **Its coverage in PR 1 is the pinned-stream arms, not PR 1's live run** — that run
  declares no servers, so it makes no MCP tool call and would pass with this branch deleted (§3).
  The live assertion on a normalized tool identity is PR 3's, below.
- **PR 3** — **`used_mcp_tool`**, blocked since Phase 1 on "a second injecting adapter"; copilot is
  that adapter. It must match the `tool_use` event and **not assume it is the first tool in the
  transcript** (§9: the model may route through a `ToolSearch` step first)

---

## 4. Risks

1. **Slice 1 can generate unscoped work** — a bad answer on plugin-declared servers is a Phase 0 gap.
2. **Arming `supports_mcp_injection`** wakes an interaction that has never executed (risk 3 above).
3. **Five mutation runs** (four slices in three PRs, plus PR 0 and PR 0b — §3). Run the suite once
   per PR at merge rather than per push (`full-suite-gates-merge-not-commit`).
4. **copilot's version is unreadable and unpinnable from outside** (§8) — `--no-auto-update` is
   load-bearing for *code selection*, not just update traffic. Any new probe must go through the same
   argv the run uses, or probe and run can execute different code.

---

## 5. Also updated as each slice lands

- `DESIGN_MCP_Support.md`: §2 (measurements), §8 Phase 2 (status), §9 probe #3's two open halves
- `TODO_Contained_HOME.md` §4: any count that moves
- issue #81: the adapter × capability and milestone tables
