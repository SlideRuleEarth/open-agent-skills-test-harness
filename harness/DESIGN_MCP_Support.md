# Design: MCP Server Support in the Agent-Skills Test Harness

**Status: draft for discussion** · CLI versions verified against: claude 2.1.113 · codex 0.140.0 · copilot 1.0.64 · agy 1.1.1

## 1. Context and goals

We are developing a custom MCP server (SlideRule data access). The harness needs to:

1. **Let scenarios exercise MCP servers** — declare a server (local stdio process or remote HTTP/SSE endpoint), have the agent under test call its tools, and assert on that usage.
2. **Control access** — MCP stays hermetically OFF by default; only scenarios that explicitly declare servers get them, optionally restricted to specific tools. Credentials (API keys, bearer tokens) come from environment variables at run time and never appear in scenario YAML or archived artifacts.

All four adapters (claude, codex, copilot, antigravity) are in scope, with a phased rollout grounded in what each CLI can actually do.

### 1.1 Current state — and a hermeticity bug

MCP is intentionally disabled today, but only the Claude adapter actually enforces it:

- **claude** — hermetic. `--strict-mcp-config` is in `_HERMETIC` (`adapters/claude.py:63-68`) with no `--mcp-config`, so no servers load.
- **codex** — **leaks**. The isolated HOME symlinks `~/.codex` wholesale (`isolation.py:130-149`), so any `[mcp_servers.*]` in the user's real `config.toml` loads in every run.
- **copilot** — **leaks**. `--disable-builtin-mcps` (`adapters/copilot.py:73`) only disables the bundled GitHub server; the user's real `~/.copilot/mcp-config.json` still loads through the symlinked HOME.
- **antigravity** — **leaks** (latently). `<iso_home>/.gemini/config/mcp_config.json` is a symlink to the real file; benign today only because that file happens to be empty.

Fixing this is Phase 0 and is worth doing regardless of the rest of this design.

## 2. Per-CLI MCP capability survey

Everything marked **(verified)** was checked against the installed CLI's help output or embedded docs; **(inferred)** items are documented behavior not confirmable from `--help` and carry a verification task (§9).

### claude 2.1.113
- `--mcp-config <configs...>` — loads servers from JSON files or inline strings (verified). Paired with the already-passed `--strict-mcp-config` ("only use MCP servers from --mcp-config"), declared servers become the *only* servers — hermetic opt-in for free.
- Tool gating: `--allowedTools` / `--disallowedTools`; MCP tools are named `mcp__<server>__<tool>`; `mcp__<server>` allows the whole server (flags verified; naming inferred).
- Transports: stdio (`command`/`args`/`env`) and remote (`"type": "http"|"sse"`, `url`, `headers`) in the `mcpServers` JSON (inferred).
- Caveat: the harness runs with `--dangerously-skip-permissions` (`adapters/claude.py:100-101`); `--allowedTools` is a *permission* allowlist and is likely moot under it (inferred — see §6 decision C2).

### codex 0.140.0
- No MCP flag on `exec`; the mechanism is `-c key=value` dotted-path TOML overrides, valid on `exec` (verified).
- Config vocabulary (verified via `codex mcp add --help` and binary strings): stdio `command`/`args`/`env`; remote `url` (streamable HTTP) with `bearer_token_env_var`, `http_headers`, `env_http_headers` (header value taken from a named env var); `enabled_tools`/`disabled_tools`; `startup_timeout_sec`/`tool_timeout_sec`.
- `bearer_token_env_var` is notable: the secret never appears in argv or on disk — only the env-var *name* does.
- Gap: no legacy SSE support surfaced anywhere (inferred gap).
- The harness parser already normalizes `mcp_tool_call` items into TOOL_CALL events (`adapters/codex.py:168-195`).

### copilot 1.0.64
- `--additional-mcp-config <json>` — JSON string or `@file`, repeatable, augments `~/.copilot/mcp-config.json` for the session (verified).
- Per-server vocabulary via `copilot mcp add`: `--transport stdio|http|sse` (all three), `--env`, `--header`, `--timeout`, and per-server `--tools "*"|list|""` filter (verified).
- Also useful: `--secret-env-vars <names>` redacts those env values from output (verified); `COPILOT_HOME` overrides `~/.copilot` (verified); `--disable-mcp-server <name>`, `--allow-tool`/`--deny-tool` with `MyMCP(tool)` syntax (verified).
- No "ignore user mcp-config" flag exists — hermeticity must come from masking the file in the isolated HOME.

