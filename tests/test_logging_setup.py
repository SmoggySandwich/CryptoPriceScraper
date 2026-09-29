"""Logging setup, especially the no-console (pythonw) case."""

from __future__ import annotations

import io
import logging
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from scraper.config import LoggingConfig
from scraper.logging_setup import (
    RateLimitedLogger,
    UtcFormatter,
    log_unhandled_exception,
    setup_logging,
)


class LoggingTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.cfg = LoggingConfig(
            log_dir=self.base / "logs",
            filename="test.log",
            console_level=logging.INFO,
            file_level=logging.DEBUG,
            max_bytes=65536,
            backup_count=1,
        )
        self.addCleanup(self._reset)
        # setup_logging attaches a console handler whenever sys.stderr exists,
        # so without this the records under test would print into the suite
        # output. Redirecting also makes "a stream exists" a controlled fact
        # rather than a property of however the suite happens to be invoked.
        self.stderr = io.StringIO()
        self.addCleanup(setattr, sys, "stderr", sys.stderr)
        sys.stderr = self.stderr

    def _reset(self):
        package = logging.getLogger("scraper")
        for handler in list(package.handlers):
            handler.close()
            package.removeHandler(handler)

    def log_file(self) -> Path:
        return self.cfg.log_dir / self.cfg.filename


class UtcFormatterTests(unittest.TestCase):
    def test_renders_utc_with_a_z_suffix(self):
        record = logging.LogRecord(
            "scraper.test", logging.INFO, __file__, 1, "hello", None, None
        )
        record.created = datetime(2026, 9, 28, 0, 5, 3, tzinfo=UTC).timestamp()
        self.assertEqual(UtcFormatter().formatTime(record), "2026-09-28T00:05:03.000Z")

    def test_is_not_affected_by_the_local_timezone(self):
        # logging defaults its converter to time.localtime; ours must not.
        record = logging.LogRecord(
            "scraper.test", logging.INFO, __file__, 1, "hello", None, None
        )
        record.created = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC).timestamp()
        self.assertEqual(UtcFormatter().formatTime(record), "2026-01-01T12:00:00.000Z")


