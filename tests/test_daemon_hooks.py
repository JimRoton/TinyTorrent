import asyncio
import os
import sys
import time

import pytest

from tinytorrent.common.hooks import HookCommand, HookEvent
from tinytorrent.common.ids import generate_peer_id
from tinytorrent.common.priority import Priority
from tinytorrent.daemon import magnet as magnet_module
from tinytorrent.daemon.hooks import HookRunner, build_context, build_daemon_context
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


STARTED = HookEvent.DAEMON_STARTED
STOPPING = HookEvent.DAEMON_STOPPING

# Runtime honours whatever interval it is given; the MIN_INTERVAL_SECONDS
# floor is enforced when parsing config, so these build HookCommands
# directly to keep the tests quick. Kept comfortably above the cost of
# spawning an interpreter -- a tighter interval churns through
# subprocesses fast enough to occasionally upset asyncio's child watcher
# across the many short-lived event loops this suite creates.
FAST = 0.2


async def _wait_for(predicate, timeout=5.0):
    """Poll until ``predicate()`` is true, so timing tests aren't fixed sleeps."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


def _repeating(argv, interval_seconds=FAST, on_failure="ignore", timeout_seconds=30.0):
    return HookCommand(
        argv=tuple(argv),
        on_failure=on_failure,
        timeout_seconds=timeout_seconds,
        interval_seconds=interval_seconds,
    )


def _count(path) -> int:
    return len(path.read_text()) if path.exists() else 0


class TestRepeatingHooks:
    def test_runs_immediately_and_then_repeats(self, tmp_path):
        marker = tmp_path / "ticks"

        async def scenario():
            runner = HookRunner({STARTED: (_repeating(_append(marker, "x")),)})
            runner.fire(STARTED, {})
            reached = await _wait_for(lambda: _count(marker) >= 2)
            await runner.cancel_repeating()
            return reached

        assert _run(scenario()) is True

    def test_cancel_repeating_stops_it(self, tmp_path):
        marker = tmp_path / "ticks"

        async def scenario():
            runner = HookRunner({STARTED: (_repeating(_append(marker, "x")),)})
            runner.fire(STARTED, {})
            await _wait_for(lambda: _count(marker) >= 1)
            await runner.cancel_repeating()
            settled = _count(marker)
            # Well over one interval later, nothing more should have run.
            await asyncio.sleep(FAST * 3)
            return settled, _count(marker)

        settled, after = _run(scenario())
        assert after <= settled + 1  # at most a run already in flight when cancelled

    def test_wait_idle_does_not_hang_on_repeating_tasks(self, tmp_path):
        marker = tmp_path / "ticks"

        async def scenario():
            runner = HookRunner({STARTED: (_repeating(_append(marker, "x")),)})
            runner.fire(STARTED, {})
            # A repeating task never finishes; wait_idle must ignore it.
            await asyncio.wait_for(runner.wait_idle(), timeout=5)
            await runner.cancel_repeating()

        _run(scenario())

    def test_one_shot_and_repeating_commands_coexist(self, tmp_path):
        once = tmp_path / "once"
        ticks = tmp_path / "ticks"

        async def scenario():
            runner = HookRunner(
                {
                    STARTED: (
                        _command(_append(once, "1")),
                        _repeating(_append(ticks, "x")),
                    )
                }
            )
            runner.fire(STARTED, {})
            await runner.wait_idle()  # the one-shot chain
            await _wait_for(lambda: _count(ticks) >= 2)
            await runner.cancel_repeating()

        _run(scenario())
        assert once.read_text() == "1"
        assert _count(ticks) >= 2

    def test_a_failing_one_shot_does_not_stop_a_repeating_command(self, tmp_path):
        ticks = tmp_path / "ticks"

        async def scenario():
            runner = HookRunner(
                {
                    STARTED: (
                        _command(_py("raise SystemExit(1)"), on_failure="abort_remaining"),
                        _repeating(_append(ticks, "x")),
                    )
                }
            )
            runner.fire(STARTED, {})
            reached = await _wait_for(lambda: _count(ticks) >= 2)
            await runner.cancel_repeating()
            return reached

        # The repeating command is detached, so the chain's abort_remaining
        # has no bearing on it.
        assert _run(scenario()) is True

    def test_abort_remaining_stops_repeating_after_a_failure(self, tmp_path):
        marker = tmp_path / "ticks"

        async def scenario():
            argv = _py(f"open({str(marker)!r}, 'a').write('x'); raise SystemExit(2)")
            runner = HookRunner({STARTED: (_repeating(argv, on_failure="abort_remaining"),)})
            runner.fire(STARTED, {})
            await _wait_for(lambda: _count(marker) >= 1)
            await asyncio.sleep(FAST * 3)
            await runner.cancel_repeating()
            return _count(marker)

        assert _run(scenario()) == 1

    def test_ignore_keeps_repeating_after_a_failure(self, tmp_path):
        marker = tmp_path / "ticks"

        async def scenario():
            argv = _py(f"open({str(marker)!r}, 'a').write('x'); raise SystemExit(2)")
            runner = HookRunner({STARTED: (_repeating(argv, on_failure="ignore"),)})
            runner.fire(STARTED, {})
            reached = await _wait_for(lambda: _count(marker) >= 2)
            await runner.cancel_repeating()
            return reached

        assert _run(scenario()) is True

    def test_runs_never_overlap(self, tmp_path):
        # Each run writes on entry and on exit; with non-overlapping runs
        # the result is strictly alternating, never two entries in a row.
        marker = tmp_path / "log"

        async def scenario():
            argv = _py(
                f"import time; f=open({str(marker)!r},'a'); f.write('s'); f.flush(); "
                "time.sleep(0.15); f.write('e'); f.close()"
            )
            runner = HookRunner({STARTED: (_repeating(argv, interval_seconds=FAST),)})
            runner.fire(STARTED, {})
            await _wait_for(lambda: marker.exists() and marker.read_text().count("e") >= 2)
            await runner.cancel_repeating()
            return marker.read_text()

        log = _run(scenario())
        assert "ss" not in log, f"overlapping runs detected: {log!r}"

    def test_placeholders_are_substituted_in_repeating_commands(self, tmp_path):
        async def scenario():
            argv = _py("import sys; open(sys.argv[1], 'a').write('x')") + ("%download_dir%/out",)
            runner = HookRunner({STARTED: (_repeating(argv),)})
            runner.fire(STARTED, {"download_dir": str(tmp_path)})
            await _wait_for(lambda: (tmp_path / "out").exists())
            await runner.cancel_repeating()

        _run(scenario())
        assert (tmp_path / "out").exists()

    def test_cancel_repeating_is_safe_with_nothing_running(self):
        async def scenario():
            await HookRunner({}).cancel_repeating()

        _run(scenario())


class TestRunNow:
    def test_awaits_the_commands(self, tmp_path):
        marker = tmp_path / "stopped"

        async def scenario():
            runner = HookRunner({STOPPING: (_command(_append(marker, "bye")),)})
            await runner.run_now(STOPPING, {})
            # Unlike fire(), this has already finished by the time it returns.
            assert marker.read_text() == "bye"

        _run(scenario())

    def test_runs_commands_in_order(self, tmp_path):
        marker = tmp_path / "order"

        async def scenario():
            runner = HookRunner(
                {STOPPING: (_command(_append(marker, "a")), _command(_append(marker, "b")))}
            )
            await runner.run_now(STOPPING, {})

        _run(scenario())
        assert marker.read_text() == "ab"

    def test_honours_abort_remaining(self, tmp_path):
        marker = tmp_path / "marker"

        async def scenario():
            runner = HookRunner(
                {
                    STOPPING: (
                        _command(_py("raise SystemExit(1)"), on_failure="abort_remaining"),
                        _command(_append(marker, "reached")),
                    )
                }
            )
            await runner.run_now(STOPPING, {})

        _run(scenario())
        assert not marker.exists()

    def test_unconfigured_event_is_a_noop(self):
        async def scenario():
            await HookRunner({}).run_now(STOPPING, {})

        _run(scenario())


class TestRunForTestWithIntervals:
    def test_repeating_command_runs_exactly_once(self, tmp_path):
        marker = tmp_path / "ticks"

        async def scenario():
            runner = HookRunner({STARTED: (_repeating(_append(marker, "x"), interval_seconds=60),)})
            results = await runner.run_for_test(STARTED, {})
            await asyncio.sleep(FAST * 4)  # nothing further should fire
            return results

        results = _run(scenario())
        assert _count(marker) == 1
        assert len(results) == 1

    def test_reports_the_configured_interval(self, tmp_path):
        async def scenario():
            runner = HookRunner({STARTED: (_repeating(_py("pass"), interval_seconds=60),)})
            return await runner.run_for_test(STARTED, {})

        (result,) = _run(scenario())
        assert result["interval_seconds"] == 60.0

    def test_one_shot_commands_report_no_interval(self, tmp_path):
        async def scenario():
            runner = HookRunner({STOPPING: (_command(_py("pass")),)})
            return await runner.run_for_test(STOPPING, {})

        (result,) = _run(scenario())
        assert result["interval_seconds"] is None

    def test_a_failing_repeating_command_does_not_flag_later_commands(self, tmp_path):
        async def scenario():
            runner = HookRunner(
                {
                    STARTED: (
                        _repeating(
                            _py("raise SystemExit(1)"),
                            interval_seconds=60,
                            on_failure="abort_remaining",
                        ),
                        _command(_py("pass")),
                    )
                }
            )
            return await runner.run_for_test(STARTED, {})

        results = _run(scenario())
        # The repeating command is detached, so it can't abort the chain.
        assert [r["would_run_in_production"] for r in results] == [True, True]


class TestBuildDaemonContext:
    def _context(self, **overrides):
        kwargs = {
            "download_dir": "/dl",
            "state_file": "/state.json",
            "socket_path": "/run/tinytorrentd.sock",
            "max_active": 4,
            "torrent_count": 2,
        }
        kwargs.update(overrides)
        return build_daemon_context(**kwargs)

    def test_includes_every_documented_placeholder(self):
        assert set(self._context()) == {
            "download_dir",
            "state_file",
            "socket_path",
            "max_active",
            "torrent_count",
            "pid",
        }

    def test_values(self):
        context = self._context()
        assert context["download_dir"] == "/dl"
        assert context["state_file"] == "/state.json"
        assert context["socket_path"] == "/run/tinytorrentd.sock"
        assert context["max_active"] == "4"
        assert context["torrent_count"] == "2"
        assert context["pid"] == str(os.getpid())

    def test_every_value_is_a_string(self):
        assert all(isinstance(v, str) for v in self._context().values())

    def test_missing_socket_path_becomes_empty(self):
        assert self._context(socket_path=None)["socket_path"] == ""

    def test_has_no_torrent_placeholders(self):
        context = self._context()
        assert "id" not in context
        assert "name" not in context
        assert "info_hash" not in context

    def test_extra_values_are_merged_in(self):
        assert self._context(reason="sigterm")["reason"] == "sigterm"

    def test_torrent_placeholders_stay_literal_in_a_daemon_hook(self, tmp_path):
        # A torrent hook copied onto a daemon event degrades to a literal
        # argument rather than failing.
        async def scenario():
            out = tmp_path / "out"
            argv = _py("import sys; open(sys.argv[1], 'w').write(sys.argv[2])") + (
                str(out),
                "%name%",
            )
            runner = HookRunner({STARTED: (_command(argv),)})
            runner.fire(STARTED, self._context())
            await runner.wait_idle()
            return out.read_text()

        assert _run(scenario()) == "%name%"
