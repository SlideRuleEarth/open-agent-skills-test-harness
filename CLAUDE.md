# Working in this repo

**Authoritative documents.** `harness/DESIGN_MCP_Support.md` is the specification for MCP
support — read the section you are about to change before changing it. `harness/TODO_Contained_HOME.md`
§4 is the list of mistakes already made here, kept so they are not made again, and its
verification block holds the commands and the current arm/mutation/check counts. **Do not copy
those numbers anywhere else.** A count that lives in two places drifts, and the drift is
silent; point at the block instead. The same holds for a count restated in prose beside the
table it counts — write the phrase that re-derives itself ("every child-and-group fact")
rather than the numeral, which is right until someone adds a row.

**Verification is not optional.** Any change to `harness/agentskill_evals/` runs the selftest,
the mutation suite and Ruff before it is claimed as done, per that block. A mutation suite that
reports fewer mutations than last time has lost coverage — the one failure neither command
reports as an error.

## Four rules that apply before you write the code

**Fix the principle, not the reproduction.** State the argument that justifies your fix, then
read what it actually quantifies over. If the argument is broader than the case in front of you
— and it usually is — every site it covers is part of the same fix, now, in the same commit.
Three review rounds on PR #100 were one rule being widened one route at a time, because each
fix stopped exactly where the reproduction stopped. The tell is a justification that never
mentions the specific case it was written for.

**A new fact about whether something is trustworthy joins the existing predicate.** When you
learn a new way a run, row, request or message can be untrustworthy, add it where the existing
reasons already live — the one function every caller reads — never as a parallel flag consulted
by one caller. The tell is a new boolean that only one call site checks: the other callers
will keep publishing their old conclusion, and the exit status will disagree with the output.

The dual of that mistake is one field asked to carry two independent facts. Why something
stopped and what happened while it was cleaning up are not alternatives — both are true of the
same ending — so a verdict over them is a conjunction over every fact, never a lookup on
whichever arrived last. The tell is a flat enumeration of things that occur at *different
phases* of one lifecycle: if two entries can be true at once, they belong on different axes,
and the axes are usually asymmetric in a way that names the case you forgot.

**Every assertion must be able to fail, and you must be able to say how.** Before adding a
check, write down what a broken implementation produces and confirm your assertion rejects it.
Four specific traps, all of which have shipped here: a mixed `and`/`or` that is true by
precedence regardless of the interesting term — parenthesize, or split into several checks; a
placeholder expression standing in for something not easily reachable — extract the predicate
into a function instead; a check that only exercises the code path where the bug cannot
appear — if a fix depends on a path being taken, test the path where it is not taken; and an
assertion that passes because nothing was recorded, when *not running the code at all* also
records nothing. That last one needs a positive fact to check — a step that says it ran, a
hook that reports it fired — and often a witness from outside the process under test, since
a claim and the thing it claims about must not have the same author. Its pure form is
`all(...)` over a collection nothing was put into: any universally quantified check needs a
structural clause ahead of it saying what must be there. A witness may only assert what it
can actually observe from where it stands — narrow the claim to that, rather than narrowing
what would have been worth claiming. And the witness itself rests on a premise: an
observation channel that was never connected reports the same silence as a channel reporting
success, so make it say something positive before you trust it saying nothing — and prove it
can still report the failure by causing that failure on purpose, which is what the mutation
suite does for the arms and what a negative control does for a fixture.

**A duplicated rule must be pinned to its original.** Some files cannot import the code they
must agree with — `harness/fixtures/` runs as a subprocess of an agent CLI with only the
standard library reachable. Duplicating a small rule there is acceptable; a duplicate that can
drift silently is not. Assert the copy equal to the original on the cases that distinguish
them, as `verify_mcp_fixtures.py` does for `_id_key`, `_valid_request_id` and
`_envelope_shape`. Where import *is* possible, import — a check that re-derives a definition
cannot disagree with it.

## Measurements and probes

A probe's output is evidence for a design decision, so it is under test like anything else.
Keep classification in named functions rather than an `elif` chain inside `main()`, so it can
be driven on synthetic rows. A fleet-wide negative requires every row answered; absence of a
positive result is not a negative result. And before trusting a reading, ask whether the
quantity is observable from where the instrument stands at all — C3-3 spent three rounds being
made more careful before that question was asked, and the answer was no.
