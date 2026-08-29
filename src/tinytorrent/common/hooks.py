"""Shared schema for event hooks: the ``hooks`` section of config.json.

This module only defines and validates the *data* (which events exist,
what a configured command looks like, how ``%placeholder%`` substitution
works) so that both ``common/config.py`` (parsing) and
``daemon/hooks.py`` (execution) can depend on it without either depending
on the other. See DESIGN.md's "Event Hooks" section for the full design.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

DEFAULT_TIMEOUT_SECONDS = 60.0
_VALID_ON_FAILURE = {"ignore", "abort_remaining"}
_PLACEHOLDER_RE = re.compile(r"%([A-Za-z0-9_]+)%")


class HookEvent(str, Enum):
    """Torrent lifecycle points a user can attach commands to."""

    METADATA_FETCHED = "metadata_fetched"
    DOWNLOAD_STARTED = "download_started"
    DOWNLOAD_COMPLETED = "download_completed"
    DOWNLOAD_ERROR = "download_error"
    TORRENT_PURGED = "torrent_purged"


class HookConfigError(Exception):
    pass


@dataclass(frozen=True)
class HookCommand:
    """One configured command: an argv list, never a shell string.

    ``argv`` elements may contain ``%placeholder%`` tokens (see
    ``substitute_argv``) that get filled in from the triggering torrent
    before the command runs. Substitution happens per-argument, so a
    hostile value (e.g. a torrent name pulled from untrusted metadata)
    can never be interpreted as shell syntax.
    """

    argv: tuple[str, ...]
    on_failure: str = "ignore"  # "ignore" | "abort_remaining" (this event's later commands)
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS


def parse_hooks_config(raw: object) -> "dict[HookEvent, tuple[HookCommand, ...]]":
    """Parse and validate the ``hooks`` section of config.json.

    Expected shape::

        {
          "download_completed": [
            {
              "command": ["cp", "-r", "%download_dir%/%name%", "/media/done/"],
              "on_failure": "ignore",
              "timeout_seconds": 60
            }
          ]
        }

    Commands for one event run in the order listed. Unknown event names
    or malformed entries raise ``HookConfigError`` (wrapped as a
    ``ConfigError`` by ``common/config.py``).
    """
    if not isinstance(raw, dict):
        raise HookConfigError("'hooks' must be an object mapping event names to lists of commands")

    valid_events = {e.value for e in HookEvent}
    parsed: dict[HookEvent, tuple[HookCommand, ...]] = {}
    for event_name, entries in raw.items():
        if event_name not in valid_events:
            raise HookConfigError(
                f"unknown hook event {event_name!r} (expected one of: {', '.join(sorted(valid_events))})"
            )
        if not isinstance(entries, list) or not entries:
            raise HookConfigError(f"hooks.{event_name} must be a non-empty list of command objects")
        parsed[HookEvent(event_name)] = tuple(
            _parse_command(event_name, index, entry) for index, entry in enumerate(entries)
        )
    return parsed


def _parse_command(event_name: str, index: int, entry: object) -> HookCommand:
    where = f"hooks.{event_name}[{index}]"
    if not isinstance(entry, dict):
        raise HookConfigError(f"{where} must be an object")

    argv = entry.get("command")
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise HookConfigError(f"{where}.command must be a non-empty list of strings")

    on_failure = entry.get("on_failure", "ignore")
    if on_failure not in _VALID_ON_FAILURE:
        raise HookConfigError(
            f"{where}.on_failure must be one of: {', '.join(sorted(_VALID_ON_FAILURE))}"
        )

    timeout_raw = entry.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    try:
        timeout_seconds = float(timeout_raw)
    except (TypeError, ValueError):
        raise HookConfigError(f"{where}.timeout_seconds must be a number") from None
    if timeout_seconds <= 0:
        raise HookConfigError(f"{where}.timeout_seconds must be positive")

    extra_keys = set(entry) - {"command", "on_failure", "timeout_seconds"}
    if extra_keys:
        raise HookConfigError(f"{where} has unknown key(s): {', '.join(sorted(extra_keys))}")

    return HookCommand(argv=tuple(argv), on_failure=on_failure, timeout_seconds=timeout_seconds)


def substitute_argv(argv: "tuple[str, ...]", context: "dict[str, str]") -> "list[str]":
    """Fill in ``%placeholder%`` tokens in each argument from ``context``.

    An unrecognized placeholder (typo, or a value this event doesn't
    provide) is left as literal text rather than raising, since a bare
    ``%`` is otherwise a legal character in a command-line argument.
    """
    return [_PLACEHOLDER_RE.sub(lambda m: context.get(m.group(1), m.group(0)), arg) for arg in argv]
