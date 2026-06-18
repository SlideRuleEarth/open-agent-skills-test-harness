"""Parser self-tests — validate every adapter's parse() against a captured
sample of its CLI's output. Runs with zero agent CLIs installed, so it's a fast
way to confirm the harness is wired correctly (and a regression guard when an
agent changes its output schema).

    python -m agentskill_evals selftest
"""

from __future__ import annotations

from .adapters import get_adapter

# --- captured sample outputs (one per agent format) ------------------------

CLAUDE = """\
{"type":"system","subtype":"init","session_id":"s1","tools":["Bash","Write"],"model":"claude","cwd":"/tmp"}
{"type":"assistant","message":{"content":[{"type":"text","text":"I'll scaffold the app."},{"type":"tool_use","id":"t1","name":"Bash","input":{"command":"npm install"}}]}}
{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"t1","is_error":false}]}}
{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t2","name":"Write","input":{"file_path":"package.json"}}]}}
{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t3","name":"StructuredOutput","input":{"ok":true}}]}}
{"type":"result","subtype":"success","is_error":false,"result":"Done. Created the app.","total_cost_usd":0.0123,"duration_ms":4567,"structured_output":{"ok":true}}
"""

CODEX = """\
{"type":"thread.started","thread_id":"th1"}
{"type":"item.started","item":{"id":"i1","type":"command_execution","command":"npm install"}}
{"type":"item.completed","item":{"id":"i1","type":"command_execution","exit_code":0,"aggregated_output":"added 1 package"}}
{"type":"item.completed","item":{"id":"i2","type":"file_change","changes":[{"path":"package.json"}]}}
{"type":"item.completed","item":{"id":"i3","type":"agent_message","text":"Created demo-app."}}
{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":20}}
"""

ANTIGRAVITY_STREAM = """\
{"type":"session.start","id":"a1"}
{"type":"tool_use","tool":"shell","args":{"command":"npm install"}}
{"type":"tool_result","tool":"shell"}
{"type":"result","text":"Done building demo-app."}
"""

ANTIGRAVITY_JSON = '{"result":"All done."}'
ANTIGRAVITY_RAW = "just a plain text answer with no JSON"


def _check(name, cond, msg, failures, verbose):
    status = "ok" if cond else "FAIL"
    if verbose or not cond:
        print(f"  [{status}] {name}: {msg}")
    if not cond:
        failures.append(name)


def _check_isolation(failures, verbose):
    """Validate the HOME overlay: declared skills present, undeclared masked, vendor kept,
    auth/config passed through, missing ancestors built. Pure filesystem — no CLIs."""
    import os
    import shutil
    import tempfile

    from .isolation import build_isolated_home

    print("isolation overlay:")
    real = tempfile.mkdtemp(prefix="ase-realhome-")
    declared_root = tempfile.mkdtemp(prefix="ase-skills-")
    dest = tempfile.mkdtemp(prefix="ase-isohome-")
    shutil.rmtree(dest)  # build_isolated_home (re)creates it
    try:
        # a fake real HOME: a global skills dir with two repo skills + a vendor bundle,
        # plus auth + an unrelated dotfile.
        os.makedirs(os.path.join(real, ".codex", "skills", "sliderule-api"))
        os.makedirs(os.path.join(real, ".codex", "skills", "sliderule-params"))
        os.makedirs(os.path.join(real, ".codex", "skills", ".system", "imagegen"))
        open(os.path.join(real, ".codex", "auth.json"), "w").close()
        open(os.path.join(real, ".gitconfig"), "w").close()
        # the cell declares only sliderule-api (its source lives outside HOME, like skills_root)
        os.makedirs(os.path.join(declared_root, "sliderule-api"))

        build_isolated_home(
            dest,
            [".codex/skills", ".gemini/config/skills"],   # one present, one missing (nested)
            {"sliderule-api", "sliderule-params"},        # repo superset to mask
            [os.path.join(declared_root, "sliderule-api")],
            real,
        )

        skills = os.path.join(dest, ".codex", "skills")
        names = set(os.listdir(skills)) if os.path.isdir(skills) else set()
        _check("isolation.declared_present", "sliderule-api" in names,
               f"declared sliderule-api present (got {sorted(names)})", failures, verbose)
        _check("isolation.undeclared_masked", "sliderule-params" not in names,
               "undeclared sliderule-params removed", failures, verbose)
        _check("isolation.vendor_kept", ".system" in names,
               "vendor .system bundle preserved", failures, verbose)
        _check("isolation.auth_passthrough",
               os.path.islink(os.path.join(dest, ".codex", "auth.json")),
               "auth.json passed through as a symlink", failures, verbose)
        _check("isolation.dotfile_passthrough",
               os.path.islink(os.path.join(dest, ".gitconfig")),
               ".gitconfig passed through as a symlink", failures, verbose)
        gem = os.path.join(dest, ".gemini", "config", "skills")
        gem_names = sorted(os.listdir(gem)) if os.path.isdir(gem) else ["<MISSING>"]
        _check("isolation.missing_ancestor_built", gem_names == ["sliderule-api"],
               f"missing nested skills dir built with declared only (got {gem_names})",
               failures, verbose)
    finally:
        for d in (real, declared_root, dest):
            shutil.rmtree(d, ignore_errors=True)


