"""Plain-text formatting for `tinytorrent list` output (no JSON mode, by design)."""

from __future__ import annotations

from typing import Any

_BYTE_UNITS = ["B", "KB", "MB", "GB", "TB", "PB"]

# Torrent names routinely run to release-scene lengths that wrap the
# terminal and wreck the column alignment. Only the display is
# shortened -- the torrent's real name is untouched, so the files on
# disk and every other command are unaffected.
NAME_DISPLAY_LIMIT = 72
_ELLIPSIS = "..."


def format_bytes(n: "int | None") -> str:
    if n is None:
        return "-"
    size = float(n)
    for unit in _BYTE_UNITS:
        if size < 1024 or unit == _BYTE_UNITS[-1]:
            return f"{int(size)}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}{_BYTE_UNITS[-1]}"  # pragma: no cover - unreachable in practice


def format_speed(bps: "float | None") -> str:
    if not bps or bps <= 0:
        return "-"
    return f"{format_bytes(int(bps))}/s"


def format_eta(seconds: "float | None") -> str:
    if seconds is None:
        return "-"
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    minutes, s = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m{s:02d}s"
    hours, m = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{m:02d}m"
    days, h = divmod(hours, 24)
    return f"{days}d{h:02d}h"


def format_progress(bytes_downloaded: "int | None", total_length: "int | None") -> str:
    if not total_length:
        return "-"
    pct = (bytes_downloaded / total_length) * 100
    return f"{pct:.1f}%"


def format_name(name: "str | None") -> str:
    """Shorten a torrent name for display only.

    Names longer than ``NAME_DISPLAY_LIMIT`` are cut to that many
    characters with an ellipsis appended, so the truncation is visible
    rather than silently losing the tail.
    """
    if not name:
        return "-"
    if len(name) <= NAME_DISPLAY_LIMIT:
        return name
    return name[:NAME_DISPLAY_LIMIT] + _ELLIPSIS


def format_torrent_table(torrents: "list[dict[str, Any]]") -> str:
    if not torrents:
        return "No torrents."

    headers = ["ID", "NAME", "STATUS", "PRIORITY", "PROGRESS", "SPEED", "ETA"]
    rows = [
        [
            t["id"],
            format_name(t["name"]),
            t["status"],
            t["priority"],
            format_progress(t.get("bytes_downloaded"), t.get("total_length")),
            format_speed(t.get("download_rate_bps")),
            format_eta(t.get("eta_seconds")),
        ]
        for t in torrents
    ]

    widths = [
        max(len(headers[i]), *(len(str(row[i])) for row in rows)) for i in range(len(headers))
    ]
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths))]
    lines += ["  ".join(str(c).ljust(w) for c, w in zip(row, widths)) for row in rows]
    return "\n".join(lines)


def _format_interval(seconds: float) -> str:
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    minutes, s = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m{s:02d}s" if s else f"{minutes}m"
    hours, m = divmod(minutes, 60)
    return f"{hours}h{m:02d}m" if m else f"{hours}h"


_OUTCOME_LABELS = {
    "ok": "ok",
    "failed": "FAILED",
    "timeout": "TIMED OUT",
    "start_error": "COULD NOT START",
}


def format_hook_test_results(
    event: str,
    torrent_id: "str | None",
    torrent_name: "str | None",
    response: "dict[str, Any]",
) -> str:
    """Format the result of `tinytorrent test` for display.

    ``torrent_id``/``torrent_name`` are None for the daemon-wide events,
    which have no torrent to run against.
    """
    if torrent_id is None:
        header = f"Testing event '{event}' (daemon-wide; no torrent involved)"
    else:
        header = f"Testing event '{event}' against torrent {torrent_id} ({torrent_name})"

    if not response.get("configured", False):
        return f"{header}\n\nNo hooks configured for event '{event}'. Nothing to run."

    results = response.get("results", [])
    total = len(results)
    lines = [header, ""]
    for i, result in enumerate(results, start=1):
        argv_display = " ".join(result["argv"])
        outcome_label = _OUTCOME_LABELS.get(result["outcome"], result["outcome"])
        detail = f" (exit {result['returncode']})" if result.get("returncode") is not None else ""
        lines.append(f"[{i}/{total}] {argv_display}")
        lines.append(f"        result: {outcome_label}{detail}")
        interval = result.get("interval_seconds")
        if interval:
            lines.append(
                f"        repeats: every {_format_interval(interval)} after each run finishes "
                "(run once here)"
            )
        if not result.get("would_run_in_production", True):
            lines.append(
                "        note: would NOT have run in production "
                "(an earlier command failed with on_failure=abort_remaining)"
            )
        output = result.get("output") or ""
        if output:
            indented = "\n".join(f"        | {line}" for line in output.splitlines())
            lines.append(indented)
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"
