"""Logging configuration: UTC timestamps, a rotating file handler and a console.

The console handler is conditional, and that is load bearing.  Under
``pythonw.exe`` -- which is how you avoid a console window flashing every five
minutes when the task runs from Task Scheduler -- ``sys.stdout`` and
``sys.stderr`` are not merely closed, they are ``None``.  A ``StreamHandler``
built on ``None`` raises inside ``logging``'s emit path, and ``logging``'s own
error handling then checks ``sys.stderr`` (falsy) and swallows the record
silently.  Every log line would vanish with no trace.
"""

from __future__ import annotations

import logging
import os
import sys
import time
import traceback
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import LoggingConfig

_LOG_FORMAT = "%(asctime)s %(levelname)-8s [pid=%(process)d] %(name)s: %(message)s"

#: The crash log is written outside the logging system and so has no rotation of
#: its own. It is bounded here rather than left to grow without limit: a crash
#: loop is exactly when this file matters, and is also when it would fill a disk.
_CRASH_LOG_MAX_BYTES = 1_048_576


class RateLimitedLogger:
    """Log a repeated message on first sight, then at most every ``every`` seconds.

    The failure this exists for is a single config typo: one bad symbol makes
    Binance's batch endpoint answer 400 on every tick, which at a ten-second
    cadence is 8,640 identical WARNINGs a day. Those lines would consume the
    entire log budget and rotate out the earlier entries that explain how the
    problem started. The first occurrence is never suppressed, because that is
    the one that tells you what changed.
    """

    def __init__(self, every: float = 300.0, *, now=time.monotonic) -> None:
        self._every = every
        self._now = now
        self._last: dict[tuple, float] = {}

    def should_log(self, key: tuple) -> bool:
        """Whether ``key`` is due. Records the attempt when it returns ``True``."""
        moment = self._now()
        previous = self._last.get(key)
        if previous is not None and moment - previous < self._every:
            return False
        self._last[key] = moment
        return True

    def log(self, target: logging.Logger, level: int, key: tuple, message: str, *args) -> None:
        if self.should_log(key):
            target.log(level, message, *args)


def _rotate_crash_log(path: Path) -> None:
    """Keep one previous generation of the crash log. Never raises."""
    try:
        if path.stat().st_size < _CRASH_LOG_MAX_BYTES:
            return
        # os.replace rather than unlink-then-rename: it overwrites the backup
        # atomically, and it cannot lose the current log if it fails halfway.
        os.replace(path, path.with_name(path.name + ".1"))
    except OSError:
        pass


class UtcFormatter(logging.Formatter):
    """Format log times in UTC rather than the machine's local time.

    ``logging`` defaults ``converter`` to ``time.localtime``.  Overriding
    ``formatTime`` directly gives exact control over the rendering; ``converter``
    is set as well so anything bypassing ``formatTime`` still lands on UTC.
    """

    converter = time.gmtime

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        moment = datetime.fromtimestamp(record.created, UTC)
        return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def setup_logging(cfg: LoggingConfig, *, verbose: bool = False) -> logging.Logger:
    """Configure the ``scraper`` logger tree and return its root logger."""
    package_logger = logging.getLogger("scraper")
    package_logger.setLevel(logging.DEBUG)
    # Reconfiguring in-process (tests, repeated calls) must not stack handlers.
    for handler in list(package_logger.handlers):
        handler.close()
        package_logger.removeHandler(handler)
    # Keep our records out of the root logger, whose handlers are not ours to own.
    package_logger.propagate = False

    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    formatter = UtcFormatter(_LOG_FORMAT)

    file_handler = RotatingFileHandler(
        cfg.log_dir / cfg.filename,
        maxBytes=cfg.max_bytes,
        backupCount=cfg.backup_count,
        encoding="utf-8",
        delay=False,
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG if verbose else cfg.file_level)
    package_logger.addHandler(file_handler)

    if sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(formatter)
        console.setLevel(logging.DEBUG if verbose else cfg.console_level)
        package_logger.addHandler(console)
    else:
        package_logger.info("console logging disabled: no stream attached (pythonw)")

    return package_logger


def log_unhandled_exception(exc: BaseException, base_dir: Path) -> None:
    """Record a crash that happened before, or outside, the logging system.

    Under ``pythonw`` a startup failure produces no visible output at all, so a
    plain unbuffered append is the only channel left.
    """
    crash_log = base_dir / "logs" / "main.crash.log"
    try:
        crash_log.parent.mkdir(parents=True, exist_ok=True)
        _rotate_crash_log(crash_log)
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        with crash_log.open("a", encoding="utf-8") as handle:
            handle.write(f"\n===== {stamp} =====\n")
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=handle)
    except OSError:
        # Nothing further we can do; never raise from the crash handler.
        pass
