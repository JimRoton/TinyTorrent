import asyncio
import hashlib

import pytest

from tinytorrent.common.ids import generate_peer_id
from tinytorrent.common.priority import Priority
from tinytorrent.daemon.manager import DaemonManager
from tinytorrent.daemon.piece_manager import PieceStorage
from tinytorrent.daemon.scheduler import SchedulerError
from tinytorrent.daemon.torrent_info import parse_info_dict
from tinytorrent.daemon.torrent_session import TorrentSession, TorrentStatus


def _no_tracker_magnet(name="test-torrent", hash_byte=b"\x00"):
    info_hash_hex = (hash_byte * 20).hex()
    return f"magnet:?xt=urn:btih:{info_hash_hex}&dn={name}"


def _run(coro):
    return asyncio.run(coro)


class TestAddTorrent:
    def test_returns_a_short_id(self, tmp_path):
        async def scenario():
            manager = DaemonManager(tmp_path / "downloads", tmp_path / "state.json", generate_peer_id())
            torrent_id = await manager.add_torrent(_no_tracker_magnet())
            assert isinstance(torrent_id, str)
            assert len(torrent_id) == 4

        _run(scenario())

    def test_added_torrent_appears_in_list(self, tmp_path):
        async def scenario():
            manager = DaemonManager(tmp_path / "downloads", tmp_path / "state.json", generate_peer_id())
            torrent_id = await manager.add_torrent(_no_tracker_magnet(), priority=Priority.HIGH)
            sessions = manager.list_torrents()
            assert any(s.torrent_id == torrent_id for s in sessions)
            found = manager.get_torrent(torrent_id)
            assert found.priority == Priority.HIGH

        _run(scenario())

    def test_invalid_magnet_raises_before_registering(self, tmp_path):
        async def scenario():
            manager = DaemonManager(tmp_path / "downloads", tmp_path / "state.json", generate_peer_id())
            with pytest.raises(Exception):
                await manager.add_torrent("not-a-magnet-link")
            assert manager.list_torrents() == []

        _run(scenario())

    def test_ids_are_unique(self, tmp_path):
        async def scenario():
            manager = DaemonManager(tmp_path / "downloads", tmp_path / "state.json", generate_peer_id())
            ids = set()
            for i in range(10):
                tid = await manager.add_torrent(_no_tracker_magnet(name=f"t{i}", hash_byte=bytes([i])))
                ids.add(tid)
            assert len(ids) == 10

        _run(scenario())

    def test_saves_state_immediately(self, tmp_path):
        async def scenario():
            state_file = tmp_path / "state.json"
            manager = DaemonManager(tmp_path / "downloads", state_file, generate_peer_id())
            await manager.add_torrent(_no_tracker_magnet())
            assert state_file.exists()

        _run(scenario())


class TestPurge:
    def test_purge_removes_from_list(self, tmp_path):
        async def scenario():
            manager = DaemonManager(tmp_path / "downloads", tmp_path / "state.json", generate_peer_id())
            torrent_id = await manager.add_torrent(_no_tracker_magnet())
            await manager.purge(torrent_id)
            assert manager.list_torrents() == []
            with pytest.raises(SchedulerError):
                manager.get_torrent(torrent_id)

        _run(scenario())

    def test_purge_unknown_torrent_raises(self, tmp_path):
        async def scenario():
            manager = DaemonManager(tmp_path / "downloads", tmp_path / "state.json", generate_peer_id())
            with pytest.raises(SchedulerError):
                await manager.purge("nope")

        _run(scenario())

    def test_purge_with_data_deletes_files(self, tmp_path):
        async def scenario():
            manager = DaemonManager(tmp_path / "downloads", tmp_path / "state.json", generate_peer_id())
            info = parse_info_dict(
                {
                    b"name": b"file.txt",
                    b"piece length": 10,
                    b"pieces": hashlib.sha1(b"a" * 10).digest(),
                    b"length": 10,
                }
            )
            session = TorrentSession(
                "zzzz", _fake_magnet(), manager.download_dir, generate_peer_id(), info=info
            )
            session.storage = PieceStorage(info, manager.download_dir)
            manager._magnet_uris["zzzz"] = "magnet:?xt=urn:btih:" + "00" * 20
            await manager.scheduler.add(session)

            path = manager.download_dir / "file.txt"
            assert path.exists()

            await manager.purge("zzzz", with_data=True)
            assert not path.exists()

        _run(scenario())

    def test_purge_without_data_keeps_files(self, tmp_path):
        async def scenario():
            manager = DaemonManager(tmp_path / "downloads", tmp_path / "state.json", generate_peer_id())
            info = parse_info_dict(
                {
                    b"name": b"file.txt",
                    b"piece length": 10,
                    b"pieces": hashlib.sha1(b"a" * 10).digest(),
                    b"length": 10,
                }
            )
            session = TorrentSession(
                "zzzz", _fake_magnet(), manager.download_dir, generate_peer_id(), info=info
            )
            session.storage = PieceStorage(info, manager.download_dir)
            manager._magnet_uris["zzzz"] = "magnet:?xt=urn:btih:" + "00" * 20
            await manager.scheduler.add(session)

            path = manager.download_dir / "file.txt"
            await manager.purge("zzzz", with_data=False)
            assert path.exists()

        _run(scenario())


