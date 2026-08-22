"""IPC message schema shared between the daemon's socket server and the
CLI client.

Wire format: newline-delimited JSON over a Unix domain socket. Each
request is one JSON object per line; the daemon replies with one JSON
object per line, in the same order requests arrived on that connection.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

DEFAULT_SOCKET_PATH = "/run/tinytorrent/tinytorrentd.sock"


class IPCError(Exception):
    """A malformed request/response line, or a broken connection."""


@dataclass(frozen=True)
class Request:
    cmd: str
    args: dict[str, Any] = field(default_factory=dict)

    def to_line(self) -> bytes:
        return (json.dumps({"cmd": self.cmd, "args": self.args}) + "\n").encode("utf-8")

    @staticmethod
    def from_line(line: bytes) -> "Request":
        obj = _decode_json_line(line)
        if not isinstance(obj, dict) or "cmd" not in obj:
            raise IPCError(f"malformed request: {obj!r}")
        cmd = obj["cmd"]
        args = obj.get("args", {})
        if not isinstance(cmd, str) or not isinstance(args, dict):
            raise IPCError(f"malformed request: {obj!r}")
        return Request(cmd=cmd, args=args)


@dataclass(frozen=True)
class Response:
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_line(self) -> bytes:
        payload: dict[str, Any] = {"ok": self.ok}
        if self.ok:
            payload["data"] = self.data
        else:
            payload["error"] = self.error
        return (json.dumps(payload) + "\n").encode("utf-8")

    @staticmethod
    def from_line(line: bytes) -> "Response":
        obj = _decode_json_line(line)
        if not isinstance(obj, dict) or "ok" not in obj:
            raise IPCError(f"malformed response: {obj!r}")
        if obj["ok"]:
            data = obj.get("data", {})
            if not isinstance(data, dict):
                raise IPCError(f"malformed response data: {obj!r}")
            return Response(ok=True, data=data)
        return Response(ok=False, error=str(obj.get("error", "unknown error")))

    @staticmethod
    def success(data: "dict[str, Any] | None" = None) -> "Response":
        return Response(ok=True, data=data or {})

    @staticmethod
    def failure(error: str) -> "Response":
        return Response(ok=False, error=error)


def _decode_json_line(line: bytes) -> Any:
    try:
        return json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IPCError(f"malformed JSON line: {exc}") from exc
