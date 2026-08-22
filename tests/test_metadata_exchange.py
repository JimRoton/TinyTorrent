import asyncio
import hashlib
import struct

import pytest

from tinytorrent.daemon import metadata_exchange as me
from tinytorrent.daemon.bencode import encode as bencode_encode
from tinytorrent.daemon.peer_wire import handshake as h
from tinytorrent.daemon.peer_wire import messages as m
from tinytorrent.daemon.peer_wire.connection import PeerConnection
from tinytorrent.daemon.peer_wire.extension import build_extended_handshake

INFO_HASH_PLACEHOLDER = bytes(range(20))
OUR_PEER_ID = bytes(range(20, 40))
SERVER_PEER_ID = bytes(range(40, 60))


class TestBuildRequest:
    def test_uses_peer_extended_message_id(self):
        msg = me.build_request(3, peer_extended_message_id=7)
        assert msg.extended_message_id == 7

    def test_payload_round_trips(self):
        msg = me.build_request(3, peer_extended_message_id=7)
        parsed = me.parse_metadata_message(msg)
        assert parsed.msg_type == me.MSG_TYPE_REQUEST
        assert parsed.piece == 3


class TestParseMetadataMessage:
    def test_data_message_with_trailing_bytes(self):
        header = bencode_encode({"msg_type": me.MSG_TYPE_DATA, "piece": 0, "total_size": 100})
        payload = header + b"raw piece bytes"
        parsed = me.parse_metadata_message(m.Extended(extended_message_id=1, payload=payload))
        assert parsed.msg_type == me.MSG_TYPE_DATA
        assert parsed.piece == 0
        assert parsed.total_size == 100
        assert parsed.data == b"raw piece bytes"

    def test_reject_message(self):
        payload = bencode_encode({"msg_type": me.MSG_TYPE_REJECT, "piece": 2})
        parsed = me.parse_metadata_message(m.Extended(extended_message_id=1, payload=payload))
        assert parsed.msg_type == me.MSG_TYPE_REJECT
        assert parsed.piece == 2
        assert parsed.data == b""
        assert parsed.total_size is None

    def test_malformed_bencode(self):
        with pytest.raises(me.MetadataExchangeError):
            me.parse_metadata_message(m.Extended(extended_message_id=1, payload=b"not bencode"))

    def test_non_dict_header(self):
        payload = bencode_encode([1, 2, 3])
        with pytest.raises(me.MetadataExchangeError):
            me.parse_metadata_message(m.Extended(extended_message_id=1, payload=payload))

    def test_missing_required_fields(self):
        payload = bencode_encode({"msg_type": me.MSG_TYPE_REQUEST})
        with pytest.raises(me.MetadataExchangeError):
            me.parse_metadata_message(m.Extended(extended_message_id=1, payload=payload))


class TestPieceCount:
    def test_exact_multiple(self):
        assert me.piece_count(me.METADATA_PIECE_SIZE * 3) == 3

    def test_partial_last_piece(self):
        assert me.piece_count(me.METADATA_PIECE_SIZE * 2 + 1) == 3

    def test_small_metadata(self):
        assert me.piece_count(1) == 1

    def test_zero(self):
        assert me.piece_count(0) == 0


class TestAssembleAndVerify:
    def _info_dict_bytes(self):
        return bencode_encode({"name": "file.txt", "length": 100, "piece length": 16384, "pieces": b"x" * 20})

    def test_success(self):
        data = self._info_dict_bytes()
        info_hash = hashlib.sha1(data).digest()
        result = me.assemble_and_verify({0: data}, len(data), info_hash)
        assert result == {b"name": b"file.txt", b"length": 100, b"piece length": 16384, b"pieces": b"x" * 20}

    def test_missing_piece(self):
        data = self._info_dict_bytes()
        info_hash = hashlib.sha1(data).digest()
        with pytest.raises(me.MetadataExchangeError, match="missing"):
            me.assemble_and_verify({}, len(data), info_hash)

    def test_size_mismatch(self):
        data = self._info_dict_bytes()
        info_hash = hashlib.sha1(data).digest()
        with pytest.raises(me.MetadataExchangeError, match="size"):
            me.assemble_and_verify({0: data + b"extra"}, len(data), info_hash)

    def test_hash_mismatch(self):
        data = self._info_dict_bytes()
        wrong_hash = bytes(20)
        with pytest.raises(me.MetadataExchangeError, match="info hash"):
            me.assemble_and_verify({0: data}, len(data), wrong_hash)

    def test_multi_piece_assembly_order(self):
        full = bencode_encode({"blob": "x" * (me.METADATA_PIECE_SIZE + 10)})
        half = len(full) // 2
        p0, p1 = full[:half], full[half:]
        info_hash = hashlib.sha1(full).digest()
        result = me.assemble_and_verify({1: p1, 0: p0}, len(full), info_hash)
        assert result == {b"blob": ("x" * (me.METADATA_PIECE_SIZE + 10)).encode()}


