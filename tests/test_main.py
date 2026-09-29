"""Tick orchestration, exit codes and run modes."""

from __future__ import annotations

import io
import logging
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import main
from main import (
    EXIT_LOCKED,
    EXIT_NO_DATA,
    EXIT_OK,
    EXIT_UNEXPECTED,
    MAX_CONSECUTIVE_FAILURES,
    run_forever,
    run_once,
    tick,
)
from scraper import store
from tests.helpers import FakeFetcher, RecordingSleep, make_config, observation

MOMENT = datetime(2026, 9, 28, 0, 5, 3, tzinfo=UTC)
HEADER = "timestamp_utc,price_usd,source\n"


class FailingLock:
    """A lock that is already held elsewhere."""

    def __init__(self):
        self.released = False

    def acquire(self) -> bool:
        return False

    def release(self) -> None:
        self.released = True


class MainTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.cfg = make_config(self.root)
        store.ensure_dirs(self.cfg.storage.data_dir)

    def csv(self, day: str, symbol: str) -> Path:
        return self.cfg.storage.data_dir / f"{day}_{symbol}.csv"

    def zip(self, day: str, symbol: str) -> Path:
        return self.cfg.storage.data_dir / f"{day}_{symbol}.csv.zip"


class TickTests(MainTestCase):
    def test_writes_one_row_per_observation(self):
        fetcher = FakeFetcher([observation("BTC", 1.0), observation("ETH", 2.0)])
        result = tick(self.cfg, client=fetcher, now_fn=lambda: MOMENT)
        self.assertEqual(result.written, 2)
        self.assertEqual(result.observed, 2)
        self.assertIn("2026-09-28T00:05:03Z", self.csv("2026-09-28", "BTC").read_text())

    def test_row_timestamp_and_filename_share_one_instant(self):
        # Both derive from a single clock reading, which is what makes running
        # the archive step before the append safe.
        fetcher = FakeFetcher([observation("BTC", 1.0)])
        tick(self.cfg, client=fetcher, now_fn=lambda: MOMENT)
        row = self.csv("2026-09-28", "BTC").read_text().splitlines()[1]
        self.assertTrue(row.startswith("2026-09-28T00:05:03Z"))
        self.assertTrue(self.csv("2026-09-28", "BTC").name.startswith("2026-09-28"))

    def test_a_failed_fetch_still_rolls_the_day_over(self):
        # An outage must not be able to starve the archiving of a finished day.
        self.csv("2026-09-26", "ETH").write_text(HEADER, encoding="utf-8")
        result = tick(self.cfg, client=FakeFetcher([], ["BTC"]), now_fn=lambda: MOMENT)
        self.assertEqual(result.written, 0)
        self.assertEqual(result.archived, 1)
        self.assertTrue(self.zip("2026-09-26", "ETH").exists())

    def test_reports_coins_that_yielded_no_price(self):
        fetcher = FakeFetcher([observation("BTC", 1.0)], missing=["HYPE"])
        result = tick(self.cfg, client=fetcher, now_fn=lambda: MOMENT)
        self.assertEqual(result.missing, ("HYPE",))
        self.assertFalse(self.csv("2026-09-28", "HYPE").exists())

    def test_passes_the_configured_coins_to_the_fetcher(self):
        fetcher = FakeFetcher([observation("BTC", 1.0)])
        tick(self.cfg, client=fetcher, now_fn=lambda: MOMENT)
        self.assertEqual(fetcher.calls, [self.cfg.collection.coins])


