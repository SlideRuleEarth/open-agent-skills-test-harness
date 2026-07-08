"""LLM-as-judge.

The judge is itself one of the agent adapters (default: claude) run in a
throwaway workspace. It is handed a compact transcript of the graded run — the
user prompt, the agent's final answer, the commands/tools it ran, and the
resulting file tree — and scores each rubric behavior pass/fail with a reason.

All judge runs are tool-free (``disable_tools=True``): the judge prompt
contains everything needed for grading (transcript, tool trace, file
contents). Tools would let the judge explore its own empty temp workspace
and draw wrong conclusions about file existence.

Adapters that support native structured output (``supports_output_schema``,
e.g. Claude Code's ``--json-schema``) additionally get a schema constraint.
Other adapters (Copilot, Codex, AntiGravity) get explicit JSON format
instructions in the prompt and the verdict is parsed from the final answer.

Because the judge rides on the same adapter machinery, you can grade with any
agent (``--judge-agent codex``), not just Claude.
"""

from __future__ import annotations

import json
import re
import tempfile
from typing import Any, Optional

from dataclasses import dataclass

from .adapters import get_adapter
from .adapters.base import RunOptions
from .exec import ExecResult, execute
from .schema import RunResult
from .workspace_view import (
    JUDGE_MAX_FILES,
    JUDGE_MAX_INLINE_BYTES,
    JUDGE_MAX_INLINE_FILES,
    file_tree,
    inline_files,
    seeded_relpaths,
    writes_outside_workspace,
)

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

@dataclass
class JudgeResult:
    """Verdict plus the full execution trace, so callers can save judge artifacts."""
    verdict: dict
    exec_result: ExecResult


_JSON_FORMAT_INSTRUCTIONS = """
Your FINAL response must contain a single JSON object (optionally inside a
```json code fence).  Do NOT include any other text after the JSON.  Schema:

{
  "items": [
    {"behavior": "<rubric text>", "pass": true/false, "reason": "<one sentence>"},
    ...one entry per rubric item, in order...
  ],
  "summary": "<short overall summary>"
}"""

class Judge:
    def __init__(self, agent: str = "claude", model: Optional[str] = None, timeout: int = 240):
        self.adapter = get_adapter(agent)
        self.agent = self.adapter.name
        self.model = model
        self.timeout = timeout

    def available(self) -> bool:
        return self.adapter.is_available()

    def __call__(self, *, result: RunResult, workdir: str, spec: Any, rubric: list[str], cfg: dict) -> JudgeResult:
        native_schema = self.adapter.supports_output_schema
        prompt = _build_prompt(spec, result, workdir, rubric,
                               native_schema=native_schema)

        if native_schema:
            opts = RunOptions(
                model=self.model,
                auto_approve=False,
                disable_tools=True,
                output_schema=VERDICT_SCHEMA,
            )
        else:
            opts = RunOptions(
                model=self.model,
                auto_approve=True,
                disable_tools=True,
            )

        with tempfile.TemporaryDirectory(prefix="judge-") as tmp:
            ex = execute(
                self.adapter, prompt, opts,
                cwd=tmp, timeout=self.timeout,
                agent_name=f"judge:{self.agent}", eval_name=getattr(spec, "name", ""),
            )
        verdict = _coerce_verdict(ex.result, rubric)
        return JudgeResult(verdict=verdict, exec_result=ex)


def _build_prompt(spec: Any, result: RunResult, workdir: str, rubric: list[str],
                  *, native_schema: bool = True) -> str:
    goal = getattr(spec, "description", "") or "(no description)"
    user_prompt = getattr(spec, "prompt", "")
    commands = result.commands()
    tools = result.tool_names()
    extra = writes_outside_workspace(result, workdir)
    # Annotate seeded fixture/files: the judge must not credit a pre-seeded input as work
    # the agent did.
    seeded = seeded_relpaths(spec)
    tree = file_tree(workdir, extra, max_files=JUDGE_MAX_FILES, seeded=seeded)
    inline = inline_files(workdir, extra, max_files=JUDGE_MAX_INLINE_FILES,
                          max_bytes=JUDGE_MAX_INLINE_BYTES, seeded=seeded)
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
    if not native_schema:
        parts.append(_JSON_FORMAT_INSTRUCTIONS)
    note = ""
    if not commands and not tools:
        note = ("\n\nNOTE: No tool/command trace was captured for this agent (its CLI "
                "may not expose one). Grade trace-dependent rubric items from the final "
                "answer and file artifacts instead.")
    return "\n".join(parts) + note


_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)


def _extract_json(text: str) -> Optional[dict]:
    """Try to extract a JSON verdict from text, handling markdown fences."""
    text = (text or "").strip()
    if not text:
        return None
    # 1) direct parse
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass
    # 2) inside a markdown code fence
    for m in reversed(list(_FENCE_RE.finditer(text))):
        try:
            obj = json.loads(m.group(1).strip())
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            continue
    # 3) widest { ... } span (first open, last close)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start : end + 1])
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            pass
    return None


def _coerce_verdict(rr: RunResult, rubric: list[str]) -> dict:
    """Normalize whatever the judge returned into the verdict shape."""
    data = rr.structured_output
    if isinstance(data, dict) and "items" in data:
        pass  # native schema worked
    else:
        data = _extract_json(rr.final_text)
    if not isinstance(data, dict) or "items" not in data:
        reason = rr.error or "judge produced no parseable verdict"
        return {
            "items": [{"behavior": b, "pass": False, "reason": reason} for b in rubric],
            "summary": f"judge failed: {reason}",
            "judge_error": True,
        }
    for it in data.get("items", []):
        v = it.get("pass")
        it["pass"] = v is True or (isinstance(v, str) and v.lower() == "true")
    data.setdefault("summary", "")
    return data
