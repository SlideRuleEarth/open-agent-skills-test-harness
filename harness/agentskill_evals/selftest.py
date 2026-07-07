"""Parser self-tests — validate every adapter's parse() against a captured
sample of its CLI's output. Runs with zero agent CLIs installed, so it's a fast
way to confirm the harness is wired correctly (and a regression guard when an
agent changes its output schema).

    python -m agentskill_evals selftest
"""

from __future__ import annotations

from .adapters import get_adapter
from .adapters.base import RunOptions
from .schema import EventKind
from .spec import EvalSpec

# --- captured sample outputs (one per agent format) ------------------------

CLAUDE = """\
{"type":"system","subtype":"init","session_id":"s1","tools":["Bash","Write"],"model":"claude","cwd":"/tmp"}
{"type":"assistant","message":{"content":[{"type":"text","text":"I'll scaffold the app."},{"type":"tool_use","id":"t1","name":"Bash","input":{"command":"npm install"}}]}}
{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"t1","is_error":false}]}}
{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t2","name":"Write","input":{"file_path":"package.json"}}]}}
{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t4","name":"Skill","input":{"skill":"sliderule-api"}}]}}
{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t5","name":"Glob","input":{"pattern":"**/SKILL.md","path":"/etc"}}]}}
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

# Covers branches CODEX above never exercises: an mcp_tool_call's completion (success is silently
# dropped once its id is deduped — this must still surface a TOOL_RESULT, error or not), a
# `reasoning` item, a top-level `error` event, file_change's dict-form `changes` / single
# `path` fallback shapes (_codex_changed_paths), and an itype this adapter doesn't specifically
# recognize (e.g. a native tool added after this adapter was written) still surfacing its path
# via the generic fallback rather than being silently dropped.
CODEX_EXTRA = """\
{"type":"thread.started","thread_id":"th2"}
{"type":"item.started","item":{"id":"m1","type":"mcp_tool_call","tool":"search","server":"web"}}
{"type":"item.completed","item":{"id":"m1","type":"mcp_tool_call","tool":"search","server":"web","error":"timeout"}}
{"type":"item.completed","item":{"id":"r1","type":"reasoning","text":"thinking about the plan"}}
{"type":"error","message":"a transient network error"}
{"type":"item.completed","item":{"id":"f1","type":"file_change","changes":{"a.txt":{},"b.txt":{}}}}
{"type":"item.completed","item":{"id":"f2","type":"file_change","path":"single.txt"}}
{"type":"item.completed","item":{"id":"u1","type":"file_search","path":"/etc/passwd"}}
{"type":"turn.completed","usage":{"input_tokens":5,"output_tokens":5}}
"""

ANTIGRAVITY_STREAM = """\
{"type":"session.start","id":"a1"}
{"type":"tool_use","tool":"shell","args":{"command":"npm install"}}
{"type":"tool_result","tool":"shell"}
{"type":"error","message":"a transient error"}
{"type":"tool_use","tool":"skill","args":{"skill":"sliderule-api"}}
{"type":"result","text":"Done building demo-app."}
"""

ANTIGRAVITY_JSON = '{"result":"All done."}'
ANTIGRAVITY_RAW = "just a plain text answer with no JSON"

# The real `--output-format json` shape (agy 1.0.16+) — a conversation_id that keys the
# on-disk transcript, tested together in _check_antigravity_transcript below.
ANTIGRAVITY_JSON_RESULT = (
    '{"conversation_id":"conv-test-1","status":"SUCCESS",'
    '"response":"Done building demo-app.","duration_seconds":1.5,"num_turns":1,'
    '"usage":{"input_tokens":10,"output_tokens":20,"thinking_tokens":5,"total_tokens":35}}'
)

ANTIGRAVITY_TRANSCRIPT = """\
{"step_index":0,"source":"USER_EXPLICIT","type":"USER_INPUT","content":"do the task"}
{"step_index":1,"source":"SYSTEM","type":"CONVERSATION_HISTORY"}
{"step_index":2,"source":"MODEL","type":"PLANNER_RESPONSE","thinking":"planning the work","tool_calls":[{"name":"run_command","args":{"CommandLine":"npm install"}}]}
{"step_index":3,"source":"MODEL","type":"RUN_COMMAND","content":"added 1 package"}
{"step_index":4,"source":"MODEL","type":"PLANNER_RESPONSE","tool_calls":[{"name":"write_to_file","args":{"TargetFile":"package.json","CodeContent":"{}"}}]}
{"step_index":5,"source":"MODEL","type":"CODE_ACTION","content":"Created file package.json"}
{"step_index":6,"source":"MODEL","type":"PLANNER_RESPONSE","tool_calls":[{"name":"skill","args":{"skill":"sliderule-api"}}]}
{"step_index":7,"source":"SYSTEM","type":"CHECKPOINT","content":"summary"}
{"step_index":8,"source":"MODEL","type":"ERROR_MESSAGE","content":"a transient warning"}
{"step_index":9,"source":"MODEL","type":"PLANNER_RESPONSE","content":"Done building demo-app."}
"""

COPILOT = """\
{"type":"session.skills_loaded","data":{"skills":[]},"id":"s1","timestamp":"2026-06-22T00:00:00Z","parentId":"p1","ephemeral":true}
{"type":"session.tools_updated","data":{"model":"claude-sonnet-4.6"},"id":"s2","timestamp":"2026-06-22T00:00:00Z","parentId":"p1","ephemeral":true}
{"type":"user.message","data":{"content":"list files"},"id":"u1","timestamp":"2026-06-22T00:00:00Z","parentId":"p1"}
{"type":"assistant.turn_start","data":{"turnId":"0","interactionId":"i1"},"id":"t1","timestamp":"2026-06-22T00:00:01Z","parentId":"u1"}
{"type":"assistant.message","data":{"messageId":"m1","model":"claude-sonnet-4.6","content":"","toolRequests":[{"toolCallId":"tc1","name":"report_intent","arguments":{"intent":"Listing files"},"type":"function"},{"toolCallId":"tc2","name":"shell","arguments":{"command":"ls -la"},"type":"function"},{"toolCallId":"tc3","name":"view","arguments":{"path":"/tmp/project"},"type":"function"},{"toolCallId":"tc4","name":"skill","arguments":{"skill":"sliderule-params"},"type":"function"}],"interactionId":"i1","turnId":"0","outputTokens":50},"id":"m1","timestamp":"2026-06-22T00:00:02Z","parentId":"t1"}
{"type":"tool.execution_complete","data":{"toolCallId":"tc2","success":true,"result":{"content":"file1.txt\\nfile2.txt"}},"id":"r1","timestamp":"2026-06-22T00:00:02Z","parentId":"m1"}
{"type":"tool.execution_complete","data":{"toolCallId":"tc3","success":true,"result":{"content":"file1.txt\\nfile2.txt"}},"id":"r2","timestamp":"2026-06-22T00:00:02Z","parentId":"m1"}
{"type":"assistant.turn_end","data":{"turnId":"0"},"id":"e1","timestamp":"2026-06-22T00:00:03Z","parentId":"r2"}
{"type":"assistant.turn_start","data":{"turnId":"1","interactionId":"i1"},"id":"t2","timestamp":"2026-06-22T00:00:03Z","parentId":"e1"}
{"type":"assistant.message","data":{"messageId":"m2","model":"claude-sonnet-4.6","content":"Found 2 files: file1.txt and file2.txt","toolRequests":[],"interactionId":"i1","turnId":"1","outputTokens":20},"id":"m2","timestamp":"2026-06-22T00:00:04Z","parentId":"t2"}
{"type":"assistant.turn_end","data":{"turnId":"1"},"id":"e2","timestamp":"2026-06-22T00:00:04Z","parentId":"m2"}
{"type":"result","timestamp":"2026-06-22T00:00:04Z","sessionId":"sess1","exitCode":0,"usage":{"premiumRequests":1,"totalApiDurationMs":2000,"sessionDurationMs":4000}}
"""


def _check(name, cond, msg, failures, verbose):
    status = "ok" if cond else "FAIL"
    if verbose or not cond:
        print(f"  [{status}] {name}: {msg}")
    if not cond:
        failures.append(name)


def _check_isolation(failures, verbose):
    """Validate the HOME overlay: declared skills present, undeclared masked, vendor kept,
    auth/config passed through, missing ancestors built, plugin-registry skills masked
    without duplicating declared ones into unrelated plugins. Pure filesystem — no CLIs."""
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
        os.makedirs(os.path.join(real, "_cfg"))  # reproduce the config-mirror escape hazard
        open(os.path.join(real, ".codex", "auth.json"), "w").close()
        open(os.path.join(real, ".gitconfig"), "w").close()
        # a plugin registry sibling of .gemini/config/skills (both live under .gemini/config/,
        # exercising two different leaf types sharing an ancestor): one plugin mirrors this
        # repo's skills (the real-world leak — e.g. via `agy plugin import`), plus a vendor
        # skill in the same plugin, plus a second, unrelated vendor-only plugin.
        os.makedirs(os.path.join(real, ".gemini", "config", "plugins",
                                  "sliderule-skills", "skills", "sliderule-api"))
        os.makedirs(os.path.join(real, ".gemini", "config", "plugins",
                                  "sliderule-skills", "skills", "vendor-thing"))
        open(os.path.join(real, ".gemini", "config", "plugins",
                           "sliderule-skills", "plugin.json"), "w").close()
        os.makedirs(os.path.join(real, ".gemini", "config", "plugins",
                                  "other-plugin", "skills", "other-skill"))
        # the cell declares only sliderule-api (its source lives outside HOME, like skills_root)
        os.makedirs(os.path.join(declared_root, "sliderule-api"))

        build_isolated_home(
            dest,
            [".codex/skills", ".gemini/config/skills"],   # one present, one missing (nested)
            {"sliderule-api", "sliderule-params"},        # repo superset to mask
            [os.path.join(declared_root, "sliderule-api")],
            real,
            plugin_registry_subpaths=[".gemini/config/plugins"],
        )

        skills = os.path.join(dest, ".codex", "skills")
        names = set(os.listdir(skills)) if os.path.isdir(skills) else set()
        _check("isolation.declared_present", "sliderule-api" in names,
               f"declared sliderule-api present (got {sorted(names)})", failures, verbose)
        _check("isolation.declared_is_copy",
               os.path.isdir(os.path.join(skills, "sliderule-api"))
               and not os.path.islink(os.path.join(skills, "sliderule-api")),
               "declared skill is a copy (writes can't reach the source)", failures, verbose)
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

        plugin_skills = os.path.join(dest, ".gemini", "config", "plugins",
                                      "sliderule-skills", "skills")
        plugin_names = sorted(os.listdir(plugin_skills)) if os.path.isdir(plugin_skills) else []
        _check("isolation.plugin_repo_skill_masked", plugin_names == ["vendor-thing"],
               f"plugin's leaked repo skill dropped, its vendor skill kept "
               f"(got {plugin_names})", failures, verbose)
        _check("isolation.plugin_not_re_added",
               "sliderule-api" not in plugin_names,
               "declared skill isn't duplicated into an unrelated plugin's skills/ "
               "(it's already injected once via the primary skills dir)", failures, verbose)
        _check("isolation.plugin_metadata_passthrough",
               os.path.islink(os.path.join(dest, ".gemini", "config", "plugins",
                                            "sliderule-skills", "plugin.json")),
               "plugin.json passed through as a symlink", failures, verbose)
        other_plugin_skills = os.path.join(dest, ".gemini", "config", "plugins",
                                            "other-plugin", "skills")
        other_names = sorted(os.listdir(other_plugin_skills)) if os.path.isdir(other_plugin_skills) else []
        _check("isolation.unrelated_plugin_untouched", other_names == ["other-skill"],
               f"vendor-only plugin left alone (got {other_names})", failures, verbose)

        # config mirrors must use a fresh temp dir, not dest/_cfg — which is a symlink to the
        # real HOME's _cfg here, so writing through it would escape the temp tree.
        hazard = os.path.islink(os.path.join(dest, "_cfg"))
        cfg_root = tempfile.mkdtemp(prefix="cfg-", dir=dest)
        open(os.path.join(cfg_root, "mirror-marker"), "w").close()
        escaped = os.path.exists(os.path.join(real, "_cfg", "mirror-marker"))
        _check("isolation.cfg_mirror_no_escape", hazard and not escaped,
               f"config mirror stays in temp even when ~/_cfg exists "
               f"(hazard_present={hazard}, escaped={escaped})", failures, verbose)
    finally:
        for d in (real, declared_root, dest):
            shutil.rmtree(d, ignore_errors=True)


def _check_provision(failures, verbose):
    """Provisioned skills are copies, not symlinks, so a write inside one can't mutate the
    repo's skill source. Pure filesystem — no CLIs."""
    import os
    import shutil
    import tempfile

    print("skill provisioning:")
    src = tempfile.mkdtemp(prefix="ase-skillsrc-")
    ws = tempfile.mkdtemp(prefix="ase-ws-")
    try:
        open(os.path.join(src, "SKILL.md"), "w").close()
        get_adapter("claude").provision_skills(ws, [src])
        placed = os.path.join(ws, ".claude", "skills", os.path.basename(src))
        is_copy = os.path.isdir(placed) and not os.path.islink(placed)
        if is_copy:
            open(os.path.join(placed, "scratch.txt"), "w").close()
        source_clean = not os.path.exists(os.path.join(src, "scratch.txt"))
        _check("provision.copy_not_symlink", is_copy and source_clean,
               f"workspace skill is a copy; source unchanged (copy={is_copy}, clean={source_clean})",
               failures, verbose)
    finally:
        shutil.rmtree(ws, ignore_errors=True)
        shutil.rmtree(src, ignore_errors=True)


