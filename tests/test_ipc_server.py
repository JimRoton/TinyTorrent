import asyncio
import json

import pytest

import sys

from tinytorrent.common.hooks import HookCommand, HookEvent
from tinytorrent.common.ids import generate_peer_id
from tinytorrent.daemon.ipc_server import IPCServer
from tinytorrent.daemon.hooks import HookRunner
from tinytorrent.daemon.manager import DaemonManager
from tinytorrent.daemon.torrent_session import TorrentStatus


def _no_tracker_magnet(name="test-torrent", hash_byte=b"\x00"):
    info_hash_hex = (hash_byte * 20).hex()
    return f"magnet:?xt=urn:btih:{info_hash_hex}&dn={name}"


async def _send(reader, writer, obj) -> dict:
    writer.write((json.dumps(obj) + "\n").encode("utf-8"))
    await writer.drain()
    line = await reader.readline()
    return json.loads(line.decode("utf-8"))


class _ServerFixture:
    def __init__(self, tmp_path):
        self.manager = DaemonManager(tmp_path / "downloads", tmp_path / "state.json", generate_peer_id())
        self.socket_path = tmp_path / "tinytorrentd.sock"
        self.server = IPCServer(self.manager, self.socket_path)

    async def __aenter__(self):
        await self.server.start()
        return self

    async def __aexit__(self, *exc_info):
        await self.server.stop()

    async def connect(self):
        return await asyncio.open_unix_connection(path=str(self.socket_path))


def _run(coro):
    return asyncio.run(coro)


class TestBasicRequestResponse:
    def test_list_on_empty_daemon(self, tmp_path):
        async def scenario():
            async with _ServerFixture(tmp_path) as fx:
                reader, writer = await fx.connect()
                resp = await _send(reader, writer, {"cmd": "list", "args": {}})
                assert resp == {"ok": True, "data": {"torrents": []}}
                writer.close()

        _run(scenario())

    def test_unknown_command(self, tmp_path):
        async def scenario():
            async with _ServerFixture(tmp_path) as fx:
                reader, writer = await fx.connect()
                resp = await _send(reader, writer, {"cmd": "fly-to-the-moon", "args": {}})
                assert resp["ok"] is False
                assert "fly-to-the-moon" in resp["error"]
                writer.close()

        _run(scenario())

    def test_malformed_json_line(self, tmp_path):
        async def scenario():
            async with _ServerFixture(tmp_path) as fx:
                reader, writer = await fx.connect()
                writer.write(b"not json at all\n")
                await writer.drain()
                line = await reader.readline()
                resp = json.loads(line.decode("utf-8"))
                assert resp["ok"] is False
                writer.close()

        _run(scenario())

    def test_connection_survives_a_bad_request(self, tmp_path):
        async def scenario():
            async with _ServerFixture(tmp_path) as fx:
                reader, writer = await fx.connect()
                bad = await _send(reader, writer, {"nope": True})
                assert bad["ok"] is False
                good = await _send(reader, writer, {"cmd": "list", "args": {}})
                assert good["ok"] is True
                writer.close()

        _run(scenario())


