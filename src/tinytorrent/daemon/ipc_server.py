"""Unix domain socket server exposing the daemon's commands to the CLI.

Each connection is handled independently and requests on a connection
are processed strictly one at a time, in order — a client that sends
several requests back-to-back on one connection gets responses back in
the same order, which is all the CLI (one request per invocation) needs,
while still allowing multiple CLI invocations to talk to the daemon at
once via separate connections.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

from tinytorrent.common.hooks import HookEvent
from tinytorrent.common.ipc_protocol import IPCError, Request, Response
from tinytorrent.common.priority import Priority
from tinytorrent.common.priority import from_str as priority_from_str
from tinytorrent.daemon.magnet import MagnetParseError
from tinytorrent.daemon.manager import DaemonManager
from tinytorrent.daemon.scheduler import SchedulerError
from tinytorrent.daemon.torrent_session import TorrentSession

logger = logging.getLogger(__name__)

Handler = Callable[[DaemonManager, "dict[str, Any]"], Awaitable[Response]]


class IPCServer:
    def __init__(self, manager: DaemonManager, socket_path: Path):
        self.manager = manager
        self.socket_path = Path(socket_path)
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()  # stale socket left behind by a previous (crashed) run
        self._server = await asyncio.start_unix_server(
            self._handle_connection, path=str(self.socket_path)
        )
        os.chmod(self.socket_path, 0o600)  # only this user may talk to the daemon

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self.socket_path.unlink(missing_ok=True)

    async def serve_forever(self) -> None:
        if self._server is None:
            raise RuntimeError("IPCServer.start() must be called before serve_forever()")
        async with self._server:
            await self._server.serve_forever()

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                try:
                    line = await reader.readline()
                except asyncio.LimitOverrunError:
                    await self._send(writer, Response.failure("request line too long"))
                    continue
                if not line:
                    return  # client closed the connection
                await self._handle_line(line, writer)
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    async def _handle_line(self, line: bytes, writer: asyncio.StreamWriter) -> None:
        try:
            request = Request.from_line(line)
        except IPCError as exc:
            await self._send(writer, Response.failure(str(exc)))
            return

        response = await self._dispatch(request)
        await self._send(writer, response)

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, response: Response) -> None:
        writer.write(response.to_line())
        await writer.drain()

    async def _dispatch(self, request: Request) -> Response:
        handler = _HANDLERS.get(request.cmd)
        if handler is None:
            return Response.failure(f"unknown command {request.cmd!r}")
        try:
            return await handler(self.manager, request.args)
        except (MagnetParseError, SchedulerError, ValueError) as exc:
            return Response.failure(str(exc))
        except Exception:
            logger.exception("unhandled error processing command %r", request.cmd)
            return Response.failure("internal error")


# -- command handlers -----------------------------------------------------


async def _handle_add(manager: DaemonManager, args: "dict[str, Any]") -> Response:
    magnet_uri = args.get("magnet")
    if not isinstance(magnet_uri, str) or not magnet_uri:
        raise ValueError("'magnet' is required")
    priority = _parse_priority(args.get("priority", "normal"))
    torrent_id = await manager.add_torrent(magnet_uri, priority=priority)
    return Response.success({"id": torrent_id})


async def _handle_purge(manager: DaemonManager, args: "dict[str, Any]") -> Response:
    with_data = bool(args.get("with_data", False))

    # "errors" purges every errored torrent instead of one by id. The two
    # are mutually exclusive; the CLI enforces that too, but the daemon
    # can be spoken to directly so it checks as well.
    if bool(args.get("errors", False)):
        if args.get("id"):
            raise ValueError("'id' and 'errors' cannot be combined")
        purged = await manager.purge_errored(with_data=with_data)
        return Response.success({"purged": purged})

    torrent_id = _require_str(args, "id")
    await manager.purge(torrent_id, with_data=with_data)
    return Response.success({"purged": [torrent_id]})


async def _handle_list(manager: DaemonManager, _args: "dict[str, Any]") -> Response:
    torrents = [_session_to_json(s) for s in manager.list_torrents()]
    return Response.success({"torrents": torrents})


async def _handle_priority(manager: DaemonManager, args: "dict[str, Any]") -> Response:
    torrent_id = _require_str(args, "id")
    priority = _parse_priority(args.get("priority"))
    await manager.set_priority(torrent_id, priority)
    return Response.success()


async def _handle_promote(manager: DaemonManager, args: "dict[str, Any]") -> Response:
    torrent_id = _require_str(args, "id")
    await manager.promote(torrent_id)
    return Response.success()


async def _handle_pause(manager: DaemonManager, args: "dict[str, Any]") -> Response:
    torrent_id = _require_str(args, "id")
    await manager.pause(torrent_id)
    return Response.success()


async def _handle_resume(manager: DaemonManager, args: "dict[str, Any]") -> Response:
    torrent_id = _require_str(args, "id")
    await manager.resume(torrent_id)
    return Response.success()


async def _handle_test(manager: DaemonManager, args: "dict[str, Any]") -> Response:
    event = _parse_hook_event(args.get("event"))
    torrent_id = args.get("id") or None
    name = args.get("name") or None
    if torrent_id is not None and not isinstance(torrent_id, str):
        raise ValueError("'id' must be a string")
    if name is not None and not isinstance(name, str):
        raise ValueError("'name' must be a string")
    deleted_data = bool(args.get("deleted_data", False))
    result = await manager.test_hook(event, torrent_id=torrent_id, name=name, deleted_data=deleted_data)
    return Response.success(result)


_HANDLERS: "dict[str, Handler]" = {
    "add": _handle_add,
    "purge": _handle_purge,
    "list": _handle_list,
    "priority": _handle_priority,
    "promote": _handle_promote,
    "pause": _handle_pause,
    "resume": _handle_resume,
    "test": _handle_test,
}


def _require_str(args: "dict[str, Any]", key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"'{key}' is required")
    return value


def _parse_priority(value: Any) -> Priority:
    if not isinstance(value, str):
        raise ValueError("'priority' must be a string")
    return priority_from_str(value)


def _parse_hook_event(value: Any) -> HookEvent:
    if not isinstance(value, str):
        raise ValueError("'event' must be a string")
    try:
        return HookEvent(value)
    except ValueError:
        valid = ", ".join(e.value for e in HookEvent)
        raise ValueError(f"invalid event {value!r} (expected one of: {valid})") from None


def _session_to_json(session: TorrentSession) -> "dict[str, Any]":
    progress = session.progress()
    return {
        "id": session.torrent_id,
        "name": session.display_name,
        "status": progress.status.value,
        "priority": session.priority.value,
        "bytes_downloaded": progress.bytes_downloaded,
        "total_length": progress.total_length,
        "download_rate_bps": progress.download_rate_bps,
        "eta_seconds": progress.eta_seconds,
        "num_pieces": progress.num_pieces,
        "completed_pieces": progress.completed_pieces,
        "error_message": progress.error_message,
    }
