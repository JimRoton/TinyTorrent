"""BEP 3 peer wire protocol messages (post-handshake).

Every message on the wire is length-prefixed: a 4-byte big-endian length
followed by that many bytes (a message id byte plus payload), except the
zero-length keep-alive. Framing (reading exactly ``length`` bytes off a
socket) is a connection-layer concern — see ``connection.py``. This module
only converts between wire bytes and typed message objects.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum


class MessageId(IntEnum):
    CHOKE = 0
    UNCHOKE = 1
    INTERESTED = 2
    NOT_INTERESTED = 3
    HAVE = 4
    BITFIELD = 5
    REQUEST = 6
    PIECE = 7
    CANCEL = 8
    PORT = 9
    EXTENDED = 20


class PeerProtocolError(Exception):
    """Raised on malformed or unrecognized peer wire protocol data."""


@dataclass(frozen=True)
class KeepAlive:
    pass


@dataclass(frozen=True)
class Choke:
    pass


@dataclass(frozen=True)
class Unchoke:
    pass


@dataclass(frozen=True)
class Interested:
    pass


@dataclass(frozen=True)
class NotInterested:
    pass


@dataclass(frozen=True)
class Have:
    piece_index: int


@dataclass(frozen=True)
class Bitfield:
    bitfield: bytes


@dataclass(frozen=True)
class Request:
    index: int
    begin: int
    length: int


@dataclass(frozen=True)
class Piece:
    index: int
    begin: int
    block: bytes


@dataclass(frozen=True)
class Cancel:
    index: int
    begin: int
    length: int


@dataclass(frozen=True)
class PortMessage:
    listen_port: int


@dataclass(frozen=True)
class Extended:
    extended_message_id: int
    payload: bytes


Message = (
    KeepAlive
    | Choke
    | Unchoke
    | Interested
    | NotInterested
    | Have
    | Bitfield
    | Request
    | Piece
    | Cancel
    | PortMessage
    | Extended
)

# Generous upper bound on a single message frame, to guard against a
# malicious or buggy peer claiming an enormous length prefix and forcing
# us to allocate unbounded memory before we've validated anything.
MAX_FRAME_LENGTH = 1 << 20  # 1 MiB — comfortably covers bitfields and blocks


def encode(message: Message) -> bytes:
    """Encode a message into its full wire frame, length prefix included."""
    if isinstance(message, KeepAlive):
        return struct.pack("!I", 0)
    body = _encode_body(message)
    return struct.pack("!I", len(body)) + body


def _encode_body(message: Message) -> bytes:
    if isinstance(message, Choke):
        return bytes([MessageId.CHOKE])
    if isinstance(message, Unchoke):
        return bytes([MessageId.UNCHOKE])
    if isinstance(message, Interested):
        return bytes([MessageId.INTERESTED])
    if isinstance(message, NotInterested):
        return bytes([MessageId.NOT_INTERESTED])
    if isinstance(message, Have):
        return bytes([MessageId.HAVE]) + struct.pack("!I", message.piece_index)
    if isinstance(message, Bitfield):
        return bytes([MessageId.BITFIELD]) + message.bitfield
    if isinstance(message, Request):
        return bytes([MessageId.REQUEST]) + struct.pack(
            "!III", message.index, message.begin, message.length
        )
    if isinstance(message, Piece):
        return (
            bytes([MessageId.PIECE])
            + struct.pack("!II", message.index, message.begin)
            + message.block
        )
    if isinstance(message, Cancel):
        return bytes([MessageId.CANCEL]) + struct.pack(
            "!III", message.index, message.begin, message.length
        )
    if isinstance(message, PortMessage):
        return bytes([MessageId.PORT]) + struct.pack("!H", message.listen_port)
    if isinstance(message, Extended):
        return bytes([MessageId.EXTENDED, message.extended_message_id]) + message.payload
    raise PeerProtocolError(f"cannot encode message of type {type(message).__name__}")


def decode(frame: bytes) -> Message:
    """Decode a message frame — the bytes *after* the 4-byte length prefix.

    An empty frame decodes to ``KeepAlive``, matching a zero-length prefix
    on the wire.
    """
    if len(frame) == 0:
        return KeepAlive()

    message_id = frame[0]
    body = frame[1:]

    if message_id == MessageId.CHOKE:
        _expect_len(body, 0, "choke")
        return Choke()
    if message_id == MessageId.UNCHOKE:
        _expect_len(body, 0, "unchoke")
        return Unchoke()
    if message_id == MessageId.INTERESTED:
        _expect_len(body, 0, "interested")
        return Interested()
    if message_id == MessageId.NOT_INTERESTED:
        _expect_len(body, 0, "not interested")
        return NotInterested()
    if message_id == MessageId.HAVE:
        _expect_len(body, 4, "have")
        (piece_index,) = struct.unpack("!I", body)
        return Have(piece_index)
    if message_id == MessageId.BITFIELD:
        return Bitfield(body)
    if message_id == MessageId.REQUEST:
        _expect_len(body, 12, "request")
        index, begin, length = struct.unpack("!III", body)
        return Request(index, begin, length)
    if message_id == MessageId.PIECE:
        if len(body) < 8:
            raise PeerProtocolError(f"'piece' message too short: {len(body)} bytes")
        index, begin = struct.unpack("!II", body[:8])
        return Piece(index, begin, body[8:])
    if message_id == MessageId.CANCEL:
        _expect_len(body, 12, "cancel")
        index, begin, length = struct.unpack("!III", body)
        return Cancel(index, begin, length)
    if message_id == MessageId.PORT:
        _expect_len(body, 2, "port")
        (listen_port,) = struct.unpack("!H", body)
        return PortMessage(listen_port)
    if message_id == MessageId.EXTENDED:
        if len(body) < 1:
            raise PeerProtocolError("'extended' message missing extended message id")
        return Extended(body[0], body[1:])

    raise PeerProtocolError(f"unknown message id {message_id}")


def _expect_len(body: bytes, expected: int, name: str) -> None:
    if len(body) != expected:
        raise PeerProtocolError(
            f"'{name}' message has wrong length: expected {expected}, got {len(body)}"
        )
