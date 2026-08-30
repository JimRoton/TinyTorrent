"""Runs user-configured commands in response to torrent lifecycle events.

Hook commands are executed as a plain argv list via
``asyncio.create_subprocess_exec`` -- never through a shell -- so that
values substituted from torrent metadata (which originates from the
network and is not trusted) can never be interpreted as shell syntax.

Firing an event (``HookRunner.fire``) is synchronous and non-blocking: it
schedules the configured commands as a background asyncio task and
returns immediately. The caller (a ``TorrentSession`` reaching a status
transition, or the manager handling a purge) never waits for hook
commands to finish -- a slow or hung command can't stall downloading or
scheduling. One consequence, by design: hook commands are not guaranteed
to finish before the daemon exits (e.g. a systemd restart).

``daemon_stopping`` is the one exception, run via ``run_now`` rather than
``fire``: the daemon awaits it during shutdown. A stop hook that isn't
waited for is useless, since the process exits out from under it. Each
command's ``timeout_seconds`` is what bounds how long shutdown can be
held up.

A ``daemon_started`` command carrying ``interval_seconds`` is detached
from that event's ordered chain into its own repeating task, which runs
the command immediately and then again after each run finishes. Those
tasks are the only hooks that outlive the event that started them, so
they are tracked separately and cancelled by ``cancel_repeating`` when
the daemon shuts down.

``HookRunner.run_for_test`` is the other entry point (used by
``tinytorrent test``): unlike ``fire``, it runs synchronously (the caller
awaits it) and unconditionally runs every configured command for an
event -- ignoring ``on_failure`` rather than stopping early -- so a user
debugging their hook config can see every command's result in one go.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

from tinytorrent.common.hooks import HookCommand, HookEvent, substitute_argv
from tinytorrent.daemon.torrent_session import TorrentSession

logger = logging.getLogger(__name__)

_OUTPUT_CAPTURE_LIMIT = 4000  # cap captured stdout/stderr, both for logging and for `test`
_KILL_REAP_TIMEOUT = 5.0  # how long to wait to collect a killed child's status


@dataclass(frozen=True)
class CommandResult:
    """The outcome of running one hook command, after placeholder substitution."""

    argv: "tuple[str, ...]"
    outcome: str  # "ok" | "failed" | "timeout" | "start_error"
    returncode: "int | None"
    output: str


class HookRunner:
    """Fires configured commands for torrent lifecycle events."""

    def __init__(self, hooks: "dict[HookEvent, tuple[HookCommand, ...]] | None" = None):
        self._hooks = hooks or {}
        self._tasks: "set[asyncio.Task]" = set()
        # Repeating tasks are kept apart from ``_tasks`` because they
        # never finish on their own -- ``wait_idle`` would hang on them.
        self._repeating: "set[asyncio.Task]" = set()

    def commands_for(self, event: HookEvent) -> "tuple[HookCommand, ...]":
        return self._hooks.get(event, ())

    def fire(self, event: HookEvent, context: "dict[str, str]") -> None:
        """Schedule this event's configured commands to run; do not wait.

        Commands carrying ``interval_seconds`` are split out into their
        own repeating tasks; the rest keep the existing ordered
        one-shot chain, including ``on_failure`` propagation between
        them.
        """
        commands = self._hooks.get(event)
        if not commands:
            return

        one_shot = tuple(c for c in commands if not c.repeats)
        repeating = tuple(c for c in commands if c.repeats)

        if one_shot:
            task = asyncio.ensure_future(self._run_event(event, one_shot, context))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        for command in repeating:
            task = asyncio.ensure_future(self._repeat(event, command, context))
            self._repeating.add(task)
            task.add_done_callback(self._repeating.discard)

    async def run_now(self, event: HookEvent, context: "dict[str, str]") -> None:
        """Run this event's commands and wait for them to finish.

        Used for ``daemon_stopping``, where fire-and-forget would mean
        the commands are killed by the process exiting.
        """
        commands = self._hooks.get(event)
        if not commands:
            return
        await self._run_event(event, commands, context)

    async def cancel_repeating(self) -> None:
        """Cancel every repeating (``interval_seconds``) hook task, and wait.

        Awaiting the cancelled tasks matters: a repeating command may be
        mid-subprocess, and this is what gives it the chance to kill and
        reap that child before the daemon's event loop goes away.
        """
        tasks = list(self._repeating)
        self._repeating.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _repeat(
        self, event: HookEvent, command: HookCommand, context: "dict[str, str]"
    ) -> None:
        """Run one command immediately, then again after each run finishes.

        The wait happens *after* a run completes rather than on a fixed
        wall-clock period, so two runs of the same command never overlap
        and a command slower than its interval backs off instead of
        piling up.
        """
        while True:
            argv = substitute_argv(command.argv, context)
            result = await self._run_one(event, argv, command.timeout_seconds)
            self._log_result(event, result)
            if result.outcome != "ok" and command.on_failure == "abort_remaining":
                logger.info(
                    "hook for %s: %r failed with on_failure=abort_remaining, not repeating it again",
                    event.value,
                    result.argv,
                )
                return
            await asyncio.sleep(command.interval_seconds)

    async def wait_idle(self) -> None:
        """Wait for all currently-scheduled hook runs to finish.

        Not used by the daemon itself (hooks are intentionally
        fire-and-forget) -- for tests that need to observe a hook's
        effect deterministically instead of sleeping.
        """
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def run_for_test(self, event: HookEvent, context: "dict[str, str]") -> "list[dict]":
        """Run every command configured for ``event`` and report on each.

        Used by ``tinytorrent test``. Unlike ``fire``, this always runs
        every command regardless of ``on_failure`` -- so a user testing
        their config sees whether *each* command works, not just the
        ones production would have reached. Each result also reports
        whether that command *would* have run in real (production)
        usage, given ``on_failure`` and the outcome of earlier commands.

        A repeating command is run exactly once here, however long its
        ``interval_seconds`` is; the configured interval is reported so
        the user can see the cadence without waiting for it.
        """
        results = []
        would_run = True
        for command in self._hooks.get(event, ()):
            argv = substitute_argv(command.argv, context)
            result = await self._run_one(event, argv, command.timeout_seconds)
            results.append(
                {
                    "argv": list(result.argv),
                    "outcome": result.outcome,
                    "returncode": result.returncode,
                    "output": result.output,
                    "on_failure": command.on_failure,
                    "interval_seconds": command.interval_seconds,
                    "would_run_in_production": would_run,
                }
            )
            # A repeating command runs on its own schedule, so it never
            # takes part in the one-shot chain's abort propagation --
            # neither being skipped by it nor causing it.
            if command.repeats:
                continue
            if would_run and result.outcome != "ok" and command.on_failure == "abort_remaining":
                would_run = False  # every command from here on would have been skipped
        return results

    async def _run_event(
        self,
        event: HookEvent,
        commands: "tuple[HookCommand, ...]",
        context: "dict[str, str]",
    ) -> None:
        for command in commands:
            argv = substitute_argv(command.argv, context)
            result = await self._run_one(event, argv, command.timeout_seconds)
            self._log_result(event, result)
            if result.outcome != "ok" and command.on_failure == "abort_remaining":
                logger.info(
                    "hook for %s: on_failure=abort_remaining, skipping remaining commands", event.value
                )
                return

    async def _run_one(self, event: HookEvent, argv: "list[str]", timeout_seconds: float) -> CommandResult:
        argv_t = tuple(argv)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as exc:
            return CommandResult(argv=argv_t, outcome="start_error", returncode=None, output=str(exc))

        try:
            output, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return CommandResult(argv=argv_t, outcome="timeout", returncode=None, output="")
        except asyncio.CancelledError:
            # Cancellation (a repeating hook stopped at shutdown) must not
            # leave the child running. Shield the reap so the same
            # cancellation doesn't interrupt the cleanup, and bound it so
            # shutdown can never block on a child that won't be reaped --
            # the kill signal is what matters, collecting the status is
            # only tidiness.
            proc.kill()
            try:
                await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=_KILL_REAP_TIMEOUT)
            except (asyncio.TimeoutError, asyncio.CancelledError, ProcessLookupError):
                pass
            raise

        text = output[-_OUTPUT_CAPTURE_LIMIT:].decode("utf-8", errors="replace").strip()
        outcome = "ok" if proc.returncode == 0 else "failed"
        return CommandResult(argv=argv_t, outcome=outcome, returncode=proc.returncode, output=text)

    @staticmethod
    def _log_result(event: HookEvent, result: CommandResult) -> None:
        if result.outcome == "ok":
            logger.info("hook for %s: %r succeeded", event.value, result.argv)
        elif result.outcome == "timeout":
            logger.warning("hook for %s: %r timed out", event.value, result.argv)
        elif result.outcome == "start_error":
            logger.warning("hook for %s: failed to start %r: %s", event.value, result.argv, result.output)
        else:
            logger.warning(
                "hook for %s: %r exited %s: %s", event.value, result.argv, result.returncode, result.output
            )


def build_daemon_context(
    *,
    download_dir,
    state_file,
    socket_path,
    max_active: int,
    torrent_count: int,
    **extra: str,
) -> "dict[str, str]":
    """Build the ``%placeholder%`` context for a daemon-wide event.

    ``daemon_started`` and ``daemon_stopping`` have no torrent, so none
    of the per-torrent placeholders exist for them. An unrecognized
    placeholder is left as literal text (see ``substitute_argv``), so a
    torrent hook copied onto a daemon event degrades to a literal
    ``%name%`` argument rather than failing.
    """
    context = {
        "download_dir": str(download_dir),
        "state_file": str(state_file),
        "socket_path": "" if socket_path is None else str(socket_path),
        "max_active": str(max_active),
        "torrent_count": str(torrent_count),
        "pid": str(os.getpid()),
    }
    context.update(extra)
    return context


def build_context(session: TorrentSession, **extra: str) -> "dict[str, str]":
    """Build the ``%placeholder%`` substitution context for one torrent."""
    context = {
        "id": session.torrent_id,
        "name": session.display_name,
        "status": session.status.value,
        "download_dir": str(session.download_dir),
        "total_bytes": str(session.info.total_length) if session.info is not None else "0",
        "priority": session.priority.value,
        "info_hash": session.magnet.info_hash_hex,
    }
    context.update(extra)
    return context
