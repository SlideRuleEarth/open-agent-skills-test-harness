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

WHY IT IS WRITTEN BEFORE THE I/O HALF. Everything below is a shape, not a behaviour, and
shapes are where twelve rounds of review on the design found their defects: a step that failed
had no legal value to record, a catch-all that could not be named for most steps, a pairing key
coarser than the thing it keyed, arming a fault standing in for firing it. Prose held every one
of those without complaining. A dataclass and a validator do not.

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

from dataclasses import dataclass

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

# The typed outcome a `failed` fact pairs with, where one exists. `failed` exists because the
# two axes describe the same events and must be able to agree: without it, a teardown that ran
# and did not work has NO structurally valid record at all, since `shutdown_reap_failed` could
# not be said of `child_reaped` and the only remaining spelling — omitting the fact — is what
# the validator calls malformed.
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


def is_clean(reason: str) -> bool:
    """Total over both axes and the start reason. Anything unrecognized is an ANOMALY.

    There is deliberately no default-clean branch. Adding a reason without classifying it must
    fail the cell rather than pass it — the failure has to be the loud one, or the enumeration
    decays into documentation.
    """
    return reason in CLEAN_REASONS


# ---------------------------------------------------------------------------------------
# The structural validator (§10.5.1)
# ---------------------------------------------------------------------------------------
# Owned by the READER. The proxy may assert the same shape before writing, but that assertion
# is not the check: a proxy broken enough to write an empty trigger list is precisely the wrong
# process to ask whether it did, and a claim and the thing it claims about must not have the
# same author.


def _fact_state(facts: dict, key: str) -> str | None:
    entry = facts.get(key)
    return entry.get("state") if isinstance(entry, dict) else None


def validate(instance: dict) -> tuple[str, ...]:
    """Every way this record is malformed, as stable problem codes, in a fixed order.

    A record that fails is `malformed_record` — an anomaly whose subject is the PROXY rather
    than the run. Codes rather than prose so an arm can assert which rule fired: "the validator
    rejected it" is satisfied by a validator that rejects everything, which is why §10.9 also
    drives the one legal record that looks malformed (the `spawn_failed` shape, where every
    child-and-group fact is legitimately `not_applicable`).
    """
    problems: list[str] = []

    triggers = instance.get("triggers") or []
    if not triggers:
        # The vacuity guard. Both clauses below quantify over collections, and an empty one
        # satisfies them while contradicting the definition of an instance, which HAS a latch.
        problems.append("triggers_empty")
    for entry in triggers:
        reason = entry.get("reason") if isinstance(entry, dict) else None
        if reason not in TRIGGERS:
            problems.append(f"trigger_unknown:{reason}")
    latch = triggers[0].get("reason") if triggers and isinstance(triggers[0], dict) else None

    outcomes = instance.get("outcomes") or []
    for entry in outcomes:
        kind = entry.get("kind") if isinstance(entry, dict) else None
        if kind not in OUTCOMES:
            problems.append(f"outcome_unknown:{kind}")

    facts = instance.get("facts")
    facts = facts if isinstance(facts, dict) else {}
    for key in FACTS:
        if key not in facts:
            problems.append(f"fact_missing:{key}")
    for key in sorted(facts):
        if key not in FACTS:
            problems.append(f"fact_unknown:{key}")

    for key in FACTS:
        state = _fact_state(facts, key)
        if state is None or state not in STATES:
            if key in facts:
                problems.append(f"state_unknown:{key}:{state}")
            continue
        if state == NOT_APPLICABLE and latch not in _NOT_APPLICABLE_LICENSED_BY[key]:
            problems.append(f"not_applicable_unlicensed:{key}")

    problems.extend(_pairing_problems(facts, outcomes, instance))

    # Only a process that reaped a child can hold its exit status, so `done` without one claims
    # a reap while lacking the single piece of evidence a reaper necessarily has, and a status
    # attached to `failed` or `not_applicable` is a status for a child that was never reaped —
    # invented, because there was nowhere to get it.
    has_status = "child_status" in instance
    if _fact_state(facts, CHILD_REAPED) == DONE and not has_status:
        problems.append("child_status_missing")
    if _fact_state(facts, CHILD_REAPED) != DONE and has_status:
        problems.append("child_status_forbidden")

    return tuple(problems)


