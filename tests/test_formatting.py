from tinytorrent.cli.formatting import (
    NAME_DISPLAY_LIMIT,
    display_width,
    format_bytes,
    format_eta,
    format_hook_test_results,
    format_name,
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
        # Each column starts at the same offset on every line. Total line
        # lengths differ, because the final column is deliberately left
        # unpadded rather than trailing spaces onto every row.
        for column in ("STATUS", "PRIORITY", "PROGRESS", "SPEED", "ETA"):
            header_offset = lines[0].index(column)
            for line in lines[1:]:
                assert len(line) > header_offset
                assert line[header_offset - 1] == " "


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


def _torrent(name, **overrides):
    row = {
        "id": "a3f9",
        "name": name,
        "status": "downloading",
        "priority": "normal",
        "bytes_downloaded": 5,
        "total_length": 10,
    }
    row.update(overrides)
    return row


class TestFormatName:
    def test_short_name_is_unchanged(self):
        assert format_name("ubuntu.iso") == "ubuntu.iso"

    def test_name_exactly_at_the_limit_is_unchanged(self):
        name = "x" * NAME_DISPLAY_LIMIT
        assert format_name(name) == name

    def test_one_character_over_is_truncated(self):
        name = "x" * (NAME_DISPLAY_LIMIT + 1)
        assert format_name(name) == "x" * NAME_DISPLAY_LIMIT + "..."

    def test_keeps_the_first_72_characters(self):
        name = "".join(str(i % 10) for i in range(200))
        assert format_name(name).startswith(name[:NAME_DISPLAY_LIMIT])

    def test_truncated_output_is_limit_plus_ellipsis(self):
        assert len(format_name("y" * 500)) == NAME_DISPLAY_LIMIT + 3

    def test_ends_with_an_ellipsis_when_truncated(self):
        assert format_name("z" * 200).endswith("...")

    def test_empty_name(self):
        assert format_name("") == "-"

    def test_none_name(self):
        assert format_name(None) == "-"


class TestTorrentTableNameTruncation:
    def test_long_name_is_truncated_in_the_table(self):
        name = "A" * 120
        text = format_torrent_table([_torrent(name)])
        assert "A" * NAME_DISPLAY_LIMIT + "..." in text
        assert name not in text

    def test_short_name_appears_in_full(self):
        text = format_torrent_table([_torrent("ubuntu-24.04.iso")])
        assert "ubuntu-24.04.iso" in text

    def test_truncation_does_not_change_other_columns(self):
        text = format_torrent_table([_torrent("B" * 200, id="zzzz", status="error")])
        assert "zzzz" in text
        assert "error" in text

    def test_column_stays_aligned_across_mixed_lengths(self):
        text = format_torrent_table([_torrent("C" * 200), _torrent("tiny", id="b7c1")])
        lines = text.splitlines()
        # Every row pads to the same width, so STATUS starts at one column.
        starts = [line.index("downloading") for line in lines[1:]]
        assert len(set(starts)) == 1

    def test_no_row_exceeds_the_display_limit_for_its_name(self):
        text = format_torrent_table([_torrent("D" * 400)])
        assert "D" * (NAME_DISPLAY_LIMIT + 1) not in text


class TestFormatNameBracketRemoval:
    def test_removes_a_square_bracket_tag(self):
        assert format_name("[HorribleSubs] Some Show") == "Some Show"

    def test_removes_a_round_bracket_tag(self):
        assert format_name("Ubuntu 24.04 (Noble Numbat) Desktop") == "Ubuntu 24.04 Desktop"

    def test_removes_several_tags(self):
        assert format_name("Cafe Society (2016) [BluRay] [x264] Movie") == "Cafe Society Movie"

    def test_removes_trailing_tags_entirely(self):
        assert format_name("Cafe Society (2016) [BluRay]") == "Cafe Society"

    def test_unwinds_nested_brackets(self):
        assert format_name("Nested [outer [inner] tag] Title") == "Nested Title"

    def test_leaves_an_unmatched_bracket_alone(self):
        assert format_name("Unmatched [bracket here") == "Unmatched [bracket here"

    def test_leaves_an_unmatched_paren_alone(self):
        assert format_name("Unmatched (paren here") == "Unmatched (paren here"

    def test_empty_brackets(self):
        assert format_name("Title [] ()") == "Title"

    def test_a_name_that_is_only_a_tag(self):
        assert format_name("[only-a-tag]") == "-"

    def test_collapses_whitespace_left_behind(self):
        assert format_name("A [x] [y] B") == "A B"

    def test_tidies_doubled_separators(self):
        assert format_name("Show.Name.S01E01.[1080p].WEB-DL.mkv") == "Show.Name.S01E01.WEB-DL.mkv"

    def test_tidies_space_before_extension(self):
        assert format_name("[Group] Some Show - 01 [1080p].mkv") == "Some Show - 01.mkv"

    def test_does_not_strip_brackets_from_the_real_name(self):
        # format_name returns a new string; nothing mutates the input.
        original = "[Group] Title"
        format_name(original)
        assert original == "[Group] Title"


class TestFormatNamePictographRemoval:
    def test_removes_an_emoji_with_variation_selector(self):
        assert format_name("❤️ Movie") == "Movie"

    def test_removes_supplementary_plane_emoji(self):
        assert format_name("HOT \U0001F525\U0001F525 Movie") == "HOT Movie"

    def test_removes_a_star(self):
        assert format_name("Movie ⭐") == "Movie"

    def test_removes_a_check_mark(self):
        assert format_name("✅ Verified Movie") == "Verified Movie"

    def test_removes_flag_regional_indicators(self):
        assert format_name("\U0001F1FA\U0001F1F8 Movie") == "Movie"

    def test_removes_skin_tone_modifiers(self):
        assert format_name("\U0001F44D\U0001F3FB Movie") == "Movie"

    def test_removes_zero_width_joiner_sequences(self):
        assert format_name("\U0001F468‍\U0001F4BB Movie") == "Movie"

    def test_a_name_that_is_only_emoji(self):
        assert format_name("\U0001F525\U0001F525\U0001F525") == "-"

    def test_keeps_ordinary_typographic_symbols(self):
        assert format_name("Temp 25° © Studio™") == "Temp 25° © Studio™"

    def test_keeps_accented_latin(self):
        assert format_name("Café Society") == "Café Society"

    def test_keeps_cjk_titles(self):
        assert format_name("日本語のタイトル [RAW]") == "日本語のタイトル"

    def test_keeps_maths_symbols_and_dashes(self):
        assert format_name("A ± B × C — D") == "A ± B × C — D"

    def test_keeps_ascii_punctuation(self):
        name = "Show_Name-2026 (S01) v2.0 #1 @home"
        assert format_name(name) == "Show_Name-2026 v2.0 #1 @home"

    def test_keeps_caret_which_is_a_modifier_symbol(self):
        assert format_name("Two^Three") == "Two^Three"


class TestFormatNameCombined:
    def test_cleaning_then_truncation_uses_the_cleaned_length(self):
        # 80 real characters plus a tag: the tag goes, then the remainder
        # is truncated -- so the tag never eats into the visible budget.
        name = "A" * 80 + " [1080p]"
        assert format_name(name) == "A" * NAME_DISPLAY_LIMIT + "..."

    def test_a_name_under_the_limit_only_after_cleaning_is_not_truncated(self):
        name = "B" * 70 + " [some-very-long-release-group-tag]"
        assert format_name(name) == "B" * 70

    def test_realistic_release_name(self):
        name = "[SubsPlease] Some Anime - 12 (1080p) [A1B2C3D4].mkv"
        assert format_name(name) == "Some Anime - 12.mkv"

    def test_table_shows_the_cleaned_name(self):
        text = format_torrent_table([_torrent("[Group] \U0001F525 Real Title (1080p).mkv")])
        assert "Real Title.mkv" in text
        assert "[Group]" not in text
        assert "\U0001F525" not in text


class TestDisplayWidth:
    def test_ascii_is_one_column_each(self):
        assert display_width("Ubuntu") == 6

    def test_empty_string(self):
        assert display_width("") == 0

    def test_cjk_is_two_columns_each(self):
        assert display_width("日本語") == 6

    def test_hangul_is_two_columns_each(self):
        assert display_width("한국어") == 6

    def test_fullwidth_latin_is_two_columns_each(self):
        assert display_width("ＡＢ") == 4

    def test_halfwidth_katakana_is_one_column(self):
        assert display_width("ｱｲｳ") == 3

    def test_cyrillic_is_narrow(self):
        assert display_width("Здравствуй") == 10

    def test_accented_latin_is_narrow(self):
        assert display_width("Café") == 4

    def test_combining_marks_take_no_room(self):
        # "e" + combining acute renders as one column, not two.
        assert display_width("é") == 1

    def test_composed_and_decomposed_forms_agree(self):
        assert display_width("é") == display_width("é")

    def test_zero_width_joiner_takes_no_room(self):
        assert display_width("a‍b") == 2

    def test_mixed_script(self):
        assert display_width("A日B") == 4


class TestTableAlignment:
    def _table(self, names):
        return format_torrent_table(
            [
                {
                    "id": f"row{i}",
                    "name": name,
                    "status": "error",
                    "priority": "normal",
                    "bytes_downloaded": 0,
                    "total_length": 10,
                }
                for i, name in enumerate(names)
            ]
        )

    def test_status_column_starts_at_one_offset_for_cjk_rows(self):
        text = self._table(["Ubuntu.24.04.iso", "日本語のタイトル", "Café Society"])
        offsets = {display_width(line[: line.index("error")]) for line in text.splitlines()[1:]}
        assert len(offsets) == 1

    def test_header_aligns_with_the_rows(self):
        text = self._table(["日本語のタイトル"])
        header, row = text.splitlines()
        assert display_width(header[: header.index("STATUS")]) == display_width(
            row[: row.index("error")]
        )

    def test_hangul_rows_align(self):
        text = self._table(["한국어 제목", "plain-name"])
        offsets = {display_width(line[: line.index("error")]) for line in text.splitlines()[1:]}
        assert len(offsets) == 1

    def test_fullwidth_rows_align(self):
        text = self._table(["ＦＵＬＬＷＩＤＴＨ", "plain"])
        offsets = {display_width(line[: line.index("error")]) for line in text.splitlines()[1:]}
        assert len(offsets) == 1

    def test_no_trailing_whitespace_on_any_line(self):
        text = self._table(["日本語", "plain-name"])
        assert all(line == line.rstrip() for line in text.splitlines())

    def test_header_has_no_trailing_whitespace(self):
        text = self._table(["x"])
        assert text.splitlines()[0].endswith("ETA")

    def test_columns_are_separated_by_two_spaces(self):
        text = self._table(["ab"])
        assert "row0  ab" in text


class TestWidthBasedTruncation:
    def test_cjk_name_is_truncated_by_columns_not_characters(self):
        name = "日" * 60  # 120 columns
        out = format_name(name)
        assert out.endswith("...")
        assert display_width(out) == NAME_DISPLAY_LIMIT + 3

    def test_cjk_name_exactly_at_the_limit_is_kept(self):
        name = "日" * (NAME_DISPLAY_LIMIT // 2)
        assert format_name(name) == name

    def test_one_column_over_is_truncated(self):
        name = "日" * (NAME_DISPLAY_LIMIT // 2) + "X"
        assert format_name(name).endswith("...")

    def test_a_wide_character_straddling_the_limit_is_dropped(self):
        # 71 narrow + one wide: the wide char would occupy columns 72-73,
        # so it is dropped rather than half-printed.
        name = "A" * 71 + "日" + "B" * 10
        out = format_name(name)
        assert display_width(out) <= NAME_DISPLAY_LIMIT + 3
        assert "日" not in out

    def test_mixed_script_name_respects_the_column_budget(self):
        out = format_name("日本語" * 10 + "X" * 40)
        assert display_width(out) == NAME_DISPLAY_LIMIT + 3

    def test_ascii_truncation_is_unchanged(self):
        assert format_name("A" * 100) == "A" * NAME_DISPLAY_LIMIT + "..."


class TestUnicodeNormalisation:
    def test_decomposed_name_is_composed_for_display(self):
        assert format_name("Café Society") == "Café Society"

    def test_composed_and_decomposed_render_identically(self):
        assert format_name("Café Society") == format_name("Café Society")

    def test_normalised_name_measures_as_narrow(self):
        assert display_width(format_name("Café Society")) == 12
