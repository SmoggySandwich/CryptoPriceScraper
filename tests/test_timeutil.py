"""Day labels and wall-clock grid alignment."""

from __future__ import annotations

import unittest
from datetime import UTC, date, datetime, timedelta

from scraper import timeutil


class IsoZTests(unittest.TestCase):
    def test_formats_as_iso_z_with_second_precision(self):
        moment = datetime(2026, 9, 27, 0, 5, 3, 123456, tzinfo=UTC)
        self.assertEqual(timeutil.iso_z(moment), "2026-09-27T00:05:03Z")

    def test_uses_z_suffix_not_offset(self):
        self.assertNotIn("+00:00", timeutil.iso_z(datetime(2026, 1, 1, tzinfo=UTC)))

    def test_converts_non_utc_input(self):
        # A +02:00 instant at 02:00 is midnight UTC on the same calendar date.
        from datetime import timezone

        moment = datetime(2026, 9, 27, 2, 0, 0, tzinfo=timezone(timedelta(hours=2)))
        self.assertEqual(timeutil.iso_z(moment), "2026-09-27T00:00:00Z")


class DayLabelTests(unittest.TestCase):
    def test_last_second_of_day_stays_on_that_day(self):
        moment = datetime(2026, 9, 27, 23, 59, 59, 999999, tzinfo=UTC)
        self.assertEqual(timeutil.day_label(moment), "2026-09-27")

    def test_first_instant_of_next_day_rolls_over(self):
        moment = datetime(2026, 9, 28, 0, 0, 0, 0, tzinfo=UTC)
        self.assertEqual(timeutil.day_label(moment), "2026-09-28")

    def test_parse_accepts_a_valid_label(self):
        self.assertEqual(timeutil.parse_day_label("2026-09-27"), date(2026, 9, 27))

    def test_parse_rejects_unpadded_and_impossible_dates(self):
        for bad in ("2026-9-7", "2026-13-01", "2026-02-30", "not-a-date", "", "2026-09-27T00:00:00Z"):
            with self.subTest(bad=bad):
                self.assertIsNone(timeutil.parse_day_label(bad))


class GridSlotTests(unittest.TestCase):
    def test_mid_slot_advances_to_next_boundary(self):
        now = datetime(2026, 9, 27, 0, 3, 12, 500000, tzinfo=UTC)
        target = timeutil.next_grid_slot(now, 300)
        self.assertEqual(target, datetime(2026, 9, 27, 0, 5, 0, tzinfo=UTC))

    def test_exactly_on_a_boundary_moves_to_the_following_one(self):
        # Strictly-future is what stops a busy loop or a double tick.
        now = datetime(2026, 9, 27, 0, 5, 0, tzinfo=UTC)
        target = timeutil.next_grid_slot(now, 300)
        self.assertEqual(target, datetime(2026, 9, 27, 0, 10, 0, tzinfo=UTC))
        self.assertGreater(target, now)

    def test_rolls_over_midnight(self):
        now = datetime(2026, 9, 27, 23, 58, 0, tzinfo=UTC)
        target = timeutil.next_grid_slot(now, 300)
        self.assertEqual(target, datetime(2026, 9, 28, 0, 0, 0, tzinfo=UTC))

    def test_never_catches_up_after_a_long_stall(self):
        # Nine hours suspended. Accumulating from the previous tick would queue
        # ~108 immediate polls; the clock-derived slot queues exactly one.
        now = datetime(2026, 9, 27, 9, 0, 0, tzinfo=UTC)
        target = timeutil.next_grid_slot(now, 300)
        self.assertLessEqual((target - now).total_seconds(), 300)
        self.assertGreater(target, now)

    def test_rejects_non_positive_period(self):
        with self.assertRaises(ValueError):
            timeutil.next_grid_slot(datetime(2026, 9, 27, tzinfo=UTC), 0)

    def test_seconds_until_clamps_at_zero(self):
        past = datetime(2026, 9, 27, 0, 0, 0, tzinfo=UTC)
        self.assertEqual(timeutil.seconds_until(past, past + timedelta(seconds=5)), 0.0)


class PathTests(unittest.TestCase):
    def test_csv_and_zip_names(self):
        from pathlib import Path

        data = Path("data")
        self.assertEqual(timeutil.csv_path(data, "2026-09-27", "ETH").name, "2026-09-27_ETH.csv")
        self.assertEqual(
            timeutil.zip_path(data, "2026-09-27", "ETH").name, "2026-09-27_ETH.csv.zip"
        )


if __name__ == "__main__":
    unittest.main()