def _pairing_problems(facts: dict, outcomes: list, instance: dict) -> list[str]:
    """The `failed` pairings, both directions, keyed by the EXACT FACT and never by the step.

    A step number is coarser than the thing it identifies — step 2 owns both
    `child_stdin_closed` and `drain_ended` — so a `shutdown_anomaly(step=2)` would license
    `failed` on either of them, or on both, having arisen from one operation. A pairing key one
    level coarser than what it pairs is not a weaker check; it is a check that passes for the
    wrong reason while reading like coverage.
    """
    problems: list[str] = []
    kinds = [e.get("kind") for e in outcomes if isinstance(e, dict)]
    anomaly_facts = {e.get("fact") for e in outcomes
                     if isinstance(e, dict) and e.get("kind") == SHUTDOWN_ANOMALY}
    configured = _configured(instance)
    # A malformed entry becomes `None` rather than being skipped, so it lands in the
    # `fired_unconfigured` check below instead of vanishing. Dropping what a loop does not
    # recognize is how a record ends up validating clean because part of it was unreadable —
    # the same shape as every other silent-absence defect this section has been through.
    fired = {e.get("fact") if isinstance(e, dict) else None
             for e in instance.get("fired") or []}

    for entry in outcomes:
        if isinstance(entry, dict) and entry.get("kind") == SHUTDOWN_ANOMALY:
            if entry.get("fact") not in FACTS:
                problems.append(f"anomaly_unkeyed:{entry.get('fact')}")

    for key in FACTS:
        state = _fact_state(facts, key)
        typed = _TYPED_PAIRING[key]
        paired = ((typed is not None and typed in kinds)
                  or key in anomaly_facts
                  or key in fired)
        if state == FAILED and not paired:
            problems.append(f"failed_unpaired:{key}")
        if state != FAILED and key in anomaly_facts:
            problems.append(f"anomaly_orphan:{key}")

    for kind in kinds:
        key = _OUTCOME_PAIRING.get(kind)
        if key is not None and _fact_state(facts, key) != FAILED:
            problems.append(f"outcome_unpaired:{kind}")

    # Firing is EVIDENCE, not a verdict input — the arming already made the instance anomalous,
    # so a fired record adds nothing to the verdict and everything to the record. What it must
    # do is line up: only a fired record may pair with a completion fact, and only for a fact
    # the configuration listed.
    for key in sorted(fired, key=str):
        if key not in configured:
            problems.append(f"fired_unconfigured:{key}")
        if _fact_state(facts, key) != FAILED:
            problems.append(f"fired_unpaired:{key}")

    return problems


def _configured(instance: dict) -> frozenset[str]:
    """The completion facts the fault point is armed on, if any.

    Named facts rather than step numbers, for the same reason the anomaly is: steps 3 and 5
    produce ONE fact between them, so a step-keyed fault configuration could not say which.
    """
    fault = instance.get("fault_point")
    if not isinstance(fault, dict):
        return frozenset()
    return frozenset(f for f in fault.get("suppresses") or [] if isinstance(f, str))


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


def reasons(instance: dict) -> tuple[str, ...]:
    """Every reason the instance recorded, in the start record as much as the terminator.

    `fault_point_configured` is here, and that is the point of it: a fault armed and never
    fired leaves a clean trigger and clean outcomes, so a verdict reading only the two axes
    would pass a run whose whole purpose was to fail.
    """
    out = [e["reason"] for e in instance.get("triggers") or []
           if isinstance(e, dict) and "reason" in e]
    out += [e["kind"] for e in instance.get("outcomes") or []
            if isinstance(e, dict) and "kind" in e]
    if _configured(instance):
        out.append(FAULT_POINT_CONFIGURED)
    return tuple(out)


def verdict(instance: dict) -> Verdict:
    """The one classification every consumer reads (§10.5.1).

    A monotonic conjunction over everything recorded, never a lookup on the last thing that
    happened. Monotonic means evidence only ever moves the verdict toward anomalous — the
    no-heal rule of §10.5 stated as an algebraic property rather than as a warning, and applied
    within one instance rather than across restarts.
    """
    problems = validate(instance)
    anomalous = tuple(r for r in reasons(instance) if not is_clean(r))
    return Verdict(clean=not problems and not anomalous,
                   problems=problems, anomalous=anomalous)
