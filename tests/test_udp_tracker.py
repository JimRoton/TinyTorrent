import asyncio
import socket
import struct
import threading

import pytest

from tinytorrent.daemon.tracker import udp_tracker
from tinytorrent.daemon.tracker.http_tracker import TrackerError

INFO_HASH = bytes(range(20))
PEER_ID = bytes(range(20, 40))


class FakeUdpTracker:
    """A minimal BEP 15 UDP tracker server for round-trip testing."""

    def __init__(self, *, peers_bytes: bytes = b"", mode: str = "normal"):
        self.peers_bytes = peers_bytes
        self.mode = mode  # "normal" | "error" | "silent" | "bad_transaction"
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc_info):
        self.sock.close()
        self._thread.join(timeout=2)

    def _serve(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(4096)
            except OSError:
                return

            if self.mode == "silent":
                continue

            if len(data) == 16:  # connect request
                _magic, action, transaction_id = struct.unpack("!qII", data)
                if action != udp_tracker._ACTION_CONNECT:
                    continue
                self._reply_connect(addr, transaction_id)
            elif len(data) == 98:  # announce request
                _connection_id, action, transaction_id = struct.unpack("!qII", data[:16])
                if action != udp_tracker._ACTION_ANNOUNCE:
                    continue
                self._reply_announce(addr, transaction_id)

    def _reply_connect(self, addr, transaction_id):
        if self.mode == "bad_transaction":
            transaction_id ^= 0xFFFFFFFF
        response = struct.pack("!IIq", udp_tracker._ACTION_CONNECT, transaction_id, 0x1122334455667788)
        self.sock.sendto(response, addr)

    def _reply_announce(self, addr, transaction_id):
        if self.mode == "error":
            message = b"rejected: over quota"
            response = struct.pack("!II", udp_tracker._ACTION_ERROR, transaction_id) + message
            self.sock.sendto(response, addr)
            return
        header = struct.pack(
            "!IIIII",
            udp_tracker._ACTION_ANNOUNCE,
            transaction_id,
            1800,
            0,
            len(self.peers_bytes) // 6,
        )
        self.sock.sendto(header + self.peers_bytes, addr)


def _run(coro):
    return asyncio.run(coro)


class TestAnnounceRoundTrip:
    def test_successful_announce(self):
        peers_bytes = bytes([203, 0, 113, 5]) + struct.pack("!H", 51413)
        with FakeUdpTracker(peers_bytes=peers_bytes) as tracker:
            result = _run(
                udp_tracker.announce(
                    f"udp://127.0.0.1:{tracker.port}",
                    info_hash=INFO_HASH,
                    peer_id=PEER_ID,
                    port=6881,
                    uploaded=0,
                    downloaded=0,
                    left=1000,
                )
            )
        assert result.interval == 1800
        assert result.peers == (("203.0.113.5", 51413),)

    def test_multiple_peers(self):
        peers_bytes = (
            bytes([203, 0, 113, 5]) + struct.pack("!H", 1000)
            + bytes([198, 51, 100, 7]) + struct.pack("!H", 2000)
        )
        with FakeUdpTracker(peers_bytes=peers_bytes) as tracker:
            result = _run(
                udp_tracker.announce(
                    f"udp://127.0.0.1:{tracker.port}",
                    info_hash=INFO_HASH,
                    peer_id=PEER_ID,
                    port=6881,
                    uploaded=0,
                    downloaded=0,
                    left=1000,
                    event="started",
                )
            )
        assert result.peers == (("203.0.113.5", 1000), ("198.51.100.7", 2000))

    def test_tracker_error_response(self):
        with FakeUdpTracker(mode="error") as tracker:
            with pytest.raises(TrackerError, match="rejected: over quota"):
                _run(
                    udp_tracker.announce(
                        f"udp://127.0.0.1:{tracker.port}",
                        info_hash=INFO_HASH,
                        peer_id=PEER_ID,
                        port=6881,
                        uploaded=0,
                        downloaded=0,
                        left=1000,
                    )
                )

    def test_timeout_when_tracker_silent(self, monkeypatch):
        monkeypatch.setattr(udp_tracker, "_INITIAL_TIMEOUT", 0.05)
        monkeypatch.setattr(udp_tracker, "_MAX_RETRIES", 1)
        with FakeUdpTracker(mode="silent") as tracker:
            with pytest.raises(TrackerError, match="timed out"):
                _run(
                    udp_tracker.announce(
                        f"udp://127.0.0.1:{tracker.port}",
                        info_hash=INFO_HASH,
                        peer_id=PEER_ID,
                        port=6881,
                        uploaded=0,
                        downloaded=0,
                        left=1000,
                    )
                )

    def test_transaction_id_mismatch_rejected(self, monkeypatch):
        monkeypatch.setattr(udp_tracker, "_INITIAL_TIMEOUT", 0.05)
        monkeypatch.setattr(udp_tracker, "_MAX_RETRIES", 1)
        with FakeUdpTracker(mode="bad_transaction") as tracker:
            with pytest.raises(TrackerError):
                _run(
                    udp_tracker.announce(
                        f"udp://127.0.0.1:{tracker.port}",
                        info_hash=INFO_HASH,
                        peer_id=PEER_ID,
                        port=6881,
                        uploaded=0,
                        downloaded=0,
                        left=1000,
                    )
                )


class TestValidation:
    def test_wrong_scheme_rejected(self):
        with pytest.raises(TrackerError):
            _run(
                udp_tracker.announce(
                    "http://tracker.example/announce",
                    info_hash=INFO_HASH,
                    peer_id=PEER_ID,
                    port=6881,
                    uploaded=0,
                    downloaded=0,
                    left=0,
                )
            )

    def test_bad_info_hash_length(self):
        with pytest.raises(TrackerError):
            _run(
                udp_tracker.announce(
                    "udp://127.0.0.1:1",
                    info_hash=b"short",
                    peer_id=PEER_ID,
                    port=6881,
                    uploaded=0,
                    downloaded=0,
                    left=0,
                )
            )

    def test_unsupported_event(self):
        with pytest.raises(TrackerError):
            _run(
                udp_tracker.announce(
                    "udp://127.0.0.1:1",
                    info_hash=INFO_HASH,
                    peer_id=PEER_ID,
                    port=6881,
                    uploaded=0,
                    downloaded=0,
                    left=0,
                    event="bogus",
                )
            )
