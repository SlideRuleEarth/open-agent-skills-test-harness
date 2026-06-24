"""CLI progress indicator — spinner + elapsed timer + phase updates.

Provides a single-line status that overwrites itself in-place on a TTY,
falling back to plain newline-per-phase output when stderr is not a terminal.
The spinner runs on a background thread so the main thread stays blocked on
the agent subprocess without any coordination.

Usage (from the runner)::

    with Progress(total_cells=3) as p:
        p.update(cell=1, phase="running agent", eval_name="api+params", model="opus-4.8")
        # ... long agent run ...
        p.update(cell=1, phase="running judge")
        # ...
        p.done(cell=1, passed=True)
"""

from __future__ import annotations

import sys
import threading
import time

_BRAILLE = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _fmt_elapsed(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


class Progress:
    def __init__(self, total_cells: int = 1, file=None):
        self._file = file or sys.stderr
        self._tty = hasattr(self._file, "isatty") and self._file.isatty()
        self._total = total_cells
        self._cell = 0
        self._phase = ""
        self._label = ""
        self._start = time.monotonic()
        self._phase_start = self._start
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_line_len = 0

    def __enter__(self):
        if self._tty:
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self._tty:
            self._clear_line()

    def update(self, *, cell: int, phase: str,
               eval_name: str = "", model: str = ""):
        with self._lock:
            self._cell = cell
            self._phase = phase
            self._phase_start = time.monotonic()
            if eval_name or model:
                parts = []
                if eval_name:
                    parts.append(eval_name)
                if model:
                    parts.append(model)
                self._label = " — ".join(parts)
        if not self._tty:
            self._print_plain()

    def done(self, *, cell: int, passed: bool | None = None):
        with self._lock:
            self._cell = cell
            self._phase = ""
        if not self._tty:
            mark = "✓" if passed else ("✗" if passed is False else "·")
            elapsed = _fmt_elapsed(time.monotonic() - self._start)
            self._file.write(f"  {mark} cell {cell}/{self._total} done [{elapsed}]\n")
            self._file.flush()

    # --- internals ------------------------------------------------------------

    def _spin(self):
        idx = 0
        while not self._stop.wait(0.1):
            with self._lock:
                line = self._render(idx)
            self._write_line(line)
            idx = (idx + 1) % len(_BRAILLE)

    def _render(self, frame: int) -> str:
        elapsed = _fmt_elapsed(time.monotonic() - self._start)
        phase_elapsed = _fmt_elapsed(time.monotonic() - self._phase_start)
        spinner = _BRAILLE[frame]
        cell_str = f"cell {self._cell}/{self._total}" if self._total > 1 else ""
        parts = [f"{spinner} [{elapsed}]"]
        if cell_str:
            parts.append(cell_str)
        if self._phase:
            parts.append(self._phase)
            if phase_elapsed != elapsed:
                parts.append(f"({phase_elapsed})")
        if self._label:
            parts.append(self._label)
        return "  ".join(parts)

    def _write_line(self, line: str):
        pad = max(self._last_line_len - len(line), 0)
        self._file.write(f"\r{line}{' ' * pad}")
        self._file.flush()
        self._last_line_len = len(line)

    def _clear_line(self):
        if self._last_line_len:
            self._file.write("\r" + " " * self._last_line_len + "\r")
            self._file.flush()
            self._last_line_len = 0

    def _print_plain(self):
        elapsed = _fmt_elapsed(time.monotonic() - self._start)
        cell_str = f"cell {self._cell}/{self._total}: " if self._total > 1 else ""
        self._file.write(f"  [{elapsed}] {cell_str}{self._phase}")
        if self._label:
            self._file.write(f"  {self._label}")
        self._file.write("\n")
        self._file.flush()
