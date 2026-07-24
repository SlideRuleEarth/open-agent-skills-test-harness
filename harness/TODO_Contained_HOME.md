# Contained HOME — handoff

Branch: `harness/contained-home-81`, off `main` at `5b9579e` (the #84 merge).
Umbrella issue: #81. Predecessors: #82 (Phase 0 hermeticity), #84 (provenance + Phase 1 MCP).

Read this instead of the #84 diff. Everything below is what you need to start; the reasoning
behind it is in `DESIGN_MCP_Support.md` §5.3 and `TODO_Version_Provenance.md`.

---

## 0. STATUS — claude is DONE (2026-07-23)

The open decision is settled and the claude adapter is implemented, mutation-tested, and
live-verified. What changed:

- **Decision: MATERIALIZE.** Contained mode is `isolation.build_isolated_home(
  contained_subpaths=...)` — `None` keeps the historical symlink overlay; a sequence
  (including empty) switches the wholesale symlink pass OFF and copies only what is named, so
  `home_write_escapes()` returns `[]` and `_refuse_uncontained_home` lifts **untouched**.
- **claude's surface is `[]` — nothing copied.** On macOS claude authenticates from the login
  **keychain**, not from HOME, so a contained HOME needs `CLAUDE_CODE_OAUTH_TOKEN` exported
  (the operator exports it like any `${VAR}`; the harness does **not** read the keychain).
  Verified live on 2.1.113: an empty contained HOME runs, calls its MCP tool, and still emits
  `claude_code_version` in its init event.
- **The materialize cost/downside for claude is therefore zero** — no credential duplication,
  no per-adapter live-run loop. That will NOT hold for the next adapters (see §1).
- **Both in-overlay pass-through sites also copy under containment** (vendor skills in
  `_build_skills_dir`, plugin packages in `_mask_plugin_registry_dir`) — the easy-to-miss part.
- **A contained subpath colliding with a mask is refused at build time** (`_insert_copy_leaf`):
  copying the real MCP config over the neutral `{}` would be contained but a hermeticity
  regression that looks like it worked.
- Verification: selftest **469 arms** (was 461: +9, −1 decorative), `mutate_mcp.py`
  **79/79** caught by the intended arm, live credential run contained with the token in no
  artifact and no real-home file. `mutate_mcp.py` gained a per-selftest `timeout` so a looping
  mutation can never again wedge the suite.

**Still open — the next adapters.** codex ($CODEX_HOME), copilot, antigravity each need their
contained surface mapped **empirically** (§5 step 4 / §6). Their auth may well live under HOME,
so their `contained_home_subpaths` will be non-empty and the credential-duplication /
`_purge`-cleanup story in §1 becomes real for them. The rest of this doc is the original
handoff, still accurate for that work.

---

## 1. The job

**Credential-bearing MCP runs are refused. Make them possible without lying about it.**

A scenario whose `mcp_servers:` interpolates a `${VAR}` is currently refused before the agent
launches. That is correct today and it blocks the headline feature of Phase 1.

Why it is refused: the isolated HOME is a symlink **mask**, not a sandbox. `_overlay()` passes
every unmasked real-HOME entry through as a symlink, so `$HOME/.cache/x` *is* `~/.cache/x`,
and writing to a passed-through file overwrites the real one. Once an MCP tool result can
hand the resolved token back to the model, the model can write it where this harness neither
deletes nor scrubs. Deleting the overlay afterwards proves nothing about where its symlinks
pointed, and there is no scrub available: the harness does not know which of the real home's
directories were written to and will not go searching a user's home.

So the refusal is not conservatism to be relaxed. **Lifting it requires actually containing
the writes.**

### The open decision (SETTLED 2026-07-23 → materialize; see §0. Kept for the reasoning.)

| | Materialize | Allowlist + verify |
|---|---|---|
| What | Build only the adapter's declared config surface. Real directories, files **copied**, no outward symlinks at all. | Keep declared auth files as symlinks; hash them before and after the run; fail the cell if content changed. |
| Guarantee | Prevention | Detection after the fact |
| Cost | Per-adapter empirical work: nobody knows what each CLI actually needs from HOME. Needs live runs. | Small; lands soon. |
| Downside | Duplicates the user's long-lived CLI credentials into a per-cell tempdir. | The token is already in the real file by the time you notice. |

Prior lean (mine, not decided): **materialize**, because this whole line of work has been
"answer about the world, not about the call", and detection-after-the-fact is the weaker
sibling of that. The credential-duplication concern is real but it is precisely what the
`_purge`/`_remove` machinery exists to clean up, at `0700`, verifiably.

Note the forcing constraint: **our own escape rule is "any symlink resolving outside the
overlay."** A contained HOME therefore contains *no* outward symlinks — including auth. Under
materialize, auth files must be copied, not linked. That is not an incidental detail; it is
the crux of the cost.

Failure mode of materialize is a CLI erroring because something it needed was not declared —
fails closed, which is right, but it is a slow live-run loop per adapter.

---

## 2. What already exists (do not rebuild)

| Thing | Where | What it does |
|---|---|---|
| `home_write_escapes(home)` | `agentskill_evals/isolation.py` | Every symlink in the overlay whose `realpath` falls outside it. **This is the lifting condition** — when it returns `[]`, the refusal stops firing on its own. Do not add special cases to it; make the HOME satisfy it. |
| `_refuse_uncontained_home(home, eval_name, refs)` | `agentskill_evals/runner.py` | The refusal. Also refuses when `home is None` (no overlay = real HOME). |
| `interpolated_refs(servers)` | `agentskill_evals/mcp.py` | Which declared fields carry a `${VAR}`. The exposure gate. **Never** use `bool(secrets)` for this — short values are excluded from redaction on purpose and are still credentials. |
| `build_isolated_home(...)` / `_overlay(...)` | `agentskill_evals/isolation.py` | The overlay builder you will be changing. `_overlay` step 1 is the wholesale symlink pass — the thing that creates every escape. |
| `_CellCleanup` / `_purge` / `_remove` | `agentskill_evals/runner.py` | Registration + verified outward-in removal of credential directories, with findings that survive a crash. If you copy credentials anywhere, register the directory here. |
| `_scrub_tree` and friends | `agentskill_evals/runner.py` | Archived-workspace scrub. Not in scope, but read `_scrub_file`/`_scrub_link` before writing any new filesystem traversal — they encode the object-kind inventory the hard way. |

Adapter contract fields that declare HOME surface (`adapters/base.py`):
`global_skills_subpaths`, `isolation_config_masks`, `plugin_registry_config_masks`,
`global_plugin_registry_subpaths`, `isolation_config_homes`.
Materialize will likely need a new one — "these subpaths must be materialized writable" — and
that is an adapter-contract change, so it needs a default that fails closed.

---

## 3. Constraints that must not regress

These are settled. They cost ten review rounds; do not relitigate them in code.

1. **A fact learned by executing a program the agent independently executes again may not
   CLEAR a security decision.** Version telemetry may *warn*; only the runtime contract may
   *pass* a run.
2. **The executing version is read from the run's own structural telemetry** — never a probe,
   never model-controlled text (assistant prose), never workspace-controlled text
   (`source: "project"` skill paths).
3. **Adapters fail closed rather than degrade silently.**
4. **Secrets:** `${VAR}` from the harness process env only; fail-fast at validation; never
   committed in YAML; scratch config `0600`, outside the workspace, deleted post-run; every
   interpolated secret scrubbed from all artifacts including recorded `argv`.
5. **The exec workspace is unconditionally detached** and moved into `artifacts/` afterwards.
   Not tied to the isolation flag — a cwd is a write capability.
6. **Deletion is answered by `os.path.lexists`, not by "did this raise."**
7. **A finding is forgotten only once it is on disk**, and acknowledged at the return, because
   `_failed_cell` *rewrites* `result.json` from a rebuilt result.

---

## 4. Verification protocol

Non-negotiable, in this order. `SELFTEST PASSED` alone is not evidence.

```sh
harness/.venv/bin/python -m agentskill_evals.cli selftest          # 461 at 5b9579e; 469 after claude
harness/.venv/bin/python -m compileall -q harness/agentskill_evals/
harness/.venv/bin/python -m pyflakes harness/agentskill_evals/*.py harness/agentskill_evals/adapters/*.py
python3 harness/tools/mutate_mcp.py                               # 71/71 at 5b9579e; 79/79 after claude
git diff --check
```

Pre-existing pyflakes noise, leave alone: unused `load_spec` in `cli.py:22`, unused `Optional`
in `adapters/__init__.py:9`, unused `os` in `adapters/codex.py:23`, and many "f-string is
missing placeholders" in `selftest.py`.

**Every new arm must be mutation-tested.** Add the mutation to `harness/tools/mutate_mcp.py`
in the same commit as the arm. An arm nothing can break is decorative, and this project has
caught its own decorative arms four separate times.

Things that have gone wrong in the *tests*, so you can skip learning them again:

- A FIFO fixture on the main thread wedged the whole suite under the mutation that makes the
  scrub read every non-directory. Use a **socket** — same `_give_up` branch, but `open()`
  fails `ENXIO` instead of blocking. The one arm that genuinely needs a FIFO joins a 20s
  thread for exactly this reason.
- An 8-space mutation anchor is a substring of the same call indented 12 spaces elsewhere in
  the file. It matched the wrong site and injected an `IndentationError`, which the runner
  reports as "failed, but NOT via" — not as a defect found. Pin anchors with a leading
  newline, and check the mutant still parses.
- A test's cleanup must not be able to reach further than what it created. A mutation that
  changed `exec_ws` made a fixture's `rmtree(dirname(cwd))` resolve to the **system temp
  dir** and it deleted its own working tree. Fixtures now verify the shape before touching.
- Selftest arms within one section share mutable setup, and a raise aborts the siblings.
  There is a `_try` helper and a per-section crash guard; use them.
- A test helper that walked a **real-home overlay** with `home_write_escapes` was harmless at
  `followlinks=False` but, under the M65 mutation that flips it to `followlinks=True`, walked
  the whole real home at 100% CPU and wedged the suite for tens of minutes. Never feed a
  real-home overlay to a walk a mutation can turn recursive; the fix gated that capture to the
  small contained-home fixtures only. `mutate_mcp.py` now also bounds each selftest with a
  `timeout` (reported as `TIMEOUT`, counted as *uncaught*), so a looping mutation is a finding
  rather than an infinite hang — but it is a backstop, not a licence to hang.

---

## 5. Suggested order

1. **Settle materialize vs allowlist.** Everything else depends on it.
2. Build the contained HOME for **one** adapter (claude — it is the only one with Phase 1 MCP
   delivery working end to end, verified live on 2.1.113). Prove `home_write_escapes()`
   returns `[]` for it and that the refusal lifts without touching the refusal.
3. Live-run `scenarios/mcp_echo_cred.yaml` (the credential variant of `mcp_echo_smoke.yaml` —
   `mcp_echo_smoke` itself interpolates no `${VAR}` and takes the plain overlay) and confirm
   the token is in no artifact and no real-home file. **[done for claude — §0]**
4. Only then generalize the adapter contract. **[the contract exists; the next adapter is the
   generalization test — its surface is almost certainly non-empty, unlike claude's.]**

**Do this next, and do it before more adapters:** the refusal lives in `_run_cell_body` and is
adapter-independent, so Phase 1b/2/3 credential runs are all gated behind this. Containment
also subsumes per-cell `$CODEX_HOME` materialization, which is already on the list as codex's
ABA fix and its route to `parallel_safe_config = True`.

## 6. Then, in order

- **Phase 1b codex** — `-c` mapping + canonical `mcp__server__tool` naming in its parser.
  Blocked on §9 probe #2 (whether TOML array/inline-table values survive `-c`). Pairs with
  `$CODEX_HOME` materialization, above.
- **Phase 2 copilot** — `--additional-mcp-config @file`, per-server `tools`, `--secret-env-vars`.
- **Phase 3 antigravity** — MCP injection.
- **C3 harness-owned filtering proxy** — required before any scenario points `tools:` at a
  server its author does not control, and required for agy tool gating regardless.

Smaller, unblocked:

- Report the **witnessed** MCP server set from the init event so MCP matrices can reach
  `verified`. Today `mcp_servers_seen()` returns `None` once `--mcp-config` is on argv, so
  matrices read `unverified`.
- Portable `used_mcp_tool` assertion (§7) once a second adapter lands.
- Refuse `isolated: false` combined with `mcp_servers:`.
- Sweep for other default-held invariants (`judge`, `max_cells`, `provision`).

Still open in `DESIGN_MCP_Support.md` §9: claude's `mcpServers` http/sse JSON shape; copilot's
MCP tool-name format and plugin-declared server reach; agy's transcript tool-name format and
`url` vs `serverUrl`.
