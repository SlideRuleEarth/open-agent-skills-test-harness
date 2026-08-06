#!/usr/bin/env python3
"""Mutation-test the declared-MCP and credential-containment arms.

Each mutation reintroduces a plausible version of the defect the arm exists to catch; the
named arm MUST go red. An arm that stays green while its defect is present is decorative.

Run it: `python3 harness/tools/mutate_mcp.py` (needs `harness/.venv`). It copies the tree to
a tempdir, checks the baseline passes, then applies one mutation at a time and re-runs the
suite that owns the mutated file. Exit 0 only when every mutation is caught BY ITS NAMED ARM
— "some arm failed" is not the same claim.

TWO SUITES, chosen by the file the mutation perturbs rather than declared per entry.
`agentskill_evals/` is proven by the selftest; `fixtures/` and `tools/` are proven by
`tools/verify_mcp_fixtures.py`, which is the only thing that drives them. Deriving the suite
from the target rather than adding a sixth field is the same argument `_classify` already
makes about the id prefix: a fact that can be stated independently will eventually be stated
wrongly, and here the wrong suite means a mutation reported as uncaught because the program
that would have caught it never ran.

Three failure modes it reports, all of which have happened and none of which mean the code
is fine:

  STALE ANCHOR         the find-text no longer exists; the mutation tested nothing. Re-anchor
                       it against the current source. Expect this whenever you refactor.
  failed, but NOT via  the selftest went red for another reason — very often the mutated
                       source no longer parses (an anchor whose indentation was a substring
                       of a differently-indented line, a condition split across lines without
                       parentheses). Check the mutant compiles before believing the result.
  *** MISSED ***       the defect is present and every arm still passes. Either the arm is
                       decorative, or a SECOND defence added later masks this one — see M53,
                       where deregistration and note-deduplication each hide the other, so
                       the mutation has to remove both (`find`/`repl` accept tuples for this).

Adding a mutation: keep it to the smallest edit that reintroduces the real defect, and point
it at the one arm that should notice. If you cannot name that arm, the arm does not exist yet
and writing it is the actual work.

THREE ID PREFIXES, and they are counted and reported separately.

  M<n>   a PRODUCTION mutation — the normal case, and what "N/N caught" is a claim about.
         It perturbs the code under test and asks whether the instrument notices.
  F<n>   a FIXTURE-or-TOOL mutation, proven by `verify_mcp_fixtures.py`. These perturb an
         instrument and ask whether that instrument's own verifier notices — the same
         epistemics as `I*` rather than `M*`, so they are counted apart from production for
         the same reason. What makes them worth having, where mutating the selftest is not,
         is that the verifier is a SEPARATE program from what it checks: `verify_mcp_fixtures`
         does not import the shim, it drives it over pipes. Nothing here is circular, and the
         gap they close is real — until now the fixtures and probes carried no mutation
         coverage at all, so every assertion in that verifier was named but unproven.
  I<n>   an INSTRUMENT mutation — it perturbs `selftest.py` itself. Almost always the wrong
         thing to write, because mutating the test to prove the test fails is circular and
         establishes nothing about the code. It is legitimate only where the selftest has a
         FEATURE of its own whose failure mode no production edit can reach. Both current
         entries are the ARM COUNTER, which is such a feature: I1 stops it counting and I2
         reverts it to a process-lifetime total, and each failure leaves every arm passing
         and the banner reporting a plausible wrong number.
         If a new `I*` seems necessary, that is the moment to check you are not just testing
         the test — the separate heading in the summary exists so this decision is made in
         the open rather than by quietly appending to the list.

The prefix is NOT taken on trust: `_classify` derives the class from the target file and
refuses to start unless the id agrees. A convention relating two independent facts holds only
until someone types the wrong letter, and every direction miscounts exactly what the split
reporting exists to keep straight. It also refuses a mutation aimed at this file: mutating the
mutation runner to see whether the mutation runner notices is the circularity the `I*` heading
exists to keep rare, with none of the justification.

Every result line carries the wall time of the suite run that produced it, and the summary
names the slowest. Read those against the `baseline:` lines: a mutation taking several times
its suite's baseline is a defect that costs runtime rather than one that reddens an arm, and
it is the only notice anyone gets before it grows past `_SUITE_TIMEOUT` and reports as a hang.
"""
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Repo-relative: this file lives at <repo>/harness/tools/, so the harness package root is
# its grandparent. Was an absolute path while it lived in a scratchpad, which is exactly how
# 71 mutations nearly did not survive a branch handoff.
HARNESS = Path(__file__).resolve().parent.parent
RUNNER = "agentskill_evals/runner.py"
CLAUDE = "agentskill_evals/adapters/claude.py"
BASE = "agentskill_evals/adapters/base.py"
MCP = "agentskill_evals/mcp.py"
ISO = "agentskill_evals/isolation.py"
CODEX = "agentskill_evals/adapters/codex.py"
COPILOT = "agentskill_evals/adapters/copilot.py"
AGY = "agentskill_evals/adapters/antigravity.py"
SCHEMA = "agentskill_evals/schema.py"
CLI = "agentskill_evals/cli.py"
PROXY = "agentskill_evals/mcp_proxy.py"
AUDIT = "agentskill_evals/mcp_audit.py"
# The suite mutates PRODUCTION code and asks whether the selftest notices. This one target
# is the exception, and only for the arm counter: that counter is a feature of the selftest
# whose failure mode (it stops counting, every arm still passes, the banner still says
# PASSED) cannot be reached from anywhere else. Adding other mutations here would be testing
# the test rather than the code, which is not what this tool is for.
SELFTEST = "agentskill_evals/selftest.py"
# Instruments, proven by `verify_mcp_fixtures.py` rather than by the selftest — nothing in
# `agentskill_evals` imports them, so no arm can reach them and every `F*` below would report
# MISSED if it were routed to the selftest. See `_suite_for`.
SHIM = "fixtures/probe_era_mcp_server.py"
ECHO = "fixtures/echo_mcp_server.py"
PIPEPROBE = "tools/probe_mcp_pipelining.py"
# The proxy's I/O half and the awkward server it is driven against. PRODUCTION code that no
# selftest arm can reach — it is only executed by running the real program over real pipes —
# so it is `M*` like any other production target, proven by a THIRD suite. The classification
# and the suite are different questions, and conflating them is what the split below fixes:
# `M`/`I`/`F` says what a mutation perturbs, `_suite_for` says who would notice.
PROXY_IO = "agentskill_evals/mcp_proxy_io.py"
TARGET = "fixtures/proxy_target_server.py"
# Not targets, the suites themselves. A mutation aimed here would be asking a verifier whether
# it notices being broken.
VERIFIER = "tools/verify_mcp_fixtures.py"
PROXY_VERIFIER = "tools/verify_mcp_proxy.py"
SELF = "tools/mutate_mcp.py"

