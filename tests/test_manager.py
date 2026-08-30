import asyncio
import hashlib
import json
import sys
import time

import pytest

from tinytorrent.common.hooks import HookCommand, HookEvent
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


def _py(code: str) -> tuple[str, ...]:
    return (sys.executable, "-c", code)


def _hook(argv, on_failure="ignore", timeout_seconds=30.0) -> HookCommand:
    return HookCommand(argv=tuple(argv), on_failure=on_failure, timeout_seconds=timeout_seconds)


class TestPauseAndResume:
    def test_pause_sets_paused_status(self, tmp_path):
        async def scenario():
            manager = DaemonManager(tmp_path / "downloads", tmp_path / "state.json", generate_peer_id())
            torrent_id = await manager.add_torrent(_no_tracker_magnet())
            await manager.pause(torrent_id)
            assert manager.get_torrent(torrent_id).status == TorrentStatus.PAUSED

        _run(scenario())

    def test_resume_clears_paused_status(self, tmp_path):
        async def scenario():
            manager = DaemonManager(tmp_path / "downloads", tmp_path / "state.json", generate_peer_id())
            torrent_id = await manager.add_torrent(_no_tracker_magnet())
            await manager.pause(torrent_id)
            await manager.resume(torrent_id)
            assert manager.get_torrent(torrent_id).status != TorrentStatus.PAUSED

        _run(scenario())

    def test_pause_is_persisted(self, tmp_path):
        state_file = tmp_path / "state.json"

        async def scenario():
            manager = DaemonManager(tmp_path / "downloads", state_file, generate_peer_id())
            torrent_id = await manager.add_torrent(_no_tracker_magnet())
            await manager.pause(torrent_id)
            return torrent_id

        torrent_id = _run(scenario())
        payload = json.loads(state_file.read_text(encoding="utf-8"))
        (entry,) = payload["torrents"]
        assert entry["id"] == torrent_id
        assert entry["paused"] is True

    def test_resume_is_persisted(self, tmp_path):
        state_file = tmp_path / "state.json"

        async def scenario():
            manager = DaemonManager(tmp_path / "downloads", state_file, generate_peer_id())
            torrent_id = await manager.add_torrent(_no_tracker_magnet())
            await manager.pause(torrent_id)
            await manager.resume(torrent_id)

        _run(scenario())
        payload = json.loads(state_file.read_text(encoding="utf-8"))
        assert payload["torrents"][0]["paused"] is False

    def test_paused_state_survives_a_restart(self, tmp_path):
        state_file = tmp_path / "state.json"

        async def first_run():
            manager = DaemonManager(tmp_path / "downloads", state_file, generate_peer_id())
            torrent_id = await manager.add_torrent(_no_tracker_magnet())
            await manager.pause(torrent_id)
            return torrent_id

        torrent_id = _run(first_run())

        async def second_run():
            manager = DaemonManager(tmp_path / "downloads", state_file, generate_peer_id())
            await manager.load_from_disk()
            session = manager.get_torrent(torrent_id)
            assert session.status == TorrentStatus.PAUSED
            # A restored pause must also keep the torrent out of an
            # active slot, not merely label it.
            assert not manager.scheduler.is_active(torrent_id)

        _run(second_run())

    def test_pause_unknown_id_raises(self, tmp_path):
        async def scenario():
            manager = DaemonManager(tmp_path / "downloads", tmp_path / "state.json", generate_peer_id())
            with pytest.raises(SchedulerError):
                await manager.pause("zzzz")

        _run(scenario())

    def test_resume_unknown_id_raises(self, tmp_path):
        async def scenario():
            manager = DaemonManager(tmp_path / "downloads", tmp_path / "state.json", generate_peer_id())
            with pytest.raises(SchedulerError):
                await manager.resume("zzzz")

        _run(scenario())


