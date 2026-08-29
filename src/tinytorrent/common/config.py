"""Shared configuration for both `tinytorrentd` and `tinytorrent`.

Every value has a built-in default, can be overridden in a JSON config
file, and can be overridden again per-invocation via CLI flags — flags
win over the config file, which wins over the built-in default.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

from tinytorrent.common.hooks import HookCommand, HookConfigError, HookEvent, parse_hooks_config

_XDG_CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
_XDG_STATE_HOME = Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser()
_XDG_RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")).expanduser()

DEFAULT_CONFIG_PATH = _XDG_CONFIG_HOME / "tinytorrent" / "config.json"
DEFAULT_DOWNLOAD_DIR = Path("~/Downloads/tinytorrent").expanduser()
DEFAULT_STATE_FILE = _XDG_STATE_HOME / "tinytorrent" / "state.json"
DEFAULT_SOCKET_PATH = _XDG_RUNTIME_DIR / "tinytorrentd.sock"
DEFAULT_MAX_ACTIVE = 4
# How long the CLI waits for tinytorrentd to respond to one IPC request
# (connect + read the response line) before giving up. Distinct from a
# hook command's own `timeout_seconds` in the `hooks` config -- this one
# governs the CLI<->daemon socket round trip, not a hook subprocess.
DEFAULT_IPC_TIMEOUT_SECONDS = 15.0

_PATH_FIELDS = {"download_dir", "state_file", "socket_path"}


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    download_dir: Path = DEFAULT_DOWNLOAD_DIR
    state_file: Path = DEFAULT_STATE_FILE
    socket_path: Path = DEFAULT_SOCKET_PATH
    max_active: int = DEFAULT_MAX_ACTIVE
    ipc_timeout_seconds: float = DEFAULT_IPC_TIMEOUT_SECONDS
    # Event hooks (see DESIGN.md): config-file only, no CLI flag
    # equivalent -- a list of argv commands per event doesn't map cleanly
    # onto flags the way the scalar settings above do.
    hooks: "dict[HookEvent, tuple[HookCommand, ...]]" = field(default_factory=dict)

    @staticmethod
    def load(config_path: "Path | None" = None) -> "Config":
        """Load defaults overlaid with a JSON config file, if present."""
        config = Config()
        path = Path(config_path).expanduser() if config_path is not None else DEFAULT_CONFIG_PATH
        if not path.exists():
            return config
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"could not read config file {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"config file {path} must contain a JSON object")
        return _apply(config, raw)

    def with_overrides(self, **overrides) -> "Config":
        """Apply CLI-flag overrides. Keys with a ``None`` value are treated as 'not set'."""
        real_overrides = {k: v for k, v in overrides.items() if v is not None}
        return _apply(self, real_overrides)


def _apply(config: Config, raw: dict) -> Config:
    valid_fields = {f.name for f in fields(Config)}
    unknown = set(raw) - valid_fields
    if unknown:
        raise ConfigError(f"unknown config key(s): {', '.join(sorted(unknown))}")

    updates: dict = {}
    for key, value in raw.items():
        if key in _PATH_FIELDS:
            updates[key] = Path(value).expanduser()
        elif key == "max_active":
            try:
                max_active = int(value)
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"'max_active' must be an integer, got {value!r}") from exc
            if max_active < 1:
                raise ConfigError("'max_active' must be at least 1")
            updates[key] = max_active
        elif key == "ipc_timeout_seconds":
            try:
                ipc_timeout_seconds = float(value)
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"'ipc_timeout_seconds' must be a number, got {value!r}") from exc
            if ipc_timeout_seconds <= 0:
                raise ConfigError("'ipc_timeout_seconds' must be greater than 0")
            updates[key] = ipc_timeout_seconds
        elif key == "hooks":
            try:
                updates[key] = parse_hooks_config(value)
            except HookConfigError as exc:
                raise ConfigError(str(exc)) from exc
        else:
            updates[key] = value
    return replace(config, **updates)
