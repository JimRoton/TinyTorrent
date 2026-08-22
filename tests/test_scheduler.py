import asyncio

import pytest

from tinytorrent.common.priority import Priority
from tinytorrent.daemon.scheduler import Scheduler, SchedulerError
from tinytorrent.daemon.torrent_session import ProgressSnapshot, TorrentStatus


class FakeSession:
    """A minimal stand-in for TorrentSession, controllable from tests.

    ``download()`` blocks until either ``finish()`` is called (simulating
    natural completion) or the task is cancelled (simulating preemption).
    """

    def __init__(self, torrent_id: str, priority: Priority = Priority.NORMAL, bytes_downloaded: int = 0):
        self.torrent_id = torrent_id
        self.priority = priority
        self.status = TorrentStatus.QUEUED
        self.bytes_downloaded = bytes_downloaded
        self.download_call_count = 0
        self.was_cancelled = False
        self._release = asyncio.Event()
        self._finish_as_error = False

    def progress(self) -> ProgressSnapshot:
        return ProgressSnapshot(
            status=self.status,
            bytes_downloaded=self.bytes_downloaded,
            total_length=1000,
            download_rate_bps=0.0,
            eta_seconds=None,
            num_pieces=10,
            completed_pieces=0,
        )

    async def download(self) -> None:
        self.download_call_count += 1
        self.status = TorrentStatus.DOWNLOADING
        try:
            await self._release.wait()
            self.status = TorrentStatus.ERROR if self._finish_as_error else TorrentStatus.COMPLETE
        except asyncio.CancelledError:
            self.was_cancelled = True
            raise

    def finish(self) -> None:
        self._release.set()

    def error_out(self) -> None:
        self._finish_as_error = True
        self._release.set()


def _run(coro):
    return asyncio.run(coro)


class TestBasicScheduling:
    def test_torrents_up_to_cap_all_become_active(self):
        async def scenario():
            scheduler = Scheduler(max_active=4)
            sessions = [FakeSession(f"t{i}") for i in range(4)]
            for s in sessions:
                await scheduler.add(s)
            await asyncio.sleep(0)  # let tasks start
            assert all(scheduler.is_active(s.torrent_id) for s in sessions)

        _run(scenario())

    def test_fifth_torrent_stays_queued(self):
        async def scenario():
            scheduler = Scheduler(max_active=4)
            sessions = [FakeSession(f"t{i}") for i in range(5)]
            for s in sessions:
                await scheduler.add(s)
            await asyncio.sleep(0)
            assert all(scheduler.is_active(s.torrent_id) for s in sessions[:4])
            assert not scheduler.is_active(sessions[4].torrent_id)
            assert sessions[4].status == TorrentStatus.QUEUED

        _run(scenario())

    def test_same_priority_never_preempts(self):
        async def scenario():
            scheduler = Scheduler(max_active=2)
            a = FakeSession("a", Priority.NORMAL)
            b = FakeSession("b", Priority.NORMAL)
            c = FakeSession("c", Priority.NORMAL)
            for s in (a, b, c):
                await scheduler.add(s)
            await asyncio.sleep(0)
            assert scheduler.is_active("a")
            assert scheduler.is_active("b")
            assert not scheduler.is_active("c")
            assert a.was_cancelled is False
            assert b.was_cancelled is False

        _run(scenario())


class TestAutomaticPreemption:
    def test_higher_priority_add_preempts_lowest_active(self):
        async def scenario():
            scheduler = Scheduler(max_active=2)
            low_a = FakeSession("low_a", Priority.LOW)
            low_b = FakeSession("low_b", Priority.LOW)
            await scheduler.add(low_a)
            await scheduler.add(low_b)
            await asyncio.sleep(0)
            assert scheduler.is_active("low_a")
            assert scheduler.is_active("low_b")

            high = FakeSession("high", Priority.HIGH)
            await scheduler.add(high)
            await asyncio.sleep(0)

            assert scheduler.is_active("high")
            # Exactly one of the two low-priority torrents got evicted.
            active_lows = [t for t in ("low_a", "low_b") if scheduler.is_active(t)]
            assert len(active_lows) == 1

        _run(scenario())

    def test_priority_change_can_trigger_preemption(self):
        async def scenario():
            scheduler = Scheduler(max_active=1)
            a = FakeSession("a", Priority.NORMAL)
            b = FakeSession("b", Priority.LOW)
            await scheduler.add(a)
            await scheduler.add(b)
            await asyncio.sleep(0)
            assert scheduler.is_active("a")
            assert not scheduler.is_active("b")

            await scheduler.set_priority("b", Priority.HIGH)
            await asyncio.sleep(0)

            assert scheduler.is_active("b")
            assert not scheduler.is_active("a")
            assert a.was_cancelled is True
            assert a.status == TorrentStatus.QUEUED

        _run(scenario())

    def test_eviction_tie_break_is_least_progress(self):
        async def scenario():
            scheduler = Scheduler(max_active=2)
            behind = FakeSession("behind", Priority.LOW, bytes_downloaded=10)
            ahead = FakeSession("ahead", Priority.LOW, bytes_downloaded=500)
            await scheduler.add(behind)
            await scheduler.add(ahead)
            await asyncio.sleep(0)

            high = FakeSession("high", Priority.HIGH)
            await scheduler.add(high)
            await asyncio.sleep(0)

            assert scheduler.is_active("high")
            assert scheduler.is_active("ahead")
            assert not scheduler.is_active("behind")
            assert behind.was_cancelled is True
            assert ahead.was_cancelled is False

        _run(scenario())