def _check_workspace_reset(failures, verbose):
    """A reused run-id must not let stale files survive into the next cell workspace."""
    import os
    import shutil
    import tempfile

    from .runner import _prepare_workspace

    print("workspace reset:")
    root = tempfile.mkdtemp(prefix="ase-cell-")
    try:
        ws = os.path.join(root, "workspace")
        os.makedirs(ws)
        with open(os.path.join(ws, "stale.txt"), "w") as fh:
            fh.write("old run")

        _prepare_workspace(ws)

        clean = os.path.isdir(ws) and not os.listdir(ws)
        _check("runner.workspace_reset", clean,
               f"existing per-cell workspace is recreated empty (clean={clean})",
               failures, verbose)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _check_report(failures, verbose):
    """The per-cell report.md renders the prompt, the full transcript, every produced file,
    and the judge verdict — and degrades gracefully when the judge is off. Pure — no CLIs."""
    import os
    import shutil
    import tempfile

    from .assertions import AssertionResult
    from .runner import CellResult, render_report
    from .schema import EventKind, NormalizedEvent, RunResult

    print("per-cell report:")
    root = tempfile.mkdtemp(prefix="ase-report-")
    try:
        cell_dir = os.path.join(root, "cell")
        ws = os.path.join(cell_dir, "workspace")
        os.makedirs(ws)
        with open(os.path.join(ws, "run.py"), "w") as fh:
            fh.write("print('hello from run.py')\n")

        rr = RunResult(
            agent="claude", eval_name="demo", prompt="Write run.py that prints hello.",
            workdir=ws, final_text="Done — created run.py.",
            events=[
                NormalizedEvent(EventKind.AGENT_MESSAGE, text="I'll create run.py."),
                NormalizedEvent(EventKind.TOOL_CALL, tool_name="Bash", command="python run.py"),
                NormalizedEvent(EventKind.FILE_CHANGE, path="run.py"),
            ],
            cost_usd=0.01, duration_ms=1234,
        )
        verdict = {"items": [
            {"behavior": "creates run.py", "pass": True, "reason": "file present"},
            {"behavior": "prints hello", "pass": False, "reason": "not verified"},
        ], "summary": "partially correct"}
        ja = AssertionResult("llm_judge", False, "1/2 rubric items", kind="judge", details=verdict)
        fa = AssertionResult("file_exists", True, "run.py exists")

        cell = CellResult(agent="claude", model="claude-haiku-4-5", eval_name="demo",
                          skill="scenario", passed=False, run_result=rr,
                          assertions=[fa, ja], artifacts_dir=cell_dir)
        md = render_report(cell)
        _check("report.prompt", "Write run.py that prints hello." in md,
               "prompt present", failures, verbose)
        _check("report.transcript", "python run.py" in md and "I'll create run.py." in md,
               "transcript shows assistant text + shell command", failures, verbose)
        _check("report.final", "Done — created run.py." in md,
               "final answer present", failures, verbose)
        _check("report.files", "run.py" in md and "hello from run.py" in md,
               "produced file inlined in full", failures, verbose)
        _check("report.judge", "partially correct" in md and "creates run.py" in md,
               "judge verdict + per-item reasons present", failures, verbose)
        _check("report.no_scenario_source", "**source:**" not in md,
               "no scenario_path set -> no source line", failures, verbose)

        # scenario_path set -> the report points back at the exact file that produced the run
        # (its `name:` is free text and can drift from the filename, so the path is the one
        # unambiguous pointer), instead of inlining the whole YAML.
        cell_scen = CellResult(agent="claude", model="claude-haiku-4-5", eval_name="demo",
                               skill="scenario", passed=False, run_result=rr,
                               assertions=[fa, ja], artifacts_dir=cell_dir,
                               scenario_path="/repo/scenarios/some_scenario.yaml")
        md_scen = render_report(cell_scen)
        _check("report.scenario_source_path",
               "- **source:** `/repo/scenarios/some_scenario.yaml`" in md_scen,
               f"scenario_path renders as a source line, not inlined YAML: "
               f"{[line for line in md_scen.splitlines() if 'source' in line]}",
               failures, verbose)

        # judge off → no llm_judge assertion: graceful note, but prompt + response stay.
        cell_off = CellResult(agent="claude", model=None, eval_name="demo",
                              skill="scenario", passed=True, run_result=rr,
                              assertions=[fa], artifacts_dir=cell_dir)
        md_off = render_report(cell_off)
        _check("report.judge_off_note", "judge for yourself" in md_off.lower(),
               "judge-off shows reviewer note", failures, verbose)
        _check("report.judge_off_keeps_evidence",
               "Write run.py that prints hello." in md_off and "hello from run.py" in md_off,
               "judge-off still shows prompt + produced files", failures, verbose)

        # judge artifacts — _write_judge_artifacts produces judge_* files + judge_report.md
        from .exec import ExecResult
        from .runner import Runner
        judge_rr = RunResult(
            agent="judge:claude", eval_name="demo",
            prompt="You are grading whether an AI coding agent completed a task correctly.",
            workdir="/tmp/judge-scratch",
            final_text='{"items": [{"behavior": "creates run.py", "pass": true, "reason": "ok"}], "summary": "pass"}',
            events=[NormalizedEvent(EventKind.AGENT_MESSAGE, text="Evaluating the rubric...")],
            cost_usd=0.002, duration_ms=500, resolved_model="claude-haiku-4-5",
        )
        judge_ex = ExecResult(result=judge_rr, stdout='{"type":"result"}\n', stderr="")
        runner_obj = Runner.__new__(Runner)
        runner_obj._write_judge_artifacts(cell_dir, cell, judge_ex)
        _check("report.judge_artifacts",
               all(os.path.isfile(os.path.join(cell_dir, f))
                   for f in ("judge_stdout.jsonl", "judge_stderr.txt", "judge_events.json",
                             "judge_result.json", "judge_report.md")),
               "all five judge_* files written", failures, verbose)
        judge_md = open(os.path.join(cell_dir, "judge_report.md")).read()
        _check("report.judge_report_prompt",
               "grading whether" in judge_md,
               "judge_report.md contains the grading prompt", failures, verbose)
        _check("report.judge_report_transcript",
               "Evaluating the rubric" in judge_md,
               "judge_report.md contains the judge's transcript", failures, verbose)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _check_path_resolution(failures, verbose):
    """file_exists must not pass on a seeded fixture the agent never produced, and tool-trace
    paths must resolve against the workspace (the agent's cwd), not the harness process cwd."""
    import os
    import shutil
    import tempfile

    from .assertions import _resolve_artifact
    from .schema import EventKind, NormalizedEvent, RunResult
    from .workspace_view import writes_outside_workspace

    print("path resolution:")
    ws = tempfile.mkdtemp(prefix="ase-paths-")
    outside = tempfile.mkdtemp(prefix="ase-outside-")
    try:
        # a seeded fixture lives in the workspace, but NOT at the path the assertion expects
        os.makedirs(os.path.join(ws, "fixtures"))
        open(os.path.join(ws, "fixtures", "report.md"), "w").close()

        def _rr(paths):
            return RunResult(agent="x", eval_name="e", prompt="", workdir=ws,
                             events=[NormalizedEvent(EventKind.FILE_CHANGE, path=p) for p in paths])

        # 1. a seeded fixture with a matching basename must NOT satisfy a different path (no false pass)
        path, where = _resolve_artifact(_rr([]), ws, "output/report.md")
        _check("paths.no_fixture_falsepass", path is None,
               f"seeded fixtures/report.md does not satisfy output/report.md (got {where})",
               failures, verbose)

        # 2. a RELATIVE trace path resolves under the workspace → found via write-trace
        os.makedirs(os.path.join(ws, "out"))
        open(os.path.join(ws, "out", "x.md"), "w").close()
        path, where = _resolve_artifact(_rr(["out/x.md"]), ws, "x.md")
        _check("paths.rel_trace_in_workspace",
               path == os.path.abspath(os.path.join(ws, "out", "x.md")) and where == "write-trace",
               f"relative trace resolved under the workspace (got {path}, {where})", failures, verbose)

        # 3. a relative trace path is NOT mis-reported as 'written outside the workspace'
        out = writes_outside_workspace(_rr(["out/x.md"]), ws)
        _check("paths.rel_not_outside", out == [],
               f"relative trace path not flagged outside the workspace (got {out})", failures, verbose)

        # 4. a genuinely-outside ABSOLUTE write IS surfaced
        abs_out = os.path.join(outside, "evil.md")
        open(abs_out, "w").close()
        out = writes_outside_workspace(_rr([abs_out]), ws)
        _check("paths.abs_outside_surfaced", out == [os.path.realpath(abs_out)],
               f"absolute outside write surfaced (got {out})", failures, verbose)

        # 5. relative symlink trace path that escapes workspace is blocked
        secret = os.path.join(outside, "secret.txt")
        open(secret, "w").close()
        os.symlink(outside, os.path.join(ws, "link"))
        path, where = _resolve_artifact(_rr(["link/secret.txt"]), ws, "secret.txt")
        _check("paths.symlink_rel_blocked", path is None,
               f"relative symlink trace escape blocked (got {path}, {where})",
               failures, verbose)

        # 6. absolute trace path through workspace symlink is also blocked
        abs_link_path = os.path.join(ws, "link", "secret.txt")
        path, where = _resolve_artifact(_rr([abs_link_path]), ws, "secret.txt")
        _check("paths.symlink_abs_blocked", path is None,
               f"absolute symlink trace escape blocked (got {path}, {where})",
               failures, verbose)

        # 7. genuinely-outside absolute trace path still passes
        path, where = _resolve_artifact(_rr([secret]), ws, "secret.txt")
        _check("paths.outside_abs_passes",
               path == os.path.abspath(secret) and where == "write-trace",
               f"genuine outside absolute trace passes (got {path}, {where})",
               failures, verbose)

        # 8. normal inside trace path through no symlink still works
        normal = os.path.join(ws, "sub")
        os.makedirs(normal, exist_ok=True)
        open(os.path.join(normal, "ok.txt"), "w").close()
        path, where = _resolve_artifact(_rr(["sub/ok.txt"]), ws, "ok.txt")
        _check("paths.normal_inside_passes",
               path is not None and where == "write-trace",
               f"normal inside trace passes (got {path}, {where})",
               failures, verbose)

        # 9. writes_outside_workspace must resolve the same `ws/link -> outside` symlink from
        #    case 5/6: a bare abspath check sees `link/secret.txt` as textually inside ws and
        #    would miss that the write really landed outside.
        out = writes_outside_workspace(_rr(["link/secret.txt"]), ws)
        _check("paths.symlink_write_surfaced_as_outside",
               out == [os.path.realpath(secret)],
               f"write through a workspace-internal symlink is surfaced as outside (got {out})",
               failures, verbose)
    finally:
        shutil.rmtree(ws, ignore_errors=True)
        shutil.rmtree(outside, ignore_errors=True)


