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


def _section(fn, failures, verbose):
    """Run one check section, containing a crash to that section.

    A section that RAISES used to take the whole selftest with it: no summary line, and
    every section after it silently never ran, so one crash hid an unknown number of real
    results and each fix-and-rerun cycle surfaced only the next one. Individual arms
    already have this property — a false condition is recorded and the run continues — and
    this extends it one level up.

    A crash is recorded as a FAILURE, never a skip. That direction is the whole point: the
    alternative turns today's loud abort into a quiet green, which is strictly worse than
    the problem being fixed. The traceback is printed rather than swallowed, because the
    reason this is safe to contain is that the diagnostic survives containment.

    Catches `Exception`, not `BaseException`, so KeyboardInterrupt and SystemExit still
    stop the run — a selftest that cannot be interrupted is its own defect.
    """
    name = fn.__name__.removeprefix("_check_")
    try:
        fn(failures, verbose)
    except Exception:                       # noqa: BLE001 — containment is the point
        import traceback
        failures.append(f"{name}.CRASHED")
        print(f"  [FAIL] {name}.CRASHED: the section raised instead of reporting, so its "
              f"remaining arms never ran and prove nothing. Sections after it still did.")
        print("".join("    " + ln for ln in traceback.format_exc().splitlines(True)))


def _flag_pair(argv, flag, value) -> bool:
    """True if argv contains ``flag`` immediately followed by ``value``."""
    return any(a == flag and i + 1 < len(argv) and argv[i + 1] == value
               for i, a in enumerate(argv))


def _cop_child_env(**over):
    """The child env for a copilot build_argv fixture. build_argv resolves everything it
    needs from this mapping and the filesystem — it spawns NO subprocess — so a fixture
    env is exactly the keys it pins, nothing ambient."""
    return dict(over)


# Monkeypatches installed on IMPORTED modules, newest last, as (module, attr, original).
# The suite patches adapter internals to drive checks that can't be driven any other way
# (scoping the agent scan to fixture roots, stubbing the Windows ODR registry reader), and
# those patches span hundreds of checks — far too wide to wrap each in its own try/finally
# without burying the checks in indentation. So each install registers its undo here and
# run_selftest restores whatever is left in ONE outer finally: a check that raises can no
# longer leave a stub bolted onto an adapter, which in an embedding process (the suite is
# importable, not just a CLI entry point) would silently weaken every later adapter call.
# The inline restores stay where the patch's real scope ends and deregister as they go, so
# on the happy path the net has nothing left to do.
_MODULE_PATCHES: list[tuple] = []


def _patch_module_attr(mod, name, value):
    """Set ``mod.name = value``, register the undo on _MODULE_PATCHES, and return the
    original for an inline restore."""
    orig = getattr(mod, name)
    _MODULE_PATCHES.append((mod, name, orig))
    setattr(mod, name, value)
    return orig


def _unpatch_module_attr(mod, name, orig):
    """Restore ``mod.name`` and drop its entry from the net (most recent first)."""
    setattr(mod, name, orig)
    for i in range(len(_MODULE_PATCHES) - 1, -1, -1):
        if _MODULE_PATCHES[i][0] is mod and _MODULE_PATCHES[i][1] == name:
            del _MODULE_PATCHES[i]
            break


def _restore_module_patches():
    """Undo every still-installed patch, newest first. A no-op once the inline restores
    have run; the safety net when an exception skipped them."""
    while _MODULE_PATCHES:
        mod, name, orig = _MODULE_PATCHES.pop()
        setattr(mod, name, orig)


def _cop_scope_agent_scan(roots):
    """Scope copilot's custom-agent FILE scan to ``roots``; returns the original function
    so the caller can restore it in a ``finally``. ``roots`` is read at CALL time, so a
    fixture created later can append its own root to the same list.

    Needed because the agent walk is UNBOUNDED: copilot stops it at its git root (or at
    the OS home only when the cwd is in no repo), and the harness refuses to learn which
    by executing git, so it visits every ancestor up to ``/``. A fixture under a shared
    ``$TMPDIR`` therefore walks through directories other people write to, where one
    stray ``.claude/agents/x.md`` would fail-close a build_argv call and crash the suite
    — deciding checks that are about enumeration, home resolution, or argv mapping and
    have nothing to do with agents.

    What is masked is what unrelated ancestors CONTAIN, never which ancestors get
    visited: the walk under test is untouched, and every check that asserts on agent
    discovery plants its files inside a registered root."""
    import os
    import agentskill_evals.adapters.copilot as _m
    orig = _m._agent_definition_files

    def scoped(d):
        rd = os.path.realpath(d)
        if any(rd == r or rd.startswith(r + os.sep) for r in roots):
            return orig(d)
        return []

    return _patch_module_attr(_m, "_agent_definition_files", scoped)


def _cop_restore_agent_scan(orig):
    """Undo _cop_scope_agent_scan."""
    import agentskill_evals.adapters.copilot as _m
    _unpatch_module_attr(_m, "_agent_definition_files", orig)


def _cop_private_fixture(prefix: str):
    """``(root, cwd, env)`` for copilot argv fixtures that must not depend on ANYTHING
    ambient:

    * ``cwd`` is a freshly created private directory,
    * ``HOME``/``USERPROFILE`` is its realpath-resolved private parent, so home
      resolution never falls back to this machine's real home,
    * ``COPILOT_HOME`` is a private config home carrying only the
      ``customAgents.defaultLocalOnly`` opt-out the isolation sanitizer injects, so the
      remote-agents gate is satisfied without reading this machine's real ~/.copilot
      (the opt-out is required for EVERY run, not just an in-repo one).

    Pinning the home does NOT bound the custom-agent walk — nothing does; see
    _cop_scope_agent_scan, which is what keeps a stray ``$TMPDIR/.claude/agents`` from
    deciding these fixtures."""
    import os
    import tempfile as _t
    root = os.path.realpath(_t.mkdtemp(prefix=prefix))
    cwd = os.path.join(root, "ws")
    chome = os.path.join(root, "copilot-home")
    os.makedirs(cwd)
    os.makedirs(chome)
    with open(os.path.join(chome, "config.json"), "w") as fh:
        fh.write('{"customAgents": {"defaultLocalOnly": true}}')
    env = _cop_child_env(COPILOT_HOME=chome, HOME=root, USERPROFILE=root)
    return root, cwd, env


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
    # This stand-in invokes no CLI and reads no configuration, so it has nothing for
    # concurrent cells to share — which is exactly what the flag asserts. Set so the
    # parallel-dispatch tests (cell_idx assignment) can still exercise --jobs>1; the real
    # adapters all leave it False because their config homes ARE shared (see base.Adapter).
    parallel_safe_config = True


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
    # Defined out here because the finally below restores them; an arm that raises early
    # must not turn its own cleanup into a NameError.
    real_home_env = os.environ.get("HOME")
    contained_home = tempfile.mkdtemp(prefix="ase-fakehome-")
    seen: dict = {}
    orig_execute = runner_mod.execute

    def _try(fn, default=None):
        """Run fn, turning any exception into `default` — arms below assert on VALUES, and a
        mutation that makes production code raise would otherwise abort the section."""
        try:
            return fn()
        except Exception:
            return default

    def _own_tempdir(path):
        """The exec tempdir, but only if it still looks like one.

        These arms chmod and rmtree what `_run_cell` handed the agent. A defect that makes
        exec_ws the tempdir ROOT turns `dirname(cwd)` into the SYSTEM temp directory, and
        this cleanup would then lock or delete it — mutation testing found that by wiping
        its own working tree. A test's cleanup must never be able to reach further than the
        thing it created."""
        parent = os.path.dirname(path or "")
        return parent if os.path.basename(path or "") == "workspace" and (
            os.path.basename(parent).startswith("ase-ws-")) else None

    def _fake_execute(adapter, prompt, opts, *, cwd, timeout, env_overrides, agent_name, eval_name):
        seen["cwd"] = cwd
        seen["scratch"] = opts.mcp_scratch_dir
        seen["home"] = opts.home
        # the isolated HOME is deleted right after execute() returns, so the mask must be
        # inspected here, while the agent would actually see it.
        if opts.home:
            mask = os.path.join(opts.home, ".fakecli", "mcp.json")
            if os.path.isfile(mask) and not os.path.islink(mask):
                seen["mask_content"] = open(mask).read()
            # $HOME is WRITABLE by the child, so the harness knows this directory's initial
            # contents and not its final ones. Every real agent does this — caches, session
            # state — and any of it can be a copy of what the agent was handed.
            with open(os.path.join(opts.home, "agent-cache.txt"), "w") as f:
                f.write("tok sk-secret-abcdef\n")
        with open(os.path.join(cwd, "run.py"), "w") as f:
            f.write("print('hi')\n")
        # A cwd is a place the agent can WRITE, so `..` is part of the attack surface, not
        # just `list_dir`'s. Written every time: the arms below assert where it landed.
        with open(os.path.join(cwd, "..", "above-cwd.txt"), "w") as f:
            f.write("tok sk-secret-abcdef\n")
        if seen.get("make_special"):
            # A special file the scrub cannot read and therefore cannot certify: it gets
            # QUARANTINED, which is a deletion the cell has to report.
            #
            # A socket rather than a FIFO, deliberately. Both are special files and both
            # take the same `_give_up` branch, but `open()` on a FIFO BLOCKS while `open()`
            # on a socket fails ENXIO at once — and this arm drives `_run_cell` on the main
            # thread. Mutation testing found that the hard way: the mutation that makes the
            # scrub treat every non-directory as a readable regular file wedged the whole
            # suite here. The one arm that must use a FIFO joins a 20s thread for this
            # reason; every other arm should simply not arm the trap.
            def _sock():
                import socket as _socket
                s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
                try:
                    s.bind(os.path.join(cwd, "sock"))
                finally:
                    s.close()
            _try(_sock)
        if seen.get("lock_exec_root") and os.path.basename(cwd) == "workspace":
            # The agent locks the directory it was handed. `rmtree(ignore_errors=True)` then
            # answers "did this raise" rather than "is it gone", and both secret-bearing
            # files survived a cell that said nothing about them.
            os.chmod(os.path.dirname(cwd), 0o000)
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
        _check("relocate.parent_of_exec_cwd_is_not_published",
               not os.path.exists(os.path.join(cell.artifacts_dir, "above-cwd.txt"))
               and not os.path.isdir(os.path.dirname(cwd_used or repo_root)),
               f"what the agent wrote ABOVE its cwd is discarded, not archived: exec_ws is "
               f"nested one level inside the harness's own tempdir, so `..` reaches a "
               f"directory that is deleted whole rather than the artifact tree: "
               f"cell_dir={sorted(os.listdir(cell.artifacts_dir))}", failures, verbose)

        # Everything above ran isolated. The exec-dir relocation must NOT be gated on that
        # flag: `--no-isolated` means "see my real HOME and installed skills", and review
        # used it to write `../agent-created.txt` straight into cell_dir, where the workspace
        # scrub never looks because only `workspace` is archived.
        seen.clear()
        run_dir2 = os.path.join(repo_root, "artifacts", "run2")
        os.makedirs(run_dir2)
        r.run_id, r.run_dir, r.isolated = "run2", run_dir2, False
        r._secrets = r._run_secrets = ("sk-secret-abcdef",)
        try:
            cell2 = r._run_cell(ModelTarget(), spec)
        finally:
            r._secrets = r._run_secrets = ()
        cwd2 = seen.get("cwd")
        inside2 = os.path.abspath(cwd2 or "").startswith(
            os.path.abspath(cell2.artifacts_dir) + os.sep)
        above2 = os.path.join(cell2.artifacts_dir, "above-cwd.txt")
        _check("relocate.exec_cwd_detached_even_when_not_isolated",
               cwd2 is not None and not inside2 and not os.path.exists(above2)
               and os.path.isfile(os.path.join(cell2.artifacts_dir, "workspace", "run.py")),
               f"a --no-isolated cell still runs in a detached tempdir, so `../x` from the "
               f"agent's cwd cannot reach the artifact directory — HOME isolation and "
               f"execution-directory isolation answer different questions, and gating the "
               f"second on the first left the results tree writable: cwd={cwd2} "
               f"cell_dir={sorted(os.listdir(cell2.artifacts_dir))}", failures, verbose)

        # The exec tempdir holds whatever the agent wrote, including anything it echoed back
        # from an MCP result. `shutil.rmtree(..., ignore_errors=True)` reports on the CALL,
        # not on the directory: a chmod 000 left both secret-bearing files on disk in silence.
        seen.clear()
        seen["lock_exec_root"] = True
        run_dir3 = os.path.join(repo_root, "artifacts", "run3")
        os.makedirs(run_dir3)
        r.run_id, r.run_dir, r.isolated = "run3", run_dir3, True
        r._secrets = r._run_secrets = ("sk-secret-abcdef",)
        try:
            r._run_cell(ModelTarget(), spec)  # the cell's verdict is not the point here
        finally:
            r._secrets = r._run_secrets = ()
        root3 = _own_tempdir(seen.get("cwd")) or os.path.join(repo_root, "no-exec-root")
        _try(lambda: os.chmod(root3, 0o700))  # a regression leaves it undeletable otherwise
        _check("relocate.locked_exec_dir_is_actually_removed",
               not os.path.exists(root3),
               f"a temp dir the agent locked is still removed, through the same outward-in "
               f"escalation the workspace quarantine uses — `ignore_errors=True` answers "
               f"'did this raise', which is not the question being asked of a directory "
               f"holding resolved credentials: {root3} "
               f"survivors={_try(lambda: sorted(os.listdir(root3)), [])}", failures, verbose)

        # And when removal genuinely cannot succeed, saying nothing is the failure. The note
        # has to reach the artifacts — result.json included, which step 6 wrote long before
        # any of this was knowable — and has to cost the cell its pass.
        seen.clear()
        run_dir4 = os.path.join(repo_root, "artifacts", "run4")
        os.makedirs(run_dir4)
        r.run_id, r.run_dir = "run4", run_dir4
        r._secrets = r._run_secrets = ("sk-secret-abcdef",)
        _orig_remove = runner_mod._remove
        runner_mod._remove = lambda p: False if "ase-ws-" in p else _orig_remove(p)
        try:
            cell4 = r._run_cell(ModelTarget(), spec)
        finally:
            runner_mod._remove = _orig_remove
            r._secrets = r._run_secrets = ()
            leftover4 = _own_tempdir(seen.get("cwd"))
            if leftover4:
                _try(lambda: os.chmod(leftover4, 0o700))
                _try(lambda: shutil.rmtree(leftover4, ignore_errors=True))
        err4 = cell4.run_result.error or ""
        rj4 = os.path.join(cell4.artifacts_dir, "result.json")
        res4 = _try(lambda: open(rj4).read(), "")
        rep4 = _try(lambda: open(os.path.join(cell4.artifacts_dir, "report.md")).read(), "")
        _check("relocate.undeletable_exec_dir_is_durable_and_load_bearing",
               cell4.passed is False and "could not be removed and is still on disk" in err4
               and "could not be removed" in res4 and "could not be removed" in rep4
               and "sk-secret-abcdef" not in res4 and "sk-secret-abcdef" not in rep4,
               f"a credential directory that outlives its cell fails that cell and says so "
               f"in result.json as well as report.md — result.json is serialized at step 6, "
               f"before the workspace is even moved, so a note appended afterwards reaches "
               f"only half the readers unless it is re-written: passed={cell4.passed} "
               f"in_result_json={'could not be removed' in res4} err={err4[:90]!r}",
               failures, verbose)

        # The MCP scratch dir is the one place a resolved `${VAR}` is written in the clear —
        # its `mcp.json` IS the credential — and it was removed by the same best-effort call.
        from .mcp import parse_mcp_servers
        seen.clear()
        run_dir5 = os.path.join(repo_root, "artifacts", "run5")
        os.makedirs(run_dir5)
        r.run_id, r.run_dir = "run5", run_dir5
        spec5 = EvalSpec(name="demo", prompt="hi",
                         source_path=os.path.join(repo_root, "demo.yaml"),
                         mcp_servers=parse_mcp_servers(
                             {"echo": {"command": "/bin/echo"}}, where="selftest"))
        runner_mod._remove = lambda p: False if "ase-mcp-" in p else _orig_remove(p)
        try:
            cell5 = r._run_cell(ModelTarget(), spec5)
        finally:
            runner_mod._remove = _orig_remove
            r._secrets = r._run_secrets = ()
            s5 = seen.get("scratch") or ""
            if os.path.basename(s5).startswith("ase-mcp-"):  # never reach past what we made
                _try(lambda: shutil.rmtree(s5, ignore_errors=True))
        err5 = cell5.run_result.error or ""
        _check("relocate.mcp_scratch_dir_removal_is_load_bearing",
               bool(seen.get("scratch")) and cell5.passed is False
               and "MCP scratch directory" in err5 and (seen.get("scratch") or "?") in err5,
               f"the scratch dir that held the interpolated credentials is purged through "
               f"the verified path too, and a failure to remove it fails the cell naming the "
               f"directory — the config file in there is the resolved secret in the clear, so "
               f"'probably deleted' is not an answer: scratch={seen.get('scratch')} "
               f"passed={cell5.passed} err={err5[:110]!r}", failures, verbose)

        # ...but only if the note SURVIVES. `execute()` raising is not an exotic path — a
        # crashed CLI, a timeout handler that throws, an adapter bug — and it transfers
        # control out of the frame holding the note. Review forced both at once and got a
        # cell that recorded `RuntimeError` and nothing about the `mcp.json` left on disk.
        seen.clear()
        run_dir6 = os.path.join(repo_root, "artifacts", "run6")
        os.makedirs(run_dir6)
        r.run_id, r.run_dir = "run6", run_dir6

        def _crashing_execute(*a, **kw):
            _fake_execute(*a, **kw)  # record the scratch dir, then die like a real CLI
            raise RuntimeError("child crashed")

        runner_mod.execute = _crashing_execute
        runner_mod._remove = lambda p: False if "ase-mcp-" in p else _orig_remove(p)
        try:
            cell6 = r._run_cell(ModelTarget(), spec5)
        finally:
            runner_mod.execute = _fake_execute
            runner_mod._remove = _orig_remove
            r._secrets = r._run_secrets = ()
            s6 = seen.get("scratch") or ""
            if os.path.basename(s6).startswith("ase-mcp-"):  # never reach past what we made
                _try(lambda: shutil.rmtree(s6, ignore_errors=True))
        err6 = cell6.run_result.error or ""
        rep6 = _try(lambda: open(os.path.join(cell6.artifacts_dir, "report.md")).read(), "")
        _check("relocate.scratch_failure_survives_a_crashing_execute",
               bool(seen.get("scratch")) and "child crashed" in err6
               and (seen.get("scratch") or "?") in err6
               and err6.count("MCP scratch directory") == 1
               and "MCP scratch directory" in rep6,
               f"a crash in execute() must not swallow the credential-directory failure "
               f"recorded on the way out: the note lived in a body local and the exception "
               f"left the body, so the crashed cell reported only the crash. Once, not "
               f"twice — a directory that could not be removed is deregistered, so the "
               f"outer sweep neither retries what already escalated nor repeats itself. "
               f"scratch={seen.get('scratch')} err={err6[:130]!r}", failures, verbose)

        # The other half of the same loss: `execute` returns, the note is recorded, and
        # something AFTER it raises before the body reaches the code that applies notes —
        # "an OSError mid-move" is the case `_run_cell`'s own docstring already names.
        seen.clear()
        run_dir7 = os.path.join(repo_root, "artifacts", "run7")
        os.makedirs(run_dir7)
        r.run_id, r.run_dir = "run7", run_dir7
        _orig_move = shutil.move

        def _failing_move(src, dst, *a, **kw):
            # Scoped to the exec tempdir: patching a stdlib function must not be able to
            # reach anything else in this process, this section's own teardown included.
            if "ase-ws-" in str(src):
                raise OSError("selftest: move failed")
            return _orig_move(src, dst, *a, **kw)

        shutil.move = _failing_move
        runner_mod._remove = lambda p: False if "ase-mcp-" in p else _orig_remove(p)
        try:
            cell7 = r._run_cell(ModelTarget(), spec5)
        finally:
            shutil.move = _orig_move
            runner_mod._remove = _orig_remove
            r._secrets = r._run_secrets = ()
            s7 = seen.get("scratch") or ""
            if os.path.basename(s7).startswith("ase-mcp-"):
                _try(lambda: shutil.rmtree(s7, ignore_errors=True))
        err7 = cell7.run_result.error or ""
        res7 = _try(lambda: open(os.path.join(cell7.artifacts_dir, "result.json")).read(), "")
        _check("relocate.scratch_failure_survives_a_raise_after_the_run",
               bool(seen.get("scratch")) and (seen.get("scratch") or "?") in err7
               and "MCP scratch directory" in err7 and "MCP scratch directory" in res7,
               f"a raise anywhere between the agent exiting and the notes being applied "
               f"dropped the same failure on the floor — the fix is not an extra except "
               f"clause per raise site but holding the note in the frame that SURVIVES the "
               f"raise: scratch={seen.get('scratch')} err={err7[:130]!r}", failures, verbose)

        # And a raise BEFORE the agent ever launches — a `${VAR}` that stopped resolving, an
        # adapter whose RunOptions changed shape — used to escape the only code that removes
        # the scratch dir, because the guard started at `execute` and the directory does not.
        seen.clear()
        run_dir8 = os.path.join(repo_root, "artifacts", "run8")
        os.makedirs(run_dir8)
        r.run_id, r.run_dir = "run8", run_dir8
        removed: list = []
        _orig_opts = runner_mod.RunOptions

        def _exploding_options(*a, **kw):
            raise TypeError("selftest: RunOptions changed shape")

        runner_mod.RunOptions = _exploding_options
        runner_mod._remove = lambda p: (removed.append(p), _orig_remove(p))[1]
        try:
            r._run_cell(ModelTarget(), spec5)  # the cell's verdict is not the point here
        finally:
            runner_mod.RunOptions = _orig_opts
            runner_mod._remove = _orig_remove
            r._secrets = r._run_secrets = ()
        _check("relocate.scratch_dir_removed_even_if_the_run_never_starts",
               any(os.path.basename(p).startswith("ase-mcp-") for p in removed),
               f"the scratch dir holds the resolved credentials from the instant it is "
               f"created, so the guard has to start where the directory does rather than at "
               f"`execute` with two raise-capable statements in between: "
               f"removed={[os.path.basename(p) for p in removed]}", failures, verbose)

        # Holding the note in the surviving frame is only half of it: reading it must not
        # DESTROY it either. A drain-on-read puts the note back in a local — one frame later
        # than the original bug and just as lossy the moment the write behind it raises.
        seen.clear()
        run_dir9 = os.path.join(repo_root, "artifacts", "run9")
        os.makedirs(run_dir9)
        r.run_id, r.run_dir = "run9", run_dir9
        _orig_rwj = runner_mod.Runner._rwj
        writes: list = []

        def _flaky_rwj(self, path, data):
            if os.path.basename(path) == "result.json":
                writes.append(path)
                if len(writes) == 2:  # the rewrite that is supposed to carry the note
                    raise OSError("selftest: result.json write failed")
            return _orig_rwj(self, path, data)

        runner_mod.Runner._rwj = _flaky_rwj
        runner_mod._remove = lambda p: False if "ase-mcp-" in p else _orig_remove(p)
        try:
            cell9 = r._run_cell(ModelTarget(), spec5)
        finally:
            runner_mod.Runner._rwj = _orig_rwj
            runner_mod._remove = _orig_remove
            r._secrets = r._run_secrets = ()
            s9 = seen.get("scratch") or ""
            if os.path.basename(s9).startswith("ase-mcp-"):
                _try(lambda: shutil.rmtree(s9, ignore_errors=True))
        err9 = cell9.run_result.error or ""
        res9 = _try(lambda: open(os.path.join(cell9.artifacts_dir, "result.json")).read(), "")
        _check("relocate.cleanup_note_is_acknowledged_only_once_it_is_on_disk",
               bool(seen.get("scratch")) and "result.json write failed" in err9
               and "MCP scratch directory" in err9 and (seen.get("scratch") or "?") in err9
               and "MCP scratch directory" in res9,
               f"the write that was supposed to carry the note raised, and the crash path "
               f"has to still find it: a note is owed to a reader until it REACHES one, so "
               f"handing it out and forgetting it are separate steps and the second happens "
               f"after the artifact writes return. err={err9[:150]!r} "
               f"in_result_json={'MCP scratch directory' in res9}", failures, verbose)

        # And the same again one write later. Acknowledging after `result.json` but before
        # `report.md` looks safe — the note IS on disk — right up until the crash path
        # rewrites `result.json` from a fresh result that no longer carries it.
        seen.clear()
        run_dir12 = os.path.join(repo_root, "artifacts", "run12")
        os.makedirs(run_dir12)
        r.run_id, r.run_dir = "run12", run_dir12
        _orig_wcj = runner_mod.Runner._write_cell_json
        cj_writes: list = []

        def _flaky_write_cell_json(self, cell_dir, cell):
            cj_writes.append(cell_dir)
            if len(cj_writes) == 1:  # the body's write, after result.json already landed
                raise OSError("selftest: cell.json write failed")
            return _orig_wcj(self, cell_dir, cell)

        runner_mod.Runner._write_cell_json = _flaky_write_cell_json
        runner_mod._remove = lambda p: False if "ase-mcp-" in p else _orig_remove(p)
        try:
            cell12 = r._run_cell(ModelTarget(), spec5)
        finally:
            runner_mod.Runner._write_cell_json = _orig_wcj
            runner_mod._remove = _orig_remove
            r._secrets = r._run_secrets = ()
            s12 = seen.get("scratch") or ""
            if os.path.basename(s12).startswith("ase-mcp-"):
                _try(lambda: shutil.rmtree(s12, ignore_errors=True))
        res12 = _try(lambda: open(os.path.join(cell12.artifacts_dir, "result.json")).read(), "")
        _check("relocate.cleanup_note_survives_the_crash_rewriting_result_json",
               bool(seen.get("scratch")) and "cell.json write failed" in res12
               and "MCP scratch directory" in res12,
               f"the note reached result.json and a LATER write raised — and the crash path "
               f"then rewrites result.json from a result it rebuilt, so anything already "
               f"forgotten is overwritten rather than merely absent. Acknowledgement belongs "
               f"after every write that carries the note, not after the first one: "
               f"in_result_json={'MCP scratch directory' in res12} res={res12[:120]!r}",
               failures, verbose)

        # The isolated HOME is built well before the guard that removed it, and everything in
        # between — the overlay build, a progress update, the effort resolution — can raise.
        class _AngryProgress:
            def update(self, **kw):
                if str(kw.get("phase", "")).startswith("running agent"):
                    raise RuntimeError("selftest: progress exploded")

            def done(self, **kw):
                pass

        seen.clear()
        run_dir10 = os.path.join(repo_root, "artifacts", "run10")
        os.makedirs(run_dir10)
        r.run_id, r.run_dir = "run10", run_dir10
        homes_seen: list = []
        _saved_progress = r.progress
        r.progress = _AngryProgress()
        runner_mod._remove = lambda p: (homes_seen.append(p), _orig_remove(p))[1]
        try:
            cell10 = r._run_cell(ModelTarget(), spec)
        finally:
            r.progress = _saved_progress
            runner_mod._remove = _orig_remove
            r._secrets = r._run_secrets = ()
        homes = [p for p in homes_seen if os.path.basename(p).startswith("ase-home-")]
        _check("relocate.isolated_home_is_owned_from_the_moment_it_exists",
               bool(homes) and all(not os.path.lexists(p) for p in homes)
               and "progress exploded" in (cell10.run_result.error or ""),
               f"a raise between building the isolated HOME and reaching the code that "
               f"removes it left `ase-home-*` behind, unnamed: registration belongs at "
               f"creation, since until then the only thing that knows the directory exists "
               f"is a local in the frame the raise unwinds. "
               f"homes={[os.path.basename(p) for p in homes]}", failures, verbose)

        # ...and a HOME that will not go is NAMED — on `warnings`, not `error`. It carries
        # config masks and symlinks into the real home, so it is a leaked temp directory
        # rather than a leaked secret, and failing the cell over it would be a lie.
        seen.clear()
        run_dir11 = os.path.join(repo_root, "artifacts", "run11")
        os.makedirs(run_dir11)
        r.run_id, r.run_dir = "run11", run_dir11
        stuck_homes: list = []
        r.progress = _AngryProgress()

        def _no_home_removal(p):
            if "ase-home-" in p:
                stuck_homes.append(p)
                return False
            return _orig_remove(p)

        runner_mod._remove = _no_home_removal
        try:
            cell11 = r._run_cell(ModelTarget(), spec)
        finally:
            r.progress = _saved_progress
            runner_mod._remove = _orig_remove
            r._secrets = r._run_secrets = ()
            for p in stuck_homes:  # never reach past what the runner made
                if os.path.basename(p).startswith("ase-home-"):
                    _try(lambda p=p: shutil.rmtree(p, ignore_errors=True))
        warns11 = " ".join(cell11.run_result.warnings or [])
        rep11 = _try(lambda: open(os.path.join(cell11.artifacts_dir, "report.md")).read(), "")
        _check("relocate.stubborn_isolated_home_warns_rather_than_failing_the_cell",
               "the isolated HOME could not be removed" in warns11
               and "holds this cell's resolved credentials" not in warns11
               and "the isolated HOME" not in (cell11.run_result.error or "")
               and "the isolated HOME could not be removed" in rep11,
               f"same registration and same verified removal as the credential dirs, but a "
               f"different verdict: this one lands on `warnings` (durable via cell.json and "
               f"report.md) instead of failing the cell, and its sentence must not claim it "
               f"holds credentials. warnings={warns11[:130]!r} "
               f"err={(cell11.run_result.error or '')[:60]!r}", failures, verbose)

        # ...but only while it holds what the HARNESS put in it. `$HOME` is writable by the
        # child, so a cell that resolved credentials has handed the agent somewhere to copy
        # them; the severity has to follow the contents, not the creation.
        # A spec that actually resolves a `${VAR}`, so `_secrets` is non-empty and the scrub
        # has something to do — `spec5` declares a server but interpolates nothing.
        os.environ["ASE_SELFTEST_TOKEN"] = "sk-secret-abcdef"
        spec_secret = EvalSpec(
            name="demo", prompt="hi", source_path=os.path.join(repo_root, "demo.yaml"),
            mcp_servers=parse_mcp_servers(
                {"echo": {"command": "/bin/echo", "env": {"TOKEN": "${ASE_SELFTEST_TOKEN}"}}},
                where="selftest"))

        # An interpolating cell is REFUSED unless its $HOME has no write path into the real
        # home, so these arms need a contained one to reach the behaviour they are about.
        # Pointing HOME at an empty directory is also the honest fixture: the arms below
        # were symlinking the developer's actual home into a temp overlay on every run.
        os.environ["HOME"] = contained_home

        seen.clear()
        run_dir13 = os.path.join(repo_root, "artifacts", "run13")
        os.makedirs(run_dir13)
        r.run_id, r.run_dir = "run13", run_dir13
        stuck13: list = []
        leaked13 = False

        def _no_home_removal13(p):
            if "ase-home-" in p:
                stuck13.append(p)
                return False
            return _orig_remove(p)

        runner_mod._remove = _no_home_removal13
        try:
            cell13 = r._run_cell(ModelTarget(), spec_secret)
        finally:
            runner_mod._remove = _orig_remove
            r._secrets = r._run_secrets = ()
            # Read the premise BEFORE tearing it down: an earlier draft deleted the leaked
            # HOME here and then asked whether the token was in it.
            home13 = seen.get("home") or ""
            leaked13 = bool(home13) and os.path.isfile(
                os.path.join(home13, "agent-cache.txt"))
            for p in stuck13:
                if os.path.basename(p).startswith("ase-home-"):
                    _try(lambda p=p: shutil.rmtree(p, ignore_errors=True))
        err13 = cell13.run_result.error or ""
        _check("relocate.child_writable_home_is_credential_bearing_after_the_run",
               leaked13 and cell13.passed is False
               and "could have written this cell's resolved credentials" in err13
               and "no resolved credentials are in it" not in err13,
               f"the agent copied its token into $HOME and the removal failed: a directory "
               f"the child can write is credential-bearing from the moment the cell HAS "
               f"credentials, so it fails the cell and its sentence claims reach rather than "
               f"denying contents the harness stopped controlling at launch. "
               f"leaked={leaked13} passed={cell13.passed} err={err13[:130]!r}",
               failures, verbose)

        # Acknowledgement moved to the last statement before the return because everything
        # between the writes and there can still raise — and `_failed_cell` REWRITES
        # result.json, so an early ack erases the finding rather than merely omitting it.
        class _DoneExplodes:
            def __init__(self):
                self.calls = 0

            def update(self, **kw):
                pass

            def done(self, **kw):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("selftest: progress.done exploded")

        seen.clear()
        run_dir14 = os.path.join(repo_root, "artifacts", "run14")
        os.makedirs(run_dir14)
        r.run_id, r.run_dir = "run14", run_dir14
        r.progress = _DoneExplodes()
        runner_mod._remove = lambda p: False if "ase-mcp-" in p else _orig_remove(p)
        try:
            cell14 = r._run_cell(ModelTarget(), spec5, cell_idx=1)
        finally:
            r.progress = _saved_progress
            runner_mod._remove = _orig_remove
            r._secrets = r._run_secrets = ()
            s14 = seen.get("scratch") or ""
            if os.path.basename(s14).startswith("ase-mcp-"):
                _try(lambda: shutil.rmtree(s14, ignore_errors=True))
        res14 = _try(lambda: open(os.path.join(cell14.artifacts_dir, "result.json")).read(), "")
        _check("relocate.cleanup_note_survives_a_raise_after_the_artifacts",
               bool(seen.get("scratch")) and "progress.done exploded" in res14
               and "MCP scratch directory" in res14,
               f"the artifact writes had all returned and the note was still not safe: the "
               f"judge artifacts and `progress.done` come after them, and a raise there "
               f"reaches the path that REBUILDS result.json. Acknowledge where nothing is "
               f"left to go wrong: in_result_json={'MCP scratch directory' in res14} "
               f"res={res14[:120]!r}", failures, verbose)

        # The scrub's verdict is the one finding that CANNOT be recomputed: it reports what
        # it already deleted, so a rescan after a raise sees a clean tree and says nothing.
        seen.clear()
        run_dir15 = os.path.join(repo_root, "artifacts", "run15")
        os.makedirs(run_dir15)
        r.run_id, r.run_dir = "run15", run_dir15
        seen["make_special"] = True
        r.progress = _DoneExplodes()
        try:
            cell15 = r._run_cell(ModelTarget(), spec_secret, cell_idx=1)
        finally:
            r.progress = _saved_progress
            r._secrets = r._run_secrets = ()
            os.environ.pop("ASE_SELFTEST_TOKEN", None)
        res15 = _try(lambda: open(os.path.join(cell15.artifacts_dir, "result.json")).read(), "")
        ws15 = os.path.join(cell15.artifacts_dir, "workspace")
        _check("relocate.scrub_verdict_survives_a_raise_that_rebuilds_the_result",
               "progress.done exploded" in res15 and "could not certify" in res15
               and "sock" in res15 and not os.path.lexists(os.path.join(ws15, "sock")),
               f"the scrub removed an artifact it could not certify and then a later raise "
               f"rebuilt the result: rescanning finds the tree clean, so the deletion goes "
               f"unreported unless the original verdict rode the same protocol as the purge "
               f"failures. An evidence deletion that nothing records is worse than either "
               f"outcome it chose between. res={res15[:150]!r}", failures, verbose)

        # The overlay masks READS. Its unmasked entries are symlinks into the real home, so
        # a token written through one lands where nothing this run deletes or scrubs can
        # reach — and deleting a symlink certifies nothing about its target.
        seen.clear()
        run_dir16 = os.path.join(repo_root, "artifacts", "run16")
        os.makedirs(run_dir16)
        r.run_id, r.run_dir = "run16", run_dir16
        os.makedirs(os.path.join(contained_home, "cache"), exist_ok=True)  # one passthrough
        os.environ["ASE_SELFTEST_TOKEN"] = "sk-secret-abcdef"
        try:
            cell16 = r._run_cell(ModelTarget(), spec_secret)
        finally:
            r._secrets = r._run_secrets = ()
            _try(lambda: os.rmdir(os.path.join(contained_home, "cache")))
        err16 = cell16.run_result.error or ""
        _check("mcp.credential_run_is_refused_when_home_writes_escape_the_overlay",
               cell16.passed is False and "interpolates a credential" in err16
               and "cache" in err16 and "Refusing" in err16
               and seen.get("cwd") is None,
               f"a cell that resolves a `${{VAR}}` under a symlink overlay is refused before "
               f"the agent starts, naming the entries that lead out: the harness will not "
               f"go hunting through the user's home afterwards, so 'run it and scrub' is "
               f"not on the table and 'run it and delete the overlay' certifies nothing. "
               f"ran={seen.get('cwd') is not None} passed={cell16.passed} "
               f"err={err16[:150]!r}", failures, verbose)

        # A credential too short to redact is still a credential. This is the arm that would
        # have caught gating on `bool(secrets)` — the redaction set is empty here.
        seen.clear()
        run_dir18 = os.path.join(repo_root, "artifacts", "run18")
        os.makedirs(run_dir18)
        r.run_id, r.run_dir = "run18", run_dir18
        os.environ["ASE_SELFTEST_SHORT"] = "ab"
        spec_short = EvalSpec(
            name="demo", prompt="hi", source_path=os.path.join(repo_root, "demo.yaml"),
            mcp_servers=parse_mcp_servers(
                {"echo": {"command": "/bin/echo", "env": {"TOKEN": "${ASE_SELFTEST_SHORT}"}}},
                where="selftest"))
        os.makedirs(os.path.join(contained_home, "cache"), exist_ok=True)
        try:
            cell18 = r._run_cell(ModelTarget(), spec_short)
        finally:
            os.environ.pop("ASE_SELFTEST_SHORT", None)
            r._secrets = r._run_secrets = ()
            _try(lambda: os.rmdir(os.path.join(contained_home, "cache")))
        err18 = cell18.run_result.error or ""
        _check("mcp.short_credential_run_is_refused_like_any_other",
               cell18.passed is False and "interpolates a credential" in err18
               and seen.get("cwd") is None,
               f"the resolved value is two characters, so the redaction set is EMPTY — and "
               f"the token is no less a token. Gating exposure on what can be scrubbed "
               f"would have let this one run and leak: ran={seen.get('cwd') is not None} "
               f"err={err18[:110]!r}", failures, verbose)

        # And a declaration that interpolates NOTHING must not be treated as credentialed.
        seen.clear()
        run_dir17 = os.path.join(repo_root, "artifacts", "run17")
        os.makedirs(run_dir17)
        r.run_id, r.run_dir = "run17", run_dir17
        stuck17: list = []

        def _no_home_removal17(p):
            if "ase-home-" in p:
                stuck17.append(p)
                return False
            return _orig_remove(p)

        runner_mod._remove = _no_home_removal17
        try:
            cell17 = r._run_cell(ModelTarget(), spec5)  # `mcp_servers`, but no ${VAR}
        finally:
            runner_mod._remove = _orig_remove
            r._secrets = r._run_secrets = ()
            for p in stuck17:
                if os.path.basename(p).startswith("ase-home-"):
                    _try(lambda p=p: shutil.rmtree(p, ignore_errors=True))
        err17 = cell17.run_result.error or ""
        warns17 = " ".join(cell17.run_result.warnings or [])
        _check("mcp.home_severity_follows_interpolation_not_declaration",
               bool(seen.get("cwd")) and "the isolated HOME" not in err17
               and "the isolated HOME could not be removed" in warns17
               and "resolved credentials into it" not in warns17,
               f"declaring a server is not handling a secret: `{{'echo': {{'command': "
               f"'/bin/echo'}}}}` interpolates nothing, so a stubborn HOME is a leaked "
               f"tempdir and must not fail the cell claiming the agent could have written "
               f"credentials it was never given. Gated on `${{VAR}}` in the declaration, "
               f"not on `bool(secrets)` — short values are excluded from redaction on "
               f"purpose and are still credentials. err={err17[:90]!r} "
               f"warnings={warns17[:110]!r}", failures, verbose)
    finally:
        runner_mod.execute = orig_execute
        if real_home_env is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = real_home_env
        os.environ.pop("ASE_SELFTEST_TOKEN", None)
        shutil.rmtree(contained_home, ignore_errors=True)
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

        # What the overlay lets a write REACH, which is a different question from what it
        # lets the model read — and the one the harness had never asked.
        from .isolation import build_isolated_home, home_write_escapes
        esc_home = tempfile.mkdtemp(prefix="ase-esc-")
        beyond = tempfile.mkdtemp(prefix="ase-beyond-")
        # A symlink BEHIND the escape, reachable only by following it. The walk must not:
        # the overlay is small and the real home is not, and a path found that way is not a
        # path the overlay actually offers. Not a loop back into `real` — `followlinks=True`
        # would spin on a cycle forever, which is a worse way to learn the same thing.
        os.symlink(beyond, os.path.join(real, ".fakecli", "plugins", "elsewhere"))
        try:
            build_isolated_home(esc_home, [".fakecli/skills"], set(), [], real_home=real)
            escapes = home_write_escapes(esc_home)
            skills = os.path.join(esc_home, ".fakecli", "skills")
            authlink = os.path.join(esc_home, ".fakecli", "auth.json")
            _check("mcp_masked_home.write_escapes_are_directory_symlinks_out_of_the_overlay",
                   escapes == [os.path.join(".fakecli", "plugins")]
                   and os.path.islink(authlink)
                   and os.path.isdir(skills) and not os.path.islink(skills),
                   f"a passed-through directory is a path OUT: writing "
                   f"$HOME/.fakecli/plugins/x creates real/.fakecli/plugins/x, which no "
                   f"cleanup here deletes and no scrub reads. Exactly the directory "
                   f"symlinks: the masked skills dir is materialized so it is not one, "
                   f"`auth.json` is a symlink to a FILE (clobberable, but you cannot plant "
                   f"a new file through it), and the escape is reported once rather than "
                   f"once per entry of the real directory behind it — the walk must not "
                   f"descend through what it is reporting. escapes={escapes}",
                   failures, verbose)
            _check("mcp_masked_home.contained_overlay_reports_no_escapes",
                   home_write_escapes(tempfile.mkdtemp(prefix="ase-empty-")) == []
                   and home_write_escapes(None) == [],
                   "an overlay with nothing symlinked out reports nothing, so the refusal "
                   "this feeds lifts itself once the HOME is materialized instead of "
                   "needing the check removed", failures, verbose)
        finally:
            shutil.rmtree(esc_home, ignore_errors=True)
            shutil.rmtree(beyond, ignore_errors=True)
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

    # build_argv reads the state that decides hermeticity BEFORE launch; the child reads
    # it again AFTER, and runs code in between (copilot execs git before globbing its
    # agent dirs). Nothing can make that window empty — the discovery paths climb to `/`
    # and the config home belongs to the child — so execute() re-checks once the child is
    # gone and an adapter that finds the state moved fails the run. The default hook has
    # no preflight state to re-check and stays a no-op.
    class _LateLeakCli(Adapter):
        name = "lateleak"
        binary = sys.executable

        def build_argv(self, prompt, opts, *, cwd):
            return [sys.executable, "-c", "pass"]

        def parse(self, stdout, stderr, exit_code, *, opts=None):
            return ParseOutput()

        def verify_post_run(self, argv, opts, *, cwd, stdout="", stderr=""):
            raise RuntimeError("late.md appeared during the launch window")

    ex_late = _execute(_LateLeakCli(), "p", RunOptions(), cwd="/tmp", timeout=60)
    _check("mcp_exec.post_run_leak_fails_the_run",
           ex_late.result.exit_code == 0
           and "hermeticity was not confirmed after the run"
               in (ex_late.result.error or "")
           and "late.md" in (ex_late.result.error or ""),
           f"a clean-exiting run whose hermeticity premise broke during launch is "
           f"still FAILED (detection, not prevention): {ex_late.result.error!r}",
           failures, verbose)
    _check("mcp_exec.post_run_default_is_noop",
           Adapter.verify_post_run(spy, ["x"], RunOptions(), cwd="/tmp") is None
           and not (_execute(spy, "p", RunOptions(), cwd="/tmp", timeout=5)
                    .result.error or "").startswith("MCP hermeticity"),
           "an adapter with no preflight state to re-check is unaffected by the "
           "post-run hook", failures, verbose)

    # execute() is not the only place the harness spawns an agent CLI: probe_model runs
    # one per candidate model, against the same discovery paths, with the same launch
    # window — and it does NOT go through execute(). It has to run the verifier itself,
    # and a probe it can't clear must come back unavailable rather than merely un-audited.
    class _LateLeakProbe(_LateLeakCli):
        name = "lateleakprobe"

        def _probe_argv(self, model, *, cwd=None, env=None):
            return [sys.executable, "-c", "pass"]

    _pv_ran: list = []

    class _CleanProbe(_LateLeakProbe):
        name = "cleanprobe"

        def verify_post_run(self, argv, opts, *, cwd, stdout="", stderr=""):
            # also pins WHAT the probe hands the verifier: its own fresh workspace and
            # the env its argv was built from, not the harness's ambient context
            _pv_ran.append((cwd, isinstance(opts.effective_env, dict)))

    with contextlib.redirect_stderr(io.StringIO()):
        _pv_leak = _LateLeakProbe().probe_model("m", timeout=30)
        _pv_ok = _CleanProbe().probe_model("m", timeout=30)
    _check("mcp_probe.probe_runs_the_post_run_verifier",
           _pv_leak.accepted is False and _pv_ok.accepted is True
           and len(_pv_ran) == 1 and os.path.isabs(_pv_ran[0][0])
           and _pv_ran[0][1] and not os.path.exists(_pv_ran[0][0]),
           f"a model probe is verified after it runs like any other child and fails "
           f"closed when it can't be cleared, against the probe's own workspace and "
           f"env: leak_accepted={_pv_leak.accepted} clean_accepted={_pv_ok.accepted} "
           f"saw={_pv_ran!r}", failures, verbose)

    # A probe that TIMES OUT is the case that used to escape entirely: subprocess.run's
    # kill reaches only the direct child, and the verifier was inside the try block the
    # TimeoutExpired jumped out of. So the harness would leave a grandchild running — an
    # MCP server, in the real case — and report the model unavailable without ever
    # checking what the child had done. Both halves are pinned here: the group is reaped,
    # and the verifier still runs on whatever output was captured before the kill.
    import shutil as _psh
    import tempfile as _pt
    import time as _ptime

    _pt_dir = _pt.mkdtemp(prefix="ase-probetimeout-")
    _pt_tick = os.path.join(_pt_dir, "tick")
    _pt_seen: list = []
    try:
        # the child outlives the timeout and leaves a grandchild ticking a file behind it
        _pt_grandchild = (
            "import time\n"
            "while True:\n"
            f"    open({_pt_tick!r}, 'a').write('.')\n"
            "    time.sleep(0.05)\n"
        )
        _pt_child = (
            "import subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, '-c', {_pt_grandchild!r}])\n"
            "sys.stdout.write('partial output before the kill\\n')\n"
            "sys.stdout.flush()\n"
            "time.sleep(30)\n"
        )

        class _TimeoutProbe(_LateLeakCli):
            name = "timeoutprobe"

            def _probe_argv(self, model, *, cwd=None, env=None):
                return [sys.executable, "-c", _pt_child]

            def verify_post_run(self, argv, opts, *, cwd, stdout="", stderr=""):
                _pt_seen.append(stdout)

        _pt_res = _TimeoutProbe().probe_model("m", timeout=2)
        # the grandchild must have really started, or the reaping arm proves nothing
        _pt_started = os.path.exists(_pt_tick)
        _pt_before = os.path.getsize(_pt_tick) if _pt_started else -1
        _ptime.sleep(0.6)
        _pt_after = os.path.getsize(_pt_tick) if _pt_started else -2
    finally:
        _psh.rmtree(_pt_dir, ignore_errors=True)
    _check("mcp_probe.timed_out_probe_is_verified_and_reaped",
           _pt_res.accepted is False and _pt_started and _pt_before == _pt_after
           and len(_pt_seen) == 1 and "partial output" in _pt_seen[0],
           f"a probe killed on timeout still goes through the post-run verifier — with "
           f"the output captured before the kill, the only account the child ever gave — "
           f"and its whole process group dies with it instead of orphaning an MCP server "
           f"past the check meant to prove none started: accepted={_pt_res.accepted} "
           f"grandchild_started={_pt_started} ticks {_pt_before}->{_pt_after} "
           f"verifier_calls={len(_pt_seen)}", failures, verbose)

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

    _check_exec_early_exit_group_kill(failures, verbose)
    _check_exec_normal_exit_group_sweep(failures, verbose)
    _check_exec_process_tree_handles(failures, verbose)


