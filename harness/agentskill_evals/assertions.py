"""Assertion library.

Each assertion type is a function (result, workdir, spec, cfg, ctx) -> AssertionResult.
Deterministic types inspect the filesystem, the normalized event/tool trace,
the exit code, or the final answer. `llm_judge` and `output_matches_schema`
delegate to the judge / schema validator via the shared context.

Assertion config (`cfg`) is the per-check dict from the eval spec, e.g.
    {"type": "file_exists", "path": "report.md", "contains": "summary"}
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .schema import RunResult
from .workspace_view import resolve_trace_path


@dataclass
class AssertionResult:
    type: str
    passed: bool
    message: str
    kind: str = "deterministic"  # or "judge"
    details: dict = field(default_factory=dict)


@dataclass
class AssertionContext:
    """Shared services an assertion may need (judge, schema validator)."""

    spec: Any  # EvalSpec (avoid import cycle)
    judge: Optional[Callable[..., dict]] = None  # set by the runner when judging is enabled
    skills_subdir: str = ".claude/skills"  # adapter's skill provisioning path
    judge_exec: Any = None  # populated by _llm_judge with the judge's ExecResult for artifact saving


CheckFn = Callable[[RunResult, str, Any, dict, AssertionContext], AssertionResult]
_REGISTRY: dict[str, CheckFn] = {}


def register(type_name: str):
    def deco(fn: CheckFn) -> CheckFn:
        _REGISTRY[type_name] = fn
        return fn
    return deco


def run_assertion(
    cfg: dict, result: RunResult, workdir: str, spec: Any, ctx: AssertionContext
) -> AssertionResult:
    atype = cfg.get("type")
    fn = _REGISTRY.get(atype)
    if fn is None:
        return AssertionResult(
            type=str(atype), passed=False, message=f"unknown assertion type {atype!r}"
        )
    try:
        return fn(result, workdir, spec, cfg, ctx)
    except Exception as exc:  # an assertion bug shouldn't crash the whole run
        return AssertionResult(
            type=str(atype), passed=False, message=f"assertion errored: {exc}"
        )


def _label(cfg: dict, default: str) -> str:
    return cfg.get("description") or default


# ---------------------------------------------------------------------------
# Filesystem
# ---------------------------------------------------------------------------

def _escapes_workspace(rel: str) -> bool:
    norm = os.path.normpath(rel)
    return os.path.isabs(norm) or norm == os.pardir or norm.startswith(os.pardir + os.sep)


def _is_within(path: str, root: str) -> bool:
    rp = os.path.realpath(path)
    rr = os.path.realpath(root)
    return rp == rr or rp.startswith(rr + os.sep)


def _is_lexically_within(path: str, root: str) -> bool:
    ap = os.path.abspath(path)
    ar = os.path.abspath(root)
    return ap == ar or ap.startswith(ar + os.sep)


@register("file_exists")
def _file_exists(result, workdir, spec, cfg, ctx):
    rel = cfg["path"]
    if _escapes_workspace(rel):
        return AssertionResult("file_exists", False,
                               _label(cfg, f"path {rel!r} escapes workspace"))
    path, where = _resolve_artifact(result, workdir, rel)
    if path is None:
        return AssertionResult("file_exists", False, _label(cfg, f"missing file: {rel}"))
    if where == "workspace" and not _is_within(path, workdir):
        return AssertionResult("file_exists", False,
                               _label(cfg, f"path {rel!r} resolves outside workspace (symlink?)"))
    # An artifact produced *anywhere* still counts as produced: the harness grades whether the
    # run created it, not whether the model typed the destination correctly (e.g. an absolute
    # path with a mangled run-id). Flag a non-workspace hit so it's visible, never silent.
    note = "" if where == "workspace" else f"  [produced at {path} via {where}, not at {rel}]"
    size = os.path.getsize(path)
    if "min_size" in cfg and size < int(cfg["min_size"]):
        return AssertionResult(
            "file_exists", False, f"{rel} too small ({size} < {cfg['min_size']} bytes){note}"
        )
    if "contains" in cfg or "matches" in cfg:
        text = _read_text(path)
        if "contains" in cfg and cfg["contains"] not in text:
            return AssertionResult("file_exists", False, f"{rel} missing text {cfg['contains']!r}{note}")
        if "matches" in cfg and not re.search(cfg["matches"], text):
            return AssertionResult("file_exists", False, f"{rel} no regex match /{cfg['matches']}/{note}")
    return AssertionResult(
        "file_exists", True, _label(cfg, f"{rel} exists ({size} bytes){note}"),
        details={"resolved_path": path, "resolved_via": where},
    )


def _resolve_artifact(result, workdir: str, rel: str) -> tuple[Optional[str], Optional[str]]:
    """Locate the artifact `rel`, tolerant of an agent that wrote it to the wrong path.

    Order of resolution, returning (abs_path, where):
      1. "workspace"   — the expected location, ``workdir/rel``.
      2. "write-trace" — a file the run is proven to have created (Write/Edit/… tool calls, via
                         ``result.file_paths_touched()``) whose basename matches and that exists on
                         disk. This catches a misplaced destination — e.g. a model mangling the
                         run-id/date in an absolute path — without trusting it to land in the
                         workspace. Trace paths resolve against the WORKSPACE (the agent's cwd), so
                         a relative path is never matched against the harness process cwd.
    Returns (None, None) if not found.

    There is deliberately no "any file with this basename anywhere under the workspace" fallback: a
    seeded fixture or a provisioned-skill file sharing the basename would otherwise satisfy the
    assertion for work the agent never did (a false pass). Only the exact path, or a file the run is
    proven to have produced, counts.
    """
    exact = os.path.join(workdir, rel)
    if os.path.isfile(exact):
        return exact, "workspace"
    base = os.path.basename(rel.rstrip("/"))
    if base:
        # latest matching write wins (the agent's final state for that filename)
        for p in reversed(result.file_paths_touched()):
            if not p:
                continue
            ap = os.path.abspath(resolve_trace_path(p, workdir))
            if os.path.basename(ap) == base and os.path.isfile(ap):
                if _is_lexically_within(ap, workdir) and not _is_within(ap, workdir):
                    continue
                return ap, "write-trace"
    return None, None


@register("file_absent")
def _file_absent(result, workdir, spec, cfg, ctx):
    rel = cfg["path"]
    if _escapes_workspace(rel):
        return AssertionResult("file_absent", False,
                               _label(cfg, f"path {rel!r} escapes workspace"))
    target = os.path.join(workdir, rel)
    if not _is_within(target, workdir):
        return AssertionResult("file_absent", False,
                               _label(cfg, f"path {rel!r} resolves outside workspace (symlink?)"))
    exists = os.path.exists(target)
    return AssertionResult("file_absent", not exists,
                           _label(cfg, f"{rel} {'unexpectedly present' if exists else 'absent'}"),
                           details={"path": target})


@register("dir_exists")
def _dir_exists(result, workdir, spec, cfg, ctx):
    rel = cfg["path"]
    if _escapes_workspace(rel):
        return AssertionResult("dir_exists", False,
                               _label(cfg, f"path {rel!r} escapes workspace"))
    target = os.path.join(workdir, rel)
    if not _is_within(target, workdir):
        return AssertionResult("dir_exists", False,
                               _label(cfg, f"path {rel!r} resolves outside workspace (symlink?)"))
    ok = os.path.isdir(target)
    return AssertionResult("dir_exists", ok, _label(cfg, f"dir {rel} {'exists' if ok else 'missing'}"),
                           details={"path": target})


# ---------------------------------------------------------------------------
# Tool / command trace
# ---------------------------------------------------------------------------

@register("ran_command")
def _ran_command(result, workdir, spec, cfg, ctx):
    cmds = result.commands()
    hit = _match_any(cmds, cfg)
    if hit is not None:
        return AssertionResult("ran_command", True, _label(cfg, f"ran: {hit}"))
    crit = cfg.get("contains") or cfg.get("matches") or cfg.get("equals") or "?"
    return AssertionResult(
        "ran_command", False, _label(cfg, f"no command matched {crit!r}"),
        details={"commands_seen": cmds},
    )


@register("used_tool")
def _used_tool(result, workdir, spec, cfg, ctx):
    want = cfg["name"].lower()
    names = [t.lower() for t in result.tool_names()]
    ok = want in names
    return AssertionResult("used_tool", ok,
                           _label(cfg, f"tool {cfg['name']} {'used' if ok else 'not used'}"),
                           details={"tools_seen": result.tool_names()})


@register("tool_count")
def _tool_count(result, workdir, spec, cfg, ctx):
    n = len(result.tool_calls())
    lo = int(cfg.get("min", 0))
    hi = int(cfg.get("max", 10**9))
    ok = lo <= n <= hi
    return AssertionResult("tool_count", ok, _label(cfg, f"{n} tool calls (want {lo}..{hi})"),
                           details={"n": n, "min": lo, "max": hi})


# ---------------------------------------------------------------------------
# Skill interaction trace
# ---------------------------------------------------------------------------

def _in_skill_dir(path: str, skills_subdir: str, skill: str,
                  subdir: str = "", filename: str = "") -> bool:
    """Check whether *path* falls inside a skill's provisioned directory.

    Works for both absolute and relative (to workspace) paths because it
    does a substring match on the canonical ``<skills_subdir>/<skill>/…`` segment.
    """
    prefix = f"{skills_subdir}/{skill}"
    if subdir:
        prefix = f"{prefix}/{subdir}"
    if filename:
        return f"{prefix}/{filename}" in path
    return f"{prefix}/" in path


def _skill_trace_hits(result, ctx, skill: str,
                      subdir: str = "", filename: str = "") -> list[str]:
    """Return trace paths/commands that reference a skill (or sub-path within it)."""
    sd = ctx.skills_subdir
    hits: list[str] = []
    for e in result.events:
        if e.path and _in_skill_dir(e.path, sd, skill, subdir, filename):
            hits.append(e.path)
        if e.command and _in_skill_dir(e.command, sd, skill, subdir, filename):
            hits.append(e.command)
    return hits


@register("skill_triggered")
def _skill_triggered(result, workdir, spec, cfg, ctx):
    skill = cfg["skill"]
    hits = _skill_trace_hits(result, ctx, skill)
    if hits:
        return AssertionResult(
            "skill_triggered", True,
            _label(cfg, f"skill {skill} triggered ({len(hits)} access(es))"),
            details={"hits": hits})
    return AssertionResult(
        "skill_triggered", False,
        _label(cfg, f"skill {skill} was not triggered"))


@register("skill_not_triggered")
def _skill_not_triggered(result, workdir, spec, cfg, ctx):
    skill = cfg["skill"]
    hits = _skill_trace_hits(result, ctx, skill)
    if not hits:
        return AssertionResult(
            "skill_not_triggered", True,
            _label(cfg, f"skill {skill} correctly not triggered"))
    return AssertionResult(
        "skill_not_triggered", False,
        _label(cfg, f"skill {skill} was triggered ({len(hits)} access(es))"),
        details={"hits": hits})


@register("skill_reference_read")
def _skill_reference_read(result, workdir, spec, cfg, ctx):
    skill = cfg["skill"]
    filename = cfg.get("path", "")
    hits = _skill_trace_hits(result, ctx, skill, subdir="references", filename=filename)
    target = f"{skill}/references/{filename}" if filename else f"{skill}/references/*"
    if hits:
        return AssertionResult(
            "skill_reference_read", True,
            _label(cfg, f"reference {target} read"),
            details={"hits": hits})
    return AssertionResult(
        "skill_reference_read", False,
        _label(cfg, f"reference {target} not read"))


@register("skill_reference_not_read")
def _skill_reference_not_read(result, workdir, spec, cfg, ctx):
    skill = cfg["skill"]
    filename = cfg.get("path", "")
    hits = _skill_trace_hits(result, ctx, skill, subdir="references", filename=filename)
    target = f"{skill}/references/{filename}" if filename else f"{skill}/references/*"
    if not hits:
        return AssertionResult(
            "skill_reference_not_read", True,
            _label(cfg, f"reference {target} correctly not read"))
    return AssertionResult(
        "skill_reference_not_read", False,
        _label(cfg, f"reference {target} was read"),
        details={"hits": hits})


@register("skill_script_executed")
def _skill_script_executed(result, workdir, spec, cfg, ctx):
    skill = cfg["skill"]
    filename = cfg.get("path", "")
    hits = _skill_trace_hits(result, ctx, skill, subdir="scripts", filename=filename)
    target = f"{skill}/scripts/{filename}" if filename else f"{skill}/scripts/*"
    if hits:
        return AssertionResult(
            "skill_script_executed", True,
            _label(cfg, f"script {target} executed"),
            details={"hits": hits})
    return AssertionResult(
        "skill_script_executed", False,
        _label(cfg, f"script {target} not executed"))


@register("skill_script_not_executed")
def _skill_script_not_executed(result, workdir, spec, cfg, ctx):
    skill = cfg["skill"]
    filename = cfg.get("path", "")
    hits = _skill_trace_hits(result, ctx, skill, subdir="scripts", filename=filename)
    target = f"{skill}/scripts/{filename}" if filename else f"{skill}/scripts/*"
    if not hits:
        return AssertionResult(
            "skill_script_not_executed", True,
            _label(cfg, f"script {target} correctly not executed"))
    return AssertionResult(
        "skill_script_not_executed", False,
        _label(cfg, f"script {target} was executed"),
        details={"hits": hits})


# ---------------------------------------------------------------------------
# Process / output text
# ---------------------------------------------------------------------------

@register("exit_code")
def _exit_code(result, workdir, spec, cfg, ctx):
    want = int(cfg.get("equals", 0))
    ok = result.exit_code == want
    return AssertionResult("exit_code", ok, _label(cfg, f"exit={result.exit_code} (want {want})"),
                           details={"exit_code": result.exit_code, "want": want})


@register("no_error")
def _no_error(result, workdir, spec, cfg, ctx):
    ok = not result.error and not result.timed_out and result.exit_code == 0
    msg = "clean run" if ok else (result.error or ("timed out" if result.timed_out else f"exit {result.exit_code}"))
    return AssertionResult("no_error", ok, _label(cfg, msg),
                           details={"error": result.error, "timed_out": result.timed_out,
                                     "exit_code": result.exit_code})


@register("final_contains")
def _final_contains(result, workdir, spec, cfg, ctx):
    hit = _match_any([result.final_text], cfg)
    ok = hit is not None
    crit = cfg.get("contains") or cfg.get("matches") or "?"
    return AssertionResult("final_contains", ok,
                           _label(cfg, f"final answer {'matched' if ok else 'did not match'} {crit!r}"),
                           details={"final_text": result.final_text})


# ---------------------------------------------------------------------------
# Schema + judge
# ---------------------------------------------------------------------------

@register("output_matches_schema")
def _output_matches_schema(result, workdir, spec, cfg, ctx):
    schema = cfg.get("schema") or getattr(spec, "output_schema", None)
    if not schema:
        return AssertionResult("output_matches_schema", False, "no output_schema defined")
    if result.structured_output is None:
        return AssertionResult("output_matches_schema", False,
                               "agent produced no parseable structured output")
    ok, err = validate_schema(result.structured_output, schema)
    return AssertionResult("output_matches_schema", ok,
                           _label(cfg, "structured output valid" if ok else f"schema: {err}"),
                           details={} if ok else {"schema_error": err})


@register("llm_judge")
def _llm_judge(result, workdir, spec, cfg, ctx):
    if ctx.judge is None:
        return AssertionResult("llm_judge", False, "judge not configured (use --judge-agent)",
                               kind="judge")
    rubric = cfg.get("rubric") or getattr(spec, "rubric", []) or []
    threshold = float(cfg.get("threshold", 1.0))  # fraction of rubric items that must pass
    jr = ctx.judge(result=result, workdir=workdir, spec=spec, rubric=rubric, cfg=cfg)
    # JudgeResult carries both the verdict and the full exec trace.
    verdict = jr.verdict if hasattr(jr, "verdict") else jr
    if hasattr(jr, "exec_result"):
        ctx.judge_exec = jr.exec_result
    items = verdict.get("items", [])
    expected = len(rubric) if rubric else (len(items) or 1)
    scored = items[:expected]
    passed_items = sum(1 for it in scored if it.get("pass"))
    frac = passed_items / expected
    # The bare top-level `pass` is only trustworthy when there was never a rubric to score in
    # the first place (`not rubric`) — gating on `not items` instead would let a judge that
    # ignores the "one entry per rubric item" instruction and returns `{"items": [], "pass":
    # true}` bypass real per-item scoring: `frac` would correctly compute 0/expected, but the
    # bare `pass` would silently override it into a pass.
    if not rubric and "pass" in verdict and not items:
        vp = verdict["pass"]
        ok = vp is True or (isinstance(vp, str) and vp.lower() == "true")
    else:
        ok = frac >= threshold
    msg = verdict.get("summary") or f"{passed_items}/{expected} rubric items satisfied"
    return AssertionResult("llm_judge", ok, msg, kind="judge", details=verdict)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _match_any(strings: list[str], cfg: dict) -> Optional[str]:
    """Return the first string satisfying EVERY criterion `cfg` supplies, else None.

    `contains`/`matches`/`equals` are ANDed when more than one is given — a cfg like
    `{contains: "atl06x", matches: "^conda run"}` means "a command that both mentions atl06x AND
    starts with conda run", not "either." (ORing them would let any string satisfying just one
    criterion pass, silently more permissive than a spec author supplying two constraints would
    expect.)
    """
    contains = cfg.get("contains")
    matches = cfg.get("matches")
    equals = cfg.get("equals")
    ci = cfg.get("ignore_case", False)
    if contains is None and matches is None and equals is None:
        return None
    for s in strings:
        if s is None:
            continue
        hay = s.lower() if ci else s
        if contains is not None:
            needle = contains.lower() if ci else contains
            if needle not in hay:
                continue
        if matches is not None and not re.search(matches, s, re.IGNORECASE if ci else 0):
            continue
        if equals is not None:
            want = str(equals).lower() if ci else str(equals)
            if hay.strip() != want.strip():
                continue
        return s
    return None


def validate_schema(instance: Any, schema: dict) -> tuple[bool, str]:
    """Validate `instance` against `schema`.

    Uses jsonschema if installed; otherwise a minimal built-in validator
    covering type/required/properties/items — enough for typical eval schemas.
    """
    try:
        import jsonschema  # type: ignore

        jsonschema.validate(instance, schema)
        return True, ""
    except ModuleNotFoundError:
        return _mini_validate(instance, schema, "$")
    except Exception as exc:  # jsonschema.ValidationError et al.
        return False, str(exc).splitlines()[0]


_TYPE_MAP = {
    "object": dict,
    "array": list,
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "null": type(None),
}


def _mini_validate(inst: Any, schema: dict, path: str) -> tuple[bool, str]:
    t = schema.get("type")
    if t:
        py = _TYPE_MAP.get(t)
        if py and not isinstance(inst, py):
            return False, f"{path}: expected {t}, got {type(inst).__name__}"
        if t == "boolean" and isinstance(inst, bool):
            pass
        elif t in ("number", "integer") and isinstance(inst, bool):
            return False, f"{path}: expected {t}, got boolean"
    if t == "object" or isinstance(inst, dict):
        for req in schema.get("required", []):
            if not isinstance(inst, dict) or req not in inst:
                return False, f"{path}: missing required property {req!r}"
        props = schema.get("properties", {})
        if isinstance(inst, dict):
            for k, subschema in props.items():
                if k in inst:
                    ok, err = _mini_validate(inst[k], subschema, f"{path}.{k}")
                    if not ok:
                        return False, err
    if (t == "array" or isinstance(inst, list)) and "items" in schema and isinstance(inst, list):
        for i, el in enumerate(inst):
            ok, err = _mini_validate(el, schema["items"], f"{path}[{i}]")
            if not ok:
                return False, err
    return True, ""


def known_types() -> list[str]:
    return sorted(_REGISTRY)
