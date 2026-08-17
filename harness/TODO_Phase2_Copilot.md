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

The installed CLI is **1.0.79**. That is the build the *behavioural* measurements below were taken
against, and they carry it from **each run's own stream**. It is not a blanket warrant over §2: one class
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

Claude's warnings are **recorded, not printed** (`notices.py` → `RunResult.warnings`), which weakens the
"false green" argument the fail decision rested on. But only so far, and the limits are narrower than an
earlier revision of this section claimed — both corrections are load-bearing for §1 and were wrong here:

- The warning survives into **`report.md` and `summary.json`'s `cells[].warnings`. That is all.** The
  per-cell JSONs do *not* carry it: `RunResult.to_dict()` ([schema.py:148](agentskill_evals/schema.py#L148))
  omits `warnings`, so `result.json` lacks it, and `_write_cell_json` emits `assertions.json`, whose keys
  stop at `assertions`. There is no `cell.json` — the method name is a misnomer that
  [runner.py:2077](agentskill_evals/runner.py#L2077) and [selftest.py:12535](agentskill_evals/selftest.py#L12535)
  both repeat in prose.
- **The health axis does not report the shortfall.** It reports the *status*, and its verdicts compare
  cells to **each other, not to the declaration**: if the server fails to come up in every cell the sets
  agree and `mcp_server_set_verified` stays **true**, while `mcp_server_health_verified` goes false only
  when health *differs* between cells. The axes detect **drift, not shortfall**.

So the only artifact that compares what ran against what the scenario *declared* is the warning string,
in two places, neither of them a machine-readable per-cell field.

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
"recorded in the artifacts" suggests: two prose locations, no machine-readable per-cell field stating the
discrepancy, and an axis that reports drift rather than shortfall (see above). Nothing *forces* anyone to
look. That is the whole of the "false green" objection, and it survives the decision rather than being
answered by it — which is why **D** exists.

**What answers it is not the witness — it is slice 3's acceptance.** The scenario the objection really
fears is *injection silently not working*: the harness writes no usable config, copilot reports the
declared server absent, the witness warns, the cell goes green. A witness cannot close that, because the
witness is downstream of the same broken step. A **live end-to-end case that asserts the bearer arrived
on every receipt and that both gating signs hold** does close it, and slice 3's acceptance now carries
exactly that (added in review, PR #118). A is defensible *because* that case exists; without it, C would
have been the safer call.

### What the decision does and does not unblock

It fixes slice 2's **policy** — every status class now has a verdict, and the change becomes a port of
claude's resolution rather than a new one. It does **not** unblock slice 2's **implementation**: which
status string a healthy injected copilot server actually reports is still unmeasured (slice 1, question
2). A says what to do with each class; it does not say which string is which class.

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
and both are contradicted by (a). The one genuinely open case is **remote `type` omission**, which no
probe has exercised — slice 1 closes it.

### (c) Capability-survey claims — read from the CLI's own help, not exercised here

Real, and recorded as verified in the survey; but nothing in this repo has driven them.

- `--additional-mcp-config <json>` is documented as a JSON string **or `@file`**, repeatable, augmenting
  the user config for the session. **Only `@file` is exercised**, by all three probes. Inline JSON,
  repeatability, and the merge semantics of "augments" are unexercised — and slice 3 leans on the last of
  those holding for the harness's own file.
- `--secret-env-vars <names>` redacts those env values from output.
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

The three shipped copilot probes read **server-side receipts** — what copilot *sent*. Four of the five
unknowns are on the other side: what copilot *says it did*, which one copilot run can answer together.
The fifth is §2(b)'s: a shape read from an execution that carries no version.

1. **MCP tool-name format in copilot's own JSON events.** §9 probe #3's first unanswered half. Needed by
   the parser (slice 4) and by `used_mcp_tool`. agy's is *inferred* as `mcp_<server>_<tool>` from binary
   strings; claude's is `mcp__<server>__<tool>`; copilot's is unmeasured.
2. **What `session.mcp_servers_loaded` reports for a DECLARED server** — the exact name spelling and the
   status value for a healthy injected server. **Slice 2 cannot be designed without this**: it decides
   which statuses are permitted, and guessing the healthy one is the same trap as building to an
   unmeasured config shape.
3. **Does `--disable-mcp-server` reach plugin-declared servers**, and under what naming — §9 probe #3's
   second unanswered half. A Phase 0 hermeticity question, not a Phase 2 blocker. *May generate work:*
   isolated runs already mask plugins, but a negative answer leaves a documented gap on non-isolated runs.
4. **`--secret-env-vars` actual behaviour** — §8 lists it as belt-and-braces and nothing has measured
   what it does to an MCP-bearing run.
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

Per repo policy the probes' **classification lives in named functions**, driven offline on synthetic
rows in `verify_mcp_fixtures.py` (§E), with `F*` mutations. A fleet-wide negative requires every row
answered; absence of a positive is not a negative result.

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
