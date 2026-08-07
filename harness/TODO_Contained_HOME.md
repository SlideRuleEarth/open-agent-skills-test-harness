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

harness/.venv/bin/python -m agentskill_evals.cli selftest     # prints "— N arms"; 562 here
harness/.venv/bin/python -m compileall -q harness/agentskill_evals/
make -C harness lint                                          # ruff; must print "All checks passed!"
python3 -u harness/tools/mutate_mcp.py                        # 311/311 production + 2/2 instrument + 14/14 fixture
harness/.venv/bin/python harness/tools/verify_mcp_fixtures.py # fixtures + C3-2/C3-3 probe; 293 checks
harness/.venv/bin/python harness/tools/verify_mcp_proxy.py    # the C3 proxy over real pipes; prints "— N checks"; 77 here
git diff --check
```

**The mutation suite now runs THREE suites, and which one runs is a different question from
which total a mutation counts in.** The suite is chosen by the file: `agentskill_evals/` is
proven by the selftest, `fixtures/` and `tools/` by `verify_mcp_fixtures.py`, and the proxy's
I/O half plus its awkward server by `verify_mcp_proxy.py` — the two files named explicitly,
because both sit in a directory another suite owns. The **class** is chosen by the file's role:
`M*` perturbs production, `I*` the selftest itself, `F*` an instrument. Those agreed while
there were two suites and stopped agreeing the moment `mcp_proxy_io.py` arrived — production
code no arm can reach. Running `mutate_mcp.py` therefore covers the last two lines above as
well, and its three totals are three different claims. Do not add them together.

**Every command in that block runs without a network and without a privilege**, and that is a
requirement rather than an observation: the proxy verifier's `read_failed` case used to open a
loopback socket, so under a sandbox that denies `bind()` the whole file died at `EPERM` and a
reviewer could not run the suite this work's evidence rests on (review, PR #103). If a case
needs an arrangement the environment might refuse, find another arrangement — a skipped check
and a check that cannot fail look identical from the outside.

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
- **An observation that matches a rule's SHAPE may still not be the behaviour it forbids.**
  C3-3 counted any repeated request id as evidence that the spent-id rule costs the fleet. But
  a repeat while the first request is unanswered is a live duplicate, which JSON-RPC forbids
  and the proxy refuses on entirely separate grounds; only a repeat *after* the response is
  the legal-but-refused behaviour being priced. The probe reported a client the proxy rejects
  anyway as proof that the proxy is too strict. **When measuring the cost of a rule, the
  observation has to exclude cases some other rule already covers** — otherwise the price
  includes traffic that was never going to work.
- **Some measurements cannot be taken from where you are standing, and the answer is to stop
  claiming them.** C3-3 needs to know whether an arrival preceded a response. Three rounds of
  work went into observing that better — log at arrival, then drain continuously — and the
  honest end point is that **no observer inside the server can establish it at all**: bytes
  can sit unread in the kernel pipe while the main thread writes a response, so the ordering
  is a property of scheduling, not of the client. Reducing the error rate from systematic to
  occasional is real progress and still not a measurement. The fix was to make the probe
  *report and not conclude*, and to name the workload that would conclude — one where the
  driver waits for each response, so the ordering is true by construction. **When an
  instrument keeps needing to be made more careful, check whether the quantity is observable
  from that vantage point at all.**
- **A new fact about a run belongs in the CLASSIFIER, not bolted onto the exit code.** When
  the shim gained a "my reader died" signal, I threaded it into the exit status and stopped —
  so a broken row still classified as a clean measurement, the fleet-wide conclusion was
  printed over it, and the tool then exited 1 having already published the wrong sentence.
  That is the same "fleet-wide claim from an incomplete run" defect fixed two rounds earlier,
  walking in through a door the fix did not cover, because the earlier fix taught `unmeasured`
  about two ways of being unanswered and this was a third. **When you add a reason a run might
  be untrustworthy, put it where the existing reasons live** — one predicate that everything
  downstream reads — rather than beside them. The tell is a new boolean on the row that only
  one caller consults.
- **When a claim is retracted, the retraction has to reach every place that made it.** After
  agreeing that C3-3 concludes nothing, the committed tree still had a docstring calling a
  positive result "conclusive", a design paragraph saying the same, and a function still
  describing its output as "wire order" — the exact phrase the round had just established the
  instrument cannot deliver. The behaviour was right and the authoritative text was a round
  behind. **Grep for the retracted claim, not just the changed function**; a reviewer reading
  the docs gets the old answer, and so does the next person to touch the code.
- **A tool that only prints its findings has not reported them.** The malformed-request events
  were written to a temp log, and `probe()` carried only the timeline — so without `-v` a
  client sending malformed frames produced no finding in the summary and no effect on the exit
  status. **Anything that changes the interpretation of a run has to reach the result object
  and the exit code**, not just a file someone might read.
- **An ON-DEMAND reader cannot observe arrival order, and moving the log is not enough.**
  Telling those two id-repeats apart is a question about the order messages crossed the
  stream. Three attempts: logging from the main loop gave *processing* order; moving the log
  into the read helper looked like the fix and was not, because the helper is still only
  called when the loop wants a line — so outside the artificially held window the shim
  answered the current request before asking for more input, and two requests that both
  crossed before the response was written were logged `req, resp, req`. **Nothing that reads
  on demand can know when bytes it has not asked for arrived**; the only fix is to drain
  continuously, which here means a reader thread. Fourth time C3 has been bitten by a buffer
  between the wire and the code reporting on it, and the *first* time the buffer was the
  program's own control flow rather than an I/O layer.

  The related trap in the same change: `verify` covered only the case where the shim happens
  to drain continuously anyway (inside the delay window), so every check passed over the
  defect. **When a fix depends on a code path being taken, check the path where it is NOT.**
- **Record an outbound event AFTER the write, not before.** `response_id` was logged first, so
  a graceful closure written to a departed reader — C3-1's measured agy behaviour, where the
  write raises and nothing leaves — was recorded as an answer that never happened. Any later
  request on that id then read as post-response reuse against a response no client received.
- **A terminal verdict truncates the evidence after it.** The proxy `Fail`s on a duplicate id
  and tears the connection down, so a later reuse in the same log is traffic the rule being
  priced would never have seen. Classification has to stop where the system stops. **When
  reading a log to predict a component's behaviour, model its early exits too** — otherwise
  the reading includes a future that the component's own failure prevents.
- **A measurement of a rule must use the RULE'S OWN definition of its terms.** C3-3 asks "does
  any CLI reuse a request id", to price a rule under which `1` and `1.0` are the same id and
  `0` and `-0.0` are the same id. It deduplicated on `repr`, under which none of those pairs
  match — so the CLI whose reuse the proxy would refuse was the one the probe reported as
  clean. Same family as the `inputRequests` fixture written from the code: the check and the
  thing checked have to disagree when the thing is wrong, and they cannot if the check
  re-derives the definition instead of importing it. `request_id_key` is now exported for the
  probe, and where importing is impossible — the shim runs with only the stdlib reachable —
  the copy is asserted equal to the original on the cases that distinguish them.
- **"Absence of a positive" needs a sample size argument, and it is not the same for every
  question.** C3-2 and C3-3 ride on one run, and only one of them is settled by it: pipelining
  happens exactly once per connection, so a clean run is an answer, while an allocator that
  emitted 0 and 1 has said nothing about the fourth request. I gave both the same treatment
  because they came from the same log. **Before concluding from a negative, ask how many times
  the run gave the behaviour a chance to appear** — and if a cell exercises it more often than
  the probe did, the probe has not priced it.
- **A compound assertion can be unfalsifiable by operator precedence.** `"costs" not in t and
  "unpriced" in t or "does NOT price" in t` is `(A and B) or C`, and C was true by
  construction — a check that could not fail, written while fixing a finding about checks that
  cannot fail. Caught by reading it back rather than by running it, because it passed.
  **Parenthesize every mixed `and`/`or` in an assertion, and prefer several `check()` calls to
  one clever one**: a check whose failure mode you cannot state is not a check.
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
- **A READER'S INPUT IS THE FORMAT, NOT THE WRITER'S PROMISE.** `mcp_audit.validate` was
  written against the record the proxy intends to write, so `{"triggers": {}}` raised
  `KeyError`, an array `state` raised `TypeError`, and a scalar `fired` raised on iteration —
  six shapes crashed and two more passed clean. The module exists BECAUSE the writer is under
  suspicion, which makes "the writer would never emit that" the one justification unavailable
  to it. **A crash is worse than the malformed input that caused it**: a failed cell is a
  result, and a traceback out of `verify_post_run` is an absent verdict — the outcome the
  whole section exists to make impossible, arriving through the code written to guarantee it.
  Parse first, into types, reporting every shape it cannot use; judge afterwards.
- **`x.get(k) or []` erases the difference between MISSING, NULL and EMPTY.** All three become
  "nothing on that axis", and nothing on an axis is legal — so a writer that forgot the axis
  entirely validated clean. The same idiom hid a second one: a fault point whose suppression
  list was empty read as no fault point at all, which passed exactly the arm-only run that
  case exists to reject. **Wherever absence has a meaning, check what else produces the same
  value** — this is the completion-facts lesson (§10.5.1) applied to the record's own keys,
  which is where it should have been applied first.
- **A BOOLEAN `present` FLAG THROWS AWAY THE THING BEING CHECKED.** `child_status` was parsed
  as `"child_status" in raw`, so `null`, `"fabricated"` and `true` were all a clean verdict on
  a record claiming to hold a child's exit status. `null` is the sharpest — the record asserts
  the evidence exists and carries none of it — but `true` is the one a type check still misses,
  because **`isinstance(True, int)` holds in Python**, so the obvious repair accepts a boolean
  standing in for an exit code. Presence-not-content is the same defect as
  absence-not-emptiness one level down, and it arrives the same way: a flag substituted for a
  value nobody looked at. **Preserve the value, then check it**; a sentinel distinguishes
  absent from every value JSON can carry, and a `present` boolean cannot.
- **ERASING A BAD VALUE INTO `None` HIDES IT WHEREVER ABSENCE IS LEGAL.**
  `x if isinstance(x, str) else None` turns every non-conforming value into the same `None`
  the parser uses for "not there" — so a `cause: null` under a `done` step was
  indistinguishable from a step with no cause, and the rule forbidding a cause under `done`,
  added one round earlier, never saw one. Three sibling fields used the identical idiom and
  survived only because their tags make them REQUIRED, so a different check caught the
  erasure: **coverage by accident, from the same defect.** Preserve or report; never
  substitute the absence marker for a value that was present. And the arm has to feed it
  `null`, because a valid string cannot catch a parser erasing the field before validation.
- **A payload is legal only under the tag that READS it, and that rule quantifies over every
  tagged union in the file.** `cause_forbidden` was written for completion facts and stopped
  there, leaving `anomaly` legal on any trigger and `fact`/`exception` legal on any outcome —
  fields nothing consults, which is precisely what that rule exists to reject. One rule
  widened one route at a time, again, and the tell was as advertised: the justification
  ("nothing reports what nothing looks at") never mentioned completion facts.
- **A closed set that is only checked on the way OUT is not checked.** The fault point's
  suppression targets are completion facts, but the parser accepted any string — so
  `{"suppresses": ["not_a_fact"]}` had no structural problem. It was easy to miss because the
  instance is anomalous regardless (the hook was armed), and **"anomalous for an unrelated
  reason" reads exactly like "checked"** in the output. Whenever a field's legality is implied
  by another rule already failing the run, that is the moment it needs its own check.
- **Inferring a cause from available evidence accepts contradictory records.** A `failed`
  completion fact used to be legal if SOME pairing existed, so one record could carry both a
  real group-kill failure and a fault-point suppression of that same step — two incompatible
  accounts of one operation, no problem reported. The fix is that the fact DECLARES its cause
  and the reader checks the record bears exactly that one. **"Is there evidence for this?" is
  a weaker question than "is this the evidence?"**, and the gap between them is where two
  truths sit side by side.
- **A TOTALITY assertion over a membership test is a tautology.** The first cut of the audit
  arms tried to pin "every enumerated reason has a verdict" as `all(is_clean(r) or r not in
  CLEAN_REASONS for r in EVERY_REASON)` — which is `P or not P` once you notice `is_clean(r)`
  IS `r in CLEAN_REASONS`, so it passes against any set whatsoever, including an empty one.
  Not the precedence trap above; the disjunction simply restates the function's definition.
  Where a classification is a set lookup, totality is free and therefore not worth asserting —
  what is worth asserting is the **exact set**, because the failure that matters is a sixth
  member appearing, and that is the direction nothing announces: a cell that now passes where
  it used to fail. Two arms, because the two failures are different: the set stated exactly,
  and an unenumerated reason classifying as an anomaly.
- **A mutation whose defect is a CRASH needs an arm that can catch one.** An unhashable id
  reaching `dict.pop` raises several frames from anything that could log it — which is the
  defect — but an arm that lets it propagate turns the mutation into "failed, but NOT via",
  which proves nothing and reads like a tooling problem. The arm calls through a helper that
  converts an exception into a value the assertion rejects. Same principle as the rest of §4:
  the arm has to be able to *observe* the failure it is named for.
- **One field cannot carry both WHY something stopped and WHAT HAPPENED on the way out.**
  §10.5.1 was drafted as a single closed list of "reasons an instance can end", and it read as
  complete because every entry in it was real. It was not: `client_eof → shutdown_write_failed
  → shutdown_child_killed` is one ending with three facts in it, and a single-slot reason has
  to drop two. Whichever it drops is the load-bearing one — drop the trigger and a protocol
  anomaly reads as a clean client close; drop the outcomes and a failed process-group teardown,
  which is a surviving grandchild holding a credential, is certified clean by the record meant
  to prove the opposite. The fix is two axes and a **monotonic conjunction** over both, not a
  lookup on the last event. **The tell is enumerating things that can happen at different
  phases of one lifecycle into one flat list**: ask whether two entries can be true of the same
  ending, and if they can, they are not alternatives. The second tell is asymmetry once the
  axes are split — `read_failed` had no shutdown-phase counterpart, and that missing
  `shutdown_read_failed` is the §4 defect above (a swallowed `OSError` presenting as clean EOF)
  waiting to happen in the proxy instead of the shim.
- **Totality over an enumeration says nothing about the endings that write no record.** The
  same section's verification plan was "drive every reason and check every reason has a
  verdict" — which cannot reach `SIGKILL`, a crash after the start record, or a terminator
  truncated mid-write, because in each the process that would name a reason is already gone.
  Those need the absence rule (a start with no well-formed terminator is an anomaly) and their
  own cases, half of them driven against the *reader* with synthetic logs rather than against
  the program. A closed enumeration is evidence about the endings that can speak.
- **A LIST OF EXCEPTIONS IS NOT EVIDENCE THAT ANYTHING RAN.** The two-axis fix defined an empty
  cleanup-outcome list as "every step did what it promised" — but every outcome in it records
  something going *wrong*, so a teardown that skipped its process-group kill outright raises
  nothing, records nothing, and satisfies a verdict computed from the reasons alone. The thing
  being certified clean is a surviving credential-bearing grandchild: the exact failure the
  step exists to prevent, passed by the record that was supposed to prove it did not happen.
  The fix is **positive completion facts** — each step records `done` or `not_applicable` with
  its justification, and *missing* is an anomaly, because a step that reported nothing cannot
  be told apart from a step that never ran. **Whenever absence is given a meaning, check
  whether two different situations produce it**; here a blank meant both "did not apply" and
  "did not happen". And since a completion fact is the implementation's own claim about itself,
  one case has to verify it from outside the process — the group really gone, checked by the
  driver, not asserted by the proxy.
- **"EVERY recorded X is clean" is TRUE when nothing was recorded.** The fix above made the
  verdict a conjunction over the triggers, the outcomes and the completion facts — and every
  clause of it is universally quantified, so a terminator with an empty trigger list satisfies
  all of them. That is not a clean instance, it is a broken writer, and the verdict said clean.
  Any `all()` needs a **structural clause first**: the collections that must be non-empty are
  non-empty, every required field is present, every value is from its closed set. Owned by the
  *reader*, not the writer — a process broken enough to emit the malformed record is the wrong
  one to ask whether it did. And the validator needs the **legal record that resembles an
  illegal one** among its cases (here `spawn_failed`, whose facts are genuinely inapplicable),
  or "reject everything" passes the whole suite. Same rule as the arms: state what a broken
  implementation produces, then check the assertion rejects it — `all([])` is the purest form
  of an assertion that cannot fail.
- **An outside observer can only assert what it can observe — the C3-3 lesson, second
  instance.** The external check on teardown was written as "confirms the direct child was
  reaped", which no other process can establish: a proxy that exits without reaping leaves a
  child that init adopts and reaps, and afterwards the two are identical. The load-bearing
  property is narrower and *is* observable — **nothing from the instance is still alive** — so
  that is what the case claims. Prefer **monotone evidence over a probe**: an inherited pipe
  whose EOF proves every holder is gone cannot race the proxy's exit and cannot be fooled by a
  PID recycled under `kill(pid, 0)`. Then check the discriminating power of the fixture itself
  — a helper that exits on its own, or on stdin EOF, lets a proxy that skipped the group kill
  pass on the helper's good manners rather than on its own behaviour.
- **The EVIDENCE CHANNEL has a premise, and it needs its own positive check.** The teardown case
  proves "nothing survived" by reading EOF on a pipe inherited into the child's group — sound,
  and worthless if the descriptor never arrived. A broken `pass_fds`, or a helper that closes
  the writer at startup, produces an immediate EOF and a passing case with nothing torn down.
  So the helper writes a **distinctive readiness token** through that same pipe and holds the
  writer for life, and the driver requires the token, then requires **no EOF before shutdown
  starts**, and only then treats EOF as the finding. The token must be the helper's own, or the
  child's liveness is accepted as the helper's. Third instance of one pattern in this PR: a
  check that passes hardest when nothing happened — first in the proxy's outcome list, then in
  the verdict's quantifiers, then in the instrument built to catch the first two.
- **A liveness signal is about WHOEVER holds it, not about who you meant.** The readiness token
  above proves the helper *once* held the pipe; it says nothing about who holds it now, because
  every process on the inheritance path got a copy. A helper that writes its token and closes
  its writer then survives undetected: the ancestors keep the pipe open, the pre-shutdown check
  sees no EOF, and the EOF arrives later for the wrong reason. Attribution needs a **sole
  holder** — each stage closes its copy as soon as it has passed it on, the driver's own close
  included, or EOF never arrives at all and the case hangs instead of passing.
- **A NEGATIVE observation is only about the subject once every other candidate is gone.** The
  negative control above requires "no EOF" to mean "the helper survived" — but non-EOF only
  ever means *somebody* holds a writer, so taken too early it is satisfied by an ancestor and
  proves nothing. Two defects then cancel: a helper that closes its writer early and survives,
  plus a proxy that keeps its copy until exit, gives a positive case passing on the proxy's
  exit and a control passing on the proxy's copy, with the survivor invisible to both. Fixing
  it is ordering, not extra assertions — wait for every ancestor to exit, *then* observe.
  **Ask what else could satisfy a negative observation, and eliminate those first**; and close
  with the positive (kill the group, require EOF), so the control cannot pass by observing
  nothing at all.
- **TWO ENUMERATIONS DESCRIBING THE SAME EVENT MUST BE ABLE TO AGREE.** The completion facts
  could say `done`, `not_applicable`, or nothing — and nothing was defined as malformed. But the
  outcome axis already had `shutdown_reap_failed` and `shutdown_group_kill_failed`, which are
  those same steps running and failing, and there was **no legal way to say so on the fact
  side**. A record of a failed teardown could not be written at all. The fix is a `failed` state
  paired to its outcome, closed in **both** directions — a `failed` fact requires its outcome
  and the outcome requires the fact — because a validator checking one direction lets the writer
  record the failure on whichever axis it prefers and stay silent on the other. **When one thing
  is described by two enumerations, enumerate the cross-product, not each list alone**; the
  missing cell is where a real state ends up unwriteable.
- **A CATCH-ALL has to be reachable from every case it claims to cover.** `shutdown_anomaly` was
  defined as an exception escaping *any* teardown step, and then paired with only the two facts
  that had no typed outcome — so an exception during the drain, the reap or the group kill had
  no legal way to be recorded, which is the same unwriteable-state defect as the round before,
  found in the mechanism added to prevent it. A catch-all is only a catch-all if the pairing
  rule quantifies the way its definition does. It also needs its scope pinned: the anomaly
  carries the step it escaped, and the validator requires that step to match the fact claiming
  it, or one catch-all excuses a failure anywhere in the record.
- **CONFIGURED IS NOT FIRED, AND ONE FACT CANNOT BE BOTH.** The rule that a suppressed step
  reads `failed(fault_point_configured)` let *arming* justify the claim that a step was
  suppressed — and, read in the other direction, made a genuinely unfired hook structurally
  invalid. Those are exactly the two states the no-op-injection case exists to tell apart, so
  the pairing rule destroyed the test it was written alongside. Arming is a start-record fact
  and is anomalous **on its own**; firing is per-fact evidence and is the only thing a `failed`
  completion may pair with. **When a check needs a hook's activation, ask whether the record it
  reads proves activation or merely intent** — and check that the isolating case can still go
  green when the clause under test is deleted, or it is pinning something else.
- **A PAIRING KEY MUST BE AS FINE-GRAINED AS THE THING IT PAIRS.** The catch-all outcome was
  keyed by step number, and step 2 owns two completion facts — so one `shutdown_anomaly(step=2)`
  raised by closing stdin would license `drain_ended: failed` as well, or instead. The check
  "the step matches the fact" cannot see the difference, so it passes for the wrong reason,
  which is worse than not having it: it reads like coverage. The key is now the exact fact, and
  the sibling case is pinned in both directions. **The tell is a key whose value set is smaller
  than the set of things it identifies** — look for a table row that names two things, and for
  a cross-check written against the coarser of two available identifiers.
- **Evidence that only one actor can hold must be structurally tied to that actor's claim.**
  `child_status` is obtainable only by the process that reaped the child, so it is required
  exactly when `child_reaped` says `done` and forbidden otherwise. Left loose, a writer can
  claim the reap while lacking the one thing a reaper necessarily has, or attach a status to a
  child it never reaped. The observation that fabricating it would be *a lie rather than an
  omission* was already in the design as a remark — **a remark about what a liar would have to
  do is not a check**; make the reader enforce it.
- **A control that suppresses a step must suppress everything downstream that DEPENDS on it.**
  The control leaves the child alive on purpose, so it also has to suppress the reap — a live
  child cannot be reaped, and a control that stopped at "don't kill" would hang or crawl to the
  terminator through a give-up path, reporting a reap failure for a reap never attempted. Ask
  what the suppressed step was supposed to *produce*, then follow the consumers.
- **A FIX APPLIED TO ONE INSTRUMENT MUST BE STATED AS A RULE, OR THE NEXT ONE ARRIVES WITHOUT
  IT.** The helper channel was given a two-sided proof — announce, then no-premature-EOF, then
  a control that keeps the holder alive — and the very next round added a *second* channel for
  the child carrying the identical unverified premise: never passed to the child, EOF at once,
  accepted as proof the child had exited. The defect and its fix were on the same page. What
  stopped it recurring was writing the requirement as a quantified rule over **every** liveness
  channel instead of as two repairs, and then noticing the rule now also demands a control for
  the child. This is `CLAUDE.md`'s first rule, and the tell is exactly as advertised: the
  justification for the helper channel's fix never mentioned helpers.
- **An IDENTIFIER is not an OBSERVATION.** Having specified that the driver waits for the direct
  child to exit, I wrote that it "takes" the child from the audit log's spawn record — which
  supplies a pid, not a death. The driver cannot `wait()` a process it did not spawn; a
  liveness probe on the pid is point-in-time and recycles; and the terminator's `child_reaped`
  is the claim the case exists to test, so leaning on it is circular. The fix is the same
  mechanism one level shallower — a second inherited pipe, scoped to the child and not passed
  to the helper, whose EOF is external evidence of the child's exit. **When a step says "wait
  for X", check that the driver can observe X at all**, rather than that it can name X.
- **Two independent reports of the same identity must be cross-checked before either is acted
  on.** The helper reports its process group; the proxy reports the child's. Believing one
  without the other lets the control pass against the wrong group entirely — a mis-grouped
  helper `READY`s, survives, gets cleaned up, and the child's actual group, the only thing
  step 4 is supposed to kill, was never under test. So the two must agree, and the child must
  satisfy `pid == pgid` under `start_new_session=True`; disagreement fails the case rather
  than being reconciled, and only the vouched-for group is ever signalled.
- **Cleanup targets come from the thing being cleaned up, never from discovery.** The control
  has to kill a group it deliberately left running, and the only non-guessing source for that
  group id is the process in it — so the helper reports its own PGID in its readiness record,
  along with a driver-supplied nonce that stops a leftover helper from answering for this run.
  Anything else is the driver deciding for itself which processes belong to the run, which is
  how the `rmtree(dirname(cwd))` fixture above deleted its own working tree. The driver also
  refuses to signal its own group or an ancestor's, for the same reason.
- **A detector is worth what its negative control is worth.** "The helper holds the descriptor
  for life" and "every ancestor closed its copy" are fixture assertions, and the previous three
  findings are all about assertions nobody made the code demonstrate. So one case **suppresses
  the group kill on purpose** and requires that the survivor be reported — the only arrangement
  that tells a channel attributing survival to the helper apart from one reporting on whatever
  ancestor still holds a copy. This is `mutate_mcp.py`'s argument applied to a fixture: an
  instrument that has never been shown failing is an instrument nobody has tested. Whatever the
  control leaves running, the driver must then clean up — a test for leaked credential-bearing
  processes that leaks one has picked the wrong side of its own point.
- **A closed key set is closed in BOTH directions, and only one of them gets tested.** The
  structural validator was specified as "every completion fact present, every value from its
  closed set" — which a validator iterating the names it already knows satisfies while ignoring
  a fact added later. Missing keys are the case everyone writes; **unrecognized keys are how the
  next field silently stops being checked**, the same drift the "no default-clean branch" rule
  exists to stop on the value side. Pin both, and pin them per key rather than once for the set.
- **A count restated in prose beside the table it counts will be wrong.** Twice in one PR: the
  section's first draft said "seven reasons" of an eleven-row table, and "three facts" of the
  four that `spawn_failed` makes inapplicable. Both were counting rows, not entries, and both
  read fine. The fix is not a more careful count, it is **not restating it** — "every
  child-and-group fact" cannot drift, and re-derives itself when the table changes. Same rule as
  the verification-block counts in §4, applied to prose.
- **A test-only hook is a verdict input, or it is a way to pass without being tested.** The
  fault-injection point that lets the driver reach the endings with no reason was specified as
  "recorded in the start record", which sounds like it closes the hole and does not: if the
  fault is armed and never fires, the trigger is clean, the outcomes are clean, and the stated
  verdict formula passes a run whose whole purpose was to fail. The **configuration** is the
  anomalous fact, not the firing — and there is a case for the no-op injection specifically,
  because the failure mode is a hook that quietly does nothing.
- **A STEP ORDER CAN ENCODE A CORRECTNESS PROPERTY, AND THE OBVIOUS IMPLEMENTATION BREAKS IT
  SILENTLY.** §10.5 says terminate the group (step 4) *before* reaping the child (step 5), which
  reads like tidiness and is not: the group is named by the child's pid, and that pid stops
  being unique the instant it is reaped, so `killpg` after `wait()` names a group the kernel may
  have reassigned. Holding the child as an unreaped **zombie** through step 4 is the whole
  mechanism. Then the constraint propagates upward in a way nothing warned about — step 3 may
  not call `wait()` either, and `os.waitid(..., WNOWAIT)`, the POSIX way to wait without
  consuming, **is not available on macOS**. **When a design fixes an order, ask what property
  the order buys and then check every call that could violate it**, because the violating
  version passes every test and differs only in which processes it signals.
- **AN ERRNO IS A MEASUREMENT, NOT A MEANING.** `killpg` returns `EPERM` on macOS for a group
  whose members are all zombies, where Linux returns 0 — and this proxy deliberately keeps a
  zombie in that group, so reading `EPERM` as failure reports `shutdown_group_kill_failed` on
  **every clean shutdown on one of the two supported platforms**. That is the false-failure half
  of §10.5's rule, and it is the half that is easy to write while feeling careful. The check is
  the same one the C3 probes exist for: before treating a return value as evidence, produce it
  on purpose and look.
- **A BOUND BELONGS TO THE STEP THAT OWNS THE WAIT, NOT TO EACH STEP THAT TOUCHES IT.** The
  drain (step 2) and the escalation (step 3) were bounded separately, so a server that merely
  needed `SIGKILL` — an outcome §10.5.1 classifies **clean** — had its drain time out first and
  came out as a teardown anomaly. Two timers over one wait always disagree about something. The
  tell is two steps asking the same question of the same descriptor.
- **NON-BLOCKING ON ONE END OF A PIPE IS NOT NON-BLOCKING.** `signal.set_wakeup_fd` requires the
  *write* end to be non-blocking and says nothing about the read end, so the sweep that collects
  signals arriving during the teardown blocked forever on an empty pipe — after every clean
  shutdown. No terminator was written, so the absence rule reported an anomaly for a run that
  had done everything right. The failure only shows on the path that reads an *empty* channel,
  which is the ordinary path; every case with something to read passed.
- **AN INSTRUMENT MUST NOT BE COUPLED TO THE THING IT OBSERVES.** The credential-bearing helper
  inherited the proxy's stderr, which is a descriptor the *driver* holds — so the driver's read
  of it blocked until the helper exited, and the helper is precisely the process the case needs
  to still be running. A liveness test that deadlocks on its subject surviving proves nothing.
  Fixed on both sides: the helper gets `DEVNULL`, and the driver gives the proxy a stderr
  **file** rather than a pipe, because anything in the group can hold a pipe open.
- **A CONTROL CAN PASS ON THE FIXTURE'S MANNERS — SECOND INSTANCE, ONE LEVEL DOWN.** The rule
  was already written for the helper ("must survive everything except a group signal"), and the
  control then failed because the *child* exited on stdin close and EOF'd its own channel. The
  suppressed steps were never under test. The fixture now closes stdout while staying alive, so
  the drain settles cleanly and the only anomalies in the record are the injected ones. Same
  quantifier lesson as the liveness channels: the rule was about one participant when it was
  about every participant.
- **THE INSTRUMENT FOR "A READ ERROR THAT IS NOT EOF" HAS TO PRODUCE AN OBSERVABLE ERROR.** Two
  obvious arrangements do not: a directory as stdin makes CPython refuse to start (`<stdin> is
  a directory, cannot continue`), so the proxy never reaches its own boundary and there is no
  log at all; a pty whose master is closed reads as **plain EOF** on macOS, which is the very
  thing the case must distinguish itself from. **Check that the failure you are injecting
  arrives at the layer you are testing**, rather than upstream of it — which is also how the
  next two candidates were rejected: a write-only *regular file* as stdin is never reported
  ready by kqueue, so the proxy blocks and the case times out, and `/dev/null` opened write-only
  makes `kevent` itself fail, so the error surfaces in the pump's catch-all rather than in the
  read handler the case is about. What works is the **write end of a pipe whose reader is
  already closed**: ready to select, `EBADF` to read, and — unlike the loopback socket with
  `SO_LINGER {1, 0}` it replaces — it needs no network, which is what made the whole file
  unrunnable under a sandbox that denies `bind()` (review, PR #103). **An instrument that needs
  a privilege is an instrument some reviewer cannot run**, and a suite nobody else can run is
  evidence nobody else can check.
- **CLASSIFYING BY THE WRONG AXIS SURVIVES UNTIL THE THIRD CASE.** `mutate_mcp.py` derived a
  mutation's class (`M` production / `I` selftest / `F` instrument) from *which suite proves
  it*, which agreed with the file's role for as long as there were two suites. The third suite
  broke it in both directions at once: `mcp_proxy_io.py` is production proven by a driver, and
  `proxy_target_server.py` is an instrument proven by the same driver. "What does this perturb?"
  and "who would notice?" are two questions, and only the first determines which total a
  mutation belongs in. A derivation that has never been wrong is not the same as a correct one.
- **AN UNTESTED PATH THAT NOBODY SAYS IS UNTESTED READS AS TESTED.** Three cleanup outcomes —
  `shutdown_read_failed`, `shutdown_reap_failed`, `shutdown_group_kill_failed` — cannot be
  arranged from outside the proxy at all, so the driver reaches them through the fault point's
  `fail` mode. That drives the **record** and not the code that would produce it in the wild.
  The driver says so in its own header, and §10.9 says so too, because "every outcome driven"
  is otherwise a claim about coverage the file does not have.
- **AN ERRNO WITH MORE THAN ONE CAUSE IS NOT EVIDENCE — the `EPERM` lesson, corrected.** §4
  already said "produce it on purpose and look", and I did: macOS returns `EPERM` from `killpg`
  for an all-zombie group where Linux returns 0. What that establishes is ONE cause, not an
  equivalence, and `kill(2)` defines the errno as the inability to signal group members — which
  a sandbox restriction or a differently credentialed descendant also produces, either of them
  leaving a live member while the branch certified success. **A measurement licenses the
  direction it measured, never the converse.** The fix is not a better errno reading but
  positive evidence: deliver the signal while the group is provably ours, then confirm
  emptiness after the reap with a signal-0 probe, whose worst outcome is a false failure.
- **DETECTION IS NOT CLEANUP.** The absence rule catches a proxy killed before its teardown and
  fails the cell — and the credential-bearing server it was fronting is still running, because
  `start_new_session=True` put it out of reach of every ancestor's group kill. Recording an
  ending is not ending it. The fix is a **guardian**: a third process in its own session
  holding a lifeline, which kills the child's group when the proxy dies.
- **CONTAINMENT THAT IS OPTIONAL, LATE, OR ANONYMOUS IS NOT CONTAINMENT — one review round, three
  faults, one fix.** The guardian above was spawned *after* the child, was "best effort" so a
  failure to start let the run continue, and signalled a bare pgid it could not show was still
  the group it created. Each looks like a separate hardening job and each was a consequence of
  the same structural choice: the guardian was a bystander. Making it the child's **parent**
  answers all three at once — it cannot be late because it is what starts the server, it cannot
  be absent because no guardian means no spawn (`spawn_failed`, fail closed), and it holds the
  group's identity because an **unreaped member pins the pgid against reuse** and only a parent
  can hold one. The general rule: when three defenses of one property each need their own
  patch, the property is being defended from the wrong place. Corollaries worth keeping:
  - **A pin, not a probe.** `killpg(pgid, 0)` succeeding says something is there, not that it is
    *yours*. Only an unreaped member establishes that, so every real signal goes through the
    process holding one — and that process stops signalling the moment it reaps, because
    releasing the pin ends its licence to act. A probe that delivers nothing is the one group
    operation that stays sound without a pin, which is why the emptiness check is signal 0.
  - **Readiness is a handshake, not a return value.** `Popen` returning says a fork succeeded.
    The guardian reports its own pid, the proxy checks it against the process it spawned, and
    the spawn record — which the reader now REQUIRES to carry it — is written only afterwards.
    An optional field whose value nothing validated stayed clean with it missing, `false`, and
    `"alive"`.
- **CLEANING UP AFTER A FAILURE DOES NOT ERASE THE EVIDENCE OF IT.** The guardian stood down
  whenever the proxy's teardown had merely **run**, on the reasoning that a group kill which was
  attempted and failed should leave its survivors for §10.9's liveness cases to observe. That
  reasoning is wrong, and the shape of the error is worth more than the case: the audit record
  already holds the failure, so the survivors were not the evidence — they were a leak with a
  justification attached. The rule is that the record carries the evidence and the mechanism
  carries the cleanup, and only an **explicitly armed test-only control** may retain a process.
  That distinction is exactly why the fault point has separate `suppress` and `fail` modes, and
  both directions are now driven: firing retains, failing sweeps.
- **A GUARANTEE MUST BE STATED AT THE WIDTH OF ITS MECHANISM.** "Nothing from this instance is
  still alive" was the claim; a process group was the mechanism; and a descendant that calls
  `setsid()` leaves the group and survives a clean run unreported. There is no portable
  containment stronger than a process group on both supported platforms, so the claim was the
  thing that had to move. The limit is now driven as a case that asserts what actually happens
  — clean verdict, `group_terminated: done`, helper alive — because **a documented boundary
  nothing checks outdates itself quietly**, and this one fails loudly if containment is widened.
- **A RULE ENFORCED AT ONE FIELD IS A RULE ABOUT THAT FIELD.** `NaN`/`Infinity` were "refused"
  only because `valid_request_id` rejects a NaN id; nothing looked anywhere else in a message,
  because Python's JSON decoder accepts all three as a documented extension. The check belongs
  at the decoder (`parse_constant`), which is the one place that quantifies over the message.
  Same shape in the framing: an empty line, undecodable bytes and a partial line at EOF were
  each skipped by a different guard written for a different purpose — `strip()`,
  `errors="replace"`, and an EOF path that discarded its buffer — and each produced a clean
  verdict for a stream that had gone wrong. **`errors="replace"` on a boundary is a rewrite,
  not a tolerance**: it forwards bytes the peer never sent.
- **"HAS SOMETHING GONE WRONG" IS NOT "DID THIS GO WRONG HERE".** The drain stopped on
  `if self.triggers`, which is always true during a teardown — there is already the trigger
  that started it — so a malformed frame arriving on the way out was recorded as an anomaly and
  then followed by more forwarding. The terminality test has to be a **count compared across
  the call**, not a truthiness test on an accumulator. The tell is a guard that reads state
  which the surrounding phase guarantees is already set.
- **A GRAMMAR IS AN ORDERING, SO CHECK IT AS ONE.** The log reader checked that the start record
  came first and nothing else, which accepted `start → terminator → spawn` and
  `start → spawn → terminator → event` as clean — a terminator that does not terminate, with
  work recorded after the record on which the absence rule, the no-heal rule and every
  completion fact rest. One rank per line kind and "the sequence is sorted" cannot disagree with
  itself about a case, where a hand-written first-record test only ever covers the end someone
  thought about.
- **AN ARCHIVED ARTIFACT MUST CARRY ENUMERATED REASONS, NOT PROSE.** The dropped-message event
  logged the decision layer's human-readable reason, which quotes the request id — an arbitrary
  wire value of any length and content the peer chose. `Drop` now carries a **code** from a
  closed set for the log and its prose for stderr, and the reader validates the code against
  that set. The tell is a field the writer formats and the reader accepts as any non-empty
  string.
- **THE OBVIOUS TYPE CHECK ADMITS THE VALUE THAT IS NOT A VALUE — third instance.** `isinstance(
  True, int)` was the first, `null` under a present-but-unread optional the second, and
  `isinstance(float("nan"), float)` the third: a timestamp reader accepted `NaN` and `Infinity`,
  which are floats and are not times. Same as the first two, the check has to name the property
  the field is *for* — finite, here — and not the type that carries it. Note the value arrives
  by two routes and both needed closing: the decoder produces it from a bare `NaN` token unless
  told not to, so the log reader now refuses those constants with the same `parse_constant` the
  wire does, imported rather than restated.
- **A TOTALITY CHECK IS BLIND TO EVERY CASE BELOW IT.** "Every reason in the enumeration was
  actually produced" sat at the end of its own section, where it quantified over the runs above
  it and nothing else. It passed the day a new trigger was added and driven two sections
  further down, and would have kept passing had the case been deleted. A fleet-wide claim goes
  **last in the file**, for the same reason a fleet-wide negative requires every row answered.
- **A MUTATION THAT REDDENS THE RECORD CHECKS WHILE THE LIVENESS CHECKS STAY GREEN IS A
  MUTATION AIMED ONE LEVEL AWAY FROM THE DEFECT.** Two arrived in one round. Removing step 4
  left the group alive in the record but not in fact, because the reap order sweeps it too;
  neutering the guardian's watch loop made it exit, which the proxy reads as `guardian_lost`
  and cleans up from. Both are the M53/M270 pattern — a property defended in two places needs
  a mutation that removes **both** — and the tell is the same each time: the intended arm is
  green and a dozen unintended ones are red.
- **A MUTATION MUST NOT BE ABLE TO DO MORE DAMAGE THAN THE DEFECT IT MODELS.** This one cost a
  developer machine, so it is written down at the length it earned.
  M289 replaced the guardian's EOF exit with `continue`. With the lifeline at EOF the descriptor
  is always readable, so the mutant's wait became a busy loop — in a process that is its own
  session leader with no parent to reap it. **24 orphans at ~35% CPU each accumulated across
  three runs, load average 186**, unnoticed until someone opened Activity Monitor. A mutant is
  broken code by construction, so the bound a mutation removes may be the process's only way
  out; that is a constraint on how mutations are WRITTEN.
  The first fix made it worse. A `ps`-scanning reaper was added to the runner to kill anything
  still executing out of the work tree — a process-killing loop in a test tool — and then, to
  prove the reaper did not over-reach, a mutation that **deleted its filter**: `kill(SIGKILL)`
  over every line of `ps ax`, which is every process the user owns. It was inert only because
  its anchor accidentally matched the mutation table rather than the function, and the next
  commit "fixed" the anchor. Both the reaper and its mutation are gone. Three rules came out of
  it, and the third is the one that matters:
  - a mutation of a loop exit must leave the process able to exit, checked by reading it before
    it is added;
  - `substring in command` matches EVERY process when the substring is empty, so any predicate
    that selects processes to kill needs its blast radius bounded before the first candidate is
    read — not as defence against a defect anyone made, but because the failure mode is
    unbounded while the purpose is narrow;
  - **when the tooling that verifies safety becomes the most dangerous code in the tree, delete
    it rather than making it safer.** The leak had already been fixed at its source; the reaper
    was insurance against a class of mistake that should not be made in the first place, and it
    bought that insurance with a `kill` loop running unattended for an hour at a time.
- **A DIAGNOSTIC MUST NEVER BE ABLE TO PREVENT THE ACTION IT DESCRIBES.** The guardian's sweep
  announced itself before signalling, and stderr is inherited from the CLI — so with that pipe
  closed the `print` raised `BrokenPipeError` and a credential-bearing process group survived,
  killed by nothing because its executioner stopped to write a log line. The reproduction was
  one function; the rule quantifies over **every** stderr write in the program, so all of them
  go through a helper that swallows `OSError` and `ValueError`, and the sweep additionally acts
  before it speaks so the ordering says what the helper guarantees. The general shape: **an
  I/O call on a channel you do not own, placed on a path that must complete.**
- **A LOUD ANOMALY DOES NOT AUTHORIZE ACTING ON AN UNCERTAIN IDENTITY.** The `guardian_lost`
  path signalled the remembered pgid after checking `getpgid(child_pid) == child_pgid` — a
  check whose own docstring admitted a reaped pid can be reused, and which for a group leader
  degenerates to "some group leader has this number". Recording the ending honestly does not
  make the signal safe: the two are independent. The path now refuses to signal and records the
  failure, and §10.6 carries the surviving server as a **limit** rather than a branch. The tell
  is a guard documented as weak and then used as though it were strong.
- **A PARTITION THE RECORD ASSERTS MUST BE A PARTITION THE CODE ENFORCES.** `spawn_failed` meant
  "no server ran", four `not_applicable` facts said so, and the audit was observably false: the
  guardian spawned the child and reported readiness in one step, so a report the proxy REJECTED
  still left a `/usr/bin/touch` child that had created its marker — 6 runs in 20. The fix is
  structural rather than defensive: two phases, and a guardian that has not been accepted is
  never told what to run. Note where the error was — not in the record, which reported what it
  could see, but in an implementation that drew the boundary one step later than the document.
- **WHEN THE END-TO-END WITNESS IS A RACE, FIND A SECOND ONE THAT IS NOT.** The marker file
  above catches a regression about one time in twenty, because the wrongly-spawned child is
  killed within a millisecond or two — a detector that misses 95% of the time is not a check,
  it is a coin. The deterministic witness was already available and one line away: have the
  process under suspicion **say which phase it reached**, on a channel the driver already reads,
  and pair the run that must stop early with one that must go further. Two rules met there — a
  claim and its subject must not share an author (the guardian reports on the proxy's
  behaviour), and an absent string proves nothing without a run in which it is present.
- **A FIELD CARRIED RAW OWES A TOTAL PREDICATE.** The audit reader's contract is that arbitrary
  decoded JSON produces a verdict and never an exception, and `[] in FROZENSET` raises
  `TypeError: unhashable type`. Every field the validator looks up in a closed set is narrowed
  to a string by the parser first — except the two carried **raw**, because for them "present
  but wrong" is a different verdict from "absent". `child_status` survived by accident:
  `is_json_int` is an `isinstance` check and so total over any value. The guardian injection was
  the same shape with a set lookup, and `"guardian": []` crashed `log_verdict`, which is the
  function that decides whether a gated cell passed — **a crash there is not a failed cell, it
  is no verdict at all**. Two things to carry forward. The rule is *the predicate must be total*,
  not *this field needs an isinstance*; and the regression arm drives an unhashable value
  through **every** membership-tested position rather than the one that broke, because what
  makes the other nine safe is a parser invariant, and an invariant nothing drives is a comment.
  The rule was already written down twice in the same tree — over `kind` in the log reader and
  in `valid_request_id` — which is what makes this a *third* instance rather than a discovery:
  a rule stated at one site is a rule about that site.
- **AN OUTCOME THAT NAMES AN ACT IS WRITTEN ONLY WHERE THE ACT HAPPENED.** `shutdown_child_killed`
  says this instance delivered a `SIGKILL`; step 3's escalation wrote it whether or not
  `_deliver` reported success. The ending that exposed it is the one whose policy is to signal
  **nothing** — guardian gone, no pin, delivery refused — so the archived log recorded a kill
  beside the two failures proving nothing was signalled. Step 4 had read that same return value
  since the day it was written. The tell is a call whose result is discarded on one path and
  consulted on another, when both paths record what the call did. Two corollaries:
  - The escalation now **ends** on the refusal rather than trying the next order — the only
    process that could signal is gone, so the second order fails identically and the message
    after the loop would then claim a kill was delivered.
  - Making step 3's loop read the return value made it **textually identical to step 4's**, so
    two mutation anchors silently became ambiguous — which is what turned up the next entry.
- **AN AMBIGUOUS MUTATION ANCHOR IS A MUTATION THAT HAS QUIETLY STOPPED TESTING WHAT IT NAMES.**
  `mutate_mcp.py` applies `original.replace(find, repl, 1)`, so an anchor matching twice still
  produces a mutant, still reddens some arm, and still prints `CAUGHT` — while perturbing
  whichever site is earlier in the file. **Five entries were in this state**, and the run was
  green throughout: M7 (the `str` redactor's loop, identical to its `bytes` twin), M184, M187,
  M217, M269. Four of them are the leading-newline lesson already recorded three bullets down —
  a 4-space anchor is a substring of the same line indented 8 — **which nothing enforced**, so
  it had been true since the day it was written down. The fix is therefore not five edits but
  the guard beside the stale-anchor check: an anchor matching a number of times other than one
  is **refused, not warned**, and a refused mutation is uncaught, so the suite exits non-zero.
  Two general points worth more than the five:
  - **A rule written in a lessons file is not enforced by having been written.** This one had
    been, verbatim, and four entries violated it anyway. If a rule is checkable, the check is
    the artifact; the prose is a comment on it.
  - **The failure mode of an instrument is not the failure mode of the code it tests.** A
    mutation suite whose count is unchanged can still have lost coverage — the sibling of
    "fewer mutations than last time" in §4's header, and the harder one to see, because the
    number that would have told you is the one that stayed the same.
  Note what the guard does **not** claim: it pins each mutation to one site, and it does not
  say the other site is covered. Four of the five siblings — the `bytes` redactor's loop,
  `parse_log`'s per-line map check, `instance_verdict`'s `anomalous`, and `_pump`'s in-loop
  signal handler — have no mutation of their own. M187's pair was merged into one tuple edit
  because `_str_or` and `_opt_str` are the same rule (M53's pattern); the other three are a
  coverage question, recorded here rather than answered.
- **A SHAPE CAPTURED FROM A WRITER IS NOT A CLAIM ABOUT A READER.** §9 probe #1 asked whether
  claude accepts the `{"type","url","headers"}` config the adapter writes. `claude mcp add
  --transport http` writes exactly that shape for itself, which is authoritative — about
  `.mcp.json`, the project-scope file. The adapter uses `--mcp-config`, **a different entry
  point**, and "they share a parser" was an assumption of precisely the kind a probe exists to
  remove. Both were measured; both agreed; the point is that agreeing was a finding rather than
  a foregone conclusion.
- **THE STATED QUESTION WAS NARROWER THAN THE ONE THAT MATTERED, and the gap held the
  credential.** Probe #1 was written as a question about JSON shape. What §8's SlideRule
  pattern actually needs is a bearer token in `headers` **arriving at the server**, and nothing
  in the probe's wording asked that — yet a token that never arrives is a broken run presenting
  as an empty result, which is the failure mode hardest to attribute. Answering it needed a
  server that could say what it received, so the probe grew a fixture. **When a probe's
  question is about a format, ask what the format is FOR, and whether that survives the trip.**
  Two more facts fell out of the same receipts at no cost — the client's `MCP-Protocol-Version`
  header confirming C3-0 from the other side, and an in-band CLI version string on a channel
  the agent's own output cannot reach — which is the ordinary return on recording everything an
  instrument sees rather than only the field under test.
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
  ~~the **decision layer** + its arms, wired to nothing (#100)~~ →
  ~~the **audit record types**, the structural validator and `verdict()`
  (`agentskill_evals/mcp_audit.py`), written before the code that produces them~~ → the
  **I/O half** (spawn, the two pumps, `SIGTERM`/`SIGINT` handlers, §10.5's shutdown, writing
  the audit log) plus a wire-level driver → the adapter integration that unlocks `tools:`.
  Everything before the last slice cannot affect any run, which is the point: this is harness
  code in the request path of every gated cell.
  **The I/O half starts from §10.5.1, written before its code**: every way an instance can end,
  on two axes — the triggers, latched in order, plus the cleanup outcomes accumulated after
  them — together with a positive completion fact per teardown step, since a list of things
  that went wrong is silent about a step that never ran at all. The verdict is a monotonic
  conjunction over all of it rather than a lookup on whatever happened last, behind a
  structural clause, since every other clause is universally quantified and an empty record
  satisfies them all.
  One total `is_clean` that every consumer reads (terminator record, per-instance verdict,
  `verify_post_run`), no default-clean branch, and phase carried in the reason rather than in a
  flag each caller applies, which dissolves the "`EPIPE` is clean in exactly one place"
  conditional into distinct reasons. Endings that no enumeration can reach — `SIGKILL`, a crash
  after the start record, a truncated terminator — are covered by one absence rule and are
  tested separately, because totality over the enum cannot see them. That enumeration exists up
  front because this is the defect shape §4 has already paid for twice on #100; the single-axis
  first draft reproduced it *in the section written to prevent it* (review, #101), which is the
  cheapest possible demonstration that it needed writing down.
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