def run_selftest(verbose: bool = False) -> int:
    failures: list[str] = []

    # Claude
    print("claude adapter:")
    out = get_adapter("claude").parse(CLAUDE, "", 0)
    cmds = [e.command for e in out.events if e.command]
    tools = [e.tool_name for e in out.events if e.tool_name]
    _check("claude.command", "npm install" in cmds, f"commands={cmds}", failures, verbose)
    _check("claude.tools", {"Bash", "Write"} <= set(tools), f"tools={tools}", failures, verbose)
    _check("claude.no_structured_tool", "StructuredOutput" not in tools,
           "StructuredOutput is not traced as a real tool", failures, verbose)
    _check("claude.structured", out.structured_output == {"ok": True},
           f"structured={out.structured_output}", failures, verbose)
    _check("claude.final", out.final_text == "Done. Created the app.", repr(out.final_text), failures, verbose)
    _check("claude.cost", out.cost_usd == 0.0123, f"cost={out.cost_usd}", failures, verbose)

    # Codex
    print("codex adapter:")
    out = get_adapter("codex").parse(CODEX, "", 0)
    cmds = [e.command for e in out.events if e.command]
    paths = [e.path for e in out.events if e.path]
    _check("codex.command", cmds == ["npm install"], f"commands={cmds}", failures, verbose)
    _check("codex.file", "package.json" in paths, f"paths={paths}", failures, verbose)
    _check("codex.final", out.final_text == "Created demo-app.", repr(out.final_text), failures, verbose)

    # AntiGravity (3 shapes)
    print("antigravity adapter:")
    out = get_adapter("antigravity").parse(ANTIGRAVITY_STREAM, "", 0)
    cmds = [e.command for e in out.events if e.command]
    _check("antigravity.stream.command", cmds == ["npm install"], f"commands={cmds}", failures, verbose)
    _check("antigravity.stream.final", out.final_text == "Done building demo-app.", repr(out.final_text), failures, verbose)

    out = get_adapter("antigravity").parse(ANTIGRAVITY_JSON, "", 0)
    _check("antigravity.json.final", out.final_text == "All done.", repr(out.final_text), failures, verbose)

    out = get_adapter("antigravity").parse(ANTIGRAVITY_RAW, "", 0)
    _check("antigravity.raw.final", out.final_text == ANTIGRAVITY_RAW, repr(out.final_text), failures, verbose)

    # schema validator fallback
    print("schema validator:")
    from .assertions import validate_schema
    ok, _ = validate_schema({"name": "x", "port": 3000},
                            {"type": "object", "required": ["name", "port"],
                             "properties": {"name": {"type": "string"}, "port": {"type": "integer"}}})
    _check("schema.valid", ok, "valid object passes", failures, verbose)
    bad, err = validate_schema({"name": "x"},
                               {"type": "object", "required": ["name", "port"]})
    _check("schema.invalid", not bad, f"missing-required caught: {err}", failures, verbose)

    # HOME isolation overlay
    _check_isolation(failures, verbose)

    print()
    if failures:
        print(f"SELFTEST FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("SELFTEST PASSED")
    return 0
