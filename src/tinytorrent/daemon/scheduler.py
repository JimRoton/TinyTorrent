"""Scheduler: enforces the concurrency cap and priority-based preemption.

Reacts only to queue-changing events (add / priority change / a torrent
finishing / remove / promote) — there's no periodic polling. Preemption
never discards data: a torrent bumped back to QUEUED keeps whatever
pieces it already has (verified on disk by its TorrentSession) and the
TorrentSession object itself is retained with its in-memory state, so a
later promotion resumes rather than restarting from scratch.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from tinytorrent.common.priority import Priority, is_higher
from tinytorrent.daemon.torrent_session import TorrentSession, TorrentStatus

logger = logging.getLogger(__name__)

MAX_ACTIVE_TORRENTS = 4

_PRIORITY_RANK = {Priority.LOW: 0, Priority.NORMAL: 1, Priority.HIGH: 2}
_FINISHED_STATUSES = {TorrentStatus.COMPLETE, TorrentStatus.ERROR}
# Finished OR paused: neither is eligible to be picked up by rebalancing.
# Paused is kept separate from _FINISHED_STATUSES because it's not
# terminal -- resume() puts a torrent right back into contention.
_NOT_SCHEDULABLE_STATUSES = _FINISHED_STATUSES | {TorrentStatus.PAUSED}


class SchedulerError(Exception):
    pass


@dataclass
class _ManagedTorrent:
    session: TorrentSession
    task: "asyncio.Task | None" = None  # set only while actively running


class Scheduler:
    """Owns the set of torrents and decides which are active vs queued."""

    def __init__(self, max_active: int = MAX_ACTIVE_TORRENTS):
        self.max_active = max_active
        self._torrents: dict[str, _ManagedTorrent] = {}
        self._lock = asyncio.Lock()

    # -- public, queue-changing operations --------------------------------

    async def add(self, session: TorrentSession) -> None:
        async with self._lock:
            if session.torrent_id in self._torrents:
                raise SchedulerError(f"torrent {session.torrent_id} already registered")
            self._torrents[session.torrent_id] = _ManagedTorrent(session=session)
            await self._rebalance()

    async def remove(self, torrent_id: str) -> None:
        async with self._lock:
            managed = self._require(torrent_id)
            await self._stop(managed)
            del self._torrents[torrent_id]
            await self._rebalance()

    async def set_priority(self, torrent_id: str, priority: Priority) -> None:
        async with self._lock:
            managed = self._require(torrent_id)
            managed.session.priority = priority
            await self._rebalance()

    async def promote(self, torrent_id: str) -> None:
        """Force a queued torrent into an active slot now, regardless of priority."""
        async with self._lock:
            managed = self._require(torrent_id)
            if managed.task is not None:
                return  # already active
            if managed.session.status in _NOT_SCHEDULABLE_STATUSES:
                return  # nothing to promote (finished, or paused -- resume() it first)
            if len(self._active_managed()) >= self.max_active:
                victim = self._weakest_active(self._active_managed())
                if victim is not None:
                    await self._stop(victim)
            self._start(managed)

    async def pause(self, torrent_id: str) -> None:
        """Stop a torrent's download and hold it out of scheduling until resumed.

        Unlike preemption (a torrent bumped back to QUEUED by a
        higher-priority arrival), a paused torrent is never
        auto-restarted by rebalancing -- it stays out of contention for
        an active slot until resume() explicitly puts it back in.
        Whatever pieces it already wrote to disk are untouched, exactly
        like preemption.
        """
        async with self._lock:
            managed = self._require(torrent_id)
            if managed.session.status in _FINISHED_STATUSES:
                return  # nothing to pause
            if managed.session.status == TorrentStatus.PAUSED:
                return  # already paused
            await self._stop(managed)  # cancels if active; _stop() resets status to QUEUED
            managed.session.status = TorrentStatus.PAUSED
            await self._rebalance()  # pausing an active torrent frees its slot

    async def resume(self, torrent_id: str) -> None:
        """Make a paused torrent eligible for scheduling again.

        This doesn't force it active the way promote() does -- it just
        re-enters normal priority-based contention for a slot, exactly
        like a torrent that was just added.
        """
        async with self._lock:
            managed = self._require(torrent_id)
            if managed.session.status != TorrentStatus.PAUSED:
                return  # not paused, nothing to do
            managed.session.status = TorrentStatus.QUEUED
            await self._rebalance()

    async def shutdown(self) -> None:
        async with self._lock:
            for managed in list(self._torrents.values()):
                await self._stop(managed)

    def list_sessions(self) -> list[TorrentSession]:
        return [m.session for m in self._torrents.values()]

    def get_session(self, torrent_id: str) -> TorrentSession:
        return self._require(torrent_id).session

    def is_active(self, torrent_id: str) -> bool:
        return self._require(torrent_id).task is not None

    # -- internals ----------------------------------------------------------

    def _require(self, torrent_id: str) -> _ManagedTorrent:
        managed = self._torrents.get(torrent_id)
        if managed is None:
            raise SchedulerError(f"unknown torrent id {torrent_id!r}")
        return managed

    def _active_managed(self) -> list[_ManagedTorrent]:
        return [m for m in self._torrents.values() if m.task is not None]

    def _queued_managed(self) -> list[_ManagedTorrent]:
        return [
            m
            for m in self._torrents.values()
            if m.task is None and m.session.status not in _NOT_SCHEDULABLE_STATUSES
        ]

    async def _rebalance(self) -> None:
        # Fill any free active slots with the best queued candidates.
        while len(self._active_managed()) < self.max_active:
            candidate = self._best_queued_candidate()
            if candidate is None:
                break
            self._start(candidate)

        # At capacity: preempt only if a queued torrent strictly outranks
        # the weakest currently-active one.
        while True:
            active = self._active_managed()
            if len(active) < self.max_active:
                break
            candidate = self._best_queued_candidate()
            if candidate is None:
                break
            weakest = self._weakest_active(active)
            if weakest is None or not is_higher(candidate.session.priority, weakest.session.priority):
                break
            await self._stop(weakest)
            self._start(candidate)

    def _best_queued_candidate(self) -> "_ManagedTorrent | None":
        queued = self._queued_managed()
        if not queued:
            return None
        # max() returns the first element on ties, and _queued_managed()
        # preserves insertion order, so equal-priority torrents resolve to
        # "earliest added is preferred" — matching the design's same-tier
        # first-in-stays-active rule.
        return max(queued, key=lambda m: _PRIORITY_RANK[m.session.priority])

    def _weakest_active(self, active: list[_ManagedTorrent]) -> "_ManagedTorrent | None":
        if not active:
            return None
        min_rank = min(_PRIORITY_RANK[m.session.priority] for m in active)
        lowest_tier = [m for m in active if _PRIORITY_RANK[m.session.priority] == min_rank]
        # Tie-break within the lowest tier: least progress is evicted first.
        return min(lowest_tier, key=lambda m: m.session.progress().bytes_downloaded)

    def _start(self, managed: _ManagedTorrent) -> None:
        managed.task = asyncio.ensure_future(self._run(managed))

    async def _run(self, managed: _ManagedTorrent) -> None:
        try:
            await managed.session.download()
        except asyncio.CancelledError:
            return  # cancellation is driven by _stop(), which handles cleanup
        except Exception:
            logger.exception(
                "torrent %s: unexpected error escaped download()", managed.session.torrent_id
            )
        managed.task = None
        async with self._lock:
            await self._rebalance()

    async def _stop(self, managed: _ManagedTorrent) -> None:
        task = managed.task
        if task is None:
            return
        managed.task = None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("torrent %s: error while stopping", managed.session.torrent_id)
        if managed.session.status not in _FINISHED_STATUSES:
            managed.session.status = TorrentStatus.QUEUED