def _check_version_provenance_shared(failures, verbose):
    """The tiers, the warn-once bookkeeping and the "no version source" case, in base.py.

    These are the parts every adapter shares, so a defect here is a defect in all four at
    once. Two of them are the kind that fail SILENTLY, which is why they get arms rather
    than trust: a denylist that can never match looks exactly like an empty denylist, and
    a warn-once key that forgets the agent looks exactly like a matrix that had nothing to
    warn about.
    """
    import io
    import sys

    from .adapters import base as _b

    print("version provenance (shared):")

    # --- a denylist an adapter can never enforce is refused at construction -----------
    # The trap: check_denied() is reached with None on every run of an adapter that has no
    # version source, so every denied entry silently fails to fire. Whoever added the entry
    # gets no signal — a denylist that never matches is indistinguishable from one with
    # nothing to match. This has to be a construction error or it is nothing.
    unenforceable = ""
    try:
        _b.VersionProvenance(agent="ghost", verified=("1.0.0",), verified_on="2026-01-01",
                             clear_hint="h", unreadable="no source in the stream",
                             denied={"6.6.6": "loads a server silently"})
    except ValueError as exc:
        unenforceable = str(exc)
    # ...and the same declaration WITHOUT the denylist is fine, so the arm is pinning the
    # combination rather than just "unreadable adapters cannot be built".
    built_ok = True
    try:
        _b.VersionProvenance(agent="ghost", verified=("1.0.0",), verified_on="2026-01-01",
                             clear_hint="h", unreadable="no source in the stream")
    except ValueError:
        built_ok = False
    # --- an UNKNOWN version is a denial too, once anything is denylisted --------------
    # Review found this hole: check_denied used to return successfully whenever the version
    # was None, so a run that completed normally, produced a good MCP witness and stated no
    # version passed — while excluding nothing. "Could not read the version" is not
    # evidence the version was fine, and the denylist tier covers exactly the defects the
    # witness cannot see, so a clean witness says nothing about it either.
    armed = _b.VersionProvenance(agent="armed", verified=("1.0.0",), verified_on="d",
                                 clear_hint="h", denied={"6.6.6": "loads a server"})
    unarmed = _b.VersionProvenance(agent="unarmed", verified=("1.0.0",), verified_on="d",
                                   clear_hint="h")

    def _denied_msg(prov, version, completed):
        try:
            prov.check_denied(version, completed=completed)
        except RuntimeError as exc:
            return str(exc)
        return ""

    unknown_completed = _denied_msg(armed, None, True)
    unknown_crashed = _denied_msg(armed, None, False)
    unknown_no_denylist = _denied_msg(unarmed, None, True)
    known_good = _denied_msg(armed, "1.0.0", True)
    _check("provenance.unknown_version_fails_closed_when_anything_is_denied",
           "never stated which" in unknown_completed
           and unknown_crashed == "" and unknown_no_denylist == "" and known_good == "",
           f"a COMPLETED run that states no version fails closed while any build is "
           f"denylisted, since nothing about it excludes those builds; a run that crashed "
           f"before emitting telemetry does not ({unknown_crashed == ''}), because failing "
           f"it by version would report 'denylisted build' for what is a crash; and with "
           f"an empty denylist an unknown version is merely unknown "
           f"({unknown_no_denylist == ''}), which keeps every adapter's normal state quiet",
           failures, verbose)

    _check("provenance.unenforceable_denylist_is_refused",
           "can never fire" in unenforceable and built_ok,
           f"declaring a denylist alongside `unreadable` raises at construction "
           f"({unenforceable[:60]!r}), because such entries are reached only with a "
           f"version that never arrives; the same adapter without a denylist builds fine",
           failures, verbose)

    # --- warn-once is keyed PER ADAPTER ---------------------------------------------
    # A global key would mean the first runner in a mixed matrix silences every other
    # runner's notice — each would be a different CLI with a different unverified build,
    # and only one of them would ever be reported.
    a = _b.VersionProvenance(agent="agent_a", verified=("1.0.0",), verified_on="d",
                             clear_hint="hint A")
    b = _b.VersionProvenance(agent="agent_b", verified=("1.0.0",), verified_on="d",
                             clear_hint="hint B")
    saved_warned = set(_b._WARNED_VERSIONS)
    saved_err = sys.stderr
    try:
        _b._WARNED_VERSIONS.clear()
        sys.stderr = io.StringIO()
        a.warn_drift("9.9.9")
        b.warn_drift("9.9.9")      # same version, different agent -> must still warn
        a.warn_drift("9.9.9")      # repeat of a already-warned pair -> silent
        mixed = sys.stderr.getvalue()
    finally:
        sys.stderr = saved_err
        _b._WARNED_VERSIONS.clear()
        _b._WARNED_VERSIONS.update(saved_warned)
    _check("provenance.warn_once_is_keyed_per_adapter",
           mixed.count("warning:") == 2
           and "agent_a" in mixed and "agent_b" in mixed,
           f"two runners on the same unverified version each warn once ("
           f"{mixed.count('warning:')} warnings), rather than the first one silencing "
           f"the second — the key is (agent, version), not version", failures, verbose)

    # --- the "no version source" notice -------------------------------------------
    # It must not read as drift ("you are on an unverified build"), because there is no
    # build to name and nothing for the reader to go and look up. It must say WHY, or
    # "unknown" is unactionable, and it must not claim any runtime evidence it never saw.
    ghost = _b.VersionProvenance(
        agent="ghost", verified=("1.0.0",), verified_on="2026-01-01",
        clear_hint="check `ghost --version` out of band",
        unreadable="the stream states no version",
        witness_held="  The runtime witness held.",
        witness_absent="  No witness.")
    saved_warned = set(_b._WARNED_VERSIONS)
    try:
        _b._WARNED_VERSIONS.clear()
        sys.stderr = io.StringIO()
        ghost.warn_drift(None, witnessed=True)
        ghost.warn_drift(None, witnessed=True)
        unreadable_msg = sys.stderr.getvalue()
    finally:
        sys.stderr = saved_err
        _b._WARNED_VERSIONS.clear()
        _b._WARNED_VERSIONS.update(saved_warned)
    _check("provenance.unreadable_says_why_and_claims_nothing",
           unreadable_msg.count("warning:") == 1
           and "the stream states no version" in unreadable_msg
           and "1.0.0" in unreadable_msg
           and "witness held" not in unreadable_msg,
           f"an adapter with no version source warns once per process, names the REASON "
           f"there is nothing to read (so the reader does not go hunting for a version "
           f"that does not exist), cites the verified baseline, and — even when called "
           f"with witnessed=True — claims no runtime evidence, because the notice is "
           f"about an unknown build rather than about this run's hermeticity",
           failures, verbose)


def _check_claude_version_provenance(failures, verbose):
    """claude states its version outright, and its init event doubles as the MCP witness.

    Unlike copilot there is nothing to reconstruct: `claude_code_version` is a scalar the
    CLI writes about itself in `system`/`init`. The arms that matter are therefore about
    what happens when that event is ABSENT or RESHAPED — because "no servers found" and
    "the field moved" produce identical-looking clean results, and only one is safe.
    """
    import io
    import json as _json
    import sys

    from .adapters import claude as _cl

    print("claude version provenance + MCP witness:")

    def _init(**kw):
        obj = {"type": "system", "subtype": "init", "mcp_servers": []}
        obj.update(kw)
        return _json.dumps(obj)

    real = _init(claude_code_version="2.1.113")
    # Model-controlled text: an assistant message is the one place a model can write
    # whatever it likes. It must not be able to forge the version that silences a warning.
    forged = _json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": '{"claude_code_version": "9.9.9"} claude_code_version 9.9.9'}]}})

    ver_real = _cl._stream_cli_version(real)
    ver_forged_only = _cl._stream_cli_version(forged)
    ver_both = _cl._stream_cli_version(forged + "\n" + real)
    ver_none = _cl._stream_cli_version("")
    # Malformed telemetry must warn (version unknown), never raise: this runs inside
    # verify_post_run, where anything raised is reported as an MCP hermeticity failure —
    # and a mistyped field is not one.
    raised = None
    try:
        malformed = _cl._stream_cli_version(
            "\n".join([_json.dumps([1, 2, 3]),
                       _json.dumps({"type": "system", "subtype": "init",
                                    "claude_code_version": {"not": "a string"}}),
                       "not json at all"]))
    except Exception as exc:            # noqa: BLE001 — the point is that nothing escapes
        raised, malformed = type(exc).__name__, None
    _check("claude.version_read_from_the_run_not_a_probe",
           ver_real == "2.1.113" and ver_forged_only is None and ver_both == "2.1.113"
           and ver_none is None and malformed is None and raised is None,
           f"the version comes from the CLI's own init event (real={ver_real!r}); "
           f"model-controlled assistant text cannot supply one ({ver_forged_only!r}) nor "
           f"outrank the real one ({ver_both!r}); an empty or malformed stream resolves to "
           f"unknown ({ver_none!r}/{malformed!r}) rather than raising ({raised!r}), since a "
           f"raise here would be reported as an MCP hermeticity failure", failures, verbose)

    # --- the witness -----------------------------------------------------------------
    ok = _cl._mcp_witness(real, 0)
    live = _cl._mcp_witness(_init(claude_code_version="2.1.113",
                                  mcp_servers=[{"name": "leaky"}]), 0)
    # The field renamed/reshaped out from under the check, on a run that completed: this
    # is the ABA-shaped failure — it yields an empty server list, which reads exactly like
    # a hermetic run.
    reshaped = _cl._mcp_witness(
        _json.dumps({"type": "system", "subtype": "init", "mcpServers": []}), 0)
    # A run that never got as far as its init event is EXCUSED, not failed — a crash is
    # not evidence of a leak. But it is also not evidence of hermeticity, hence witnessed
    # is False and the drift notice must not claim otherwise.
    crashed = _cl._mcp_witness("", 1)
    completed_silently = _cl._mcp_witness("", 0)
    # --- evidence arriving AFTER the first init event still counts -------------------
    # Review reproduced both halves of this: a stream whose opening init reported an empty
    # server list and whose second reported a live one passed verification, and the version
    # reader took whichever build the stream OPENED with. An adapter that reads only the
    # start of a stream can be told anything by the rest of it.
    two_inits_live = _init(claude_code_version="2.1.113") + "\n" + _init(
        claude_code_version="2.1.113", mcp_servers=[{"name": "late"}])
    two_inits_reshaped = _init(claude_code_version="2.1.113") + "\n" + _json.dumps(
        {"type": "system", "subtype": "init", "mcp_servers": "not-a-list"})
    late = _cl._mcp_witness(two_inits_live, 0)
    late_reshaped = _cl._mcp_witness(two_inits_reshaped, 0)
    # Disagreeing versions resolve to unknown rather than to the first one seen — a stream
    # that tells two stories about what ran has established neither.
    ver_disagree = _cl._stream_cli_version(
        _init(claude_code_version="2.1.113") + "\n" + _init(claude_code_version="9.9.9"))
    ver_repeat = _cl._stream_cli_version(
        _init(claude_code_version="2.1.113") + "\n" + _init(claude_code_version="2.1.113"))
    late_leak = ""
    try:
        _cl.ClaudeAdapter().verify_post_run([], None, cwd=".", stdout=two_inits_live,
                                            exit_code=0)
    except RuntimeError as exc:
        late_leak = str(exc)
    _check("claude.evidence_after_the_first_init_still_counts",
           late[1] == ["late"] and late_reshaped[0] is not None
           and ver_disagree is None and ver_repeat == "2.1.113"
           and "late" in late_leak,
           f"a server named by a LATER init event is still reported {late[1]} and still "
           f"fails the run, rather than the check having made up its mind on the first "
           f"event; a reshaped list anywhere is a violation ({late_reshaped[0]!r}); and "
           f"disagreeing versions resolve to unknown ({ver_disagree!r}) while a repeated "
           f"identical one does not ({ver_repeat!r})", failures, verbose)

    _check("claude.mcp_witness_fails_closed",
           ok == (None, [], True, {})
           and live[0] is None and live[1] == ["leaky"]
           # The message must NAME the reshape, not merely be non-empty: a fallback path
           # ("no init event at all") also yields a violation here, so `is not None` alone
           # passes for the wrong reason and would survive the field-check being removed.
           and reshaped[0] is not None and "mcp_servers" in reshaped[0]
           and reshaped[2] is False
           and crashed == (None, [], False, {})
           and completed_silently[0] is not None,
           f"a hermetic run witnesses an empty server list {ok}; a loaded server is "
           f"surfaced {live}; a RESHAPED field fails closed rather than reading as clean "
           f"{reshaped[0]!r}; a run that died before its init event is excused {crashed} "
           f"while one that exited 0 with no witness at all is not "
           f"{completed_silently[0]!r}", failures, verbose)

    # --- the tiers, end to end through verify_post_run --------------------------------
    from .adapters import base as _b
    saved_denied = dict(_cl._DENIED_VERSIONS)
    saved_warned = set(_b._WARNED_VERSIONS)
    saved_err = sys.stderr
    adapter = _cl.ClaudeAdapter()
    try:
        _cl._DENIED_VERSIONS.clear()
        _cl._DENIED_VERSIONS["6.6.6"] = "loads MCP servers past --strict-mcp-config"
        denied = ""
        try:
            adapter.verify_post_run([], None, cwd=".",
                                    stdout=_init(claude_code_version="6.6.6"), exit_code=0)
        except RuntimeError as exc:
            denied = str(exc)
        # A verified build that witnessed clean is entirely silent.
        _b._WARNED_VERSIONS.clear()
        sys.stderr = io.StringIO()
        adapter.verify_post_run([], None, cwd=".", stdout=real, exit_code=0)
        quiet = sys.stderr.getvalue()
        # An unverified build warns once and does not claim evidence it lacks.
        sys.stderr = io.StringIO()
        adapter.verify_post_run([], None, cwd=".",
                                stdout=_init(claude_code_version="9.9.9"), exit_code=0)
        adapter.verify_post_run([], None, cwd=".",
                                stdout=_init(claude_code_version="9.9.9"), exit_code=0)
        warned = sys.stderr.getvalue()
        # A live server fails the run outright.
        leaked = ""
        try:
            adapter.verify_post_run([], None, cwd=".",
                                    stdout=_init(claude_code_version="2.1.113",
                                                 mcp_servers=[{"name": "leaky"}]),
                                    exit_code=0)
        except RuntimeError as exc:
            leaked = str(exc)
        # END TO END, the reviewer's reproduction: a run that COMPLETED with a clean
        # witness but stated no version must fail closed while anything is denylisted.
        # Testing check_denied directly is not enough — the defect that got shipped was in
        # verify_post_run's wiring, and a `completed=False` hardcoded here would restore it
        # while every isolated arm stayed green (confirmed by mutation R4).
        no_version_completed = ""
        try:
            adapter.verify_post_run(
                [], None, cwd=".",
                stdout=_json.dumps({"type": "system", "subtype": "init",
                                    "mcp_servers": []}), exit_code=0)
        except RuntimeError as exc:
            no_version_completed = str(exc)
        # A run that died before its init event is excused from the witness — so it
        # reaches the drift notice with NO MCP evidence at all. Claiming "the runtime
        # witness held" there would be the notice inventing the very check it is warning
        # about, which is the sentence a reader would quote to justify shipping. It must
        # also NOT be failed by version: no telemetry here means a crash, not a denied
        # build. Guarded so that a defect making it raise is reported as an arm failure
        # rather than crashing the selftest — a crash is a red signal of the wrong kind,
        # and it hides which arm was supposed to catch it.
        _b._WARNED_VERSIONS.clear()
        sys.stderr = io.StringIO()
        crashed_run_raised = ""
        try:
            adapter.verify_post_run([], None, cwd=".", stdout="", exit_code=1)
        except RuntimeError as exc:
            crashed_run_raised = str(exc)
        unwitnessed = sys.stderr.getvalue()
    finally:
        sys.stderr = saved_err
        _cl._DENIED_VERSIONS.clear()
        _cl._DENIED_VERSIONS.update(saved_denied)
        _b._WARNED_VERSIONS.clear()
        _b._WARNED_VERSIONS.update(saved_warned)
    _check("claude.version_tiers",
           "denylist" in denied and "AFTER the fact" in denied
           and quiet == ""
           and warned.count("warning:") == 1 and "9.9.9" in warned
           and "strict-mcp-config" in warned
           and "witness held" in warned
           and "leaky" in leaked and "not MCP-hermetic" in leaked
           and "never stated which" in no_version_completed
           and crashed_run_raised == ""
           and "witness held" not in unwitnessed
           and "no MCP server list at all" in unwitnessed,
           f"a denylisted build fails the run by name and says plainly that this is "
           f"detection after the CLI already ran; a verified build that witnessed clean "
           f"is silent; an unverified one warns ONCE per process and names the flag the "
           f"whole argument rests on; a run that actually loaded a server fails outright; "
           f"a COMPLETED run that states no version fails closed through verify_post_run "
           f"while a crashed one does not; and a run that never produced a witness says so "
           f"rather than claiming one held — a security notice that overstates its own "
           f"evidence is worse than none",
           failures, verbose)


