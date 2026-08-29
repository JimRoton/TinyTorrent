import json
from pathlib import Path

import pytest

from tinytorrent.common.config import Config, ConfigError
from tinytorrent.common.hooks import HookEvent


class TestLoadDefaults:
    def test_missing_file_returns_defaults(self, tmp_path):
        config = Config.load(tmp_path / "nope.json")
        assert config == Config()

    def test_none_uses_default_path(self, monkeypatch, tmp_path):
        # DEFAULT_CONFIG_PATH almost certainly doesn't exist in the test
        # sandbox's home dir, so this should just fall back to defaults
        # without raising.
        config = Config.load(None)
        assert isinstance(config, Config)


class TestLoadFromFile:
    def test_overrides_download_dir(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"download_dir": str(tmp_path / "dl")}), encoding="utf-8")
        config = Config.load(path)
        assert config.download_dir == tmp_path / "dl"

    def test_overrides_max_active(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"max_active": 8}), encoding="utf-8")
        config = Config.load(path)
        assert config.max_active == 8

    def test_expands_tilde_in_paths(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"download_dir": "~/mydownloads"}), encoding="utf-8")
        config = Config.load(path)
        assert config.download_dir == tmp_path / "mydownloads"

    def test_unknown_key_rejected(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"bogus_key": 1}), encoding="utf-8")
        with pytest.raises(ConfigError):
            Config.load(path)

    def test_invalid_max_active_type(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"max_active": "lots"}), encoding="utf-8")
        with pytest.raises(ConfigError):
            Config.load(path)

    def test_max_active_below_one_rejected(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"max_active": 0}), encoding="utf-8")
        with pytest.raises(ConfigError):
            Config.load(path)

    def test_malformed_json(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(ConfigError):
            Config.load(path)

    def test_not_a_json_object(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(ConfigError):
            Config.load(path)


class TestWithOverrides:
    def test_none_values_do_not_override(self):
        config = Config()
        overridden = config.with_overrides(download_dir=None, max_active=None)
        assert overridden == config

    def test_explicit_values_override(self, tmp_path):
        config = Config()
        overridden = config.with_overrides(socket_path=tmp_path / "custom.sock")
        assert overridden.socket_path == tmp_path / "custom.sock"
        assert overridden.download_dir == config.download_dir  # unaffected

    def test_cli_override_beats_file_override(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"max_active": 8}), encoding="utf-8")
        config = Config.load(path).with_overrides(max_active=2)
        assert config.max_active == 2


class TestIpcTimeout:
    def test_default(self):
        assert Config().ipc_timeout_seconds == 15.0

    def test_overrides_from_file(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"ipc_timeout_seconds": 90}), encoding="utf-8")
        assert Config.load(path).ipc_timeout_seconds == 90.0

    def test_accepts_a_float(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"ipc_timeout_seconds": 2.5}), encoding="utf-8")
        assert Config.load(path).ipc_timeout_seconds == 2.5

    def test_non_numeric_rejected(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"ipc_timeout_seconds": "soon"}), encoding="utf-8")
        with pytest.raises(ConfigError):
            Config.load(path)

    def test_zero_rejected(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"ipc_timeout_seconds": 0}), encoding="utf-8")
        with pytest.raises(ConfigError):
            Config.load(path)

    def test_negative_rejected(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"ipc_timeout_seconds": -5}), encoding="utf-8")
        with pytest.raises(ConfigError):
            Config.load(path)

    def test_cli_override_beats_file(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"ipc_timeout_seconds": 90}), encoding="utf-8")
        assert Config.load(path).with_overrides(ipc_timeout_seconds=3.0).ipc_timeout_seconds == 3.0


class TestHooksSection:
    def test_default_is_empty(self):
        assert Config().hooks == {}

    def test_parsed_from_file(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(
            json.dumps(
                {
                    "hooks": {
                        "download_completed": [
                            {"command": ["cp", "-r", "%download_dir%/%name%", "/done/"]}
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        config = Config.load(path)
        (command,) = config.hooks[HookEvent.DOWNLOAD_COMPLETED]
        assert command.argv == ("cp", "-r", "%download_dir%/%name%", "/done/")

    def test_coexists_with_other_settings(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(
            json.dumps(
                {"max_active": 2, "hooks": {"download_error": [{"command": ["notify-send", "x"]}]}}
            ),
            encoding="utf-8",
        )
        config = Config.load(path)
        assert config.max_active == 2
        assert HookEvent.DOWNLOAD_ERROR in config.hooks

    def test_unknown_event_surfaces_as_config_error(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(
            json.dumps({"hooks": {"download_finished": [{"command": ["x"]}]}}), encoding="utf-8"
        )
        with pytest.raises(ConfigError) as exc:
            Config.load(path)
        assert "download_finished" in str(exc.value)

    def test_malformed_command_surfaces_as_config_error(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(
            json.dumps({"hooks": {"download_completed": [{"command": "cp a b"}]}}), encoding="utf-8"
        )
        with pytest.raises(ConfigError):
            Config.load(path)

    def test_hooks_not_an_object_surfaces_as_config_error(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"hooks": ["download_completed"]}), encoding="utf-8")
        with pytest.raises(ConfigError):
            Config.load(path)
