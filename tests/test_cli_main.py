import asyncio
import threading

import pytest

from tinytorrent.cli import main as cli_main
from tinytorrent.common.ids import generate_peer_id
from tinytorrent.common.ipc_protocol import Response
from tinytorrent.daemon.ipc_server import IPCServer
from tinytorrent.daemon.manager import DaemonManager
from tinytorrent.daemon.torrent_session import TorrentStatus


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


class TestPauseResumeAndTestArgParsing:
    def test_pause(self):
        args = cli_main.build_parser().parse_args(["pause", "a3f9"])
        assert args.command == "pause"
        assert args.id == "a3f9"

    def test_resume(self):
        args = cli_main.build_parser().parse_args(["resume", "a3f9"])
        assert args.command == "resume"
        assert args.id == "a3f9"

    def test_test_requires_an_event(self):
        with pytest.raises(SystemExit):
            cli_main.build_parser().parse_args(["test", "--id", "a3f9"])

    def test_test_rejects_an_unknown_event(self):
        with pytest.raises(SystemExit):
            cli_main.build_parser().parse_args(["test", "--event", "download_finished", "--id", "a3f9"])

    def test_test_accepts_every_documented_event(self):
        parser = cli_main.build_parser()
        for event in (
            "metadata_fetched",
            "download_started",
            "download_completed",
            "download_error",
            "torrent_purged",
        ):
            args = parser.parse_args(["test", "--event", event, "--id", "a3f9"])
            assert args.event == event

    def test_test_with_name(self):
        args = cli_main.build_parser().parse_args(
            ["test", "--event", "download_completed", "--name", "ubuntu.iso"]
        )
        assert args.name == "ubuntu.iso"
        assert args.id is None

    def test_deleted_data_defaults_false(self):
        args = cli_main.build_parser().parse_args(["test", "--event", "torrent_purged", "--id", "a3f9"])
        assert args.deleted_data is False

    def test_deleted_data_flag(self):
        args = cli_main.build_parser().parse_args(
            ["test", "--event", "torrent_purged", "--id", "a3f9", "--deleted-data"]
        )
        assert args.deleted_data is True

    def test_timeout_flag_parses_as_float(self):
        args = cli_main.build_parser().parse_args(["--timeout", "42.5", "list"])
        assert args.ipc_timeout_seconds == 42.5

    def test_timeout_defaults_to_none(self):
        args = cli_main.build_parser().parse_args(["list"])
        assert args.ipc_timeout_seconds is None


class TestPauseResumeAndTestHandlers:
    def test_pause_success(self, monkeypatch, capsys):
        captured = {}

        def fake_call(socket_path, cmd, args=None, timeout=15.0):
            captured["cmd"] = cmd
            captured["args"] = args
            return Response.success()

        monkeypatch.setattr(cli_main.ipc_client, "call", fake_call)
        assert cli_main.main(["pause", "a3f9"]) == 0
        assert captured["cmd"] == "pause"
        assert captured["args"] == {"id": "a3f9"}
        assert "paused a3f9" in capsys.readouterr().out

    def test_pause_failure_returns_one(self, monkeypatch, capsys):
        monkeypatch.setattr(
            cli_main.ipc_client, "call", lambda *a, **kw: Response.failure("unknown torrent id 'a3f9'")
        )
        assert cli_main.main(["pause", "a3f9"]) == 1
        assert "unknown torrent id" in capsys.readouterr().err

    def test_resume_success(self, monkeypatch, capsys):
        captured = {}

        def fake_call(socket_path, cmd, args=None, timeout=15.0):
            captured["cmd"] = cmd
            return Response.success()

        monkeypatch.setattr(cli_main.ipc_client, "call", fake_call)
        assert cli_main.main(["resume", "a3f9"]) == 0
        assert captured["cmd"] == "resume"
        assert "resumed a3f9" in capsys.readouterr().out

    def test_test_requires_id_or_name_before_contacting_the_daemon(self, monkeypatch, capsys):
        def explode(*a, **kw):
            raise AssertionError("the daemon should not have been contacted")

        monkeypatch.setattr(cli_main.ipc_client, "call", explode)
        assert cli_main.main(["test", "--event", "download_completed"]) == 1
        assert "--id or --name" in capsys.readouterr().err

    def test_test_sends_every_argument(self, monkeypatch, capsys):
        captured = {}

        def fake_call(socket_path, cmd, args=None, timeout=15.0):
            captured["cmd"] = cmd
            captured["args"] = args
            return Response.success(
                {"torrent_id": "a3f9", "torrent_name": "demo", "configured": False, "results": []}
            )

        monkeypatch.setattr(cli_main.ipc_client, "call", fake_call)
        code = cli_main.main(
            ["test", "--event", "torrent_purged", "--id", "a3f9", "--deleted-data"]
        )
        assert code == 0
        assert captured["cmd"] == "test"
        assert captured["args"] == {
            "event": "torrent_purged",
            "id": "a3f9",
            "name": None,
            "deleted_data": True,
        }

    def test_test_prints_results(self, monkeypatch, capsys):
        def fake_call(socket_path, cmd, args=None, timeout=15.0):
            return Response.success(
                {
                    "torrent_id": "a3f9",
                    "torrent_name": "ubuntu.iso",
                    "configured": True,
                    "results": [
                        {
                            "argv": ["cp", "/dl/ubuntu.iso", "/done"],
                            "outcome": "ok",
                            "returncode": 0,
                            "output": "",
                            "on_failure": "ignore",
                            "would_run_in_production": True,
                        }
                    ],
                }
            )

        monkeypatch.setattr(cli_main.ipc_client, "call", fake_call)
        assert cli_main.main(["test", "--event", "download_completed", "--id", "a3f9"]) == 0
        out = capsys.readouterr().out
        assert "ubuntu.iso" in out
        assert "cp /dl/ubuntu.iso /done" in out

    def test_test_failure_returns_one(self, monkeypatch, capsys):
        monkeypatch.setattr(
            cli_main.ipc_client, "call", lambda *a, **kw: Response.failure("no torrent with id 'zzzz'")
        )
        assert cli_main.main(["test", "--event", "download_completed", "--id", "zzzz"]) == 1
        assert "no torrent with id" in capsys.readouterr().err