class RunOnceTests(MainTestCase):
    def test_success_returns_zero(self):
        code = run_once(
            self.cfg,
            client=FakeFetcher([observation("BTC", 1.0)]),
            now_fn=lambda: MOMENT,
            lock=None,
        )
        self.assertEqual(code, EXIT_OK)

    def test_all_sources_failing_returns_three(self):
        code = run_once(
            self.cfg, client=FakeFetcher([], ["BTC", "ETH"]), now_fn=lambda: MOMENT, lock=None
        )
        self.assertEqual(code, EXIT_NO_DATA)

    def test_returns_three_even_though_it_still_archived(self):
        self.csv("2026-09-26", "ETH").write_text(HEADER, encoding="utf-8")
        code = run_once(self.cfg, client=FakeFetcher([]), now_fn=lambda: MOMENT, lock=None)
        self.assertEqual(code, EXIT_NO_DATA)
        self.assertTrue(self.zip("2026-09-26", "ETH").exists())

    def test_contention_is_benign_so_scheduler_history_stays_clean(self):
        # Under Task Scheduler or cron this only means the loop already has the
        # lock, which is not a failure and must not be recorded as one.
        lock = FailingLock()
        code = run_once(
            self.cfg, client=FakeFetcher([observation("BTC", 1.0)]), now_fn=lambda: MOMENT, lock=lock
        )
        self.assertEqual(code, EXIT_OK)
        self.assertFalse(lock.released)
        self.assertFalse(self.csv("2026-09-28", "BTC").exists())


class RunForeverTests(MainTestCase):
    def test_contention_is_an_error_here(self):
        # Two long-running loops would fight over every rollover, so this one
        # is loud.
        code = run_forever(
            self.cfg,
            client=FakeFetcher([observation("BTC", 1.0)]),
            now_fn=lambda: MOMENT,
            sleep=RecordingSleep(),
            lock=FailingLock(),
        )
        self.assertEqual(code, EXIT_LOCKED)

    def test_polls_repeatedly_and_sleeps_on_the_grid(self):
        sleep = RecordingSleep()
        code = run_forever(
            self.cfg,
            client=FakeFetcher([observation("BTC", 1.0)]),
            now_fn=lambda: MOMENT,
            sleep=sleep,
            lock=None,
            max_ticks=3,
        )
        self.assertEqual(code, EXIT_OK)
        # 00:05:03 -> the next ten-second slot is 00:05:10, i.e. 7 seconds away.
        self.assertEqual(sleep.delays, [7.0, 7.0])

    def test_a_suspend_does_not_produce_a_catch_up_burst(self):
        # The machine suspends and resumes nine hours later. Because the target
        # is recomputed from the clock rather than accumulated from the previous
        # tick, exactly one sleep happens and it is under one interval --
        # an accumulated schedule would have queued ~108 immediate polls.
        after_resume = datetime(2026, 9, 28, 9, 0, 7, tzinfo=UTC)
        calls = {"n": 0}

        def clock():
            calls["n"] += 1
            return MOMENT if calls["n"] == 1 else after_resume

        sleep = RecordingSleep()
        run_forever(
            self.cfg,
            client=FakeFetcher([observation("BTC", 1.0)]),
            now_fn=clock,
            sleep=sleep,
            lock=None,
            max_ticks=2,
        )
        # 09:00:07 -> the next grid slot is 09:00:10, i.e. 3 seconds away.
        self.assertEqual(sleep.delays, [3.0])
        self.assertLessEqual(sleep.delays[0], 10)

    def test_keyboard_interrupt_exits_cleanly(self):
        def interrupting_sleep(_seconds):
            raise KeyboardInterrupt

        code = run_forever(
            self.cfg,
            client=FakeFetcher([observation("BTC", 1.0)]),
            now_fn=lambda: MOMENT,
            sleep=interrupting_sleep,
            lock=None,
        )
        self.assertEqual(code, EXIT_OK)


class ExplodingFetcher:
    """A fetcher whose every call raises, like a locked CSV file would."""

    def __init__(self, error, *, fail_for: int = 10**9) -> None:
        self.error = error
        self.fail_for = fail_for
        self.calls = 0

    def fetch_all(self, coins):
        self.calls += 1
        if self.calls <= self.fail_for:
            raise self.error
        return [observation("BTC", 1.0)], []