async def _start_server(handler):
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


class TestFetchMetadataFromPeer:
    def test_successful_fetch(self):
        info = bencode_encode({"name": "ubuntu.iso", "length": 12345})
        info_hash = hashlib.sha1(info).digest()

        async def scenario():
            async def handler(reader, writer):
                raw = await reader.readexactly(h.HANDSHAKE_LENGTH)
                h.parse_handshake(raw)
                writer.write(h.build_handshake(info_hash, SERVER_PEER_ID))
                await writer.drain()

                # Read client's extended handshake.
                frame = await _read_frame(reader)
                client_ext = m.decode(frame)
                assert isinstance(client_ext, m.Extended)

                # Reply with our own extended handshake advertising ut_metadata.
                server_ut_id = 5
                writer.write(
                    m.encode(
                        build_extended_handshake(
                            supported_extensions={"ut_metadata": server_ut_id},
                            metadata_size=len(info),
                        )
                    )
                )
                await writer.drain()

                # Serve the single metadata piece request.
                frame = await _read_frame(reader)
                request = m.decode(frame)
                assert isinstance(request, m.Extended)
                assert request.extended_message_id == server_ut_id
                parsed_request = me.parse_metadata_message(request)
                assert parsed_request.msg_type == me.MSG_TYPE_REQUEST
                assert parsed_request.piece == 0

                header = bencode_encode(
                    {"msg_type": me.MSG_TYPE_DATA, "piece": 0, "total_size": len(info)}
                )
                data_msg = m.Extended(
                    extended_message_id=me.LOCAL_UT_METADATA_ID, payload=header + info
                )
                writer.write(m.encode(data_msg))
                await writer.drain()
                writer.close()

            server, port = await _start_server(handler)
            async with server:
                conn = await PeerConnection.connect(
                    "127.0.0.1", port, info_hash=info_hash, our_peer_id=OUR_PEER_ID
                )
                result = await me.fetch_metadata_from_peer(conn, info_hash, timeout=5)
                conn.close()

            assert result == {b"name": b"ubuntu.iso", b"length": 12345}

        asyncio.run(scenario())

    def test_peer_without_ut_metadata_support_rejected(self):
        async def scenario():
            async def handler(reader, writer):
                raw = await reader.readexactly(h.HANDSHAKE_LENGTH)
                h.parse_handshake(raw)
                writer.write(h.build_handshake(INFO_HASH_PLACEHOLDER, SERVER_PEER_ID))
                await writer.drain()

                await _read_frame(reader)  # client's extended handshake
                writer.write(m.encode(build_extended_handshake(supported_extensions={})))
                await writer.drain()
                writer.close()

            server, port = await _start_server(handler)
            async with server:
                conn = await PeerConnection.connect(
                    "127.0.0.1", port, info_hash=INFO_HASH_PLACEHOLDER, our_peer_id=OUR_PEER_ID
                )
                with pytest.raises(me.MetadataExchangeError, match="does not support ut_metadata"):
                    await me.fetch_metadata_from_peer(conn, INFO_HASH_PLACEHOLDER, timeout=5)
                conn.close()

        asyncio.run(scenario())

    def test_peer_rejects_request(self):
        async def scenario():
            async def handler(reader, writer):
                raw = await reader.readexactly(h.HANDSHAKE_LENGTH)
                h.parse_handshake(raw)
                writer.write(h.build_handshake(INFO_HASH_PLACEHOLDER, SERVER_PEER_ID))
                await writer.drain()

                await _read_frame(reader)  # client's extended handshake
                server_ut_id = 5
                writer.write(
                    m.encode(
                        build_extended_handshake(
                            supported_extensions={"ut_metadata": server_ut_id}, metadata_size=100
                        )
                    )
                )
                await writer.drain()

                await _read_frame(reader)  # request for piece 0
                reject = bencode_encode({"msg_type": me.MSG_TYPE_REJECT, "piece": 0})
                writer.write(
                    m.encode(m.Extended(extended_message_id=me.LOCAL_UT_METADATA_ID, payload=reject))
                )
                await writer.drain()
                writer.close()

            server, port = await _start_server(handler)
            async with server:
                conn = await PeerConnection.connect(
                    "127.0.0.1", port, info_hash=INFO_HASH_PLACEHOLDER, our_peer_id=OUR_PEER_ID
                )
                with pytest.raises(me.MetadataExchangeError, match="rejected"):
                    await me.fetch_metadata_from_peer(conn, INFO_HASH_PLACEHOLDER, timeout=5)
                conn.close()

        asyncio.run(scenario())


async def _read_frame(reader: asyncio.StreamReader) -> bytes:
    length_bytes = await reader.readexactly(4)
    (length,) = struct.unpack("!I", length_bytes)
    return await reader.readexactly(length)
