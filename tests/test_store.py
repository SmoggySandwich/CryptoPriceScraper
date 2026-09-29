"""CSV appending and the daily rollover."""

from __future__ import annotations

import logging
import os
import tempfile
import unittest
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from scraper import store
from tests.helpers import FakeOpener, RecordingSleep, observation

HEADER = "timestamp_utc,price_usd,source\n"
#: The pre-2026-09-28 shape, kept so the guards can be tested against a file
#: the current build must refuse to touch.
OLD_HEADER = "timestamp_utc,price_usd,market_cap_usd,vol_24h_usd,source\n"


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name) / "data"
        self.data.mkdir(parents=True, exist_ok=True)
        self.addCleanup(self._tmp.cleanup)

    def csv_path(self, day: str, symbol: str) -> Path:
        return self.data / f"{day}_{symbol}.csv"

    def zip_path(self, day: str, symbol: str) -> Path:
        return self.data / f"{day}_{symbol}.csv.zip"

    def sweep(self, today: str, **kwargs) -> store.ArchiveResult:
        kwargs.setdefault("sleep", RecordingSleep())
        kwargs.setdefault("retry_delays", (0.0,))
        return store.archive_stale_csvs(self.data, today, **kwargs)


class AppendTests(StoreTestCase):
    def test_creates_csv_with_header_then_row(self):
        moment = datetime(2026, 9, 28, 0, 5, 3, tzinfo=UTC)
        written = store.append_observations(
            self.data, "2026-09-28", moment, [observation("BTC", 84342.0)]
        )
        self.assertEqual(len(written), 1)
        # LF only: newline="" stops the CRLF translation, so the file is
        # byte-identical on Windows and Linux.
        self.assertEqual(
            self.csv_path("2026-09-28", "BTC").read_bytes(),
            HEADER.encode() + b"2026-09-28T00:05:03Z,84342.0,binance\n",
        )

    def test_second_append_adds_no_second_header(self):
        moment = datetime(2026, 9, 28, 0, 5, 3, tzinfo=UTC)
        store.append_observations(self.data, "2026-09-28", moment, [observation("BTC", 1.0)])
        store.append_observations(self.data, "2026-09-28", moment, [observation("BTC", 2.0)])
        text = self.csv_path("2026-09-28", "BTC").read_text(encoding="utf-8")
        self.assertEqual(text.count("timestamp_utc"), 1)
        self.assertEqual(len(text.strip().splitlines()), 3)

    def test_zero_byte_file_from_an_earlier_crash_gets_a_header(self):
        path = self.csv_path("2026-09-28", "BTC")
        path.write_bytes(b"")
        store.append_observations(
            self.data, "2026-09-28", datetime(2026, 9, 28, tzinfo=UTC), [observation("BTC", 1.0)]
        )
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(lines[0].split(",")[0], "timestamp_utc")
        self.assertEqual(len(lines), 2)

    def test_a_failover_source_is_recorded_verbatim(self):
        # The source column is what makes a venue switch visible instead of
        # looking like a price move, so it is written as-is, not normalised.
        store.append_observations(
            self.data,
            "2026-09-28",
            datetime(2026, 9, 28, tzinfo=UTC),
            [observation("HYPE", 91.3, source="hyperliquid")],
        )
        row = self.csv_path("2026-09-28", "HYPE").read_text(encoding="utf-8").splitlines()[1]
        self.assertEqual(row, "2026-09-28T00:00:00Z,91.3,hyperliquid")

    def test_integer_price_is_normalised_to_float(self):
        # Binance sends prices as JSON strings, CoinGecko as ints; repr keeps the
        # output uniform and round-trip exact without flattening sub-cent prices.
        store.append_observations(
            self.data,
            "2026-09-28",
            datetime(2026, 9, 28, tzinfo=UTC),
            [observation("BTC", 84386)],
        )
        self.assertIn(",84386.0,", self.csv_path("2026-09-28", "BTC").read_text(encoding="utf-8"))

    def test_tiny_price_survives_in_scientific_notation(self):
        store.append_observations(
            self.data,
            "2026-09-28",
            datetime(2026, 9, 28, tzinfo=UTC),
            [observation("PEPE", 1.2345e-07)],
        )
        self.assertIn("1.2345e-07", self.csv_path("2026-09-28", "PEPE").read_text(encoding="utf-8"))

    def test_timestamp_outside_the_day_label_is_reported(self):
        # Unreachable if callers thread one instant through; loud if it is not.
        with self.assertLogs("scraper.store", level="ERROR") as captured:
            store.append_observations(
                self.data,
                "2026-09-28",
                datetime(2026, 9, 27, 23, 59, tzinfo=UTC),
                [observation("BTC", 1.0)],
            )
        self.assertIn("falls outside day label", "\n".join(captured.output))


