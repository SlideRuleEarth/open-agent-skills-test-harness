#!/usr/bin/env python3
"""Mutation-test the declared-MCP and credential-containment arms.

Each mutation reintroduces a plausible version of the defect the arm exists to catch; the
named arm MUST go red. An arm that stays green while its defect is present is decorative.

Run it: `python3 harness/tools/mutate_mcp.py` (needs `harness/.venv`). It copies the tree to
a tempdir, checks the baseline passes, then applies one mutation at a time and re-runs the
selftest. Exit 0 only when every mutation is caught BY ITS NAMED ARM — "some arm failed" is
not the same claim.

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

Every result line carries the wall time of the selftest run that produced it, and the summary
names the slowest. Read those against the `baseline:` line: a mutation taking several times
the baseline is a defect that costs runtime rather than one that reddens an arm, and it is
the only notice anyone gets before it grows past `_SELFTEST_TIMEOUT` and reports as a hang.
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
    ("M7-redact-shortest-first", MCP,
     "    for form in sorted(forms, key=len, reverse=True):",
     "    for form in sorted(forms, key=len):",
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
]


# A selftest never legitimately runs this long — the whole suite is ~10-30s, with a couple
# of arms deliberately joining a 20s thread. A mutation that blows past this is looping or
# blocked (M65's followlinks flip once walked a whole real home at 100% CPU because a test
# helper fed it a real-home overlay), and without a bound it wedges the ENTIRE suite with no
# output. Bounded, such a mutation is reported as TIMEOUT and the suite carries on — a
# hanging mutation is a finding, not a reason to lose the other 78.
_SELFTEST_TIMEOUT = 300


def run(cwd):
    """Run the selftest in `cwd`. Returns (returncode, output, elapsed_seconds).

    Elapsed is measured with a monotonic clock so a wall-clock adjustment mid-suite (a run
    this long can straddle one) cannot produce a negative or wildly inflated duration.
    """
    t0 = time.monotonic()
    try:
        p = subprocess.run(
            [str(cwd / ".venv/bin/python"), "-m", "agentskill_evals", "selftest"],
            cwd=cwd, capture_output=True, text=True, timeout=_SELFTEST_TIMEOUT)
    except subprocess.TimeoutExpired:
        return 124, "__TIMEOUT__", time.monotonic() - t0
    return p.returncode, p.stdout + p.stderr, time.monotonic() - t0


def main():
    started = time.monotonic()
    tmp = Path(tempfile.mkdtemp(prefix="mutate-mcp-"))
    work = tmp / "harness"
    shutil.copytree(HARNESS, work, symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__", "artifacts", "build"))
    rc, out, baseline_s = run(work)
    if out == "__TIMEOUT__":
        print(f"BASELINE TIMED OUT after {_SELFTEST_TIMEOUT}s — the unmutated selftest hung, "
              f"so nothing below would prove anything.")
        return 1
    if rc != 0:
        print("BASELINE FAILED — mutations prove nothing:")
        print(out[-3000:])
        return 1
    # The reference every per-mutation time below is read against; without it those numbers
    # describe the machine, not the mutation.
    print(f"baseline: SELFTEST PASSED in {baseline_s:.1f}s\n")

    caught = 0
    slowest = (0.0, None)
    for mid, rel, find, repl, arm in MUTATIONS:
        path = work / rel
        original = path.read_text()
        # `find`/`repl` may be tuples: some properties are now defended in two places, and
        # reintroducing the defect means removing both (see M53).
        edits = list(zip(find, repl)) if isinstance(find, tuple) else [(find, repl)]
        if any(f not in original for f, _ in edits):
            # No time to report, and "(0.0s)" would read as a suite that ran instantly rather
            # than one that never started — the same lie the None/[] distinction exists to
            # prevent elsewhere. Say which it is.
            print(f"{mid}: STALE ANCHOR — text not found in {rel} (selftest not run)")
            continue
        mutated = original
        for f, r in edits:
            mutated = mutated.replace(f, r, 1)
        path.write_text(mutated)
        rc, out, elapsed = run(work)
        path.write_text(original)
        took = f"({elapsed:.1f}s)"
        if elapsed > slowest[0]:
            slowest = (elapsed, mid)
        failed = re.findall(r"\[FAIL\]\s+([^:]+):", out)
        if out == "__TIMEOUT__":
            # Not a clean catch: the arm never got to report because the selftest hung. A
            # mutation whose defect is an infinite loop must be caught by an arm that BOUNDS
            # the work (a thread + join), not by the suite's own timeout — so this counts as
            # uncaught and fails the run, forcing a real fix rather than masking the hang.
            print(f"{mid}: *** TIMEOUT *** selftest exceeded {_SELFTEST_TIMEOUT}s — the "
                  f"defect hangs rather than reddening {arm} {took}")
        elif rc != 0 and arm in failed:
            print(f"{mid}: CAUGHT by {arm} {took}")
            caught += 1
        elif rc != 0:
            print(f"{mid}: failed, but NOT via {arm} -> {failed} {took}")
        else:
            print(f"{mid}: *** MISSED *** selftest still passes with the defect present {took}")
    print(f"\n{caught}/{len(MUTATIONS)} caught by the intended arm")
    total = time.monotonic() - started
    slow = f"slowest {slowest[1]} at {slowest[0]:.1f}s" if slowest[1] else "no mutation ran"
    print(f"elapsed: {total / 60:.1f} min total, baseline {baseline_s:.1f}s, {slow}")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if caught == len(MUTATIONS) else 1


if __name__ == "__main__":
    sys.exit(main())
