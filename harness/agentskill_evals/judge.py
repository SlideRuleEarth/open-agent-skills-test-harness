"""LLM-as-judge.

The judge is itself one of the agent adapters (default: claude) run in a
throwaway workspace, tool-free, with a JSON-Schema-constrained verdict. It is
handed a compact transcript of the graded run — the user prompt, the agent's
final answer, the commands/tools it ran, and the resulting file tree — and
scores each rubric behavior pass/fail with a reason.

Because the judge rides on the same adapter machinery, you can grade with any
agent (`--judge-agent codex`), not just Claude.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Optional

from .adapters import get_adapter
from .adapters.base import RunOptions
from .exec import execute
from .schema import RunResult

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "behavior": {"type": "string"},
                    "pass": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["behavior", "pass", "reason"],
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["items", "summary"],
}

_MAX_FILES = 60
_MAX_INLINE_BYTES = 1500
_TEXT_EXT = {".md", ".txt", ".py", ".json", ".yaml", ".yml", ".toml", ".cfg",
             ".ini", ".js", ".ts", ".html", ".css", ".sh", ".csv"}


class Judge:
    def __init__(self, agent: str = "claude", model: Optional[str] = None, timeout: int = 240):
        self.adapter = get_adapter(agent)
        self.agent = self.adapter.name
        self.model = model
        self.timeout = timeout

    def available(self) -> bool:
        return self.adapter.is_available()

    def __call__(self, *, result: RunResult, workdir: str, spec: Any, rubric: list[str], cfg: dict) -> dict:
        prompt = _build_prompt(spec, result, workdir, rubric)
        opts = RunOptions(
            model=self.model,
            auto_approve=False,
            disable_tools=True,
            output_schema=VERDICT_SCHEMA,
        )
        with tempfile.TemporaryDirectory(prefix="judge-") as tmp:
            ex = execute(
                self.adapter, prompt, opts,
                cwd=tmp, timeout=self.timeout,
                agent_name=f"judge:{self.agent}", eval_name=getattr(spec, "name", ""),
            )
        verdict = _coerce_verdict(ex.result, rubric)
        return verdict


def _build_prompt(spec: Any, result: RunResult, workdir: str, rubric: list[str]) -> str:
    goal = getattr(spec, "description", "") or "(no description)"
    user_prompt = getattr(spec, "prompt", "")
    commands = result.commands()
    tools = result.tool_names()
    extra = _writes_outside_workspace(result, workdir)
    tree = _file_tree(workdir, extra)
    inline = _inline_new_files(workdir, extra)
    rubric_block = "\n".join(f"  {i+1}. {b}" for i, b in enumerate(rubric)) or "  (none)"

    parts = [
        "You are grading whether an AI coding agent completed a task correctly.",
        "Judge ONLY against the rubric. Be strict but fair. Do not reward intent — reward observed behavior in the transcript/artifacts.",
        "",
        f"## Task goal\n{goal}",
        f"\n## Prompt given to the agent\n{user_prompt}",
        f"\n## Agent's final answer\n{result.final_text or '(empty)'}",
        f"\n## Shell commands the agent ran ({len(commands)})\n"
        + ("\n".join(f"  $ {c}" for c in commands) if commands else "  (none captured)"),
        f"\n## Tools the agent used\n  {', '.join(tools) if tools else '(none captured)'}",
        f"\n## Files in the workspace after the run\n{tree}",
    ]
    if inline:
        parts.append("\n## Selected file contents\n" + inline)
    parts.append(
        "\n## Rubric — evaluate EACH item independently\n" + rubric_block
        + "\n\nReturn one verdict object per rubric item (in order), each with the "
        "behavior text, a boolean `pass`, and a one-sentence `reason`. Then a short "
        "overall `summary`."
    )
    note = ""
    if not commands and not tools:
        note = ("\n\nNOTE: No tool/command trace was captured for this agent (its CLI "
                "may not expose one). Grade trace-dependent rubric items from the final "
                "answer and file artifacts instead.")
    return "\n".join(parts) + note


def _writes_outside_workspace(result: RunResult, workdir: str) -> list[str]:
    """Absolute paths the run created that landed OUTSIDE the workspace (e.g. the model wrote to an
    absolute path with a mangled run-id). Surfacing them lets the judge grade the artifact the run
    actually produced, not just whatever happened to land in the workspace."""
    wd = os.path.abspath(workdir)
    out: list[str] = []
    seen: set[str] = set()
    for p in result.file_paths_touched():
        if not p:
            continue
        ap = os.path.abspath(p)
        if ap in seen:
            continue
        try:
            inside = os.path.commonpath([wd, ap]) == wd
        except ValueError:        # different drives, etc. → treat as outside
            inside = False
        if not inside and os.path.isfile(ap):
            seen.add(ap)
            out.append(ap)
    return out


def _file_tree(workdir: str, extra: list[str] = ()) -> str:
    lines: list[str] = []
    count = 0
    truncated = False
    for root, dirs, files in os.walk(workdir):
        dirs[:] = [d for d in sorted(dirs) if d not in (".git", "node_modules", "__pycache__")]
        # don't expose the provisioned skills as "agent output"
        rel_root = os.path.relpath(root, workdir)
        if rel_root.startswith((".claude", ".agents", ".antigravity", ".codex")):
            continue
        for f in sorted(files):
            if count >= _MAX_FILES:
                truncated = True
                break
            rel = os.path.relpath(os.path.join(root, f), workdir)
            lines.append(f"  {rel}")
            count += 1
        if truncated:
            break
    if truncated:
        lines.append(f"  ... (+ more, truncated at {_MAX_FILES})")
    for ap in extra:
        lines.append(f"  {ap}   [written OUTSIDE the workspace by this run]")
    return "\n".join(lines) if lines else "  (workspace empty)"


def _inline_new_files(workdir: str, extra: list[str] = ()) -> str:
    chunks: list[str] = []
    budget = 5

    def _maybe(path: str, label: str) -> None:
        nonlocal budget
        if budget <= 0:
            return
        if os.path.splitext(path)[1].lower() not in _TEXT_EXT:
            return
        try:
            if os.path.getsize(path) > _MAX_INLINE_BYTES:
                return
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                body = fh.read()
        except OSError:
            return
        chunks.append(f"--- {label} ---\n{body}")
        budget -= 1

    for root, dirs, files in os.walk(workdir):
        dirs[:] = [d for d in sorted(dirs) if d not in (".git", "node_modules", "__pycache__")]
        rel_root = os.path.relpath(root, workdir)
        if rel_root.startswith((".claude", ".agents", ".antigravity", ".codex")):
            continue
        for f in sorted(files):
            if budget <= 0:
                return "\n\n".join(chunks)
            path = os.path.join(root, f)
            _maybe(path, os.path.relpath(path, workdir))
    for ap in extra:
        if budget <= 0:
            break
        _maybe(ap, f"{ap}  [outside workspace]")
    return "\n\n".join(chunks)


def _coerce_verdict(rr: RunResult, rubric: list[str]) -> dict:
    """Normalize whatever the judge returned into the verdict shape."""
    data = rr.structured_output
    if not isinstance(data, dict):
        # try to salvage JSON from the final text
        try:
            data = json.loads(rr.final_text)
        except (json.JSONDecodeError, ValueError, TypeError):
            data = None
    if not isinstance(data, dict) or "items" not in data:
        reason = rr.error or "judge produced no parseable verdict"
        return {
            "items": [{"behavior": b, "pass": False, "reason": reason} for b in rubric],
            "summary": f"judge failed: {reason}",
            "judge_error": True,
        }
    # ensure booleans
    for it in data.get("items", []):
        it["pass"] = bool(it.get("pass"))
    data.setdefault("summary", "")
    return data