class TestFullCommandFlow:
    def test_add_list_priority_promote_purge(self, tmp_path):
        async def scenario():
            async with _ServerFixture(tmp_path) as fx:
                reader, writer = await fx.connect()

                add_resp = await _send(
                    reader, writer, {"cmd": "add", "args": {"magnet": _no_tracker_magnet(), "priority": "low"}}
                )
                assert add_resp["ok"] is True
                torrent_id = add_resp["data"]["id"]
                assert isinstance(torrent_id, str) and len(torrent_id) == 4

                list_resp = await _send(reader, writer, {"cmd": "list", "args": {}})
                assert list_resp["ok"] is True
                torrents = list_resp["data"]["torrents"]
                assert len(torrents) == 1
                assert torrents[0]["id"] == torrent_id
                assert torrents[0]["priority"] == "low"

                pri_resp = await _send(
                    reader, writer, {"cmd": "priority", "args": {"id": torrent_id, "priority": "high"}}
                )
                assert pri_resp["ok"] is True
                list_resp2 = await _send(reader, writer, {"cmd": "list", "args": {}})
                assert list_resp2["data"]["torrents"][0]["priority"] == "high"

                promote_resp = await _send(reader, writer, {"cmd": "promote", "args": {"id": torrent_id}})
                assert promote_resp["ok"] is True

                purge_resp = await _send(
                    reader, writer, {"cmd": "purge", "args": {"id": torrent_id, "with_data": False}}
                )
                assert purge_resp["ok"] is True

                list_resp3 = await _send(reader, writer, {"cmd": "list", "args": {}})
                assert list_resp3["data"]["torrents"] == []

                writer.close()

        _run(scenario())

    def test_add_missing_magnet_fails_cleanly(self, tmp_path):
        async def scenario():
            async with _ServerFixture(tmp_path) as fx:
                reader, writer = await fx.connect()
                resp = await _send(reader, writer, {"cmd": "add", "args": {}})
                assert resp["ok"] is False
                assert "magnet" in resp["error"]
                writer.close()

        _run(scenario())

    def test_add_invalid_magnet_fails_cleanly(self, tmp_path):
        async def scenario():
            async with _ServerFixture(tmp_path) as fx:
                reader, writer = await fx.connect()
                resp = await _send(reader, writer, {"cmd": "add", "args": {"magnet": "not-a-magnet"}})
                assert resp["ok"] is False
                writer.close()

        _run(scenario())

    def test_add_invalid_priority_fails_cleanly(self, tmp_path):
        async def scenario():
            async with _ServerFixture(tmp_path) as fx:
                reader, writer = await fx.connect()
                resp = await _send(
                    reader,
                    writer,
                    {"cmd": "add", "args": {"magnet": _no_tracker_magnet(), "priority": "urgent"}},
                )
                assert resp["ok"] is False
                assert "priority" in resp["error"]
                writer.close()

        _run(scenario())

    def test_purge_unknown_id_fails_cleanly(self, tmp_path):
        async def scenario():
            async with _ServerFixture(tmp_path) as fx:
                reader, writer = await fx.connect()
                resp = await _send(reader, writer, {"cmd": "purge", "args": {"id": "nope"}})
                assert resp["ok"] is False
                writer.close()

        _run(scenario())

    def test_priority_missing_id_fails_cleanly(self, tmp_path):
        async def scenario():
            async with _ServerFixture(tmp_path) as fx:
                reader, writer = await fx.connect()
                resp = await _send(reader, writer, {"cmd": "priority", "args": {"priority": "high"}})
                assert resp["ok"] is False
                writer.close()

        _run(scenario())


class TestPipeliningAndConcurrency:
    def test_pipelined_requests_get_in_order_responses(self, tmp_path):
        async def scenario():
            async with _ServerFixture(tmp_path) as fx:
                reader, writer = await fx.connect()
                for i in range(5):
                    writer.write(
                        (json.dumps({"cmd": "add", "args": {"magnet": _no_tracker_magnet(name=f"t{i}", hash_byte=bytes([i]))}}) + "\n").encode()
                    )
                await writer.drain()

                ids = []
                for _ in range(5):
                    line = await reader.readline()
                    resp = json.loads(line.decode("utf-8"))
                    assert resp["ok"] is True
                    ids.append(resp["data"]["id"])

                assert len(set(ids)) == 5  # all unique
                writer.close()

        _run(scenario())

    def test_concurrent_connections_do_not_interfere(self, tmp_path):
        async def scenario():
            async with _ServerFixture(tmp_path) as fx:
                reader1, writer1 = await fx.connect()
                reader2, writer2 = await fx.connect()

                resp1 = await _send(
                    reader1, writer1, {"cmd": "add", "args": {"magnet": _no_tracker_magnet(name="a", hash_byte=b"\x01")}}
                )
                resp2 = await _send(
                    reader2, writer2, {"cmd": "add", "args": {"magnet": _no_tracker_magnet(name="b", hash_byte=b"\x02")}}
                )
                assert resp1["ok"] and resp2["ok"]
                assert resp1["data"]["id"] != resp2["data"]["id"]

                list_resp = await _send(reader1, writer1, {"cmd": "list", "args": {}})
                assert len(list_resp["data"]["torrents"]) == 2

                writer1.close()
                writer2.close()

        _run(scenario())


