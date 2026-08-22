import asyncio
import json

import pytest

from tinytorrent.common.ids import generate_peer_id
from tinytorrent.daemon.ipc_server import IPCServer
from tinytorrent.daemon.manager import DaemonManager


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
