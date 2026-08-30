import pytest

from tinytorrent.common.hooks import (
    DEFAULT_TIMEOUT_SECONDS,
    HookCommand,
    HookConfigError,
    HookEvent,
    parse_hooks_config,
    substitute_argv,
)


class TestHookEvent:
    def test_all_documented_events_exist(self):
        assert {e.value for e in HookEvent} == {
            "metadata_fetched",
            "download_started",
            "download_completed",
            "download_error",
            "torrent_purged",
        }

    def test_lookup_by_value(self):
        assert HookEvent("download_completed") is HookEvent.DOWNLOAD_COMPLETED

    def test_unknown_value_raises(self):
        with pytest.raises(ValueError):
            HookEvent("download_finished")


class TestParseValidConfig:
    def test_minimal_command_gets_defaults(self):
        parsed = parse_hooks_config({"download_completed": [{"command": ["true"]}]})
        (command,) = parsed[HookEvent.DOWNLOAD_COMPLETED]
        assert command == HookCommand(
            argv=("true",), on_failure="ignore", timeout_seconds=DEFAULT_TIMEOUT_SECONDS
        )

    def test_explicit_fields(self):
        parsed = parse_hooks_config(
            {
                "download_error": [
                    {
                        "command": ["notify-send", "failed"],
                        "on_failure": "abort_remaining",
                        "timeout_seconds": 5,
                    }
                ]
            }
        )
        (command,) = parsed[HookEvent.DOWNLOAD_ERROR]
        assert command.argv == ("notify-send", "failed")
        assert command.on_failure == "abort_remaining"
        assert command.timeout_seconds == 5.0

    def test_commands_keep_configured_order(self):
        parsed = parse_hooks_config(
            {
                "download_completed": [
                    {"command": ["first"]},
                    {"command": ["second"]},
                    {"command": ["third"]},
                ]
            }
        )
        assert [c.argv[0] for c in parsed[HookEvent.DOWNLOAD_COMPLETED]] == ["first", "second", "third"]

    def test_multiple_events(self):
        parsed = parse_hooks_config(
            {"download_started": [{"command": ["a"]}], "torrent_purged": [{"command": ["b"]}]}
        )
        assert set(parsed) == {HookEvent.DOWNLOAD_STARTED, HookEvent.TORRENT_PURGED}

    def test_empty_hooks_object_is_valid(self):
        assert parse_hooks_config({}) == {}

    def test_timeout_accepts_float(self):
        parsed = parse_hooks_config({"download_completed": [{"command": ["x"], "timeout_seconds": 0.5}]})
        assert parsed[HookEvent.DOWNLOAD_COMPLETED][0].timeout_seconds == 0.5


class TestParseRejectsBadConfig:
    def test_not_an_object(self):
        with pytest.raises(HookConfigError):
            parse_hooks_config(["download_completed"])

    def test_unknown_event_name(self):
        with pytest.raises(HookConfigError) as exc:
            parse_hooks_config({"download_finished": [{"command": ["x"]}]})
        assert "download_finished" in str(exc.value)

    def test_event_value_must_be_a_list(self):
        with pytest.raises(HookConfigError):
            parse_hooks_config({"download_completed": {"command": ["x"]}})

    def test_event_list_must_not_be_empty(self):
        with pytest.raises(HookConfigError):
            parse_hooks_config({"download_completed": []})

    def test_entry_must_be_an_object(self):
        with pytest.raises(HookConfigError):
            parse_hooks_config({"download_completed": ["cp a b"]})

    def test_command_is_required(self):
        with pytest.raises(HookConfigError):
            parse_hooks_config({"download_completed": [{"on_failure": "ignore"}]})

    def test_command_must_not_be_empty(self):
        with pytest.raises(HookConfigError):
            parse_hooks_config({"download_completed": [{"command": []}]})

    def test_command_must_be_a_list_not_a_shell_string(self):
        with pytest.raises(HookConfigError):
            parse_hooks_config({"download_completed": [{"command": "cp a b"}]})

    def test_command_elements_must_be_strings(self):
        with pytest.raises(HookConfigError):
            parse_hooks_config({"download_completed": [{"command": ["cp", 7]}]})

    def test_invalid_on_failure(self):
        with pytest.raises(HookConfigError):
            parse_hooks_config({"download_completed": [{"command": ["x"], "on_failure": "explode"}]})

    def test_non_numeric_timeout(self):
        with pytest.raises(HookConfigError):
            parse_hooks_config({"download_completed": [{"command": ["x"], "timeout_seconds": "soon"}]})

    def test_zero_timeout_rejected(self):
        with pytest.raises(HookConfigError):
            parse_hooks_config({"download_completed": [{"command": ["x"], "timeout_seconds": 0}]})

    def test_negative_timeout_rejected(self):
        with pytest.raises(HookConfigError):
            parse_hooks_config({"download_completed": [{"command": ["x"], "timeout_seconds": -1}]})

    def test_unknown_key_in_entry(self):
        with pytest.raises(HookConfigError) as exc:
            parse_hooks_config({"download_completed": [{"command": ["x"], "retries": 3}]})
        assert "retries" in str(exc.value)

    def test_error_message_names_the_offending_entry(self):
        with pytest.raises(HookConfigError) as exc:
            parse_hooks_config({"download_completed": [{"command": ["ok"]}, {"command": []}]})
        assert "hooks.download_completed[1]" in str(exc.value)


class TestSubstituteArgv:
    def test_replaces_known_placeholders(self):
        result = substitute_argv(
            ("cp", "%download_dir%/%name%", "/dest"), {"download_dir": "/dl", "name": "iso"}
        )
        assert result == ["cp", "/dl/iso", "/dest"]

    def test_leaves_unknown_placeholders_literal(self):
        assert substitute_argv(("echo", "%nope%"), {"name": "x"}) == ["echo", "%nope%"]

    def test_bare_percent_is_untouched(self):
        assert substitute_argv(("echo", "100%"), {}) == ["echo", "100%"]

    def test_empty_context_leaves_everything_literal(self):
        assert substitute_argv(("echo", "%name%"), {}) == ["echo", "%name%"]

    def test_substitution_is_per_argument_not_a_shell_split(self):
        # A value containing spaces stays exactly one argv element.
        assert substitute_argv(("echo", "%name%"), {"name": "two words"}) == ["echo", "two words"]

    def test_shell_metacharacters_stay_one_argument(self):
        # The security property: a hostile torrent name is never parsed as syntax.
        hostile = "x; rm -rf / #"
        result = substitute_argv(("echo", "%name%"), {"name": hostile})
        assert result == ["echo", hostile]
        assert len(result) == 2

    def test_multiple_occurrences_in_one_argument(self):
        assert substitute_argv(("%id%-%id%",), {"id": "a3f9"}) == ["a3f9-a3f9"]

    def test_returns_a_list_not_a_tuple(self):
        assert isinstance(substitute_argv(("x",), {}), list)