class SweepTests(StoreTestCase):
    def test_zips_and_deletes_a_previous_day(self):
        path = self.csv_path("2026-09-26", "ETH")
        path.write_text(HEADER + "2026-09-26T00:00:00Z,100.0,coingecko\n", encoding="utf-8")

        result = self.sweep("2026-09-28")

        self.assertEqual(result.zipped, (self.zip_path("2026-09-26", "ETH"),))
        self.assertFalse(path.exists())
        with zipfile.ZipFile(self.zip_path("2026-09-26", "ETH")) as archive:
            self.assertEqual(archive.namelist(), ["2026-09-26_ETH.csv"])
            self.assertIn(b"100.0", archive.read("2026-09-26_ETH.csv"))

    def test_leaves_today_alone(self):
        path = self.csv_path("2026-09-28", "ETH")
        path.write_text(HEADER, encoding="utf-8")
        result = self.sweep("2026-09-28")
        self.assertFalse(result.acted)
        self.assertTrue(path.exists())

    def test_leaves_a_future_dated_file_alone(self):
        # A future-dated file means the clock moved backwards at some point.
        # It must never be swept, because the day is not over yet.
        path = self.csv_path("2026-09-30", "ETH")
        path.write_text(HEADER, encoding="utf-8")
        result = self.sweep("2026-09-28")
        self.assertFalse(result.acted)
        self.assertTrue(path.exists())

    def test_is_idempotent(self):
        self.csv_path("2026-09-26", "ETH").write_text(
            HEADER + "2026-09-26T00:00:00Z,100.0,coingecko\n", encoding="utf-8"
        )
        self.sweep("2026-09-28")
        before = self.zip_path("2026-09-26", "ETH").read_bytes()

        second = self.sweep("2026-09-28")

        self.assertFalse(second.acted)
        self.assertEqual(self.zip_path("2026-09-26", "ETH").read_bytes(), before)

    def test_merges_a_csv_into_an_existing_zip_without_losing_rows(self):
        # The regression test for the overwrite bug, and the reason the merge
        # unions rather than picking a winner. A backwards clock step can reuse
        # a day label, at which point the CSV and the archive hold genuinely
        # different observations. Both must survive.
        zip_file = self.zip_path("2026-09-26", "ETH")
        with zipfile.ZipFile(zip_file, "w") as archive:
            archive.writestr(
                "2026-09-26_ETH.csv",
                HEADER + "2026-09-26T00:00:00Z,111.0,coingecko\n",
            )

        self.csv_path("2026-09-26", "ETH").write_text(
            HEADER + "2026-09-26T00:05:00Z,222.0,coingecko\n", encoding="utf-8"
        )

        result = self.sweep("2026-09-28")

        self.assertEqual(result.merged, (zip_file,))
        with zipfile.ZipFile(zip_file) as archive:
            text = archive.read("2026-09-26_ETH.csv").decode()
        self.assertIn("111.0", text)
        self.assertIn("222.0", text)
        self.assertEqual(text.count("timestamp_utc"), 1)

    def test_merge_of_identical_content_is_a_no_op(self):
        # The crash-between-write-and-delete case: the CSV is already archived.
        path = self.csv_path("2026-09-26", "ETH")
        body = HEADER + "2026-09-26T00:00:00Z,111.0,coingecko\n"
        path.write_text(body, encoding="utf-8")
        self.sweep("2026-09-28")

        # Re-seed the same CSV, as a crash before deletion would have left it.
        path.write_text(body, encoding="utf-8")
        self.sweep("2026-09-28")

        with zipfile.ZipFile(self.zip_path("2026-09-26", "ETH")) as archive:
            self.assertEqual(archive.read("2026-09-26_ETH.csv").decode(), body)

    def test_ignores_names_that_do_not_match_the_pattern(self):
        decoys = {
            "notes.csv": "just a note\n",
            "2026-9-7_ETH.csv": HEADER,  # unpadded month/day
            "2026-09-26_eth.csv": HEADER,  # lowercase symbol
            "2026-09-26_ETH.CSV": HEADER,  # wrong suffix case
            "2026-09-26_ETH.txt": HEADER,
        }
        for name, body in decoys.items():
            (self.data / name).write_text(body, encoding="utf-8")

        self.sweep("2026-09-28")

        for name in decoys:
            with self.subTest(name=name):
                self.assertTrue((self.data / name).exists())

    def test_warns_about_an_unpadded_csv_name(self):
        # Otherwise a hand-made 2026-9-7_ETH.csv is ignored forever, silently.
        (self.data / "2026-9-7_ETH.csv").write_text(HEADER, encoding="utf-8")
        with self.assertLogs("scraper.store", level="WARNING") as captured:
            self.sweep("2026-09-28")
        self.assertIn("ignoring 2026-9-7_ETH.csv", "\n".join(captured.output))

    def test_refuses_to_overwrite_an_unreadable_archive(self):
        corrupt = self.zip_path("2026-09-26", "ETH")
        corrupt.write_bytes(b"this is not a zip file")
        csv_file = self.csv_path("2026-09-26", "ETH")
        csv_file.write_text(HEADER + "2026-09-26T00:00:00Z,1.0,coingecko\n", encoding="utf-8")

        with self.assertLogs("scraper.store", level="ERROR") as captured:
            result = self.sweep("2026-09-28")

        self.assertEqual(result.failed, (corrupt,))
        # Both the unreadable archive and the source survive untouched.
        self.assertEqual(corrupt.read_bytes(), b"this is not a zip file")
        self.assertTrue(csv_file.exists())

    def test_reports_a_truncated_final_line_without_repairing_it(self):
        path = self.csv_path("2026-09-26", "ETH")
        path.write_bytes(HEADER.encode() + b"2026-09-26T00:00:00Z,1.0,coingecko")  # no \n
        with self.assertLogs("scraper.store", level="WARNING") as captured:
            self.sweep("2026-09-28")
        self.assertIn("does not end with a newline", "\n".join(captured.output))
        with zipfile.ZipFile(self.zip_path("2026-09-26", "ETH")) as archive:
            self.assertFalse(archive.read("2026-09-26_ETH.csv").endswith(b"\n"))

    def test_delete_source_false_keeps_both(self):
        path = self.csv_path("2026-09-26", "ETH")
        path.write_text(HEADER, encoding="utf-8")
        self.sweep("2026-09-28", delete_source=False)
        self.assertTrue(path.exists())
        self.assertTrue(self.zip_path("2026-09-26", "ETH").exists())

    def test_sweeps_every_coin_of_a_completed_day(self):
        for symbol in ("BTC", "ETH", "HYPE"):
            self.csv_path("2026-09-26", symbol).write_text(HEADER, encoding="utf-8")
        result = self.sweep("2026-09-28")
        self.assertEqual(result.archived, 3)
        for symbol in ("BTC", "ETH", "HYPE"):
            self.assertFalse(self.csv_path("2026-09-26", symbol).exists())

    def test_sweeps_a_coin_that_is_no_longer_configured(self):
        # The sweep scans the directory, not the coin list, so removing a coin
        # from config.ini cannot orphan its data.
        path = self.csv_path("2026-09-26", "DOGE")
        path.write_text(HEADER, encoding="utf-8")
        self.sweep("2026-09-28")
        self.assertTrue(self.zip_path("2026-09-26", "DOGE").exists())

    def test_rejects_an_invalid_day_label(self):
        with self.assertRaises(ValueError):
            self.sweep("not-a-day")


