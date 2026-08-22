from tinytorrent.cli.formatting import (
    format_bytes,
    format_eta,
    format_progress,
    format_speed,
    format_torrent_table,
)


class TestFormatBytes:
    def test_none(self):
        assert format_bytes(None) == "-"

    def test_bytes(self):
        assert format_bytes(500) == "500B"

    def test_kilobytes(self):
        assert format_bytes(2048) == "2.0KB"

    def test_megabytes(self):
        assert format_bytes(5 * 1024 * 1024) == "5.0MB"

    def test_gigabytes(self):
        assert format_bytes(3 * 1024 ** 3) == "3.0GB"

    def test_zero(self):
        assert format_bytes(0) == "0B"


class TestFormatSpeed:
    def test_none(self):
        assert format_speed(None) == "-"

    def test_zero(self):
        assert format_speed(0) == "-"

    def test_negative(self):
        assert format_speed(-5) == "-"

    def test_positive(self):
        assert format_speed(1024) == "1.0KB/s"


class TestFormatEta:
    def test_none(self):
        assert format_eta(None) == "-"

    def test_seconds_only(self):
        assert format_eta(45) == "45s"

    def test_minutes(self):
        assert format_eta(125) == "2m05s"

    def test_hours(self):
        assert format_eta(3725) == "1h02m"

    def test_days(self):
        assert format_eta(90000) == "1d01h"


class TestFormatProgress:
    def test_no_total_length(self):
        assert format_progress(0, None) == "-"
        assert format_progress(0, 0) == "-"

    def test_half_done(self):
        assert format_progress(50, 100) == "50.0%"

    def test_complete(self):
        assert format_progress(100, 100) == "100.0%"


class TestFormatTorrentTable:
    def test_empty(self):
        assert format_torrent_table([]) == "No torrents."

    def test_single_row_contains_all_fields(self):
        table = format_torrent_table(
            [
                {
                    "id": "a3f9",
                    "name": "ubuntu.iso",
                    "status": "downloading",
                    "priority": "high",
                    "bytes_downloaded": 512,
                    "total_length": 1024,
                    "download_rate_bps": 2048,
                    "eta_seconds": 30,
                }
            ]
        )
        lines = table.splitlines()
        assert len(lines) == 2  # header + one row
        assert "ID" in lines[0] and "NAME" in lines[0]
        assert "a3f9" in lines[1]
        assert "ubuntu.iso" in lines[1]
        assert "50.0%" in lines[1]
        assert "2.0KB/s" in lines[1]
        assert "30s" in lines[1]

    def test_columns_align(self):
        table = format_torrent_table(
            [
                {"id": "a", "name": "short", "status": "queued", "priority": "low", "bytes_downloaded": 0, "total_length": 0, "download_rate_bps": 0, "eta_seconds": None},
                {"id": "bbbbbbbb", "name": "a much longer name here", "status": "downloading", "priority": "high", "bytes_downloaded": 1, "total_length": 2, "download_rate_bps": 1, "eta_seconds": 1},
            ]
        )
        lines = table.splitlines()
        # Header and both rows should be the same length once padded.
        assert len(lines[0]) == len(lines[1]) == len(lines[2])
