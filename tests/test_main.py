"""Tick orchestration, exit codes and run modes."""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from main import EXIT_LOCKED, EXIT_NO_DATA, EXIT_OK, run_forever, run_once, tick
from scraper import store
from tests.helpers import FakeFetcher, RecordingSleep, make_config, observation

MOMENT = datetime(2026, 9, 28, 0, 5, 3, tzinfo=UTC)
HEADER = "timestamp_utc,price_usd,market_cap_usd,vol_24h_usd,source\n"


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
        # 00:05:03 -> the next grid slot is 00:10:00, i.e. 297 seconds away.
        self.assertEqual(sleep.delays, [297.0, 297.0])

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
        # 09:00:07 -> the next grid slot is 09:05:00, i.e. 293 seconds away.
        self.assertEqual(sleep.delays, [293.0])
        self.assertLessEqual(sleep.delays[0], 300)

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


if __name__ == "__main__":
    unittest.main()
