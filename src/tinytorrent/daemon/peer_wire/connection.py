"""Asyncio TCP connection to a single peer: handshake + message framing.

This is the transport layer only — it knows how to open a connection,
perform the BEP 3 handshake, and read/write length-prefixed messages. It
has no opinion about choke state, piece selection, or what a 'piece'
message means; that belongs to the torrent session that drives it.
"""

from __future__ import annotations

import asyncio
import struct

from tinytorrent.daemon.peer_wire import handshake, messages
from tinytorrent.daemon.peer_wire.messages import Message, PeerProtocolError

DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_HANDSHAKE_TIMEOUT = 10.0


class PeerConnectionError(Exception):
    """Raised when connecting to, or handshaking with, a peer fails."""


class PeerConnection:
    """An open, handshaken connection to a single remote peer."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        peer_id: bytes,
        reserved: bytes,
        address: tuple[str, int],
    ) -> None:
        self._reader = reader
        self._writer = writer
        self.peer_id = peer_id
        self.reserved = reserved
        self.address = address

    @property
    def supports_extension_protocol(self) -> bool:
        return bool(self.reserved[5] & 0x10)

    @classmethod
    async def connect(
        cls,
        host: str,
        port: int,
        *,
        info_hash: bytes,
        our_peer_id: bytes,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        handshake_timeout: float = DEFAULT_HANDSHAKE_TIMEOUT,
    ) -> "PeerConnection":
        """Open a TCP connection to (host, port) and perform the BEP 3 handshake."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=connect_timeout
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise PeerConnectionError(f"could not connect to {host}:{port}: {exc}") from exc

        try:
            parsed = await cls._do_handshake(
                reader, writer, info_hash, our_peer_id, handshake_timeout, host, port
            )
        except Exception:
            writer.close()
            raise

        return cls(
            reader,
            writer,
            peer_id=parsed.peer_id,
            reserved=parsed.reserved,
            address=(host, port),
        )

    @staticmethod
    async def _do_handshake(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        info_hash: bytes,
        our_peer_id: bytes,
        timeout: float,
        host: str,
        port: int,
    ) -> handshake.ParsedHandshake:
        outgoing = handshake.build_handshake(info_hash, our_peer_id)
        writer.write(outgoing)
        await writer.drain()

        try:
            raw = await asyncio.wait_for(
                reader.readexactly(handshake.HANDSHAKE_LENGTH), timeout=timeout
            )
        except asyncio.IncompleteReadError as exc:
            raise PeerConnectionError(f"peer {host}:{port} closed connection during handshake") from exc
        except asyncio.TimeoutError as exc:
            raise PeerConnectionError(f"handshake with {host}:{port} timed out") from exc

        parsed = handshake.parse_handshake(raw)
        if parsed.info_hash != info_hash:
            raise PeerConnectionError(f"peer {host}:{port} handshake info_hash mismatch")
        return parsed

    async def send(self, message: Message) -> None:
        self._writer.write(messages.encode(message))
        await self._writer.drain()

    async def receive(self) -> Message:
        """Read and decode the next message frame from the peer."""
        try:
            length_prefix = await self._reader.readexactly(4)
        except asyncio.IncompleteReadError as exc:
            raise PeerConnectionError("connection closed while reading message length") from exc

        (length,) = struct.unpack("!I", length_prefix)
        if length > messages.MAX_FRAME_LENGTH:
            raise PeerProtocolError(f"message frame too large: {length} bytes")
        if length == 0:
            return messages.decode(b"")

        try:
            frame = await self._reader.readexactly(length)
        except asyncio.IncompleteReadError as exc:
            raise PeerConnectionError("connection closed while reading message body") from exc

        return messages.decode(frame)

    def close(self) -> None:
        self._writer.close()

    async def wait_closed(self) -> None:
        try:
            await self._writer.wait_closed()
        except (ConnectionError, OSError):
            pass
