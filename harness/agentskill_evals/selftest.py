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
{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t4","name":"Skill","input":{"skill":"skill-alpha"}}]}}
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
{"type":"tool_use","tool":"skill","args":{"skill":"skill-alpha"}}
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
{"step_index":6,"source":"MODEL","type":"PLANNER_RESPONSE","tool_calls":[{"name":"skill","args":{"skill":"skill-alpha"}}]}
{"step_index":7,"source":"SYSTEM","type":"CHECKPOINT","content":"summary"}
{"step_index":8,"source":"MODEL","type":"ERROR_MESSAGE","content":"a transient warning"}
{"step_index":9,"source":"MODEL","type":"PLANNER_RESPONSE","content":"Done building demo-app."}
"""

COPILOT = """\
{"type":"session.skills_loaded","data":{"skills":[]},"id":"s1","timestamp":"2026-06-22T00:00:00Z","parentId":"p1","ephemeral":true}
{"type":"session.tools_updated","data":{"model":"claude-sonnet-4.6"},"id":"s2","timestamp":"2026-06-22T00:00:00Z","parentId":"p1","ephemeral":true}
{"type":"user.message","data":{"content":"list files"},"id":"u1","timestamp":"2026-06-22T00:00:00Z","parentId":"p1"}
{"type":"assistant.turn_start","data":{"turnId":"0","interactionId":"i1"},"id":"t1","timestamp":"2026-06-22T00:00:01Z","parentId":"u1"}
{"type":"assistant.message","data":{"messageId":"m1","model":"claude-sonnet-4.6","content":"","toolRequests":[{"toolCallId":"tc1","name":"report_intent","arguments":{"intent":"Listing files"},"type":"function"},{"toolCallId":"tc2","name":"shell","arguments":{"command":"ls -la"},"type":"function"},{"toolCallId":"tc3","name":"view","arguments":{"path":"/tmp/project"},"type":"function"},{"toolCallId":"tc4","name":"skill","arguments":{"skill":"skill-beta"},"type":"function"}],"interactionId":"i1","turnId":"0","outputTokens":50},"id":"m1","timestamp":"2026-06-22T00:00:02Z","parentId":"t1"}
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


def _flag_pair(argv, flag, value) -> bool:
    """True if argv contains ``flag`` immediately followed by ``value``."""
    return any(a == flag and i + 1 < len(argv) and argv[i + 1] == value
               for i, a in enumerate(argv))


def _check_isolation(failures, verbose):
    """Validate the HOME overlay: declared skills present, undeclared masked, vendor kept,
    auth/config passed through, missing ancestors built, plugin-registry skills masked
    without duplicating declared ones into unrelated plugins. Pure filesystem — no CLIs."""
    import os
    import shutil
    import tempfile

    from .isolation import build_isolated_home, resolve_visible_skills

    print("isolation overlay:")
    real = tempfile.mkdtemp(prefix="ase-realhome-")
    declared_root = tempfile.mkdtemp(prefix="ase-skills-")
    repo_checkout = tempfile.mkdtemp(prefix="ase-repo-")     # a fake checkout of this repo
    vendor_src = tempfile.mkdtemp(prefix="ase-vendorsrc-")   # a real skill dir outside it
    dest = tempfile.mkdtemp(prefix="ase-isohome-")
    shutil.rmtree(dest)  # build_isolated_home (re)creates it
    try:
        # a fake real HOME: a global skills dir with two repo skills + a vendor bundle,
        # plus auth + an unrelated dotfile.
        os.makedirs(os.path.join(real, ".codex", "skills", "skill-alpha"))
        os.makedirs(os.path.join(real, ".codex", "skills", "skill-beta"))
        os.makedirs(os.path.join(real, ".codex", "skills", ".system", "imagegen"))
        os.makedirs(os.path.join(real, "_cfg"))  # reproduce the config-mirror escape hazard
        open(os.path.join(real, ".codex", "auth.json"), "w").close()
        open(os.path.join(real, ".gitconfig"), "w").close()
        # a plugin registry sibling of .gemini/config/skills (both live under .gemini/config/,
        # exercising two different leaf types sharing an ancestor): one plugin mirrors this
        # repo's skills (the real-world leak — e.g. via `agy plugin import`), plus a vendor
        # skill in the same plugin, plus a second, unrelated vendor-only plugin.
        os.makedirs(os.path.join(real, ".gemini", "config", "plugins",
                                  "repo-skills-plugin", "skills", "skill-alpha"))
        os.makedirs(os.path.join(real, ".gemini", "config", "plugins",
                                  "repo-skills-plugin", "skills", "vendor-thing"))
        open(os.path.join(real, ".gemini", "config", "plugins",
                           "repo-skills-plugin", "plugin.json"), "w").close()
        os.makedirs(os.path.join(real, ".gemini", "config", "plugins",
                                  "other-plugin", "skills", "other-skill"))
        # user-level MCP configs that leak through the overlay without a config-file mask:
        # copilot's sits in ~/.copilot (a wholesale-symlinked dir), agy's in .gemini/config/
        # (an overlay-real dir shared with the skills + plugins leaves above, so the file
        # itself would pass through as a symlink).
        user_mcp = '{"mcpServers":{"leaky":{"command":"evil"}}}'
        os.makedirs(os.path.join(real, ".copilot"))
        with open(os.path.join(real, ".copilot", "mcp-config.json"), "w") as fh:
            fh.write(user_mcp)
        open(os.path.join(real, ".copilot", "config.json"), "w").close()
        with open(os.path.join(real, ".gemini", "config", "mcp_config.json"), "w") as fh:
            fh.write(user_mcp)
        # installed plugins can carry MCP servers of their own: copilot's whole
        # installed-plugins dir is neutralized as an empty-dir mask; agy's per-plugin
        # mcp_config.json is masked inside the plugin registry overlay.
        os.makedirs(os.path.join(real, ".copilot", "installed-plugins", "mkt", "some-plugin"))
        with open(os.path.join(real, ".copilot", "installed-plugins", "mkt", "some-plugin",
                               "plugin.json"), "w") as fh:
            fh.write('{"mcpServers":{"plug":{"command":"evil"}}}')
        with open(os.path.join(real, ".gemini", "config", "plugins", "repo-skills-plugin",
                               "mcp_config.json"), "w") as fh:
            fh.write(user_mcp)
        # a config that must be SANITIZED, not replaced — it mixes auth (keep) with
        # plugin registrations (drop), like copilot's config.json.
        with open(os.path.join(real, ".copilot", "state.json"), "w") as fh:
            fh.write("auth+plugins")
        # the cell declares only skill-alpha (its source lives outside HOME, like skills_root)
        os.makedirs(os.path.join(declared_root, "skill-alpha"))
        # stale installs the name-based mask can't catch: a symlink into the checkout under a
        # retired name, and a broken symlink (its checkout was deleted). A symlink resolving
        # elsewhere is a vendor skill and must survive.
        os.makedirs(os.path.join(repo_checkout, "skills_examples", "skill-retired"))
        skills_dir = os.path.join(real, ".codex", "skills")
        os.symlink(os.path.join(repo_checkout, "skills_examples", "skill-retired"),
                   os.path.join(skills_dir, "skill-retired"))
        os.symlink(os.path.join(real, "no-such-checkout", "gone-skill"),
                   os.path.join(skills_dir, "gone-skill"))
        os.symlink(vendor_src, os.path.join(skills_dir, "vendor-linked"))

        build_isolated_home(
            dest,
            [".codex/skills", ".gemini/config/skills"],   # one present, one missing (nested)
            {"skill-alpha", "skill-beta"},        # repo superset to mask
            [os.path.join(declared_root, "skill-alpha")],
            real,
            plugin_registry_subpaths=[".gemini/config/plugins"],
            repo_root=repo_checkout,
            config_file_masks={".copilot/mcp-config.json": "{}",
                               ".copilot/installed-plugins": None,   # dir mask
                               # sanitizing (callable) mask: derives from the real content
                               ".copilot/state.json":
                                   lambda real: open(real).read().replace("plugins", "-"),
                               ".gemini/config/mcp_config.json": "{}",
                               ".missing-cli/mcp.json": "{}"},  # real file absent
            plugin_config_masks={"mcp_config.json": "{}"},
        )

        skills = os.path.join(dest, ".codex", "skills")
        names = set(os.listdir(skills)) if os.path.isdir(skills) else set()
        _check("isolation.declared_present", "skill-alpha" in names,
               f"declared skill-alpha present (got {sorted(names)})", failures, verbose)
        _check("isolation.declared_is_copy",
               os.path.isdir(os.path.join(skills, "skill-alpha"))
               and not os.path.islink(os.path.join(skills, "skill-alpha")),
               "declared skill is a copy (writes can't reach the source)", failures, verbose)
        _check("isolation.undeclared_masked", "skill-beta" not in names,
               "undeclared skill-beta removed", failures, verbose)
        _check("isolation.vendor_kept", ".system" in names,
               "vendor .system bundle preserved", failures, verbose)
        _check("isolation.stale_repo_link_masked", "skill-retired" not in names,
               "stale symlink into the checkout (retired name) dropped", failures, verbose)
        _check("isolation.broken_link_masked", "gone-skill" not in names,
               "broken symlink dropped", failures, verbose)
        _check("isolation.vendor_symlink_kept", "vendor-linked" in names,
               "symlink resolving outside the checkout kept as vendor", failures, verbose)

        # resolve_visible_skills must classify the same way the overlay masks
        class _FakeAdapter:
            global_skills_subpaths = [".codex/skills"]
        vis = resolve_visible_skills(_FakeAdapter(), ["skill-alpha"],
                                     {"skill-alpha", "skill-beta"}, isolated=True,
                                     real_home=real, repo_root=repo_checkout)
        _check("isolation.visibility_classifies_stale",
               {"skill-retired", "gone-skill"} <= set(vis["masked"])
               and "vendor-linked" in vis["vendor"],
               f"stale/broken links reported masked, external symlink reported vendor "
               f"(masked={vis['masked']}, vendor={vis['vendor']})", failures, verbose)
        _check("isolation.auth_passthrough",
               os.path.islink(os.path.join(dest, ".codex", "auth.json")),
               "auth.json passed through as a symlink", failures, verbose)
        _check("isolation.dotfile_passthrough",
               os.path.islink(os.path.join(dest, ".gitconfig")),
               ".gitconfig passed through as a symlink", failures, verbose)
        gem = os.path.join(dest, ".gemini", "config", "skills")
        gem_names = sorted(os.listdir(gem)) if os.path.isdir(gem) else ["<MISSING>"]
        _check("isolation.missing_ancestor_built", gem_names == ["skill-alpha"],
               f"missing nested skills dir built with declared only (got {gem_names})",
               failures, verbose)

        plugin_skills = os.path.join(dest, ".gemini", "config", "plugins",
                                      "repo-skills-plugin", "skills")
        plugin_names = sorted(os.listdir(plugin_skills)) if os.path.isdir(plugin_skills) else []
        _check("isolation.plugin_repo_skill_masked", plugin_names == ["vendor-thing"],
               f"plugin's leaked repo skill dropped, its vendor skill kept "
               f"(got {plugin_names})", failures, verbose)
        _check("isolation.plugin_not_re_added",
               "skill-alpha" not in plugin_names,
               "declared skill isn't duplicated into an unrelated plugin's skills/ "
               "(it's already injected once via the primary skills dir)", failures, verbose)
        _check("isolation.plugin_metadata_passthrough",
               os.path.islink(os.path.join(dest, ".gemini", "config", "plugins",
                                            "repo-skills-plugin", "plugin.json")),
               "plugin.json passed through as a symlink", failures, verbose)
        other_plugin_skills = os.path.join(dest, ".gemini", "config", "plugins",
                                            "other-plugin", "skills")
        other_names = sorted(os.listdir(other_plugin_skills)) if os.path.isdir(other_plugin_skills) else []
        _check("isolation.unrelated_plugin_untouched", other_names == ["other-skill"],
               f"vendor-only plugin left alone (got {other_names})", failures, verbose)

        # config-file masks (MCP hermeticity, DESIGN_MCP_Support.md Phase 0): the masked file
        # is a real neutral file, its siblings still pass through, a mask in a dir shared
        # with skills/plugin leaves works, an absent real file is still materialized, and
        # the user's real config is never modified.
        cop_mask = os.path.join(dest, ".copilot", "mcp-config.json")
        _check("isolation.mcp_mask_real_file",
               os.path.isfile(cop_mask) and not os.path.islink(cop_mask)
               and open(cop_mask).read() == "{}"
               and (os.stat(cop_mask).st_mode & 0o777) == 0o600,
               "masked mcp-config.json is a real 0600 file containing '{}', not a symlink",
               failures, verbose)
        _check("isolation.mcp_mask_sibling_passthrough",
               os.path.islink(os.path.join(dest, ".copilot", "config.json")),
               "masking one file keeps its siblings passing through as symlinks",
               failures, verbose)
        gem_mask = os.path.join(dest, ".gemini", "config", "mcp_config.json")
        _check("isolation.mcp_mask_shared_ancestor",
               os.path.isfile(gem_mask) and not os.path.islink(gem_mask)
               and open(gem_mask).read() == "{}",
               "mask works in a dir shared with skills + plugin-registry leaves",
               failures, verbose)
        missing_mask = os.path.join(dest, ".missing-cli", "mcp.json")
        _check("isolation.mcp_mask_absent_file_created",
               os.path.isfile(missing_mask) and open(missing_mask).read() == "{}",
               "a mask whose real file doesn't exist is still materialized",
               failures, verbose)
        _check("isolation.mcp_mask_source_untouched",
               open(os.path.join(real, ".copilot", "mcp-config.json")).read() == user_mcp
               and open(os.path.join(real, ".gemini", "config",
                                     "mcp_config.json")).read() == user_mcp,
               "the real HOME's MCP configs are never modified", failures, verbose)
        plug_dir_mask = os.path.join(dest, ".copilot", "installed-plugins")
        _check("isolation.mcp_dir_mask",
               os.path.isdir(plug_dir_mask) and not os.path.islink(plug_dir_mask)
               and os.listdir(plug_dir_mask) == [],
               "a None-content mask materializes an empty real dir (installed plugins — "
               "and their MCP servers — are gone)", failures, verbose)
        _check("isolation.mcp_dir_mask_source_untouched",
               os.path.isfile(os.path.join(real, ".copilot", "installed-plugins", "mkt",
                                           "some-plugin", "plugin.json")),
               "the real installed-plugins content is never modified", failures, verbose)
        plug_mcp = os.path.join(dest, ".gemini", "config", "plugins",
                                "repo-skills-plugin", "mcp_config.json")
        _check("isolation.plugin_mcp_config_masked",
               os.path.isfile(plug_mcp) and not os.path.islink(plug_mcp)
               and open(plug_mcp).read() == "{}"
               and open(os.path.join(real, ".gemini", "config", "plugins",
                                     "repo-skills-plugin",
                                     "mcp_config.json")).read() == user_mcp,
               "a plugin's own mcp_config.json is masked inside the registry overlay; the "
               "real one is untouched", failures, verbose)
        san = os.path.join(dest, ".copilot", "state.json")
        _check("isolation.callable_mask_sanitizes",
               os.path.isfile(san) and not os.path.islink(san)
               and open(san).read() == "auth+-"
               and open(os.path.join(real, ".copilot", "state.json")).read()
               == "auth+plugins",
               "a callable mask derives sanitized content from the real file, which stays "
               "untouched", failures, verbose)

        # traversing/absolute/drive-anchored mask paths must be rejected — the mask is
        # opened O_TRUNC at the joined path, so any of them would write outside the
        # overlay ('C:evil.json' is drive-relative on Windows though POSIX isabs says no).
        # Empty/dot-only paths name no file and would be SILENTLY discarded — an adapter
        # typo would quietly leave the real config live, so they're errors too.
        bad_dest = tempfile.mkdtemp(prefix="ase-badmask-")
        try:
            for bad in ("../evil.json", "/etc/evil.json", ".copilot/../../evil.json",
                        "C:evil.json", "\\evil.json", "", ".", "./"):
                try:
                    build_isolated_home(bad_dest, [], (), (), real,
                                        config_file_masks={bad: "{}"})
                    ok = False
                except ValueError:
                    ok = True
                _check(f"isolation.mask_path_rejected[{bad}]", ok,
                       f"mask path {bad!r} raises ValueError", failures, verbose)
        finally:
            shutil.rmtree(bad_dest, ignore_errors=True)

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
        for d in (real, declared_root, repo_checkout, vendor_src, dest):
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

        # a seeded input file is annotated as such, not presented as model output
        with open(os.path.join(ws, "input.json"), "w") as fh:
            fh.write('{"seed": true}')
        cell_seed = CellResult(agent="claude", model=None, eval_name="demo",
                               skill="scenario", passed=True, run_result=rr,
                               assertions=[fa], artifacts_dir=cell_dir,
                               seeded_paths=["input.json"])
        md_seed = render_report(cell_seed)
        _check("report.seeded_annotated",
               "input.json   [seeded input, not model output]" in md_seed,
               "seeded file annotated in the report's file tree", failures, verbose)
        _check("report.seeded_inline_labeled",
               "input.json  [seeded input, not model output] ---" in md_seed,
               "seeded file's inline block labeled as input", failures, verbose)
        os.unlink(os.path.join(ws, "input.json"))

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
    the eval workspace was nested inside this repo's own checkout (``<repo>/artifacts/<run>/.../
    workspace``). At the time, a ``git init`` in the workspace (runner.py) stopped a
    skill-discovery mechanism that deliberately halts its walk-up at the nearest ``.git``, but did
    nothing against a general-purpose file-browsing agent that just ``list_dir``s a parent
    directory by absolute path and reads whatever undeclared skill sits there in plain sight — so
    the `git init` boundary was later removed as dead weight once the exec workspace was relocated
    to a tempdir outside the repo tree (see the "how one cell runs" isolation layers in
    runner.py). The real transcript showed
    exactly that: `list_dir` on the scenario dir, then the repo root, then `view_file` on
    ``skill-alpha/SKILL.md`` and ``skill-gamma/scripts/openapi.py`` — none of which were
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
        for name in ("skill-alpha", "skill-gamma", "skill-delta"):
            os.makedirs(os.path.join(repo_root, name))
            with open(os.path.join(repo_root, name, "SKILL.md"), "w") as f:
                f.write("---\nname: " + name + "\n---\n")
        workspace = os.path.join(repo_root, "artifacts", "run1", "model", "scenario",
                                  "SimpleATL06PromptGrandMesa", "workspace")
        os.makedirs(workspace)

        def _rr(events):
            return RunResult(agent="antigravity", eval_name="e", prompt="", workdir=workspace,
                             events=events)

        repo_skill_names = {"skill-alpha", "skill-gamma", "skill-delta"}

        # 1. the real escape: list_dir/view_file on the undeclared skill-alpha SKILL.md via
        #    the repo's real absolute path — no skill declared for this eval at all.
        leak_path = os.path.join(repo_root, "skill-alpha", "SKILL.md")
        rr = _rr([NormalizedEvent(EventKind.TOOL_CALL, tool_name="view_file", path=leak_path)])
        leaks = leaked_skill_reads(rr, workspace, repo_root, repo_skill_names, declared_names=set())
        _check("leak.undeclared_skill_read_detected", leaks == [os.path.realpath(leak_path)],
               f"undeclared skill read via real repo path is caught: {leaks}", failures, verbose)

        # 2. a read of a DECLARED skill's real path is not a leak (it's expected the model was
        #    given this one).
        rr2 = _rr([NormalizedEvent(EventKind.TOOL_CALL, tool_name="view_file", path=leak_path)])
        leaks2 = leaked_skill_reads(rr2, workspace, repo_root, repo_skill_names,
                                    declared_names={"skill-alpha"})
        _check("leak.declared_skill_not_flagged", leaks2 == [],
               f"declared skill's real-path read is not a leak: {leaks2}", failures, verbose)

        # 3. reading the provisioned COPY inside the workspace itself is not a leak.
        prov_dir = os.path.join(workspace, ".antigravity", "skills", "skill-alpha")
        os.makedirs(prov_dir)
        prov_path = os.path.join(prov_dir, "SKILL.md")
        open(prov_path, "w").close()
        rr3 = _rr([NormalizedEvent(EventKind.TOOL_CALL, tool_name="view_file", path=prov_path)])
        leaks3 = leaked_skill_reads(rr3, workspace, repo_root, repo_skill_names, declared_names=set())
        _check("leak.workspace_copy_not_flagged", leaks3 == [],
               f"provisioned in-workspace copy is not a leak: {leaks3}", failures, verbose)

        # 4. a run_command whose command string references the undeclared skill's real script
        #    path (e.g. `python /repo/skill-gamma/scripts/openapi.py ...`) is also caught.
        script = os.path.join(repo_root, "skill-gamma", "scripts", "openapi.py")
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
        os.symlink(os.path.join(repo_root, "skill-alpha"), evil_link)
        rr6 = _rr([NormalizedEvent(EventKind.TOOL_CALL, tool_name="view_file",
                                   path=os.path.join(evil_link, "SKILL.md"))])
        leaks6 = leaked_skill_reads(rr6, workspace, repo_root, repo_skill_names, declared_names=set())
        _check("leak.symlink_escape_detected",
               leaks6 == [os.path.realpath(os.path.join(repo_root, "skill-alpha", "SKILL.md"))],
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
    # True so effort-threading checks see the resolved value in RunOptions; _run_cell_body
    # nulls the effort for adapters without this control (see effort.unsupported_not_claimed).
    supports_reasoning_effort = True


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
    from .spec import EvalSpec, ModelTarget

    print("workspace relocation (exec dir escapes the repo tree):")
    repo_root = tempfile.mkdtemp(prefix="ase-repo2-")
    seen: dict = {}
    orig_execute = runner_mod.execute

    def _fake_execute(adapter, prompt, opts, *, cwd, timeout, env_overrides, agent_name, eval_name):
        seen["cwd"] = cwd
        # the isolated HOME is deleted right after execute() returns, so the mask must be
        # inspected here, while the agent would actually see it.
        if opts.home:
            mask = os.path.join(opts.home, ".fakecli", "mcp.json")
            if os.path.isfile(mask) and not os.path.islink(mask):
                seen["mask_content"] = open(mask).read()
        with open(os.path.join(cwd, "run.py"), "w") as f:
            f.write("print('hi')\n")
        rr = RunResult(
            agent=agent_name, eval_name=eval_name, prompt=prompt, workdir=cwd,
            events=[NormalizedEvent(EventKind.FILE_CHANGE, path="run.py")],
            final_text="done",
        )
        return ExecResult(result=rr, stdout="", stderr="")

    # masks alone (no global skills dirs — _FakeAdapter declares none) must still trigger
    # the HOME overlay: the MCP kill-switch can't depend on an adapter also declaring
    # skills paths.
    class _MaskingFakeAdapter(_FakeAdapter):
        isolation_config_masks = {".fakecli/mcp.json": "{}"}

    runner_mod.execute = _fake_execute
    try:
        run_dir = os.path.join(repo_root, "artifacts", "run1")
        os.makedirs(run_dir)
        r = runner_mod.Runner.__new__(runner_mod.Runner)
        r.agent, r.adapter, r.targets = "fake", _MaskingFakeAdapter(), [ModelTarget()]
        r.artifacts_root = os.path.join(repo_root, "artifacts")
        r.run_id, r.skills_root, r.judge = "run1", repo_root, None
        r.provision, r.command, r.auto_approve = False, "", True
        r.reasoning_effort = None
        r.jobs, r.isolated, r.progress = 1, True, None
        r._repo_skill_names, r.run_dir = set(), run_dir
        # reached now that _MaskingFakeAdapter's config mask triggers the HOME overlay
        r._repo_root = repo_root

        spec = EvalSpec(name="demo", prompt="hi", source_path=os.path.join(repo_root, "demo.yaml"),
                        assertions=[{"type": "file_exists", "path": "run.py"}])
        cell = r._run_cell(ModelTarget(), spec)

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
        _check("relocate.mcp_mask_threaded_into_home",
               seen.get("mask_content") == "{}",
               f"adapter-declared config mask materialized as '{{}}' in the isolated HOME "
               f"the agent ran with (got {seen.get('mask_content')!r})", failures, verbose)
        # assertion details recorded paths under the (now deleted) exec_ws tempdir — they
        # must be remapped onto the final cell workspace so assertions.json points at
        # files that still exist.
        fe = next((a for a in cell.assertions if a.type == "file_exists"), None)
        rp = ((fe.details or {}).get("resolved_path", "") if fe else "")
        _check("relocate.assertion_details_remapped",
               rp == os.path.join(cell_workspace, "run.py") and os.path.isfile(rp),
               f"file_exists resolved_path points at the final workspace, not the deleted "
               f"tempdir: {rp}", failures, verbose)
    finally:
        runner_mod.execute = orig_execute
        shutil.rmtree(repo_root, ignore_errors=True)


def _check_mcp_hermetic_paths(failures, verbose):
    """MCP hermeticity must hold on every execution path, not just successful isolated
    Runner cells (the Phase-0 review's finding 5): (1) build_mcp_masked_home neutralizes
    the MCP configs — including per-plugin ones and a custom config-home env var — while
    passing everything else through; (2) model probes run against that mask-only overlay;
    (3) judge runs get the same overlay; (4) an overlay build failure on a runner whose
    MCP-off depends on it fails the cell CLOSED instead of executing with the real HOME
    (while runners with a CLI-level kill-switch keep the graceful non-isolated fallback)."""
    import os
    import shutil
    import sys
    import tempfile

    from .adapters.base import Adapter, ParseOutput, ProbeResult
    from .isolation import build_mcp_masked_home

    print("MCP hermetic paths (masked-home overlay, probes, judge, fail-closed):")

    # --- 1) build_mcp_masked_home against a fake real HOME + custom config home ---------
    real = tempfile.mkdtemp(prefix="ase-mcpreal-")
    custom = tempfile.mkdtemp(prefix="ase-mcpcustom-")
    home = None
    old_var = os.environ.get("FAKECLI_HOME")
    try:
        os.makedirs(os.path.join(real, ".fakecli", "plugins", "p1"))
        with open(os.path.join(real, ".fakecli", "mcp.json"), "w") as fh:
            fh.write('{"mcpServers":{"leaky":{}}}')
        with open(os.path.join(real, ".fakecli", "plugins", "p1", "mcp_config.json"), "w") as fh:
            fh.write('{"mcpServers":{"plug":{}}}')
        open(os.path.join(real, ".fakecli", "plugins", "p1", "plugin.json"), "w").close()
        open(os.path.join(real, ".fakecli", "auth.json"), "w").close()
        with open(os.path.join(custom, "mcp.json"), "w") as fh:
            fh.write('{"mcpServers":{"custom-leaky":{}}}')
        open(os.path.join(custom, "auth.json"), "w").close()

        class _MaskedCli:
            isolation_config_masks = {".fakecli/mcp.json": "{}"}
            plugin_registry_config_masks = {"mcp_config.json": "{}"}
            global_plugin_registry_subpaths = [".fakecli/plugins"]
            isolation_config_homes = [("FAKECLI_HOME", ".fakecli", None)]

        os.environ["FAKECLI_HOME"] = custom
        home, iso_env = build_mcp_masked_home(_MaskedCli(), real_home=real)
        mask = os.path.join(home, ".fakecli", "mcp.json")
        plug_mask = os.path.join(home, ".fakecli", "plugins", "p1", "mcp_config.json")
        _check("mcp_masked_home.global_and_plugin_masked",
               open(mask).read() == "{}" and not os.path.islink(mask)
               and open(plug_mask).read() == "{}" and not os.path.islink(plug_mask),
               "global + per-plugin MCP configs neutralized in the overlay",
               failures, verbose)
        _check("mcp_masked_home.everything_else_passes_through",
               os.path.islink(os.path.join(home, ".fakecli", "auth.json"))
               and os.path.islink(os.path.join(home, ".fakecli", "plugins", "p1",
                                               "plugin.json")),
               "auth/config/plugin metadata still pass through as symlinks",
               failures, verbose)
        mirror = iso_env.get("FAKECLI_HOME", "")
        _check("mcp_masked_home.custom_home_mirrored",
               bool(mirror) and mirror != custom
               and open(os.path.join(mirror, "mcp.json")).read() == "{}"
               and os.path.islink(os.path.join(mirror, "auth.json")),
               f"a set custom config-home var is mirrored with re-rooted masks, not left "
               f"pointing at the real config: {iso_env}", failures, verbose)

        class _NoMaskCli:
            pass
        none_home, none_env = build_mcp_masked_home(_NoMaskCli(), real_home=real)
        _check("mcp_masked_home.no_masks_no_overlay",
               none_home is None and none_env == {},
               "an adapter without masks gets no overlay (runs against the real HOME)",
               failures, verbose)
    finally:
        if old_var is None:
            os.environ.pop("FAKECLI_HOME", None)
        else:
            os.environ["FAKECLI_HOME"] = old_var
        for d in (home, real, custom):
            if d:
                shutil.rmtree(d, ignore_errors=True)

    # --- 2) probe_model runs inside the mask-only overlay, in a fresh private workspace -
    # _ProbeCli keeps the pre-Phase-0 `_probe_argv(self, model)` shape ON PURPOSE: it
    # doubles as the out-of-tree compat check (_probe_argv_compat adapts the call).
    class _ProbeCli(Adapter):
        name = "probefake"
        binary = sys.executable
        isolation_config_masks = {".fakeprobe-mcp/mcp.json": "{}"}

        def _probe_argv(self, model):
            return [sys.executable, "-c",
                    "import os; home=os.environ.get('HOME',''); print('PROBEHOME='+home); "
                    "print('PROBECWD='+os.getcwd()); "
                    "print('PROBEMASK='+open(os.path.join(home,'.fakeprobe-mcp','mcp.json'))"
                    ".read())"]

        def _parse_probe_cost(self, output):
            self.probe_output = output
            return ProbeResult(accepted=True)

        def build_argv(self, prompt, opts, *, cwd):
            return []

        def parse(self, stdout, stderr, exit_code, *, opts=None):
            return ParseOutput()

    probe_cli = _ProbeCli()
    pr = probe_cli.probe_model("any")
    out = getattr(probe_cli, "probe_output", "")
    real_home = os.path.abspath(os.path.expanduser("~"))
    probed_home = next((ln[len("PROBEHOME="):] for ln in out.splitlines()
                        if ln.startswith("PROBEHOME=")), "")
    probed_cwd = next((ln[len("PROBECWD="):] for ln in out.splitlines()
                       if ln.startswith("PROBECWD=")), "")
    _check("mcp_probe.masked_home",
           pr.accepted and "PROBEMASK={}" in out
           and probed_home and os.path.abspath(probed_home) != real_home,
           f"the probe subprocess saw a masked throwaway HOME, not the real one "
           f"(home={probed_home!r}) — via a legacy (model)-only _probe_argv override",
           failures, verbose)
    # the probe must run in its own fresh private temp workspace — not the harness cwd
    # and not the shared system temp ROOT itself, where anyone's planted workspace
    # configs (.agents/mcp_config.json on agy) would load; /tmp is world-writable.
    _check("mcp_probe.private_workspace",
           probed_cwd
           and os.path.basename(probed_cwd).startswith("ase-probe-")
           and os.path.realpath(probed_cwd) != os.path.realpath(os.getcwd())
           and os.path.realpath(probed_cwd) != os.path.realpath(tempfile.gettempdir())
           and not os.path.isdir(probed_cwd),
           f"probe ran in a fresh private workspace, deleted afterwards "
           f"(cwd={probed_cwd!r})", failures, verbose)
    _check("mcp_probe.overlay_cleaned",
           not probed_home or not os.path.isdir(probed_home),
           "the probe's mask-only overlay is deleted afterwards", failures, verbose)

    # a NEW-style _probe_argv receives the probe workspace and env explicitly (agy needs
    # cwd for --add-dir; codex/copilot need env for MCP enumeration).
    class _CtxProbeCli(Adapter):
        name = "ctxprobefake"
        binary = sys.executable

        def _probe_argv(self, model, *, cwd=None, env=None):
            self.got_cwd, self.got_env = cwd, env
            return [sys.executable, "-c", "print('ok')"]

        def build_argv(self, prompt, opts, *, cwd):
            return []

        def parse(self, stdout, stderr, exit_code, *, opts=None):
            return ParseOutput()

    ctx_cli = _CtxProbeCli()
    ctx_pr = ctx_cli.probe_model("any")
    _check("mcp_probe.context_passed",
           ctx_pr.accepted and getattr(ctx_cli, "got_cwd", None)
           and os.path.basename(ctx_cli.got_cwd).startswith("ase-probe-")
           and isinstance(getattr(ctx_cli, "got_env", None), dict),
           f"new-style _probe_argv receives the probe workspace + exact env "
           f"(cwd={getattr(ctx_cli, 'got_cwd', None)!r})", failures, verbose)

    # --- 3) judge runs get the same overlay ---------------------------------------------
    import agentskill_evals.judge as judge_mod
    from .exec import ExecResult
    from .schema import RunResult

    seen: dict = {}
    orig_execute = judge_mod.execute

    def _fake_judge_execute(adapter, prompt, opts, *, cwd, timeout,
                            agent_name=None, eval_name="", env_overrides=None):
        seen["home"] = opts.home
        if opts.home:
            p = os.path.join(opts.home, ".copilot", "mcp-config.json")
            if os.path.isfile(p) and not os.path.islink(p):
                seen["mask"] = open(p).read()
        rr = RunResult(agent=agent_name or "judge", eval_name=eval_name, prompt=prompt,
                       workdir=cwd,
                       final_text='{"items":[{"behavior":"b","pass":true,'
                                  '"reason":"ok"}],"summary":"s"}')
        return ExecResult(result=rr, stdout="", stderr="")

    judge_mod.execute = _fake_judge_execute
    try:
        graded_rr = RunResult(agent="copilot", eval_name="e", prompt="p", workdir="/tmp")
        j = judge_mod.Judge(agent="copilot")
        ws = tempfile.mkdtemp(prefix="ase-judgews-")
        try:
            res = j(result=graded_rr, workdir=ws, spec=EvalSpec(name="e", prompt="p",
                    source_path="/x.yaml"), rubric=["b"], cfg={})
        finally:
            shutil.rmtree(ws, ignore_errors=True)
        _check("mcp_judge.masked_home",
               seen.get("home") and seen.get("mask") == '{"mcpServers": {}}'
               and res.verdict["items"][0]["pass"] is True,
               f"a judge on a mask-dependent runner executes with the masked overlay "
               f"(home={seen.get('home')!r}, mask={seen.get('mask')!r})", failures, verbose)
        _check("mcp_judge.overlay_cleaned",
               seen.get("home") and not os.path.isdir(seen["home"]),
               "the judge's overlay is deleted afterwards", failures, verbose)
    finally:
        judge_mod.execute = orig_execute

    # --- 4) overlay failure: fail closed with masks, graceful fallback without ----------
    import agentskill_evals.runner as runner_mod
    from .spec import ModelTarget

    repo_root = tempfile.mkdtemp(prefix="ase-repo5-")
    orig_run_execute = runner_mod.execute
    orig_build = runner_mod.build_isolated_home

    def _fake_run_execute(adapter, prompt, opts, *, cwd, timeout, env_overrides,
                          agent_name, eval_name):
        with open(os.path.join(cwd, "ran.txt"), "w") as f:
            f.write("ran\n")
        rr = RunResult(agent=agent_name, eval_name=eval_name, prompt=prompt, workdir=cwd,
                       final_text="done")
        return ExecResult(result=rr, stdout="", stderr="")

    def _broken_build(*a, **k):
        raise OSError("symlinks unavailable")

    class _MaskDependentCli(_FakeAdapter):
        isolation_config_masks = {".fakecli/mcp.json": "{}"}

    class _SkillsOnlyCli(_FakeAdapter):
        global_skills_subpaths = [".fakecli/skills"]

    runner_mod.execute = _fake_run_execute
    runner_mod.build_isolated_home = _broken_build
    try:
        def _mk_runner(adapter):
            run_dir = os.path.join(repo_root, "artifacts", adapter.__class__.__name__)
            os.makedirs(run_dir, exist_ok=True)
            r = runner_mod.Runner.__new__(runner_mod.Runner)
            r.agent, r.adapter, r.targets = "fake", adapter, [ModelTarget()]
            r.artifacts_root = os.path.join(repo_root, "artifacts")
            r.run_id, r.skills_root, r.judge = "run1", repo_root, None
            r.provision, r.command, r.auto_approve = False, "", True
            r.reasoning_effort = None
            r.jobs, r.isolated, r.progress = 1, True, None
            r._repo_skill_names, r.run_dir = set(), run_dir
            r._repo_root = repo_root
            return r

        import contextlib
        import io
        spec = EvalSpec(name="demo", prompt="hi",
                        source_path=os.path.join(repo_root, "demo.yaml"))
        with contextlib.redirect_stderr(io.StringIO()):
            cell_closed = _mk_runner(_MaskDependentCli())._run_cell(ModelTarget(), spec)
        _check("mcp_failclosed.mask_dependent_cell_fails",
               cell_closed.passed is False
               and "failing closed" in (cell_closed.run_result.error or "")
               and not os.path.isfile(os.path.join(cell_closed.artifacts_dir,
                                                   "workspace", "ran.txt")),
               f"overlay failure on a mask-dependent runner fails the cell without "
               f"executing the agent: {cell_closed.run_result.error!r}", failures, verbose)

        with contextlib.redirect_stderr(io.StringIO()):
            cell_open = _mk_runner(_SkillsOnlyCli())._run_cell(ModelTarget(), spec)
        _check("mcp_failclosed.maskless_fallback_kept",
               not cell_open.run_result.error
               and os.path.isfile(os.path.join(cell_open.artifacts_dir,
                                               "workspace", "ran.txt")),
               f"a runner without masks keeps the graceful non-isolated fallback "
               f"(error={cell_open.run_result.error!r})", failures, verbose)
    finally:
        runner_mod.execute = orig_run_execute
        runner_mod.build_isolated_home = orig_build
        shutil.rmtree(repo_root, ignore_errors=True)

    # --- 5) probe/judge overlay failure also fails CLOSED -------------------------------
    import contextlib
    import io

    import agentskill_evals.adapters.base as base_mod

    def _broken_masked_home(adapter, real_home=None):
        raise OSError("symlinks unavailable")

    orig_masked = base_mod.build_mcp_masked_home
    base_mod.build_mcp_masked_home = _broken_masked_home
    try:
        probe_cli2 = _ProbeCli()
        with contextlib.redirect_stderr(io.StringIO()):
            pr2 = probe_cli2.probe_model("any")
        _check("mcp_probe.fail_closed",
               pr2.accepted is False and not hasattr(probe_cli2, "probe_output"),
               "an overlay failure skips the probe entirely (model reported unavailable) "
               "instead of probing with the real HOME", failures, verbose)
    finally:
        base_mod.build_mcp_masked_home = orig_masked

    judge_calls: list = []
    orig_judge_masked = judge_mod.build_mcp_masked_home
    orig_judge_exec = judge_mod.execute
    judge_mod.build_mcp_masked_home = _broken_masked_home
    judge_mod.execute = lambda *a, **k: judge_calls.append(1)
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            res2 = judge_mod.Judge(agent="copilot")(
                result=RunResult(agent="copilot", eval_name="e", prompt="p", workdir="/tmp"),
                workdir="/tmp", spec=EvalSpec(name="e", prompt="p", source_path="/x.yaml"),
                rubric=["b1", "b2"], cfg={})
        _check("mcp_judge.fail_closed",
               not judge_calls and res2.verdict.get("judge_error") is True
               and [it["pass"] for it in res2.verdict["items"]] == [False, False]
               and "hermetically" in res2.verdict["items"][0]["reason"],
               f"an overlay failure yields a judge-failed verdict without executing "
               f"(calls={len(judge_calls)}): {res2.verdict.get('summary')!r}",
               failures, verbose)
    finally:
        judge_mod.build_mcp_masked_home = orig_judge_masked
        judge_mod.execute = orig_judge_exec

    # --- 6) pre-Phase-0 out-of-tree adapters (2-tuple config homes) keep working --------
    from .isolation import config_home_entries

    class _LegacyCli(Adapter):
        name = "legacy"
        binary = "legacy-cli"
        isolation_config_homes = [("LEGACY_HOME", "skills")]  # old (var, skills_sub) form

        def build_argv(self, prompt, opts, *, cwd):
            return []

        def parse(self, stdout, stderr, exit_code, *, opts=None):
            return ParseOutput()

    legacy = _LegacyCli()
    entries = config_home_entries(legacy)
    legacy_env = legacy.env({"LEGACY_HOME": "/real", "HOME": "/old"},
                            RunOptions(home="/iso"))
    _check("mcp_compat.legacy_two_tuple",
           entries == [("LEGACY_HOME", None, "skills")]
           and legacy_env.get("HOME") == "/iso" and "LEGACY_HOME" not in legacy_env,
           f"a 2-tuple isolation_config_homes adapter normalizes and env() still clears "
           f"the unmirrored var: {entries}", failures, verbose)

    # --- 7) workspace config masks (agy's --add-dir discovery channel) ------------------
    from .runner import _apply_workspace_config_masks

    ws = tempfile.mkdtemp(prefix="ase-wsmask-")
    outside = tempfile.mkdtemp(prefix="ase-wsmask-out-")
    try:
        os.makedirs(os.path.join(ws, ".agents", "plugins", "p1"))
        with open(os.path.join(ws, ".agents", "mcp_config.json"), "w") as fh:
            fh.write('{"mcpServers":{"seeded":{}}}')
        with open(os.path.join(ws, ".agents", "plugins", "p1", "mcp_config.json"), "w") as fh:
            fh.write('{"mcpServers":{"plug":{}}}')
        open(os.path.join(ws, ".agents", "plugins", "p1", "plugin.json"), "w").close()
        # a DOT-prefixed plugin dir: Python's glob excludes it from `*`, but agy discovers
        # it — so the dot-inclusive `plugins/.*/` companion pattern must reach it.
        os.makedirs(os.path.join(ws, ".agents", "plugins", ".hidden"))
        with open(os.path.join(ws, ".agents", "plugins", ".hidden",
                               "mcp_config.json"), "w") as fh:
            fh.write('{"mcpServers":{"hidden":{}}}')
        target = os.path.join(outside, "victim.json")
        with open(target, "w") as fh:
            fh.write("outside")
        os.makedirs(os.path.join(ws, ".agents", "plugins", "evil"))
        os.symlink(target, os.path.join(ws, ".agents", "plugins", "evil",
                                        "mcp_config.json"))

        class _WsCli:
            workspace_config_masks = {
                ".agents/mcp_config.json": '{"mcpServers": {}}',
                ".agents/plugins/*/mcp_config.json": '{"mcpServers": {}}',
                ".agents/plugins/.*/mcp_config.json": '{"mcpServers": {}}',
            }

        masked = _apply_workspace_config_masks(_WsCli(), ws)
        neutral = '{"mcpServers": {}}'
        evil_path = os.path.join(ws, ".agents", "plugins", "evil", "mcp_config.json")
        hidden_path = os.path.join(ws, ".agents", "plugins", ".hidden",
                                   "mcp_config.json")
        _check("mcp_wsmask.seeded_neutralized",
               masked == [os.path.join(".agents", "mcp_config.json"),
                          os.path.join(".agents", "plugins", ".hidden", "mcp_config.json"),
                          os.path.join(".agents", "plugins", "evil", "mcp_config.json"),
                          os.path.join(".agents", "plugins", "p1", "mcp_config.json")]
               and open(os.path.join(ws, ".agents", "mcp_config.json")).read() == neutral
               and open(os.path.join(ws, ".agents", "plugins", "p1",
                                     "mcp_config.json")).read() == neutral,
               f"seeded workspace MCP configs (direct + per-plugin glob) neutralized: "
               f"{masked}", failures, verbose)
        # a DOT-prefixed plugin's config must be neutralized too (glob's `*` skips it —
        # the `.*` companion is what reaches it).
        _check("mcp_wsmask.dot_plugin_neutralized",
               open(hidden_path).read() == neutral,
               "a dot-prefixed plugin dir's mcp_config.json is neutralized via the "
               "dot-inclusive glob", failures, verbose)
        # an outside-pointing symlink is NEUTRALIZED — unlinked and replaced with a real
        # neutral file (a skipped-but-live link would keep the outside config loadable) —
        # while its outside target is never written through.
        _check("mcp_wsmask.symlink_escape_neutralized",
               open(target).read() == "outside"
               and not os.path.islink(evil_path)
               and open(evil_path).read() == neutral,
               "an escaping symlink is replaced by a neutral real file; its outside "
               "target untouched", failures, verbose)
        _check("mcp_wsmask.nothing_created",
               os.path.getsize(os.path.join(ws, ".agents", "plugins", "p1",
                                            "plugin.json")) == 0
               and not os.path.exists(os.path.join(ws, ".mcp.json")),
               "non-matching files untouched; absent configs are not pre-created",
               failures, verbose)

        # same for an escaping symlink on an INTERMEDIATE component: a seeded
        # `.agents -> <outside dir>` must not leave the outside config discoverable
        # through the link (glob matches through dir symlinks), nor be written through.
        ws2 = tempfile.mkdtemp(prefix="ase-wsmask2-")
        out2 = tempfile.mkdtemp(prefix="ase-wsmask2-out-")
        try:
            with open(os.path.join(out2, "mcp_config.json"), "w") as fh:
                fh.write('{"mcpServers":{"outside":{}}}')
            os.symlink(out2, os.path.join(ws2, ".agents"))
            masked2 = _apply_workspace_config_masks(_WsCli(), ws2)
            agents2 = os.path.join(ws2, ".agents")
            _check("mcp_wsmask.dir_symlink_escape_neutralized",
                   masked2 == [os.path.join(".agents", "mcp_config.json")]
                   and not os.path.islink(agents2) and os.path.isdir(agents2)
                   and open(os.path.join(agents2, "mcp_config.json")).read() == neutral
                   and open(os.path.join(out2, "mcp_config.json")).read()
                   == '{"mcpServers":{"outside":{}}}',
                   "an escaping DIRECTORY symlink is unlinked and rebuilt as a real dir "
                   "with the neutral config; the outside dir untouched",
                   failures, verbose)
        finally:
            shutil.rmtree(ws2, ignore_errors=True)
            shutil.rmtree(out2, ignore_errors=True)
    finally:
        shutil.rmtree(ws, ignore_errors=True)
        shutil.rmtree(outside, ignore_errors=True)

    # --- 8) exec.execute(): env before argv, argv failures fail closed ------------------
    # An adapter whose argv depends on ambient config state (codex enumerating MCP
    # servers) must see the CHILD's exact environment — os.environ + the scenario's
    # `env:` overrides + isolation — so execute() computes env first and hands it over
    # via opts.effective_env. And when an adapter can't guarantee a hermetic argv
    # (RuntimeError from enumeration), the run must FAIL, not crash or proceed.
    from .exec import execute as _execute

    class _EnvSpyCli(Adapter):
        name = "envspy"
        binary = "definitely-not-installed-xyz"  # execute() bails before spawning

        def build_argv(self, prompt, opts, *, cwd):
            self.got_env, self.got_cwd = opts.effective_env, cwd
            return ["x"]

        def parse(self, stdout, stderr, exit_code, *, opts=None):
            return ParseOutput()

    spy = _EnvSpyCli()
    _execute(spy, "p", RunOptions(), cwd="/tmp", timeout=5,
             env_overrides={"SPY_CONFIG_HOME": "/scenario/override"})
    _check("mcp_exec.effective_env_before_argv",
           isinstance(getattr(spy, "got_env", None), dict)
           and spy.got_env.get("SPY_CONFIG_HOME") == "/scenario/override"
           and spy.got_cwd == "/tmp",
           "build_argv sees the child's exact env (scenario overrides included) via "
           "opts.effective_env", failures, verbose)

    class _UnhermeticCli(Adapter):
        name = "unhermetic"
        binary = sys.executable

        def build_argv(self, prompt, opts, *, cwd):
            raise RuntimeError("cannot enumerate MCP servers")

        def parse(self, stdout, stderr, exit_code, *, opts=None):
            return ParseOutput()

    ex_closed = _execute(_UnhermeticCli(), "p", RunOptions(), cwd="/tmp", timeout=5)
    _check("mcp_exec.argv_failure_fails_closed",
           "hermetic invocation" in (ex_closed.result.error or "")
           and "cannot enumerate MCP servers" in ex_closed.result.error
           and not ex_closed.result.argv,
           f"an argv-construction failure becomes a failed run (nothing executed): "
           f"{ex_closed.result.error!r}", failures, verbose)

    # On Windows process.env is case-insensitive, so a scenario `env: {copilot_home: ...}`
    # that dodges isolation's case-sensitive COPILOT_HOME pop would still be read by the
    # child as COPILOT_HOME — an isolation escape. execute() folds keys to a single
    # uppercase form (win32-gated at the call site) before isolation runs; the fold itself
    # is pure and checked on every host. Composing it with copilot's isolation env() shows
    # the escape is closed: the folded COPILOT_HOME is popped (unmirrored → isolated home).
    from .exec import _fold_env_keys_case_insensitive as _fold
    folded = _fold({"COPILOT_HOME": "C:\\real", "copilot_home": "C:\\evil",
                    "Path": "/p", "PATH": "/q"})
    iso_env = get_adapter("copilot").env(
        _fold({"copilot_home": "C:\\evil", "HOME": "/h"}), RunOptions(home="/iso"))
    _check("mcp_exec.win_env_case_fold_closes_isolation_escape",
           folded == {"COPILOT_HOME": "C:\\evil", "PATH": "/q"}   # one key each, last wins
           and "copilot_home" not in iso_env and "COPILOT_HOME" not in iso_env
           and iso_env.get("HOME") == "/iso",
           f"case-colliding env keys fold to one uppercase key (last-wins) so isolation's "
           f"COPILOT_HOME pop can't be dodged by a lowercase alias: {folded}, "
           f"iso COPILOT_HOME={iso_env.get('COPILOT_HOME')!r}", failures, verbose)


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
    from .spec import EvalSpec, ModelTarget

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
        r.agent, r.adapter, r.targets = "fake", _FakeAdapter(), [ModelTarget()]
        r.artifacts_root = os.path.join(repo_root, "artifacts")
        r.run_id, r.skills_root, r.judge = "run1", repo_root, None
        r.provision, r.command, r.auto_approve = False, "", True
        r.reasoning_effort = None
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
    from .spec import EvalSpec, ModelTarget

    print("cell crash safety:")
    repo_root = tempfile.mkdtemp(prefix="ase-repo3-")
    seen: dict = {}
    orig_execute = runner_mod.execute

    def _crashing_execute(adapter, prompt, opts, *, cwd, timeout, env_overrides, agent_name, eval_name):
        seen["cwd"] = cwd
        with open(os.path.join(cwd, "partial.txt"), "w") as f:
            f.write("partial output before crash\n")
        raise RuntimeError("simulated mid-run crash")

    runner_mod.execute = _crashing_execute
    try:
        run_dir = os.path.join(repo_root, "artifacts", "run1")
        os.makedirs(run_dir)
        r = runner_mod.Runner.__new__(runner_mod.Runner)
        r.agent, r.adapter, r.targets = "fake", _FakeAdapter(), [ModelTarget()]
        r.artifacts_root = os.path.join(repo_root, "artifacts")
        r.run_id, r.skills_root, r.judge = "run1", repo_root, None
        r.provision, r.command, r.auto_approve = False, "", True
        r.reasoning_effort = None
        r.jobs, r.isolated, r.progress = 1, True, None
        r._repo_skill_names, r.run_dir = set(), run_dir

        spec = EvalSpec(name="demo", prompt="hi", source_path=os.path.join(repo_root, "demo.yaml"))
        cell = None
        raised = False
        try:
            cell = r._run_cell(ModelTarget(), spec)
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
            _check("crash.partial_output_preserved",
                   os.path.isfile(os.path.join(cell.artifacts_dir, "workspace", "partial.txt")),
                   "partial output written before the crash is moved into cell_dir/workspace "
                   "(the evidence needed to debug the crash), not deleted with the tempdir",
                   failures, verbose)
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

    from .cli import (ModelsConfig, _duplicate_names, _is_yaml_error,
                      _load_models_config, _resolve_targets)
    from .spec import ModelTarget

    print("cli.py helpers:")

    # duplicate eval names (same artifacts dir + same matrix key) are detected
    dup_specs = [EvalSpec(name="a", prompt="p", source_path="/one/evals/a.yaml"),
                 EvalSpec(name="a", prompt="p", source_path="/two/evals/a.yaml"),
                 EvalSpec(name="b", prompt="p", source_path="/one/evals/b.yaml")]
    dupes = _duplicate_names(dup_specs)
    _check("cli.duplicate_names",
           set(dupes) == {"a"} and len(dupes["a"]) == 2,
           f"duplicate eval names detected with their sources: {dupes}", failures, verbose)
    _check("cli.no_false_duplicates", _duplicate_names(dup_specs[1:]) == {},
           "distinct names produce no duplicates", failures, verbose)

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
        targets = _resolve_targets("claude", [ModelTarget("explicit")], cfg, all_models=True)
    _check("cli.resolve_targets.explicit_wins", targets == [ModelTarget("explicit")],
           f"--model wins over --all-models: {targets}", failures, verbose)
    _check("cli.resolve_targets.conflict_warned", "ignored" in buf.getvalue(),
           f"a warning is printed instead of silently dropping --all-models: {buf.getvalue()!r}",
           failures, verbose)
    # a target that pins only an effort inherits the models.yaml default model
    targets = _resolve_targets("claude", [ModelTarget(None, "high")], cfg, all_models=False)
    _check("cli.resolve_targets.effort_only_gets_default",
           targets == [ModelTarget("a", "high")],
           f"effort-only target resolves the default model: {targets}", failures, verbose)

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

    # judge.timeout: parsed as an int; a bad value warns and is ignored (never crashes)
    tmp2 = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    try:
        tmp2.write('{"judge": {"agent": "claude", "timeout": "300"}}')
        tmp2.close()
        mc2 = _load_models_config(tmp2.name)
        _check("cli.judge_timeout_parsed", mc2.judge.get("timeout") == 300,
               f"judge.timeout coerced to int: {mc2.judge}", failures, verbose)
    finally:
        os.unlink(tmp2.name)
    tmp3 = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    try:
        tmp3.write('{"judge": {"agent": "claude", "timeout": "soon"}}')
        tmp3.close()
        mc3 = _load_models_config(tmp3.name)
        _check("cli.judge_timeout_bad_warned",
               "timeout" not in mc3.judge and any("timeout" in w for w in mc3.warnings),
               f"non-integer judge.timeout warns and is dropped: {mc3.warnings}",
               failures, verbose)
    finally:
        os.unlink(tmp3.name)

    # _resolve_skills_root / _default_config_path / repo_root_for: the skills_examples/
    # auto-descent and models.yaml discovery, on a temp tree (no YAML parsing involved —
    # only os.path checks, so a models.yaml can be an empty file here).
    import shutil as _shutil
    import tempfile as _tempfile

    from .cli import _default_config_path, _resolve_skills_root
    from .spec import repo_root_for

    repo = _tempfile.mkdtemp(prefix="ase-fakerepo-")
    ext = _tempfile.mkdtemp(prefix="ase-extskills-")
    try:
        nested = os.path.join(repo, "skills_examples")
        os.makedirs(os.path.join(nested, "skill-x"))
        open(os.path.join(nested, "skill-x", "SKILL.md"), "w").close()
        open(os.path.join(repo, "models.yaml"), "w").close()

        got = _resolve_skills_root(repo)
        _check("cli.skills_root.auto_descends", got == nested,
               f"repo root descends into skills_examples/ (got {got})", failures, verbose)
        _check("cli.skills_root.direct_used_as_is", _resolve_skills_root(nested) == nested,
               "a root that already holds skills is used as-is", failures, verbose)
        _check("cli.skills_root.no_skills_unchanged",
               _resolve_skills_root(ext) == os.path.abspath(ext),
               "a dir with no skills (and no skills_examples/) is returned unchanged",
               failures, verbose)
        # an external skills root: skills at the top level, its own models.yaml
        os.makedirs(os.path.join(ext, "skill-y"))
        open(os.path.join(ext, "skill-y", "SKILL.md"), "w").close()
        open(os.path.join(ext, "models.yaml"), "w").close()
        _check("cli.skills_root.external_direct", _resolve_skills_root(ext) == os.path.abspath(ext),
               "an external direct skills root is used as-is", failures, verbose)

        _check("cli.config.parent_fallback",
               _default_config_path(nested) == os.path.join(repo, "models.yaml"),
               "skills under skills_examples/ find the repo-root models.yaml", failures, verbose)
        _check("cli.config.local_preferred",
               _default_config_path(ext) == os.path.join(ext, "models.yaml"),
               "a skills root with its own models.yaml uses it", failures, verbose)

        _check("cli.repo_root_for.nested", repo_root_for(nested) == repo,
               "skills_examples/ maps back to its checkout root", failures, verbose)
        _check("cli.repo_root_for.external", repo_root_for(ext) == os.path.abspath(ext),
               "an external skills root is its own repo root", failures, verbose)
    finally:
        _shutil.rmtree(repo, ignore_errors=True)
        _shutil.rmtree(ext, ignore_errors=True)


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
                            path=".claude/skills/skill-alpha/references/foo.md"),
            NormalizedEvent(EventKind.TOOL_CALL, tool_name="Bash",
                            path=".claude/skills/skill-alpha/scripts/bar.py"),
        ]
        rr = RunResult(agent="x", eval_name="e", prompt="", workdir=ws, events=events,
                       exit_code=0, final_text="Done, printed hello",
                       structured_output={"ok": True})
        spec = EvalSpec(name="t", prompt="p", source_path="/x.yaml", skills=["skill-alpha"])
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

        _pf({"type": "skill_triggered", "skill": "skill-alpha"}, True, "skill_triggered.pass")
        _pf({"type": "skill_triggered", "skill": "sliderule-other"}, False,
            "skill_triggered.fail")

        _pf({"type": "skill_not_triggered", "skill": "sliderule-other"}, True,
            "skill_not_triggered.pass")
        _pf({"type": "skill_not_triggered", "skill": "skill-alpha"}, False,
            "skill_not_triggered.fail")

        _pf({"type": "skill_reference_read", "skill": "skill-alpha"}, True,
            "skill_reference_read.pass")
        _pf({"type": "skill_reference_read", "skill": "sliderule-other"}, False,
            "skill_reference_read.fail")

        _pf({"type": "skill_reference_not_read", "skill": "sliderule-other"}, True,
            "skill_reference_not_read.pass")
        _pf({"type": "skill_reference_not_read", "skill": "skill-alpha"}, False,
            "skill_reference_not_read.fail")

        _pf({"type": "skill_script_executed", "skill": "skill-alpha"}, True,
            "skill_script_executed.pass")
        _pf({"type": "skill_script_executed", "skill": "sliderule-other"}, False,
            "skill_script_executed.fail")

        _pf({"type": "skill_script_not_executed", "skill": "sliderule-other"}, True,
            "skill_script_not_executed.pass")
        _pf({"type": "skill_script_not_executed", "skill": "skill-alpha"}, False,
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

        # A skill read via the adapter's GLOBAL skills dir must also count as triggered:
        # isolation copies declared skills into every global dir of the isolated HOME, so
        # codex reading ~/.codex/skills/<skill>/SKILL.md used the skill even though the
        # path never mentions the project-local .agents/skills.
        rr_glob = RunResult(
            agent="codex", eval_name="e", prompt="", workdir=ws,
            events=[NormalizedEvent(
                EventKind.TOOL_CALL, tool_name="Read",
                path="/tmp/ase-home-x/.codex/skills/skill-alpha/SKILL.md")])
        ctx_glob = AssertionContext(
            spec=spec, skills_subdir=".agents/skills",
            skill_dirs=[".agents/skills", ".codex/skills", ".agents/skills"])
        ar_g = run_assertion({"type": "skill_triggered", "skill": "skill-alpha"},
                             rr_glob, ws, spec, ctx_glob)
        _check("assertion.skill_triggered.global_dir", ar_g.passed is True,
               f"a global-skills-dir read counts as triggered: {ar_g.message}",
               failures, verbose)
        # ...and without skill_dirs, the single-subdir fallback still works as before.
        ctx_fallback = AssertionContext(spec=spec, skills_subdir=".codex/skills")
        ar_f = run_assertion({"type": "skill_triggered", "skill": "skill-alpha"},
                             rr_glob, ws, spec, ctx_fallback)
        _check("assertion.skill_triggered.subdir_fallback", ar_f.passed is True,
               f"skill_dirs=None falls back to skills_subdir: {ar_f.message}",
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
               ".antigravity/skills/skill-alpha/SKILL.md" in paths,
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


def _check_exec_timeout_group_kill(failures, verbose):
    """A timed-out agent must not orphan its grandchildren: agent CLIs spawn subprocesses
    (shell tools, MCP servers), and killing only the direct child leaves them burning API
    budget and writing into a workspace the runner is about to relocate. execute() starts
    the agent in its own process group and SIGKILLs the whole group on timeout."""
    import os
    import shutil
    import sys
    import tempfile
    import time

    from .adapters.base import Adapter, ParseOutput, RunOptions
    from .exec import execute

    print("exec timeout group kill:")
    if not hasattr(os, "killpg"):
        if verbose:
            print("  [skipped — no process groups on this platform]")
        return

    tmp = tempfile.mkdtemp(prefix="ase-timeout-")
    pidfile = os.path.join(tmp, "grandchild.pid")
    script = (
        "import os, subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({pidfile!r}, 'w').write(str(p.pid))\n"
        "time.sleep(60)\n"
    )

    class _SleeperAdapter(Adapter):
        name = "fake-sleeper"
        binary = sys.executable

        def build_argv(self, prompt, opts, *, cwd):
            return [sys.executable, "-c", script]

        def parse(self, stdout, stderr, exit_code, *, opts=None):
            return ParseOutput()

    grandchild_pid = None
    try:
        start = time.monotonic()
        ex = execute(_SleeperAdapter(), "p", RunOptions(), cwd=tmp, timeout=1,
                     agent_name="fake-sleeper", eval_name="t")
        elapsed = time.monotonic() - start
        _check("exec.timeout_flagged", ex.result.timed_out and ex.result.error is not None,
               f"timeout surfaced (timed_out={ex.result.timed_out})", failures, verbose)
        _check("exec.timeout_returns_promptly", elapsed < 15,
               f"execute() returned in {elapsed:.1f}s, not hung on surviving pipes",
               failures, verbose)

        for _ in range(20):
            if os.path.isfile(pidfile):
                try:
                    grandchild_pid = int(open(pidfile).read().strip())
                    break
                except ValueError:
                    pass
            time.sleep(0.05)
        alive = None
        if grandchild_pid:
            for _ in range(40):     # give SIGKILL a moment to be delivered/reaped
                try:
                    os.kill(grandchild_pid, 0)
                    alive = True
                except ProcessLookupError:
                    alive = False
                    break
                time.sleep(0.05)
        _check("exec.timeout_kills_grandchild",
               grandchild_pid is not None and alive is False,
               f"grandchild (pid={grandchild_pid}) dead after group kill (alive={alive})",
               failures, verbose)
    finally:
        if grandchild_pid:
            try:
                os.kill(grandchild_pid, 9)   # never leak a sleeper if the check failed
            except (ProcessLookupError, PermissionError):
                pass
        shutil.rmtree(tmp, ignore_errors=True)


def _check_inline_truncation(failures, verbose):
    """The report inlines every text file but must cap each one: a legitimate multi-MB CSV
    export would otherwise land verbatim in report.md. truncate=True (report) inlines up to
    the cap with a note; truncate=False (judge) keeps the old skip-entirely behavior."""
    import os
    import shutil
    import tempfile

    from .workspace_view import inline_files

    print("inline truncation:")
    ws = tempfile.mkdtemp(prefix="ase-inline-")
    try:
        with open(os.path.join(ws, "big.csv"), "w") as fh:
            fh.write("x" * 100)
        out_trunc = inline_files(ws, max_bytes=10, truncate=True)
        _check("inline.truncated_with_note",
               "xxxxxxxxxx" in out_trunc and "truncated at 10 bytes" in out_trunc
               and "x" * 11 not in out_trunc,
               f"oversize file inlined up to the cap with a note: {out_trunc!r}",
               failures, verbose)
        out_skip = inline_files(ws, max_bytes=10)
        _check("inline.judge_still_skips", out_skip == "",
               f"without truncate, an oversize file is skipped entirely: {out_skip!r}",
               failures, verbose)
        out_full = inline_files(ws)
        _check("inline.uncapped_full", "x" * 100 in out_full,
               "no cap inlines the whole file", failures, verbose)
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def _check_model_error_heuristic(failures, verbose):
    """_looks_like_model_error must not re-label unrelated failures: the old any-of
    ("model", "not found", ...) check fired whenever stderr merely mentioned the model name
    or said "not found" about something else, steering debugging at models.yaml for wrong
    reasons."""
    from .runner import _looks_like_model_error

    print("model-error heuristic:")
    _check("modelerr.phrase_hit",
           _looks_like_model_error("", "error: invalid model 'foo'", "foo"),
           "an explicit rejection phrase fires", failures, verbose)
    _check("modelerr.model_plus_keyword",
           _looks_like_model_error("exit 1", "gpt-x is not available", "gpt-x"),
           "model id + rejection keyword fires", failures, verbose)
    _check("modelerr.unrelated_not_found",
           not _looks_like_model_error("config file not found", "", "claude-opus-4-8"),
           "'not found' about something else does NOT fire", failures, verbose)
    _check("modelerr.model_mention_alone",
           not _looks_like_model_error("network unreachable", "retrying model claude-opus",
                                       "claude-opus"),
           "mentioning the model without a rejection keyword does NOT fire",
           failures, verbose)


def _check_scenario_override_validation(failures, verbose):
    """Bad run-knob overrides in a scenario (`jobs: two`, `judge: 3`) must be a clean
    ValueError at load time — the same treatment as any other malformed scenario — not an
    unguarded int() traceback later in cmd_run. JSON scenarios, so no PyYAML needed."""
    import os
    import tempfile

    from .spec import load_scenario

    print("scenario override validation:")

    def _load(payload: str):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write(payload)
            name = f.name
        try:
            return load_scenario(name)
        finally:
            os.unlink(name)

    base = '"name": "s", "prompt": "p", "target": {"runner": "claude"}'
    for field, bad in (("jobs", '"two"'), ("max_cells", '[1]'),
                       ("isolated", '"yes"'), ("judge", '3')):
        try:
            _load("{" + base + f', "{field}": {bad}' + "}")
            ok = False
        except ValueError:
            ok = True
        _check(f"scenario.bad_{field}_rejected", ok,
               f"`{field}: {bad}` raises a clean ValueError", failures, verbose)

    scen = _load("{" + base + ', "jobs": "4"' + "}")
    _check("scenario.jobs_coerced", scen.overrides.get("jobs") == 4,
           f"a numeric string is coerced to int: {scen.overrides}", failures, verbose)


def _check_reasoning_effort(failures, verbose):
    """The typed `reasoning_effort` knob (issue #67): validated at spec load, threaded
    CLI-override > per-target pin > spec > unset through Runner into RunOptions, and mapped
    per adapter only where the CLI has an equivalent control (claude --effort, codex
    model_reasoning_effort, copilot --reasoning-effort; antigravity has none — effort lives
    in its model-id tier). Per-target pins (ModelTarget) let one run compare the same or
    different models at different efforts."""
    import os
    import shutil
    import sys
    import tempfile

    import agentskill_evals.runner as runner_mod
    from .exec import ExecResult
    from .schema import RunResult
    from .spec import (REASONING_EFFORT_LEVELS, EvalSpec, ModelTarget,
                       _spec_from_raw, load_scenario, parse_model_target)

    print("reasoning effort:")

    # A REAL 32-bit win32 harness fails every copilot _mcp_disable_args call closed
    # by design (the WOW64 gate) — copilot's positive argv-mapping checks can't run
    # there and are skipped (the gate itself is asserted in the copilot section).
    # The copilot calls also pin COPILOT_HOME to a nonexistent dir so they never
    # read — or fail closed on — this machine's real ~/.copilot (config, agents).
    _cop_wow64 = sys.platform == "win32" and sys.maxsize <= 2**32
    _cop_env = {"COPILOT_HOME": os.path.join(tempfile.gettempdir(),
                                             "ase-no-such-copilot-home")}

    # --- per-adapter argv mapping ---
    argv = get_adapter("claude").build_argv("p", RunOptions(reasoning_effort="high"), cwd="/tmp")
    _check("effort.claude_flag",
           "--effort" in argv and argv[argv.index("--effort") + 1] == "high",
           f"claude maps to --effort: {argv}", failures, verbose)
    argv = get_adapter("codex").build_argv("p", RunOptions(reasoning_effort="low"), cwd="/tmp")
    _check("effort.codex_config",
           'model_reasoning_effort="low"' in argv
           and argv[argv.index('model_reasoning_effort="low"') - 1] == "-c"
           and argv[-1] == "p",
           f"codex maps to -c model_reasoning_effort, before the positional prompt: {argv}",
           failures, verbose)
    if not _cop_wow64:
        argv = get_adapter("copilot").build_argv(
            "p", RunOptions(reasoning_effort="medium", effective_env=_cop_env),
            cwd="/tmp")
        _check("effort.copilot_flag",
               "--reasoning-effort" in argv
               and argv[argv.index("--reasoning-effort") + 1] == "medium",
               f"copilot maps to --reasoning-effort: {argv}", failures, verbose)
    for name in ("claude", "codex", "copilot"):
        _check(f"effort.{name}_supported_declared",
               get_adapter(name).supports_reasoning_effort,
               f"{name} declares supports_reasoning_effort", failures, verbose)
    ag = get_adapter("antigravity")
    _check("effort.antigravity_unsupported", not ag.supports_reasoning_effort,
           "antigravity declares no reasoning-effort support (tiered model ids instead)",
           failures, verbose)
    ag_plain = ag.build_argv("p", RunOptions(), cwd="/tmp")
    ag_effort = ag.build_argv("p", RunOptions(reasoning_effort="high"), cwd="/tmp")
    _check("effort.antigravity_argv_unchanged", ag_plain == ag_effort,
           f"antigravity argv unchanged when effort set: {ag_effort}", failures, verbose)

    # --- unset keeps every argv unchanged (existing behavior preserved) ---
    for name in ("claude", "codex") if _cop_wow64 else ("claude", "codex", "copilot"):
        opts = (RunOptions(effective_env=_cop_env) if name == "copilot"
                else RunOptions())
        plain = get_adapter(name).build_argv("p", opts, cwd="/tmp")
        _check(f"effort.{name}_absent_when_unset",
               not any("effort" in a for a in plain),
               f"{name} argv carries no effort token when unset: {plain}", failures, verbose)

    # --- spec load: normalization + typed validation ---
    s = _spec_from_raw({"prompt": "p", "reasoning_effort": " HIGH "}, "/x.yaml")
    _check("effort.spec_normalized", s.reasoning_effort == "high",
           f"value is trimmed + lowercased at load: {s.reasoning_effort!r}", failures, verbose)
    _check("effort.spec_default_none",
           _spec_from_raw({"prompt": "p"}, "/x.yaml").reasoning_effort is None,
           "unset stays None", failures, verbose)
    try:
        _spec_from_raw({"prompt": "p", "reasoning_effort": "hgih"}, "/x.yaml")
        bad_ok = False
    except ValueError as exc:
        bad_ok = all(l in str(exc) for l in REASONING_EFFORT_LEVELS)
    _check("effort.spec_bad_value_rejected", bad_ok,
           "an unknown level raises a clean ValueError naming the valid levels",
           failures, verbose)

    # --- scenario files get the same field (via the shared _spec_from_raw) ---
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write('{"name": "s", "prompt": "p", "reasoning_effort": "medium", '
                '"target": {"runner": "claude"}}')
        scen_path = f.name
    try:
        scen = load_scenario(scen_path)
    finally:
        os.unlink(scen_path)
    _check("effort.scenario_field", scen.spec.reasoning_effort == "medium",
           f"a scenario's top-level reasoning_effort lands on its spec: "
           f"{scen.spec.reasoning_effort!r}", failures, verbose)

    # --- per-target parsing: `id@effort` strings and mappings (shared by --model + scenarios) ---
    t = parse_model_target("claude-haiku-4.5@high", "--model")
    _check("effort.at_suffix_parsed", t == ModelTarget("claude-haiku-4.5", "high"),
           f"'id@effort' splits into (model, effort): {t}", failures, verbose)
    t = parse_model_target("plain-model", "--model")
    _check("effort.plain_model_no_effort", t == ModelTarget("plain-model"),
           f"a plain id stays effortless: {t}", failures, verbose)
    t = parse_model_target({"model": "m", "reasoning_effort": "LOW"}, "/s.yaml")
    _check("effort.mapping_normalized", t == ModelTarget("m", "low"),
           f"mapping form parses + normalizes: {t}", failures, verbose)
    for bad, tag in (("haiku@hgih", "bad_suffix"), ({"model": "m", "efort": "low"}, "bad_key"),
                     ({}, "empty_mapping"), ("@high", "no_model_before_at")):
        try:
            parse_model_target(bad, "--model")
            ok = False
        except ValueError:
            ok = True
        _check(f"effort.parse_{tag}_rejected", ok,
               f"{bad!r} raises a clean ValueError", failures, verbose)

    # --- artifact dirs: same model at two efforts must not collide ---
    _check("effort.target_seg_distinct",
           runner_mod._target_seg(ModelTarget("m", "high"))
           != runner_mod._target_seg(ModelTarget("m")),
           "an effort-pinned target gets its own artifacts segment", failures, verbose)

    # --- Runner threading: CLI override > per-target pin > spec > unset ---
    repo_root = tempfile.mkdtemp(prefix="ase-effort-")
    seen: dict = {}
    orig_execute = runner_mod.execute

    def _fake_execute(adapter, prompt, opts, *, cwd, timeout, env_overrides, agent_name, eval_name):
        seen["effort"] = opts.reasoning_effort
        rr = RunResult(agent=agent_name, eval_name=eval_name, prompt=prompt,
                       workdir=cwd, final_text="done")
        return ExecResult(result=rr, stdout="", stderr="")

    runner_mod.execute = _fake_execute
    try:
        r = runner_mod.Runner.__new__(runner_mod.Runner)
        r.agent, r.adapter, r.targets = "fake", _FakeAdapter(), [ModelTarget()]
        r.artifacts_root = os.path.join(repo_root, "artifacts")
        r.run_id, r.skills_root, r.judge = "run1", repo_root, None
        r.provision, r.command, r.auto_approve = False, "", True
        r.jobs, r.isolated, r.progress = 1, True, None
        r._repo_skill_names = set()
        r.run_dir = os.path.join(repo_root, "artifacts", "run1")
        os.makedirs(r.run_dir)

        spec = EvalSpec(name="demo", prompt="hi", reasoning_effort="low",
                        source_path=os.path.join(repo_root, "demo.yaml"))
        r.reasoning_effort = "high"
        r._run_cell(ModelTarget(), spec)
        _check("effort.cli_override_wins", seen.get("effort") == "high",
               f"run-level effort beats the spec's: {seen.get('effort')!r}", failures, verbose)
        r._run_cell(ModelTarget("m", "medium"), spec)
        _check("effort.cli_beats_target", seen.get("effort") == "high",
               f"run-level effort beats a per-target pin: {seen.get('effort')!r}",
               failures, verbose)
        r.reasoning_effort = None
        cell = r._run_cell(ModelTarget("m", "medium"), spec)
        _check("effort.target_beats_spec", seen.get("effort") == "medium",
               f"a per-target pin beats the spec's effort: {seen.get('effort')!r}",
               failures, verbose)
        _check("effort.cell_records_both",
               cell.reasoning_effort == "medium" and cell.effective_effort == "medium",
               f"the cell records the pinned + effective efforts: "
               f"{cell.reasoning_effort!r}/{cell.effective_effort!r}", failures, verbose)
        r._run_cell(ModelTarget(), spec)
        _check("effort.spec_value_used", seen.get("effort") == "low",
               f"spec effort used when no run-level or per-target value: "
               f"{seen.get('effort')!r}", failures, verbose)
        r._run_cell(ModelTarget(), EvalSpec(name="demo2", prompt="hi",
                                            source_path=os.path.join(repo_root, "demo2.yaml")))
        _check("effort.unset_stays_none", seen.get("effort") is None,
               f"nothing set → RunOptions.reasoning_effort is None: {seen.get('effort')!r}",
               failures, verbose)

        # a runner with NO effort control must not claim one: the resolved effort is nulled
        # before RunOptions, and effective_effort stays None (the pin is still recorded as
        # requested column identity) — otherwise summary.json/report.md would report a
        # thinking budget the CLI silently ignored.
        class _NoEffortAdapter(_FakeAdapter):
            supports_reasoning_effort = False

        r.adapter = _NoEffortAdapter()
        r.reasoning_effort = "high"
        cell = r._run_cell(ModelTarget("m", "medium"), spec)
        _check("effort.unsupported_not_claimed",
               seen.get("effort") is None and cell.effective_effort is None
               and cell.reasoning_effort == "medium",
               f"unsupporting runner: opts={seen.get('effort')!r} "
               f"effective={cell.effective_effort!r} pin={cell.reasoning_effort!r}",
               failures, verbose)
    finally:
        runner_mod.execute = orig_execute
        shutil.rmtree(repo_root, ignore_errors=True)


def _check_api_compat(failures, verbose):
    """The pre-#67 module API keeps working for programmatic callers: Runner(models=[...]),
    Runner.models / Scenario.models, and render_matrix/render_markdown called with plain
    model-id columns — each converts to an effort-less ModelTarget instead of raising."""
    import os
    import shutil
    import tempfile

    import agentskill_evals.runner as runner_mod
    from .spec import ModelTarget

    print("pre-#67 API compat:")
    repo_root = tempfile.mkdtemp(prefix="ase-compat-")
    try:
        r = runner_mod.Runner("claude", models=["m1", None],
                              artifacts_root=os.path.join(repo_root, "artifacts"),
                              run_id="compat", skills_root=repo_root)
        _check("compat.runner_models_kwarg",
               r.targets == [ModelTarget("m1"), ModelTarget(None)],
               f"models= converts to effort-less targets: {r.targets}", failures, verbose)
        _check("compat.runner_models_property", r.models == ["m1", None],
               f"Runner.models still answers with model ids: {r.models}", failures, verbose)
        try:
            runner_mod.Runner("claude", artifacts_root=os.path.join(repo_root, "artifacts"),
                              run_id="x", skills_root=repo_root)
            ok = False
        except TypeError:
            ok = True
        _check("compat.runner_requires_columns", ok,
               "neither targets= nor models= raises a clear TypeError", failures, verbose)
        out = runner_mod.render_matrix([], "claude", ["m1", None])
        _check("compat.render_matrix_plain_ids",
               "m1" in out and "default" in out,
               f"render_matrix accepts plain model-id columns: {out!r}", failures, verbose)
    finally:
        shutil.rmtree(repo_root, ignore_errors=True)


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
    _check("claude.skill_path", skill_paths == [".claude/skills/skill-alpha/SKILL.md"],
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
    # MCP kill-switch (DESIGN_MCP_Support.md Phase 0): the isolated HOME symlinks ~/.codex
    # wholesale, so any [mcp_servers.*] in the user's real config.toml loads in every run —
    # and `-c mcp_servers={}` does NOT clear it (verified 0.140.0: -c deep-merges with the
    # persisted table). Every configured server must be disabled BY NAME, on runs and on
    # model probes alike. The enumerator is patched so these argv-shape checks never depend
    # on (or read) the machine's real ~/.codex, and the post-verify — which would otherwise
    # spawn a real codex — is stubbed to a no-op here (its own fail-closed behavior is
    # exercised separately below with a mocked subprocess).
    from .adapters.codex import CodexAdapter
    orig_enum = CodexAdapter._configured_mcp_server_names
    orig_verify = CodexAdapter._verify_all_mcp_disabled
    CodexAdapter._verify_all_mcp_disabled = lambda self, *a, **k: None
    try:
        CodexAdapter._configured_mcp_server_names = \
            lambda self, cwd=None, env=None: []
        cargv = get_adapter("codex").build_argv("do the task", RunOptions(model="gpt-5.4-mini"), cwd="/tmp")
        pre = cargv[:cargv.index("exec")]  # top-level flags precede the exec subcommand
        _check("codex.argv",
               "--ask-for-approval" in pre and "never" in pre
               and "--sandbox" in pre and "workspace-write" in pre
               and "--full-auto" not in cargv and cargv[-1] == "do the task",
               f"non-interactive approval+sandbox before exec, prompt last: {cargv}", failures, verbose)
        CodexAdapter._configured_mcp_server_names = \
            lambda self, cwd=None, env=None: ["user_srv", "other-srv"]
        kargv = get_adapter("codex").build_argv("do the task", RunOptions(), cwd="/tmp")
        _check("codex.argv_mcp_killswitch",
               "mcp_servers.user_srv.enabled=false" in kargv
               and "mcp_servers.other-srv.enabled=false" in kargv
               and "mcp_servers={}" not in kargv
               and kargv[kargv.index("mcp_servers.user_srv.enabled=false") - 1] == "-c",
               f"every configured server disabled by name (not the ineffective empty-table "
               f"override): {kargv}", failures, verbose)
        pargv = get_adapter("codex")._probe_argv("gpt-5.4-mini")
        _check("codex.probe_mcp_killswitch",
               "mcp_servers.user_srv.enabled=false" in pargv
               and "mcp_servers.other-srv.enabled=false" in pargv,
               f"per-name disables also passed on model probes: {pargv}", failures, verbose)
        # a name outside codex's own `mcp add` charset can't be addressed by -c dotted
        # paths; the quoted form is emitted anyway so codex refuses to load its config —
        # the run fails closed instead of silently starting the server.
        CodexAdapter._configured_mcp_server_names = \
            lambda self, cwd=None, env=None: ["weird name.v2"]
        import contextlib as _ctx
        import io as _io
        _stderr = _io.StringIO()
        with _ctx.redirect_stderr(_stderr):
            wargv = get_adapter("codex").build_argv("p", RunOptions(), cwd="/tmp")
        _check("codex.exotic_mcp_name_fails_closed",
               'mcp_servers."weird name.v2".enabled=false' in wargv
               and "fail closed" in _stderr.getvalue(),
               f"non-bare-key server name → quoted override (codex errors out, fail-closed) "
               f"+ warning: {wargv}", failures, verbose)
        # enumeration must see the CHILD's context, not the harness's: build_argv forwards
        # its cwd and opts.effective_env (the exact subprocess env, set by exec.execute()
        # before argv construction) — a trusted project's .codex/config.toml contributes
        # servers by cwd, and a scenario's `env: {CODEX_HOME: ...}` moves the global
        # config (both verified 0.140.0), so enumerating from the harness context would
        # miss or over-disable (and over-disabling kills the run, "invalid transport").
        seen_ctx: dict = {}

        def _spy_enum(self, cwd=None, env=None):
            seen_ctx["cwd"], seen_ctx["env"] = cwd, env
            return []

        CodexAdapter._configured_mcp_server_names = _spy_enum
        child_env = {"CODEX_HOME": "/custom/codex-home", "HOME": "/child-home"}
        get_adapter("codex").build_argv(
            "p", RunOptions(effective_env=child_env), cwd="/the/child/ws")
        _check("codex.enumeration_uses_child_context",
               seen_ctx.get("cwd") == "/the/child/ws" and seen_ctx.get("env") == child_env,
               f"build_argv hands the child's cwd + effective env to the MCP enumerator: "
               f"{seen_ctx}", failures, verbose)
        # `--disable plugins` must ride on every codex invocation: plugins can ship
        # .mcp.json servers that never appear in config.toml (verified — enumeration can't
        # see them, and a -c disable for a non-config name breaks the run with "invalid
        # transport"). Enumerator is [] here so the probe builds cleanly.
        CodexAdapter._configured_mcp_server_names = \
            lambda self, cwd=None, env=None: []
        _check("codex.argv_plugins_disabled",
               _flag_pair(cargv, "--disable", "plugins")
               and _flag_pair(get_adapter("codex")._probe_argv("m"), "--disable", "plugins"),
               "--disable plugins on runs and probes closes the plugin MCP channel",
               failures, verbose)
        # extra_args ride AFTER the kill-switch overrides and their post-verify, where a
        # config-channel token would outrank the verified MCP-off state (verified
        # 0.140.0: the later of two duplicate `-c mcp_servers.<n>.enabled=` overrides
        # wins) or switch which configuration loads (--profile/-p per-profile configs,
        # --cd/-C project discovery, --enable plugins re-opening the plugin channel).
        # build_argv fails closed on every spelling — separate value, attached short
        # form, --flag=value — while neutral extra_args still pass through verbatim.
        ok_argv = get_adapter("codex").build_argv(
            "p", RunOptions(extra_args=["--skip-git-repo-check"]), cwd="/tmp")
        _leaked = []
        for _extras in (["-c", "mcp_servers.x.enabled=true"],
                        ["-cmcp_servers.x.enabled=true"],
                        ["--config", "mcp_servers.x.enabled=true"],
                        ["--config=mcp_servers.x.enabled=true"],
                        ["--enable", "plugins"],
                        ["--profile", "work"], ["-p", "work"],
                        ["--cd", "/elsewhere"], ["-C", "/elsewhere"]):
            try:
                get_adapter("codex").build_argv(
                    "p", RunOptions(extra_args=_extras), cwd="/tmp")
                _leaked.append(_extras)
            except RuntimeError:
                pass
        _check("codex.extra_args_config_channels_fail_closed",
               "--skip-git-repo-check" in ok_argv and not _leaked,
               f"config-channel extra_args fail closed in every spelling; neutral ones "
               f"pass through: leaked={_leaked}", failures, verbose)
    finally:
        CodexAdapter._configured_mcp_server_names = orig_enum
        CodexAdapter._verify_all_mcp_disabled = orig_verify

    # The primary — and ONLY — MCP enumerator is codex itself (`codex --disable plugins
    # mcp list --json`); the fixture config.toml drives the live cross-checks below. There
    # is no offline fallback: a hand-parsed subset of one config.toml misses the
    # system/managed layers codex reads, so any failure of the CLI enumeration FAILS
    # CLOSED. Profiles never contribute (0.140.0 rejects the legacy `profile=` key and
    # ignores inactive [profiles.*] tables), so `eps`/`gamma` must not appear in codex's
    # own listing either.
    import os
    import shutil as _sh
    import subprocess as _sp
    import tempfile as _tmp
    import types as _types

    import agentskill_evals.adapters.codex as codex_mod
    _codex_home = _tmp.mkdtemp(prefix="ase-codexhome-")
    _codex_ws = _tmp.mkdtemp(prefix="ase-codexws-")
    try:
        with open(os.path.join(_codex_home, "config.toml"), "w") as fh:
            fh.write('model = "gpt-5.4"\n'
                     'mcp_servers.delta = { command = "echo", env = { NESTED = "1" } }\n'
                     '[mcp_servers.alpha]\ncommand = "echo"\n'
                     '[mcp_servers."weird name.v2"]\ncommand = "echo"\n'
                     '[profiles.work.mcp_servers.eps]\ncommand = "echo"\n'
                     '[profiles.side]\nmcp_servers.gamma = { command = "echo" }\n')
        expected = ["alpha", "delta", "weird name.v2"]
        _cx_env = {**os.environ, "CODEX_HOME": _codex_home}

        # a project .codex/config.toml above the cwd — used by the live trusted-project
        # cross-check further down.
        _proj = os.path.join(_codex_ws, "proj", "sub")
        os.makedirs(os.path.join(_codex_ws, "proj", ".codex"))
        os.makedirs(_proj)
        with open(os.path.join(_codex_ws, "proj", ".codex", "config.toml"), "w") as fh:
            fh.write('[mcp_servers.project_srv]\ncommand = "echo"\n')

        # NO offline fallback: any primary-enumeration failure fails CLOSED. Parsing only
        # the global config.toml would miss codex's system/managed layers, and a later
        # successful main invocation could then launch an un-disabled server.
        _cli_down = CodexAdapter()
        _cli_down._mcp_names_via_cli = lambda cwd=None, env=None: None
        try:
            _cli_down._configured_mcp_server_names(cwd=_proj, env=_cx_env)
            enum_closed = False
        except RuntimeError as exc:
            enum_closed = "failing closed" in str(exc)
        _check("codex.mcp_enumeration_fails_closed", enum_closed,
               "a failed `codex mcp list --json` raises (fail closed) instead of parsing "
               "an incomplete offline view of config.toml that misses system/managed "
               "layers", failures, verbose)

        # `mcp list --json` output is validated strictly: schema drift must fail closed,
        # never be read as an authoritative "no servers".
        _v = codex_mod._validate_mcp_list_json
        _check("codex.mcp_list_json_strict",
               _v([{"name": "s1", "enabled": True}, {"name": "s0"}]) == ["s0", "s1"]
               and _v([]) == []
               and _v(None) is None                      # `null` stdout
               and _v({"servers": [{"name": "s1"}]}) is None  # wrapped-object drift
               and _v([{"name": 7}]) is None             # non-string name
               and _v([{"no_name": "x"}]) is None,       # entry without a name
               "only a list of {name: str} objects is authoritative; anything else fails "
               "closed", failures, verbose)

        # post-verify recognizes an ENABLED server: an entry counts as enabled unless it
        # carries `"enabled": false`; drift → None so the caller fails closed.
        _en = codex_mod._enabled_mcp_names
        _check("codex.mcp_enabled_names",
               _en([{"name": "a", "enabled": False}, {"name": "b"}]) == {"b"}
               and _en([{"name": "a", "enabled": False}]) == set()
               and _en([]) == set()
               and _en(None) is None
               and _en([{"no_name": 1}]) is None,
               "enabled unless `enabled: false`; unrecognized shape → None (fail closed)",
               failures, verbose)

        # after the -c ...enabled=false overrides are applied, codex's OWN re-enumeration
        # (same cwd/env, overrides in place) must show every server disabled — a
        # higher-precedence managed/MDM layer can outrank -c, so a server still reported
        # enabled (or an unreadable re-check) FAILS CLOSED. subprocess is stubbed via a
        # namespace swap (no global monkeypatch of the real module).
        _pv = CodexAdapter()

        def _fake_all_disabled(argv, **kw):
            return _types.SimpleNamespace(
                returncode=0, stdout='[{"name": "user_srv", "enabled": false}]')

        def _fake_still_enabled(argv, **kw):
            return _types.SimpleNamespace(
                returncode=0, stdout='[{"name": "user_srv", "enabled": true}]')

        _real_cx_sp = codex_mod.subprocess
        try:
            codex_mod.subprocess = _types.SimpleNamespace(
                run=_fake_all_disabled, DEVNULL=_sp.DEVNULL,
                TimeoutExpired=_sp.TimeoutExpired)
            pv_ok = True
            try:
                _pv._verify_all_mcp_disabled(
                    ["-c", "mcp_servers.user_srv.enabled=false"], cwd="/tmp", env=_cx_env)
            except RuntimeError:
                pv_ok = False
            codex_mod.subprocess.run = _fake_still_enabled
            try:
                _pv._verify_all_mcp_disabled(
                    ["-c", "mcp_servers.user_srv.enabled=false"], cwd="/tmp", env=_cx_env)
                pv_closed = False
            except RuntimeError as exc:
                pv_closed = "still enabled" in str(exc)
        finally:
            codex_mod.subprocess = _real_cx_sp
        _check("codex.mcp_post_verify_fails_closed",
               pv_ok and pv_closed,
               "post-verify passes when the re-check confirms every server disabled, and "
               "fails closed when a managed layer keeps one enabled", failures, verbose)

        # live cross-checks when the codex CLI is installed: its own effective-config
        # view (the primary enumerator at runtime) must match the fixture — and must
        # SHIFT with the cwd: a trusted project's .codex/config.toml contributes servers
        # (verified 0.140.0), which is exactly why enumeration runs in the child's
        # context. env is passed explicitly — no os.environ mutation.
        if get_adapter("codex").is_available():
            via_cli = get_adapter("codex")._mcp_names_via_cli(cwd=_codex_ws, env=_cx_env)
            _check("codex.mcp_enumeration_via_cli",
                   via_cli == expected,
                   f"`codex --disable plugins mcp list --json` agrees on the fixture: "
                   f"{via_cli}", failures, verbose)
            if _sh.which("git"):
                _troot = os.path.join(_codex_ws, "proj")
                _sp.run(["git", "init", "-q", _troot], check=True,
                        capture_output=True, env=_cx_env)
                with open(os.path.join(_codex_home, "config.toml"), "a") as fh:
                    fh.write(f'\n[projects."{os.path.realpath(_troot)}"]\n'
                             f'trust_level = "trusted"\n')
                in_proj = get_adapter("codex")._mcp_names_via_cli(cwd=_proj, env=_cx_env)
                outside = get_adapter("codex")._mcp_names_via_cli(cwd=_codex_ws,
                                                                  env=_cx_env)
                _check("codex.mcp_enumeration_trusted_project_by_cwd",
                       in_proj == sorted(expected + ["project_srv"])
                       and outside == expected,
                       f"a trusted project's .codex/config.toml contributes servers from "
                       f"inside its tree (even a subdir) and not from outside: "
                       f"in={in_proj}, out={outside}", failures, verbose)
            elif verbose:
                print("  [skipped — git not installed] "
                      "codex.mcp_enumeration_trusted_project_by_cwd")
        elif verbose:
            print("  [skipped — codex CLI not installed] codex.mcp_enumeration_via_cli")
    finally:
        _sh.rmtree(_codex_home, ignore_errors=True)
        _sh.rmtree(_codex_ws, ignore_errors=True)
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
    _check("copilot.skill_path", skill_paths == [".agents/skills/skill-beta/SKILL.md"],
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
    import sys

    # A REAL 32-bit win32 harness fails every copilot _mcp_disable_args call closed
    # by design (the WOW64 gate, asserted below via a shim AND — on such a host —
    # natively here). The positive argv/enumeration checks in this section can't run
    # there: they get architecture-aware skips instead of crashing the suite. The
    # argv-shape calls also pin COPILOT_HOME to a nonexistent dir so they never
    # read — or fail closed on — this machine's real ~/.copilot (config, agents).
    _cop_wow64 = sys.platform == "win32" and sys.maxsize <= 2**32
    _cop_env = {"COPILOT_HOME": os.path.join(_tmp.gettempdir(),
                                             "ase-no-such-copilot-home")}
    if _cop_wow64:
        try:
            get_adapter("copilot").build_argv(
                "do the task", RunOptions(model="auto", effective_env=_cop_env),
                cwd="/tmp")
            _wow64_msg = "did not raise"
        except RuntimeError as exc:
            _wow64_msg = str(exc)
        _check("copilot.argv_32bit_win32_fails_closed",
               "32-bit" in _wow64_msg and "failing closed" in _wow64_msg,
               f"a real 32-bit win32 harness fails copilot argv construction closed "
               f"(WOW64 gate); positive argv checks skipped: {_wow64_msg}",
               failures, verbose)
    else:
        cargv = get_adapter("copilot").build_argv(
            "do the task",
            RunOptions(model="auto", effective_env=_cop_env,
                       extra_args=["--banner"]),  # neutral extra_args pass through
            cwd="/tmp")
        _check("copilot.argv",
               cargv[0] == "copilot" and "-p" in cargv and "--output-format" in cargv
               and "json" in cargv and "--allow-all" in cargv and "--model" in cargv
               and "--banner" in cargv and cargv[-1] != "do the task",
               f"copilot argv: {cargv}", failures, verbose)
        cargv_dt = get_adapter("copilot").build_argv(
            "judge", RunOptions(disable_tools=True, effective_env=_cop_env),
            cwd="/tmp")
        _check("copilot.disable_tools",
               "--available-tools" in cargv_dt
               and cargv_dt[cargv_dt.index("--available-tools") + 1] == "",
               f"disable_tools → --available-tools '': {cargv_dt}", failures, verbose)
    _check("copilot.no_output_schema",
           not get_adapter("copilot").supports_output_schema,
           "copilot has no native output schema support", failures, verbose)
    # copilot has no "ignore user MCP config" flag (--disable-builtin-mcps only covers the
    # bundled GitHub server) and loads servers from the user config, installed plugins, AND
    # workspace configs — masks cover the first two under isolation; COPILOT_HOME must be
    # mirrored or a set var bypasses them (design, Phase 0). The mcp-config mask must be
    # the empty shape copilot ACCEPTS: bare "{}" fails validation ("mcpServers: Required",
    # verified 1.0.64) and would kill every isolated run before execution.
    _cop_masks = get_adapter("copilot").isolation_config_masks
    _check("copilot.mcp_config_mask_declared",
           _cop_masks.get(".copilot/mcp-config.json") == '{"mcpServers": {}}'
           and _cop_masks.get(".copilot/installed-plugins", "x") is None
           and _cop_masks.get(".copilot/agents", "x") is None
           and callable(_cop_masks.get(".copilot/config.json"))
           and get_adapter("copilot").isolation_config_homes
           == [("COPILOT_HOME", ".copilot", None)],
           "copilot masks mcp-config.json (valid empty shape), installed-plugins, "
           "agents/ (frontmatter mcp-servers start outside --disable-mcp-server), and "
           "sanitizes config.json; COPILOT_HOME mirrored", failures, verbose)

    # the config.json sanitizer: plugin records there carry an absolute cache_path the
    # loader follows even when installed-plugins/ is masked empty (verified 1.0.64) — they
    # must go; auth/settings must stay; the file's JSONC comment lines must not break it;
    # and customAgents.defaultLocalOnly must come out FORCED true — the documented setting
    # (`copilot help config`) that is the only off-switch for remote custom-agent
    # discovery, whose org/enterprise listings can carry mcp-servers outside the
    # --disable-mcp-server set (1.0.64 bundle).
    import json as _json
    from .adapters.copilot import _sanitized_copilot_config
    _san_dir = _tmp.mkdtemp(prefix="ase-copcfg-")
    try:
        cfg_path = os.path.join(_san_dir, "config.json")
        with open(cfg_path, "w") as fh:
            fh.write('// User settings belong in settings.json.\n'
                     '// This file is managed automatically.\n'
                     '{\n'
                     '  "copilotTokens": {"github.com": "tok-KEEP"},\n'
                     '  "installedPlugins": [{"name": "linear", "enabled": true,'
                     ' "cache_path": "/real/plugins/linear"}],\n'
                     '  "enabledPlugins": {"linear": true},\n'
                     '  "customAgents": {"defaultLocalOnly": false, "keep": 1}\n'
                     '}\n')
        sanitized = _json.loads(_sanitized_copilot_config(cfg_path))
        _check("copilot.config_sanitizer",
               sanitized.get("copilotTokens") == {"github.com": "tok-KEEP"}
               and "installedPlugins" not in sanitized
               and "enabledPlugins" not in sanitized
               and sanitized.get("customAgents") == {"defaultLocalOnly": True,
                                                     "keep": 1},
               f"plugin registrations dropped, auth kept, JSONC comments handled, "
               f"defaultLocalOnly forced true over an explicit false (sibling keys "
               f"kept): {sorted(sanitized)}", failures, verbose)
        _check("copilot.config_sanitizer_fail_closed",
               _json.loads(_sanitized_copilot_config(os.path.join(_san_dir, "nope.json")))
               == {"customAgents": {"defaultLocalOnly": True}},
               "an unreadable config.json sanitizes to the neutral-but-hermetic shape "
               "(no plugins can load, remote agents opted out)",
               failures, verbose)
    finally:
        _sh.rmtree(_san_dir, ignore_errors=True)

    # The whole enumeration block below builds positive copilot argv — skipped on a
    # real 32-bit win32 harness, where _mcp_disable_args fails closed by design
    # (see copilot.argv_32bit_win32_fails_closed above).
    if not _cop_wow64:
        # --disable-mcp-server enumeration: user config ($COPILOT_HOME else ~/.copilot) plus
        # the workspace .mcp.json/.github/mcp.json/.vscode/mcp.json copilot discovers from the
        # run cwd UPWARD (it walks ancestors) — this is what covers probes, judge runs,
        # non-isolated runs, and scenario-seeded workspace configs.
        import json as _json
        _cop_home = _tmp.mkdtemp(prefix="ase-cophome-")
        _cop_ws_root = _tmp.mkdtemp(prefix="ase-copws-")
        _old_cop_home = os.environ.get("COPILOT_HOME")
        try:
            with open(os.path.join(_cop_home, "mcp-config.json"), "w") as fh:
                _json.dump({"mcpServers": {"user-srv": {"command": "echo"}}}, fh)
            ws = os.path.join(_cop_ws_root, "nested", "ws")
            os.makedirs(os.path.join(ws, ".github"))
            os.makedirs(os.path.join(ws, ".vscode"))
            with open(os.path.join(ws, ".mcp.json"), "w") as fh:
                _json.dump({"mcpServers": {"ws-srv": {}}}, fh)
            with open(os.path.join(ws, ".github", "mcp.json"), "w") as fh:
                _json.dump({"mcpServers": {"gh-srv": {}}}, fh)
            with open(os.path.join(ws, ".vscode", "mcp.json"), "w") as fh:
                _json.dump({"servers": {"vsc-srv": {}}}, fh)   # the .vscode key spelling
            with open(os.path.join(_cop_ws_root, ".mcp.json"), "w") as fh:
                _json.dump({"mcpServers": {"ancestor-srv": {}}}, fh)
            os.environ["COPILOT_HOME"] = _cop_home
            dargv = get_adapter("copilot").build_argv("do it", RunOptions(), cwd=ws)
            disabled = [dargv[i + 1] for i, a in enumerate(dargv) if a == "--disable-mcp-server"]
            _check("copilot.mcp_disable_enumeration",
                   {"user-srv", "ws-srv", "gh-srv", "vsc-srv", "ancestor-srv"} <= set(disabled),
                   f"user + workspace + ancestor servers all disabled by name: {disabled}",
                   failures, verbose)
            # ...and the user config must resolve from the CHILD's env, not the harness's:
            # a scenario's `env: {COPILOT_HOME: ...}` override reaches build_argv through
            # opts.effective_env (exec.execute() sets it), with no os.environ involvement.
            os.environ.pop("COPILOT_HOME", None)
            eargv = get_adapter("copilot").build_argv(
                "do it", RunOptions(effective_env={"COPILOT_HOME": _cop_home}), cwd=None)
            edis = [eargv[i + 1] for i, a in enumerate(eargv) if a == "--disable-mcp-server"]
            _check("copilot.mcp_disable_uses_child_env",
                   "user-srv" in edis,
                   f"a COPILOT_HOME set only in the child's effective env still enumerates "
                   f"its servers: {edis}", failures, verbose)
            # ...and a RELATIVE COPILOT_HOME must resolve against the CHILD's cwd — copilot
            # resolves it against its own process cwd (verified 1.0.64: `copilot mcp list`
            # with COPILOT_HOME=relhome reads <its cwd>/relhome), so anchoring to the
            # harness's cwd would enumerate a different config than the run actually loads
            # (direct and `isolated: false` runs; isolation clears/repoints the var).
            rel_cop_home = os.path.join(ws, "rel-cop-home")
            os.makedirs(rel_cop_home)
            with open(os.path.join(rel_cop_home, "mcp-config.json"), "w") as fh:
                _json.dump({"mcpServers": {"rel-srv": {"command": "echo"}}}, fh)
            rargv = get_adapter("copilot").build_argv(
                "do it", RunOptions(effective_env={"COPILOT_HOME": "rel-cop-home"}), cwd=ws)
            rdis = [rargv[i + 1] for i, a in enumerate(rargv) if a == "--disable-mcp-server"]
            _check("copilot.mcp_disable_relative_home_uses_child_cwd",
                   "rel-srv" in rdis,
                   f"a relative COPILOT_HOME enumerates from the child's cwd, not the "
                   f"harness's: {rdis}", failures, verbose)
            # ...and a SET-BUT-EMPTY home var must enumerate <child cwd>/.copilot — Node's
            # homedir() returns "" for a set-but-empty HOME, so copilot's ".copilot" join
            # goes relative and resolves against its process cwd (verified 1.0.64: `copilot
            # mcp list` with HOME="" reads <its cwd>/.copilot). Treating empty as unset
            # would enumerate the harness user's real ~/.copilot instead — the wrong config.
            # POSIX-only: this empty-base-is-relative behaviour is Node/libuv's POSIX rule;
            # on win32 libuv REJECTS an empty %USERPROFILE% (fail closed — see home_resolution).
            if os.name != "nt":
                cwd_cop_home = os.path.join(ws, ".copilot")
                os.makedirs(cwd_cop_home)
                with open(os.path.join(cwd_cop_home, "mcp-config.json"), "w") as fh:
                    _json.dump({"mcpServers": {"empty-home-srv": {"command": "echo"}}}, fh)
                hargv = get_adapter("copilot").build_argv(
                    "do it", RunOptions(effective_env={"HOME": ""}), cwd=ws)
                hdis = [hargv[i + 1] for i, a in enumerate(hargv) if a == "--disable-mcp-server"]
                _check("copilot.mcp_disable_empty_home_uses_child_cwd",
                       "empty-home-srv" in hdis,
                       f"a set-but-empty HOME enumerates <child cwd>/.copilot, as copilot "
                       f"does: {hdis}", failures, verbose)
        finally:
            if _old_cop_home is None:
                os.environ.pop("COPILOT_HOME", None)
            else:
                os.environ["COPILOT_HOME"] = _old_cop_home
            _sh.rmtree(_cop_home, ignore_errors=True)
            _sh.rmtree(_cop_ws_root, ignore_errors=True)

    # Custom agents are an MCP channel --disable-mcp-server does NOT reach: a selected
    # agent's frontmatter mcp-servers are started per name by initializeMcpHost without
    # consulting disabledMcpServers, and no flag disables custom agents (1.0.64 bundle).
    # So _mcp_disable_args must FAIL CLOSED on any discoverable local agent file —
    # <home>/agents plus .github/agents / .claude/agents from the run cwd up to the
    # nearest git root (inclusive), copilot's own walk boundary — and, in a git-repo
    # cwd, on a config that doesn't provably opt out of the REMOTE org/enterprise
    # agents listing (customAgents.defaultLocalOnly, which the sanitizer injects).
    if not _cop_wow64:
        _ag_dir = _tmp.mkdtemp(prefix="ase-copagents-")
        try:
            _ag_home = os.path.join(_ag_dir, "home")
            _ag_repo = os.path.join(_ag_dir, "repo")
            _ag_ws = os.path.join(_ag_repo, "ws")
            os.makedirs(_ag_home)
            os.makedirs(_ag_ws)
            _ag_env = {"COPILOT_HOME": _ag_home}

            def _cop_agents_err(cwd):
                try:
                    get_adapter("copilot").build_argv(
                        "p", RunOptions(effective_env=_ag_env), cwd=cwd)
                    return ""
                except RuntimeError as exc:
                    return str(exc)

            ag_clean = _cop_agents_err(_ag_ws) == ""      # empty tree builds fine
            os.makedirs(os.path.join(_ag_home, "agents", "nested"))
            open(os.path.join(_ag_home, "agents", "readme.txt"), "w").close()
            ag_nonmd = _cop_agents_err(_ag_ws) == ""      # only *.md is an agent
            # case-insensitive superset of copilot's *.md glob, at any depth
            open(os.path.join(_ag_home, "agents", "nested", "Helper.MD"), "w").close()
            _e = _cop_agents_err(_ag_ws)
            ag_home_md = "custom-agent" in _e and "failing closed" in _e
            _sh.rmtree(os.path.join(_ag_home, "agents"))
            os.makedirs(os.path.join(_ag_repo, ".github", "agents"))
            open(os.path.join(_ag_repo, ".github", "agents", "x.md"), "w").close()
            ag_github = "custom-agent" in _cop_agents_err(_ag_ws)
            _sh.rmtree(os.path.join(_ag_repo, ".github"))
            os.makedirs(os.path.join(_ag_repo, ".claude", "agents"))
            open(os.path.join(_ag_repo, ".claude", "agents", "x.agent.md"), "w").close()
            ag_claude = "custom-agent" in _cop_agents_err(_ag_ws)
            _sh.rmtree(os.path.join(_ag_repo, ".claude"))
            _check("copilot.custom_agents_fail_closed",
                   ag_clean and ag_nonmd and ag_home_md and ag_github and ag_claude,
                   "any *.md under <home>/agents (recursive, case-insensitive) or a "
                   ".github/agents / .claude/agents dir on the cwd walk fails closed "
                   "(their mcp-servers start outside --disable-mcp-server); empty "
                   "dirs and non-md files don't", failures, verbose)

            # remote agents: a git-repo cwd requires the defaultLocalOnly opt-out —
            # and the walk must stop at the git root exactly as copilot's does (an
            # agent file ABOVE it is not copilot's to discover).
            os.makedirs(os.path.join(_ag_repo, ".git"))
            os.makedirs(os.path.join(_ag_dir, ".claude", "agents"))
            open(os.path.join(_ag_dir, ".claude", "agents", "above.md"), "w").close()
            with open(os.path.join(_ag_home, "config.json"), "w") as fh:
                fh.write('{"customAgents": {"defaultLocalOnly": true}}')
            ag_boundary = _cop_agents_err(_ag_ws) == ""   # boundary + opt-out → runs
            os.remove(os.path.join(_ag_home, "config.json"))
            _e = _cop_agents_err(_ag_ws)
            ag_remote = "defaultLocalOnly" in _e and "failing closed" in _e
            _sh.rmtree(os.path.join(_ag_repo, ".git"))
            with open(os.path.join(_ag_repo, ".git"), "w") as fh:
                fh.write("gitdir: /elsewhere\n")           # worktree-style .git FILE
            ag_gitfile = "defaultLocalOnly" in _cop_agents_err(_ag_ws)
            _check("copilot.remote_agents_fail_closed",
                   ag_boundary and ag_remote and ag_gitfile,
                   "a git-repo cwd (dir or worktree-file .git) without the "
                   "customAgents.defaultLocalOnly opt-out fails closed — remote "
                   "org/enterprise agents can carry mcp-servers outside the disable "
                   "set; with the opt-out (as the sanitizer injects) it runs, and "
                   "the agent walk stops at the git root exactly like copilot's",
                   failures, verbose)
        finally:
            _sh.rmtree(_ag_dir, ignore_errors=True)

    # extra_args configuration channels fail closed (mirrors the codex vet): each
    # spelling raises BEFORE enumeration (proven by stubbing the enumerator to a
    # sentinel), neutral tokens reach enumeration untouched. Arch-independent — the
    # vet precedes even the WOW64 gate.
    _CopAd = type(get_adapter("copilot"))
    _orig_cop_disable = _CopAd._mcp_disable_args

    def _sentinel_disable(self, cwd, env=None):
        raise AssertionError("enumeration ran before the extra_args vet")

    _CopAd._mcp_disable_args = _sentinel_disable
    try:
        _cop_leaked = []
        for _extras in (["--additional-mcp-config", '{"mcpServers":{"x":{}}}'],
                        ["--additional-mcp-config=@/x/mcp.json"],
                        ["--agent", "helper"], ["--agent=helper"],
                        ["--plugin-dir", "/x/plug"], ["--plugin-dir=/x/plug"],
                        ["--config-dir", "/x/cfg"], ["--config-dir=/x/cfg"],
                        ["-C", "/elsewhere"], ["-C/elsewhere"]):
            try:
                get_adapter("copilot").build_argv(
                    "p", RunOptions(extra_args=_extras), cwd="/tmp")
                _cop_leaked.append(_extras)
            except RuntimeError as exc:
                if "configuration channel" not in str(exc):
                    _cop_leaked.append(_extras)      # raised, but not by the vet
        try:
            get_adapter("copilot").build_argv(
                "p", RunOptions(extra_args=["--banner"]), cwd="/tmp")
            _cop_neutral = "enumeration-skipped"     # sentinel must have fired
        except AssertionError:
            _cop_neutral = "vet-passed"
        except RuntimeError:
            _cop_neutral = "vetoed"
    finally:
        _CopAd._mcp_disable_args = _orig_cop_disable
    _check("copilot.extra_args_config_channels_fail_closed",
           not _cop_leaked and _cop_neutral == "vet-passed",
           f"config-channel extra_args (--additional-mcp-config/--agent/--plugin-dir/"
           f"--config-dir, =value and attached -C forms) raise before enumeration; "
           f"neutral tokens reach it: leaked={_cop_leaked}, neutral={_cop_neutral}",
           failures, verbose)

    # Windows ODR registry servers (design §2): copilot discovers MCP servers via a
    # registry-advertised command's `mcp list` output and registers them under names
    # sanitized to [0-9a-zA-Z_./@-] with FNV-1a-32 de-collision — reproduced exactly so
    # --disable-mcp-server can hit them (over-disabling is harmless, verified 1.0.64).
    # The pipeline is pure-python + a subprocess, so it's fully testable off-Windows by
    # stubbing only the registry read; hash vectors are verified against the bundle's JS
    # algorithm run under node.
    import sys

    import agentskill_evals.adapters.copilot as copilot_mod
    _check("copilot.odr_name_resolution",
           copilot_mod._odr_fnv1a32("a_b") == "1ba46871"
           and copilot_mod._odr_fnv1a32("my server") == "809ef0ee"
           and copilot_mod._odr_fnv1a32("日本語サーバ") == "2c06a795"
           and copilot_mod._odr_resolved_names(
               [{"name": "a b"}, {"name": "a_b"}, {"name": "a_b"}, "junk", {"name": ""}])
           == ["a_b", "a_b_1ba46871", "a_b_1ba46871_2"]
           and copilot_mod._split_odr_command('"C:\\odr host.exe" --flag "two words"')
           == ("C:\\odr host.exe", ["--flag", "two words"])
           and copilot_mod._split_odr_command("odr.exe serve")
           == ("odr.exe", ["serve"]),
           "name sanitize/de-collide (node-verified FNV-1a) + command-line split match "
           "copilot's ODR converter", failures, verbose)
    # copilot reads `entry.server ?? entry` (a wrapped {"server": {...}} entry is
    # unwrapped) and sanitizes per UTF-16 code UNIT, so an astral character (two surrogate
    # units) becomes TWO underscores — Python's per-code-point re.sub would emit only one,
    # disabling the wrong/no server.
    _check("copilot.odr_name_resolution_wrapped_and_astral",
           copilot_mod._odr_sanitize_name("x😀y") == "x__y"
           and copilot_mod._odr_sanitize_name("a\u00e9b") == "a_b"   # BMP non-ASCII → one _
           and copilot_mod._odr_resolved_names(
               [{"server": {"name": "wrapped server"}}, {"name": "x😀y"}])
           == ["wrapped_server", "x__y"],
           "wrapped entries are unwrapped and astral chars sanitize per UTF-16 unit "
           "(→ __), matching copilot", failures, verbose)
    # The ODR command line is trimmed and split with ECMAScript whitespace, not
    # Python's — /\s/ and String.trim() exclude U+001C–U+001F and U+0085 (Python-space,
    # so str.split(None) would split there and hand the same executable DIFFERENT
    # arguments than copilot passes) and include U+FEFF (not Python-space, so a
    # BOM-wrapped value would not trim). Expectations below are byte-identical to the
    # bundle's split function replayed under node v24 (the runtime copilot ships on).
    # The unquoted branch mirrors copilot's /^([^\s]+)\s*(.*)$/: a line terminator
    # directly after the first token is consumed by \s*, but one interrupting the
    # tail fails the regex — copilot then skips ODR entirely (its loader catches);
    # the harness raises and fails closed there, the conservative side.

    def _cop_split_raises(cmdline):
        try:
            copilot_mod._split_odr_command(cmdline)
        except ValueError:
            return True
        return False

    _check("copilot.odr_split_js_whitespace",
           copilot_mod._split_odr_command("C:\\odr.exe\u001carg")
           == ("C:\\odr.exe\u001carg", [])
           and copilot_mod._split_odr_command("C:\\odr.exe\u0085arg")
           == ("C:\\odr.exe\u0085arg", [])
           and copilot_mod._split_odr_command("C:\\odr.exe\u001farg")
           == ("C:\\odr.exe\u001farg", [])
           and copilot_mod._split_odr_command("\ufeffC:\\odr\\host.exe mcp-src\ufeff")
           == ("C:\\odr\\host.exe", ["mcp-src"])
           and copilot_mod._split_odr_command("C:\\odr.exe\u00a0--flag\u3000x")
           == ("C:\\odr.exe", ["--flag", "x"])
           and copilot_mod._split_odr_command("C:\\odr.exe a\u001cb c")
           == ("C:\\odr.exe", ["a\u001cb", "c"])
           and copilot_mod._split_odr_command("exe\ncd") == ("exe", ["cd"])
           and copilot_mod._split_odr_command('"C:\\exe" a\nb')
           == ("C:\\exe", ["a", "b"])
           and copilot_mod._split_odr_command('"C:\\exe"\ufeffa') == ("C:\\exe", ["a"])
           and copilot_mod._split_odr_command("C:\\odr.exe\u2028x")
           == ("C:\\odr.exe", ["x"])
           and _cop_split_raises("exe a\nb")
           and _cop_split_raises("C:\\odr.exe a\u2028b"),
           "ODR splitting uses the exact ECMAScript whitespace class (U+001C/U+0085 "
           "glue, U+FEFF/NBSP/U+3000 separate or trim, quoted branch tolerates "
           "newlines, LT-interrupted unquoted tails fail closed) — node-replay "
           "verified", failures, verbose)
    # The Windows fully-qualified predicate copilot home resolution leans on. Runs on every
    # host because _win_fully_qualified uses ntpath explicitly — so the UNC-root, device-
    # namespace, volume-GUID, and canonicality logic (the heart of the path findings) is
    # verified off-win32 too. splitdrive returns a bare \\?\C: (and \\?\C:.copilot) as a
    # complete "drive" with an empty tail on every supported Python, so these fixtures are
    # version-stable (asserted on 3.10–3.14). Windows skips path normalization ONLY after
    # an EXACT literal \\?\ prefix, so under \\?\ only an already-canonical literal prefix
    # qualifies (a rooted drive, a volume-GUID root, or the extended-UNC share root — each
    # with an optional single trailing separator); a noncanonical literal \\?\ (a '/' or a
    # '.'/'..'/internal-or-repeated-empty segment) and ANY nonliteral //?/ spelling fail
    # closed, because copilot's Node resolver canonicalizes/folds-to-literal (its open then
    # skips normalization) while the harness's spelling is Win32-normalized, diverging. A
    # \\.\ path is exempt — Windows normalizes it in both processes, so it reconverges —
    # except a BARE \\.\ (or \\?\) root or an INCOMPLETE extended-UNC root, which
    # 3.10/3.11 (bare) and 3.11 (trailing-sep UNC) join with a DOUBLED separator vs
    # Node's single one (PROVEN divergence there; on 3.12+ the joins coincide and the
    # rejection is CONSERVATIVE — one uniform version-stable rule): fail closed.
    # Ordinary UNC roots must be COMPLETE (server AND share): Node joins a bare \\ into
    # \mcp-config.json, resolved from the child drive's root — a real file copilot
    # loads — while the harness's join names nothing. A TERMINAL-COLON body end is
    # rejected everywhere, two cases under one test: a WHOLE-DRIVE colon root
    # (\\?\foo:, \\srv\share:) is a PROVEN glue — splitdrive returns it whole as a
    # drive ending in ":", so ntpath.join GLUES the child name on where Node inserts
    # the separator, and no normalization reconverges a colon glue — while a colon-
    # terminal DEVICE body past a rooted drive (\\?\C:\dir:) joins exactly like Node
    # on every version and is rejected CONSERVATIVELY (stream syntax, never a
    # directory). The ordinary-UNC arm only rejects the whole-drive form: a UNC tail
    # merely ending in ":" (\\srv\share\dir:) joins like Node and passes.
    _check("copilot.win_fully_qualified",
           # ACCEPT: drive-with-root, complete UNC shares, and safe device paths
           copilot_mod._win_fully_qualified("C:\\Users\\me")
           and copilot_mod._win_fully_qualified("\\\\server\\share")       # bare UNC ROOT
           and copilot_mod._win_fully_qualified("\\\\server\\share\\sub")
           and copilot_mod._win_fully_qualified("//server/share")
           and copilot_mod._win_fully_qualified("\\\\?\\C:\\Users\\me")    # rooted literal \\?\
           and copilot_mod._win_fully_qualified("\\\\?\\C:\\")             # drive root + trailing sep
           and copilot_mod._win_fully_qualified("\\\\?\\UNC\\srv\\share")  # extended UNC root
           and copilot_mod._win_fully_qualified("\\\\?\\UNC\\srv\\share\\")
           and copilot_mod._win_fully_qualified("\\\\?\\UNC\\srv\\share\\sub")
           and copilot_mod._win_fully_qualified("\\\\.\\UNC\\srv\\share")
           and copilot_mod._win_fully_qualified("\\\\?\\unc\\srv\\share")  # case-insensitive
           and copilot_mod._win_fully_qualified(                          # volume-GUID root
               "\\\\?\\Volume{12345678-1234-1234-1234-123456789abc}")
           and copilot_mod._win_fully_qualified(                          # + trailing sep
               "\\\\?\\Volume{12345678-1234-1234-1234-123456789abc}\\")
           and copilot_mod._win_fully_qualified(
               "\\\\?\\Volume{12345678-1234-1234-1234-123456789abc}\\sub")
           and copilot_mod._win_fully_qualified("\\\\.\\C:\\a\\..\\b")     # \\.\ normalizes -> converges
           and copilot_mod._win_fully_qualified("\\\\.\\C:/real")         # \\.\ fwd slash -> converges
           # colon segments with a ROOTED tail — and an ordinary-UNC tail merely
           # ENDING in ":" — join like Node on every version
           and copilot_mod._win_fully_qualified("\\\\srv\\share:\\sub")
           and copilot_mod._win_fully_qualified("\\\\?\\foo:\\x")
           and copilot_mod._win_fully_qualified("\\\\srv\\share\\dir:")
           # REJECT: unrooted device drives (both namespaces), noncanonical literal \\?\,
           # and any nonliteral //?/ spelling
           and not copilot_mod._win_fully_qualified("\\\\?\\C:")           # bare device drive
           and not copilot_mod._win_fully_qualified("\\\\.\\C:")
           and not copilot_mod._win_fully_qualified("//?/C:")
           # bare namespace roots and incomplete extended-UNC roots: 3.10/3.11 join the
           # bare forms — and 3.11 the trailing-sep UNC forms — with a DOUBLED separator
           # (\\?\\mcp-config.json) where Node's single-separator join names a local
           # DOS-device alias / share the harness never enumerates; no config can live
           # at these roots on any version
           and not copilot_mod._win_fully_qualified("\\\\?\\")
           and not copilot_mod._win_fully_qualified("\\\\.\\")
           and not copilot_mod._win_fully_qualified("//?/")
           and not copilot_mod._win_fully_qualified("\\\\?\\UNC")
           and not copilot_mod._win_fully_qualified("\\\\?\\UNC\\")
           and not copilot_mod._win_fully_qualified("\\\\?\\UNC\\srv")
           and not copilot_mod._win_fully_qualified("\\\\?\\UNC\\srv\\")
           # incomplete ORDINARY UNC roots: no volume exists without server AND share —
           # Node joins a bare \\ into \mcp-config.json, resolved from the CHILD's
           # current-drive root (a real file copilot loads), while the harness's
           # three-separator/incomplete-UNC join names nothing; \\srv\ joins with a
           # doubled separator on 3.10/3.11
           and not copilot_mod._win_fully_qualified("\\\\")
           and not copilot_mod._win_fully_qualified("\\\\srv")
           and not copilot_mod._win_fully_qualified("\\\\srv\\")
           and not copilot_mod._win_fully_qualified("//")
           and not copilot_mod._win_fully_qualified("//srv/")
           # whole-drive terminal-colon roots: splitdrive returns the root as a drive
           # ending in ":", so ntpath.join GLUES the child name on
           # (\\?\foo:mcp-config.json) where Node inserts the separator — PROVEN
           # divergence, in the device namespaces AND ordinary UNC
           and not copilot_mod._win_fully_qualified("\\\\?\\foo:")
           and not copilot_mod._win_fully_qualified("\\\\.\\foo:")
           and not copilot_mod._win_fully_qualified("\\\\?\\UNC\\srv\\share:")
           and not copilot_mod._win_fully_qualified("\\\\srv\\share:")
           # ...while a colon-terminal DEVICE body past a rooted drive joins exactly
           # like Node on every version (verified 3.10–3.14) and is rejected
           # CONSERVATIVELY — a terminal-colon component is NTFS stream syntax,
           # never a directory
           and not copilot_mod._win_fully_qualified("\\\\?\\C:\\dir:")
           and not copilot_mod._win_fully_qualified("\\\\?\\C:.copilot")   # drive-relative device
           and not copilot_mod._win_fully_qualified("\\\\?\\C:\\a\\..\\b") # literal \\?\ + '..'
           and not copilot_mod._win_fully_qualified("\\\\?\\C:/real")      # literal \\?\ + '/'
           and not copilot_mod._win_fully_qualified("\\\\?\\UNC\\srv/share")
           and not copilot_mod._win_fully_qualified("\\\\?\\C:\\a\\.")     # literal \\?\ + '.'
           and not copilot_mod._win_fully_qualified("\\\\?\\C:\\a\\\\b")   # literal \\?\ internal dup sep
           and not copilot_mod._win_fully_qualified("\\\\?\\C:\\x\\\\")    # repeated trailing empty
           and not copilot_mod._win_fully_qualified("//?/C:/real")        # nonliteral //?/
           and not copilot_mod._win_fully_qualified("//?/C:/base./real")  # nonliteral //?/ + trailing period
           and not copilot_mod._win_fully_qualified("//?/UNC/srv/share")  # nonliteral //?/
           and not copilot_mod._win_fully_qualified("\\rooted")            # driveless-rooted
           and not copilot_mod._win_fully_qualified("C:")                  # bare drive
           and not copilot_mod._win_fully_qualified("C:x")                 # drive-relative
           and not copilot_mod._win_fully_qualified("relpath"),
           "win32 fully-qualified predicate: lettered-drive-with-root, COMPLETE UNC "
           "shares (incl. the bare share root ntpath.isabs mis-reports on 3.10, fixed in "
           "3.11), canonical literal \\\\?\\ device paths (rooted drive, volume-GUID root, "
           "COMPLETE extended-UNC share — each with an optional single trailing "
           "separator), and \\\\.\\ paths outside the shared rejections (Windows "
           "normalizes them in both processes) qualify; unrooted device drives in either "
           "namespace, bare namespace roots and incomplete extended-UNC roots (their "
           "joins can DOUBLE the separator vs Node's), incomplete ordinary UNC roots "
           "(\\\\, \\\\srv, \\\\srv\\ — Node resolves a bare \\\\ from the child drive's "
           "root while the harness's join names nothing), terminal-colon body ends "
           "(whole-drive roots GLUE in ntpath.join where Node inserts the separator — "
           "proven; a colon-terminal device body past a rooted drive, \\\\?\\C:\\dir:, "
           "joins like Node and is rejected conservatively as stream syntax), "
           "noncanonical literal \\\\?\\ paths (a '/' "
           "or a '.'/'..'/internal-or-repeated-empty segment), and every nonliteral //?/ "
           "spelling (copilot folds it to a literal \\\\?\\ that skips normalization while "
           "the harness's stays Win32-normalized) fail closed",
           failures, verbose)

    # copilot's user config home is $COPILOT_HOME, else Node os.homedir() — %USERPROFILE%
    # on Windows, $HOME elsewhere. A stray HOME on win32 must NOT redirect it. A RELATIVE
    # home resolves against copilot's own process cwd (verified 1.0.64), i.e. the CHILD's
    # cwd — resolving against the harness's cwd would enumerate a different config than
    # the run loads. POSIX: a SET-BUT-EMPTY $HOME is preserved, not treated as unset —
    # Node's homedir() returns "" for it, so copilot reads <its cwd>/.copilot (verified
    # 1.0.64); an empty COPILOT_HOME IS unset to copilot everywhere (verified 1.0.64, falls
    # back to $HOME/.copilot). win32 (via _win_fully_qualified): a rooted-but-driveless home
    # takes the child cwd's drive (where copilot resolves it), a complete UNC root is
    # accepted as-is, and both a drive-relative home (bare "D:"/"C:x" — per-drive cwd
    # unknowable) and an absent or present-but-<3-char %USERPROFILE% fail closed —
    # libuv resolves only an ABSENT one via GetUserProfileDirectoryW (the real profile
    # dir, unnameable from the env); a present short/empty one is a UV_ENOENT ERROR
    # that Node's homedir() surfaces as a throw. Absolute fixtures are
    # drive-qualified so they are fully absolute on win32 too.
    _win = sys.platform == "win32"
    _dq = "C:" if _win else ""

    def _cop_home_raises(env, cwd):
        try:
            copilot_mod._copilot_home(env, cwd)
        except RuntimeError:
            return True
        return False

    _check("copilot.home_resolution",
           copilot_mod._copilot_home({"COPILOT_HOME": _dq + "/custom"}) == _dq + "/custom"
           and copilot_mod._copilot_home(
               {"USERPROFILE": _dq + "/u/prof", "HOME": _dq + "/u/home"})
               == os.path.join(_dq + ("/u/prof" if _win else "/u/home"), ".copilot")
           and copilot_mod._copilot_home({"COPILOT_HOME": "rel-home"}, _dq + "/child/ws")
               == os.path.normpath(os.path.join(_dq + "/child/ws", "rel-home"))
           and copilot_mod._copilot_home({"USERPROFILE": "rel-base", "HOME": "rel-base"},
                                         _dq + "/child/ws")
               == os.path.normpath(os.path.join(_dq + "/child/ws", "rel-base", ".copilot"))
           and copilot_mod._copilot_home(
               {"COPILOT_HOME": "", "USERPROFILE": _dq + "/u/prof",
                "HOME": _dq + "/u/home"})
               == os.path.join(_dq + ("/u/prof" if _win else "/u/home"), ".copilot")
           # POSIX empty-$HOME is preserved (→ <cwd>/.copilot); win32 handles empty
           # %USERPROFILE% by failing closed, asserted in the win32 block below.
           and (_win or copilot_mod._copilot_home({"HOME": ""}, "/child/ws")
                        == os.path.normpath(os.path.join("/child/ws", ".copilot")))
           and (not _win or (
               copilot_mod._copilot_home({"COPILOT_HOME": "\\rooted"}, "D:\\child\\ws")
                   == "D:\\rooted"
               # complete UNC root accepted as-is (not anchored, not rejected)
               and copilot_mod._copilot_home({"COPILOT_HOME": "\\\\srv\\share"},
                                             "D:\\child\\ws") == "\\\\srv\\share"
               # rooted device-namespace home accepted; a BARE device drive fails closed
               # (its joins drop the separator — \\?\C:mcp-config.json is not a path
               # copilot reads), from COPILOT_HOME and via a USERPROFILE join alike
               and copilot_mod._copilot_home({"COPILOT_HOME": "\\\\?\\C:\\real"},
                                             "D:\\child\\ws") == "\\\\?\\C:\\real"
               # a single trailing separator is join-stable, and a \\.\ device path (which
               # Windows normalizes in both processes) is accepted even when noncanonical
               and copilot_mod._copilot_home({"COPILOT_HOME": "\\\\?\\C:\\"},
                                             "D:\\child\\ws") == "\\\\?\\C:\\"
               and copilot_mod._copilot_home({"COPILOT_HOME": "\\\\.\\C:\\a\\..\\b"},
                                             "D:\\child\\ws") == "\\\\.\\C:\\a\\..\\b"
               # a complete EXTENDED UNC share root is accepted as-is on every Python
               # (3.11+ splitdrive returns it whole with an empty tail; joins match Node)
               and copilot_mod._copilot_home({"COPILOT_HOME": "\\\\?\\UNC\\srv\\share"},
                                             "D:\\child\\ws") == "\\\\?\\UNC\\srv\\share"
               # a volume-GUID root is accepted as-is (its join inserts the separator,
               # matching Node — verified 3.10–3.14)
               and copilot_mod._copilot_home(
                   {"COPILOT_HOME":
                    "\\\\?\\Volume{12345678-1234-1234-1234-123456789abc}"},
                   "D:\\child\\ws")
                   == "\\\\?\\Volume{12345678-1234-1234-1234-123456789abc}"
               and _cop_home_raises({"COPILOT_HOME": "\\\\?\\C:"}, "D:\\child\\ws")
               and _cop_home_raises({"USERPROFILE": "\\\\?\\C:"}, "D:\\child\\ws")
               # a bare namespace root and an incomplete extended-UNC root fail closed
               # (3.10/3.11 join them with a doubled separator vs Node's single one)
               and _cop_home_raises({"COPILOT_HOME": "\\\\?\\"}, "D:\\child\\ws")
               and _cop_home_raises({"COPILOT_HOME": "\\\\?\\UNC\\srv\\"},
                                    "D:\\child\\ws")
               # incomplete ORDINARY UNC and terminal-colon roots fail closed too
               # (Node resolves a bare \\ from the child drive's root; a colon root
               # glues in ntpath.join where Node inserts the separator)
               and _cop_home_raises({"COPILOT_HOME": "\\\\"}, "D:\\child\\ws")
               and _cop_home_raises({"COPILOT_HOME": "\\\\srv\\"}, "D:\\child\\ws")
               and _cop_home_raises({"COPILOT_HOME": "\\\\?\\foo:"}, "D:\\child\\ws")
               # a drive-relative device form, a NONCANONICAL literal \\?\ home, and any
               # nonliteral //?/ spelling fail closed — copilot's Node resolver
               # canonicalizes/folds-to-literal (its open then skips normalization) while
               # the harness's spelling is Win32-normalized, so the two would diverge
               and _cop_home_raises({"COPILOT_HOME": "\\\\?\\C:.copilot"},
                                    "D:\\child\\ws")
               and _cop_home_raises({"COPILOT_HOME": "\\\\?\\C:\\a\\..\\b"},
                                    "D:\\child\\ws")
               and _cop_home_raises({"COPILOT_HOME": "\\\\?\\C:/real"}, "D:\\child\\ws")
               and _cop_home_raises({"COPILOT_HOME": "//?/C:/real"}, "D:\\child\\ws")
               # drive-relative homes fail closed — different drive AND same drive as cwd
               and _cop_home_raises({"COPILOT_HOME": "C:drive-rel"}, "D:\\child\\ws")
               and _cop_home_raises({"COPILOT_HOME": "D:"}, "D:\\child\\ws")
               and _cop_home_raises({"COPILOT_HOME": "D:sub"}, "D:\\child\\ws")
               # absent %USERPROFILE% (libuv → GetUserProfileDirectoryW) and a present
               # but <3-char one (libuv errors, Node homedir() throws) both fail closed
               and _cop_home_raises({}, "D:\\child\\ws")
               and _cop_home_raises({"USERPROFILE": ""}, "D:\\child\\ws")
               and _cop_home_raises({"USERPROFILE": "C:"}, "D:\\child\\ws")
               # the RAW %USERPROFILE% is vetted BEFORE the .copilot join: a value
               # splitdrive returns whole as a drive ending in ":" would GLUE the
               # join (\\?\foo:.copilot vs Node's \\?\foo:\.copilot) — fail closed
               and _cop_home_raises({"USERPROFILE": "\\\\?\\foo:"}, "D:\\child\\ws")
               and _cop_home_raises({"USERPROFILE": "\\\\.\\foo:"}, "D:\\child\\ws")
               and _cop_home_raises({"USERPROFILE": "\\\\srv\\share:"},
                                    "D:\\child\\ws")
               # \\?\UNC\srv\share: splits whole (→ glue → raise) on 3.11+ only;
               # 3.10 keeps a rooted tail and INSERTS the separator exactly like
               # Node — the byte-identical join is accepted there
               and (_cop_home_raises({"USERPROFILE": "\\\\?\\UNC\\srv\\share:"},
                                     "D:\\child\\ws")
                    if sys.version_info >= (3, 11) else
                    copilot_mod._copilot_home(
                        {"USERPROFILE": "\\\\?\\UNC\\srv\\share:"}, "D:\\child\\ws")
                    == "\\\\?\\UNC\\srv\\share:\\.copilot")
               and copilot_mod._copilot_home({"USERPROFILE": "C:\\Users\\me"},
                                             "D:\\child\\ws")
                   == os.path.join("C:\\Users\\me", ".copilot"))),
           "COPILOT_HOME wins (empty = unset, as copilot treats it); otherwise "
           "USERPROFILE on win32, HOME elsewhere; a POSIX empty HOME resolves to the "
           "child cwd, relative homes resolve against the child's cwd as copilot does; "
           "win32 driveless-rooted homes take the child cwd's drive, complete UNC roots "
           "(incl. the extended \\\\?\\UNC form), canonical literal \\\\?\\ device paths "
           "(rooted, volume-GUID, one trailing sep ok), and \\\\.\\ paths outside the "
           "shared rejections are accepted, and drive-relative homes, bare/drive-"
           "relative device drives, bare or incomplete namespace and ordinary-UNC "
           "roots, terminal-colon roots, noncanonical literal-\\\\?\\ paths, nonliteral "
           "//?/ spellings, plus absent/short USERPROFILE fail closed; a RAW "
           "USERPROFILE that is one whole colon-terminal drive is vetted before the "
           ".copilot join (it would GLUE where Node inserts the separator)",
           failures, verbose)

    # The win32 arm above only runs on Windows and there is no Windows CI — so the
    # explicit-COPILOT_HOME device/UNC fixtures are ALSO exercised on every host by
    # shimming the module-local `sys` (platform reads "win32", everything else
    # delegates — same technique as the ODR extension check). Only these fixtures are
    # portable: an ACCEPTED explicit home returns early through the pure-ntpath
    # predicate and a REJECTED one raises on ntpath tests, neither touching os.path
    # (posixpath on this host); the absent/short-USERPROFILE raises and the raw-
    # USERPROFILE whole-drive-colon vet (pure ntpath on the raw value) also fire
    # BEFORE any join. The USERPROFILE .copilot join itself and cwd anchoring DO go
    # through os.path, so those assertions stay win32-only above.
    _real_sys_home = copilot_mod.sys

    class _Win32SysHome:
        platform = "win32"

        def __getattr__(self, _n):
            return getattr(_real_sys_home, _n)

    def _shim_accepts(home):
        return copilot_mod._copilot_home({"COPILOT_HOME": home}, "D:\\child\\ws") == home

    copilot_mod.sys = _Win32SysHome()
    try:
        home_shim_ok = (
            _shim_accepts("\\\\srv\\share")
            and _shim_accepts("\\\\?\\C:\\real")
            and _shim_accepts("\\\\?\\C:\\")
            and _shim_accepts("\\\\.\\C:\\a\\..\\b")
            and _shim_accepts("\\\\?\\UNC\\srv\\share")
            and _shim_accepts("\\\\?\\Volume{12345678-1234-1234-1234-123456789abc}")
            # drive-relative and unrooted/noncanonical/nonliteral device forms
            and _cop_home_raises({"COPILOT_HOME": "\\\\?\\C:"}, "D:\\child\\ws")
            and _cop_home_raises({"COPILOT_HOME": "\\\\?\\C:.copilot"}, "D:\\child\\ws")
            and _cop_home_raises({"COPILOT_HOME": "\\\\?\\C:\\a\\..\\b"}, "D:\\child\\ws")
            and _cop_home_raises({"COPILOT_HOME": "\\\\?\\C:/real"}, "D:\\child\\ws")
            and _cop_home_raises({"COPILOT_HOME": "//?/C:/real"}, "D:\\child\\ws")
            and _cop_home_raises({"COPILOT_HOME": "C:drive-rel"}, "D:\\child\\ws")
            and _cop_home_raises({"COPILOT_HOME": "D:"}, "D:\\child\\ws")
            and _cop_home_raises({"COPILOT_HOME": "D:sub"}, "D:\\child\\ws")
            # bare/incomplete device-namespace roots
            and _cop_home_raises({"COPILOT_HOME": "\\\\?\\"}, "D:\\child\\ws")
            and _cop_home_raises({"COPILOT_HOME": "\\\\.\\"}, "D:\\child\\ws")
            and _cop_home_raises({"COPILOT_HOME": "//?/"}, "D:\\child\\ws")
            and _cop_home_raises({"COPILOT_HOME": "\\\\?\\UNC"}, "D:\\child\\ws")
            and _cop_home_raises({"COPILOT_HOME": "\\\\?\\UNC\\"}, "D:\\child\\ws")
            and _cop_home_raises({"COPILOT_HOME": "\\\\?\\UNC\\srv"}, "D:\\child\\ws")
            and _cop_home_raises({"COPILOT_HOME": "\\\\?\\UNC\\srv\\"}, "D:\\child\\ws")
            # bare/incomplete ORDINARY UNC roots — Node joins a bare \\ into
            # \mcp-config.json, resolved from the CHILD drive's root, while the
            # harness's join names nothing (on 3.10 splitdrive returns an EMPTY
            # drive for \\ and \\srv; the two-separator prefix test still rejects)
            and _cop_home_raises({"COPILOT_HOME": "\\\\"}, "D:\\child\\ws")
            and _cop_home_raises({"COPILOT_HOME": "\\\\srv"}, "D:\\child\\ws")
            and _cop_home_raises({"COPILOT_HOME": "\\\\srv\\"}, "D:\\child\\ws")
            and _cop_home_raises({"COPILOT_HOME": "//srv/"}, "D:\\child\\ws")
            # terminal-colon roots — ntpath.join GLUES where Node inserts the sep
            and _cop_home_raises({"COPILOT_HOME": "\\\\srv\\share:"}, "D:\\child\\ws")
            and _cop_home_raises({"COPILOT_HOME": "\\\\?\\foo:"}, "D:\\child\\ws")
            and _cop_home_raises({"COPILOT_HOME": "\\\\.\\foo:"}, "D:\\child\\ws")
            and _cop_home_raises({"COPILOT_HOME": "\\\\?\\UNC\\srv\\share:"},
                                 "D:\\child\\ws")
            # absent/short USERPROFILE raises fire before any os.path join
            and _cop_home_raises({}, "D:\\child\\ws")
            and _cop_home_raises({"USERPROFILE": ""}, "D:\\child\\ws")
            and _cop_home_raises({"USERPROFILE": "C:"}, "D:\\child\\ws")
            # ...and so does the raw-USERPROFILE whole-drive-colon vet: these would
            # GLUE the .copilot join (\\?\foo:.copilot vs Node's \\?\foo:\.copilot)
            and _cop_home_raises({"USERPROFILE": "\\\\?\\foo:"}, "D:\\child\\ws")
            and _cop_home_raises({"USERPROFILE": "\\\\.\\foo:"}, "D:\\child\\ws")
            and _cop_home_raises({"USERPROFILE": "\\\\srv\\share:"}, "D:\\child\\ws")
            # 3.11+ splitdrive absorbs \\?\UNC\srv\share: whole → pre-join raise
            # (portable); 3.10 keeps a rooted tail and INSERTS like Node — that
            # accept runs through os.path.join, so it stays win32-only above
            and (sys.version_info < (3, 11)
                 or _cop_home_raises({"USERPROFILE": "\\\\?\\UNC\\srv\\share:"},
                                     "D:\\child\\ws"))
        )
    finally:
        copilot_mod.sys = _real_sys_home
    _check("copilot.home_device_paths_cross_host", home_shim_ok,
           "the win32 _copilot_home accept/reject decisions for explicit device, UNC, "
           "and drive-relative COPILOT_HOME values (pure-ntpath paths that never touch "
           "os.path) hold on every host via the sys shim — incl. bare/incomplete "
           "namespace and ordinary-UNC roots, terminal-colon roots, absent/short "
           "USERPROFILE, and raw whole-drive-colon USERPROFILE bases (vetted before "
           "the .copilot join they would GLUE) failing closed", failures, verbose)

    _check("copilot.odr_gate_off_non_win32",
           sys.platform == "win32" or copilot_mod._odr_registry_command() is None,
           "off Windows the ODR gate reports no command (no registry to read)",
           failures, verbose)

    # The ODR registry read must pin the 64-BIT view: copilot 1.0.64's native helper
    # opens the key with KEY_READ | KEY_WOW64_64KEY, while a 32-bit process's DEFAULT
    # view is the redirected WOW6432Node one — there the key can be absent (the gate
    # would read "off") while copilot's 64-bit view is populated. No Windows CI, so
    # `winreg` is stubbed into sys.modules (the function imports it lazily) and the
    # module-local `sys` shimmed to win32; the stub records the access mask OpenKey
    # receives and drives the blank-value/absent/denied arms too.
    _fake_winreg = type(sys)("winreg")   # a real module object, no importlib involved
    _fake_winreg.HKEY_LOCAL_MACHINE = object()
    _fake_winreg.KEY_READ = 0x20019          # the real winreg constants
    _fake_winreg.KEY_WOW64_64KEY = 0x0100
    _fake_winreg._value = "  C:\\odr\\host.exe mcp-src  "
    _fake_winreg._raise = None
    _reg_calls = []

    class _FakeRegKey:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def _fake_openkey(root, sub, reserved=0, access=None):
        _reg_calls.append((root, sub, reserved, access))
        if _fake_winreg._raise is not None:
            raise _fake_winreg._raise
        return _FakeRegKey()

    _fake_winreg.OpenKey = _fake_openkey
    _fake_winreg.QueryValueEx = lambda _key, _name: (_fake_winreg._value, 1)
    _real_sys_reg = copilot_mod.sys

    class _Win32SysReg:
        platform = "win32"

        def __getattr__(self, _n):
            return getattr(_real_sys_reg, _n)

    _saved_winreg = sys.modules.get("winreg")
    sys.modules["winreg"] = _fake_winreg
    copilot_mod.sys = _Win32SysReg()
    try:
        reg_cmd = copilot_mod._odr_registry_command()          # value → stripped
        _fake_winreg._value = "   "
        reg_blank = copilot_mod._odr_registry_command()        # blank value → gate off
        # the value is trimmed with ECMAScript trim(), matching the bundle's
        # `value?.trim()` falsy test: a lone U+FEFF (JS-space, not Python-space)
        # reads gate-OFF exactly as it does for copilot, and a BOM-wrapped command
        # trims clean
        _fake_winreg._value = "\ufeff"
        reg_bom_blank = copilot_mod._odr_registry_command()    # JS-blank → gate off
        _fake_winreg._value = "\ufeff C:\\odr\\host.exe mcp-src \ufeff"
        reg_bom_cmd = copilot_mod._odr_registry_command()      # BOM-wrapped → trimmed
        _fake_winreg._raise = FileNotFoundError("no key")
        reg_absent = copilot_mod._odr_registry_command()       # absent key → gate off
        _fake_winreg._raise = PermissionError("denied")
        try:
            copilot_mod._odr_registry_command()
            reg_denied = "did not raise"
        except RuntimeError as exc:
            reg_denied = str(exc)
    finally:
        copilot_mod.sys = _real_sys_reg
        if _saved_winreg is None:
            sys.modules.pop("winreg", None)
        else:
            sys.modules["winreg"] = _saved_winreg
    _check("copilot.odr_registry_64bit_view",
           reg_cmd == "C:\\odr\\host.exe mcp-src"
           and reg_blank is None and reg_bom_blank is None
           and reg_bom_cmd == "C:\\odr\\host.exe mcp-src"
           and reg_absent is None
           and "failing closed" in reg_denied
           and len(_reg_calls) == 6
           and all(root is _fake_winreg.HKEY_LOCAL_MACHINE
                   and sub == copilot_mod._ODR_REGISTRY_SUBKEY
                   and reserved == 0
                   and access == (_fake_winreg.KEY_READ
                                  | _fake_winreg.KEY_WOW64_64KEY)
                   for root, sub, reserved, access in _reg_calls),
           "the ODR registry key is opened with copilot 1.0.64's exact access mask — "
           "KEY_READ | KEY_WOW64_64KEY, the 64-bit view from any harness bitness, "
           "never the WOW6432Node default a 32-bit process would read (where an "
           "absent key would fake the gate 'off'); a missing key or blank/JS-blank "
           "(U+FEFF) value still reads as gate-off, a BOM-wrapped command JS-trims "
           "clean, and an unreadable key fails closed (stubbed winreg + sys shim)",
           failures, verbose)

    # A 32-bit harness process on win32 can't prove MCP hermeticity at all — the WOW64
    # file-system redirector remaps System32 (the ODR command launch included) to
    # SysWOW64, so what it reads and launches is not what 64-bit copilot sees. The
    # gate must fire FIRST: env={} would raise the absent-USERPROFILE error later if
    # it were mis-ordered. Cross-host via a shim whose maxsize reads 32-bit.
    _real_sys32 = copilot_mod.sys

    class _Win32Sys32Bit:
        platform = "win32"
        maxsize = 2**31 - 1

        def __getattr__(self, _n):
            return getattr(_real_sys32, _n)

    copilot_mod.sys = _Win32Sys32Bit()
    try:
        get_adapter("copilot")._mcp_disable_args("D:\\child\\ws", env={})
        bits_raise = "did not raise"
    except RuntimeError as exc:
        bits_raise = str(exc)
    finally:
        copilot_mod.sys = _real_sys32
    _check("copilot.win32_32bit_harness_fails_closed",
           "32-bit" in bits_raise and "WOW64" in bits_raise
           and "failing closed" in bits_raise,
           "on win32 a 32-bit harness fails closed before enumerating anything — "
           "WOW64 redirection gives it different filesystem (System32 → SysWOW64) "
           "and default-registry views than copilot's 64-bit process (bitness shim, "
           "cross-host; the raise precedes even the USERPROFILE checks)",
           failures, verbose)

    _odr_dir = _tmp.mkdtemp(prefix="ase-odr-")
    _orig_odr_cmd = copilot_mod._odr_registry_command
    try:
        listing = os.path.join(_odr_dir, "listing.py")
        with open(listing, "w") as fh:
            fh.write("import sys\n"
                     "assert sys.argv[1:] == ['mcp', 'list'], sys.argv\n"
                     "print('some banner line')\n"  # copilot tolerates non-JSON prefixes
                     "print('{\"servers\": [{\"name\": \"reg srv\"},"
                     " {\"name\": \"plain\"}]}')\n")
        copilot_mod._odr_registry_command = lambda: f'"{sys.executable}" "{listing}"'
        odr_names = get_adapter("copilot")._odr_mcp_server_names()
        odr_disabled_ok = odr_names == ["reg_srv", "plain"]
        if not _cop_wow64:   # _mcp_disable_args itself fails closed on real 32-bit win32
            empty_home = _tmp.mkdtemp(prefix="ase-odr-home-")
            try:
                # A full os.environ copy: the mocked listing subprocess needs a real
                # process environment (win32 python won't start without SystemRoot &
                # co), and COPILOT_HOME (absolute on every platform) pins the user
                # config to the empty fixture — a bare HOME would fail closed on
                # win32, where copilot ignores it and an absent USERPROFILE can't
                # name a home.
                oargv = get_adapter("copilot")._mcp_disable_args(
                    None, env={**os.environ, "COPILOT_HOME": empty_home})
                odis = [oargv[i + 1] for i, a in enumerate(oargv)
                        if a == "--disable-mcp-server"]
            finally:
                _sh.rmtree(empty_home, ignore_errors=True)
            odr_disabled_ok = odr_disabled_ok and odis == ["reg_srv", "plain"]
        _check("copilot.odr_servers_disabled", odr_disabled_ok,
               f"registry-advertised servers are enumerated via the ODR command and "
               f"disabled under their resolved names: {odr_names}", failures, verbose)

        # while the gate is ON, an enumeration failure must fail CLOSED — a run with
        # un-disabled registry servers is exactly what Phase 0 forbids.
        with open(listing, "w") as fh:
            fh.write("import sys; sys.exit(2)\n")
        try:
            get_adapter("copilot")._odr_mcp_server_names()
            odr_closed = False
        except RuntimeError as exc:
            odr_closed = "failing closed" in str(exc)
        with open(listing, "w") as fh:
            fh.write("print('{\"not_servers\": 1}')\n")
        try:
            get_adapter("copilot")._odr_mcp_server_names()
            odr_shape_closed = False
        except RuntimeError as exc:
            odr_shape_closed = "failing closed" in str(exc)
        _check("copilot.odr_failure_fails_closed",
               odr_closed and odr_shape_closed,
               "a failing ODR command or a drifted output shape raises (run fails "
               "closed) instead of reading as 'no servers'", failures, verbose)

        # ...and a NON-fully-qualified registry executable must fail closed too, WITHOUT
        # being launched: Python/CreateProcess resolves a bare name via the HARNESS's
        # application dir + ambient PATH (the env= block is not consulted), while
        # copilot's Node/libuv resolves it via the CHILD's PATH — two different binaries
        # could run, and the harness would disable the wrong listing's names.
        copilot_mod._odr_registry_command = lambda: 'odr.exe serve'
        try:
            get_adapter("copilot")._odr_mcp_server_names()
            odr_rel_closed = False
        except RuntimeError as exc:
            odr_rel_closed = ("fully-qualified" in str(exc)
                              and "failing closed" in str(exc))
        copilot_mod._odr_registry_command = lambda: '"odr host.exe" --flag'
        try:
            get_adapter("copilot")._odr_mcp_server_names()
            odr_relq_closed = False
        except RuntimeError as exc:
            odr_relq_closed = "fully-qualified" in str(exc)
        _check("copilot.odr_relative_command_fails_closed",
               odr_rel_closed and odr_relq_closed,
               "a bare/relative ODR executable (quoted or not) raises before launching — "
               "harness and copilot could resolve different binaries", failures, verbose)

        # ...and even a FULLY-QUALIFIED executable is rejected on win32 when it lacks an
        # extension: CreateProcess (the harness) runs the exact extensionless file, but
        # Node/libuv never tries an extensionless exact name — it probes <name>.com then
        # <name>.exe — so the two could run different binaries. The predicate mirrors
        # libuv's name_has_ext exactly (FIRST dot in the filename portion, nonempty
        # remainder — a leading dot counts, a dot only in a directory doesn't) and is
        # pure string logic, asserted on every host; the call-site raise is win32-gated
        # but exercised on every host below via a `sys` shim.
        ext_ok = (copilot_mod._win_exe_has_extension("C:\\bin\\odr.exe")
                  and copilot_mod._win_exe_has_extension("C:/bin/odr.exe")
                  and copilot_mod._win_exe_has_extension("C:\\bin\\odr.foo.exe")
                  and copilot_mod._win_exe_has_extension("C:\\bin\\.odr")
                  and copilot_mod._win_exe_has_extension("C:\\bin.d\\o.exe")
                  and not copilot_mod._win_exe_has_extension("C:\\bin\\odr")
                  and not copilot_mod._win_exe_has_extension("C:\\bin\\odr.")
                  and not copilot_mod._win_exe_has_extension("C:\\bin.d\\odr"))
        # The call-site raise is win32-gated, but there's no Windows CI — exercise it on
        # every host by shimming the module-local `sys` so `platform` reads "win32" (all
        # other attributes delegate to the real module). A FULLY-QUALIFIED but
        # extensionless executable (C:\bin\odr) must raise BEFORE any launch.
        _real_sys = copilot_mod.sys

        class _Win32Sys:
            platform = "win32"

            def __getattr__(self, _n):
                return getattr(_real_sys, _n)

        copilot_mod._odr_registry_command = lambda: 'C:\\bin\\odr serve'
        copilot_mod.sys = _Win32Sys()
        try:
            get_adapter("copilot")._odr_mcp_server_names()
            ext_raise = "did not raise"
        except RuntimeError as exc:
            ext_raise = str(exc)
        finally:
            copilot_mod.sys = _real_sys
        ext_ok = (ext_ok and "no extension" in ext_raise
                  and "failing closed" in ext_raise)
        _check("copilot.odr_extensionless_exe_fails_closed", ext_ok,
               "libuv's name_has_ext is mirrored exactly, and an extensionless (but "
               "fully-qualified) win32 ODR executable raises before launching — "
               "CreateProcess would run the exact file while Node/libuv runs "
               "<name>.com/<name>.exe (the win32 raise is exercised cross-host via a "
               "sys shim)",
               failures, verbose)

        copilot_mod._odr_registry_command = lambda: None
        _check("copilot.odr_gate_off_no_disables",
               get_adapter("copilot")._odr_mcp_server_names() == [],
               "no registry command → no ODR servers, nothing fails", failures, verbose)
    finally:
        copilot_mod._odr_registry_command = _orig_odr_cmd
        _sh.rmtree(_odr_dir, ignore_errors=True)

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
           skill_paths == [".antigravity/skills/skill-alpha/SKILL.md"],
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
    # agy has no MCP flags at all — masking its file-based discovery configs is its only
    # MCP kill-switch (design, Phase 0): the global mcp_config.json, plugins.json (which
    # can register EXTERNAL plugin dirs by absolute path), every registry plugin's own
    # mcp_config.json, and the workspace files it discovers via --add-dir. The workspace
    # channel spans FOUR customization roots (.agents/.agent/_agents/_agent — each
    # verified 1.1.1 with a sentinel server launched at session start) × four files:
    # the root's mcp_config.json, per-plugin plugins/*/mcp_config.json AND the
    # dot-inclusive plugins/.*/mcp_config.json companion (Python's glob excludes
    # dot-leading names from `*`, but agy discovers dot-prefixed plugin dirs too), and
    # plugins.json (registers external plugin dirs; verified 1.1.1 that a workspace
    # plugins.json entry's external mcp_config.json launches).
    _agy_ws_expected = {
        f"{root}/{rel}": content
        for root in (".agents", ".agent", "_agents", "_agent")
        for rel, content in (("mcp_config.json", '{"mcpServers": {}}'),
                             ("plugins/*/mcp_config.json", '{"mcpServers": {}}'),
                             ("plugins/.*/mcp_config.json", '{"mcpServers": {}}'),
                             ("plugins.json", "{}"))
    }
    _check("antigravity.mcp_config_mask_declared",
           get_adapter("antigravity").isolation_config_masks
           == {".gemini/config/mcp_config.json": '{"mcpServers": {}}',
               ".gemini/config/plugins.json": "{}"}
           and get_adapter("antigravity").plugin_registry_config_masks
           == {"mcp_config.json": '{"mcpServers": {}}'}
           and get_adapter("antigravity").workspace_config_masks == _agy_ws_expected,
           "antigravity masks global + plugins.json + per-plugin + all four workspace "
           "roots' MCP configs", failures, verbose)
    # probes must anchor --add-dir to the fresh private probe workspace (cwd), falling
    # back to a throwaway dir — NEVER the shared system temp root, where anyone's planted
    # .agents/mcp_config.json would load (agy scans the anchor's roots at session start;
    # /tmp is world-writable on Linux).
    import tempfile as _tf
    _agy_pargv = get_adapter("antigravity")._probe_argv("m", cwd="/priv/probe-ws")
    _agy_fallback = get_adapter("antigravity")._probe_argv("m")
    _fb_anchor = _agy_fallback[_agy_fallback.index("--add-dir") + 1]
    _check("antigravity.probe_private_workspace",
           _agy_pargv[_agy_pargv.index("--add-dir") + 1] == os.path.abspath("/priv/probe-ws")
           and _fb_anchor != _tf.gettempdir()
           and os.path.basename(_fb_anchor).startswith("ase-probe-agy-"),
           f"probe --add-dir anchors to the private probe cwd (fallback: fresh dir, "
           f"never the shared temp root): {_fb_anchor}", failures, verbose)
    _sh.rmtree(_fb_anchor, ignore_errors=True)

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
                        skills=["skill-alpha"],
                        assertions=[{"type": "skill_triggered", "skill": "skill-beta"}])
    vr = validate_spec(bad_spec, available_skills={"skill-alpha", "skill-beta"})
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
                          skills=["skill-alpha"],
                          assertions=[{"type": "skill_not_triggered", "skill": "skill-alpha"}])
    vr8 = validate_spec(warn_spec4)
    _check("validate.not_triggered_provisioned",
           vr8.ok and any("provisioned" in w for w in vr8.warnings),
           f"warning for skill_not_triggered on provisioned: {vr8.warnings}", failures, verbose)

    # warning: skill_not_triggered for unprovisioned skill (tautology)
    warn_spec5 = EvalSpec(name="t", prompt="p", source_path="/x/e.yaml",
                          skills=["skill-alpha"],
                          assertions=[{"type": "skill_not_triggered", "skill": "skill-beta"}])
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
                          skills=["skill-alpha", "skill-alpha"])
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

    # error (not warning): a missing seed file — the run would silently skip it and fail
    # confusingly downstream, so block before spending tokens
    seed_spec = EvalSpec(name="t", prompt="p", source_path="/x/e.yaml", skills=["s"],
                         files=["no_such_seed_file.json"])
    vr_seed = validate_spec(seed_spec)
    _check("validate.missing_seed_is_error",
           not vr_seed.ok and any("does not exist" in e for e in vr_seed.errors),
           f"missing seed file blocks the run: {vr_seed.errors}", failures, verbose)

    # rubric + explicit llm_judge: exactly ONE judge assertion (no double judge run/billing)
    dedup_spec = EvalSpec(name="t", prompt="p", source_path="/x/e.yaml",
                          rubric=["a"],
                          assertions=[{"type": "llm_judge", "threshold": 0.5}])
    n_judge = sum(1 for a in dedup_spec.effective_assertions()
                  if a.get("type") == "llm_judge")
    _check("spec.judge_not_doubled", n_judge == 1,
           f"explicit llm_judge suppresses the rubric-compiled one: {n_judge} judge "
           "assertion(s)", failures, verbose)
    rubric_only = EvalSpec(name="t", prompt="p", source_path="/x/e.yaml", rubric=["a"])
    n_judge2 = sum(1 for a in rubric_only.effective_assertions()
                   if a.get("type") == "llm_judge")
    _check("spec.rubric_still_compiled", n_judge2 == 1,
           f"rubric alone still compiles to one llm_judge: {n_judge2}", failures, verbose)

    # warning: top-level rubric shadowed by an explicit llm_judge with its OWN rubric
    shadow_rubric = EvalSpec(name="t", prompt="p", source_path="/x/e.yaml", skills=["s"],
                             rubric=["x"],
                             assertions=[{"type": "llm_judge", "rubric": ["y"]}])
    vr_shadow = validate_spec(shadow_rubric)
    _check("validate.rubric_shadowed_warned",
           vr_shadow.ok and any("ignored" in w for w in vr_shadow.warnings),
           f"warning when an explicit llm_judge rubric shadows the top-level one: "
           f"{vr_shadow.warnings}", failures, verbose)

    # a malformed `files:` entry (two-key dict) must be a clean load error, not a TypeError
    # later inside resolved_files()
    from .spec import _spec_from_raw
    try:
        _spec_from_raw({"prompt": "p", "files": [{"a": "b", "c": "d"}]}, "/x.yaml")
        files_ok = False
    except ValueError:
        files_ok = True
    _check("spec.malformed_files_entry", files_ok,
           "a two-key files mapping raises a clean ValueError at load", failures, verbose)

    # clean spec passes with no errors or warnings
    clean_spec = EvalSpec(name="t", prompt="Use {skill} to run", source_path="/x/e.yaml",
                          skills=["skill-alpha"],
                          assertions=[{"type": "skill_triggered", "skill": "skill-alpha"},
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
        from .spec import ModelTarget, load_scenario

        def _load_scen(payload):
            with _tmpmod.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as _f:
                _yaml.dump(payload, _f)
                _f.flush()
                return load_scenario(_f.name)

        _scen = _load_scen({"name": "multi", "prompt": "say hi",
                            "target": {"runner": "claude", "model": ["opus-4-8", "haiku-4-5"]},
                            "skills": []})
        _check("scenario.multi_model",
               _scen.targets == [ModelTarget("opus-4-8"), ModelTarget("haiku-4-5")],
               f"list model parsed: {_scen.targets}", failures, verbose)
        _scen = _load_scen({"name": "single", "prompt": "say hi",
                            "target": {"runner": "claude", "model": "opus-4-8"}, "skills": []})
        _check("scenario.single_model", _scen.targets == [ModelTarget("opus-4-8")],
               f"string model parsed: {_scen.targets}", failures, verbose)
        _scen = _load_scen({"name": "nomodel", "prompt": "say hi",
                            "target": {"runner": "claude"}, "skills": []})
        _check("scenario.no_model", _scen.targets == [ModelTarget()],
               f"omitted model → [ModelTarget()]: {_scen.targets}", failures, verbose)
        # per-model efforts (issue #67 follow-up): one run compares different efforts —
        # both the `id@effort` suffix and the mapping form pin an effort per column
        _scen = _load_scen({"name": "efforts", "prompt": "say hi",
                            "target": {"runner": "copilot",
                                       "model": ["claude-haiku-4.5@high",
                                                 {"model": "claude-opus-4.6",
                                                  "reasoning_effort": "low"}]},
                            "skills": []})
        _check("scenario.per_model_effort",
               _scen.targets == [ModelTarget("claude-haiku-4.5", "high"),
                                 ModelTarget("claude-opus-4.6", "low")],
               f"@suffix and mapping forms pin per-model efforts: {_scen.targets}",
               failures, verbose)
        try:
            _load_scen({"name": "bad", "prompt": "say hi",
                        "target": {"runner": "claude", "model": "haiku@hgih"}, "skills": []})
            bad_ok = False
        except ValueError:
            bad_ok = True
        _check("scenario.bad_effort_suffix_rejected", bad_ok,
               "a typo'd @effort suffix raises a clean ValueError at load", failures, verbose)
        # malformed list entries must be load errors, not silently dropped so the run spends
        # budget on the DEFAULT model instead of what the author (mis)wrote
        for bad_model, tag in (([{}], "empty_mapping"), ([None], "null_entry"),
                               ([""], "blank_entry")):
            try:
                _load_scen({"name": "bad", "prompt": "say hi",
                            "target": {"runner": "claude", "model": bad_model},
                            "skills": []})
                ok = False
            except ValueError:
                ok = True
            _check(f"scenario.{tag}_model_rejected", ok,
                   f"model: {bad_model!r} raises a clean ValueError instead of running "
                   "the default model", failures, verbose)
        _scen = _load_scen({"name": "emptylist", "prompt": "say hi",
                            "target": {"runner": "claude", "model": []}, "skills": []})
        _check("scenario.empty_model_list_is_default", _scen.targets == [ModelTarget()],
               f"an empty model list still means the default target: {_scen.targets}",
               failures, verbose)
        # deprecated pre-#67 `.models` view still answers with the model ids
        _check("scenario.models_compat_property", _scen.models == [None],
               f"Scenario.models keeps working for old callers: {_scen.models}",
               failures, verbose)

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
    # MCP hermeticity on the non-cell paths: masked-home overlay, probes, judge, fail-closed
    _check_mcp_hermetic_paths(failures, verbose)
    _check_parallel_cell_idx(failures, verbose)
    _check_cell_crash_safety(failures, verbose)

    # cli.py's pure helpers (YAML-error detection, --model/--all-models, models.yaml validation)
    _check_cli_helpers(failures, verbose)
    _check_progress_thread_safety(failures, verbose)

    # real pass/fail behavior for every deterministic assertion type
    _check_assertion_pass_fail(failures, verbose)

    # verdict coercion edge cases (string "false", extra items)
    _check_verdict_coercion(failures, verbose)

    # timeout kills the agent's whole process group, not just the direct child
    _check_exec_timeout_group_kill(failures, verbose)

    # report inlining is capped per file; the judge's skip behavior is unchanged
    _check_inline_truncation(failures, verbose)

    # model-rejection annotation only fires on actual rejections
    _check_model_error_heuristic(failures, verbose)

    # scenario run-knob overrides are type-checked at load
    _check_scenario_override_validation(failures, verbose)

    # typed reasoning-effort knob: spec validation, Runner threading, per-adapter mapping
    _check_reasoning_effort(failures, verbose)

    # deprecated pre-#67 module API (models= / .models / plain-id render columns)
    _check_api_compat(failures, verbose)

    print()
    if failures:
        print(f"SELFTEST FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("SELFTEST PASSED")
    return 0