MUTATIONS = [
    ("M1-witness-fails-any-server", CLAUDE,
     "        undeclared = sorted(s for s in live if s not in declared)",
     "        undeclared = sorted(live)",
     "mcp.witness_permits_declared_servers_and_only_those"),
    ("M2-witness-permits-everything", CLAUDE,
     "        undeclared = sorted(s for s in live if s not in declared)",
     "        undeclared = []",
     "mcp.witness_permits_declared_servers_and_only_those"),
    ("M3-no-opts-permits-everything", CLAUDE,
     '        declared = set(getattr(opts, "mcp_servers", None) or {})',
     '        declared = set(getattr(opts, "mcp_servers", None) or {}) if opts else set(live)',
     "mcp.witness_without_options_treats_everything_as_undeclared"),
    ("M4-claude-claims-native-tool-filter", CLAUDE,
     '    mcp_tool_filter = "unbuilt"',
     '    mcp_tool_filter = "native"',
     "mcp.claude_refuses_tools_it_cannot_enforce"),
    ("M5-all-adapters-claim-injection", BASE,
     "    supports_mcp_injection = False",
     "    supports_mcp_injection = True",
     "mcp.adapters_without_injection_refuse_rather_than_drop_the_servers"),
    ("M6-interpolate-into-command", MCP,
     "            command=_abs_command(s.command, base_dir),\n            args=[_abs_arg(a, base_dir) for a in s.args],",
     "            command=_abs_command(sub(s.command) if s.command else None, base_dir),\n            args=[_abs_arg(sub(a), base_dir) for a in s.args],",
     "mcp.interpolation_cannot_choose_what_program_runs"),
    # The `str` redactor. Its `bytes` twin two functions down runs the identical loop, so the
    # bare line matched both and this mutation only ever reached whichever came first.
    ("M7-redact-shortest-first", MCP,
     ("    for form in sorted(forms, key=len, reverse=True):\n"
      "        text = text.replace(form, REDACTED)"),
     ("    for form in sorted(forms, key=len):\n"
      "        text = text.replace(form, REDACTED)"),
     "mcp.longest_secret_is_redacted_first"),
    ("M8-no-short-secret-floor", MCP,
     "MIN_REDACTABLE_LEN = 6",
     "MIN_REDACTABLE_LEN = 0",
     "mcp.too_short_to_redact_is_warned_and_left_alone"),
    ("M9-config-world-readable", CLAUDE,
     "os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)",
     "os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)",
     "mcp.claude_config_is_not_world_readable"),
    ("M10-inline-json-instead-of-file", CLAUDE,
     '            argv += ["--mcp-config", self._write_mcp_config(opts)]',
     '            argv += ["--mcp-config", "{}"]',
     "mcp.claude_writes_a_file_not_inline_json"),
    ("M11-json-artifacts-unredacted", RUNNER,
     "        _write_json(path, redact_obj(obj, self._secrets) if self._secrets else obj)",
     "        _write_json(path, obj)",
     "mcp.secrets_are_scrubbed_from_every_artifact_shape"),
    ("M12-text-artifacts-unredacted", RUNNER,
     "        _write(path, redact(text, self._secrets) if self._secrets else text)",
     "        _write(path, text)",
     "mcp.secrets_are_scrubbed_from_every_artifact_shape"),
    ("M13-native-filter-keys-accepted", MCP,
     '_NATIVE_FILTER_KEYS = {"allowedTools", "enabledTools", "enabled_tools", "disabled_tools",\n                       "allowed_tools", "disabledTools"}',
     "_NATIVE_FILTER_KEYS = set()",
     "mcp.native_filter_spellings_are_refused_by_name"),
    ("M14-dunder-names-allowed", MCP,
     '        if "__" in name:',
     "        if False:",
     "mcp.server_name_cannot_contain_the_tool_name_separator"),
    ("M15-scratch-dir-optional", CLAUDE,
     "        if not opts.mcp_scratch_dir:",
     "        if False:",
     "mcp.claude_refuses_to_write_secrets_without_a_scratch_dir"),
    ("M16-strict-mcp-config-dropped", CLAUDE,
     '        "--strict-mcp-config",\n',
     "",
     "mcp.declared_servers_stay_hermetic"),
    ("M17-unset-var-not-reported", MCP,
     "        if name not in environ:",
     "        if False:",
     "mcp.unset_variable_is_a_validation_error_naming_it"),
    # --- round 2: the five defects found reviewing 88d43c6 ---------------------------
    ("M18-only-the-raw-spelling-is-redacted", MCP,
     "    forms = {value}\n    for ensure_ascii in (True, False):",
     "    forms = {value}\n    for ensure_ascii in ():",
     "mcp.redaction_survives_json_escaping"),
    ("M19-redact-values-but-not-keys", MCP,
     "        return {redact_obj(k, secrets): redact_obj(v, secrets) for k, v in obj.items()}",
     "        return {k: redact_obj(v, secrets) for k, v in obj.items()}",
     "mcp.redaction_covers_dict_keys_and_stringified_leaves"),
    ("M20-workspace-not-scrubbed", RUNNER,
     "    if not secrets:\n        return []",
     "    if True:\n        return []",
     "mcp.archived_workspace_is_scrubbed"),
    ("M21-scrub-follows-symlinks", RUNNER,
     "                if stat.S_ISLNK(mode):",
     "                if False:",
     "mcp.workspace_scrub_does_not_follow_symlinks"),
    ("M22-filenames-keep-the-secret", RUNNER,
     "            new = redact(name, secrets)",
     "            new = name",
     "mcp.archived_workspace_is_scrubbed"),
    ("M23-summary-uses-the-cleared-cell-registry", RUNNER,
     "        _write_json(os.path.join(self.run_dir, \"summary.json\"),\n                    redact_obj(summary, self._run_secrets))",
     "        _write_json(os.path.join(self.run_dir, \"summary.json\"),\n                    redact_obj(summary, self._secrets))",
     "mcp.run_summary_is_scrubbed_after_cells_clear_their_secrets"),
    ("M23b-summary-md-uses-the-cleared-registry", RUNNER,
     "                      self._run_secrets))",
     "                      self._secrets))",
     "mcp.run_summary_is_scrubbed_after_cells_clear_their_secrets"),
    ("M24-server-status-discarded", CLAUDE,
     "            status = s.get(\"status\") if isinstance(s, dict) else None",
     "            status = \"connected\"",
     "mcp.declared_server_must_be_reported_connected"),
    ("M25-refusals-only-in-the-cli-preflight", "agentskill_evals/exec.py",
     '        if getattr(opts, "mcp_servers", None):',
     "        if False:",
     "mcp.refusals_hold_on_the_programmatic_path"),
    # --- round 3: the four defects found reviewing cba3ab4 + de25c86 -----------------
    ("M26-scrub-writes-through-hardlinks", RUNNER,
     "    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or \".\", prefix=\".scrub-\")",
     ("    open(path, 'wb').write(scrubbed)\n    return\n"
     "    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or \".\", prefix=\".scrub-\")"),
     "mcp.workspace_scrub_breaks_hardlinks_instead_of_writing_through_them"),
    ("M27-symlink-target-left-alone", RUNNER,
     "    clean = redact(target, secrets)",
     "    clean = target",
     "mcp.symlink_target_is_scrubbed_even_though_it_is_not_followed"),
    ("M28-no-permission-repair-before-the-walk", RUNNER,
     "    _make_traversable(root)",
     "    pass  # _make_traversable(root)",
     "mcp.unreadable_subtree_is_opened_rather_than_silently_skipped"),
    ("M29-names-judged-one-component-at-a-time", RUNNER,
     "    return redact(rel, secrets) != rel",
     "    return False",
     "mcp.secret_spanning_path_components_is_scrubbed"),
    ("M30-uncertifiable-artifact-kept", RUNNER,
     ('        """Delete what could not be certified, and remember it for the caller."""\n'
     "        lost.add(_rel(path))"),
     ('        """Delete what could not be certified, and remember it for the caller."""\n'
     "        return\n        lost.add(_rel(path))"),
     "mcp.uncertifiable_artifact_is_removed_and_named"),
    ("M31-warnings-not-attached-to-the-result", "agentskill_evals/exec.py",
     "    rr.warnings.extend(warned)",
     "    pass  # rr.warnings.extend(warned)",
     "mcp.post_run_warnings_survive_the_process_that_printed_them"),
    ("M32-health-warning-back-to-stderr-only", CLAUDE,
     '            warn(f"warning: [claude] declared MCP server(s) {detail} were reported by the "',
     '            print(f"warning: [claude] declared MCP server(s) {detail} were reported by the "',
     "mcp.rate_limited_warnings_still_record_on_every_cell"),
    ("M33-rate-limit-suppresses-the-record-too", BASE,
     "        echo = key not in _WARNED_VERSIONS",
     "        if key in _WARNED_VERSIONS:\n            return\n        echo = True",
     "mcp.rate_limited_warnings_still_record_on_every_cell"),
    ("M34-collector-is-process-wide", "agentskill_evals/notices.py",
     "_local = threading.local()",
     "_local = type('G', (), {})()",
     "mcp.warning_collection_is_per_cell_not_per_process"),
    # --- round 4: the six defects found reviewing 0a29935 ----------------------------
    ("M35-root-symlink-is-walked-through", RUNNER,
     "    if not stat.S_ISDIR(st.st_mode):",
     "    if False:",
     "mcp.workspace_root_must_itself_be_a_real_directory"),
    ("M36-permission-repair-chmods-a-shared-inode", RUNNER,
     "        if st.st_nlink > 1:\n            # Widening the mode",
     "        if False:\n            # Widening the mode",
     "mcp.permission_repair_never_widens_a_shared_inode"),
    ("M37-every-non-directory-is-a-regular-file", RUNNER,
     "                elif stat.S_ISREG(mode):",
     "                elif True:",
     "mcp.special_files_are_removed_rather_than_read"),
    ("M38-extended-attributes-are-invisible", "agentskill_evals/xattrs.py",
     'def listxattr(path: str) -> list[bytes]:',
     'def listxattr(path: str) -> list[bytes]:\n    return []',
     "mcp.extended_attributes_are_scrubbed_like_contents"),
    ("M39-quarantine-cannot-unlock-what-it-deletes", RUNNER,
     "    try:\n        os.lchflags(path, 0)\n    except (AttributeError, OSError):\n        pass",
     "    pass",
     "mcp.quarantine_proves_the_deletion_rather_than_assuming_it"),
    ("M40-failed-deletion-reported-as-a-removal", RUNNER,
     "        if not _remove(path):\n            stuck.add(_rel(path))",
     "        _remove(path)",
     "mcp.unremovable_leak_is_reported_as_a_leak_not_as_a_removal"),
    ("M41-assembled-paths-checked-once-not-to-a-fixed-point", RUNNER,
     "    for _ in range(_SCRUB_ROUNDS):",
     "    for _ in range(1):",
     "mcp.assembled_path_check_runs_to_a_fixed_point"),
    # --- round 5: the three defects found reviewing 930d5cd -------------------------
    ("M42-exec-dir-detached-only-when-isolated", RUNNER,
     ("        exec_root = tempfile.mkdtemp(prefix=\"ase-ws-\")\n"
     "        exec_ws = os.path.join(exec_root, \"workspace\")\n"
     "        os.makedirs(exec_ws)"),
     ("        exec_root = tempfile.mkdtemp(prefix=\"ase-ws-\")\n"
     "        exec_ws = os.path.join(exec_root, \"workspace\")\n"
     "        os.makedirs(exec_ws)\n"
     "        if not self.isolated:\n"
     "            exec_ws = workspace"),
     "relocate.exec_cwd_detached_even_when_not_isolated"),
    ("M43-exec-cwd-is-the-tempdir-root", RUNNER,
     "        exec_ws = os.path.join(exec_root, \"workspace\")\n        os.makedirs(exec_ws)",
     "        exec_ws = exec_root",
     "relocate.parent_of_exec_cwd_is_not_published"),
    ("M44-unreadable-root-certified-clean", RUNNER,
     "    except FileNotFoundError:\n        return []  # nothing was archived",
     "    except OSError:\n        return []  # nothing was archived",
     "mcp.unreadable_root_is_repaired_or_reported_never_certified"),
    ("M45-symlink-xattrs-edited-in-place", RUNNER,
     ("        os.symlink(clean, tmp)\n"
     "        for name, value in clean_attrs:\n"
     "            xattrs.setxattr(tmp, name, value)\n"
     "        os.replace(tmp, path)"),
     ("        _scrub_xattrs(path, secrets)\n"
     "        os.unlink(path)\n"
     "        os.symlink(clean, path)\n"
     "        return"),
     "mcp.multiply_linked_symlink_is_replaced_not_edited"),
    # --- round 6: the one defect found reviewing 1229b8e ----------------------------
    # The backstop in _run_cell's finally still removes the directory, so what this breaks is
    # the RECORD: the failure never reaches the cell's own result.
    ("M46-body-purge-not-recorded", RUNNER,
     "        cleanup.purge(exec_root)",
     "        shutil.rmtree(exec_root, ignore_errors=True)",
     "relocate.undeletable_exec_dir_is_durable_and_load_bearing"),
    ("M51-purge-is-best-effort-everywhere", RUNNER,
     '    if _remove(path):\n        return ""',
     '    if shutil.rmtree(path, ignore_errors=True) is None:\n        return ""',
     "relocate.locked_exec_dir_is_actually_removed"),
    ("M47-undeletable-exec-dir-says-nothing", RUNNER,
     ('    if not path or not os.path.lexists(path):\n        return ""\n    if _remove(path):\n'
     '        return ""'),
     ('    if not path or not os.path.lexists(path):\n        return ""\n    if True:\n'
     "        shutil.rmtree(path, ignore_errors=True)\n"
     '        return ""'),
     "relocate.undeletable_exec_dir_is_durable_and_load_bearing"),
    ("M48-note-never-reaches-result-json", RUNNER,
     "        if rr.error != error_before or pending:",
     "        if False:",
     "relocate.undeletable_exec_dir_is_durable_and_load_bearing"),
    ("M49-scratch-dir-removed-best-effort", RUNNER,
     "            cleanup.purge(mcp_scratch)",
     ("            if mcp_scratch:\n"
     "                shutil.rmtree(mcp_scratch, ignore_errors=True)"),
     "relocate.mcp_scratch_dir_removal_is_load_bearing"),
    # --- round 7: a note held in a frame the exception unwinds is a note nobody reads -----
    # The scratch failure is still RECORDED, and the success path still reports it; only the
    # crash path stops draining it. That is exactly the shape review found.
    ("M52-failed-cell-drops-the-cleanup-notes", RUNNER,
     ("        cleanup.note(_scrub_and_note(workspace, self._secrets))\n"
     "        pending = cleanup.pending()\n        _record_notes(rr, pending)"),
     ("        cleanup.note(_scrub_and_note(workspace, self._secrets))\n"
     "        pending = []\n        _record_notes(rr, pending)"),
     "relocate.scratch_failure_survives_a_crashing_execute"),
    # A directory whose removal already escalated and failed stays registered: the outer
    # sweep retries what cannot work and reports the same sentence twice.
    # Two defences now hold "a failure is reported once": the entry is deregistered so the
    # outer sweep cannot retry it, and `note` refuses a duplicate. Removing either alone is
    # invisible, which is what a second layer is FOR — so this mutation removes both.
    ("M53-failed-purge-retried-and-double-reported", RUNNER,
     ("            self._owned.remove(entry)\n            label, owned, fatal, tail = entry",
      "        if text and (fatal, text) not in self._notes:"),
     ("            label, owned, fatal, tail = entry",
      "        if text:"),
     "relocate.scratch_failure_survives_a_crashing_execute"),
    # `None` means "no scratch dir this cell", not "sweep everything": a sentinel here purges
    # exec_root while the agent's workspace is still inside it, on every non-MCP cell.
    ("M54-purge-none-means-purge-everything", RUNNER,
     "        for entry in [e for e in self._owned if path and e[1] == path]:",
     "        for entry in [e for e in self._owned if path is None or e[1] == path]:",
     "relocate.produced_file_in_artifacts"),
    # Registered only once the run is under way, so the window in which the directory exists
    # but nothing owns it reopens.
    ("M55-scratch-registered-too-late", RUNNER,
     ('                cleanup.own("the MCP scratch directory", mcp_scratch,\n'
     "                            tail=_CREDENTIAL_TAIL if interpolated else _CONFIG_TAIL)"),
     "                pass",
     "relocate.scratch_dir_removed_even_if_the_run_never_starts"),
    # --- round 8: reading a note must not destroy it, and the HOME is a resource too ------
    # Drain-on-read. The note is still held by the surviving frame, so the crash path is
    # reached — but it arrives empty, because the read that preceded the failing write
    # already forgot it.
    ("M56-pending-drains-on-read", RUNNER,
     "        return list(self._notes)",
     "        notes, self._notes = self._notes, []\n        return notes",
     "relocate.cleanup_note_is_acknowledged_only_once_it_is_on_disk"),
    # Acknowledged before the writes rather than after: same loss, one line earlier.
    ("M57-acknowledged-before-the-writes", RUNNER,
     ('            self._rwj(os.path.join(cell_dir, "result.json"), rr.to_dict())\n'
     "\n        cell = CellResult("),
     ('            self._rwj(os.path.join(cell_dir, "result.json"), rr.to_dict())\n'
     "        cleanup.acknowledge(pending)\n\n        cell = CellResult("),
     "relocate.cleanup_note_survives_the_crash_rewriting_result_json"),
    # The isolated HOME goes back to being a bare local, owned by nothing until the guard.
    # Re-anchored after the P1b contained-HOME change split the creation registration across
    # two lines and made its severity conditional (`materializes_auth`). These arms exercise
    # the non-materialize path, where that condition is False and the behaviour is unchanged.
    ("M58-isolated-home-registered-late", RUNNER,
     ('            cleanup.own("the isolated HOME", iso_home, fatal=materializes_auth,\n'
     "                        tail=_CONTAINED_TAIL if materializes_auth else None)"),
     "            pass",
     "relocate.isolated_home_is_owned_from_the_moment_it_exists"),
    # A leaked temp directory reported as a leaked credential: fails the cell and says the
    # config-mask overlay held secrets, neither of which is true.
    ("M59-isolated-home-failure-is-fatal", RUNNER,
     "            cleanup.own(\"the isolated HOME\", iso_home, fatal=materializes_auth,",
     '            cleanup.own("the isolated HOME", iso_home, fatal=True,',
     "relocate.stubborn_isolated_home_warns_rather_than_failing_the_cell"),
    # --- round 9: contents are not fixed at creation; the last safe moment is the return --
    # The HOME keeps the severity the harness gave it when it built the masks, ignoring that
    # the child then had it as $HOME with write access.
    ("M60-writable-home-keeps-its-creation-severity", RUNNER,
     ('                cleanup.own("the isolated HOME", iso_home,\n'
     "                            tail=_CONTAINED_TAIL if materializes_auth else _EXPOSED_TAIL)"),
     "                pass",
     "relocate.child_writable_home_is_credential_bearing_after_the_run"),
    # Escalated in severity but still claiming the directory holds nothing.
    ("M61-exposed-home-denies-its-contents", RUNNER,
     "                            tail=_CONTAINED_TAIL if materializes_auth else _EXPOSED_TAIL)",
     "                            tail=_TEMPDIR_TAIL)",
     "relocate.child_writable_home_is_credential_bearing_after_the_run"),
    # Acknowledged once the artifact writes return — but the judge artifacts and
    # `progress.done` come after them, and a raise there rebuilds the result.
    ("M62-acknowledged-before-the-judge-and-progress", RUNNER,
     '\n        self._rw(os.path.join(cell_dir, "report.md"), render_report(cell))\n',
     ('\n        self._rw(os.path.join(cell_dir, "report.md"), render_report(cell))\n'
     "        cleanup.acknowledge(pending)\n"),
     "relocate.cleanup_note_survives_a_raise_after_the_artifacts"),
    # The scrub's verdict goes back to being a body local, so the rebuild rescans a tree the
    # scrub already cleaned and reports nothing about what it deleted.
    ("M63-scrub-verdict-outside-the-protocol", RUNNER,
     ("        cleanup.note(_scrub_and_note(workspace, self._secrets))\n"
     "        pending = cleanup.pending()\n"
     "        if _record_notes(rr, pending):"),
     ("        scrub_note = _scrub_and_note(workspace, self._secrets)\n"
     "        pending = ([(True, scrub_note)] if scrub_note else []) + cleanup.pending()\n"
     "        if _record_notes(rr, pending):"),
     "relocate.scrub_verdict_survives_a_raise_that_rebuilds_the_result"),
    # --- round 10: the overlay bounds reads, not writes; declaring != interpolating ------
    # Re-anchored after P1 unified the credential sources: the refusal call moved out of the
    # `if interpolated:` block and now spans three lines. Same defect, same arm.
    ("M64-credential-run-not-refused", RUNNER,
     ("                _refuse_uncontained_home(iso_home, spec.name,\n"
     "                                         list(interpolated) + list(cred_env_present),\n"
     "                                         _cred_source(interpolated, cred_env_present))"),
     "                pass",
     "mcp.credential_run_is_refused_when_home_writes_escape_the_overlay"),
    # The detector follows the symlinks it is meant to report, so it descends into the real
    # home and reports its contents instead of the one entry that leads there.
    ("M65-escape-walk-follows-what-it-reports", ISO,
     "    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):",
     "    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):",
     "mcp_masked_home.write_escapes_are_any_symlink_out_of_the_overlay"),
    # Only directory symlinks counted — the first version of this check. A file symlink
    # cannot be used to PLANT a file outside, but writing through it replaces the real
    # file's contents with the token, which is the same leak by a different verb.
    ("M66-only-directory-symlinks-counted", ISO,
     "            if target != root_key and not target.startswith(inside):",
     ("            if (os.path.isdir(target) and target != root_key\n"
     "                    and not target.startswith(inside)):"),
     "mcp_masked_home.write_escapes_are_any_symlink_out_of_the_overlay"),
    # Dangling links skipped: nothing to stat, so nothing to worry about — except that a
    # write through one creates the target it was missing, outside the overlay.
    ("M69-dangling-symlinks-skipped", ISO,
     "            if target != root_key and not target.startswith(inside):",
     ("            if (os.path.exists(target) and target != root_key\n"
     "                    and not target.startswith(inside)):"),
     "mcp_masked_home.write_escapes_are_any_symlink_out_of_the_overlay"),
    # Back to asking whether `mcp_servers` was declared rather than whether a ${VAR} was
    # interpolated, so a credential-free cell is failed for credentials it never had.
    # Re-anchored: `interpolated` moved out of the credential block and up to before the
    # HOME is built, because it now decides what KIND of home the cell needs. Same defect,
    # same arm, new site — this is the STALE ANCHOR case the header warns is routine.
    ("M67-severity-follows-declaration-not-interpolation", RUNNER,
     "\n        interpolated = interpolated_refs(spec.mcp_servers) if spec.mcp_servers else []",
     "\n        interpolated = list(spec.mcp_servers or {})",
     "mcp.home_severity_follows_interpolation_not_declaration"),
    # `bool(secrets)` instead: passes for long values, silently drops short ones, which are
    # excluded from redaction ON PURPOSE and are still credentials.
    # `bool(secrets)` as the exposure gate: correct for long values, silently open for short
    # ones, which are excluded from redaction ON PURPOSE and are still credentials.
    # Re-anchored with M67. `sorted(secrets)` no longer expresses the defect at this site —
    # `secrets` is not bound until the credential block below — so the same gate is spelled
    # by resolving early and reading the redaction set off the result. Identical meaning: a
    # value too short to redact is treated as no credential at all.
    ("M68-exposure-gated-on-the-redaction-set", RUNNER,
     "\n        interpolated = interpolated_refs(spec.mcp_servers) if spec.mcp_servers else []",
     ("\n        interpolated = (sorted(spec.resolved_mcp_servers()[1])"
     " if spec.mcp_servers else [])"),
     "mcp.short_credential_run_is_refused_like_any_other"),
    # Only the target canonicalized. On macOS `/var` is a symlink to `/private/var`, so a
    # link pointing inside its own overlay compares as outside — over-refusal, which is the
    # safe direction, but it makes the structural lifting condition unreachable.
    # (No mutation for dropping `normcase`: it is identity on darwin, so nothing here could
    # observe its absence — it earns its place on Windows only.)
    ("M70-root-not-canonicalized", ISO,
     "    root = os.path.realpath(home)",
     "    root = os.path.abspath(home)",
     "mcp_masked_home.write_escapes_are_any_symlink_out_of_the_overlay"),
    # --- contained HOME (#81): the refusal lifts by being SATISFIED, not exempted --------
    # Containment never engages, so a credential cell keeps hitting the refusal it is now
    # entitled to pass. The whole feature, reduced to one constant.
    ("M72-contained-mode-never-engaged", RUNNER,
     "\n        contain_home = has_credentials and contained_subs is not None",
     "\n        contain_home = False",
     "mcp.credential_run_is_permitted_once_the_home_is_contained"),
    # `is not None` -> truthiness. Reads an EMPTY declaration as an absent one, which is
    # precisely claude's answer (it needs nothing from the real home), so the one adapter
    # this work exists to unblock silently stays refused while every other arm passes.
    ("M73-empty-declaration-read-as-unmapped", RUNNER,
     "\n        contain_home = has_credentials and contained_subs is not None",
     "\n        contain_home = has_credentials and bool(contained_subs)",
     "mcp.empty_contained_declaration_contains_rather_than_refuses"),
    # The custom config home is mirrored anyway. The mirror is built by the wholesale
    # symlink pass and lands INSIDE the contained home, so every escape comes back one level
    # down while the home still looks materialized from the top.
    ("M74-custom-config-home-mirrored-into-a-contained-home", RUNNER,
     ("                for var, replaces, skills_sub in ([] if contain_home\n"
     "                                                  else config_home_entries(adapter)):"),
     "                for var, replaces, skills_sub in config_home_entries(adapter):",
     "mcp.contained_home_does_not_mirror_a_custom_config_home"),
    # The wholesale symlink pass still runs under containment — the single line that makes
    # the home a mask again. Everything else about contained mode still works.
    ("M75-wholesale-symlink-pass-survives-containment", ISO,
     "\n    if os.path.isdir(real_dir) and not contained:",
     "\n    if os.path.isdir(real_dir):",
     "contained_home.no_name_leads_out_of_a_hostile_real_home"),
    # Vendor skills symlinked anyway. This is the site that hides behind "contained mode ==
    # skip the wholesale pass": the skills dir is rebuilt entry by entry and mints one
    # outward symlink per vendor skill, all of them escapes.
    ("M76-vendor-skills-still-symlinked", ISO,
     ("            if contained:\n"
     "                _materialize(src, dst)\n"
     "            else:\n"
     "                os.symlink(src, dst)\n"
     "            placed.add(name)"),
     ("            os.symlink(src, dst)\n"
     "            placed.add(name)"),
     "contained_home.vendor_skills_are_copied_not_symlinked"),
    # The same defect at the plugin registry, the second pass-through site — anchored on the
    # `src, dst =` line above it because the four lines that follow are byte-identical to
    # the skills-dir version at the same indentation (the substring-anchor trap).
    ("M77-plugin-contents-still-symlinked", ISO,
     ("            src, dst = os.path.join(real_plugin, name), os.path.join(dst_plugin, name)\n"
     "            if contained:\n"
     "                _materialize(src, dst)\n"
     "            else:\n"
     "                os.symlink(src, dst)"),
     ("            src, dst = os.path.join(real_plugin, name), os.path.join(dst_plugin, name)\n"
     "            os.symlink(src, dst)"),
     "contained_home.plugin_packages_are_copied_not_symlinked"),
    # (No M78: the `S_ISREG` guard in `_materialize` is intentionally NOT mutation-tested.
    #  `shutil.copyfile` raises SpecialFileError on a FIFO and a socket open fails ENXIO —
    #  both already caught downstream — so removing the guard is unobservable from userspace;
    #  its only unique job is device nodes, which a test cannot create without root. An arm
    #  there would be decorative, so there is none, and thus no mutation. See _materialize's
    #  docstring and the note in selftest's contained-home section.)
    # Directories not recursed, so a declared subpath naming a directory silently yields
    # nothing — the CLI then fails on something absent, which is fail-closed but is not the
    # contract the field advertises.
    ("M79-declared-directories-not-recursed", ISO,
     "\n    if stat.S_ISDIR(st.st_mode):",
     "\n    if False and stat.S_ISDIR(st.st_mode):",
     "contained_home.declared_directory_is_copied_by_content"),
    # Back to last-write-wins insertion. A contained subpath colliding with a config mask
    # replaces the neutral "{}" with a faithful copy of the user's REAL MCP config: the home
    # stays perfectly contained, so every containment arm still passes, and hermeticity is
    # gone. Found by this suite's own fixture declaring a path twice by accident.
    ("M80-copy-leaf-displaces-a-mask", ISO,
     "        _insert_copy_leaf(tree, sub)",
     "        _insert_leaf(tree, [sub], _COPY_LEAF)",
     "contained_home.copy_declaration_may_not_displace_a_mask"),
    ("M50-purge-always-claims-success", RUNNER,
     '    if _remove(path):\n        return ""\n    return f"{label} could not be removed',
     '    if True:\n        return ""\n    return f"{label} could not be removed',
     "mcp.credential_scratch_dir_removal_is_verified_not_best_effort"),
    # --- P1 credential-handling fixes -------------------------------------------------
    # No adapter credential env var enters the redaction set, so an echoed
    # CLAUDE_CODE_OAUTH_TOKEN archives verbatim — the interpolation scrub set never sees it.
    # Re-anchored after P1c split names from values. Targets `env_secrets` alone so the
    # redaction set empties while `cred_env_present` (and thus containment) is untouched —
    # isolating the defect to the arm about redaction.
    ("M81-adapter-credential-env-var-not-redacted", RUNNER,
     "\n        env_secrets = tuple(dict.fromkeys(child_env[name] for name in cred_env_present))",
     "\n        env_secrets = ()",
     "mcp.adapter_credential_env_var_is_redacted"),
    # The contained HOME that COPIED real auth is registered non-fatal at creation (the
    # pre-fix behaviour), so a crash before the MCP-resolution upgrade leaves the copied auth
    # on disk under a warning claiming no credentials are present.
    ("M82-contained-home-with-copied-auth-registered-nonfatal", RUNNER,
     ('            cleanup.own("the isolated HOME", iso_home, fatal=materializes_auth,\n'
     "                        tail=_CONTAINED_TAIL if materializes_auth else None)"),
     '            cleanup.own("the isolated HOME", iso_home, fatal=False)',
     "mcp.contained_home_that_copies_auth_is_credential_bearing_before_the_copy"),
    # An adapter credential env var no longer makes the cell credential-bearing, so an
    # ordinary token-set run with no `mcp_servers` drops back to the symlink overlay — the
    # containment gap the child exploited to write the OAuth token into the real home.
    ("M83-env-credential-does-not-trigger-containment", RUNNER,
     "        has_credentials = bool(interpolated) or bool(cred_env_present)",
     "        has_credentials = bool(interpolated)",
     "mcp.credential_env_var_triggers_containment_without_mcp_servers"),
    # Containment still considers env creds (contain_home does), but the REFUSAL only fires
    # for an interpolated `${VAR}`, so an env-credential cell that cannot be contained
    # (`isolated: false`, unmapped adapter) runs uncontained instead of being refused.
    ("M84-env-credential-refusal-not-triggered", RUNNER,
     "\n            if has_credentials:",
     "\n            if interpolated:",
     "mcp.credential_env_var_run_is_refused_under_isolated_false"),
    # Detection samples the process env, not the child's effective env, so a token supplied
    # only through `spec.env` is invisible: no containment, no refusal, no redaction, while
    # the child still receives it and can write it into the real home.
    ("M85-credential-detection-ignores-spec-env", RUNNER,
     "\n        child_env = {**os.environ, **(spec.env or {})}",
     "\n        child_env = dict(os.environ)",
     "mcp.credential_detection_reads_the_childs_effective_environment"),
    # An adapter declaring a contained subpath that collides with its own skills dir: the
    # copy would displace the masking leaf, so the home is contained but the skills it
    # exposes are the user's real ones. Nothing else in the suite exercises the SHIPPED
    # adapters' declarations — every other contained-home arm drives a fake adapter.
    ("M86-codex-contained-surface-collides-with-its-skills-dir", CODEX,
     '\n    contained_home_subpaths: list[str] = [".codex/auth.json"]',
     '\n    contained_home_subpaths: list[str] = [".codex/skills"]',
     "contained.declared_surfaces_build_and_contain"),
    # The §3a survival assertion, reintroduced as a real adapter defect: copilot's env()
    # strips a variable the adapter DECLARES as a credential. The runner samples the value
    # before env() runs, so it would redact and contain on a token the child never received.
    ("M87-copilot-env-strips-a-declared-credential-var", COPILOT,
     '\n    _BUILD_REDIRECT_VARS = ("COPILOT_CLI_DIST_DIR",)',
     '\n    _BUILD_REDIRECT_VARS = ("COPILOT_CLI_DIST_DIR", "GH_TOKEN")',
     "contained.declared_credential_env_vars_survive_adapter_env"),
    # The trust gate, reintroduced: drop the flag from the cell argv and codex is back to
    # refusing to start in the detached tempdir every cell runs in. This is the defect that
    # sat unnoticed for ten days because a cell that never starts is indistinguishable from
    # a cell that failed — the arm exists so it cannot sit unnoticed again.
    ("M88-codex-cell-argv-loses-the-git-repo-trust-gate-flag", CODEX,
     '\n                 "--skip-git-repo-check",\n                 "--json"]',
     '\n                 "--json"]',
     "codex.skips_the_git_repo_trust_gate"),
    # Same defect on the probe path, which fails differently: a probe that dies on the gate
    # is reported as an unavailable MODEL, so the cause is disguised rather than surfaced.
    ("M89-codex-probe-argv-loses-the-git-repo-trust-gate-flag", CODEX,
     '\n                "--skip-git-repo-check",\n                "--json", "-m", model, "say ok"]',
     '\n                "--json", "-m", model, "say ok"]',
     "codex.skips_the_git_repo_trust_gate"),
    # The whole contract of the reporting-path witness: an unwitnessed run must say None,
    # not `()`. Collapsing them lets a cell that crashed before its init event contribute
    # agreement it never established, and the matrix reads verified on its strength.
    ("M90-unwitnessed-run-reports-an-empty-server-set", CLAUDE,
     "\n    if violation is not None or not witnessed:\n        return None",
     "\n    if violation is not None or not witnessed:\n        return ()",
     "claude.witnessed_servers_distinguishes_none_from_empty"),
    # Statuses dropped where the AXIS is built, so `echo` connected in one cell and failed
    # in another compare equal and a matrix where one cell had no working tool surface
    # reports as verified. Aimed at the consistency layer rather than at the claude helper:
    # mutating the helper reddens its own arm first, which is a catch by the wrong test and
    # leaves the matrix-scale property unproven.
    ("M91-consistency-drops-witnessed-status", RUNNER,
     ("\n                health_raw.append(None if any(st is None for _, st in pairs) "
     "else pairs)"),
     "\n                health_raw.append(tuple((n, None) for n, _ in pairs))",
     "runner.mcp_axis_compares_server_health_not_just_names"),
    # An UNSTATED status counted as a known one: two cells naming `echo` with no health
    # given compare equal and the matrix reports verified, which is agreement invented out
    # of silence. This is the defect review found in the first cut of this axis.
    ("M94-unstated-health-counts-as-known", RUNNER,
     ("\n                health_raw.append(None if any(st is None for _, st in pairs) "
     "else pairs)"),
     "\n                health_raw.append(pairs)",
     "runner.mcp_axis_treats_unstated_health_as_unknown"),
    # The other half of that finding: argv's disable set treated as health UNKNOWN rather
    # than as nothing-to-state parks every codex and copilot matrix at unverified forever,
    # over a question that cannot be asked about a server that was never started.
    ("M95-argv-disable-set-treated-as-unknown-health", RUNNER,
     "\n            health_raw.append(() if seen is not None else None)",
     "\n            health_raw.append(None)",
     "runner.mcp_axis_treats_unstated_health_as_unknown"),
    # Two defences cover this one, so the mutation removes BOTH — the M53 shape. `set()` is
    # undefined on an unhashable entry, and normalizing at ingestion is what stops one ever
    # reaching it; either alone keeps a finished matrix reportable, which is the point, and
    # is also why neither shows up as a defect on its own.
    ("M96-unhashable-witness-entry-crashes-the-report", RUNNER,
     (("\n            by_key: dict = {}\n            for v in values:\n"
      "                if v is not None:\n                    by_key.setdefault(repr(v), v)\n"
      "            known = [by_key[k] for k in sorted(by_key)]"),
      ("\n                pairs = tuple((str(n), None if st is None else str(st))\n"
      "                              for n, st in (_server_pair(e) for e in witnessed))")),
     ("\n            known = sorted({v for v in values if v is not None}, key=repr)",
      "\n                pairs = tuple(witnessed)"),
     "runner.consistency_reports_rather_than_raising_on_an_unmodelled_witness"),
    # The per-cell artifact losing the witness: the aggregate still lists the distinct
    # states, but nothing says which cell produced which without re-parsing stdout.
    ("M97-result-json-drops-the-witness", SCHEMA,
     '\n            "mcp_servers_witnessed": witness_json(self.mcp_servers_witnessed),',
     "",
     "schema.run_result_records_its_provenance_per_cell"),
    # null and [] collapsed in the artifact: "the run stated nothing" serialized as "it
    # hosted no servers", which is the distinction the whole axis rests on.
    ("M98-witness-json-collapses-none-into-empty", SCHEMA,
     "\n    if witnessed is None:\n        return None",
     "\n    if witnessed is None:\n        return []",
     "schema.run_result_records_its_provenance_per_cell"),
    # The consistency check ignoring the witness and falling back to argv alone — the state
    # this work started from, where --mcp-config made the axis unreadable and no MCP matrix
    # could ever reach `verified`.
    ("M92-consistency-ignores-the-witness", RUNNER,
     "\n            witnessed = c.run_result.mcp_servers_witnessed",
     "\n            witnessed = None",
     "runner.mcp_axis_reads_the_runs_own_witness_not_just_argv"),
    # The witness's names not reduced to names, so a cell drawing on the witness compares
    # `(name, status)` pairs against a sibling's bare argv names — a difference in EVIDENCE
    # SOURCE reported as a difference in configuration.
    ("M93-witness-names-not-reduced-to-names", RUNNER,
     "\n                names_raw.append(tuple(n for n, _ in pairs))",
     "\n                names_raw.append(pairs)",
     "runner.mcp_axis_reads_the_runs_own_witness_not_just_argv"),
    # An existing field quietly narrowed: `mcp_server_set_verified` reports the SET, and
    # folding the health verdict into it makes a matrix with a readable, uniform set report
    # `false` beside `mcp_server_set_unknown_cells: 0`. Every consumer that already reads
    # this field is then wrong, and nothing in the artifact says the meaning moved.
    ("M99-set-verdict-narrowed-by-the-health-verdict", RUNNER,
     '\n            "mcp_server_set_verified": mcp_set_verified,',
     '\n            "mcp_server_set_verified": mcp_verified,',
     "runner.mcp_set_verification_is_reported_separately_from_health"),
    # Health drift asserted across a server set that itself varied. Health values carry
    # their names, so the set difference propagates into this axis and is announced a second
    # time as a difference in whether servers WORKED — which was never shown.
    ("M100-health-drift-asserted-across-a-varying-server-set", RUNNER,
     "\n        if len(servers) == 1 and len(health) > 1:",
     "\n        if len(health) > 1:",
     "runner.mcp_health_is_only_compared_within_a_uniform_server_set"),
    # The same gate on the POSITIVE verdict, which fails the other way: two cells that each
    # disabled a different server both have nothing outstanding, so health compares equal and
    # reports verified — a green field on an axis whose two cells share no server at all.
    ("M101-health-verified-without-a-common-server-set", RUNNER,
     ("\n        mcp_health_verified = (mcp_set_verified\n"
     "                               and len(health) == 1 and health_unknown == 0)"),
     "\n        mcp_health_verified = (len(health) == 1 and health_unknown == 0)",
     "runner.mcp_health_is_only_compared_within_a_uniform_server_set"),
    # `isolated: false` + `mcp_servers:` back to running: the declared servers load beside
    # the user's real ones, so the set the scenario states is a SUBSET of what ran.
    ("M102-declared-servers-run-without-the-overlay", "agentskill_evals/exec.py",
     "\n            if gap:",
     "\n            if False:",
     "mcp.declared_servers_require_isolation_where_mcp_off_is_a_mask"),
    # The guard stops being told what the run actually got, so every declared-server run is
    # judged as if it had no overlay — over-refusal, which takes the working mask-dependent
    # ISOLATED run with it. The verdict has to follow this run's HOME, not a constant.
    ("M103-guard-ignores-the-runs-actual-home", "agentskill_evals/exec.py",
     "\n            gap = adapter.mcp_off_gap(opts.home)",
     "\n            gap = adapter.mcp_off_gap(None)",
     "mcp.declared_servers_require_isolation_where_mcp_off_is_a_mask"),
    # A CLI-level kill-switch made to require an overlay anyway — the over-refusal direction
    # again, now expressed against the tri-state.
    ("M104-cli-kill-switch-made-to-need-an-overlay", BASE,
     "\n        if mech is MCPOffMechanism.CLI:\n            return None",
     ("\n        if mech is MCPOffMechanism.CLI and isolated_home is not None:\n"
     "            return None"),
     "mcp.declared_servers_require_isolation_where_mcp_off_is_a_mask"),
    # The unclassified default becomes a CLAIM. Every adapter anyone adds next declares
    # nothing, so this is the difference between "new adapter fails closed" and "new adapter
    # runs declared servers beside the user's own, isolated or not".
    ("M105-unclassified-default-reads-as-a-cli-kill-switch", BASE,
     '\n    mcp_off_mechanism: MCPOffMechanism | None = None',
     '\n    mcp_off_mechanism: MCPOffMechanism | None = MCPOffMechanism.CLI',
     "mcp.declared_servers_require_isolation_where_mcp_off_is_a_mask"),
    # The exact regression review found, reintroduced: UNCLASSIFIED folded into
    # OVERLAY_MASKS, which is what a BOOLEAN made unavoidable by giving both states one
    # value. An adapter nobody classified is then cleared by any isolated HOME — by an
    # overlay that materializes nothing for it, since it declares no masks. Caught on the
    # MESSAGE as well as the verdict: with no masks it still refuses, for the wrong reason.
    ("M106-unclassified-folded-into-the-mask-dependent-state", BASE,
     '\n    mcp_off_mechanism: MCPOffMechanism | None = None',
     '\n    mcp_off_mechanism: MCPOffMechanism | None = MCPOffMechanism.OVERLAY_MASKS',
     "mcp.declared_servers_require_isolation_where_mcp_off_is_a_mask"),
    # A self-contradicting declaration goes unchecked: an adapter naming the overlay as its
    # mechanism while declaring no masks is cleared by any HOME, and the overlay it points
    # at materializes nothing. The run goes green having masked nothing at all.
    ("M107-mask-mechanism-not-checked-against-declared-masks", BASE,
     ("\n            if not (self.isolation_config_masks "
     "or self.plugin_registry_config_masks):"),
     "\n            if False:",
     "mcp.declared_servers_require_isolation_where_mcp_off_is_a_mask"),
    # DECLARING a plugin mask taken as the mask having somewhere to act. With no
    # `global_plugin_registry_subpaths` root there is no plugin to materialize it in, and
    # `build_mcp_masked_home` returns (None, {}) — so the adapter is cleared by an overlay
    # that was never built. The subtler half of the same self-contradiction.
    ("M112-plugin-masks-counted-without-a-registry-to-apply-them-in", BASE,
     ("\n            if (self.plugin_registry_config_masks\n"
     "                    and not self.global_plugin_registry_subpaths):"),
     "\n            if False:",
     "mcp.declared_servers_require_isolation_where_mcp_off_is_a_mask"),
    # The SHIPPED case, and the reason the check is per-channel rather than aggregate:
    # antigravity declares both kinds of mask, so losing its registry root orphans the plugin
    # masks while `.gemini/config/mcp_config.json` keeps any "does it have masks" test happy.
    # Fakes alone would not have caught this; the arm asserts the shipped adapters satisfy
    # their own declarations for exactly that reason.
    # A rooted plugin mask taken as covering the CUSTOM config home too. Both mirror
    # builders forward only rerooted DIRECT masks, so the mirror the child is repointed at
    # leaves that channel unmasked — the fourth way "declared" and "in effect" come apart.
    ("M114-plugin-masks-assumed-to-reach-a-custom-config-home", BASE,
     "\n            if self.plugin_registry_config_masks and config_home_entries(self):",
     "\n            if False:",
     "mcp.declared_servers_require_isolation_where_mcp_off_is_a_mask"),
    ("M113-antigravity-loses-the-registry-its-plugin-masks-need", AGY,
     '\n    global_plugin_registry_subpaths = [".gemini/config/plugins"]', "",
     "mcp.declared_servers_require_isolation_where_mcp_off_is_a_mask"),
    # The shipped mapping, pinned per adapter. Dropping a CLI declaration silently refuses a
    # run that is hermetic without the overlay (over-refusal, which removes the only working
    # non-isolated path rather than opening a hole, and so goes unnoticed until someone's
    # scenario stops running); dropping a mask declaration drops that adapter to unclassified,
    # which refuses it even WITH an overlay.
    ("M108-claude-stops-declaring-its-cli-kill-switch", CLAUDE,
     "\n    mcp_off_mechanism = MCPOffMechanism.CLI", "",
     "mcp.declared_servers_require_isolation_where_mcp_off_is_a_mask"),
    ("M109-codex-stops-declaring-its-argv-kill-switch", CODEX,
     "\n    mcp_off_mechanism = MCPOffMechanism.CLI", "",
     "mcp.declared_servers_require_isolation_where_mcp_off_is_a_mask"),
    ("M110-copilot-stops-declaring-its-overlay-dependence", COPILOT,
     "\n    mcp_off_mechanism = MCPOffMechanism.OVERLAY_MASKS", "",
     "mcp.declared_servers_require_isolation_where_mcp_off_is_a_mask"),
    ("M111-antigravity-stops-declaring-its-overlay-dependence", AGY,
     "\n    mcp_off_mechanism = MCPOffMechanism.OVERLAY_MASKS", "",
     "mcp.declared_servers_require_isolation_where_mcp_off_is_a_mask"),
    # The check is simply gone. Measured live before it existed: the cell spends a model
    # call and comes back `exited with code 1`, with "Not logged in" buried in a truncated
    # JSON blob inside an assertion message.
    ("M115-contained-cell-with-no-credential-route-runs-anyway", RUNNER,
     ("\n                if (contain_home and required_cred\n"
     "                        and not any(child_env.get(name) for name in required_cred)):"),
     "\n                if False:",
     "mcp.contained_home_without_its_credential_env_var_is_refused"),
    # Inverted: it fires when the credential IS present and stays silent when it is not —
    # a refusal aimed at exactly the runs that can succeed. Caught only by the second half
    # of the arm, which is why that half exists.
    ("M116-refusal-fires-on-the-runs-that-can-authenticate", RUNNER,
     "\n                        and not any(child_env.get(name) for name in required_cred)):",
     "\n                        and any(child_env.get(name) for name in required_cred)):",
     "mcp.contained_home_without_its_credential_env_var_is_refused"),
    # The inference this check was rewritten to remove (review, PR #99): read the requirement
    # out of `credential_env_vars` instead of the adapter's own answer. That list asserts
    # forwarding and redaction — it does not say the environment is the ONLY route, so an
    # adapter authenticating through a helper or socket that containment leaves intact gets
    # its working cell refused. Caught by the third arm case, which is that adapter.
    ("M117-requirement-inferred-from-the-forwarding-list", RUNNER,
     ('                required_cred = list(getattr(\n'
     '                    adapter, "contained_home_required_credential_env_vars", None) or [])'),
     ('                required_cred = list(getattr(\n'
     '                    adapter, "credential_env_vars", None) or [])'),
     "mcp.contained_home_without_its_credential_env_var_is_refused"),
    # A required name that is not also a declared credential env var: the runner would refuse
    # a cell for the absence of a variable it never redacts and never contains on.
    ("M118-required-credential-outside-the-redacted-set", COPILOT,
     ('    contained_home_required_credential_env_vars = [\n'
     '        "COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"]'),
     ('    contained_home_required_credential_env_vars = [\n'
     '        "COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PAT"]'),
     "contained.required_credential_env_vars_are_declared_and_answered"),
    # The unmapped default reinstated on an adapter that has an answer: an empty contained
    # surface severs every HOME-side route, so "nobody looked" is not a state it may be in.
    ("M119-contained-surface-with-no-recorded-auth-answer", CLAUDE,
     '    contained_home_required_credential_env_vars = ["CLAUDE_CODE_OAUTH_TOKEN"]',
     "    contained_home_required_credential_env_vars = None",
     "contained.required_credential_env_vars_are_declared_and_answered"),
    # `tags:` stops reaching the per-cell artifact, so a result can no longer say whether it
    # was a regression or an experiment — the state the second mark was documented as fixing
    # while nothing actually read it (review, second round).
    ("M120-cell-artifact-drops-the-spec-tags", RUNNER,
     '                "tags": cell.tags,\n',
     "",
     "artifacts.spec_tags_are_recorded_per_cell"),
    ("M121-summary-drops-the-spec-tags", RUNNER,
     '                    "eval": c.eval_name, "skill": c.skill, "tags": c.tags,',
     '                    "eval": c.eval_name, "skill": c.skill,',
     "artifacts.spec_tags_are_recorded_per_cell"),
    # Carried on the success path but not the crash path — the half where attribution matters
    # most, and the half a single-path test would never notice.
    ("M122-crashed-cell-loses-its-tags", RUNNER,
     ("                          scenario_path=getattr(spec, \"source_path\", None),\n"
     "                          tags=list(spec.tags or []))"),
     "                          scenario_path=getattr(spec, \"source_path\", None))",
     "artifacts.spec_tags_are_recorded_per_cell"),
    # The rejection removed: `--config x --tag y` reads like a selection, silently runs the
    # scenario whatever its tags say, and bills a model call for it.
    ("M123-tag-with-config-is-silently-ignored-again", CLI,
     ("        if args.tag is not None:\n"
      "            # --tag filters DISCOVERED evals;"),
     ("        if False:\n"
      "            # --tag filters DISCOVERED evals;"),
     "cli.tag_with_config_is_refused_not_ignored"),
    # The SUCCESS path's assignment, which M122's crash-path twin does not cover. Both cells
    # shared an eval name and therefore an artifacts directory, so the crash cell overwrote
    # the success cell's assertions.json before the arm read it and this mutation stayed
    # green — the arm has three distinct names now, and this is what pins that (review,
    # third round).
    ("M125-successful-cell-loses-its-tags", RUNNER,
     ("            seeded_paths=sorted(seeded_relpaths(spec)),\n"
      "            tags=list(spec.tags or []),\n"),
     "            seeded_paths=sorted(seeded_relpaths(spec)),\n",
     "artifacts.spec_tags_are_recorded_per_cell"),
    # The empty-form bypass — and it takes BOTH edits, which is the finding rather than an
    # inconvenience. `nargs="*"` alone is harmless while the guard tests PRESENCE, and a
    # truthiness guard alone is harmless while the parser cannot produce an empty list; each
    # single-site version was written first and reported MISSED, correctly, because neither
    # is a defect on its own. Restore both and a bare `--tag` is `[]` again — present but
    # falsy — which walks past the refusal into a real scenario run (review, third round).
    # Caught by the empty-form case in the arm, and by nothing else.
    ("M126-bare-tag-is-present-but-falsy-again", CLI,
     ('    sp.add_argument("--tag", nargs="+", help="only evals with one of these tags")',
      ("        if args.tag is not None:\n"
       "            # --tag filters DISCOVERED evals;")),
     ('    sp.add_argument("--tag", nargs="*", help="only evals with one of these tags")',
      ("        if args.tag:\n"
       "            # --tag filters DISCOVERED evals;")),
     "cli.tag_with_config_is_refused_not_ignored"),
    # The arm counter stops counting. Every arm still passes and the banner still says
    # PASSED, so without its own arm this is invisible — which is precisely the failure the
    # counter was added to make visible (a section that silently stops running).
    # --- C3 proxy decision layer (DESIGN_MCP_Support.md §10) ------------------------------
    # Every one of these is a way the proxy would DEGRADE — forward something it did not
    # understand — rather than fail the cell, which is the single property §10.5 exists to
    # guarantee. They are the reason the decision layer is pure: a mutation cannot reach
    # logic that only a wire-level driver exercises.
    #
    # The naive envelope reading, "no `id` means notification": an id-less RESULT is accepted
    # as a response instead of being refused, so a malformed frame gets forwarded.
    ("M128-idless-result-accepted-as-a-response", PROXY,
     '            return Anomaly(MALFORMED, "result response with no `id`")',
     "            return RESULT",
     "proxy.envelope_shape_is_established_positively"),
    # A null request id read as a notification, which puts it on the never-answer path.
    ("M129-null-request-id-becomes-a-notification", PROXY,
     "        if has_id:\n",
     "        if has_id and msg['id'] is not None:\n",
     "proxy.envelope_shape_is_established_positively"),
    # A JSON-RPC batch stops being its own diagnosis. MCP's stdio binding allows exactly one
    # message per line, so an array is a conformance refusal and the log must say which.
    ("M130-batch-loses-its-own-diagnosis", PROXY,
     ("    if isinstance(msg, list):\n"
      '        return Anomaly(BATCH, "JSON-RPC batch: an array is not a legal MCP stdio '
      'message")\n'),
     "",
     "proxy.envelope_shape_is_established_positively"),
    # Era read off the method name: `initialize` is honoured even when the request declared
    # itself modern — the exact inversion §10.2 forbids.
    ("M131-modern-era-honours-initialize", PROXY,
     '        return method != "initialize"',
     "        return True",
     "proxy.era_comes_from_metadata_not_method"),
    # Modern-only collapses from a CATEGORY to one method, so a bare `subscriptions/listen`
    # is answered under legacy semantics that have no such method — the probe shim's bug.
    ("M132-modern-only-methods-collapse-to-one", PROXY,
     '\nMODERN_ONLY_METHODS = ("server/discover", "subscriptions/listen")',
     '\nMODERN_ONLY_METHODS = ("server/discover",)',
     "proxy.era_comes_from_metadata_not_method"),
    # The version gate relaxed: an unread revision is forwarded on the strength of looking
    # modern. This is the mutation that stands in for every future revision.
    ("M133-unimplemented-modern-version-forwarded", PROXY,
     "    if claimed not in MODERN_VERSIONS:\n",
     "    if False:\n",
     "proxy.version_gate_fails_closed"),
    ("M134-unimplemented-negotiated-version-forwarded", PROXY,
     "    if claimed not in LEGACY_VERSIONS:\n",
     "    if False:\n",
     "proxy.version_gate_fails_closed"),
    # Partial modern metadata laundered into legacy: a capabilities-only request is read as
    # a bare one instead of the broken modern request a server must reject.
    ("M135-capabilities-without-a-version-is-laundered", PROXY,
     "    return VER_KEY in meta or CAP_KEY in meta",
     "    return VER_KEY in meta",
     "proxy.partial_modern_metadata_is_not_legacy"),
    # A structural scan reintroduced by the back door: an EMPTY `tools` array counts as a
    # definition, so the capability flag in every modern handshake trips the check.
    ("M136-empty-tools-array-counts-as-a-definition", PROXY,
     "    return isinstance(value, list) and bool(value)",
     "    return isinstance(value, list)",
     "proxy.capabilities_are_not_tool_definitions"),
    # The filter edits the message in place, so the source advertisement is destroyed and
    # cannot be logged as the expected event §10.5 wants recorded.
    ("M137-filter-mutates-the-source-message", PROXY,
     "    out = dict(result)\n",
     "    out = result\n",
     "proxy.tools_result_is_filtered_or_refused_never_forwarded_unfiltered"),
    # A nameless tool entry passes the shape check, so it is silently dropped by the filter
    # rather than refused — a decision the proxy is not entitled to make.
    ("M138-nameless-tool-entry-passes-the-shape-check", PROXY,
     '    return all(isinstance(t, dict) and isinstance(t.get("name"), str) for t in tools)',
     "    return True",
     "proxy.tools_result_is_filtered_or_refused_never_forwarded_unfiltered"),
    # Correlation collapses to one direction, so a server->client id reads as the client's.
    ("M139-correlation-ignores-direction", PROXY,
     ("    def method_for(self, direction: str, req_id: Any) -> str | None:\n"
      "        entry = self._by_direction[direction].get(self._key(req_id))"),
     ("    def method_for(self, direction: str, req_id: Any) -> str | None:\n"
      '        entry = self._by_direction["c2s"].get(self._key(req_id))'),
     "proxy.correlation_is_direction_scoped"),
    # Retirement stops retiring, so a reused subscription id correlates against stale state.
    ("M140-retired-subscription-id-stays-resident", PROXY,
     "        entry = self._by_direction[direction].pop(self._key(req_id), None)",
     "        entry = self._by_direction[direction].get(self._key(req_id))",
     "proxy.subscriptions_retire_at_both_orderly_ends"),
    # Graceful closure stops checking WHICH request it closes, so any empty result retires a
    # subscription that is still open.
    ("M141-graceful-closure-ignores-the-method", PROXY,
     '    if method != "subscriptions/listen":\n        return False\n',
     "",
     "proxy.subscriptions_retire_at_both_orderly_ends"),
    # A second `initialize` is accepted as a renegotiation, retroactively moving every
    # message already exchanged to a revision it was not read under.
    ("M142-second-initialize-accepted-as-renegotiation", PROXY,
     "        if self.legacy_version is not None:\n",
     "        if False:\n",
     "proxy.legacy_state_is_the_negotiated_version_and_initialize_happens_once"),
    # Observed-version telemetry stops being a set of distinct eras.
    ("M143-observed-versions-record-duplicates", PROXY,
     ("        if version not in self.observed:\n"
      "            self.observed.append(version)"),
     "        self.observed.append(version)",
     "proxy.legacy_state_is_the_negotiated_version_and_initialize_happens_once"),
    # The refusal stops saying the server was never contacted, so a scenario author debugs a
    # server that never saw the call.
    ("M144-refusal-hides-that-the-server-was-not-contacted", PROXY,
     '                        f"server was not contacted"),',
     '                        f"server was consulted"),',
     "proxy.off_list_call_is_refused_without_reaching_the_server"),
    # --- the envelope's TYPED fields, each a distinct way through -------------------------
    # A boolean id is accepted. `isinstance(True, int)` is true in Python, so this is the one
    # illegal id that also aliases a legal one: id `True` and id `1` become the same entry.
    ("M145-boolean-request-id-passes-as-an-integer", PROXY,
     "    if isinstance(value, bool):\n        return False\n",
     "",
     "proxy.envelope_shape_is_established_positively"),
    # `params` may be any JSON. The schema says it is an object, and every reader below
    # (`_meta`, `name`, `protocolVersion`) is written against that.
    ("M146-params-need-not-be-an-object", PROXY,
     '        if "params" in msg and not isinstance(msg["params"], dict):\n',
     "        if False:\n",
     "proxy.envelope_shape_is_established_positively"),
    # An `error` object with no code or no message is forwarded as a valid error response,
    # handing the client a reply it cannot interpret to a request the proxy did forward.
    ("M147-error-object-fields-unchecked", PROXY,
     ('    if not isinstance(err, dict):\n'
      '        return False\n'
      '    code = err.get("code")\n'
      "    if not isinstance(code, int) or isinstance(code, bool):\n"
      "        return False\n"
      '    return isinstance(err.get("message"), str)'),
     "    return isinstance(err, dict)",
     "proxy.envelope_shape_is_established_positively"),
    # A scalar `result` classifies as a result response, so every reader of `msg["result"]`
    # downstream — the tools filter included — is working against something that is not a map.
    ("M148-scalar-result-classifies-as-a-response", PROXY,
     '        if not isinstance(msg["result"], dict):\n',
     "        if False:\n",
     "proxy.envelope_shape_is_established_positively"),
    # --- the dispatcher: the decisions that only `decide()` can be asked about -------------
    # The era/method check becomes advisory: a modern `initialize` or a bare
    # `subscriptions/listen` is forwarded for the server to sort out, and a proxy that has
    # lost track of the era no longer knows where tool definitions live.
    ("M149-era-mismatch-is-forwarded-not-refused", PROXY,
     "    if not method_matches_era(method, modern=modern):\n",
     "    if False:\n",
     "proxy.era_comes_from_metadata_not_method"),
    # THE REVIEWED DEFECT, restored exactly: `inputRequests` read as a list. It is a map, so
    # iteration walks the string KEYS, every entry fails `isinstance(req, dict)`, and a
    # conforming tool-bearing sampling result reads as clean and reaches the model.
    ("M150-input-requests-read-as-a-list", PROXY,
     ('        requests = result["inputRequests"]\n'
      "        if not isinstance(requests, dict):\n"
      "            return Anomaly(BAD_INPUT_REQUESTS,\n"
      '                           f"InputRequiredResult.inputRequests is "\n'
      '                           f"{type(requests).__name__}, not the map of server-assigned '
      'ids "\n'
      '                           f"the schema defines; its tool-bearing entries cannot be '
      'read")\n'
      "        for key, req in requests.items():\n"
      "            if not isinstance(req, dict):\n"
      "                return Anomaly(BAD_INPUT_REQUESTS,\n"
      '                               f"inputRequests[{key!r}] is {type(req).__name__}, "\n'
      '                               f"not a request object")\n'),
     ('        for req in result.get("inputRequests") or []:\n'
      "            if not isinstance(req, dict):\n"
      "                continue\n"),
     "proxy.modern_sampling_requests_are_a_keyed_map_not_a_list"),
    # The id-less error loses its own diagnosis and is reported as a malformed frame, sending
    # whoever reads the audit log after a framing bug that does not exist (§10.4).
    ("M151-idless-error-diagnosed-as-a-bad-frame", PROXY,
     ("        return Fail(Anomaly(UNCORRELATED,\n"
      '                            "error response carries no `id`'),
     ("        return Fail(Anomaly(MALFORMED,\n"
      '                            "error response carries no `id`'),
     "proxy.envelope_shape_is_established_positively"),
    # A live id is silently overwritten: record `tools/list` on id 1, then `ping` on id 1, and
    # the tool advertisement correlates as a `ping` and is forwarded UNFILTERED.
    ("M152-live-request-id-is-overwritten", PROXY,
     "        if key in live:\n",
     "        if False:\n",
     "proxy.a_live_request_id_is_never_silently_reused"),
    # The closure stops naming WHICH subscription it closes, so a complete result carrying
    # someone else's subscription id retires a subscription that is still open.
    ("M153-closure-ignores-the-subscription-id", PROXY,
     "    return meta.get(SUB_KEY) == msg.get(\"id\")",
     "    return True",
     "proxy.graceful_closure_is_the_conforming_shape_not_an_empty_object"),
    # THE REVIEWED DEFECT, restored exactly: the closure must be a literally empty result.
    # The spec says "empty" in prose and prints `resultType` + `_meta`, so this rejects every
    # conforming shutdown — including our own probe shim's — and fails the cell that was
    # closing cleanly.
    ("M154-closure-must-be-a-literally-empty-result", PROXY,
     ('    if result.get("resultType") != "complete":\n'
      "        return False\n"
      '    meta = result.get("_meta")\n'
      "    if not isinstance(meta, dict):\n"
      "        return False\n"
      '    return meta.get(SUB_KEY) == msg.get("id")'),
     "    return not result",
     "proxy.graceful_closure_is_the_conforming_shape_not_an_empty_object"),
    # THE REVIEWED DEFECT, restored exactly: the connection is read under the version the
    # CLIENT PROPOSED rather than the one the server answered with. Every later legacy message
    # is then interpreted under a revision that was never agreed.
    ("M155-negotiated-version-is-the-clients-proposal", PROXY,
     ("        self.pending_initialize = None\n"
      "        self.legacy_version = negotiated\n"),
     ("        self.legacy_version = self.pending_initialize\n"
      "        self.pending_initialize = None\n"),
     "proxy.legacy_state_is_the_negotiated_version_and_initialize_happens_once"),
    # A second `initialize` sent before the first is answered slips through — the case a state
    # holding only an ACCEPTED version cannot see.
    ("M156-second-initialize-while-the-first-is-pending", PROXY,
     "        if self.pending_initialize is not None:\n",
     "        if False:\n",
     "proxy.legacy_state_is_the_negotiated_version_and_initialize_happens_once"),
    # The gate moves back onto the client's PROPOSAL, which fails a negotiation that was about
    # to succeed: a client offering a revision this proxy has not read, answered by a server
    # offering one it has, is ordinary and not an anomaly.
    ("M157-proposed-version-is-gated-too", PROXY,
     ('    if not isinstance(claimed, str) or not claimed:\n'
      '        return Anomaly(MALFORMED, f"initialize protocolVersion is {claimed!r}")\n'
      "    return claimed"),
     ("    if claimed not in LEGACY_VERSIONS:\n"
      '        return Anomaly(UNIMPLEMENTED_VERSION, f"proposed {claimed!r}")\n'
      "    return claimed"),
     "proxy.legacy_state_is_the_negotiated_version_and_initialize_happens_once"),
    # The allowlist stops being consulted at all. This is the mutation the whole split exists
    # for: with the arm calling `refusal_for` directly it was invisible, because a
    # well-worded refusal nobody ever sends is still well worded (review, PR #100).
    ("M158-off-list-call-is-forwarded", PROXY,
     "    if name is not None and name not in allowed:\n",
     "    if False:\n",
     "proxy.off_list_call_is_refused_without_reaching_the_server"),
    # A `tools/call` with no string `params.name` is checked against the allowlist anyway, so
    # the refusal names `None` instead of the envelope being refused as malformed (§10.4).
    ("M159-tools-call-name-shape-unchecked", PROXY,
     "        if not isinstance(name, str) or not name:\n",
     "        if False:\n",
     "proxy.off_list_call_is_refused_without_reaching_the_server"),
    # A server-originated request is accepted with no legacy `initialize` in force, which the
    # modern revision forbids outright.
    ("M160-server-request-needs-no-legacy-state", PROXY,
     "    if state.legacy_version is None:\n",
     "    if False:\n",
     "proxy.server_originated_requests_need_legacy_actually_in_force"),
    # An uncorrelated response is forwarded instead of refused. A `tools/list` result on an id
    # nobody requested has no correlated method, so it is not recognised as tool-bearing and
    # goes through UNFILTERED — the guarantee defeated by a response the proxy cannot place.
    ("M161-uncorrelated-response-is-forwarded", PROXY,
     "    if method is None:\n",
     "    if False:\n",
     "proxy.tools_result_is_filtered_or_refused_never_forwarded_unfiltered"),
    # Correlation looks in the SAME direction the response is travelling rather than the one
    # the request came from, so nothing ever correlates and every tools/list response is
    # either refused or (with M161) forwarded unfiltered.
    ("M162-responses-correlate-in-their-own-direction", PROXY,
     "_ORIGIN_OF = {C2S: S2C, S2C: C2S}",
     "_ORIGIN_OF = {C2S: C2S, S2C: S2C}",
     "proxy.tools_result_is_filtered_or_refused_never_forwarded_unfiltered"),
    # --- the era must be ESTABLISHED, not merely unclaimed --------------------------------
    # Absence of modern metadata is read as legacy again, so a client that simply never
    # handshakes has every request waved through — the version gate defeated without malice
    # and without an exotic client, since §10.6's locations are stated per version.
    ("M163-bare-request-is-legacy-by-default", PROXY,
     ("    if not modern and state.legacy_version is None "
      "and method not in PRE_INITIALIZE_METHODS:\n"),
     "    if False:\n",
     "proxy.a_bare_request_needs_an_era_that_was_actually_established"),
    # A PENDING handshake satisfies the era gate — the tolerant reading, and the hole it
    # opens is the one §10.6 needs shut: the pipelined request's response can arrive before
    # the negotiation, and is then read under no version at all.
    ("M174-a-pending-initialize-counts-as-an-era", PROXY,
     ("    if not modern and state.legacy_version is None "
      "and method not in PRE_INITIALIZE_METHODS:\n"),
     ("    if (not modern and state.legacy_version is None\n"
      "            and state.pending_initialize is None\n"
      "            and method not in PRE_INITIALIZE_METHODS):\n"),
     "proxy.a_bare_request_needs_an_era_that_was_actually_established"),
    # A REFUSED handshake stays pending forever, so the connection is neither negotiated nor
    # open to a fresh attempt and a legitimate retry reads as a second `initialize`.
    ("M175-errored-initialize-stays-pending", PROXY,
     "            state.abandon_initialize()\n",
     "            pass\n",
     "proxy.a_bare_request_needs_an_era_that_was_actually_established"),
    # The sanctioned pre-initialization set loses `ping`, which the lifecycle explicitly
    # permits before the `initialize` response — the opposite failure, a clean cell refused.
    ("M164-pre-initialize-ping-is-refused", PROXY,
     'PRE_INITIALIZE_METHODS = ("initialize", "ping")',
     'PRE_INITIALIZE_METHODS = ("initialize",)',
     "proxy.a_bare_request_needs_an_era_that_was_actually_established"),
    # THE REVIEWED DEFECT, restored exactly: the off-list refusal is produced BEFORE the id is
    # claimed, so an off-list call on a live id answers the request that id really belongs to
    # — the client's outstanding `tools/list` looks answered while its real response is still
    # in flight.
    ("M165-refusal-jumps-the-duplicate-id-check", PROXY,
     ('    opened = inflight.record(C2S, msg["id"], method,\n'
      "                             version if modern else state.legacy_version)\n"
      "    if isinstance(opened, Anomaly):\n"
      "        return Fail(opened)\n"
      "    if name is not None and name not in allowed:\n"),
     ("    if name is not None and name not in allowed:\n"
      '        return Refuse(refusal_for(msg["id"], name, server), name)\n'
      '    opened = inflight.record(C2S, msg["id"], method,\n'
      "                             version if modern else state.legacy_version)\n"
      "    if isinstance(opened, Anomaly):\n"
      "        return Fail(opened)\n"
      "    if False:\n"),
     "proxy.a_live_request_id_is_never_silently_reused"),
    # Modern `InputRequiredResult` semantics applied to every era, so a legacy response
    # carrying an `inputRequests` key of its own — `Result` is open-ended there — is refused
    # as a bad MRTR payload. §10.6's locations are per version, not per shape.
    ("M166-input-requests-read-in-every-era", PROXY,
     ('    if (modern and isinstance(result, dict)\n'
      '            and result.get("resultType") == "input_required"\n'
      '            and "inputRequests" in result):\n'),
     ('    if (isinstance(result, dict)\n'
      '            and result.get("resultType") == "input_required"\n'
      '            and "inputRequests" in result):\n'),
     "proxy.modern_sampling_requests_are_a_keyed_map_not_a_list"),
    # A fractional id is refused. The schema — the declared source of truth — says
    # `string | number`, and JSON-RPC only says fractions SHOULD NOT be used, so this fails a
    # conforming client over style rather than over conformance.
    ("M167-fractional-request-id-refused", PROXY,
     "    return isinstance(value, float) and math.isfinite(value)",
     "    return False",
     "proxy.envelope_shape_is_established_positively"),
    # The closure defaults an absent `resultType` to complete. That rule exists for servers on
    # EARLIER revisions, and no earlier revision has `subscriptions/listen` — so it accepts a
    # closure that cannot exist, including the bare `{}` the prose seems to describe.
    ("M168-closure-defaults-an-absent-result-type", PROXY,
     '    if result.get("resultType") != "complete":\n',
     '    if result.get("resultType", "complete") != "complete":\n',
     "proxy.graceful_closure_is_the_conforming_shape_not_an_empty_object"),
    # --- cancellation: the notification that is not just forwarded ------------------------
    # THE REVIEWED DEFECT, restored exactly: the cancellation retires the direction it
    # TRAVELLED rather than the one that issued the request. The modern revision requires the
    # case that separates them — a server tearing down a subscription cancels the CLIENT's
    # `subscriptions/listen` — so the subscription stays resident and its id later collides.
    ("M169-cancellation-retires-the-senders-own-map", PROXY,
     ("    if legacy:\n"
      "        inflight.cancel(S2C, req_id)\n"
      "    elif modern:\n"
      "        inflight.cancel(C2S, req_id)\n"),
     "    inflight.cancel(direction, req_id)\n",
     "proxy.cancellation_retires_the_request_it_actually_names"),
    # Routing by SEARCH rather than by protocol: try the sender's map, then the peer's. A
    # server can then retire the CLIENT's outstanding `tools/list` by naming its id — a
    # cancellation it is not permitted to send, and the reply that follows has lost the
    # correlation that would have filtered it.
    ("M176-cancellation-routed-by-searching-both-maps", PROXY,
     ("    if legacy:\n"
      "        inflight.cancel(S2C, req_id)\n"
      "    elif modern:\n"
      "        inflight.cancel(C2S, req_id)\n"),
     ("    for where in (direction, _ORIGIN_OF[direction]):\n"
      "        if inflight.cancel(where, req_id) is not None:\n"
      "            break\n"),
     "proxy.cancellation_retires_the_request_it_actually_names"),
    # A negotiated legacy version is taken to settle the era for the whole connection, so every
    # server cancellation reads as legacy. Modern semantics are per REQUEST: an initialized
    # client may still open a modern subscription, and its teardown then finds nothing in the
    # server's map and leaves the stream open (reviewed defect, restored).
    ("M179-a-legacy-session-makes-every-server-cancellation-legacy", PROXY,
     ("    legacy = (state.legacy_version is not None\n"
      "              and inflight.method_for(S2C, req_id) is not None)\n"),
     "    legacy = state.legacy_version is not None\n",
     "proxy.cancellation_retires_the_request_it_actually_names"),
    # The genuine collision is resolved by guessing instead of failing: id live under both
    # eras, and the legacy branch wins because it is written first.
    ("M180-ambiguous-cancellation-guesses-instead-of-failing", PROXY,
     "    if legacy and modern:\n",
     "    if False:\n",
     "proxy.cancellation_retires_the_request_it_actually_names"),
    # THE REVIEWED DEFECT, restored exactly: reopening a cancelled id clears its quarantine,
    # so a straggling tool advertisement correlates as the NEW request and is forwarded
    # unfiltered — cancel `tools/list` on id 1, reuse id 1 for a `ping`, then the late result.
    ("M177-cancelled-id-quarantine-cleared-on-reuse", PROXY,
     ("        spent = self._spent[direction].get(key)\n"
      "        if spent is not None:\n"),
     ("        spent = self._spent[direction].get(key)\n"
      "        if spent == SPENT_BY_ANSWER:\n"),
     "proxy.a_late_response_to_a_cancelled_request_is_dropped_not_fatal"),
    # The SECOND reviewed defect on the same tombstone: the id is released once a straggler has
    # been dropped, on the reasoning that the request is then over on both sides. The server is
    # the side this boundary does not trust, and one observed response says nothing about the
    # next — so cancel, one straggler, reuse, second straggler, forwarded unfiltered.
    ("M178-quarantine-lifts-once-a-straggler-is-seen", PROXY,
     "            return Drop(msg, DROP_LATE_CANCELLED,\n",
     ("            inflight._spent[origin].pop(inflight._key(req_id), None)\n"
      "            return Drop(msg, DROP_LATE_CANCELLED,\n"),
     "proxy.a_late_response_to_a_cancelled_request_is_dropped_not_fatal"),
    # The nested `requestId` goes unvalidated, so `requestId: []` reaches `dict.pop` through a
    # tuple and raises `TypeError: unhashable` inside a pump — the crash `valid_request_id`
    # prevents on the envelope, arriving by a route that skipped it.
    ("M170-cancelled-id-is-not-validated", PROXY,
     '    if not isinstance(params, dict) or not valid_request_id(params.get("requestId")):\n',
     '    if not isinstance(params, dict) or "requestId" not in params:\n',
     "proxy.cancellation_retires_the_request_it_actually_names"),
    # The cancellation race becomes fatal again: a response already in flight when the
    # cancellation was sent answers an id nothing is waiting for, and the cell fails on a
    # sequence the spec describes and tells the client to ignore.
    ("M171-late-response-to-a-cancelled-request-is-fatal", PROXY,
     "        if spent == SPENT_BY_CANCEL:\n",
     "        if False:\n",
     "proxy.a_late_response_to_a_cancelled_request_is_dropped_not_fatal"),
    # THE REVIEWED DEFECT: an id is freed once the server answers it, so the bypass the
    # cancellation quarantine closes is reachable with no cancellation at all — `tools/list` on
    # id 1, its filtered answer, `ping` on id 1, second `tools/list` result forwarded
    # unfiltered. Aimed at `settle` rather than a call site: all five response paths run
    # through it, and mutating one would leave the other four to disagree.
    ("M181-an-answered-id-is-freed-not-spent", PROXY,
     ("        method = self._drop(direction, req_id)\n"
      "        if method is not None:\n"
      "            self._spent[direction][self._key(req_id)] = SPENT_BY_ANSWER\n"),
     "        method = self._drop(direction, req_id)\n",
     "proxy.a_live_request_id_is_never_silently_reused"),
    # The same rule applied where it does NOT belong, which is the over-strict direction §10.5
    # warns about: a locally refused call burns its id even though nothing was forwarded and
    # nothing can ever answer it. The client's next request on that id — and every MRTR retry —
    # is then refused for a reason it cannot act on.
    ("M182-a-refused-call-spends-an-id-nothing-was-sent-on", PROXY,
     "        return self._drop(direction, req_id)\n",
     ("        method = self._drop(direction, req_id)\n"
      "        self._spent[direction][self._key(req_id)] = SPENT_BY_ANSWER\n"
      "        return method\n"),
     "proxy.a_live_request_id_is_never_silently_reused"),
    # A second response to an already-answered request is silently DROPPED, by widening the
    # documented-race branch to cover every spent id. The race is documented for cancellation
    # and nothing else, so this discards a server protocol violation without a word — the
    # degradation §10.5 forbids, wearing the clothes of a sanctioned drop.
    ("M183-a-second-response-is-dropped-like-a-cancellation-race", PROXY,
     "        if spent == SPENT_BY_CANCEL:\n",
     "        if spent is not None:\n",
     "proxy.a_live_request_id_is_never_silently_reused"),
    # Numeric ids keyed by Python's type name again, so a request on `1` answered on `1.0`
    # reads as uncorrelated — and both can be live at once.
    ("M172-numeric-ids-keyed-by-python-type", PROXY,
     '    return ("s", req_id) if isinstance(req_id, str) else ("n", req_id)',
     "    return (type(req_id).__name__, req_id)",
     "proxy.correlation_is_direction_scoped"),
    # The `resultType` discriminator is ignored, so ANY modern result carrying a key called
    # `inputRequests` is read as MRTR — the structural scan §10.6 rejects, by the back door.
    ("M173-result-type-discriminator-ignored", PROXY,
     ('    if (modern and isinstance(result, dict)\n'
      '            and result.get("resultType") == "input_required"\n'
      '            and "inputRequests" in result):\n'),
     '    if modern and isinstance(result, dict) and "inputRequests" in result:\n',
     "proxy.modern_sampling_requests_are_a_keyed_map_not_a_list"),
    ("I1-arm-counter-stops-counting", SELFTEST,
     "    global _ARMS_RUN\n    _ARMS_RUN += 1\n",
     "    global _ARMS_RUN\n",
     "selftest.arm_count_is_live"),
    # The ASSIGNMENT in the banner reverts to the raw process-lifetime counter, so a second
    # `run_selftest()` in one interpreter reports the sum of both runs while every arm still
    # passes and the number still looks plausible (492, then 984 — found in review).
    #
    # Aimed at the assignment, not at `_arms_since`. The first version of this mutation
    # perturbed the helper, which the arm exercised directly — so the helper was pinned and
    # the wiring that actually regressed was not, and "2/2 instrument" claimed coverage it
    # did not have (review, sixth round). `_banner` exists so this line is inside the
    # function the arm calls and the run prints.
    ("I2-arm-count-reverts-to-process-lifetime", SELFTEST,
     "    arms = _arms_since(arms_before)\n",
     "    arms = _ARMS_RUN\n",
     "selftest.arm_count_is_per_run_not_per_process"),
    # Still rejected, but only AFTER the scenario is read — so the flag no longer fails fast,
    # and a scenario that does not parse reports a file error for a run that was never going
    # to happen. Two edits, because reordering is a move: delete, then re-insert below the
    # load. The arm pins the order as well as the rejection, which is what catches this.
    ("M124-tag-rejection-happens-after-the-scenario-is-loaded", CLI,
     (('        if args.tag is not None:\n'
       '            # --tag filters DISCOVERED evals; a scenario is selected by path and there is no\n'),
      "        scenario = _load_scenario(args.config)\n"),
     (('        if False:\n'
       '            # --tag filters DISCOVERED evals; a scenario is selected by path and there is no\n'),
      ("        scenario = _load_scenario(args.config)\n"
       "        if args.tag is not None:\n"
       "            print('error: --tag does not select a scenario', file=sys.stderr)\n"
       "            return 2\n")),
     "cli.tag_with_config_is_refused_not_ignored"),

    # ---- C3's audit record and its verdict (§10.5.1) -------------------------------------
    # Every one of these reintroduces a defect that survived a round of review on the design
    # itself, where prose held it without complaining. They are shape defects, and the point of
    # writing the types before the I/O half is that a shape defect reddens an arm in seconds.
    #
    # The first group is the PARSE layer, which exists because the reader's input is whatever
    # `json.loads` returned and the writer is the thing under suspicion. Each of these makes
    # the validator assume a shape instead of checking it, and the arm they redden catches the
    # resulting exception — a traceback out of `verify_post_run` is not a failed cell, it is an
    # absent verdict, which is the outcome §10.5 exists to make impossible.
    # LEADING NEWLINE, per §4: `parse_log` runs the same test on each LINE at 8 spaces, and this
    # 4-space anchor is a substring of it. The newline pins it to `parse()`, which is the reader
    # this mutation names.
    ("M184-reader-assumes-its-input-is-a-map", AUDIT,
     "\n    if not isinstance(raw, dict):",
     "\n    if False:",
     "audit.every_malformed_shape_is_a_problem_code_not_an_exception"),
    ("M185-reader-assumes-a-list-where-json-allows-anything", AUDIT,
     "    if not isinstance(value, list):",
     "    if False:",
     "audit.every_malformed_shape_is_a_problem_code_not_an_exception"),
    ("M186-reader-assumes-list-entries-are-maps", AUDIT,
     ('        if not isinstance(entry, dict):\n'
      '            problems.append(f"trigger_not_a_map:{i}")'),
     ('        if False:\n'
      '            problems.append(f"trigger_not_a_map:{i}")'),
     "audit.every_malformed_shape_is_a_problem_code_not_an_exception"),
    # BOTH SITES, per M53. `_str_or` and `_opt_str` run the identical test on identical lines —
    # `_opt_str`'s own docstring says "it is the same idiom" — so no leading newline separates
    # them and removing the rule from one leaves the other enforcing it. A property defended in
    # two places needs a mutation that removes both.
    ("M187-reader-assumes-a-scalar-is-a-string", AUDIT,
     (("def _str_or(value: Any, problems: list[str], code: str) -> str | None:\n"
       "    if isinstance(value, str):"),
      "    value = entry[key]\n    if isinstance(value, str):"),
     (("def _str_or(value: Any, problems: list[str], code: str) -> str | None:\n"
       "    if True:"),
      "    value = entry[key]\n    if True:"),
     "audit.every_malformed_shape_is_a_problem_code_not_an_exception"),
    # `raw.get(k) or []` in its original form: a writer that forgot an axis entirely then
    # validates as one that recorded nothing on it, and recording nothing is legal.
    ("M188-a-missing-axis-reads-as-an-empty-one", AUDIT,
     ('        if required:\n'
      '            problems.append(f"missing:{key}")'),
     ('        if False:\n'
      '            problems.append(f"missing:{key}")'),
     "audit.missing_null_and_empty_are_three_different_records"),
    ("M189-null-is-not-distinguished-from-a-bad-type", AUDIT,
     ('    if value is None:\n'
      '        problems.append(f"null:{key}")'),
     ('    if False:\n'
      '        problems.append(f"null:{key}")'),
     "audit.missing_null_and_empty_are_three_different_records"),

    # ---- the vacuity guard and the fact key set -----------------------------------------
    ("M190-empty-record-passes-vacuously", AUDIT,
     "    if not record.triggers:",
     "    if False:",
     "audit.an_empty_trigger_list_is_malformed_not_vacuously_clean"),
    ("M191-validator-rejects-every-record", AUDIT,
     "    if not record.triggers:",
     "    if True:",
     "audit.a_clean_instance_is_clean"),
    ("M192-missing-completion-facts-are-not-checked", AUDIT,
     ('        if key not in record.facts:\n'
      '            problems.append(f"fact_missing:{key}")'),
     ('        if False:\n'
      '            problems.append(f"fact_missing:{key}")'),
     "audit.every_missing_completion_fact_is_caught_individually"),
    # The spot-check defect in its exact form: four of the five facts are checked, and the
    # fifth is the one whose step silently never ran.
    ("M193-completion-facts-checked-all-but-one", AUDIT,
     ('    for key in FACTS:\n'
      '        if key not in record.facts:'),
     ('    for key in FACTS[:4]:\n'
      '        if key not in record.facts:'),
     "audit.every_missing_completion_fact_is_caught_individually"),
    ("M194-unknown-fact-names-are-ignored", AUDIT,
     "    for key in sorted(set(record.fact_keys) - set(FACTS)):",
     "    for key in ():",
     "audit.an_unrecognized_completion_fact_name_is_malformed"),
    ("M195-unknown-completion-state-accepted", AUDIT,
     "        if entry.state not in STATES:",
     "        if False:",
     "audit.an_unrecognized_completion_state_is_malformed"),
    ("M196-not-applicable-needs-no-licence", AUDIT,
     ("        if (entry.state == NOT_APPLICABLE\n"
      "                and record.latch not in _NOT_APPLICABLE_LICENSED_BY[key]):"),
     ("        if (False\n"
      "                and record.latch not in _NOT_APPLICABLE_LICENSED_BY[key]):"),
     "audit.not_applicable_needs_the_trigger_that_licenses_it"),
    # Step 1 always runs, so a blank here is never justified — not even under `spawn_failed`,
    # where the other four legitimately are.
    ("M197-spawn-failure-excuses-the-intake-close", AUDIT,
     "    INTAKE_CLOSED: frozenset(),",
     "    INTAKE_CLOSED: frozenset({SPAWN_FAILED}),",
     "audit.not_applicable_needs_the_trigger_that_licenses_it"),
    # ...and the same rule from the other side: the `spawn_failed` record is LEGAL, and a
    # validator that rejects it scores full marks against every other case here.
    ("M198-legal-spawn-failed-record-rejected", AUDIT,
     "    CHILD_STDIN_CLOSED: frozenset({SPAWN_FAILED}),",
     "    CHILD_STDIN_CLOSED: frozenset(),",
     "audit.spawn_failed_is_structurally_valid_and_anomalous"),

    # ---- the declared cause, and the evidence that must match it exactly -----------------
    ("M199-a-failed-fact-need-not-name-its-cause", AUDIT,
     "            if cause not in causes_for(key):",
     "            if False:",
     "audit.a_failed_fact_declares_its_cause_rather_than_having_one_inferred"),
    ("M200-a-declared-cause-need-not-be-supported", AUDIT,
     "            elif cause not in evidence:",
     "            elif False:",
     "audit.failed_and_its_outcome_require_each_other"),
    # The `or` this replaced: one record carrying both a real group-kill failure and a fault
    # suppression of the same step, which are two incompatible accounts of one operation.
    ("M201-contradictory-causes-are-tolerated", AUDIT,
     "            if len(evidence) > 1:",
     "            if False:",
     "audit.a_failed_fact_cannot_carry_two_incompatible_causes"),
    ("M202-outcome-needs-no-failed-fact", AUDIT,
     ('        if entry is None or entry.state != FAILED:\n'
      '            problems.append(f"outcome_unpaired:{kind}")'),
     ('        if False:\n'
      '            problems.append(f"outcome_unpaired:{kind}")'),
     "audit.failed_and_its_outcome_require_each_other"),
    # `failed` deleted from the state set: a teardown that ran and did not work then has no
    # legal spelling at all, and the only remaining one — omitting the fact — is malformed.
    ("M203-a-failed-step-has-no-writeable-state", AUDIT,
     "STATES = frozenset({DONE, NOT_APPLICABLE, FAILED})",
     "STATES = frozenset({DONE, NOT_APPLICABLE})",
     "audit.a_failed_teardown_is_recordable_and_anomalous"),

    # ---- the catch-all, keyed by fact and carrying its payload ---------------------------
    # The coarse key. Any anomaly anywhere licenses `failed` anywhere — which is what a
    # step-keyed catch-all did, since step 2 owns two facts.
    ("M204-any-anomaly-licenses-any-failed-fact", AUDIT,
     "    if any(o.kind == SHUTDOWN_ANOMALY and o.fact == key for o in record.outcomes):",
     "    if any(o.kind == SHUTDOWN_ANOMALY for o in record.outcomes):",
     "audit.a_shutdown_anomaly_licenses_only_the_fact_it_names"),
    ("M205-catch-all-need-not-name-its-fact", AUDIT,
     ('            if entry.fact not in FACTS:\n'
      '                problems.append(f"anomaly_unkeyed:{entry.fact}")'),
     ('            if False:\n'
      '                problems.append(f"anomaly_unkeyed:{entry.fact}")'),
     "audit.the_catch_all_reaches_the_facts_with_no_typed_outcome"),
    # The catch-all narrowed to the facts that already have a typed outcome — which is where
    # it started, and leaves an exception escaping steps 1 or 2 unrecordable.
    ("M206-catch-all-does-not-reach-the-untyped-facts", AUDIT,
     "    base = {SHUTDOWN_ANOMALY, FAULT_POINT_FIRED}",
     "    base = {FAULT_POINT_FIRED} if typed is None else {SHUTDOWN_ANOMALY, FAULT_POINT_FIRED}",
     "audit.the_catch_all_reaches_the_facts_with_no_typed_outcome"),
    ("M207-anomaly-may-name-a-fact-that-did-not-fail", AUDIT,
     ('            if SHUTDOWN_ANOMALY in evidence:\n'
      '                problems.append(f"anomaly_orphan:{key}")'),
     ('            if False:\n'
      '                problems.append(f"anomaly_orphan:{key}")'),
     "audit.a_shutdown_anomaly_whose_fact_is_not_failed_is_malformed"),
    # The tagged union read in one direction only: `cause` is examined under `failed` and
    # ignored everywhere else, so a step that completed while carrying the cause of its own
    # failure is not tolerated so much as unread.
    ("M225-a-cause-under-done-is-never-read", AUDIT,
     ('            if cause is not None:\n'
      '                problems.append(f"cause_forbidden:{key}:{cause}")'),
     ('            if False:\n'
      '                problems.append(f"cause_forbidden:{key}:{cause}")'),
     "audit.a_cause_is_forbidden_wherever_the_state_is_not_failed"),
    # The two discriminated unions, each losing the payload its tag promises. A
    # `protocol_anomaly` that declines to say WHICH anomaly withholds the one thing the audit
    # log exists to carry, and it looks structurally fine.
    ("M208-protocol-anomaly-need-not-say-which", AUDIT,
     "        elif entry.reason == PROTOCOL_ANOMALY and entry.anomaly not in ANOMALY_KINDS:",
     "        elif False:",
     "audit.a_tagged_entry_must_carry_the_payload_its_tag_promises"),
    ("M209-shutdown-anomaly-need-not-carry-its-exception", AUDIT,
     ('            if not entry.exception:\n'
      '                problems.append("anomaly_no_exception")'),
     ('            if False:\n'
      '                problems.append("anomaly_no_exception")'),
     "audit.a_tagged_entry_must_carry_the_payload_its_tag_promises"),

    # ---- evidence only a reaper can hold -------------------------------------------------
    ("M210-reap-claimed-without-its-exit-status", AUDIT,
     "    if reaped_done and not present:",
     "    if False:",
     "audit.child_status_travels_with_the_reap_and_only_with_it"),
    ("M211-exit-status-for-a-child-never-reaped", AUDIT,
     "    if not reaped_done and present:",
     "    if False:",
     "audit.child_status_travels_with_the_reap_and_only_with_it"),
    # Presence back in place of the value: `null`, `"fabricated"` and `true` are then all a
    # clean verdict, and the record claims evidence it does not carry.
    ("M226-child-status-is-checked-for-presence-not-content", AUDIT,
     "    if present and not is_json_int(record.child_status):",
     "    if False:",
     "audit.child_status_is_an_exit_status_and_not_merely_present"),
    # ...and the type check that a boolean walks straight through, because `isinstance(True,
    # int)` holds. The failure this catches looks like a real exit code in every dump.
    ("M227-a-boolean-passes-for-an-exit-status", AUDIT,
     "    return isinstance(value, int) and not isinstance(value, bool)",
     "    return isinstance(value, int)",
     "audit.child_status_is_an_exit_status_and_not_merely_present"),
    # The erasure. Every non-conforming value for an OPTIONAL field becomes the same `None`
    # that means "not there", so `cause: null` under `done` is indistinguishable from a fact
    # with no cause and the rule forbidding one never sees it.
    # Anchored past `value = entry[key]` because `_str_or` has a byte-identical body, and an
    # anchor matching the first occurrence would mutate the REQUIRED-field helper instead —
    # a different defect, caught by a different arm, reported as this one.
    ("M229-a-present-optional-field-is-erased-into-absence", AUDIT,
     ('    value = entry[key]\n'
      '    if isinstance(value, str):\n'
      '        return value\n'
      '    problems.append(code)\n'
      '    return None'),
     ('    value = entry[key]\n'
      '    if isinstance(value, str):\n'
      '        return value\n'
      '    return None'),
     "audit.a_present_optional_field_is_never_erased_into_absence"),
    # A payload carried outside the tag that reads it — never consulted, and therefore never
    # reported, which is the defect `cause_forbidden` exists for.
    ("M230-a-stray-anomaly-kind-is-tolerated", AUDIT,
     "        elif entry.reason != PROTOCOL_ANOMALY and entry.anomaly is not None:",
     "        elif False:",
     "audit.a_payload_is_legal_only_under_the_tag_that_reads_it"),
    ("M231-a-stray-outcome-payload-is-tolerated", AUDIT,
     "        elif entry.fact is not None or entry.exception is not None:",
     "        elif False:",
     "audit.a_payload_is_legal_only_under_the_tag_that_reads_it"),
    # An empty exception string: the field is there and says nothing, which the `is None`
    # spelling of this check waves through.
    ("M232-an-empty-exception-counts-as-carried", AUDIT,
     "            if not entry.exception:",
     "            if entry.exception is None:",
     "audit.a_tagged_entry_must_carry_the_payload_its_tag_promises"),
    # A fault point armed on a name that is not a completion fact. The instance is anomalous
    # from the arming either way, which is precisely what makes the malformed configuration
    # invisible without its own check.
    ("M228-suppression-targets-are-not-checked", AUDIT,
     "    for name in sorted(record.suppresses - set(FACTS)):",
     "    for name in ():",
     "audit.every_suppression_target_is_a_known_completion_fact"),

    # ---- arming is not firing -------------------------------------------------------------
    ("M212-fault-firing-need-not-be-configured", AUDIT,
     "        if key not in record.suppresses:",
     "        if False:",
     "audit.only_a_fired_fault_point_pairs_with_a_failed_fact"),
    ("M213-fault-firing-need-not-have-suppressed-anything", AUDIT,
     ('        if entry is None or entry.state != FAILED:\n'
      '            problems.append(f"fired_unpaired:{key}")'),
     ('        if False:\n'
      '            problems.append(f"fired_unpaired:{key}")'),
     "audit.only_a_fired_fault_point_pairs_with_a_failed_fact"),
    # Arming stops being a verdict input, which is the state the no-op-injection case exists
    # to reject: a hook armed and silently never fired then produces a passing run.
    ("M214-arming-a-fault-point-is-forgiven", AUDIT,
     "    if record.fault_point:",
     "    if False:",
     "audit.arming_a_fault_point_fails_the_instance_on_its_own"),
    # An arm-only hook suppresses nothing by design, so reading emptiness as "no fault point"
    # passes exactly the run that case exists to reject.
    ("M215-an-empty-suppression-list-reads-as-no-fault-point", AUDIT,
     "    if record.fault_point:",
     "    if record.suppresses:",
     "audit.arming_a_fault_point_fails_the_instance_on_its_own"),
    # ...and the collapse in the other direction: firing counted as a verdict input too, so
    # the suppressed-step control can no longer say what made it anomalous.
    ("M216-firing-is-counted-as-well-as-arming", AUDIT,
     "        out.append(FAULT_POINT_CONFIGURED)",
     ("        out.append(FAULT_POINT_CONFIGURED)\n"
      "    out += [FAULT_POINT_CONFIGURED for _f in record.fired]"),
     "audit.a_suppressed_step_is_recorded_without_being_excused"),

    # ---- the verdict as a conjunction over everything -------------------------------------
    # Leading newline again: `instance_verdict` computes the same tuple at 8 spaces, and the
    # 4-space anchor is a substring of it. This one is `verdict()`, the entry point the arm reads.
    ("M217-verdict-reads-only-the-first-reason", AUDIT,
     "\n    anomalous = tuple(r for r in reasons(record) if not is_clean(r))",
     "\n    anomalous = tuple(r for r in reasons(record)[:1] if not is_clean(r))",
     "audit.each_axis_can_fail_the_instance_on_its_own"),
    ("M218-cleanup-outcomes-do-not-reach-the-verdict", AUDIT,
     "    out += [o.kind for o in record.outcomes]",
     "    out += []",
     "audit.a_clean_teardown_never_launders_an_anomalous_one"),
    # A runner-up trigger treated as a teardown fault, so EOF followed by signal escalation —
    # ordinary behaviour on half the fleet — fails the cell.
    ("M219-a-second-clean-trigger-is-treated-as-an-anomaly", AUDIT,
     "    out = [t.reason for t in record.triggers]",
     ("    out = [t.reason for t in record.triggers[:1]]\n"
      "    out += [SHUTDOWN_ANOMALY for _t in record.triggers[1:]]"),
     "audit.two_clean_triggers_do_not_compose_into_a_failure"),
    # C3-1 measured both of these against conforming CLIs, so failing on either fails a clean
    # cell — which §10.5 counts as the same defect as forwarding a definition.
    ("M220-a-measured-clean-outcome-fails-the-cell", AUDIT,
     "    SHUTDOWN_WRITE_FAILED,\n",
     "",
     "audit.the_two_measured_cleanup_outcomes_stay_clean"),
    ("M221-forced-termination-fails-the-cell", AUDIT,
     "    SHUTDOWN_CHILD_KILLED,\n",
     "",
     "audit.the_two_measured_cleanup_outcomes_stay_clean"),
    ("M222-is-clean-defaults-to-clean", AUDIT,
     "    return reason in CLEAN_REASONS",
     "    return True",
     "audit.an_unenumerated_reason_is_an_anomaly_not_a_pass"),
    # A reason that is clean without being a reason at all. Nothing else notices: it forgives
    # nothing until the day something is spelled that way, and then it forgives silently.
    ("M223-a-clean-reason-that-is-not-an-enumerated-reason", AUDIT,
     "    CLIENT_EOF, SIGNAL_TERM, SIGNAL_INT,\n",
     '    CLIENT_EOF, SIGNAL_TERM, SIGNAL_INT, "client_hung_up",\n',
     "audit.an_unenumerated_reason_is_an_anomaly_not_a_pass"),
    # A sixth reason forgiven, and this one IS enumerated — so the arm above stays green and
    # only the exact-set arm notices. The direction that never announces itself: a cell that
    # now passes where it used to fail.
    ("M224-a-sixth-reason-quietly-becomes-clean", AUDIT,
     "    SHUTDOWN_CHILD_KILLED,\n})",
     "    SHUTDOWN_CHILD_KILLED,\n    SHUTDOWN_READ_FAILED,\n})",
     "audit.is_clean_is_total_and_has_no_default_clean_branch"),

    # ---- the audit LOG: instances, the absence rule, and the cell verdict ---------------
    # Everything above judges one instance from a map. These perturb the layer that decides
    # WHICH records make up an instance and whether the file as a whole clears the cell —
    # where the failures have nothing to enumerate, because the process that would have named
    # a reason is already gone.

    # The false-failure direction, which §10.5 weighs exactly as heavily as the false pass.
    # Every line then reports its own required keys as unrecognized, so the reader refuses
    # every log ever written and the arms that assert a rule fires stay green throughout.
    ("M233-the-reader-rejects-every-log", AUDIT,
     "    known = _ENVELOPE_KEYS.union(required, optional)",
     "    known = _ENVELOPE_KEYS",
     "audit_log.an_ordinary_gated_run_reads_clean"),
    # The refinement §10.5 spells out: a proper-subset allowlist normally MEANS the server
    # advertises off-list tools and the proxy strips them, so a rule read off `removed` the
    # same way as `forwarded` fails precisely the cell where filtering worked.
    ("M234-a-stripped-tool-is-read-as-a-leaked-one", AUDIT,
     "                if name in allowed:",
     "                if name not in allowed:",
     "audit_log.a_filtered_advertisement_is_an_expected_event"),

    # ---- the endings with no reason to record --------------------------------------------
    ("M235-a-start-with-no-terminator-passes", AUDIT,
     '        problems.append("terminator_absent")',
     "        pass",
     "audit_log.a_start_with_no_terminator_is_an_anomaly"),
    # The partial final line waved through as "just a partial write", which turns a proxy
    # killed mid-record into one that ended cleanly — and leaves nothing in the file saying so.
    ("M236-a-truncated-line-is-silently-skipped", AUDIT,
     '            problems.append(f"unparseable_line:{i}")',
     "            pass",
     "audit_log.a_truncated_final_line_is_absent_rather_than_repaired"),
    # The vacuity guard removed: `all()` over no instances is True, so a gated server whose
    # proxy never wrote a start record certifies an UNGATED run.
    ("M237-an-empty-log-is-vacuously-clean", AUDIT,
     '        problems = problems + ("no_instances",)',
     "        pass",
     "audit_log.an_empty_log_is_not_a_clean_log"),
    ("M238-a-blank-line-is-not-worth-reporting", AUDIT,
     '            problems.append(f"blank_line:{i}")',
     "            pass",
     "audit_log.every_malformed_line_is_a_code_rather_than_an_exception"),

    # ---- the no-heal rule ------------------------------------------------------------------
    # "Find the latest verdict for this server", written exactly as anyone would write it.
    # Every clean restart then papers over the anomalous instance ahead of it.
    ("M239-the-last-instance-answers-for-the-file", AUDIT,
     "    return LogVerdict(clean=not problems and all(v.clean for v in verdicts),",
     "    return LogVerdict(clean=not problems and all(v.clean for v in verdicts[-1:]),",
     "audit_log.a_clean_restart_never_heals_the_instance_before_it"),
    ("M240-a-terminator-with-no-start-is-forgiven", AUDIT,
     '        problems.append("start_absent")',
     "        pass",
     "audit_log.a_terminator_answers_only_for_its_own_instance"),
    # The second record wins, which is what a writer reusing an instance id across restarts
    # produces: the later run's clean terminator read as the earlier run's.
    ("M241-a-second-record-overwrites-the-first", AUDIT,
     "        elif entry[kind] is not None:",
     "        elif False:",
     "audit_log.a_second_record_never_overwrites_the_first"),
    ("M242-the-per-instance-grammar-is-not-checked", AUDIT,
     "        if ranks != sorted(ranks):",
     "        if False:",
     "audit_log.the_terminator_is_last_and_the_start_is_first"),
    # The half that WAS checked before, so this one pins the half that was not: a start-first
    # test accepts a terminator with a spawn, an event, or anything else recorded after it —
    # a terminator that does not terminate.
    ("M280-only-the-start-record-has-to-be-first", AUDIT,
     "        if ranks != sorted(ranks):",
     "        if kinds and kinds[0] != LINE_START:",
     "audit_log.the_terminator_is_last_and_the_start_is_first"),

    # ---- §10.6's guarantee, read back off the log -----------------------------------------
    ("M243-an-off-list-tool-may-be-advertised", AUDIT,
     "                if name not in allowed:",
     "                if False:",
     "audit_log.the_allowlist_is_checked_in_both_directions"),
    ("M244-an-off-list-call-may-be-forwarded", AUDIT,
     "            elif kind == CALL_FORWARDED and tool not in allowed:",
     "            elif False:",
     "audit_log.the_allowlist_is_checked_in_both_directions"),
    # The direction a "reject everything" proxy passes: refusing a tool the scenario allowed
    # is a silently reduced tool surface, which is a WRONG eval rather than a failed one.
    ("M245-an-allowed-tool-may-be-refused", AUDIT,
     "            elif kind == CALL_REFUSED and tool in allowed:",
     "            elif False:",
     "audit_log.the_allowlist_is_checked_in_both_directions"),

    # ---- the line and event envelopes ------------------------------------------------------
    # `["start"] in LINE_KINDS` raises `TypeError: unhashable type` — §10.4's envelope crash
    # arriving inside the one component whose contract is that it does not raise.
    ("M246-a-line-kind-is-looked-up-before-it-is-typed", AUDIT,
     "        if not isinstance(kind, str) or kind not in LINE_KINDS:",
     "        if kind not in LINE_KINDS:",
     "audit_log.every_malformed_line_is_a_code_rather_than_an_exception"),
    # An unrecognized key on a record, unreported — which is how an interpolated credential
    # ends up in an artifact nobody is checking the shape of.
    ("M247-an-unrecognized-key-on-a-record-is-ignored", AUDIT,
     "    for key in sorted(set(record) - known):",
     "    for key in ():",
     "audit_log.every_malformed_line_is_a_code_rather_than_an_exception"),
    ("M248-an-event-need-not-carry-its-payload", AUDIT,
     "        for key in _EVENT_FIELDS[kind]:",
     "        for key in ():",
     "audit_log.every_malformed_line_is_a_code_rather_than_an_exception"),
    # The tagged-union rule the record layer already applies, one level up: a payload on the
    # wrong event kind is read by nobody, so it can say anything and nothing disagrees.
    ("M249-a-payload-on-the-wrong-event-kind-is-tolerated", AUDIT,
     "        for key in sorted(_EVENT_PAYLOAD_KEYS - set(_EVENT_FIELDS[kind])):",
     "        for key in ():",
     "audit_log.every_malformed_line_is_a_code_rather_than_an_exception"),
    # §10.7's claim for this log is that it is wire-level evidence "per call ... and WHEN".
    ("M250-a-record-need-not-say-when", AUDIT,
     "        usable = is_json_int(ts) or (isinstance(ts, float) and math.isfinite(ts))",
     "        usable = True",
     "audit_log.every_malformed_line_is_a_code_rather_than_an_exception"),
    # ...and the half of that rule that `isinstance(x, float)` alone gets wrong: `NaN` and
    # `Infinity` are floats and are not times, which is `isinstance(True, int)` one type over.
    ("M296-a-non-finite-float-is-a-timestamp", AUDIT,
     "        usable = is_json_int(ts) or (isinstance(ts, float) and math.isfinite(ts))",
     "        usable = is_json_int(ts) or isinstance(ts, float)",
     "audit_log.a_timestamp_is_a_finite_number_and_NaN_is_not_a_line"),
    # ...and the decoder half, which is why a NaN never reaches the check above from a real
    # log: the same `parse_constant` refusal the wire uses, applied to the file.
    ("M297-the-log-decoder-accepts-a-python-extension", AUDIT,
     "            raw = json.loads(line, parse_constant=refuse_json_extension)",
     "            raw = json.loads(line)",
     "audit_log.a_timestamp_is_a_finite_number_and_NaN_is_not_a_line"),
    # The guardian's evidence unvalidated again: optional and unread is how it read clean with
    # the field missing, `false`, or `"alive"` (review, PR #103).
    ("M298-guardian-evidence-is-optional-again", AUDIT,
     '    LINE_SPAWN: (("child_pid", "child_pgid", "guardian_pid"), ()),',
     '    LINE_SPAWN: (("child_pid", "child_pgid"), ("guardian_pid",)),',
     "audit_log.a_spawn_record_must_carry_usable_guardian_evidence"),
    ("M299-any-guardian-value-will-do", AUDIT,
     "        if not is_json_int(guardian) or guardian <= 0:",
     "        if guardian is None:",
     "audit_log.a_spawn_record_must_carry_usable_guardian_evidence"),
    # ...and the half of §10.7's claim that is actually about calls: the rule applied to the
    # three lifecycle records and not to the event records it exists for.
    ("M279-only-the-lifecycle-records-need-a-time", AUDIT,
     "    for record in inst.records:",
     "    for record in (inst.start or {}, inst.spawn or {}, inst.terminator or {}):",
     "audit_log.every_malformed_line_is_a_code_rather_than_an_exception"),

    # ---- the start and spawn records' own claims -------------------------------------------
    ("M251-the-log-need-not-name-the-server-it-gates", AUDIT,
     "        if not isinstance(name, str) or name != server:",
     "        if False:",
     "audit_log.the_log_belongs_to_the_server_it_gates"),
    ("M252-a-spawn-record-may-survive-spawn-failed", AUDIT,
     "    if record.latch == SPAWN_FAILED and inst.spawn is not None:",
     "    if False:",
     "audit_log.a_spawn_record_and_spawn_failed_are_exact_complements"),
    ("M253-a-child-may-be-forwarded-to-without-a-spawn-record", AUDIT,
     "    if record.latch != SPAWN_FAILED and inst.spawn is None:",
     "    if False:",
     "audit_log.a_spawn_record_and_spawn_failed_are_exact_complements"),
    # `start_new_session=True` makes the child its own group leader, so a spawn record where
    # pid and pgid disagree says the group step 4 killed was not the group the child was in —
    # a surviving credential-bearing grandchild, reported as a successful cleanup.
    ("M254-the-child-need-not-lead-its-own-group", AUDIT,
     "        elif pid != pgid:",
     "        elif False:",
     "audit_log.the_spawn_record_must_name_a_group_leader"),
    ("M255-a-pid-is-taken-on-trust", AUDIT,
     "        if not is_json_int(pid) or not is_json_int(pgid):",
     "        if False:",
     "audit_log.the_spawn_record_must_name_a_group_leader"),

    # ---- arming rides on the start record --------------------------------------------------
    # A terminator allowed to declare an arming that never happened...
    ("M256-a-terminator-may-declare-its-own-arming", AUDIT,
     '    raw = {k: v for k, v in (inst.terminator or {}).items() if k != "fault_point"}',
     "    raw = dict(inst.terminator or {})",
     "audit_log.arming_is_read_from_the_start_record_and_nowhere_else"),
    # ...and the direction that matters more: one allowed to hide an arming that did.
    ("M257-arming-recorded-at-the-start-is-not-read", AUDIT,
     '    if inst.start is not None and "fault_point" in inst.start:',
     "    if False:",
     "audit_log.arming_is_read_from_the_start_record_and_nowhere_else"),

    # ---- §10.7's telemetry as evidence about the gate --------------------------------------
    ("M258-an-unimplemented-version-may-be-observed", AUDIT,
     "        elif version not in IMPLEMENTED_VERSIONS:",
     "        elif False:",
     "audit_log.an_observed_version_the_proxy_cannot_implement_is_a_leak"),

    # ---- the proxy's I/O half, proven by tools/verify_mcp_proxy.py ----------------------
    # Production code the selftest cannot reach: it is only executed by running the real
    # program over real pipes, which is why the third suite exists. Every entry below is a
    # defect that would produce a plausible-looking run — the whole failure mode §10.5 names,
    # where a bug is a silently wrong eval rather than a loud failure.

    # The audit log's evidence about the one thing it exists to record. The client still sees a
    # filtered list, so nothing on the wire looks wrong; only the log is lying.
    ("M259-the-log-understates-what-was-forwarded", PROXY_IO,
     "                            forwarded=[n for n in kept if isinstance(n, str)],",
     "                            forwarded=[],",
     "...and the audit log records the filtering as the expected event it is"),
    # The filtered list sent back to the SERVER instead of on to the client, so the client is
    # answered by nothing and the server is told what its own tools are.
    ("M260-a-filtered-result-goes-back-the-way-it-came", PROXY_IO,
     ("                            removed=list(action.removed))\n"
      "            self._send(direction, action.msg, back=False, shutting_down=shutting_down)"),
     ("                            removed=list(action.removed))\n"
      "            self._send(direction, action.msg, back=True, shutting_down=shutting_down)"),
     "an off-list tool is stripped from the advertisement the client sees"),
    # ...and the mirror image: the refusal forwarded to the server, which is the whole boundary
    # inverted — the off-list call reaches the server and the client is never answered.
    ("M261-a-refusal-is-forwarded-instead-of-answered", PROXY_IO,
     ("            self.sink.write(audit.LINE_EVENT, event=audit.CALL_REFUSED, "
      "tool=action.tool)\n"
      "            self._send(direction, action.msg, back=True, shutting_down=shutting_down)"),
     ("            self.sink.write(audit.LINE_EVENT, event=audit.CALL_REFUSED, "
      "tool=action.tool)\n"
      "            self._send(direction, action.msg, back=False, shutting_down=shutting_down)"),
     "an off-list `tools/call` is answered by the proxy and never reaches the server"),
    # `Fail` stops being terminal, which is the one thing §10.5 says it must be. The connection
    # carries on and the cell passes with an unaccounted-for message in it.
    ("M262-an-anomaly-does-not-stop-the-connection", PROXY_IO,
     "            self._trigger(audit.PROTOCOL_ANOMALY, anomaly=action.anomaly.kind)",
     "            pass",
     "a JSON-RPC batch array is an anomaly, not traffic"),
    # The tag without its payload: torn down for a protocol reason, declining to say which.
    ("M263-the-anomaly-kind-is-not-recorded", PROXY_IO,
     "            self._trigger(audit.PROTOCOL_ANOMALY, anomaly=action.anomaly.kind)",
     "            self._trigger(audit.PROTOCOL_ANOMALY, anomaly=None)",
     "protocol_anomaly: it carries WHICH anomaly, not just that there was one"),

    # ---- the three ways an ending gets the wrong name -------------------------------------
    # A server that died mid-request read as the client hanging up. The spec tells clients to
    # RESTART an unexpectedly exited server, so this is the case most easily mistaken for
    # normality — and it is the difference between a failed cell and a clean one.
    ("M264-a-dead-child-reads-as-a-departing-client", PROXY_IO,
     "                self._trigger(audit.CHILD_EXIT)",
     "                self._trigger(audit.CLIENT_EOF)",
     "child_exit: a server that exits while the connection is live"),
    # §4's canonical defect, in the proxy this time: a swallowed read error presenting as a
    # clean end of stream, so an instrument failure wears the clean-shutdown label.
    ("M265-a-read-error-wears-the-clean-shutdown-label", PROXY_IO,
     ("            _note(f\"mcp-proxy: read failed on {direction}: {exc}\")\n"
      "            self._trigger(audit.READ_FAILED)"),
     ("            _note(f\"mcp-proxy: read failed on {direction}: {exc}\")\n"
      "            self._trigger(audit.CLIENT_EOF)"),
     "read_failed: a read error is not an end of stream"),
    # PHASE IS PART OF THE REASON, and these are the two directions of getting it wrong. First:
    # the shutdown-phase write failure recorded as a live one, which fails every clean agy cell
    # — C3-1 measured agy closing stdin and ceasing to read at once.
    ("M266-the-shutdown-drain-fails-the-cell-on-EPIPE", PROXY_IO,
     "            if shutting_down:",
     "            if False:",
     "shutdown_write_failed: measured against agy — recorded, swallowed, and CLEAN"),
    # ...and the reverse: a write that failed MID-CONVERSATION, with what it carried never
    # arriving, recorded as the clean teardown outcome and the cell passing.
    ("M267-a-live-write-failure-is-forgiven-as-a-teardown-one", PROXY_IO,
     "            if shutting_down:",
     "            if True:",
     "client_write_failed: a write to a departed client DURING forwarding"),

    # ---- the shutdown sequence -------------------------------------------------------------
    # No handlers, so default disposition terminates without running one and no terminator is
    # written — on HALF THE SHIPPED FLEET, since C3-1 measured codex and copilot signalling
    # rather than closing stdin. A false failure indistinguishable from the real one.
    ("M268-the-signal-handlers-are-never-installed", PROXY_IO,
     "            signal.signal(sig, lambda _sig, _frame: None)",
     "            pass",
     "signal_term: the client signals, and the handler still writes a terminator"),
    # The runners-up sweep dropped, so a signal arriving during the teardown goes unrecorded and
    # the log cannot say a CLI closed stdin and then signalled.
    # Leading newline: `_pump`'s select loop calls the same handler at 24 spaces. The site this
    # names is the runners-up collection AFTER the pump, which is where a signal is lost.
    ("M269-a-signal-during-the-teardown-is-lost", PROXY_IO,
     "\n            self._on_signal(wake_r)",
     "\n            pass",
     "a client that closes stdin and THEN signals records both, and stays clean"),
    # A buffered log, which makes a killed proxy indistinguishable from one that never ran —
    # and "never ran" is the half of §10.5's partition that is NOT a failure.
    # DEFENDED IN TWO PLACES, so reintroducing the defect has to remove both (cf. M53): the
    # handle is opened LINE-BUFFERED and every write is flushed besides, which means neither
    # edit alone changes the bytes on disk. A block-buffered log is the actual failure — the
    # start record sits in userspace when the SIGKILL lands, and a killed proxy becomes
    # indistinguishable from one that never ran.
    ("M270-the-audit-log-is-not-flushed-per-record", PROXY_IO,
     ('            self._handle = open(path, "a", buffering=1, encoding="utf-8")',
      "        self._handle.flush()"),
     ('            self._handle = open(path, "a", encoding="utf-8")',
      "        pass"),
     "...and the start record was flushed before the child was spawned"),
    # Truncation instead of append, so the anomalous first instance vanishes rather than fails.
    ("M271-a-restart-truncates-the-log", PROXY_IO,
     '            self._handle = open(path, "a", buffering=1, encoding="utf-8")',
     '            self._handle = open(path, "w", buffering=1, encoding="utf-8")',
     "a restarted proxy APPENDS, so the killed instance is still in the file"),
    # STEP 4 AS A NO-OP, claiming success. This is the mutation the two liveness channels exist
    # for: every other check in that file passes against it, the record says `group_terminated:
    # done`, and a credential-bearing grandchild outlives the run.
    # DEFENDED IN TWO PLACES since the guardian became the child's parent (cf. M53, M270), and
    # the second one is deliberate rather than incidental: the reap order sweeps the group
    # before it releases the pin, because reaping is what ends the guardian's licence to signal.
    # Removing step 4 alone therefore leaves nothing alive and reddens only the record checks —
    # which is the arm agreeing with the defect one level away from where the defect lives. The
    # single-edit version was written first and reported exactly that.
    ("M272-the-process-group-is-never-terminated", PROXY_IO,
     ("        delivered = self._step(audit.GROUP_TERMINATED, self._terminate_group)   # step 4",
      "        self._signal(signal.SIGKILL)\n        try:\n            self.child.wait("),
     (("        delivered = True\n"
       "        self._done(audit.GROUP_TERMINATED)                                   # step 4"),
      "        try:\n            self.child.wait("),
     "an ordinary clean shutdown leaves nothing in the child's process group alive"),
    # Forced termination made a failure, so a server that merely needed SIGKILL fails the cell —
    # which §10.5.1 classifies CLEAN because the spec only SHOULDs a prompt exit.
    ("M273-a-killed-child-fails-the-cell", PROXY_IO,
     "                self._outcome(audit.SHUTDOWN_CHILD_KILLED)",
     "                self._outcome(audit.SHUTDOWN_READ_FAILED)",
     "shutdown_child_killed: forced termination is the standard escalation, so CLEAN"),
    # The drain given its own deadline again, so the escalation never runs and a server that
    # needed SIGKILL is reported as a `shutdown_anomaly` instead of ending cleanly.
    # BOTH ANCHORS CARRY CONTEXT UNIQUE TO STEP 3, because step 4's loop is now textually
    # identical — same orders, same `_deliver`, same refusal check. `replace(..., 1)` would take
    # whichever came first in the file and report CAUGHT either way, so an ambiguous anchor here
    # is a mutation that quietly stops testing what it names.
    ("M274-the-drain-gives-up-before-the-escalation", PROXY_IO,
     ('            raise TimeoutError("step 3 is suppressed, and the child has not finished")\n'
      "        for order in (ORDER_TERM, ORDER_KILL):"),
     ('            raise TimeoutError("step 3 is suppressed, and the child has not finished")\n'
      "        for order in ():"),
     "shutdown_child_killed: forced termination is the standard escalation, so CLEAN"),
    # The escalation's refusal ignored, so `shutdown_child_killed` is written on the path where
    # NOTHING was signalled — the guardian-loss ending, whose policy is deliberately to signal
    # nothing, logging a kill beside the two failures proving it did not happen.
    ("M317-a-kill-that-could-not-be-delivered-is-recorded-as-delivered", PROXY_IO,
     ("                raise _EscalationUndelivered(\n"
      '                    f"the child\'s stdout was still open and step 3\'s escalation could '
      'not be "\n                    f"delivered: {error}")'),
     "                pass",
     "...and records no kill it did not deliver"),
    # An unbounded drain instead of an anomaly: something outside the child's group holds its
    # stdout and the proxy waits for it, which is a hang rather than a verdict.
    ("M275-a-drain-that-never-ends-is-not-reported", PROXY_IO,
     '        raise TimeoutError(\n            f"the child\'s stdout was still open after a group SIGKILL',
     '        self._done(audit.DRAIN_ENDED)\n        return\n        raise TimeoutError(\n            f"the child\'s stdout was still open after a group SIGKILL',
     "shutdown_anomaly: a bounded drain that never reaches EOF says so"),

    # ---- the fault point, which is itself under test ---------------------------------------
    # Arming not recorded, so a hook that silently never fires produces a PASSING run — the
    # exact case §10.9 spends a dedicated arm-only run on.
    ("M276-arming-is-not-recorded-in-the-start-record", PROXY_IO,
     '            started["fault_point"] = self.fault.record()',
     "            pass",
     "armed and wired to suppress nothing STILL fails the instance"),
    # Suppression that relabels the fact instead of skipping the step, so the control's steps
    # 3-5 run after all and the processes it needs alive are killed.
    ("M277-a-suppressed-step-runs-anyway", PROXY_IO,
     ("        self.fired.append(fact)\n"
      "        self._failed(fact, audit.FAULT_POINT_FIRED)\n"
      "        return True"),
     ("        self.fired.append(fact)\n"
      "        self._failed(fact, audit.FAULT_POINT_FIRED)\n"
      "        return False"),
     "with steps 3-5 suppressed, BOTH channels are still open after the proxy exits"),
    # Two incompatible accounts of one step: the typed outcome AND a firing, which says the step
    # was attempted and failed and also that it never ran.
    ("M278-an-injected-failure-also-claims-suppression", PROXY_IO,
     ("        if mode == FAIL:\n"
      "            typed = audit.typed_outcome(fact)"),
     ("        if mode == FAIL:\n"
      "            self.fired.append(fact)\n"
      "            typed = audit.typed_outcome(fact)"),
     "shutdown_read_failed: recorded, paired to drain_ended, and anomalous"),

    # ---- framing: every way a line can fail to BE a line (review, PR #103) --------------
    ("M281-an-empty-line-is-skipped-rather-than-refused", PROXY_IO,
     "            try:\n                text = line.decode(\"utf-8\")",
     ("            if not line.strip():\n                continue\n"
      "            try:\n                text = line.decode(\"utf-8\")"),
     "a blank line is not a message, and is terminal"),
    # `errors="replace"` on a trust boundary is a REWRITE, not a tolerance: it forwards bytes
    # the peer never sent, and the cell passes.
    ("M282-undecodable-bytes-are-rewritten-and-forwarded", PROXY_IO,
     '                text = line.decode("utf-8")',
     '                text = line.decode("utf-8", "replace")',
     "bytes that are not UTF-8 are not a message, and are terminal"),
    # The residue discarded at EOF, so a stream that ended mid-message reads as one that ended.
    ("M283-a-half-written-line-at-EOF-is-discarded", PROXY_IO,
     "            self._flush_residue(direction)",
     "            self._buffers[direction] = b\"\"",
     "a partial line at EOF is not a message, and is terminal"),
    # The drain's terminality test back to a truthiness check on an accumulator that the
    # teardown guarantees is already non-empty, so a malformed frame is recorded and then
    # followed by more forwarding.
    ("M284-a-drain-anomaly-does-not-stop-the-drain", PROXY_IO,
     "            if len(self.triggers) > before:",
     "            if self.triggers and not shutting_down:",
     "a malformed frame during the drain stops it, and what followed is not forwarded"),
    ("M285-NaN-and-Infinity-reach-the-decision-layer", PROXY,
     "        msg = json.loads(line, parse_constant=refuse_json_extension)",
     "        msg = json.loads(line)",
     "proxy.a_json_extension_constant_is_not_a_legal_message"),

    # ---- the group's verdict is positive evidence, not an errno ------------------------
    # THE REVIEWED DEFECT ITSELF: `EPERM` read as an empty group. It is drivable because a
    # zombie-only group answers the probe with exactly that errno on macOS — which is how the
    # original inference came to look measured — and the `child_reaped=fail` case leaves our own
    # unreaped child in the group on purpose. A mutant that reads it as confirmation records one
    # outcome where two are true, and would certify a live member a sandbox had made
    # unsignallable (review, PR #103).
    ("M286-an-unsignallable-group-is-confirmed-gone", PROXY_IO,
     ("        except OSError:\n"
      "            pass                         # present but unsignallable; still present"),
     ("        except OSError:\n"
      "            return True"),
     "shutdown_reap_failed: recorded, paired to child_reaped, and anomalous"),
    ("M287-the-group-is-never-confirmed-empty", PROXY_IO,
     "            self._guard(audit.GROUP_TERMINATED, self._confirm_group_gone)",
     "            self._done(audit.GROUP_TERMINATED)",
     "shutdown_reap_failed: recorded, paired to child_reaped, and anomalous"),
    # ...and the false-failure direction, which §10.5 weighs the same: `ESRCH` — the group is
    # gone, which is the goal — no longer read as confirmation, so every clean cell fails.
    ("M294-ESRCH-is-not-read-as-confirmation", PROXY_IO,
     ("        except ProcessLookupError:\n"
      "            return True\n"
      "        except OSError:"),
     ("        except ProcessLookupError:\n"
      "            return False\n"
      "        except OSError:"),
     "...and the whole exchange still ends clean"),
    # NO MUTATION for preferring the guardian's report over a probe of the proxy's own, and it
    # is worth saying why rather than leaving the gap: by the time the fact is settled the reap
    # has happened either way, so the two answers differ only inside the pid-reuse window — the
    # thing nothing can stage. The report is kept because it is the tighter measurement, taken
    # in the process that did the reaping; the probe is not wrong, it is later.

    # ---- the guardian ------------------------------------------------------------------
    # A guardian that never watches: it is the child's parent, so a proxy SIGKILLed before its
    # teardown leaves a credential-bearing server with nothing that will terminate it. The
    # audit still fails the cell, which is exactly why this needs a case that looks at the
    # PROCESS rather than at the record.
    # THE SWEEP ITSELF, made a no-op: the guardian still notices the death, still exits, and
    # leaves the group running. This is the mutation that proves the whole mechanism, and it is
    # deliberately aimed at the ACTION rather than at the detection — a guardian that exits
    # early instead closes the report pipe, which the proxy reads as `guardian_lost` and cleans
    # up from, so it reddens the record checks while leaving nothing alive. The arm and the
    # defect agreeing one level away from the defect, again; the first version did exactly that.
    ("M288-the-guardian-notices-and-does-nothing", PROXY_IO,
     ("        self._signal(signal.SIGTERM)\n"
      "        time.sleep(min(self.grace, 0.2))\n"
      "        self._signal(signal.SIGKILL)"),
     "        pass",
     "a proxy SIGKILLed before its teardown: nothing in the child's process group is left alive"),
    # ...and the detection: the lifeline's EOF no longer read as the proxy being gone, so the
    # one ending the guardian exists for is the one it sits through.
    # `return` RATHER THAN `continue`, and the difference is not stylistic. The first version
    # spun: with the lifeline at EOF the descriptor is always readable, so the loop turned into
    # a busy wait in a process that is its own session leader and has no parent to reap it —
    # 24 orphans at 35% CPU each, accumulated across three runs before anyone looked at
    # Activity Monitor, with the machine's load average at 186.
    #
    # A MUTANT IS BROKEN CODE BY CONSTRUCTION, SO THE BOUND IT BREAKS MAY BE ITS ONLY WAY OUT.
    # That is a constraint on how mutations are WRITTEN and not something to be cleaned up
    # after: the cleanup this incident first grew — a `ps`-scanning reaper in the runner — was
    # a process-killing loop added to a test tool, and the mutation written to prove it worked
    # was `kill(SIGKILL)` over every line of `ps ax`. Both are gone. A mutation of a loop exit
    # must leave the process able to exit, and that is checked by reading it, here, before it
    # is added.
    ("M289-an-EOF-on-the-lifeline-is-not-a-death", PROXY_IO,
     ("            if not order:\n"
      "                break                    # EOF: the proxy is gone and never released "
      "the pin"),
     ("            if not order:\n"
      "                return"),
     "a proxy SIGKILLed before its teardown: nothing in the child's process group is left alive"),
    # The guardian's program never runs the guardian, so no child is ever started and the
    # handshake times out. Fail-closed is the intended behaviour of a MISSING guardian, so the
    # arm this reddens is the one that drives that on purpose.
    ("M295-the-guardian-program-does-nothing", PROXY_IO,
     "        return run_guardian(int(args[1]))",
     "        return 0",
     "the control: with a working guardian, this wiring DOES start a server that announces"),
    # Standing down on anything but a FIRED retention, which is the reviewed defect: a teardown
    # that ran and failed silenced the mechanism that exists to clean up after it.
    ("M290-any-teardown-that-ran-stands-the-guardian-down", PROXY_IO,
     ("        retained = [f for f in (audit.GROUP_TERMINATED, audit.CHILD_REAPED) "
      "if f in self.fired]"),
     "        retained = [audit.GROUP_TERMINATED]",
     ("steps 4 and 5 both failed, so the sweep is the one on the lifeline's EOF: and the group "
      "is swept anyway, evidence kept")),
    # ...and the other direction: standing down never happens, so the guardian sweeps up the
    # survivors §10.9's control deliberately leaves alive and the evidence goes with them.
    ("M302-the-guardian-is-never-stood-down", PROXY_IO,
     "        if retained and self._lifeline is not None:",
     "        if False:",
     "with steps 3-5 suppressed, BOTH channels are still open after the proxy exits"),
    # The reap that releases the pin no longer sweeps first, so a step 4 that failed leaves the
    # group alive at exactly the moment the guardian stops being able to signal it.
    ("M303-the-pin-is-released-without-a-sweep", PROXY_IO,
     "        self._signal(signal.SIGKILL)\n        try:\n            self.child.wait(",
     "        try:\n            self.child.wait(",
     ("step 4 failed, so the reap order sweeps before it releases the pin: and the group is "
      "swept anyway, evidence kept")),
    # A guardian established after the child, which is the window a SIGKILL fits through — and
    # is unreachable now that the guardian is what spawns it. What IS reachable is the other
    # half of the same rule: a guardian that could not be established, not failing the run.
    ("M304-an-unestablished-guardian-is-not-a-spawn-failure", PROXY_IO,
     ("        ready = self._establish_guardian()\n"
      "        if ready is None:"),
     ("        ready = self._establish_guardian() or {\n"
      "            \"guardian_pid\": 0, \"child_pid\": 0, \"child_pgid\": 0}\n"
      "        if False:"),
     "the guardian's program is not there: no server runs, and the instance ends `spawn_failed`"),
    # The readiness handshake reduced to `Popen` having returned, which says a fork happened and
    # nothing about whether our code ran in it.
    ("M305-any-ready-report-will-do", PROXY_IO,
     ("        if ready is None or not audit.is_json_int(ready.get(\"guardian_pid\")) \\\n"
      "                or ready[\"guardian_pid\"] != self.guardian.pid:"),
     "        if ready is None:",
     "a ready report whose pid is not the guardian's is not readiness"),
    # The guardian's death during a run treated as ordinary, so a live credential-bearing child
    # keeps being forwarded to with nothing holding its identity.
    ("M306-a-lost-guardian-is-not-terminal", PROXY_IO,
     "        self._trigger(audit.GUARDIAN_LOST)",
     "        pass",
     "a guardian that dies once the child exists latches `guardian_lost`"),
    # ...and the reviewed defect itself, reintroduced: with the pin holder gone, signal the
    # remembered pgid anyway. `getpgid(pid) == pgid` is what used to authorize this, and it is
    # not an identity — a reaped pid can be reused, and a group leader's pgid IS its pid, so the
    # check degenerates to "some group leader has this number" (review, PR #103). The arm is the
    # one that requires the server to be STILL RUNNING and the record to say so.
    ("M307-a-lost-pin-signals-the-remembered-pgid-anyway", PROXY_IO,
     ("        report = None if self._guardian_lost() else self._ask(order)\n"
      "        if report is None:"),
     ("        report = None if self._guardian_lost() else self._ask(order)\n"
      "        if report is None and os.getpgid(self.child_pid) == self.child_pgid:\n"
      "            os.killpg(self.child_pgid, signal.SIGKILL)\n"
      "            return None\n"
      "        if report is None:"),
     "...and the proxy REFUSES to signal a group nothing can identify, and says so"),
    # A DIAGNOSTIC THAT CAN ABORT THE CLEANUP IT DESCRIBES. The sweep's own log line, moved back
    # ahead of the signal and written with a bare `print`: with the CLI's end of stderr closed
    # it raises `BrokenPipeError`, the guardian exits, and a credential-bearing group survives.
    ("M308-the-sweep-announces-itself-before-it-acts", PROXY_IO,
     "        self._signal(signal.SIGTERM)\n        time.sleep(min(self.grace, 0.2))",
     ('        print(f"mcp-proxy guardian: terminating group {self.child_pgid}",\n'
      "              file=sys.stderr)\n"
      "        self._signal(signal.SIGTERM)\n        time.sleep(min(self.grace, 0.2))"),
     "...and the guardian still sweeps: a diagnostic cannot abort the cleanup it describes"),
    # ...and the same rule one process over: a raising diagnostic inside the pump, which the
    # catch-all then reports as `read_failed` — a protocol fault logged as a pipe fault.
    ("M309-a-diagnostic-in-the-pump-can-raise", PROXY_IO,
     '            _note(f"mcp-proxy: anomaly {action.anomaly.kind}: {action.anomaly.detail}")',
     ('            print(f"mcp-proxy: anomaly {action.anomaly.kind}: '
      '{action.anomaly.detail}",\n                  file=sys.stderr)'),
     "a broken stderr does not turn a protocol anomaly into a read failure"),
    # THE TWO-PHASE HANDSHAKE COLLAPSED BACK INTO ONE: the launch order written before the ready
    # report is read, so the guardian holds a command before it has been authenticated and
    # spawns the server the proxy is about to repudiate. The audit still says `spawn_failed` and
    # records no spawn — which is exactly the point, since the marker proves the command ran.
    # ONE EDIT, in the proxy, because the guardian needs none: it reads the next line whenever
    # that line arrives, so writing it early is the whole defect.
    ("M310-the-launch-order-goes-out-before-the-guardian-is-trusted", PROXY_IO,
     ("        if not self._order(order_w, setup):\n"
      "            return None\n"
      "        ready = self._read_report(time.monotonic() + self.grace)"),
     ("        if not self._order(order_w, setup):\n"
      "            return None\n"
      '        if not self._order(order_w, {"command": self.cfg.command,\n'
      '                                     "args": list(self.cfg.args), "env": env,\n'
      '                                     "cwd": self.cfg.cwd,\n'
      '                                     "inherit": list(inherit)}):\n'
      "            return None\n"
      "        ready = self._read_report(time.monotonic() + self.grace)"),
     "a rejected guardian is never told what to run, and says which phase it reached"),
    # The guardian injection unrecorded again, so an injected guardian failure and a real one
    # are the same record — a fault point with no provenance.
    ("M312-the-guardian-injection-is-not-recorded", PROXY_IO,
     ('        found = {"suppresses": sorted(self.targets)}\n'
      "        if self.guardian:"),
     ('        found = {"suppresses": sorted(self.targets)}\n'
      "        if False:"),
     "the guardian's program is not there: the injection is recorded in the start record"),
    ("M313-a-guardian-injection-alone-does-not-arm-the-fault-point", PROXY_IO,
     "            return cls(armed=bool(guardian), guardian=guardian)",
     "            return cls(armed=False, guardian=guardian)",
     "the guardian's program is not there: the injection is recorded in the start record"),
    ("M314-any-guardian-mode-is-accepted", AUDIT,
     "        elif record.guardian not in GUARDIAN_MODES:",
     "        elif False:",
     "audit.the_guardian_injection_is_recorded_beside_the_suppression_targets"),
    ("M316-the-guardian-mode-is-membership-tested-before-it-is-typed", AUDIT,
     ("        if not isinstance(record.guardian, str):\n"
      '            problems.append(f"guardian_mode_not_a_string:{record.guardian!r}")\n'
      "        elif record.guardian not in GUARDIAN_MODES:"),
     ("        if False:\n"
      '            problems.append(f"guardian_mode_not_a_string:{record.guardian!r}")\n'
      "        elif record.guardian not in GUARDIAN_MODES:"),
     "audit.no_json_value_can_make_the_reader_raise"),
    ("M315-the-fault-point-map-is-open", AUDIT,
     '            for key in sorted(set(raw["fault_point"]) - {"suppresses", "guardian"}):',
     '            for key in sorted(set(raw["fault_point"]) - set(raw["fault_point"])):',
     "audit.the_guardian_injection_is_recorded_beside_the_suppression_targets"),

    # ---- the archived log carries codes, not prose --------------------------------------
    ("M292-the-drop-event-carries-the-peers-own-text", PROXY_IO,
     "self.sink.write(audit.LINE_EVENT, event=audit.MESSAGE_DROPPED, reason=action.code)",
     "self.sink.write(audit.LINE_EVENT, event=audit.MESSAGE_DROPPED, reason=action.detail)",
     "a late response to a cancelled request is dropped, and recorded as a CODE"),
    ("M293-a-drop-reason-may-be-any-string", AUDIT,
     "            if not isinstance(reason, str) or reason not in DROP_REASONS:",
     "            if not isinstance(reason, str) or not reason:",
     "audit_log.every_malformed_line_is_a_code_rather_than_an_exception"),

    # ---- F: instruments, proven by tools/verify_mcp_fixtures.py -------------------------
    # Everything above asks whether the selftest notices a defect in production code. These
    # ask whether the fixture verifier notices a defect in a fixture or probe — the gap named
    # in that file's own header ("fixtures carry no selftest arms and are not mutation
    # targets"), which left 278 named checks with nothing proving any of them can fail. Each
    # one below reintroduces a defect this repo actually shipped, most of them from #100's
    # instrument rounds.
    ("F1-shim-decides-era-by-method", SHIM,
     '        return "modern", v, "_meta"',
     '        return "modern", v, "method"',
     "decided by _meta not method"),
    # C3-0's load-bearing column is what the two parties SPOKE, not what one of them asked
    # for. Recording the claim puts `2099-01-01` in the results table for a session that
    # actually ran on 2025-11-25.
    ("F2-shim-records-the-clients-guess-as-the-era", SHIM,
     "    version = claimed if version is None else version",
     "    version = claimed",
     "era.version is not the client's guess"),
    # The §4 lesson in its original form: a failed read swallowed into an empty chunk makes
    # the main loop log `stdin_eof`, so an instrument failure is published as "this CLI shut
    # the server down cleanly" — the exact conclusion C3-1 exists to draw.
    ("F3-shim-reports-a-failed-read-as-clean-eof", SHIM,
     ("                _rx_failed = True\n"
      "                _rx_eof = True\n"),
     ("                _rx_failed = False\n"
      "                _rx_eof = True\n"),
     "...and the terminator says so instead of claiming a clean stdin close"),
    # The identity marker exists because a model can reconstruct a plausible-looking reply
    # from the server name alone. A constant marker is reconstructible, which is the whole
    # defect — and it looks correct in every transcript.
    ("F4-echo-identity-marker-is-a-constant", ECHO,
     '        return _text(f"{IDENTITY}:{text}" if IDENTITY else text)',
     '        return _text(f"echo:{text}" if IDENTITY else text)',
     "...and it is the instance's OWN marker, not a constant"),
    # Modern envelope shape leaking into legacy replies. A legacy client is entitled to a
    # result with none of this in it, and the proxy's own era rules are written against a
    # fixture that keeps them apart.
    ("F5-echo-sends-modern-shape-to-legacy-clients", ECHO,
     "    if modern:\n        out = {\"resultType\": \"complete\"}",
     "    if True:\n        out = {\"resultType\": \"complete\"}",
     "no resultType leaked into legacy"),
    # The row's TRUSTWORTHINESS back out of the classifier and into the exit status alone —
    # the defect that let the probe print the fleet-wide negative over a broken instrument
    # and then exit 1, having already published the wrong sentence (review, PR #100).
    ("F6-probe-classifier-ignores-a-dead-reader", PIPEPROBE,
     '    if row.get("reader_failed"):\n        return INSTRUMENT_FAILED',
     '    if False:\n        return INSTRUMENT_FAILED',
     "a row whose shim broke is classified as instrument-failed, not as a measurement"),
    # Classified correctly and then dropped from the one predicate every caller reads. This
    # is the same defect one layer downstream, and it is why the two are separate mutations:
    # F6 alone would leave this path uncovered.
    ("F7-probe-unmeasured-forgets-instrument-failure", PIPEPROBE,
     "            if classify(r) in (NO_ERA, UNMEASURED, INSTRUMENT_FAILED)]",
     "            if classify(r) in (NO_ERA, UNMEASURED)]",
     "...so it counts as unmeasured"),
    # A live duplicate counted as post-response reuse: the false positive that reported a
    # client pipelining `ping(1)` behind a held `initialize(1)` as proof that §10.4 refuses
    # something the spec permits, when JSON-RPC forbids it outright (review, PR #100).
    ("F8-probe-counts-a-live-duplicate-as-reuse", PIPEPROBE,
     "            if key in live:\n                duplicates.append(key)",
     "            if key in live:\n                reuse.append(key)",
     "a repeat BEFORE the response is a live duplicate, not that reuse"),

]


