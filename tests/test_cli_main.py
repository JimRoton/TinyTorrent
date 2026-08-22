import asyncio
import threading

import pytest

from tinytorrent.cli import main as cli_main
from tinytorrent.common.ids import generate_peer_id
from tinytorrent.common.ipc_protocol import Response
from tinytorrent.daemon.ipc_server import IPCServer
from tinytorrent.daemon.manager import DaemonManager


class TestArgParsing:
    def test_add_defaults_to_normal_priority(self):
        args = cli_main.build_parser().parse_args(["add", "magnet:?xt=urn:btih:aa"])
        assert args.command == "add"
        assert args.priority == "normal"

    def test_add_explicit_priority(self):
        args = cli_main.build_parser().parse_args(["add", "magnet:?xt=urn:btih:aa", "--priority", "high"])
        assert args.priority == "high"

    def test_purge_with_data_flag(self):
        args = cli_main.build_parser().parse_args(["purge", "a3f9", "--with-data"])
        assert args.id == "a3f9"
        assert args.with_data is True

    def test_purge_without_flag_defaults_false(self):
        args = cli_main.build_parser().parse_args(["purge", "a3f9"])
        assert args.with_data is False

    def test_priority_requires_valid_level(self):
        parser = cli_main.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["priority", "a3f9", "urgent"])

    def test_list_takes_no_extra_args(self):
        args = cli_main.build_parser().parse_args(["list"])
        assert args.command == "list"

    def test_promote(self):
        args = cli_main.build_parser().parse_args(["promote", "a3f9"])
        assert args.id == "a3f9"

    def test_missing_command_errors(self):
        with pytest.raises(SystemExit):
            cli_main.build_parser().parse_args([])


class TestCommandHandlers:
    """Each handler's request-building and output, with ipc_client.call stubbed out."""

    def test_add_success(self, monkeypatch, capsys):
        captured = {}

        def fake_call(socket_path, cmd, args=None, timeout=15.0):
            captured["cmd"] = cmd
            captured["args"] = args
            return Response.success({"id": "a3f9"})

        monkeypatch.setattr(cli_main.ipc_client, "call", fake_call)
        code = cli_main.main(["add", "magnet:?xt=urn:btih:" + "00" * 20, "--priority", "high"])
        assert code == 0
        assert captured["cmd"] == "add"
        assert captured["args"]["priority"] == "high"
        assert "a3f9" in capsys.readouterr().out

    def test_add_failure_exit_code(self, monkeypatch, capsys):
        monkeypatch.setattr(cli_main.ipc_client, "call", lambda *a, **k: Response.failure("bad magnet"))
        code = cli_main.main(["add", "whatever"])
        assert code == 1
        assert "bad magnet" in capsys.readouterr().err

    def test_purge_passes_with_data_flag(self, monkeypatch):
        captured = {}

        def fake_call(socket_path, cmd, args=None, timeout=15.0):
            captured["args"] = args
            return Response.success()

        monkeypatch.setattr(cli_main.ipc_client, "call", fake_call)
        cli_main.main(["purge", "a3f9", "--with-data"])
        assert captured["args"] == {"id": "a3f9", "with_data": True}

    def test_list_renders_table(self, monkeypatch, capsys):
        torrents = [
            {
                "id": "a3f9",
                "name": "file.iso",
                "status": "downloading",
                "priority": "normal",
                "bytes_downloaded": 10,
                "total_length": 100,
                "download_rate_bps": 10,
                "eta_seconds": 9,
            }
        ]
        monkeypatch.setattr(cli_main.ipc_client, "call", lambda *a, **k: Response.success({"torrents": torrents}))
        code = cli_main.main(["list"])
        assert code == 0
        out = capsys.readouterr().out
        assert "a3f9" in out
        assert "file.iso" in out

    def test_priority_command(self, monkeypatch):
        captured = {}

        def fake_call(socket_path, cmd, args=None, timeout=15.0):
            captured["cmd"], captured["args"] = cmd, args
            return Response.success()

        monkeypatch.setattr(cli_main.ipc_client, "call", fake_call)
        code = cli_main.main(["priority", "a3f9", "low"])
        assert code == 0
        assert captured["cmd"] == "priority"
        assert captured["args"] == {"id": "a3f9", "priority": "low"}

    def test_promote_command(self, monkeypatch):
        captured = {}

        def fake_call(socket_path, cmd, args=None, timeout=15.0):
            captured["cmd"], captured["args"] = cmd, args
            return Response.success()

        monkeypatch.setattr(cli_main.ipc_client, "call", fake_call)
        code = cli_main.main(["promote", "a3f9"])
        assert code == 0
        assert captured["cmd"] == "promote"
        assert captured["args"] == {"id": "a3f9"}

    def test_daemon_unreachable_reports_friendly_error(self, monkeypatch, capsys):
        def raise_unreachable(*a, **k):
            raise cli_main.DaemonUnreachableError("no socket")

        monkeypatch.setattr(cli_main.ipc_client, "call", raise_unreachable)
        code = cli_main.main(["list"])
        assert code == 1
        err = capsys.readouterr().err
        assert "no socket" in err
        assert "tinytorrentd" in err


class _BackgroundDaemon:
    """Runs a real IPCServer/DaemonManager on its own event loop in a
    background thread, so the (synchronous, one-`asyncio.run()`-per-call)
    CLI can connect to it like it would a real running tinytorrentd —
    each `cli_main.main()` call spins up and tears down its own event
    loop, which only works if the server lives on a loop that keeps
    running independently of any single call.
    """

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.socket_path = tmp_path / "tinytorrentd.sock"
        self.manager = None
        self.loop = None
        self.server = None
        self.thread = None

    def start(self) -> None:
        ready = threading.Event()

        def run():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)

            async def setup():
                self.manager = DaemonManager(
                    self.tmp_path / "downloads", self.tmp_path / "state.json", generate_peer_id()
                )
                self.server = IPCServer(self.manager, self.socket_path)
                await self.server.start()

            self.loop.run_until_complete(setup())
            ready.set()
            self.loop.run_forever()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        assert ready.wait(timeout=5), "background daemon failed to start"

    def stop(self) -> None:
        fut = asyncio.run_coroutine_threadsafe(self.server.stop(), self.loop)
        fut.result(timeout=5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)


class TestEndToEnd:
    def test_add_list_purge_through_real_daemon(self, tmp_path):
        daemon = _BackgroundDaemon(tmp_path)
        daemon.start()
        try:
            magnet = "magnet:?xt=urn:btih:" + "00" * 20 + "&dn=demo"
            socket_arg = ["--socket", str(daemon.socket_path)]

            code = cli_main.main([*socket_arg, "add", magnet])
            assert code == 0

            sessions = daemon.manager.list_torrents()
            assert len(sessions) == 1
            torrent_id = sessions[0].torrent_id

            code = cli_main.main([*socket_arg, "priority", torrent_id, "high"])
            assert code == 0
            assert daemon.manager.get_torrent(torrent_id).priority.value == "high"

            code = cli_main.main([*socket_arg, "purge", torrent_id])
            assert code == 0
            assert daemon.manager.list_torrents() == []
        finally:
            daemon.stop()
