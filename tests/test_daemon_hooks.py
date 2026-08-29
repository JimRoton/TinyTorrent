import asyncio
import sys

import pytest

from tinytorrent.common.hooks import HookCommand, HookEvent
from tinytorrent.common.ids import generate_peer_id
from tinytorrent.common.priority import Priority
from tinytorrent.daemon import magnet as magnet_module
from tinytorrent.daemon.hooks import HookRunner, build_context
from tinytorrent.daemon.torrent_session import TorrentSession, TorrentStatus

COMPLETED = HookEvent.DOWNLOAD_COMPLETED


def _run(coro):
    return asyncio.run(coro)


def _py(code: str) -> tuple[str, ...]:
    """A hook command that runs a snippet with this interpreter.

    Using sys.executable rather than a shell utility keeps these tests
    independent of which coreutils happen to be installed and where.
    """
    return (sys.executable, "-c", code)


def _append(path, text: str) -> tuple[str, ...]:
    return _py(f"open({str(path)!r}, 'a').write({text!r})")


def _command(argv, on_failure="ignore", timeout_seconds=30.0) -> HookCommand:
    return HookCommand(argv=tuple(argv), on_failure=on_failure, timeout_seconds=timeout_seconds)


def _session(tmp_path, name="demo", hash_byte=b"\x00") -> TorrentSession:
    magnet = magnet_module.parse(f"magnet:?xt=urn:btih:{(hash_byte * 20).hex()}&dn={name}")
    return TorrentSession(
        "a3f9", magnet, tmp_path / "downloads", generate_peer_id(), priority=Priority.HIGH
    )


class TestFire:
    def test_no_configured_commands_is_a_noop(self, tmp_path):
        async def scenario():
            runner = HookRunner({})
            runner.fire(COMPLETED, {})
            await runner.wait_idle()

        _run(scenario())

    def test_runs_a_configured_command(self, tmp_path):
        marker = tmp_path / "marker"

        async def scenario():
            runner = HookRunner({COMPLETED: (_command(_append(marker, "ran")),)})
            runner.fire(COMPLETED, {})
            await runner.wait_idle()

        _run(scenario())
        assert marker.read_text() == "ran"

    def test_fire_does_not_block_the_caller(self, tmp_path):
        marker = tmp_path / "marker"

        async def scenario():
            runner = HookRunner({COMPLETED: (_command(_append(marker, "ran")),)})
            runner.fire(COMPLETED, {})
            # fire() only schedules; nothing has been awaited, so the
            # command cannot have run yet.
            assert not marker.exists()
            await runner.wait_idle()

        _run(scenario())
        assert marker.exists()

    def test_commands_run_in_configured_order(self, tmp_path):
        marker = tmp_path / "order"

        async def scenario():
            runner = HookRunner(
                {
                    COMPLETED: (
                        _command(_append(marker, "a")),
                        _command(_append(marker, "b")),
                        _command(_append(marker, "c")),
                    )
                }
            )
            runner.fire(COMPLETED, {})
            await runner.wait_idle()

        _run(scenario())
        assert marker.read_text() == "abc"

    def test_placeholders_are_substituted_before_running(self, tmp_path):
        async def scenario():
            argv = _py("import sys; open(sys.argv[1], 'w').write('done')") + ("%download_dir%/%name%",)
            runner = HookRunner({COMPLETED: (_command(argv),)})
            runner.fire(COMPLETED, {"download_dir": str(tmp_path), "name": "out.txt"})
            await runner.wait_idle()

        _run(scenario())
        assert (tmp_path / "out.txt").read_text() == "done"

    def test_only_the_fired_event_runs(self, tmp_path):
        completed_marker = tmp_path / "completed"
        error_marker = tmp_path / "error"

        async def scenario():
            runner = HookRunner(
                {
                    COMPLETED: (_command(_append(completed_marker, "x")),),
                    HookEvent.DOWNLOAD_ERROR: (_command(_append(error_marker, "x")),),
                }
            )
            runner.fire(COMPLETED, {})
            await runner.wait_idle()

        _run(scenario())
        assert completed_marker.exists()
        assert not error_marker.exists()


class TestOnFailure:
    def test_ignore_continues_to_the_next_command(self, tmp_path):
        marker = tmp_path / "marker"

        async def scenario():
            runner = HookRunner(
                {
                    COMPLETED: (
                        _command(_py("raise SystemExit(3)"), on_failure="ignore"),
                        _command(_append(marker, "reached")),
                    )
                }
            )
            runner.fire(COMPLETED, {})
            await runner.wait_idle()

        _run(scenario())
        assert marker.read_text() == "reached"

    def test_abort_remaining_skips_the_rest(self, tmp_path):
        marker = tmp_path / "marker"

        async def scenario():
            runner = HookRunner(
                {
                    COMPLETED: (
                        _command(_py("raise SystemExit(3)"), on_failure="abort_remaining"),
                        _command(_append(marker, "reached")),
                    )
                }
            )
            runner.fire(COMPLETED, {})
            await runner.wait_idle()

        _run(scenario())
        assert not marker.exists()

    def test_abort_remaining_does_not_abort_on_success(self, tmp_path):
        marker = tmp_path / "marker"

        async def scenario():
            runner = HookRunner(
                {
                    COMPLETED: (
                        _command(_py("pass"), on_failure="abort_remaining"),
                        _command(_append(marker, "reached")),
                    )
                }
            )
            runner.fire(COMPLETED, {})
            await runner.wait_idle()

        _run(scenario())
        assert marker.read_text() == "reached"

    def test_a_command_that_cannot_start_counts_as_a_failure(self, tmp_path):
        marker = tmp_path / "marker"

        async def scenario():
            runner = HookRunner(
                {
                    COMPLETED: (
                        _command(("/nonexistent/tinytorrent-hook-binary",), on_failure="abort_remaining"),
                        _command(_append(marker, "reached")),
                    )
                }
            )
            runner.fire(COMPLETED, {})
            await runner.wait_idle()

        _run(scenario())
        assert not marker.exists()