class TestHookWiring:
    def test_sessions_are_wired_to_the_hook_runner(self, tmp_path):
        async def scenario():
            manager = DaemonManager(tmp_path / "downloads", tmp_path / "state.json", generate_peer_id())
            torrent_id = await manager.add_torrent(_no_tracker_magnet())
            session = manager.get_torrent(torrent_id)
            # The session fires lifecycle events back at the manager,
            # which is what turns them into hook runs.
            assert session._on_event is not None

        _run(scenario())

    def test_purge_fires_the_torrent_purged_hook(self, tmp_path):
        marker = tmp_path / "purged"

        async def scenario():
            hooks = {
                HookEvent.TORRENT_PURGED: (
                    _hook(_py(f"open({str(marker)!r}, 'w').write('x')")),
                )
            }
            manager = DaemonManager(
                tmp_path / "downloads", tmp_path / "state.json", generate_peer_id(), hooks=hooks
            )
            torrent_id = await manager.add_torrent(_no_tracker_magnet())
            await manager.purge(torrent_id)
            await manager.hook_runner.wait_idle()

        _run(scenario())
        assert marker.exists()

    def test_purge_hook_receives_torrent_placeholders(self, tmp_path):
        out = tmp_path / "context"

        async def scenario():
            argv = _py("import sys; open(sys.argv[1], 'w').write(sys.argv[2] + '|' + sys.argv[3])") + (
                str(out),
                "%name%",
                "%deleted_data%",
            )
            hooks = {HookEvent.TORRENT_PURGED: (_hook(argv),)}
            manager = DaemonManager(
                tmp_path / "downloads", tmp_path / "state.json", generate_peer_id(), hooks=hooks
            )
            torrent_id = await manager.add_torrent(_no_tracker_magnet(name="my-torrent"))
            await manager.purge(torrent_id, with_data=True)
            await manager.hook_runner.wait_idle()

        _run(scenario())
        assert out.read_text() == "my-torrent|true"

    def test_purge_without_data_reports_deleted_data_false(self, tmp_path):
        out = tmp_path / "context"

        async def scenario():
            argv = _py("import sys; open(sys.argv[1], 'w').write(sys.argv[2])") + (
                str(out),
                "%deleted_data%",
            )
            hooks = {HookEvent.TORRENT_PURGED: (_hook(argv),)}
            manager = DaemonManager(
                tmp_path / "downloads", tmp_path / "state.json", generate_peer_id(), hooks=hooks
            )
            torrent_id = await manager.add_torrent(_no_tracker_magnet())
            await manager.purge(torrent_id, with_data=False)
            await manager.hook_runner.wait_idle()

        _run(scenario())
        assert out.read_text() == "false"

    def test_no_hooks_configured_is_harmless(self, tmp_path):
        async def scenario():
            manager = DaemonManager(tmp_path / "downloads", tmp_path / "state.json", generate_peer_id())
            torrent_id = await manager.add_torrent(_no_tracker_magnet())
            await manager.purge(torrent_id)
            await manager.hook_runner.wait_idle()
            assert manager.list_torrents() == []

        _run(scenario())


