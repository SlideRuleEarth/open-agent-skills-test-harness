"""C3 — the harness-owned filtering proxy: the AUDIT RECORD and its VERDICT.

`DESIGN_MCP_Support.md` §10.5 and §10.5.1 are the specification; this module is their whole
implementation, and like `mcp_proxy.py` it does no I/O. The proxy writes these records and the
post-run check reads them, but the question they both ask — *did this instance end cleanly?* —
is answered here, once, by `verdict()`.

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

from dataclasses import dataclass, field
from typing import Any

from .mcp_proxy import ANOMALY_KINDS

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


def is_exit_status(value: Any) -> bool:
    """A JSON integer, and `True` is not one.

    `isinstance(True, int)` holds in Python, so the obvious check accepts a boolean — and a
    boolean here is a record claiming an exit status it does not have, which is the same
    fabrication the presence rule exists to catch, one type down.
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
    if present and not is_exit_status(record.child_status):
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