def _check_workspace_view_skill_dir_match(failures, verbose):
    """file_tree/inline_files must exclude the provisioned skill dirs (.claude/.agents/etc.) by
    path SEGMENT, not bare string prefix — a real top-level dir the model creates named e.g.
    `.codexnotes` must not be swallowed just because it starts with `.codex`."""
    import os
    import shutil
    import tempfile

    from .workspace_view import file_tree

    print("workspace_view skill-dir matching:")
    ws = tempfile.mkdtemp(prefix="ase-skilldir-")
    try:
        os.makedirs(os.path.join(ws, ".codex"))
        open(os.path.join(ws, ".codex", "SKILL.md"), "w").close()
        os.makedirs(os.path.join(ws, ".codexnotes"))
        open(os.path.join(ws, ".codexnotes", "real_output.txt"), "w").close()

        tree = file_tree(ws)
        _check("workspace_view.lookalike_dir_kept", "real_output.txt" in tree,
               f"a real dir merely PREFIXED by a skill-dir name is not swallowed: {tree!r}",
               failures, verbose)
        _check("workspace_view.real_skill_dir_excluded", "SKILL.md" not in tree,
               f"the actual provisioned skill dir is still excluded: {tree!r}", failures, verbose)
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def _check_leaked_skill_reads(failures, verbose):
    """Reproduces the antigravity escape from run 20260707-072933_scen_SimpleATL06PromptGrandMesa:
    the eval workspace is nested inside this repo's own checkout (``<repo>/artifacts/<run>/.../
    workspace``); ``git init`` in the workspace (runner.py) stops a skill-discovery mechanism that
    deliberately halts its walk-up at the nearest ``.git``, but does nothing against a
    general-purpose file-browsing agent that just ``list_dir``s a parent directory by absolute
    path and reads whatever undeclared skill sits there in plain sight. The real transcript showed
    exactly that: `list_dir` on the scenario dir, then the repo root, then `view_file` on
    ``sliderule-api/SKILL.md`` and ``sliderule-openapi/scripts/openapi.py`` — none of which were
    declared — while the run was still reported as ``isolated: true``.

    ``leaked_skill_reads`` must catch this after the fact from the trace: an undeclared repo
    skill read via an absolute path that resolves under the real repo root but outside the
    workspace copy."""
    import os
    import shutil
    import tempfile

    from .schema import EventKind, NormalizedEvent, RunResult
    from .workspace_view import leaked_skill_reads

    print("leaked skill reads (hermeticity escape):")
    repo_root = tempfile.mkdtemp(prefix="ase-repo-")
    try:
        # Mirror the real layout: workspace nested inside the repo checkout, sibling to the
        # repo's own skill directories.
        for name in ("sliderule-api", "sliderule-openapi", "sliderule-examples"):
            os.makedirs(os.path.join(repo_root, name))
            with open(os.path.join(repo_root, name, "SKILL.md"), "w") as f:
                f.write("---\nname: " + name + "\n---\n")
        workspace = os.path.join(repo_root, "artifacts", "run1", "model", "scenario",
                                  "SimpleATL06PromptGrandMesa", "workspace")
        os.makedirs(workspace)

        def _rr(events):
            return RunResult(agent="antigravity", eval_name="e", prompt="", workdir=workspace,
                             events=events)

        repo_skill_names = {"sliderule-api", "sliderule-openapi", "sliderule-examples"}

        # 1. the real escape: list_dir/view_file on the undeclared sliderule-api SKILL.md via
        #    the repo's real absolute path — no skill declared for this eval at all.
        leak_path = os.path.join(repo_root, "sliderule-api", "SKILL.md")
        rr = _rr([NormalizedEvent(EventKind.TOOL_CALL, tool_name="view_file", path=leak_path)])
        leaks = leaked_skill_reads(rr, workspace, repo_root, repo_skill_names, declared_names=set())
        _check("leak.undeclared_skill_read_detected", leaks == [os.path.realpath(leak_path)],
               f"undeclared skill read via real repo path is caught: {leaks}", failures, verbose)

        # 2. a read of a DECLARED skill's real path is not a leak (it's expected the model was
        #    given this one).
        rr2 = _rr([NormalizedEvent(EventKind.TOOL_CALL, tool_name="view_file", path=leak_path)])
        leaks2 = leaked_skill_reads(rr2, workspace, repo_root, repo_skill_names,
                                    declared_names={"sliderule-api"})
        _check("leak.declared_skill_not_flagged", leaks2 == [],
               f"declared skill's real-path read is not a leak: {leaks2}", failures, verbose)

        # 3. reading the provisioned COPY inside the workspace itself is not a leak.
        prov_dir = os.path.join(workspace, ".antigravity", "skills", "sliderule-api")
        os.makedirs(prov_dir)
        prov_path = os.path.join(prov_dir, "SKILL.md")
        open(prov_path, "w").close()
        rr3 = _rr([NormalizedEvent(EventKind.TOOL_CALL, tool_name="view_file", path=prov_path)])
        leaks3 = leaked_skill_reads(rr3, workspace, repo_root, repo_skill_names, declared_names=set())
        _check("leak.workspace_copy_not_flagged", leaks3 == [],
               f"provisioned in-workspace copy is not a leak: {leaks3}", failures, verbose)

        # 4. a run_command whose command string references the undeclared skill's real script
        #    path (e.g. `python /repo/sliderule-openapi/scripts/openapi.py ...`) is also caught.
        script = os.path.join(repo_root, "sliderule-openapi", "scripts", "openapi.py")
        cmd = f"conda run -n env python {script} applies-to atl06x"
        rr4 = _rr([NormalizedEvent(EventKind.TOOL_CALL, tool_name="run_command", command=cmd)])
        leaks4 = leaked_skill_reads(rr4, workspace, repo_root, repo_skill_names, declared_names=set())
        _check("leak.command_reference_detected", leaks4 == [os.path.realpath(script)],
               f"undeclared skill script referenced in a command is caught: {leaks4}",
               failures, verbose)

        # 4b. the marker embedded MID-TOKEN (e.g. --script=/repo/...), not just at the start of
        # the token, must also be caught — a bare startswith() check misses this shape entirely.
        cmd_embedded = f"python --script={script} --verbose"
        rr4b = _rr([NormalizedEvent(EventKind.TOOL_CALL, tool_name="run_command",
                                    command=cmd_embedded)])
        leaks4b = leaked_skill_reads(rr4b, workspace, repo_root, repo_skill_names,
                                     declared_names=set())
        _check("leak.command_reference_mid_token_detected",
               leaks4b == [os.path.realpath(script)],
               f"undeclared skill script embedded mid-token (--script=...) is caught: {leaks4b}",
               failures, verbose)

        # 5. no leaked names at all (every repo skill declared) -> no leaks, no filesystem work.
        rr5 = _rr([NormalizedEvent(EventKind.TOOL_CALL, tool_name="view_file", path=leak_path)])
        leaks5 = leaked_skill_reads(rr5, workspace, repo_root, repo_skill_names,
                                    declared_names=repo_skill_names)
        _check("leak.all_declared_short_circuits", leaks5 == [],
               f"nothing to leak once every repo skill is declared: {leaks5}", failures, verbose)

        # 6. a symlink planted inside the workspace pointing at an undeclared skill IS caught —
        #    a bare abspath check sees `workspace/evil/SKILL.md` as textually "inside" and would
        #    miss it entirely; realpath resolution follows it to the real, undeclared target.
        evil_link = os.path.join(workspace, "evil")
        os.symlink(os.path.join(repo_root, "sliderule-api"), evil_link)
        rr6 = _rr([NormalizedEvent(EventKind.TOOL_CALL, tool_name="view_file",
                                   path=os.path.join(evil_link, "SKILL.md"))])
        leaks6 = leaked_skill_reads(rr6, workspace, repo_root, repo_skill_names, declared_names=set())
        _check("leak.symlink_escape_detected",
               leaks6 == [os.path.realpath(os.path.join(repo_root, "sliderule-api", "SKILL.md"))],
               f"symlink from inside workspace to an undeclared skill is caught: {leaks6}",
               failures, verbose)
    finally:
        shutil.rmtree(repo_root, ignore_errors=True)


class _FakeAdapter:
    """Bare-minimum adapter stand-in for _check_workspace_relocation — no real CLI is invoked
    (execute() itself is monkeypatched), this only needs to satisfy the attributes _run_cell
    reads before it gets there."""
    global_skills_subpaths: list[str] = []
    skills_subdir = "fake/skills"