def _check_unreadable_version_adapters(failures, verbose):
    """codex and antigravity cannot know which build ran — and say so rather than guess.

    Both were checked empirically (see the constants in each adapter). The risk this arm
    guards is not that they report the wrong version; it is that someone later "fixes" the
    warning by wiring in a `--version` probe, which would report a build the run may never
    have used, or adds a denylist entry that silently never fires.
    """
    import io
    import sys

    from .adapters import antigravity as _ag
    from .adapters import base as _b
    from .adapters import codex as _cx

    print("codex/antigravity version provenance:")

    # codex's verify_post_run now also re-checks MCP state, which shells out to the real
    # binary; stub the enumeration so this arm tests provenance only (the re-check has its
    # own arms below).
    class _QuietCodex(_cx.CodexAdapter):
        def _mcp_disable_args(self, cwd=None, env=None):
            return []

    opts = _cx.RunOptions()
    saved_warned = set(_b._WARNED_VERSIONS)
    saved_err = sys.stderr
    try:
        _b._WARNED_VERSIONS.clear()
        sys.stderr = io.StringIO()
        _QuietCodex().verify_post_run([], opts, cwd=".", stdout="", exit_code=0)
        _QuietCodex().verify_post_run([], opts, cwd=".", stdout="", exit_code=0)
        _ag.AntigravityAdapter().verify_post_run([], opts, cwd=".", stdout="", exit_code=0)
        both = sys.stderr.getvalue()
    finally:
        sys.stderr = saved_err
        _b._WARNED_VERSIONS.clear()
        _b._WARNED_VERSIONS.update(saved_warned)

    _check("codex_antigravity.version_unreadable_is_stated_not_guessed",
           both.count("warning:") == 2
           and "--ephemeral" in both and "0.140.0" in both
           and "1.1.1" in both
           and _cx._PROVENANCE.unreadable is not None
           and _ag._PROVENANCE.unreadable is not None
           and not _cx._PROVENANCE.denied and not _ag._PROVENANCE.denied,
           f"each warns once per process ({both.count('warning:')} total) and names why "
           f"there is no version to read — codex's points at the `--ephemeral` flag this "
           f"harness itself passes, which is the actionable part: the version is "
           f"purchasable only by giving up the isolation it buys. Neither carries a "
           f"denylist, which VersionProvenance would reject as unenforceable anyway",
           failures, verbose)

    # antigravity's constant must track the build its CHANNEL INVENTORY was established
    # against (1.1.1), not the newer build that merely happens to be installed (1.1.2) —
    # listing the latter because it "seems fine" is the constant blessing an unknown
    # state, which is the prose-rot failure it was introduced to stop.
    _check("antigravity.constant_tracks_the_verified_build_not_the_installed_one",
           _ag._VERIFIED_VERSIONS == ("1.1.1",)
           and "1.1.2" not in _ag._VERIFIED_VERSIONS,
           f"antigravity records {_ag._VERIFIED_VERSIONS} — the build whose customization "
           f"roots and plugin mcp_config channel were confirmed with live sentinel "
           f"servers — so the newer installed build warns instead of being silently "
           f"blessed", failures, verbose)


def _check_matrix_consistency(failures, verbose):
    """A matrix claims its cells are comparable. This checks that they actually were.

    "Model A beat model B" silently requires the cells to differ ONLY in the model. Three
    things can drift underneath that without failing any cell: the CLI rewriting itself
    mid-matrix (the failure that started this work — copilot went 1.0.64 → 1.0.72 in four
    days), the MCP server set changing, and a cell falling back to non-isolated.

    Reported, never enforced: the cells have already run and been paid for, and each is
    still individually valid. What is prevented is reading the DIFFERENCE between them as
    caused by the variable under test.
    """
    import io
    import os
    import shutil
    import sys
    import tempfile as _tempfile

    from . import runner as runner_mod
    from .schema import RunResult

    print("matrix consistency:")

    root = _tempfile.mkdtemp(prefix="ase-cons-")

    def _cell(version=None, argv=(), isolated=True, name="e"):
        rr = RunResult(agent="copilot", eval_name=name, prompt="", workdir="",
                       argv=list(argv))
        rr.cli_version = version
        return runner_mod.CellResult(
            agent="copilot", model="m", eval_name=name, skill=None, passed=True,
            run_result=rr, isolated=isolated)

    def _consistency(cells, agent="copilot"):
        r = runner_mod.Runner(agent, models=["m"],
                              artifacts_root=os.path.join(root, "a"),
                              run_id="c", skills_root=root)
        err = io.StringIO()
        saved, sys.stderr = sys.stderr, err
        try:
            out = r._consistency(cells)
            r._warn_inconsistent(out)
        finally:
            sys.stderr = saved
        return out, err.getvalue()

    try:
        # copilot's own spelling — the fixture must use the adapter's real flag or
        # mcp_servers_seen reads nothing and the arm passes for the wrong reason.
        disabled = ["--disable-mcp-server", "known"]
        # The motivating case: an auto-update mid-matrix. Every cell passes.
        drifted, drift_msg = _consistency([
            _cell("1.0.64", disabled), _cell("1.0.72", disabled)])
        # Uniform on all three axes.
        uniform, uniform_msg = _consistency([
            _cell("1.0.72", disabled), _cell("1.0.72", disabled)])
        # A cell that fell back to non-isolated saw skills its siblings did not.
        iso_drift, _ = _consistency([
            _cell("1.0.72", disabled), _cell("1.0.72", disabled, isolated=False)])
        # Different configurations: one cell had a server the other did not.
        srv_drift, _ = _consistency([
            _cell("1.0.72", disabled),
            _cell("1.0.72", ["--disable-mcp-server", "other"])])
        # UNKNOWN IS NOT AGREEMENT. Every codex/agy matrix looks like this, and it must
        # not report a verified-uniform version — that would be a green line standing in
        # for a check that could not run.
        unknown, unknown_msg = _consistency([_cell(None, []), _cell(None, [])], "codex")
        # One readable cell + one unreadable one is not a version CHANGE either; there is
        # simply nothing to compare against. It must not be reported as drift.
        partial, partial_msg = _consistency([_cell("1.0.72", disabled), _cell(None, disabled)])
        # Server names are arbitrary JSON object keys for copilot, so a name can contain
        # any separator. Joining the set into a string made {"a,b"} and {"a","b"} the same
        # value — two genuinely different configurations reported as consistent. The sets
        # are kept structurally for exactly this.
        ambiguous, _ = _consistency([
            _cell("1.0.72", ["--disable-mcp-server", "a,b"]),
            _cell("1.0.72", ["--disable-mcp-server", "a", "--disable-mcp-server", "b"])])
        # UNKNOWN IS NOT AGREEMENT ON *EVERY* AXIS, not just the version that motivated
        # the tri-state. Found in review: gating `verified` on the CLI version alone let a
        # matrix whose MCP axis was never read report the green primary state anyway, one
        # field away from `mcp_server_set_unknown_cells: 2`. claude reaches that state by
        # taking a --mcp-config it cannot resolve to names.
        opaque_mcp, opaque_msg = _consistency(
            [_cell("2.1.113", ["--strict-mcp-config", "--mcp-config", "x.json"]),
             _cell("2.1.113", ["--strict-mcp-config", "--mcp-config", "x.json"])], "claude")
        # ...and the other side of that rule: an adapter that can PROVE it ran no servers
        # says [] and stays verified. Without this, the stricter rule above would park
        # every claude matrix at "unverified" forever, which is just the green light's
        # useless twin.
        proven_empty, proven_msg = _consistency(
            [_cell("2.1.113", ["--strict-mcp-config"]),
             _cell("2.1.113", ["--strict-mcp-config"])], "claude")
        claude_ad = runner_mod.get_adapter("claude")
        claude_seen = (
            claude_ad.mcp_servers_seen(["--strict-mcp-config"]),
            claude_ad.mcp_servers_seen(["--strict-mcp-config", "--mcp-config", "x.json"]),
            claude_ad.mcp_servers_seen(["--strict-mcp-config", "--mcp-config=x.json"]),
            claude_ad.mcp_servers_seen(["-p", "hi"]),
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)

    _check("runner.matrix_consistency_flags_mid_matrix_drift",
           drifted["comparability"] == "drift"
           and any("1.0.64" in d and "1.0.72" in d for d in drifted["drift"])
           and "not strictly comparable" in drift_msg
           and uniform["comparability"] == "verified" and uniform_msg == ""
           and iso_drift["comparability"] == "drift"
           and any("isolation varied" in d for d in iso_drift["drift"])
           and srv_drift["comparability"] == "drift"
           and any("MCP server set varied" in d for d in srv_drift["drift"])
           and ambiguous["comparability"] == "drift"
           and ambiguous["mcp_server_sets"] == [["a", "b"], ["a,b"]],
           f"a matrix straddling a CLI auto-update is reported as not comparable even "
           f"though every cell passed ({drifted['cli_versions']}), and so is one where "
           f"isolation or the MCP server set varied; a uniform matrix says nothing at all "
           f"({uniform_msg!r}). Server sets are compared STRUCTURALLY, so a name "
           f"containing the separator ({{'a,b'}} vs {{'a','b'}}) is not collapsed into "
           f"agreement — {ambiguous['mcp_server_sets']}", failures, verbose)

    _check("runner.unknown_version_is_not_reported_as_agreement",
           unknown["comparability"] == "unverified"
           and unknown["cli_version_verified"] is False
           and unknown["cli_version_unknown_cells"] == 2
           and unknown["drift"] == [] and unknown_msg == ""
           and partial["comparability"] == "unverified"
           and partial["drift"] == [],
           f"a matrix where no cell states its version (every codex/agy run) reports "
           f"comparability={unknown['comparability']!r} — NOT a green 'verified', since "
           f"absence of evidence is not agreement, and not 'drift' either, since nothing "
           f"actually differed. The tri-state carries that in the PRIMARY field, so "
           f"automation reading it cannot mistake an uncheckable matrix for a checked "
           f"one; one readable cell beside an unreadable one is likewise unverified "
           f"({partial['cli_versions']})",
           failures, verbose)

    _check("runner.verified_requires_every_axis_known",
           opaque_mcp["comparability"] == "unverified"
           and opaque_mcp["cli_version_verified"] is True
           and opaque_mcp["mcp_server_set_verified"] is False
           and opaque_mcp["mcp_server_set_unknown_cells"] == 2
           and opaque_mcp["drift"] == [] and opaque_msg == "",
           f"a matrix whose CLI version is known and uniform but whose MCP configuration "
           f"was never read reports comparability={opaque_mcp['comparability']!r}, not "
           f"'verified'. Gating the primary state on the version axis alone rebuilt the "
           f"misleading green one field over: cli_version_verified="
           f"{opaque_mcp['cli_version_verified']} beside mcp_server_set_unknown_cells="
           f"{opaque_mcp['mcp_server_set_unknown_cells']}. Every axis must be positively "
           f"known before the matrix claims its cells were compared", failures, verbose)

    _check("runner.claude_proves_empty_mcp_set_rather_than_reporting_unknown",
           claude_seen == ([], None, None, None)
           and proven_empty["comparability"] == "verified"
           and proven_empty["mcp_server_sets"] == [[]]
           and proven_empty["mcp_server_set_unknown_cells"] == 0
           and proven_empty["mcp_server_set_verified"] is True
           and proven_msg == "",
           f"claude reports [] — not unknown — when argv PROVES no server could load "
           f"(--strict-mcp-config with no --mcp-config), so its matrices still reach "
           f"'verified' ({proven_empty['comparability']!r}) under the every-axis rule. It "
           f"falls back to unknown the moment argv stops proving it: a --mcp-config in "
           f"either spelling, or a missing --strict-mcp-config — {claude_seen}. Proving "
           f"the empty set and admitting ignorance are different claims and this is where "
           f"they part", failures, verbose)


def _check_parallel_requires_isolation(failures, verbose):
    """Concurrent cells share mutable CLI configuration — isolated or not.

    An isolated home is a symlink OVERLAY, not a copy: `isolation._overlay` wholesale-
    symlinks every entry it is not told to mask, so two isolated cells' `.codex/config.toml`
    are two paths to one real file. The first version of this guard gated on isolation,
    believing a private home made concurrency safe; review disproved it, and the arm below
    now proves the sharing directly rather than assuming either way.

    So the property that matters is per-cell MATERIALIZED config (`parallel_safe_config`),
    which no real adapter can currently claim — `--jobs>1` is therefore refused outright.
    It was an unenforced invariant before that: DEFAULT_JOBS is 1, so the harness was safe
    by accident, and `jobs` is a scenario-override key, so YAML could raise it without
    anyone passing a flag.
    """
    import os
    import shutil
    import tempfile as _tempfile

    from . import runner as runner_mod
    from .isolation import build_isolated_home

    print("parallel cells require materialized config:")

    root = _tempfile.mkdtemp(prefix="ase-par-")

    def _mk(jobs, isolated):
        return runner_mod.Runner("claude", models=["m1"],
                                 artifacts_root=os.path.join(root, "artifacts"),
                                 run_id="par", skills_root=root,
                                 jobs=jobs, isolated=isolated)

    def _run(jobs, isolated):
        """Empty spec list: run() reaches the guard before it would execute any cell, so
        this exercises the refusal without launching a CLI."""
        try:
            _mk(jobs, isolated).run([])
        except RuntimeError as exc:
            return str(exc)
        return ""

    # The load-bearing case, and the one an earlier revision of this arm got WRONG: an
    # isolated home is a symlink overlay, so parallel ISOLATED cells share every config
    # file the overlay does not explicitly mask. Proven directly below rather than asserted,
    # because the whole guard rests on it.
    shared_home = _tempfile.mkdtemp(prefix="ase-shared-")
    os.makedirs(os.path.join(shared_home, ".codex"))
    with open(os.path.join(shared_home, ".codex", "config.toml"), "w") as fh:
        fh.write("original\n")
    cells = []
    for i in range(2):
        h = _tempfile.mkdtemp(prefix=f"ase-cell{i}-")
        build_isolated_home(h, [".codex/skills"], set(), [], shared_home)
        cells.append(os.path.join(h, ".codex", "config.toml"))
    with open(cells[0], "w") as fh:                       # cell A writes...
        fh.write("[mcp_servers.sneaky]\n")
    leaked_to_sibling = "sneaky" in open(cells[1]).read()  # ...cell B sees it
    leaked_to_real = "sneaky" in open(
        os.path.join(shared_home, ".codex", "config.toml")).read()

    try:
        refused_isolated = _run(4, True)
        refused_unisolated = _run(4, False)
        # Serial must keep working in BOTH modes: --jobs 1 is the default and the whole
        # workaround, and non-isolated serial is a documented opt-out. A guard that broke
        # either would be worse than the hole it closes.
        serial_isolated = _run(1, True)
        serial_unisolated = _run(1, False)
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(shared_home, ignore_errors=True)

    _check("runner.isolated_homes_share_config_between_cells",
           leaked_to_sibling and leaked_to_real,
           f"an isolated home is a symlink OVERLAY, not a copy: a write through one cell's "
           f"overlay is visible through another's ({leaked_to_sibling}) and lands in the "
           f"real home ({leaked_to_real}). This is the fact the parallelism guard rests "
           f"on — asserted directly, because an earlier revision assumed the opposite and "
           f"gated on isolation instead of on the adapter", failures, verbose)

    _check("runner.parallel_refused_until_config_is_materialized",
           "refusing to run 4 cells in parallel" in refused_isolated
           and "--jobs 1" in refused_isolated
           and "parallel_safe_config" in refused_isolated
           and refused_unisolated != ""
           and serial_isolated == "" and serial_unisolated == "",
           f"--jobs>1 is refused for a runner whose config is not materialized per cell — "
           f"ISOLATED included, since isolation does not stop the sharing — and the message "
           f"names what would lift it. Both serial modes still run",
           failures, verbose)


def _check_codex_post_run_mcp_recheck(failures, verbose):
    """codex closes the launch window it used to leave open.

    Everything codex verified before this ran from build_argv — `_verify_all_mcp_disabled`
    is post-*enumeration*, pre-*launch*, despite the name. A server added to config between
    building argv and codex reading its config at startup was never named on the disable
    set and loaded live, with nothing checking afterwards. copilot has closed this window
    since Phase 0.

    The two halves are deliberately unequal and the arms pin that: codex's stream evidence
    is presence-only (it emits nothing when a server *starts* — verified live against a
    sentinel stdio server it launched silently), so re-enumeration carries the load.
    """
    import json as _json

    print("codex post-run MCP re-check:")

    from .adapters import codex as _cx

    opts = _cx.RunOptions()

    def _adapter(enumerated):
        """A codex adapter whose post-run enumeration reports `enumerated`."""
        class _Stub(_cx.CodexAdapter):
            def _mcp_disable_args(self, cwd=None, env=None):
                if enumerated is None:
                    raise RuntimeError("`codex mcp list --json` did not return a list")
                return [t for n in enumerated
                        for t in ("-c", f"mcp_servers.{n}.enabled=false")]
        return _Stub()

    def _run(adapter, argv, stdout=""):
        try:
            adapter.verify_post_run(argv, opts, cwd=".", stdout=stdout, exit_code=0)
        except RuntimeError as exc:
            return str(exc)
        return ""

    launched = ["-c", "mcp_servers.known.enabled=false"]

    # Nothing moved: the same server set is configured now as at launch.
    clean = _run(_adapter(["known"]), launched)
    # A server appeared in the launch window — configured now, not disabled on the argv
    # that actually ran. This is the defect the whole check exists for.
    appeared = _run(_adapter(["known", "sneaky"]), launched)
    # Enumeration itself stopped working after the run: hermeticity is no longer
    # ESTABLISHABLE, which is not the same as established, so it fails closed.
    unenumerable = _run(_adapter(None), launched)
    _check("codex.post_run_reenumeration_narrows_the_launch_window",
           clean == "" and "sneaky" in appeared and "not provably MCP-hermetic" in appeared
           and "no longer is" in unenumerable,
           f"a run whose configured server set is unchanged passes; one where a server "
           f"appeared after argv was built fails, naming it; and an enumeration that stops "
           f"working post-run fails closed rather than assuming the pre-launch check still "
           f"holds", failures, verbose)

    # --- the residual gap, asserted as a KNOWN HOLE rather than as correct behaviour ---
    # A server added after argv was built, started by codex, then removed again before this
    # check re-enumerates passes both halves whenever the model never called one of its
    # tools: the stream is silent (codex emits nothing when a server STARTS — established
    # live) and re-enumeration sees the restored config.
    #
    # An earlier revision asserted this same outcome with a comment saying the stream half
    # covered it. That is true of copilot, whose witness names every server it brought up;
    # it is false for codex, and the claim contradicted this file's own finding. Review
    # caught it. The arm is kept — the behaviour is real and worth pinning so a future
    # change to it is deliberate — but it is NAMED as a gap, so nobody reads a green
    # selftest as evidence the window is shut.
    #
    # If this ever goes red because codex grew a server-start event, that is not a
    # regression: it means the hole can be closed. Rewrite the check to use it.
    idle_server_reverted = _run(_adapter([]), launched, stdout="")
    _check("codex.KNOWN_GAP_idle_server_reverted_before_recheck_is_undetectable",
           idle_server_reverted == "",
           f"DOCUMENTED HOLE, not a passing property: a server that started during the run "
           f"and was removed before the post-run enumeration goes unreported when the model "
           f"never called it. Both halves are blind — no tool call means no stream evidence, "
           f"and codex emits nothing when a server starts, so absence of evidence is not "
           f"evidence of absence here. Closing it needs a materialized private config for "
           f"the child (see verify_post_run, DESIGN_MCP_Support.md §1); until then a green "
           f"codex run means 'no leak was detected', NOT 'no server ran'", failures, verbose)

    # --- the stream half: presence proves a leak, absence proves nothing --------------
    # Shapes taken verbatim from a live codex 0.140.0 run against a sentinel stdio server.
    leak = "\n".join([
        _json.dumps({"type": "item.started", "item": {
            "id": "item_1", "type": "mcp_tool_call", "server": "sentinel",
            "tool": "sentinel_ping", "status": "in_progress"}}),
        _json.dumps({"type": "item.completed", "item": {
            "id": "item_1", "type": "mcp_tool_call", "server": "sentinel",
            "tool": "sentinel_ping", "status": "failed",
            "error": {"message": "user cancelled MCP tool call"}}}),
    ])
    # A call the approval policy REFUSED still proves the server was live and reachable —
    # verified: this is exactly what the sentinel run produced.
    streamed = _run(_adapter(["known"]), launched, stdout=leak)
    calls = _cx._mcp_tool_calls(leak)
    # ONLY the failed completion, with no preceding item.started. The distinction is not
    # academic: the sentinel run produced exactly this outcome (the approval policy
    # cancelled the call), and a check that counted only successful calls would report a
    # clean run while a server had been live and reachable for the whole session. With
    # both events present an arm cannot tell the two rules apart — the started event
    # carries it either way — so this fixture is what makes the claim testable.
    refused_only = _cx._mcp_tool_calls(_json.dumps({
        "type": "item.completed", "item": {
            "id": "item_1", "type": "mcp_tool_call", "server": "sentinel",
            "tool": "sentinel_ping", "status": "failed",
            "error": {"message": "user cancelled MCP tool call"}}}))
    # Deduped across item.started/item.completed rather than reported twice.
    native_only = _cx._mcp_tool_calls(_json.dumps({"type": "item.completed", "item": {
        "type": "command_execution", "command": "ls"}}))
    # Malformed telemetry must not raise: this runs inside verify_post_run, where a raise
    # is reported as an MCP hermeticity failure, and a mistyped item is not one.
    raised = None
    try:
        malformed = _cx._mcp_tool_calls(
            "\n".join([_json.dumps([1, 2]), _json.dumps({"item": "not-a-dict"}),
                       "not json"]))
    except Exception as exc:  # noqa: BLE001 — the point is that nothing escapes
        raised, malformed = type(exc).__name__, None

    _check("codex.stream_reports_mcp_tool_calls_as_leaks",
           calls == ["sentinel/sentinel_ping"] and "sentinel/sentinel_ping" in streamed
           and "not MCP-hermetic" in streamed
           and refused_only == ["sentinel/sentinel_ping"]
           and native_only == [] and malformed == [] and raised is None,
           f"an mcp_tool_call in the run's own stream fails the run even though the "
           f"config re-reads clean ({calls}) — testimony a later edit cannot retract — and "
           f"a call the approval policy REFUSED still counts, since the server was live to "
           f"refuse it; native tool items are not leaks ({native_only}); malformed "
           f"telemetry yields no leak report ({malformed!r}) rather than raising "
           f"({raised!r})", failures, verbose)

    # --- the re-check must run in the CHILD's context, not the harness's --------------
    # Enumerating with the harness's own cwd/env can resolve a different server set
    # entirely (trusted-project configs, $CODEX_HOME), so a missing RunOptions is a
    # fail-closed condition rather than a silent fallback to os.environ.
    # The EXCEPTION TYPE is asserted, not just the message. Removing the guard yields an
    # AttributeError whose text also contains "effective_env", so a message-only assertion
    # passes on the broken code — the arm would then be pinning the crash rather than the
    # deliberate refusal. Every caller of verify_post_run treats RuntimeError as "this run
    # is not provably hermetic"; an AttributeError is an unhandled bug.
    no_opts_type, no_opts = "", ""
    try:
        _adapter(["known"]).verify_post_run(launched, None, cwd=".", stdout="")
    except Exception as exc:  # noqa: BLE001 — the type is exactly what is under test
        no_opts_type, no_opts = type(exc).__name__, str(exc)
    _check("codex.post_run_recheck_requires_the_childs_context",
           no_opts_type == "RuntimeError" and "effective_env" in no_opts,
           f"re-checking without the RunOptions the invocation was built from fails "
           f"closed with a deliberate RuntimeError ({no_opts_type}), rather than quietly "
           f"enumerating against the harness's environment — which resolves a different "
           f"set of servers than the run saw — or crashing with an AttributeError that "
           f"no caller is prepared to read as a hermeticity failure", failures, verbose)

    # --- argv spellings the comparison depends on ------------------------------------
    # Under-counting what argv disabled makes a correctly-disabled server read as "new",
    # failing hermetic runs. codex accepts all four spellings (verified 0.140.0), so all
    # four have to be read back.
    spellings = [
        _cx._disabled_server_names(["-c", "mcp_servers.a.enabled=false"]),
        _cx._disabled_server_names(["-cmcp_servers.a.enabled=false"]),
        _cx._disabled_server_names(["--config", "mcp_servers.a.enabled=false"]),
        _cx._disabled_server_names(["--config=mcp_servers.a.enabled=false"]),
    ]
    quoted = _cx._disabled_server_names(["-c", 'mcp_servers."odd name".enabled=false'])
    unrelated = _cx._disabled_server_names(
        ["-c", 'model_reasoning_effort="low"', "-c", "memories.use_memories=false"])
    _check("codex.disable_argv_is_read_back_in_every_spelling",
           all(s == {"a"} for s in spellings) and quoted == {"odd name"}
           and unrelated == set(),
           f"all four `-c`/`--config` spellings codex accepts are read back "
           f"({[sorted(s) for s in spellings]}), including the quoted-key form "
           f"({sorted(quoted)}); unrelated overrides contribute nothing "
           f"({sorted(unrelated)}). Under-counting here would fail hermetic runs by "
           f"reading a disabled server as newly appeared", failures, verbose)


