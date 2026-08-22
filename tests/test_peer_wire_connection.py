import asyncio
import struct

import pytest

from tinytorrent.daemon.peer_wire import handshake as h
from tinytorrent.daemon.peer_wire import messages as m
from tinytorrent.daemon.peer_wire.connection import PeerConnection, PeerConnectionError
from tinytorrent.daemon.peer_wire.messages import PeerProtocolError

INFO_HASH = bytes(range(20))
OUR_PEER_ID = bytes(range(20, 40))
SERVER_PEER_ID = bytes(range(40, 60))


async def _start_server(handler):
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


class TestConnectAndHandshake:
    def test_successful_handshake(self):
        async def scenario():
            async def handler(reader, writer):
                raw = await reader.readexactly(h.HANDSHAKE_LENGTH)
                parsed = h.parse_handshake(raw)
                assert parsed.info_hash == INFO_HASH
                writer.write(h.build_handshake(INFO_HASH, SERVER_PEER_ID))
                await writer.drain()
                writer.close()

            server, port = await _start_server(handler)
            async with server:
                conn = await PeerConnection.connect(
                    "127.0.0.1", port, info_hash=INFO_HASH, our_peer_id=OUR_PEER_ID
                )
                assert conn.peer_id == SERVER_PEER_ID
                assert conn.supports_extension_protocol is True
                assert conn.address == ("127.0.0.1", port)
                conn.close()
                await conn.wait_closed()

        asyncio.run(scenario())

    def test_info_hash_mismatch_rejected(self):
        other_hash = bytes(range(1, 21))

        async def scenario():
            async def handler(reader, writer):
                await reader.readexactly(h.HANDSHAKE_LENGTH)
                writer.write(h.build_handshake(other_hash, SERVER_PEER_ID))
                await writer.drain()
                writer.close()

            server, port = await _start_server(handler)
            async with server:
                with pytest.raises(PeerConnectionError, match="mismatch"):
                    await PeerConnection.connect(
                        "127.0.0.1", port, info_hash=INFO_HASH, our_peer_id=OUR_PEER_ID
                    )

        asyncio.run(scenario())

    def test_connection_closed_during_handshake(self):
        async def scenario():
            async def handler(reader, writer):
                await reader.readexactly(h.HANDSHAKE_LENGTH)
                writer.close()  # close without responding

            server, port = await _start_server(handler)
            async with server:
                with pytest.raises(PeerConnectionError, match="closed connection"):
                    await PeerConnection.connect(
                        "127.0.0.1", port, info_hash=INFO_HASH, our_peer_id=OUR_PEER_ID
                    )

        asyncio.run(scenario())

    def test_connect_refused(self):
        async def scenario():
            # Bind then immediately close, to get a port nothing is listening on.
            server, port = await _start_server(lambda r, w: None)
            server.close()
            await server.wait_closed()

            with pytest.raises(PeerConnectionError):
                await PeerConnection.connect(
                    "127.0.0.1", port, info_hash=INFO_HASH, our_peer_id=OUR_PEER_ID
                )

        asyncio.run(scenario())


class TestSendReceive:
    def test_message_round_trip(self):
        async def scenario():
            async def handler(reader, writer):
                await reader.readexactly(h.HANDSHAKE_LENGTH)
                writer.write(h.build_handshake(INFO_HASH, SERVER_PEER_ID))
                await writer.drain()

                frame_len_bytes = await reader.readexactly(4)
                (length,) = struct.unpack("!I", frame_len_bytes)
                body = await reader.readexactly(length)
                assert m.decode(body) == m.Interested()

                writer.write(m.encode(m.Unchoke()))
                await writer.drain()
                writer.close()

            server, port = await _start_server(handler)
            async with server:
                conn = await PeerConnection.connect(
                    "127.0.0.1", port, info_hash=INFO_HASH, our_peer_id=OUR_PEER_ID
                )
                await conn.send(m.Interested())
                reply = await conn.receive()
                assert reply == m.Unchoke()
                conn.close()

        asyncio.run(scenario())

    def test_oversized_frame_rejected(self):
        async def scenario():
            async def handler(reader, writer):
                await reader.readexactly(h.HANDSHAKE_LENGTH)
                writer.write(h.build_handshake(INFO_HASH, SERVER_PEER_ID))
                await writer.drain()
                # Claim an absurdly large frame without sending the body.
                writer.write(struct.pack("!I", m.MAX_FRAME_LENGTH + 1))
                await writer.drain()

            server, port = await _start_server(handler)
            async with server:
                conn = await PeerConnection.connect(
                    "127.0.0.1", port, info_hash=INFO_HASH, our_peer_id=OUR_PEER_ID
                )
                with pytest.raises(PeerProtocolError, match="too large"):
                    await conn.receive()
                conn.close()

        asyncio.run(scenario())

    def test_receive_after_peer_closes(self):
        async def scenario():
            async def handler(reader, writer):
                await reader.readexactly(h.HANDSHAKE_LENGTH)
                writer.write(h.build_handshake(INFO_HASH, SERVER_PEER_ID))
                await writer.drain()
                writer.close()

            server, port = await _start_server(handler)
            async with server:
                conn = await PeerConnection.connect(
                    "127.0.0.1", port, info_hash=INFO_HASH, our_peer_id=OUR_PEER_ID
                )
                with pytest.raises(PeerConnectionError):
                    await conn.receive()

        asyncio.run(scenario())
