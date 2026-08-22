from pathlib import Path

from tinytorrent.common.config import ConfigError
from tinytorrent.daemon import main as daemon_main


class TestParseArgs:
    def test_defaults_are_none(self):
        args = daemon_main._parse_args([])
        assert args.download_dir is None
        assert args.state_file is None
        assert args.socket_path is None
        assert args.max_active is None
        assert args.verbose is False

    def test_overrides_parsed(self):
        args = daemon_main._parse_args(
            [
                "--download-dir",
                "/tmp/dl",
                "--state-file",
                "/tmp/state.json",
                "--socket",
                "/tmp/x.sock",
                "--max-active",
                "8",
                "-v",
            ]
        )
        assert args.download_dir == Path("/tmp/dl")
        assert args.state_file == Path("/tmp/state.json")
        assert args.socket_path == Path("/tmp/x.sock")
        assert args.max_active == 8
        assert args.verbose is True


class TestMainConfigErrorHandling:
    def test_bad_config_file_returns_nonzero(self, tmp_path, monkeypatch):
        bad_config = tmp_path / "config.json"
        bad_config.write_text("{not json", encoding="utf-8")
        code = daemon_main.main(["--config", str(bad_config)])
        assert code == 1
