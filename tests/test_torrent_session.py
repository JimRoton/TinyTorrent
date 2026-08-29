import asyncio
import hashlib
import socket
import struct
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from tinytorrent.common.hooks import HookEvent
from tinytorrent.common.ids import generate_peer_id
from tinytorrent.common.priority import Priority
from tinytorrent.daemon import metadata_exchange as me
from tinytorrent.daemon import torrent_session as ts
from tinytorrent.daemon.bencode import encode as bencode_encode
from tinytorrent.daemon.magnet import MagnetLink
from tinytorrent.daemon.peer_wire import handshake as h
from tinytorrent.daemon.peer_wire import messages as m
from tinytorrent.daemon.peer_wire.extension import build_extended_handshake
from tinytorrent.daemon.torrent_info import parse_info_dict
from tinytorrent.daemon.torrent_session import TorrentSession, TorrentStatus, _bitfield_to_piece_set

SERVER_PEER_ID = bytes(range(40, 60))


def _hash(data: bytes) -> bytes:
    return hashlib.sha1(data).digest()


def _make_session(piece_length=10, length=25, name=b"file.txt", completed=None):
    num_pieces = -(-length // piece_length)
    pieces = b"".join(_hash(bytes([i])) for i in range(num_pieces))
    info = parse_info_dict(
        {b"name": name, b"piece length": piece_length, b"pieces": pieces, b"length": length}
    )
    magnet = MagnetLink(info_hash=b"\x00" * 20, display_name=None, trackers=())
    session = TorrentSession(
        "abcd", magnet, "/tmp/unused", generate_peer_id(), info=info, completed_pieces=completed
    )
    return session


class TestBitfieldParsing:
    def test_all_bits_set(self):
        assert _bitfield_to_piece_set(b"\xff", 8) == set(range(8))

    def test_partial_byte(self):
        # 10110000 -> pieces 0, 2, 3
        assert _bitfield_to_piece_set(bytes([0b10110000]), 5) == {0, 2, 3}

    def test_stops_at_num_pieces_even_if_bitfield_longer(self):
        assert _bitfield_to_piece_set(b"\xff\xff", 3) == {0, 1, 2}

    def test_short_bitfield_treated_as_missing_trailing_bits(self):
        assert _bitfield_to_piece_set(b"\xff", 12) == {0, 1, 2, 3, 4, 5, 6, 7}


class TestProgressAndRate:
    def test_progress_before_metadata(self):
        magnet = MagnetLink(info_hash=b"\x00" * 20, display_name="mystery", trackers=())
        session = TorrentSession("abcd", magnet, "/tmp/unused", generate_peer_id())
        snapshot = session.progress()
        assert snapshot.status == TorrentStatus.QUEUED
        assert snapshot.total_length == 0
        assert snapshot.eta_seconds is None

    def test_progress_reflects_completed_pieces(self):
        session = _make_session(piece_length=10, length=25, completed={0, 1})
        snapshot = session.progress()
        assert snapshot.bytes_downloaded == 20
        assert snapshot.completed_pieces == 2
        assert snapshot.num_pieces == 3

    def test_eta_uses_rate_and_remaining_bytes(self):
        session = _make_session(piece_length=10, length=25, completed={0})
        session._rate_samples = [(0.0, 0), (10.0, 100)]  # 10 bytes/sec
        snapshot = session.progress()
        assert snapshot.download_rate_bps == pytest.approx(10.0)
        # remaining = 25 - 10 = 15 bytes, at 10 B/s -> 1.5s
        assert snapshot.eta_seconds == pytest.approx(1.5)

    def test_no_eta_when_rate_is_zero(self):
        session = _make_session(piece_length=10, length=25, completed={0})
        snapshot = session.progress()
        assert snapshot.download_rate_bps == 0.0
        assert snapshot.eta_seconds is None

    def test_display_name_falls_back_to_magnet(self):
        magnet = MagnetLink(info_hash=b"\x00" * 20, display_name="cool.iso", trackers=())
        session = TorrentSession("abcd", magnet, "/tmp/unused", generate_peer_id())
        assert session.display_name == "cool.iso"

    def test_display_name_prefers_metadata_once_known(self):
        session = _make_session(name=b"real-name.txt")
        assert session.display_name == "real-name.txt"


class TestPickPiece:
    def test_picks_lowest_needed_piece_peer_has(self):
        session = _make_session(piece_length=10, length=25)
        assert session._pick_piece({0, 1, 2}) == 0

    def test_skips_completed_pieces(self):
        session = _make_session(piece_length=10, length=25, completed={0})
        assert session._pick_piece({0, 1, 2}) == 1

    def test_skips_pieces_owned_by_another_peer(self):
        session = _make_session(piece_length=10, length=25)
        session._piece_owner[0] = "1.2.3.4:6881"
        assert session._pick_piece({0, 1, 2}) == 1

    def test_returns_none_if_peer_has_nothing_needed(self):
        session = _make_session(piece_length=10, length=25, completed={0, 1, 2})
        assert session._pick_piece({0, 1, 2}) is None

    def test_returns_none_if_peer_has_no_relevant_pieces(self):
        session = _make_session(piece_length=10, length=25)
        assert session._pick_piece(set()) is None


# --- End-to-end: fake tracker + fake seeding peer -----------------------


class _TrackerHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = self.server.response_body
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002 - silence default logging
        pass


def _start_fake_http_tracker(response_body: bytes):
    server = HTTPServer(("127.0.0.1", 0), _TrackerHandler)
    server.response_body = response_body
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _build_full_bitfield(num_pieces: int) -> bytes:
    num_bytes = -(-num_pieces // 8)
    bits = bytearray(num_bytes)
    for i in range(num_pieces):
        byte_index, bit_index = divmod(i, 8)
        bits[byte_index] |= 0x80 >> bit_index
    return bytes(bits)


async def _read_frame(reader: asyncio.StreamReader) -> bytes:
    length_bytes = await reader.readexactly(4)
    (length,) = struct.unpack("!I", length_bytes)
    return await reader.readexactly(length)


async def _seeding_peer_handler(reader, writer, *, info_hash, content, metadata_bytes, piece_length, num_pieces):
    """A fake peer that behaves like a full seed: serves metadata on
    request, then serves piece data for the whole torrent. Handles both
    a metadata-only connection and a download connection through the
    same generic message loop, mirroring how a real peer can't assume in
    advance which one it's talking to.
    """
    server_ut_id = 5
    raw = await reader.readexactly(h.HANDSHAKE_LENGTH)
    h.parse_handshake(raw)
    writer.write(h.build_handshake(info_hash, SERVER_PEER_ID))
    await writer.drain()

    writer.write(m.encode(m.Bitfield(_build_full_bitfield(num_pieces))))
    writer.write(m.encode(m.Unchoke()))
    await writer.drain()

    while True:
        try:
            frame = await _read_frame(reader)
        except asyncio.IncompleteReadError:
            return
        message = m.decode(frame)

        if isinstance(message, m.Extended) and message.extended_message_id == 0:
            writer.write(
                m.encode(
                    build_extended_handshake(
                        supported_extensions={"ut_metadata": server_ut_id},
                        metadata_size=len(metadata_bytes),
                    )
                )
            )
            await writer.drain()
        elif isinstance(message, m.Extended) and message.extended_message_id == server_ut_id:
            request = me.parse_metadata_message(message)
            header = bencode_encode(
                {"msg_type": me.MSG_TYPE_DATA, "piece": request.piece, "total_size": len(metadata_bytes)}
            )
            writer.write(m.encode(m.Extended(extended_message_id=me.LOCAL_UT_METADATA_ID, payload=header + metadata_bytes)))
            await writer.drain()
        elif isinstance(message, m.Request):
            start = message.index * piece_length + message.begin
            data = content[start:start + message.length]
            writer.write(m.encode(m.Piece(index=message.index, begin=message.begin, block=data)))
            await writer.drain()
        # Interested and anything else: no response needed.


class TestTorrentSessionEndToEnd:
    def test_full_download_via_fake_tracker_and_peer(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ts, "TRACKER_RETRY_DELAY", 0.05)
        monkeypatch.setattr(ts, "METADATA_RETRY_DELAY", 0.05)

        content = b"HELLO WORLD, THIS IS TINYTORRENT DEMO!!"
        piece_length = 16
        num_pieces = -(-len(content) // piece_length)  # 3: 16, 16, 8
        pieces_hashes = b"".join(
            _hash(content[i * piece_length: (i + 1) * piece_length]) for i in range(num_pieces)
        )
        info_dict = {
            b"name": b"hello.txt",
            b"piece length": piece_length,
            b"pieces": pieces_hashes,
            b"length": len(content),
        }
        metadata_bytes = bencode_encode(info_dict)
        info_hash = _hash(metadata_bytes)

        events = []

        async def scenario():
            async def handler(reader, writer):
                await _seeding_peer_handler(
                    reader,
                    writer,
                    info_hash=info_hash,
                    content=content,
                    metadata_bytes=metadata_bytes,
                    piece_length=piece_length,
                    num_pieces=num_pieces,
                )

            peer_server = await asyncio.start_server(handler, "127.0.0.1", 0)
            peer_port = peer_server.sockets[0].getsockname()[1]

            peer_bytes = socket.inet_aton("127.0.0.1") + struct.pack("!H", peer_port)
            tracker_response = bencode_encode({b"interval": 1800, b"peers": peer_bytes})
            http_server, thread = _start_fake_http_tracker(tracker_response)

            try:
                async with peer_server:
                    tracker_url = f"http://127.0.0.1:{http_server.server_port}/announce"
                    magnet = MagnetLink(info_hash=info_hash, display_name="hello.txt", trackers=(tracker_url,))
                    session = TorrentSession(
                        "t1",
                        magnet,
                        tmp_path,
                        generate_peer_id(),
                        on_event=lambda event, _session: events.append(event),
                    )
                    await asyncio.wait_for(session.download(), timeout=15)
            finally:
                http_server.shutdown()
                thread.join(timeout=2)

            return session

        session = asyncio.run(scenario())

        assert session.status == TorrentStatus.COMPLETE
        assert session.error_message is None
        assert session.completed_pieces == set(range(num_pieces))
        downloaded = (tmp_path / "hello.txt").read_bytes()
        assert downloaded == content
        assert events == [
            HookEvent.METADATA_FETCHED,
            HookEvent.DOWNLOAD_STARTED,
            HookEvent.DOWNLOAD_COMPLETED,
        ]

    def test_no_trackers_results_in_error_status(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ts, "METADATA_RETRY_DELAY", 0.01)
        magnet = MagnetLink(info_hash=b"\x00" * 20, display_name=None, trackers=())
        session = TorrentSession("t2", magnet, tmp_path, generate_peer_id())

        asyncio.run(asyncio.wait_for(session.download(), timeout=5))

        assert session.status == TorrentStatus.ERROR
        assert session.error_message is not None


class TestLifecycleEvents:
    def test_firing_without_a_callback_is_harmless(self):
        session = _make_session()
        session._fire(HookEvent.DOWNLOAD_COMPLETED)  # no on_event wired

    def test_callback_receives_the_event_and_session(self):
        received = []
        magnet = MagnetLink(info_hash=b"\x00" * 20, display_name="x", trackers=())
        session = TorrentSession(
            "abcd",
            magnet,
            "/tmp/unused",
            generate_peer_id(),
            on_event=lambda event, s: received.append((event, s)),
        )
        session._fire(HookEvent.DOWNLOAD_STARTED)
        assert received == [(HookEvent.DOWNLOAD_STARTED, session)]

    def test_failure_fires_download_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ts, "METADATA_RETRY_DELAY", 0.01)
        events = []
        magnet = MagnetLink(info_hash=b"\x00" * 20, display_name=None, trackers=())
        session = TorrentSession(
            "t9",
            magnet,
            tmp_path,
            generate_peer_id(),
            on_event=lambda event, _s: events.append(event),
        )

        asyncio.run(asyncio.wait_for(session.download(), timeout=5))

        assert session.status == TorrentStatus.ERROR
        assert events == [HookEvent.DOWNLOAD_ERROR]

    def test_already_complete_on_disk_does_not_fire_download_completed(self, tmp_path):
        # Resuming a finished torrent after a restart re-verifies it from
        # disk. That must not re-trigger the user's completion hooks.
        content = b"tinytorrent completed content!!"
        piece_length = 16
        num_pieces = -(-len(content) // piece_length)
        pieces_hashes = b"".join(
            _hash(content[i * piece_length: (i + 1) * piece_length]) for i in range(num_pieces)
        )
        info = parse_info_dict(
            {
                b"name": b"done.txt",
                b"piece length": piece_length,
                b"pieces": pieces_hashes,
                b"length": len(content),
            }
        )
        (tmp_path / "done.txt").write_bytes(content)

        events = []
        magnet = MagnetLink(info_hash=b"\x00" * 20, display_name=None, trackers=())
        session = TorrentSession(
            "t8",
            magnet,
            tmp_path,
            generate_peer_id(),
            info=info,
            on_event=lambda event, _s: events.append(event),
        )

        asyncio.run(asyncio.wait_for(session.download(), timeout=5))

        assert session.status == TorrentStatus.COMPLETE
        assert events == []
