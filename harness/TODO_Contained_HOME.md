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

---

## 0b. STATUS — all four adapters are now mapped (2026-07-27)

The prediction in the paragraph this replaces was **wrong in the interesting direction**, and
the way it was wrong is the finding. It said the next adapters' auth "may well live under
HOME, so their `contained_home_subpaths` will be non-empty and the credential-duplication
story becomes real for them". Measured, on macOS:

| adapter | credential store | surface | duplication |
| --- | --- | --- | --- |
| claude 2.1.113 | login keychain | `[]` + `CLAUDE_CODE_OAUTH_TOKEN` | none |
| copilot 1.0.72 | login keychain | `[]` + `GH_TOKEN` (or `COPILOT_GITHUB_TOKEN`/`GITHUB_TOKEN`) | none |
| antigravity 1.1.7 | login keychain | **unmapped (`None`) — no route exists** | n/a |
| codex 0.140.0 | **file** (`~/.codex/auth.json`) | `[".codex/auth.json"]` | real |

**Three of four keep auth in the macOS login keychain, which a contained home structurally
cannot reach** — reaching it needs `~/Library/Keychains`, and that is an outward symlink,
which containment forbids by its own escape rule. Copying is not the fallback: that file is
every password on the machine. claude's note had already worked this out for claude; what is
new is that it generalizes, and that it makes the env-var token the *normal* answer rather
than a claude peculiarity. Materialize's headline cost — duplicating long-lived credentials —
turns out to apply to exactly **one** adapter.

**The method mattered more than any single answer.** Additive probing (guess a surface, see if
it works) confirmed nothing and produced two wrong guesses for copilot — `.copilot/config.json`
*looks* like the credential store, and the CLI's own error message points at `gh auth login`.
What worked is **subtractive**: start from the overlay, which is known to work, and mask
top-level HOME entries until auth breaks. `harness/tools/probe_contained_home.py --bisect`
does that in ~7 runs and lands on `~/Library`, then `~/Library/Keychains` confirms it. The
tool drives the harness's own launch path (`build_isolated_home` → `adapter.env` →
`adapter.build_argv` → `exec.run_captured`), so what it measures is what a cell would get.

**All of this was measured on macOS, and `contained_home_subpaths` is not platform-conditional
— but the gap that leaves is narrower than it first looks.** The harness runs on macOS *and*
Linux; only Windows is refused, and deliberately (`exec._unsupported_platform`: a Job Object
cannot be assigned until `CreateProcess` returns, so a grandchild started in that window — an
MCP server most of all — escapes it permanently).

The machinery is portable: `isolation.py`'s containment path contains no platform branch at
all. So is the recommended route — `[]` plus an environment token works anywhere, because the
token comes from the environment rather than from HOME, and the keychain never enters into it.
What does not port is the *negative* half of the finding: "the keychain is unreachable, so `[]`
is the only answer." On Linux there is probably a credential file under HOME that would let a
contained run work with no token exported, and nobody has looked; antigravity's `None` may be
wrong in its own favour there for the same reason. These declarations are therefore **correct
but incomplete** on Linux rather than false, and what is missing buys convenience, not safety.
Tracked in §6, along with the ordering constraint that matters more than the mapping itself:
the selftest is not Linux-clean, and a surface validated by a suite that does not pass is not
a measurement.

Three things worth carrying forward:

- **antigravity is a negative result, and it is recorded as one.** It has no env-var
  credential (its 1.1.7 binary contains no `GEMINI_API_KEY`/`GOOGLE_API_KEY` string) and its
  keychain is unreachable, so `contained_home_subpaths` stays `None` and the refusal keeps
  firing — which is the correct outcome, not a gap. Note the failure mode if anyone forces
  it: agy with no readable credential does not exit, it falls back to **interactive OAuth**
  and opens a browser at a Google sign-in page. A headless cell hangs there for 60s. The
  probe now suppresses browser launching by default for exactly this reason; the bisect that
  discovered it opened a sign-in tab on the operator's machine first.
