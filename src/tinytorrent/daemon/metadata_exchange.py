"""BEP 9 metadata exchange (the ut_metadata extension).

TinyTorrent only ever starts from a magnet link, so it never has the
torrent's info dict up front — only the info hash. This module fetches
the info dict from any one connected peer that already has it, over the
BEP 10 extension protocol, then verifies the result actually hashes to
the info hash we asked for (peers are untrusted, so this check is what
makes it safe to build a download plan from what they send back).
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tinytorrent.daemon.bencode import BencodeDecodeError, decode, decode_from, encode
from tinytorrent.daemon.peer_wire.extension import (
    EXTENDED_HANDSHAKE_ID,
    build_extended_handshake,
    parse_extended_handshake,
)
from tinytorrent.daemon.peer_wire.messages import Extended

if TYPE_CHECKING:
    from tinytorrent.daemon.peer_wire.connection import PeerConnection

EXTENSION_NAME = "ut_metadata"
METADATA_PIECE_SIZE = 16384  # 16 KiB, fixed by BEP 9

MSG_TYPE_REQUEST = 0
MSG_TYPE_DATA = 1
MSG_TYPE_REJECT = 2

# Our local id for the ut_metadata extension — what we tell peers to use
# when sending *us* a ut_metadata message. 0 is reserved for the base
# extended handshake itself, so this must be non-zero.
LOCAL_UT_METADATA_ID = 1

DEFAULT_METADATA_TIMEOUT = 30.0  # seconds, covering the whole fetch


class MetadataExchangeError(Exception):
    """Raised when metadata can't be fetched from a peer, or fails verification."""


def build_request(piece_index: int, *, peer_extended_message_id: int) -> Extended:
    """Build an outgoing metadata request for ``piece_index``.

    ``peer_extended_message_id`` is the id *the peer* advertised for
    ut_metadata in their extended handshake — BEP 10 messages are always
    addressed using the recipient's chosen id, not the sender's.
    """
    payload = encode({"msg_type": MSG_TYPE_REQUEST, "piece": piece_index})
    return Extended(extended_message_id=peer_extended_message_id, payload=payload)


@dataclass(frozen=True)
class MetadataMessage:
    msg_type: int
    piece: int
    total_size: int | None
    data: bytes  # raw piece bytes for 'data' messages; empty otherwise


def parse_metadata_message(message: Extended) -> MetadataMessage:
    try:
        header, offset = decode_from(message.payload, 0)
    except BencodeDecodeError as exc:
        raise MetadataExchangeError(f"malformed ut_metadata message: {exc}") from exc

    if not isinstance(header, dict):
        raise MetadataExchangeError("ut_metadata message header was not a dict")

    msg_type = header.get(b"msg_type")
    piece = header.get(b"piece")
    if not isinstance(msg_type, int) or not isinstance(piece, int):
        raise MetadataExchangeError("ut_metadata message missing msg_type/piece")

    total_size = header.get(b"total_size")
    if not isinstance(total_size, int):
        total_size = None

    # For 'data' messages the raw piece bytes follow the bencoded header
    # directly, with no further delimiter — whatever's left in the payload.
    trailing = message.payload[offset:]

    return MetadataMessage(msg_type=msg_type, piece=piece, total_size=total_size, data=trailing)


def piece_count(metadata_size: int) -> int:
    return (metadata_size + METADATA_PIECE_SIZE - 1) // METADATA_PIECE_SIZE