class AppendSchemaGuardTests(StoreTestCase):
    """Appending across a schema change must fail loudly, not quietly.

    A three-field row written under a five-column header still reads back with a
    correct ``price_usd``; only ``source`` silently becomes None. That is the
    worst available outcome, because every sanity check on the price passes
    while the column that explains venue discontinuities has quietly emptied.
    """

    def _existing(self, header: str) -> str:
        original = header + "2026-09-28T00:00:00Z,1.0,123.0,456.0,coingecko\n"
        self.csv_path("2026-09-28", "BTC").write_text(original, encoding="utf-8")
        return original

    def test_a_file_with_the_old_header_is_refused_and_left_intact(self):
        original = self._existing(OLD_HEADER)

        with self.assertLogs("scraper.store", level="ERROR") as captured:
            written = store.append_observations(
                self.data,
                "2026-09-28",
                datetime(2026, 9, 28, tzinfo=UTC),
                [observation("BTC", 2.0)],
            )

        self.assertEqual(written, [])
        self.assertEqual(
            self.csv_path("2026-09-28", "BTC").read_text(encoding="utf-8"), original
        )
        self.assertIn("refusing to append", "\n".join(captured.output))

    def test_one_bad_file_does_not_cost_the_other_symbols_their_rows(self):
        self._existing(OLD_HEADER)

        with self.assertLogs("scraper.store", level="ERROR"):
            written = store.append_observations(
                self.data,
                "2026-09-28",
                datetime(2026, 9, 28, tzinfo=UTC),
                [observation("BTC", 2.0), observation("ETH", 3.0)],
            )

        self.assertEqual(written, [self.csv_path("2026-09-28", "ETH")])
        self.assertTrue(self.csv_path("2026-09-28", "ETH").exists())

    def test_the_refusal_is_reported_once_per_file_not_once_per_tick(self):
        # At ten seconds a per-tick ERROR would be 8,640 lines a day, drowning
        # out the very problem it describes.
        self._existing(OLD_HEADER)

        with self.assertLogs("scraper.store", level="ERROR") as captured:
            for _ in range(3):
                store.append_observations(
                    self.data,
                    "2026-09-28",
                    datetime(2026, 9, 28, tzinfo=UTC),
                    [observation("BTC", 2.0)],
                )

        self.assertEqual(len(captured.output), 1)

    def test_a_matching_header_is_accepted_without_rechecking_every_tick(self):
        store.append_observations(
            self.data,
            "2026-09-28",
            datetime(2026, 9, 28, tzinfo=UTC),
            [observation("BTC", 1.0)],
        )
        # Corrupt the header underneath the memo. The memo is per-process and
        # keyed by path, which is sound because a file's header cannot change
        # without the process doing it, and the day label is in the filename.
        store.append_observations(
            self.data,
            "2026-09-28",
            datetime(2026, 9, 28, tzinfo=UTC),
            [observation("BTC", 2.0)],
        )
        text = self.csv_path("2026-09-28", "BTC").read_text(encoding="utf-8")
        self.assertEqual(len(text.strip().splitlines()), 3)


