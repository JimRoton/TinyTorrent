"""`tinytorrent`: the CLI client. Talks to `tinytorrentd` over its Unix socket."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tinytorrent.cli import ipc_client
from tinytorrent.cli.formatting import format_torrent_table
from tinytorrent.cli.ipc_client import DaemonUnreachableError
from tinytorrent.common.config import Config, ConfigError, DEFAULT_CONFIG_PATH

_PRIORITY_CHOICES = ["high", "normal", "low"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tinytorrent", description="A minimal command-line BitTorrent client.")
    parser.add_argument("--config", type=Path, default=None, help=f"config file path (default: {DEFAULT_CONFIG_PATH})")
    parser.add_argument("--socket", dest="socket_path", type=Path, default=None, help="daemon Unix socket path")

    subparsers = parser.add_subparsers(dest="command", required=True)

    add_p = subparsers.add_parser("add", help="add a torrent by link/magnet")
    add_p.add_argument("magnet", help="magnet URI")
    add_p.add_argument("--priority", choices=_PRIORITY_CHOICES, default="normal")

    purge_p = subparsers.add_parser("purge", help="remove a torrent, keeping any downloaded data")
    purge_p.add_argument("id")
    purge_p.add_argument("--with-data", action="store_true", help="also delete downloaded data")

    subparsers.add_parser("list", help="list torrents and their status")

    priority_p = subparsers.add_parser("priority", help="set a torrent's priority")
    priority_p.add_argument("id")
    priority_p.add_argument("level", choices=_PRIORITY_CHOICES)

    promote_p = subparsers.add_parser("promote", help="force a queued torrent into an active download slot")
    promote_p.add_argument("id")

    return parser


def main(argv: "list[str] | None" = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = Config.load(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    config = config.with_overrides(socket_path=args.socket_path)

    handlers = {
        "add": _cmd_add,
        "purge": _cmd_purge,
        "list": _cmd_list,
        "priority": _cmd_priority,
        "promote": _cmd_promote,
    }

    try:
        return handlers[args.command](config, args)
    except DaemonUnreachableError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("Is tinytorrentd running?", file=sys.stderr)
        return 1


def _cmd_add(config: Config, args: argparse.Namespace) -> int:
    response = ipc_client.call(config.socket_path, "add", {"magnet": args.magnet, "priority": args.priority})
    if not response.ok:
        print(f"error: {response.error}", file=sys.stderr)
        return 1
    print(f"added torrent {response.data['id']}")
    return 0


def _cmd_purge(config: Config, args: argparse.Namespace) -> int:
    response = ipc_client.call(config.socket_path, "purge", {"id": args.id, "with_data": args.with_data})
    if not response.ok:
        print(f"error: {response.error}", file=sys.stderr)
        return 1
    print(f"purged {args.id}" + (" (and deleted its data)" if args.with_data else ""))
    return 0


def _cmd_list(config: Config, _args: argparse.Namespace) -> int:
    response = ipc_client.call(config.socket_path, "list", {})
    if not response.ok:
        print(f"error: {response.error}", file=sys.stderr)
        return 1
    print(format_torrent_table(response.data.get("torrents", [])))
    return 0


def _cmd_priority(config: Config, args: argparse.Namespace) -> int:
    response = ipc_client.call(config.socket_path, "priority", {"id": args.id, "priority": args.level})
    if not response.ok:
        print(f"error: {response.error}", file=sys.stderr)
        return 1
    print(f"set {args.id} priority to {args.level}")
    return 0


def _cmd_promote(config: Config, args: argparse.Namespace) -> int:
    response = ipc_client.call(config.socket_path, "promote", {"id": args.id})
    if not response.ok:
        print(f"error: {response.error}", file=sys.stderr)
        return 1
    print(f"promoted {args.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
