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
# Floor on ``interval_seconds`` -- a typo'd fraction of a second would
# otherwise spin the daemon running a subprocess in a tight loop.
MIN_INTERVAL_SECONDS = 1.0
_VALID_ON_FAILURE = {"ignore", "abort_remaining"}
_PLACEHOLDER_RE = re.compile(r"%([A-Za-z0-9_]+)%")


class HookEvent(str, Enum):
    """Lifecycle points a user can attach commands to.

    Most are torrent lifecycle points and carry a torrent's details in
    their substitution context. ``daemon_started`` and
    ``daemon_stopping`` are daemon-wide and have no torrent -- see
    ``DAEMON_EVENTS``.
    """

    METADATA_FETCHED = "metadata_fetched"
    DOWNLOAD_STARTED = "download_started"
    DOWNLOAD_COMPLETED = "download_completed"
    DOWNLOAD_ERROR = "download_error"
    TORRENT_PURGED = "torrent_purged"
    DAEMON_STARTED = "daemon_started"
    DAEMON_STOPPING = "daemon_stopping"


# Events that fire for the daemon as a whole rather than for one torrent.
# They get a different substitution context, and `tinytorrent test` does
# not need (or accept) a torrent to test them against.
DAEMON_EVENTS = frozenset({HookEvent.DAEMON_STARTED, HookEvent.DAEMON_STOPPING})

# Events whose commands may carry ``interval_seconds`` and repeat.
# Repeating only has an unambiguous meaning for an event that fires
# exactly once at a known moment: repeating a per-torrent event would
# raise questions this design doesn't want to answer (repeat per
# torrent? for how long? after it's purged?).
INTERVAL_EVENTS = frozenset({HookEvent.DAEMON_STARTED})


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
    # Set only on ``daemon_started`` commands. When present, this command
    # is detached from the event's ordered one-shot chain and instead
    # runs on its own repeating schedule -- immediately, then again
    # ``interval_seconds`` after each run *finishes* (so runs never
    # overlap and a slow command backs off rather than piling up).
    interval_seconds: "float | None" = None

    @property
    def repeats(self) -> bool:
        return self.interval_seconds is not None


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

    ``daemon_started`` commands may additionally carry
    ``interval_seconds``, which detaches them from that ordered chain and
    reruns them on a repeating schedule::

        {
          "daemon_started": [
            {"command": ["/usr/local/bin/health-check.sh"], "interval_seconds": 60}
          ]
        }
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

    interval_seconds = _parse_interval(where, event_name, entry)

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

    extra_keys = set(entry) - {"command", "on_failure", "timeout_seconds", "interval_seconds"}
    if extra_keys:
        raise HookConfigError(f"{where} has unknown key(s): {', '.join(sorted(extra_keys))}")

    return HookCommand(
        argv=tuple(argv),
        on_failure=on_failure,
        timeout_seconds=timeout_seconds,
        interval_seconds=interval_seconds,
    )


def _parse_interval(where: str, event_name: str, entry: dict) -> "float | None":
    if "interval_seconds" not in entry:
        return None

    if HookEvent(event_name) not in INTERVAL_EVENTS:
        allowed = ", ".join(sorted(e.value for e in INTERVAL_EVENTS))
        raise HookConfigError(
            f"{where}.interval_seconds is only supported on: {allowed} "
            f"(not on {event_name!r})"
        )

    try:
        interval_seconds = float(entry["interval_seconds"])
    except (TypeError, ValueError):
        raise HookConfigError(f"{where}.interval_seconds must be a number") from None
    if interval_seconds < MIN_INTERVAL_SECONDS:
        raise HookConfigError(
            f"{where}.interval_seconds must be at least {MIN_INTERVAL_SECONDS}"
        )
    return interval_seconds


def substitute_argv(argv: "tuple[str, ...]", context: "dict[str, str]") -> "list[str]":
    """Fill in ``%placeholder%`` tokens in each argument from ``context``.

    An unrecognized placeholder (typo, or a value this event doesn't
    provide) is left as literal text rather than raising, since a bare
    ``%`` is otherwise a legal character in a command-line argument.
    """
    return [_PLACEHOLDER_RE.sub(lambda m: context.get(m.group(1), m.group(0)), arg) for arg in argv]