### antigravity (agy) 1.1.1
- No MCP flags at all (verified absence). Discovery is purely file-based: `~/.gemini/config/mcp_config.json` — `{"mcpServers": {name: {command, args, env}}}` for stdio, `serverUrl` (SSE) or `url` for remote (verified via embedded docs + changelog).
- **No tool-allowlist mechanism** (verified absence) — documented gap.
- Transcript tool names look like `mcp_<server>_<tool>` with single underscores (inferred from binary strings).

## 3. Scenario schema extension

New optional top-level field on evals and scenarios. Absent ⇒ MCP hermetically off (the default today, post-Phase-0 for all adapters).

```yaml
mcp_servers:                              # dict[str, server]; key = server name = tool namespace
  sliderule:                              # → tools surface as mcp__sliderule__<tool>
    command: python3                      # stdio transport (XOR with url)
    args: [fixtures/sliderule_mcp.py]     # relative paths resolve against the scenario file, then absolutized
    env:
      SLIDERULE_DOMAIN: slideruleearth.io
      SLIDERULE_API_KEY: ${SLIDERULE_MCP_TOKEN}    # interpolated from harness process env
    tools: [atl06p, atl03x]               # optional allowlist; omit ⇒ all tools

  sliderule-remote:
    url: https://sliderule.example.com/mcp     # remote transport (XOR with command)
    transport: sse                        # http (default) | sse
    headers:
      Authorization: Bearer ${SLIDERULE_MCP_TOKEN}
```

Validation rules (added to `validate_spec`, `spec.py:237-395`, so misconfiguration fails before any tokens are spent):
- Exactly one of `command` / `url` per server; unknown keys → error.
- `${VAR}` interpolation in `env` values, `headers` values, and `url`, resolved from the harness process environment at load time; unresolvable → error naming the variable.
- Every interpolated value is registered in a per-run **secrets set** (used for artifact redaction, §5.3).
- Relative `command`/`args` paths resolve via the spec's `base_dir()` (same convention as `files:`, `spec.py:159-182`) and are absolutized — the agent runs in a tempdir workspace, so relative paths would otherwise break.
- `tools:` on an antigravity target → warning ("advisory only"); `mcp_servers` + `isolated: false` on an antigravity target → error (§4, agy).

### Plumbing (mirrors the existing `output_schema` / `env` pattern)

1. `EvalSpec.mcp_servers: dict` — parsed in `_spec_from_raw` (`spec.py:433-500`).
2. `RunOptions.mcp_servers: Optional[dict]` and `RunOptions.mcp_scratch_dir: Optional[str]` (`adapters/base.py:27-45`).
3. Runner populates both per cell (`runner.py:345-352`). The scratch dir is a per-cell `ase-mcp-` tempdir created alongside the isolated home and deleted in the same `finally` (`runner.py:359-361`). It is deliberately **outside the workspace**: the workspace is relocated into `artifacts/**/workspace` and inlined into `report.md` (`runner.py:413-415`, `render_report`), which would archive secret-bearing config files.
4. Each adapter's `build_argv` / `env` consumes them (§4).

## 4. Per-adapter mapping

| adapter | config delivery | kill-switch (default, no `mcp_servers`) | tool allowlist | remote transports |
|---|---|---|---|---|
| claude | `--mcp-config <scratch>/mcp.json` (keep `--strict-mcp-config`) | already hermetic | `--allowedTools mcp__s__t,…` (advisory, §6-C2) | http + sse (inferred) |
| codex | `-c mcp_servers.<n>.<key>=…` argv overrides | add `-c mcp_servers={}` | `-c mcp_servers.<n>.enabled_tools=[…]` | streamable HTTP; no SSE |
| copilot | `--additional-mcp-config @<scratch>/mcp-config.json` | isolation config-file mask of `~/.copilot/mcp-config.json` | per-server `tools` in config | http + sse (verified) |
| antigravity | write real file `<iso_home>/.gemini/config/mcp_config.json` | mask same file to `{}` | none — warn | SSE `serverUrl` / `url` |

Details:

- **claude** (`adapters/claude.py:90-113`): when `opts.mcp_servers` is set, write `{"mcpServers": …}` to `<scratch>/mcp.json` and append `["--mcp-config", path]`. A *file*, not inline JSON — argv is recorded in `result.json` (`exec.py:49`, `runner.py:495`), so inline JSON with resolved secrets would land in artifacts. Per-server `tools:` compiles to `--allowedTools` entries.
- **codex** (`adapters/codex.py:86-104`): with no servers declared, always add `-c mcp_servers={}` (closes the Phase-0 leak). With servers: `-c mcp_servers.sliderule.command="python3"`, `-c 'mcp_servers.sliderule.args=[…]'`, `-c 'mcp_servers.sliderule.env={…}'`, `-c 'mcp_servers.sliderule.enabled_tools=[…]'`; remote: `url` + `bearer_token_env_var` / `env_http_headers` so HTTP secrets never materialize. Stdio `env` literals do pass through argv → covered by the redaction pass (§5.3). Writing a per-run `$CODEX_HOME/config.toml` was considered and rejected for Phase 1 (§6-B).
- **copilot** (`adapters/copilot.py:99-110`): `--additional-mcp-config @<scratch>/mcp-config.json` with per-server `tools`, transport, `env`, `headers`; add `--secret-env-vars <names>` as belt-and-braces. Kill-switch: no flag exists, so mask the user file via the isolation mechanism below; non-isolated fallback: parse the real file and pass `--disable-mcp-server <name>` per entry.
- **antigravity** (`adapters/antigravity.py:147-159`): only injection point is the discovery file. Under isolation, `.gemini/config/` is already a real directory in the overlay (it's an ancestor of the skills leaf), so the harness replaces the `mcp_config.json` symlink with a real per-run file. Secrets live only in the isolated-home tempdir, deleted right after execution and never archived. Consequence: **MCP on agy requires isolation ON** (the default); combining it with `isolated: false` is a validation error.

### Isolation-layer generalization

Extend `build_isolated_home` (`isolation.py:73-114`) with a third leaf type — a **config-file mask**: adapter-declared HOME-relative file paths materialized as real files (with supplied content, or `{}` to neutralize) instead of symlinked. E.g. `isolation_config_masks = [".copilot/mcp-config.json"]`, `[".gemini/config/mcp_config.json"]`. One mechanism serves both the Phase 0 hermeticity fix and the copilot/agy injection path.

## 5. Access control

### 5.1 Opt-in (server level)
Default stays hermetic via the kill-switch column in §4. When `mcp_servers` is declared, only those servers are reachable: claude's `--strict-mcp-config`, codex's full-table override, copilot/agy's masked user config.

### 5.2 Tool gating (tool level)
Per-server `tools:` compiles to each CLI's native filter — codex `enabled_tools` and copilot per-server `tools` are hard filters (verified). Claude: `--allowedTools` is likely advisory under `--dangerously-skip-permissions`; Phase 1 accepts advisory gating (assertions still catch off-limits usage), with a documented upgrade path (§6-C2) if hard enforcement matters. agy: no mechanism; validation warns.

### 5.3 Credentials
- `${VAR}` interpolation from the harness process env only; fail-fast at validation; values never committed in YAML.
- Prefer CLI-native env-var **indirection** where it exists (codex `bearer_token_env_var`, `env_http_headers`) — the value then never touches disk or argv at all.
- Scratch config files created `0600`, outside the workspace, deleted post-run.
- **Redaction pass**: every interpolated secret value is scrubbed from `stdout.jsonl`, `stderr.txt`, `events.json`, `result.json` (including recorded `argv`), and `report.md` in `_write_artifacts` / `render_report` (`runner.py:488-495`, `runner.py:766`). This also covers the case no CLI flag can: a tool *result* echoing a token back into the transcript.

## 6. Decision points

**A. Config delivery for claude/copilot: inline argv JSON vs scratch file vs workspace `.mcp.json`.**
Inline JSON leaks resolved secrets into `result.json` (argv is archived) and needs shell-safe quoting. Workspace files are archived and inlined into `report.md`; claude ignores workspace `.mcp.json` under `--strict-mcp-config` anyway. → **Scratch file** in the per-cell tempdir, deleted post-run.

**B. Codex delivery: `-c` overrides vs materialized `$CODEX_HOME/config.toml`.**
`-c` needs no isolation surgery, keeps HTTP secrets env-indirect natively, and its one exposure (stdio env literals in argv) is covered by redaction. A materialized config home means converting `.codex` from a wholesale symlink into a masked dir with a merged TOML (preserving auth) — heavier and riskier. → **`-c` overrides**, with config-home materialization as the fallback if TOML array/inline-table `-c` values prove unreliable (verification task).

**C1. Allowlist shape: flat top-level `allowed_tools: [mcp__sliderule__*]` vs per-server `tools:`.**
Flat matches claude's flag but forces claude's naming onto codex/copilot (whose native filters take bare tool names per server) and repeats server names. → **Per-server `tools:`**; adapters compile to their native form. A flat form can be added later without breakage.

**C2. Claude gating strength: advisory vs hard.**
(a) Advisory — keep `--dangerously-skip-permissions`, pass `--allowedTools` anyway, rely on assertions to flag violations. (b) Hard — for gated MCP scenarios, swap to `--permission-mode dontAsk` + an explicit `--allowedTools` covering built-ins plus declared MCP tools. (c) `--disallowedTools` deny-listing — requires knowing the server's full tool list; not statically available. → **(a) for Phase 1** with an explicit verification task; document (b) as the upgrade.

**D. Where secrets interpolate: harness at load time vs CLI-native expansion.**
CLI-native keeps values off disk but is inconsistent (copilot/agy have nothing). → **Hybrid**: harness interpolation as the uniform contract + fail-fast validation; adapters use native indirection where available (codex); redaction pass regardless.

## 7. Testing and assertion story

- `used_tool` (`assertions.py:217-224`) matches normalized tool names case-insensitively, so it works as soon as parsers emit stable MCP names. Parser state: **claude** passes `tool_use` names through untouched — MCP names should arrive as `mcp__<server>__<tool>` (verify); **codex** currently emits `item["tool"]` alone (`codex.py:175`) — change to canonical `mcp__{server}__{tool}` when both fields are present; **copilot** MCP naming unverified; **agy** likely `mcp_<server>_<tool>` (verify).
- New assertion **`used_mcp_tool {server, tool?}`** matching all four naming conventions (`mcp__s__t`, `mcp_s_t`, `s(t)`, bare `t` with adapter context), so scenarios stay portable across runners. `validate_spec` errors when it references a server not declared in `mcp_servers` (mirroring the `skill_triggered` pattern). Optionally extend `used_tool` with a `matches:` regex for wildcards.
- **Fixture**: `harness/fixtures/echo_mcp_server.py` — a zero-dependency Python stdio MCP server (JSON-RPC 2.0 over stdin/stdout: `initialize`, `tools/list`, `tools/call`; tools `echo` and `add`; ~100 lines). Used for (i) offline parser goldens in `selftest.py` (like the existing `CODEX_EXTRA` sample that already covers `mcp_tool_call`), and (ii) a live smoke scenario `scenarios/mcp_echo_smoke.yaml` asserting `used_mcp_tool {server: echo, tool: echo}` + `final_contains`. CI never depends on the real SlideRule server.
- A documented example scenario shows the SlideRule pattern: remote `url` + `Authorization: Bearer ${SLIDERULE_MCP_TOKEN}` + `tools:` allowlist + rubric/assertions on the returned data.

## 8. Phasing

- **Phase 0 — hermetic hardening** (no schema change; fixes today's leaks): codex `-c mcp_servers={}`; isolation config-file masks for copilot `~/.copilot/mcp-config.json` and agy `.gemini/config/mcp_config.json`; selftest coverage.
- **Phase 1 — schema + claude + codex**: `mcp_servers:` parse/validate/interpolate, secrets registry + redaction pass, scratch-dir plumbing, claude `--mcp-config` file, codex `-c` mapping + canonical tool naming in its parser, echo fixture + goldens + smoke scenario. Both CLIs' mechanisms fully verified; codex parsing is half-done already.
- **Phase 2 — copilot**: `--additional-mcp-config @file`, per-server `tools`, `--secret-env-vars`, verify MCP tool naming in its JSON events, parser tweak if needed.
- **Phase 3 — antigravity**: config materialization on the Phase-0 mask mechanism, `serverUrl`/`url` support, tool-name normalization; document the gaps (no tool gating; isolation required).

## 9. Open verification tasks

1. claude: `mcpServers` http/sse JSON shape accepted by `--mcp-config`; whether `--allowedTools` gates MCP tools under `--dangerously-skip-permissions`.
2. codex: `-c` override ordering (does per-key config beat an earlier `mcp_servers={}`? if not, emit the empty table only when no servers are declared); TOML array / inline-table values via `-c`.
3. copilot: exact JSON key spelling in `mcp-config.json` (capture via `copilot mcp add` in a throwaway `COPILOT_HOME`); MCP tool-name format in its JSON events.
4. agy: transcript tool-name format; `url` vs `serverUrl` for streamable HTTP vs SSE.
