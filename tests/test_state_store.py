import json

import pytest

from tinytorrent.common.priority import Priority
from tinytorrent.daemon.state_store import StateStoreError, TorrentRecord, load, save


class TestLoadMissingFile:
    def test_returns_empty_list(self, tmp_path):
        assert load(tmp_path / "does-not-exist.json") == []


class TestSaveAndLoadRoundTrip:
    def test_single_record_without_metadata(self, tmp_path):
        path = tmp_path / "state.json"
        records = [TorrentRecord("a3f9", "magnet:?xt=urn:btih:" + "00" * 20, Priority.HIGH)]
        save(path, records)
        loaded = load(path)
        assert loaded == records

    def test_record_with_info_dict(self, tmp_path):
        path = tmp_path / "state.json"
        info_dict = {b"name": b"file.txt", b"piece length": 16384, b"pieces": b"x" * 20, b"length": 100}
        records = [
            TorrentRecord("a3f9", "magnet:?xt=urn:btih:" + "00" * 20, Priority.LOW, info_dict=info_dict)
        ]
        save(path, records)
        loaded = load(path)
        assert loaded[0].info_dict == info_dict

    def test_multiple_records_preserve_order(self, tmp_path):
        path = tmp_path / "state.json"
        records = [
            TorrentRecord("aaaa", "magnet:?xt=urn:btih:" + "11" * 20, Priority.HIGH),
            TorrentRecord("bbbb", "magnet:?xt=urn:btih:" + "22" * 20, Priority.NORMAL),
            TorrentRecord("cccc", "magnet:?xt=urn:btih:" + "33" * 20, Priority.LOW),
        ]
        save(path, records)
        loaded = load(path)
        assert [r.torrent_id for r in loaded] == ["aaaa", "bbbb", "cccc"]

    def test_empty_list_round_trips(self, tmp_path):
        path = tmp_path / "state.json"
        save(path, [])
        assert load(path) == []


class TestAtomicSave:
    def test_no_leftover_tmp_file(self, tmp_path):
        path = tmp_path / "state.json"
        save(path, [TorrentRecord("aaaa", "magnet:?xt=urn:btih:" + "11" * 20, Priority.NORMAL)])
        assert path.exists()
        assert not (tmp_path / "state.json.tmp").exists()

    def test_creates_parent_directories(self, tmp_path):
        path = tmp_path / "nested" / "dir" / "state.json"
        save(path, [])
        assert path.exists()

    def test_overwrites_existing_file(self, tmp_path):
        path = tmp_path / "state.json"
        save(path, [TorrentRecord("aaaa", "magnet:?xt=urn:btih:" + "11" * 20, Priority.NORMAL)])
        save(path, [TorrentRecord("bbbb", "magnet:?xt=urn:btih:" + "22" * 20, Priority.LOW)])
        loaded = load(path)
        assert [r.torrent_id for r in loaded] == ["bbbb"]


class TestMalformedInput:
    def test_not_a_dict(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(StateStoreError):
            load(path)

    def test_missing_torrents_key(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text(json.dumps({"version": 1}), encoding="utf-8")
        with pytest.raises(StateStoreError):
            load(path)

    def test_invalid_json(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(StateStoreError):
            load(path)

    def test_record_missing_id(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text(json.dumps({"version": 1, "torrents": [{"magnet": "x", "priority": "high"}]}), encoding="utf-8")
        with pytest.raises(StateStoreError):
            load(path)

    def test_record_bad_priority(self, tmp_path):
        path = tmp_path / "state.json"
        entry = {"id": "aaaa", "magnet": "x", "priority": "urgent"}
        path.write_text(json.dumps({"version": 1, "torrents": [entry]}), encoding="utf-8")
        with pytest.raises(StateStoreError):
            load(path)

    def test_record_bad_info_dict_b64(self, tmp_path):
        path = tmp_path / "state.json"
        entry = {"id": "aaaa", "magnet": "x", "priority": "high", "info_dict_b64": "not-valid-base64!!"}
        path.write_text(json.dumps({"version": 1, "torrents": [entry]}), encoding="utf-8")
        with pytest.raises(StateStoreError):
            load(path)


class TestPausedField:
    def test_defaults_to_false(self):
        record = TorrentRecord("a3f9", "magnet:?xt=urn:btih:" + "00" * 20, Priority.NORMAL)
        assert record.paused is False

    def test_round_trips_true(self, tmp_path):
        path = tmp_path / "state.json"
        records = [
            TorrentRecord("a3f9", "magnet:?xt=urn:btih:" + "00" * 20, Priority.NORMAL, paused=True)
        ]
        save(path, records)
        assert load(path)[0].paused is True

    def test_round_trips_false(self, tmp_path):
        path = tmp_path / "state.json"
        records = [
            TorrentRecord("a3f9", "magnet:?xt=urn:btih:" + "00" * 20, Priority.NORMAL, paused=False)
        ]
        save(path, records)
        assert load(path)[0].paused is False

    def test_written_to_the_json_payload(self, tmp_path):
        path = tmp_path / "state.json"
        save(path, [TorrentRecord("a3f9", "magnet:?xt=urn:btih:" + "00" * 20, Priority.LOW, paused=True)])
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["torrents"][0]["paused"] is True

    def test_absent_in_older_state_files_defaults_to_false(self, tmp_path):
        # State files written before pause existed have no "paused" key.
        path = tmp_path / "state.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "torrents": [
                        {"id": "a3f9", "magnet": "magnet:?xt=urn:btih:" + "00" * 20, "priority": "normal"}
                    ],
                }
            ),
            encoding="utf-8",
        )
        assert load(path)[0].paused is False

    def test_paused_survives_alongside_an_info_dict(self, tmp_path):
        path = tmp_path / "state.json"
        info_dict = {b"name": b"file.txt", b"piece length": 16384, b"pieces": b"x" * 20, b"length": 100}
        save(
            path,
            [
                TorrentRecord(
                    "a3f9",
                    "magnet:?xt=urn:btih:" + "00" * 20,
                    Priority.HIGH,
                    info_dict=info_dict,
                    paused=True,
                )
            ],
        )
        loaded = load(path)[0]
        assert loaded.paused is True
        assert loaded.info_dict == info_dict