def assemble_and_verify(pieces: dict[int, bytes], metadata_size: int, info_hash: bytes) -> dict:
    """Assemble collected metadata pieces, verify against ``info_hash``, decode.

    Raises ``MetadataExchangeError`` if pieces are missing, the assembled
    size is wrong, or the SHA-1 hash doesn't match — a peer can send
    whatever bytes it likes, so this check is what makes it safe to trust
    the result.
    """
    expected_count = piece_count(metadata_size)
    missing = [i for i in range(expected_count) if i not in pieces]
    if missing:
        raise MetadataExchangeError(f"missing metadata piece(s): {missing}")

    assembled = b"".join(pieces[i] for i in range(expected_count))
    if len(assembled) != metadata_size:
        raise MetadataExchangeError(
            f"assembled metadata size {len(assembled)} does not match expected {metadata_size}"
        )

    digest = hashlib.sha1(assembled).digest()
    if digest != info_hash:
        raise MetadataExchangeError("assembled metadata does not match the requested info hash")

    try:
        info_dict = decode(assembled)
    except BencodeDecodeError as exc:
        raise MetadataExchangeError(f"metadata is not valid bencode: {exc}") from exc
    if not isinstance(info_dict, dict):
        raise MetadataExchangeError("metadata did not decode to a dict")

    return info_dict


async def fetch_metadata_from_peer(
    connection: "PeerConnection",
    info_hash: bytes,
    *,
    timeout: float = DEFAULT_METADATA_TIMEOUT,
) -> dict:
    """Negotiate the extension protocol and fetch the info dict from one peer.

    Raises ``MetadataExchangeError`` if the peer doesn't support
    ut_metadata, doesn't know the metadata size itself yet, rejects our
    requests, or the assembled result doesn't verify.
    """
    if not connection.supports_extension_protocol:
        raise MetadataExchangeError(f"peer {connection.address} does not support the extension protocol")

    await connection.send(
        build_extended_handshake(supported_extensions={EXTENSION_NAME: LOCAL_UT_METADATA_ID})
    )

    peer_handshake = await _await_peer_extended_handshake(connection, timeout)

    if EXTENSION_NAME not in peer_handshake.supported_extensions:
        raise MetadataExchangeError(f"peer {connection.address} does not support ut_metadata")
    if peer_handshake.metadata_size is None:
        raise MetadataExchangeError(f"peer {connection.address} does not know the metadata size yet")

    peer_ut_metadata_id = peer_handshake.supported_extensions[EXTENSION_NAME]
    metadata_size = peer_handshake.metadata_size
    total_pieces = piece_count(metadata_size)

    pieces: dict[int, bytes] = {}
    for index in range(total_pieces):
        await connection.send(build_request(index, peer_extended_message_id=peer_ut_metadata_id))
        pieces[index] = await _await_metadata_piece(connection, index, timeout)

    return assemble_and_verify(pieces, metadata_size, info_hash)


async def _await_peer_extended_handshake(connection: "PeerConnection", timeout: float):
    async def _read_until_handshake():
        while True:
            message = await connection.receive()
            if isinstance(message, Extended) and message.extended_message_id == EXTENDED_HANDSHAKE_ID:
                return parse_extended_handshake(message)
            # Anything else (choke/have/bitfield/...) arriving first is
            # ignored — it's not relevant to fetching metadata.

    try:
        return await asyncio.wait_for(_read_until_handshake(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise MetadataExchangeError(
            f"timed out waiting for extended handshake from {connection.address}"
        ) from exc


async def _await_metadata_piece(connection: "PeerConnection", expected_index: int, timeout: float) -> bytes:
    async def _read_until_piece():
        while True:
            message = await connection.receive()
            if not isinstance(message, Extended) or message.extended_message_id != LOCAL_UT_METADATA_ID:
                continue
            parsed = parse_metadata_message(message)
            if parsed.piece != expected_index:
                continue
            if parsed.msg_type == MSG_TYPE_REJECT:
                raise MetadataExchangeError(
                    f"peer {connection.address} rejected metadata piece {expected_index}"
                )
            if parsed.msg_type == MSG_TYPE_DATA:
                return parsed.data
            # Anything else addressed to our piece index is ignored.

    try:
        return await asyncio.wait_for(_read_until_piece(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise MetadataExchangeError(
            f"timed out waiting for metadata piece {expected_index} from {connection.address}"
        ) from exc
