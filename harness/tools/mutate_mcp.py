#!/usr/bin/env python3
"""Mutation-test the declared-MCP and credential-containment arms.

Each mutation reintroduces a plausible version of the defect the arm exists to catch; the
named arm MUST go red. An arm that stays green while its defect is present is decorative.

Run it: `python3 harness/tools/mutate_mcp.py` (needs `harness/.venv`). It copies the tree to
a tempdir, checks the baseline passes, then applies one mutation at a time and re-runs the
suite that owns the mutated file. Exit 0 only when every mutation is caught BY ITS NAMED ARM
— "some arm failed" is not the same claim.

THREE SUITES, chosen by the file the mutation perturbs rather than declared per entry.
`agentskill_evals/` is proven by the selftest; `fixtures/` and `tools/` by
`tools/verify_mcp_fixtures.py`; and the proxy's I/O half plus the awkward server it is driven
against by `tools/verify_mcp_proxy.py`, those two named explicitly because each sits in a
directory another suite owns. Deriving the suite from the target rather than adding a sixth
field is the same argument `_classify` already makes about the id prefix: a fact that can be
stated independently will eventually be stated wrongly, and here the wrong suite means a
mutation reported as uncaught because the program that would have caught it never ran.

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

Every result line carries TWO clocks, and the difference between them is the point. Wall time
is what `_SUITE_TIMEOUT` bounds; CPU time is what the mutation actually spent. Read both
against the `baseline:` lines: a mutation taking several times its suite's baseline IN CPU is
the M65 shape — a defect that has turned some walk recursive and is burning a core — and it is
the only notice anyone gets before it grows past the timeout and reports as a hang. Wall time
alone stopped being able to say that the moment `--jobs` existed, because eight suites sharing
a machine all take longer without any of them being wrong.

`--jobs N` runs N mutations at once, each in its OWN copy of the tree, defaulting to 1. It is
worth having because this suite is mostly WAITING: the proxy arms spend their time on settles
and grace periods rather than on a core, which is why a serial run leaves the machine ~90%
idle. What parallelism must not do is change a verdict, so everything that decides one stays
where it was — the anchor and arm guards run serially up front, each worker mutates only its
own tree, and results are printed in list order however they finish. The one thing that DID
have to change is in the proxy verifier: its survivor check was scoped by pid identity, which
excludes a leak from a previous mutation but not a live guardian from a concurrent worker, so
`mcp_proxy_io.is_guardian_command` now scopes by tree as well.
"""
import concurrent.futures
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import NamedTuple

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
HTTPFIX = "fixtures/http_mcp_server.py"
# The opt-in live probe. A target like any other despite never running in the block: its
# startup path is driven offline by `verify_mcp_fixtures.py` §E18, precisely because "nothing
# routine runs it" is what let a fix land in one copy and not this one.
PROBE1 = "tools/probe_remote_mcp.py"
# The three copilot probes, same category and same reason: opt-in, never run by the block, and
# about to have an adapter decision rest on the word they print. §E19 drives their classifiers.
SESSPROBE = "tools/probe_session_mcp.py"
CCONFIG = "tools/probe_copilot_config.py"
CGATE = "tools/probe_copilot_gating.py"
CGATE_REMOTE = "tools/probe_copilot_remote_gating.py"
# Phase 2 slice 1's fourth copilot probe: the one that reads copilot's OWN account of the run
# rather than server-side receipts. Slice 2's witness change and slice 4's parser are both
# about to be built on the words it prints, which is the same weight the three above carry.
CEVENTS = "tools/probe_copilot_events.py"
# The proxy's I/O half and the awkward server it is driven against. PRODUCTION code that no
# selftest arm can reach — it is only executed by running the real program over real pipes —
# so it is `M*` like any other production target, proven by a THIRD suite. The classification
# and the suite are different questions, and conflating them is what the split below fixes:
# `M`/`I`/`F` says what a mutation perturbs, `_suite_for` says who would notice.
PROXY_IO = "agentskill_evals/mcp_proxy_io.py"
TARGET = "fixtures/proxy_target_server.py"
# Not targets: a mutation aimed at a verifier asks that verifier whether it notices being
# broken, and the mutated program and the judging program are then the same one.
VERIFIER = "tools/verify_mcp_fixtures.py"
PROXY_VERIFIER = "tools/verify_mcp_proxy.py"
# THIS FILE IS A TARGET, which it was not until `verify_mcp_fixtures.py` §E17 began driving the
# readers below from a different program. See `_classify` for where the line falls: what §E17
# drives is fair game, and this runner's own scoring is not.
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
    # Claiming a CLI-NATIVE filter, which claude has never had. Re-anchored and re-armed by
    # the C3 adapter integration: the declared value moved from `"unbuilt"` to `"proxy"`, so
    # this entry's find-text went stale and its arm named a check that no longer exists —
    # uncaught on both counts, and reported only as a skip. M318 is the sibling that goes the
    # other way, back to a value that refuses.
    ("M4-claude-claims-native-tool-filter", CLAUDE,
     '    mcp_tool_filter = "proxy"',
     '    mcp_tool_filter = "native"',
     "mcp.claude_gates_tools_through_the_harness_proxy"),
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
    # The variable name is the disambiguator, and it is load-bearing rather than incidental:
    # the C3 adapter integration added a SECOND file created exactly this way — the proxy's
    # own config, which now carries the credential this one used to. Two identical lines, and
    # this anchor silently began matching both (caught by the ambiguity guard, not by review).
    # M323 is the other one.
    ("M9-config-world-readable", CLAUDE,
     "fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)",
     "fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)",
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
    # The regression that shipped for real. copilot 1.0.75 moved the agent frontmatter
    # schema out of app.js into schemas/ and the SDK typings; a scan of app.js alone then
    # reported `mcp-servers` MISSING — the wording reserved for a channel that was
    # REMOVED — and nobody noticed for three releases. Deleting the walk restores exactly
    # that: the bundle's sibling files stop being read.
    ("M344-marker-scan-narrows-back-to-one-file", COPILOT,
     "\n    for dirpath, dirnames, filenames in os.walk(root, onerror=_on_walk_error):",
     "\n    for dirpath, dirnames, filenames in []:",
     "copilot.channel_markers_scan_the_bundle"),
    # The opposite failure, and the one a widening invites: the scan reads everything,
    # including the wasm blobs and prebuilt binaries it cannot honestly claim to have
    # examined, so `searched` overstates what was ruled out.
    ("M345-the-scan-skips-what-is-not-a-text-file-again", COPILOT,
     "\n            (text_files if name.endswith(_TEXT_FIRST_SUFFIXES) else other_files).append(entry)",
     "\n            text_files.append(entry) if name.endswith(_TEXT_FIRST_SUFFIXES) else None",
     "copilot.channel_markers_scan_the_bundle"),
    # The command claims complete coverage over a scan that stopped early — 189 of 240
    # files on the real 1.0.79 (external review, second round).
    ("M353-the-command-claims-coverage-it-did-not-have", "agentskill_evals/cli.py",
     "\n        if audit.scanned_everything:",
     "\n        if True:",
     "copilot.channel_markers_scan_the_bundle"),
    # ...and the denominator that claim is measured against is never collected, so
    # `scanned_everything` is true of every scan including the ones that stopped early.
    ("M354-eligible-files-are-never-counted", COPILOT,
     "\n        eligible=len(ordered), unenumerated=tuple(walk_failures))",
     "\n        eligible=0, unenumerated=tuple(walk_failures))",
     "copilot.channel_markers_scan_the_bundle"),
    # THE CONTRADICTION, restored verbatim: the coverage line makes a marker claim, which
    # the marker line below also makes, and with 10 of 11 markers beside an unlistable
    # directory the command printed both "Every marker was found" and "1 marker(s) were
    # not found in the rest" (external review, fourth round).
    ("M358-the-coverage-line-states-the-marker-result-too", "agentskill_evals/cli.py",
     '\n                  f"bundle exists is unknown and this run supports no claim about coverage")',
     '\n                  f"bundle exists is unknown. Every marker was found, which no unread "\n                  f"path can retract — but this run supports no claim about coverage")',
     "copilot.channel_markers_scan_the_bundle"),
    # ...and the branch that refuses to quantify coverage over an unenumerated bundle is
    # skipped, so one of the two quantified lines is printed instead.
    ("M359-an-unlistable-bundle-gets-a-quantified-coverage-line", "agentskill_evals/cli.py",
     "\n        elif audit.unenumerated:",
     "\n        elif False:",
     "copilot.channel_markers_scan_the_bundle"),
    # A failed traversal counted as exactly ONE eligible file, so `searched + unreadable
    # >= eligible` came out true over a subtree nobody enumerated and the command printed
    # a complete-coverage line (external review, third round).
    ("M355-an-unlistable-directory-counts-as-one-file", COPILOT,
     "\n        eligible=len(ordered), unenumerated=tuple(walk_failures))",
     "\n        eligible=len(ordered) + len(walk_failures), unenumerated=tuple(walk_failures))",
     "copilot.channel_markers_scan_the_bundle"),
    # ...and the guard that makes coverage UNANSWERABLE rather than false when a directory
    # would not list: without it the denominator is compared against anyway.
    ("M356-an-unlistable-directory-still-permits-a-coverage-claim", COPILOT,
     "\n        if self.unenumerated:\n            return False",
     "\n        if False:\n            return False",
     "copilot.channel_markers_scan_the_bundle"),
    # ...and the two kinds of ignorance are folded back together: an unreadable FILE is one
    # known unit, an unlistable DIRECTORY is an unknown quantity, and counting them alike
    # is what made the denominator wrong in the first place.
    ("M357-a-directory-failure-is-recorded-as-a-file-failure", COPILOT,
     "\n        searched=tuple(searched), unreadable=tuple(unreadable),",
     "\n        searched=tuple(searched), unreadable=tuple(unreadable) + tuple(walk_failures),",
     "copilot.channel_markers_scan_the_bundle"),
    # A directory that cannot be listed vanishes silently, so a marker inside it reads as
    # absent with nothing recording the hole (external review).
    ("M352-a-denied-directory-leaves-no-trace", COPILOT,
     "\n    for dirpath, dirnames, filenames in os.walk(root, onerror=_on_walk_error):",
     "\n    for dirpath, dirnames, filenames in os.walk(root):",
     "copilot.channel_markers_scan_the_bundle"),
    # A file that could not be opened counts as searched, so an unreadable bundle reports
    # "absent" about files nothing ever read — the audit's own version of the defect it
    # exists to catch, one level down.
    ("M346-unread-file-still-licenses-the-word-searched", COPILOT,
     "\n    except OSError:\n        return False",
     "\n    except OSError:\n        return True",
     "copilot.channel_markers_scan_the_bundle"),
    # A scan that read nothing goes back to reporting every marker absent, which is
    # bit-for-bit what a build that dropped every channel produces.
    ("M347-blind-scan-reports-every-channel-vanished", COPILOT,
     "\n        if not self.searched:\n            return MARKER_NOT_SEARCHED",
     "\n        if False:\n            return MARKER_NOT_SEARCHED",
     "copilot.channel_markers_scan_the_bundle"),
    # PARTIAL blindness reports as a confident finding about the build — the defect the
    # first cut of this fix shipped with, one file short of the empty case M347 covers.
    ("M348-a-blind-spot-still-licenses-a-confident-missing", COPILOT,
     "\n        if blind:\n            return MARKER_INCOMPLETE",
     "\n        if False:\n            return MARKER_INCOMPLETE",
     "copilot.channel_markers_scan_the_bundle"),
    # The file that would not open is dropped instead of recorded, so nothing downstream
    # can know the scan had a hole in it.
    ("M349-an-unreadable-file-leaves-no-trace", COPILOT,
     "\n        else:\n            unreadable.append(rel)",
     "\n        else:\n            pass",
     "copilot.channel_markers_scan_the_bundle"),
    # THE READ OVERLAP, which had no mutation at all until the straddle arm was given a
    # directory of its own: while it shared one with bundles holding every marker, the
    # assertion passed with the overlap deleted (review).
    ("M350-the-chunk-overlap-is-dropped", COPILOT,
     "\n            tail = buf[-longest:]",
     "\n            tail = b\"\"",
     "copilot.channel_bundle_audit"),
    # One file, two spellings: the biggest file in the bundle is scanned twice. Aimed at
    # the WALK side, which is where the two spellings meet — normalising only the `app_js`
    # side is a no-op whenever it is already normalised, which it is for the bare relative
    # path the case is about, so a mutation there changes nothing and reports MISSED.
    ("M351-a-relative-app-js-is-scanned-twice", COPILOT,
     "\n            norm = os.path.normpath(full)",
     "\n            norm = full",
     "copilot.channel_markers_scan_the_bundle"),
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
    # THE BOUNDED JOIN THAT KEEPS M37 A FINDING RATHER THAN A HANG. The relocation fixture is
    # a FIFO — chosen so the arm needs no socket privileges — and M37 makes the scrub `open()`
    # it, which never returns. On the main thread that wedges the whole suite with no output.
    # Legitimate as an `I*` because the bound is a feature of the SELFTEST, unreachable by any
    # production edit: shorten it and the arm must notice it did not finish.
    # SHORTENING the bound is NOT the mutation: the cell finishes in well under 10ms on a clean
    # tree, so `join(0.01)` is behaviourally identical and came back MISSED — the suite catching
    # a mutation that could not fail, which is the same defect as a check that cannot fail.
    # REMOVING it is discriminating: the main thread reaches the assertion while the cell is
    # still running, so `not t15.is_alive()` goes false. Confirmed by hand, three runs of three.
    ("I3-the-relocation-cell-is-not-bounded", SELFTEST,
     "            t15.join(20.0)",
     "            pass",
     "relocate.scrub_verdict_survives_a_raise_that_rebuilds_the_result"),
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
    # THE LOOSENING THAT WAS PROPOSED AND REJECTED (PR #108). Licensing off the trigger LIST
    # accepts a blank behind any runner-up, which is only sound while no arrangement can put a
    # pre-phase trigger behind an ending that did enter the phase — a premise the reader would
    # then be resting on without checking.
    ("M334-the-licence-reads-the-trigger-list-instead-of-the-latch", AUDIT,
     "                and record.latch not in _NOT_APPLICABLE_LICENSED_BY[key]):",
     ("                and not ({t.reason for t in record.triggers}\n"
      "                         & _NOT_APPLICABLE_LICENSED_BY[key])):"),
     "audit.the_licence_reads_the_latch_and_not_the_trigger_list"),
    # THE INSTRUMENT'S OWN LEAK, reintroduced. Sleeping the ceiling instead of waiting on the
    # lifeline is what the first `mute` did, and it left a guardian running with `PPID 1` after
    # the verifier printed ALL PASS — found by review, not by the suite, because nothing looked.
    ("M337-the-mute-guardian-sleeps-instead-of-watching-its-lifeline", PROXY_IO,
     ("            deadline = time.monotonic() + _MUTE_CEILING\n"
      "            while time.monotonic() < deadline:\n"
      "                if _readable(self.lifeline, GUARDIAN_POLL):\n"
      "                    break"),
     "            time.sleep(_MUTE_CEILING)",
     "the `mute` guardian goes when its proxy goes, rather than sleeping out its ceiling"),
    # THE RECOGNISER THE SURVIVOR CHECK READS ITS ABSENCES THROUGH, broken both ways. It is one
    # predicate answering two questions — is this a guardian rather than the proxy that spawned
    # it, and is it OURS rather than a concurrent `--jobs` worker's — so each term needs its own
    # entry. Dropping the tree makes it match every worker's guardian, which is the survivor
    # check failing for another worker's reason; dropping the flag makes it match the proxy too,
    # which counts a live parent as a leaked child. Both are asked of synthetic argv rather than
    # of the machine, because a serial verifier has no second tree to be wrong about.
    ("M341-the-guardian-recogniser-ignores-which-tree-launched-it", PROXY_IO,
     "    return GUARDIAN_FLAG in command and os.path.abspath(__file__) in command",
     '    return GUARDIAN_FLAG in command and "mcp_proxy_io" in command',
     ("...and one launched from another copy of the tree is NOT, which is what makes a "
      "parallel run's survivor check scoped to its own worker")),
    ("M342-every-process-naming-this-file-is-a-guardian", PROXY_IO,
     "\n    return GUARDIAN_FLAG in command and os.path.abspath(__file__) in command",
     "\n    return os.path.abspath(__file__) in command",
     "...and the proxy itself is not, though its argv names this same file"),
    # AND THE ARGV THE RECOGNISER IS PINNED TO. The two exist so a live guardian's command line
    # and the string looked for in `ps` cannot drift apart; a launcher that spells the path
    # differently breaks that pinning without either function looking wrong on its own. What
    # notices is the POSITIVE CONTROL — the observer finding the `mute` guardian while the case
    # holds it alive — which is the check that stops every absence below being read as proof.
    ("M343-the-guardian-is-launched-under-a-name-nothing-matches", PROXY_IO,
     "    return [program, os.path.abspath(__file__), GUARDIAN_FLAG, str(order_fd)]",
     "    return [program, os.path.relpath(__file__), GUARDIAN_FLAG, str(order_fd)]",
     "the survivor observer finds the `mute` guardian while the case is holding it alive"),
    # NO MUTATION FOR "spawn_failed IS FIRST", and the reason is worth more than a mutation.
    # The obvious one — drain the wakeup pipe before `_spawn()` — was written, driven, and came
    # back MISSED, correctly: there is no window between `signal.signal()` and `_spawn()` in
    # which a signal can arrive, so an earlier drain finds an empty pipe and changes nothing.
    # Displacing `spawn_failed` would require latching a signal from INSIDE `_spawn()`, where
    # the wakeup fd is a local of `run()` and not in scope. The first position is therefore
    # structural at a level this file cannot perturb, which is a stronger guarantee than a
    # caught mutation and a weaker one than it looks: it holds because of a scope boundary, so
    # a refactor that threads the fd into `_spawn()` would end it silently. That is what the
    # writer check in `verify_mcp_proxy.py` stands guard over, mutation or no mutation.
    #
    # What IS producible is the other half: dropping the post-teardown drain loses a signal the
    # log is written to carry. The pair matters — an arm asserting only "spawn_failed is first"
    # is equally satisfied by a proxy that never records a signal at all.
    ("M336-a-signal-arriving-during-teardown-is-dropped", PROXY_IO,
     "            self._on_signal(wake_r)\n        finally:",
     "            pass\n        finally:",
     "...and the signal IS recorded behind it, rather than being lost"),
    # THE WITNESS ITSELF, removed. The nonce marker is what says the case's window was entered,
    # and while its absence was a printed NOTE the run carried on and the case was scored over a
    # window that never opened. This is the mutation that would have reported MISSED then: it
    # takes the marker away and nothing else, so what catches it is the witness being an
    # assertion rather than a remark (review, PR #109).
    ("M338-the-phase-marker-is-never-written", PROXY_IO,
     "            self._phase(f\"mute-waiting {os.environ.get(PHASE_NONCE_ENV, '')}\")\n",
     "",
     "the awaited phase marker appears, so the case below measures its own window"),
    # THE DEFECT THIS BRANCH SHIPPED: a fifth control var was declared and the strip site kept
    # naming four, so `ASE_MCP_PHASE_NONCE` was handed to the declared server. Removing it from
    # the set is that state exactly, and the check reads the set rather than a copy of it, so
    # the mutation is caught by the same clause a sixth var would be covered by.
    ("M339-a-control-var-is-left-out-of-the-strip-set", PROXY_IO,
     "CONTROL_ENV = (FAULT_ENV, INHERIT_ENV, GRACE_ENV, GUARDIAN_ENV, PHASE_NONCE_ENV)",
     "CONTROL_ENV = (FAULT_ENV, INHERIT_ENV, GRACE_ENV, GUARDIAN_ENV)",
     "...and no variable the proxy reads for itself is in it"),
    # ...and the site as well as the declaration. M339 perturbs WHAT is declared a control var
    # and this perturbs whether the declaration is acted on, which are two different edits a
    # refactor can make. The pair is what says the tuple and the loop are load-bearing together.
    ("M340-the-child-inherits-the-proxys-control-vars", PROXY_IO,
     "        for key in CONTROL_ENV:\n            env.pop(key, None)\n",
     "",
     "...and no variable the proxy reads for itself is in it"),
    ("M293-a-drop-reason-may-be-any-string", AUDIT,
     "            if not isinstance(reason, str) or reason not in DROP_REASONS:",
     "            if not isinstance(reason, str) or not reason:",
     "audit_log.every_malformed_line_is_a_code_rather_than_an_exception"),

    # ---- absent and empty are two facts, not one ----------------------------------------
    # THE DEFECT AS SHIPPED: `tools: []` is a state the schema admits, and the proxy refused to
    # start on it — so the documented configuration passed every preflight and died at launch.
    ("M332-the-empty-allowlist-is-refused-again", PROXY_IO,
     "    if not all(isinstance(t, str) and t for t in tools):",
     "    if not tools or not all(isinstance(t, str) and t for t in tools):",
     "an empty allowlist is a filter that admits nothing, not a config the proxy rejects"),
    # ...and the same conflation in the direction that matters more: an ABSENT key becomes an
    # empty allowlist, so a config that never named a filter starts a proxy that reports one.
    ("M333-a-missing-allowlist-becomes-an-empty-one", PROXY_IO,
     '    tools = _require(raw, "tools", list, "the declared `tools:` allowlist")',
     '    tools = raw.get("tools") or []',
     "an ABSENT `tools` key is still refused, and the two are not the same fact"),

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

    ("F9-remote-header-is-dropped-before-it-is-recorded", HTTPFIX,
     '                       headers={k.lower(): v for k, v in self.headers.items()})',
     '                       headers={})',
     "a declared header ARRIVES, with its value intact \u2014 \u00a79 probe #1's real question"),
    # The witness that says nothing at startup: its later silence stops being readable, which
    # is the defect the startup row exists to prevent rather than a cosmetic one.
    ("F10-the-receipts-witness-never-announces-itself", HTTPFIX,
     '    RECEIPTS.write("listening", port=actual,',
     '    _skip = (lambda *a, **k: None)("listening", port=actual,',
     "...and the receipts witness records its own startup, so silence is readable"),
    # SSE answered in place: the reply rides the POST's own response, which satisfies a client
    # that never implemented the transport and fails the one that did.
    ("F11-the-legacy-transport-answers-in-place", HTTPFIX,
     ("            if reply is not None:\n                stream.send(_sse_frame(reply))\n"
      "            self._empty(202)"),
     "            self._json(200, reply)",
     "...a POST there is answered 202, with the reply NOT in its response"),
    # The endpoint event is what tells the client where to POST; without it the client has
    # nowhere to send and the failure looks like a server that is simply not answering.
    ("F12-the-sse-stream-never-names-its-endpoint", HTTPFIX,
     '        stream.send(f"event: endpoint\\ndata: {PATH_MESSAGES}?sessionId={session}\\n\\n".encode())',
     '        stream.send(b"")',
     "the SSE stream's FIRST event names the endpoint to POST to"),
    # A reply with nowhere to go, accepted anyway.
    ("F13-a-post-for-an-unopened-session-is-accepted", HTTPFIX,
     "            if stream is None:\n                self._empty(404)\n                return",
     "            if stream is None:\n                self._empty(202)\n                return",
     "...while a POST for an unopened session is 404, not a silently stranded 202"),
    # The tools stop being the stdio fixture's tools, which is the whole reason this fixture
    # imports rather than restates them.
    ("F14-the-http-fixture-serves-its-own-tool-list", HTTPFIX,
     '        return echo.result_envelope(req_id, {"tools": echo.TOOLS},',
     '        return echo.result_envelope(req_id, {"tools": []},',
     "...and serves the SAME tools as the stdio fixture, because it imports them"),

    # ---- the transport-level MUSTs and the endpoint's identity (review, PR #106) --------
    ("F15-origin-is-not-validated-on-POST", HTTPFIX,
     "        if not self._origin_ok():\n            return 403\n",
     "",
     "a cross-origin POST is refused 403 \u2014 the transport's DNS-rebinding MUST"),
    ("F16-origin-is-not-validated-on-GET", HTTPFIX,
     ("        if not self._origin_ok():\n"
      "            self._refuse(403)                # a GET carries no body, but it ends the "
      "same way\n            return\n"),
     "",
     "...and a cross-origin GET on the SSE endpoint is refused too, not just POST"),
    # Deny-everything scores full marks on both checks above unless something asserts the
    # ordinary request still works.
    ("F17-every-origin-is-refused-including-none", HTTPFIX,
     "        return origin is None or origin in ALLOWED_ORIGINS",
     "        return False",
     "...while a request with no Origin at all is served, as a non-browser client"),
    ("F18-any-path-is-the-streamable-endpoint", HTTPFIX,
     "        if path not in (PATH_STREAMABLE, PATH_MESSAGES):\n            return 404",
     "        if False:\n            return 404",
     "a POST to a path that is not the endpoint is 404, not quietly served"),
    ("F19-the-endpoint-is-matched-by-prefix", HTTPFIX,
     "        if path not in (PATH_STREAMABLE, PATH_MESSAGES):",
     "        if path.startswith((PATH_STREAMABLE, PATH_MESSAGES)) is False:",
     "...and a prefix of the endpoint is not the endpoint either"),
    ("F20-a-session-id-is-minted-that-nothing-honours", HTTPFIX,
     "        self._json(200, reply)\n\n    def do_GET",
     "        self._json(200, reply, {\"Mcp-Session-Id\": uuid.uuid4().hex})\n\n    def do_GET",
     "...and advertises NO session id, because it keeps no session state"),
    ("F21-the-sse-path-is-matched-by-prefix", HTTPFIX,
     '        if self.path.split("?", 1)[0] != PATH_SSE:',
     "        if not self.path.startswith(PATH_SSE):",
     "...and a prefix of the SSE path is not the SSE path"),

    # ---- C3 adapter integration: the harness's filter is on the wire or it is nowhere ------
    # `tools:` goes back to being refused. The validator's message would even look right, which
    # is why the arm reads the FILTER VALUE and not just the absence of an error.
    ("M318-claude-goes-back-to-refusing-tools-it-can-now-enforce", CLAUDE,
     '    mcp_tool_filter = "proxy"',
     '    mcp_tool_filter = "unbuilt"',
     "mcp.claude_gates_tools_through_the_harness_proxy"),
    # The enforcing set forgets the value that reaches it, which refuses every gated server
    # while every adapter still declares a filter it believes in.
    ("M319-the-enforcing-set-drops-the-proxy", BASE,
     'ENFORCING_TOOL_FILTERS = frozenset({"native", "complement", "proxy"})',
     'ENFORCING_TOOL_FILTERS = frozenset({"native", "complement"})',
     "mcp.claude_gates_tools_through_the_harness_proxy"),
    # ...and the other direction: a set that admits everything accepts an allowlist nothing
    # applies, which is the exact degradation the refusal exists for.
    ("M320-every-filter-value-counts-as-enforcement", BASE,
     "            if mechanism in self.ENFORCING_TOOL_FILTERS:\n                continue",
     "            if True:\n                continue",
     "mcp.a_tool_filter_outside_the_enforcing_set_still_refuses"),
    # THE SUBSTITUTION ITSELF: the gated server is handed to claude directly, so the CLI talks
    # to the real server and the allowlist is a comment.
    ("M321-a-gated-server-is-handed-to-the-cli-unproxied", CLAUDE,
     "            if s.tools is not None:",
     "            if False:",
     "mcp.a_gated_server_is_replaced_by_the_proxy_in_the_cli_config"),
    # The credential is written into the file the CLI reads as well as the proxy's own.
    ("M322-the-gated-credential-is-also-left-in-the-cli-config", CLAUDE,
     "                entry = self._write_proxy_config(name, s, opts.mcp_scratch_dir)",
     ("                entry = self._write_proxy_config(name, s, opts.mcp_scratch_dir)\n"
      "                entry[\"env\"] = dict(s.env or {})"),
     "mcp.a_gated_servers_credential_leaves_the_cli_facing_config"),
    # The proxy's config is world-readable for the window it exists, which is the same
    # exposure `mcp.json` is created 0600 to avoid — and it now holds the credential.
    # `cfg_path` rather than `path` is what separates this from M9 — see the note there.
    ("M323-the-proxy-config-is-world-readable", CLAUDE,
     "fd = os.open(cfg_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)",
     "fd = os.open(cfg_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)",
     "mcp.the_proxy_config_is_not_world_readable"),
    # The allowlist never reaches the proxy, which then has no filter to apply.
    ("M324-the-allowlist-is-not-passed-to-the-proxy", CLAUDE,
     '            "tools": sorted(s.tools),',
     '            "tools": ["*"],',
     "mcp.the_proxy_config_carries_the_allowlist_and_its_audit_path"),
    # WRITER AND READER DISAGREE ABOUT THE PATH, which is why it is one function: the log gets
    # written, the verdict reads elsewhere, and `no_instances` fails a run that was fine.
    #
    # It has to INLINE the path on one side. Mutating `audit_log_path` itself is not a defect
    # this design admits — both sides call it, so both move together and the agreement holds;
    # the first version of this entry did that and was MISSED for the right reason. The defect
    # the shared function prevents is someone spelling the path out at one of the two sites.
    ("M325-the-writer-spells-the-audit-path-out-instead-of-sharing-it", CLAUDE,
     '            "audit_log": self.audit_log_path(scratch_dir, name),',
     '            "audit_log": os.path.join(scratch_dir, "audit.jsonl"),',
     "mcp.the_proxy_config_carries_the_allowlist_and_its_audit_path"),
    # FAIL-OPEN on the case that matters most: no log at all reads as nothing to complain
    # about, so a gated server whose proxy never ran passes.
    ("M326-a-missing-audit-log-is-excused", CLAUDE,
     '                text = ""          # judged as `no_instances` below, not excused',
     "                return None",
     "mcp.a_gated_server_with_no_audit_log_fails_the_cell"),
    # The verdict is computed and discarded — the arms that call `gating_failure` directly all
    # stay green, and only the one going through `verify_post_run` notices.
    ("M327-the-gating-verdict-is-computed-and-not-raised", CLAUDE,
     ("        gating = self.gating_failure(opts)\n        if gating:\n"
      "            raise RuntimeError(gating)"),
     ("        gating = self.gating_failure(opts)\n        if False:\n"
      "            raise RuntimeError(gating)"),
     "mcp.verify_post_run_actually_reaches_the_gating_verdict"),
    # The name is trusted into a path. Harmless while the schema forbids separators, which is
    # exactly the premise the check exists to keep from becoming load-bearing silently.
    ("M328-a-server-name-is-trusted-into-a-filename", CLAUDE,
     "        if not _NAME_RE.match(name):",
     "        if False:",
     "mcp.a_server_name_that_could_escape_the_scratch_dir_is_refused"),
    # THE FILTER ANSWERS FOR THE ADAPTER AGAIN, which is the state this branch was added to
    # leave: `proxy` for every gated server, including one the proxy has no way to reach.
    ("M329-the-filter-claim-ignores-the-transport", CLAUDE,
     '        if not server.is_stdio:\n            return "unbuilt", (',
     '        if False:\n            return "unbuilt", (',
     "mcp.a_remote_server_cannot_be_gated_by_a_proxy_that_speaks_stdio"),
    # ...and the caller asks the class attribute instead of the server, which is the same defect
    # one level up: correct only while every declared server has the same answer.
    ("M330-the-gating-question-is-asked-once-for-the-whole-mapping", BASE,
     "            mechanism, why = self.tool_filter_for(server)",
     "            mechanism, why = self.mcp_tool_filter, None",
     "mcp.the_gating_question_is_asked_per_server_not_per_scenario"),
    # The writer trusts that a validator ran. It produces `"command": null` and drops the url
    # and headers the server was actually declared with.
    ("M331-the-proxy-config-writer-forgets-the-transport", CLAUDE,
     "        if not s.is_stdio:\n            raise RuntimeError(",
     "        if False:\n            raise RuntimeError(",
     "mcp.the_proxy_config_writer_refuses_what_it_cannot_proxy"),

    # ---- the MUST the Origin argument already covered, and the witness of the call ---------
    ("F22-the-protocol-version-header-is-never-validated", HTTPFIX,
     "        if path == PATH_STREAMABLE and not self._version_ok():\n            return 400",
     "        if False:\n            return 400",
     "an unsupported MCP-Protocol-Version is refused 400 — the binding's other MUST"),
    # Deny-everything again: it scores full marks on F22's check, and only a control driving a
    # version the server really supports can tell the two apart.
    ("F23-every-protocol-version-is-refused-including-none", HTTPFIX,
     "        return claimed is None or claimed in SUPPORTED_VERSIONS",
     "        return False",
     ("...while EVERY version `initialize` can negotiate is served, so the set it is checked "
      "against cannot narrow away from the set it is chosen from")),
    # The drift the import exists to prevent: a set narrower than the one `_initialize` selects
    # from lets this server 400 a version it negotiated itself.
    ("F24-the-supported-set-drifts-from-the-negotiated-one", HTTPFIX,
     "SUPPORTED_VERSIONS = echo.LEGACY_VERSIONS",
     "SUPPORTED_VERSIONS = echo.LEGACY_VERSIONS[:1]",
     ("...while EVERY version `initialize` can negotiate is served, so the set it is checked "
      "against cannot narrow away from the set it is chosen from")),
    # The witness stops recording the call, so nothing but the model's own final text says a
    # tool ran — which is the assertion the live probe was found to be making (PR #106).
    ("F25-the-call-itself-is-never-recorded", HTTPFIX,
     ('    RECEIPTS.write("rpc", method=method, id=req_id,\n'
      '                   tool=params.get("name") if method == "tools/call" else None)'),
     "    pass",
     ("the witness records the CALL ITSELF, so 'the tool ran' and 'the model repeated its "
      "prompt' stop looking alike")),
    # A tool name on every message: it satisfies the check above while identifying nothing.
    ("F26-every-message-is-recorded-as-a-tool-call", HTTPFIX,
     '                   tool=params.get("name") if method == "tools/call" else None)',
     '                   tool="echo")',
     ("...and a message that is NOT a tool call records no tool name, so the field tells them "
      "apart")),

    # ---- the runner's own readers, which nothing but a full run used to execute -----------
    # THE REGRESSION THAT SHIPPED, pinned. `_SUITES` grew a third field, `run()` kept unpacking
    # two, and `mutate_mcp.py` could not start either verifier suite — for a whole push, with
    # no check able to say so (review, PR #106).
    # ANCHORED ACROSS LINES, and that is forced rather than stylistic — a hazard particular to
    # mutating THIS file. An anchor of a single unescaped line appears twice here: once in the
    # code, once as the `find` string quoting it a hundred lines up. The ambiguous-anchor guard
    # caught it on the first run. Any anchor spanning a newline is written with a `\n` escape,
    # so the literal's source bytes differ from the code's and the match is unique again.
    ("F27-the-suite-record-is-read-positionally-again", SELF,
     '    return [str(cwd / ".venv/bin/python"), *_SUITES[suite].argv]\n\n\n_TIMEOUT_OUTPUT',
     ('    argv, _ = _SUITES[suite]\n'
      '    return [str(cwd / ".venv/bin/python"), *argv]\n\n\n_TIMEOUT_OUTPUT'),
     "every declared suite yields a runnable command, which is the line that broke"),
    # A suite that prints a line per check but declares no parser for them: the arm guard skips
    # it silently, which is the disarm rather than a failure.
    ("F28-a-label-printing-suite-declares-no-label-parser", SELF,
     '    "fixtures": _Suite((VERIFIER,), r"^\\s*FAIL\\s+(.+?)\\s\\s<- ", _ALL_LABELS),',
     '    "fixtures": _Suite((VERIFIER,), r"^\\s*FAIL\\s+(.+?)\\s\\s<- "),',
     ("...and the two suites that print a line per check both declare one, since a `None` "
      "there disarms the arm guard without saying so")),
    # A label parser matching every line makes the empty parse unreachable, and the guard's
    # fail-closed branch becomes unreachable code that reads like a safeguard.
    ("F29-the-label-parser-matches-lines-that-are-not-checks", SELF,
     '_ALL_LABELS = r"^\\s*(?:ok|FAIL)\\s+(.+?)(?:\\s\\s<- .*)?$"',
     '_ALL_LABELS = r"^(.+)$"',
     ("...and finds nothing in output that is not a check, so the empty parse it now refuses "
      "on is a state that can really occur")),
    # THE REPORTED DEFECT ITSELF, in the copy it was reported in: parse in the `return`, and a
    # first line that is not JSON raises out with the child alive and the caller holding None.
    ("F30-the-probe-parses-the-announcement-undefended-again", PROBE1,
     ("    try:\n        info = json.loads(line)\n    except ValueError:\n"
      "        return reaped("),
     "    info = json.loads(line)\n    if False:\n        return reaped(",
     "a fixture that says something that is not JSON is a NAMED failure, not a traceback"),
    # The header row stops naming the message it carried, so the per-message header questions
    # collapse back into one question about the run — which is how "every post-handshake
    # request declares a version" degraded into "at least one did" (review, PR #106).
    ("F31-the-header-row-forgets-which-message-it-carried", HTTPFIX,
     "                       rpc=msg.get(\"method\") if isinstance(msg, dict) else None,",
     "                       rpc=None,",
     "a request row names the message it carried, on the same row as its headers"),
    # ...or names the same message every time, which satisfies the check above while
    # identifying nothing — the F26 shape, one row over.
    ("F32-every-request-row-names-the-same-message", HTTPFIX,
     ("                       rpc=msg.get(\"method\") if isinstance(msg, dict) else None,\n"
      "                       # EVERY header"),
     '                       rpc="initialize",\n                       # EVERY header',
     ("...and a request carrying a DIFFERENT method records that one, so the field is not the "
      "same word every time")),

    # ---- the ORDER of the refusal, which every body-sending check is blind to -------------
    # THE REPORTED REGRESSION, exactly: read the body, then decide. Accepted requests behave
    # identically — which is why the three Origin checks that send bodies all stayed green over
    # it — and a refused cross-origin caller gets to name the read (review, PR #106).
    ("F33-origin-is-validated-only-after-the-body-is-read", HTTPFIX,
     ('        refusal = self._refusal_for(path)\n        if refusal is not None:\n'
      '            # Recorded anyway, with no message, because "did the credential arrive" '
      'must stay\n'
      "            # answerable for a request that was REFUSED — a token sent to a rejected "
      "origin\n            # still left the client.\n"
      "            self._record(None)\n            self._refuse(refusal)\n            return\n"
      "        msg = self._body()\n        self._record(msg)"),
     ("        msg = self._body()\n        refusal = self._refusal_for(path)\n"
      "        if refusal is not None:\n            self._record(msg)\n"
      "            self._refuse(refusal)\n            return\n        self._record(msg)"),
     ("a cross-origin POST is refused WITHOUT its body — 403 arrives though the declared 50MB "
      "never does")),
    # Refused, promptly, and then left open — so the undelivered body desynchronizes the
    # connection and whatever arrives next is read as a request.
    ("F34-a-refusal-leaves-the-connection-open", HTTPFIX,
     '        self.send_header("Connection", "close")\n',
     "",
     "...and the connection is CLOSED, since an unread body has desynchronized it"),
    # The refusal stops being recorded, so the credential question goes unanswered for exactly
    # the requests most worth asking it about.
    ("F35-a-refused-request-is-not-recorded", HTTPFIX,
     "            self._record(None)\n            self._refuse(refusal)",
     "            self._refuse(refusal)",
     ("...while the refused POST is still RECORDED, so a credential sent to a rejected origin "
      "is not invisible")),
    # THE POSITIVE CONTROL'S OWN CONTROL. `env_seen: []` is what the child reports when the
    # strip works, when the environment never arrived, and when this reporter is broken — so
    # the case asserts a variable it must see, and this is the mutation proving that clause can
    # fail. Without it the leak check passes against a fixture that answers nothing.
    # ---- the copilot probes, whose verdict an adapter decision is about to rest on --------
    # THE READER READS THE OTHER FIXTURE'S SPELLING. The echo server writes `kind="request"`
    # with the JSON-RPC method; the HTTP server writes `kind="rpc"` for that and uses
    # `kind="request"` for the HTTP verb. A probe holding the wrong one finds nothing, forever,
    # and reports a perfect filter — which is why §E19 pins each reader to rows its own fixture
    # wrote rather than to a dict typed next to the assertion.
    ("F37-the-stdio-probe-reads-the-http-fixtures-spelling", CGATE,
     '    return any(r.get("kind") == "request" and r.get("method") == "tools/call"',
     '    return any(r.get("kind") == "rpc" and r.get("method") == "tools/call"',
     "the stdio probe's reader agrees with the receipt the echo fixture actually writes"),
    # THE DEFECT AS IT SHIPPED, in both probes: no on-list clause, so a `tools:` that suppresses
    # the whole server scores ENFORCED. It printed exactly that over a real run before the
    # prompt was fixed, which is why the branch is driven rather than merely present.
    ("F38-the-allowlist-need-not-admit-anything", CGATE,
     "    if not called(gated, ALLOWED):",
     "    if False:",
     "stdio: NEITHER tool arriving under the allowlist is SUPPRESSES_ALL, not a filter"),
    ("F39-the-remote-allowlist-need-not-admit-anything", CGATE_REMOTE,
     "    if not called(gated, ALLOWED):",
     "    if False:",
     "remote: NEITHER tool arriving under the allowlist is SUPPRESSES_ALL, not a filter"),
    # ...and the control's half of the same rule. The gated arm is read for two facts of
    # opposite sign, so a control that exercised only one of them leaves the other to the model.
    ("F40-the-control-need-only-have-called-the-off-list-tool", CGATE,
     "    if not called(control, OFF_LIST) or not called(control, ALLOWED):",
     "    if not called(control, OFF_LIST):",
     "stdio: ...and neither does one that never called the on-list tool"),
    # A CREDENTIAL SENT ONCE AND DROPPED is a different animal from one sent always, and this
    # is the weakening probe #1 already had to repair once (PR #106). Now that the bearer gates
    # the exit status rather than only the tally, the weakening has somewhere to hide.
    # RE-ANCHORED, and the reason is worth more than the entry. This pointed at the containment
    # form of `credential_arrived`; the equality fix rewrote that line, so the anchor went stale
    # and the every-vs-any property lost its mutation — while `F48`, added in the same round,
    # covers intact-vs-containing and looks like a replacement without being one. The full suite
    # reported STALE ANCHOR and refused to claim 52/52; driving only the NEW mutations, which is
    # what I did, could not have found it. Changing a line invalidates every mutation aimed at
    # it, and the two axes over one expression are still two axes (review, PR #110).
    # --- Phase 2 slice 1: the events probe, and the `type`-omission arm -------------------
    # THE UNMEASURED/NEGATIVE COLLAPSE, which is this file's most-repeated defect one probe
    # over. "No MCP tool call in the stream" and "copilot uses bare tool names" are different
    # facts, and reading the first as the second would tell slice 4 to match a bare name.
    ("F171-no-tool-call-reads-as-a-bare-name", CEVENTS,
     "    if name is None:\n        return UNMEASURED",
     "    if name is None:\n        return BARE",
     "no MCP tool call at all is UNMEASURED, never BARE"),
    # The positive control IS the measurement. Without it, a run where the sentinel never
    # travelled certifies redaction — a channel nobody proved was connected reporting silence.
    ("F172-redaction-is-certified-without-a-control", CEVENTS,
     "    if sentinel not in control_stream:\n        return CONTROL_FAILED, (",
     "    if False:\n        return CONTROL_FAILED, (",
     "a control that never carried the sentinel measures nothing"),
    # A conjunction over three questions, not a lookup on the last one read.
    ("F173-two-answered-questions-are-enough", CEVENTS,
     "    return (fmt != UNMEASURED\n            and spelling != REPORTS_NEITHER",
     "    return (fmt != UNMEASURED\n            or spelling != REPORTS_NEITHER",
     "...and ONE gap (server never named) is enough to fail it"),
    # The advertised name and the config key are the whole reason question 2 is answerable.
    # Checking only one spelling makes the other read as "our server never appeared".
    ("F174-only-the-config-key-is-ever-recognised", CEVENTS,
     "    for name, status in seen:\n        if name == ADVERTISED_NAME:\n            return REPORTS_ADVERTISED, status",
     "    for name, status in seen:\n        if False:\n            return REPORTS_ADVERTISED, status",
     "...the ADVERTISED name is recognised as a different answer"),
    # A witness that reads only the first event cannot see a server that failed later — the
    # exact transition slice 2 exists to classify.
    ("F175-a-later-status-transition-is-invisible", CEVENTS,
     '        elif etype == "session.mcp_server_status_changed" and isinstance(data, dict):',
     "        elif False:",
     "the witness reads BOTH events, so a later transition is not invisible"),
    # The structural clause. Without it, a fixture that never started reports "the tool never
    # arrived" and it is published as a finding about `type`.
    ("F176-an-unstarted-fixture-becomes-a-type-finding", CGATE_REMOTE,
     "    if not server_ran(records):\n        return INSTRUMENT_FAILED, (",
     "    if False:\n        return INSTRUMENT_FAILED, (",
     "...but a server that never announced itself is INSTRUMENT_FAILED, not a finding"),
    # The arm is worthless if the flag does not change the shape it writes.
    ("F177-the-omission-arm-writes-type-anyway", CGATE_REMOTE,
     '    if write_type:\n        server["type"] = kind',
     '    if True:\n        server["type"] = kind',
     "write_type=False really omits the key, and True really writes it"),
    ("F41-the-bearer-need-only-arrive-once", CGATE_REMOTE,
     '    return all((r.get("headers") or {}).get("authorization", "") == expected for r in seen)',
     '    return any((r.get("headers") or {}).get("authorization", "") == expected for r in seen)',
     "the bearer counts only when it is on EVERY request that carried headers"),
    # "copilot wrote it differently" and "copilot never wrote it" lead to different work — one
    # is an adapter change, the other is another probe run. Collapsing them reports work that
    # does not exist, or hides work that does.
    ("F42-a-differently-named-container-reads-as-unexercised", CCONFIG,
     ('    out["servers_container"] = (CONFIRMED if found_container == container\n'
      '                                else f"differs:{found_container}")'),
     ('    out["servers_container"] = (CONFIRMED if found_container == container\n'
      '                                else UNEXERCISED)'),
     "a differently-named container is reported as `differs`, naming what was found"),
    # THE SET CLOSED IN ONE DIRECTION ONLY, which is the state that actually shipped: the first
    # run reported "keys that DIFFER: none" while copilot was writing a `type` discriminator
    # nothing in §3 mentions. Checking only the keys you thought of cannot report the one you
    # did not.
    ("F43-only-the-expected-keys-are-looked-for", CCONFIG,
     "    return sorted(k for k in body if k not in known)",
     "    return sorted(k for k in body if k in known and k not in known)",
     "a key the adapter has no plan for is reported, since that is the one nobody sees"),
    # `copilot mcp add name -- --url X` writes a well-formed LOCAL entry whose command is
    # `--url`. A probe reading only "did a record appear" calls that a measured remote spelling.
    ("F44-a-local-entry-passes-as-the-remote-shape", CCONFIG,
     "    if command is not None and url is None:",
     "    if False:",
     "a remote add filed as a local entry is named, not counted as the remote spelling"),
    # ARRIVING IS NOT WORKING. Receipts record a request coming IN; nothing in them can see the
    # answer going OUT, so without this clause a client that forwards the call and drops the
    # reply scores ENFORCED and the harness gates onto a tool that returns nothing.
    ("F45-a-call-that-arrives-need-not-have-answered", CGATE,
     "    if not answered:",
     "    if False:",
     "stdio: an on-list call whose reply never came back is ANSWER_LOST, not ENFORCED"),
    ("F46-a-remote-call-that-arrives-need-not-have-answered", CGATE_REMOTE,
     "    if not answered:",
     "    if False:",
     "remote: an on-list call whose reply never came back is ANSWER_LOST, not ENFORCED"),
    # THE PERMISSIVE DEFAULT, which is the only default that would keep the older calls working
    # — and hands ENFORCED to any caller that forgets the argument.
    ("F47-the-round-trip-fact-gets-a-permissive-default", CGATE,
     "def classify(gated: list[dict], control: list[dict], answered: bool) -> tuple[str, str]:",
     "def classify(gated: list[dict], control: list[dict], answered: bool = True):",
     "stdio: the round-trip fact is required rather than defaulted"),
    # CONTAINMENT ACCEPTS A VALUE THAT IS NOT THE ONE DECLARED — `Bearer <sentinel>-altered`
    # passes, and the server then received something the harness never sent.
    ("F48-the-bearer-need-only-contain-the-token", CGATE_REMOTE,
     '    return all((r.get("headers") or {}).get("authorization", "") == expected for r in seen)',
     '    return all(sentinel in (r.get("headers") or {}).get("authorization", "") for r in seen)',
     "...and a bearer the client altered around the token does not count as arrival"),
    # A `url` IS NOT THE SHAPE. The gating probes write four keys by hand; confirming one of
    # them leaves the credential and the allowlist resting on documentation.
    #
    # RE-AIMED after the full suite reported MISSED. This perturbed a `missing` list that only
    # ever changed the failure MESSAGE — the absent case was already refused by the type checks
    # added a round later, so the mutation produced no defect. That clause is gone and this now
    # perturbs the guard that actually refuses an absent `headers`. The MISS is the interesting
    # part: the anchor never went stale, so the preflight could not see it, and driving only
    # the mutations I had TOUCHED could not either — what changed was the code underneath an
    # untouched entry (full run, PR #110).
    ("F49-an-absent-headers-map-is-treated-as-a-present-one", CCONFIG,
     "    headers, tools = body.get(\"headers\"), body.get(\"tools\")",
     ('    headers, tools = body.get("headers") or {"Authorization": "Bearer x"}, '
      'body.get("tools")'),
     "a remote entry missing the credential or the allowlist is not the shape §8 needs"),
    ("F50-the-transport-discriminator-is-not-checked", CCONFIG,
     "    if kind != want_type:",
     "    if False:",
     "...and a transport discriminator that is not the one asked for is refused"),
    # THE VERDICT, NOT THE CLASSIFIER. Each of these leaves its named function correct and stops
    # `main` from acting on it — the exact gap that let `remote_shape` be right while the exit
    # status ignored it, and the reason the verdicts were extracted at all.
    # RE-ANCHORED when `run_ok` became `certifies_native`. Caught by `stale_anchors` in a
    # second, which is the entire argument for putting it before the baselines.
    ("F51-the-remote-verdict-ignores-the-credential", CGATE_REMOTE,
     "    return verdict == ENFORCED and bearer_ok and version_ok",
     "    return verdict == ENFORCED and version_ok",
     "remote: ...and the same, per transport, over bearer and version too"),
    ("F52-the-config-verdict-ignores-the-remote-shape", CCONFIG,
     "    return 1 if (differs or surprises or not remote_ok) else 0",
     "    return 1 if (differs or surprises) else 0",
     "the config probe fails on ANY of its three findings, not just the stdio ones"),
    # THE ANCHOR VALIDATOR'S OWN TWO WAYS OF BEING WRONG. It exists because a stale anchor cost
    # a 77-minute run to discover; a validator that reads one half of a two-part `find`, or that
    # treats "matches twice" as fine, would hand back the same silence for the same money.
    ("F53-the-validator-reads-only-the-first-anchor", SELF,
     ("        for f in (find if isinstance(find, tuple) else (find,)):\n"
      "            n = text.count(f)"),
     ("        for f in (find[:1] if isinstance(find, tuple) else (find,)):\n"
      "            n = text.count(f)"),
     "...and a two-part anchor is checked in both parts, not just the first"),
    ("F54-an-anchor-matching-twice-is-accepted", SELF,
     "            if n != 1:\n                out.append((mid, rel, n))",
     "            if n == 0:\n                out.append((mid, rel, n))",
     "...and so is one that matches twice, which would mutate the wrong site"),
    # THE MARKER BACK WHERE THE CLI CAN READ IT. `answered` is evidence about a reply only
    # while the CLI holds no other copy; a driver-chosen marker in the config satisfies the
    # round-trip clause with nothing having returned. Two versions of this line shipped wrong.
    ("F55-the-round-trip-marker-is-put-where-the-cli-can-read-it", CGATE,
     '                            "ECHO_MCP_IDENTITY": IDENTITY_GENERATE}}',
     '                            "ECHO_MCP_IDENTITY": "a-marker-the-driver-chose"}}',
     "the config asks the server to MINT a marker rather than carrying one"),
    # THE SERVER STOPS MINTING and treats the sentinel as a literal marker, which makes every
    # reply "contain the marker" for free — the unfalsifiable form of the same clause.
    ("F56-the-generate-sentinel-is-used-as-the-marker", ECHO,
     'if IDENTITY == IDENTITY_GENERATE:\n    IDENTITY = uuid.uuid4().hex',
     'if False:\n    IDENTITY = uuid.uuid4().hex',
     "the digest reported is not the sentinel's, so the server minted rather than echoed"),
    # ...and the route back to the driver that does not pass through the CLI.
    ("F57-the-minted-marker-is-not-reported-to-the-driver", ECHO,
     "             identity_digest=identity_digest())",
     '             identity_digest="")',
     "the receipts carry a DIGEST, and that marker VALUE appears nowhere in them"),
    # A RECEIPT THAT DISAGREES WITH THE REPLY makes `answered` unfalsifiable in the other
    # direction: the driver looks for a value the tool never emits.
    ("F58-the-reported-identity-is-not-the-one-the-reply-carries", ECHO,
     '        return _text(f"{IDENTITY}:{text}" if IDENTITY else text)',
     '        return _text(text)',
     "...and the reply carries a token whose digest is the one reported"),
    # THE KNOB STOPS BEING OPT-IN, which silently changes what every verbatim-echo check means.
    ("F59-every-server-mints-a-marker-whether-asked-or-not", ECHO,
     'IDENTITY = os.environ.get("ECHO_MCP_IDENTITY") or ""',
     'IDENTITY = os.environ.get("ECHO_MCP_IDENTITY") or "a-marker-nobody-asked-for"',
     "...while a server not asked for a marker reports none, so the knob stays opt-in"),
    # THE NAME AND THE MEANING PULLED APART AGAIN: certification widened back to "settled", so
    # LEAKED — the finding these probes exist to catch — would exit 0 as permission.
    ("F60-a-settled-negative-certifies-native", CGATE,
     "    return verdict == ENFORCED and version_ok",
     "    return settled(verdict) and version_ok",
     "stdio: a settled negative does NOT certify `native`, which is what exit 0 claims"),
    ("F61-a-leaked-transport-still-certifies-native", CGATE_REMOTE,
     "    return verdict == ENFORCED and bearer_ok and version_ok",
     "    return settled(verdict) and bearer_ok and version_ok",
     "remote: ...and the same, per transport, over bearer and version too"),
    # THE VERSION GATE, in each of the three. A run that cannot say which build it measured
    # certifies nothing, and this used to be a string in a `print`.
    ("F62-an-unreadable-version-is-usable-anyway", CGATE,
     ('    if rc != 0:\n        return (text or f"exit {rc}"), False\n'
      "    return (text, bool(_VERSION_RE.search(text)))"),
     ('    if rc != 0:\n        return (text or f"exit {rc}"), False\n'
      "    return (text, bool(text))"),
     "stdio: a preflight version must LOOK like a version, not merely be output"),
    ("F64-the-measured-discriminator-goes-back-to-being-a-surprise", CCONFIG,
     '    "type": "type",\n}',
     "}",
     "the discriminator copilot actually writes is a known key, not a permanent surprise"),
    # PRESENCE IS NOT SHAPE: `headers: []` and `tools: "wrong"` are values §8's pattern cannot
    # be built from, filed as confirmation that it can.
    ("F65-a-headers-value-need-not-carry-a-bearer", CCONFIG,
     "    if not (isinstance(auth, str) and auth.startswith(\"Bearer \") and auth[7:].strip()):",
     "    if False:",
     "a headers value that is not a mapping is not the credential half of §8's pattern"),
    ("F66-an-allowlist-need-not-be-a-list-of-names", CCONFIG,
     ("    if not (isinstance(tools, list) and tools\n"
      "            and all(isinstance(t, str) and t for t in tools)):"),
     "    if False:",
     "...and an allowlist that is not a non-empty list of names is not one either"),
    # THE RECEIPTS BECOME A ROUTE TO THE MARKER AGAIN. The path is in the config the CLI reads
    # and the file is in its working directory under `--allow-all`, so a plaintext marker there
    # is readable without any reply having returned.
    ("F67-the-receipts-carry-the-marker-in-plaintext", ECHO,
     "    return hashlib.sha256(IDENTITY.encode(\"utf-8\")).hexdigest() if IDENTITY else \"\"",
     '    return IDENTITY',
     "the receipts carry a DIGEST, and that marker VALUE appears nowhere in them"),
    # THE LIVE PROBE FAILS OPEN on a receipt that reports the sentinel — the exact state a
    # broken mint produces, and a transcript containing that word then scored ENFORCED.
    ("F68-any-string-counts-as-a-minted-marker", CGATE,
     "            return digest if isinstance(digest, str) and _DIGEST_RE.match(digest) else \"\"",
     '            return digest if isinstance(digest, str) else ""',
     "a receipt whose identity is the generation sentinel is not a minted marker"),
    ("F69-an-empty-digest-is-satisfied-by-anything", CGATE,
     "    if not digest:\n        return False",
     "    if not digest:\n        return True",
     "...and an empty digest is never satisfied by any transcript"),
    # THE VERSION IDENTIFIES A DIFFERENT EXECUTION. copilot's launcher can resolve different
    # cached code between two invocations, which is why the adapter reads it in-band.
    ("F70-an-arm-with-no-witness-is-treated-as-witnessed", CGATE,
     "    if any(f is None for f in found):",
     "    if False:",
     "stdio: an executed arm with no witness leaves the run UNVERIFIED"),
    ("F71-the-arms-need-not-have-run-the-same-build", CGATE,
     "    if len(set(found)) != 1:",
     "    if False:",
     "stdio: ...and arms that ran different builds do not agree on one"),
    ("F73-the-remote-arms-need-not-agree-on-a-build", CGATE_REMOTE,
     "    if len(set(found)) != 1:",
     "    if False:",
     "remote: ...and arms that ran different builds do not agree on one"),
    ("F74-a-remote-arm-with-no-witness-is-treated-as-witnessed", CGATE_REMOTE,
     "    if any(f is None for f in found):",
     "    if False:",
     "remote: an executed arm with no witness leaves the run UNVERIFIED"),
    # THE CONTROL STOPS DECIDING ALONE, so the second model call runs for a number that cannot
    # move the verdict — and `classify` and `main` stop agreeing about what a run means.
    ("F72-the-control-never-decides-on-its-own", CGATE,
     ("    if not called(control, OFF_LIST) or not called(control, ALLOWED):\n"
      "        return UNMEASURED, (f\"the CONTROL called {OFF_LIST}={called(control, OFF_LIST)} \""),
     ("    if False:\n"
      "        return UNMEASURED, (f\"the CONTROL called {OFF_LIST}={called(control, OFF_LIST)} \""),
     "stdio: ...and one that skipped a tool decides UNMEASURED, so no second call"),
    # THE CONSUMER, not the rule. F72 perturbs `control_verdict`; these perturb whether `main`
    # and `measure` ACT on it — and removing either left every check green until §E19 started
    # counting calls with a fake runner (review, PR #110).
    ("F75-stdio-main-runs-the-gated-arm-anyway", CGATE,
     "        decided = control_verdict(control)\n        if decided is not None:",
     "        decided = control_verdict(control)\n        if False:",
     "stdio main does NOT run the gated arm once the control has decided"),
    ("F76-remote-measure-runs-the-gated-arm-anyway", CGATE_REMOTE,
     '        if label == "gated" and control_verdict(results["control"][0]) is not None:',
     "        if False:",
     "remote measure does NOT run the gated arm once the control has decided"),
    # THE PLAINTEXT BACK IN THE RECEIPTS, in a field the schema check would not have noticed.
    ("F77-a-receipt-field-leaks-the-marker-beside-the-digest", ECHO,
     "             identity_digest=identity_digest())",
     "             identity_digest=identity_digest(), leaked_plaintext=IDENTITY)",
     "the receipts carry a DIGEST, and that marker VALUE appears nowhere in them"),
    # A CONSTANT IS NOT A MINT. Every digest/reply/receipt check above passes on a fixed
    # 32-hex marker, and a constant in this file's SOURCE is readable by a CLI that can read
    # files — which restores the non-reply route the digest was introduced to close.
    ("F78-the-minted-marker-is-a-constant", ECHO,
     "    IDENTITY = uuid.uuid4().hex",
     '    IDENTITY = "a" * 32',
     "two `@generate` instances mint DIFFERENT markers, so it is not a constant"),
    # AND THE POOLED VERSION AGREEMENT: per-transport singletons let two builds each enforce
    # one transport and report as one build enforcing both.
    ("F79-each-transport-certifies-against-its-own-build", CGATE_REMOTE,
     "        return 0 if (ok and pooled_ok) else 1",
     "        return 0 if ok else 1",
     "remote: every arm of every transport names ONE build, not one per transport"),
    ("F36-the-child-reports-no-environment-at-all", TARGET,
     '"pgid": os.getpgid(0), "env_seen": sorted(set(seen))}',
     '"pgid": os.getpgid(0), "env_seen": []}',
     "the child's environment arrives by the route a control var would take"),

    # ------------------------------------------------------------------------------------
    # THE PARALLEL RUNNER'S OWN READERS (§E17), which are `F*` for the reason at the top of
    # the file: they perturb an instrument, and what judges them is `verify_mcp_fixtures.py` —
    # a different program, run from the mutated copy while the runner doing the scoring keeps
    # executing from the original tree. The scoring itself is still off limits.
    # ------------------------------------------------------------------------------------
    # THE VERDICT, three ways. Each collapses a distinction the summary is a claim about: a
    # hang counted as a defect nothing noticed, any red suite counted as the right red suite,
    # and a green one counted as a catch.
    ("F80-a-hung-suite-is-scored-as-a-defect-that-passed", SELF,
     ("    if outcome.output == _TIMEOUT_OUTPUT:\n"
      "        return TIMEOUT\n"),
     "",
     ("...and a hung one is TIMEOUT rather than whatever the later branches would say of it")),
    ("F81-any-red-suite-counts-as-the-named-arm", SELF,
     "\n    return CAUGHT if arm in failed else NOT_VIA",
     "\n    return CAUGHT",
     "...one that goes red on something else is NOT the same answer"),
    ("F82-a-suite-that-still-passes-counts-as-a-catch", SELF,
     ("    if outcome.returncode == 0:\n"
      "        return MISSED"),
     ("    if outcome.returncode == 0:\n"
      "        return CAUGHT"),
     "...one that still passes with the defect present is MISSED"),
    # THE LINE AND THE VERDICT, made to disagree. Two readers of one run is how they drifted
    # before `result_line` was written from `verdict`'s answer rather than from its conditions.
    ("F83-the-printed-line-contradicts-the-verdict-it-was-built-from", SELF,
     '\n    return f"{mid}: *** MISSED *** {suite} still passes with the defect present {took}"',
     '\n    return f"{mid}: CAUGHT by {arm} {took}"',
     "...and it says MISSED exactly when the verdict does"),
    ("F84-only-the-wall-clock-is-printed", SELF,
     '\n    took = f"({outcome.wall:.1f}s wall, {outcome.cpu:.1f}s cpu)"',
     '\n    took = f"({outcome.wall:.1f}s wall)"',
     "the printed line carries the verdict, the arm and BOTH clocks"),
    # `--jobs`, BOTH WAYS OF ACCEPTING WHAT SHOULD BE REFUSED. A typo'd flag and a zero both
    # end in a serial run that looks like a machine which did not speed up.
    ("F85-an-unknown-argument-is-ignored-rather-than-refused", SELF,
     '\n            raise ValueError(f"unknown argument {arg!r}; the only one is `--jobs N`")',
     "\n            continue",
     "...and every way of asking for it wrongly is refused rather than rounded to 1"),
    ("F86-zero-jobs-is-accepted-and-quietly-becomes-one", SELF,
     "\n    if not text.isdigit() or int(text) < 1:",
     "\n    if not text.isdigit():",
     "...and every way of asking for it wrongly is refused rather than rounded to 1"),
    # A repeated id stops being reported, which is the state this runner shipped in until an
    # id collision produced eight mutations under four names and a green run either way.
    ("F137-the-id-comparison-includes-the-description-again", SELF,
     "\n    head = mid.split(\"-\", 1)[0]",
     "\n    head = mid",
     "...and two entries sharing a NUMBER collide however differently they are described"),
    ("F136-a-repeated-mutation-id-reports-as-a-clean-table", SELF,
     "\n        seen[_canonical_mid(mid)] = seen.get(_canonical_mid(mid), 0) + 1",
     "\n        seen[_canonical_mid(mid)] = 1",
     "...and two entries sharing a NUMBER collide however differently they are described"),
    # THE SLOWEST-MUTATION WARNING, on the axis that stops meaning anything under load and on
    # the entries that never ran.
    ("F87-the-slowest-mutation-is-picked-by-wall-clock", SELF,
     "\n    return max(ran, key=lambda r: r.cpu, default=None)",
     "\n    return max(ran, key=lambda r: r.wall, default=None)",
     "the slowest mutation is the one that spent the most CPU, not the most wall clock"),
    ("F88-a-mutation-that-never-ran-is-ranked-among-those-that-did", SELF,
     "\n    ran = [r for r in records if r.verdict not in (UNAPPLIED, ABANDONED)]",
     "\n    ran = list(records)",
     "...and a mutation that never ran is not ranked at all, nor mistaken for 'nothing ran'"),
    # APPLY / RUN / REVERT. The first is the mutation never reaching the file the suite reads;
    # the second is a tree going back into the pool still mutated, which is the defect that
    # only exists because the trees became shared property.
    ("F89-the-suite-is-run-before-the-mutation-is-written", SELF,
     ("    path.write_text(mutated)\n"
      "    try:\n"
      "        outcome = run(work, suite)"),
     "    try:\n        outcome = run(work, suite)",
     "the file the suite sees is the MUTATED one, and it is put back afterwards"),
    ("F90-the-revert-happens-only-when-the-suite-could-be-run", SELF,
     ("    try:\n"
      "        outcome = run(work, suite)\n"
      "    finally:\n"
      "        path.write_text(original)"),
     ("    outcome = run(work, suite)\n"
      "    path.write_text(original)"),
     ("...and it is put back even when the suite could not be run at all, since a mutated "
      "tree goes back into the pool for the next mutation to draw")),
    ("F91-an-unapplied-mutation-is-counted-as-a-catch", SELF,
     ("        return Record(mid, kind, UNAPPLIED,\n"
      '                      f"{mid}: STALE ANCHOR'),
     ("        return Record(mid, kind, CAUGHT,\n"
      '                      f"{mid}: STALE ANCHOR'),
     "an anchor that no longer matches is UNAPPLIED, and says the suite did not run"),
    ("F92-an-anchor-matching-twice-is-applied-to-whichever-site-is-first", SELF,
     "\n    if any(c > 1 for c in counts):",
     "\n    if False and any(c > 1 for c in counts):",
     "...and one that matches twice is too, rather than mutating whichever site is first"),
    # THE WORK TREE. Sharing the venv is only safe because nothing resolves through it, and
    # this is that argument's failure mode: a tree missing the package binds the ORIGINAL, runs
    # unmutated code, and reports MISSED for every entry an hour later.
    ("F93-the-work-tree-omits-the-package-and-binds-the-original", SELF,
     '\n                    ignore=shutil.ignore_patterns("__pycache__", "artifacts", "build", ".venv"))',
     '\n                    ignore=shutil.ignore_patterns("__pycache__", "artifacts", "build", ".venv", "agentskill_evals"))',
     "a work tree binds its OWN copy of the package, not the original it was copied from"),
    # THE TWO CLOCKS AT THE KERNEL. A signalled suite reported as a positive exit code reads as
    # an ordinary failing run; a timeout that kills without reaping leaves a pid the NEXT
    # mutation's `wait4` cannot wait for; and a CPU figure that is always zero silently retires
    # the only early warning §4 has before a looping defect becomes a hang.
    ("F94-a-signalled-suite-reports-a-positive-exit-code", SELF,
     "\n    return -os.WTERMSIG(status) if os.WIFSIGNALED(status) else os.WEXITSTATUS(status)",
     "\n    return os.WEXITSTATUS(status)",
     "...and an exit status is read the way subprocess spells it, signals negative"),
    ("F95-a-timed-out-suite-is-killed-but-never-reaped", SELF,
     ("        _kill_all([proc.pid])\n"
      "        _pid, status, usage = os.wait4(proc.pid, 0)\n"
      "        proc.returncode = _exit_code(status)"),
     ("        _kill_all([proc.pid])\n"
      "        proc.returncode = -signal.SIGKILL"),
     "a child that outlives its bound is reported as such, killed, and reaped"),
    ("F96-cpu-is-never-actually-read-off-the-wait", SELF,
     ("            proc.returncode = _exit_code(status)\n"
      "            return status, usage.ru_utime + usage.ru_stime"),
     ("            proc.returncode = _exit_code(status)\n"
      "            return status, 0.0"),
     "a child's CPU is measured from the wait that reaps THAT child"),
    # THE CONTAINMENT GAP ITSELF, restored exactly as it shipped: the timeout path kills the
    # process it holds a handle on and nothing else. Every proxy, guardian, fixture server and
    # helper a hung suite started outlives it, outlives the `rmtree` of the tree they were
    # launched from, and runs beside the workers still going (review, PR #111).
    ("F97-a-timed-out-suite-is-killed-without-its-descendants", SELF,
     ("\n        swept = kill_owned(proc.pid, marker)\n"
      "        leftover, fault = swept.leftover, swept.fault"),
     "\n        _signal(proc.pid)",
     "...and a hung suite takes its whole process tree with it, `setsid` and all"),
    # AND THE ORDER, which is the mechanism rather than a detail. Signal first and the parentage
    # arm has nothing left to read: every descendant is reparented to init the instant the root
    # dies, and with no marker there is no second way to find them.
    # WRITTEN THROUGH `_signal`, NOT AROUND IT, and that is not a style note. The first version
    # of this entry spelled the premature kill as a bare `os.kill(root, signal.SIGKILL)`. The
    # verifier calls `kill_owned` once with no parentage root, which was then spelled `-1` — so
    # the mutant executed `os.kill(-1, SIGKILL)`, which POSIX defines as every process the user
    # may signal, and it closed every application on the machine (2026-08-12). A mutation suite
    # runs deliberately broken code by design, so a destructive call is only as contained as its
    # worst reachable variant: the guard has to be in the primitive, and the mutation has to go
    # through it.
    ("F98-the-tree-is-signalled-before-it-is-enumerated", SELF,
     ("        while True:\n"
      "            table = process_tree()"),
     ("        while True:\n"
      "            if root is not None:\n"
      "                _signal(root)\n"
      "            table = process_tree()"),
     "...and a hung suite takes its whole process tree with it, `setsid` and all"),
    ("F99-the-sweep-reaches-only-the-immediate-children", SELF,
     ("    owned, frontier = set(), [root]\n"
      "    while frontier:\n"
      "        pid = frontier.pop()\n"
      "        for kid in children.get(pid, ()):\n"
      "            if kid not in owned:\n"
      "                owned.add(kid)\n"
      "                frontier.append(kid)"),
     "    owned = set(children.get(root, ()))",
     "the sweep walks the whole chain, not just the immediate children"),
    # THE `None` GUARD. This is the mutation the sentinel was CHOSEN for: with a falsy string it
    # would have swept every process on the machine from inside the mutation runner, and with
    # `None` it raises before signalling anything. What reddens is the parentage-only case,
    # which is why that one is driven through `survives`.
    ("F100-the-marker-arm-is-not-guarded-at-all", SELF,
     "\n    if marker is not None:",
     "\n    if True:",
     "the sweep walks the whole chain, not just the immediate children"),
    ("F101-an-empty-marker-is-accepted-instead-of-refused", SELF,
     ('    if marker == "":\n'
      '        raise ValueError('),
     ("    if False:\n"
      "        raise ValueError("),
     "...and an empty marker is REFUSED rather than quietly matching every process alive"),
    # A ZOMBIE IS NOT A SURVIVOR. Counting one makes the sweep spin out its whole deadline over
    # a process that is already dead and then report it as a leak — the reaped suite itself,
    # every time, which would make a real leftover unreadable among the noise.
    ("F102-a-zombie-counts-as-a-live-descendant", SELF,
     ('            if not state.startswith("Z"):\n'
      "                table[pid] = Proc(ppid, state, command)"),
     "            table[pid] = Proc(ppid, state, command)",
     "a child that outlives its bound is reported as such, killed, and reaped"),
    # NO MUTATION REMOVES `_signal`'s OWN GUARD, and the refusal is the finding rather than a
    # gap. Every other entry here reintroduces a defect and asks whether an arm notices; that
    # one would reintroduce the ability to broadcast a SIGKILL and then ask the machine. The
    # check that covers it (`no non-process ever reaches the one function that signals`) is
    # driven directly on the values instead, which establishes the same property without a
    # version of this file that can take the host down existing on disk for eleven minutes.
    # The rule generalizes: a mutation may perturb WHICH processes are chosen, never the guard
    # that decides whether a chosen thing is a process at all.
    # THE FREEZE, WHICH IS THE ONLY THING THAT CLOSES THE RACE. `SIGSTOP` becomes `SIGCONT` —
    # still a signal, still delivered to exactly the same set, and completely unable to stop a
    # tree from spawning while it is being enumerated. That is the version this replaced, whose
    # backstop was the claim that every descendant's argv names the work tree; measured, more
    # than a third of them do not, and the ones caught between fork and exec have no argv at all.
    ("F110-the-tree-is-enumerated-without-being-stopped", SELF,
     "\n                _signal(pid, signal.SIGSTOP)",
     "\n                _signal(pid, signal.SIGCONT)",
     "...and every one of them is gone, including those spawned after the first snapshot"),
    # AND THE PART THAT MAKES FREEZING CONVERGE. One round stops what parentage reaches now;
    # the next catches what those had already forked. Stopping once and killing is the same
    # race one generation deeper.
    ("F111-the-freeze-does-not-iterate-to-a-fixed-point", SELF,
     ("            if not fresh and not pending:\n"
      "                quiesced = True\n"
      "                break"),
     ("            if not fresh:\n"
      "                quiesced = True\n"
      "                break"),
     "the freeze waits for SIGSTOP to be OBSERVED, not merely sent"),
    # AND THE PREDICATE THE WAIT TURNS ON. A state test that answers "stopped" for a running
    # process is the same defect one layer down, and it is the layer where the platform's
    # spelling lives.
    ("F113-a-running-process-reads-as-a-stopped-one", SELF,
     '\n    return proc.state[:1] in ("T", "t")',
     "\n    return True",
     "the freeze waits for SIGSTOP to be OBSERVED, not merely sent"),
    ("F114-a-freeze-that-never-settled-still-reports-a-clean-sweep", SELF,
     ("\n    if not quiesced:\n"
      '        reasons.append(f"the process tree did not stop spawning within '
      '{deadline:.0f}s: "'),
     ("\n    if False:\n"
      '        reasons.append(f"the process tree did not stop spawning within '
      '{deadline:.0f}s: "'),
     "...and a stop that never lands is a named failure, not a clean sweep"),
    # AND WHAT IS REPORTED AFTERWARDS. Re-enumerating asks the machine about orphans it can no
    # longer reach by parentage and may not name by marker — so it answers "clean" about
    # precisely the processes that got away. The frozen set is the only honest population.
    ("F112-survivors-are-looked-for-by-asking-the-machine-again", SELF,
     "\n    return tuple(sorted(pid for pid in frozen if pid in table))",
     "\n    return tuple(sorted(owned_pids(table, root, marker)))",
     "the survivors reported are the ones we froze, not whatever the machine still admits to"),
    # PRESENCE IS NOT CONFIRMATION. Both mutations collapse the two: the first drops the record
    # of what was actually seen stopped, the second treats a pid that disappeared before it
    # could be confirmed as one that settled. Each turns a process that exited — possibly after
    # forking — into evidence of a clean tree.
    ("F115-a-pid-that-vanished-before-confirmation-counts-as-settled", SELF,
     "\n            lost |= (frozen - confirmed) - table.keys()",
     "\n            lost |= set()",
     "a pid that vanishes before its stop is CONFIRMED leaves containment unestablished"),
    ("F116-an-unconfirmed-disappearance-reaches-no-output", SELF,
     ("\n    if lost:\n"
      '        reasons.append(f"{sorted(lost)} vanished before being observed stopped, '
      'so anything "'),
     ("\n    if False:\n"
      '        reasons.append(f"{sorted(lost)} vanished before being observed stopped, '
      'so anything "'),
     "a pid that vanishes before its stop is CONFIRMED leaves containment unestablished"),
    # THE OBSERVER, BROKEN ONE CLAUSE AT A TIME. Everything the sweep concludes is read off an
    # absence, so each of these turns a channel that could not answer into a machine with
    # nothing on it — and the timeout path then certifies a tree it never looked at.
    ("F104-a-ps-that-cannot-run-escapes-as-a-raw-OSError", SELF,
     ("    except (OSError, subprocess.SubprocessError) as exc:\n"
      '        raise ObserverFailed(f"`ps` did not run: {exc!r}") from exc'),
     ("    except (KeyboardInterrupt,) as exc:\n"
      '        raise ObserverFailed(f"`ps` did not run: {exc!r}") from exc'),
     "a `ps` that is not there at all is a named failure, not an empty machine"),
    ("F105-a-failing-ps-is-read-as-an-empty-machine", SELF,
     ("    if done.returncode != 0:\n"
      '        raise ObserverFailed(f"`ps` exited {done.returncode}: '
      '{done.stderr.strip()[:200]!r}")'),
     "    if False:\n        raise ObserverFailed(\"\")",
     "...and one that exits non-zero is a failure even though its output parses fine"),
    ("F106-the-observer-never-witnesses-itself", SELF,
     "\n    if os.getpid() not in seen:",
     "\n    if False and os.getpid() not in seen:",
     "...and one that succeeds without listing THIS process has not enumerated the machine"),
    # AND THE TWO HALVES OF WHAT A FAILED OBSERVER MUST NOT COST. Losing the descendants is a
    # containment failure that has to be NAMED; losing the root as well is a hang, because the
    # runner then waits forever on a child nothing killed.
    ("F107-a-blind-sweep-is-reported-as-a-clean-one", SELF,
     ("    except ObserverFailed as exc:\n"
      "        fault = str(exc)"),
     ("    except ObserverFailed:\n"
      '        fault = ""'),
     "...and the descendants are reported UNACCOUNTED FOR rather than certified gone"),
    ("F108-a-failed-sweep-takes-the-reap-down-with-it", SELF,
     ("    finally:\n"
      "        _kill_all([proc.pid])\n"
      "        _pid, status, usage = os.wait4(proc.pid, 0)\n"
      "        proc.returncode = _exit_code(status)"),
     ("    _kill_all([proc.pid])\n"
      "    _pid, status, usage = os.wait4(proc.pid, 0)\n"
      "    proc.returncode = _exit_code(status)"),
     "...and an UNEXPECTED failure in the sweep still leaves the suite killed and reaped"),
    ("F109-a-containment-failure-reaches-no-output", SELF,
     ('\n             f"{outcome.containment}" if outcome.containment else "")'),
     ('\n             f"{outcome.containment}" if False else "")'),
     "...and the TIMEOUT line says so, since a fact that reaches no output is a fact nobody has"),
    ("F103-a-sweep-that-left-something-behind-says-nothing", SELF,
     '\n             f"{list(outcome.leftover)}" if outcome.leftover else "")',
     '\n             f"{list(outcome.leftover)}" if False else "")',
     "...and a timeout whose sweep left something behind names the pids in its line"),
    # THE TEARDOWN'S TWO HALVES, which are one rule from each end: the registry cleanup reads is
    # written BEFORE the act, and the cleanup that reads it reaches every member. Both were
    # wrong, and both leave the same wreckage — a process SIGSTOPped and never killed, which
    # never exits and which `wait4` then waits on forever (review, PR #111).
    ("F117-the-registry-is-written-after-the-batch-it-must-cover", SELF,
     ("\n            frozen |= fresh\n"
      "            # Re-signalling a pending one is free and covers a signal that was lost to "
      "a race\n"
      "            # with its own exec; a stop delivered twice is still one stop.\n"
      "            for pid in sorted(fresh | pending):\n"
      "                _signal(pid, signal.SIGSTOP)"),
     ("\n            # Re-signalling a pending one is free and covers a signal that was lost to "
      "a race\n"
      "            # with its own exec; a stop delivered twice is still one stop.\n"
      "            for pid in sorted(fresh | pending):\n"
      "                _signal(pid, signal.SIGSTOP)\n"
      "            frozen |= fresh"),
     "a pid stopped before the batch failed is still killed, so nothing is left SIGSTOPped"),
    ("F118-a-cleanup-loop-gives-up-at-its-first-failure", SELF,
     ("\n        except (ValueError, OSError):\n"
      "            unreachable.append(pid)"),
     ("\n        except (ValueError, OSError):\n"
      "            unreachable.append(pid)\n"
      "            break"),
     "...and one cleanup signal failing does not abandon the rest of the set"),
    ("F119-a-pid-nothing-can-signal-is-not-worth-mentioning", SELF,
     ("\n    if unreachable:\n"
      '        reasons.append(f"{sorted(set(unreachable))} could not be signalled at all, '
      'so they are "'),
     ("\n    if False:\n"
      '        reasons.append(f"{sorted(set(unreachable))} could not be signalled at all, '
      'so they are "'),
     "...and a pid that cannot be signalled at all is a NAMED containment failure"),
    # WHETHER THE TREE CAN BE HANDED ON, BROKEN ONE FACT AT A TIME. The predicate is a
    # disjunction over two independent facts, so each half has its own mutation: an `or` read as
    # either of its operands is the precedence trap §4 names, and it passes every case where the
    # two happen to agree.
    ("F120-a-tree-nobody-could-enumerate-counts-as-clean", SELF,
     "\n    return bool(outcome.leftover) or bool(outcome.containment)",
     "\n    return bool(outcome.leftover)",
     "a work tree is contaminated by EITHER survivors or a sweep that could not look"),
    ("F121-a-tree-with-a-survivor-in-it-counts-as-clean", SELF,
     "\n    return bool(outcome.leftover) or bool(outcome.containment)",
     "\n    return bool(outcome.containment)",
     "a work tree is contaminated by EITHER survivors or a sweep that could not look"),
    # AND WHAT IS DONE WITH THE ANSWER. Four sites act on it, and each of them was the whole
    # defect on its own: the tree going back into the pool, the run carrying on beside whatever
    # is in it, the `rmtree` at the end, and the exit status.
    ("F122-a-contaminated-tree-goes-back-into-the-pool", SELF,
     "\n        clean = not record.contaminated",
     "\n        clean = True",
     "...and one whose sweep could not be cleared never does, nor runs anything beside it"),
    ("F123-a-sweep-that-raised-leaves-the-tree-reusable", SELF,
     "\n    clean = False\n    try:\n        record = apply_and_run(work, entry, suite, kind)",
     "\n    clean = True\n    try:\n        record = apply_and_run(work, entry, suite, kind)",
     "...and a run that RAISED contaminates too, since what survived it is exactly unknown"),
    ("F124-the-run-carries-on-past-a-containment-failure", SELF,
     "\n    if poisoned.is_set():\n        return _abandoned(entry, kind)",
     "\n    if False:\n        return _abandoned(entry, kind)",
     "...and once the run has stopped the rest are ABANDONED without drawing a tree at all"),
    ("F125-a-worker-waiting-on-the-pool-waits-on-the-pill", SELF,
     ("\n    if work is None:\n"
      "        trees.put(work)\n"
      "        return _abandoned(entry, kind)"),
     "\n    if work is None:\n        pass",
     "...and a worker already inside `get()` draws the pill instead of waiting forever"),
    ("F126-the-tree-a-survivor-is-running-in-is-deleted-anyway", SELF,
     '\n    if poisoned.is_set():\n        print(f"WORK TREES KEPT at {tmp}',
     '\n    if False:\n        print(f"WORK TREES KEPT at {tmp}',
     "a work tree with something still running out of it is KEPT, and its path is printed"),
    ("F127-a-poisoned-run-still-exits-0", SELF,
     "\n    return 0 if caught == totals and not poisoned.is_set() else 1",
     "\n    return 0 if caught == totals else 1",
     "a run that caught everything exits 0 only if nothing poisoned it along the way"),
    ("F128-an-abandoned-mutation-is-ranked-as-the-slowest-run", SELF,
     "\n    ran = [r for r in records if r.verdict not in (UNAPPLIED, ABANDONED)]",
     "\n    ran = [r for r in records if r.verdict != UNAPPLIED]",
     "...and a mutation that never ran is not ranked at all, nor mistaken for 'nothing ran'"),
    # THE BASELINE, WHICH IS THE TIMEOUT THAT SAID LEAST. It ends the run before any mutation has
    # run, in a tree already in the pool, and it reported neither what its sweep left nor that
    # the tree must be kept.
    ("F129-a-baseline-timeout-drops-what-its-sweep-left", SELF,
     '\n                  f"hung, so nothing below would prove anything.{containment_note(base)}")',
     '\n                  f"hung, so nothing below would prove anything.")',
     "a BASELINE that times out reports what its sweep left, and keeps the tree it left it in"),
    ("F130-a-baseline-timeout-leaves-the-run-unpoisoned", SELF,
     "\n            if contaminated(base):\n                poisoned.set()",
     "\n            if False:\n                poisoned.set()",
     "a BASELINE that times out reports what its sweep left, and keeps the tree it left it in"),
    # THE RULE AT THE BOUNDARY THAT OWNS THE TREES. Written per-spawner it has to be remembered
    # by the next spawner, and it was not: the first cut of "an exception counts as
    # contamination" lived in `_draw_and_run`, where the reproduction was, and a BASELINE that
    # raised walked past it into the delete (review, PR #111).
    ("F131-only-a-mutation-worker-can-poison-the-run", SELF,
     "\n    except BaseException:\n        poisoned.set()\n        raise",
     "\n    except BaseException:\n        raise",
     "a run that ends by RAISING marks its trees unaccounted for, wherever it raised"),
    # AND THE TWO WAYS A TEARDOWN REPORTS SUCCESS IT DOES NOT HAVE: not looking, and not being
    # asked. `ignore_errors=True` beside `return True` is a function suppressing its own
    # failures and then certifying the outcome; a caller that drops the answer is the same
    # silence one frame up.
    ("F132-the-answer-from-the-teardown-is-not-read", SELF,
     "\n    return rc if swept else 1",
     "\n    return rc",
     "...and a run whose trees would not go is red, however green its mutations were"),
    ("F133-a-deletion-is-certified-without-being-observed", SELF,
     '\n    if os.path.exists(tmp):\n        print(f"WORK TREES NOT DELETED',
     '\n    if False:\n        print(f"WORK TREES NOT DELETED',
     "...and a deletion that silently did nothing is reported, not claimed as success"),
    # THE WINDOW NO PRE-DRAW CHECK CAN CLOSE. Stopping the run reaches everything not yet
    # started; a sibling already inside its suite finishes and used to hand back an ordinary
    # verdict, counted and printed as a result. The second mutation is the other half of the
    # same condition: relabelling EVERYTHING loses the one report that explains the run.
    ("F134-a-verdict-measured-beside-the-unknown-tenant-still-counts", SELF,
     ("\n        if not record.contaminated and poisoned.is_set():\n            record = "
      "_overlapped(record)"),
     "\n        if False:\n            record = _overlapped(record)",
     "a verdict produced while ANOTHER worker's tree went unaccounted for is INCONCLUSIVE"),
    ("F135-the-record-that-stopped-the-run-is-relabelled-too", SELF,
     ("\n        if not record.contaminated and poisoned.is_set():\n            record = "
      "_overlapped(record)"),
     "\n        if poisoned.is_set():\n            record = _overlapped(record)",
     "...while the record that caused the stop keeps its own report rather than being relabelled"),

    # C3-4's probe. Every one of these is a way to make the §10.10 session decision come back
    # WRONG rather than come back missing — which is the failure mode worth buying arms for,
    # since a probe that visibly cannot measure gets re-run and one that quietly mismeasures
    # gets published.
    ("F138-a-failed-read-counts-as-a-released-session", SESSPROBE,
     "    if reply.status is None:\n        return UNREADABLE",
     "    if reply.status is None:\n        return DEAD",
     "a transport failure is UNREADABLE, never DEAD — a failed read is not a released session"),
    # The same collapse one door along: a server erroring, or refusing the credential, is not
    # a session that went away.
    ("F139-a-server-side-failure-reads-as-a-gone-session", SESSPROBE,
     ("        return ALIVE if rpc_response(reply.events, want_id) is not None else UNREADABLE"
      "\n    return UNREADABLE"),
     ("        return ALIVE if rpc_response(reply.events, want_id) is not None else UNREADABLE"
      "\n    return DEAD"),
     "...and neither is a server-side failure, nor an auth refusal"),
    # A 2xx IS A TRANSPORT FACT. Dropping the correlation makes an empty 200, a CDN
    # interstitial and a stream carrying only a priming event all read as a live session.
    ("F150-any-2xx-is-taken-for-a-live-session", SESSPROBE,
     "        return ALIVE if rpc_response(reply.events, want_id) is not None else UNREADABLE",
     "        return ALIVE",
     ("a 2xx with NO JSON-RPC answer in it is UNREADABLE, not ALIVE — an empty body, a CDN "
      "interstitial and an unparseable one are transport successes carrying no MCP evidence")),
    # Correlation dropped entirely: someone else's response, or a replayed one, answers us.
    ("F151-a-response-to-another-request-answers-ours", SESSPROBE,
     "        if \"id\" not in ev or request_id_key(ev[\"id\"]) != want:\n            continue",
     "        if False:\n            continue",
     "...and a JSON-RPC response to a DIFFERENT id does not answer ours"),
    # An ERROR response dropped from the accepted shapes. "Method not found" is the server
    # talking to us — refusing to count it reports a live session as unreadable on a
    # technicality, which is the false negative that matches the wrong direction of the
    # tri-state.
    ("F152-an-error-response-is-not-counted-as-the-server-answering", SESSPROBE,
     "        if classify_envelope(ev) not in (RESULT, ERROR):\n            continue",
     "        if classify_envelope(ev) not in (RESULT,):\n            continue",
     ("a well-formed result and a well-formed error both answer our id — an error is the "
      "server talking to us, which is what liveness asks")),
    # THE PRIMING-EVENT BUG, restored: keep only the first event and the probe reads whatever
    # arrived first as its answer.
    ("F153-only-the-first-sse-event-is-kept", SESSPROBE,
     "        return tuple(out)",
     "        return tuple(out[:1])",
     ("every event in a stream is kept, not just the first — a priming event before the "
      "response is permitted, and reading position instead of id makes it the answer")),
    # The out-of-band error reader made id-correlated, which discards the one message worth
    # reading: the live server answers a dead session with `"id": "server-error"`.
    ("F154-the-error-message-is-thrown-away-unless-it-correlates", SESSPROBE,
     "        if isinstance(ev, dict) and isinstance(ev.get(\"error\"), dict):",
     "        if isinstance(ev, dict) and isinstance(ev.get(\"error\"), dict) and False:",
     "an out-of-band error message is readable though its id matches nothing we sent"),
    # THE LIFECYCLE, unmeasured: a handshake the probe never completed reads the same as one
    # the server accepted.
    ("F155-an-unsent-initialized-reads-as-an-accepted-one", SESSPROBE,
     "    return ack is not None and ack.status is not None and 200 <= ack.status < 300",
     "    return True",
     ("...and a rejected, failed or NEVER-SENT one is not accepted — a handshake the probe "
      "skipped must not read the same as one the server took")),
    # Cleanup that believes the DELETE's 200 instead of observing the session gone — the exact
    # claim-versus-observation distinction this probe exists to draw.
    ("F156-a-session-is-marked-released-without-being-observed-gone", SESSPROBE,
     "        if state == DEAD:\n            ledger.mark_released(sid)",
     "        ledger.mark_released(sid)",
     ("a session whose post-DELETE read is UNREADABLE stays OUTSTANDING — cleanup keyed on "
      "readability would walk past exactly the sessions most likely to still be alive")),
    # THE CREDENTIAL GOES BACK INTO ARGV. Not a denylist any more — the refusal is the whole
    # loop, so re-admitting command-line values is the mutation.
    ("F157-a-header-value-is-accepted-from-argv-again", SESSPROBE,
     "    for raw in header_args or ():\n        name = (raw.partition(\":\")[0] or raw).strip()",
     "    for raw in [] or ():\n        name = (raw.partition(\":\")[0] or raw).strip()",
     ("NO header value is accepted from the command line, whatever the header is called, and "
      "each one is REFUSED rather than silently dropped — a list of which names are secret can "
      "always be one name short, so the rule is the flag")),
    # The refusal stops naming the flag that works, so the caller is stuck.
    ("F159-the-refusal-does-not-name-the-working-flag", SESSPROBE,
     ("            f\"world-readable, and a list of which header names are secret can always be one \"\n"
      "            f\"name short. Use --header-env '{name}=VAR_NAME', with the variable holding the \"\n"
      "            f\"COMPLETE value including any scheme, e.g. VAR_NAME='Bearer eyJ…'.\")"),
     "            f\"world-readable.\")",
     ("...and each refusal NAMES the flag that works, so the error is directed rather than a "
      "bare rejection")),
    # ENVELOPE RIGOUR, un-delegated. Each of these re-derives a rule the proxy already owns.
    ("F160-envelope-shape-is-not-checked-against-the-proxys-rule", SESSPROBE,
     "        if classify_envelope(ev) not in (RESULT, ERROR):\n            continue",
     "        if \"jsonrpc\" not in ev:\n            continue",
     ("every frame the PROXY classifies malformed is refused here too — the probe inherits the "
      "rule rather than agreeing with it by coincidence")),
    # Identity by STRING, which collapses the two domains JSON-RPC keeps apart: `"1"` and `1`
    # are different ids, and `str()` makes them one. Plain `==` is NOT the mutation here — the
    # shape gate rejects boolean ids before the comparison runs, so `==` is equivalent past it
    # and an equivalent mutant proves nothing (found by the suite reporting it uncaught).
    ("F161-request-id-domains-are-collapsed-by-stringifying", SESSPROBE,
     "        if \"id\" not in ev or request_id_key(ev[\"id\"]) != want:\n            continue",
     "        if \"id\" not in ev or str(ev[\"id\"]) != str(want_id):\n            continue",
     "a STRING id does not answer a NUMBER id — different domains per JSON-RPC"),
    # PER-SESSION HANDSHAKE, dropped: a sampled session that never entered normal operation is
    # measured as though it had.
    ("F162-the-eligibility-gate-does-not-gate", SESSPROBE,
     "        eligible, why = session_eligible(s, version)\n        if not eligible:",
     "        eligible, why = session_eligible(s, version)\n        if False:",
     ("Q3: EVERY sampled session was measurable — a row taken through an incomplete "
      "handshake, or a different revision, is a reading of another quantity entirely")),
    # THE EXCLUSION MADE COSMETIC AGAIN: the row is announced excluded and still enters the
    # verdict, which is what published `2 of 2 answered alike: indeterminate`.
    ("F165-excluded-rows-still-enter-the-sample", SESSPROBE,
     "    measurable = [r for r in rows if r.get(\"eligible\")]",
     "    measurable = list(rows)",
     ("rows that fail the gate are excluded from the SAMPLE, not merely announced — a verdict "
      "over rows that measured nothing is agreement about nothing")),
    # An errored `initialize` readmitted: a correlated response taken for a successful one.
    ("F166-an-errored-initialize-is-a-completed-handshake", SESSPROBE,
     "    if isinstance(response.get(\"error\"), dict):",
     "    if False:",
     ("an `initialize` that ERRORS is not a completed handshake, and no `initialized` is sent "
      "after it — a correlated response is not a successful one")),
    # The revision dropped from the gate, so a drifted session is measured after all.
    ("F167-a-drifted-revision-passes-the-gate", SESSPROBE,
     "    if expected_version is not None and session[\"version\"] != expected_version:",
     "    if False:",
     ("a session negotiating another revision is excluded from Q3 before it is measured, and "
      "the reason names both revisions")),
    # THE RUN-LEVEL GATE BACK ON THE WEAKER CONDITION: an errored `initialize` has a
    # correlated response, so the run continues into Q3, the control and Q4 with no revision.
    ("F169-the-runs-own-failed-handshake-does-not-stop-it", SESSPROBE,
     "        if not handshake_complete(s):\n            why = session_eligible(s, None)[1]",
     "        if s[\"response\"] is None:\n            why = session_eligible(s, None)[1]",
     ("a run whose OWN handshake failed measures nothing below it — not Q3, not the control, "
      "not Q4 — because a correlated ERROR is still a failed handshake")),
    # ...and the far-end precondition removed, so the two can drift apart in silence again.
    ("F170-a-question-runs-without-a-known-run-revision", SESSPROBE,
     ("    if not version:\n        check(\"Q3: the run's protocol revision is known before "
      "any session is sampled — \""),
     ("    if False:\n        check(\"Q3: the run's protocol revision is known before "
      "any session is sampled — \""),
     ("Q3 and Q4 refuse to run without a known run revision, so main()'s gate and their own "
      "precondition cannot drift apart in silence")),
    # Cleanup back on the run's revision rather than each session's own.
    ("F168-cleanup-releases-every-session-under-one-revision", SESSPROBE,
     "        per_session = ledger.version_for(sid) or version",
     "        per_session = version",
     ("...and cleanup releases each session UNDER that revision — read from what the DELETE "
      "carried, not from what the ledger was asked for")),
    # ...and the half of it that is the notification rather than the response.
    ("F163-an-incomplete-handshake-passes-on-the-response-alone", SESSPROBE,
     ("    return (isinstance(response, dict) and isinstance(response.get(\"result\"), dict)\n"
      "            and bool(session.get(\"version\")) and bool(session.get(\"initialized\")))"),
     ("    return (isinstance(response, dict) and isinstance(response.get(\"result\"), dict)\n"
      "            and bool(session.get(\"version\")))"),
     ("a handshake is complete only with a SUCCESSFUL result, a declared version AND an "
      "accepted `initialized` — any one missing is an incomplete handshake")),
    # A cohort whose handshake never completed still contributes a survival reading.
    ("F164-a-cohort-is-measured-through-an-incomplete-handshake", SESSPROBE,
     "    if _ineligible:\n        for c in _ineligible:",
     "    if []:\n        for c in _ineligible:",
     ("Q4: every cohort completed its handshake and negotiated the same revision — an "
      "idle session that never entered normal operation is a different quantity")),
    # An unset credential variable silently produces no header, so the failure surfaces as an
    # auth error far from its cause.
    ("F158-an-unset-credential-variable-is-skipped-in-silence", SESSPROBE,
     ("            errors.append(f\"--header-env {name!r} names ${var}, which is not set\")\n"
      "            continue"),
     "            continue",
     ("...and an unset or empty variable is a NAMED error, not a silently absent header — a "
      "credential that quietly fails to be set surfaces as an auth failure far from its cause")),
    # THE POSITIVE CONTROL, DELETED. Without it a session never shown to exist is credited to
    # the server as a clean release — the row looks tidiest exactly when it means least.
    ("F140-a-release-is-certified-without-the-session-ever-being-alive", SESSPROBE,
     "    if before != ALIVE:\n        return INDETERMINATE",
     "    if False:\n        return INDETERMINATE",
     "a session never demonstrably ALIVE is INDETERMINATE however tidy the rest looks"),
    # `not ALIVE` instead of `DEAD` — the plausible wrong predicate, which folds the
    # instrument's own silence into the cleanest possible answer.
    ("F141-anything-but-alive-is-read-as-a-release", SESSPROBE,
     "    if after == DEAD:\n        return RELEASED",
     "    if after != ALIVE:\n        return RELEASED",
     ("an UNREADABLE session after the DELETE is INDETERMINATE, not a release — `not ALIVE` "
      "would have published the instrument's own silence as the cleanest possible outcome")),
    # The structural clause §4 asks for ahead of every universal, removed. A run that opened
    # no session at all then publishes agreement.
    ("F142-an-empty-sample-reports-as-agreement", SESSPROBE,
     ('    if not outcomes:\n        return "no sessions were opened, so the sample '
      'establishes nothing", False'),
     ('    if False:\n        return "no sessions were opened, so the sample '
      'establishes nothing", False'),
     ("an EMPTY sample is not agreement — `all()` over a list nothing was put into is true, "
      "and this is the function that stands where that would have been published")),
    # THE C3-3 MISTAKE, WRITTEN OUT. The bound is still censored and still names the horizon,
    # so the first check stays green; only the forbidden reading is added. If that check were
    # decorative this mutation would survive, which is exactly what it is here to prove.
    ("F143-the-censored-bound-also-claims-the-session-never-expires", SESSPROBE,
     '        bound = f"lifetime > {horizon_s}s, censored"',
     '        bound = f"lifetime > {horizon_s}s, censored — it does not expire"',
     "...and never says the session does not expire, which no observation at W supports"),
    # A cohort that produced NO reading, phrased as a survival: it adds a survivor to the
    # record on the strength of nothing having been observed.
    ("F144-an-unreadable-cohort-is-published-as-a-survival", SESSPROBE,
     '    return f"UNREADABLE at {horizon_s}s — no observation, not a survival"',
     '    return f"lifetime > {horizon_s}s, censored"',
     "an UNREADABLE cohort is not a survival and claims no bound"),
    # A server that answered with a revision we refuse, filed as a server that said nothing.
    ("F145-an-unimplemented-revision-reads-as-silence", SESSPROBE,
     "    return ERA_UNKNOWN",
     "    return ERA_NONE",
     "a version outside §10.2's allowlist is UNKNOWN, not silence"),
    # Scope decided on the era alone, so a modern server issuing a session id — the anomaly
    # worth seeing — is filed as ordinary.
    ("F146-a-modern-server-issuing-a-session-is-treated-as-ordinary", SESSPROBE,
     "    return era in (ERA_LEGACY, ERA_UNKNOWN) and bool(session_id)",
     "    return era in (ERA_LEGACY, ERA_UNKNOWN, ERA_MODERN) and bool(session_id)",
     ("a MODERN server is out of scope even when it issues an id — that is the anomaly, and "
      "a lookup on the era alone would call it ordinary")),
    # Discrimination claimed from an empty record: nothing was compared, and the answer is yes.
    ("F147-discrimination-is-claimed-with-nothing-to-compare", SESSPROBE,
     "    return bool(said) and bool(unknown_message) and unknown_message not in said",
     "    return unknown_message not in said",
     "...and no recorded messages is not discrimination — the structural clause again"),
    # The live target answers ordinary POSTs with SSE framing. A reader that assumed JSON
    # reports a conformant server as malformed — and every session reading with it.
    ("F148-an-sse-framed-body-is-parsed-as-plain-json", SESSPROBE,
     '    if "text/event-stream" in (content_type or "") or raw.startswith("event:"):',
     "    if False:",
     ("a JSON body parses, and an SSE-framed one parses to the same thing — a reader that "
      "assumed application/json would report a conformant server as malformed")),
    # `urllib` raises on 4xx. Losing the status here turns the one answer this probe exists to
    # read into "the instrument could not tell", from a server that answered perfectly.
    ("F149-a-404-is-swallowed-as-a-transport-failure", SESSPROBE,
     "        return Reply(exc.code, dict(exc.headers or {}),",
     "        return Reply(None, dict(exc.headers or {}),",
     ("a 404 arrives as a STATUS with its body, not as a transport error — urllib raises on "
      "4xx, and filing that as a failure reports every released session as unmeasurable")),

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
_ALL_LABELS = r"^\s*(?:ok|FAIL)\s+(.+?)(?:\s\s<- .*)?$"


class _Suite(NamedTuple):
    """How to run one suite, and how to read two different things out of its output.

    A NAMED RECORD RATHER THAN A BARE TUPLE, and that is the fix rather than the tidying. The
    third field arrived with the arm-names-a-real-check guard, and three readers had to agree
    about the new shape: two were updated, and `run()` — which said `argv, _ = _SUITES[suite]`
    — was not. So the runner raised `ValueError: too many values to unpack` before its first
    baseline, for BOTH suites that declare a parser, and the guard's own code path had
    therefore never executed once (review, PR #106; found by driving it rather than by reading
    it). A positional record with an optional tail is a shape every reader must re-derive
    independently, which is the same class of mistake as a rule duplicated instead of imported.
    A named one cannot be mis-unpacked and the next field costs no reader a change.
    """

    argv: tuple            # ...appended to the venv interpreter
    failed: str            # the regex naming a check that went RED
    labels: str | None = None   # ...and EVERY label printed, where the suite prints its passes
    source: str | None = None   # ...or the file whose TEXT must contain each arm, where not


# `labels` is None for the selftest and only for it: that suite prints section headings and
# `[FAIL]` lines rather than one line per arm, so its full label set is not recoverable from
# its OUTPUT. It is recoverable from its SOURCE, which is what `source` is for — and the gap
# between those two sentences cost two mutations. Renaming an arm left `M4` naming a check
# that no longer existed; the mutation still ran, still broke the code, and was reported as a
# skip, because the guard that would have caught it only covers suites that print their passes
# (review, PR #106 → the C3 adapter integration). A substring test over the file is cruder than
# reading printed labels and catches exactly the case that matters: an arm nothing names.
_SUITES = {
    "selftest": _Suite(("-m", "agentskill_evals", "selftest"), r"\[FAIL\]\s+([^:]+):",
                       source=SELFTEST),
    "fixtures": _Suite((VERIFIER,), r"^\s*FAIL\s+(.+?)\s\s<- ", _ALL_LABELS),
    "proxy": _Suite((PROXY_VERIFIER,), r"^\s*FAIL\s+(.+?)\s\s<- ", _ALL_LABELS),
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


def command_for(cwd, suite):
    """The argv `run` will spawn for `suite`, under `cwd`'s venv.

    A FUNCTION RATHER THAN AN EXPRESSION INSIDE `run`, so it can be driven without paying for
    a suite. This is the exact line that raised `ValueError` for every fixture and proxy
    mutation, and the reason it reached a push is that nothing but a 63-minute run ever
    executed it — a tool whose own readers are only exercised by its slowest path is one whose
    breakage is discovered late by definition. §4's rule about probes applies to the runner
    too: keep the logic in named functions so a check can drive it on synthetic input.
    """
    return [str(cwd / ".venv/bin/python"), *_SUITES[suite].argv]


_TIMEOUT_OUTPUT = "__TIMEOUT__"
# How often the waiter re-asks whether the suite has exited. It is not a busy loop at this
# interval — a 40s proxy suite costs ~800 wakeups — and it bounds how late a TIMEOUT is
# noticed, which against a 300s ceiling is noise.
_POLL = 0.05


class Outcome(NamedTuple):
    """One suite run, on both clocks.

    TWO TIMES RATHER THAN ONE, and they answer different questions. `wall` is what
    `_SUITE_TIMEOUT` is measured against, so it is the number to read for headroom. `cpu` is
    what the run actually spent on a core, so it is the number that survives a loaded machine —
    and under `--jobs N` the machine is loaded BY THIS PROGRAM, which makes the wall figure a
    statement about the scheduler rather than about the mutation.
    """

    returncode: int
    output: str
    wall: float
    cpu: float
    # Pids the timeout path could not get rid of. Empty on every path that did not time out, and
    # empty on almost all that did — a non-empty one is a leak the run says out loud rather than
    # notes, because a process is executing out of a directory this program is about to hand to
    # the next mutation and then delete. Neither of those now happens: see `contaminated`, which
    # is the one predicate every reader of these two fields asks, and `discard`. This comment
    # described the consequence for a while after the sweep had learned to report it and before
    # anything acted on it, which is the state a contract is least useful in.
    leftover: tuple = ()
    # ...and why the sweep could not be trusted, "" when the observer answered. A SECOND FIELD
    # because it is a second fact: "nothing was left behind" and "nothing could be looked at"
    # are both compatible with an empty `leftover`, and collapsing them would let a blind `ps`
    # certify a process tree nobody enumerated. Each is separately enough to condemn the tree.
    containment: str = ""


def _exit_code(status):
    """A `wait4` status as subprocess spells it: negative for a signal, else the exit code."""
    return -os.WTERMSIG(status) if os.WIFSIGNALED(status) else os.WEXITSTATUS(status)


class Proc(NamedTuple):
    """One row of the process table.

    A NAMED RECORD RATHER THAN A BARE TUPLE, for the reason `_Suite` gives further down: the
    `state` field arrived late, three readers had to agree about the new shape, and a positional
    row is one every reader re-derives independently. It is also the field the freeze turns on —
    dropped after the zombie test until a reviewer showed the fixed point needed it.
    """

    ppid: int
    state: str
    command: str


def is_stopped(proc):
    """Whether `ps` says this process is actually stopped, rather than merely signalled.

    THE DIFFERENCE IS THE WHOLE RACE. `os.kill` returns when the signal is QUEUED; the target
    keeps running until the kernel delivers it, and a target that is still running can still
    fork. A fixed point over pids we have SIGNALLED is therefore not a fixed point over pids
    that have STOPPED, and the gap between them is exactly wide enough for a late child to be
    born, be orphaned by the kill, and carry no argv anyone can find it by (review, PR #111).

    `T` is stopped on both supported platforms; Linux additionally spells a traced stop `t`.
    Flags may follow the letter (`T+`, `TN`), so this reads the first character only.
    """
    return proc.state[:1] in ("T", "t")


class ObserverFailed(Exception):
    """`ps` did not answer, or answered without having enumerated the machine.

    NOT THE SAME FACT AS `ps` ANSWERING "NOTHING", and that is the whole reason this is an
    exception rather than an empty table. Everything downstream reads absence as proof — the
    sweep decides it is finished when it sees no owned processes — so an observation channel
    that was never connected reports the same silence as one reporting success, and the
    timeout path would certify a tree it never looked at.

    `verify_mcp_proxy.py` learned this in PR #109, with `ps` denied: rc 127, and the verifier
    printed ALL PASS including "the mute guardian goes". This file then grew a SECOND process
    observer without the rule, and a reviewer reproduced all three modes against it (PR #111).
    There is now one observer and one rule; the proxy verifier reads this function rather than
    keeping a copy that can drift from it.
    """


def process_tree():
    """Every live pid, with its parent and command line. Raises rather than answering blind.

    THREE WAYS IT CAN FAIL AND ALL THREE RAISE, including the two that look like answers. `ps`
    missing or denied is an `OSError` and would otherwise escape from the middle of a teardown;
    a non-zero exit with empty stdout parses as an empty machine, which reads as "nothing to
    sweep"; and a `ps` that cannot see THIS process has not enumerated the machine whatever it
    exited with, so what it says about any other pid is not evidence. That last one is a
    POSITIVE fact about the channel, obtained before any conclusion is drawn from its silence.

    ZOMBIES ARE EXCLUDED, AND THAT IS A FACT RATHER THAN A FILTER. A dead process awaiting its
    reap holds no descriptors, spawns nothing, and its children were reparented when it died.
    Counting one as a survivor would make the timeout path report a leak it had actually
    cleaned up, and spend its whole deadline doing it. The self-witness is checked BEFORE that
    exclusion, since this process is not a zombie and must be visible either way.
    """
    try:
        done = subprocess.run(["ps", "-eo", "pid=,ppid=,stat=,command="],  # noqa: S607 — PATH
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ObserverFailed(f"`ps` did not run: {exc!r}") from exc
    if done.returncode != 0:
        raise ObserverFailed(f"`ps` exited {done.returncode}: {done.stderr.strip()[:200]!r}")
    table, seen = {}, set()
    for line in done.stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[0].isdigit() and parts[1].isdigit():
            pid, ppid, state, command = int(parts[0]), int(parts[1]), parts[2], parts[3]
            if pid <= 0:
                continue        # macOS lists kernel_task as pid 0; `_signal` would refuse it
            seen.add(pid)
            if not state.startswith("Z"):
                table[pid] = Proc(ppid, state, command)
    if os.getpid() not in seen:
        raise ObserverFailed(f"`ps` answered without listing this process ({os.getpid()}) among "
                             f"its {len(seen)} row(s), so it did not enumerate the machine")
    return table


class Sweep(NamedTuple):
    """What one sweep achieved, as two facts rather than one.

    `leftover` is what was still alive when it gave up; `fault` is why the sweep could not be
    trusted at all — a broken observer, or a tree that would not stop spawning. They are not
    alternatives: an empty `leftover` means "nothing survived" only when `fault` is empty too,
    and the reading that collapses them is the one that certifies a tree nobody enumerated.
    """

    leftover: tuple = ()
    fault: str = ""


def _signal(pid, sig=signal.SIGKILL):
    """Signal one process. THE ONLY PLACE THIS FILE SIGNALS ANYTHING, and it refuses a pid
    that is not a process.

    `kill(-1, ...)` IS DEFINED AS "EVERY PROCESS THIS USER MAY SIGNAL", and `kill(0, ...)` as
    "this process's whole group". Neither is a sentinel; both are broadcasts, and the operating
    system will not ask whether one was meant. This function exists because a `-1` used
    elsewhere as a harmless-looking "no root" placeholder reached `os.kill` under a mutation and
    took down every application the user had open (2026-08-12).

    IT IS THE PRIMITIVE THE MUTATIONS GO THROUGH TOO. A mutation suite runs deliberately broken
    versions of this file, so a destructive call is only as contained as its WORST reachable
    variant — which means the containment cannot live in the callers, and a mutation aimed at a
    kill path has to be written through this function rather than around it.
    """
    if not isinstance(pid, int) or pid <= 0:
        raise ValueError(f"refusing to signal {pid!r}: a pid must be positive, and a "
                         f"non-positive one is a broadcast rather than a process")
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass                         # already gone, or never ours to begin with


def _kill_all(pids, sig=signal.SIGKILL):
    """Signal every one of `pids`. Returns the ones that could not be signalled at all.

    EVERY MEMBER IS ATTEMPTED, INCLUDING AFTER ONE OF THEM RAISES, which a plain loop does not
    do: the first failure abandons the rest of the set. In a teardown that is the whole point of
    the teardown being lost, and here it is worse than a leak — everything this sweep reaches
    has been SIGSTOPped, and a stopped process nobody kills never exits, so the `wait4` the
    teardown exists to make safe would block on it forever (review, PR #111).

    THE SAME SHAPE HAS COST THIS PROGRAM A GUARDIAN. `sweep()` in `mcp_proxy_io.py` announced
    itself before signalling; with the CLI's end of stderr closed the announcement raised
    `BrokenPipeError` and a credential-bearing group was left alive by its own log line
    (PR #103). One step of a cleanup failing is not permission to skip the next.

    NOTHING IS RAISED OUT OF HERE, for the same reason: this runs while something else may
    already be unwinding, and an exception from a cleanup path replaces the failure that caused
    it. `_signal` already treats a process that is gone or was never ours as done, so what
    reaches these handlers is a pid it REFUSED — a broadcast target, see its docstring — or an
    errno nobody expected. Both are facts the caller reports rather than trips over.
    """
    unreachable = []
    for pid in sorted(pids):
        try:
            _signal(pid, sig)
        except (ValueError, OSError):
            unreachable.append(pid)
    return tuple(unreachable)


def owned_pids(table, root=None, marker=None):
    """Everything one suite run owns: the `root`'s descendants, plus anything naming `marker`.

    NEITHER SELECTOR HAS A MAGIC NUMBER. "No root" is `None`, exactly as "no marker" is — a
    value that cannot be a pid, cannot be an `in` operand, and cannot be handed to `os.kill`.
    An earlier version used `-1` for it, on the reasoning that no process has that parent. That
    is true and it is not the point: the value did not stay inside the lookup it was invented
    for, and `kill(-1)` is the most destructive call on the system.

    TWO INDEPENDENT REASONS FOR MEMBERSHIP, because each is blind exactly where the other sees.

    The PPID CHAIN is the complete answer while the chain exists, and it is the only one that
    reaches a process whose argv says nothing about us. It reaches through a `setsid` — a new
    session changes the group, never the parent — which is what makes it, and not a process
    group, the right instrument here: the proxy's guardian is spawned `start_new_session=True`
    on purpose, and the escaping helper in §10.9 calls `setsid` on purpose, so a `killpg` aimed
    at the suite misses precisely the two processes this exists to catch.

    The MARKER IS SUPPLEMENTARY EVIDENCE, and nothing rests on it. It recovers SOME of what
    the chain no longer reaches — the instant the root is signalled its descendants are
    reparented and every link above them is gone — and where it does, the work tree's path is a
    fresh `mkdtemp` no other process on the machine can be carrying, so a match is conclusive.

    A MISS IS NOT, AND THE EARLIER CONTRACT CLAIMED OTHERWISE. It said every process a suite
    starts carries that path. Measured over one run of each suite, 5 of 7 selftest descendants,
    35 of 49 fixture descendants and 67 of 169 proxy descendants did not: `node`, `git`, and
    above all processes caught BETWEEN FORK AND EXEC, which `ps` shows as a bare `(python3.10)`
    with no argv at all — the state a just-forked child is in, which is exactly the child a
    race produces. Containment therefore rests on `kill_owned` freezing the tree while
    parentage still connects it, and this arm only adds to what that already reached.

    "NO MARKER" IS `None`, AND THE EMPTY STRING IS REFUSED. `"" in command` is true of every
    process alive, so a marker arm switched off by falsiness puts one boolean between this
    function and SIGKILLing the machine — and this file's whole purpose is to run versions of
    itself with a line removed. `None` is not a boolean weaker than that; it is a value `in`
    cannot accept, so the mutation that deletes the guard raises `TypeError` and kills nothing,
    where the mutation that deletes a `if marker:` would have swept the host. The empty string
    is then refused outright rather than treated as `None`, because a caller that computed one
    by accident meant a path and got nothing.
    """
    if marker == "":
        raise ValueError("an empty marker matches every process alive, not none of them; pass "
                         "None to sweep by parentage only")
    if root is not None and (not isinstance(root, int) or root <= 0):
        raise ValueError(f"refusing to sweep from root {root!r}: pass None for no root, never "
                         f"a non-positive pid, which every signalling call reads as a broadcast")
    children = {}
    for pid, proc in table.items():
        children.setdefault(proc.ppid, []).append(pid)
    owned, frontier = set(), [root]
    while frontier:
        pid = frontier.pop()
        for kid in children.get(pid, ()):
            if kid not in owned:
                owned.add(kid)
                frontier.append(kid)
    if marker is not None:
        owned |= {pid for pid, proc in table.items() if marker in proc.command}
    if root in table:
        owned.add(root)
    # NEVER OURSELVES, on either arm. The driver is not in the tree it sweeps and its argv does
    # not name a work tree, so this cannot fire today — it is here because the cost of being
    # wrong about that once is the run killing the process doing the killing.
    owned.discard(os.getpid())
    return owned


def survivors(frozen, table, root=None, marker=None):
    """Which of the pids we FROZE are still alive — asked of the set, never of the machine.

    A NAMED FUNCTION so the case that distinguishes it can be driven synthetically. It only
    ever matters when a sweep has actually left something behind, which no live case can
    arrange on demand: SIGKILL works, and a survivor is by definition the thing that did not
    happen. Driven on a table it is trivial, and the mutation that re-enumerates comes back
    CAUGHT instead of MISSED (review, PR #111).

    RE-ENUMERATING IS THE DEFECT. After the kill the survivors are orphans: parentage no longer
    reaches them because the root is gone, and the marker may never have named them — more than
    a third of real descendants carry no work-tree path, and one caught between fork and exec
    carries no argv at all. So asking the machine again answers "clean" about exactly the
    processes that got away, which is the one answer this must not be able to give.

    `root` and `marker` are accepted and unused for that reason: they are what a re-enumeration
    would need, and their presence in the signature is what lets the mutation be written.
    """
    return tuple(sorted(pid for pid in frozen if pid in table))


def kill_owned(root, marker=None, deadline=5.0):
    """Stop everything one suite run owns, then kill it. Returns a `Sweep`.

    FREEZE FIRST, KILL SECOND, and the order is the whole mechanism. An earlier version
    enumerated and killed in one pass, trusting the marker to recover anything spawned in
    between — on the claim that every process a suite starts carries its work tree's path in
    its argv. THAT CLAIM IS FALSE, and measurably so: over one run of each suite, 5 of 7
    selftest descendants, 35 of 49 fixture descendants and 67 of 169 proxy descendants had no
    such path — `node`, `git`, and above all processes caught BETWEEN FORK AND EXEC, which
    `ps` shows as a bare `(python3.10)` with no argv at all (review, PR #111).

    That last case is what makes the marker not merely incomplete but backwards: the processes
    it cannot name are exactly the just-forked ones, which are exactly the ones the race is
    about. So this no longer races. `SIGSTOP` is unblockable and a stopped process cannot
    fork, so each round freezes what parentage can still reach and the next round catches only
    what those had already forked before they stopped. The set therefore shrinks to nothing,
    and once it does, NOTHING IN THE TREE CAN CREATE ANYTHING — the kill that follows is
    against a fixed population rather than a moving one.

    WHAT IS REPORTED IS THE SET THIS FUNCTION FROZE, not a re-enumeration. After the kill the
    survivors are orphans, unreachable by parentage and possibly unnamed by the marker, so
    asking the machine again would answer "clean" about exactly the processes that got away.

    A TREE THAT WILL NOT QUIESCE IS A NAMED FAILURE, not a silent one. Everything frozen is
    killed on every path, including that one and including an exception, because a process left
    stopped is worse than one left running: it never exits, and the `wait4` above would block
    on it forever. That sentence rests entirely on WHEN `frozen` is written and on `_kill_all`
    finishing its loop — a registry updated after the act names nothing the act already touched,
    and a cleanup that stops at its first failure abandons the rest of what it was registered
    for. Both were true here, and the second is why the kills go through `_kill_all`.

    ON THE TIMEOUT PATH ONLY, and never after a run that finished. A sweep after a clean run
    would be tidying away exactly the survivors the proxy verifier exists to notice, and a green
    suite would then certify a leak this function had quietly cleaned up on its behalf.
    """
    end = time.monotonic() + deadline
    frozen: set[int] = set()      # ours, and registered for the kill below BEFORE being stopped
    confirmed: set[int] = set()   # ...and since SEEN stopped, so safe to lose
    lost: set[int] = set()        # ...but gone before that, so unaccounted for
    unreachable: tuple = ()       # ...and refused by `_signal`, so stopped and unkillable
    quiesced = False
    try:
        while True:
            table = process_tree()
            fresh = owned_pids(table, root, marker) - frozen
            # SIGNALLED IS NOT STOPPED, and a fixed point over the wrong one of those is not a
            # fixed point at all. `os.kill` returns once the signal is queued; until the kernel
            # delivers it the target is still running and can still fork. Concluding on "no new
            # pids" therefore concluded on a snapshot taken while the root was free to produce
            # one more child — orphaned by the kill that followed, markerless, and invisible to
            # every later scan (review, PR #111). So the loop also requires every pid it has
            # signalled to be OBSERVED stopped, from the same `ps` state it already reads.
            #
            # CONFIRMATION IS TRACKED SEPARATELY FROM PRESENCE, because "gone" is not an answer.
            # Intersecting `frozen` with the live table made a pid that DISAPPEARED satisfy the
            # fixed point: signalled, then exited before the next look — and a process that
            # exits may have forked on the way out, reparenting a child that no longer has a
            # link to anything and may carry nothing in its argv to name it. Driven by review:
            # snapshot root, SIGSTOP, next snapshot holds only orphan 101, and the sweep
            # returned a clean `Sweep` having never signalled it (PR #111).
            #
            # Once a pid has been SEEN stopped it is safe to lose: it cannot have forked after
            # the observation, and anything it forked before appears as `fresh` on this or a
            # later look. Before that, its disappearance is simply unaccounted for, and no
            # amount of looping can resolve it — the process is gone. So it is terminal, and it
            # is reported rather than waited on.
            confirmed |= {pid for pid in frozen & table.keys() if is_stopped(table[pid])}
            lost |= (frozen - confirmed) - table.keys()
            pending = (frozen & table.keys()) - confirmed
            if not fresh and not pending:
                quiesced = True
                break
            # REGISTERED BEFORE IT IS SIGNALLED, never after. `frozen` is the only thing the
            # `finally` below can reach, so it has to name a process from before the moment that
            # process becomes stopped — not from after the whole batch is. Updating it after the
            # loop meant a failure partway through left everything already SIGSTOPped in this
            # round unregistered, and therefore unkilled: two pids stopped, one raise, and a
            # cleanup that ran over an empty set (review, PR #111). A pid registered and then
            # never signalled costs a SIGKILL against something that was ours anyway; a pid
            # signalled and never registered is stopped forever, and `wait4` blocks on it.
            frozen |= fresh
            # Re-signalling a pending one is free and covers a signal that was lost to a race
            # with its own exec; a stop delivered twice is still one stop.
            for pid in sorted(fresh | pending):
                _signal(pid, signal.SIGSTOP)
            if time.monotonic() >= end:
                break
            time.sleep(_POLL)
    finally:
        unreachable = _kill_all(frozen)
    left = tuple(sorted(frozen))
    while left:
        left = survivors(frozen, process_tree(), root, marker)
        if not left or time.monotonic() >= end + deadline:
            break
        unreachable += _kill_all(left)
        time.sleep(_POLL)
    reasons = []
    if not quiesced:
        reasons.append(f"the process tree did not stop spawning within {deadline:.0f}s: "
                       f"{len(frozen)} frozen and killed, but something in it was still "
                       f"creating children")
    if lost:
        reasons.append(f"{sorted(lost)} vanished before being observed stopped, so anything "
                       f"forked between the signal and the exit is reparented and unnamed")
    if unreachable:
        reasons.append(f"{sorted(set(unreachable))} could not be signalled at all, so they are "
                       f"stopped rather than killed and will never exit on their own")
    return Sweep(tuple(left), "; ".join(reasons))


def _await(proc, timeout, marker=None):
    """Reap `proc` and everything it owns, as (status_or_None, cpu, leftover).

    A `None` status means it outlived `timeout`; `leftover` is what the sweep could not kill,
    and is empty on every path that did not time out. `marker` scopes the sweep to one work
    tree — see `owned_pids`, where `None` sweeps by parentage only and `""` is refused.

    `os.wait4` RATHER THAN `subprocess.run`, and the reason is arithmetic rather than taste.
    Per-child CPU has to come from the wait that reaps THAT child: `getrusage(RUSAGE_CHILDREN)`
    is a running total for the whole process, so a delta around one suite is that suite's CPU
    plus whatever the other seven workers happened to finish inside the same window. Under
    `--jobs 1` the two agree, which is exactly why the mistake would have survived review.

    What `cpu` covers is the suite process and every descendant IT reaped — so the proxy and
    fixture servers a suite starts and waits for are included, and a process the suite LEAKED
    is not. That is the right boundary for this purpose: a leak is a finding for the survivor
    check, not a line item in a runtime budget.

    Popen's own bookkeeping is bypassed on purpose. `proc.kill()` calls `poll()` first, which
    would reap the child itself and leave the blocking `wait4` below raising `ChildProcessError`
    on a pid nothing can wait for twice; `returncode` is assigned at the end instead, which is
    what stops `__del__` reporting a still-running child.
    """
    deadline = time.monotonic() + timeout
    while True:
        pid, status, usage = os.wait4(proc.pid, os.WNOHANG)
        if pid == proc.pid:
            proc.returncode = _exit_code(status)
            return status, usage.ru_utime + usage.ru_stime, (), ""
        if time.monotonic() >= deadline:
            break
        time.sleep(_POLL)
    # THE WHOLE TREE, NOT THE PROCESS WE HAPPEN TO HOLD A HANDLE ON. Killing only the suite was
    # a containment gap with teeth under `--jobs N`: these suites spawn proxies, guardians,
    # fixture servers and helpers, and a mutation that hangs before its cleanup left every one
    # of them running — outliving the `rmtree` of the tree they were launched from, and alive
    # beside the workers still running (review, PR #111). Reproduced directly: a suite that
    # spawned a 60s child and timed out was reaped at `-SIGKILL` with the child still alive.
    #
    # THE ROOT DIES AND IS REAPED WHATEVER HAPPENS TO THE SWEEP, which is what the `finally` is
    # for and not tidiness. A denied `ps` raises out of the sweep, and without this the suite
    # process was never signalled and never waited for: the runner would then block forever on
    # a `wait4` for a child still happily running, one worker down, with no output saying why
    # (review, PR #111). Losing the descendants is a containment failure; losing the ROOT as
    # well would be a hang, and the whole point of `_SUITE_TIMEOUT` is that nothing hangs.
    #
    # AND THE FAILURE IS CARRIED SEPARATELY FROM THE RESULT. "No leftovers" and "could not
    # look" are different facts about the same ending, so they get different fields — an empty
    # `leftover` under a blind observer would certify a tree nobody enumerated, which is the
    # exact reading this whole function exists to refuse.
    #
    # AND THE KILL GOES THROUGH `_kill_all` SO THE REAP CANNOT BE SKIPPED. These are two
    # independent cleanups of one child, and a raise from the first took the second with it —
    # leaving a process nothing waits for and a `returncode` nothing sets, which surfaces as
    # `Popen.__del__` complaining rather than as a run reporting anything. Its answer is not
    # consulted on purpose: a pid that cannot be signalled is one `wait4` never returns for, so
    # a fault built from it would never reach a printer. The call is here for the property, not
    # for the value.
    leftover, fault = (), ""
    try:
        swept = kill_owned(proc.pid, marker)
        leftover, fault = swept.leftover, swept.fault
    except ObserverFailed as exc:
        fault = str(exc)
    finally:
        _kill_all([proc.pid])
        _pid, status, usage = os.wait4(proc.pid, 0)
        proc.returncode = _exit_code(status)
    return None, usage.ru_utime + usage.ru_stime, leftover, fault


def run(cwd, suite):
    """Run one suite in `cwd`, on both clocks.

    Wall time is measured with a monotonic clock so a wall-clock adjustment mid-suite (a run
    this long can straddle one) cannot produce a negative or wildly inflated duration.

    OUTPUT GOES TO A FILE, NOT A PIPE. Reading a pipe means reading it until EOF, and EOF on a
    suite's stdout is every process holding it — including one the suite leaked, which is a
    thing these suites deliberately arrange. A file cannot deadlock, and it is what makes the
    timeout above a bound on the SUITE rather than a bound on whatever outlived it.
    """
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as sink:
        t0 = time.monotonic()
        proc = subprocess.Popen(command_for(cwd, suite), cwd=cwd,   # noqa: S603 — our own venv
                                stdin=subprocess.DEVNULL, stdout=sink, stderr=subprocess.STDOUT)
        # The work tree's own path is what scopes the sweep if this times out. Every process a
        # suite starts is launched by absolute path out of this directory, and the directory is
        # a fresh `mkdtemp` belonging to one worker — so it names this run's descendants and
        # cannot name a sibling worker's.
        status, cpu, leftover, fault = _await(proc, _SUITE_TIMEOUT, str(cwd))
        wall = time.monotonic() - t0
        if status is None:
            return Outcome(124, _TIMEOUT_OUTPUT, wall, cpu, leftover, fault)
        sink.seek(0)
        return Outcome(proc.returncode, sink.read(), wall, cpu)


def failed_checks(suite, out):
    """The named checks that went red, read with the parser belonging to `suite`.

    Extracted rather than inlined so it can be driven on captured output: a parser that
    silently matches nothing turns every catch into "failed, but NOT via", which looks like
    a coverage problem and is a tooling one.
    """
    return re.findall(_SUITES[suite].failed, out, re.MULTILINE)


# The ways one mutation can end. Named rather than spelled into the printed line at each site,
# because the exit status and the output are two readers of the same fact and a run that prints
# MISSED while returning 0 is the disagreement §4's trustworthy-predicate rule is about. Only
# CAUGHT counts, and everything else therefore fails the run — including the two that are not
# statements about the mutation at all.
CAUGHT = "CAUGHT"
MISSED = "MISSED"
NOT_VIA = "NOT-via"
TIMEOUT = "TIMEOUT"
UNAPPLIED = "UNAPPLIED"        # stale or ambiguous anchor: the suite never ran
ABANDONED = "ABANDONED"        # the run had already stopped: the suite never ran, for a reason
                               # that is about an earlier mutation rather than about this one
INCONCLUSIVE = "INCONCLUSIVE"  # ...and this one DID run, but overlapped that reason: the suite
                               # executed beside a tree nobody could account for, so whatever
                               # it reported is a measurement rather than a result


def verdict(outcome, arm, failed):
    """How one mutation ended, from its run and the checks that went red.

    A FUNCTION RATHER THAN THE `elif` CHAIN IT REPLACES, for the reason §4 gives about probes
    and which is no weaker about the thing that scores them: a classifier inside `main()` can
    only be exercised by paying for a suite, so the one case nobody arranges is the one nobody
    finds. Every branch here is reachable from a synthetic `Outcome`.

    TIMEOUT IS ASKED FIRST AND IT IS NOT A TIE-BREAK. A timed-out run carries `returncode` 124
    and no parsable output, so both later branches would answer it — MISSED, in the reading
    that matters, since `arm in failed` over an empty list is false and 124 is not 0. A hang is
    uncaught, but calling it MISSED would say the defect was present and every arm passed, when
    what happened is that no arm got to report.
    """
    if outcome.output == _TIMEOUT_OUTPUT:
        return TIMEOUT
    if outcome.returncode == 0:
        return MISSED
    return CAUGHT if arm in failed else NOT_VIA


def containment_note(outcome):
    """What a timed-out sweep left behind, as text. "" when it left nothing and could look.

    ONE TEXT WITH TWO READERS, and until this was extracted the second read nothing. The
    per-mutation line said what survived and what could not be looked at; the BASELINE timeout
    printed "the suite hung" and dropped both — so the one timeout that ends a run before any
    mutation has run was the one that said least about what it left running (review, PR #111).

    BOTH FACTS, NEVER WHICHEVER IS SET. A sweep that did not finish and a sweep that could not
    look are true of the same ending, so they concatenate rather than choose: a lookup on
    whichever matched first would drop the other, which is the reading `Sweep` exists to refuse.
    """
    # "Killed the tree" is a claim read off an absence, so the case where the absence is not
    # there has to be printed rather than swallowed: whatever is still alive was launched from
    # a work tree, and that tree is neither reusable nor deletable while it is.
    stuck = (f" — {len(outcome.leftover)} descendant(s) SURVIVED the sweep: "
             f"{list(outcome.leftover)}" if outcome.leftover else "")
    # AND A SWEEP THAT COULD NOT LOOK IS NOT A SWEEP THAT FOUND NOTHING. Printed on its own
    # terms, because the difference is the whole content of the containment claim: with a blind
    # observer the descendants of this hung suite are unaccounted for rather than gone.
    blind = (f" — CONTAINMENT NOT ESTABLISHED, descendants unaccounted for: "
             f"{outcome.containment}" if outcome.containment else "")
    return f"{stuck}{blind}"


def contaminated(outcome):
    """Whether this run left its work tree unsafe to hand to the next mutation.

    THE ONE PREDICATE EVERY READER ASKS, because there are now three of them and they were
    about to disagree. `Outcome` carries two independent facts — pids the sweep could not kill,
    and a reason the sweep could not be believed — and each is separately enough to mean a live
    process is executing out of that directory. A caller that consulted one would hand the tree
    back on the other (review, PR #111), which is §4's rule about a new way of being
    untrustworthy joining the existing predicate rather than becoming a second flag.

    A DISJUNCTION, WHICH IS THE SAME SHAPE AS THE CONJUNCTION IT MIRRORS: "safe" needs both
    facts absent, so "unsafe" needs only one present. Reading `leftover` alone is the tempting
    half, and it is the half that certifies a tree nobody was able to enumerate.
    """
    return bool(outcome.leftover) or bool(outcome.containment)


def result_line(mid, suite, arm, outcome, failed):
    """The line one finished mutation prints, verdict included.

    Built beside `verdict` and from its answer, so the two cannot disagree about what happened
    — the printed text used to be chosen by a second `elif` chain over the same conditions.
    """
    took = f"({outcome.wall:.1f}s wall, {outcome.cpu:.1f}s cpu)"
    kind = verdict(outcome, arm, failed)
    if kind == TIMEOUT:
        # Not a clean catch: the arm never got to report because the suite hung. A mutation
        # whose defect is an infinite loop must be caught by an arm that BOUNDS the work (a
        # thread + join), not by the suite's own timeout — so this counts as uncaught and fails
        # the run, forcing a real fix rather than masking the hang.
        return (f"{mid}: *** TIMEOUT *** {suite} exceeded {_SUITE_TIMEOUT}s — the defect hangs "
                f"rather than reddening {arm} {took}{containment_note(outcome)}")
    if kind == CAUGHT:
        return f"{mid}: CAUGHT by {arm} {took}"
    if kind == NOT_VIA:
        return f"{mid}: failed, but NOT via {arm} -> {failed} {took}"
    return f"{mid}: *** MISSED *** {suite} still passes with the defect present {took}"


def parse_jobs(argv):
    """How many mutations to run at once, from the command line. 1 unless asked.

    RAISES `ValueError`, NOT `SystemExit`, so the refusals below can be driven by a check
    without ending the driver's own process — `SystemExit` is a `BaseException` and goes
    straight past the `except Exception` that a driver wraps a call in. `main` is what turns
    one into an exit status.

    Refusing an unknown argument rather than ignoring it: a typo'd `--job 8` that silently ran
    serially would look exactly like a machine that did not speed up.
    """
    jobs, rest = 1, list(argv)
    while rest:
        arg = rest.pop(0)
        if arg == "--jobs":
            if not rest:
                raise ValueError("--jobs needs a number, e.g. `--jobs 8`")
            jobs = _positive(rest.pop(0))
        elif arg.startswith("--jobs="):
            jobs = _positive(arg.split("=", 1)[1])
        else:
            raise ValueError(f"unknown argument {arg!r}; the only one is `--jobs N`")
    return jobs


def _positive(text):
    if not text.isdigit() or int(text) < 1:
        raise ValueError(f"--jobs needs a positive whole number, not {text!r}")
    return int(text)


def make_worktree(src, dst):
    """A copy of `src` at `dst` that a mutation bites, SHARING the venv rather than copying it.

    THE VENV IS 520MB OF A 531MB TREE, and copying it never bought the isolation it looks like
    it buys. That venv holds an editable install of `agentskill_evals` whose finder hardcodes
    the ORIGINAL tree's absolute path, so the copy points where the original does. What makes a
    mutation bite is something else entirely: the suite runs with `cwd` at the work tree, and
    `-m` puts the cwd at `sys.path[0]`, ahead of the editable finder — which `install()`
    APPENDS to `sys.meta_path`, behind the ordinary path finder. Measured both ways before this
    was changed, and `worktree_binds` is what stops the argument being load-bearing.

    So the copy cost 2.9s and half a gigabyte per worker to duplicate a directory nothing
    resolves through, which at `--jobs 8` is 23s and 4GB. Shared, a tree is ~0.05s and 11MB.
    Sharing is safe under parallelism for the same reason it is pointless: nothing writes to it.
    """
    src, dst = Path(src), Path(dst)
    shutil.copytree(src, dst, symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__", "artifacts", "build", ".venv"))
    os.symlink(src / ".venv", dst / ".venv")
    return dst


def worktree_binds(work):
    """Where `agentskill_evals` resolves for a suite run inside `work` — asked, not assumed.

    THE ONE THING A WORK TREE HAS TO DO, and the only failure mode of the sharing above that
    would not announce itself. A tree that resolved to the ORIGINAL package would run unmutated
    code and report MISSED for every entry — which fails the run, but an hour later and while
    reading like a total loss of coverage rather than like a broken tree. Asked once per tree,
    for ~0.2s, and answered by the interpreter that will run the suites rather than by an
    argument about `sys.meta_path`.
    """
    done = subprocess.run([str(work / ".venv/bin/python"), "-c",   # noqa: S603 — our own venv
                           "import agentskill_evals as m; print(m.__file__)"],
                          cwd=work, capture_output=True, text=True, timeout=120)
    return done.stdout.strip()


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
    if rel in (VERIFIER, PROXY_VERIFIER):
        raise SystemExit(
            f"mutation {mid!r} targets {rel}, which is a suite rather than something a suite "
            f"checks. Asking a verifier whether it notices being mutated establishes nothing "
            f"about it: the mutated program and the program judging the mutation are the same "
            f"one.")
    # `SELF` USED TO BE REFUSED HERE TOO, on that same argument, and the argument stopped
    # applying: `verify_mcp_fixtures.py` §E17 now drives this runner's suite-readers, and it is
    # a DIFFERENT program, executed from the mutated copy while the runner doing the judging
    # keeps running from the original tree. That separation is the whole content of the rule —
    # a claim and the thing it claims about must not have the same author — and it holds here.
    # It was worth reopening because the alternative had a cost: the `_SUITES` record grew a
    # field, `run()` was not updated with it, and the runner could not start EITHER verifier
    # suite for a full push, with nothing able to say so (review, PR #106).
    #
    # WHAT IS STILL INADMISSIBLE, and the line is not the file: this runner's own SCORING —
    # `_classify`, the caught/missed accounting, the anchor guards — is executed only by the
    # unmutated original, so a mutation of it would be judged by the code it perturbs and
    # would report MISSED for a defect nothing ever ran. Aim at what §E17 drives, or write the
    # check first.
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


def _canonical_mid(mid):
    """The identifying part of a mutation id: its class letter and number, e.g. `M338`.

    The rest of the string is a description, and comparing it is what let the FIRST cut of
    this guard miss the exact collision it was written for: `M338-marker-scan-narrows...`
    and `M338-the-phase-marker-is-never-written` are different strings, so a whole-string
    compare called them distinct and reported a clean table (external review). The number
    is the name — it is what a result line carries, what `stale_anchors` prints, and what
    anyone types to talk about one entry.
    """
    head = mid.split("-", 1)[0]
    return head if head[:1] in "MIF" and head[1:].isdigit() else mid


def duplicate_ids(mutations):
    """Every `(id, count)` used by more than one entry, worst first.

    THE SAME ARGUMENT AS `stale_anchors`, one field over, and it was learned the same way:
    four entries were added as M338-M341 while M338-M341 already existed against the proxy.
    Every one of the eight ran and every one was CAUGHT, so both totals were right and the
    suite exited 0 — the failure is not in the arithmetic. It is that "M340: CAUGHT by ..."
    now names two different defects, so a reader cannot get from a result line back to the
    entry that produced it, and `--only M340` would silently mean one of them. An id is a
    NAME; the anchor guard exists because a mutation that could match two sites is not a
    measurement, and an id that can mean two mutations is not a report.

    Cheap enough to run before the baseline, like every other refusal here, because
    discovering it after the fact costs the whole run — which is exactly what it cost.
    """
    seen = {}
    for mid, *_rest in mutations:
        seen[_canonical_mid(mid)] = seen.get(_canonical_mid(mid), 0) + 1
    return sorted(((mid, n) for mid, n in seen.items() if n > 1),
                  key=lambda pair: (-pair[1], pair[0]))


def stale_anchors(root, mutations):
    """Every `(id, target, occurrences)` whose `find` text is not in its target exactly once.

    THE SAME ARGUMENT AS `_classify`, and it was learned the expensive way. A stale anchor was
    already detected — but only when the loop REACHED that mutation, which for an entry two
    thirds of the way down a 387-mutation list is 60 minutes in. Worse, the failure it reports
    is silent about its own cause: `credential_arrived` was rewritten from containment to
    equality, `F41` still pointed at the line that no longer existed, and a NEW mutation added
    in the same round covered a DIFFERENT axis over the same expression — so the list looked
    like it had gained coverage while it had lost some. Driving only the new mutations, which
    is the natural thing to do, cannot find that (review, PR #110).

    A FUNCTION OVER A ROOT so §E17 can drive it on a synthetic tree, and never called from
    inside a mutated copy: under an applied mutation the target legitimately no longer contains
    its own anchor, so this belongs before the run and not during it.
    """
    out = []
    for mid, rel, find, _repl, _arm in mutations:
        text = (Path(root) / rel).read_text()
        for f in (find if isinstance(find, tuple) else (find,)):
            n = text.count(f)
            if n != 1:
                out.append((mid, rel, n))
    return out


class Record(NamedTuple):
    """One finished mutation: what happened, what to print, and what it cost.

    THE LINE TRAVELS WITH THE VERDICT rather than being printed where it is produced. Under
    `--jobs N` the mutations finish out of order and the output has to come back in list order,
    so a worker cannot print; and a worker that returned only a verdict would leave the text to
    be rebuilt by a second reader of the same facts, which is how the two came to disagree
    before `result_line` existed.
    """

    mid: str
    kind: str          # M / I / F — which of the three totals this counts in
    verdict: str
    line: str
    wall: float
    cpu: float
    # Whether the work tree this ran in is still safe to hand to the next mutation. It travels
    # here because `Outcome` does not leave `apply_and_run`, and the alternative — a caller
    # re-deriving it by reading the printed line — is a second reader of a fact, spelled as
    # prose, which is how the line and the verdict came to disagree before `result_line`.
    contaminated: bool = False


def apply_and_run(work, entry, suite, kind):
    """Mutate `work`, run `suite`, put the file back, and say what happened.

    THE REVERT IS IN A `finally`, which it was not while this was inline in the loop. Serially
    an exception between the two writes ended the run anyway; with a tree handed back to a pool
    it would leave a mutated file behind for whichever mutation drew that tree next, and every
    result after it would be a fact about two defects at once.
    """
    mid, rel, find, repl, arm = entry
    path = work / rel
    original = path.read_text()
    # `find`/`repl` may be tuples: some properties are now defended in two places, and
    # reintroducing the defect means removing both (see M53).
    edits = list(zip(find, repl)) if isinstance(find, tuple) else [(find, repl)]
    counts = [original.count(f) for f, _ in edits]
    if any(c == 0 for c in counts):
        # No time to report, and "(0.0s)" would read as a suite that ran instantly rather than
        # one that never started — the same lie the None/[] distinction exists to prevent
        # elsewhere. Say which it is.
        return Record(mid, kind, UNAPPLIED,
                      f"{mid}: STALE ANCHOR — text not found in {rel} ({suite} not run)",
                      0.0, 0.0)
    if any(c > 1 for c in counts):
        # THE OTHER HALF OF THE SAME GUARD, and the half that fails silently. `replace(f, r, 1)`
        # takes whichever occurrence comes first, so an anchor matching twice still produces a
        # mutant, still reddens SOME arm, and still prints CAUGHT — while testing whichever site
        # happens to be earlier in the file rather than the one the mutation is named for. Five
        # entries were in this state, four of them because a 4-space anchor is a substring of the
        # same line indented 8 (the leading-newline lesson in §4, which nothing enforced), and one
        # because a fix here made two functions textually identical. Refused rather than warned: a
        # mutation that has quietly stopped testing what it names is exactly the failure this
        # suite exists to prevent in the code it mutates (review, PR #103).
        return Record(mid, kind, UNAPPLIED,
                      f"{mid}: AMBIGUOUS ANCHOR — matches {counts} times in {rel}; pin it with "
                      f"a leading newline or adjacent context ({suite} not run)", 0.0, 0.0)
    mutated = original
    for f, r in edits:
        mutated = mutated.replace(f, r, 1)
    path.write_text(mutated)
    try:
        outcome = run(work, suite)
    finally:
        path.write_text(original)
    failed = failed_checks(suite, outcome.output)
    return Record(mid, kind, verdict(outcome, arm, failed),
                  result_line(mid, suite, arm, outcome, failed), outcome.wall, outcome.cpu,
                  contaminated(outcome))


# PHRASED OVER THE DISJUNCTION IT ACTUALLY COVERS. Two things poison a run — a sweep that
# reported a failure, and a run that raised before it could report anything — and only the first
# is a containment failure. A message asserting the first would be false of the second, which is
# the same overclaim §4's note about a contract outliving its evidence is about.
_STOPPED = ("the run stopped after a work tree could not be established as safe to reuse, and a "
            "verdict produced beside a live process out of that tree is not a verdict")


def _abandoned(entry, kind):
    """The record a mutation gets when the run stopped before it could be drawn.

    A VERDICT OF ITS OWN rather than a MISSED or a silence. Silence would leave the totals
    short with nothing saying why; MISSED would assert that the suite ran and passed with the
    defect present, which is a claim about an arm nobody exercised.
    """
    return Record(entry[0], kind, ABANDONED, f"{entry[0]}: NOT RUN — {_STOPPED}", 0.0, 0.0)


def _overlapped(record):
    """The same record, relabelled: it ran, but beside a tree nobody could account for.

    THE PRE-DRAW CHECK CANNOT REACH A WORKER THAT HAS ALREADY DRAWN. Stopping the run stops
    everything not yet started; the seven siblings already inside `apply_and_run` run to
    completion and used to hand back ordinary verdicts, which were counted and printed as
    results (review, PR #111). Their suites executed on a machine with an unknown extra tenant
    on it — competing for cores, ports and the fixture servers these suites bind — so what they
    report is a measurement taken under conditions nobody can state, which is not the same thing
    as a result and must not be added to a total.

    THE ORIGINAL LINE IS KEPT AND ANNOTATED rather than replaced. What the arm did is still the
    most useful thing to know when reading the wreckage afterwards; what must not survive is its
    standing as evidence.

    ITS OWN TREE IS CLEAN AND GOES BACK, which is not a contradiction: the unaccounted-for
    process is in a SIBLING's tree, and nothing can draw this one anyway, because every mutation
    after the poisoning refuses before it draws.
    """
    return record._replace(
        verdict=INCONCLUSIVE,
        line=f"{record.line} — INCONCLUSIVE: another worker's tree went unaccounted for while "
             f"this ran, so this was measured beside it and is not evidence either way")


def _draw_and_run(trees, entry, suite, kind, poisoned):
    """One mutation, in whichever tree is free — and the tree goes back only if it is CLEAN.

    THE `finally` IS WHAT KEEPS THE POOL FROM DRAINING. A worker that returned without putting
    its tree back would take one thread's worth of parallelism with it, and eight such would
    leave the run wedged on an empty queue with no output and nothing to say why.

    A CONTAMINATED TREE IS NEVER HANDED BACK, which is the hole that sentence used to have.
    `Outcome.leftover` already said "the next mutation to draw this tree inherits it" — and the
    tree went back on exactly that path, so the next mutation ran beside a live process out of
    the same directory, with the pre-revert code still resident in it, and every verdict after
    that was a fact about two runs at once. The tree was then deleted from under it at the end
    (review, PR #111).

    AND THE RUN STOPS, rather than continuing one tree short. There is no reading under which
    the remaining mutations are worth their twelve minutes: the run has already failed, a
    process nobody can account for is executing out of a directory this program made, and every
    later verdict is measured on a machine that now has an unknown extra tenant.

    STOPPING IS NOT INSTANT, THOUGH, AND THAT IS THE SECOND HALF. It reaches everything not yet
    started; the siblings already inside `apply_and_run` finish, and their results were counted
    (review, PR #111). They are relabelled — see `_overlapped` — because "the run stopped" and
    "this particular result is untrustworthy" are different facts about different mutations, and
    only the first of them is what the pre-draw check above expresses.

    AN EXCEPTION COUNTS AS CONTAMINATION, because `clean` is only ever set by getting to the end
    of a run that said it was clean. A sweep that raised is precisely the case where what is
    still running is unknown, so the default has to be the careful one.

    THE PILL IS WHAT THE POOL GETS INSTEAD. A thread that passed the check above and is already
    blocked in `get()` cannot see the event; putting `None` in the quarantined tree's place is
    what wakes it. It is checked the instant it is drawn and never used as a path — the sentinel
    rule from `_signal` applies here too, one accident smaller.
    """
    if poisoned.is_set():
        return _abandoned(entry, kind)
    work = trees.get()
    if work is None:
        trees.put(work)
        return _abandoned(entry, kind)
    clean = False
    try:
        record = apply_and_run(work, entry, suite, kind)
        clean = not record.contaminated
        # ASKED BEFORE THE `finally` SETS IT, so what this sees is a SIBLING's poisoning and
        # never this worker's own. A record that is itself contaminated keeps its own line: it
        # is the report of the thing that stopped the run, and relabelling it inconclusive would
        # lose the one verdict that explains all the others.
        if not record.contaminated and poisoned.is_set():
            record = _overlapped(record)
        return record
    finally:
        if clean:
            trees.put(work)
        else:
            # THE FLAG BEFORE THE QUEUE, for the reason `sweep()` in `mcp_proxy_io.py` learned
            # in PR #103: whatever can fail goes after whatever must not be lost.
            poisoned.set()
            trees.put(None)


def slowest(records):
    """The record that spent the most CPU, or None where nothing ran.

    CPU RATHER THAN WALL, which is the whole reason both are carried. §4 reads this number
    against the baseline as the M65 early warning — a mutation that has turned some walk
    recursive shows up here as several times its suite's baseline, long before it grows past
    `_SUITE_TIMEOUT`. Wall time cannot say that under `--jobs N`: eight suites sharing a
    machine each take longer, and the loudest wall figure would name whichever mutation was
    unluckiest with the scheduler.

    Mutations that never ran are excluded rather than ranked at 0.0, so "the slowest" is always
    a statement about a suite that executed. BOTH KINDS OF NEVER-RAN: a stale anchor and an
    abandoned run are different reasons for the same 0.0, and a `max` over nothing but those
    would name one of them as the slowest suite in the run.
    """
    ran = [r for r in records if r.verdict not in (UNAPPLIED, ABANDONED)]
    return max(ran, key=lambda r: r.cpu, default=None)


def main(argv=None):
    started = time.monotonic()
    try:
        jobs = parse_jobs(sys.argv[1:] if argv is None else argv)
    except ValueError as exc:
        print(exc)
        return 2
    # Before the baseline, which costs a selftest run: a misclassified entry makes every
    # number below it wrong, so it is worth nothing to discover that at the end.
    kinds = {mid: _classify(mid, rel) for mid, rel, _f, _r, _a in MUTATIONS}
    suites = {mid: _suite_for(rel) for mid, rel, _f, _r, _a in MUTATIONS}
    dupes = duplicate_ids(MUTATIONS)
    if dupes:
        print("DUPLICATE MUTATION IDS — an id that names two mutations makes every line "
              "reporting it ambiguous, and the totals stop being readable back to entries:")
        for mid, n in dupes:
            print(f"  {mid} is used {n} times")
        return 1
    stale = stale_anchors(HARNESS, MUTATIONS)
    if stale:
        print("STALE OR AMBIGUOUS ANCHORS — these mutations cannot be applied, and a run that "
              "discovers that on the way past has already spent the hour:")
        for mid, rel, n in stale:
            print(f"  {mid} -> {rel} matches {n} time(s), expected exactly 1")
        return 1
    # THE TREES GO IN A `finally`, which they did not while there was one of them and four ways
    # to leave. Every early return below — a hung baseline, a failed one, an arm naming no check
    # — used to walk out past the `rmtree` at the bottom and leave the copy behind; the three
    # that print a refusal are also the three anyone is most likely to hit twice in a row while
    # fixing what they refuse over. One tree was a leak worth half a gigabyte and nobody's
    # attention. Eight is the same leak eight times, which is how it was finally noticed.
    # AND THE POISON-ON-EXCEPTION RULE LIVES HERE, at the boundary that owns the trees, rather
    # than at each place that spawns something into one. The first cut put it in `_draw_and_run`
    # — where the reproduction was — so a BASELINE that raised reached this `finally` with
    # `poisoned` still false and the trees were deleted out from under whatever it had left
    # running (review, PR #111). Written per-spawner, the rule has to be remembered by the next
    # spawner; written here it quantifies over everything between the `mkdtemp` and the delete,
    # including `worktree_binds`, including a `KeyboardInterrupt`, and including whatever runs
    # in there next year. `BaseException` on purpose: a Ctrl-C during a mutation run leaves
    # suites running exactly as an unexpected error does, and it is the likelier of the two.
    tmp = Path(tempfile.mkdtemp(prefix="mutate-mcp-"))
    poisoned = threading.Event()
    swept = False
    try:
        rc = _run_suite(tmp, jobs, kinds, suites, started, poisoned)
    except BaseException:
        poisoned.set()
        raise
    finally:
        swept = discard(tmp, poisoned)
    # AND ITS ANSWER IS READ. A `discard` nobody asks is a `discard` that can quietly fail and
    # leave every tree on disk behind a run that exits 0.
    return rc if swept else 1


def discard(tmp, poisoned):
    """Delete the work trees, unless something may still be running out of one. True iff GONE.

    DELETING A TREE WITH A LIVE PROCESS IN IT IS THE SECOND HALF OF THE SAME LEAK. The sweep
    already says when it left a descendant alive or could not look; `rmtree` then removes the
    directory that process is executing out of, which destroys the only evidence of what it was
    and where it came from while doing nothing whatever about the process (review, PR #111).

    SO A POISONED RUN KEEPS ITS TREES AND SAYS WHERE THEY ARE. §4's verification block globs for
    exactly this prefix afterwards and will report them, which is the correct outcome rather
    than a nuisance: the tree is the evidence, and the glob is the only thing that would
    otherwise notice.

    AND THE ANSWER IS OBSERVED, NEVER ASSUMED. `ignore_errors=True` next to `return True` is a
    function suppressing its own failures and then reporting success — a clean run could leave
    every tree on disk and still exit 0 (review, PR #111). The flag stays, because a teardown
    that raises partway through is worse than one that does what it can; what changes is that
    the claim afterwards comes from looking. It is the same rule `probe_group_empty` follows in
    `mcp_proxy_io.py`: an errno from a call you made is a fact about the call, and the question
    was about the world.

    ONE FACT, TWO REASONS. `False` means the trees are still there, and the message above says
    which of "kept on purpose" and "would not go" it was. Every caller wants the fact.
    """
    if poisoned.is_set():
        print(f"WORK TREES KEPT at {tmp} — a run ended without establishing that nothing is "
              f"still executing out of them, and deleting a directory a process is running "
              f"from would lose what it was while doing nothing about it. Look with "
              f"`ps -ef | grep {tmp}`, kill whatever is there, then remove the tree by hand.")
        return False
    shutil.rmtree(tmp, ignore_errors=True)
    if os.path.exists(tmp):
        print(f"WORK TREES NOT DELETED — {tmp} is still on disk after `rmtree`, which suppressed "
              f"whatever stopped it. Half a gigabyte per run accumulates in silence otherwise.")
        return False
    return True


def run_verdict(caught, totals, poisoned):
    """The exit status one whole run earns. 0 only when there is nothing to say against it.

    BOTH TOTALS MUST BE CLEAN: an instrument mutation surviving means the arm guarding the
    selftest's own reporting is decorative, which is the same failure as any other MISSED.

    AND THE POISON JOINS THE PREDICATE rather than being left to the totals to notice on its
    behalf. They would notice, today, because the only thing that poisons a run is a TIMEOUT and
    a TIMEOUT is never a CAUGHT — but that is a coupling between two facts nothing states, and
    §4's rule is that a new way of being untrustworthy goes where the existing reasons already
    live, not into a second flag one caller happens to read.

    A FUNCTION so it can be driven: the alternative is reachable only by paying for a whole run
    that fails, which is the one case nobody arranges twice.
    """
    return 0 if caught == totals and not poisoned.is_set() else 1


def _run_suite(tmp, jobs, kinds, suites, started, poisoned):
    # ONE TREE PER WORKER, never one tree shared: a mutation is a write to a file the next
    # mutation reads, so two workers in one tree would be testing each other's defects. Never
    # more trees than mutations either — the pool would hold a copy nothing ever draws.
    trees: queue.Queue = queue.Queue()
    made = []
    for n in range(max(1, min(jobs, len(MUTATIONS)))):
        one = make_worktree(HARNESS, tmp / f"w{n}" / "harness")
        bound = worktree_binds(one)
        if not bound or not Path(bound).resolve().is_relative_to(one.resolve()):
            print(f"WORK TREE DOES NOT BIND — a suite run in {one} imports `agentskill_evals` "
                  f"from {bound or '(nothing; the interpreter said nothing)'}, so every "
                  f"mutation below would perturb a copy nothing executes and report MISSED.")
            return 1
        made.append(one)
        trees.put(one)
    work = made[0]
    # One baseline per suite actually used. Only the suites in play: paying for a green run
    # of a suite nothing below mutates would be measuring the machine, and a suite whose
    # baseline is never checked can be broken while its mutations all report CAUGHT for the
    # wrong reason.
    #
    # SERIAL, IN ONE TREE, EVEN AT `--jobs 8`. Two reasons, and the second is the one that
    # matters: a baseline is a REFERENCE, so it has to be measured under the conditions its
    # readers assume — an unloaded machine — and three baselines run concurrently would each
    # report a slower number than any mutation is later compared against. The saving would be
    # ~35s of a run this changes from 80 minutes to ten.
    baseline = {}
    for suite in sorted(set(suites.values())):
        base = run(work, suite)
        if base.output == _TIMEOUT_OUTPUT:
            # THE FLAG BEFORE THE MESSAGE. This tree is one of the ones in the pool, so a
            # baseline whose sweep left something behind poisons the run exactly as a mutation
            # would — and setting it first means a `print` that fails cannot be what loses it.
            if contaminated(base):
                poisoned.set()
            print(f"BASELINE TIMED OUT after {_SUITE_TIMEOUT}s — the unmutated {suite} suite "
                  f"hung, so nothing below would prove anything.{containment_note(base)}")
            return 1
        if base.returncode != 0:
            print(f"BASELINE FAILED ({suite}) — mutations prove nothing:")
            print(base.output[-3000:])
            return 1
        # The reference every per-mutation time below is read against; without it those
        # numbers describe the machine, not the mutation. BOTH CLOCKS, because the per-mutation
        # lines now carry both and a CPU figure compared against a wall reference is nonsense
        # in the direction that hides things — the proxy suite spends ~40 of its ~43 seconds
        # waiting, so its CPU baseline is a fortieth of its wall one.
        baseline[suite] = (base.wall, base.cpu)
        out = base.output
        # EVERY ARM MUST NAME A CHECK THIS SUITE ACTUALLY PRINTS. The arm is a second, untyped
        # reference to a check — a copied string — so renaming the check silently unhooks the
        # mutation, which then reports "failed, but NOT via" and reads like a coverage problem
        # instead of a broken reference. It happened to F10 during review (PR #106). The
        # baseline run is the only place the full label set is known, and it is already paid
        # for, so the check costs nothing. Refused rather than warned, like the anchor guards.
        #
        # AND THE PARSE MUST HAVE FOUND SOMETHING FIRST. `if printed:` skipped the guard
        # entirely when the label regex matched nothing, so a change to a verifier's output
        # format would clear every arm in that suite at once — the guard containing, inside
        # itself, the vacuous-success class it was written to close (review, PR #106). It is
        # §4's `all(...)` over a collection nothing was put into, and it takes the same
        # remedy: a structural clause saying something must be there, ahead of the universal
        # one. The wrong-arm control proves membership works against a NON-EMPTY parse and is
        # silent about the empty one, which is why it did not catch this.
        # THE SOURCE VARIANT, for a suite whose passes never reach its output. Same guard, same
        # refusal, different evidence: the arm must appear as a literal somewhere in the file
        # that defines the checks. It cannot tell a live arm from a commented-out one, and does
        # not need to — what it catches is an arm naming a check that no longer exists at all.
        src = _SUITES[suite].source
        if src is not None:
            text = (work / src).read_text()
            orphan = sorted({a for m, _r, _f, _rp, a in MUTATIONS
                             if suites[m] == suite and f'"{a}"' not in text})
            if orphan:
                print(f"ARM NAMES NO CHECK in {src} — a renamed arm unhooks its mutation and "
                      f"reports only as a skip:")
                for a in orphan:
                    print(f"  {a!r}")
                return 1
        pattern = _SUITES[suite].labels
        if pattern is not None:
            printed = set(re.findall(pattern, out, re.MULTILINE))
            if not printed:
                print(f"NO CHECK LABELS PARSED from the {suite} baseline — its output format "
                      f"moved out from under {pattern!r}, and every arm below would be "
                      f"cleared by a guard with nothing to compare against.")
                return 1
            orphan = sorted({a for m, r, _f, _r, a in MUTATIONS
                             if suites[m] == suite and a not in printed})
            if orphan:
                print(f"ARM NAMES NO CHECK in the {suite} suite — a renamed check unhooks its "
                      f"mutation silently:")
                for a in orphan:
                    print(f"  {a!r}")
                return 1
        print(f"baseline: {suite} PASSED in {base.wall:.1f}s wall / {base.cpu:.1f}s cpu")
    print(f"\nrunning {len(MUTATIONS)} mutations across {len(made)} work tree(s)\n")

    # Counted apart, and reported apart. An `I*` mutation perturbs the INSTRUMENT (the
    # selftest itself) rather than the code under test, so folding it into one total would
    # make "N/N caught" claim more production coverage than exists. Splitting it here is what
    # keeps the exception visible at the point anyone reads the result, instead of only to
    # someone who opens this file — and makes adding a second one a deliberate act that shows
    # up in the output rather than a quiet edit to the list.
    caught = {"M": 0, "I": 0, "F": 0}
    totals = {"M": 0, "I": 0, "F": 0}
    for kind in kinds.values():
        totals[kind] += 1
    # A TREE IS DRAWN, NOT ASSIGNED. Mutations differ by two orders of magnitude in cost — a
    # proxy arm is ~43s and a selftest arm ~6s — so dealing them out round-robin would leave
    # one worker holding every proxy entry while the rest finished and idled. The queue hands
    # each finishing worker whatever is next, which is the only scheduling this needs.
    #
    # AND THE OUTPUT IS IN LIST ORDER, however the runs finish. Iterating the futures in
    # submission order blocks on the first unfinished one while every other worker keeps
    # running, so the lines still stream — they just stream in the order a reader can diff
    # against the last run.
    records = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(made)) as pool:
        futures = [pool.submit(_draw_and_run, trees, entry, suites[entry[0]], kinds[entry[0]],
                               poisoned) for entry in MUTATIONS]
        for future in futures:
            record = future.result()
            records.append(record)
            print(record.line, flush=True)
            if record.verdict == CAUGHT:
                caught[record.kind] += 1
    if poisoned.is_set():
        print(f"\n*** RUN STOPPED *** a sweep left a process alive in a work tree, could not "
              f"establish that it had not, or the mutation itself failed before it could say — "
              f"see the last line above that is neither NOT RUN nor INCONCLUSIVE. Nothing after "
              f"that point was run, and whatever was ALREADY running when it happened is marked "
              f"INCONCLUSIVE rather than counted: {_STOPPED}.")
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
    worst = slowest(records)
    slow = (f"slowest {worst.mid} at {worst.cpu:.1f}s cpu ({worst.wall:.1f}s wall)"
            if worst else "no mutation ran")
    bases = ", ".join(f"{s} baseline {w:.1f}s wall / {c:.1f}s cpu"
                      for s, (w, c) in sorted(baseline.items()))
    print(f"elapsed: {total / 60:.1f} min total across {len(made)} tree(s), {bases}, {slow}")
    return run_verdict(caught, totals, poisoned)


if __name__ == "__main__":
    sys.exit(main())