def _check_workspace_relocation(failures, verbose):
    """Under isolation, _run_cell must execute the agent in a tempdir with NO path
    relationship to this repo's checkout — not `<repo>/artifacts/.../workspace`, which let
    antigravity `list_dir` its way up two directories into the real repo's undeclared skills
    (run 20260707-072933_scen_SimpleATL06PromptGrandMesa; see _check_leaked_skill_reads).
    Monkeypatches `execute()` so no real agent CLI runs, drives `_run_cell` end to end, and
    checks: (1) the cwd the fake agent saw is outside the repo tree, (2) the file it "produced"
    still ends up in cell_dir/workspace for artifacts/report, (3) the temp exec dir is gone
    afterward."""
    import os
    import shutil
    import tempfile

    import agentskill_evals.runner as runner_mod
    from .exec import ExecResult
    from .schema import EventKind, NormalizedEvent, RunResult
    from .spec import EvalSpec

    print("workspace relocation (exec dir escapes the repo tree):")
    repo_root = tempfile.mkdtemp(prefix="ase-repo2-")
    seen: dict = {}
    orig_execute = runner_mod.execute

    def _fake_execute(adapter, prompt, opts, *, cwd, timeout, env_overrides, agent_name, eval_name):
        seen["cwd"] = cwd
        with open(os.path.join(cwd, "run.py"), "w") as f:
            f.write("print('hi')\n")
        rr = RunResult(
            agent=agent_name, eval_name=eval_name, prompt=prompt, workdir=cwd,
            events=[NormalizedEvent(EventKind.FILE_CHANGE, path="run.py")],
            final_text="done",
        )
        return ExecResult(result=rr, stdout="", stderr="")

    runner_mod.execute = _fake_execute
    try:
        run_dir = os.path.join(repo_root, "artifacts", "run1")
        os.makedirs(run_dir)
        r = runner_mod.Runner.__new__(runner_mod.Runner)
        r.agent, r.adapter, r.models = "fake", _FakeAdapter(), [None]
        r.artifacts_root = os.path.join(repo_root, "artifacts")
        r.run_id, r.skills_root, r.judge = "run1", repo_root, None
        r.provision, r.command, r.auto_approve = False, "", True
        r.jobs, r.isolated, r.progress = 1, True, None
        r._repo_skill_names, r.run_dir = set(), run_dir

        spec = EvalSpec(name="demo", prompt="hi", source_path=os.path.join(repo_root, "demo.yaml"))
        cell = r._run_cell(None, spec)

        cell_workspace = os.path.join(cell.artifacts_dir, "workspace")
        cwd_used = seen.get("cwd")
        outside = cwd_used is not None and not (
            os.path.abspath(cwd_used) == os.path.abspath(repo_root)
            or os.path.abspath(cwd_used).startswith(os.path.abspath(repo_root) + os.sep)
        )
        _check("relocate.exec_cwd_outside_repo", outside,
               f"exec cwd is outside the repo checkout (got {cwd_used})", failures, verbose)
        _check("relocate.produced_file_in_artifacts",
               os.path.isfile(os.path.join(cell_workspace, "run.py")),
               "produced file still lands in cell_dir/workspace for artifacts", failures, verbose)
        _check("relocate.temp_exec_dir_cleaned",
               cwd_used is not None and not os.path.isdir(cwd_used),
               "temp exec dir removed after copy-back", failures, verbose)
    finally:
        runner_mod.execute = orig_execute
        shutil.rmtree(repo_root, ignore_errors=True)


def _check_parallel_cell_idx(failures, verbose):
    """Under --jobs>1, each future submitted to the pool must carry its OWN cell_idx/total —
    before the fix, `pool.submit(self._run_cell, m, s)` passed neither, so every parallel cell
    defaulted to cell_idx=0: phase updates were indistinguishable ("cell 0/N" for every cell),
    and `if p and cell_idx:` inside _run_cell_body is falsy for 0, so its own p.done() call never
    fired at all — run()'s parallel branch called p.done() separately instead, using
    as_completed()'s completion ORDER as the cell number, not the cell's real identity."""
    import os
    import shutil
    import tempfile

    import agentskill_evals.runner as runner_mod
    from .exec import ExecResult
    from .progress import Progress
    from .schema import RunResult
    from .spec import EvalSpec

    print("parallel cell_idx assignment:")
    repo_root = tempfile.mkdtemp(prefix="ase-repo4-")
    seen_cell_idxs: list = []
    orig_execute = runner_mod.execute

    def _fake_execute(adapter, prompt, opts, *, cwd, timeout, env_overrides, agent_name, eval_name):
        rr = RunResult(agent=agent_name, eval_name=eval_name, prompt=prompt, workdir=cwd,
                       final_text="done")
        return ExecResult(result=rr, stdout="", stderr="")

    class _RecordingProgress(Progress):
        def done(self, *, cell, passed=None, cost=""):
            seen_cell_idxs.append(cell)

    runner_mod.execute = _fake_execute
    try:
        run_dir = os.path.join(repo_root, "artifacts", "run1")
        os.makedirs(run_dir)
        r = runner_mod.Runner.__new__(runner_mod.Runner)
        r.agent, r.adapter, r.models = "fake", _FakeAdapter(), [None]
        r.artifacts_root = os.path.join(repo_root, "artifacts")
        r.run_id, r.skills_root, r.judge = "run1", repo_root, None
        r.provision, r.command, r.auto_approve = False, "", True
        r.jobs, r.isolated = 2, True
        r.progress = _RecordingProgress(total_cells=2, file=open(os.devnull, "w"))
        r._repo_skill_names, r.run_dir = set(), run_dir

        specs = [EvalSpec(name=f"demo{i}", prompt="hi",
                          source_path=os.path.join(repo_root, f"demo{i}.yaml"))
                for i in range(2)]
        r.run(specs)

        _check("runner.parallel_cell_idx_nonzero",
               len(seen_cell_idxs) == 2 and all(c != 0 for c in seen_cell_idxs),
               f"each parallel cell's own p.done() call fires with a real (nonzero) cell_idx: "
               f"{seen_cell_idxs}", failures, verbose)
        _check("runner.parallel_cell_idx_distinct", sorted(seen_cell_idxs) == [1, 2],
               f"the 2 cells get distinct indices matching their actual identity, not both "
               f"0 or a duplicate: {seen_cell_idxs}", failures, verbose)
    finally:
        runner_mod.execute = orig_execute
        shutil.rmtree(repo_root, ignore_errors=True)


def _check_cell_crash_safety(failures, verbose):
    """A cell that raises mid-run (a network blip inside execute(), a buggy assertion, ...) must
    not propagate out of _run_cell: run() has no try/except of its own around the call, so an
    uncaught exception here would abort every OTHER cell in the batch too. _run_cell must catch
    it, still write SOME artifacts (report.md/assertions.json) recording the failure, and clean
    up the exec_ws tempdir rather than leaking it."""
    import os
    import shutil
    import tempfile

    import agentskill_evals.runner as runner_mod
    from .spec import EvalSpec

    print("cell crash safety:")
    repo_root = tempfile.mkdtemp(prefix="ase-repo3-")
    seen: dict = {}
    orig_execute = runner_mod.execute

    def _crashing_execute(adapter, prompt, opts, *, cwd, timeout, env_overrides, agent_name, eval_name):
        seen["cwd"] = cwd
        raise RuntimeError("simulated mid-run crash")

    runner_mod.execute = _crashing_execute
    try:
        run_dir = os.path.join(repo_root, "artifacts", "run1")
        os.makedirs(run_dir)
        r = runner_mod.Runner.__new__(runner_mod.Runner)
        r.agent, r.adapter, r.models = "fake", _FakeAdapter(), [None]
        r.artifacts_root = os.path.join(repo_root, "artifacts")
        r.run_id, r.skills_root, r.judge = "run1", repo_root, None
        r.provision, r.command, r.auto_approve = False, "", True
        r.jobs, r.isolated, r.progress = 1, True, None
        r._repo_skill_names, r.run_dir = set(), run_dir

        spec = EvalSpec(name="demo", prompt="hi", source_path=os.path.join(repo_root, "demo.yaml"))
        cell = None
        raised = False
        try:
            cell = r._run_cell(None, spec)
        except Exception:
            raised = True

        _check("crash.no_exception_propagates", not raised and cell is not None,
               "a raising cell returns a CellResult instead of propagating", failures, verbose)
        if cell is not None:
            _check("crash.marked_failed", cell.passed is False,
                   f"crashed cell is recorded as failed (passed={cell.passed})",
                   failures, verbose)
            _check("crash.error_recorded", "simulated mid-run crash" in (cell.run_result.error or ""),
                   f"the exception message is preserved: {cell.run_result.error!r}",
                   failures, verbose)
            report_path = os.path.join(cell.artifacts_dir, "report.md")
            _check("crash.report_written", os.path.isfile(report_path),
                   "report.md is still written for a crashed cell", failures, verbose)
        cwd_used = seen.get("cwd")
        _check("crash.exec_ws_cleaned_up",
               cwd_used is not None and not os.path.isdir(cwd_used),
               f"exec_ws tempdir is removed even though the cell crashed (got {cwd_used})",
               failures, verbose)
    finally:
        runner_mod.execute = orig_execute
        shutil.rmtree(repo_root, ignore_errors=True)