def _check_copilot_version_provenance(failures, verbose):
    """Version drift is MESSAGED, never gated on — and the version is read from the run.

    The adapter cannot gate on a version learned by executing the CLI: a preflight
    `--version` resolves its app.js through writable caches the real argv bypasses, so the
    two can execute different code. But the version that ACTUALLY ran is recoverable from
    the child's own stream, which is the same evidence class as the MCP witness. These
    arms pin that, the three severity tiers, and the bundle audit that clears a new build.
    """
    import io
    import json as _json
    import os
    import shutil
    import sys
    import tempfile

    from .adapters import copilot as _cop

    print("copilot version provenance:")

    def _skills(*entries):
        """entries are (source, path); the real event carries both — verified against a
        captured 1.0.72 run, which lists builtin and project skills side by side."""
        return _json.dumps({"type": "session.skills_loaded", "ephemeral": True,
                            "data": {"skills": [{"name": "s", "source": s, "path": p}
                                                for s, p in entries]}})

    app = "/Users/x/Library/Caches/copilot/pkg/darwin-arm64/1.0.72/builtin/s/SKILL.md"
    # A model can write anything it likes into its own message; the version must not be
    # readable from there, or a model could silence the drift warning by describing a
    # verified version. Only builtin session.skills_loaded paths count.
    forged = _json.dumps({"type": "assistant.message",
                          "data": {"content": "running pkg/darwin-arm64/9.9.9/app.js"}})
    ver_real = _cop._stream_cli_version(_skills(("builtin", app)))
    ver_forged_only = _cop._stream_cli_version(forged)
    ver_both = _cop._stream_cli_version(_skills(("builtin", app)) + "\n" + forged)
    ver_none = _cop._stream_cli_version("")
    # disagreeing app roots => unknown, not a coin flip
    ver_ambig = _cop._stream_cli_version(_skills(
        ("builtin", app), ("builtin", "/c/pkg/darwin-arm64/1.0.64/builtin/s/SKILL.md")))
    # ...but a PROJECT skill path is workspace-controlled (verified live: they arrive as
    # <workspace>/.agents/skills/<name>/SKILL.md). A repo laid out to look like an app
    # root would otherwise force that same "disagreement" and resolve to None — silently
    # disarming the denylist from inside the workspace under test.
    ver_project = _cop._stream_cli_version(_skills(
        ("builtin", app), ("project", "/ws/.agents/skills/pkg/darwin-arm64/9.9.9/S.md")))
    # a prerelease build is REPORTED as itself (and so warns), not dropped as unreadable
    ver_pre = _cop._stream_cli_version(_skills(
        ("builtin", "/c/pkg/darwin-arm64/1.0.73-beta.1/builtin/s/SKILL.md")))
    # A version-shaped `pkg/<x>/<v>/` component can appear on EITHER SIDE of the real app
    # root, and the two want opposite tiebreaks — which is why the match is anchored on
    # the `builtin/` the CLI's own layout guarantees, rather than tiebroken at all.
    #   ABOVE it: a cache root is an ordinary directory ($COPILOT_CACHE_HOME is
    #   caller-supplied), so one can be placed at a version-shaped path. Taking the FIRST
    #   match then reports the outer decoy — a 6.6.6 build reporting itself as a VERIFIED
    #   1.0.72, silently, which is the whole tier defeated.
    ver_decoy_above = _cop._stream_cli_version(_skills(
        ("builtin", "/x/pkg/darwin-arm64/1.0.72/c/pkg/darwin-arm64/6.6.6/builtin/s/S.md")))
    #   BELOW it: a built-in skill's own directory can be named to the same shape. Taking
    #   the LAST match reports that instead — the mirror-image failure, and the reason
    #   "just take the deepest one" is not the fix it looks like.
    ver_decoy_below = _cop._stream_cli_version(_skills(
        ("builtin", "/c/pkg/darwin-arm64/1.0.72/builtin/pkg/darwin-arm64/9.9.9/S.md")))
    # malformed telemetry is an unknown version, NOT an exception: this runs inside
    # verify_post_run, where anything raised is reported as an MCP hermeticity failure.
    malformed = "\n".join([
        _json.dumps({"type": "session.skills_loaded", "data": {"skills": 5}}),
        _json.dumps({"type": "session.skills_loaded", "data": {"skills": ["x"]}}),
        _json.dumps({"type": "session.skills_loaded", "data": "not-an-object"}),
        _json.dumps(["not", "an", "object"]),
    ])
    try:
        ver_malformed, raised_malformed = _cop._stream_cli_version(malformed), None
    except Exception as exc:            # noqa: BLE001 — not raising IS the assertion
        ver_malformed, raised_malformed = None, exc
    _check("copilot.version_read_from_the_run_not_a_probe",
           ver_real == "1.0.72" and ver_forged_only is None and ver_both == "1.0.72"
           and ver_none is None and ver_ambig is None and ver_project == "1.0.72"
           and ver_pre == "1.0.73-beta.1"
           and ver_decoy_above == "6.6.6" and ver_decoy_below == "1.0.72"
           and ver_malformed is None and raised_malformed is None,
           f"the executing version is recovered from the child's own BUILTIN app-root "
           f"paths (same evidence class as the MCP witness, no second execution); model "
           f"prose cannot forge it, a workspace-controlled project skill path cannot "
           f"make it ambiguous, a prerelease reports as itself, a version-shaped "
           f"directory on EITHER side of the app root cannot stand in for it, and "
           f"malformed telemetry is an unknown version rather than a raised hermeticity "
           f"failure: real={ver_real!r} forged_only={ver_forged_only!r} "
           f"real+forged={ver_both!r} empty={ver_none!r} ambiguous={ver_ambig!r} "
           f"with_project_path={ver_project!r} prerelease={ver_pre!r} "
           f"decoy_above={ver_decoy_above!r} decoy_below={ver_decoy_below!r} "
           f"malformed={ver_malformed!r} raised={raised_malformed!r}", failures, verbose)

    # --- the three tiers ---------------------------------------------------------
    saved_denied = dict(_cop._DENIED_VERSIONS)
    saved_warned = set(_cop._WARNED_VERSIONS)
    saved_err = sys.stderr
    try:
        _cop._DENIED_VERSIONS.clear()
        _cop._DENIED_VERSIONS["6.6.6"] = "masks plugins incorrectly"
        denied = ""
        try:
            _cop._check_cli_version_denied("6.6.6")
        except RuntimeError as exc:
            denied = str(exc)
        # a verified version is neither denied nor warned about
        _cop._WARNED_VERSIONS.clear()
        sys.stderr = io.StringIO()
        _cop._check_cli_version_denied(_cop._VERIFIED_VERSIONS[0])
        _cop._warn_cli_version_drift(_cop._VERIFIED_VERSIONS[0], witnessed=True)
        quiet = sys.stderr.getvalue()
        # an unknown version warns ONCE per process, however many cells run
        sys.stderr = io.StringIO()
        _cop._warn_cli_version_drift("9.9.9", witnessed=True)
        _cop._warn_cli_version_drift("9.9.9", witnessed=True)
        warned = sys.stderr.getvalue()
        # an undeterminable version warns too, rather than passing silently
        sys.stderr = io.StringIO()
        _cop._warn_cli_version_drift(None, witnessed=True)
        warned_unknown = sys.stderr.getvalue()
        # A run that never completed is EXCUSED from producing a witness, so it reaches
        # this warning with no MCP evidence at all. Claiming "the runtime witness held"
        # there would be the notice inventing the very check it is warning about.
        sys.stderr = io.StringIO()
        _cop._WARNED_VERSIONS.clear()
        _cop._warn_cli_version_drift("9.9.9", witnessed=False)
        unwitnessed = sys.stderr.getvalue()
    finally:
        sys.stderr = saved_err
        _cop._DENIED_VERSIONS.clear()
        _cop._DENIED_VERSIONS.update(saved_denied)
        _cop._WARNED_VERSIONS.clear()
        _cop._WARNED_VERSIONS.update(saved_warned)

    _check("copilot.version_tiers",
           "denylist" in denied and "AFTER the fact" in denied and quiet == ""
           and warned.count("warning:") == 1 and "9.9.9" in warned
           and "ADDED" in warned and "verify-copilot-channels" in warned
           and "witness held" in warned
           and warned_unknown.count("warning:") == 1
           and "witness held" not in unwitnessed
           and "no MCP witness at all" in unwitnessed,
           f"a build known to be broken fails closed by name (the one tier the runtime "
           f"contract cannot reach — such a defect leaves the witness intact); a verified "
           f"build is silent; an unknown or undeterminable one warns ONCE per process and "
           f"says plainly that a passing run does not cover a channel the build ADDED — "
           f"claims the witness held only when one was actually produced, and calls the "
           f"denial what it is — detection after the CLI has already run, since the "
           f"version is only readable from the run's own output: "
           f"denied={denied[:50]!r} quiet={quiet!r} warns={warned.count('warning:')} "
           f"unknown_warns={warned_unknown.count('warning:')} "
           f"unwitnessed_claims_witness={'witness held' in unwitnessed}",
           failures, verbose)

    # --- one stray stdout line must not masquerade as a hermeticity failure -------
    # iter_jsonl yields any well-formed JSON VALUE; a bare `42` on its own line has no
    # .get(). Raising on it inside verify_post_run is reported as "MCP hermeticity was
    # not confirmed", so a single junk line would bury a perfectly good witness later in
    # the same stream — and, being indistinguishable from a real leak, would be acted on
    # as one. Skipping non-objects is not leniency: the witness itself is still required.
    junk = "\n".join(["42", "[1, 2]", '"a string"', "null"])
    witness_line = _json.dumps({"type": "session.mcp_servers_loaded", "data": {"servers": [
        {"name": _cop._WITNESS_SENTINEL, "status": "disabled"}]}})
    try:
        junk_witness, junk_raised = _cop._mcp_witness(junk + "\n" + witness_line, 0), None
    except Exception as exc:            # noqa: BLE001 — not raising IS the assertion
        junk_witness, junk_raised = None, exc
    try:
        junk_parsed = _cop.CopilotAdapter().parse(junk + "\n" + witness_line, "", 0)
        parse_raised = None
    except Exception as exc:            # noqa: BLE001 — not raising IS the assertion
        junk_parsed, parse_raised = None, exc
    # and the contract is still enforced: junk ALONE is a run with no witness at all
    junk_only, _live_only, _w_only = _cop._mcp_witness(junk, 0)
    _check("copilot.malformed_telemetry_is_not_a_leak_report",
           junk_raised is None and junk_witness == (None, [], True)
           and parse_raised is None and junk_parsed is not None
           and junk_only is not None,
           f"a non-object JSON line is skipped by both the witness reader and the "
           f"parser rather than raising — an AttributeError there is announced as an MCP "
           f"hermeticity failure, which would let one junk line both hide a valid witness "
           f"and impersonate a leak — while a stream of NOTHING BUT junk still fails the "
           f"witness contract: witness={junk_witness!r} witness_raised={junk_raised!r} "
           f"parse_raised={parse_raised!r} junk_only_violation={junk_only!r}",
           failures, verbose)

    # --- the build the run executes must not be redirectable by the environment ---
    # Found while re-reading 1.0.72's loader to check the --no-auto-update claim:
    # COPILOT_CLI_DIST_DIR is consulted BEFORE any argv, imports `<value>/index.js`
    # directly, and is subject to no version floor and no cache-root constraint. An
    # ambient value would run arbitrary code as the agent and make the provenance reading
    # describe a build nobody chose.
    _opts = _cop.RunOptions(model="m")
    redirect_env = _cop.CopilotAdapter().env(
        {"COPILOT_CLI_DIST_DIR": "/tmp/evil", "PATH": "/usr/bin"}, _opts)
    _check("copilot.build_redirect_env_is_cleared",
           "COPILOT_CLI_DIST_DIR" not in redirect_env and redirect_env.get("PATH"),
           f"COPILOT_CLI_DIST_DIR is stripped from every copilot launch, isolated or not "
           f"— it overrides --no-auto-update entirely and points the loader at an "
           f"arbitrary index.js, which no MCP flag on the argv would then apply to: "
           f"kept={sorted(redirect_env)}", failures, verbose)

    # --- the bundle audit that clears a new build --------------------------------
    tmp = tempfile.mkdtemp(prefix="ase-bundle-")
    try:
        markers = [m for m, _why in _cop._MCP_CHANNEL_MARKERS]
        root = os.path.join(tmp, "pkg", "darwin-arm64")
        # Every one of these is a directory the CLI's own loader treats as a candidate:
        # it keeps whatever has a readable app.js, with NO name test, and orders by a
        # PREFIX parse (read out of 1.0.72's index.js). So `1.0.73foo` and `1.0.73-`
        # parse as 1.0.73 and can outrank the running build, and `nonsense` is selectable
        # by exact name through --prefer-version. A tidier semver filter here skipped all
        # three — saying nothing about a bundle that can execute, which in an audit reads
        # exactly like having cleared it.
        for ver, body in (("1.0.72", " ".join(markers)),
                          ("2.0.0", " ".join(markers[:-1])),   # one channel dropped
                          ("1.0.73-beta.1", " ".join(markers)),
                          ("1.0.73foo", " ".join(markers)),
                          ("1.0.73-", " ".join(markers)),
                          ("nonsense", " ".join(markers))):
            os.makedirs(os.path.join(root, ver))
            with open(os.path.join(root, ver, "app.js"), "w") as f:
                f.write(body)
        found = _cop.find_cli_bundles({"COPILOT_CACHE_HOME": tmp, "COPILOT_HOME": tmp})
        # A root the audit does not scan is a bundle it says nothing about, which is
        # indistinguishable from one it cleared. XDG_CACHE_HOME is not a synonym for
        # ~/.cache — where it points elsewhere, ~/.cache/copilot is the wrong directory.
        roots_xdg = _cop._bundle_search_roots({"XDG_CACHE_HOME": "/xdg"})
        roots_win = _cop._bundle_search_roots({"LOCALAPPDATA": "C:\\Users\\u\\AppData"})
        covers_xdg = os.path.join("/xdg", "copilot", "pkg") in roots_xdg
        covers_localappdata = any("AppData" in r for r in roots_win)
        # The real per-platform cache roots are always scanned too (that is the point of
        # the command), so this host's own bundles legitimately show up — restrict to the
        # fixture to keep the assertion independent of what is installed here.
        versions = [v for v, p in found if p.startswith(tmp)]
        full = _cop.audit_channel_markers(os.path.join(root, "1.0.72", "app.js"))
        gapped = _cop.audit_channel_markers(os.path.join(root, "2.0.0", "app.js"))
        missing = sorted(m for m, ok in gapped.items() if not ok)
        # a marker straddling the 1 MiB read boundary must still be found
        straddle = os.path.join(tmp, "straddle.js")
        pad = (1 << 20) - (len(markers[0]) // 2)
        with open(straddle, "w") as f:
            f.write("." * pad + " ".join(markers))
        straddled = _cop.audit_channel_markers(straddle)
        # Ordered by the loader's own comparator, not by semver: an unparseable name sorts
        # below everything, `-` anywhere means prerelease, and the raw name breaks ties.
        expect = ["nonsense", "1.0.72", "1.0.73-", "1.0.73-beta.1", "1.0.73foo", "2.0.0"]
        _check("copilot.channel_bundle_audit",
               versions == expect and all(full.values())
               and missing == [markers[-1]] and all(straddled.values())
               and covers_xdg and covers_localappdata,
               f"the audit discovers every bundle the loader could run — using the "
               f"loader's candidate rule (readable app.js, no name test) and its ordering "
               f"(prefix parse, prerelease before its release, unparseable last), across "
               f"the XDG and Windows cache roots, since a bundle or a root the audit "
               f"skips reads the same as one it cleared — reports every channel marker "
               f"present for an intact build, names the one a build DROPPED (the "
               f"silent-degradation case it exists for), and finds a marker straddling "
               f"the read-chunk boundary: versions={versions} "
               f"all_present={all(full.values())} missing={missing} "
               f"straddle_ok={all(straddled.values())} xdg={covers_xdg} "
               f"localappdata={covers_localappdata}", failures, verbose)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _check_exec_early_exit_group_kill(failures, verbose):
    """The timeout kill must still reach the group when the LEADER has already exited.

    The check above keeps the parent alive for the whole timeout, so the group is always
    reachable through the live leader — and it therefore passed while this shape was
    broken. The shape that matters is the real one: the CLI starts an MCP server, exits,
    and the server inherits and holds the captured stdout pipe. ``communicate()`` blocks
    on that pipe (not on the process), reaps the leader via its own ``poll()``, and times
    out — at which point deriving the group from ``os.getpgid(proc.pid)`` raises
    ``ProcessLookupError`` and the kill silently degrades to a no-op on a dead pid.

    Measured before the fix: a 1s timeout returned after 10.1s, ``timed_out=True``, and
    the grandchild ran to completion and wrote its marker. The group id is now captured at
    launch (``_ProcessTree``), so both halves hold — prompt return AND a dead grandchild.
    """
    import os
    import shutil
    import sys
    import tempfile
    import time

    from .exec import run_captured

    if not hasattr(os, "killpg"):
        if verbose:
            print("  [skipped — no process groups on this platform]")
        return

    tmp = tempfile.mkdtemp(prefix="ase-earlyexit-")
    pidfile = os.path.join(tmp, "grandchild.pid")
    # The grandchild INHERITS the pipe (no DEVNULL — that is what makes communicate()
    # block on it) and outlives the parent, which records its pid and exits at once.
    # Liveness is checked from that pid rather than from a marker file the grandchild
    # writes after sleeping: such a marker is absent either way inside the window, so it
    # asserted nothing, and only the elapsed-time half of this arm ever had teeth.
    child = (
        "import subprocess, sys\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        f"open({pidfile!r}, 'w').write(str(p.pid))\n"
        "sys.exit(0)\n"
    )
    grandchild_pid = None
    try:
        start = time.monotonic()
        _out, _err, code, timed_out = run_captured(
            [sys.executable, "-c", child], cwd=tmp, env=dict(os.environ), timeout=1)
        elapsed = time.monotonic() - start
        try:
            grandchild_pid = int(open(pidfile).read().strip())
        except (OSError, ValueError):
            grandchild_pid = None
        alive = None
        if grandchild_pid:
            for _ in range(40):
                try:
                    os.kill(grandchild_pid, 0)
                    alive = True
                except ProcessLookupError:
                    alive = False
                    break
                time.sleep(0.05)
        _check("exec.timeout_kills_group_after_leader_exits",
               timed_out and code == -9 and elapsed < 5
               and grandchild_pid is not None and alive is False,
               f"a parent that exits while a grandchild holds the captured pipe open is "
               f"still reaped as a GROUP: the timeout returns promptly instead of "
               f"blocking on the surviving pipe, and the grandchild does not outlive it "
               f"(elapsed={elapsed:.2f}s timed_out={timed_out} code={code} "
               f"grandchild={grandchild_pid} alive_after={alive})", failures, verbose)
    finally:
        if grandchild_pid:
            try:
                os.kill(grandchild_pid, 9)
            except (ProcessLookupError, PermissionError):
                pass
        shutil.rmtree(tmp, ignore_errors=True)


def _check_exec_normal_exit_group_sweep(failures, verbose):
    """A SUCCESSFUL run must not orphan descendants either.

    Both checks above hang the leak off a timeout, so both passed while the ordinary case
    leaked. Nothing about the shape needs a timeout: a CLI that starts an MCP server,
    points its stdio somewhere other than the captured pipes, and exits 0 has nothing left
    to block ``communicate()`` — it returns at once, reports success, and the server keeps
    running. The pipes are what made the timeout version visible at all.

    Measured before the fix: ``run_captured`` returned in 0.03s with exit code 0 while the
    grandchild ran on to write its marker. ``_ProcessTree.close`` now sweeps the group on
    every return, so the same run still returns in 0.03s and the marker never appears.
    """
    import os
    import shutil
    import sys
    import tempfile
    import time

    from .exec import run_captured

    if not hasattr(os, "killpg"):
        if verbose:
            print("  [skipped — no process groups on this platform]")
        return

    tmp = tempfile.mkdtemp(prefix="ase-sweep-")
    pidfile = os.path.join(tmp, "grandchild.pid")
    # The PARENT records the pid, before it exits — a grandchild that writes its own
    # would lose the race against the very SIGKILL under test and leave nothing to check.
    # Liveness is then read from the pid directly: an earlier version of this arm waited
    # for a marker file the grandchild only writes after sleeping, which reads "dead"
    # whether or not it was killed, and passed against the unfixed code.
    child = (
        "import subprocess, sys\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],\n"
        "                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        f"open({pidfile!r}, 'w').write(str(p.pid))\n"
        "sys.exit(0)\n"
    )
    grandchild_pid = None
    try:
        start = time.monotonic()
        _out, _err, code, timed_out = run_captured(
            [sys.executable, "-c", child], cwd=tmp, env=dict(os.environ), timeout=30)
        elapsed = time.monotonic() - start
        try:
            grandchild_pid = int(open(pidfile).read().strip())
        except (OSError, ValueError):
            grandchild_pid = None
        alive = None
        if grandchild_pid:
            for _ in range(40):         # give SIGKILL a moment to be delivered/reaped
                try:
                    os.kill(grandchild_pid, 0)
                    alive = True
                except ProcessLookupError:
                    alive = False
                    break
                time.sleep(0.05)
        _check("exec.normal_exit_sweeps_the_group",
               code == 0 and not timed_out and elapsed < 5
               and grandchild_pid is not None and alive is False,
               f"a clean exit is evidence the AGENT finished, not its descendants: the "
               f"group is swept on every return, so a grandchild that detached its stdio "
               f"and outlived a successful run is still reaped (elapsed={elapsed:.2f}s "
               f"code={code} grandchild={grandchild_pid} alive_after={alive})",
               failures, verbose)
    finally:
        if grandchild_pid:
            try:
                os.kill(grandchild_pid, 9)   # never leak a sleeper if the check failed
            except (ProcessLookupError, PermissionError):
                pass
        shutil.rmtree(tmp, ignore_errors=True)


def _check_exec_process_tree_handles(failures, verbose):
    """The Windows job-object path and the spawn/post-spawn error split.

    Neither can be exercised by running a real process on a POSIX host, so both are
    driven the way copilot's ODR registry read is: against a stub injected at the one
    seam the production code calls through (``_win32_kernel32``). This pins the CALL
    SEQUENCE and the failure handling; it is not a substitute for running on Windows,
    which was not possible here (see ``_win32_assign_job``).
    """
    import sys

    from . import exec as exec_mod
    from .adapters.base import Adapter, ParseOutput
    from .exec import ChildSpawned

    calls: list = []

    class _FakeFn:
        def __init__(self, name, ret):
            self.name, self.ret = name, ret
            self.restype = self.argtypes = None

        def __call__(self, *args):
            calls.append((self.name, args))
            return self.ret

    class _IsProcessInJob(_FakeFn):
        """IsProcessInJob reports through an out-parameter, so the stub has to write it —
        the production code reads membership from there, not from the return value."""

        def __init__(self, name, ret, member):
            super().__init__(name, ret)
            self.member = member

        def __call__(self, *args):
            args[2]._obj.value = self.member
            return super().__call__(*args)

    class _FakeK32:
        def __init__(self, rets, member=1):
            self._rets = rets
            self._member = member
            self._fns: dict = {}

        def __getattr__(self, name):
            if name not in self._fns:
                ret = self._rets.get(name, 1)
                self._fns[name] = (
                    _IsProcessInJob(name, ret, self._member)
                    if name == "IsProcessInJob" else _FakeFn(name, ret))
            return self._fns[name]

    class _FakeProc:
        _handle = 7
        pid = -12345            # never used: _HAS_KILLPG is forced off for these arms
        killed = False

        def kill(self):
            self.killed = True

    saved_k32 = exec_mod._win32_kernel32
    try:
        # --- the happy path: create, limit, assign, VERIFY membership ------------
        exec_mod._win32_kernel32 = lambda: _FakeK32({"CreateJobObjectW": 4242})
        job = exec_mod._win32_assign_job(_FakeProc())
        names = [c[0] for c in calls]
        set_info = next((c for c in calls if c[0] == "SetInformationJobObject"), None)
        assigned = job == 4242 and names == [
            "CreateJobObjectW", "SetInformationJobObject", "AssignProcessToJobObject",
            "IsProcessInJob"]
        # the info class must be the EXTENDED one, or the kill-on-close flag is ignored
        info_class_ok = set_info is not None and set_info[1][1] == 9
        limits = exec_mod._kill_on_close_limits()
        flag_ok = limits.BasicLimitInformation.LimitFlags == 0x2000

        # --- every step that can fail must fail CLOSED, and close its handle -----
        # Including SetInformationJobObject: kill-on-close is what kills the tree when
        # the harness dies before it can terminate the job, so a job that will not accept
        # it does not contain the tree under the very failure it is there for.
        # Including IsProcessInJob returning "not a member": that is how an assignment
        # that "succeeded" against an already-exited process is caught — the shape that
        # made the POSIX path fail open.
        failure_modes = {
            "create": ({"CreateJobObjectW": 0}, 1),
            "limit": ({"CreateJobObjectW": 4242, "SetInformationJobObject": 0}, 1),
            "assign": ({"CreateJobObjectW": 4242, "AssignProcessToJobObject": 0}, 1),
            "verify_call": ({"CreateJobObjectW": 4242, "IsProcessInJob": 0}, 1),
            "not_a_member": ({"CreateJobObjectW": 4242}, 0),
        }
        fail_closed, leaked, unswept = [], [], []
        for label, (rets, member) in failure_modes.items():
            calls.clear()
            exec_mod._win32_kernel32 = lambda r=rets, m=member: _FakeK32(r, member=m)
            if exec_mod._win32_assign_job(_FakeProc()) is not None:
                fail_closed.append(label)
            names = [c[0] for c in calls]
            # a job that was created but abandoned must not leak its handle
            if label != "create" and "CloseHandle" not in names:
                leaked.append(label)
            # ...and must be EMPTIED before it is released, because a failed setup does
            # not mean an empty job: `assign` may have succeeded and only the membership
            # re-read failed, and the `limit` case is worst — the step that failed IS
            # kill-on-close, so closing the handle kills nothing. Terminate, then close.
            if label != "create" and names[-2:] != ["TerminateJobObject", "CloseHandle"]:
                unswept.append(label)

        # --- kill() terminates the JOB, not just the direct child ----------------
        calls.clear()
        exec_mod._win32_kernel32 = lambda: _FakeK32({})
        proc = _FakeProc()
        saved_killpg, exec_mod._HAS_KILLPG = exec_mod._HAS_KILLPG, False
        try:
            tree = exec_mod._ProcessTree(proc)
            tree._job = 4242            # as if _win32_assign_job had succeeded
            tree.kill()
            job_killed = ([c[0] for c in calls] == ["TerminateJobObject"]
                          and not proc.killed)
            # a job that refuses to terminate must not swallow the other kills: false is
            # a failure, not a completed terminate.
            calls.clear()
            exec_mod._win32_kernel32 = lambda: _FakeK32({"TerminateJobObject": 0})
            stubborn = _FakeProc()
            tree = exec_mod._ProcessTree(stubborn)
            tree._job = 4242
            tree.kill()
            terminate_checked = stubborn.killed
        finally:
            exec_mod._HAS_KILLPG = saved_killpg

        # --- the sweep on a normal return TERMINATES; it does not rely on the close ----
        # Kill-on-close only fires if CloseHandle succeeds, so a sweep resting on it does
        # nothing in exactly the case it would have to survive — and close() would then
        # return normally with the whole tree alive. Terminate first, check the close, and
        # fail the run if either half did not happen.
        swept, sweep_err = [], []
        saved_killpg3, exec_mod._HAS_KILLPG = exec_mod._HAS_KILLPG, False
        try:            # _FakeProc's pid is a placeholder; keep it away from a real killpg
            for label, rets in (("ok", {}),
                                ("terminate_fails", {"TerminateJobObject": 0}),
                                ("close_fails", {"CloseHandle": 0})):
                calls.clear()
                exec_mod._win32_kernel32 = lambda r=rets: _FakeK32(r)
                tree = exec_mod._ProcessTree(_FakeProc())
                tree._job = 4242
                raised = None
                try:
                    tree.close()
                except RuntimeError as exc:
                    raised = exc
                swept.append((label, [c[0] for c in calls], raised is None))
                if label == "ok":
                    sweep_err.append(swept[-1][1] == ["TerminateJobObject", "CloseHandle"]
                                     and raised is None)
                else:
                    # the close is attempted even when the terminate failed, so neither
                    # failure can leave the handle behind on top of the live tree
                    sweep_err.append(raised is not None
                                     and swept[-1][1] == ["TerminateJobObject",
                                                          "CloseHandle"])
        finally:
            exec_mod._HAS_KILLPG = saved_killpg3
        sweep_ok = all(sweep_err)

        _check("exec.win32_normal_return_sweeps_the_job",
               sweep_ok,
               f"close() terminates the job explicitly and then checks the handle "
               f"release, rather than leaning on kill-on-close — a close that fails is "
               f"a kill that never happened, and either failure fails the run instead of "
               f"returning a result the surviving tree could have influenced: "
               f"{[(lbl, seq, 'clean' if ok else 'raised') for lbl, seq, ok in swept]}",
               failures, verbose)

        _check("exec.win32_job_object_kills_the_tree",
               assigned and info_class_ok and flag_ok and not fail_closed
               and not leaked and not unswept and job_killed and terminate_checked,
               f"the Windows path puts the child in a Job Object (so a grandchild whose "
               f"parent already exited is still terminated — no process groups there, and "
               f"taskkill /T walks links an exited parent no longer has), requires the "
               f"kill-on-close limit, re-reads membership from the kernel, abandons the "
               f"job on ANY failed step rather than running uncontained, EMPTIES it "
               f"before releasing it (a failed setup does not mean an empty job, and the "
               f"limit failure is precisely the loss of kill-on-close), and treats a "
               f"false TerminateJobObject as a failure rather than a kill: "
               f"assigned={assigned} info_class={info_class_ok} flag={flag_ok} "
               f"failed_open={fail_closed or 'none'} leaked_handles={leaked or 'none'} "
               f"abandoned_unswept={unswept or 'none'} "
               f"job_killed={job_killed} terminate_checked={terminate_checked}",
               failures, verbose)

        # --- an uncontained tree fails the RUN, rather than running weakly -------
        # Killing only the direct child is what let a leaked MCP server outlive its own
        # run while the run reported success. If the tree cannot be contained the launch
        # is a post-spawn failure: a child did start, so it is still parsed and audited.
        shim_u = type(exec_mod)("subprocess_shim_uncontained")
        shim_u.PIPE, shim_u.DEVNULL = exec_mod.subprocess.PIPE, exec_mod.subprocess.DEVNULL
        shim_u.TimeoutExpired = exec_mod.subprocess.TimeoutExpired
        uncontained_proc = _FakeProc()

        class _NeverCommunicates:
            def __init__(self, *a, **k):
                self.pid, self.returncode = uncontained_proc.pid, None

            def communicate(self, timeout=None):
                raise AssertionError("an uncontained launch must not be waited on")

            def kill(self):
                uncontained_proc.killed = True

        shim_u.Popen = _NeverCommunicates
        saved_sub2, exec_mod.subprocess = exec_mod.subprocess, shim_u
        saved_killpg2, exec_mod._HAS_KILLPG = exec_mod._HAS_KILLPG, False
        try:                        # no killpg and no job => nothing contains the tree
            exec_mod._win32_kernel32 = lambda: _FakeK32({"CreateJobObjectW": 0})
            uncontained_err = None
            try:
                exec_mod.run_captured(["x"], cwd=".", env={}, timeout=1)
            except BaseException as exc:     # noqa: BLE001 — the type IS the assertion
                uncontained_err = exc
        finally:
            exec_mod.subprocess = saved_sub2
            exec_mod._HAS_KILLPG = saved_killpg2
        _check("exec.uncontained_launch_fails_closed",
               isinstance(uncontained_err, ChildSpawned)
               and "cannot be contained" in str(uncontained_err)
               and uncontained_proc.killed,
               f"a launch whose tree cannot be contained fails the run instead of "
               f"proceeding with direct-child-only cleanup, and the child it did start "
               f"is killed and audited (raised={type(uncontained_err).__name__} "
               f"child_killed={uncontained_proc.killed})", failures, verbose)

        # --- Windows is uncontainable, so nothing is LAUNCHED there ---------------
        # The job can only be assigned once CreateProcess has returned, and Windows
        # associates a member's FUTURE children only. A grandchild spawned inside that
        # window is therefore never in the job and survives TerminateJobObject — and "the
        # agent immediately starts an MCP server" needs no unlucky timing to land there.
        #
        # So the refusal has to come BEFORE the spawn. An earlier revision of this fix
        # rejected win32 just after Popen and was rejected in review for exactly the right
        # reason: by then the child exists, and the launch-window grandchild it is meant
        # to prevent has already had its chance. Spawning and then killing is not a
        # refusal. The assertion is therefore that Popen is NEVER CONSTRUCTED — hence a
        # shim whose only behaviour is to fail the arm if anyone calls it.
        popen_calls = []

        class _MustNotSpawn:
            def __init__(self, *a, **k):
                popen_calls.append(a)
                raise AssertionError("run_captured must not spawn on an unsupported "
                                     "platform")

        shim_w = type(exec_mod)("subprocess_shim_win32")
        shim_w.PIPE, shim_w.DEVNULL = exec_mod.subprocess.PIPE, exec_mod.subprocess.DEVNULL
        shim_w.TimeoutExpired = exec_mod.subprocess.TimeoutExpired
        shim_w.Popen = _MustNotSpawn
        fake_sys = type(exec_mod)("sys_shim")
        fake_sys.platform = "win32"      # exec.py reads sys ONLY for .platform
        saved_sys, exec_mod.sys = exec_mod.sys, fake_sys
        saved_sub3, exec_mod.subprocess = exec_mod.subprocess, shim_w
        try:
            win_err = None
            try:
                exec_mod.run_captured(["x"], cwd=".", env={}, timeout=1)
            except BaseException as exc:     # noqa: BLE001 — the type IS the assertion
                win_err = exc
            # the BACKSTOP, exercised directly: even handed a job that assigned cleanly
            # AND a working process group, a tree on win32 must not report containment,
            # so no future caller can inherit a guarantee that was never true here.
            exec_mod._win32_kernel32 = lambda: _FakeK32({"CreateJobObjectW": 4242})
            backstop = exec_mod._ProcessTree(_FakeProc())
            backstop_contained = backstop.contained
        finally:
            exec_mod.sys = saved_sys
            exec_mod.subprocess = saved_sub3
        # An OSError subclass, so every existing caller's "could not start at all" path
        # handles it fail-closed without having to learn a new type first.
        _check("exec.win32_is_refused_before_launch",
               isinstance(win_err, exec_mod.UnsupportedPlatform)
               and isinstance(win_err, OSError)
               and not popen_calls
               and "Nothing was launched" in str(win_err)
               and backstop_contained is False,
               f"a Windows run is refused WITHOUT SPAWNING — the race is a grandchild "
               f"started in the child's first instants, so a check that runs once the "
               f"child exists has already let it happen — and the refusal is an OSError "
               f"subclass so existing callers fail closed on it unchanged; a job that "
               f"assigns cleanly still never reads as containment: "
               f"raised={type(win_err).__name__} popen_calls={len(popen_calls)} "
               f"contained_backstop={backstop_contained} "
               f"reason={str(win_err)[:70]!r}", failures, verbose)

        # --- ChildSpawned: a post-spawn failure is NOT a failure to spawn --------
        # run_captured must distinguish them, because they demand opposite handling: a
        # child that never started has nothing to audit, while one that broke mid-flight
        # may already have brought an MCP server up.
        shim = type(exec_mod)("subprocess_shim")
        shim.PIPE, shim.DEVNULL = exec_mod.subprocess.PIPE, exec_mod.subprocess.DEVNULL
        shim.TimeoutExpired = exec_mod.subprocess.TimeoutExpired

        broken = _FakeProc()

        class _BrokenPipePopen:
            def __init__(self, *a, **k):
                self.pid, self.returncode = broken.pid, None

            def communicate(self, timeout=None):
                raise OSError("the pipe went away mid-run")

            def kill(self):
                broken.killed = True

        # The tree is stubbed rather than suppressed: this arm is about how run_captured
        # CLASSIFIES the failure, and it only reaches that code at all on a contained
        # launch (an uncontained one fails closed above). Stubbing also keeps the fake's
        # placeholder pid away from a real killpg.
        class _FakeTree:
            contained, why_uncontained = True, ""
            killed = closed_ = False

            def __init__(self, proc):
                self._proc = proc

            def kill(self):
                type(self).killed = True
                self._proc.kill()

            def close(self):
                type(self).closed_ = True

        shim.Popen = _BrokenPipePopen
        saved_sub, exec_mod.subprocess = exec_mod.subprocess, shim
        saved_tree, exec_mod._ProcessTree = exec_mod._ProcessTree, _FakeTree
        try:
            raised = None
            try:
                exec_mod.run_captured(["x"], cwd=".", env={}, timeout=1)
            except BaseException as exc:      # noqa: BLE001 — the type IS the assertion
                raised = exc
        finally:
            exec_mod.subprocess = saved_sub
            exec_mod._ProcessTree = saved_tree
        wrapped = (isinstance(raised, ChildSpawned)
                   and isinstance(raised.__cause__, OSError) and broken.killed
                   and _FakeTree.killed and _FakeTree.closed_)

        # ...and the consumer acts on that split: the probe audits a child that RAN and
        # skips the audit only when there was no child at all.
        audited: list = []

        class _AuditCli(Adapter):
            name = "auditcli"
            binary = sys.executable

            def build_argv(self, prompt, opts, *, cwd):
                return [self.binary, "-c", "pass"]

            def _probe_argv(self, model, *, cwd=None, env=None):
                return [self.binary, "-c", "pass"]

            def parse(self, stdout, stderr, exit_code, *, opts=None):
                return ParseOutput()

            def verify_post_run(self, argv, opts, *, cwd, stdout="", stderr="",
                                exit_code=None):
                audited.append(stdout)

        saved_rc = exec_mod.run_captured
        try:
            exec_mod.run_captured = lambda *a, **k: (_ for _ in ()).throw(
                ChildSpawned(OSError("broke"), stdout="said something", stderr=""))
            post_spawn = _AuditCli().probe_model("m", timeout=5)
            audited_post_spawn = list(audited)
            audited.clear()
            exec_mod.run_captured = lambda *a, **k: (_ for _ in ()).throw(
                OSError("could not spawn"))
            never_spawned = _AuditCli().probe_model("m", timeout=5)
        finally:
            exec_mod.run_captured = saved_rc

        _check("exec.post_spawn_failure_is_still_audited",
               wrapped and post_spawn.accepted is False
               and audited_post_spawn == ["said something"]
               and never_spawned.accepted is False and audited == [],
               f"a failure AFTER the child started is reported as ChildSpawned (carrying "
               f"the output it managed to produce) and still goes through the post-run "
               f"MCP audit, while a failure to spawn at all skips it — before, both "
               f"arrived as OSError and a child that HAD run escaped the audit: "
               f"wrapped={wrapped} audited_after_post_spawn={audited_post_spawn!r} "
               f"audited_after_spawn_failure={audited!r}", failures, verbose)
    finally:
        exec_mod._win32_kernel32 = saved_k32


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
    # read — or fail closed on — this machine's real ~/.copilot (config, agents),
    # and stub the ODR gate OFF so a 64-bit Windows host with a live ODR registry
    # (where every copilot argv fails closed by design) can still run them.
    import agentskill_evals.adapters.copilot as _cop_mod_eff
    _cop_wow64 = sys.platform == "win32" and sys.maxsize <= 2**32
    _cop_eff_priv, _cop_eff_cwd, _cop_env = _cop_private_fixture("ase-copeff-")
    _odr_real_eff = _patch_module_attr(_cop_mod_eff, "_odr_registry_command",
                                       lambda: None)
    # ...and scope the (unbounded) custom-agent walk to this fixture's own tree, so an
    # ambient agents dir in a shared ancestor can't fail these argv checks closed
    _defs_real_eff = _cop_scope_agent_scan([_cop_eff_priv])

    # --- per-adapter argv mapping ---
    try:
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
                cwd=_cop_eff_cwd)
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
               "antigravity declares no reasoning-effort support (tiered model ids "
               "instead)", failures, verbose)
        ag_plain = ag.build_argv("p", RunOptions(), cwd="/tmp")
        ag_effort = ag.build_argv("p", RunOptions(reasoning_effort="high"), cwd="/tmp")
        _check("effort.antigravity_argv_unchanged", ag_plain == ag_effort,
               f"antigravity argv unchanged when effort set: {ag_effort}", failures,
               verbose)

        # --- unset keeps every argv unchanged (existing behavior preserved) ---
        for name in ("claude", "codex") if _cop_wow64 else ("claude", "codex", "copilot"):
            opts = (RunOptions(effective_env=_cop_env) if name == "copilot"
                    else RunOptions())
            plain = get_adapter(name).build_argv(
                "p", opts, cwd=_cop_eff_cwd if name == "copilot" else "/tmp")
            _check(f"effort.{name}_absent_when_unset",
                   not any("effort" in a for a in plain),
                   f"{name} argv carries no effort token when unset: {plain}",
                   failures, verbose)
    finally:
        _unpatch_module_attr(_cop_mod_eff, "_odr_registry_command", _odr_real_eff)
        _cop_restore_agent_scan(_defs_real_eff)
        shutil.rmtree(_cop_eff_priv, ignore_errors=True)

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


