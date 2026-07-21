# TODO — extend version provenance to the other runners

**Status: done on `harness/version-provenance-81`.** The machinery lives in
`adapters/base.py` (`VersionProvenance`); copilot was moved onto it without changing a
byte of its messages, and codex, antigravity and claude were ported. What each adapter
could actually establish differs a lot, and the differences are the interesting part —
see "Per-runner state" below, which now records outcomes rather than intentions.

The remaining open items are at the bottom under "Still open".

---

Follow-up to the copilot work on `harness/mcp-support-design-81`. The copilot adapter now
records which CLI builds its MCP analysis has actually been checked against, reads the
version that *really ran* out of the run's own output, and tells the user when those
disagree. The other three adapters do not, and they have the same exposure.

## Why this exists

The copilot adapter was written and verified against CLI **1.0.64** on 2026-07-17. Four
days later it was running against **1.0.72** — copilot's updater rewrites its executable in
place — and nothing in the harness noticed, because provenance lived only in prose
comments. The safety argument was silently eight minor versions stale.

That is not a copilot-specific failure mode. Every adapter here encodes findings about a
CLI that ships on its own schedule and updates itself.

## The pattern to copy

From `adapters/copilot.py`:

1. **`_VERIFIED_VERSIONS` / `_VERIFIED_ON`** — provenance as queryable data, not prose. The
   dated comments stay as findings; the constant is the source of truth.
2. **`_DENIED_VERSIONS`** — builds *known* to break an assumption, refused by name. This
   tier matters because a defect can leave the runtime evidence perfectly intact (broken
   plugin masking does not disturb the MCP witness), so no runtime check would ever fire.
3. **Read the version from the run, never from a probe** (`_stream_cli_version`). A
   preflight `--version` can resolve different code than the real invocation. Read it from
   structural fields the CLI emits about itself — and *not* from anywhere model-controlled
   text can reach, or a model can forge the string that silences the warning.

   Model-controlled prose is only the obvious half. Review found the subtler one: the
   *same* structural event also carried `source: "project"` skill paths, which are
   **workspace-controlled**. A repo laid out as `.agents/skills/pkg/x/9.9.9/SKILL.md`
   injected a second version, and because disagreement resolves to "unknown", that alone
   silently disarmed the denylist — from inside the workspace under test. Whatever field
   a runner exposes, filter it to the entries the CLI vouches for itself, and check what
   *else* rides in on the same event. The workspace does not get a vote on which build
   the harness thinks it ran.
4. **Three tiers**: contract violation → fail; denylisted version → fail; unrecognized
   version → warn once per process, run proceeds.

   **Say which of those are prevention and which are detection.** None of them are
   prevention. The version is only legible in the run's own output, so the denylist fires
   after the CLI has finished — a denylisted build has already done whatever it does, and
   what the tier actually buys is that the result never counts. Review read the original
   wording ("the run is refused by version") as a claim that the build could not run at
   all. Wherever a tier acts later than a reader would assume, the message has to say so.
5. **`verify-copilot-channels`** — audits an installed bundle against the inventory of
   discovery channels the adapter neutralizes, so clearing a new build is a minute rather
   than an afternoon.

## Per-runner state — outcomes

Versions below are what was installed on the dev host on 2026-07-21.

The headline result: **only two of the four CLIs state the executing version anywhere the
harness may legitimately read it.** That was the open question going in, and the answer
shaped the design more than anything else — see "A CLI that cannot state its version"
under Design notes.

### `codex` — version is UNREADABLE, by a trade-off this harness chose

Settled by experiment, not inference:

- `codex exec --json` names no version anywhere. A complete successful run emits exactly
  four events — `thread.started` (thread_id only), `turn.started`, `item.completed`,
  `turn.completed` — and none carries one.
- codex *does* record it, in the rollout file's `session_meta.cli_version`, keyed by an
  `id` equal to the stream's `thread_id`. That would have qualified: the rollout is written
  by the run being judged, so it is the same evidence class as the MCP witness, and the
  adapter already has precedent for reading an id-keyed side-channel.
- **But `--ephemeral` suppresses the rollout entirely**, and this adapter passes
  `--ephemeral` deliberately, for isolation. Verified both directions: with it, no rollout
  exists for the run's thread_id; without it, one appears carrying `cli_version 0.140.0`.

