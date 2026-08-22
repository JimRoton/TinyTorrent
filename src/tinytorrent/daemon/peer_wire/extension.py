"""BEP 10 extension protocol: the base extended handshake.

Peers that set the extension-protocol reserved bit exchange an "extended
handshake" — an ``Extended`` message with extended-message-id 0, whose
payload is a bencoded dict advertising which named extensions they support
and the local message id each is mapped to. Extension-specific messages
(like BEP 9's ut_metadata, in ``metadata_exchange.py``) are built on top of
this negotiation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tinytorrent.daemon.bencode import BencodeDecodeError, decode, encode
from tinytorrent.daemon.peer_wire.messages import Extended, PeerProtocolError

EXTENDED_HANDSHAKE_ID = 0
CLIENT_VERSION = "TinyTorrent/0.1"


def build_extended_handshake(
    *, supported_extensions: dict[str, int], metadata_size: int | None = None
) -> Extended:
    """Build our own outgoing BEP 10 extended handshake message."""
    payload: dict[str, object] = {
        "m": dict(supported_extensions),
        "v": CLIENT_VERSION,
    }
    if metadata_size is not None:
        payload["metadata_size"] = metadata_size
    return Extended(extended_message_id=EXTENDED_HANDSHAKE_ID, payload=encode(payload))


@dataclass(frozen=True)
class ExtendedHandshake:
    supported_extensions: dict[str, int] = field(default_factory=dict)
    metadata_size: int | None = None
    client_version: str | None = None


def parse_extended_handshake(message: Extended) -> ExtendedHandshake:
    if message.extended_message_id != EXTENDED_HANDSHAKE_ID:
        raise PeerProtocolError(
            f"expected extended handshake (id {EXTENDED_HANDSHAKE_ID}), "
            f"got id {message.extended_message_id}"
        )

    try:
        payload = decode(message.payload)
    except BencodeDecodeError as exc:
        raise PeerProtocolError(f"malformed extended handshake payload: {exc}") from exc

    if not isinstance(payload, dict):
        raise PeerProtocolError("extended handshake payload was not a bencoded dict")

    supported_raw = payload.get(b"m", {})
    if not isinstance(supported_raw, dict):
        raise PeerProtocolError("extended handshake 'm' field was not a dict")
    supported = {
        key.decode("utf-8", errors="replace"): value
        for key, value in supported_raw.items()
        if isinstance(key, bytes) and isinstance(value, int)
    }

    metadata_size = payload.get(b"metadata_size")
    if not isinstance(metadata_size, int):
        metadata_size = None

    version = payload.get(b"v")
    client_version = version.decode("utf-8", errors="replace") if isinstance(version, bytes) else None

    return ExtendedHandshake(
        supported_extensions=supported,
        metadata_size=metadata_size,
        client_version=client_version,
    )