class TestTestHook:
    def test_runs_the_configured_commands(self, tmp_path):
        marker = tmp_path / "ran"

        async def scenario():
            hooks = {
                HookEvent.DOWNLOAD_COMPLETED: (_hook(_py(f"open({str(marker)!r}, 'w').write('x')")),)
            }
            manager = DaemonManager(
                tmp_path / "downloads", tmp_path / "state.json", generate_peer_id(), hooks=hooks
            )
            torrent_id = await manager.add_torrent(_no_tracker_magnet())
            return await manager.test_hook(HookEvent.DOWNLOAD_COMPLETED, torrent_id=torrent_id)

        result = _run(scenario())
        assert marker.exists()
        assert result["configured"] is True
        assert [r["outcome"] for r in result["results"]] == ["ok"]

    def test_reports_the_torrent_it_ran_against(self, tmp_path):
        async def scenario():
            manager = DaemonManager(tmp_path / "downloads", tmp_path / "state.json", generate_peer_id())
            torrent_id = await manager.add_torrent(_no_tracker_magnet(name="ubuntu.iso"))
            return await manager.test_hook(HookEvent.DOWNLOAD_COMPLETED, torrent_id=torrent_id)

        result = _run(scenario())
        assert result["torrent_name"] == "ubuntu.iso"

    def test_unconfigured_event_reports_not_configured(self, tmp_path):
        async def scenario():
            manager = DaemonManager(tmp_path / "downloads", tmp_path / "state.json", generate_peer_id())
            torrent_id = await manager.add_torrent(_no_tracker_magnet())
            return await manager.test_hook(HookEvent.DOWNLOAD_ERROR, torrent_id=torrent_id)

        result = _run(scenario())
        assert result["configured"] is False
        assert result["results"] == []

    def test_does_not_perform_the_real_action(self, tmp_path):
        # Testing torrent_purged must not actually purge the torrent.
        async def scenario():
            manager = DaemonManager(tmp_path / "downloads", tmp_path / "state.json", generate_peer_id())
            torrent_id = await manager.add_torrent(_no_tracker_magnet())
            await manager.test_hook(HookEvent.TORRENT_PURGED, torrent_id=torrent_id)
            assert len(manager.list_torrents()) == 1

        _run(scenario())

    def test_resolves_by_name(self, tmp_path):
        async def scenario():
            manager = DaemonManager(tmp_path / "downloads", tmp_path / "state.json", generate_peer_id())
            torrent_id = await manager.add_torrent(_no_tracker_magnet(name="ubuntu.iso"))
            result = await manager.test_hook(HookEvent.DOWNLOAD_COMPLETED, name="ubuntu.iso")
            assert result["torrent_id"] == torrent_id

        _run(scenario())

    def test_name_match_is_case_insensitive(self, tmp_path):
        async def scenario():
            manager = DaemonManager(tmp_path / "downloads", tmp_path / "state.json", generate_peer_id())
            torrent_id = await manager.add_torrent(_no_tracker_magnet(name="Ubuntu.ISO"))
            result = await manager.test_hook(HookEvent.DOWNLOAD_COMPLETED, name="ubuntu.iso")
            assert result["torrent_id"] == torrent_id

        _run(scenario())

    def test_ambiguous_name_is_rejected(self, tmp_path):
        async def scenario():
            manager = DaemonManager(tmp_path / "downloads", tmp_path / "state.json", generate_peer_id())
            await manager.add_torrent(_no_tracker_magnet(name="dupe", hash_byte=b"\x01"))
            await manager.add_torrent(_no_tracker_magnet(name="dupe", hash_byte=b"\x02"))
            with pytest.raises(ValueError) as exc:
                await manager.test_hook(HookEvent.DOWNLOAD_COMPLETED, name="dupe")
            assert "use --id" in str(exc.value)

        _run(scenario())

    def test_unknown_id_is_rejected(self, tmp_path):
        async def scenario():
            manager = DaemonManager(tmp_path / "downloads", tmp_path / "state.json", generate_peer_id())
            with pytest.raises(ValueError):
                await manager.test_hook(HookEvent.DOWNLOAD_COMPLETED, torrent_id="zzzz")

        _run(scenario())

    def test_unknown_name_is_rejected(self, tmp_path):
        async def scenario():
            manager = DaemonManager(tmp_path / "downloads", tmp_path / "state.json", generate_peer_id())
            with pytest.raises(ValueError):
                await manager.test_hook(HookEvent.DOWNLOAD_COMPLETED, name="nope")

        _run(scenario())

    def test_neither_id_nor_name_is_rejected(self, tmp_path):
        async def scenario():
            manager = DaemonManager(tmp_path / "downloads", tmp_path / "state.json", generate_peer_id())
            with pytest.raises(ValueError):
                await manager.test_hook(HookEvent.DOWNLOAD_COMPLETED)

        _run(scenario())

    def test_falls_back_to_name_when_id_is_not_found(self, tmp_path):
        async def scenario():
            manager = DaemonManager(tmp_path / "downloads", tmp_path / "state.json", generate_peer_id())
            torrent_id = await manager.add_torrent(_no_tracker_magnet(name="ubuntu.iso"))
            result = await manager.test_hook(
                HookEvent.DOWNLOAD_COMPLETED, torrent_id="zzzz", name="ubuntu.iso"
            )
            assert result["torrent_id"] == torrent_id

        _run(scenario())


def _repeating_hook(argv, interval_seconds=0.2, on_failure="ignore"):
    return HookCommand(
        argv=tuple(argv),
        on_failure=on_failure,
        timeout_seconds=30.0,
        interval_seconds=interval_seconds,
    )


async def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


