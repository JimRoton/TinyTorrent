"""Ties together the scheduler, torrent sessions, and on-disk state.

This is the daemon's single source of truth. The IPC layer's command
handlers (add, purge, list, set priority, promote) are thin wrappers
around a ``DaemonManager`` instance.
"""

from __future__ import annotations

import logging
from pathlib import Path

from tinytorrent.common.hooks import HookCommand, HookEvent
from tinytorrent.common.ids import generate_torrent_id
from tinytorrent.common.priority import Priority
from tinytorrent.daemon import magnet as magnet_module
from tinytorrent.daemon import state_store
from tinytorrent.daemon import torrent_info as ti
from tinytorrent.daemon.hooks import HookRunner, build_context
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
    ) -> None:
        self.download_dir = Path(download_dir)
        self.state_file = Path(state_file)
        self.our_peer_id = our_peer_id
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

    async def test_hook(
        self,
        event: HookEvent,
        *,
        torrent_id: "str | None" = None,
        name: "str | None" = None,
        deleted_data: bool = False,
    ) -> "dict":
        """Run every command configured for ``event`` against a real torrent
        and report on each -- for ``tinytorrent test``. Does not perform
        the real action associated with ``event`` (e.g. testing
        ``torrent_purged`` does not actually purge anything); it only
        substitutes that torrent's real data into the configured commands
        and runs them, so a user can check their hook config works.
        """
        session = self._resolve_torrent(torrent_id, name)
        context = build_context(session, deleted_data="true" if deleted_data else "false")
        commands = self.hook_runner.commands_for(event)
        results = await self.hook_runner.run_for_test(event, context)
        return {
            "torrent_id": session.torrent_id,
            "torrent_name": session.display_name,
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
