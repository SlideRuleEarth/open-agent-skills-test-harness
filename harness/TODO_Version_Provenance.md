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
4. **Three tiers**: contract violation → fail; denylisted version → fail; unrecognized
   version → warn once per process, run proceeds.
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
- **Be honest about the ceiling.** None of this detects a channel a new build *added* — no
  marker exists for code nobody has written yet. It converts silent drift into dated,
  actionable drift and makes "should a human re-read this bundle?" a decision someone
  actually takes, rather than one taken by default.
