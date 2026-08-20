# Phase 2 — copilot MCP injection

**Goal, decided 2026-08-17:** copilot reaches §8's motivating pattern — a **remote** MCP server with a
bearer token in `headers` and a per-server `tools:` allowlist that is really enforced — with no proxy
and no transport bridge. Scope is **stdio *and* remote**, not stdio first.

`harness/DESIGN_MCP_Support.md` is authoritative for every fact below; this file is the build order and
the decisions behind it. **Nothing here blocks Phase 2** — §1 was the last blocking decision and closed
on 2026-08-17, though a later option **D** may yet supersede it without stopping any slice (§1).
Read §2 (copilot), §5.2, §5.3 and §8 before changing anything. Counts live only in
`TODO_Contained_HOME.md` §4 — do not restate them here.

---

## 0. Why copilot, and why now

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
- **The health axis does not report the shortfall.** It reports the *status*, and its verdicts compare
  cells to **each other, not to the declaration**: if the server fails to come up in every cell the sets
  agree and `mcp_server_set_verified` stays **true**, while `mcp_server_health_verified` goes false only
  when health *differs* between cells. The axes detect **drift, not shortfall**.

So the only thing that compares what ran against what the scenario *declared* is the warning string —
durable in two places, typed in neither, and additionally echoed to a stderr nothing archives.

Four resolutions, three of them considered when this was decided and the fourth raised afterwards.
**B was never acceptable**, and is kept only to stay ruled out:

| | behaviour | verdict |
| --- | --- | --- |
| **A** | copilot **warns**, matching claude | **CHOSEN**, for now. Consistent immediately; a declared-but-dead server still lets the cell pass, carried by **the warning alone** — the health axis records the status, not the shortfall |
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
leak the current code catches. Slice 2's new question — *did this DECLARED server end up healthy?* — is
the one that needs the last status. So the declared-set axis gets an end-state reading and the
kill-switch axis keeps its any-time reading; they are two predicates over one status sequence, not one
predicate to be retuned. Per the repo rule the declared fact still **joins the existing predicate**
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
  This is the line slice 2 changes.

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

## 3. Build order — four slices

Each slice touches `agentskill_evals/`, so each pays the full gate in `TODO_Contained_HOME.md` §4:
selftest, mutation suite, Ruff. Four slices means four mutation runs.

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
   `disabled`** — which is what slice 2 splits `_INERT_MCP_STATUSES` on. Note `pending` in particular:
   it is a *transient* that appears before `connected`, so slice 2's witness must read the server's
   **last** status and not its first. The probe had that bug and the review caught it (PR #120).
3. **Does `--disable-mcp-server` reach plugin-declared servers**, and under what naming — §9 probe #3's
   second unanswered half. A Phase 0 hermeticity question, not a Phase 2 blocker. *May generate work:*
   isolated runs already mask plugins, but a negative answer leaves a documented gap on non-isolated runs.
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

**Four of slice 1's five questions are answered here** (the fifth, plugin-declared servers, is a
separate change), every reading version-qualified to copilot
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
own connection status for the config, a paired control for the transport, the last status rather than
the first. Slices 2–4 read the same event stream and will meet the same trap.

### Slice 2 — the hermeticity witness learns about declared servers

Mirror of claude's Phase 1 change (§8 line 282), adapted to copilot's event shape
(`session.mcp_servers_loaded` + `session.mcp_server_status_changed`, rather than claude's `system`/`init`).

- permit **exactly** the declared set, and only that
- **undeclared + any status ⇒ fail the run** — unchanged, this is the kill-switch
- declared + healthy ⇒ allowed
- declared + `failed` / unrecognised / absent ⇒ **warn, not raise** (§1, decided — matching claude)

Fails closed if the declared set is unavailable: this must never become a way to *disable* the audit.
Standing rule from §8 applies — *a fact learned from the run may warn; only the runtime contract may
pass*.

Also feeds the **two-axis** comparability reporting (§8): the set and the health are separate verdicts,
`None` ≠ `()`, and health is only compared within a uniform set.

### Slice 3 — injection

- `supports_mcp_injection = True` (currently the base default `False`), `mcp_tool_filter = "native"`,
  `tool_filter_for` per server
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
2. **remote with a real credential, adapter path** — live against `fixtures/http_mcp_server.py`, which
   already writes one receipt per request, driven **through the adapter/runner from a scenario**, with:
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

- normalize copilot's MCP tool names using slice 1's measurement
- **`used_mcp_tool`** — blocked since Phase 1 on "a second injecting adapter"; copilot is that adapter.
  It must match the `tool_use` event and **not assume it is the first tool in the transcript** (§9: the
  model may route through a `ToolSearch` step first)

---

## 4. Risks

1. **Slice 1 can generate unscoped work** — a bad answer on plugin-declared servers is a Phase 0 gap.
2. **Arming `supports_mcp_injection`** wakes an interaction that has never executed (risk 3 above).
3. **Four mutation runs.** Consider running the suite once per slice at merge rather than per push
   (`full-suite-gates-merge-not-commit`).
4. **copilot's version is unreadable and unpinnable from outside** (§8) — `--no-auto-update` is
   load-bearing for *code selection*, not just update traffic. Any new probe must go through the same
   argv the run uses, or probe and run can execute different code.

---

## 5. Also updated as each slice lands

- `DESIGN_MCP_Support.md`: §2 (measurements), §8 Phase 2 (status), §9 probe #3's two open halves
- `TODO_Contained_HOME.md` §4: any count that moves
- issue #81: the adapter × capability and milestone tables