class CrashResilienceTests(MainTestCase):
    """Loop mode is the deployment, so there is no supervisor to restart it.

    Under --once each tick was a fresh process and systemd's Restart=always
    caught a crash. Now a single unreachable exception -- most plausibly a
    Windows sharing violation from an AV scanner holding today's CSV -- used to
    end the run permanently and silently, and the loss would surface as a gap in
    the data days later.
    """

    def run_loop(self, fetcher, *, max_ticks=3, **kwargs):
        return run_forever(
            self.cfg,
            client=fetcher,
            now_fn=lambda: MOMENT,
            sleep=RecordingSleep(),
            lock=None,
            max_ticks=max_ticks,
            **kwargs,
        )

    def test_a_raising_tick_does_not_kill_the_loop(self):
        fetcher = ExplodingFetcher(OSError("file is held open"))
        with self.assertLogs("scraper.main", level="ERROR"):
            code = self.run_loop(fetcher, max_ticks=3)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(fetcher.calls, 3)

    def test_the_loop_recovers_once_the_failure_clears(self):
        fetcher = ExplodingFetcher(OSError("transient"), fail_for=2)
        with self.assertLogs("scraper.main", level="ERROR"):
            code = self.run_loop(fetcher, max_ticks=4)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(fetcher.calls, 4)
        # The successful ticks after the failure still wrote their rows.
        self.assertTrue(self.csv("2026-09-28", "BTC").exists())

    def test_a_wedged_process_still_exits_for_a_supervisor_to_notice(self):
        # Riding out a blip is the point; looking alive while collecting
        # nothing for hours is not.
        fetcher = ExplodingFetcher(OSError("never clears"))
        with self.assertLogs("scraper.main", level="ERROR"):
            code = self.run_loop(fetcher, max_ticks=None)
        self.assertEqual(code, EXIT_UNEXPECTED)
        self.assertEqual(fetcher.calls, MAX_CONSECUTIVE_FAILURES)

    def test_the_failure_counter_resets_on_success(self):
        # Two failures, a success, then two more must not add up to four.
        fetcher = SwitchableFetcher()
        with self.assertLogs("scraper.main", level="ERROR"):
            code = self.run_loop(fetcher, max_ticks=4)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(fetcher.calls, 4)


class SwitchableFetcher:
    """Fails on calls 1-2 and 5-6, succeeds otherwise."""

    def __init__(self) -> None:
        self.calls = 0

    def fetch_all(self, coins):
        self.calls += 1
        if self.calls in {1, 2, 5, 6}:
            raise OSError("flaky")
        return [observation("BTC", 1.0)], []


class SummaryTests(MainTestCase):
    def test_the_per_tick_success_line_is_debug_not_info(self):
        # At ten seconds an INFO line per tick would be 8,640 lines a day --
        # more log than the events it describes.
        with self.assertLogs("scraper.main", level="DEBUG") as captured:
            tick(
                self.cfg,
                client=FakeFetcher([observation("BTC", 1.0)]),
                now_fn=lambda: MOMENT,
            )
        # The line still exists, so nothing is lost when debugging a specific
        # tick -- it is only the default level that keeps it off disk.
        self.assertIn("poll ok", "\n".join(r.getMessage() for r in captured.records))
        above_debug = [r for r in captured.records if r.levelno >= logging.INFO]
        self.assertEqual(above_debug, [])

    def test_a_summary_is_logged_every_window(self):
        summary = main.TickSummary()
        for _ in range(3):
            summary.record(
                main.TickResult(
                    observed=1,
                    written=1,
                    archived=0,
                    missing=(),
                    prices=(("BTC", 1.0, "binance"),),
                ),
                0.25,
            )
        rendered = summary.render(0)
        self.assertIn("3 ticks", rendered)
        self.assertIn("3 rows", rendered)
        self.assertIn("BTC=binance:3", rendered)
        self.assertIn("slowest tick 0.25s", rendered)

    def test_the_summary_reports_what_never_arrived(self):
        summary = main.TickSummary()
        summary.record(
            main.TickResult(observed=1, written=1, archived=0, missing=("HYPE",)),
            0.1,
        )
        self.assertIn("missing HYPE:1", summary.render(0))

    def test_the_summary_is_emitted_by_the_loop(self):
        with self.assertLogs("scraper.main", level="INFO") as captured:
            run_forever(
                self.cfg,
                client=FakeFetcher([observation("BTC", 1.0)]),
                now_fn=lambda: MOMENT,
                sleep=RecordingSleep(),
                lock=None,
                max_ticks=main.SUMMARY_EVERY_TICKS,
            )
        self.assertIn("summary:", "\n".join(captured.output))


