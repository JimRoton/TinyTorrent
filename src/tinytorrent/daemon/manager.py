"""Ties together the scheduler, torrent sessions, and on-disk state.

This is the daemon's single source of truth. The IPC layer's command
handlers (add, purge, list, set priority, promote) are thin wrappers
around a ``DaemonManager`` instance.
"""

from __future__ import annotations

import logging
from pathlib import Path

from tinytorrent.common.hooks import DAEMON_EVENTS, HookCommand, HookEvent
from tinytorrent.common.ids import generate_torrent_id
from tinytorrent.common.priority import Priority
from tinytorrent.daemon import magnet as magnet_module
from tinytorrent.daemon import state_store
from tinytorrent.daemon import torrent_info as ti
from tinytorrent.daemon.hooks import HookRunner, build_context, build_daemon_context
from tinytorrent.daemon.magnet import MagnetParseError
from tinytorrent.daemon.state_store import TorrentRecord
from tinytorrent.daemon.scheduler import Scheduler, SchedulerError
from tinytorrent.daemon.torrent_session import TorrentSession, TorrentStatus

logger = logging.getLogger(__name__)


class DaemonManager:
    def __init__(
        self,
        download_dir: Path,
        state_file: Path,
        our_peer_id: bytes,
        *,
        max_active: int = 4,
        hooks: "dict[HookEvent, tuple[HookCommand, ...]] | None" = None,
        socket_path: "Path | None" = None,
    ) -> None:
        self.download_dir = Path(download_dir)
        self.state_file = Path(state_file)
        self.our_peer_id = our_peer_id
        # Carried only so daemon-wide hooks can report it via
        # %socket_path%; the manager itself never touches the socket.
        self.socket_path = Path(socket_path) if socket_path is not None else None
        self.scheduler = Scheduler(max_active=max_active)
        self.hook_runner = HookRunner(hooks)
        # torrent_id -> the exact magnet URI the user supplied. Kept
        # separately rather than reconstructed from MagnetLink, since
        # re-serializing a magnet URI from its parsed parts isn't
        # guaranteed to byte-for-byte match what was originally given
        # (percent-encoding choices, param order, extra ignored params).
        self._magnet_uris: dict[str, str] = {}

    async def load_from_disk(self) -> None:
        """Reload persisted torrents at startup (e.g. after a systemd restart)."""
        try:
            records = state_store.load(self.state_file)
        except state_store.StateStoreError:
            logger.exception(
                "could not load state file %s; starting with no torrents", self.state_file
            )
            return

        for record in records:
            try:
                session = self._session_from_record(record)
            except (MagnetParseError, ti.TorrentInfoError) as exc:
                logger.error("skipping unloadable torrent %s: %s", record.torrent_id, exc)
                continue
            self._magnet_uris[record.torrent_id] = record.magnet_uri
            await self.scheduler.add(session)

    def _session_from_record(self, record: TorrentRecord) -> TorrentSession:
        magnet = magnet_module.parse(record.magnet_uri)
        info = ti.parse_info_dict(record.info_dict) if record.info_dict is not None else None
        session = TorrentSession(
            record.torrent_id,
            magnet,
            self.download_dir,
            self.our_peer_id,
            priority=record.priority,
            info=info,
            on_event=self._on_torrent_event,
        )
        if record.paused:
            # A pause is a persisted user choice (see state_store.py) --
            # restore it before the scheduler ever sees this session, so
            # it's never briefly eligible for an active slot on startup.
            session.status = TorrentStatus.PAUSED
        return session

    def _on_torrent_event(self, event: HookEvent, session: TorrentSession) -> None:
        self.hook_runner.fire(event, build_context(session))

    def daemon_context(self, **extra: str) -> "dict[str, str]":
        """Substitution context for the daemon-wide (torrent-less) events."""
        return build_daemon_context(
            download_dir=self.download_dir,
            state_file=self.state_file,
            socket_path=self.socket_path,
            max_active=self.scheduler.max_active,
            torrent_count=len(self.scheduler.list_sessions()),
            **extra,
        )

    def fire_daemon_started(self) -> None:
        """Fire ``daemon_started``, and start any repeating hooks it configures.

        Called once the IPC socket is listening, so a hook is free to
        shell out to the ``tinytorrent`` CLI.
        """
        self.hook_runner.fire(HookEvent.DAEMON_STARTED, self.daemon_context())

    # -- commands, mirroring the CLI 1:1 -----------------------------------

    async def add_torrent(self, magnet_uri: str, *, priority: Priority = Priority.NORMAL) -> str:
        magnet = magnet_module.parse(magnet_uri)  # raises MagnetParseError on bad input
        existing_ids = {s.torrent_id for s in self.scheduler.list_sessions()}
        torrent_id = generate_torrent_id(existing_ids)
        session = TorrentSession(
            torrent_id,
            magnet,
            self.download_dir,
            self.our_peer_id,
            priority=priority,
            on_event=self._on_torrent_event,
        )
        self._magnet_uris[torrent_id] = magnet_uri
        await self.scheduler.add(session)
        self.save_state()
        return torrent_id

    async def purge(self, torrent_id: str, *, with_data: bool = False) -> None:
        session = self.scheduler.get_session(torrent_id)  # raises SchedulerError if unknown
        await self.scheduler.remove(torrent_id)
        self._magnet_uris.pop(torrent_id, None)
        if with_data and session.storage is not None:
            session.storage.delete_all_files()
        self.hook_runner.fire(
            HookEvent.TORRENT_PURGED,
            build_context(session, deleted_data="true" if with_data else "false"),
        )
        self.save_state()

    async def purge_errored(self, *, with_data: bool = False) -> "list[str]":
        """Purge every torrent currently in the error state.

        Errored torrents are the ones that accumulate: they hold a slot
        against nothing and have to be cleared one id at a time. The ids
        are collected up front rather than iterated live, since each
        purge mutates the scheduler's torrent list.

        Each removal goes through ``purge()``, so ``torrent_purged``
        fires per torrent exactly as it would for a single purge.
        Returns the ids removed, in list order.
        """
        errored = [
            session.torrent_id
            for session in self.scheduler.list_sessions()
            if session.status == TorrentStatus.ERROR
        ]
        for torrent_id in errored:
            await self.purge(torrent_id, with_data=with_data)
        return errored

    async def test_hook(
        self,
        event: HookEvent,
        *,
        torrent_id: "str | None" = None,
        name: "str | None" = None,
        deleted_data: bool = False,
    ) -> "dict":
        """Run every command configured for ``event`` and report on each --
        for ``tinytorrent test``. Does not perform the real action
        associated with ``event`` (e.g. testing ``torrent_purged`` does
        not actually purge anything, and testing ``daemon_stopping``
        does not stop the daemon); it only substitutes real data into
        the configured commands and runs them, so a user can check their
        hook config works.

        For the daemon-wide events no torrent is involved, so
        ``torrent_id``/``name`` are ignored and reported back as None.
        A repeating command is run once, not on its schedule.
        """
        if event in DAEMON_EVENTS:
            # No torrent to resolve or report -- these events describe
            # the daemon itself.
            torrent_id, torrent_name = None, None
            context = self.daemon_context()
        else:
            session = self._resolve_torrent(torrent_id, name)
            torrent_id, torrent_name = session.torrent_id, session.display_name
            context = build_context(session, deleted_data="true" if deleted_data else "false")

        commands = self.hook_runner.commands_for(event)
        results = await self.hook_runner.run_for_test(event, context)
        return {
            "torrent_id": torrent_id,
            "torrent_name": torrent_name,
            "configured": bool(commands),
            "results": results,
        }

    def _resolve_torrent(self, torrent_id: "str | None", name: "str | None") -> TorrentSession:
        if torrent_id:
            try:
                return self.scheduler.get_session(torrent_id)
            except SchedulerError:
                if not name:
                    raise ValueError(f"no torrent with id {torrent_id!r}") from None
        elif not name:
            raise ValueError("must provide --id or --name")

        matches = [s for s in self.scheduler.list_sessions() if s.display_name.lower() == name.lower()]
        if not matches:
            raise ValueError(f"no torrent with name {name!r}")
        if len(matches) > 1:
            ids = ", ".join(sorted(m.torrent_id for m in matches))
            raise ValueError(f"{len(matches)} torrents match name {name!r} ({ids}); use --id instead")
        return matches[0]

    async def set_priority(self, torrent_id: str, priority: Priority) -> None:
        await self.scheduler.set_priority(torrent_id, priority)
        self.save_state()

    async def promote(self, torrent_id: str) -> None:
        # Active/queued state is transient (re-derived by the scheduler on
        # every load), so promotion doesn't need a state save.
        await self.scheduler.promote(torrent_id)

    async def pause(self, torrent_id: str) -> None:
        await self.scheduler.pause(torrent_id)  # raises SchedulerError if unknown
        self.save_state()  # paused is persisted -- see state_store.py

    async def resume(self, torrent_id: str) -> None:
        await self.scheduler.resume(torrent_id)  # raises SchedulerError if unknown
        self.save_state()

    def list_torrents(self) -> list[TorrentSession]:
        return self.scheduler.list_sessions()

    def get_torrent(self, torrent_id: str) -> TorrentSession:
        return self.scheduler.get_session(torrent_id)

    async def shutdown(self) -> None:
        # Stop the repeating daemon_started hooks first, so none of them
        # fires a fresh run while the daemon is on its way down.
        await self.hook_runner.cancel_repeating()
        # daemon_stopping is awaited rather than fired: the process is
        # about to exit, and a stop hook nobody waits for is useless.
        # It runs before the torrents are stopped, so %torrent_count%
        # still describes the daemon as it was.
        await self.hook_runner.run_now(HookEvent.DAEMON_STOPPING, self.daemon_context())
        await self.scheduler.shutdown()
        # Capture any metadata fetched since the last save, so a restart
        # doesn't need to re-fetch it from peers.
        self.save_state()

    # -- persistence --------------------------------------------------------

    def save_state(self) -> None:
        records = []
        for session in self.scheduler.list_sessions():
            magnet_uri = self._magnet_uris.get(session.torrent_id)
            if magnet_uri is None:
                logger.warning("torrent %s has no known magnet URI; skipping in state save", session.torrent_id)
                continue
            info_dict = ti.to_info_dict(session.info) if session.info is not None else None
            records.append(
                TorrentRecord(
                    torrent_id=session.torrent_id,
                    magnet_uri=magnet_uri,
                    priority=session.priority,
                    info_dict=info_dict,
                    paused=session.status == TorrentStatus.PAUSED,
                )
            )
        state_store.save(self.state_file, records)
