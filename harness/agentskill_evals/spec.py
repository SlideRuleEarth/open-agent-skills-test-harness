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
`expected_behavior`->`rubric`) so existing files keep running.
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
        """(absolute source, workspace-relative dest) for each seed file.

        A relative entry keeps its path, so `fixtures/input.json` is seeded at
        `fixtures/input.json` in the workspace (matching "paths relative to the eval
        file"), not flattened to `input.json`. A `{src: dest}` mapping sets an explicit
        destination. An absolute path, or a dest that would escape the workspace, is
        placed by basename.
        """
        out = []
        for entry in self.files:
            if isinstance(entry, dict) and len(entry) == 1:
                src_rel, dest = next(iter(entry.items()))
            else:
                src_rel = dest = entry
            src = src_rel if os.path.isabs(src_rel) else os.path.join(self.base_dir(), src_rel)
            norm = os.path.normpath(str(dest))
            # never let a seed write outside the workspace
            dest = os.path.basename(norm) if os.path.isabs(norm) or norm.startswith("..") else norm
            out.append((src, dest))
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


# ---------------------------------------------------------------------------
# Pre-flight validation
# ---------------------------------------------------------------------------

_SKILL_POSITIVE = {"skill_triggered", "skill_reference_read", "skill_script_executed"}
_SKILL_NEGATIVE = {"skill_not_triggered", "skill_reference_not_read", "skill_script_not_executed"}
_SKILL_ASSERTIONS = _SKILL_POSITIVE | _SKILL_NEGATIVE
_BUILTINS = {"skill", "skills"}

_REQUIRED_KEYS: dict[str, list[str]] = {
    "file_exists": ["path"], "file_absent": ["path"], "dir_exists": ["path"],
    "skill_triggered": ["skill"], "skill_not_triggered": ["skill"],
    "skill_reference_read": ["skill"], "skill_reference_not_read": ["skill"],
    "skill_script_executed": ["skill"], "skill_script_not_executed": ["skill"],
    "used_tool": ["name"],
}
_NEEDS_CRITERION = {"ran_command", "final_contains"}
_CRITERION_KEYS = {"contains", "matches", "equals"}

import re
_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


@dataclass
class ValidationResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_spec(spec: "EvalSpec", *,
                  available_skills: Optional[set[str]] = None,
                  judge_enabled: bool = True) -> ValidationResult:
    """Check a spec for logical errors before spending tokens.

    *available_skills* is the repo's skill superset (from ``skill_names()``).
    Returns a ``ValidationResult`` with errors (should block) and warnings.
    """
    from .assertions import known_types
    valid_types = set(known_types())

    errors: list[str] = []
    warnings: list[str] = []
    provisioned = set(spec.skills)

    # --- per-assertion structural checks ---
    for a in spec.assertions:
        atype = a.get("type", "")
        if atype and atype not in valid_types:
            errors.append(f"unknown assertion type {atype!r} — will always fail")
        for key in _REQUIRED_KEYS.get(atype, []):
            if key not in a:
                errors.append(f"`{atype}` assertion missing required key {key!r}")
        if atype in _NEEDS_CRITERION and not (set(a) & _CRITERION_KEYS):
            errors.append(
                f"`{atype}` has no `contains`/`matches`/`equals` — will always fail")
        if atype == "tool_count":
            lo = int(a.get("min", 0))
            hi = int(a.get("max", 10**9))
            if lo > hi:
                errors.append(f"`tool_count` min={lo} > max={hi} — impossible to satisfy")

    # --- skill assertions vs provisioned skills ---
    for a in spec.assertions:
        atype = a.get("type", "")
        skill = a.get("skill", "")
        if not skill or atype not in _SKILL_ASSERTIONS:
            continue
        if atype in _SKILL_POSITIVE and skill not in provisioned:
            msg = (f"assertion `{atype}` expects skill {skill!r} but it is not in "
                   f"`skills:` — it will be isolated out and can never fire")
            if available_skills is not None and skill in available_skills:
                msg += " (skill exists in the repo; add it to this scenario's `skills:`)"
            errors.append(msg)
        if atype in _SKILL_NEGATIVE and skill in provisioned:
            warnings.append(
                f"assertion `{atype}` for skill {skill!r} which IS provisioned — "
                "the model can see it; is this intentional?")
        if atype in _SKILL_NEGATIVE and skill not in provisioned:
            warnings.append(
                f"assertion `{atype}` for skill {skill!r} which is not provisioned — "
                "this will always pass trivially (possible copy-paste from another scenario)")

    # --- contradictory file assertions ---
    exists_paths = {a["path"] for a in spec.assertions
                    if a.get("type") == "file_exists" and "path" in a}
    absent_paths = {a["path"] for a in spec.assertions
                    if a.get("type") == "file_absent" and "path" in a}
    for p in exists_paths & absent_paths:
        errors.append(f"contradictory: both `file_exists` and `file_absent` for {p!r}")

    # --- contradictory exit_code + no_error ---
    has_no_error = any(a.get("type") == "no_error" for a in spec.assertions)
    for a in spec.assertions:
        if a.get("type") == "exit_code" and int(a.get("equals", 0)) != 0 and has_no_error:
            errors.append(
                f"contradictory: `exit_code` expects {a['equals']} but `no_error` requires 0")

    # --- prompt placeholders ---
    if spec.skills:
        prompt_after_vars = spec.rendered_prompt()
    else:
        prompt_after_vars = spec.prompt
    placeholders = set(_PLACEHOLDER_RE.findall(prompt_after_vars))

    if not spec.skills and (placeholders & _BUILTINS):
        errors.append(
            "prompt references {skill}/{skills} but `skills:` is empty — "
            "placeholder will stay literal")

    defined = set(spec.vars) | _BUILTINS
    undefined = placeholders - defined
    if undefined:
        warnings.append(
            f"prompt placeholder(s) {{{', '.join(sorted(undefined))}}} not defined "
            "in `vars:` or built-ins — will stay literal in the prompt")

    unused_vars = set(spec.vars) - set(_PLACEHOLDER_RE.findall(spec.prompt))
    if unused_vars:
        warnings.append(
            f"`vars:` key(s) {{{', '.join(sorted(unused_vars))}}} never referenced "
            "in prompt — dead config, possible typo")

    # --- rubric without judge ---
    if spec.rubric and not judge_enabled:
        warnings.append(
            "rubric has items but judge is disabled — rubric will be silently skipped")

    # --- duplicate assertions ---
    seen: list[str] = []
    for a in spec.assertions:
        key = json.dumps(a, sort_keys=True)
        if key in seen:
            warnings.append(f"duplicate assertion: {a}")
        else:
            seen.append(key)

    # --- duplicate skills ---
    if len(spec.skills) != len(set(spec.skills)):
        dupes = [s for s in spec.skills if spec.skills.count(s) > 1]
        warnings.append(f"duplicate skill(s) in `skills:`: {', '.join(sorted(set(dupes)))}")

    # --- vars shadowing built-ins ---
    shadowed = set(spec.vars) & _BUILTINS
    if shadowed:
        warnings.append(
            f"`vars:` key(s) {{{', '.join(sorted(shadowed))}}} shadow the built-in "
            "placeholder(s) — the var value wins and the adapter's skill substitution "
            "will have no effect")

    # --- seed files / fixture existence ---
    for src, _ in spec.resolved_files():
        if not os.path.isfile(src):
            warnings.append(f"seed file {src!r} does not exist — workspace copy will fail")
    fixture = spec.resolved_fixture()
    if fixture and not os.path.isdir(fixture):
        warnings.append(f"fixture {fixture!r} does not exist — workspace setup will fail")

    # --- empty rubric items ---
    for i, r in enumerate(spec.rubric):
        text = str(r) if not isinstance(r, str) else r
        if not text or not text.strip():
            warnings.append(f"rubric item {i + 1} is empty — the judge will waste context on it")

    return ValidationResult(errors=errors, warnings=warnings)


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
    return _spec_from_raw(_load_raw(path), path)