# A selftest never legitimately runs this long — the whole suite is ~10-30s, with a couple
# of arms deliberately joining a 20s thread. A mutation that blows past this is looping or
# blocked (M65's followlinks flip once walked a whole real home at 100% CPU because a test
# helper fed it a real-home overlay), and without a bound it wedges the ENTIRE suite with no
# output. Bounded, such a mutation is reported as TIMEOUT and the suite carries on — a
# hanging mutation is a finding, not a reason to lose the other 78.
_SUITE_TIMEOUT = 300

# Each suite says how to run it and how to read a failed check out of its output. The parser
# is per suite because the two report differently — the selftest prints `[FAIL] name: msg`,
# the fixture verifier prints `FAIL label  <- detail` — and a runner that guessed one format
# for both would read every failure of the other as "failed, but NOT via", which reports a
# working arm as a broken one.
_SUITES = {
    "selftest": (("-m", "agentskill_evals", "selftest"), r"\[FAIL\]\s+([^:]+):"),
    "fixtures": ((VERIFIER,), r"^\s*FAIL\s+(.+?)\s\s<- "),
    "proxy": ((PROXY_VERIFIER,), r"^\s*FAIL\s+(.+?)\s\s<- "),
}

# The two files the third suite proves, named rather than derived: `mcp_proxy_io.py` sits in
# `agentskill_evals/` where the selftest would otherwise be asked to catch it and would report
# MISSED for every entry, and `proxy_target_server.py` sits in `fixtures/` where
# `verify_mcp_fixtures.py` would. A path prefix cannot express either, because both directories
# hold files belonging to the other suites.
_PROXY_SUITE = (PROXY_IO, TARGET)


