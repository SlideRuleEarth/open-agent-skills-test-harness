"""C3 — the harness-owned filtering proxy: the AUDIT RECORD, the LOG, and their VERDICTS.

`DESIGN_MCP_Support.md` §10.5 and §10.5.1 are the specification; this module is their whole
implementation, and like `mcp_proxy.py` it does no I/O. The proxy writes these records and the
post-run check reads them, but the question they both ask — *did this instance end cleanly?* —
is answered here, once, by `verdict()`.

TWO LAYERS, and the second is the reason the first is worth having. `verdict()` judges one
instance from an assembled map. `log_verdict()` judges a whole append-only file: it groups the
lines by instance id, applies the ABSENCE RULE — a start record with no well-formed terminator
is an anomaly — and conjoins, so a clean restart can never heal the instance before it. Both
halves are pure over their input, the file's text included, because every rule that could be
silently wrong here is a statement about a string: a truncated final line, a terminator matched
to the wrong start, an off-list tool in a list of what was forwarded. The caller keeps `open()`
and one rule that only it can apply — for a gated server, a log that does not exist fails the
cell, since a gated server whose proxy never ran means the gating never happened.

WHY THIS EXISTS AS A MODULE AND NOT AS A FEW CHECKS AT THE CALL SITES. §10.5's rule is that a
cell fails unless the log PROVES the instance ended cleanly, so the classification is consumed
three times over: by the terminator the proxy writes, by the per-instance verdict, and by
`verify_post_run`. Three consumers each deciding for themselves what "clean" means is the
defect this design has already paid for twice (`TODO_Contained_HOME.md` §4), and it fails in a
particular way — the consumers that were not updated keep publishing their old conclusion, so
the exit status disagrees with the output.

THE INPUT IS ARBITRARY DECODED JSON, NOT A RECORD THE PROXY PROMISED TO WRITE. That is the
whole reason the reader owns this: the writer is the thing under suspicion. A validator that
assumes `triggers` is a list of maps crashes on `{"triggers": {}}`, and a crash inside
`verify_post_run` is not a failed cell — it is a harness traceback, which is a worse outcome
than the malformed record it was reading. So `parse()` accepts anything `json.loads` can
produce, never raises, and turns every shape it cannot use into a stable problem code
(review, PR #102).

MISSING, NULL AND EMPTY ARE THREE DIFFERENT THINGS. `raw.get("outcomes") or []` collapses all
three into "no outcomes", so a terminator with no `outcomes` key at all — a writer that forgot
the axis entirely — validated clean. Absence is not emptiness anywhere in this module; it is
the whole subject of §10.5.1's second half.

THE VERDICT, and the order of its three clauses matters:

    clean  <=>  the record is structurally valid
                AND every reason it holds is clean
                AND every step of §10.5's sequence recorded its completion

The structural clause is FIRST because the other two are universally quantified, and both are
vacuously true of an empty record: "every recorded reason is clean" holds of `triggers: []`,
which is not a clean instance but a broken writer. `all()` over a collection nothing was put
into is the same unfalsifiable assertion `CLAUDE.md` bans in test code.

TWO AXES PLUS THE FACTS, because one field cannot carry an ending. `client_eof ->
shutdown_write_failed -> shutdown_child_killed` is one instance with three facts in it, and a
single-slot `reason` has to drop two of them. Whichever it drops is load-bearing: drop the
trigger and a protocol anomaly reads as a clean client close; drop the outcomes and a failed
process-group teardown — a surviving grandchild holding an interpolated credential — is
certified clean by the record meant to prove the opposite.

AND THE COMPLETION FACTS, because every outcome is an exceptional observation. An empty
outcome list means nothing went wrong, which is NOT the same as everything having happened: a
teardown that silently skipped its group kill raises nothing, records nothing, and would
satisfy a verdict computed from the reasons alone. Not observing a failure is not observing a
success, and only one of the two earns a clean verdict.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .mcp_proxy import ANOMALY_KINDS, DROP_REASONS, IMPLEMENTED_VERSIONS

# ---------------------------------------------------------------------------------------
# Axis 1 — the triggers (§10.5.1)
# ---------------------------------------------------------------------------------------
# An ordered list, and the FIRST is the latch: the terminal event that stopped forwarding and
# began the teardown. Later triggers are recorded behind it and classified identically, which
# is the whole reason this is a list rather than a slot — `client_eof` followed by
# `signal_term` is a CLI closing stdin and then signalling, and two individually clean
# triggers must not compose into a failure.

CLIENT_EOF = "client_eof"
SIGNAL_TERM = "signal_term"
SIGNAL_INT = "signal_int"
SPAWN_FAILED = "spawn_failed"
CHILD_EXIT = "child_exit"
CLIENT_WRITE_FAILED = "client_write_failed"
CHILD_WRITE_FAILED = "child_write_failed"
READ_FAILED = "read_failed"
PROTOCOL_ANOMALY = "protocol_anomaly"

TRIGGERS = frozenset({
    CLIENT_EOF, SIGNAL_TERM, SIGNAL_INT, SPAWN_FAILED, CHILD_EXIT,
    CLIENT_WRITE_FAILED, CHILD_WRITE_FAILED, READ_FAILED, PROTOCOL_ANOMALY,
})

# ---------------------------------------------------------------------------------------
# Axis 2 — the cleanup outcomes (§10.5.1)
# ---------------------------------------------------------------------------------------
# What went WRONG as §10.5's six steps ran. An empty list means nothing exceptional was
# observed; it is not a claim that the steps ran.

SHUTDOWN_WRITE_FAILED = "shutdown_write_failed"
SHUTDOWN_CHILD_KILLED = "shutdown_child_killed"
SHUTDOWN_READ_FAILED = "shutdown_read_failed"
SHUTDOWN_REAP_FAILED = "shutdown_reap_failed"
SHUTDOWN_GROUP_KILL_FAILED = "shutdown_group_kill_failed"
SHUTDOWN_ANOMALY = "shutdown_anomaly"

OUTCOMES = frozenset({
    SHUTDOWN_WRITE_FAILED, SHUTDOWN_CHILD_KILLED, SHUTDOWN_READ_FAILED,
    SHUTDOWN_REAP_FAILED, SHUTDOWN_GROUP_KILL_FAILED, SHUTDOWN_ANOMALY,
})

# Recorded at the START rather than at the end, and always anomalous ON ITS OWN. That is the
# clause which stops an armed-but-silent fault hook from producing a passing run: what is
# anomalous is the CONFIGURATION, not the firing, because a fault that was armed and never
# fired would otherwise leave a clean trigger and clean outcomes satisfying the formula
# exactly — and the run would pass while pretending to have been tested.
FAULT_POINT_CONFIGURED = "fault_point_configured"
# Written when the hook actually suppresses a fact. EVIDENCE, not a verdict input: the arming
# already made the instance anomalous, so firing adds nothing to the verdict and everything to
# the record. It is the only thing a suppressed step's `failed` may name as its cause.
FAULT_POINT_FIRED = "fault_point_fired"

# Every reason, on either axis, that leaves an instance clean. Everything else is an anomaly,
# INCLUDING an unrecognized one — see `is_clean`.
CLEAN_REASONS = frozenset({
    CLIENT_EOF, SIGNAL_TERM, SIGNAL_INT,
    # C3-1 measured this against agy: the client closes stdin and stops reading at once, so
    # the spec's graceful-closure write fails with EPIPE against a conforming peer. Recorded,
    # swallowed, and the sequence continues.
    SHUTDOWN_WRITE_FAILED,
    # The spec makes forced termination the standard escalation and only SHOULDs a prompt exit
    # after stdin closes, so a server that needed SIGKILL is worth recording and not worth
    # failing on. The terminator's promise — child reaped, group gone — still holds.
    SHUTDOWN_CHILD_KILLED,
})

# ---------------------------------------------------------------------------------------
# The completion facts — what actually ran (§10.5.1)
# ---------------------------------------------------------------------------------------

INTAKE_CLOSED = "intake_closed"
CHILD_STDIN_CLOSED = "child_stdin_closed"
DRAIN_ENDED = "drain_ended"
CHILD_REAPED = "child_reaped"
GROUP_TERMINATED = "group_terminated"

# In §10.5's step order. The key set is closed in BOTH directions and only one of them is
# obvious: a missing fact is the case everyone writes, and an UNRECOGNIZED one is how a fact
# added later goes unchecked, because a validator that iterates the names it knows accepts a
# record carrying one it has never heard of.
FACTS = (INTAKE_CLOSED, CHILD_STDIN_CLOSED, DRAIN_ENDED, CHILD_REAPED, GROUP_TERMINATED)

DONE = "done"
NOT_APPLICABLE = "not_applicable"
FAILED = "failed"
STATES = frozenset({DONE, NOT_APPLICABLE, FAILED})

# `not_applicable` is licensed by the LATCH trigger, not by any trigger in the list: the
# licence means the step never applied, and only the event that ended the instance before the
# step could apply can say that. A later trigger arrives during teardown, by which time the
# step has already had its chance.
_NOT_APPLICABLE_LICENSED_BY = {
    # Step 1 always runs. There is no ending in which the proxy did not have an intake to
    # close, so a blank here is never justified.
    INTAKE_CLOSED: frozenset(),
    CHILD_STDIN_CLOSED: frozenset({SPAWN_FAILED}),
    DRAIN_ENDED: frozenset({SPAWN_FAILED}),
    CHILD_REAPED: frozenset({SPAWN_FAILED}),
    GROUP_TERMINATED: frozenset({SPAWN_FAILED}),
}

# The typed outcome a `failed` fact may name as its cause, where one exists. `failed` exists
# because the two axes describe the same events and must be able to agree: without it, a
# teardown that ran and did not work has NO structurally valid record at all, since
# `shutdown_reap_failed` could not be said of `child_reaped` and the only remaining spelling —
# omitting the fact — is what the validator calls malformed.
_TYPED_PAIRING = {
    INTAKE_CLOSED: None,
    CHILD_STDIN_CLOSED: None,
    DRAIN_ENDED: SHUTDOWN_READ_FAILED,
    CHILD_REAPED: SHUTDOWN_REAP_FAILED,
    GROUP_TERMINATED: SHUTDOWN_GROUP_KILL_FAILED,
}
# The reverse direction, and it is not optional: a validator checking only that a `failed` fact
# has its outcome lets the writer record the failure on whichever axis it finds convenient and
# stay silent on the other. Note the two CLEAN outcomes are deliberately absent — a write that
# failed during the drain does not stop the drain, so `drain_ended` still reads `done`.
_OUTCOME_PAIRING = {v: k for k, v in _TYPED_PAIRING.items() if v is not None}


def typed_outcome(fact: str) -> str | None:
    """The cleanup outcome that names this fact's own way of failing, where one exists.

    Two facts have none — `intake_closed` and `child_stdin_closed` — and that is what
    `shutdown_anomaly` is for. Exported so the writer names the same outcome the reader pairs
    against, rather than restating the table: a check that re-derives a definition cannot
    disagree with it, and a copy of one silently can.
    """
    return _TYPED_PAIRING.get(fact)


def causes_for(fact: str) -> frozenset[str]:
    """The causes a `failed` fact may name — its typed outcome, the catch-all, or suppression.

    EXACTLY ONE, declared rather than inferred. Accepting a fact that merely has *some*
    pairing available lets contradictory evidence sit in one record: a `group_terminated`
    carrying both `shutdown_group_kill_failed` and a fault-point firing says the kill was
    attempted and failed AND that it never ran, and an inferring validator reports no problem
    at all (review, PR #102).
    """
    typed = _TYPED_PAIRING.get(fact)
    base = {SHUTDOWN_ANOMALY, FAULT_POINT_FIRED}
    return frozenset(base | ({typed} if typed else set()))


def is_clean(reason: str) -> bool:
    """Total over both axes and the start reason. Anything unrecognized is an ANOMALY.

    There is deliberately no default-clean branch. Adding a reason without classifying it must
    fail the cell rather than pass it — the failure has to be the loud one, or the enumeration
    decays into documentation.
    """
    return reason in CLEAN_REASONS


# ---------------------------------------------------------------------------------------
# The parsed record
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Trigger:
    reason: str
    anomaly: str | None = None       # required when `reason` is `protocol_anomaly`


@dataclass(frozen=True)
class Outcome:
    kind: str
    fact: str | None = None          # required when `kind` is `shutdown_anomaly`
    exception: str | None = None     # ...and so is this


@dataclass(frozen=True)
class Fact:
    state: str
    cause: str | None = None         # required when `state` is `failed`


# Absent, distinguishable from every value JSON can carry — including `null`. A boolean
# `present` flag cannot make that distinction, and `child_status: null` is a record claiming
# the evidence exists while carrying none of it (review, PR #102).
_MISSING = object()


def is_json_int(value: Any) -> bool:
    """A JSON integer, and `True` is not one.

    `isinstance(True, int)` holds in Python, so the obvious check accepts a boolean — and a
    boolean here is a record claiming an exit status it does not have, which is the same
    fabrication the presence rule exists to catch, one type down.

    ONE predicate, not one per field. An exit status and a pid are the same shape and the same
    trap, so the log layer below reuses this rather than restating it — a rule written twice is
    a rule that can be fixed once.
    """
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class Record:
    """One instance's assembled records, after shape checking and before judgement.

    Assembly from the log is I/O and lives with the reader; everything from here down is a
    pure function of what it assembled.
    """

    triggers: tuple[Trigger, ...] = ()
    outcomes: tuple[Outcome, ...] = ()
    facts: dict[str, Fact] = field(default_factory=dict)
    fact_keys: tuple[str, ...] = ()          # as written, so an unknown NAME can be reported
    fired: tuple[str, ...] = ()
    fault_point: bool = False                # PRESENT, which is the anomalous fact
    suppresses: frozenset[str] = frozenset()
    child_status: Any = _MISSING             # the VALUE, or `_MISSING`; never a flag

    @property
    def latch(self) -> str | None:
        return self.triggers[0].reason if self.triggers else None


def _str_or(value: Any, problems: list[str], code: str) -> str | None:
    if isinstance(value, str):
        return value
    problems.append(code)
    return None


def _opt_str(entry: dict, key: str, problems: list[str], code: str) -> str | None:
    """An OPTIONAL string field: absent, a string, or a reported problem — never erased.

    `entry.get(k) if isinstance(..., str) else None` turns every non-conforming value into
    `None`, which is the same thing this parser uses for "not there" — so a `cause: null` on a
    `done` fact was indistinguishable from a fact with no cause, and the rule forbidding a
    cause under `done` never saw one (review, PR #102). The other three fields here survived
    only because their tags make them REQUIRED, so a different check caught the erasure; that
    is coverage by accident, and it is the same idiom.
    """
    if key not in entry:
        return None
    value = entry[key]
    if isinstance(value, str):
        return value
    problems.append(code)
    return None


def _as_list(raw: Any, key: str, problems: list[str], *, required: bool) -> list:
    """A list, or a reported problem — never an exception, and never a silent empty.

    `missing`, `null` and "not a list" are three different codes because they are three
    different writer bugs, and the one that used to be invisible is `missing`: `raw.get(k) or
    []` turns a forgotten axis into an empty one, and an empty one is legal.
    """
    if key not in raw:
        if required:
            problems.append(f"missing:{key}")
        return []
    value = raw[key]
    if value is None:
        problems.append(f"null:{key}")
        return []
    if not isinstance(value, list):
        problems.append(f"not_a_list:{key}")
        return []
    return value


def parse(raw: Any) -> tuple[Record, tuple[str, ...]]:
    """Arbitrary decoded JSON in; a best-effort `Record` and every shape problem out.

    Best-effort rather than all-or-nothing so one read reports everything wrong with a record
    instead of one problem per re-run — and every normalization it performs is REPORTED, so
    the verdict is anomalous regardless of what the rest of the pass makes of the remains.
    """
    problems: list[str] = []
    if not isinstance(raw, dict):
        return Record(), ("not_a_map:record",)

    triggers = []
    for i, entry in enumerate(_as_list(raw, "triggers", problems, required=True)):
        if not isinstance(entry, dict):
            problems.append(f"trigger_not_a_map:{i}")
            continue
        reason = _str_or(entry.get("reason"), problems, f"trigger_reason_not_a_string:{i}")
        if reason is not None:
            anomaly = _opt_str(entry, "anomaly", problems,
                               f"trigger_anomaly_not_a_string:{i}")
            triggers.append(Trigger(reason, anomaly))

    outcomes = []
    for i, entry in enumerate(_as_list(raw, "outcomes", problems, required=True)):
        if not isinstance(entry, dict):
            problems.append(f"outcome_not_a_map:{i}")
            continue
        kind = _str_or(entry.get("kind"), problems, f"outcome_kind_not_a_string:{i}")
        if kind is not None:
            fact = _opt_str(entry, "fact", problems, f"outcome_fact_not_a_string:{i}")
            exc = _opt_str(entry, "exception", problems,
                           f"outcome_exception_not_a_string:{i}")
            outcomes.append(Outcome(kind, fact, exc))

    facts: dict[str, Fact] = {}
    fact_keys: tuple[str, ...] = ()
    if "facts" not in raw:
        problems.append("missing:facts")
    elif raw["facts"] is None:
        problems.append("null:facts")
    elif not isinstance(raw["facts"], dict):
        problems.append("not_a_map:facts")
    else:
        fact_keys = tuple(str(k) for k in raw["facts"])
        for key, entry in raw["facts"].items():
            key = str(key)
            if not isinstance(entry, dict):
                problems.append(f"fact_not_a_map:{key}")
                continue
            state = _str_or(entry.get("state"), problems, f"fact_state_not_a_string:{key}")
            if state is not None:
                cause = _opt_str(entry, "cause", problems,
                                 f"fact_cause_not_a_string:{key}")
                facts[key] = Fact(state, cause)

    fired = []
    for i, entry in enumerate(_as_list(raw, "fired", problems, required=False)):
        if not isinstance(entry, dict):
            problems.append(f"fired_not_a_map:{i}")
            continue
        fact = _str_or(entry.get("fact"), problems, f"fired_fact_not_a_string:{i}")
        if fact is not None:
            fired.append(fact)

    # PRESENCE is the anomalous fact, not a non-empty suppression list. An arm-only hook
    # suppresses nothing by design, and reading emptiness as "no fault point" gave exactly the
    # passing verdict the arm-only case exists to reject.
    fault_point = "fault_point" in raw and raw["fault_point"] is not None
    suppresses: set[str] = set()
    if "fault_point" in raw and raw["fault_point"] is None:
        problems.append("null:fault_point")
    elif fault_point:
        if not isinstance(raw["fault_point"], dict):
            problems.append("not_a_map:fault_point")
        else:
            for i, item in enumerate(_as_list(raw["fault_point"], "suppresses", problems,
                                              required=True)):
                if isinstance(item, str):
                    suppresses.add(item)
                else:
                    problems.append(f"suppresses_not_a_string:{i}")

    return (Record(tuple(triggers), tuple(outcomes), facts, fact_keys, tuple(fired),
                   fault_point, frozenset(suppresses), raw.get("child_status", _MISSING)),
            tuple(problems))


# ---------------------------------------------------------------------------------------
# The structural validator (§10.5.1)
# ---------------------------------------------------------------------------------------
# Owned by the READER. The proxy may assert the same shape before writing, but that assertion
# is not the check: a proxy broken enough to write an empty trigger list is precisely the wrong
# process to ask whether it did, and a claim and the thing it claims about must not have the
# same author.


def validate(record: Record) -> tuple[str, ...]:
    """Every way a parsed record is malformed, as stable problem codes, in a fixed order.

    A record that fails is `malformed_record` — an anomaly whose subject is the PROXY rather
    than the run. Codes rather than prose so an arm can assert which rule fired: "the validator
    rejected it" is satisfied by a validator that rejects everything, which is why §10.9 also
    drives the one legal record that looks malformed (the `spawn_failed` shape, where every
    child-and-group fact is legitimately `not_applicable`).
    """
    problems: list[str] = []

    if not record.triggers:
        # The vacuity guard. Both clauses below quantify over collections, and an empty one
        # satisfies them while contradicting the definition of an instance, which HAS a latch.
        problems.append("triggers_empty")
    for entry in record.triggers:
        if entry.reason not in TRIGGERS:
            problems.append(f"trigger_unknown:{entry.reason}")
        # A discriminated union: the tag promises a payload, so the payload is required. A
        # bare `protocol_anomaly` says the connection was torn down for a protocol reason and
        # declines to say which, which is precisely what the audit log exists to record.
        elif entry.reason == PROTOCOL_ANOMALY and entry.anomaly not in ANOMALY_KINDS:
            problems.append(f"protocol_anomaly_kind:{entry.anomaly}")
        # ...and the same union closed the other way: a payload is legal only under the tag
        # that READS it. An `anomaly` on a `client_eof` trigger is never consulted, and an
        # unread field is the defect `cause_forbidden` was added for one round ago — this is
        # that rule reaching the two tagged unions it did not name.
        elif entry.reason != PROTOCOL_ANOMALY and entry.anomaly is not None:
            problems.append(f"trigger_anomaly_forbidden:{entry.reason}")

    for entry in record.outcomes:
        if entry.kind not in OUTCOMES:
            problems.append(f"outcome_unknown:{entry.kind}")
        elif entry.kind == SHUTDOWN_ANOMALY:
            if entry.fact not in FACTS:
                problems.append(f"anomaly_unkeyed:{entry.fact}")
            # `not` rather than `is None`: an empty string is a record that carries the field
            # and says nothing in it, which is the presence-not-content defect again.
            if not entry.exception:
                problems.append("anomaly_no_exception")
        elif entry.fact is not None or entry.exception is not None:
            problems.append(f"outcome_payload_forbidden:{entry.kind}")

    for key in FACTS:
        if key not in record.facts:
            problems.append(f"fact_missing:{key}")
    for key in sorted(set(record.fact_keys) - set(FACTS)):
        problems.append(f"fact_unknown:{key}")

    for key in FACTS:
        entry = record.facts.get(key)
        if entry is None:
            continue
        if entry.state not in STATES:
            problems.append(f"state_unknown:{key}:{entry.state}")
            continue
        if (entry.state == NOT_APPLICABLE
                and record.latch not in _NOT_APPLICABLE_LICENSED_BY[key]):
            problems.append(f"not_applicable_unlicensed:{key}")

    problems.extend(_cause_problems(record))

    # Only a process that reaped a child can hold its exit status, so `done` without one claims
    # a reap while lacking the single piece of evidence a reaper necessarily has, and a status
    # attached to `failed` or `not_applicable` is a status for a child that was never reaped —
    # invented, because there was nowhere to get it.
    reaped = record.facts.get(CHILD_REAPED)
    reaped_done = reaped is not None and reaped.state == DONE
    present = record.child_status is not _MISSING
    if reaped_done and not present:
        problems.append("child_status_missing")
    if not reaped_done and present:
        problems.append("child_status_forbidden")
    # Presence was never the claim being made. `null`, `"fabricated"` and `true` are all a
    # record asserting it holds a child's exit status while holding something else, and a
    # `present` boolean cannot tell any of them from the real thing.
    if present and not is_json_int(record.child_status):
        problems.append(f"child_status_not_an_integer:{record.child_status!r}")

    # The fault point's targets are completion facts, so a name outside the closed set is a
    # malformed configuration. The instance is anomalous either way — the hook was armed — but
    # "anomalous for an unrelated reason" is not the same as "checked", and the arming is the
    # only thing that would have been reported.
    for name in sorted(record.suppresses - set(FACTS)):
        problems.append(f"suppresses_unknown:{name}")

    return tuple(problems)


def _evidence_for(record: Record, key: str) -> frozenset[str]:
    """Every cause this record carries evidence for, on one fact.

    Keyed by the EXACT FACT and never by the step. A step number is coarser than the thing it
    identifies — step 2 owns both `child_stdin_closed` and `drain_ended` — so a
    `shutdown_anomaly(step=2)` would license `failed` on either of them, or on both, having
    arisen from one operation. A pairing key one level coarser than what it pairs is not a
    weaker check; it is a check that passes for the wrong reason while reading like coverage.
    """
    found = set()
    typed = _TYPED_PAIRING.get(key)
    kinds = {o.kind for o in record.outcomes}
    if typed is not None and typed in kinds:
        found.add(typed)
    if any(o.kind == SHUTDOWN_ANOMALY and o.fact == key for o in record.outcomes):
        found.add(SHUTDOWN_ANOMALY)
    if key in record.fired:
        found.add(FAULT_POINT_FIRED)
    return frozenset(found)


def _cause_problems(record: Record) -> list[str]:
    """The declared cause of each `failed` fact, and the evidence it must exactly match."""
    problems: list[str] = []

    for key in FACTS:
        entry = record.facts.get(key)
        state = entry.state if entry is not None else None
        evidence = _evidence_for(record, key)
        if state == FAILED:
            cause = entry.cause if entry is not None else None
            if cause not in causes_for(key):
                problems.append(f"cause_unknown:{key}:{cause}")
            elif cause not in evidence:
                problems.append(f"cause_unsupported:{key}:{cause}")
            # Exactly one. A step the fault point stopped from running cannot ALSO have been
            # attempted and failed, and a record saying both is not a record with extra detail
            # — it is two incompatible accounts of one step.
            if len(evidence) > 1:
                problems.append(f"cause_contradicted:{key}:{','.join(sorted(evidence))}")
        else:
            # The union is closed in BOTH directions. A `done` step carrying the cause of its
            # own failure is a record disagreeing with itself, and reading `cause` only under
            # `failed` means the contradiction is not so much tolerated as unread.
            cause = entry.cause if entry is not None else None
            if cause is not None:
                problems.append(f"cause_forbidden:{key}:{cause}")
            if SHUTDOWN_ANOMALY in evidence:
                problems.append(f"anomaly_orphan:{key}")

    for kind in {o.kind for o in record.outcomes}:
        key = _OUTCOME_PAIRING.get(kind)
        if key is None:
            continue
        entry = record.facts.get(key)
        if entry is None or entry.state != FAILED:
            problems.append(f"outcome_unpaired:{kind}")

    # Firing is EVIDENCE, not a verdict input — the arming already made the instance anomalous,
    # so a fired record adds nothing to the verdict and everything to the record. What it must
    # do is line up: it is legal only for a fact the configuration listed, and only where that
    # fact actually reads `failed`.
    for key in sorted(set(record.fired)):
        if key not in record.suppresses:
            problems.append(f"fired_unconfigured:{key}")
        entry = record.facts.get(key)
        if entry is None or entry.state != FAILED:
            problems.append(f"fired_unpaired:{key}")

    return problems


# ---------------------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    """Clean or not, plus everything that made it so. Never just a boolean.

    The audit log has to say WHY an instance ended cleanly and not merely THAT it did: a
    verdict with no reason cannot be checked against C3-1's shutdown table when a CLI changes
    its behaviour underneath us, which C3-1 already caught happening once.
    """

    clean: bool
    problems: tuple[str, ...]
    anomalous: tuple[str, ...]


def reasons(record: Record) -> tuple[str, ...]:
    """Every reason the instance recorded, in the start record as much as the terminator.

    `fault_point_configured` is here, and that is the point of it: a fault armed and never
    fired leaves a clean trigger and clean outcomes, so a verdict reading only the two axes
    would pass a run whose whole purpose was to fail.
    """
    out = [t.reason for t in record.triggers]
    out += [o.kind for o in record.outcomes]
    if record.fault_point:
        out.append(FAULT_POINT_CONFIGURED)
    return tuple(out)


def problems(raw: Any) -> tuple[str, ...]:
    """Shape problems and semantic problems together, for one piece of decoded JSON."""
    record, shape = parse(raw)
    return shape + validate(record)


def verdict(raw: Any) -> Verdict:
    """The one classification every consumer reads (§10.5.1). Never raises.

    A monotonic conjunction over everything recorded, never a lookup on the last thing that
    happened. Monotonic means evidence only ever moves the verdict toward anomalous — the
    no-heal rule of §10.5 stated as an algebraic property rather than as a warning, and applied
    within one instance rather than across restarts.
    """
    record, shape = parse(raw)
    found = shape + validate(record)
    anomalous = tuple(r for r in reasons(record) if not is_clean(r))
    return Verdict(clean=not found and not anomalous, problems=found, anomalous=anomalous)


# ---------------------------------------------------------------------------------------
# The audit LOG (§10.5) — one file, many instances, and the CELL verdict
# ---------------------------------------------------------------------------------------
# Everything above judges ONE instance from a map somebody already assembled. This half is
# where that map comes from, and it is pure over the file's TEXT rather than over a file: the
# rules that matter here — a start with no terminator, a truncated final line, one clean
# restart standing in for the anomalous instance before it — are all statements about a string,
# so an arm can drive them and a mutation can break them. What is left for the caller is
# `open()`, and the one rule that lives there is stated from the other side: for a gated
# server, an audit log that does NOT exist fails the cell, because a gated server whose proxy
# never ran means the gating never happened.
#
# THE CLIENT MAY RESTART THE PROXY, so the file is append-only across instances and the verdict
# is PER INSTANCE. A later instance never heals an earlier one: the cell verdict is the
# conjunction over every instance, and one anomalous or unterminated instance fails the cell no
# matter how many clean instances follow it. "Find the latest verdict for this server", or a
# dedupe keyed on the server name, would each let a clean restart paper over exactly the
# failure this file exists to catch (§10.5).

LINE_START = "start"
LINE_SPAWN = "spawn"
LINE_EVENT = "event"
LINE_TERMINATOR = "terminator"
LINE_KINDS = frozenset({LINE_START, LINE_SPAWN, LINE_EVENT, LINE_TERMINATOR})

# THE PER-INSTANCE GRAMMAR, as a rank per kind: `start`, then an optional `spawn`, then the
# events, and the terminator LAST. Checking only that the start came first accepted `start ->
# terminator -> spawn` and `start -> spawn -> terminator -> event` as clean, which is a
# terminator that does not terminate — the record on which the absence rule, the no-heal rule
# and every completion fact rest, with work recorded after it (review, PR #103). A rank
# comparison rather than a hand-written state machine because the grammar IS an ordering, and
# "the sequence is sorted" cannot disagree with itself about a case.
_LINE_ORDER = {LINE_START: 0, LINE_SPAWN: 1, LINE_EVENT: 2, LINE_TERMINATOR: 3}

# On every line, whatever its kind. `ts` is here because §10.7's whole claim for this log is
# that it is wire-level evidence "per call, with the server, the tool, whether it was allowed
# or refused, and WHEN" — a record with no time is not that.
_ENVELOPE_KEYS = frozenset({"instance", "kind", "ts"})

# The events (§10.5, §10.7). These are the ORDINARY things the proxy did, recorded so the
# invariant can be checked against what was FORWARDED rather than against what was seen: a
# proper-subset allowlist normally MEANS the server advertises off-list tools and the proxy
# strips them, so "the log records no off-list advertisement" would reject exactly the case
# where filtering worked.
TOOLS_ADVERTISED = "tools_advertised"    # a `tools/list` response, after filtering
CALL_FORWARDED = "call_forwarded"        # an on-list `tools/call` that reached the server
CALL_REFUSED = "call_refused"            # an off-list `tools/call` the proxy answered itself
MESSAGE_DROPPED = "message_dropped"      # the documented late-response race (§10.4)

# Each event kind and the payload it must carry — which is also the payload it MAY carry. A
# field nothing consults is the thing being rejected: a `tool` on an advertisement is read by
# nobody, so it can say anything at all and no check will ever disagree with it.
_EVENT_FIELDS = {
    TOOLS_ADVERTISED: ("forwarded", "removed"),
    CALL_FORWARDED: ("tool",),
    CALL_REFUSED: ("tool",),
    MESSAGE_DROPPED: ("reason",),
}
EVENT_KINDS = frozenset(_EVENT_FIELDS)
# Every payload key any event kind uses. The unknown-key sweep subtracts this and the
# per-kind sweep intersects it, so the two partition the record between them: a key here but
# on the wrong kind is `event_payload_forbidden`, and a key nowhere here is `event_key_unknown`.
_EVENT_PAYLOAD_KEYS = frozenset(k for keys in _EVENT_FIELDS.values() for k in keys)

# Per line kind: what it must carry, and what it may. The three axes are deliberately NOT in
# the required column — `parse()` above owns their presence, and a rule with two owners is a
# rule that gets half-fixed. They appear as optional only so the unknown-key sweep does not
# report the keys the record layer is reading.
_LINE_FIELDS = {
    LINE_START: (("server", "pid"), ("fault_point",)),
    LINE_SPAWN: (("child_pid", "child_pgid"), ("guardian_pid",)),
    LINE_TERMINATOR: (("observed",),
                      ("triggers", "outcomes", "facts", "fired", "child_status")),
}


@dataclass(frozen=True)
class Instance:
    """Every line one proxy instance wrote, grouped and nothing more.

    Deliberately not judged yet. Grouping is the step that can lose evidence — a dedupe, a
    "latest wins", a terminator matched to the wrong start — so it happens once, here, and
    every rule below reads the result rather than the file.
    """

    instance_id: str
    start: dict | None = None
    spawn: dict | None = None
    terminator: dict | None = None
    events: tuple[dict, ...] = ()
    problems: tuple[str, ...] = ()       # from GROUPING: duplicates and out-of-order lines

    @property
    def records(self) -> tuple[dict, ...]:
        found = [r for r in (self.start, self.spawn, self.terminator) if r is not None]
        return tuple(found) + self.events


def parse_log(text: str) -> tuple[tuple[Instance, ...], tuple[str, ...]]:
    """The log's TEXT in; instances in first-appearance order and every unusable line out.

    Never raises, for the reason `parse()` never raises: the writer is the thing under
    suspicion, and a traceback out of `verify_post_run` is not a failed cell but an ABSENT
    verdict — the one outcome §10.5 exists to make impossible, arriving through the code
    written to guarantee it.

    A TRUNCATED FINAL LINE IS NOT REPAIRED. It is reported as unparseable and contributes no
    record, so the instance it belonged to has no terminator and the absence rule below fires.
    Repairing it — or ignoring it as "just a partial write" — would turn a proxy killed
    mid-record into one that ended cleanly, which is the precise inversion the absence rule
    exists to prevent (§10.9).
    """
    problems: list[str] = []
    order: list[str] = []
    grouped: dict[str, dict] = {}

    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()          # the newline that terminated the last record, not a line

    for i, line in enumerate(lines):
        if not line.strip():
            problems.append(f"blank_line:{i}")
            continue
        try:
            raw = json.loads(line)
        except ValueError:
            problems.append(f"unparseable_line:{i}")
            continue
        if not isinstance(raw, dict):
            problems.append(f"line_not_a_map:{i}")
            continue
        instance_id = raw.get("instance")
        if not isinstance(instance_id, str) or not instance_id:
            problems.append(f"line_instance:{i}:{instance_id!r}")
            continue
        kind = raw.get("kind")
        # `isinstance` FIRST. `["start"] in LINE_KINDS` raises `TypeError: unhashable type`,
        # which is the envelope crash of §10.4 arriving by a route that skipped envelope
        # validation — inside the reader whose entire contract is that it does not raise.
        if not isinstance(kind, str) or kind not in LINE_KINDS:
            problems.append(f"line_kind:{i}:{kind!r}")
            continue

        entry = grouped.get(instance_id)
        if entry is None:
            entry = grouped[instance_id] = {LINE_START: None, LINE_SPAWN: None,
                                            LINE_TERMINATOR: None, "events": [], "kinds": [],
                                            "problems": []}
            order.append(instance_id)
        entry["kinds"].append(kind)
        if kind == LINE_EVENT:
            entry["events"].append(raw)
        elif entry[kind] is not None:
            # Two starts, or two terminators, for one instance id. Overwriting would let the
            # second answer for the first, which is the same substitution the per-instance
            # verdict exists to refuse one level up.
            entry["problems"].append(f"{kind}_duplicate")
        else:
            entry[kind] = raw

    instances = []
    for instance_id in order:
        entry = grouped[instance_id]
        kinds = entry["kinds"]
        ranks = [_LINE_ORDER[k] for k in kinds]
        if ranks != sorted(ranks):
            # The start record is written BEFORE the child is spawned and therefore before a
            # single byte is forwarded (§10.5), and the terminator is written LAST, after every
            # step of the teardown. An append-only file that disagrees is a writer that buffered
            # or reordered — and then neither boundary holds: the partition between "logged a
            # start" and "spawned nothing" at one end, and at the other a terminator whose
            # completion facts describe steps that had not happened when it was written.
            entry["problems"].append(f"records_out_of_order:{','.join(kinds)}")
        instances.append(Instance(instance_id, entry[LINE_START], entry[LINE_SPAWN],
                                  entry[LINE_TERMINATOR], tuple(entry["events"]),
                                  tuple(entry["problems"])))
    return tuple(instances), tuple(problems)


def assemble(inst: Instance) -> dict:
    """The one map `verdict()` reads: the terminator's axes plus the START record's fault point.

    The fault point comes from the start and only from the start, in both directions. Taking it
    from wherever it appears would let a terminator declare an arming that never happened; not
    taking it from the start would let a terminator hide one that did — and hiding it is the
    case that matters, since `fault_point_configured` is the clause that stops an armed-but-
    silent hook from producing a passing run (§10.5.1). A `fault_point` on the terminator is
    dropped here and reported by the unknown-key sweep, so it can neither add nor subtract.
    """
    raw = {k: v for k, v in (inst.terminator or {}).items() if k != "fault_point"}
    if inst.start is not None and "fault_point" in inst.start:
        raw["fault_point"] = inst.start["fault_point"]
    return raw


def _field_problems(record: dict, kind: str) -> list[str]:
    """The keys a line must carry and the keys it may — closed in both directions."""
    problems: list[str] = []
    required, optional = _LINE_FIELDS[kind]
    for key in required:
        if key not in record:
            problems.append(f"{kind}_key_missing:{key}")
    known = _ENVELOPE_KEYS.union(required, optional)
    for key in sorted(set(record) - known):
        problems.append(f"{kind}_key_unknown:{key}")
    return problems


def _ts_problems(inst: Instance) -> list[str]:
    """Every line carries a time, EVENTS INCLUDED — and they are the ones that matter most.

    §10.7's claim for this log is that it is wire-level evidence "per call, with the server, the
    tool, whether it was allowed or refused, and WHEN". The event records are the per-call half
    of that, so a rule applied only to the three lifecycle records would leave the half the
    claim is actually about unchecked. One loop over every record rather than a clause inside
    the per-kind checks, because a rule with one owner cannot be applied to some of its cases.
    """
    problems = []
    for record in inst.records:
        ts = record.get("ts")
        if not is_json_int(ts) and not isinstance(ts, float):
            problems.append(f"{record.get('kind')}_ts:{ts!r}")
    return problems


def _names(entry: dict, key: str, index: int, problems: list[str]) -> list[str]:
    """A list of tool names on an event, with every non-name reported rather than skipped."""
    out = []
    for j, item in enumerate(_as_list(entry, key, problems, required=True)):
        if isinstance(item, str):
            out.append(item)
        else:
            problems.append(f"event_name:{index}:{key}:{j}")
    return out


def _event_problems(events: tuple[dict, ...], allowed: frozenset[str]) -> list[str]:
    """The guarantee of §10.6, checked against what the proxy says it actually forwarded.

    BOTH DIRECTIONS, and the second one is the one a lazy implementation passes. That an
    off-list tool was never advertised and never called is satisfied in full by a proxy that
    stripped everything and refused everything — "rejects everything scores full marks" is the
    same defect §10.9 spends a case on for the structural validator, and the allowlist is where
    it would do the most damage, because a silently reduced tool surface is a wrong eval rather
    than a failed one.
    """
    problems: list[str] = []
    for i, event in enumerate(events):
        problems.extend(f"event_key_unknown:{i}:{key}"
                        for key in sorted(set(event) - _ENVELOPE_KEYS - {"event"}
                                          - _EVENT_PAYLOAD_KEYS))
        kind = event.get("event")
        if not isinstance(kind, str) or kind not in EVENT_KINDS:
            problems.append(f"event_unknown:{i}:{kind!r}")
            continue
        for key in _EVENT_FIELDS[kind]:
            if key not in event:
                problems.append(f"event_key_missing:{i}:{kind}:{key}")
        for key in sorted(_EVENT_PAYLOAD_KEYS - set(_EVENT_FIELDS[kind])):
            if key in event:
                problems.append(f"event_payload_forbidden:{i}:{kind}:{key}")

        if kind == TOOLS_ADVERTISED:
            for name in _names(event, "forwarded", i, problems):
                if name not in allowed:
                    problems.append(f"off_list_advertised:{name}")
            for name in _names(event, "removed", i, problems):
                if name in allowed:
                    problems.append(f"on_list_removed:{name}")
        elif kind in (CALL_FORWARDED, CALL_REFUSED):
            tool = event.get("tool")
            if not isinstance(tool, str) or not tool:
                problems.append(f"event_tool:{i}:{tool!r}")
            elif kind == CALL_FORWARDED and tool not in allowed:
                problems.append(f"off_list_call_forwarded:{tool}")
            elif kind == CALL_REFUSED and tool in allowed:
                problems.append(f"on_list_call_refused:{tool}")
        else:
            # A CLOSED SET, imported rather than re-derived. Free prose here is how an id or a
            # method name the peer chose ends up in an archived artifact, and a reader that
            # accepted any non-empty string would never say so.
            reason = event.get("reason")
            if not isinstance(reason, str) or reason not in DROP_REASONS:
                problems.append(f"event_reason:{i}:{reason!r}")
    return problems


@dataclass(frozen=True)
class InstanceVerdict:
    """One instance's ending, named. Never a bare boolean, for §10.5.1's fourth rule."""

    instance_id: str
    clean: bool
    problems: tuple[str, ...]
    anomalous: tuple[str, ...]


def instance_verdict(inst: Instance, *, server: str,
                     allowed: frozenset[str]) -> InstanceVerdict:
    """One instance, judged: the log's own rules, then §10.5.1's over the assembled record."""
    problems = list(inst.problems)
    problems.extend(_ts_problems(inst))

    if inst.start is None:
        # The partition of §10.5 has two halves and this is neither: an instance that logged a
        # start must also log a terminator, and an instance that logged nothing spawned nothing.
        # A terminator with no start is a third thing, and what it says cannot be trusted —
        # there is no record of the boundary it claims to close.
        problems.append("start_absent")
    else:
        problems.extend(_field_problems(inst.start, LINE_START))
        name = inst.start.get("server")
        if not isinstance(name, str) or name != server:
            problems.append(f"server_mismatch:{name!r}")
        if not is_json_int(inst.start.get("pid")):
            problems.append(f"start_pid:{inst.start.get('pid')!r}")

    if inst.spawn is not None:
        problems.extend(_field_problems(inst.spawn, LINE_SPAWN))
        pid, pgid = inst.spawn.get("child_pid"), inst.spawn.get("child_pgid")
        if not is_json_int(pid) or not is_json_int(pgid):
            problems.append(f"spawn_ids:{pid!r}:{pgid!r}")
        elif pid != pgid:
            # `start_new_session=True` makes the child a session and process-group leader, so
            # its pgid IS its pid. That launch decision exists only so step 4 has a group to
            # signal (§10.5), and a spawn record disagreeing with it means the group the
            # teardown went on to kill was not the one the child was in.
            problems.append(f"spawn_not_group_leader:{pid}:{pgid}")

    problems.extend(_event_problems(inst.events, allowed))

    anomalous: tuple[str, ...] = ()
    if inst.terminator is None:
        # THE ABSENCE RULE (§10.5). It covers the four endings no enumeration can reach — an
        # uncatchable SIGKILL to the proxy, a crash after the start record, a teardown that
        # died before step 6, and a truncated final line — because in each the process that
        # would have named a reason is already gone. The record layer is deliberately NOT run
        # here: it would report a dozen missing axes and describe a broken writer, when what
        # happened is that the writer stopped.
        problems.append("terminator_absent")
    else:
        problems.extend(_field_problems(inst.terminator, LINE_TERMINATOR))
        problems.extend(_observed_problems(inst.terminator))
        record, shape = parse(assemble(inst))
        problems.extend(shape)
        problems.extend(validate(record))
        anomalous = tuple(r for r in reasons(record) if not is_clean(r))
        problems.extend(_spawn_partition(record, inst))

    return InstanceVerdict(inst.instance_id, not problems and not anomalous,
                           tuple(problems), anomalous)


def _observed_problems(terminator: dict) -> list[str]:
    """§10.7's version telemetry, cross-checked against the gate that produced it.

    Recording is evidence, not control — the thing that keeps an unimplemented version from
    being forwarded is §10.2's gate. Which is exactly why this check is worth having: every
    version that reaches `observed` passed that gate, so one outside the implemented set is the
    gate having leaked, reported by the telemetry that was supposed to merely describe it.
    """
    problems: list[str] = []
    for j, version in enumerate(_as_list(terminator, "observed", problems, required=False)):
        if not isinstance(version, str):
            problems.append(f"observed_not_a_string:{j}")
        elif version not in IMPLEMENTED_VERSIONS:
            problems.append(f"observed_unimplemented:{version}")
    return problems


def _spawn_partition(record: Record, inst: Instance) -> list[str]:
    """`spawn_failed` and a spawn record are exact complements, and both directions matter.

    The signal handlers are why this is exact rather than approximate: a handler sets a flag and
    the control path acts on it (§10.5), so a signal arriving between the start record and the
    spawn does not skip the spawn. There is therefore no ending in which a terminator exists,
    the spawn did not happen, and the latch says anything but `spawn_failed` — and a record
    claiming one is claiming four `not_applicable` facts it has no licence for.
    """
    if record.latch is None:
        return []                       # `triggers_empty` already says what is wrong here
    if record.latch == SPAWN_FAILED and inst.spawn is not None:
        return ["spawn_record_after_spawn_failed"]
    if record.latch != SPAWN_FAILED and inst.spawn is None:
        return ["spawn_record_missing"]
    return []


@dataclass(frozen=True)
class LogVerdict:
    """The CELL's verdict for one gated server: the conjunction over every instance."""

    clean: bool
    problems: tuple[str, ...]                    # of the file, not of any one instance
    instances: tuple[InstanceVerdict, ...]

    @property
    def unclean(self) -> tuple[InstanceVerdict, ...]:
        return tuple(v for v in self.instances if not v.clean)


def log_verdict(text: str, *, server: str, allowed: frozenset[str]) -> LogVerdict:
    """The whole file, judged. Never raises.

    THE STRUCTURAL CLAUSE IS FIRST, and here it is `no_instances`. The conjunction below is
    universally quantified, so it is vacuously true of a log with nothing in it — and a log with
    nothing in it is a gated server whose proxy never wrote a start record, which means the
    gating never happened and a pass would certify an ungated run. Same rule as §10.5.1's
    `triggers_empty`, one level up, and reported as a code rather than as a silent `False` so
    the verdict still says why.
    """
    instances, problems = parse_log(text)
    verdicts = tuple(instance_verdict(i, server=server, allowed=allowed) for i in instances)
    if not verdicts:
        problems = problems + ("no_instances",)
    return LogVerdict(clean=not problems and all(v.clean for v in verdicts),
                      problems=problems, instances=verdicts)
