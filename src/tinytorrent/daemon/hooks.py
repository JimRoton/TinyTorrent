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

``HookRunner.run_for_test`` is the other entry point (used by
``tinytorrent test``): unlike ``fire``, it runs synchronously (the caller
awaits it) and unconditionally runs every configured command for an
event -- ignoring ``on_failure`` rather than stopping early -- so a user
debugging their hook config can see every command's result in one go.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from tinytorrent.common.hooks import HookCommand, HookEvent, substitute_argv
from tinytorrent.daemon.torrent_session import TorrentSession

logger = logging.getLogger(__name__)

_OUTPUT_CAPTURE_LIMIT = 4000  # cap captured stdout/stderr, both for logging and for `test`


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

    def commands_for(self, event: HookEvent) -> "tuple[HookCommand, ...]":
        return self._hooks.get(event, ())

    def fire(self, event: HookEvent, context: "dict[str, str]") -> None:
        """Schedule this event's configured commands to run; do not wait."""
        commands = self._hooks.get(event)
        if not commands:
            return
        task = asyncio.ensure_future(self._run_event(event, commands, context))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

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
                    "would_run_in_production": would_run,
                }
            )
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