- **codex is the one that pays.** It stores a file, and it reads no credential from the
  environment at all — an invalid `OPENAI_API_KEY` produces a response byte-identical to
  setting nothing, so the variable is never consulted in `exec` mode. The rotation hazard is
  therefore live: the child can rewrite its copy, and a rotated refresh token dies with the
  tempdir while the real store keeps the old one. Measured with `--rotation-check`: codex did
  **not** rewrite the copy, and the real `auth.json` was byte-identical after a full run.
  That is one run, whose token had last refreshed nine days earlier — it is not "codex never
  rotates", and it should be re-checked rather than cited.
- **codex cells could not run at all — found here, fixed here.** codex refuses to start
  outside a git repo or trusted project, every cell workspace is a detached tempdir, and
  nothing passed `--skip-git-repo-check`, so codex runs had been dying before reaching the
  model since at least 2026-07-17. It failed identically under the plain overlay, so
  containment did not cause it; mapping codex's surface is simply what made anyone look.
  The flag is now on both the cell and probe argv, and `cheapshot_harness_smoke` on
  gpt-5.4-mini passes **7/7** — the first codex cell this harness has completed.

  The flag rather than `git init`, deliberately: both clear the gate (verified 0.140.0), but
  codex resolves a *trusted project's* `.codex/config.toml` from the git root above cwd, and
  that file contributes MCP servers — so creating a repo in the workspace would open a config
  channel inside the one directory the agent can write to, after `_mcp_disable_args` has
  already enumerated. What the flag gives up is codex's guardrail against editing files it
  cannot roll back, which this harness does not rely on: the workspace is a throwaway tempdir
  that gets archived, and the sandbox stays `workspace-write` with approvals off.

  Worth noting how it hid for ten days: **a cell that never starts is indistinguishable from
  a cell that failed.** The result was a red cell with an error string, which is what a bad
  model answer also looks like. Arm `codex.skips_the_git_repo_trust_gate` exists so it cannot
  hide again, on the probe path too — there the same defect is disguised as an *unavailable
  model*, which is worse.

Verification: selftest **477 arms** (474 on `main`, +3), `mutate_mcp.py` **88/88** caught by the intended arm
(M86 codex-surface-collides-with-its-skills-dir, M87 copilot-env-strips-a-declared-credential-var,
M88/M89 codex-loses-the-trust-gate-flag on the cell and probe argv), plus two live runs:
`cheapshot_harness_smoke` on copilot with `GH_TOKEN` set, which ran **contained** rather than
refused and left **zero** occurrences of the token across the artifact tree; and the same
scenario on codex/gpt-5.4-mini, which passes **7/7**.

The rest of this doc is the original handoff. §1's cost table is still the right analysis; it
simply resolves to "free" for three adapters and "real" for one.

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
| --- | --- | --- |
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
| --- | --- | --- |
| `home_write_escapes(home)` | `agentskill_evals/isolation.py` | Every symlink in the overlay whose `realpath` falls outside it. **This is the lifting condition** — when it returns `[]`, the refusal stops firing on its own. Do not add special cases to it; make the HOME satisfy it. |
| `_refuse_uncontained_home(home, eval_name, refs)` | `agentskill_evals/runner.py` | The refusal. Also refuses when `home is None` (no overlay = real HOME). |
| `interpolated_refs(servers)` | `agentskill_evals/mcp.py` | Which declared fields carry a `${VAR}`. The exposure gate. **Never** use `bool(secrets)` for this — short values are excluded from redaction on purpose and are still credentials. |
| `build_isolated_home(...)` / `_overlay(...)` | `agentskill_evals/isolation.py` | The overlay builder you will be changing. `_overlay` step 1 is the wholesale symlink pass — the thing that creates every escape. |
| `_CellCleanup` / `_purge` / `_remove` | `agentskill_evals/runner.py` | Registration + verified outward-in removal of credential directories, with findings that survive a crash. If you copy credentials anywhere, register the directory here. |
| `probe_contained_home.py` | `harness/tools/` | Maps an adapter's contained surface against the real CLI, driving the harness's own launch path. `--bisect` is the subtractive search that finds where a CLI's credential lives; `--rotation-check` reports whether the child rewrote a copied credential; `--overlay` is the control that separates "containment broke it" from "already broken". Suppresses browser launching by default — a CLI that cannot authenticate may fall back to interactive OAuth. |
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

### 3a. The credential-handling invariant (cost four review rounds; do not rediscover)

**Credential presence, redaction values, containment, refusal, and cleanup severity must all
derive from the same effective environment supplied to the child, after scenario overrides.**

