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
  message schema, ID generation, event hook schema).
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
  "max_active": 4,
  "ipc_timeout_seconds": 15
}
```

`ipc_timeout_seconds` (default 15) is how long the CLI waits for
`tinytorrentd` to respond to one request — connecting to the socket and
reading the response line — before giving up with "tinytorrentd did not
respond within Ns". It's unrelated to a hook command's own
`timeout_seconds` in the `hooks` config below, which bounds a hook
subprocess instead. Override it per-invocation with `tinytorrent --timeout
<seconds> ...` if a slow daemon or a long-running `tinytorrent test` needs
more room.

## Event hooks

TinyTorrent can run configured commands when a torrent reaches certain
points in its lifecycle — for example, copying a finished download
somewhere else. Hooks are config-file only (`hooks` in `config.json`);
there's no CLI flag for them, since a list of commands per event doesn't
map cleanly onto flags. See `DESIGN.md` for the full design and the
reasoning behind each choice below.

```json
{
  "hooks": {
    "download_completed": [
      {
        "command": ["cp", "-r", "%download_dir%/%name%", "/media/done/"],
        "on_failure": "ignore",
        "timeout_seconds": 60
      }
    ],
    "download_error": [
      {
        "command": ["notify-send", "TinyTorrent", "download failed: %name%"]
      }
    ]
  }
}
```

**Events**: `metadata_fetched`, `download_started`, `download_completed`,
`download_error`, `torrent_purged`.

**Placeholders**, filled in per-command from the triggering torrent:
`%id%`, `%name%`, `%status%`, `%download_dir%`, `%total_bytes%`,
`%priority%`, `%info_hash%`, and — for `torrent_purged` only —
`%deleted_data%` (`"true"`/`"false"`, whether `--with-data` was used). An
unrecognized `%placeholder%` is left as literal text.

**`command` is always an argv list, never a shell string.** Commands run
via `execve`-style process spawning, not through `/bin/sh`. This means
pipes, `&&`, and redirects don't work directly — write a small script and
invoke that if you need shell logic — but it also means a hostile
`%name%` (torrent names come from the network) can never be interpreted
as shell syntax. This was a deliberate security tradeoff; see DESIGN.md.

**`on_failure`** is `"ignore"` (default: log and continue to the next
configured command for this event) or `"abort_remaining"` (skip the rest
of this event's commands after a failure). It does not affect other
events. **`timeout_seconds`** (default 60) kills a hung command and
counts it as a failure.

**Hooks run fire-and-forget.** Firing an event never blocks the
scheduler or the torrent that triggered it, and the daemon does not wait
for in-flight hook commands to finish before shutting down — a command
still running when `tinytorrentd` stops (e.g. a systemd restart) is not
guaranteed to complete.

**Using `scp` in a hook**: `scp`/`ssh` password prompts can't work here —
there's no terminal attached to a hook's subprocess, and no one around to
answer a 3am prompt anyway. Set up key-based auth instead: generate a
passphrase-less keypair for TinyTorrent, add the public key to the
destination's `authorized_keys`, pre-populate `known_hosts` for that host
(`ssh-keyscan host >> ~/.ssh/known_hosts`), and always include `-o
BatchMode=yes` so an auth problem fails fast instead of hanging until
`timeout_seconds`:

```json
{"command": ["scp", "-i", "/home/you/.config/tinytorrent/hook_key", "-o", "BatchMode=yes",
             "-r", "%download_dir%/%name%", "user@host:/dest/"]}
```

### Testing a hook without waiting for the real event

`tinytorrent test` runs an event's configured commands right now, against
a real torrent's real data, and prints each command's result — so you can
check your `hooks` config works without waiting for a torrent to actually
finish (or fail, or get purged):

```bash
tinytorrent test --event download_completed --id a3f9
tinytorrent test --event download_error --name "ubuntu.iso"
tinytorrent test --event torrent_purged --id a3f9 --deleted-data
```

Match by `--id` (checked first) or `--name` (tried if `--id` is omitted,
or if it wasn't found and `--name` was also given; ambiguous name matches
are rejected — use `--id` instead). `--deleted-data` only matters for
`--event torrent_purged`, and only sets the `%deleted_data%` placeholder
— running this command never actually purges anything.

Unlike a real firing, `test` blocks and prints every configured command's
outcome (`ok` / `FAILED` / `TIMED OUT` / `COULD NOT START`), including
its output — and it runs **every** command for the event even after one
fails, regardless of `on_failure`, so you can see whether each one works.
A command that a real firing would have skipped (because an earlier one
failed with `on_failure: "abort_remaining"`) is still run and shown, but
flagged as "would NOT have run in production."

## Pause & resume

`tinytorrent pause <id>` stops a torrent's download and holds it out of
scheduling — unlike automatic preemption (a lower-priority torrent bumped
back to the queue), a paused torrent does **not** auto-restart when a
slot frees up. `tinytorrent resume <id>` puts it back into normal
priority-based contention for a slot. A pause is a deliberate choice, so
it persists across a `tinytorrentd` restart, just like priority does —
see DESIGN.md for the full reasoning.

## CLI usage

```bash
tinytorrent add "magnet:?xt=urn:btih:...&dn=..." [--priority high|normal|low]
tinytorrent list
tinytorrent priority <id> <high|normal|low>
tinytorrent promote <id>
tinytorrent pause <id>
tinytorrent resume <id>
tinytorrent purge <id> [--with-data]
tinytorrent test --event <event> [--id <id>] [--name <name>] [--deleted-data]
```