class TestDaemonContext:
    def test_reports_the_daemon_settings(self, tmp_path):
        manager = DaemonManager(
            tmp_path / "downloads",
            tmp_path / "state.json",
            generate_peer_id(),
            max_active=7,
            socket_path=tmp_path / "d.sock",
        )
        context = manager.daemon_context()
        assert context["download_dir"] == str(tmp_path / "downloads")
        assert context["state_file"] == str(tmp_path / "state.json")
        assert context["socket_path"] == str(tmp_path / "d.sock")
        assert context["max_active"] == "7"
        assert context["torrent_count"] == "0"

    def test_torrent_count_tracks_added_torrents(self, tmp_path):
        async def scenario():
            manager = DaemonManager(tmp_path / "downloads", tmp_path / "state.json", generate_peer_id())
            await manager.add_torrent(_no_tracker_magnet(hash_byte=b"\x01"))
            await manager.add_torrent(_no_tracker_magnet(hash_byte=b"\x02"))
            assert manager.daemon_context()["torrent_count"] == "2"

        _run(scenario())

    def test_socket_path_is_optional(self, tmp_path):
        manager = DaemonManager(tmp_path / "downloads", tmp_path / "state.json", generate_peer_id())
        assert manager.daemon_context()["socket_path"] == ""


class TestDaemonStartedHook:
    def test_fires_configured_commands(self, tmp_path):
        marker = tmp_path / "started"

        async def scenario():
            hooks = {
                HookEvent.DAEMON_STARTED: (_hook(_py(f"open({str(marker)!r}, 'w').write('x')")),)
            }
            manager = DaemonManager(
                tmp_path / "downloads", tmp_path / "state.json", generate_peer_id(), hooks=hooks
            )
            manager.fire_daemon_started()
            await manager.hook_runner.wait_idle()

        _run(scenario())
        assert marker.exists()

    def test_receives_the_daemon_context(self, tmp_path):
        out = tmp_path / "context"

        async def scenario():
            argv = _py("import sys; open(sys.argv[1], 'w').write(sys.argv[2])") + (
                str(out),
                "%max_active%",
            )
            manager = DaemonManager(
                tmp_path / "downloads",
                tmp_path / "state.json",
                generate_peer_id(),
                max_active=3,
                hooks={HookEvent.DAEMON_STARTED: (_hook(argv),)},
            )
            manager.fire_daemon_started()
            await manager.hook_runner.wait_idle()

        _run(scenario())
        assert out.read_text() == "3"

    def test_starts_repeating_commands(self, tmp_path):
        ticks = tmp_path / "ticks"

        async def scenario():
            argv = _py(f"open({str(ticks)!r}, 'a').write('x')")
            manager = DaemonManager(
                tmp_path / "downloads",
                tmp_path / "state.json",
                generate_peer_id(),
                hooks={HookEvent.DAEMON_STARTED: (_repeating_hook(argv),)},
            )
            manager.fire_daemon_started()
            reached = await _wait_for(
                lambda: ticks.exists() and len(ticks.read_text()) >= 2
            )
            await manager.shutdown()
            return reached

        assert _run(scenario()) is True

    def test_no_hooks_configured_is_harmless(self, tmp_path):
        async def scenario():
            manager = DaemonManager(tmp_path / "downloads", tmp_path / "state.json", generate_peer_id())
            manager.fire_daemon_started()
            await manager.hook_runner.wait_idle()

        _run(scenario())


