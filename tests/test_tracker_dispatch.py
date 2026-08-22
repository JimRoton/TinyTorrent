import asyncio

import pytest

from tinytorrent.daemon.tracker import AnnounceResult, TrackerError, announce
from tinytorrent.daemon.tracker import http_tracker as http_tracker_module
from tinytorrent.daemon.tracker import udp_tracker as udp_tracker_module


def test_dispatches_http(monkeypatch):
    async def fake(url, **kwargs):
        return AnnounceResult(peers=(), interval=1)

    monkeypatch.setattr(http_tracker_module, "announce", fake)
    # Re-point the dispatcher's bound reference too.
    import tinytorrent.daemon.tracker as dispatch_module

    monkeypatch.setattr(dispatch_module, "_http_announce", fake)
    result = asyncio.run(announce("http://tracker.example/announce", info_hash=b"", peer_id=b""))
    assert result.interval == 1


def test_dispatches_udp(monkeypatch):
    async def fake(url, **kwargs):
        return AnnounceResult(peers=(), interval=2)

    import tinytorrent.daemon.tracker as dispatch_module

    monkeypatch.setattr(dispatch_module, "_udp_announce", fake)
    result = asyncio.run(announce("udp://tracker.example:80", info_hash=b"", peer_id=b""))
    assert result.interval == 2


def test_unsupported_scheme():
    with pytest.raises(TrackerError):
        asyncio.run(announce("ftp://tracker.example/announce", info_hash=b"", peer_id=b""))
