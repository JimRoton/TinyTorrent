"""Magnet URI parsing (BEP 9).

TinyTorrent only accepts magnet/link input (no local .torrent files), so
this is the sole entry point for adding a torrent. Only BitTorrent v1
info hashes (``urn:btih:``) are supported; v2-only links (``urn:btmh:``)
are rejected with a clear error rather than silently failing later.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit


class MagnetParseError(Exception):
    """Raised when a string is not a valid, supported magnet URI."""


@dataclass(frozen=True)
class MagnetLink:
    info_hash: bytes  # 20 raw bytes (SHA-1), per BitTorrent v1
    display_name: str | None
    trackers: tuple[str, ...]

    @property
    def info_hash_hex(self) -> str:
        return self.info_hash.hex()


_HEX_40 = re.compile(r"^[0-9A-Fa-f]{40}$")
_BASE32_32 = re.compile(r"^[A-Za-z2-7]{32}$")

_BTIH_PREFIX = "urn:btih:"
_BTMH_PREFIX = "urn:btmh:"


def parse(uri: str) -> MagnetLink:
    """Parse a magnet URI into a ``MagnetLink``.

    Raises ``MagnetParseError`` on anything that isn't a well-formed
    magnet URI with a supported (v1) info hash.
    """
    uri = uri.strip()
    if not uri:
        raise MagnetParseError("empty magnet URI")

    parts = urlsplit(uri)
    if parts.scheme.lower() != "magnet":
        raise MagnetParseError(f"not a magnet URI (scheme is {parts.scheme!r})")

    if not parts.query:
        raise MagnetParseError("magnet URI has no parameters")

    # parse_qsl percent-decodes values for us.
    pairs = parse_qsl(parts.query, keep_blank_values=True)

    xt_values = [value for key, value in pairs if key == "xt"]
    display_name = next((value for key, value in pairs if key == "dn"), None)
    trackers = tuple(value for key, value in pairs if key == "tr")

    info_hash = _resolve_info_hash(xt_values)

    return MagnetLink(info_hash=info_hash, display_name=display_name, trackers=trackers)


def _resolve_info_hash(xt_values: list[str]) -> bytes:
    if not xt_values:
        raise MagnetParseError("magnet URI has no 'xt' (info hash) parameter")

    for value in xt_values:
        if value.lower().startswith(_BTIH_PREFIX):
            return _decode_btih(value[len(_BTIH_PREFIX):])

    if any(value.lower().startswith(_BTMH_PREFIX) for value in xt_values):
        raise MagnetParseError(
            "this magnet link is BitTorrent v2-only ('urn:btmh:'); "
            "only v1 ('urn:btih:') links are supported"
        )

    raise MagnetParseError(f"unsupported 'xt' parameter(s): {xt_values!r}")


def _decode_btih(raw: str) -> bytes:
    if _HEX_40.match(raw):
        return bytes.fromhex(raw)
    if _BASE32_32.match(raw):
        return base64.b32decode(raw.upper())
    raise MagnetParseError(
        f"invalid info hash {raw!r} (expected 40 hex characters or 32 base32 characters)"
    )
