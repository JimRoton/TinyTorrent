"""BEP 15 UDP tracker client.

Unlike the HTTP tracker, this protocol is small, binary, and BitTorrent-
specific, so it's implemented directly on raw UDP sockets (via asyncio's
``loop.sock_*`` helpers) rather than pulled in from a library: a connect
handshake to get a short-lived connection id, then an announce request
using that id.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
import struct
from urllib.parse import urlsplit

from tinytorrent.daemon.tracker.http_tracker import AnnounceResult, TrackerError

_PROTOCOL_MAGIC = 0x41727101980
_ACTION_CONNECT = 0
_ACTION_ANNOUNCE = 1
_ACTION_ERROR = 3

_EVENT_CODES = {None: 0, "completed": 1, "started": 2, "stopped": 3}

# Per BEP 15: timeout doubles each retry, starting at 15s, up to 8 retries.
# We cap retries lower in practice — a tracker that's unreachable after a
# few doublings isn't going to answer.
_INITIAL_TIMEOUT = 15.0
_MAX_RETRIES = 4


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
    numwant: int = 50,
) -> AnnounceResult:
    """Perform a BEP 15 UDP tracker announce (connect, then announce)."""
    if len(info_hash) != 20:
        raise TrackerError(f"info_hash must be 20 bytes, got {len(info_hash)}")
    if len(peer_id) != 20:
        raise TrackerError(f"peer_id must be 20 bytes, got {len(peer_id)}")
    if event is not None and event not in _EVENT_CODES:
        raise TrackerError(f"unsupported announce event {event!r}")

    parts = urlsplit(announce_url)
    if parts.scheme != "udp":
        raise TrackerError(f"not a udp:// tracker URL: {announce_url!r}")
    if not parts.hostname or not parts.port:
        raise TrackerError(f"udp tracker URL missing host/port: {announce_url!r}")

    loop = asyncio.get_running_loop()
    try:
        addr_info = await loop.getaddrinfo(parts.hostname, parts.port, type=socket.SOCK_DGRAM)
    except socket.gaierror as exc:
        raise TrackerError(f"could not resolve tracker host {parts.hostname!r}: {exc}") from exc
    if not addr_info:
        raise TrackerError(f"no addresses found for tracker host {parts.hostname!r}")
    family, socktype, proto, _canonname, sockaddr = addr_info[0]

    sock = socket.socket(family, socktype, proto)
    sock.setblocking(False)
    try:
        await loop.sock_connect(sock, sockaddr)
        connection_id = await _do_connect(loop, sock)
        return await _do_announce(
            loop,
            sock,
            connection_id,
            info_hash=info_hash,
            peer_id=peer_id,
            port=port,
            uploaded=uploaded,
            downloaded=downloaded,
            left=left,
            event=event,
            numwant=numwant,
        )
    finally:
        sock.close()


async def _send_and_receive(loop: asyncio.AbstractEventLoop, sock: socket.socket, payload: bytes) -> bytes:
    last_error: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        timeout = _INITIAL_TIMEOUT * (2 ** attempt)
        try:
            await loop.sock_sendall(sock, payload)
            return await asyncio.wait_for(loop.sock_recv(sock, 65507), timeout=timeout)
        except asyncio.TimeoutError as exc:
            last_error = exc
            continue
    raise TrackerError("udp tracker timed out after retries") from last_error


def _check_response(response: bytes, expected_action: int, expected_transaction_id: int) -> None:
    if len(response) < 8:
        raise TrackerError("udp tracker response too short")
    action, transaction_id = struct.unpack("!II", response[:8])
    if transaction_id != expected_transaction_id:
        raise TrackerError("udp tracker response transaction id mismatch")
    if action == _ACTION_ERROR:
        message = response[8:].decode("utf-8", errors="replace")
        raise TrackerError(f"udp tracker returned error: {message}")
    if action != expected_action:
        raise TrackerError(f"udp tracker returned unexpected action {action} (expected {expected_action})")


async def _do_connect(loop: asyncio.AbstractEventLoop, sock: socket.socket) -> int:
    transaction_id = int.from_bytes(os.urandom(4), "big")
    request = struct.pack("!qII", _PROTOCOL_MAGIC, _ACTION_CONNECT, transaction_id)
    response = await _send_and_receive(loop, sock, request)
    if len(response) < 16:
        raise TrackerError("udp tracker connect response too short")
    _check_response(response, _ACTION_CONNECT, transaction_id)
    (connection_id,) = struct.unpack("!q", response[8:16])
    return connection_id


async def _do_announce(
    loop: asyncio.AbstractEventLoop,
    sock: socket.socket,
    connection_id: int,
    *,
    info_hash: bytes,
    peer_id: bytes,
    port: int,
    uploaded: int,
    downloaded: int,
    left: int,
    event: str | None,
    numwant: int,
) -> AnnounceResult:
    transaction_id = int.from_bytes(os.urandom(4), "big")
    event_code = _EVENT_CODES[event]
    key = int.from_bytes(os.urandom(4), "big")

    request = struct.pack(
        "!qII20s20sqqqIIIiH",
        connection_id,
        _ACTION_ANNOUNCE,
        transaction_id,
        info_hash,
        peer_id,
        downloaded,
        left,
        uploaded,
        event_code,
        0,  # IP address: 0 = let the tracker use the packet's source address
        key,
        numwant,
        port,
    )
    response = await _send_and_receive(loop, sock, request)
    if len(response) < 20:
        raise TrackerError("udp tracker announce response too short")
    _check_response(response, _ACTION_ANNOUNCE, transaction_id)

    _action, _transaction_id, interval, _leechers, _seeders = struct.unpack("!IIIII", response[:20])
    peers_data = response[20:]
    if len(peers_data) % 6 != 0:
        raise TrackerError("udp tracker peers field length is not a multiple of 6")

    peers = []
    for i in range(0, len(peers_data), 6):
        chunk = peers_data[i:i + 6]
        ip = str(ipaddress.IPv4Address(chunk[:4]))
        (peer_port,) = struct.unpack("!H", chunk[4:6])
        peers.append((ip, peer_port))

    return AnnounceResult(peers=tuple(peers), interval=interval)
