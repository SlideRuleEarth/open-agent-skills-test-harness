"""The common shape every adapter normalizes its agent's output into.

This is the contract that decouples assertions/judge/runner from any specific
agent CLI. An adapter's only hard job is: given raw stdout/stderr/exit-code,
produce a list of `NormalizedEvent` plus a `RunResult`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class EventKind(str, Enum):
    """Agent-agnostic event categories.

    Adapters map their native event/item types onto these. Not every agent
    emits every kind; tool-trace assertions degrade gracefully when an agent's
    output format doesn't expose tool calls (see the AntiGravity adapter).
    """

    SESSION_START = "session_start"  # init / session metadata
    REASONING = "reasoning"          # model thinking / chain-of-thought summaries
    AGENT_MESSAGE = "agent_message"  # assistant text shown to a user
    TOOL_CALL = "tool_call"          # the agent invoked a tool (shell/edit/etc.)
    TOOL_RESULT = "tool_result"      # output returned from a tool
    FILE_CHANGE = "file_change"      # a file was created/modified/deleted
    RESULT = "result"               # terminal event with the final answer
    ERROR = "error"                  # error / non-fatal warning
    OTHER = "other"                  # recognized JSON we didn't map


@dataclass
class NormalizedEvent:
    """A single event from an agent run, normalized across CLIs."""

    kind: EventKind
    raw: dict[str, Any] = field(default_factory=dict)
    text: Optional[str] = None        # message / reasoning / result text
    tool_name: Optional[str] = None   # e.g. "Bash", "shell", "edit", "run_command"
    command: Optional[str] = None      # shell command string, when applicable
    path: Optional[str] = None         # file path for file-change / file tool calls
    is_error: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "text": self.text,
            "tool_name": self.tool_name,
            "command": self.command,
            "path": self.path,
            "is_error": self.is_error,
        }


@dataclass
class RunResult:
    """The outcome of running one eval against one agent."""

    agent: str
    eval_name: str
    prompt: str
    workdir: str
    argv: list[str] = field(default_factory=list)
    exit_code: int = -1
    events: list[NormalizedEvent] = field(default_factory=list)
    final_text: str = ""
    structured_output: Optional[Any] = None  # parsed final JSON, when a schema was requested
    cost_usd: Optional[float] = None
    premium_requests: Optional[float] = None
    duration_ms: Optional[int] = None
    resolved_model: Optional[str] = None  # actual model used (when adapter can detect it)
    stdout_path: Optional[str] = None
    stderr_path: Optional[str] = None
    timed_out: bool = False
    error: Optional[str] = None  # harness-level error (binary missing, crash, timeout)

    @property
    def cost_str(self) -> str:
        parts = []
        if self.cost_usd is not None:
            parts.append(f"${self.cost_usd:.4f}")
        if self.premium_requests is not None:
            parts.append(f"{self.premium_requests}req")
        return " / ".join(parts)

    # --- convenience views used by assertions -------------------------------

    def tool_calls(self) -> list[NormalizedEvent]:
        return [e for e in self.events if e.kind == EventKind.TOOL_CALL]

    def commands(self) -> list[str]:
        """Every shell command string the agent ran (across tool calls)."""
        out: list[str] = []
        for e in self.events:
            if e.command:
                out.append(e.command)
        return out

    def tool_names(self) -> list[str]:
        return [e.tool_name for e in self.events if e.tool_name]

    def file_paths_touched(self) -> list[str]:
        out: list[str] = []
        for e in self.events:
            if e.path and e.kind in (EventKind.FILE_CHANGE, EventKind.TOOL_CALL):
                out.append(e.path)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "eval_name": self.eval_name,
            "prompt": self.prompt,
            "workdir": self.workdir,
            "argv": self.argv,
            "exit_code": self.exit_code,
            "final_text": self.final_text,
            "structured_output": self.structured_output,
            "cost_usd": self.cost_usd,
            "premium_requests": self.premium_requests,
            "duration_ms": self.duration_ms,
            "resolved_model": self.resolved_model,
            "timed_out": self.timed_out,
            "error": self.error,
            "n_events": len(self.events),
            "commands": self.commands(),
            "tool_names": self.tool_names(),
        }
