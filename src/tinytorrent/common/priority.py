"""Torrent priority tiers, shared between the daemon and the CLI."""

from __future__ import annotations

from enum import Enum


class Priority(str, Enum):
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


# Higher number = higher priority, for comparisons and preemption tie-breaks.
_ORDER = {Priority.LOW: 0, Priority.NORMAL: 1, Priority.HIGH: 2}


def is_higher(a: Priority, b: Priority) -> bool:
    """True if priority ``a`` strictly outranks priority ``b``."""
    return _ORDER[a] > _ORDER[b]


def from_str(value: str) -> Priority:
    try:
        return Priority(value.lower())
    except ValueError as exc:
        valid = ", ".join(p.value for p in Priority)
        raise ValueError(f"invalid priority {value!r} (expected one of: {valid})") from exc