def _fake_magnet():
    from tinytorrent.daemon.magnet import MagnetLink

    return MagnetLink(info_hash=b"\x00" * 20, display_name=None, trackers=())


class TestSetPriority:
    def test_updates_session_priority(self, tmp_path):
        async def scenario():
            manager = DaemonManager(tmp_path / "downloads", tmp_path / "state.json", generate_peer_id())
            torrent_id = await manager.add_torrent(_no_tracker_magnet(), priority=Priority.LOW)
            await manager.set_priority(torrent_id, Priority.HIGH)
            assert manager.get_torrent(torrent_id).priority == Priority.HIGH

        _run(scenario())


class TestPersistenceRoundTrip:
    def test_reload_recreates_torrents(self, tmp_path):
        async def scenario():
            download_dir = tmp_path / "downloads"
            state_file = tmp_path / "state.json"
            peer_id = generate_peer_id()

            manager1 = DaemonManager(download_dir, state_file, peer_id)
            torrent_id = await manager1.add_torrent(_no_tracker_magnet(name="movie.mkv"), priority=Priority.HIGH)
            await manager1.shutdown()

            manager2 = DaemonManager(download_dir, state_file, peer_id)
            await manager2.load_from_disk()

            reloaded = manager2.get_torrent(torrent_id)
            assert reloaded.priority == Priority.HIGH
            assert reloaded.magnet.display_name == "movie.mkv"

        _run(scenario())

    def test_reload_restores_known_metadata(self, tmp_path):
        async def scenario():
            download_dir = tmp_path / "downloads"
            state_file = tmp_path / "state.json"
            peer_id = generate_peer_id()

            info = parse_info_dict(
                {
                    b"name": b"file.txt",
                    b"piece length": 10,
                    b"pieces": hashlib.sha1(b"a" * 10).digest(),
                    b"length": 10,
                }
            )
            manager1 = DaemonManager(download_dir, state_file, peer_id)
            session = TorrentSession("zzzz", _fake_magnet(), download_dir, peer_id, info=info)
            manager1._magnet_uris["zzzz"] = "magnet:?xt=urn:btih:" + "00" * 20
            await manager1.scheduler.add(session)
            manager1.save_state()

            manager2 = DaemonManager(download_dir, state_file, peer_id)
            await manager2.load_from_disk()
            reloaded = manager2.get_torrent("zzzz")

            assert reloaded.info is not None
            assert reloaded.info.name == "file.txt"
            assert reloaded.info.total_length == 10

        _run(scenario())

    def test_reload_with_no_state_file_is_empty(self, tmp_path):
        async def scenario():
            manager = DaemonManager(tmp_path / "downloads", tmp_path / "state.json", generate_peer_id())
            await manager.load_from_disk()
            assert manager.list_torrents() == []

        _run(scenario())