def _spec_from_raw(raw: dict, path: str) -> EvalSpec:
    # canonical + accepted aliases
    prompt = raw.get("prompt") or raw.get("query") or raw.get("input")
    if not prompt:
        raise ValueError(f"{path}: missing required `prompt` (or legacy `query`)")
    rubric = raw.get("rubric") or raw.get("expected_behavior") or []
    if isinstance(rubric, str):
        rubric = [rubric]

    # Normalize skills to list[str]. A scalar `skills: sliderule-api` must become a
    # one-element list, not be iterated character-by-character downstream.
    skills = raw.get("skills")
    if skills is None:
        skills = raw.get("skill")          # singular alias
    if skills is None:
        skills = []
    elif isinstance(skills, str):
        skills = [skills]
    elif isinstance(skills, (list, tuple)):
        bad = [s for s in skills if not isinstance(s, str)]
        if bad:
            raise ValueError(f"{path}: `skills` entries must be strings; got {bad!r}")
        skills = [str(s) for s in skills]
    else:
        raise ValueError(
            f"{path}: `skills` must be a string or a list of strings, "
            f"got {type(skills).__name__}")

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


# ---------------------------------------------------------------------------
# Scenarios — a higher-level, ad-hoc eval that provisions a combination of skills
# together and pins a target (runner:model). Run with `run --config <file>`.
# ---------------------------------------------------------------------------

_SCENARIO_OVERRIDE_KEYS = ("max_cells", "jobs", "judge", "isolated")


@dataclass
class Scenario:
    """A combination eval: an EvalSpec plus a pinned target and optional run-knob overrides."""
    spec: EvalSpec
    runner: str
    model: Optional[str]
    overrides: dict          # subset of {max_cells, jobs, judge, isolated}
    source_path: str


def load_scenario(path: str) -> Scenario:
    """Load a scenario file (an eval spec + a `target:` block). The runner is validated by
    the CLI (spec.py must not import adapters)."""
    raw = _load_raw(path)

    target = raw.get("target")
    if not isinstance(target, dict):
        raise ValueError(
            f"{path}: a scenario needs a `target:` mapping with a `runner:` (and optional "
            "`model:`), e.g.\n  target:\n    runner: claude\n    model: claude-haiku-4-5")
    runner = target.get("runner")
    if not runner or not isinstance(runner, str):
        raise ValueError(f"{path}: target.runner is required (a runner name, e.g. claude).")
    model = target.get("model")
    model = str(model) if model else None

    spec = _spec_from_raw(raw, path)     # reuses prompt/skills parsing + the prompt-required check
    if not spec.skills:
        raise ValueError(
            f"{path}: a scenario needs a non-empty `skills:` list — the combination to "
            "provision together.")
    spec.agents = None                   # the target governs the runner; ignore any eval `agents:`
    spec.skill_name = "scenario"         # artifacts: .../<runner>/<model>/scenario/<name>/

    overrides = {k: raw[k] for k in _SCENARIO_OVERRIDE_KEYS if k in raw}
    return Scenario(spec=spec, runner=runner.strip(), model=model,
                    overrides=overrides, source_path=os.path.abspath(path))


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
