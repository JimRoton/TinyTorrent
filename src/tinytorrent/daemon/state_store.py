"""Persists the daemon's torrent list to disk (JSON) so ``tinytorrentd``
can reload and resume after a systemd restart.

Only identity and configuration — magnet link, priority, and (once known)
the raw info dict — are persisted here. Download *progress* is
deliberately not: ``PieceStorage.scan_completed_pieces()`` re-derives
completed pieces by re-hashing what's actually on disk at startup, which
can't drift out of sync with reality the way a separate progress record
could (see DESIGN.md).
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path

from tinytorrent.common.priority import Priority
from tinytorrent.daemon.bencode import BencodeDecodeError, decode, encode

STATE_FILE_VERSION = 1


class StateStoreError(Exception):
    pass


@dataclass
class TorrentRecord:
    torrent_id: str
    magnet_uri: str
    priority: Priority
    info_dict: dict | None = None  # decoded bencode dict (bytes keys), once metadata is known


def load(path: Path) -> list[TorrentRecord]:
    """Load persisted torrent records. Returns ``[]`` if the file doesn't exist."""
    path = Path(path)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateStoreError(f"could not read state file {path}: {exc}") from exc

    if not isinstance(raw, dict) or not isinstance(raw.get("torrents"), list):
        raise StateStoreError(f"state file {path} has an unrecognized format")

    return [_record_from_json(entry) for entry in raw["torrents"]]


def save(path: Path, records: list[TorrentRecord]) -> None:
    """Atomically write ``records`` to ``path``."""
    path = Path(path)
    payload = {
        "version": STATE_FILE_VERSION,
        "torrents": [_record_to_json(r) for r in records],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp_path.replace(path)  # atomic rename on POSIX filesystems


def _record_to_json(record: TorrentRecord) -> dict:
    entry: dict = {
        "id": record.torrent_id,
        "magnet": record.magnet_uri,
        "priority": record.priority.value,
    }
    if record.info_dict is not None:
        entry["info_dict_b64"] = base64.b64encode(encode(record.info_dict)).decode("ascii")
    return entry


def _record_from_json(entry: dict) -> TorrentRecord:
    if not isinstance(entry, dict):
        raise StateStoreError(f"malformed torrent record: {entry!r}")
    try:
        torrent_id = entry["id"]
        magnet_uri = entry["magnet"]
        priority = Priority(entry["priority"])
    except (KeyError, ValueError) as exc:
        raise StateStoreError(f"malformed torrent record: {entry!r}") from exc

    info_dict = None
    b64_value = entry.get("info_dict_b64")
    if b64_value:
        try:
            decoded_bytes = base64.b64decode(b64_value, validate=True)
            info_dict = decode(decoded_bytes)
        except (BencodeDecodeError, ValueError) as exc:
            raise StateStoreError(f"malformed info_dict_b64 for torrent {torrent_id}: {exc}") from exc
        if not isinstance(info_dict, dict):
            raise StateStoreError(f"info_dict_b64 for torrent {torrent_id} did not decode to a dict")

    return TorrentRecord(torrent_id=torrent_id, magnet_uri=magnet_uri, priority=priority, info_dict=info_dict)