def _check_cost_formatting(failures, verbose):
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


def _check_claude_adapter(failures, verbose):
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


def _check_codex_adapter(failures, verbose):
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
            # This check needs a WORKING git, and `which` only proves a file exists —
            # a broken install, an ownership/format refusal, or a shim answering for
            # `git` all fail at run time. Treat that as "can't run this check" and skip
            # it; a fixture that can't build its own precondition must never crash the
            # suite (`check=True` here used to take every other check down with it).
            # Global/system git config is neutralized so a developer's init template or
            # hooks can't shape the fixture repo.
            _troot = os.path.join(_codex_ws, "proj")
            _git_env = {**_cx_env, "GIT_CONFIG_GLOBAL": os.devnull,
                        "GIT_CONFIG_SYSTEM": os.devnull}
            _git_ok = False
            if _sh.which("git"):
                try:
                    # a clean exit is not proof: verify the repo the check needs
                    # actually exists (a stand-in answering for `git` can exit 0
                    # having created nothing)
                    _git_ok = (_sp.run(["git", "init", "-q", _troot],
                                       capture_output=True, env=_git_env,
                                       timeout=60).returncode == 0
                               and os.path.isdir(os.path.join(_troot, ".git")))
                except (OSError, ValueError, _sp.SubprocessError):
                    _git_ok = False
            if _git_ok:
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
                print("  [skipped — no working git] "
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


def _check_copilot_adapter(failures, verbose):
    # Copilot
    import os
    import shutil as _sh
    import tempfile as _tmp

    from .schema import RunResult as _RR
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
    # there: they get architecture-aware skips instead of crashing the suite. Every
    # argv-shape call runs in a PRIVATE fixture (_cop_private_fixture: private cwd,
    # pinned HOME/USERPROFILE, private COPILOT_HOME) so none of them reads this
    # machine's real ~/.copilot, and the custom-agent FILE scan is scoped to the
    # registered fixture roots (_cop_scan_roots below) so an ambient agents dir in a
    # shared ancestor — the walk now climbs to / — can't decide any of them.
    _cop_wow64 = sys.platform == "win32" and sys.maxsize <= 2**32
    _cop_priv, _cop_cwd, _cop_env = _cop_private_fixture("ase-copargv-")
    _cop_scan_roots = [_cop_priv]
    _defs_real_cop = _cop_scope_agent_scan(_cop_scan_roots)
    # The ODR gate is stubbed OFF for every positive argv/enumeration check in this
    # section: on a 64-bit Windows host whose live ODR registry is populated the
    # adapter fails EVERY invocation closed by design (asserted separately in
    # copilot.odr_gate_on_fails_closed), which would otherwise crash these checks.
    # Restored at the ODR-specific checks below.
    import agentskill_evals.adapters.copilot as copilot_mod
    _odr_real_cop = _patch_module_attr(copilot_mod, "_odr_registry_command",
                                       lambda: None)
    if _cop_wow64:
        try:
            get_adapter("copilot").build_argv(
                "do the task", RunOptions(model="auto", effective_env=_cop_env),
                cwd=_cop_cwd)
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
            cwd=_cop_cwd)
        _check("copilot.argv",
               cargv[0] == "copilot" and "-p" in cargv and "--output-format" in cargv
               and "json" in cargv and "--allow-all" in cargv and "--model" in cargv
               and "--banner" in cargv and cargv[-1] != "do the task",
               f"copilot argv: {cargv}", failures, verbose)
        # Built-in / feature-gated in-process servers are disabled by NAME on every
        # argv: --disable-builtin-mcps' help covers only github-mcp-server in
        # 1.0.64, and config `enabledMcpServers` can switch on the staff-gated
        # computer-use — which --disable-mcp-server must pre-empt by name.
        _cop_builtins = {"github-mcp-server", "playwright", "bluebird",
                         "computer-use"}
        _cargv_dis = {cargv[i + 1] for i, a in enumerate(cargv)
                      if a == "--disable-mcp-server"}
        _pargv = get_adapter("copilot")._probe_argv("m", cwd=_cop_cwd, env=_cop_env)
        _pargv_dis = {_pargv[i + 1] for i, a in enumerate(_pargv)
                      if a == "--disable-mcp-server"}
        _check("copilot.builtin_mcps_disabled_by_name",
               _cop_builtins <= _cargv_dis and _cop_builtins <= _pargv_dis,
               f"github-mcp-server/playwright/bluebird/computer-use are name-"
               f"disabled on run AND probe argv (run={sorted(_cargv_dis)}, "
               f"probe={sorted(_pargv_dis)})", failures, verbose)
        cargv_dt = get_adapter("copilot").build_argv(
            "judge", RunOptions(disable_tools=True, effective_env=_cop_env),
            cwd=_cop_cwd)
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
           # settings.json takes the SAME sanitizer: 1.0.64 migrates user settings
           # between the two files at startup, so a key stripped from one would ride
           # back in through the other, which the overlay symlinks unless declared here
           and _cop_masks.get(".copilot/settings.json")
               is _cop_masks.get(".copilot/config.json")
           and get_adapter("copilot").isolation_config_homes
           == [("COPILOT_HOME", ".copilot", None)],
           "copilot masks mcp-config.json (valid empty shape), installed-plugins, "
           "agents/ (--disable-mcp-server cannot be AIMED at frontmatter mcp-servers: "
           "the names are unenumerable), and "
           "sanitizes config.json AND settings.json (copilot moves keys between them); "
           "COPILOT_HOME mirrored", failures, verbose)

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
        # the fixture exercises copilot's FULL JSONC grammar (live-verified via
        # `copilot mcp list`): full-line, inline, and block comments, trailing
        # commas — with comment-lookalikes inside string values staying data
        with open(cfg_path, "w") as fh:
            fh.write('// User settings belong in settings.json.\n'
                     '{\n'
                     '  "copilotTokens": {"github.com": "tok-KEEP"}, // inline\n'
                     '  /* block\n     comment */\n'
                     '  "endpoint": "http://x/*not-a*/comment//either",\n'
                     '  "installedPlugins": [{"name": "linear", "enabled": true,'
                     ' "cache_path": "/real/plugins/linear"}],\n'
                     '  "enabledPlugins": {"linear": true},\n'
                     '  "enabledMcpServers": ["computer-use"],\n'
                     '  "customAgents": {"defaultLocalOnly": false, "keep": 1,},\n'
                     '}\n')
        sanitized = _json.loads(_sanitized_copilot_config(cfg_path))
        _check("copilot.config_sanitizer",
               sanitized.get("copilotTokens") == {"github.com": "tok-KEEP"}
               and sanitized.get("endpoint") == "http://x/*not-a*/comment//either"
               and "installedPlugins" not in sanitized
               and "enabledPlugins" not in sanitized
               and "enabledMcpServers" not in sanitized
               and sanitized.get("customAgents") == {"defaultLocalOnly": True,
                                                     "keep": 1},
               f"plugin registrations AND enabledMcpServers (feature-gated builtin "
               f"switch, e.g. computer-use) dropped, auth kept, the full JSONC "
               f"grammar handled (inline/block comments, trailing commas; string "
               f"contents untouched), defaultLocalOnly forced true over an explicit "
               f"false (sibling keys kept): {sorted(sanitized)}", failures, verbose)
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
        # --disable-mcp-server enumeration: user config ($COPILOT_HOME else ~/.copilot)
        # plus the workspace candidates. The workspace walk is a documented
        # CONSERVATIVE SUPERSET of 1.0.64 — copilot reads only .mcp.json/.github/
        # mcp.json (first existing per dir, cwd→git root; .vscode/mcp.json was
        # REMOVED in 1.0.64), while the harness checks all three candidates in
        # every ancestor: over-enumeration only adds harmless disables. This is
        # what covers probes, judge runs, non-isolated runs, and scenario-seeded
        # workspace configs.
        import json as _json
        # realpath: the custom-agent scan is scoped by PHYSICAL path
        # (_cop_scope_agent_scan), so a /var → /private/var tmpdir must be resolved or
        # these fixtures' own trees would be masked along with the ambient ones.
        _cop_home = os.path.realpath(_tmp.mkdtemp(prefix="ase-cophome-"))
        _cop_ws_root = os.path.realpath(_tmp.mkdtemp(prefix="ase-copws-"))
        _cop_scan_roots += [_cop_home, _cop_ws_root]
        _old_cop_home = os.environ.get("COPILOT_HOME")
        _old_home_pins = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
        try:
            with open(os.path.join(_cop_home, "mcp-config.json"), "w") as fh:
                _json.dump({"mcpServers": {"user-srv": {"command": "echo"}}}, fh)
            # every config home here carries the remote-agents opt-out the sanitizer
            # injects, so these MCP-enumeration checks never trip the custom-agent gate
            _cop_optout = '{"customAgents": {"defaultLocalOnly": true}}'
            with open(os.path.join(_cop_home, "config.json"), "w") as fh:
                fh.write(_cop_optout)
            ws = os.path.join(_cop_ws_root, "nested", "ws")
            os.makedirs(os.path.join(ws, ".github"))
            os.makedirs(os.path.join(ws, ".vscode"))
            with open(os.path.join(ws, ".mcp.json"), "w") as fh:
                _json.dump({"mcpServers": {"ws-srv": {}}}, fh)
            with open(os.path.join(ws, ".github", "mcp.json"), "w") as fh:
                _json.dump({"mcpServers": {"gh-srv": {}}}, fh)
            with open(os.path.join(ws, ".vscode", "mcp.json"), "w") as fh:
                # superset-only candidate: 1.0.64 no longer reads .vscode/mcp.json
                # (or the "servers" spelling) — kept enumerated, harmless disable
                _json.dump({"servers": {"vsc-srv": {}}}, fh)
            with open(os.path.join(_cop_ws_root, ".mcp.json"), "w") as fh:
                _json.dump({"mcpServers": {"ancestor-srv": {}}}, fh)
            # Both walks climb from the cwd to the filesystem root. The MCP-config one
            # is meant to (ancestor-srv is enumerated below); the custom-agent one has
            # its FILE scan scoped to the registered fixture roots, so an ambient
            # ~/.claude/agents or a planted $TMPDIR/.claude/agents can't fail this
            # block closed (see _cop_scope_agent_scan).
            os.environ["COPILOT_HOME"] = _cop_home
            os.environ["HOME"] = os.environ["USERPROFILE"] = _cop_ws_root
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
                "do it", RunOptions(effective_env=_cop_child_env(
                    COPILOT_HOME=_cop_home)), cwd=None)
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
            with open(os.path.join(rel_cop_home, "config.json"), "w") as fh:
                fh.write(_cop_optout)
            rargv = get_adapter("copilot").build_argv(
                "do it", RunOptions(effective_env=_cop_child_env(
                    COPILOT_HOME="rel-cop-home", HOME=_cop_ws_root,
                    USERPROFILE=_cop_ws_root)), cwd=ws)
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
            # The agent scan is stubbed out entirely for the one call: this check is
            # about home RESOLUTION and nothing else, and HOME="" would otherwise put
            # <cwd>/.copilot/agents (which this fixture creates) in scope and fail the
            # call closed before it ever enumerates mcp-config.json.
            if os.name != "nt":
                cwd_cop_home = os.path.join(ws, ".copilot")
                os.makedirs(cwd_cop_home)
                with open(os.path.join(cwd_cop_home, "mcp-config.json"), "w") as fh:
                    _json.dump({"mcpServers": {"empty-home-srv": {"command": "echo"}}}, fh)
                with open(os.path.join(cwd_cop_home, "config.json"), "w") as fh:
                    fh.write(_cop_optout)
                _real_agent_scan = copilot_mod._custom_agent_files
                copilot_mod._custom_agent_files = lambda *a, **k: []
                try:
                    hargv = get_adapter("copilot").build_argv(
                        "do it", RunOptions(effective_env=_cop_child_env(HOME="")),
                        cwd=ws)
                finally:
                    copilot_mod._custom_agent_files = _real_agent_scan
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
            for _k, _v in _old_home_pins.items():
                if _v is None:
                    os.environ.pop(_k, None)
                else:
                    os.environ[_k] = _v
            _sh.rmtree(_cop_home, ignore_errors=True)
            _sh.rmtree(_cop_ws_root, ignore_errors=True)

    # USER mcp-config.json is parsed with copilot's JSONC grammar — live-verified
    # via `copilot mcp list`: line/inline/block comments and trailing commas all
    # accept (three JSONC-declared fixture servers listed), JSON5 forms and garbage
    # make copilot itself ERROR OUT ('Failed to read configuration ...'). A strict
    # parser here would silently enumerate NOTHING for a JSONC-only config — the
    # exact bypass Phase 0 forbids — so the user file gets the JSONC parser and an
    # existing-but-unparseable one fails closed; WORKSPACE files stay strict (also
    # live-verified: a trailing comma makes copilot silently ignore the file).
    if not _cop_wow64:
        _jc_home = os.path.realpath(_tmp.mkdtemp(prefix="ase-copjsonc-"))
        _jc_root = os.path.realpath(_tmp.mkdtemp(prefix="ase-copjsoncws-"))
        # registered so the custom-agent scan reads this fixture's own tree and
        # nothing above it (see _cop_scope_agent_scan)
        _cop_scan_roots += [_jc_home, _jc_root]
        _jc_ws = os.path.join(_jc_root, "ws")
        os.makedirs(_jc_ws)
        try:
            with open(os.path.join(_jc_home, "config.json"), "w") as fh:
                fh.write('{"customAgents": {"defaultLocalOnly": true}}')

            def _jc_disables():
                a = get_adapter("copilot").build_argv(
                    "p", RunOptions(effective_env=_cop_child_env(
                        COPILOT_HOME=_jc_home, HOME=_jc_root,
                        USERPROFILE=_jc_root)),
                    cwd=_jc_ws)
                return [a[i + 1] for i, t in enumerate(a)
                        if t == "--disable-mcp-server"]

            with open(os.path.join(_jc_home, "mcp-config.json"), "w") as fh:
                fh.write('// line comment\n'
                         '{\n'
                         '  /* block\n     comment */\n'
                         '  "mcpServers": {\n'
                         '    "jsonc-line": { "command": "true" }, // inline\n'
                         '    "jsonc-block": { "command": "true" } /* tail */,\n'
                         '    "str-aware": { "command": "http://x/*y", '
                         '"args": ["a,]", "b//c"] },\n'
                         '  },\n'
                         '}\n')
            _jc_got = _jc_disables()
            jsonc_ok = {"jsonc-line", "jsonc-block", "str-aware"} <= set(_jc_got)
            with open(os.path.join(_jc_home, "mcp-config.json"), "w") as fh:
                fh.write("{ this is not json ")
            try:
                _jc_disables()
                garbage_closed = ""
            except RuntimeError as exc:
                garbage_closed = str(exc)
            with open(os.path.join(_jc_home, "mcp-config.json"), "w") as fh:
                fh.write('{ mcpServers: { "j5": { "command": "true" } } }')
            try:
                _jc_disables()
                json5_closed = ""
            except RuntimeError as exc:
                json5_closed = str(exc)
            os.remove(os.path.join(_jc_home, "mcp-config.json"))
            with open(os.path.join(_jc_ws, ".mcp.json"), "w") as fh:
                fh.write('{"mcpServers": {"ws-trailing": {"command": "true"},}}')
            ws_strict_ok = "ws-trailing" not in _jc_disables()
            _check("copilot.user_mcp_config_jsonc",
                   jsonc_ok
                   and "failing closed" in garbage_closed
                   and "mcp-config.json" in garbage_closed
                   and "failing closed" in json5_closed
                   and ws_strict_ok,
                   f"user mcp-config.json parses with copilot's live-verified JSONC "
                   f"grammar (comments incl. inline/block, trailing commas; string "
                   f"contents untouched: {_jc_got}); an existing-but-unparseable "
                   f"user file (garbage or the JSON5 forms copilot rejects) fails "
                   f"closed as copilot itself errors out; workspace files stay "
                   f"strict (trailing comma → ignored, matching copilot)",
                   failures, verbose)
        finally:
            _sh.rmtree(_jc_home, ignore_errors=True)
            _sh.rmtree(_jc_root, ignore_errors=True)

    # JSONC grammar edges that must fail the SAFE way (copilot_mod pure helpers):
    #  - a comment is TOKEN-SEPARATING whitespace, not deletion: `tr/*x*/ue` must NOT
    #    collapse to `true` (copilot reduces that malformed config to {}). Reading it as
    #    `true` would let a comment FABRICATE customAgents.defaultLocalOnly and green-
    #    light a repo run whose remote-agent listing is still live.
    #  - an UNTERMINATED block comment raises (copilot's parser errors too).
    #  - a leading UTF-8 BOM is consumed (copilot accepts a BOM-prefixed config; strict
    #    json.loads rejects one — failing a valid mcp-config.json closed, or dropping
    #    auth from a BOM-prefixed config.json).
    _bom = chr(0xFEFF)

    def _jsonc_raises(s):
        try:
            copilot_mod._jsonc_loads(s)
            return False
        except ValueError:
            return True

    jsonc_comment_ws = _jsonc_raises('{"customAgents":{"defaultLocalOnly":tr/*x*/ue}}')
    jsonc_unterminated = (_jsonc_raises('{"a": 1 /* no close')
                          and _jsonc_raises('{"a": 1 /* no close *'))
    jsonc_bom_ok = (copilot_mod._jsonc_loads(_bom + '{"a": 1}') == {"a": 1}
                    and copilot_mod._jsonc_loads(_bom + '// c\n{"a": 2}') == {"a": 2})
    _optdir = _tmp.mkdtemp(prefix="ase-copopt-")
    try:
        # the security payoff: the comment-glued opt-out must NOT read as opted-out...
        with open(os.path.join(_optdir, "config.json"), "w") as fh:
            fh.write('{"customAgents":{"defaultLocalOnly":tr/*x*/ue}}')
        opt_not_fabricated = copilot_mod._remote_agents_opted_out(_optdir) is False
        # ...while a genuine BOM-prefixed opt-out still does
        with open(os.path.join(_optdir, "config.json"), "w", encoding="utf-8") as fh:
            fh.write(_bom + '{"customAgents": {"defaultLocalOnly": true}}')
        opt_bom_ok = copilot_mod._remote_agents_opted_out(_optdir) is True
        # a BOM-prefixed WORKSPACE file (strict JSON) still enumerates its servers —
        # utf-8-sig consumes the BOM; leaving it would MISS the server (a leak)
        with open(os.path.join(_optdir, ".mcp.json"), "w", encoding="utf-8") as fh:
            fh.write(_bom + '{"mcpServers": {"ws-bom": {"command": "true"}}}')
        ws_bom_ok = "ws-bom" in copilot_mod._mcp_server_names(
            os.path.join(_optdir, ".mcp.json"))
    finally:
        _sh.rmtree(_optdir, ignore_errors=True)

    # The opt-out lives in EITHER of copilot's two settings files, because 1.0.64 moves
    # it between them: a value written into config.json is applied for that run and then
    # migrated into settings.json (verified live against the installed CLI — the
    # harness's own injected opt-out came back out of config.json and appeared intact in
    # a settings.json copilot created). Reading only config.json makes the opt-out
    # evaporate after the first run that touches the home, which strands non-isolated
    # users in a loop: set the documented key, copilot relocates it, the next run fails
    # closed telling them to set the key they already set. Which file WINS when the two
    # disagree is decided in a native module, not readable JS — so a disagreement is not
    # guessed at, it fails closed.
    _twodir = _tmp.mkdtemp(prefix="ase-coptwo-")
    try:
        _tw_cfg = os.path.join(_twodir, "config.json")
        _tw_set = os.path.join(_twodir, "settings.json")
        _tw_on = '{"customAgents": {"defaultLocalOnly": true}}'
        _tw_off = '{"customAgents": {"defaultLocalOnly": false}}'

        def _tw(cfg, settings):
            for p, body in ((_tw_cfg, cfg), (_tw_set, settings)):
                if body is None:
                    if os.path.exists(p):
                        os.remove(p)
                else:
                    with open(p, "w") as fh:
                        fh.write(body)
            return copilot_mod._remote_agents_opted_out(_twodir)

        # the migrated shape: config.json no longer carries it, settings.json does
        tw_settings_only = _tw(None, _tw_on) is True
        tw_config_only = _tw(_tw_on, None) is True
        tw_both = _tw(_tw_on, _tw_on) is True
        # neither set, and an explicit false, are not opted out
        tw_neither = _tw(None, None) is False
        tw_false = _tw(_tw_off, None) is False and _tw(None, _tw_off) is False
        # disagreement fails closed in BOTH directions — no precedence is assumed
        tw_conflict = _tw(_tw_on, _tw_off) is False and _tw(_tw_off, _tw_on) is False
        # A PRESENT customAgents that does not itself say defaultLocalOnly:true is a
        # contradiction, whatever shape it has. The migration moves the whole key across,
        # so {"customAgents": {}} beside a settings.json opt-out overwrites the opt-out
        # with {} — judging the subkey's presence instead of the key's read that as clean.
        tw_empty = all(
            _tw(body, _tw_on) is False and _tw(_tw_on, body) is False
            for body in ('{"customAgents": {}}',
                         '{"customAgents": null}',
                         '{"customAgents": []}',
                         '{"customAgents": "local"}',
                         '{"customAgents": {"other": true}}',
                         '{"customAgents": {"defaultLocalOnly": "true"}}',
                         '{"customAgents": {"defaultLocalOnly": 1}}')
        )
        # an UNRELATED key is not a contradiction — the other file still speaks
        tw_unrelated = _tw('{"theme": "dark"}', _tw_on) is True
    finally:
        _sh.rmtree(_twodir, ignore_errors=True)
    _check("copilot.opt_out_read_from_both_settings_files",
           tw_settings_only and tw_config_only and tw_both and tw_neither
           and tw_false and tw_conflict and tw_empty and tw_unrelated,
           "the remote-agent opt-out counts from config.json OR settings.json — copilot "
           "migrates it between them at startup, so reading one file loses it after the "
           "first run — while an explicit false, a missing key, and any DISAGREEMENT "
           "between the two files (precedence being decided in a native module the "
           "harness cannot read) all fail closed; a customAgents key that is PRESENT but "
           "does not itself set defaultLocalOnly:true (empty object, wrong type, truthy "
           "non-true) contradicts the other file, because the migration transports the "
           "whole key", failures, verbose)
    _check("copilot.jsonc_grammar_edges",
           jsonc_comment_ws and jsonc_unterminated and jsonc_bom_ok
           and opt_not_fabricated and opt_bom_ok and ws_bom_ok,
           "JSONC comments are token-separating whitespace (tr/*x*/ue does NOT become "
           "true, so a comment can't fabricate customAgents.defaultLocalOnly — the "
           "opt-out reads False), an unterminated block comment raises, and a leading "
           "UTF-8 BOM is consumed for user JSONC (a BOM-prefixed opt-out still reads "
           "True) and strict workspace files (BOM-prefixed servers still enumerated)",
           failures, verbose)

    # Custom agents are an MCP channel --disable-mcp-server cannot be AIMED at: a
    # selected agent's frontmatter mcp-servers WOULD honor the disable set, but their
    # names are unenumerable (local frontmatter plus REMOTE org/enterprise listings the
    # harness cannot read), and no flag disables custom agents (1.0.64 bundle). So
    # _mcp_disable_args must FAIL CLOSED on any discoverable local agent file —
    # <home>/agents plus .github/agents / .claude/agents from the run cwd up to the
    # FILESYSTEM ROOT — and on any config that doesn't provably opt out of the REMOTE
    # org/enterprise listing (customAgents.defaultLocalOnly, which the sanitizer
    # injects).
    #
    # The walk carries NO boundary. copilot's own is `gitRoot if git discovery finds a
    # repo else os.homedir()`: the git arm would take a git EXECUTION that copilot
    # repeats at launch (the two-execution gap the ODR gate refuses), and the home arm
    # is not a safe substitute because the choice is an either/or — a home nested in a
    # repo gets walked past (copilot.agent_walk_not_bounded_by_home). Nothing in these
    # fixtures needs a git binary, and nothing about them changes if one is missing.
    if not _cop_wow64:
        # realpath: the scan scope below matches on PHYSICAL paths, so an unresolved
        # /var → /private/var tmpdir would mask the fixture's own files
        _ag_dir = os.path.realpath(_tmp.mkdtemp(prefix="ase-copagents-"))
        _cop_scan_roots.append(_ag_dir)
        try:
            _ag_home = os.path.join(_ag_dir, "home")
            _ag_proj = os.path.join(_ag_dir, "proj")
            _ag_ws = os.path.join(_ag_proj, "ws")
            os.makedirs(_ag_home)
            os.makedirs(_ag_ws)
            # The walk climbs past this fixture into shared TMPDIR ancestors — nothing
            # bounds it — so the scan is SCOPED to _ag_dir (registered above) and only
            # files this fixture plants can decide these checks.
            _ag_env = _cop_child_env(COPILOT_HOME=_ag_home, HOME=_ag_dir,
                                     USERPROFILE=_ag_dir)
            # the remote-agents opt-out is required for EVERY run now, so the fixture
            # config home carries it and the LOCAL-agent checks below test only what
            # they name (its own gate is asserted separately)
            _ag_optout = os.path.join(_ag_home, "config.json")
            with open(_ag_optout, "w") as fh:
                fh.write('{"customAgents": {"defaultLocalOnly": true}}')

            def _cop_agents_err(cwd, **extra_env):
                try:
                    get_adapter("copilot").build_argv(
                        "p", RunOptions(effective_env={**_ag_env, **extra_env}),
                        cwd=cwd)
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
            os.makedirs(os.path.join(_ag_proj, ".github", "agents"))
            open(os.path.join(_ag_proj, ".github", "agents", "x.md"), "w").close()
            ag_github = "custom-agent" in _cop_agents_err(_ag_ws)
            _sh.rmtree(os.path.join(_ag_proj, ".github"))
            os.makedirs(os.path.join(_ag_proj, ".claude", "agents"))
            open(os.path.join(_ag_proj, ".claude", "agents", "x.agent.md"), "w").close()
            ag_claude = "custom-agent" in _cop_agents_err(_ag_ws)
            _sh.rmtree(os.path.join(_ag_proj, ".claude"))
            _check("copilot.custom_agents_fail_closed",
                   ag_clean and ag_nonmd and ag_home_md and ag_github and ag_claude,
                   "any *.md under <home>/agents (recursive, case-insensitive) or a "
                   ".github/agents / .claude/agents dir on the cwd walk fails closed "
                   "(the harness cannot enumerate agent-declared mcp-server names); "
                   "empty dirs and non-md files don't", failures, verbose)

            # The agent walk must NOT be narrowed by any repository marker. copilot's
            # own walk does stop at a git root, but the harness cannot learn where that
            # is without executing git — and copilot executes git again at launch, so a
            # stateful/slow/differently-resolved git can answer the two runs
            # differently and leave copilot walking FARTHER than the harness looked.
            # Every .git spelling below must therefore be inert: the agent file above it
            # is still found.
            os.makedirs(os.path.join(_ag_dir, ".claude", "agents"))
            open(os.path.join(_ag_dir, ".claude", "agents", "above.md"), "w").close()
            _ag_marker = os.path.join(_ag_proj, ".git")
            os.makedirs(_ag_marker)                                  # bare .git dir
            ag_marker_dir = "above.md" in _cop_agents_err(_ag_ws)
            open(os.path.join(_ag_marker, "HEAD"), "w").close()      # + a HEAD
            ag_marker_head = "above.md" in _cop_agents_err(_ag_ws)
            _sh.rmtree(_ag_marker)
            with open(_ag_marker, "w") as fh:                        # worktree .git FILE
                fh.write("gitdir: /elsewhere\n")
            ag_marker_file = "above.md" in _cop_agents_err(_ag_ws)
            os.remove(_ag_marker)
            # ...nor by anything that redirects git's own discovery
            ag_git_env = all(
                "above.md" in _cop_agents_err(_ag_ws, **{v: val})
                for v, val in (("GIT_DIR", ""), ("GIT_DIR", "/some/gitdir"),
                               ("GIT_WORK_TREE", _ag_proj),
                               ("GIT_CEILING_DIRECTORIES", _ag_proj)))
            _check("copilot.agent_walk_not_narrowed_by_git",
                   ag_marker_dir and ag_marker_head and ag_marker_file and ag_git_env,
                   "the local-agent scan is never narrowed by a repository signal — "
                   "not a .git dir (empty or HEAD-bearing), not a worktree .git file, "
                   "not GIT_DIR/GIT_WORK_TREE/GIT_CEILING_DIRECTORIES in the child env "
                   "— because pinning the real boundary would mean executing git while "
                   "copilot executes it again at launch (the ODR two-execution gap); "
                   "an agent file above every one of those markers is still found",
                   failures, verbose)
            _sh.rmtree(os.path.join(_ag_dir, ".claude"))

            # The REMOTE-listing gate is likewise never CLEARED by a preflight: the
            # opt-out is required unconditionally, in a repo or not.
            os.remove(_ag_optout)
            _e = _cop_agents_err(_ag_ws)
            ag_remote = "defaultLocalOnly" in _e and "failing closed" in _e
            ag_remote_norepo = "defaultLocalOnly" in _cop_agents_err(_ag_home)
            with open(_ag_optout, "w") as fh:
                fh.write('{"customAgents": {"defaultLocalOnly": true}}')
            ag_optout_runs = _cop_agents_err(_ag_ws) == ""
            _check("copilot.remote_agents_fail_closed",
                   ag_remote and ag_remote_norepo and ag_optout_runs,
                   "a config that doesn't provably set customAgents.defaultLocalOnly "
                   "fails closed on EVERY run — remote org/enterprise agents can carry "
                   "mcp-servers the harness cannot name, and proving a cwd is outside "
                   "a repository would take a git execution copilot independently "
                   "repeats — while the opt-out (as the sanitizer injects it) runs "
                   "clean", failures, verbose)
        finally:
            _sh.rmtree(_ag_dir, ignore_errors=True)

    # The convention-dir walk runs over the PHYSICAL cwd: copilot resolves paths the
    # same way, so a cwd symlinked into a tree must find the agent dirs living there.
    if not _cop_wow64:
        _gs_root = os.path.realpath(_tmp.mkdtemp(prefix="ase-copgs-"))
        _cop_scan_roots.append(_gs_root)
        try:
            _gs_home = os.path.join(_gs_root, "home")
            os.makedirs(_gs_home)
            with open(os.path.join(_gs_home, "config.json"), "w") as fh:
                fh.write('{"customAgents": {"defaultLocalOnly": true}}')
            _gs_env = _cop_child_env(COPILOT_HOME=_gs_home, HOME=_gs_root,
                                     USERPROFILE=_gs_root)

            def _gs_err(cwd):
                try:
                    get_adapter("copilot").build_argv(
                        "p", RunOptions(effective_env=_gs_env), cwd=cwd)
                    return ""
                except RuntimeError as exc:
                    return str(exc)

            _gs_proj = os.path.join(_gs_root, "proj")
            _gs_sub = os.path.join(_gs_proj, "sub")
            os.makedirs(_gs_sub)
            os.makedirs(os.path.join(_gs_proj, ".github", "agents"))
            open(os.path.join(_gs_proj, ".github", "agents", "mid.md"), "w").close()
            physical_found = "custom-agent" in _gs_err(_gs_sub)
            _check("copilot.agent_walk_physical_cwd", physical_found,
                   "the convention-dir walk runs over the resolved cwd and finds the "
                   "agent dirs of its ancestors", failures, verbose)
            # a cwd SYMLINKED into that subtree must resolve to the physical path (an
            # abspath walk would climb the LINK's parents and miss mid.md entirely).
            # Windows may forbid creating the link at all — that arm is then SKIPPED
            # outright rather than folded into a passing _check, which would report
            # symlink behaviour as verified when nothing exercised it.
            _gs_link = os.path.join(_gs_root, "link")
            try:
                os.symlink(_gs_sub, _gs_link, target_is_directory=True)
            except (OSError, NotImplementedError, AttributeError):
                _gs_link = None
            if _gs_link is not None:
                _check("copilot.agent_walk_symlinked_cwd",
                       "custom-agent" in _gs_err(_gs_link),
                       "a cwd symlinked into a tree resolves to the physical path and "
                       "finds the same agent files copilot's own physical resolution "
                       "does", failures, verbose)
            elif verbose:
                print("  [skipped — symlink creation unavailable] "
                      "copilot.agent_walk_symlinked_cwd")
        finally:
            _sh.rmtree(_gs_root, ignore_errors=True)

    # The walk is NOT bounded by the child's OS home. copilot's boundary is an
    # either/or — `boundary = gitDiscovery(cwd).found ? gitRoot : os.homedir()` — not the
    # nearer of the two, so a home NESTED IN a repository is walked straight past: with
    # HOME=/repo/home and cwd /repo/home/ws, copilot reads /repo/.github/agents. A
    # harness that stopped at the home would miss exactly that file, and
    # defaultLocalOnly would not save the run (it suppresses REMOTE agents only, so the
    # missed LOCAL agent still brings up its own mcp-servers). Deciding whether the home
    # is the real boundary means deciding whether git discovery succeeds — the execution
    # the harness refuses to make — so the home is simply INERT here, in every spelling.
    #
    # This fixture reproduces that geometry exactly: an agent dir at the repo root, the
    # child's home BELOW it, the cwd below that.
    if not _cop_wow64:
        _hb_root = os.path.realpath(_tmp.mkdtemp(prefix="ase-cophb-"))
        _cop_scan_roots.append(_hb_root)
        try:
            _hb_repo = os.path.join(_hb_root, "repo")
            _hb_home = os.path.join(_hb_repo, "home")
            _hb_cwd = os.path.join(_hb_home, "ws")
            _hb_cop = os.path.join(_hb_home, ".copilot")
            os.makedirs(_hb_cwd)
            os.makedirs(_hb_cop)
            # SYNTHETIC GEOMETRY, deliberately: a marker dir standing where copilot's
            # boundary would be. The code under test never runs git — it walks to `/`
            # precisely BECAUSE it refuses to trust a git it would have to execute — so a
            # real repository would exercise nothing this doesn't, while costing an
            # ambient subprocess (and its timeout) on every suite run. Nothing below reads
            # `.git`; it marks the spot for the reader.
            os.makedirs(os.path.join(_hb_repo, ".git"), exist_ok=True)
            with open(os.path.join(_hb_cop, "config.json"), "w") as fh:
                fh.write('{"customAgents": {"defaultLocalOnly": true}}')

            def _hb_err(**home_env):
                env = _cop_child_env(COPILOT_HOME=_hb_cop, HOME=_hb_home,
                                     USERPROFILE=_hb_home)
                env.update(home_env)
                try:
                    get_adapter("copilot").build_argv(
                        "p", RunOptions(effective_env=env), cwd=_hb_cwd)
                    return ""
                except RuntimeError as exc:
                    return str(exc)

            # nothing planted yet: the run builds clean, so the failures below are the
            # agent files and not some unrelated gate
            hb_clean = _hb_err() == ""
            # the P1 case — an agent dir ABOVE the child's home, at the repo root that
            # is copilot's actual boundary. An exactly-spelled home must NOT hide it.
            os.makedirs(os.path.join(_hb_repo, ".github", "agents"))
            open(os.path.join(_hb_repo, ".github", "agents", "above_home.md"),
                 "w").close()
            hb_above_home_closed = "above_home.md" in _hb_err()
            # ...and no spelling of the home changes that: exact, empty, relative,
            # trailing separator, or absent altogether are all equally inert
            hb_spelling_inert = all(
                "above_home.md" in _hb_err(**{k: v for k in ("HOME", "USERPROFILE")})
                for v in ("", "home", _hb_home + os.sep, _hb_root))
            _hb_unset = {"COPILOT_HOME": _hb_cop}
            try:
                get_adapter("copilot").build_argv(
                    "p", RunOptions(effective_env=_hb_unset), cwd=_hb_cwd)
                hb_unset_closed = False
            except RuntimeError as exc:
                hb_unset_closed = "above_home.md" in str(exc)
            # an agent dir AT the home is in scope too
            os.makedirs(os.path.join(_hb_home, ".claude", "agents"))
            open(os.path.join(_hb_home, ".claude", "agents", "at_home.md"), "w").close()
            hb_at_home_closed = "at_home.md" in _hb_err()
            _check("copilot.agent_walk_not_bounded_by_home",
                   hb_clean and hb_above_home_closed and hb_spelling_inert
                   and hb_unset_closed and hb_at_home_closed,
                   "the child's OS home does not bound the local-agent scan: an agent "
                   "dir ABOVE it — where copilot's real boundary, the git root, puts it "
                   "in scope — fails the run closed, whatever the home is spelled as "
                   "(exact, empty, relative, trailing separator, unset)", failures,
                   verbose)
        finally:
            _sh.rmtree(_hb_root, ignore_errors=True)

    # The launch window. _mcp_disable_args reads the agent dirs and MCP configs before
    # launch; copilot reads them again for itself at startup, and execs `git rev-parse`
    # (child PATH) for its convention-dir boundary BEFORE it globs the agent dirs.
    # Whatever answers for git there can plant an agent file or a config entry that
    # copilot then discovers and the disable set never named. Neither closing move
    # exists: the discovery paths are every ancestor up to `/` plus a config home the
    # child owns and can rewrite (isolation's empty agents/ mask included — same uid),
    # and copilot's git can't be bound to a binary the harness trusts more, because the
    # harness would resolve the same PATH to find one. So it is DETECTED instead:
    # verify_post_run re-runs the enumeration after the child exits and fails the run if
    # the answer moved. Re-executing the scan is sound in this direction only — it can
    # add failures, never clear a gate (contrast the git preflight this branch refuses).
    if not _cop_wow64:
        _pr_root = os.path.realpath(_tmp.mkdtemp(prefix="ase-coppr-"))
        _cop_scan_roots.append(_pr_root)
        try:
            _pr_cwd = os.path.join(_pr_root, "ws")
            _pr_cop = os.path.join(_pr_root, ".copilot")
            os.makedirs(_pr_cwd)
            os.makedirs(_pr_cop)
            _pr_cfg = os.path.join(_pr_cop, "mcp-config.json")
            with open(os.path.join(_pr_cop, "config.json"), "w") as fh:
                fh.write('{"customAgents": {"defaultLocalOnly": true}}')
            with open(_pr_cfg, "w") as fh:
                fh.write('{"mcpServers": {"early": {"command": "x"}}}')
            _pr_env = _cop_child_env(COPILOT_HOME=_pr_cop, HOME=_pr_root,
                                     USERPROFILE=_pr_root)
            _pr_opts = RunOptions(effective_env=_pr_env)
            _pr_cop_ad = get_adapter("copilot")
            _pr_argv = _pr_cop_ad.build_argv("p", _pr_opts, cwd=_pr_cwd)

            def _pr_err():
                try:
                    return ("" if _pr_cop_ad.verify_post_run(
                        _pr_argv, _pr_opts, cwd=_pr_cwd) is None else "returned")
                except RuntimeError as exc:
                    return str(exc)

            # nothing moved: the re-check is a clean no-op, so the failures below are
            # the planted state and not a re-check that fails on everything
            pr_clean = _pr_err() == ""
            # a server configured AFTER argv was built is not in the launched disable
            # set — copilot loads its config at startup, inside the window
            with open(_pr_cfg, "w") as fh:
                fh.write('{"mcpServers": {"early": {"command": "x"},'
                         ' "late": {"command": "y"}}}')
            pr_late_server = _pr_err()
            # ...and an agent file planted in the window fails closed the same way,
            # through the enumeration's own agent gate (config restored first, so the
            # server arm above can't be what fires here)
            with open(_pr_cfg, "w") as fh:
                fh.write('{"mcpServers": {"early": {"command": "x"}}}')
            os.makedirs(os.path.join(_pr_cwd, ".github", "agents"))
            open(os.path.join(_pr_cwd, ".github", "agents", "late.md"), "w").close()
            pr_late_agent = _pr_err()
            _check("copilot.post_run_recheck_catches_launch_window",
                   pr_clean
                   and "late" in pr_late_server
                   and "not provably MCP-hermetic" in pr_late_server
                   and "early" not in pr_late_server.split("are configured now")[0]
                   and "late.md" in pr_late_agent
                   and "no longer" in pr_late_agent,
                   f"state that moves between argv construction and the child's own "
                   f"read is caught after the run — a late server names itself, a late "
                   f"agent file fails the re-check closed: "
                   f"server={pr_late_server[:60]!r} agent={pr_late_agent[:60]!r}",
                   failures, verbose)

            # ...but re-reading state cannot catch a change that is UNDONE before the
            # child exits: plant, let copilot load the server, restore the original bytes,
            # and the post-run read is byte-identical to the pre-run one. The only witness
            # left is copilot itself, which streams the status of every configured server.
            # So the fixture below restores the clean state — the re-check above is now a
            # no-op again, proving these arms fire on the STREAM and nothing else.
            _sh.rmtree(os.path.join(_pr_cwd, ".github"))

            def _pr_out(*events, exit_code=None):
                try:
                    return ("" if _pr_cop_ad.verify_post_run(
                        _pr_argv, _pr_opts, cwd=_pr_cwd, exit_code=exit_code,
                        stdout="\n".join(_json.dumps(e) for e in events)) is None
                        else "returned")
                except RuntimeError as exc:
                    return str(exc)

            def _pr_loaded(*servers):
                return {"type": "session.mcp_servers_loaded", "ephemeral": True,
                        "data": {"servers": [{"name": n, "status": s}
                                             for n, s in servers]}}

            # the shape a real hermetic run emits, verbatim from 1.0.64 under the
            # harness's flags: configured, listed, and disabled
            pr_disabled_ok = _pr_out(_pr_loaded(("github-mcp-server", "disabled")),
                                     {"type": "assistant.turn_end", "data": {}}) == ""
            # the ABA case: nothing on disk moved, but copilot says it brought one up
            pr_aba = _pr_out(_pr_loaded(("github-mcp-server", "disabled"),
                                        ("planted", "connected")))
            # a server that goes live LATER, after the loaded event said disabled
            pr_changed = _pr_out(
                _pr_loaded(("planted", "disabled")),
                {"type": "session.mcp_server_status_changed", "ephemeral": True,
                 "data": {"serverName": "planted", "status": "connected"}})
            # `failed` still spawned the process, and a status a later version invents is
            # not assumed inert — the allowlist is {disabled, not_configured}
            pr_failed = _pr_out(_pr_loaded(("planted", "failed")))
            pr_unknown = _pr_out(_pr_loaded(("planted", "quantum-superposed")))
            # noise on stdout must not crash the reader or invent servers
            pr_noise_ok = (_pr_out(_pr_loaded(("github-mcp-server", "disabled")),
                                   {"type": "assistant.idle", "data": {}},
                                   {"type": "result", "exitCode": 0}) == ""
                           and _pr_err() == "")
            _check("copilot.post_run_stream_evidence_catches_reverted_leak",
                   pr_disabled_ok and pr_noise_ok
                   and all("planted" in r and "event stream" in r
                           for r in (pr_aba, pr_changed, pr_failed, pr_unknown))
                   and "github-mcp-server" not in pr_aba,
                   f"a leak reverted before the child exits reads back clean on disk and "
                   f"is caught anyway, because copilot's own event stream already named "
                   f"the server it brought up (and a disabled one is not a leak): "
                   f"disabled_ok={pr_disabled_ok} aba={pr_aba[:60]!r} "
                   f"changed={bool(pr_changed)} failed={bool(pr_failed)} "
                   f"unknown={bool(pr_unknown)}", failures, verbose)

            # The stream arms above are only as good as the event still existing. The CLI
            # version cannot be read (npm metadata disagrees with the binary the updater
            # rewrote in place) or pinned (a bare `copilot --version` resolves its app.js
            # through a writable version cache the harness's own --no-auto-update argv
            # bypasses), so the CONTRACT is required instead of a version: a run that
            # emitted its own end-of-session `result` without ever naming an MCP host has
            # lost the witness, and silently returning "no live servers" would read exactly
            # like a clean run.
            pr_gap = _pr_out({"type": "assistant.turn_end", "data": {}},
                             {"type": "result", "exitCode": 0})
            # renamed event == the drift this is here to catch
            pr_renamed = _pr_out({"type": "session.mcp_servers_ready",
                                  "data": {"servers": []}},
                                 {"type": "result", "exitCode": 0})
            # ...but a child that never finished is not blamed for a report it never had
            # the chance to make: no `result`, NONZERO exit, no requirement (the state
            # re-read, which covers exactly that case, has already run above)
            pr_incomplete = _pr_out({"type": "assistant.turn_end", "data": {}}) == ""
            pr_empty_stream = _pr_out() == ""
            pr_killed = _pr_out(exit_code=-9) == ""
            # A ZERO EXIT demands the witness even when NOTHING in the stream is
            # recognizable. Inferring completion from copilot's own `result` event would
            # make the gate self-defeating: a build that renamed both events emits
            # neither, reads as "never finished", and passes — precisely the drift this
            # replaced a version number to catch.
            pr_zero_exit = _pr_out({"type": "session.done"}, exit_code=0)
            pr_renamed_both = _pr_out({"type": "session.mcp_servers_ready",
                                       "data": {"servers": []}},
                                      {"type": "session.finished"}, exit_code=0)
            # A well-formed but EMPTY list proves nothing: the built-in sentinel is
            # configured and disabled on every hermetic invocation, so a witness that
            # does not name it is not describing the host this adapter was verified
            # against. (Presence is what is required — a sentinel reported LIVE is a
            # leak, and is reported as one, not as a broken contract.)
            pr_empty_list = _pr_out(_pr_loaded(), {"type": "result", "exitCode": 0})
            pr_other_only = _pr_out(_pr_loaded(("something-else", "disabled")),
                                    {"type": "result", "exitCode": 0})
            # Payload SHAPE: a renamed field, a retyped one, a missing data object, and a
            # malformed entry each read as "no servers" to a presence-only check.
            pr_no_data = _pr_out({"type": "session.mcp_servers_loaded"}, exit_code=0)
            pr_field_renamed = _pr_out(
                {"type": "session.mcp_servers_loaded",
                 "data": {"mcpServers": [{"name": "github-mcp-server",
                                          "status": "disabled"}]}}, exit_code=0)
            pr_servers_null = _pr_out({"type": "session.mcp_servers_loaded",
                                       "data": {"servers": None}}, exit_code=0)
            pr_bad_entry = _pr_out(
                {"type": "session.mcp_servers_loaded",
                 "data": {"servers": [{"name": "github-mcp-server",
                                       "status": "disabled"}, {"name": 17}]}},
                exit_code=0)
            # ...and the same strictness on the transition event, which feeds `live`
            pr_bad_changed = _pr_out(
                _pr_loaded(("github-mcp-server", "disabled")),
                {"type": "session.mcp_server_status_changed",
                 "data": {"server": "planted", "status": "connected"}}, exit_code=0)
            # the real hermetic shape still passes with a zero exit
            pr_strict_ok = _pr_out(_pr_loaded(("github-mcp-server", "disabled")),
                                   {"type": "result", "exitCode": 0}, exit_code=0) == ""
            _check("copilot.post_run_requires_the_mcp_witness_contract",
                   "session.mcp_servers_loaded" in pr_gap
                   and all(bool(r) for r in (
                       pr_renamed, pr_zero_exit, pr_renamed_both, pr_empty_list,
                       pr_other_only, pr_no_data, pr_field_renamed, pr_servers_null,
                       pr_bad_entry, pr_bad_changed))
                   and "github-mcp-server" in pr_empty_list
                   and "servers" in pr_field_renamed
                   and "malformed" in pr_bad_entry and "malformed" in pr_bad_changed
                   and pr_incomplete and pr_empty_stream and pr_killed and pr_strict_ok,
                   f"a normally-finished run must produce a WELL-FORMED MCP witness "
                   f"naming the built-in sentinel — a zero exit alone demands it (no "
                   f"event rename can disguise that), and a renamed field, a retyped "
                   f"one, a malformed entry or an empty list are contract violations "
                   f"rather than 'no servers'; a killed or unfinished child is still not "
                   f"penalized: gap={bool(pr_gap)} renamed={bool(pr_renamed)} "
                   f"zero_exit={bool(pr_zero_exit)} both_renamed={bool(pr_renamed_both)} "
                   f"empty_list={bool(pr_empty_list)} other_only={bool(pr_other_only)} "
                   f"no_data={bool(pr_no_data)} field={bool(pr_field_renamed)} "
                   f"null={bool(pr_servers_null)} bad_entry={bool(pr_bad_entry)} "
                   f"bad_changed={bool(pr_bad_changed)} killed={pr_killed} "
                   f"incomplete={pr_incomplete} ok={pr_strict_ok}", failures, verbose)
        finally:
            _sh.rmtree(_pr_root, ignore_errors=True)

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
                        # hidden --prefer-version selects a DIFFERENT cached CLI version,
                        # past which this adapter's assumptions no longer hold
                        ["--prefer-version", "1.0.63"], ["--prefer-version=1.0.63"],
                        # --output-format adds no server: it BLINDS the audit. extra_args
                        # are appended last and copilot takes the last value for a
                        # repeated option, so a trailing `text` turns off the JSON stream
                        # verify_post_run reads its ABA-immune evidence from — after
                        # which a reverted launch-window leak is invisible again
                        ["--output-format", "text"], ["--output-format=text"],
                        # even re-stating the value the harness itself passes: the point
                        # is that this option must not be caller-controlled at all
                        ["--output-format", "json"],
                        # -C plus COMBINED short-option clusters carrying it: copilot
                        # accepts -sC/tmp (== -s -C /tmp), which a leading-'-C' rule
                        # would miss
                        ["-C", "/elsewhere"], ["-C/elsewhere"],
                        ["-sC/tmp"], ["-abC/tmp"]):
            try:
                get_adapter("copilot").build_argv(
                    "p", RunOptions(extra_args=_extras), cwd="/tmp")
                _cop_leaked.append(_extras)
            except RuntimeError as exc:
                if "configuration channel" not in str(exc):
                    _cop_leaked.append(_extras)      # raised, but not by the vet
            except AssertionError:
                # the sentinel fired: the vet passed this token through to enumeration.
                # Caught rather than allowed to propagate so a regression is reported as
                # THIS check failing, instead of aborting the suite with a traceback that
                # names no check (which is how a dropped token first showed up here).
                _cop_leaked.append(_extras)
        # a short cluster WITHOUT the cwd flag is neutral and must reach enumeration;
        # -s/--silent is one of them — live-verified NOT to suppress the MCP events, so
        # blocking it would be a false positive the witness does not need
        _cop_neutral = None
        for _neutral in (["--banner"], ["-vs"], ["-s"], ["--silent"]):
            try:
                get_adapter("copilot").build_argv(
                    "p", RunOptions(extra_args=_neutral), cwd="/tmp")
                _cop_neutral = "enumeration-skipped"     # sentinel must have fired
            except AssertionError:
                _cop_neutral = _cop_neutral or "vet-passed"
            except RuntimeError:
                _cop_neutral = "vetoed"
    finally:
        _CopAd._mcp_disable_args = _orig_cop_disable
    _check("copilot.extra_args_config_channels_fail_closed",
           not _cop_leaked and _cop_neutral == "vet-passed",
           f"config-channel extra_args (--additional-mcp-config/--agent/--plugin-dir/"
           f"--config-dir/--prefer-version, the audit-blinding --output-format, =value "
           f"forms, and -C in attached AND combined short-cluster forms like -sC/tmp) "
           f"raise before enumeration; neutral tokens incl. -s/--silent and C-free short "
           f"clusters reach it: leaked={_cop_leaked}, neutral={_cop_neutral}",
           failures, verbose)

    # Windows ODR registry gate (design §2): copilot discovers MCP servers via a
    # registry-advertised command's `mcp list` output. The harness does NOT
    # pre-enumerate that listing — copilot executes the command a second,
    # independent time, and a stateful/time-varying command can hand the two
    # processes DIFFERENT listings, so a populated gate FAILS CLOSED for the whole
    # invocation instead (copilot.odr_gate_on_fails_closed below). What stays
    # testable off-Windows is the gate DETECTION (the registry read + ECMAScript
    # trim/falsy test) and the fail-closed decisions, exercised via stubs.
    # `copilot_mod` and `sys` are already in scope from earlier in run_selftest.
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

    # The positive argv/enumeration checks above ran with the ODR gate stubbed OFF
    # (see the stub near the top of this section) so a live-ODR 64-bit Windows host
    # wouldn't fail them closed. The ODR-specific checks below drive the gate
    # themselves — restore the real registry reader now. (The agent-scan scope stays
    # installed: the ODR build arm below still runs a full _mcp_disable_args in the
    # private fixture, and the agent walk precedes the gate inside it.)
    _unpatch_module_attr(copilot_mod, "_odr_registry_command", _odr_real_cop)

    _check("copilot.odr_gate_off_non_win32",
           sys.platform == "win32" or copilot_mod._odr_registry_command() is None,
           "off Windows the ODR gate reports no command (no registry to read)",
           failures, verbose)

    # The ODR registry read must pin the 64-BIT view AND request only query access:
    # copilot 1.0.64's native helper calls RegGetValueW (opens with KEY_QUERY_VALUE)
    # with the 64-bit-view flag, so the harness uses KEY_QUERY_VALUE | KEY_WOW64_64KEY —
    # NOT the broader KEY_READ (a query-only ACL would deny KEY_READ and make the harness
    # reject a host copilot reads fine). A 32-bit process's DEFAULT view is the redirected
    # WOW6432Node one — there the key can be absent (the gate would read "off") while
    # copilot's 64-bit view is populated. No Windows CI, so
    # `winreg` is stubbed into sys.modules (the function imports it lazily) and the
    # module-local `sys` shimmed to win32; the stub records the access mask OpenKey
    # receives and drives the blank-value/absent/denied arms too.
    _fake_winreg = type(sys)("winreg")   # a real module object, no importlib involved
    _fake_winreg.HKEY_LOCAL_MACHINE = object()
    _fake_winreg.KEY_READ = 0x20019          # the real winreg constants
    _fake_winreg.KEY_QUERY_VALUE = 0x0001    # query-only — what copilot's RegGetValueW uses
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
                   and access == (_fake_winreg.KEY_QUERY_VALUE
                                  | _fake_winreg.KEY_WOW64_64KEY)
                   for root, sub, reserved, access in _reg_calls),
           "the ODR registry key is opened with KEY_QUERY_VALUE | KEY_WOW64_64KEY — the "
           "query-only access copilot's RegGetValueW uses (never the broader KEY_READ, "
           "which a query-only ACL would deny, making the harness reject a host copilot "
           "reads fine) plus the 64-bit view from any harness bitness (never the "
           "WOW6432Node default a 32-bit process would read, where an absent key would "
           "fake the gate 'off'); a missing key or blank/JS-blank (U+FEFF) value still "
           "reads as gate-off, a BOM-wrapped command JS-trims clean, and an unreadable "
           "key fails closed (stubbed winreg + sys shim)",
           failures, verbose)

    # A 32-bit harness process on win32 can't prove MCP hermeticity at all — the WOW64
    # file-system redirector remaps System32 config-file reads to SysWOW64, so the
    # files it reads are not what 64-bit copilot sees (the ODR command is copilot's to
    # launch, not the harness's). The bitness check must fire FIRST: env={} would raise
    # the absent-USERPROFILE error later if it were mis-ordered. Cross-host via a shim
    # whose maxsize reads 32-bit.
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

    # With the gate POPULATED, every invocation fails CLOSED: copilot executes the
    # registry command ITSELF to discover MCP servers, and a second, independent
    # harness execution could be handed a DIFFERENT listing (a stateful/time-varying
    # command) — so there is no sound pre-enumeration, only refusal. (Revisions
    # before this one ran the command and disabled the resolved names; that
    # two-execution gap is exactly what this replaces.) _assert_odr_gate_off raises
    # directly, and the whole _mcp_disable_args build fails closed through it, so no
    # cell/probe/judge argv is produced with un-disabled ODR servers live.
    _odr_saved_cmd = _patch_module_attr(copilot_mod, "_odr_registry_command",
                                        lambda: "C:\\odr\\host.exe mcp-src")
    try:
        try:
            get_adapter("copilot")._assert_odr_gate_off()
            odr_assert_closed = ""
        except RuntimeError as exc:
            odr_assert_closed = str(exc)
        # _mcp_disable_args must propagate the same fail-closed (build_argv/_probe_argv
        # both route through it). The private fixture — registered agent-scan scope
        # plus a config home carrying the opt-out — clears the earlier custom-agent
        # raises (which precede the gate), so the ODR gate is provably what fires. A
        # real 32-bit win32 harness fails closed on bitness first, so the build arm is
        # skipped there.
        odr_build_closed = ""
        if not _cop_wow64:
            try:
                get_adapter("copilot")._mcp_disable_args(_cop_cwd, env=_cop_env)
            except RuntimeError as exc:
                odr_build_closed = str(exc)
        _check("copilot.odr_gate_on_fails_closed",
               "gate is ON" in odr_assert_closed
               and "failing closed" in odr_assert_closed
               and (_cop_wow64 or ("gate is ON" in odr_build_closed
                                   and "failing closed" in odr_build_closed)),
               "a populated ODR registry gate fails the whole invocation closed "
               "(copilot runs the command itself, so a harness re-execution can't "
               "prove it sees the same listing) rather than pre-enumerating names "
               "to disable — both _assert_odr_gate_off and the _mcp_disable_args "
               "build raise", failures, verbose)

        # gate OFF (no registry command) is a clean no-op: the assertion returns and
        # enumeration proceeds on the non-ODR channels only.
        copilot_mod._odr_registry_command = lambda: None
        _check("copilot.odr_gate_off_no_disables",
               get_adapter("copilot")._assert_odr_gate_off() is None,
               "with no registry command the ODR gate is a no-op — nothing fails "
               "and enumeration proceeds on the non-ODR channels", failures, verbose)
    finally:
        _unpatch_module_attr(copilot_mod, "_odr_registry_command", _odr_saved_cmd)
        _cop_restore_agent_scan(_defs_real_cop)     # last copilot argv check
        _sh.rmtree(_cop_priv, ignore_errors=True)   # private argv fixture

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


