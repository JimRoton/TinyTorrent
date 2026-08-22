"""Tracker announce dispatch — picks the right client for an announce URL."""

from __future__ import annotations

from urllib.parse import urlsplit

from tinytorrent.daemon.tracker.http_tracker import AnnounceResult, TrackerError
from tinytorrent.daemon.tracker.http_tracker import announce as _http_announce
from tinytorrent.daemon.tracker.udp_tracker import announce as _udp_announce

__all__ = ["AnnounceResult", "TrackerError", "announce"]


async def announce(announce_url: str, **kwargs) -> AnnounceResult:
    """Announce to a single tracker, dispatching on the URL scheme."""
    scheme = urlsplit(announce_url).scheme.lower()
    if scheme in ("http", "https"):
        return await _http_announce(announce_url, **kwargs)
    if scheme == "udp":
        return await _udp_announce(announce_url, **kwargs)
    raise TrackerError(f"unsupported tracker scheme {scheme!r} in {announce_url!r}")
