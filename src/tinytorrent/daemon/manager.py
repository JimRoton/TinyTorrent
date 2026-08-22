"""Ties together the scheduler, torrent sessions, and on-disk state.

This is the daemon's single source of truth. The IPC layer's command
handlers (add, purge, list, set priority, promote) are thin wrappers
around a ``DaemonManager`` instance.
"""

from __future__ import annotations

import logging
from pathlib import Path

from tinytorrent.common.ids import generate_torrent_id
from tinytorrent.common.priority import Priority
from tinytorrent.daemon import magnet as magnet_module
from tinytorrent.daemon import state_store
from tinytorrent.daemon import torrent_info as ti
from tinytorrent.daemon.magnet import MagnetParseError
from tinytorrent.daemon.state_store import TorrentRecord
from tinytorrent.daemon.scheduler import Scheduler
from tinytorrent.daemon.torrent_session import TorrentSession

logger = logging.getLogger(__name__)


class DaemonManager:
    def __init__(
        self,
        download_dir: Path,
        state_file: Path,
        our_peer_id: bytes,
        *,
        max_active: int = 4,
    ) -> None:
        self.download_dir = Path(download_dir)
        self.state_file = Path(state_file)
        self.our_peer_id = our_peer_id
        self.scheduler = Scheduler(max_active=max_active)
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
        return TorrentSession(
            record.torrent_id,
            magnet,
            self.download_dir,
            self.our_peer_id,
            priority=record.priority,
            info=info,
        )

    # -- commands, mirroring the CLI 1:1 -----------------------------------

    async def add_torrent(self, magnet_uri: str, *, priority: Priority = Priority.NORMAL) -> str:
        magnet = magnet_module.parse(magnet_uri)  # raises MagnetParseError on bad input
        existing_ids = {s.torrent_id for s in self.scheduler.list_sessions()}
        torrent_id = generate_torrent_id(existing_ids)
        session = TorrentSession(
            torrent_id, magnet, self.download_dir, self.our_peer_id, priority=priority
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
        self.save_state()

    async def set_priority(self, torrent_id: str, priority: Priority) -> None:
        await self.scheduler.set_priority(torrent_id, priority)
        self.save_state()

    async def promote(self, torrent_id: str) -> None:
        # Active/queued state is transient (re-derived by the scheduler on
        # every load), so promotion doesn't need a state save.
        await self.scheduler.promote(torrent_id)

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
                )
            )
        state_store.save(self.state_file, records)