class StartupTests(MainTestCase):
    """Failures that happen before the configured handlers exist.

    The scheduled task runs pythonw.exe, where sys.stdout and sys.stderr are
    both None. Anything reported only through print() is reported to nobody, so
    a task that cannot start looks identical to one that is running fine.
    """

    def setUp(self):
        super().setUp()
        # main() calls setup_logging, which attaches a RotatingFileHandler that
        # nothing closes. On Windows that open handle makes the temp directory
        # undeletable, so the cleanup fails on a file the test itself created.
        self.addCleanup(self._close_handlers)
        # setup_logging also attaches a console handler whenever a stream
        # exists, which would print these tests' log lines into the suite
        # output. Removing the stream makes "a console exists" a controlled
        # fact rather than a property of how the suite was invoked.
        self.stderr = io.StringIO()
        self.addCleanup(setattr, sys, "stderr", sys.stderr)
        sys.stderr = self.stderr

    @staticmethod
    def _close_handlers():
        package = logging.getLogger("scraper")
        for handler in list(package.handlers):
            handler.close()
            package.removeHandler(handler)

    def write_config(self, name: str, body: str) -> Path:
        """Write a config whose paths are absolute and confined to self.root.

        Absolute on purpose. ``load_config``'s ``base_dir`` default is bound at
        function-definition time, so patching either module's ``BASE_DIR``
        afterwards has no effect on it -- a relative path would silently resolve
        against the real checkout and write test data into the live dataset.
        """
        path = self.root / name
        header = (
            f"[storage]\ndata_dir = {self.root / 'data'}\n"
            f"\n[logging]\nlog_dir = {self.root / 'logs'}\n"
        )
        path.write_text(header + body, encoding="utf-8")
        return path

    def test_a_config_error_reaches_the_crash_log(self):
        # pythonw.exe sets sys.stdout and sys.stderr to None, so the print()
        # above this is a no-op there: without the crash log, a task that cannot
        # start is indistinguishable from one that is running fine.
        bad = self.write_config(
            "bad.ini", "[collection]\npoll_seconds = 1\n[coins]\nBTC = bitcoin\n"
        )
        with mock.patch("main.BASE_DIR", self.root):
            code = main.main(["--config", str(bad)])

        self.assertEqual(code, main.EXIT_CONFIG)
        crash = self.root / "logs" / "main.crash.log"
        self.assertTrue(crash.exists())
        self.assertIn("poll_seconds=1 is too low", crash.read_text(encoding="utf-8"))

    def test_config_warnings_reach_the_log_file_not_just_stderr(self):
        # The warning is raised while parsing, which happens before logging is
        # configured -- so it has to be carried on the Config and emitted once
        # the configured handlers exist.
        path = self.write_config(
            "warn.ini", "[collection]\npoll_seconds = 8\n[coins]\nBTC = bitcoin\n"
        )
        with mock.patch("main._install_sigterm_handler"), mock.patch(
            "main.run_forever", side_effect=AssertionError("startup tests must use --once")
        ), mock.patch(
            "main.PriceFetcher", return_value=FakeFetcher([observation("BTC", 1.0)])
        ):
            # --once is not optional, and run_forever is patched anyway: without
            # it main() loops forever, and because load_config's base_dir
            # default is fixed at import time the loop would write into the real
            # data directory rather than the temp one.
            code = main.main(["--once", "--config", str(path)])

        self.assertEqual(code, main.EXIT_OK)
        text = (self.root / "logs" / "scraper.log").read_text(encoding="utf-8")
        self.assertIn("poll_seconds=8 is below 10", text)


if __name__ == "__main__":
    unittest.main()