class TestServerLifecycle:
    def test_stale_socket_file_is_replaced(self, tmp_path):
        async def scenario():
            socket_path = tmp_path / "tinytorrentd.sock"
            socket_path.write_text("stale")  # simulate leftover file from a crashed run
            manager = DaemonManager(tmp_path / "downloads", tmp_path / "state.json", generate_peer_id())
            server = IPCServer(manager, socket_path)
            await server.start()
            try:
                reader, writer = await asyncio.open_unix_connection(path=str(socket_path))
                resp = await _send(reader, writer, {"cmd": "list", "args": {}})
                assert resp["ok"] is True
                writer.close()
            finally:
                await server.stop()

        _run(scenario())

    def test_socket_removed_after_stop(self, tmp_path):
        async def scenario():
            socket_path = tmp_path / "tinytorrentd.sock"
            manager = DaemonManager(tmp_path / "downloads", tmp_path / "state.json", generate_peer_id())
            server = IPCServer(manager, socket_path)
            await server.start()
            assert socket_path.exists()
            await server.stop()
            assert not socket_path.exists()

        _run(scenario())


def _add_pausable(fx, reader, writer):
    """Add a torrent and put it back in a pausable state.

    The tracker-less magnet these tests use fails metadata acquisition
    the moment its download task gets a slice of the event loop, which
    an IPC round trip always gives it -- so by the time a second command
    arrives the torrent is already in the terminal ERROR state, where
    pausing is correctly a no-op. Resetting the status keeps these tests
    about the pause/resume command plumbing; the scheduler's own pause
    semantics are covered in test_scheduler.py.
    """

    async def add():
        added = await _send(reader, writer, {"cmd": "add", "args": {"magnet": _no_tracker_magnet()}})
        torrent_id = added["data"]["id"]
        fx.manager.get_torrent(torrent_id).status = TorrentStatus.QUEUED
        return torrent_id

    return add()


