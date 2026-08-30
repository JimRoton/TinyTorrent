"""Orchestrates downloading a single torrent.

Ties together tracker announces, peer connections, metadata exchange, and
the piece manager into one coroutine (``TorrentSession.download()``) that
the scheduler runs as an asyncio task for an active torrent and cancels
for a queued/preempted one. Cancellation only discards in-flight
(unwritten) block data — any piece already verified and written to disk
stays there, so resuming later continues from the same point.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from tinytorrent.common.hooks import HookEvent
from tinytorrent.common.priority import Priority
from tinytorrent.daemon import torrent_info as ti
from tinytorrent.daemon.magnet import MagnetLink
from tinytorrent.daemon.metadata_exchange import MetadataExchangeError, fetch_metadata_from_peer
from tinytorrent.daemon.peer_wire.connection import PeerConnection, PeerConnectionError
from tinytorrent.daemon.peer_wire.messages import (
    Bitfield,
    Choke,
    Have,
    Interested,
    PeerProtocolError,
    Piece,
    Request,
    Unchoke,
)
from tinytorrent.daemon.piece_manager import PieceAssembler, PieceManagerError, PieceStorage, iter_blocks
from tinytorrent.daemon.tracker import AnnounceResult, announce as tracker_announce

logger = logging.getLogger(__name__)

# How many peers we're willing to keep active at once, and how deep a
# request pipeline we run per peer, scaled by priority. There's no
# configured global bandwidth cap (out of scope for v1), so priority is
# expressed as a larger share of concurrent connections/requests rather
# than a numeric throughput split.
PRIORITY_MAX_PEERS = {Priority.HIGH: 30, Priority.NORMAL: 15, Priority.LOW: 5}
PRIORITY_PIPELINE_DEPTH = {Priority.HIGH: 8, Priority.NORMAL: 5, Priority.LOW: 2}
PEER_CANDIDATE_MULTIPLIER = 2  # try more candidates than max_peers, since many will fail

PEER_CONNECT_TIMEOUT = 10.0
PEER_IDLE_TIMEOUT = 120.0  # a peer that sends nothing for this long is dropped
METADATA_FETCH_CONCURRENCY = 8
MAX_METADATA_ANNOUNCE_ATTEMPTS = 5
METADATA_RETRY_DELAY = 5.0
TRACKER_RETRY_DELAY = 15.0
RATE_WINDOW_SECONDS = 20.0


class TorrentStatus(str, Enum):
    FETCHING_METADATA = "fetching_metadata"
    VERIFYING = "verifying"
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    COMPLETE = "complete"
    ERROR = "error"
    PAUSED = "paused"  # set only by Scheduler.pause() -- never by TorrentSession itself


@dataclass(frozen=True)
class ProgressSnapshot:
    status: TorrentStatus
    bytes_downloaded: int
    total_length: int
    download_rate_bps: float
    eta_seconds: float | None
    num_pieces: int
    completed_pieces: int
    error_message: str | None = None


class TorrentSession:
    """Owns the download state and logic for a single torrent."""

    def __init__(
        self,
        torrent_id: str,
        magnet: MagnetLink,
        download_dir: Path,
        our_peer_id: bytes,
        *,
        priority: Priority = Priority.NORMAL,
        info: ti.TorrentInfo | None = None,
        completed_pieces: set[int] | None = None,
        on_event: "Callable[[HookEvent, TorrentSession], None] | None" = None,
    ) -> None:
        self.torrent_id = torrent_id
        self.magnet = magnet
        self.download_dir = Path(download_dir)
        self.our_peer_id = our_peer_id
        self.priority = priority
        self._on_event = on_event

        self.status = TorrentStatus.QUEUED
        self.info = info
        self.storage: PieceStorage | None = None
        self.completed_pieces: set[int] = set(completed_pieces or ())
        self.error_message: str | None = None

        self._all_piece_indices: frozenset[int] = frozenset()
        self._completed_bytes = 0
        self._raw_bytes_received = 0
        self._rate_samples: list[tuple[float, int]] = []
        self._piece_owner: dict[int, str] = {}

        if info is not None:
            self._all_piece_indices = frozenset(range(info.num_pieces))
            self._completed_bytes = sum(ti.piece_length_for(info, i) for i in self.completed_pieces)

    @property
    def display_name(self) -> str:
        if self.info is not None:
            return self.info.name
        return self.magnet.display_name or self.magnet.info_hash_hex

    def progress(self) -> ProgressSnapshot:
        total = self.info.total_length if self.info else 0
        num_pieces = self.info.num_pieces if self.info else 0
        rate = self._current_rate()
        eta = None
        remaining = total - self._completed_bytes
        if rate > 0 and remaining > 0:
            eta = remaining / rate
        return ProgressSnapshot(
            status=self.status,
            bytes_downloaded=self._completed_bytes,
            total_length=total,
            download_rate_bps=rate,
            eta_seconds=eta,
            num_pieces=num_pieces,
            completed_pieces=len(self.completed_pieces),
            error_message=self.error_message,
        )

    def _needed_pieces(self) -> frozenset[int]:
        return self._all_piece_indices - self.completed_pieces

    def _current_rate(self) -> float:
        if len(self._rate_samples) < 2:
            return 0.0
        t0, b0 = self._rate_samples[0]
        t1, b1 = self._rate_samples[-1]
        dt = t1 - t0
        if dt <= 0:
            return 0.0
        return (b1 - b0) / dt

    def _record_rate_sample(self) -> None:
        now = time.monotonic()
        self._rate_samples.append((now, self._raw_bytes_received))
        cutoff = now - RATE_WINDOW_SECONDS
        while len(self._rate_samples) > 1 and self._rate_samples[0][0] < cutoff:
            self._rate_samples.pop(0)

    def _fire(self, event: HookEvent) -> None:
        if self._on_event is not None:
            self._on_event(event, self)

    # -- lifecycle -----------------------------------------------------

    async def download(self) -> None:
        """Run this torrent to completion (or until cancelled/erroring).

        Cancellation (``asyncio.CancelledError``) is allowed to propagate
        after any in-progress network operations are cleaned up — the
        scheduler cancels this task to preempt/queue the torrent, and
        relies on ``CancelledError`` propagating so it knows the task is
        actually done.
        """
        try:
            if self.info is None:
                self.status = TorrentStatus.FETCHING_METADATA
                await self._acquire_metadata()
                self._all_piece_indices = frozenset(range(self.info.num_pieces))

            if self.storage is None:
                self.status = TorrentStatus.VERIFYING
                self.storage = PieceStorage(self.info, self.download_dir)
                self.completed_pieces = self.storage.scan_completed_pieces()
                self._completed_bytes = sum(
                    ti.piece_length_for(self.info, i) for i in self.completed_pieces
                )

            if not self._needed_pieces():
                # Already fully present on disk -- e.g. resumed after a
                # daemon restart and re-verified. Deliberately does NOT
                # fire download_completed: that event means "just finished
                # downloading", not "confirmed complete (again)", so a
                # restart doesn't repeatedly re-trigger a user's hook
                # commands for torrents that finished long ago.
                self.status = TorrentStatus.COMPLETE
                return

            self.status = TorrentStatus.DOWNLOADING
            self._fire(HookEvent.DOWNLOAD_STARTED)
            await self._download_loop()

            if not self._needed_pieces():
                self.status = TorrentStatus.COMPLETE
                self._fire(HookEvent.DOWNLOAD_COMPLETED)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - any failure becomes torrent error state
            self.status = TorrentStatus.ERROR
            self.error_message = str(exc)
            logger.exception("torrent %s failed", self.torrent_id)
            self._fire(HookEvent.DOWNLOAD_ERROR)

    # -- metadata acquisition ------------------------------------------

    async def _acquire_metadata(self) -> None:
        if not self.magnet.trackers:
            raise MetadataExchangeError("magnet link has no trackers to discover peers from")

        for attempt in range(1, MAX_METADATA_ANNOUNCE_ATTEMPTS + 1):
            peers = await self._announce_to_trackers()
            if peers:
                info_dict = await self._try_peers_for_metadata(peers)
                if info_dict is not None:
                    self.info = ti.parse_info_dict(info_dict)
                    self._fire(HookEvent.METADATA_FETCHED)
                    return
            if attempt < MAX_METADATA_ANNOUNCE_ATTEMPTS:
                await asyncio.sleep(METADATA_RETRY_DELAY)

        raise MetadataExchangeError(
            f"could not fetch metadata for {self.magnet.info_hash_hex} after "
            f"{MAX_METADATA_ANNOUNCE_ATTEMPTS} attempts"
        )

    async def _try_peers_for_metadata(self, peers: list[tuple[str, int]]) -> dict | None:
        semaphore = asyncio.Semaphore(METADATA_FETCH_CONCURRENCY)

        async def attempt(address: tuple[str, int]) -> dict | None:
            async with semaphore:
                conn = None
                try:
                    conn = await PeerConnection.connect(
                        address[0],
                        address[1],
                        info_hash=self.magnet.info_hash,
                        our_peer_id=self.our_peer_id,
                        connect_timeout=PEER_CONNECT_TIMEOUT,
                    )
                    return await fetch_metadata_from_peer(conn, self.magnet.info_hash)
                except (
                    PeerConnectionError,
                    PeerProtocolError,
                    MetadataExchangeError,
                    OSError,
                    asyncio.TimeoutError,
                ):
                    return None
                finally:
                    if conn is not None:
                        conn.close()

        tasks = [asyncio.create_task(attempt(addr)) for addr in peers[:METADATA_FETCH_CONCURRENCY * 4]]
        try:
            for coro in asyncio.as_completed(tasks):
                result = await coro
                if result is not None:
                    return result
            return None
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    # -- tracker announces -----------------------------------------------

    async def _announce_to_trackers(self) -> list[tuple[str, int]]:
        results = await asyncio.gather(
            *(self._announce_one(url) for url in self.magnet.trackers),
            return_exceptions=True,
        )
        peers: set[tuple[str, int]] = set()
        for result in results:
            if isinstance(result, AnnounceResult):
                peers.update(result.peers)
        return list(peers)

    async def _announce_one(self, tracker_url: str) -> AnnounceResult:
        left = self.info.total_length - self._completed_bytes if self.info else 1
        return await tracker_announce(
            tracker_url,
            info_hash=self.magnet.info_hash,
            peer_id=self.our_peer_id,
            port=0,  # TinyTorrent never accepts incoming connections — download only
            uploaded=0,
            downloaded=self._completed_bytes,
            left=max(left, 0),
            event="started",
        )

    # -- main download loop ----------------------------------------------

    async def _download_loop(self) -> None:
        max_peers = PRIORITY_MAX_PEERS[self.priority]

        while self._needed_pieces():
            peers = await self._announce_to_trackers()
            if not peers:
                await asyncio.sleep(TRACKER_RETRY_DELAY)
                continue

            semaphore = asyncio.Semaphore(max_peers)
            candidates = peers[: max_peers * PEER_CANDIDATE_MULTIPLIER]
            tasks = [asyncio.create_task(self._run_peer(addr, semaphore)) for addr in candidates]
            try:
                # return_exceptions=True is a safety net against a bug in
                # one peer worker taking down the whole torrent; genuine
                # cancellation (the scheduler preempting this torrent)
                # still propagates as CancelledError regardless.
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for result in results:
                    if isinstance(result, Exception):
                        logger.warning("torrent %s: peer worker failed unexpectedly: %r", self.torrent_id, result)
            except asyncio.CancelledError:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise

            if self._needed_pieces():
                await asyncio.sleep(TRACKER_RETRY_DELAY)

    async def _run_peer(self, address: tuple[str, int], semaphore: asyncio.Semaphore) -> None:
        async with semaphore:
            conn = None
            try:
                conn = await PeerConnection.connect(
                    address[0],
                    address[1],
                    info_hash=self.magnet.info_hash,
                    our_peer_id=self.our_peer_id,
                    connect_timeout=PEER_CONNECT_TIMEOUT,
                )
                await self._download_from_peer(conn)
            except (PeerConnectionError, PeerProtocolError, PieceManagerError, OSError, asyncio.TimeoutError):
                return
            finally:
                if conn is not None:
                    conn.close()

    async def _download_from_peer(self, conn: PeerConnection) -> None:
        pipeline_depth = PRIORITY_PIPELINE_DEPTH[self.priority]
        peer_has: set[int] = set()
        peer_choking = True
        am_interested = False

        current_piece: int | None = None
        assembler: PieceAssembler | None = None
        pending_blocks: list[tuple[int, int]] = []
        outstanding: list[tuple[int, int]] = []

        def release_current_piece() -> None:
            nonlocal current_piece, assembler, pending_blocks, outstanding
            if current_piece is not None:
                self._piece_owner.pop(current_piece, None)
            current_piece, assembler, pending_blocks, outstanding = None, None, [], []

        try:
            while self._needed_pieces():
                message = await asyncio.wait_for(conn.receive(), timeout=PEER_IDLE_TIMEOUT)

                if isinstance(message, Bitfield):
                    peer_has |= _bitfield_to_piece_set(message.bitfield, self.info.num_pieces)
                elif isinstance(message, Have):
                    if 0 <= message.piece_index < self.info.num_pieces:
                        peer_has.add(message.piece_index)
                elif isinstance(message, Choke):
                    peer_choking = True
                elif isinstance(message, Unchoke):
                    peer_choking = False
                elif isinstance(message, Piece):
                    if current_piece is not None and message.index == current_piece:
                        key = (message.begin, len(message.block))
                        if key in outstanding:
                            outstanding.remove(key)
                        try:
                            assembler.add_block(message.begin, message.block)
                        except PieceManagerError:
                            continue
                        self._raw_bytes_received += len(message.block)
                        self._record_rate_sample()
                        if assembler.is_complete:
                            data = assembler.assemble()
                            if self.storage.verify_piece(current_piece, data):
                                self.storage.write_piece(current_piece, data)
                                self.completed_pieces.add(current_piece)
                                self._completed_bytes += ti.piece_length_for(self.info, current_piece)
                            else:
                                logger.warning(
                                    "torrent %s: piece %s failed verification from %s",
                                    self.torrent_id,
                                    current_piece,
                                    conn.address,
                                )
                            release_current_piece()
                # Extended/Port/etc. messages are not relevant once we're
                # past metadata acquisition.

                if not am_interested and (peer_has & self._needed_pieces()):
                    await conn.send(Interested())
                    am_interested = True

                if peer_choking:
                    continue

                if current_piece is None:
                    next_piece = self._pick_piece(peer_has)
                    if next_piece is None:
                        if not (peer_has & self._needed_pieces()):
                            return  # this peer has nothing we still need
                        continue  # peer's useful pieces are all owned by other peers right now
                    current_piece = next_piece
                    self._piece_owner[current_piece] = f"{conn.address[0]}:{conn.address[1]}"
                    pending_blocks = list(iter_blocks(self.info, current_piece))
                    assembler = PieceAssembler(current_piece, pending_blocks)
                    outstanding = []

                while pending_blocks and len(outstanding) < pipeline_depth:
                    begin, length = pending_blocks.pop(0)
                    await conn.send(Request(index=current_piece, begin=begin, length=length))
                    outstanding.append((begin, length))
        finally:
            release_current_piece()

    def _pick_piece(self, peer_has: set[int]) -> int | None:
        available = (peer_has & self._needed_pieces()) - self._piece_owner.keys()
        if not available:
            return None
        return min(available)  # sequential order — simplest deterministic strategy for v1


def _bitfield_to_piece_set(bitfield: bytes, num_pieces: int) -> set[int]:
    pieces = set()
    for i in range(num_pieces):
        byte_index, bit_index = divmod(i, 8)
        if byte_index >= len(bitfield):
            break
        if bitfield[byte_index] & (0x80 >> bit_index):
            pieces.add(i)
    return pieces