class TestDaemonStoppingHook:
    def test_shutdown_runs_and_waits_for_stop_hooks(self, tmp_path):
        marker = tmp_path / "stopped"

        async def scenario():
            hooks = {
                HookEvent.DAEMON_STOPPING: (_hook(_py(f"open({str(marker)!r}, 'w').write('x')")),)
            }
            manager = DaemonManager(
                tmp_path / "downloads", tmp_path / "state.json", generate_peer_id(), hooks=hooks
            )
            await manager.shutdown()
            # Awaited, not fired -- it has already run by the time
            # shutdown() returns, with no wait_idle() needed.
            assert marker.exists()

        _run(scenario())

    def test_stop_hook_sees_torrents_before_they_are_stopped(self, tmp_path):
        out = tmp_path / "count"

        async def scenario():
            argv = _py("import sys; open(sys.argv[1], 'w').write(sys.argv[2])") + (
                str(out),
                "%torrent_count%",
            )
            manager = DaemonManager(
                tmp_path / "downloads",
                tmp_path / "state.json",
                generate_peer_id(),
                hooks={HookEvent.DAEMON_STOPPING: (_hook(argv),)},
            )
            await manager.add_torrent(_no_tracker_magnet())
            await manager.shutdown()

        _run(scenario())
        assert out.read_text() == "1"

    def test_shutdown_stops_repeating_hooks(self, tmp_path):
        ticks = tmp_path / "ticks"

        async def scenario():
            argv = _py(f"open({str(ticks)!r}, 'a').write('x')")
            manager = DaemonManager(
                tmp_path / "downloads",
                tmp_path / "state.json",
                generate_peer_id(),
                hooks={HookEvent.DAEMON_STARTED: (_repeating_hook(argv),)},
            )
            manager.fire_daemon_started()
            await _wait_for(lambda: ticks.exists())
            await manager.shutdown()
            settled = len(ticks.read_text())
            await asyncio.sleep(0.6)  # several intervals
            return settled, len(ticks.read_text())

        settled, after = _run(scenario())
        assert after <= settled + 1

    def test_shutdown_without_stop_hooks_still_works(self, tmp_path):
        async def scenario():
            manager = DaemonManager(tmp_path / "downloads", tmp_path / "state.json", generate_peer_id())
            await manager.add_torrent(_no_tracker_magnet())
            await manager.shutdown()
            assert (tmp_path / "state.json").exists()

        _run(scenario())


class TestTestHookForDaemonEvents:
    def test_needs_no_torrent(self, tmp_path):
        marker = tmp_path / "ran"

        async def scenario():
            hooks = {
                HookEvent.DAEMON_STARTED: (_hook(_py(f"open({str(marker)!r}, 'w').write('x')")),)
            }
            manager = DaemonManager(
                tmp_path / "downloads", tmp_path / "state.json", generate_peer_id(), hooks=hooks
            )
            return await manager.test_hook(HookEvent.DAEMON_STARTED)

        result = _run(scenario())
        assert marker.exists()
        assert result["torrent_id"] is None
        assert result["torrent_name"] is None
        assert result["configured"] is True

    def test_daemon_stopping_can_be_tested(self, tmp_path):
        async def scenario():
            manager = DaemonManager(tmp_path / "downloads", tmp_path / "state.json", generate_peer_id())
            return await manager.test_hook(HookEvent.DAEMON_STOPPING)

        result = _run(scenario())
        assert result["configured"] is False

    def test_testing_daemon_stopping_does_not_stop_anything(self, tmp_path):
        async def scenario():
            manager = DaemonManager(tmp_path / "downloads", tmp_path / "state.json", generate_peer_id())
            await manager.add_torrent(_no_tracker_magnet())
            await manager.test_hook(HookEvent.DAEMON_STOPPING)
            assert len(manager.list_torrents()) == 1

        _run(scenario())

    def test_a_supplied_torrent_id_is_ignored(self, tmp_path):
        async def scenario():
            manager = DaemonManager(tmp_path / "downloads", tmp_path / "state.json", generate_peer_id())
            torrent_id = await manager.add_torrent(_no_tracker_magnet())
            result = await manager.test_hook(HookEvent.DAEMON_STARTED, torrent_id=torrent_id)
            assert result["torrent_id"] is None

        _run(scenario())

    def test_an_unknown_torrent_id_is_not_an_error(self, tmp_path):
        async def scenario():
            manager = DaemonManager(tmp_path / "downloads", tmp_path / "state.json", generate_peer_id())
            result = await manager.test_hook(HookEvent.DAEMON_STARTED, torrent_id="zzzz")
            assert result["torrent_id"] is None

        _run(scenario())

    def test_repeating_command_runs_once(self, tmp_path):
        ticks = tmp_path / "ticks"

        async def scenario():
            argv = _py(f"open({str(ticks)!r}, 'a').write('x')")
            manager = DaemonManager(
                tmp_path / "downloads",
                tmp_path / "state.json",
                generate_peer_id(),
                hooks={HookEvent.DAEMON_STARTED: (_repeating_hook(argv, interval_seconds=60),)},
            )
            results = await manager.test_hook(HookEvent.DAEMON_STARTED)
            assert results["results"][0]["interval_seconds"] == 60.0

        _run(scenario())
        assert len(ticks.read_text()) == 1