That environment is one expression: `child_env = {**os.environ, **(spec.env or {})}` — the merge
`exec.execute` itself builds (`base = dict(os.environ); base.update(spec.env)`). Every credential
decision in `_run_cell_body` reads from it. The four P1 findings this branch closed were each a
different decision reading from a *narrower* source:

- **Presence** (`has_credentials`) scans the adapter's `credential_env_vars` against `child_env`.
  Sampling `os.environ` missed a token supplied only via `spec.env` (P1d): the child got it, the
  harness didn't — so no containment, no refusal, no redaction, yet the token was delivered.
- **Redaction values** are read `child_env[name]`, not `os.environ[name]`. A `spec.env` override
  reaches the child, so redacting the ambient value scrubs the wrong string and leaks the real
  one (P1a registered the env token at all; P1d fixed which value gets registered).
- **Containment** (materialize a contained HOME) and **refusal** (`_refuse_uncontained_home`,
  including under `isolated: false`) key off that same presence signal — an env credential
  triggers them exactly as an interpolated `${VAR}` does, not only when `mcp_servers:`
  interpolates (P1c).
- **Cleanup severity** (`_CONTAINED_TAIL` + `fatal=` on the HOME registration, vs `_EXPOSED_TAIL`)
  treats a HOME that *copies* long-lived auth as credential-bearing from the moment it is
  registered, before the copy lands (P1b).

Every hole had the same shape: a credential decision reading something narrower than the child's
effective env (the process env alone, or an interpolated field alone). Don't reintroduce one.

**`credential_env_vars` is a survival assertion.** Naming a variable there asserts that the
adapter's `env()` forwards it into the child *unchanged* — it survives the transformation. The
runner samples `child_env` **before** `adapter.env()` runs and trusts the value arrives, which
holds only because `base.env()` mutates HOME/USERPROFILE/XDG/config-home vars and copies
everything else through verbatim: it can neither add nor drop a credential name. A future adapter
whose `env()` rewrote or stripped a declared credential var would break this — the harness would
redact and contain on a value the child never received — so do not declare a variable the adapter
transforms without also teaching detection about the transform.

