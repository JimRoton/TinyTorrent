"""Client for talking to `tinytorrentd` over its Unix socket.

The CLI is a one-shot process — connect, send one request, read one
response, exit — so this wraps the necessary asyncio calls behind a
plain blocking function the CLI's command handlers can call directly.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from tinytorrent.common.ipc_protocol import Request, Response

DEFAULT_TIMEOUT = 15.0


class DaemonUnreachableError(Exception):
    """Raised when tinytorrentd's socket can't be reached, or doesn't respond in time."""


def call(
    socket_path: Path,
    cmd: str,
    args: "dict[str, Any] | None" = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Response:
    """Send one request to the daemon and return its response.

    Raises ``DaemonUnreachableError`` if the daemon isn't running, the
    socket can't be reached, or it doesn't respond within ``timeout``.
    """
    return asyncio.run(_call_async(Path(socket_path), cmd, args or {}, timeout))


async def _call_async(socket_path: Path, cmd: str, args: "dict[str, Any]", timeout: float) -> Response:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(path=str(socket_path)), timeout=timeout
        )
    except (OSError, asyncio.TimeoutError) as exc:
        raise DaemonUnreachableError(
            f"could not connect to tinytorrentd at {socket_path}: {exc}"
        ) from exc

    try:
        writer.write(Request(cmd=cmd, args=args).to_line())
        await writer.drain()
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise DaemonUnreachableError(f"tinytorrentd did not respond within {timeout}s") from exc
        if not line:
            raise DaemonUnreachableError("tinytorrentd closed the connection without responding")
        return Response.from_line(line)
    finally:
        writer.close()