class TestPauseAndResumeCommands:
    def test_pause_then_resume(self, tmp_path):
        async def scenario():
            async with _ServerFixture(tmp_path) as fx:
                reader, writer = await fx.connect()
                try:
                    torrent_id = await _add_pausable(fx, reader, writer)

                    resp = await _send(reader, writer, {"cmd": "pause", "args": {"id": torrent_id}})
                    assert resp["ok"] is True
                    assert fx.manager.get_torrent(torrent_id).status == TorrentStatus.PAUSED

                    resp = await _send(reader, writer, {"cmd": "resume", "args": {"id": torrent_id}})
                    assert resp["ok"] is True
                    assert fx.manager.get_torrent(torrent_id).status != TorrentStatus.PAUSED
                finally:
                    writer.close()

        _run(scenario())

    def test_pause_shows_up_in_list_status(self, tmp_path):
        async def scenario():
            async with _ServerFixture(tmp_path) as fx:
                reader, writer = await fx.connect()
                try:
                    torrent_id = await _add_pausable(fx, reader, writer)
                    await _send(reader, writer, {"cmd": "pause", "args": {"id": torrent_id}})
                    listed = await _send(reader, writer, {"cmd": "list", "args": {}})
                    assert listed["data"]["torrents"][0]["status"] == "paused"
                finally:
                    writer.close()

        _run(scenario())

    def test_pause_is_persisted_to_the_state_file(self, tmp_path):
        async def scenario():
            async with _ServerFixture(tmp_path) as fx:
                reader, writer = await fx.connect()
                try:
                    torrent_id = await _add_pausable(fx, reader, writer)
                    await _send(reader, writer, {"cmd": "pause", "args": {"id": torrent_id}})
                finally:
                    writer.close()

        _run(scenario())
        payload = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
        assert payload["torrents"][0]["paused"] is True

    def test_pause_requires_an_id(self, tmp_path):
        async def scenario():
            async with _ServerFixture(tmp_path) as fx:
                reader, writer = await fx.connect()
                try:
                    resp = await _send(reader, writer, {"cmd": "pause", "args": {}})
                    assert resp["ok"] is False
                    assert "id" in resp["error"]
                finally:
                    writer.close()

        _run(scenario())

    def test_resume_requires_an_id(self, tmp_path):
        async def scenario():
            async with _ServerFixture(tmp_path) as fx:
                reader, writer = await fx.connect()
                try:
                    resp = await _send(reader, writer, {"cmd": "resume", "args": {}})
                    assert resp["ok"] is False
                finally:
                    writer.close()

        _run(scenario())

    def test_pause_unknown_id_reports_an_error(self, tmp_path):
        async def scenario():
            async with _ServerFixture(tmp_path) as fx:
                reader, writer = await fx.connect()
                try:
                    resp = await _send(reader, writer, {"cmd": "pause", "args": {"id": "zzzz"}})
                    assert resp["ok"] is False
                    assert "zzzz" in resp["error"]
                finally:
                    writer.close()

        _run(scenario())

    def test_resume_unknown_id_reports_an_error(self, tmp_path):
        async def scenario():
            async with _ServerFixture(tmp_path) as fx:
                reader, writer = await fx.connect()
                try:
                    resp = await _send(reader, writer, {"cmd": "resume", "args": {"id": "zzzz"}})
                    assert resp["ok"] is False
                finally:
                    writer.close()

        _run(scenario())


class TestTestCommand:
    def test_reports_no_hooks_configured(self, tmp_path):
        async def scenario():
            async with _ServerFixture(tmp_path) as fx:
                reader, writer = await fx.connect()
                try:
                    added = await _send(
                        reader, writer, {"cmd": "add", "args": {"magnet": _no_tracker_magnet()}}
                    )
                    resp = await _send(
                        reader,
                        writer,
                        {"cmd": "test", "args": {"event": "download_completed", "id": added["data"]["id"]}},
                    )
                    assert resp["ok"] is True
                    assert resp["data"]["configured"] is False
                    assert resp["data"]["results"] == []
                finally:
                    writer.close()

        _run(scenario())

    def test_runs_configured_commands(self, tmp_path):
        marker = tmp_path / "ran"

        async def scenario():
            async with _ServerFixture(tmp_path) as fx:
                # The fixture builds a hook-less manager; give it a
                # configured runner so the command has something to run.
                fx.manager.hook_runner = HookRunner(
                    {
                        HookEvent.DOWNLOAD_COMPLETED: (
                            HookCommand(
                                argv=(sys.executable, "-c", f"open({str(marker)!r}, 'w').write('x')"),
                                timeout_seconds=30.0,
                            ),
                        )
                    }
                )
                reader, writer = await fx.connect()
                try:
                    added = await _send(
                        reader, writer, {"cmd": "add", "args": {"magnet": _no_tracker_magnet()}}
                    )
                    resp = await _send(
                        reader,
                        writer,
                        {"cmd": "test", "args": {"event": "download_completed", "id": added["data"]["id"]}},
                    )
                    assert resp["ok"] is True
                    assert resp["data"]["configured"] is True
                    assert resp["data"]["results"][0]["outcome"] == "ok"
                finally:
                    writer.close()

        _run(scenario())
        assert marker.exists()

    def test_rejects_an_unknown_event(self, tmp_path):
        async def scenario():
            async with _ServerFixture(tmp_path) as fx:
                reader, writer = await fx.connect()
                try:
                    added = await _send(
                        reader, writer, {"cmd": "add", "args": {"magnet": _no_tracker_magnet()}}
                    )
                    resp = await _send(
                        reader,
                        writer,
                        {"cmd": "test", "args": {"event": "download_finished", "id": added["data"]["id"]}},
                    )
                    assert resp["ok"] is False
                    assert "download_finished" in resp["error"]
                finally:
                    writer.close()

        _run(scenario())

    def test_requires_an_event(self, tmp_path):
        async def scenario():
            async with _ServerFixture(tmp_path) as fx:
                reader, writer = await fx.connect()
                try:
                    resp = await _send(reader, writer, {"cmd": "test", "args": {"id": "a3f9"}})
                    assert resp["ok"] is False
                    assert "event" in resp["error"]
                finally:
                    writer.close()

        _run(scenario())

    def test_unknown_torrent_reports_an_error(self, tmp_path):
        async def scenario():
            async with _ServerFixture(tmp_path) as fx:
                reader, writer = await fx.connect()
                try:
                    resp = await _send(
                        reader, writer, {"cmd": "test", "args": {"event": "download_completed", "id": "zzzz"}}
                    )
                    assert resp["ok"] is False
                finally:
                    writer.close()

        _run(scenario())

    def test_neither_id_nor_name_reports_an_error(self, tmp_path):
        async def scenario():
            async with _ServerFixture(tmp_path) as fx:
                reader, writer = await fx.connect()
                try:
                    resp = await _send(
                        reader, writer, {"cmd": "test", "args": {"event": "download_completed"}}
                    )
                    assert resp["ok"] is False
                finally:
                    writer.close()

        _run(scenario())