class TestNaturalCompletionFreesSlot:
    def test_completing_torrent_lets_next_queued_start(self):
        async def scenario():
            scheduler = Scheduler(max_active=1)
            a = FakeSession("a")
            b = FakeSession("b")
            await scheduler.add(a)
            await scheduler.add(b)
            await asyncio.sleep(0)
            assert scheduler.is_active("a")
            assert not scheduler.is_active("b")

            a.finish()
            await asyncio.sleep(0.05)  # let the completion + rebalance run

            assert not scheduler.is_active("a")
            assert scheduler.is_active("b")

        _run(scenario())

    def test_error_also_frees_slot(self):
        async def scenario():
            scheduler = Scheduler(max_active=1)
            a = FakeSession("a")
            b = FakeSession("b")
            await scheduler.add(a)
            await scheduler.add(b)
            await asyncio.sleep(0)

            a.error_out()
            await asyncio.sleep(0.05)

            assert a.status == TorrentStatus.ERROR
            assert scheduler.is_active("b")

        _run(scenario())


class TestManualPromote:
    def test_promote_queued_torrent_with_free_slot(self):
        async def scenario():
            scheduler = Scheduler(max_active=2)
            a = FakeSession("a")
            b = FakeSession("b")
            await scheduler.add(a)
            await asyncio.sleep(0)
            # only one torrent, one free slot
            await scheduler.promote("a")  # already active, no-op
            assert scheduler.is_active("a")

        _run(scenario())

    def test_promote_evicts_weakest_active_when_full(self):
        async def scenario():
            scheduler = Scheduler(max_active=1)
            a = FakeSession("a", Priority.HIGH)
            b = FakeSession("b", Priority.HIGH)  # same priority as a, would never auto-preempt
            await scheduler.add(a)
            await scheduler.add(b)
            await asyncio.sleep(0)
            assert scheduler.is_active("a")
            assert not scheduler.is_active("b")

            await scheduler.promote("b")
            await asyncio.sleep(0)

            assert scheduler.is_active("b")
            assert not scheduler.is_active("a")
            assert a.was_cancelled is True

        _run(scenario())

    def test_promote_finished_torrent_is_noop(self):
        async def scenario():
            scheduler = Scheduler(max_active=1)
            a = FakeSession("a")
            await scheduler.add(a)
            await asyncio.sleep(0)
            a.finish()
            await asyncio.sleep(0.05)
            assert a.status == TorrentStatus.COMPLETE

            await scheduler.promote("a")  # should not raise or reactivate
            assert not scheduler.is_active("a")

        _run(scenario())


class TestRemove:
    def test_remove_active_torrent_cancels_and_frees_slot(self):
        async def scenario():
            scheduler = Scheduler(max_active=1)
            a = FakeSession("a")
            b = FakeSession("b")
            await scheduler.add(a)
            await scheduler.add(b)
            await asyncio.sleep(0)
            assert scheduler.is_active("a")

            await scheduler.remove("a")
            await asyncio.sleep(0)

            assert scheduler.is_active("b")
            assert a.was_cancelled is True
            with pytest.raises(SchedulerError):
                scheduler.get_session("a")

        _run(scenario())

    def test_remove_unknown_torrent_raises(self):
        async def scenario():
            scheduler = Scheduler()
            with pytest.raises(SchedulerError):
                await scheduler.remove("nope")

        _run(scenario())


class TestShutdown:
    def test_shutdown_cancels_all_active(self):
        async def scenario():
            scheduler = Scheduler(max_active=4)
            sessions = [FakeSession(f"t{i}") for i in range(3)]
            for s in sessions:
                await scheduler.add(s)
            await asyncio.sleep(0)
            assert all(scheduler.is_active(s.torrent_id) for s in sessions)

            await scheduler.shutdown()

            assert all(s.was_cancelled for s in sessions)
            assert all(not scheduler.is_active(s.torrent_id) for s in sessions)

        _run(scenario())