**...and it is not a statement about how the CLI authenticates.** It says these names are
forwarded and must be redacted. It does not say they are the *only* way in, so it cannot be read
as one: a CLI may authenticate through a credential helper, a socket, or a workload identity that
a contained HOME leaves perfectly intact. `_refuse_uncredentialed_contained_home` — the preflight
that refuses a contained cell with no credential route rather than spending a model call to
rediscover "Not logged in" — therefore reads a **separate** adapter declaration,
`contained_home_required_credential_env_vars`, whose whole content is that measured claim. Its
default `None` means "unanswered" and fires nothing; `[]` is the positive "no env credential
needed here"; a non-empty list is what the refusal acts on, and every name in it must also appear
in `credential_env_vars`. Deriving the requirement from `credential_env_vars` plus an empty
`contained_home_subpaths` was the first implementation and was wrong in the direction that costs a
user a working run (review, PR #99) — mutation M117 is that exact inference, kept so it cannot
come back.

Locked by arms `credential_detection_reads_the_childs_effective_environment`,
`credential_env_var_triggers_containment_without_mcp_servers`,
`credential_env_var_run_is_refused_under_isolated_false`, `adapter_credential_env_var_is_redacted`,
and `contained_home_that_copies_auth_is_credential_bearing_before_the_copy` (mutations M81–M85),
plus `mcp.contained_home_without_its_credential_env_var_is_refused` and
`contained.required_credential_env_vars_are_declared_and_answered` (M115–M119).

---

## 4. Verification protocol

Non-negotiable, in this order. `SELFTEST PASSED` alone is not evidence.

Run from the REPO ROOT. `make -C`, not `cd harness &&` — a `cd` persists for the rest of the
block, so the line after it looked for `harness/harness/.venv/bin/python` and the block could
not be pasted as written (review, fifth round).

```sh
make -C harness dev             # once — creates .venv with the PINNED ruff (see below)

harness/.venv/bin/python -m agentskill_evals.cli selftest     # prints "— N arms"; 509 here
harness/.venv/bin/python -m compileall -q harness/agentskill_evals/
make -C harness lint                                          # ruff; must print "All checks passed!"
python3 harness/tools/mutate_mcp.py                           # 181/181 production + 2/2 instrument
harness/.venv/bin/python harness/tools/verify_mcp_fixtures.py # fixtures + C3-2/C3-3 probe; 202 checks
git diff --check
```

**The arm count is now self-reported** — the selftest ends with `SELFTEST PASSED — N arms`.
It used to live here as a hand-maintained literal and was stale for two PRs running, because
nothing makes forgetting it fail; the number above is what one macOS run produced, kept only
so a reader has something to compare against. Both counts are a FLOOR that only ever goes up:
a lower one means arms or mutations were LOST, which is the single outcome neither command
reports as a failure — and a whole section that silently stopped running now shows as a drop
even though every remaining arm passes. A few arms are conditional (PyYAML, platform), so two
machines legitimately differ by a couple; only the trend on ONE machine is evidence.

**Lint runs from `harness/.venv`, never an ambient ruff.** `pyproject.toml` pins
`required-version = "==0.16.0"` against the `dev` extra that installs it, because a family
selector like `UP` gains rules between releases — unpinned, a tool upgrade arrives looking
exactly like a code regression. A mismatched build refuses to run and names both versions.
The selected families are the ones this tree passes at **zero**, with no `ignore` list, so
any finding is introduced by the change in front of you; what is deliberately not selected is
listed in `pyproject.toml` with the reason. Ruff's `F` family subsumes pyflakes, which is why
the separate pyflakes invocation (and the "pre-existing noise, leave alone" note that used to
sit here) is gone — that noise was fixed rather than documented.

**What `VersionProvenance` does and does not buy.** It is an **audit trail plus a drift
warning**, not verification, and anything written as though it re-checks a claim per build is
overstating it (caught in review of #93). A build outside `_VERIFIED_VERSIONS` *warns and still
runs*. claude's own `witness_held` text says the runtime witness cannot distinguish "the flag
worked" from "a newer build grew a server source outside the flag's reach". codex has less
again: `_VERSION_UNREADABLE` records that it cannot identify the build it just executed at all,
because `--ephemeral` suppresses the only file that carries the version — so its sole check is
an out-of-band `codex --version`. Reviewed assertions that rest on a CLI mechanism
(`mcp_off_mechanism = CLI`, the `--strict-mcp-config` argument, codex's argv disables) are
exactly that: **reviewed**. Nothing here would catch a wrong one. What the machinery buys is
that an unaudited build is *visible* and that re-establishing a claim has a documented
procedure (`clear_hint`).

**Every new arm must be mutation-tested.** Add the mutation to `harness/tools/mutate_mcp.py`
in the same commit as the arm. An arm nothing can break is decorative, and this project has
caught its own decorative arms four separate times.

Mutations carry one of two ID prefixes and are **counted separately** in the summary. `M<n>`
is the normal case: it perturbs production code, and it is what a coverage claim rests on.
`I<n>` perturbs `selftest.py` itself, which is usually circular and proves nothing — it is
legitimate only where the selftest has a feature of its own that no production edit can
reach. Both current entries are the arm counter: `I1` stops it counting, `I2` reverts it to a
process-lifetime total, and each leaves every arm passing while the banner reports a
plausible wrong number. The classification is enforced, not conventional — `mutate_mcp.py`
refuses to start if an `I*` targets anything but the selftest, or an `M*` targets it, because
either mistake miscounts exactly what the split reporting exists to keep straight.

Things that have gone wrong in the *tests*, so you can skip learning them again:

- **The recurring one, eight times now: a check aimed BESIDE the thing that matters.** Every
  instance looks like coverage and is not, and the shape is always the same — the arm and its
  mutation agree with each other while both sit one level away from where the defect lives.
  Seen as: an arm whose two cases could not see the condition they guarded (M117); a live
  assertion the model could satisfy from its prompt without the mechanism running at all
  (`regress_mcp_two_servers`, twice); two cells sharing an artifacts directory so the second
  overwrote what the first was meant to prove (M125); an arm exercising the delta HELPER
  while the regression was the banner's ASSIGNMENT of it (I2); an arm calling the proxy's
  refusal FORMATTER, which is green whether or not any call is ever refused (M158); and — the
  purest form of it — an arm that built `inputRequests` as an array because the code read it
  as one, so the fixture and the defect were wrong together and agreed (M150). Each passed
  review of the code and was caught only by someone asking the question below.

  **The test:** write out what a broken implementation would produce, and check your assertion
  rejects it. If the answer is "it produces exactly what I asserted", the assertion is
  measuring something else. Three corollaries this project keeps rediscovering — a defect that
  passes THROUGH a helper is not tested by testing the helper, only by testing the site it
  reached; for anything with a model in the loop, the model is part of the implementation, so
  an expected value it could reconstruct from its prompt is not evidence; and where the
  subject is a *specification*, the fixture must come from the spec rather than from the code,
  because a fixture written to match the implementation cannot disagree with it. The
  `inputRequests` case and the `{}` subscription closure were both that: hand-written fixtures
  that encoded the same misreading as the code, where one look at the spec's own printed
  example would have shown a map and a `resultType`.
- **A rule applied at the wrong SCOPE is its own defect family, and it cuts both ways.** Three
  findings in one review round were all this shape: a per-version rule applied to every version
  (`inputRequests` read on legacy responses, where `Result` is open-ended and the key means
  whatever that server means by it); a backward-compatibility rule applied to a method that has
  no *back* (an absent `resultType` defaulted to `complete` on `subscriptions/listen`, which
  exists only in the revision where the field is mandatory); and prose followed over the
  schema that is declared the source of truth (`RequestId` refused for `1.5`, which the prose
  calls an integer and the schema calls a number).

  Two of those refuse conforming traffic and one forwards non-conforming traffic, which is the
  point — **over-strict is not the safe direction.** A proxy that fails a clean cell has broken
  the run just as surely as one that forwards a definition, and it is harder to diagnose
  because everything looks like it is working correctly. When writing a check, name the
  version, the direction and the document it comes from; if the rule cannot be stated with
  those three, it is not yet a rule.
- **"Nothing escapes" is a claim about ORDER, and order is the thing to check.** Letting a
  request through on a *pending* handshake looked safe because the negotiated version would
  govern its response — true only if the negotiation lands first, and nothing makes it. The
  pipelined request's response can arrive before the `initialize` response and be read under
  no version at all. When an argument for safety rests on one message preceding another,
  write down what happens in the other order; on a duplex stream both orders happen.
- **A correct principle stated at too narrow a WIDTH is still a hole, and it will be found one
  route at a time.** The spent-request-id rule took four rounds: cancelled ids remembered but
  cleared on reuse; then held, but lifted once a straggler was seen; then held permanently —
  for cancellation only. Each fix was right about the case in front of it. The tell was there
  the whole time: the argument that justified the third version ("no observed response proves
  another cannot follow, because the server is the side we do not control") **never mentioned
  cancellation**, so it had always applied to ordinary answers and graceful closures too, and
  those stayed open. **When an argument lands, re-read what it actually quantifies over and go
  fix every case it covers** — not the one that prompted it. A reviewer should not have to
  walk you along a rule's own scope.
- **Refusing something legal is a cost, and it needs a price or a flag.** The same rule refuses
  a client behaviour the spec permits — id reuse after a response. §4 already knew over-strict
  is not the safe direction, and §10.5 says failing a clean cell is as much a failure as
  forwarding a definition. The honest handling is what C3-2 did for the era gate: measure the
  fleet, and if you cannot yet, say in the design that it is an unpriced strictness rather
  than writing a comment that cites a measurement which does not exist. I had one of those —
  "the monotonic counters every measured CLI uses (C3-2, §9)" — where C3-2 measured
  *pipelining* and had never looked at ids. **A citation to the right document is not a
  citation to the right measurement.**
- **Absence of a positive result is not a negative result.** The C3-2 probe printed "No CLI
  pipelined ... costs the fleet nothing" whenever no row was positive — including a run where
  every CLI failed to connect — and then contradicted itself two lines later with the list of
  what had not been measured. Same distinction the per-row classifier already drew, dropped at
  the point where the rows became a conclusion. **Any fleet-wide claim needs every row
  answered; check that the summary cannot be produced by an empty measurement.**
- **One observed message is no evidence about the next one.** The same round, twice, in
  different disguises. A cancelled id's quarantine was released once a late response had been
  *seen*, reasoning that "the request is over on both sides" — over on the client's side; the
  other side is a server the scenario author does not control, and a second straggler is no
  more nonconforming than the first was. And a negotiated legacy version was read as settling
  the era for the whole connection, when the modern revision carries the era **per request**,
  so an initialized client can still open a modern subscription and a dual-era server can
  serve both at once. Both are the same error: **summarizing a stream into a fact about the
  connection.** Before writing state that says what the peer *is*, check whether the protocol
  says it per message; and never lift a guard on the strength of one observation from the side
  the boundary exists against.
- **Ambiguity is a third answer, and it belongs in the return type.** When a server
  cancellation could name either its own legacy request or the client's modern subscription,
  the first fix searched both maps, the second picked by era flag, and both were *choosing*.
  The message carries an id and nothing else; there is no fact to choose with. Failing loudly
  is the honest outcome, and it needed the observer to be able to return an anomaly at all —
  a function typed `-> None` cannot express "I could not tell", so it will guess.
- **An instrument's regression is not its READER's regression.** `verify_mcp_fixtures.py` E13
  covers the shim that measures pipelining, and all of it stayed green over a probe that would
  have classified a CLI which died before handshaking as "modern `n/a`, exit 0" — a false
  clean result in the tool being used to justify a design decision. The classification, not
  the log, is the probe's actual output. Factor it out of `main()` so it can be driven on
  synthetic rows (E14), and treat "what does this tool PRINT" as a thing under test whenever
  the printout is the evidence for a decision.
- **An exception argued on safety grounds can be the banned technique wearing a hat.** Asked
  to scope MRTR detection to `resultType: "input_required"`, I scoped the *version* and
  skipped the discriminator, reasoning that "a mislabelled definition is still a definition".
  That argument is the structural scan restated — it says *I will look for tool-shaped things
  regardless of what the protocol says this payload is*, which is precisely what §10.6
  rejects — and it diagnosed a plain `ping` result as tool-bearing sampling. Deviating from a
  rule is sometimes right, but the burden is to show the deviation is not an instance of the
  thing the rule exists to forbid, and "it is safer" does not discharge it: unsound in the
  strict direction is still unsound.
- **A mutation whose defect is a CRASH needs an arm that can catch one.** An unhashable id
  reaching `dict.pop` raises several frames from anything that could log it — which is the
  defect — but an arm that lets it propagate turns the mutation into "failed, but NOT via",
  which proves nothing and reads like a tooling problem. The arm calls through a helper that
  converts an exception into a value the assertion rejects. Same principle as the rest of §4:
  the arm has to be able to *observe* the failure it is named for.
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
  rather than an infinite hang — but it is a backstop, not a licence to hang. Each result line
  also carries its selftest's wall time, and the summary names the slowest; read them against
  the `baseline:` line. A mutation at several times baseline is already the M65 shape, just not
  yet past the timeout, and that gap is the only warning anyone gets before it becomes a hang.

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

- ~~**codex cannot run a cell at all — fix the trust gate first.**~~ **Done (2026-07-27)** —
  `--skip-git-repo-check` on the cell and probe argv; see §0b for the reasoning and for why
  it is the flag rather than `git init`.
- **Map the FILE-based contained surfaces on Linux — but not before the selftest passes
  there, and not before someone actually needs it.** Deliberately not scheduled; the trigger
  is a real Linux cell run or Linux CI, not tidiness.

  Scope it correctly, because the first version of this entry (and the summary that produced
  it) got it wrong in the alarming direction. What is macOS-specific is **not** the surfaces
  and **not** the machinery: `isolation.py`'s containment path has no platform branch at all
  (the one `darwin` mention is a comment about `normcase`), and the `[]` + environment-token
  route is platform-independent by construction — the token comes from the environment, never
  from HOME, so the keychain never enters into it. A Linux operator who exports `GH_TOKEN` or
  `CLAUDE_CODE_OAUTH_TOKEN` gets a working contained run today.

  What is macOS-specific is the *negative* half of the claim: "the keychain is unreachable, so
  `[]` is the only answer." On Linux there is probably a credential FILE under HOME that would
  let a contained run work with no environment token at all, and it is unmapped. antigravity's
  `None` may be wrong in the same direction and in its favour — it is refused today because
  macOS gives it no route, and a Linux build storing a file would make it mappable. So the
  declarations are **correct but incomplete** on Linux, not wrong, and the missing piece buys
  convenience rather than safety.

  **The ordering is the part worth writing down: the selftest must be Linux-clean FIRST.** The
  suite is what proves the containment machinery is sound on a platform, and it is not clean
  there — a Linux run fails a symlink-scrub arm that passes on macOS, because several
  filesystem arms encode darwin semantics (xattrs on symlinks, hardlinked symlinks). Probing
  first would produce surfaces validated by a suite that does not pass, which is a measurement
  nobody can trust. Sequence: make the selftest Linux-clean → then `probe_contained_home.py
  --bisect` per adapter (~7 runs each; `--self-check` needs no logins, so it works on a bare
  CI box) → then decide whether the field needs to be platform-conditional. Write no
  `if sys.platform` before that last step: ahead of the measurement it is just a second guess.

  That Linux selftest failure is also why the parked CI workflow
  (`harness/ci-selftest-mutation`) runs on macOS runners rather than Linux, and it is the
  reason to fix, not a reason to switch platforms.
- **Phase 1b codex** — `-c` mapping + canonical `mcp__server__tool` naming in its parser.
  Blocked on §9 probe #2 (whether TOML array/inline-table values survive `-c`). Pairs with
  `$CODEX_HOME` materialization, above.
- **Phase 2 copilot** — `--additional-mcp-config @file`, per-server `tools`, `--secret-env-vars`.
- **Phase 3 antigravity** — MCP injection.
- **C3 harness-owned filtering proxy** — required before any scenario points `tools:` at a
  server its author does not control, and required for agy tool gating regardless.
  **Designed in `DESIGN_MCP_Support.md` §10 (2026-07-29); being built.** stdio only in the
  first cut — remote `tools:` stays refused. Build order: ~~probe **C3-0**~~ →
  ~~probe **C3-1**~~ → ~~a **dual-era mode for `fixtures/echo_mcp_server.py`** (#98)~~ →
  ~~the **decision layer** + its arms, wired to nothing (#100)~~ → the **I/O half** (spawn,
  the two pumps, `SIGTERM`/`SIGINT` handlers, §10.5's shutdown, the audit log) plus a
  wire-level driver → the adapter integration that unlocks `tools:`.
  Everything before the last slice cannot affect any run, which is the point: this is harness
  code in the request path of every gated cell.
  **Probe C3-2 resolved 2026-07-31** (`tools/probe_mcp_pipelining.py`): no CLI pipelines
  requests behind `initialize`, which is what licenses §10.2 REFUSING one. A pending
  negotiation cannot govern traffic — the pipelined request's response may arrive first,
  under no version at all — so tolerating pipelining needs a defer-and-replay action, and
  C3-2 says the refusal costs the fleet nothing today. It needed a new
  shim mode (`PROBE_MCP_INIT_DELAY_MS`) *and* a raw-fd read path in the shim, because
  `sys.stdin.readline()` buffers a chunk rather than a line and would have hidden exactly the
  bytes being measured. claude is free to probe (`claude mcp list` health-checks stdio
  servers with a full handshake); the other three cost one cheap model call each.

  **Both gating probes resolved 2026-07-29** (`fixtures/probe_era_mcp_server.py`, results in
  `DESIGN_MCP_Support.md` §9). Three findings changed the build:
  1. **The fleet is split three ways.** claude and copilot `2025-11-25`, codex
     `2025-06-18`, agy **`2026-07-28`** (modern). Dual-era is a day-one requirement, and
     "legacy" is two implementations rather than one. The version allowlist is those
     three exactly.
  2. **The proxy MUST handle `SIGTERM`.** codex and copilot signal rather than closing
     stdin; without handlers that write the terminator, §10.5's verdict rule fails every
     clean cell on half the fleet.
  3. **`subscriptions/listen` exists and agy uses it.** A request that is never answered
     is normal, so the in-flight correlation map must not treat one as a leak or a
     timeout.
  The fixture step moved ahead of the proxy because agy being modern makes it a
  prerequisite rather than a contingency.

Smaller, unblocked:

- ~~Report the **witnessed** MCP server set from the init event so MCP matrices can reach
  `verified`.~~ **Done (2026-07-28)** — `ParseOutput.mcp_servers_witnessed` carries
  `(name, status)` pairs from claude's init event along the path `cli_version` already takes,
  and `_consistency` prefers it over argv. The set and the health are **two axes**, because
  the first cut folded them together and manufactured both states it exists to prevent
  (unstated health reading as agreement, and as drift). The third state is what makes the
  split work and it is a property of the source: a witness that omits a status leaves health
  *unknown*, while argv names servers it **disabled**, which never ran and so have no health
  to state. See `DESIGN_MCP_Support.md` §8 Phase 1.
- Portable `used_mcp_tool` assertion (§7) once a second adapter lands.
- ~~Refuse `isolated: false` combined with `mcp_servers:`.~~ **Done (2026-07-28)**, narrowed
  from the flat phrasing: the refusal is a property of where the adapter's **kill-switch
  lives**, not of isolation. copilot and agy keep MCP off with masks in the overlay, so a
  cell with no isolated HOME loads the user's real servers beside the declared ones and is
  refused; claude (`--strict-mcp-config`) and codex (per-server disables) hold whatever HOME
  the child is handed, and a blanket rule would have refused the one configuration that
  works today for no safety gain. Classified by a declared **tri-state**
  (`mcp_off_mechanism`: `CLI`, `OVERLAY_MASKS`, or `None` for not-determined). Deriving it
  from mask presence answered "has a mask" rather than "depends on one"; a boolean then made
  *unclassified* share a value with *uses masks*, so an adapter nobody had classified was
  cleared by any isolated HOME — by an overlay materializing nothing for it (both found in
  review). `None` fails closed in BOTH directions, the same not-mapped-is-not-a-claim rule
  `contained_home_subpaths` uses. An `OVERLAY_MASKS` claim is checked against the masks it
  names *and* against whether they have anywhere to act, **per channel**: plugin masks with
  no `global_plugin_registry_subpaths` root are materialized nowhere. Aggregate sufficiency
  is not enough — antigravity declares both kinds of mask, so losing its registry root left
  its direct mask satisfying a "does it have masks" test while the plugin channel went
  uncovered. The two `CLI` declarations are reviewed assertions, not
  verified ones — see §4's note on what provenance does and does not buy.
  **Latent until an adapter is both** — the mask-dependent adapters are exactly the ones
  that cannot inject, so `validate_mcp_support` refuses them first. It arms itself the day
  copilot or agy gains injection, which is why it went in before that rather than after.
- **Route plugin-registry masks into custom config-home mirrors.** Both mirror builders —
  `isolation.build_mcp_masked_home` and the runner's config-home mirror — forward only
  `reroot_config_masks(...)`, never `plugin_registry_subpaths` / `plugin_config_masks`. An
  adapter declaring both a plugin registry and a custom config home therefore gets a mirror
  where the plugin MCP channel is unmasked, and the child is repointed at that mirror. Needs
  a reroot for the registry subpaths (the `reroot_config_masks` prefix-strip, applied to
  paths rather than mask keys) plus the two call sites. Unreachable today — agy declares the
  plugin masks and no config home, the other three the reverse — and `Adapter.mcp_off_gap`
  REFUSES the combination meanwhile, with an arm that reads the leak out of the builder.
  Delete that refusal in the same commit that lands the routing; the arm will already be red.
- Sweep for other default-held invariants (`judge`, `max_cells`, `provision`).
- Report the `mcp_servers:` + no-overlay refusal in the **CLI pre-flight** as well, next to
  `validate_mcp_support` in `cmd_run` — today it surfaces as a failed cell per cell, where
  the sibling MCP refusals surface before anything runs ("they will always fail and waste
  tokens"). Deliberately not done with the refusal itself: nothing in the selftest drives
  `cmd_run` (only `validate_spec`), so the pre-flight copy would be a second wording of the
  rule with no arm to keep it honest. The blocker is the missing `cmd_run` harness, not the
  check — build that first, or this lands decorative.

Still open in `DESIGN_MCP_Support.md` §9: claude's `mcpServers` http/sse JSON shape; copilot's
MCP tool-name format and plugin-declared server reach; agy's transcript tool-name format and
`url` vs `serverUrl`.
