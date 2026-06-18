"""Adapter registry.

Adding a new agent = one Adapter subclass + one line here. Everything else
(runner, assertions, judge, reports) works against the normalized output.
"""

from __future__ import annotations

from typing import Optional

from .antigravity import AntigravityAdapter
from .base import Adapter, ParseOutput, RunOptions
from .claude import ClaudeAdapter
from .codex import CodexAdapter

_ADAPTERS: dict[str, Adapter] = {
    a.name: a
    for a in (
        ClaudeAdapter(),
        CodexAdapter(),
        AntigravityAdapter(),
    )
}

# Friendly aliases.
_ALIASES = {
    "claude-code": "claude",
    "cc": "claude",
    "openai": "codex",
    "agy": "antigravity",
    "google": "antigravity",
}


def get_adapter(name: str) -> Adapter:
    key = name.strip().lower()
    key = _ALIASES.get(key, key)
    if key not in _ADAPTERS:
        raise KeyError(
            f"unknown agent {name!r}; known: {', '.join(sorted(_ADAPTERS))}"
        )
    return _ADAPTERS[key]


def all_adapters() -> list[Adapter]:
    return list(_ADAPTERS.values())


def adapter_names() -> list[str]:
    return list(_ADAPTERS.keys())


def register(adapter: Adapter) -> None:
    """Register a custom adapter at runtime (for out-of-tree agents)."""
    _ADAPTERS[adapter.name] = adapter


__all__ = [
    "Adapter",
    "ParseOutput",
    "RunOptions",
    "get_adapter",
    "all_adapters",
    "adapter_names",
    "register",
]
