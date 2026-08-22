"""BEP 3 HTTP(S) tracker client.

Uses the stdlib ``urllib`` (run off the event loop via ``asyncio.to_thread``)
rather than hand-rolling HTTP/1.1 parsing — HTTP itself isn't part of the
BitTorrent protocol, so there's no reason to reimplement chunked transfer
encoding and friends from scratch. Everything BitTorrent-specific (the
announce query, the bencoded response, compact peer parsing) is ours.
"""

from __future__ import annotations

import asyncio
import ipaddress
import struct
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import quote_from_bytes

from tinytorrent.daemon.bencode import BencodeDecodeError, decode

DEFAULT_TIMEOUT = 15.0  # seconds
DEFAULT_NUMWANT = 50
USER_AGENT = "TinyTorrent/0.1"


class TrackerError(Exception):
    """Raised when a tracker announce fails or returns a bad response."""


@dataclass(frozen=True)
class AnnounceResult:
    peers: tuple[tuple[str, int], ...]
    interval: int


async def announce(
    announce_url: str,
    *,
    info_hash: bytes,
    peer_id: bytes,
    port: int,
    uploaded: int,
    downloaded: int,
    left: int,
    event: str | None = None,
    numwant: int = DEFAULT_NUMWANT,
    timeout: float = DEFAULT_TIMEOUT,
) -> AnnounceResult:
    """Perform a single BEP 3 HTTP(S) tracker announce."""
    url = _build_url(
        announce_url,
        _build_query(
            info_hash=info_hash,
            peer_id=peer_id,
            port=port,
            uploaded=uploaded,
            downloaded=downloaded,
            left=left,
            event=event,
            numwant=numwant,
        ),
    )

    try:
        body = await asyncio.to_thread(_fetch, url, timeout)
    except urllib.error.URLError as exc:
        raise TrackerError(f"could not reach tracker {announce_url}: {exc}") from exc
    except TimeoutError as exc:
        raise TrackerError(f"tracker {announce_url} timed out") from exc

    return _parse_response(announce_url, body)


def _build_query(
    *,
    info_hash: bytes,
    peer_id: bytes,
    port: int,
    uploaded: int,
    downloaded: int,
    left: int,
    event: str | None,
    numwant: int,
) -> str:
    if len(info_hash) != 20:
        raise TrackerError(f"info_hash must be 20 bytes, got {len(info_hash)}")
    if len(peer_id) != 20:
        raise TrackerError(f"peer_id must be 20 bytes, got {len(peer_id)}")

    params = [
        f"info_hash={quote_from_bytes(info_hash, safe='')}",
        f"peer_id={quote_from_bytes(peer_id, safe='')}",
        f"port={port}",
        f"uploaded={uploaded}",
        f"downloaded={downloaded}",
        f"left={left}",
        "compact=1",
        f"numwant={numwant}",
    ]
    if event is not None:
        params.append(f"event={event}")
    return "&".join(params)


def _build_url(announce_url: str, query: str) -> str:
    separator = "&" if "?" in announce_url else "?"
    return f"{announce_url}{separator}{query}"


def _fetch(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _parse_response(announce_url: str, body: bytes) -> AnnounceResult:
    try:
        response = decode(body)
    except BencodeDecodeError as exc:
        raise TrackerError(f"tracker {announce_url} returned malformed bencode: {exc}") from exc

    if not isinstance(response, dict):
        raise TrackerError(f"tracker {announce_url} response was not a bencoded dict")

    failure = response.get(b"failure reason")
    if failure is not None:
        reason = failure.decode("utf-8", errors="replace") if isinstance(failure, bytes) else failure
        raise TrackerError(f"tracker {announce_url} returned failure: {reason}")

    interval = response.get(b"interval", 1800)
    peers = _parse_peers(announce_url, response.get(b"peers", b""))

    return AnnounceResult(peers=peers, interval=int(interval))


def _parse_peers(announce_url: str, peers_field) -> tuple[tuple[str, int], ...]:
    if isinstance(peers_field, bytes):
        return _parse_compact_peers(announce_url, peers_field)
    if isinstance(peers_field, list):
        return _parse_dict_peers(peers_field)
    raise TrackerError(
        f"tracker {announce_url} 'peers' field has unrecognized type {type(peers_field).__name__}"
    )


def _parse_compact_peers(announce_url: str, data: bytes) -> tuple[tuple[str, int], ...]:
    if len(data) % 6 != 0:
        raise TrackerError(
            f"tracker {announce_url} compact peers field length {len(data)} is not a multiple of 6"
        )
    peers = []
    for i in range(0, len(data), 6):
        chunk = data[i:i + 6]
        ip = str(ipaddress.IPv4Address(chunk[:4]))
        (port,) = struct.unpack("!H", chunk[4:6])
        peers.append((ip, port))
    return tuple(peers)


def _parse_dict_peers(entries: list) -> tuple[tuple[str, int], ...]:
    peers = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        ip = entry.get(b"ip")
        port = entry.get(b"port")
        if ip is None or port is None:
            continue
        ip_str = ip.decode("utf-8", errors="replace") if isinstance(ip, bytes) else str(ip)
        peers.append((ip_str, int(port)))
    return tuple(peers)
