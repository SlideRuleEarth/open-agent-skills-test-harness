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

@register("file_exists")
def _file_exists(result, workdir, spec, cfg, ctx):
    rel = cfg["path"]
    path = os.path.join(workdir, rel)
    if not os.path.isfile(path):
        return AssertionResult("file_exists", False, _label(cfg, f"missing file: {rel}"))
    size = os.path.getsize(path)
    if "min_size" in cfg and size < int(cfg["min_size"]):
        return AssertionResult(
            "file_exists", False, f"{rel} too small ({size} < {cfg['min_size']} bytes)"
        )
    if "contains" in cfg or "matches" in cfg:
        text = _read_text(path)
        if "contains" in cfg and cfg["contains"] not in text:
            return AssertionResult("file_exists", False, f"{rel} missing text {cfg['contains']!r}")
        if "matches" in cfg and not re.search(cfg["matches"], text):
            return AssertionResult("file_exists", False, f"{rel} no regex match /{cfg['matches']}/")
    return AssertionResult("file_exists", True, _label(cfg, f"{rel} exists ({size} bytes)"))


@register("file_absent")
def _file_absent(result, workdir, spec, cfg, ctx):
    rel = cfg["path"]
    exists = os.path.exists(os.path.join(workdir, rel))
    return AssertionResult("file_absent", not exists,
                           _label(cfg, f"{rel} {'unexpectedly present' if exists else 'absent'}"))


@register("dir_exists")
def _dir_exists(result, workdir, spec, cfg, ctx):
    rel = cfg["path"]
    ok = os.path.isdir(os.path.join(workdir, rel))
    return AssertionResult("dir_exists", ok, _label(cfg, f"dir {rel} {'exists' if ok else 'missing'}"))


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
    return AssertionResult("tool_count", ok, _label(cfg, f"{n} tool calls (want {lo}..{hi})"))


# ---------------------------------------------------------------------------
# Process / output text
# ---------------------------------------------------------------------------

@register("exit_code")
def _exit_code(result, workdir, spec, cfg, ctx):
    want = int(cfg.get("equals", 0))
    ok = result.exit_code == want
    return AssertionResult("exit_code", ok, _label(cfg, f"exit={result.exit_code} (want {want})"))


@register("no_error")
def _no_error(result, workdir, spec, cfg, ctx):
    ok = not result.error and not result.timed_out and result.exit_code == 0
    msg = "clean run" if ok else (result.error or ("timed out" if result.timed_out else f"exit {result.exit_code}"))
    return AssertionResult("no_error", ok, _label(cfg, msg))


@register("final_contains")
def _final_contains(result, workdir, spec, cfg, ctx):
    hit = _match_any([result.final_text], cfg)
    ok = hit is not None
    crit = cfg.get("contains") or cfg.get("matches") or "?"
    return AssertionResult("final_contains", ok,
                           _label(cfg, f"final answer {'matched' if ok else 'did not match'} {crit!r}"))


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
                           _label(cfg, "structured output valid" if ok else f"schema: {err}"))


@register("llm_judge")
def _llm_judge(result, workdir, spec, cfg, ctx):
    if ctx.judge is None:
        return AssertionResult("llm_judge", False, "judge not configured (use --judge-agent)",
                               kind="judge")
    rubric = cfg.get("rubric") or getattr(spec, "rubric", []) or []
    threshold = float(cfg.get("threshold", 1.0))  # fraction of rubric items that must pass
    verdict = ctx.judge(result=result, workdir=workdir, spec=spec, rubric=rubric, cfg=cfg)
    items = verdict.get("items", [])
    n = len(items) or 1
    passed_items = sum(1 for it in items if it.get("pass"))
    frac = passed_items / n
    ok = bool(verdict.get("pass")) if "pass" in verdict and not items else frac >= threshold
    msg = verdict.get("summary") or f"{passed_items}/{n} rubric items satisfied"
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
    """Return the first string matching the cfg criterion, else None."""
    contains = cfg.get("contains")
    matches = cfg.get("matches")
    equals = cfg.get("equals")
    ci = cfg.get("ignore_case", False)
    for s in strings:
        if s is None:
            continue
        hay = s.lower() if ci else s
        if contains is not None:
            needle = contains.lower() if ci else contains
            if needle in hay:
                return s
        if matches is not None:
            if re.search(matches, s, re.IGNORECASE if ci else 0):
                return s
        if equals is not None and s.strip() == str(equals).strip():
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