def _check_antigravity_adapter(failures, verbose):
    # AntiGravity (3 shapes)
    import os
    import shutil as _sh
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


def _check_judge_verdict_extraction(failures, verbose):
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


def _check_schema_validator(failures, verbose):
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


def _check_progress_indicator(failures, verbose):
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


def _check_spec_validation(failures, verbose):
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


def _check_scenario_multi_model(failures, verbose):
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


def run_selftest(verbose: bool = False) -> int:
    """Run the suite, then restore any adapter patch a raising check left installed.

    The checks themselves live in _run_selftest_checks. This wrapper exists only so the
    module-patch net (_MODULE_PATCHES) has ONE unconditional finally: the patches it
    guards span most of the copilot section, and an exception escaping mid-section used
    to leave e.g. a scoped _agent_definition_files bolted onto the adapter for the rest
    of the process — harmless for the CLI, which exits, but this module is importable and
    an embedding process would keep making adapter calls against the stub."""
    try:
        return _run_selftest_checks(verbose=verbose)
    finally:
        _restore_module_patches()


def _run_selftest_checks(verbose: bool = False) -> int:
    failures: list[str] = []

    _section(_check_cost_formatting, failures, verbose)
    _section(_check_claude_adapter, failures, verbose)
    _section(_check_codex_adapter, failures, verbose)
    _section(_check_copilot_adapter, failures, verbose)
    _section(_check_antigravity_adapter, failures, verbose)
    _section(_check_judge_verdict_extraction, failures, verbose)
    _section(_check_schema_validator, failures, verbose)
    _section(_check_progress_indicator, failures, verbose)
    _section(_check_spec_validation, failures, verbose)
    _section(_check_scenario_multi_model, failures, verbose)

    # HOME isolation overlay + side-effect-free provisioning
    _section(_check_isolation, failures, verbose)
    _section(_check_provision, failures, verbose)
    _section(_check_workspace_reset, failures, verbose)
    _section(_check_snake_case_keys, failures, verbose)
    _section(_check_antigravity_transcript, failures, verbose)

    # per-cell readable report
    _section(_check_report, failures, verbose)

    # artifact / trace path resolution (no false passes on seeded fixtures; workspace-relative;
    # symlink escapes via write-trace)
    _section(_check_path_resolution, failures, verbose)
    _section(_check_workspace_view_skill_dir_match, failures, verbose)

    # undeclared repo skills read via the real on-disk checkout (workspace-escape leak)
    _section(_check_leaked_skill_reads, failures, verbose)
    _section(_check_workspace_relocation, failures, verbose)
    # MCP hermeticity on the non-cell paths: masked-home overlay, probes, judge, fail-closed
    _section(_check_mcp_hermetic_paths, failures, verbose)
    _section(_check_parallel_cell_idx, failures, verbose)
    _section(_check_cell_crash_safety, failures, verbose)

    # cli.py's pure helpers (YAML-error detection, --model/--all-models, models.yaml validation)
    _section(_check_cli_helpers, failures, verbose)
    _section(_check_progress_thread_safety, failures, verbose)

    # real pass/fail behavior for every deterministic assertion type
    _section(_check_assertion_pass_fail, failures, verbose)

    # verdict coercion edge cases (string "false", extra items)
    _section(_check_verdict_coercion, failures, verbose)

    # timeout kills the agent's whole process group, not just the direct child
    _section(_check_exec_timeout_group_kill, failures, verbose)

    # CLI version drift: read from the run, tiered, and auditable
    _section(_check_copilot_version_provenance, failures, verbose)
    _section(_check_version_provenance_shared, failures, verbose)
    _section(_check_claude_version_provenance, failures, verbose)
    _section(_check_unreadable_version_adapters, failures, verbose)
    _section(_check_matrix_consistency, failures, verbose)
    _section(_check_parallel_requires_isolation, failures, verbose)
    _section(_check_codex_post_run_mcp_recheck, failures, verbose)

    # declared MCP servers: schema, secrets, injection, refusals
    _section(_check_mcp_declared_servers, failures, verbose)

    # report inlining is capped per file; the judge's skip behavior is unchanged
    _section(_check_inline_truncation, failures, verbose)

    # model-rejection annotation only fires on actual rejections
    _section(_check_model_error_heuristic, failures, verbose)

    # scenario run-knob overrides are type-checked at load
    _section(_check_scenario_override_validation, failures, verbose)

    # typed reasoning-effort knob: spec validation, Runner threading, per-adapter mapping
    _section(_check_reasoning_effort, failures, verbose)

    # deprecated pre-#67 module API (models= / .models / plain-id render columns)
    _section(_check_api_compat, failures, verbose)

    print()
    if failures:
        print(f"SELFTEST FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("SELFTEST PASSED")
    return 0


def _check_mcp_declared_servers(failures, verbose):
    """Declared `mcp_servers:` — schema, secrets, injection, and the refusals.

    The through-line is that every way this feature can be WRONG is louder than the way it
    is right. A misspelled key, a filter the runner cannot enforce, an adapter that cannot
    inject at all, an unset credential — each fails before tokens are spent, because the
    alternative in every case is a scenario that runs without the tool surface it declared
    and fails somewhere unrecognizable.
    """
    import io as _io
    import json as _json
    import os
    import shutil as _shutil
    import stat as _stat
    import sys as _sys
    import tempfile as _tempfile
    import threading as _threading

    from . import runner as runner_mod
    from . import xattrs as _xattrs
    from .adapters import get_adapter
    from .adapters.base import RunOptions
    from .mcp import (parse_mcp_servers, redact, resolve_mcp_servers,
                      validate_mcp_servers)

    print("mcp declared servers:")

    def _try(fn, default=None):
        """Run fn, turning any exception into `default`.

        Arms below assert on VALUES, and a mutation that makes production code raise
        instead of returning the wrong value would otherwise abort this whole check with a
        traceback — failing the selftest, but naming no invariant. Collapsing the raise
        into a falsy value keeps the report pointed at the broken guarantee.
        """
        try:
            return fn()
        except Exception:
            return default

    # --- structure: everything checkable without the environment ---------------
    rejected = {}
    for label, raw in [
        ("both_transports", {"e": {"command": "x", "url": "http://y"}}),
        ("neither_transport", {"e": {}}),
        ("claude_native_filter", {"e": {"command": "x", "allowedTools": ["a"]}}),
        ("codex_native_filter", {"e": {"command": "x", "enabled_tools": ["a"]}}),
        ("unknown_key", {"e": {"command": "x", "tolls": ["a"]}}),
        ("name_with_space", {"bad name": {"command": "x"}}),
        ("name_with_dunder", {"a__b": {"command": "x"}}),
        ("bad_transport", {"e": {"url": "http://y", "transport": "grpc"}}),
        ("stdio_key_on_remote", {"e": {"url": "http://y", "args": ["a"]}}),
        ("remote_key_on_stdio", {"e": {"command": "x", "headers": {"a": "b"}}}),
        ("not_a_mapping", {"e": ["command", "x"]}),
    ]:
        try:
            parse_mcp_servers(raw, where="t")
            rejected[label] = ""
        except ValueError as exc:
            rejected[label] = str(exc)
    _check("mcp.schema_rejects_every_malformed_server",
           all(rejected.values()),
           f"each malformed server shape is a load error, not a silent default — "
           f"{sorted(k for k, v in rejected.items() if not v) or 'all rejected'}",
           failures, verbose)

    # Rejection alone is not the guarantee here — an unknown-key error would also reject
    # these. What the native-filter list buys is that the author is told the RIGHT thing:
    # claude ACCEPTS `allowedTools` in an --mcp-config server object and silently ignores
    # it, so the error has to name `tools:` as the working replacement rather than leaving
    # someone to conclude the key was merely misspelled.
    _check("mcp.native_filter_spellings_are_refused_by_name",
           all("tools:" in rejected[k] and "silently IGNORES" in rejected[k]
               for k in ("claude_native_filter", "codex_native_filter")),
           f"a CLI-native filter spelling is refused with an error naming the portable "
           f"`tools:` field and saying the CLI would have ignored it — "
           f"{rejected['claude_native_filter'][:90]!r}", failures, verbose)

    # A name carrying `__` would make `mcp__a__b__c` un-splittable back into server and
    # tool — which is exactly the split the §6-C2 post-run allowlist check performs.
    _check("mcp.server_name_cannot_contain_the_tool_name_separator",
           rejected["name_with_dunder"],
           "'a__b' is refused as a server name because claude spells MCP tools "
           "mcp__<server>__<tool>; with a dunder in the name the two halves could not be "
           "told apart afterwards", failures, verbose)

    # --- interpolation, the secrets registry, and the redaction floor ----------
    remote = parse_mcp_servers(
        {"r": {"url": "https://h/${SEG}", "headers": {"Authorization": "Bearer ${TOK}"}}},
        where="t")
    env_ok = {"TOK": "sk-secret-abcdef", "SEG": "v1"}
    res, secrets = resolve_mcp_servers(remote, env=env_ok)
    missing_errs = _try(lambda: validate_mcp_servers(remote, env={"SEG": "v1"})[0], [])
    _short_warns = _try(
        lambda: validate_mcp_servers(remote, env={"TOK": "ab", "SEG": "v1"})[1], [])
    _, short_secrets = resolve_mcp_servers(remote, env={"TOK": "ab", "SEG": "v1"})

    _check("mcp.interpolates_from_process_env_and_registers_the_secret",
           res["r"].headers["Authorization"] == "Bearer sk-secret-abcdef"
           and res["r"].url == "https://h/v1"
           and secrets == {"sk-secret-abcdef"},
           f"${{VAR}} resolves in headers and the url, and the substituted VALUE (not the "
           f"whole field) is registered for redaction — {secrets}. Registering the field "
           f"would scrub 'Bearer ' out of unrelated text; registering the value scrubs the "
           f"credential", failures, verbose)

    _check("mcp.unset_variable_is_a_validation_error_naming_it",
           len(missing_errs) == 1 and "TOK" in missing_errs[0],
           f"an unset ${{VAR}} is reported by name before the run, not raised at load — "
           f"raising would abort discovery of every OTHER eval in the directory over one "
           f"unset variable: {missing_errs}", failures, verbose)

    _check("mcp.too_short_to_redact_is_warned_and_left_alone",
           short_secrets == set() and any("TOK" in w for w in _short_warns),
           f"a 2-character secret is NOT added to the redaction set ({short_secrets}) and "
           f"the author is told why — redacting it would rewrite every unrelated "
           f"occurrence of those characters across the artifacts", failures, verbose)

    # ...which makes the redaction set the wrong thing to ask "does this cell handle a
    # secret". The exclusion above is about what can safely be REWRITTEN, not about what is
    # confidential, and the two questions had been sharing one answer.
    from .mcp import interpolated_refs
    short_spec = parse_mcp_servers(
        {"r": {"url": "https://h", "headers": {"Authorization": "${TOK}"}}}, where="t")
    _, short_set = resolve_mcp_servers(short_spec, env={"TOK": "ab"})
    plain_spec = parse_mcp_servers({"e": {"command": "/bin/echo"}}, where="t")
    _check("mcp.short_interpolated_value_still_counts_as_a_credential",
           interpolated_refs(short_spec) == ["r.headers.Authorization"]
           and short_set == set() and interpolated_refs(plain_spec) == []
           and interpolated_refs(None) == [],
           f"a 2-character token is excluded from redaction and is still a credential, so "
           f"exposure decisions read the DECLARATION for `${{VAR}}` rather than counting "
           f"the redaction set: refs={interpolated_refs(short_spec)} "
           f"redactable={short_set}. A server declared with no `${{VAR}}` at all handles "
           f"nothing confidential and must not be treated as if it did", failures, verbose)

    # `${VAR}` is honoured in credentials, never in `command`/`args`: substituting there
    # would turn an environment variable into a way to choose what program executes.
    stdio_var = parse_mcp_servers({"e": {"command": "${EVIL}", "args": ["${EVIL}"]}},
                                  where="t")
    res_v, sec_v = resolve_mcp_servers(stdio_var, env={"EVIL": "/bin/sh"})
    _check("mcp.interpolation_cannot_choose_what_program_runs",
           res_v["e"].command == "${EVIL}" and res_v["e"].args == ["${EVIL}"]
           and sec_v == set(),
           f"${{VAR}} stays literal in command/args ({res_v['e'].command!r}) — it is a "
           f"credential mechanism, and honouring it there would let an env var select the "
           f"executable, a categorically larger power than supplying a token",
           failures, verbose)

    # --- per-adapter: who can inject, who can enforce `tools:` -----------------
    plain = parse_mcp_servers({"e": {"command": "python3"}}, where="t")
    gated = parse_mcp_servers({"e": {"command": "python3", "tools": ["echo"]}}, where="t")
    inject = {a: len(get_adapter(a).validate_mcp_support(plain)[0]) for a in
              ("claude", "codex", "copilot", "antigravity")}
    tools_claude = get_adapter("claude").validate_mcp_support(gated)[0]

    _check("mcp.adapters_without_injection_refuse_rather_than_drop_the_servers",
           inject["claude"] == 0 and all(inject[a] == 1 for a in
                                         ("codex", "copilot", "antigravity")),
           f"only claude accepts declared servers today, and the other three REFUSE the "
           f"run instead of silently running without them — {inject}. Dropping them would "
           f"grade a scenario that never had the tools it asked for, and every MCP "
           f"assertion would fail for a reason that looks nothing like the cause",
           failures, verbose)

    _check("mcp.claude_refuses_tools_it_cannot_enforce",
           len(tools_claude) == 1 and "C3" in tools_claude[0]
           and "tools:" in tools_claude[0],
           f"`tools:` on claude is a validation ERROR, not an accepted no-op: the only "
           f"mechanism is deny-the-complement, which needs a tool list obtainable only "
           f"from a second server instance that can answer differently than the one claude "
           f"launches (§6-C2). The error points at C3 — {tools_claude}", failures, verbose)

    # --- claude's config materialization --------------------------------------
    scratch = _tempfile.mkdtemp(prefix="ase-mcptest-")
    cl = get_adapter("claude")
    resolved, _ = resolve_mcp_servers(
        parse_mcp_servers({"echo": {"command": "python3", "args": ["s.py"],
                                    "env": {"K": "V"}}}, where="t"), env={})
    argv = _try(lambda: cl.build_argv(
        "hi", RunOptions(mcp_servers=resolved, mcp_scratch_dir=scratch), cwd=scratch), [])
    cfg_path = os.path.join(scratch, "mcp.json")
    cfg = _try(lambda: _json.loads(open(cfg_path).read()))
    mode = _try(lambda: os.stat(cfg_path).st_mode & 0o777)

    _check("mcp.claude_writes_a_file_not_inline_json",
           _flag_pair(argv, "--mcp-config", cfg_path)
           and cfg == {"mcpServers": {"echo": {"command": "python3", "args": ["s.py"],
                                               "env": {"K": "V"}}}},
           f"the config goes to a FILE named on argv, never inline: argv is archived "
           f"verbatim into result.json, so inline JSON would publish every resolved "
           f"credential into the artifacts — {cfg}", failures, verbose)

    _check("mcp.claude_config_is_not_world_readable",
           mode == 0o600,
           f"the scratch config is created 0600 in one step (O_CREAT with the mode, not "
           f"write-then-chmod, which would leave a window where the credentials are "
           f"readable) — got {oct(mode)}", failures, verbose)

    # Any exception counts as "refused", but the MESSAGE has to explain itself: a bare
    # TypeError from joining None would also stop the run, and would tell the operator
    # nothing about why writing credentials into the workspace is refused.
    no_scratch = None
    try:
        cl.build_argv("hi", RunOptions(mcp_servers=resolved), cwd=scratch)
    except Exception as exc:
        no_scratch = str(exc)
    _check("mcp.claude_refuses_to_write_secrets_without_a_scratch_dir",
           no_scratch is not None and "workspace" in no_scratch,
           f"with no scratch dir the adapter RAISES rather than falling back to the "
           f"workspace — the workspace is archived into artifacts and inlined into "
           f"report.md, so that fallback would publish the credentials: {no_scratch!r}",
           failures, verbose)

    # `--strict-mcp-config` must survive injection, or the declared servers stop being the
    # ONLY servers and the opt-in silently becomes an opt-in-plus-whatever-the-host-has.
    _check("mcp.declared_servers_stay_hermetic",
           "--strict-mcp-config" in argv,
           "--mcp-config is paired with --strict-mcp-config, so declaring servers grants "
           "exactly those and not the user's ambient ones", failures, verbose)

    # --- the witness now permits declared servers, and only those --------------
    init_echo = _json.dumps({"type": "system", "subtype": "init",
                             "mcp_servers": [{"name": "echo", "status": "connected"}],
                             "claude_code_version": "2.1.113"})
    declared_ok = None
    try:
        cl.verify_post_run([], RunOptions(mcp_servers=resolved), cwd=scratch,
                           stdout=init_echo, stderr="", exit_code=0)
    except RuntimeError as exc:
        declared_ok = str(exc)
    undeclared = None
    try:
        cl.verify_post_run([], RunOptions(mcp_servers=resolved), cwd=scratch,
                           stdout=_json.dumps(
                               {"type": "system", "subtype": "init",
                                "mcp_servers": [{"name": "echo"}, {"name": "sneaky"}],
                                "claude_code_version": "2.1.113"}),
                           stderr="", exit_code=0)
    except RuntimeError as exc:
        undeclared = str(exc)
    none_opts = None
    try:
        cl.verify_post_run([], None, cwd=scratch, stdout=init_echo, stderr="",
                           exit_code=0)
    except RuntimeError as exc:
        none_opts = str(exc)

    _check("mcp.witness_permits_declared_servers_and_only_those",
           declared_ok is None and undeclared is not None
           and "sneaky" in undeclared and "echo" not in undeclared.split("server(s)")[1][:20],
           f"a DECLARED server no longer fails the run (was: any named server was a "
           f"kill-switch violation, so every intentional server failed closed), while an "
           f"UNDECLARED one still does and is named — declared={declared_ok!r} "
           f"undeclared={undeclared!r}", failures, verbose)

    _check("mcp.witness_without_options_treats_everything_as_undeclared",
           none_opts is not None,
           f"called with no RunOptions (selftest, out-of-tree callers) the witness reads "
           f"'nothing was declared' and fails on any reported server — defaulting the "
           f"other way would let a missing argument silently permit any server at all: "
           f"{none_opts!r}", failures, verbose)

    # --- redaction reaches the artifacts, including nested and argv ------------
    root = _tempfile.mkdtemp(prefix="ase-mcpred-")
    r = runner_mod.Runner("claude", models=["m"], artifacts_root=os.path.join(root, "a"),
                          run_id="c", skills_root=root)
    r._secrets = ("sk-secret-abcdef",)
    cell_dir = os.path.join(root, "cell")
    os.makedirs(cell_dir, exist_ok=True)
    r._rw(os.path.join(cell_dir, "t.txt"), "tool said sk-secret-abcdef back")
    r._rwj(os.path.join(cell_dir, "t.json"),
           {"argv": ["x", "--header", "Bearer sk-secret-abcdef"],
            "events": [{"raw": {"deep": {"nested": "sk-secret-abcdef"}}}]})
    txt = open(os.path.join(cell_dir, "t.txt")).read()
    js = open(os.path.join(cell_dir, "t.json")).read()
    js_obj = _json.loads(js)

    _check("mcp.secrets_are_scrubbed_from_every_artifact_shape",
           "sk-secret-abcdef" not in txt and "sk-secret-abcdef" not in js
           and js_obj["argv"][2] == "Bearer «redacted»"
           and js_obj["events"][0]["raw"]["deep"]["nested"] == "«redacted»",
           f"redaction happens at the WRITERS, so it reaches argv, arbitrarily nested "
           f"event payloads, and plain text alike without knowing any of their shapes — "
           f"and covers the case no CLI flag can, a tool RESULT echoing a token back into "
           f"the transcript. Structure survives: {js_obj['argv']}", failures, verbose)

    # --- the same scrub, on secrets the JSON encoder RE-SPELLS -----------------
    # Review reproduced this leak in both `_rwj` and the JSONL text path: the scrub used to
    # search the SERIALIZED form for the RAW value, so any secret containing a quote, a
    # backslash, a control character, or a non-ASCII byte was stored in an escaped spelling
    # the search could not match, and sailed through in plain view. These are not exotic
    # characters for a credential — a base64 secret can contain `/` and `+`, and a passphrase
    # can contain anything at all.
    tricky = ['tok"quote-abcdef', "tok\\slash-abcdef", "tökén-abcdef", "tok\nnewline-abcdef"]
    r._secrets = tuple(tricky)
    # The text path gets the secrets already JSON-ESCAPED, which is how they arrive in a
    # CLI's own stdout.jsonl stream — the artifact this harness copies rather than authors.
    r._rw(os.path.join(cell_dir, "esc.jsonl"),
          "\n".join(_json.dumps({"result": t}) for t in tricky))
    r._rwj(os.path.join(cell_dir, "esc.json"), {"argv": list(tricky), "n": {"deep": tricky}})
    esc_txt = open(os.path.join(cell_dir, "esc.jsonl")).read()
    esc_js = open(os.path.join(cell_dir, "esc.json")).read()
    esc_obj = _json.loads(esc_js)
    # Checked against BOTH spellings: the raw value and the encoder's version of it. Testing
    # only the raw one would pass against the very bug this arm exists for.
    escaped_forms = [_json.dumps(t)[1:-1] for t in tricky]
    _check("mcp.redaction_survives_json_escaping",
           all(t not in esc_txt and t not in esc_js for t in tricky)
           and all(e not in esc_txt and e not in esc_js for e in escaped_forms)
           and esc_obj["argv"] == ["«redacted»"] * len(tricky),
           f"a secret containing a quote, backslash, control character or non-ASCII byte "
           f"is scrubbed in every spelling it can reach disk in — the structured writer "
           f"compares BEFORE serialization, and the text writer also matches the escaped "
           f"form a CLI's own JSONL stream carries. argv={esc_obj['argv']}",
           failures, verbose)

    # Keys, not just values: a credential can be a dict key (an env map keyed by token, a
    # header name) as easily as a leaf, and a walk that only visits values would miss it.
    r._secrets = ("sk-secret-abcdef",)
    r._rwj(os.path.join(cell_dir, "key.json"), {"sk-secret-abcdef": "v", "x": object()})
    key_obj = _json.loads(open(os.path.join(cell_dir, "key.json")).read())
    _check("mcp.redaction_covers_dict_keys_and_stringified_leaves",
           "«redacted»" in key_obj and "sk-secret-abcdef" not in key_obj,
           f"dict KEYS are scrubbed alongside values, and a leaf the encoder would render "
           f"via default=str is rendered scrubbed — {sorted(key_obj)}", failures, verbose)

    # A value that contains another must not be left half-rewritten by the shorter one.
    overlap = redact("token=abcdef123456 short=abcdef",
                     {"abcdef", "abcdef123456"})
    _check("mcp.longest_secret_is_redacted_first",
           overlap == "token=«redacted» short=«redacted»",
           f"overlapping secrets are replaced longest-first, so the longer value is not "
           f"left as '«redacted»123456' by the shorter one's pass — {overlap!r}",
           failures, verbose)

    # --- the archived workspace, the one artifact the runner does not WRITE ----
    # It is moved into the artifact tree wholesale, so it never met `_rw`/`_rwj` and review
    # found it kept credentials every other artifact had scrubbed: an MCP result can echo a
    # token back and the agent can save it to a file.
    ws = os.path.join(root, "ws")
    os.makedirs(os.path.join(ws, "sub"), exist_ok=True)
    open(os.path.join(ws, "notes.txt"), "w").write("the token is sk-secret-abcdef\n")
    open(os.path.join(ws, "sub", "creds-sk-secret-abcdef.txt"), "w").write("x")
    # Binary: a decode-then-scrub implementation skips or corrupts this one silently.
    open(os.path.join(ws, "blob.bin"), "wb").write(b"\x00\xff" + b"sk-secret-abcdef" + b"\x00")
    outside = os.path.join(root, "outside.txt")
    open(outside, "w").write("sk-secret-abcdef untouched\n")
    os.symlink(outside, os.path.join(ws, "link.txt"))
    runner_mod._scrub_tree(ws, ("sk-secret-abcdef",))
    ws_txt = open(os.path.join(ws, "notes.txt")).read()
    ws_bin = open(os.path.join(ws, "blob.bin"), "rb").read()
    ws_names = sorted(os.listdir(os.path.join(ws, "sub")))
    _check("mcp.archived_workspace_is_scrubbed",
           "sk-secret-abcdef" not in ws_txt and b"sk-secret-abcdef" not in ws_bin
           and ws_bin.startswith(b"\x00\xff") and ws_names == ["creds-«redacted».txt"],
           f"a credential the agent wrote into its workspace is scrubbed from file "
           f"CONTENTS (text and binary alike, on bytes, so an undecodable file is not "
           f"skipped) and from file NAMES — a redacted file whose own path spells out the "
           f"token would be a scrub that only looks complete: {ws_names}",
           failures, verbose)

    _check("mcp.workspace_scrub_does_not_follow_symlinks",
           open(outside).read() == "sk-secret-abcdef untouched\n"
           and os.path.islink(os.path.join(ws, "link.txt")),
           f"symlinks are not traversed: the target can sit outside the artifact tree, and "
           f"reading through one would let a link the agent created pull arbitrary external "
           f"content INTO the archive — the link is still a link afterwards, not a copy of "
           f"what it pointed at: still_a_link={os.path.islink(os.path.join(ws, 'link.txt'))}",
           failures, verbose)

    # A HARDLINK has no target to refuse to follow — it IS the file, sharing one inode with
    # every other name pointing at it. Review demonstrated an in-place rewrite reaching
    # straight out of the artifact tree and overwriting an external file with «redacted».
    ws2 = os.path.join(root, "ws2")
    os.makedirs(ws2, exist_ok=True)
    hard_outside = os.path.join(root, "hard-outside.txt")
    open(hard_outside, "w").write("sk-secret-abcdef must survive\n")
    before_ino = os.stat(hard_outside).st_ino
    os.link(hard_outside, os.path.join(ws2, "hard.txt"))
    runner_mod._scrub_tree(ws2, ("sk-secret-abcdef",))
    hard_inside = open(os.path.join(ws2, "hard.txt")).read()
    _check("mcp.workspace_scrub_breaks_hardlinks_instead_of_writing_through_them",
           open(hard_outside).read() == "sk-secret-abcdef must survive\n"
           and os.stat(hard_outside).st_ino == before_ino
           and "sk-secret-abcdef" not in hard_inside,
           f"the archived copy is scrubbed while the file the agent hardlinked to keeps its "
           f"contents AND its inode — the replacement is written beside the original and "
           f"renamed over it, so the artifact tree gets a new inode rather than the scrub "
           f"mutating shared storage outside it: inside={hard_inside.strip()!r}",
           failures, verbose)

    # A symlink's TARGET STRING lives in the tree even though its contents do not, and
    # `readlink` reads it straight back — an innocuously named link is as good a hiding
    # place for a credential as a file containing one. Not following it is not scrubbing it.
    ws3 = os.path.join(root, "ws3")
    os.makedirs(ws3, exist_ok=True)
    os.symlink("/tmp/sk-secret-abcdef/creds", os.path.join(ws3, "innocent"))
    runner_mod._scrub_tree(ws3, ("sk-secret-abcdef",))
    link_target = os.readlink(os.path.join(ws3, "innocent"))
    _check("mcp.symlink_target_is_scrubbed_even_though_it_is_not_followed",
           "sk-secret-abcdef" not in link_target and "«redacted»" in link_target,
           f"the stored target of a symlink is redacted in place: skipping TRAVERSAL is "
           f"correct (it can point outside the tree) but says nothing about the link's own "
           f"metadata, which is archived and readable: {link_target!r}", failures, verbose)

    # `os.walk` reports an unreadable directory to `onerror` and then yields NOTHING for it,
    # which is indistinguishable from an empty one — a chmod 000 subtree was skipped in
    # silence while the scrub reported success.
    ws4 = os.path.join(root, "ws4")
    hidden = os.path.join(ws4, "hidden")
    os.makedirs(hidden, exist_ok=True)
    open(os.path.join(hidden, "buried.txt"), "w").write("buried sk-secret-abcdef\n")
    os.chmod(hidden, 0o000)
    lost4 = runner_mod._scrub_tree(ws4, ("sk-secret-abcdef",))
    _try(lambda: os.chmod(hidden, 0o700))  # a regression QUARANTINES it; don't crash the section
    buried = _try(lambda: open(os.path.join(hidden, "buried.txt")).read(), "(unreadable)")
    _check("mcp.unreadable_subtree_is_opened_rather_than_silently_skipped",
           "sk-secret-abcdef" not in buried and lost4 == [],
           f"permissions are repaired before the walk so an inaccessible subtree is scrubbed "
           f"rather than passed over — the harness owns this tree, so they are its to "
           f"restore, and an empty return means every byte really was examined: "
           f"buried={buried.strip()!r} lost={lost4}", failures, verbose)

    # A secret containing a separator spells itself out across a directory and its child
    # while every individual component looks clean, so a per-component scrub certifies a
    # tree whose own `find` output is the credential.
    ws5 = os.path.join(root, "ws5")
    os.makedirs(os.path.join(ws5, "tenantpart"), exist_ok=True)
    open(os.path.join(ws5, "tenantpart", "secretpart"), "w").write("harmless\n")
    runner_mod._scrub_tree(ws5, ("tenantpart/secretpart",))
    ws5_paths = sorted(os.path.relpath(os.path.join(d, f), ws5)
                       for d, _, fs in os.walk(ws5) for f in fs)
    _check("mcp.secret_spanning_path_components_is_scrubbed",
           not any("tenantpart/secretpart" in p for p in ws5_paths),
           f"a slash-bearing secret is caught by checking the ASSEMBLED path, not one "
           f"component at a time — the component that COMPLETES it is renamed, leaving the "
           f"prefix (which is not itself a secret) alone: {ws5_paths}", failures, verbose)

    # Fail closed on what cannot be certified. An artifact the scrub could not read or
    # rewrite is deleted rather than published unchecked, and the caller is told which —
    # a silent deletion would be worse than either outcome it is choosing between.
    ws6 = os.path.join(root, "ws6")
    os.makedirs(ws6, exist_ok=True)
    open(os.path.join(ws6, "leaky.txt"), "w").write("tok sk-secret-abcdef\n")
    open(os.path.join(ws6, "fine.txt"), "w").write("nothing to see\n")
    _orig_scrub_file = runner_mod._scrub_file
    runner_mod._scrub_file = lambda p, s: (
        (_ for _ in ()).throw(OSError(30, "Read-only file system"))
        if p.endswith("leaky.txt") else _orig_scrub_file(p, s))
    try:
        lost6 = runner_mod._scrub_tree(ws6, ("sk-secret-abcdef",))
    finally:
        runner_mod._scrub_file = _orig_scrub_file
    _check("mcp.uncertifiable_artifact_is_removed_and_named",
           lost6 == ["leaky.txt"]
           and not os.path.exists(os.path.join(ws6, "leaky.txt"))
           and os.path.exists(os.path.join(ws6, "fine.txt"))
           and "leaky.txt" in runner_mod._scrub_note(lost6),
           f"a file the scrub cannot rewrite is removed from the archive and RETURNED to "
           f"the caller, which fails the cell with it named — the rest of the workspace is "
           f"untouched, so failing closed costs one artifact rather than the run's whole "
           f"evidence: lost={lost6}", failures, verbose)

    # `os.path.isdir` FOLLOWS symlinks, so an agent that replaces its whole workspace with a
    # link aims the entire scrub at whatever it points at — review had every file under an
    # external directory rewritten to «redacted».
    ws7 = os.path.join(root, "ws7")
    ws7_target = os.path.join(root, "ws7-external")
    os.makedirs(ws7_target, exist_ok=True)
    open(os.path.join(ws7_target, "creds.txt"), "w").write("sk-secret-abcdef stays\n")
    os.symlink(ws7_target, ws7)
    lost7 = runner_mod._scrub_tree(ws7, ("sk-secret-abcdef",))
    _check("mcp.workspace_root_must_itself_be_a_real_directory",
           open(os.path.join(ws7_target, "creds.txt")).read() == "sk-secret-abcdef stays\n"
           and lost7 and not os.path.islink(ws7) and os.path.isdir(ws7),
           f"a workspace root that is not a real directory is dropped whole rather than "
           f"walked — following it would let the agent point the scrub at any directory on "
           f"the machine and rewrite it — and an empty directory is left behind so the "
           f"report writer downstream still finds the shape it expects: lost={lost7}",
           failures, verbose)

    # Reading a 000-mode file needs a chmod, and a chmod changes the INODE — which is the
    # same object every hardlink to it sees. Review watched an external file go 000 -> 600.
    ws8 = os.path.join(root, "ws8")
    os.makedirs(ws8, exist_ok=True)
    locked_outside = os.path.join(root, "locked-outside.txt")
    open(locked_outside, "w").write("sk-secret-abcdef stays\n")
    os.link(locked_outside, os.path.join(ws8, "locked.txt"))
    os.chmod(locked_outside, 0o000)
    lost8 = runner_mod._scrub_tree(ws8, ("sk-secret-abcdef",))
    mode8 = _stat.S_IMODE(os.stat(locked_outside).st_mode)
    os.chmod(locked_outside, 0o600)
    _check("mcp.permission_repair_never_widens_a_shared_inode",
           mode8 == 0o000 and lost8 == ["locked.txt"]
           and not os.path.exists(os.path.join(ws8, "locked.txt")),
           f"an unreadable file with more than one name is quarantined rather than chmod-ed "
           f"open: widening the mode would be a change visible through every OTHER name for "
           f"that inode, i.e. outside the artifact tree, so the harness drops its own name "
           f"and leaves the rest alone: external mode={mode8:04o} lost={lost8}",
           failures, verbose)

    # "Not a directory" is not the same claim as "a readable regular file". A FIFO makes
    # `open(...).read()` wait for a writer that will never come, and review hung the scrub.
    ws9 = os.path.join(root, "ws9")
    os.makedirs(ws9, exist_ok=True)
    os.mkfifo(os.path.join(ws9, "pipe"))
    open(os.path.join(ws9, "ordinary.txt"), "w").write("tok sk-secret-abcdef\n")
    box9: dict = {}
    t9 = _threading.Thread(
        target=lambda: box9.update(r=_try(lambda: runner_mod._scrub_tree(
            ws9, ("sk-secret-abcdef",)), "(raised)")), daemon=True)
    t9.start()
    t9.join(20.0)
    ord9 = _try(lambda: open(os.path.join(ws9, "ordinary.txt")).read(), "(unreadable)")
    _check("mcp.special_files_are_removed_rather_than_read",
           not t9.is_alive() and box9.get("r") == ["pipe"]
           and not os.path.lexists(os.path.join(ws9, "pipe"))
           and "sk-secret-abcdef" not in ord9,
           f"entries are classified with `lstat`, so a FIFO/socket/device is removed and "
           f"named instead of being opened — none of them holds archivable bytes and an "
           f"`open` on one never returns, which would hang the whole run rather than fail "
           f"it: returned={box9.get('r')} still_running={t9.is_alive()}", failures, verbose)

    # On macOS a file's bytes are not all in the file: `xattr -w` parks a credential beside
    # the data where no `read()` will show it, and `cp -p`/`ditto`/zip carry it along.
    ws10 = os.path.join(root, "ws10")
    ws10_sub = os.path.join(ws10, "sub")
    os.makedirs(ws10_sub, exist_ok=True)
    ws10_file = os.path.join(ws10, "plain.txt")
    open(ws10_file, "w").write("nothing to see\n")
    ws10_link = os.path.join(ws10, "plain.link")
    os.symlink("plain.txt", ws10_link)
    _try(lambda: _xattrs.setxattr(ws10_file, b"user.tok", b"sk-secret-abcdef"))
    _try(lambda: _xattrs.setxattr(ws10_file, b"user.sk-secret-abcdef", b"in the NAME"))
    _try(lambda: _xattrs.setxattr(ws10_sub, b"user.tok", b"sk-secret-abcdef"))
    _try(lambda: _xattrs.setxattr(ws10_link, b"user.tok", b"sk-secret-abcdef"))
    lost10 = runner_mod._scrub_tree(ws10, ("sk-secret-abcdef",))
    left10 = []
    for p in (ws10_file, ws10_sub, ws10_link):
        for n in _try(lambda: _xattrs.listxattr(p), []):
            left10.append((os.path.basename(p), n, _try(lambda: _xattrs.getxattr(p, n), b"")))
    _check("mcp.extended_attributes_are_scrubbed_like_contents",
           _xattrs.SUPPORTED and lost10 == []
           and not any(b"sk-secret-abcdef" in n or b"sk-secret-abcdef" in v
                       for _, n, v in left10)
           and any(b"redacted" in n or b"redacted" in v for _, n, v in left10),
           f"attribute VALUES and attribute NAMES are both scrubbed, on files, directories "
           f"and symlinks alike — metadata is archived with the tree and is invisible to "
           f"every check that reads a file's contents, so `lost == []` would otherwise be "
           f"certifying bytes it never looked at: {left10}", failures, verbose)

    # `chflags uchg` makes a file undeletable, and the old quarantine swallowed the failure:
    # it reported the path as lost while leaving the raw secret sitting there.
    ws11 = os.path.join(root, "ws11")
    os.makedirs(ws11, exist_ok=True)
    stuck11 = os.path.join(ws11, "immutable.txt")
    open(stuck11, "w").write("tok sk-secret-abcdef\n")
    _orig_scrub_file = runner_mod._scrub_file
    runner_mod._scrub_file = lambda p, s: (
        (_ for _ in ()).throw(OSError(30, "Read-only file system"))
        if p.endswith("immutable.txt") else _orig_scrub_file(p, s))
    _try(lambda: os.chflags(stuck11, _stat.UF_IMMUTABLE))
    try:
        lost11 = _try(lambda: runner_mod._scrub_tree(ws11, ("sk-secret-abcdef",)), "(raised)")
    finally:
        runner_mod._scrub_file = _orig_scrub_file
        _try(lambda: os.chflags(stuck11, 0))  # never leave the selftest root undeletable
    _check("mcp.quarantine_proves_the_deletion_rather_than_assuming_it",
           lost11 == ["immutable.txt"] and not os.path.lexists(stuck11),
           f"an immutable artifact is unlocked and actually removed — the result is the "
           f"answer to `lexists`, not the absence of an exception, because a swallowed "
           f"`unlink` failure reports a deletion that did not happen and publishes the "
           f"secret under a clean-looking `lost` entry: lost={lost11} "
           f"still_there={os.path.lexists(stuck11)}", failures, verbose)

    # And when even that fails, the difference between "removed" and "still on disk" is the
    # whole point — reporting the second as the first is the failure mode being fixed.
    ws12 = os.path.join(root, "ws12")
    os.makedirs(ws12, exist_ok=True)
    open(os.path.join(ws12, "welded.txt"), "w").write("tok sk-secret-abcdef\n")
    _orig_remove = runner_mod._remove
    runner_mod._scrub_file = lambda p, s: (_ for _ in ()).throw(OSError(30, "Read-only"))
    runner_mod._remove = lambda p: False
    try:
        note12 = runner_mod._scrub_and_note(ws12, ("sk-secret-abcdef",))
        raised12 = _try(lambda: runner_mod._scrub_tree(ws12, ("sk-secret-abcdef",)), "(raised)")
    finally:
        runner_mod._scrub_file, runner_mod._remove = _orig_scrub_file, _orig_remove
    _check("mcp.unremovable_leak_is_reported_as_a_leak_not_as_a_removal",
           raised12 == "(raised)" and "welded.txt" in note12
           and "still contains a declared secret" in note12,
           f"an artifact that can be neither scrubbed nor deleted raises rather than "
           f"joining the `lost` list, and the sentence the cell carries says the secret is "
           f"STILL THERE instead of claiming a removal — a `lost` entry promises the "
           f"artifact is gone: note={note12!r}", failures, verbose)

    # One round of assembled-path reasoning is not enough: with two secrets declared,
    # renaming a parent to remove the first CREATED the second across the new parent and an
    # untouched child the bottom-up walk had already gone past.
    # The second pair is independent of the first and forces a SECOND round: one pass over
    # the tree repairs one spelling, so a check that runs once leaves the other standing.
    ws13 = os.path.join(root, "ws13")
    os.makedirs(os.path.join(ws13, "secretAAAAtailpart"), exist_ok=True)
    os.makedirs(os.path.join(ws13, "otherpart"), exist_ok=True)
    open(os.path.join(ws13, "secretAAAAtailpart", "childsecret"), "w").write("harmless\n")
    open(os.path.join(ws13, "otherpart", "othersecret"), "w").write("harmless\n")
    ws13_secrets = ("secretAAAA", "tailpart/childsecret", "otherpart/othersecret")
    lost13 = runner_mod._scrub_tree(ws13, ws13_secrets)
    ws13_paths = sorted(os.path.relpath(os.path.join(d, n), ws13).replace(os.sep, "/")
                        for d, ds, fs in os.walk(ws13) for n in ds + fs)
    _check("mcp.assembled_path_check_runs_to_a_fixed_point",
           lost13 == []
           and not any(runner_mod._spells_secret(p, ws13_secrets) for p in ws13_paths),
           f"the tree is re-examined until no assembled path spells ANY declared secret, "
           f"rather than each component being judged once on the way past — scrubbing one "
           f"secret out of a name can complete a different one with a neighbour that has "
           f"already been visited, and repairing one spelling per pass leaves every other "
           f"one standing: {ws13_paths}", failures, verbose)

    # `os.link(src, dst, follow_symlinks=False)` works on macOS, so "a symlink is not an
    # object an unprivileged agent can share" was wrong: an external link hardlinked into the
    # workspace had its metadata rewritten through the shared inode by an in-place setxattr.
    ws14 = os.path.join(root, "ws14")
    os.makedirs(ws14, exist_ok=True)
    link_outside = os.path.join(root, "link-outside")
    _try(lambda: os.symlink("/tmp/sk-secret-abcdef/creds", link_outside))
    _try(lambda: _xattrs.setxattr(link_outside, b"user.tok", b"sk-secret-abcdef"))
    ws14_copy = os.path.join(ws14, "copy")
    shared14 = _try(lambda: (os.link(link_outside, ws14_copy, follow_symlinks=False),
                             os.lstat(link_outside).st_ino == os.lstat(ws14_copy).st_ino)[1],
                    "(cannot hardlink a symlink here)")
    lost14 = runner_mod._scrub_tree(ws14, ("sk-secret-abcdef",))
    out14 = (_try(lambda: os.readlink(link_outside), ""),
             _try(lambda: _xattrs.getxattr(link_outside, b"user.tok"), b""))
    in14 = (_try(lambda: os.readlink(ws14_copy), ""),
            _try(lambda: [_xattrs.getxattr(ws14_copy, n) for n in _xattrs.listxattr(ws14_copy)],
                 []))
    _check("mcp.multiply_linked_symlink_is_replaced_not_edited",
           shared14 is True and lost14 == []
           and out14 == ("/tmp/sk-secret-abcdef/creds", b"sk-secret-abcdef")
           and os.path.islink(ws14_copy) and "sk-secret-abcdef" not in in14[0]
           and not any(b"sk-secret-abcdef" in v for v in in14[1])
           and os.lstat(link_outside).st_ino != os.lstat(ws14_copy).st_ino,
           f"a symlink is scrubbed by BUILDING A REPLACEMENT and renaming it over the old "
           f"name, never by editing in place — a symlink CAN be hardlinked, so an in-place "
           f"`setxattr` writes through to every other name for that inode, exactly as an "
           f"in-place content rewrite did for regular files: shared_before={shared14} "
           f"outside={out14} archived={in14} lost={lost14}", failures, verbose)

    # Absence and refusal are different answers. A bare `except OSError: return []` gave them
    # the same one, certifying a workspace it could not even stat.
    ws15 = os.path.join(root, "ws15")
    ws15_ws = os.path.join(ws15, "workspace")
    os.makedirs(ws15_ws, exist_ok=True)
    open(os.path.join(ws15_ws, "leak.txt"), "w").write("tok sk-secret-abcdef\n")
    os.chmod(ws15, 0o000)
    try:
        lost15 = _try(lambda: runner_mod._scrub_tree(ws15_ws, ("sk-secret-abcdef",)), "(raised)")
    finally:
        _try(lambda: os.chmod(ws15, 0o700))
    body15 = _try(lambda: open(os.path.join(ws15_ws, "leak.txt")).read(), "(unreadable)")
    missing15 = runner_mod._scrub_tree(os.path.join(root, "ws15-never-existed"),
                                       ("sk-secret-abcdef",))
    _check("mcp.unreadable_root_is_repaired_or_reported_never_certified",
           lost15 == [] and "sk-secret-abcdef" not in body15 and missing15 == [],
           f"a root that cannot be stat-ed is repaired through its parent — `cell_dir` is "
           f"this harness's own directory — and only a genuine ENOENT returns clean, because "
           f"'nothing was archived' and 'I was refused' are opposite answers that an "
           f"`except OSError` collapses into a clean bill of health: lost={lost15} "
           f"body={body15.strip()!r} absent_root={missing15}", failures, verbose)

    # The scratch dir holds the CLI config file carrying the INTERPOLATED credentials — the
    # one place on disk where a resolved `${VAR}` is written in the clear — and it was
    # removed with the same `ignore_errors=True` that let a locked exec dir survive a cell.
    ws16 = os.path.join(root, "ws16")
    os.makedirs(ws16, exist_ok=True)
    open(os.path.join(ws16, "mcp.json"), "w").write('{"token": "sk-secret-abcdef"}')
    os.chmod(ws16, 0o000)
    gone16 = runner_mod._purge("the MCP scratch directory", ws16)
    _try(lambda: os.chmod(ws16, 0o700))
    ws17 = os.path.join(root, "ws17")
    os.makedirs(ws17, exist_ok=True)
    _orig_remove = runner_mod._remove
    runner_mod._remove = lambda p: False
    try:
        stuck17 = runner_mod._purge("the MCP scratch directory", ws17)
    finally:
        runner_mod._remove = _orig_remove
    absent17 = runner_mod._purge("the MCP scratch directory",
                                 os.path.join(root, "ws17-never-existed"))
    _check("mcp.credential_scratch_dir_removal_is_verified_not_best_effort",
           gone16 == "" and not os.path.exists(ws16)
           and ws17 in stuck17 and "resolved credentials" in stuck17 and absent17 == "",
           f"a locked credentials directory is removed through the same outward-in "
           f"escalation the workspace quarantine uses, and one that genuinely cannot go "
           f"returns a sentence NAMING it rather than an empty answer — `rmtree(..., "
           f"ignore_errors=True)` reports on the call, not on the directory: "
           f"locked_removed={not os.path.exists(ws16)} stuck={stuck17[:80]!r} "
           f"absent={absent17!r}", failures, verbose)

    # --- the run summary, written after every cell cleared its secrets ---------
    # `_secrets` is deliberately cell-scoped, so by the time the summary is written it is
    # empty — review reproduced a credential republished through both summary.json and
    # summary.md via `RunResult.error`, which carries a tail of the child's output.
    srun = os.path.join(root, "summ")
    r2 = runner_mod.Runner("claude", models=["m"], artifacts_root=srun, run_id="s",
                           skills_root=root)
    r2._secrets = ("sk-secret-abcdef",)
    r2._run_secrets = ("sk-secret-abcdef",)
    r2._secrets = ()          # exactly what _run_cell's finally leaves behind
    err_rr = runner_mod.RunResult(agent="claude", eval_name="e", prompt="", workdir="")
    err_rr.error = "child failed: Authorization: Bearer sk-secret-abcdef"
    scell = runner_mod.CellResult(agent="claude", model="m", eval_name="e", skill=None,
                                  passed=False, run_result=err_rr,
                                  artifacts_dir=os.path.join(srun, "s", "c0"))
    _serr = _io.StringIO()
    _saved, _sys.stderr = _sys.stderr, _serr
    try:
        r2._write_summary([scell], [])
    finally:
        _sys.stderr = _saved
    sum_js = open(os.path.join(srun, "s", "summary.json")).read()
    sum_md = open(os.path.join(srun, "s", "summary.md")).read()
    # Parsed, not grepped: `_write_json` writes with ensure_ascii, so the marker itself
    # lands as `«redacted»` and a raw substring test for it would read as a miss.
    sum_err = _json.loads(sum_js)["cells"][0]["error"]
    _check("mcp.run_summary_is_scrubbed_after_cells_clear_their_secrets",
           "sk-secret-abcdef" not in sum_js and "sk-secret-abcdef" not in sum_md
           and sum_err.endswith("«redacted»"),
           f"summary.json and summary.md are scrubbed against the RUN-scoped union of "
           f"every cell's secrets, because they are written long after the last cell "
           f"cleared its own registry and they AGGREGATE cells — one cell's set would "
           f"leave the others exposed. json_clean={'sk-secret-abcdef' not in sum_js} "
           f"md_clean={'sk-secret-abcdef' not in sum_md}", failures, verbose)

    # --- named is not the same as usable --------------------------------------
    # `status` used to be discarded, so a server reported as failed counted as successfully
    # present: not undeclared, so no violation, and not missing, so not even a warning.
    def _witness_warnings(servers):
        buf = _io.StringIO()
        saved, _sys.stderr = _sys.stderr, buf
        try:
            cl.verify_post_run(
                [], RunOptions(mcp_servers=resolved), cwd=scratch,
                stdout=_json.dumps({"type": "system", "subtype": "init",
                                    "mcp_servers": servers,
                                    "claude_code_version": "2.1.113"}),
                stderr="", exit_code=0)
        except RuntimeError as exc:
            return f"RAISED {exc}"
        finally:
            _sys.stderr = saved
        return buf.getvalue()

    w_failed = _witness_warnings([{"name": "echo", "status": "failed"}])
    w_nostatus = _witness_warnings([{"name": "echo"}])
    w_connected = _witness_warnings([{"name": "echo", "status": "connected"}])
    # An UNDECLARED server still fails the run no matter how sick it claims to be — the
    # hermeticity violation is that it was there at all, and a status field is attacker-
    # controlled input from the server's own host.
    w_undeclared_failed = _witness_warnings(
        [{"name": "echo", "status": "connected"}, {"name": "sneaky", "status": "failed"}])
    _check("mcp.declared_server_must_be_reported_connected",
           "echo" in w_failed and "failed" in w_failed
           and "echo" in w_nostatus and w_connected == ""
           and w_undeclared_failed.startswith("RAISED") and "sneaky" in w_undeclared_failed,
           f"a DECLARED server reported in any state but `connected` gets a durable "
           f"warning instead of passing as present — an unknown state warns too, since a "
           f"status this adapter does not recognise is not evidence of health — while a "
           f"healthy one is silent and an UNDECLARED one still fails the run whatever its "
           f"status says: failed={w_failed.strip()[:70]!r} clean={w_connected!r}",
           failures, verbose)

    # --- the refusals hold off the CLI path ------------------------------------
    # They used to live only in the CLI's pre-flight, so `Runner.run()` and any direct
    # caller could run an adapter that cannot inject servers with `mcp_servers:` quietly
    # dropped, or claude with `tools:` quietly unenforced — the exact degradations the
    # validation claims to refuse.
    from . import exec as exec_mod
    gated = parse_mcp_servers({"e": {"command": "true", "tools": ["echo"]}}, where="t")
    plain = parse_mcp_servers({"e": {"command": "true"}}, where="t")

    def _prog_run(agent, servers):
        d = _tempfile.mkdtemp(prefix="ase-mcpprog-")
        try:
            ex = exec_mod.execute(
                get_adapter(agent), "hi",
                RunOptions(mcp_servers=servers, mcp_scratch_dir=d),
                cwd=d, timeout=5)
            return ex.result.error or ""
        except Exception as exc:                # noqa: BLE001 — a raise is also a refusal
            return f"RAISED {exc}"
        finally:
            _shutil.rmtree(d, ignore_errors=True)

    prog_unsupported = _try(lambda: _prog_run("antigravity", plain), "")
    prog_gated = _try(lambda: _prog_run("claude", gated), "")
    _check("mcp.refusals_hold_on_the_programmatic_path",
           "cannot inject MCP servers" in prog_unsupported
           and "tools:" in prog_gated and "not implemented" in prog_gated,
           f"an adapter that cannot inject declared servers, and claude with a `tools:` "
           f"allowlist it cannot enforce, are BOTH refused without the CLI's pre-flight in "
           f"the picture — re-asserted at the one choke point every invocation passes "
           f"through, so the refusal cannot be routed around by calling Runner.run() or "
           f"the adapter directly: unsupported={prog_unsupported[:60]!r} "
           f"gated={prog_gated[:60]!r}", failures, verbose)

    # --- a warning nobody can read afterwards is not a warning -----------------
    # The server-health warning above went to the HARNESS process's stderr, which nothing
    # archives — `execute()` captures the CHILD's. So the message claiming that assertions
    # "will fail for a reason the results will not show" was itself that reason. It now
    # rides the result into cell.json, report.md and summary.json. Driven end to end through
    # `execute()` rather than by calling the collector directly, because the defect was
    # never in the printing — it was in nothing being wired to listen.
    from .notices import collecting as _collecting
    from .notices import warn as _warn

    from .adapters.base import Adapter as _Adapter
    from .adapters.base import ParseOutput as _ParseOutput

    class _WarningAdapter(_Adapter):
        name = "warnadapter"
        binary = _sys.executable
        skills_subdir = ".x/skills"

        def build_argv(self, prompt, opts, *, cwd):
            return [_sys.executable, "-c", ""]

        def parse(self, stdout, stderr, exit_code, *, opts=None):
            return _ParseOutput(events=[], final_text="")

        def verify_post_run(self, argv, opts, *, cwd, stdout, stderr, exit_code=None):
            _warn("warning: [warnadapter] server 'echo' was reported but not connected")
            raise RuntimeError("and then the verification failed")

    wd = _tempfile.mkdtemp(prefix="ase-warn-")
    try:
        wex = exec_mod.execute(_WarningAdapter(), "hi", RunOptions(), cwd=wd, timeout=10)
        wrr = wex.result
        wcell = runner_mod.CellResult(agent="claude", model="m", eval_name="e",
                                      skill=None, passed=False, run_result=wrr,
                                      artifacts_dir=os.path.join(wd, "c0"))
        wreport = _try(lambda: runner_mod.render_report(wcell), "")
        wrun = runner_mod.Runner("claude", models=["m"], artifacts_root=wd,
                                 run_id="w", skills_root=root)
        _wbuf = _io.StringIO()
        _wsaved, _sys.stderr = _sys.stderr, _wbuf
        try:
            wrun._write_summary([wcell], [])
        finally:
            _sys.stderr = _wsaved
        wsum = _json.loads(open(os.path.join(wd, "w", "summary.json")).read())
        wsum_warn = wsum["cells"][0].get("warnings")
    finally:
        _shutil.rmtree(wd, ignore_errors=True)
    _check("mcp.post_run_warnings_survive_the_process_that_printed_them",
           any("not connected" in w for w in wrr.warnings)
           and "not connected" in wreport
           and wsum_warn is not None and any("not connected" in w for w in wsum_warn)
           and wrr.error and "and then the verification failed" in wrr.error,
           f"a warning raised during post-run verification is recorded on the RESULT and "
           f"reaches report.md and summary.json, not just the operator's terminal — and a "
           f"warning emitted BEFORE the verification went on to raise is kept too, since "
           f"the last finding failing does not unsay the earlier ones: "
           f"warnings={wrr.warnings} error={(wrr.error or '')[:50]!r}", failures, verbose)

    # The mechanism working proves nothing about its CALLERS still using it, so claude's own
    # health warning is re-run inside a window and the drift warning — whose terminal output
    # is rate-limited once per (agent, version) — is emitted twice.
    _cbuf = _io.StringIO()
    _csaved, _sys.stderr = _sys.stderr, _cbuf
    try:
        with _collecting() as claude_durable:
            _try(lambda: cl.verify_post_run(
                [], RunOptions(mcp_servers=resolved), cwd=scratch,
                stdout=_json.dumps({"type": "system", "subtype": "init",
                                    "mcp_servers": [{"name": "echo", "status": "failed"}],
                                    "claude_code_version": "9.9.9-unverified"}),
                stderr="", exit_code=0))
        with _collecting() as drift_second:
            _try(lambda: cl.verify_post_run(
                [], RunOptions(mcp_servers=resolved), cwd=scratch,
                stdout=_json.dumps({"type": "system", "subtype": "init",
                                    "mcp_servers": [{"name": "echo", "status": "connected"}],
                                    "claude_code_version": "9.9.9-unverified"}),
                stderr="", exit_code=0))
    finally:
        _sys.stderr = _csaved
    drift_echoes = _cbuf.getvalue().count("9.9.9-unverified")
    _check("mcp.rate_limited_warnings_still_record_on_every_cell",
           any("not as connected" in w for w in claude_durable)
           and any("9.9.9-unverified" in w for w in claude_durable)
           and any("9.9.9-unverified" in w for w in drift_second)
           and drift_echoes == 1,
           f"claude's health and version-drift warnings both reach the collector, and the "
           f"drift warning's once-per-version suppression governs the TERMINAL only — a "
           f"matrix where cell 1 carries the finding and cells 2..n look clean would "
           f"misreport what happened to them: echoes={drift_echoes} "
           f"second_cell={[w[:40] for w in drift_second]}", failures, verbose)

    # Parallel cells share one process stderr, so the collector is thread-local: a
    # redirect_stderr at the call site would have attributed one cell's warnings to another.
    import threading as _threading
    _cross: dict = {}

    def _collect_one(tag, bar):
        with _collecting() as got:
            # Both windows must be OPEN before either warns, or the threads just take
            # turns and a process-wide sink would pass by luck of the scheduler.
            _try(lambda: bar.wait(5.0))
            _warn(f"warning: from-{tag}")
            _try(lambda: bar.wait(5.0))
            _cross[tag] = list(got)

    _bar = _threading.Barrier(2)
    _t1 = _threading.Thread(target=_collect_one, args=("a", _bar))
    _t2 = _threading.Thread(target=_collect_one, args=("b", _bar))
    _wbuf2 = _io.StringIO()
    _wsaved2, _sys.stderr = _sys.stderr, _wbuf2
    try:
        _t1.start(); _t2.start(); _t1.join(10.0); _t2.join(10.0)
    finally:
        _sys.stderr = _wsaved2
    _check("mcp.warning_collection_is_per_cell_not_per_process",
           _cross.get("a") == ["warning: from-a"] and _cross.get("b") == ["warning: from-b"],
           f"two collection windows open at once in different threads each capture only "
           f"their own warnings — cells run in parallel and stderr is process-global, so a "
           f"process-wide capture would file one cell's findings on another's result: "
           f"{_cross}", failures, verbose)

    _shutil.rmtree(scratch, ignore_errors=True)
    _shutil.rmtree(root, ignore_errors=True)