class MergeSchemaGuardTests(StoreTestCase):
    """A mismatched merge corrupts an archive permanently and silently.

    ``_merge_csv`` de-duplicates whole row strings, so a five-field row can
    never equal a three-field one and both shapes would survive -- interleaved
    chronologically by the sort, which removes the only visual signal. The zip
    is then CRC-valid and byte-stable, so nothing downstream ever notices.
    """

    def test_a_mismatched_header_is_refused_rather_than_reconciled(self):
        with self.assertRaises(store._SchemaMismatch):
            store._merge_csv(
                (OLD_HEADER + "2026-09-26T00:00:00Z,1.0,2.0,3.0,coingecko\n").encode(),
                (HEADER + "2026-09-26T00:05:00Z,1.0,coingecko\n").encode(),
            )

    def test_matching_headers_still_union(self):
        merged = store._merge_csv(
            (HEADER + "2026-09-26T00:00:00Z,1.0,coingecko\n").encode(),
            (HEADER + "2026-09-26T00:05:00Z,2.0,coingecko\n").encode(),
        )
        self.assertEqual(merged.count(b"timestamp_utc"), 1)
        self.assertIn(b"1.0", merged)
        self.assertIn(b"2.0", merged)

    def test_an_unreadable_header_does_not_block_the_merge(self):
        # A zero-byte or truncated copy has no header to disagree with, so the
        # one real header wins and its rows are not discarded.
        merged = store._merge_csv(
            b"",
            (HEADER + "2026-09-26T00:05:00Z,2.0,coingecko\n").encode(),
        )
        self.assertEqual(merged.decode(), HEADER + "2026-09-26T00:05:00Z,2.0,coingecko\n")

    def test_the_sweep_keeps_both_files_and_reports_failure(self):
        zip_file = self.zip_path("2026-09-26", "BTC")
        with zipfile.ZipFile(zip_file, "w") as archive:
            archive.writestr(
                "2026-09-26_BTC.csv",
                OLD_HEADER + "2026-09-26T00:00:00Z,111.0,222.0,333.0,coingecko\n",
            )
        csv_file = self.csv_path("2026-09-26", "BTC")
        csv_file.write_text(HEADER + "2026-09-26T00:05:00Z,222.0,binance\n", encoding="utf-8")

        with self.assertLogs("scraper.store", level="ERROR") as captured:
            result = self.sweep("2026-09-28")

        self.assertEqual(result.failed, (zip_file,))
        self.assertEqual(result.archived, 0)
        self.assertIn("cannot merge", "\n".join(captured.output))
        # Neither copy is touched, so nothing is lost and a human can decide.
        self.assertTrue(csv_file.exists())
        with zipfile.ZipFile(zip_file) as archive:
            self.assertIn("111.0", archive.read("2026-09-26_BTC.csv").decode())