def _suite_for(rel):
    """Which suite proves a mutation of `rel` — derived from the path, never declared.

    `agentskill_evals/` is reachable from the selftest and nothing else drives it;
    `fixtures/` and `tools/` are driven only by `verify_mcp_fixtures.py`, which does not
    import them. Routing by hand would put a sixth field on 180-odd entries whose value is
    already implied by the second, and a wrong one reports MISSED for a defect whose checker
    never ran — indistinguishable, in the output, from a decorative check.

    The proxy's I/O half is the exception the path cannot express, so it is named: it lives in
    `agentskill_evals/` but no arm can reach it, and its fixture lives in `fixtures/` but the
    other verifier does not drive it.
    """
    if rel in _PROXY_SUITE:
        return "proxy"
    return "fixtures" if rel.startswith(("fixtures/", "tools/")) else "selftest"


def run(cwd, suite):
    """Run one suite in `cwd`. Returns (returncode, output, elapsed_seconds).

    Elapsed is measured with a monotonic clock so a wall-clock adjustment mid-suite (a run
    this long can straddle one) cannot produce a negative or wildly inflated duration.
    """
    argv, _ = _SUITES[suite]
    t0 = time.monotonic()
    try:
        p = subprocess.run([str(cwd / ".venv/bin/python"), *argv],
                           cwd=cwd, capture_output=True, text=True, timeout=_SUITE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return 124, "__TIMEOUT__", time.monotonic() - t0
    return p.returncode, p.stdout + p.stderr, time.monotonic() - t0


def failed_checks(suite, out):
    """The named checks that went red, read with the parser belonging to `suite`.

    Extracted rather than inlined so it can be driven on captured output: a parser that
    silently matches nothing turns every catch into "failed, but NOT via", which looks like
    a coverage problem and is a tooling one.
    """
    return re.findall(_SUITES[suite][1], out, re.MULTILINE)


def _classify(mid, rel):
    """`M`, `I` or `F` for one mutation, refusing any entry where id and target disagree.

    The prefix and the target are independent facts, so a convention relating them holds
    only until someone types the wrong letter — at which point an `M` aimed at the selftest
    is counted as production coverage, or an `I` aimed at production is excused from it.
    Every such direction is exactly the miscount the split reporting exists to prevent, so
    the ID is checked against the file rather than trusted (review, fifth round).

    Raises rather than warns: a suite that reports a wrong total is worse than one that
    refuses to start, and this runs before any baseline so the cost is a second.
    """
    if rel in (SELF, VERIFIER, PROXY_VERIFIER):
        raise SystemExit(
            f"mutation {mid!r} targets {rel}, which is a suite rather than something a suite "
            f"checks. Asking the mutation runner whether it notices being mutated, or either "
            f"verifier whether it notices, establishes nothing about any of them.")
    # BY THE FILE'S ROLE, not by which suite proves it. Reading the class off `_suite_for` was
    # right while there were two suites and wrong the moment a third arrived: `mcp_proxy_io.py`
    # is production proven by a driver, and `proxy_target_server.py` is an instrument proven by
    # the same one, so the suite says nothing about which total either belongs in.
    expected = ("I" if rel == SELFTEST
                else "F" if rel.startswith(("fixtures/", "tools/")) else "M")
    kind = mid[0] if mid[0] in "MIF" else "?"
    if kind != expected:
        raise SystemExit(
            f"mutation {mid!r} is misclassified: {rel} is a {expected}* target — `M*` perturbs "
            f"production, `I*` perturbs {SELFTEST}, `F*` perturbs a fixture or tool proven by "
            f"{VERIFIER}. This one is {mid[0]}*. Rename it, or retarget it — the three totals "
            f"are only meaningful while the id and the file agree.")
    return kind


def main():
    started = time.monotonic()
    # Before the baseline, which costs a selftest run: a misclassified entry makes every
    # number below it wrong, so it is worth nothing to discover that at the end.
    kinds = {mid: _classify(mid, rel) for mid, rel, _f, _r, _a in MUTATIONS}
    suites = {mid: _suite_for(rel) for mid, rel, _f, _r, _a in MUTATIONS}
    tmp = Path(tempfile.mkdtemp(prefix="mutate-mcp-"))
    work = tmp / "harness"
    shutil.copytree(HARNESS, work, symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__", "artifacts", "build"))
    # One baseline per suite actually used. Only the suites in play: paying for a green run
    # of a suite nothing below mutates would be measuring the machine, and a suite whose
    # baseline is never checked can be broken while its mutations all report CAUGHT for the
    # wrong reason.
    baseline = {}
    for suite in sorted(set(suites.values())):
        rc, out, secs = run(work, suite)
        if out == "__TIMEOUT__":
            print(f"BASELINE TIMED OUT after {_SUITE_TIMEOUT}s — the unmutated {suite} suite "
                  f"hung, so nothing below would prove anything.")
            return 1
        if rc != 0:
            print(f"BASELINE FAILED ({suite}) — mutations prove nothing:")
            print(out[-3000:])
            return 1
        # The reference every per-mutation time below is read against; without it those
        # numbers describe the machine, not the mutation.
        baseline[suite] = secs
        print(f"baseline: {suite} PASSED in {secs:.1f}s")
    print()

    # Counted apart, and reported apart. An `I*` mutation perturbs the INSTRUMENT (the
    # selftest itself) rather than the code under test, so folding it into one total would
    # make "N/N caught" claim more production coverage than exists. Splitting it here is what
    # keeps the exception visible at the point anyone reads the result, instead of only to
    # someone who opens this file — and makes adding a second one a deliberate act that shows
    # up in the output rather than a quiet edit to the list.
    caught = {"M": 0, "I": 0, "F": 0}
    totals = {"M": 0, "I": 0, "F": 0}
    slowest = (0.0, None)
    for mid, rel, find, repl, arm in MUTATIONS:
        kind = kinds[mid]                    # validated against `rel` before the baseline
        suite = suites[mid]
        totals[kind] += 1
        path = work / rel
        original = path.read_text()
        # `find`/`repl` may be tuples: some properties are now defended in two places, and
        # reintroducing the defect means removing both (see M53).
        edits = list(zip(find, repl)) if isinstance(find, tuple) else [(find, repl)]
        counts = [original.count(f) for f, _ in edits]
        if any(c == 0 for c in counts):
            # No time to report, and "(0.0s)" would read as a suite that ran instantly rather
            # than one that never started — the same lie the None/[] distinction exists to
            # prevent elsewhere. Say which it is.
            print(f"{mid}: STALE ANCHOR — text not found in {rel} ({suite} not run)")
            continue
        if any(c > 1 for c in counts):
            # THE OTHER HALF OF THE SAME GUARD, and the half that fails silently. `replace(f, r,
            # 1)` takes whichever occurrence comes first, so an anchor matching twice still
            # produces a mutant, still reddens SOME arm, and still prints CAUGHT — while testing
            # whichever site happens to be earlier in the file rather than the one the mutation
            # is named for. Five entries were in this state, four of them because a 4-space
            # anchor is a substring of the same line indented 8 (the leading-newline lesson in
            # §4, which nothing enforced), and one because a fix here made two functions
            # textually identical. Refused rather than warned: a mutation that has quietly
            # stopped testing what it names is exactly the failure this suite exists to prevent
            # in the code it mutates (review, PR #103).
            print(f"{mid}: AMBIGUOUS ANCHOR — matches {counts} times in {rel}; pin it with a "
                  f"leading newline or adjacent context ({suite} not run)")
            continue
        mutated = original
        for f, r in edits:
            mutated = mutated.replace(f, r, 1)
        path.write_text(mutated)
        rc, out, elapsed = run(work, suite)
        path.write_text(original)
        took = f"({elapsed:.1f}s)"
        if elapsed > slowest[0]:
            slowest = (elapsed, mid)
        failed = failed_checks(suite, out)
        if out == "__TIMEOUT__":
            # Not a clean catch: the arm never got to report because the suite hung. A
            # mutation whose defect is an infinite loop must be caught by an arm that BOUNDS
            # the work (a thread + join), not by the suite's own timeout — so this counts as
            # uncaught and fails the run, forcing a real fix rather than masking the hang.
            print(f"{mid}: *** TIMEOUT *** {suite} exceeded {_SUITE_TIMEOUT}s — the "
                  f"defect hangs rather than reddening {arm} {took}")
        elif rc != 0 and arm in failed:
            print(f"{mid}: CAUGHT by {arm} {took}")
            caught[kind] += 1
        elif rc != 0:
            print(f"{mid}: failed, but NOT via {arm} -> {failed} {took}")
        else:
            print(f"{mid}: *** MISSED *** {suite} still passes with the defect present {took}")
    print(f"\n{caught['M']}/{totals['M']} production mutations caught by the intended arm")
    if totals["I"]:
        print(f"{caught['I']}/{totals['I']} instrument mutation(s) caught — these perturb "
              f"the selftest itself (see SELFTEST in the target list), and are NOT evidence "
              f"of production coverage")
    if totals["F"]:
        print(f"{caught['F']}/{totals['F']} fixture/tool mutation(s) caught — these perturb "
              f"instruments rather than production code, so they are NOT part of the "
              f"production total either")
    total = time.monotonic() - started
    slow = f"slowest {slowest[1]} at {slowest[0]:.1f}s" if slowest[1] else "no mutation ran"
    bases = ", ".join(f"{s} baseline {v:.1f}s" for s, v in sorted(baseline.items()))
    print(f"elapsed: {total / 60:.1f} min total, {bases}, {slow}")
    shutil.rmtree(tmp, ignore_errors=True)
    # Both must be clean: an instrument mutation surviving means the arm guarding the
    # selftest's own reporting is decorative, which is the same failure as any other MISSED.
    return 0 if caught == totals else 1


if __name__ == "__main__":
    sys.exit(main())