So the version is purchasable only by giving up the isolation `--ephemeral` buys — the
wrong trade, since hermeticity is the property under test and provenance is only the audit
trail for it. Recorded as `unreadable`, and the warning names `--ephemeral` explicitly so
the next reader can re-take the decision rather than rediscover it.

Installed 0.140.0 matches the 7 pinned findings, so there is no drift today.

### `antigravity` (`agy`) — drift confirmed and now *visible*, version unreadable

- The self-contradicting header is gone. It claimed 1.0.16 while the body cited 1.1.1;
  it now points at the constant and says why a single header line could only ever be
  wrong about most of the findings.
- `_VERIFIED_VERSIONS = ("1.1.1",)` — the build the customization-root and plugin
  `mcp_config.json` channel inventory was actually established against with live sentinel
  servers. **Deliberately not 1.1.2**, which is installed: the invocation contract was
  re-checked there and holds (`--help` still documents `-p`/`--print`, `--add-dir`,
  `--dangerously-skip-permissions`, `--model`), but the channel inventory was *not*
  re-established. Listing 1.1.2 because it "seems fine" is precisely the constant blessing
  an unknown state. So agy runs warn — correctly. The drift is real and un-re-verified,
  and the warning is what keeps it visible.
- Version unreadable: neither the `--output-format json` result object nor the
  `transcript_full.jsonl` the adapter already reads states a version.

### `claude` — the clean case

- `system`/`init` carries **`claude_code_version`** as a first-class scalar the CLI writes
  about itself. Nothing to reconstruct, nothing for a model or a workspace to forge — the
  opposite of copilot's situation, where the version has to be recovered from skill paths.
- `_VERIFIED_VERSIONS = ("2.1.113",)`, and three things were actually checked before
  writing it: `--strict-mcp-config` still exists and still reads "Only use MCP servers
  from --mcp-config, ignoring all other MCP configurations"; six captured 2.1.113 runs all
  report `mcp_servers: []`; and the `result` events of those same runs carry no key outside
  `_KNOWN_RESULT_KEYS`.
- It gained a `verify_post_run` it did not have: the init event's `mcp_servers` list is a
  genuine runtime witness that `--strict-mcp-config` still governs every server source, so
  a non-empty list now fails the run and a reshaped/absent field fails closed. Claude was
  the one adapter with a version-independent kill switch and *no* post-run evidence at all.

## Design notes before starting

- **Lift the shared machinery into `adapters/base.py`.** Tiers, the warn-once set, and the
  message template are adapter-independent; only the version *source* and the channel
  inventory are per-CLI. Doing copilot's a second time by copy-paste would be the wrong
  move.
- **Warn-once must be keyed per adapter**, not globally, or a mixed-runner matrix silences
  every runner after the first one warns.
- **Do not gate on a version learned by executing the CLI.** Standing rule on this branch:
  a fact learned by running a program the agent independently runs again may not *clear* a
  security decision. Version telemetry may warn; only the runtime contract may pass a run.
- **Never let the notice overstate its own evidence.** The drift warning used to say "the
  runtime witness held" unconditionally. But a run that did not complete normally is
  *excused* from producing a witness, so the sentence was reached with no MCP evidence at
  all — inventing the very check it was warning about. `_warn_cli_version_drift` now takes
  `witnessed`. Any port of this needs the same split: a security notice a reader would
  quote to justify shipping has to be true on every path that prints it.

- **Telemetry parsing must not raise.** These helpers run inside `verify_post_run`, where
  anything raised is reported as an MCP hermeticity failure. A mistyped `data.skills` is
  not one — malformed telemetry is an *unknown version*, which warns.

- **An unscanned location reads exactly like a cleared one.** The bundle audit missed
  `$XDG_CACHE_HOME`, `%LOCALAPPDATA%`, and prerelease directory names the loader accepts,
  and reported nothing about them — indistinguishable in the output from having checked
  them. Err toward scanning roots that may not exist.

- **Take the loader's rules from the loader, not from the spec it resembles.** The audit
  filtered candidate directories to well-formed semver, which is *stricter* than copilot:
  its loader keeps every directory with a readable `app.js` — no name test at all — and
  orders them by a leading `\d+.\d+.\d+` **prefix**. So `1.0.73foo` and `1.0.73-` outrank
  the running build and were being skipped. Two scans do this, on different filenames
  (`sea-loader.js` finds `index.js`, which then re-scans for `app.js`), and reading them
  is a ten-minute job that no amount of reasoning about semver substitutes for. Every
  runner here ships its bundle as readable JS; read it.

