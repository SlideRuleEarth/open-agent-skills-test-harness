# TODO — extend version provenance to the other runners

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

## Per-runner state

Versions below are what was installed on the dev host on 2026-07-21.

### `codex` — highest priority

- 7 findings pinned to `0.140.0`; installed **0.140.0**. No drift *today*, which is exactly
  when this is cheap to add.
- Has a real post-run verifier (`codex.mcp_post_verify_fails_closed`) and enumerates MCP
  servers by executing `codex mcp list --json`, so its safety argument is at least as
  version-dependent as copilot's.
- Open question: does the `codex exec --json` stream expose the executing version? If not,
  provenance still helps, but tier 3 has to warn on "undeterminable".

### `antigravity` (`agy`) — has drift *now*

- Header says "Verified against agy **1.0.16**" while later comments cite **1.1.1** — the
  file already disagrees with itself, which is the prose-rot failure in miniature.
- Installed is **1.1.2**, so it is ahead of every claim in the file.
- Four customization roots and a plugin MCP-config channel, all verified at different
  times. Reconcile the header against the real inventory before adding the constant, or the
  constant just blesses an unknown state.

### `claude` — lowest priority

- No version claims at all, and materially less exposed: it uses the flag-level kill switch
  `--strict-mcp-config` rather than enumerating channels, so there is no per-version channel
  inventory to go stale.
- Still worth a `_VERIFIED_VERSIONS` for the *parser* contract. Installed **2.1.113**.
- The thing to actually check: whether `--strict-mcp-config` still exists and still means
  what it meant. That single flag carries the whole argument, so it is the one marker worth
  auditing.

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
