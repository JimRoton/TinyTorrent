"""Bencode encoder/decoder (BEP 3).

Bencode is the simple serialization format BitTorrent uses for .torrent
metadata, tracker responses, and several extension-protocol messages. It
has four types:

    integers:      i<base-ten ascii digits>e
    byte strings:  <base-ten ascii length>:<bytes>
    lists:         l<bencoded values>e
    dictionaries:  d<bencoded byte string keys, sorted><bencoded values>e

Decoded byte strings and dict keys come back as ``bytes``, not ``str`` —
bencoded data mixes genuinely binary values (piece hashes, peer ids) with
text, so there's no safe universal encoding to decode into. Callers decode
the specific keys/values they know are text.
"""

from __future__ import annotations

from typing import Any, Union

BencodeValue = Union[int, bytes, list, dict]


class BencodeError(Exception):
    """Base class for bencode encode/decode errors."""


class BencodeEncodeError(BencodeError):
    pass


class BencodeDecodeError(BencodeError):
    pass


def encode(value: Any) -> bytes:
    """Encode a Python value (int, bytes, str, list, or dict) to bencode."""
    out = bytearray()
    _encode_into(value, out)
    return bytes(out)


def _encode_into(value: Any, out: bytearray) -> None:
    if isinstance(value, bool):
        # bool is a subclass of int; bencode has no boolean type, and
        # silently encoding True/False as 1/0 would hide bugs.
        raise BencodeEncodeError("bencode has no boolean type")
    if isinstance(value, int):
        out += b"i" + str(value).encode("ascii") + b"e"
    elif isinstance(value, (bytes, bytearray)):
        out += str(len(value)).encode("ascii") + b":" + bytes(value)
    elif isinstance(value, str):
        encoded = value.encode("utf-8")
        out += str(len(encoded)).encode("ascii") + b":" + encoded
    elif isinstance(value, list):
        out += b"l"
        for item in value:
            _encode_into(item, out)
        out += b"e"
    elif isinstance(value, dict):
        out += b"d"
        # Bencode dictionaries must have their keys sorted as raw byte
        # strings for the encoding to be canonical (required for reliable
        # info-hash computation).
        items = []
        for key in value:
            if isinstance(key, str):
                key_bytes = key.encode("utf-8")
            elif isinstance(key, (bytes, bytearray)):
                key_bytes = bytes(key)
            else:
                raise BencodeEncodeError(
                    f"dict keys must be str or bytes, got {type(key).__name__}"
                )
            items.append((key_bytes, value[key]))
        items.sort(key=lambda kv: kv[0])
        for key_bytes, item_value in items:
            _encode_into(key_bytes, out)
            _encode_into(item_value, out)
        out += b"e"
    else:
        raise BencodeEncodeError(f"cannot bencode value of type {type(value).__name__}")


def decode(data: bytes) -> BencodeValue:
    """Decode a single bencoded value from ``data``.

    Raises ``BencodeDecodeError`` if ``data`` is not exactly one valid
    bencoded value (trailing bytes are treated as an error, since callers
    that need to decode a value followed by other data should use
    ``decode_from`` directly).
    """
    value, offset = decode_from(data, 0)
    if offset != len(data):
        raise BencodeDecodeError(
            f"trailing data after bencoded value: {len(data) - offset} byte(s) left over"
        )
    return value


def decode_from(data: bytes, offset: int) -> tuple[BencodeValue, int]:
    """Decode a single bencoded value starting at ``offset``.

    Returns ``(value, next_offset)``. Used internally for recursive
    decoding and by callers (e.g. peer extension messages) that need to
    decode a bencoded prefix followed by raw binary data.
    """
    if offset >= len(data):
        raise BencodeDecodeError("unexpected end of data")

    marker = data[offset:offset + 1]

    if marker == b"i":
        return _decode_int(data, offset)
    if marker == b"l":
        return _decode_list(data, offset)
    if marker == b"d":
        return _decode_dict(data, offset)
    if marker.isdigit():
        return _decode_bytes(data, offset)

    raise BencodeDecodeError(f"invalid bencode marker {marker!r} at offset {offset}")


def _decode_int(data: bytes, offset: int) -> tuple[int, int]:
    assert data[offset:offset + 1] == b"i"
    end = data.find(b"e", offset)
    if end == -1:
        raise BencodeDecodeError("unterminated integer")
    raw = data[offset + 1:end]
    if raw == b"" or raw == b"-":
        raise BencodeDecodeError("empty integer")
    # Bencode forbids leading zeros (except "0" itself) and "-0".
    if raw != b"0" and (raw.startswith(b"0") or raw.startswith(b"-0")):
        raise BencodeDecodeError(f"invalid integer with leading zero: {raw!r}")
    try:
        return int(raw), end + 1
    except ValueError as exc:
        raise BencodeDecodeError(f"invalid integer {raw!r}") from exc


def _decode_bytes(data: bytes, offset: int) -> tuple[bytes, int]:
    colon = data.find(b":", offset)
    if colon == -1:
        raise BencodeDecodeError("unterminated byte string length")
    length_raw = data[offset:colon]
    if not length_raw.isdigit():
        raise BencodeDecodeError(f"invalid byte string length {length_raw!r}")
    length = int(length_raw)
    start = colon + 1
    end = start + length
    if end > len(data):
        raise BencodeDecodeError("byte string length exceeds available data")
    return data[start:end], end


def _decode_list(data: bytes, offset: int) -> tuple[list, int]:
    assert data[offset:offset + 1] == b"l"
    items: list = []
    pos = offset + 1
    while True:
        if pos >= len(data):
            raise BencodeDecodeError("unterminated list")
        if data[pos:pos + 1] == b"e":
            return items, pos + 1
        value, pos = decode_from(data, pos)
        items.append(value)


def _decode_dict(data: bytes, offset: int) -> tuple[dict, int]:
    assert data[offset:offset + 1] == b"d"
    result: dict = {}
    pos = offset + 1
    while True:
        if pos >= len(data):
            raise BencodeDecodeError("unterminated dict")
        if data[pos:pos + 1] == b"e":
            return result, pos + 1
        key, pos = decode_from(data, pos)
        if not isinstance(key, bytes):
            raise BencodeDecodeError(f"dict key must be a byte string, got {key!r}")
        value, pos = decode_from(data, pos)
        result[key] = value