class TestIpcTimeoutThreading:
    def test_default_timeout_is_passed_through(self, monkeypatch):
        captured = {}

        def fake_call(socket_path, cmd, args=None, timeout=None):
            captured["timeout"] = timeout
            return Response.success({"torrents": []})

        monkeypatch.setattr(cli_main.ipc_client, "call", fake_call)
        cli_main.main(["list"])
        assert captured["timeout"] == 15.0

    def test_timeout_flag_overrides_it(self, monkeypatch):
        captured = {}

        def fake_call(socket_path, cmd, args=None, timeout=None):
            captured["timeout"] = timeout
            return Response.success({"torrents": []})

        monkeypatch.setattr(cli_main.ipc_client, "call", fake_call)
        cli_main.main(["--timeout", "42", "list"])
        assert captured["timeout"] == 42.0

    def test_timeout_applies_to_every_command(self, monkeypatch):
        seen = []

        def fake_call(socket_path, cmd, args=None, timeout=None):
            seen.append((cmd, timeout))
            return Response.success({"id": "a3f9", "torrents": []})

        monkeypatch.setattr(cli_main.ipc_client, "call", fake_call)
        for argv in (
            ["add", "magnet:?xt=urn:btih:aa"],
            ["list"],
            ["priority", "a3f9", "high"],
            ["promote", "a3f9"],
            ["pause", "a3f9"],
            ["resume", "a3f9"],
            ["purge", "a3f9"],
        ):
            cli_main.main(["--timeout", "7", *argv])
        assert {timeout for _cmd, timeout in seen} == {7.0}


class TestPauseResumeEndToEnd:
    def test_pause_and_resume_through_real_daemon(self, tmp_path):
        daemon = _BackgroundDaemon(tmp_path)
        daemon.start()
        try:
            magnet = "magnet:?xt=urn:btih:" + "00" * 20 + "&dn=demo"
            socket_arg = ["--socket", str(daemon.socket_path)]

            assert cli_main.main([*socket_arg, "add", magnet]) == 0
            session = daemon.manager.list_torrents()[0]
            torrent_id = session.torrent_id
            # The tracker-less magnet errors out as soon as its download
            # task runs, and pausing a terminal torrent is correctly a
            # no-op -- put it back in a pausable state so this exercises
            # the CLI-to-daemon plumbing rather than that.
            session.status = TorrentStatus.QUEUED

            assert cli_main.main([*socket_arg, "pause", torrent_id]) == 0
            assert daemon.manager.get_torrent(torrent_id).status.value == "paused"

            assert cli_main.main([*socket_arg, "resume", torrent_id]) == 0
            assert daemon.manager.get_torrent(torrent_id).status.value != "paused"
        finally:
            daemon.stop()

    def test_test_command_through_real_daemon(self, tmp_path):
        daemon = _BackgroundDaemon(tmp_path)
        daemon.start()
        try:
            magnet = "magnet:?xt=urn:btih:" + "00" * 20 + "&dn=demo"
            socket_arg = ["--socket", str(daemon.socket_path)]
            assert cli_main.main([*socket_arg, "add", magnet]) == 0
            torrent_id = daemon.manager.list_torrents()[0].torrent_id

            code = cli_main.main(
                [*socket_arg, "test", "--event", "download_completed", "--id", torrent_id]
            )
            assert code == 0
        finally:
            daemon.stop()