class SetupTests(LoggingTestCase):
    def test_writes_to_the_log_file(self):
        logger = setup_logging(self.cfg)
        logger.info("hello from the test")
        self.assertIn("hello from the test", self.log_file().read_text(encoding="utf-8"))

    def test_records_a_utc_timestamp(self):
        logger = setup_logging(self.cfg)
        logger.info("stamped")
        line = self.log_file().read_text(encoding="utf-8").strip().splitlines()[-1]
        self.assertRegex(line, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z ")
        self.assertIn("pid=", line)

    def test_reattaching_does_not_stack_handlers(self):
        # Reconfiguring in-process must not duplicate every line.
        setup_logging(self.cfg)
        logger = setup_logging(self.cfg)
        logger.info("once")
        text = self.log_file().read_text(encoding="utf-8")
        self.assertEqual(text.count("once"), 1)

    def test_console_handler_is_attached_when_a_stream_exists(self):
        logger = setup_logging(self.cfg)
        kinds = {type(h) for h in logger.handlers}
        self.assertIn(logging.StreamHandler, kinds)
        logger.info("visible on the console")
        self.assertIn("visible on the console", self.stderr.getvalue())

    def test_no_stream_is_survivable(self):
        """The pythonw case.

        Under pythonw.exe, sys.stderr is None rather than merely unusable. A
        StreamHandler built on None raises inside logging's emit path, and
        logging's error handling then checks sys.stderr (None is falsy) and
        swallows the record -- so every log line would vanish silently. The
        guard must skip the console handler instead.
        """
        original = sys.stderr
        sys.stderr = None
        try:
            logger = setup_logging(self.cfg)
            kinds = {type(h) for h in logger.handlers}
            self.assertNotIn(logging.StreamHandler, kinds)
            logger.info("still recorded")  # must not raise
        finally:
            sys.stderr = original

        text = self.log_file().read_text(encoding="utf-8")
        self.assertIn("still recorded", text)
        self.assertIn("console logging disabled", text)

    def test_creates_the_log_directory(self):
        setup_logging(self.cfg)
        self.assertTrue(self.cfg.log_dir.is_dir())


class CrashLogTests(LoggingTestCase):
    def test_writes_a_traceback_for_a_pre_logging_crash(self):
        # Under pythonw a startup failure has no visible output at all, so this
        # file is the only record.
        try:
            raise RuntimeError("boom before logging existed")
        except RuntimeError as exc:
            log_unhandled_exception(exc, self.base)

        crash = self.base / "logs" / "main.crash.log"
        text = crash.read_text(encoding="utf-8")
        self.assertIn("boom before logging existed", text)
        self.assertIn("RuntimeError", text)

    def test_never_raises_even_if_the_log_directory_cannot_be_created(self):
        # Point at a path whose parent is a file, not a directory.
        blocker = self.base / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        log_unhandled_exception(RuntimeError("x"), blocker)  # must not raise

    def test_the_crash_log_is_bounded(self):
        # This file is only ever written when something has already gone badly
        # wrong, and it used to append without any rotation at all -- so the
        # worse the problem, the larger the file describing it grew.
        from scraper.logging_setup import _CRASH_LOG_MAX_BYTES

        crash = self.base / "logs" / "main.crash.log"
        crash.parent.mkdir(parents=True, exist_ok=True)
        crash.write_text("x" * (_CRASH_LOG_MAX_BYTES + 1), encoding="utf-8")

        log_unhandled_exception(RuntimeError("after the big one"), self.base)

        # One previous generation is kept, and the current file starts over.
        self.assertTrue(crash.with_name("main.crash.log.1").exists())
        text = crash.read_text(encoding="utf-8")
        self.assertIn("after the big one", text)
        self.assertLess(len(text), _CRASH_LOG_MAX_BYTES)

    def test_a_small_crash_log_is_not_rotated(self):
        crash = self.base / "logs" / "main.crash.log"
        crash.parent.mkdir(parents=True, exist_ok=True)
        crash.write_text("small\n", encoding="utf-8")

        log_unhandled_exception(RuntimeError("second"), self.base)

        self.assertFalse(crash.with_name("main.crash.log.1").exists())
        self.assertIn("small", crash.read_text(encoding="utf-8"))


class RateLimitedLoggerTests(unittest.TestCase):
    """The first occurrence is never suppressed -- it is the one that matters."""

    def setUp(self):
        self.clock = [1000.0]
        self.limiter = RateLimitedLogger(every=300.0, now=lambda: self.clock[0])
        self.logger = logging.getLogger("scraper.test.repeat")
        self.logger.setLevel(logging.DEBUG)

    def test_the_first_occurrence_always_logs(self):
        self.assertTrue(self.limiter.should_log(("BTC", "binance")))

    def test_repeats_within_the_window_are_suppressed(self):
        self.limiter.should_log(("BTC", "binance"))
        self.clock[0] += 299.0
        self.assertFalse(self.limiter.should_log(("BTC", "binance")))

    def test_the_window_reopens(self):
        self.limiter.should_log(("BTC", "binance"))
        self.clock[0] += 300.0
        self.assertTrue(self.limiter.should_log(("BTC", "binance")))

    def test_keys_are_independent(self):
        # A second symbol going missing must be reported even though the first
        # one was reported a moment ago.
        self.limiter.should_log(("BTC", "binance"))
        self.assertTrue(self.limiter.should_log(("HYPE", "binance")))

    def test_log_writes_through_at_the_recorded_level(self):
        with self.assertLogs(self.logger, level="WARNING") as captured:
            self.limiter.log(self.logger, logging.WARNING, ("BTC", "binance"), "gone: %s", "BTC")
        self.assertEqual(captured.records[0].getMessage(), "gone: BTC")

    def test_a_suppressed_repeat_writes_nothing(self):
        # Wrapped in assertLogs rather than called bare so the expected first
        # record is captured instead of propagating to the suite's output.
        with self.assertLogs(self.logger, level="WARNING"):
            self.limiter.log(self.logger, logging.WARNING, ("BTC", "binance"), "gone")
        with self.assertNoLogs(self.logger, level="DEBUG"):
            self.limiter.log(self.logger, logging.WARNING, ("BTC", "binance"), "gone again")


if __name__ == "__main__":
    unittest.main()
