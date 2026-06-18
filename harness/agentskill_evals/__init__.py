"""agentskill_evals — a cross-agent skill evaluation harness.

Run the same skill/eval against multiple coding-agent CLIs (Claude Code, Codex,
AntiGravity, ...) and assert on the result with a common set of checks
(filesystem state, tool-call trace, final-output schema, LLM-as-judge).

Each agent CLI is wrapped by an *adapter* that knows how to (a) build the CLI
invocation for a prompt and (b) translate that agent's raw output into a single
`NormalizedEvent`/`RunResult` shape. Everything downstream — assertions, the
judge, the runner, reports — works against that normalized shape, so adding a
new agent is one small adapter, not a rewrite.
"""

__version__ = "0.1.0"
