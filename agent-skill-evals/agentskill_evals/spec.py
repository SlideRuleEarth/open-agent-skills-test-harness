"""Eval spec format + loader + per-skill discovery.

The improved, forward-looking eval format (YAML preferred, JSON also accepted):

    name: identifier-disambiguation
    description: Don't confuse atl06x (X-Series) with atl06p (legacy).
    skills: [sliderule-docsearch]      # provisioned into each agent's workspace
    prompt: |
      What does the atl06x endpoint do? How is it different from atl06p?
    files: []                          # seeded into the workspace (rel to eval file)
    fixture: null                      # dir copied as the starting workspace
    agents: [claude, codex]            # optional restriction (default: all selected)
    timeout_sec: 600
    tags: [routing]
    vars: {}                           # {placeholders} substituted into prompt
    env: {}                            # extra env vars for the agent process

    # Deterministic checks. All must pass.
    assertions:
      - {type: ran_command, contains: atl06x}
      - {type: file_exists, path: report.md}

    # Behaviors graded by an LLM judge (compiled into one llm_judge assertion).
    rubric:
      - Recognizes atl06x and atl06p are two distinct endpoints.
      - Cites URLs from returned chunks, not invented ones.

    # Optional: force/validate the final structured answer against a JSON Schema.
    output_schema: null

Discovery is per-skill: each skill directory owns an `evals/` folder, e.g.
`sliderule-docsearch/evals/*.yaml`. The skill name is inferred from the
directory that contains `evals/`.

Legacy keys are accepted as aliases (`query`->`prompt`,
`expected_behavior`->`rubric`) so existing files keep running; `migrate`
rewrites them into the canonical shape.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Optional

EVAL_SUFFIXES = (".yaml", ".yml", ".json")


@dataclass
class EvalSpec:
    name: str
    prompt: str
    description: str = ""
    skills: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    fixture: Optional[str] = None
    agents: Optional[list[str]] = None        # None = run on all CLI-selected agents
    timeout_sec: int = 600
    tags: list[str] = field(default_factory=list)
    vars: dict[str, Any] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    assertions: list[dict] = field(default_factory=list)
    rubric: list[str] = field(default_factory=list)
    output_schema: Optional[dict] = None
    # provenance (set by the loader)
    source_path: Optional[str] = None
    skill_name: Optional[str] = None

    # --- derived ------------------------------------------------------------

    def rendered_prompt(self) -> str:
        """Substitute {vars} into the prompt. {skill} is filled per-adapter later."""
        text = self.prompt
        for k, v in self.vars.items():
            text = text.replace("{" + k + "}", str(v))
        return text

    def base_dir(self) -> str:
        return os.path.dirname(self.source_path) if self.source_path else os.getcwd()

    def resolved_files(self) -> list[tuple[str, str]]:
        """(absolute source, workspace-relative dest) for each seed file."""
        out = []
        for f in self.files:
            src = f if os.path.isabs(f) else os.path.join(self.base_dir(), f)
            out.append((src, os.path.basename(f)))
        return out

    def resolved_fixture(self) -> Optional[str]:
        if not self.fixture:
            return None
        return self.fixture if os.path.isabs(self.fixture) else os.path.join(self.base_dir(), self.fixture)

    def effective_assertions(self) -> list[dict]:
        """All checks to run: explicit assertions + compiled rubric + schema."""
        out = list(self.assertions)
        if self.rubric:
            out.append({"type": "llm_judge", "rubric": list(self.rubric)})
        if self.output_schema and not any(
            a.get("type") == "output_matches_schema" for a in out
        ):
            out.append({"type": "output_matches_schema"})
        return out

    def to_canonical_dict(self) -> dict:
        """The canonical on-disk representation (used by `migrate`)."""
        d: dict[str, Any] = {"name": self.name}
        if self.description:
            d["description"] = self.description
        if self.skills:
            d["skills"] = self.skills
        d["prompt"] = self.prompt
        if self.files:
            d["files"] = self.files
        if self.fixture:
            d["fixture"] = self.fixture
        if self.agents:
            d["agents"] = self.agents
        if self.timeout_sec != 600:
            d["timeout_sec"] = self.timeout_sec
        if self.tags:
            d["tags"] = self.tags
        if self.vars:
            d["vars"] = self.vars
        if self.env:
            d["env"] = self.env
        if self.assertions:
            d["assertions"] = self.assertions
        if self.rubric:
            d["rubric"] = self.rubric
        if self.output_schema:
            d["output_schema"] = self.output_schema
        return d


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _load_raw(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    if path.endswith((".yaml", ".yml")):
        try:
            import yaml  # type: ignore
        except ModuleNotFoundError as exc:  # pragma: no cover
            raise RuntimeError(
                f"{path} is YAML but PyYAML isn't installed. "
                "Run `pip install pyyaml`, or write the eval as .json."
            ) from exc
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: eval must be a mapping/object, got {type(data).__name__}")
    return data


def _infer_skill_name(path: str) -> Optional[str]:
    """If the eval lives in <skill>/evals/<file>, return <skill>."""
    parent = os.path.dirname(os.path.abspath(path))
    if os.path.basename(parent) == "evals":
        return os.path.basename(os.path.dirname(parent))
    return None


def load_spec(path: str) -> EvalSpec:
    raw = _load_raw(path)

    # canonical + accepted aliases
    prompt = raw.get("prompt") or raw.get("query") or raw.get("input")
    if not prompt:
        raise ValueError(f"{path}: missing required `prompt` (or legacy `query`)")
    rubric = raw.get("rubric") or raw.get("expected_behavior") or []
    if isinstance(rubric, str):
        rubric = [rubric]

    skills = raw.get("skills")
    if skills is None and raw.get("skill"):
        skills = [raw["skill"]]
    skills = skills or []

    name = raw.get("name") or os.path.splitext(os.path.basename(path))[0]
    skill_name = _infer_skill_name(path) or (skills[0] if skills else None)

    return EvalSpec(
        name=name,
        prompt=str(prompt).strip(),
        description=raw.get("description", ""),
        skills=skills,
        files=raw.get("files", []) or [],
        fixture=raw.get("fixture"),
        agents=raw.get("agents"),
        timeout_sec=int(raw.get("timeout_sec", 600)),
        tags=raw.get("tags", []) or [],
        vars=raw.get("vars", {}) or {},
        env={str(k): str(v) for k, v in (raw.get("env", {}) or {}).items()},
        assertions=raw.get("assertions", []) or [],
        rubric=list(rubric),
        output_schema=raw.get("output_schema"),
        source_path=os.path.abspath(path),
        skill_name=skill_name,
    )


def skill_names(skills_root: str) -> list[str]:
    """Provisionable skills: immediate subdirectories of skills_root containing a SKILL.md.

    This is the repo's "superset" — what an eval/scenario may declare, and the set isolation
    masks from the global skills dirs.
    """
    out: list[str] = []
    try:
        entries = sorted(os.listdir(skills_root))
    except OSError:
        return out
    for name in entries:
        if os.path.isfile(os.path.join(skills_root, name, "SKILL.md")):
            out.append(name)
    return out


def discover_specs(
    *,
    skills_root: Optional[str] = None,
    skill: Optional[str] = None,
    paths: Optional[list[str]] = None,
) -> list[EvalSpec]:
    """Find eval files.

    Precedence:
      * explicit `paths` (files or directories) win;
      * else a single `skill` dir's evals/;
      * else scan every `<skills_root>/*/evals/` directory.
    """
    files: list[str] = []

    if paths:
        for p in paths:
            if os.path.isfile(p):
                files.append(p)
            elif os.path.isdir(p):
                files.extend(_scan_dir(p))
    elif skill:
        root = skills_root or os.getcwd()
        evals_dir = os.path.join(root, skill, "evals")
        files.extend(_scan_dir(evals_dir))
    else:
        root = skills_root or os.getcwd()
        for entry in sorted(os.listdir(root)):
            evals_dir = os.path.join(root, entry, "evals")
            if os.path.isdir(evals_dir):
                files.extend(_scan_dir(evals_dir))

    specs = []
    for f in sorted(set(files)):
        specs.append(load_spec(f))
    return specs


def _scan_dir(d: str) -> list[str]:
    """All eval files directly inside `d` (one level; evals dirs are flat)."""
    if not os.path.isdir(d):
        return []
    out = []
    for name in sorted(os.listdir(d)):
        if name.lower().endswith(EVAL_SUFFIXES) and not name.startswith("."):
            out.append(os.path.join(d, name))
    return out