def _check_progress_thread_safety(failures, verbose):
    """Under --jobs>1, every worker thread shares one Progress instance. Before the fix, update()
    mutated shared state under a lock but then printed OUTSIDE it — another thread's update()
    could interleave between "mutate" and "print", producing a torn line combining one thread's
    cell number with another's phase/label. Drives many concurrent update() calls from several
    threads and checks every printed line is internally coherent."""
    import io
    import re
    import threading

    from .progress import Progress

    print("progress thread safety:")
    buf = io.StringIO()
    p = Progress(total_cells=4, file=buf)

    def worker(n):
        for _ in range(50):
            p.update(cell=n, phase=f"phase-{n}", eval_name=f"eval-{n}", model=f"model-{n}")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(1, 5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    torn = []
    for ln in lines:
        m = re.search(r"cell (\d+)/4", ln)
        if not m:
            continue
        this_n = m.group(1)
        for other in "1234":
            if other == this_n:
                continue
            if f"phase-{other}" in ln or f"eval-{other}" in ln or f"model-{other}" in ln:
                torn.append(ln)
    _check("progress.thread_safety_no_torn_lines", not torn and len(lines) > 0,
           f"no printed line mixes one cell's number with another cell's phase/label "
           f"({len(lines)} lines checked, torn={torn[:3]})", failures, verbose)


def _check_cli_helpers(failures, verbose):
    """Pure cli.py helpers, testable without any agent CLI or PyYAML: the yaml.YAMLError
    detection that gives malformed-YAML scenario/eval loads a clean error instead of a raw
    traceback, the --model/--all-models conflict warning, and _load_models_config's structural
    validation (duplicate model / missing default warnings). cli.py otherwise has zero selftest
    coverage — these are the pieces cheap to exercise without driving the full CLI."""
    import contextlib
    import io
    import os
    import tempfile

    from .cli import ModelsConfig, _is_yaml_error, _load_models_config, _resolve_models

    print("cli.py helpers:")

    _check("cli.is_yaml_error.plain_exception_false",
           _is_yaml_error(ValueError("not a yaml error")) is False,
           "an ordinary exception is not mistaken for a YAML error", failures, verbose)
    try:
        import yaml  # type: ignore
        has_yaml = True
    except ModuleNotFoundError:
        has_yaml = False
    if has_yaml:
        try:
            yaml.safe_load("key: [unterminated")
        except yaml.YAMLError as exc:
            _check("cli.is_yaml_error.real_yaml_error", _is_yaml_error(exc),
                   "a real yaml.YAMLError is recognized", failures, verbose)
        else:
            _check("cli.is_yaml_error.real_yaml_error", False,
                   "expected malformed YAML to raise", failures, verbose)
    elif verbose:
        print("  [skipped — PyYAML not installed] cli.is_yaml_error.real_yaml_error")

    # --model + --all-models: --model wins, and a warning is printed (not silently dropped)
    cfg = ModelsConfig({"claude": ["a", "b"]}, {"claude": "a"}, {}, [])
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        models = _resolve_models("claude", ["explicit"], cfg, all_models=True)
    _check("cli.resolve_models.explicit_wins", models == ["explicit"],
           f"--model wins over --all-models: {models}", failures, verbose)
    _check("cli.resolve_models.conflict_warned", "ignored" in buf.getvalue(),
           f"a warning is printed instead of silently dropping --all-models: {buf.getvalue()!r}",
           failures, verbose)

    # _load_models_config: duplicate model + missing default warnings (JSON, so no PyYAML needed)
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    try:
        tmp.write('{"agents": {"claude": {"models": ["a", "a"]}}}')
        tmp.close()
        mc = _load_models_config(tmp.name)
        _check("cli.load_models_config.duplicate_warned",
               any("more than once" in w for w in mc.warnings),
               f"duplicate model listing warns: {mc.warnings}", failures, verbose)
        _check("cli.load_models_config.no_default_warned",
               any("no `default:`" in w for w in mc.warnings),
               f"missing default warns: {mc.warnings}", failures, verbose)
    finally:
        os.unlink(tmp.name)


def _check_assertion_pass_fail(failures, verbose):
    """Real pass/fail coverage for every deterministic assertion type via run_assertion() — not
    just "is this cfg structurally valid" (spec validation already covers that), but "does the
    function actually pass on a matching run and fail on a non-matching one." Before this, only
    llm_judge was exercised through run_assertion(); a regression flipping e.g.
    skill_not_triggered's `if not hits` to `if hits`, or breaking tool_count's bounds check, would
    have passed every existing selftest."""
    import os
    import shutil
    import tempfile

    from .assertions import AssertionContext, run_assertion
    from .schema import EventKind, NormalizedEvent, RunResult
    from .spec import EvalSpec

    print("assertion pass/fail:")
    ws = tempfile.mkdtemp(prefix="ase-assertions-")
    try:
        with open(os.path.join(ws, "run.py"), "w") as f:
            f.write("print('hello')\n")
        os.makedirs(os.path.join(ws, "subdir"))

        events = [
            NormalizedEvent(EventKind.TOOL_CALL, tool_name="Bash", command="python run.py"),
            NormalizedEvent(EventKind.TOOL_CALL, tool_name="Read",
                            path=".claude/skills/sliderule-api/references/foo.md"),
            NormalizedEvent(EventKind.TOOL_CALL, tool_name="Bash",
                            path=".claude/skills/sliderule-api/scripts/bar.py"),
        ]
        rr = RunResult(agent="x", eval_name="e", prompt="", workdir=ws, events=events,
                       exit_code=0, final_text="Done, printed hello",
                       structured_output={"ok": True})
        spec = EvalSpec(name="t", prompt="p", source_path="/x.yaml", skills=["sliderule-api"])
        ctx = AssertionContext(spec=spec, skills_subdir=".claude/skills")

        def _pf(cfg, expect_pass, label):
            ar = run_assertion(cfg, rr, ws, spec, ctx)
            _check(f"assertion.{label}", ar.passed is expect_pass,
                   f"{cfg} -> passed={ar.passed} (want {expect_pass}): {ar.message}",
                   failures, verbose)

        _pf({"type": "file_exists", "path": "run.py"}, True, "file_exists.pass")
        _pf({"type": "file_exists", "path": "missing.txt"}, False, "file_exists.fail")

        _pf({"type": "file_absent", "path": "missing.txt"}, True, "file_absent.pass")
        _pf({"type": "file_absent", "path": "run.py"}, False, "file_absent.fail")

        _pf({"type": "dir_exists", "path": "subdir"}, True, "dir_exists.pass")
        _pf({"type": "dir_exists", "path": "no_such_dir"}, False, "dir_exists.fail")

        _pf({"type": "ran_command", "contains": "python run.py"}, True, "ran_command.pass")
        _pf({"type": "ran_command", "contains": "totally different command"}, False,
            "ran_command.fail")

        _pf({"type": "used_tool", "name": "Bash"}, True, "used_tool.pass")
        _pf({"type": "used_tool", "name": "Nonexistent"}, False, "used_tool.fail")

        _pf({"type": "tool_count", "min": 0, "max": 10}, True, "tool_count.pass")
        _pf({"type": "tool_count", "min": 100}, False, "tool_count.fail")

        _pf({"type": "skill_triggered", "skill": "sliderule-api"}, True, "skill_triggered.pass")
        _pf({"type": "skill_triggered", "skill": "sliderule-other"}, False,
            "skill_triggered.fail")

        _pf({"type": "skill_not_triggered", "skill": "sliderule-other"}, True,
            "skill_not_triggered.pass")
        _pf({"type": "skill_not_triggered", "skill": "sliderule-api"}, False,
            "skill_not_triggered.fail")

        _pf({"type": "skill_reference_read", "skill": "sliderule-api"}, True,
            "skill_reference_read.pass")
        _pf({"type": "skill_reference_read", "skill": "sliderule-other"}, False,
            "skill_reference_read.fail")

        _pf({"type": "skill_reference_not_read", "skill": "sliderule-other"}, True,
            "skill_reference_not_read.pass")
        _pf({"type": "skill_reference_not_read", "skill": "sliderule-api"}, False,
            "skill_reference_not_read.fail")

        _pf({"type": "skill_script_executed", "skill": "sliderule-api"}, True,
            "skill_script_executed.pass")
        _pf({"type": "skill_script_executed", "skill": "sliderule-other"}, False,
            "skill_script_executed.fail")

        _pf({"type": "skill_script_not_executed", "skill": "sliderule-other"}, True,
            "skill_script_not_executed.pass")
        _pf({"type": "skill_script_not_executed", "skill": "sliderule-api"}, False,
            "skill_script_not_executed.fail")

        _pf({"type": "exit_code", "equals": 0}, True, "exit_code.pass")
        _pf({"type": "exit_code", "equals": 1}, False, "exit_code.fail")

        _pf({"type": "no_error"}, True, "no_error.pass")
        rr_err = RunResult(agent="x", eval_name="e", prompt="", workdir=ws, events=[],
                           exit_code=1, error="boom")
        ar_err = run_assertion({"type": "no_error"}, rr_err, ws, spec, ctx)
        _check("assertion.no_error.fail", ar_err.passed is False,
               f"no_error fails on a run with an error: {ar_err.message}", failures, verbose)

        _pf({"type": "final_contains", "contains": "hello"}, True, "final_contains.pass")
        _pf({"type": "final_contains", "contains": "goodbye"}, False, "final_contains.fail")

        _pf({"type": "output_matches_schema",
            "schema": {"type": "object", "required": ["ok"]}}, True,
            "output_matches_schema.pass")
        _pf({"type": "output_matches_schema",
            "schema": {"type": "object", "required": ["nonexistent_field"]}}, False,
            "output_matches_schema.fail")

        # _escapes_workspace guard: file_exists/file_absent/dir_exists must reject a path that
        # normalizes outside the workspace, not crash or (worse) silently check the real
        # filesystem at that absolute location.
        for atype in ("file_exists", "file_absent", "dir_exists"):
            ar = run_assertion({"type": atype, "path": "../../etc/passwd"}, rr, ws, spec, ctx)
            _check(f"assertion.{atype}.escapes_workspace_rejected",
                   ar.passed is False and "escapes workspace" in ar.message,
                   f"{atype} rejects a path escaping the workspace: {ar.message}",
                   failures, verbose)
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def _check_verdict_coercion(failures, verbose):
    """Top-level and per-item verdict 'pass' coercion edge cases."""
    from .assertions import AssertionContext, run_assertion
    from .judge import _coerce_verdict
    from .schema import RunResult

    print("verdict coercion:")

    rr = RunResult(agent="x", eval_name="e", prompt="", workdir="/tmp")

    # "pass": "false" at item level must be coerced to False — by the REAL _coerce_verdict, fed
    # through its actual final_text -> JSON -> per-item coercion path (not a hand-duplicated
    # snippet re-run on a literal, which would still pass even if _coerce_verdict itself broke).
    rr_false = RunResult(
        agent="x", eval_name="e", prompt="", workdir="/tmp",
        final_text='{"items": [{"behavior": "a", "pass": "false", "reason": "r"}], "summary": "s"}',
    )
    v1 = _coerce_verdict(rr_false, ["a"])
    _check("verdict.item_false_str", v1["items"][0]["pass"] is False,
           f"item 'pass': 'false' coerced to False by the real _coerce_verdict: {v1}",
           failures, verbose)

    # "pass": "true" at item level must be coerced to True, likewise through the real function.
    rr_true = RunResult(
        agent="x", eval_name="e", prompt="", workdir="/tmp",
        final_text='{"items": [{"behavior": "a", "pass": "true", "reason": "r"}], "summary": "s"}',
    )
    v2 = _coerce_verdict(rr_true, ["a"])
    _check("verdict.item_true_str", v2["items"][0]["pass"] is True,
           f"item 'pass': 'true' coerced to True by the real _coerce_verdict: {v2}",
           failures, verbose)

    # top-level {"items": [], "pass": "false"} must fail
    def _fake_judge(**kw):
        from dataclasses import dataclass
        @dataclass
        class FakeJR:
            verdict: dict
        return FakeJR(verdict={"items": [], "pass": "false", "summary": "nope"})

    spec = EvalSpec(name="t", prompt="p", source_path="/x.yaml", rubric=["a"])
    ctx = AssertionContext(spec=spec, judge=_fake_judge)
    ar = run_assertion({"type": "llm_judge", "rubric": ["a"]}, rr, "/tmp", spec, ctx)
    _check("verdict.toplevel_false_str", ar.passed is False,
           f"top-level 'pass': 'false' must fail (got passed={ar.passed})",
           failures, verbose)

    # top-level {"items": [], "pass": "true"} with a REAL rubric must NOT bypass real scoring —
    # a judge that ignores the "one entry per rubric item" instruction and returns zero items
    # scored zero of the rubric; a bare top-level 'pass': true must not override that into a pass.
    def _fake_judge_bypass(**kw):
        from dataclasses import dataclass
        @dataclass
        class FakeJR:
            verdict: dict
        return FakeJR(verdict={"items": [], "pass": "true", "summary": "judge ignored the rubric"})

    ctx_bypass = AssertionContext(spec=spec, judge=_fake_judge_bypass)
    ar_bypass = run_assertion({"type": "llm_judge", "rubric": ["a", "b", "c"]}, rr, "/tmp",
                              spec, ctx_bypass)
    _check("verdict.toplevel_true_no_bypass", ar_bypass.passed is False,
           f"bare top-level 'pass': true must not bypass zero-item scoring against a real "
           f"rubric (got passed={ar_bypass.passed})", failures, verbose)

    # extra judge items beyond rubric length must not inflate score
    def _fake_judge_extra(**kw):
        from dataclasses import dataclass
        @dataclass
        class FakeJR:
            verdict: dict
        return FakeJR(verdict={
            "items": [
                {"behavior": "a", "pass": False, "reason": "no"},
                {"behavior": "b", "pass": False, "reason": "no"},
                {"behavior": "c", "pass": False, "reason": "no"},
                {"behavior": "extra1", "pass": True, "reason": "y"},
                {"behavior": "extra2", "pass": True, "reason": "y"},
                {"behavior": "extra3", "pass": True, "reason": "y"},
            ],
            "summary": "mixed",
        })

    ctx2 = AssertionContext(spec=spec, judge=_fake_judge_extra)
    spec3 = EvalSpec(name="t", prompt="p", source_path="/x.yaml", rubric=["a", "b", "c"])
    ar2 = run_assertion({"type": "llm_judge", "rubric": ["a", "b", "c"]}, rr, "/tmp", spec3, ctx2)
    _check("verdict.extra_items_no_inflate", ar2.passed is False,
           f"3 failed + 3 extra passing must not pass a 3-item rubric (got passed={ar2.passed})",
           failures, verbose)


def _check_snake_case_keys(failures, verbose):
    """A naive "insert _ before every capital" rule mangles acronyms (`URLPath` -> `u_r_l_path`),
    which would silently fail to match extract_command/extract_path's key lists for any future
    acronym-bearing tool-arg key. Pure — no CLIs."""
    from .adapters.base import snake_case_keys

    print("snake_case_keys:")
    out = snake_case_keys({"TargetFile": "a", "CommandLine": "b", "URLPath": "c", "APIKey": "d"})
    _check("snake_case.plain_pascal",
           out.get("target_file") == "a" and out.get("command_line") == "b",
           f"ordinary PascalCase keys unaffected: {out}", failures, verbose)
    _check("snake_case.acronym_not_mangled",
           out.get("url_path") == "c" and out.get("api_key") == "d",
           f"acronym-bearing keys keep the acronym as one segment (not u_r_l_path): {out}",
           failures, verbose)


def _check_antigravity_transcript(failures, verbose):
    """AntiGravity's --output-format json result carries no tool trace itself (just the
    final answer) — parse() has to go read the on-disk transcript, keyed by the result's
    conversation_id, to recover it. Builds a fake isolated HOME with that transcript planted
    at the real on-disk layout and drives parse() through opts.home, like a real isolated
    run would. Pure filesystem — no CLIs."""
    import os
    import shutil
    import tempfile

    print("antigravity transcript trace:")
    home = tempfile.mkdtemp(prefix="ase-agy-home-")
    try:
        log_dir = os.path.join(home, ".gemini", "antigravity-cli", "brain", "conv-test-1",
                                ".system_generated", "logs")
        os.makedirs(log_dir)
        with open(os.path.join(log_dir, "transcript_full.jsonl"), "w") as f:
            f.write(ANTIGRAVITY_TRANSCRIPT)

        out = get_adapter("antigravity").parse(
            ANTIGRAVITY_JSON_RESULT, "", 0, opts=RunOptions(home=home)
        )
        _check("antigravity.transcript.final", out.final_text == "Done building demo-app.",
               repr(out.final_text), failures, verbose)
        _check("antigravity.transcript.duration_ms", out.duration_ms == 1500,
               f"1.5s duration_seconds -> {out.duration_ms}ms", failures, verbose)

        cmds = [e.command for e in out.events if e.command]
        _check("antigravity.transcript.command",
               cmds == ["npm install"],
               f"CommandLine (PascalCase) extracted via snake_case_keys: {cmds}",
               failures, verbose)

        paths = [e.path for e in out.events if e.path]
        _check("antigravity.transcript.file_path", "package.json" in paths,
               f"TargetFile (PascalCase) extracted via snake_case_keys: {paths}",
               failures, verbose)
        _check("antigravity.transcript.skill_path",
               ".antigravity/skills/sliderule-api/SKILL.md" in paths,
               f"skill tool call resolves to its SKILL.md path: {paths}", failures, verbose)

        tool_names = [e.tool_name for e in out.events if e.tool_name]
        _check("antigravity.transcript.tool_names",
               {"run_command", "write_to_file", "skill"} <= set(tool_names),
               f"tool_names={tool_names}", failures, verbose)

        reasoning = [e for e in out.events if e.kind == EventKind.REASONING]
        _check("antigravity.transcript.reasoning", len(reasoning) == 1,
               f"PLANNER_RESPONSE 'thinking' surfaced as REASONING: {len(reasoning)}",
               failures, verbose)

        errors = [e for e in out.events if e.kind == EventKind.ERROR]
        _check("antigravity.transcript.error_message",
               len(errors) == 1 and errors[0].is_error,
               f"ERROR_MESSAGE step mapped to an ERROR event: {errors}", failures, verbose)

        # step_index 0 (USER_INPUT) legitimately backs the SESSION_START event; 1
        # (CONVERSATION_HISTORY) and 7 (CHECKPOINT) should produce no event at all.
        skipped = any(
            isinstance(e.raw, dict) and e.raw.get("step_index") in (1, 7)
            for e in out.events
        )
        _check("antigravity.transcript.housekeeping_skipped", not skipped,
               "CONVERSATION_HISTORY (step 1) / CHECKPOINT (step 7) produce no events",
               failures, verbose)

        result_events = [e for e in out.events if e.kind == EventKind.RESULT]
        _check("antigravity.transcript.status_success",
               len(result_events) == 1 and not result_events[0].is_error,
               f"status SUCCESS -> RESULT.is_error=False: {result_events}",
               failures, verbose)

        cargv = get_adapter("antigravity").build_argv(
            "do the task", RunOptions(model="gemini-3.5-flash"), cwd="/tmp/some-workspace"
        )
        _check("antigravity.argv",
               cargv[0] == "agy" and "-p" in cargv
               and "--output-format" in cargv and "json" in cargv
               and "--add-dir" in cargv
               and cargv[cargv.index("--add-dir") + 1] == "/tmp/some-workspace"
               and "--model" in cargv,
               f"antigravity argv: {cargv}", failures, verbose)
    finally:
        shutil.rmtree(home, ignore_errors=True)


def run_selftest(verbose: bool = False) -> int:
    failures: list[str] = []

    # cost_str formatting
    print("cost formatting:")
    from .schema import RunResult as _RR
    _check("cost.usd_only", _RR(agent="x", eval_name="", prompt="", workdir="",
           cost_usd=0.0123).cost_str == "$0.0123",
           "USD-only cost_str", failures, verbose)
    _check("cost.req_only", _RR(agent="x", eval_name="", prompt="", workdir="",
           premium_requests=0.33).cost_str == "0.33req",
           "req-only cost_str", failures, verbose)
    _check("cost.both", _RR(agent="x", eval_name="", prompt="", workdir="",
           cost_usd=0.05, premium_requests=1.0).cost_str == "$0.0500 / 1.0req",
           "both cost_str", failures, verbose)
    _check("cost.none", _RR(agent="x", eval_name="", prompt="", workdir="").cost_str == "",
           "empty cost_str", failures, verbose)

    # Claude
    print("claude adapter:")
    out = get_adapter("claude").parse(CLAUDE, "", 0)
    cmds = [e.command for e in out.events if e.command]
    tools = [e.tool_name for e in out.events if e.tool_name]
    _check("claude.command", "npm install" in cmds, f"commands={cmds}", failures, verbose)
    _check("claude.tools", {"Bash", "Write", "Skill"} <= set(tools), f"tools={tools}", failures, verbose)
    _check("claude.no_structured_tool", "StructuredOutput" not in tools,
           "StructuredOutput is not traced as a real tool", failures, verbose)
    skill_paths = [e.path for e in out.events if e.tool_name == "Skill"]
    _check("claude.skill_path", skill_paths == [".claude/skills/sliderule-api/SKILL.md"],
           f"Skill tool call extracts skill path: {skill_paths}", failures, verbose)
    glob_paths = [e.path for e in out.events if e.tool_name == "Glob"]
    _check("claude.unrecognized_tool_path", glob_paths == ["/etc"],
           f"Glob isn't in _FILE_TOOLS/_READ_TOOLS but still yields a path via the generic "
           f"fallback, so leaked_skill_reads() has something to check: {glob_paths}",
           failures, verbose)
    _check("claude.structured", out.structured_output == {"ok": True},
           f"structured={out.structured_output}", failures, verbose)
    _check("claude.final", out.final_text == "Done. Created the app.", repr(out.final_text), failures, verbose)
    _check("claude.cost", out.cost_usd == 0.0123, f"cost={out.cost_usd}", failures, verbose)
    _check("claude.resolved_model", out.resolved_model == "claude",
           f"resolved_model captured from the system/init event: {out.resolved_model!r}",
           failures, verbose)

    # Codex
    print("codex adapter:")
    out = get_adapter("codex").parse(CODEX, "", 0)
    cmds = [e.command for e in out.events if e.command]
    paths = [e.path for e in out.events if e.path]
    _check("codex.command", cmds == ["npm install"], f"commands={cmds}", failures, verbose)
    _check("codex.file", "package.json" in paths, f"paths={paths}", failures, verbose)
    _check("codex.final", out.final_text == "Created demo-app.", repr(out.final_text), failures, verbose)
    cargv = get_adapter("codex").build_argv("do the task", RunOptions(model="gpt-5.4-mini"), cwd="/tmp")
    pre = cargv[:cargv.index("exec")]  # top-level flags precede the exec subcommand
    _check("codex.argv",
           "--ask-for-approval" in pre and "never" in pre
           and "--sandbox" in pre and "workspace-write" in pre
           and "--full-auto" not in cargv and cargv[-1] == "do the task",
           f"non-interactive approval+sandbox before exec, prompt last: {cargv}", failures, verbose)
    sf = EvalSpec(name="t", prompt="p", source_path="/r/skill/evals/e.yaml",
                  files=["a.json", "fixtures/in.json", {"x/a.json": "data/a.json"},
                         "../esc.json",
                         # deeper traversal in the identity (bare string) form
                         "../../deep/etc/passwd",
                         # the {src: dest} MAPPING form with a malicious dest — the realistic
                         # attack surface, since `dest` is the field spec.py's guard actually
                         # checks (resolved_files docstring); falls back to the SOURCE's own
                         # basename, not the malicious dest's basename
                         {"b.json": "../../etc/shadow"}])
    dests = [d for _, d in sf.resolved_files()]
    _check("spec.resolved_files",
           dests == ["a.json", "fixtures/in.json", "data/a.json", "esc.json",
                     "passwd", "b.json"],
           f"seed dests (subdirs kept, traversal guarded — identity AND mapping forms, "
           f"shallow AND deep): {dests}", failures, verbose)
    e1 = get_adapter("codex").env({"CODEX_HOME": "/real", "HOME": "/old"}, RunOptions(home="/iso"))
    _check("codex.iso_env.clear",
           e1.get("HOME") == "/iso" and "CODEX_HOME" not in e1,
           f"unmirrored config-home cleared, HOME set: {e1}", failures, verbose)
    e2 = get_adapter("codex").env(
        {"CODEX_HOME": "/real"},
        RunOptions(home="/iso", isolation_env={"CODEX_HOME": "/iso/_cfg/CODEX_HOME"}))
    _check("codex.iso_env.repoint",
           e2.get("CODEX_HOME") == "/iso/_cfg/CODEX_HOME",
           f"mirrored config-home repointed: {e2}", failures, verbose)

    out_extra = get_adapter("codex").parse(CODEX_EXTRA, "", 0)
    tool_results = [e for e in out_extra.events if e.kind == EventKind.TOOL_RESULT]
    _check("codex.mcp_tool_call_completion_surfaced",
           any(e.is_error for e in tool_results),
           f"a failed mcp_tool_call's completion is surfaced as an error TOOL_RESULT, not "
           f"silently dropped once its id is deduped: {tool_results}", failures, verbose)
    reasoning = [e.text for e in out_extra.events if e.kind == EventKind.REASONING]
    _check("codex.reasoning_item", "thinking about the plan" in reasoning,
           f"a `reasoning` item is surfaced: {reasoning}", failures, verbose)
    errors = [e for e in out_extra.events if e.kind == EventKind.ERROR]
    _check("codex.error_event", len(errors) == 1 and errors[0].is_error,
           f"a top-level `error` event is surfaced: {errors}", failures, verbose)
    extra_paths = [e.path for e in out_extra.events if e.path]
    _check("codex.file_change_dict_form",
           "a.txt" in extra_paths and "b.txt" in extra_paths,
           f"file_change dict-form `changes` keys are extracted: {extra_paths}",
           failures, verbose)
    _check("codex.file_change_single_path", "single.txt" in extra_paths,
           f"file_change single `path` fallback is extracted: {extra_paths}",
           failures, verbose)
    _check("codex.unrecognized_itype_path", "/etc/passwd" in extra_paths,
           f"an itype outside the known set (e.g. a new native tool) still surfaces its path "
           f"via the generic fallback instead of being silently dropped: {extra_paths}",
           failures, verbose)

    # Copilot
    print("copilot adapter:")
    out = get_adapter("copilot").parse(COPILOT, "", 0)
    cmds = [e.command for e in out.events if e.command]
    tools = [e.tool_name for e in out.events if e.tool_name]
    _check("copilot.command", "ls -la" in cmds, f"commands={cmds}", failures, verbose)
    _check("copilot.tools", "shell" in tools and "view" in tools and "skill" in tools,
           f"tools={tools}", failures, verbose)
    skill_paths = [e.path for e in out.events if e.tool_name == "skill"]
    _check("copilot.skill_path", skill_paths == [".agents/skills/sliderule-params/SKILL.md"],
           f"skill tool call extracts skill path: {skill_paths}", failures, verbose)
    _check("copilot.no_report_intent", "report_intent" not in tools,
           "report_intent is not traced as a real tool", failures, verbose)
    _check("copilot.final", out.final_text == "Found 2 files: file1.txt and file2.txt",
           repr(out.final_text), failures, verbose)
    _check("copilot.duration", out.duration_ms == 4000,
           f"duration={out.duration_ms}", failures, verbose)
    _check("copilot.premium_requests", out.premium_requests == 1.0,
           f"premium_requests={out.premium_requests}", failures, verbose)
    _check("copilot.resolved_model", out.resolved_model == "claude-sonnet-4.6",
           f"resolved_model={out.resolved_model}", failures, verbose)
    _check("copilot.ephemeral_skipped",
           not any(e.kind == EventKind.SESSION_START and "skills_loaded" in str(e.raw)
                   for e in out.events),
           "ephemeral session events skipped", failures, verbose)
    cargv = get_adapter("copilot").build_argv("do the task", RunOptions(model="auto"), cwd="/tmp")
    _check("copilot.argv",
           cargv[0] == "copilot" and "-p" in cargv and "--output-format" in cargv
           and "json" in cargv and "--allow-all" in cargv and "--model" in cargv
           and cargv[-1] != "do the task",
           f"copilot argv: {cargv}", failures, verbose)
    cargv_dt = get_adapter("copilot").build_argv("judge", RunOptions(disable_tools=True), cwd="/tmp")
    _check("copilot.disable_tools",
           "--available-tools" in cargv_dt and cargv_dt[cargv_dt.index("--available-tools") + 1] == "",
           f"disable_tools → --available-tools '': {cargv_dt}", failures, verbose)
    _check("copilot.no_output_schema",
           not get_adapter("copilot").supports_output_schema,
           "copilot has no native output schema support", failures, verbose)

    # A _FILE_TOOLS write must be counted ONCE by file_paths_touched(), not twice — Copilot
    # emits both a TOOL_CALL and a FILE_CHANGE for the same write, and file_paths_touched() reads
    # paths from both kinds.
    copilot_write = (
        '{"type":"assistant.message","data":{"model":"claude-sonnet-4.6","content":"writing",'
        '"toolRequests":[{"toolCallId":"tc1","name":"write","arguments":{"path":"out.txt"}}]}}\n'
        '{"type":"error","data":{"message":"rate limited"}}\n'
    )
    out_ft = get_adapter("copilot").parse(copilot_write, "", 0)
    ft_rr = _RR(agent="copilot", eval_name="e", prompt="", workdir="/tmp", events=out_ft.events)
    touched = ft_rr.file_paths_touched()
    _check("copilot.file_tool_not_double_counted", touched.count("out.txt") == 1,
           f"a file-tool write is counted once, not twice: {touched}", failures, verbose)
    ft_errors = [e for e in out_ft.events if e.kind == EventKind.ERROR]
    _check("copilot.error_event", len(ft_errors) == 1 and ft_errors[0].is_error,
           f"an `error` event is surfaced: {ft_errors}", failures, verbose)

    # AntiGravity (3 shapes)
    print("antigravity adapter:")
    out = get_adapter("antigravity").parse(ANTIGRAVITY_STREAM, "", 0)
    cmds = [e.command for e in out.events if e.command]
    _check("antigravity.stream.command", cmds == ["npm install"], f"commands={cmds}", failures, verbose)
    _check("antigravity.stream.final", out.final_text == "Done building demo-app.", repr(out.final_text), failures, verbose)
    skill_paths = [e.path for e in out.events if e.tool_name == "skill"]
    _check("antigravity.skill_path",
           skill_paths == [".antigravity/skills/sliderule-api/SKILL.md"],
           f"skill tool call extracts skill path: {skill_paths}", failures, verbose)
    stream_errors = [e for e in out.events if e.kind == EventKind.ERROR]
    _check("antigravity.stream.error_event", len(stream_errors) == 1 and stream_errors[0].is_error,
           f"generic-fallback parser surfaces an `error` event, not OTHER: {stream_errors}",
           failures, verbose)

    from .adapters.antigravity import _display_to_model_id
    _check("antigravity.display_to_model_id.tiered",
           _display_to_model_id("Gemini 3.5 Flash (Medium)") == "gemini-3.5-flash-medium",
           "tiered display name maps to a hyphenated id", failures, verbose)
    _check("antigravity.display_to_model_id.plain",
           _display_to_model_id("Gemini 3.5 Pro") == "gemini-3.5-pro",
           "untiered display name maps to a hyphenated id", failures, verbose)

    out = get_adapter("antigravity").parse(ANTIGRAVITY_JSON, "", 0)
    _check("antigravity.json.final", out.final_text == "All done.", repr(out.final_text), failures, verbose)

    out = get_adapter("antigravity").parse(ANTIGRAVITY_RAW, "", 0)
    _check("antigravity.raw.final", out.final_text == ANTIGRAVITY_RAW, repr(out.final_text), failures, verbose)

    # judge JSON extraction (markdown fences, bare JSON, mixed text)
    print("judge verdict extraction:")
    from .judge import _extract_json
    fenced = '```json\n{"items": [{"behavior": "b", "pass": true, "reason": "ok"}], "summary": "good"}\n```'
    _check("judge.fenced_json", _extract_json(fenced) is not None and "items" in _extract_json(fenced),
           "extracts JSON from markdown code fence", failures, verbose)
    bare = '{"items": [{"behavior": "b", "pass": true, "reason": "ok"}], "summary": "good"}'
    _check("judge.bare_json", _extract_json(bare) is not None and "items" in _extract_json(bare),
           "extracts bare JSON", failures, verbose)
    mixed = 'Here is my verdict:\n\n```json\n{"items": [], "summary": "s"}\n```\n\nDone.'
    _check("judge.mixed_text", _extract_json(mixed) is not None and "items" in _extract_json(mixed),
           "extracts JSON from mixed text with fence", failures, verbose)
    embedded = 'The result is {"items": [{"behavior": "x", "pass": false, "reason": "n"}], "summary": "s"} and that is it.'
    _check("judge.embedded_json", _extract_json(embedded) is not None and "items" in _extract_json(embedded),
           "extracts embedded JSON from surrounding text", failures, verbose)
    _check("judge.no_json", _extract_json("just plain text with no json") is None,
           "returns None for plain text", failures, verbose)

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

    # progress indicator
    print("progress indicator:")
    import io
    from .progress import Progress
    buf = io.StringIO()
    with Progress(total_cells=2, file=buf) as p:
        p.update(cell=1, phase="running agent", eval_name="demo", model="opus")
        p.done(cell=1, passed=True)
        p.update(cell=2, phase="running judge", eval_name="demo", model="opus")
        p.done(cell=2, passed=False)
    output = buf.getvalue()
    _check("progress.phases", "running agent" in output and "running judge" in output,
           "phase updates appear in non-TTY output", failures, verbose)
    _check("progress.done", "✓" in output and "✗" in output,
           "done marks appear for pass and fail", failures, verbose)

    # pre-flight spec validation
    print("spec validation:")
    from .spec import validate_spec

    # error: skill_triggered for unprovisioned skill
    bad_spec = EvalSpec(name="t", prompt="p", source_path="/x/e.yaml",
                        skills=["sliderule-api"],
                        assertions=[{"type": "skill_triggered", "skill": "sliderule-params"}])
    vr = validate_spec(bad_spec, available_skills={"sliderule-api", "sliderule-params"})
    _check("validate.skill_not_provisioned", not vr.ok and "isolated out" in vr.errors[0],
           f"error for unprovisioned skill_triggered: {vr.errors}", failures, verbose)

    # error: contradictory file_exists + file_absent
    bad_spec2 = EvalSpec(name="t", prompt="p", source_path="/x/e.yaml", skills=["s"],
                         assertions=[{"type": "file_exists", "path": "run.py"},
                                     {"type": "file_absent", "path": "run.py"}])
    vr2 = validate_spec(bad_spec2)
    _check("validate.contradictory_files", not vr2.ok and "contradictory" in vr2.errors[0],
           f"error for file_exists+file_absent: {vr2.errors}", failures, verbose)

    # error: exit_code != 0 with no_error
    bad_spec3 = EvalSpec(name="t", prompt="p", source_path="/x/e.yaml", skills=["s"],
                         assertions=[{"type": "no_error"},
                                     {"type": "exit_code", "equals": 1}])
    vr3 = validate_spec(bad_spec3)
    _check("validate.contradictory_exit", not vr3.ok and "contradictory" in vr3.errors[0],
           f"error for exit_code+no_error: {vr3.errors}", failures, verbose)

    # error: {skill} placeholder with empty skills
    bad_spec4 = EvalSpec(name="t", prompt="Using {skill}, do stuff", source_path="/x/e.yaml",
                         skills=[])
    vr4 = validate_spec(bad_spec4)
    _check("validate.empty_skills_placeholder", not vr4.ok and "literal" in vr4.errors[0],
           f"error for empty skills + placeholder: {vr4.errors}", failures, verbose)

    # warning: rubric but judge off
    warn_spec = EvalSpec(name="t", prompt="p", source_path="/x/e.yaml", skills=["s"],
                         rubric=["checks something"])
    vr5 = validate_spec(warn_spec, judge_enabled=False)
    _check("validate.rubric_no_judge", vr5.ok and any("silently skipped" in w for w in vr5.warnings),
           f"warning for rubric without judge: {vr5.warnings}", failures, verbose)

    # warning: unused var
    warn_spec2 = EvalSpec(name="t", prompt="hello {name}", source_path="/x/e.yaml",
                          skills=["s"], vars={"name": "world", "unused": "x"})
    vr6 = validate_spec(warn_spec2)
    _check("validate.unused_var", vr6.ok and any("unused" in w for w in vr6.warnings),
           f"warning for unused vars: {vr6.warnings}", failures, verbose)

    # warning: undefined placeholder
    warn_spec3 = EvalSpec(name="t", prompt="hello {unknown}", source_path="/x/e.yaml",
                          skills=["s"])
    vr7 = validate_spec(warn_spec3)
    _check("validate.undefined_placeholder", vr7.ok and any("unknown" in w for w in vr7.warnings),
           f"warning for undefined placeholder: {vr7.warnings}", failures, verbose)

    # warning: skill_not_triggered for provisioned skill
    warn_spec4 = EvalSpec(name="t", prompt="p", source_path="/x/e.yaml",
                          skills=["sliderule-api"],
                          assertions=[{"type": "skill_not_triggered", "skill": "sliderule-api"}])
    vr8 = validate_spec(warn_spec4)
    _check("validate.not_triggered_provisioned",
           vr8.ok and any("provisioned" in w for w in vr8.warnings),
           f"warning for skill_not_triggered on provisioned: {vr8.warnings}", failures, verbose)

    # warning: skill_not_triggered for unprovisioned skill (tautology)
    warn_spec5 = EvalSpec(name="t", prompt="p", source_path="/x/e.yaml",
                          skills=["sliderule-api"],
                          assertions=[{"type": "skill_not_triggered", "skill": "sliderule-params"}])
    vr10 = validate_spec(warn_spec5)
    _check("validate.not_triggered_unprovisioned",
           vr10.ok and any("trivially" in w for w in vr10.warnings),
           f"warning for skill_not_triggered on unprovisioned: {vr10.warnings}", failures, verbose)

    # error: unknown assertion type
    bad_type = EvalSpec(name="t", prompt="p", source_path="/x/e.yaml", skills=["s"],
                        assertions=[{"type": "bogus_check"}])
    vr_bt = validate_spec(bad_type)
    _check("validate.unknown_type", not vr_bt.ok and "unknown assertion type" in vr_bt.errors[0],
           f"error for unknown type: {vr_bt.errors}", failures, verbose)

    # error: missing required key
    bad_key = EvalSpec(name="t", prompt="p", source_path="/x/e.yaml", skills=["s"],
                       assertions=[{"type": "file_exists"}])
    vr_bk = validate_spec(bad_key)
    _check("validate.missing_key", not vr_bk.ok and "missing required key" in vr_bk.errors[0],
           f"error for missing key: {vr_bk.errors}", failures, verbose)

    # error: ran_command with no criterion
    bad_rc = EvalSpec(name="t", prompt="p", source_path="/x/e.yaml", skills=["s"],
                      assertions=[{"type": "ran_command"}])
    vr_rc = validate_spec(bad_rc)
    _check("validate.no_criterion", not vr_rc.ok and "will always fail" in vr_rc.errors[0],
           f"error for no criterion: {vr_rc.errors}", failures, verbose)

    # error: tool_count min > max
    bad_tc = EvalSpec(name="t", prompt="p", source_path="/x/e.yaml", skills=["s"],
                      assertions=[{"type": "tool_count", "min": 10, "max": 2}])
    vr_tc = validate_spec(bad_tc)
    _check("validate.tool_count_range", not vr_tc.ok and "impossible" in vr_tc.errors[0],
           f"error for tool_count range: {vr_tc.errors}", failures, verbose)

    # error: tool_count with a non-integer min/max must be a clean validation error, not an
    # unguarded int() crash that takes down the whole pre-flight pass.
    bad_tc_type = EvalSpec(name="t", prompt="p", source_path="/x/e.yaml", skills=["s"],
                           assertions=[{"type": "tool_count", "min": "many"}])
    vr_tc_type = validate_spec(bad_tc_type)
    _check("validate.tool_count_bad_type",
           not vr_tc_type.ok and "must be integers" in vr_tc_type.errors[0],
           f"clean error (not a crash) for non-integer tool_count min: {vr_tc_type.errors}",
           failures, verbose)

    # error: exit_code with a non-integer `equals` must also be a clean validation error.
    bad_ec_type = EvalSpec(name="t", prompt="p", source_path="/x/e.yaml", skills=["s"],
                           assertions=[{"type": "exit_code", "equals": "zero"}])
    vr_ec_type = validate_spec(bad_ec_type)
    _check("validate.exit_code_bad_type",
           not vr_ec_type.ok and "must be an integer" in vr_ec_type.errors[0],
           f"clean error (not a crash) for non-integer exit_code equals: {vr_ec_type.errors}",
           failures, verbose)

    # warning: duplicate skills
    dup_skills = EvalSpec(name="t", prompt="p", source_path="/x/e.yaml",
                          skills=["sliderule-api", "sliderule-api"])
    vr_ds = validate_spec(dup_skills)
    _check("validate.duplicate_skills",
           vr_ds.ok and any("duplicate skill" in w for w in vr_ds.warnings),
           f"warning for duplicate skills: {vr_ds.warnings}", failures, verbose)

    # warning: var shadows built-in
    shadow = EvalSpec(name="t", prompt="Use {skill}", source_path="/x/e.yaml",
                      skills=["s"], vars={"skill": "overridden"})
    vr_sh = validate_spec(shadow)
    _check("validate.shadow_builtin",
           vr_sh.ok and any("shadow" in w for w in vr_sh.warnings),
           f"warning for shadowed built-in: {vr_sh.warnings}", failures, verbose)

    # warning: empty rubric item
    empty_rubric = EvalSpec(name="t", prompt="p", source_path="/x/e.yaml",
                            skills=["s"], rubric=["good item", "", "  "])
    vr_er = validate_spec(empty_rubric)
    _check("validate.empty_rubric",
           vr_er.ok and any("empty" in w for w in vr_er.warnings),
           f"warning for empty rubric: {vr_er.warnings}", failures, verbose)

    # clean spec passes with no errors or warnings
    clean_spec = EvalSpec(name="t", prompt="Use {skill} to run", source_path="/x/e.yaml",
                          skills=["sliderule-api"],
                          assertions=[{"type": "skill_triggered", "skill": "sliderule-api"},
                                      {"type": "no_error"}],
                          rubric=["does something"])
    vr9 = validate_spec(clean_spec, judge_enabled=True)
    _check("validate.clean", vr9.ok and not vr9.warnings,
           f"clean spec: errors={vr9.errors} warnings={vr9.warnings}", failures, verbose)

    # scenario multi-model target (needs PyYAML)
    print("scenario multi-model:")
    try:
        import yaml as _yaml
    except ModuleNotFoundError:
        print("  [skipped — PyYAML not installed]")
        _yaml = None
    if _yaml is not None:
        import tempfile as _tmpmod
        from .spec import load_scenario
        _scen_yaml = {"name": "multi", "prompt": "say hi",
                      "target": {"runner": "claude", "model": ["opus-4-8", "haiku-4-5"]},
                      "skills": []}
        with _tmpmod.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as _f:
            _yaml.dump(_scen_yaml, _f)
            _f.flush()
            _scen = load_scenario(_f.name)
        _check("scenario.multi_model", _scen.models == ["opus-4-8", "haiku-4-5"],
               f"list model parsed: {_scen.models}", failures, verbose)
        _scen_single = {"name": "single", "prompt": "say hi",
                        "target": {"runner": "claude", "model": "opus-4-8"}, "skills": []}
        with _tmpmod.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as _f:
            _yaml.dump(_scen_single, _f)
            _f.flush()
            _scen = load_scenario(_f.name)
        _check("scenario.single_model", _scen.models == ["opus-4-8"],
               f"string model parsed: {_scen.models}", failures, verbose)
        _scen_none = {"name": "nomodel", "prompt": "say hi",
                      "target": {"runner": "claude"}, "skills": []}
        with _tmpmod.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as _f:
            _yaml.dump(_scen_none, _f)
            _f.flush()
            _scen = load_scenario(_f.name)
        _check("scenario.no_model", _scen.models == [None],
               f"omitted model → [None]: {_scen.models}", failures, verbose)

    # HOME isolation overlay + side-effect-free provisioning
    _check_isolation(failures, verbose)
    _check_provision(failures, verbose)
    _check_workspace_reset(failures, verbose)
    _check_snake_case_keys(failures, verbose)
    _check_antigravity_transcript(failures, verbose)

    # per-cell readable report
    _check_report(failures, verbose)

    # artifact / trace path resolution (no false passes on seeded fixtures; workspace-relative;
    # symlink escapes via write-trace)
    _check_path_resolution(failures, verbose)
    _check_workspace_view_skill_dir_match(failures, verbose)

    # undeclared repo skills read via the real on-disk checkout (workspace-escape leak)
    _check_leaked_skill_reads(failures, verbose)
    _check_workspace_relocation(failures, verbose)
    _check_parallel_cell_idx(failures, verbose)
    _check_cell_crash_safety(failures, verbose)

    # cli.py's pure helpers (YAML-error detection, --model/--all-models, models.yaml validation)
    _check_cli_helpers(failures, verbose)
    _check_progress_thread_safety(failures, verbose)

    # real pass/fail behavior for every deterministic assertion type
    _check_assertion_pass_fail(failures, verbose)

    # verdict coercion edge cases (string "false", extra items)
    _check_verdict_coercion(failures, verbose)

    print()
    if failures:
        print(f"SELFTEST FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("SELFTEST PASSED")
    return 0
