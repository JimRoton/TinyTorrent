"""`tinytorrentd`: the background daemon entrypoint.

Wires config, ``DaemonManager``, and the IPC server together and runs
until stopped (SIGTERM/SIGINT — the signals systemd sends on stop/restart).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from pathlib import Path

from tinytorrent.common.config import Config, ConfigError, DEFAULT_CONFIG_PATH
from tinytorrent.common.ids import generate_peer_id
from tinytorrent.daemon.ipc_server import IPCServer
from tinytorrent.daemon.manager import DaemonManager

logger = logging.getLogger(__name__)


def _parse_args(argv: "list[str] | None" = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="tinytorrentd", description="TinyTorrent background daemon.")
    parser.add_argument("--config", type=Path, default=None, help=f"config file path (default: {DEFAULT_CONFIG_PATH})")
    parser.add_argument("--download-dir", type=Path, default=None, help="where downloaded files are saved")
    parser.add_argument("--state-file", type=Path, default=None, help="where torrent state is persisted")
    parser.add_argument("--socket", dest="socket_path", type=Path, default=None, help="Unix socket path to listen on")
    parser.add_argument("--max-active", type=int, default=None, help="max number of concurrently downloading torrents")
    parser.add_argument("-v", "--verbose", action="store_true", help="enable debug logging")
    return parser.parse_args(argv)


def main(argv: "list[str] | None" = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        config = Config.load(args.config)
    except ConfigError as exc:
        logger.error("%s", exc)
        return 1

    config = config.with_overrides(
        download_dir=args.download_dir,
        state_file=args.state_file,
        socket_path=args.socket_path,
        max_active=args.max_active,
    )

    try:
        asyncio.run(_run(config))
    except KeyboardInterrupt:
        pass
    return 0


async def _run(config: Config) -> None:
    peer_id = generate_peer_id()
    manager = DaemonManager(
        config.download_dir,
        config.state_file,
        peer_id,
        max_active=config.max_active,
        hooks=config.hooks,
        socket_path=config.socket_path,
    )
    await manager.load_from_disk()

    server = IPCServer(manager, config.socket_path)
    await server.start()
    logger.info("tinytorrentd listening on %s", config.socket_path)

    # Fired only once the socket is up, so a hook may shell out to the
    # `tinytorrent` CLI. This also starts any repeating (interval_seconds)
    # commands the event configures.
    manager.fire_daemon_started()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass  # not available on some platforms; SIGINT still raises KeyboardInterrupt there

    serve_task = asyncio.ensure_future(server.serve_forever())
    try:
        await stop_event.wait()
    finally:
        serve_task.cancel()
        try:
            await serve_task
        except asyncio.CancelledError:
            pass
        logger.info("shutting down...")
        await manager.shutdown()
        await server.stop()


if __name__ == "__main__":
    raise SystemExit(main())
