import asyncio

import pytest

from tinytorrent.daemon.bencode import encode
from tinytorrent.daemon.tracker import http_tracker

INFO_HASH = bytes(range(20))
PEER_ID = bytes(range(20, 40))


class TestBuildQuery:
    def test_percent_encodes_binary_fields(self):
        weird_hash = b"\x00\x01\xff" + b"\x00" * 17
        query = http_tracker._build_query(
            info_hash=weird_hash,
            peer_id=weird_hash,
            port=6881,
            uploaded=0,
            downloaded=0,
            left=0,
            event=None,
            numwant=50,
        )
        assert "info_hash=%00%01%FF%00%00%00%00%00%00%00%00%00%00%00%00%00%00%00%00" in query
        assert "peer_id=%00%01%FF%00%00%00%00%00%00%00%00%00%00%00%00%00%00%00%00" in query

    def test_includes_required_fields(self):
        query = http_tracker._build_query(
            info_hash=INFO_HASH,
            peer_id=PEER_ID,
            port=6881,
            uploaded=10,
            downloaded=20,
            left=30,
            event=None,
            numwant=50,
        )
        assert "port=6881" in query
        assert "uploaded=10" in query
        assert "downloaded=20" in query
        assert "left=30" in query
        assert "compact=1" in query
        assert "event=" not in query

    def test_includes_event_when_given(self):
        query = http_tracker._build_query(
            info_hash=INFO_HASH,
            peer_id=PEER_ID,
            port=6881,
            uploaded=0,
            downloaded=0,
            left=0,
            event="started",
            numwant=50,
        )
        assert "event=started" in query

    def test_rejects_wrong_length_hashes(self):
        with pytest.raises(http_tracker.TrackerError):
            http_tracker._build_query(
                info_hash=b"tooshort",
                peer_id=PEER_ID,
                port=6881,
                uploaded=0,
                downloaded=0,
                left=0,
                event=None,
                numwant=50,
            )


class TestBuildUrl:
    def test_no_existing_query(self):
        assert http_tracker._build_url("http://tracker.example/announce", "a=1") == (
            "http://tracker.example/announce?a=1"
        )

    def test_existing_query(self):
        url = http_tracker._build_url("http://tracker.example/announce?passkey=xyz", "a=1")
        assert url == "http://tracker.example/announce?passkey=xyz&a=1"


class TestParsePeers:
    def test_compact_peers(self):
        data = bytes([203, 0, 113, 5]) + (51413).to_bytes(2, "big")
        peers = http_tracker._parse_compact_peers("t", data)
        assert peers == (("203.0.113.5", 51413),)

    def test_compact_peers_multiple(self):
        data = (
            bytes([203, 0, 113, 5]) + (1000).to_bytes(2, "big")
            + bytes([198, 51, 100, 7]) + (2000).to_bytes(2, "big")
        )
        peers = http_tracker._parse_compact_peers("t", data)
        assert peers == (("203.0.113.5", 1000), ("198.51.100.7", 2000))

    def test_compact_peers_bad_length(self):
        with pytest.raises(http_tracker.TrackerError):
            http_tracker._parse_compact_peers("t", b"\x00\x01\x02")

    def test_dict_peers(self):
        entries = [{b"ip": b"203.0.113.5", b"port": 1000}]
        peers = http_tracker._parse_dict_peers(entries)
        assert peers == (("203.0.113.5", 1000),)

    def test_dict_peers_skips_malformed_entries(self):
        entries = [{b"ip": b"203.0.113.5"}, {b"ip": b"1.2.3.4", b"port": 5}]
        peers = http_tracker._parse_dict_peers(entries)
        assert peers == (("1.2.3.4", 5),)


class TestParseResponse:
    def test_failure_reason_raises(self):
        body = encode({b"failure reason": b"nope"})
        with pytest.raises(http_tracker.TrackerError, match="nope"):
            http_tracker._parse_response("t", body)

    def test_not_a_dict(self):
        with pytest.raises(http_tracker.TrackerError):
            http_tracker._parse_response("t", encode([1, 2, 3]))

    def test_malformed_bencode(self):
        with pytest.raises(http_tracker.TrackerError):
            http_tracker._parse_response("t", b"not bencode")

    def test_default_interval(self):
        result = http_tracker._parse_response("t", encode({b"peers": b""}))
        assert result.interval == 1800
        assert result.peers == ()

    def test_full_response(self):
        peers_bytes = bytes([203, 0, 113, 5]) + (51413).to_bytes(2, "big")
        body = encode({b"interval": 900, b"peers": peers_bytes})
        result = http_tracker._parse_response("t", body)
        assert result.interval == 900
        assert result.peers == (("203.0.113.5", 51413),)


class TestAnnounceEndToEnd:
    def test_announce_uses_fetch_and_parses_result(self, monkeypatch):
        peers_bytes = bytes([203, 0, 113, 5]) + (51413).to_bytes(2, "big")
        body = encode({b"interval": 1200, b"peers": peers_bytes})

        captured_url = {}

        def fake_fetch(url, timeout):
            captured_url["url"] = url
            return body

        monkeypatch.setattr(http_tracker, "_fetch", fake_fetch)

        result = asyncio.run(
            http_tracker.announce(
                "http://tracker.example/announce",
                info_hash=INFO_HASH,
                peer_id=PEER_ID,
                port=6881,
                uploaded=0,
                downloaded=0,
                left=1000,
                event="started",
            )
        )

        assert result.interval == 1200
        assert result.peers == (("203.0.113.5", 51413),)
        assert captured_url["url"].startswith("http://tracker.example/announce?")
        assert "event=started" in captured_url["url"]