class TestDaemonEventTestCommand:
    def test_daemon_started_needs_no_torrent(self, tmp_path):
        marker = tmp_path / "ran"

        async def scenario():
            async with _ServerFixture(tmp_path) as fx:
                fx.manager.hook_runner = HookRunner(
                    {
                        HookEvent.DAEMON_STARTED: (
                            HookCommand(
                                argv=(sys.executable, "-c", f"open({str(marker)!r}, 'w').write('x')"),
                                timeout_seconds=30.0,
                            ),
                        )
                    }
                )
                reader, writer = await fx.connect()
                try:
                    resp = await _send(
                        reader, writer, {"cmd": "test", "args": {"event": "daemon_started"}}
                    )
                    assert resp["ok"] is True
                    assert resp["data"]["torrent_id"] is None
                    assert resp["data"]["torrent_name"] is None
                    assert resp["data"]["results"][0]["outcome"] == "ok"
                finally:
                    writer.close()

        _run(scenario())
        assert marker.exists()

    def test_daemon_stopping_needs_no_torrent(self, tmp_path):
        async def scenario():
            async with _ServerFixture(tmp_path) as fx:
                reader, writer = await fx.connect()
                try:
                    resp = await _send(
                        reader, writer, {"cmd": "test", "args": {"event": "daemon_stopping"}}
                    )
                    assert resp["ok"] is True
                    assert resp["data"]["configured"] is False
                finally:
                    writer.close()

        _run(scenario())

    def test_daemon_event_reports_the_configured_interval(self, tmp_path):
        async def scenario():
            async with _ServerFixture(tmp_path) as fx:
                fx.manager.hook_runner = HookRunner(
                    {
                        HookEvent.DAEMON_STARTED: (
                            HookCommand(
                                argv=(sys.executable, "-c", "pass"),
                                timeout_seconds=30.0,
                                interval_seconds=60.0,
                            ),
                        )
                    }
                )
                reader, writer = await fx.connect()
                try:
                    resp = await _send(
                        reader, writer, {"cmd": "test", "args": {"event": "daemon_started"}}
                    )
                    assert resp["data"]["results"][0]["interval_seconds"] == 60.0
                finally:
                    writer.close()

        _run(scenario())
