"""Adapter warnings that outlive the process that printed them.

An adapter that notices something wrong but not fatal — a declared MCP server the run
never brought up, a model probe that answered but proved nothing — has, until now, said so
with ``print(..., file=sys.stderr)``. That reaches an operator watching a terminal and
nobody else. ``execute()`` archives the CHILD's stderr, not the harness's own, so the
warning appeared in no result, no cell artifact and no summary; review found the claude
server-health warning saying "assertions about them will fail for a reason the results will
not show" while being itself the reason the results would not show it.

So a warning goes two places at once: to stderr, unchanged, for the operator; and into the
enclosing collection window, which the caller attaches to the ``RunResult``. What made this
worth a module rather than a ``redirect_stderr`` at the call site is that cells run in
parallel threads — ``sys.stderr`` is process-global, so redirecting it in one thread
captures every other thread's output too, and the warning would land on an unrelated cell's
result. The sink is thread-local, and only messages routed through ``warn`` are collected,
so what a result claims about itself is always something that happened during its own run.
"""
from __future__ import annotations

import sys
import threading
from contextlib import contextmanager
from typing import Iterator

_local = threading.local()


def warn(message: str, *, echo: bool = True) -> None:
    """Emit an adapter warning: to the operator now, and to the artifacts afterwards.

    Safe to call with no window open — outside one this is exactly the ``print`` it
    replaced, which is what the probe and build-time paths want.

    ``echo=False`` records without printing, for warnings that are rate-limited on the
    terminal. Once-per-process suppression is right for an operator reading a matrix scroll
    past and wrong for the artifacts, where it would leave cell 1 carrying a finding that
    applies equally to cells 2..n — a per-cell record that lies by omission. The two
    audiences get the frequency each one needs.
    """
    if echo:
        print(message, file=sys.stderr)
    sink = getattr(_local, "sink", None)
    if sink is not None:
        sink.append(message)


@contextmanager
def collecting() -> Iterator[list[str]]:
    """Collect every ``warn`` this thread emits inside the block.

    Windows nest by restoring the previous sink rather than clearing it, so a nested
    collection cannot silently swallow the warnings its caller was gathering.
    """
    previous = getattr(_local, "sink", None)
    sink: list[str] = []
    _local.sink = sink
    try:
        yield sink
    finally:
        _local.sink = previous
        if previous is not None:
            previous.extend(sink)
