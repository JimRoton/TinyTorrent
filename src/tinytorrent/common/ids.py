"""ID generation: short torrent ids (for CLI reference) and BitTorrent peer ids."""

from __future__ import annotations

import os
import secrets
import string

_TORRENT_ID_ALPHABET = string.ascii_lowercase + string.digits
_TORRENT_ID_LENGTH = 4

CLIENT_ID = "TT"  # arbitrary two-letter client code, Azureus-style
CLIENT_VERSION = "0001"


def generate_torrent_id(existing: set[str]) -> str:
    """Generate a short id (e.g. 'a3f9') not already in ``existing``.

    Retries on collision — with a 4-character base-36 id (~1.7M values)
    this essentially never loops more than once in practice.
    """
    while True:
        candidate = "".join(secrets.choice(_TORRENT_ID_ALPHABET) for _ in range(_TORRENT_ID_LENGTH))
        if candidate not in existing:
            return candidate


def generate_peer_id() -> bytes:
    """Generate a 20-byte peer id in the common Azureus-style format.

    Format: ``-<2-letter client id><4-digit version>-`` followed by 12
    random bytes, e.g. ``-TT0001-`` + 12 random bytes = 20 bytes total.
    """
    prefix = f"-{CLIENT_ID}{CLIENT_VERSION}-".encode("ascii")
    assert len(prefix) == 8
    return prefix + os.urandom(12)
