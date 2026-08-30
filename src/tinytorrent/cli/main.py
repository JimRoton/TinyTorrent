"""`tinytorrent`: the CLI client. Talks to `tinytorrentd` over its Unix socket."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tinytorrent.cli import ipc_client
from tinytorrent.cli.formatting import format_hook_test_results, format_torrent_table
from tinytorrent.cli.ipc_client import DaemonUnreachableError
from tinytorrent.common.config import Config, ConfigError, DEFAULT_CONFIG_PATH
from tinytorrent.common.hooks import DAEMON_EVENTS, HookEvent

_PRIORITY_CHOICES = ["high", "normal", "low"]
_HOOK_EVENT_CHOICES = [e.value for e in HookEvent]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tinytorrent", description="A minimal command-line BitTorrent client.")
    parser.add_argument("--config", type=Path, default=None, help=f"config file path (default: {DEFAULT_CONFIG_PATH})")
    parser.add_argument("--socket", dest="socket_path", type=Path, default=None, help="daemon Unix socket path")
    parser.add_argument(
        "--timeout",
        dest="ipc_timeout_seconds",
        type=float,
        default=None,
        help="seconds to wait for tinytorrentd to respond before giving up (default: 15)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    add_p = subparsers.add_parser("add", help="add a torrent by link/magnet")
    add_p.add_argument("magnet", help="magnet URI")
    add_p.add_argument("--priority", choices=_PRIORITY_CHOICES, default="normal")

    purge_p = subparsers.add_parser("purge", help="remove a torrent, keeping any downloaded data")
    # Optional so `--errors` can stand in for it; exactly one of the two
    # is required, which is checked in the handler so the error message
    # can say something more useful than argparse's.
    purge_p.add_argument("id", nargs="?", default=None)
    purge_p.add_argument(
        "--errors",
        action="store_true",
        help="purge every torrent in the error state instead of one by id",
    )
    purge_p.add_argument("--with-data", action="store_true", help="also delete downloaded data")

    subparsers.add_parser("list", help="list torrents and their status")

    priority_p = subparsers.add_parser("priority", help="set a torrent's priority")
    priority_p.add_argument("id")
    priority_p.add_argument("level", choices=_PRIORITY_CHOICES)

    promote_p = subparsers.add_parser("promote", help="force a queued torrent into an active download slot")
    promote_p.add_argument("id")

    pause_p = subparsers.add_parser("pause", help="pause a torrent's download")
    pause_p.add_argument("id")

    resume_p = subparsers.add_parser("resume", help="resume a paused torrent")
    resume_p.add_argument("id")

    test_p = subparsers.add_parser(
        "test", help="run an event's configured hook commands against a real torrent and show the results"
    )
    test_p.add_argument("--event", required=True, choices=_HOOK_EVENT_CHOICES, help="event to test")
    test_p.add_argument(
        "--id", default=None, help="torrent id to test against (not used by the daemon_* events)"
    )
    test_p.add_argument(
        "--name",
        default=None,
        help="torrent name to test against (tried if --id is omitted, or not found and --name is also given)",
    )
    test_p.add_argument(
        "--deleted-data",
        action="store_true",
        help="simulate %%deleted_data%% as true (only meaningful for --event torrent_purged)",
    )

    return parser


def main(argv: "list[str] | None" = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = Config.load(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    config = config.with_overrides(socket_path=args.socket_path, ipc_timeout_seconds=args.ipc_timeout_seconds)

    handlers = {
        "add": _cmd_add,
        "purge": _cmd_purge,
        "list": _cmd_list,
        "priority": _cmd_priority,
        "promote": _cmd_promote,
        "pause": _cmd_pause,
        "resume": _cmd_resume,
        "test": _cmd_test,
    }

    try:
        return handlers[args.command](config, args)
    except DaemonUnreachableError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("Is tinytorrentd running?", file=sys.stderr)
        return 1


def _cmd_add(config: Config, args: argparse.Namespace) -> int:
    response = ipc_client.call(
        config.socket_path,
        "add",
        {"magnet": args.magnet, "priority": args.priority},
        timeout=config.ipc_timeout_seconds,
    )
    if not response.ok:
        print(f"error: {response.error}", file=sys.stderr)
        return 1
    print(f"added torrent {response.data['id']}")
    return 0


def _cmd_purge(config: Config, args: argparse.Namespace) -> int:
    if args.errors and args.id:
        print("error: give an id or --errors, not both", file=sys.stderr)
        return 1
    if not args.errors and not args.id:
        print("error: must provide a torrent id, or --errors", file=sys.stderr)
        return 1

    response = ipc_client.call(
        config.socket_path,
        "purge",
        {"id": args.id, "errors": args.errors, "with_data": args.with_data},
        timeout=config.ipc_timeout_seconds,
    )
    if not response.ok:
        print(f"error: {response.error}", file=sys.stderr)
        return 1

    suffix = " (and deleted its data)" if args.with_data else ""
    if not args.errors:
        print(f"purged {args.id}{suffix}")
        return 0

    purged = response.data.get("purged", [])
    if not purged:
        print("no torrents in the error state")
        return 0
    plural = "torrent" if len(purged) == 1 else "torrents"
    data_suffix = " (and deleted their data)" if args.with_data else ""
    print(f"purged {len(purged)} errored {plural}{data_suffix}: {', '.join(purged)}")
    return 0


def _cmd_list(config: Config, _args: argparse.Namespace) -> int:
    response = ipc_client.call(config.socket_path, "list", {}, timeout=config.ipc_timeout_seconds)
    if not response.ok:
        print(f"error: {response.error}", file=sys.stderr)
        return 1
    print(format_torrent_table(response.data.get("torrents", [])))
    return 0


def _cmd_priority(config: Config, args: argparse.Namespace) -> int:
    response = ipc_client.call(
        config.socket_path, "priority", {"id": args.id, "priority": args.level}, timeout=config.ipc_timeout_seconds
    )
    if not response.ok:
        print(f"error: {response.error}", file=sys.stderr)
        return 1
    print(f"set {args.id} priority to {args.level}")
    return 0


def _cmd_promote(config: Config, args: argparse.Namespace) -> int:
    response = ipc_client.call(config.socket_path, "promote", {"id": args.id}, timeout=config.ipc_timeout_seconds)
    if not response.ok:
        print(f"error: {response.error}", file=sys.stderr)
        return 1
    print(f"promoted {args.id}")
    return 0


def _cmd_pause(config: Config, args: argparse.Namespace) -> int:
    response = ipc_client.call(config.socket_path, "pause", {"id": args.id}, timeout=config.ipc_timeout_seconds)
    if not response.ok:
        print(f"error: {response.error}", file=sys.stderr)
        return 1
    print(f"paused {args.id}")
    return 0


def _cmd_resume(config: Config, args: argparse.Namespace) -> int:
    response = ipc_client.call(config.socket_path, "resume", {"id": args.id}, timeout=config.ipc_timeout_seconds)
    if not response.ok:
        print(f"error: {response.error}", file=sys.stderr)
        return 1
    print(f"resumed {args.id}")
    return 0


def _cmd_test(config: Config, args: argparse.Namespace) -> int:
    # The daemon-wide events describe the daemon rather than a torrent,
    # so there is nothing to match against for those.
    needs_torrent = HookEvent(args.event) not in DAEMON_EVENTS
    if needs_torrent and not args.id and not args.name:
        print("error: must provide --id or --name", file=sys.stderr)
        return 1
    response = ipc_client.call(
        config.socket_path,
        "test",
        {"event": args.event, "id": args.id, "name": args.name, "deleted_data": args.deleted_data},
        timeout=config.ipc_timeout_seconds,
    )
    if not response.ok:
        print(f"error: {response.error}", file=sys.stderr)
        return 1
    print(
        format_hook_test_results(
            args.event, response.data["torrent_id"], response.data["torrent_name"], response.data
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
