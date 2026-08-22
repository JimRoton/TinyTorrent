"""BEP 3 peer handshake, plus the BEP 10 reserved-byte extension flag."""

from __future__ import annotations

from dataclasses import dataclass

from tinytorrent.daemon.peer_wire.messages import PeerProtocolError

PROTOCOL_NAME = b"BitTorrent protocol"
HANDSHAKE_LENGTH = 1 + len(PROTOCOL_NAME) + 8 + 20 + 20  # 68 bytes

_EXTENSION_PROTOCOL_RESERVED_BYTE = 5
_EXTENSION_PROTOCOL_BIT = 0x10


def build_reserved_bytes(*, extension_protocol: bool = True) -> bytes:
    reserved = bytearray(8)
    if extension_protocol:
        reserved[_EXTENSION_PROTOCOL_RESERVED_BYTE] |= _EXTENSION_PROTOCOL_BIT
    return bytes(reserved)


def build_handshake(info_hash: bytes, peer_id: bytes, *, extension_protocol: bool = True) -> bytes:
    if len(info_hash) != 20:
        raise PeerProtocolError(f"info_hash must be 20 bytes, got {len(info_hash)}")
    if len(peer_id) != 20:
        raise PeerProtocolError(f"peer_id must be 20 bytes, got {len(peer_id)}")
    return (
        bytes([len(PROTOCOL_NAME)])
        + PROTOCOL_NAME
        + build_reserved_bytes(extension_protocol=extension_protocol)
        + info_hash
        + peer_id
    )


@dataclass(frozen=True)
class ParsedHandshake:
    reserved: bytes
    info_hash: bytes
    peer_id: bytes

    @property
    def supports_extension_protocol(self) -> bool:
        return bool(self.reserved[_EXTENSION_PROTOCOL_RESERVED_BYTE] & _EXTENSION_PROTOCOL_BIT)


def parse_handshake(data: bytes) -> ParsedHandshake:
    if len(data) != HANDSHAKE_LENGTH:
        raise PeerProtocolError(f"handshake must be {HANDSHAKE_LENGTH} bytes, got {len(data)}")

    pstrlen = data[0]
    if pstrlen != len(PROTOCOL_NAME):
        raise PeerProtocolError(f"unexpected protocol name length {pstrlen}")

    offset = 1
    pstr = data[offset:offset + pstrlen]
    if pstr != PROTOCOL_NAME:
        raise PeerProtocolError(f"unexpected protocol name {pstr!r}")
    offset += pstrlen

    reserved = data[offset:offset + 8]
    offset += 8

    info_hash = data[offset:offset + 20]
    offset += 20

    peer_id = data[offset:offset + 20]

    return ParsedHandshake(reserved=reserved, info_hash=info_hash, peer_id=peer_id)
