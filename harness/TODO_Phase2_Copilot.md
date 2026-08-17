# Phase 2 — copilot MCP injection

**Goal, decided 2026-08-17:** copilot reaches §8's motivating pattern — a **remote** MCP server with a
bearer token in `headers` and a per-server `tools:` allowlist that is really enforced — with no proxy
and no transport bridge. Scope is **stdio *and* remote**, not stdio first.

`harness/DESIGN_MCP_Support.md` is authoritative for every fact below; this file is the build order and
the open decisions. Read §2 (copilot), §5.2, §5.3 and §8 before changing anything. Counts live only in
`TODO_Contained_HOME.md` §4 — do not restate them here.

---

## 0. Why copilot, and why now

copilot is the only adapter whose `tools:` is a **hard filter on every transport it offers** — measured
on the wire at 1.0.79 (§2), the opposite of claude's answer to the same question (§6-C2, which is the
entire reason C3 exists). So the pattern this whole design was written for is reachable here through
injection alone. On claude the same pattern still needs §10.10's five bridge slices. Nothing retires
the bridge: it stays the only route on claude, and the only tool gating agy will ever have (§10.1).

The installed CLI is **1.0.79**, which is exactly the build every measurement below was taken against.
Nothing needs re-probing before starting.

---

## 1. THE OPEN DECISION — a declared server that does not work

On 2026-08-17 the call was *"a declared server reporting `failed` fails the cell"*. That was made
without §8's line on claude's Phase 1, which shipped the **opposite** resolution:

> `_mcp_witness` now permits the *declared* set and only that … A declared server that does **not**
> appear **warns** rather than raises: nothing leaked, but the scenario ran without the surface it
> asked for. So does one that appears in any state other than `connected` … An unrecognised status
> warns too … An *undeclared* server still fails the run whatever its status claims.

Claude's warnings are **recorded, not printed** (`notices.py` → `RunResult.warnings` → `cell.json`,
`report.md`, `summary.json`), and the health axis separately reports `failed` per cell (§8's two-axis
table). So the existing behaviour is a *recorded* pass, not a silent one — which materially weakens the
"false green" argument the fail decision rested on.

Three coherent resolutions. **The second is not acceptable**, and is listed only to be ruled out:

| | behaviour | cost |
| --- | --- | --- |
| **A** | copilot **warns**, matching claude | consistent immediately; a declared-but-dead server still lets the cell pass, with the warning and the health axis carrying it |
| **B** | copilot **fails**, claude keeps warning | two runners answer "did my declared server work?" differently. A scenario green on claude and red on copilot for a reason that is neither's fault. **Reject.** |
| **C** | **both fail** — apply the principle everywhere | consistent and stricter, but changes claude's shipped, reviewed behaviour and widens Phase 2 into Phase 1's code with its own arms and mutations |

**Undeclared servers fail in every option.** That is the kill-switch and it is not in question.

Recommendation: **A**, unless the failure mode has actually been hit in practice. The warning is already
recorded in three artifacts and the health axis already publishes `failed` per cell, so the information
is not lost — and C means rewriting a decision that survived review on the adapter that has been running
longest. If C is chosen it should be its own PR, before slice 2, so claude's change is reviewed on
claude's own terms.

---

## 2. What is already measured (do not re-derive)

All from §2 unless noted. **Absence in a probe means *not exercised*, never *not supported*.**

- `--additional-mcp-config <json>` — a JSON string **or `@file`**, repeatable, augments the user config
  for the session.
- Config key spellings confirmed by making copilot write its own config and reading it back
  (`tools/probe_copilot_config.py`, 1.0.79): `mcpServers`, `command`, `args`, `env`, `tools`, `url`,
  `headers` — but across **two different adds** (five from a stdio add, `url`/`headers` from the remote
  ones).
- **`type` is a transport discriminator the adapter MUST write**: `local` for stdio, `http` and `sse`
  for the remote pair. Undocumented; nothing in the harness knew about it. Omitting it produces a server
  that silently never starts and *looks exactly like a server that started with nothing to say*.
- **`tools: ["*"]` is copilot's spelling of "everything"**, written whenever no allowlist is given. An
  **absent** `tools` key is therefore not the way to say "no filter".
- **`tools:` is a hard filter on stdio, `http` and `sse`** (1.0.79) with a control arm on each: an
  off-list call never reaches the server, an on-list call does and its answer returns. Read from
  server-side receipts, never the model's account. The gated arm asserts **each sign** — the allowed
  tool must arrive, the off-list one must not — because a `tools:` that suppressed the server wholesale
  would otherwise look identical to a working filter (`SUPPRESSES_ALL` is the verdict for that case).
- The declared `Authorization: Bearer <sentinel>` reached the server on **every** request of both arms,
  value intact.
- The bearer is stored in copilot's config file **in plaintext** — which is what §5.3's
  scratch-dir-outside-the-workspace rule already assumes of every CLI here.
- `--secret-env-vars <names>` redacts those env values from output (verified 1.0.64).
- The empty config shape is `{"mcpServers": {}}`; a bare `{}` fails validation with
  `mcpServers: Required` and kills the session before execution.
- `_INERT_MCP_STATUSES = {"disabled", "not_configured"}` — [adapters/copilot.py:228](agentskill_evals/adapters/copilot.py#L228).
  Anything else counts as brought-up, `failed` included, "a spawned process being a spawned process".

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

The three shipped copilot probes read **server-side receipts** — what copilot *sent*. Every remaining
unknown is on the other side: what copilot *says it did*. One copilot run can answer all four:

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

Per repo policy the probes' **classification lives in named functions**, driven offline on synthetic
rows in `verify_mcp_fixtures.py` (§E), with `F*` mutations. A fleet-wide negative requires every row
answered; absence of a positive is not a negative result.

### Slice 2 — the hermeticity witness learns about declared servers

Mirror of claude's Phase 1 change (§8 line 282), adapted to copilot's event shape
(`session.mcp_servers_loaded` + `session.mcp_server_status_changed`, rather than claude's `system`/`init`).

- permit **exactly** the declared set, and only that
- **undeclared + any status ⇒ fail the run** — unchanged, this is the kill-switch
- declared + healthy ⇒ allowed
- declared + `failed` / unrecognised / absent ⇒ **per §1's decision**

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

**Acceptance:** live end-to-end on stdio against `fixtures/echo_mcp_server.py`, **and live against NASA
Earthdata** (`https://cmr.earthdata.nasa.gov/mcp/v1`, anonymous — the C3-4 endpoint) for remote `url` +
native `tools:` gating. That is §8's pattern minus the token, on a real third-party server.

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