- **Anchor a version extracted from a path on something structural.** `pkg/<plat>/<ver>/`
  can occur on either side of the real app root — above it because a cache root is an
  ordinary caller-supplied directory, below it because a skill directory can be named
  anything — so neither "first match" nor "deepest match" is right. The fix is to anchor
  on a segment the CLI's own layout guarantees (`.../<ver>/builtin/`). Worth writing down
  because the *test* for this is easy to get wrong too: a decoy above the root is caught
  by taking the last match, so it proves nothing about the anchor. It takes a decoy on
  each side to pin the real rule.

- **Nothing in an adapter's argv is load-bearing if an env var overrides it.** Re-reading
  copilot's loader to check the `--no-auto-update` claim turned up `COPILOT_CLI_DIST_DIR`,
  which is consulted before any argv, imports `<value>/index.js` with no version floor and
  no cache-root constraint, and would run arbitrary code as the agent while provenance
  cheerfully described whatever it found. Any port should grep its loader for
  `process.env` and account for every hit that steers code resolution.

- **Be honest about the ceiling.** None of this detects a channel a new build *added* — no
  marker exists for code nobody has written yet. It converts silent drift into dated,
  actionable drift and makes "should a human re-read this bundle?" a decision someone
  actually takes, rather than one taken by default.

- **A CLI that cannot state its version cannot have a denylist — and the failure is
  silent.** This is the one design constraint that emerged from the port rather than from
  review. `check_denied` is only ever reached with a version read from the run, so on an
  adapter with no version source it is reached with `None` on *every* run and every entry
  quietly fails to match. Nothing distinguishes that from a denylist with nothing in it,
  so whoever adds the entry gets no signal that they have added a control which does not
  control anything. `VersionProvenance.__post_init__` therefore refuses at import time to
  hold `denied` alongside `unreadable`. The general lesson: when a safety table can become
  dead code depending on a *different* field, make the combination unconstructible rather
  than documenting the hazard.

- **Distinguish "unknown build" from "unknowable build" in the message.** Both are tier 3,
  but they call for different reader actions and the drift wording actively misleads on the
  second: "the version could not be determined" invites someone to go and look for it, and
  for codex and agy there is nothing to find, ever. The `unreadable` notice says what is
  unknowable and *why*, and it deliberately claims no runtime evidence even when called
  with `witnessed=True` — it is a statement about an unidentifiable build, not about this
  run's hermeticity, and conflating the two is how a notice ends up overstating itself.

- **Mutation testing caught two decorative arms again — including the exact defect this
  file already warns about.** Third time on this branch. One arm passed `witnessed=True`
  unconditionally with nothing asserting the unwitnessed path, which is the "notice
  overstates its own evidence" failure recorded above, reintroduced while porting the
  lesson that describes it. Writing a rule down does not stop you from breaking it in the
  next file; an arm that goes red does. The other was a mutation that *crashed* rather than
  failing an arm — a red signal, but the wrong one, and it hid the fact that the arm never
  proved the fail-closed behaviour it claimed to.

## Still open

- **agy has a candidate in-band version source that is not yet usable.**
  `~/.gemini/antigravity-cli/cli.log` opens with a `Language server version: <v>` banner
  the run itself writes. Two things would have to be settled before it could be read:
  it sits at a fixed shared path, so attributing a line to *this* run depends on the
  isolated HOME actually containing it rather than the real one (unverified); and it is a
  free-form log the agent's own activity also writes into, so a naive scan reads a channel
  model-controlled text can reach — the forgery hazard copilot had to design around.
- **codex has no post-run config re-check.** Its MCP verification is entirely pre-launch
  (`_verify_all_mcp_disabled` from `build_argv`). Copilot re-enumerates *after* the run to
  close the launch-window race, where a server added between argv construction and startup
  loads un-disabled. codex is exposed to the same race and does not check for it. This is
  independent of provenance and probably wants its own change.
- The two `DESIGN_MCP_Support.md` §9 probes (claude `--allowedTools` gating, codex TOML
  array/inline-table values via `-c`) remain deferred, then Phase 1.
