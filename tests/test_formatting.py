from tinytorrent.cli.formatting import (
    format_bytes,
    format_eta,
    format_hook_test_results,
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


def _result(
    argv=("cp", "a", "b"),
    outcome="ok",
    returncode=0,
    output="",
    on_failure="ignore",
    would_run_in_production=True,
    interval_seconds=None,
):
    return {
        "argv": list(argv),
        "outcome": outcome,
        "returncode": returncode,
        "output": output,
        "on_failure": on_failure,
        "interval_seconds": interval_seconds,
        "would_run_in_production": would_run_in_production,
    }


def _response(results, configured=True):
    return {"configured": configured, "results": results}


class TestFormatHookTestResults:
    def test_header_names_event_and_torrent(self):
        text = format_hook_test_results(
            "download_completed", "a3f9", "ubuntu.iso", _response([_result()])
        )
        assert "download_completed" in text
        assert "a3f9" in text
        assert "ubuntu.iso" in text

    def test_no_hooks_configured(self):
        text = format_hook_test_results(
            "download_error", "a3f9", "ubuntu.iso", _response([], configured=False)
        )
        assert "No hooks configured" in text

    def test_lists_each_command_with_a_counter(self):
        text = format_hook_test_results(
            "download_completed",
            "a3f9",
            "demo",
            _response([_result(argv=("first",)), _result(argv=("second",))]),
        )
        assert "[1/2] first" in text
        assert "[2/2] second" in text

    def test_shows_the_substituted_argv(self):
        text = format_hook_test_results(
            "download_completed", "a3f9", "demo", _response([_result(argv=("cp", "/dl/demo", "/done"))])
        )
        assert "cp /dl/demo /done" in text

    def test_ok_outcome(self):
        text = format_hook_test_results("download_completed", "a3f9", "demo", _response([_result()]))
        assert "ok" in text
        assert "exit 0" in text

    def test_failed_outcome(self):
        text = format_hook_test_results(
            "download_completed", "a3f9", "demo", _response([_result(outcome="failed", returncode=3)])
        )
        assert "FAILED" in text
        assert "exit 3" in text

    def test_timeout_outcome(self):
        text = format_hook_test_results(
            "download_completed",
            "a3f9",
            "demo",
            _response([_result(outcome="timeout", returncode=None)]),
        )
        assert "TIMED OUT" in text
        assert "exit" not in text

    def test_start_error_outcome(self):
        text = format_hook_test_results(
            "download_completed",
            "a3f9",
            "demo",
            _response([_result(outcome="start_error", returncode=None)]),
        )
        assert "COULD NOT START" in text

    def test_includes_command_output(self):
        text = format_hook_test_results(
            "download_completed", "a3f9", "demo", _response([_result(output="hello from hook")])
        )
        assert "hello from hook" in text

    def test_multiline_output_is_indented_per_line(self):
        text = format_hook_test_results(
            "download_completed", "a3f9", "demo", _response([_result(output="line one\nline two")])
        )
        assert "| line one" in text
        assert "| line two" in text

    def test_flags_commands_production_would_have_skipped(self):
        text = format_hook_test_results(
            "download_completed",
            "a3f9",
            "demo",
            _response(
                [
                    _result(argv=("first",), outcome="failed", returncode=1, on_failure="abort_remaining"),
                    _result(argv=("second",), would_run_in_production=False),
                ]
            ),
        )
        assert "would NOT have run in production" in text

    def test_does_not_flag_commands_production_would_have_run(self):
        text = format_hook_test_results(
            "download_completed", "a3f9", "demo", _response([_result(), _result()])
        )
        assert "would NOT have run" not in text

    def test_ends_with_a_single_newline(self):
        text = format_hook_test_results("download_completed", "a3f9", "demo", _response([_result()]))
        assert text.endswith("\n")
        assert not text.endswith("\n\n")


class TestFormatHookTestResultsForDaemonEvents:
    def test_header_when_there_is_no_torrent(self):
        text = format_hook_test_results("daemon_started", None, None, _response([_result()]))
        assert "daemon-wide" in text
        assert "against torrent" not in text

    def test_no_hooks_configured_without_a_torrent(self):
        text = format_hook_test_results(
            "daemon_stopping", None, None, _response([], configured=False)
        )
        assert "No hooks configured" in text

    def test_shows_the_repeat_cadence(self):
        text = format_hook_test_results(
            "daemon_started", None, None, _response([_result(interval_seconds=60)])
        )
        assert "repeats: every 1m" in text
        assert "run once here" in text

    def test_no_cadence_line_for_one_shot_commands(self):
        text = format_hook_test_results("daemon_started", None, None, _response([_result()]))
        assert "repeats:" not in text

    def test_interval_formatting(self):
        for seconds, expected in [(30, "30s"), (60, "1m"), (90, "1m30s"), (3600, "1h"), (5400, "1h30m")]:
            text = format_hook_test_results(
                "daemon_started", None, None, _response([_result(interval_seconds=seconds)])
            )
            assert f"every {expected} " in text, f"{seconds}s rendered wrong: {text}"