class TestTimeout:
    def test_a_hung_command_is_killed_and_counts_as_a_failure(self, tmp_path):
        marker = tmp_path / "marker"

        async def scenario():
            runner = HookRunner(
                {
                    COMPLETED: (
                        _command(
                            _py("import time; time.sleep(30)"),
                            on_failure="abort_remaining",
                            timeout_seconds=0.5,
                        ),
                        _command(_append(marker, "reached")),
                    )
                }
            )
            runner.fire(COMPLETED, {})
            await runner.wait_idle()

        _run(scenario())
        assert not marker.exists()


class TestRunForTest:
    def test_reports_each_command(self, tmp_path):
        async def scenario():
            runner = HookRunner(
                {COMPLETED: (_command(_py("pass")), _command(_py("raise SystemExit(2)")))}
            )
            return await runner.run_for_test(COMPLETED, {})

        results = _run(scenario())
        assert [r["outcome"] for r in results] == ["ok", "failed"]
        assert results[0]["returncode"] == 0
        assert results[1]["returncode"] == 2

    def test_captures_command_output(self, tmp_path):
        async def scenario():
            runner = HookRunner({COMPLETED: (_command(_py("print('hello from hook')")),)})
            return await runner.run_for_test(COMPLETED, {})

        (result,) = _run(scenario())
        assert "hello from hook" in result["output"]

    def test_runs_every_command_even_after_abort_remaining(self, tmp_path):
        marker = tmp_path / "marker"

        async def scenario():
            runner = HookRunner(
                {
                    COMPLETED: (
                        _command(_py("raise SystemExit(3)"), on_failure="abort_remaining"),
                        _command(_append(marker, "reached")),
                    )
                }
            )
            return await runner.run_for_test(COMPLETED, {})

        results = _run(scenario())
        # Unlike a real firing, the second command still runs...
        assert marker.read_text() == "reached"
        assert len(results) == 2
        # ...but it's flagged as one production would have skipped.
        assert results[0]["would_run_in_production"] is True
        assert results[1]["would_run_in_production"] is False

    def test_ignore_failure_does_not_flag_later_commands(self, tmp_path):
        async def scenario():
            runner = HookRunner(
                {
                    COMPLETED: (
                        _command(_py("raise SystemExit(3)"), on_failure="ignore"),
                        _command(_py("pass")),
                    )
                }
            )
            return await runner.run_for_test(COMPLETED, {})

        results = _run(scenario())
        assert [r["would_run_in_production"] for r in results] == [True, True]

    def test_reports_timeout_outcome(self, tmp_path):
        async def scenario():
            runner = HookRunner(
                {COMPLETED: (_command(_py("import time; time.sleep(30)"), timeout_seconds=0.5),)}
            )
            return await runner.run_for_test(COMPLETED, {})

        (result,) = _run(scenario())
        assert result["outcome"] == "timeout"
        assert result["returncode"] is None

    def test_reports_start_error_outcome(self, tmp_path):
        async def scenario():
            runner = HookRunner({COMPLETED: (_command(("/nonexistent/tinytorrent-hook-binary",)),)})
            return await runner.run_for_test(COMPLETED, {})

        (result,) = _run(scenario())
        assert result["outcome"] == "start_error"

    def test_reports_substituted_argv(self, tmp_path):
        async def scenario():
            runner = HookRunner({COMPLETED: (_command(_py("pass") + ("%name%",)),)})
            return await runner.run_for_test(COMPLETED, {"name": "demo"})

        (result,) = _run(scenario())
        assert result["argv"][-1] == "demo"

    def test_unconfigured_event_returns_no_results(self, tmp_path):
        async def scenario():
            return await HookRunner({}).run_for_test(COMPLETED, {})

        assert _run(scenario()) == []


class TestCommandsFor:
    def test_returns_configured_commands(self):
        command = _command(_py("pass"))
        runner = HookRunner({COMPLETED: (command,)})
        assert runner.commands_for(COMPLETED) == (command,)

    def test_returns_empty_for_unconfigured_event(self):
        assert HookRunner({}).commands_for(COMPLETED) == ()


class TestBuildContext:
    def test_includes_every_documented_placeholder(self, tmp_path):
        context = build_context(_session(tmp_path))
        assert set(context) == {
            "id",
            "name",
            "status",
            "download_dir",
            "total_bytes",
            "priority",
            "info_hash",
        }

    def test_values_come_from_the_session(self, tmp_path):
        session = _session(tmp_path, name="ubuntu.iso", hash_byte=b"\xab")
        context = build_context(session)
        assert context["id"] == "a3f9"
        assert context["name"] == "ubuntu.iso"
        assert context["status"] == TorrentStatus.QUEUED.value
        assert context["download_dir"] == str(tmp_path / "downloads")
        assert context["priority"] == "high"
        assert context["info_hash"] == "ab" * 20

    def test_total_bytes_is_zero_before_metadata(self, tmp_path):
        assert build_context(_session(tmp_path))["total_bytes"] == "0"

    def test_extra_values_are_merged_in(self, tmp_path):
        context = build_context(_session(tmp_path), deleted_data="true")
        assert context["deleted_data"] == "true"

    def test_every_value_is_a_string(self, tmp_path):
        context = build_context(_session(tmp_path), deleted_data="false")
        assert all(isinstance(v, str) for v in context.values())