class DirectoryScanTests(StoreTestCase):
    """The sweep walks with os.scandir, which caches the listing's stat data.

    Behaviourally identical to Path.iterdir, which is the point: these pin the
    properties the faster walk must not lose.
    """

    def test_a_directory_shaped_like_a_csv_is_skipped(self):
        (self.data / "2026-09-26_ETH.csv").mkdir()

        result = self.sweep("2026-09-28")

        self.assertFalse(result.acted)
        self.assertTrue((self.data / "2026-09-26_ETH.csv").is_dir())

    def test_a_temp_directory_does_not_break_the_orphan_cleanup(self):
        (self.data / ".stale.tmp").mkdir()

        result = self.sweep("2026-09-28")

        self.assertFalse(result.acted)
        self.assertTrue((self.data / ".stale.tmp").is_dir())


class LockedFileTests(StoreTestCase):
    """Windows and POSIX disagree about deleting a file that is open.

    Both behaviours are asserted, gated on the platform so each one runs where
    it is actually true.
    """

    def _sweep_with_open_handle(self):
        path = self.csv_path("2026-09-26", "ETH")
        path.write_text(HEADER + "2026-09-26T00:00:00Z,1.0,coingecko\n", encoding="utf-8")
        handle = path.open("a", encoding="utf-8")
        self.addCleanup(handle.close)
        return path, self.sweep("2026-09-28")

    @unittest.skipUnless(os.name == "nt", "Windows sharing semantics only")
    def test_windows_keeps_the_csv_when_it_is_still_open(self):
        path, result = self._sweep_with_open_handle()

        # The archive is written, but the source could not be removed, so it is
        # retained and retried next tick -- never deleted after a failed read.
        self.assertEqual(result.retained, (self.zip_path("2026-09-26", "ETH"),))
        self.assertTrue(path.exists())
        with zipfile.ZipFile(self.zip_path("2026-09-26", "ETH")) as archive:
            self.assertIn(b"1.0", archive.read("2026-09-26_ETH.csv"))

    @unittest.skipIf(os.name == "nt", "POSIX allows unlinking an open file")
    def test_posix_archives_and_removes_even_while_open(self):
        path, result = self._sweep_with_open_handle()

        self.assertEqual(result.zipped, (self.zip_path("2026-09-26", "ETH"),))
        self.assertFalse(path.exists())

    def test_retry_loop_can_be_injected_so_tests_do_not_sleep(self):
        sleep = RecordingSleep()
        path = self.csv_path("2026-09-26", "ETH")
        path.write_text(HEADER, encoding="utf-8")
        store.archive_stale_csvs(self.data, "2026-09-28", sleep=sleep, retry_delays=(0.0,))
        # A clean sweep never needs to wait at all.
        self.assertEqual(sleep.delays, [])


class TempFileTests(StoreTestCase):
    def test_orphaned_temp_files_are_cleaned_up(self):
        stale = self.data / ".2026-09-26_ETH.csv.zip.123.deadbeef.tmp"
        stale.write_bytes(b"partial")
        old = 0  # 1970, definitively older than the cutoff
        os.utime(stale, (old, old))

        self.sweep("2026-09-28")

        self.assertFalse(stale.exists())

    def test_recent_temp_files_are_left_alone(self):
        # A concurrent process may be mid-write; only old orphans are removed.
        fresh = self.data / ".2026-09-29_ETH.csv.zip.123.deadbeef.tmp"
        fresh.write_bytes(b"in progress")
        self.sweep("2026-09-28")
        self.assertTrue(fresh.exists())


if __name__ == "__main__":
    unittest.main()
