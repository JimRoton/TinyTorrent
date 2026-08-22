# TinyTorrent

A minimal, from-scratch command-line BitTorrent client for Linux. Downloads
only — no seeding, no peer browsing, no bandwidth limits. See `DESIGN.md`
for the full feature and scheduling design.

## Layout

- `src/tinytorrent/daemon/` — `tinytorrentd`, the background daemon that
  owns all torrent state and does all the downloading.
- `src/tinytorrent/cli/` — `tinytorrent`, the CLI client that talks to the
  daemon over a Unix domain socket.
- `src/tinytorrent/common/` — code shared between daemon and CLI (IPC
  message schema, ID generation).
- `tests/` — unit tests (pytest).
- `systemd/` — the `tinytorrentd.service` unit file.

## Development setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

## Installing the daemon as a systemd (user) service

```bash
pip install .                                    # or: pip install -e . for a dev install
mkdir -p ~/.config/systemd/user
cp systemd/tinytorrentd.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now tinytorrentd
```

`tinytorrentd` runs entirely under systemd's control — there's no
`tinytorrent daemon start/stop` command. Use `systemctl --user
{start,stop,restart,status}` tinytorrentd instead, and `journalctl
--user -u tinytorrentd` to see its logs.

## Configuration

All settings live in `~/.config/tinytorrent/config.json` (optional — every
setting has a built-in default) and can also be overridden per-invocation
with CLI flags, which take precedence over the file:

```json
{
  "download_dir": "/home/you/Downloads/tinytorrent",
  "state_file": "/home/you/.local/state/tinytorrent/state.json",
  "socket_path": "/run/user/1000/tinytorrentd.sock",
  "max_active": 4
}
```

## CLI usage

```bash
tinytorrent add "magnet:?xt=urn:btih:...&dn=..." [--priority high|normal|low]
tinytorrent list
tinytorrent priority <id> <high|normal|low>
tinytorrent promote <id>
tinytorrent purge <id> [--with-data]
```