class TestPurgeErrored:
    async def _manager_with_errored(self, tmp_path, count=2, healthy=1, hooks=None):
        manager = DaemonManager(
            tmp_path / "downloads", tmp_path / "state.json", generate_peer_id(), hooks=hooks
        )
        errored = []
        for i in range(count):
            torrent_id = await manager.add_torrent(
                _no_tracker_magnet(name=f"bad-{i}", hash_byte=bytes([i + 1]))
            )
            manager.get_torrent(torrent_id).status = TorrentStatus.ERROR
            errored.append(torrent_id)
        healthy_ids = []
        for i in range(healthy):
            torrent_id = await manager.add_torrent(
                _no_tracker_magnet(name=f"ok-{i}", hash_byte=bytes([100 + i]))
            )
            manager.get_torrent(torrent_id).status = TorrentStatus.QUEUED
            healthy_ids.append(torrent_id)
        return manager, errored, healthy_ids

    def test_removes_every_errored_torrent(self, tmp_path):
        async def scenario():
            manager, errored, _ = await self._manager_with_errored(tmp_path, count=3, healthy=0)
            purged = await manager.purge_errored()
            assert sorted(purged) == sorted(errored)
            assert manager.list_torrents() == []

        _run(scenario())

    def test_leaves_healthy_torrents_alone(self, tmp_path):
        async def scenario():
            manager, errored, healthy = await self._manager_with_errored(tmp_path, count=2, healthy=2)
            purged = await manager.purge_errored()
            assert sorted(purged) == sorted(errored)
            remaining = sorted(s.torrent_id for s in manager.list_torrents())
            assert remaining == sorted(healthy)

        _run(scenario())

    def test_returns_empty_when_nothing_errored(self, tmp_path):
        async def scenario():
            manager, _, healthy = await self._manager_with_errored(tmp_path, count=0, healthy=2)
            assert await manager.purge_errored() == []
            assert len(manager.list_torrents()) == 2

        _run(scenario())

    def test_on_an_empty_daemon(self, tmp_path):
        async def scenario():
            manager = DaemonManager(tmp_path / "downloads", tmp_path / "state.json", generate_peer_id())
            assert await manager.purge_errored() == []

        _run(scenario())

    def test_persists_the_removals(self, tmp_path):
        state_file = tmp_path / "state.json"

        async def scenario():
            manager = DaemonManager(tmp_path / "downloads", state_file, generate_peer_id())
            torrent_id = await manager.add_torrent(_no_tracker_magnet())
            manager.get_torrent(torrent_id).status = TorrentStatus.ERROR
            await manager.purge_errored()

        _run(scenario())
        payload = json.loads(state_file.read_text(encoding="utf-8"))
        assert payload["torrents"] == []

    def test_fires_the_purge_hook_for_each_torrent(self, tmp_path):
        marker = tmp_path / "purged"

        async def scenario():
            hooks = {
                HookEvent.TORRENT_PURGED: (_hook(_py(f"open({str(marker)!r}, 'a').write('x')")),)
            }
            manager, errored, _ = await self._manager_with_errored(
                tmp_path, count=3, healthy=1, hooks=hooks
            )
            await manager.purge_errored()
            await manager.hook_runner.wait_idle()
            return len(errored)

        count = _run(scenario())
        assert len(marker.read_text()) == count

    def test_with_data_deletes_files(self, tmp_path):
        async def scenario():
            manager = DaemonManager(tmp_path / "downloads", tmp_path / "state.json", generate_peer_id())
            torrent_id = await manager.add_torrent(_no_tracker_magnet())
            session = manager.get_torrent(torrent_id)
            session.status = TorrentStatus.ERROR
            info_dict = {
                b"name": b"file.txt",
                b"piece length": 16384,
                b"pieces": hashlib.sha1(b"x").digest(),
                b"length": 1,
            }
            session.info = parse_info_dict(info_dict)
            session.storage = PieceStorage(session.info, manager.download_dir)
            target = manager.download_dir / "file.txt"
            assert target.exists()

            await manager.purge_errored(with_data=True)
            assert not target.exists()

        _run(scenario())
