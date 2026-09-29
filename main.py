"""Crypto price scraper -- entry point.

Two run modes share one tick implementation:

* ``python main.py`` runs forever, sleeping until the next wall-clock grid slot.
* ``python main.py --once`` polls once and exits, for Task Scheduler or cron.

Exit codes, which both schedulers surface as the run result:

===== =========================================================
``0`` Ran; rows were written, or the tick was a benign no-op.
``1`` Configuration or startup error -- nothing was attempted.
``2`` Another instance holds the lock (loop mode only).
``3`` Every source failed; no rows written. Gaps stay gaps.
``4`` Unexpected crash.
===== =========================================================
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from scraper import __version__, timeutil
from scraper.config import Config, ConfigError, load_config
from scraper.timeutil import BASE_DIR
from scraper.fetch import PriceFetcher
from scraper.locking import InstanceLock, NullLock
from scraper.logging_setup import RateLimitedLogger, log_unhandled_exception, setup_logging
from scraper.store import append_observations, archive_stale_csvs, ensure_dirs

logger = logging.getLogger("scraper.main")

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_LOCKED = 2
EXIT_NO_DATA = 3
EXIT_UNEXPECTED = 4

#: Ticks between periodic INFO summaries. At the 10s default that is one line
#: every ten minutes, about 144 a day, instead of 8,640.
SUMMARY_EVERY_TICKS = 60

#: Consecutive failed ticks before the loop gives up. At the 10s default that is
#: five minutes of solid failure -- long enough to ride out a network blip,
#: short enough that a genuinely wedged process still exits for a supervisor to
#: notice rather than sitting there looking alive and collecting nothing.
MAX_CONSECUTIVE_FAILURES = 30

#: "No price available for HYPE" repeats on every tick for as long as it lasts.
_REPEAT = RateLimitedLogger(every=300.0)


@dataclass(frozen=True)
class TickResult:
    """What one tick saw and did."""

    observed: int
    written: int
    archived: int
    missing: tuple[str, ...]
    prices: tuple[tuple[str, float, str], ...] = ()


@dataclass
class TickSummary:
    """Rolling tick statistics, reported every :data:`SUMMARY_EVERY_TICKS`.

    Loop mode discards each :class:`TickResult` as soon as it is returned, and
    the per-tick success line is far too verbose to read at 8,640 lines a day.
    Without this rollup the only durable health signal would be a log stream
    nobody scrolls. It also restores what ``--once`` used to get for free from
    the scheduler's run history: a per-run record of whether the job worked.
    """

    ticks: int = 0
    ticks_with_rows: int = 0
    rows: int = 0
    max_seconds: float = 0.0
    sources: dict[str, Counter] = field(default_factory=dict)
    missing: Counter = field(default_factory=Counter)

    def record(self, result: TickResult, seconds: float) -> None:
        self.ticks += 1
        if result.written:
            self.ticks_with_rows += 1
        self.rows += result.written
        self.max_seconds = max(self.max_seconds, seconds)
        for symbol, _price, source in result.prices:
            self.sources.setdefault(symbol, Counter())[source] += 1
        self.missing.update(result.missing)

    def reset(self) -> None:
        """Start a fresh window. Called after each summary is logged."""
        self.ticks = 0
        self.ticks_with_rows = 0
        self.rows = 0
        self.max_seconds = 0.0
        self.sources.clear()
        self.missing.clear()

    def render(self, consecutive_failures: int) -> str:
        parts = [
            f"{self.ticks} ticks",
            f"{self.ticks_with_rows} with rows",
            f"{self.rows} rows",
        ]
        if self.sources:
            parts.append(
                "sources "
                + " ".join(
                    f"{symbol}="
                    + "/".join(f"{name}:{count}" for name, count in sorted(counts.items()))
                    for symbol, counts in sorted(self.sources.items())
                )
            )
        if self.missing:
            parts.append(
                "missing "
                + " ".join(f"{symbol}:{count}" for symbol, count in sorted(self.missing.items()))
            )
        parts.append(f"slowest tick {self.max_seconds:.2f}s")
        parts.append(f"{consecutive_failures} consecutive failures")
        return ", ".join(parts)


def tick(
    cfg: Config,
    *,
    client: PriceFetcher,
    now_fn=timeutil.utcnow,
    logger: logging.Logger = logger,
) -> TickResult:
    """Fetch, roll the day over if needed, then append today's rows.

    A single clock reading drives both the day label and the row timestamps.
    That is what makes the archive step safe to run before the append: the tick
    cannot archive the day it is about to write into, and both steps agree on
    which day that is.

    The fetch failing is never fatal here -- the rollover still runs, so a
    CoinGecko outage cannot starve the archiving of a completed day.
    """
    observations, missing = client.fetch_all(cfg.collection.coins)

    now = now_fn()
    day = timeutil.day_label(now)

    archive = archive_stale_csvs(
        cfg.storage.data_dir,
        day,
        delete_source=cfg.storage.delete_source,
        compresslevel=cfg.storage.compresslevel,
        retry_delays=cfg.storage.remove_retry_delays,
        logger=logger,
    )
    if archive.acted:
        logger.info(
            "rollover: %d zipped, %d merged, %d retained, %d failed",
            len(archive.zipped),
            len(archive.merged),
            len(archive.retained),
            len(archive.failed),
        )

    written = append_observations(
        cfg.storage.data_dir, day, now, observations, logger=logger
    )

    if observations:
        # DEBUG, not INFO: at 8,640 ticks a day this one line is over a megabyte,
        # which is more log than the events it describes. The isEnabledFor guard
        # matters because the join below is eager -- without it the string is
        # built on every tick no matter what the level is set to. The periodic
        # summary in run_forever carries the signal this used to.
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "poll ok: %s",
                " ".join(f"{obs.symbol}={obs.price_usd:g}@{obs.source}" for obs in observations),
            )
    else:
        logger.warning("poll wrote no rows for %s", day)
    if missing:
        # Keyed on the exact set, so a change in which symbols are missing is
        # reported immediately rather than waiting out the rate limit.
        _REPEAT.log(
            logger,
            logging.WARNING,
            ("missing", *sorted(missing)),
            "no price available for: %s",
            ", ".join(sorted(missing)),
        )

    return TickResult(
        observed=len(observations),
        written=len(written),
        archived=archive.archived,
        missing=tuple(sorted(missing)),
        prices=tuple((obs.symbol, obs.price_usd, obs.source) for obs in observations),
    )


def run_once(
    cfg: Config,
    *,
    client: PriceFetcher,
    now_fn=timeutil.utcnow,
    lock=None,
    logger: logging.Logger = logger,
) -> int:
    """Poll once and exit."""
    lock = lock if lock is not None else InstanceLock(
        cfg.storage.data_dir,
        wait_seconds=cfg.storage.lock_wait_seconds,
        logger=logger,
    )
    if not lock.acquire():
        # Benign under a scheduler: it only means the loop already has it. Exit
        # 0 rather than 2 so a contended run is not recorded as a failure.
        logger.info("another instance is running; nothing to do")
        return EXIT_OK
    try:
        result = tick(cfg, client=client, now_fn=now_fn, logger=logger)
    finally:
        lock.release()
    return EXIT_OK if result.written else EXIT_NO_DATA


def run_forever(
    cfg: Config,
    *,
    client: PriceFetcher,
    now_fn=timeutil.utcnow,
    sleep=time.sleep,
    clock=time.monotonic,
    lock=None,
    max_ticks: int | None = None,
    logger: logging.Logger = logger,
) -> int:
    """Poll on the wall-clock grid until interrupted."""
    lock = lock if lock is not None else InstanceLock(
        cfg.storage.data_dir,
        wait_seconds=cfg.storage.lock_wait_seconds,
        logger=logger,
    )
    if not lock.acquire():
        logger.error("another instance is running; refusing to start a second")
        return EXIT_LOCKED

    ticks = 0
    consecutive_failures = 0
    summary = TickSummary()
    try:
        while True:
            # An immediate first poll: no reason to wait a full interval just
            # because the process started mid-slot.
            started = clock()
            try:
                result = tick(cfg, client=client, now_fn=now_fn, logger=logger)
            except Exception:  # noqa: BLE001 - one bad tick must not end the run
                # Loop mode is the deployment, so there is no scheduler to
                # restart a dead process. Under --once each tick was a fresh
                # process and systemd's Restart=always caught a crash; now a
                # transient PermissionError from a scanner holding today's CSV
                # would otherwise end the run permanently and silently, and the
                # loss would surface as a gap in the data days later.
                consecutive_failures += 1
                logger.exception(
                    "tick failed (%d consecutive); continuing", consecutive_failures
                )
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    logger.error(
                        "giving up after %d consecutive failures (about %.0f minutes "
                        "with nothing collected)",
                        consecutive_failures,
                        consecutive_failures * cfg.collection.poll_seconds / 60,
                    )
                    return EXIT_UNEXPECTED
            else:
                consecutive_failures = 0
                summary.record(result, clock() - started)
            ticks += 1

            if ticks % SUMMARY_EVERY_TICKS == 0:
                logger.info("summary: %s", summary.render(consecutive_failures))
                summary.reset()
            if max_ticks is not None and ticks >= max_ticks:
                return EXIT_OK

            # Recomputed from the clock every iteration, never accumulated from
            # the previous tick, so a suspend or a long stall cannot produce a
            # catch-up burst. next_grid_slot is always strictly in the future.
            now = now_fn()
            target = timeutil.next_grid_slot(now, cfg.collection.poll_seconds)
            delay = timeutil.seconds_until(target, now)
            logger.debug("next poll at %s (in %.1fs)", timeutil.iso_z(target), delay)
            sleep(delay)
    except KeyboardInterrupt:
        logger.info("interrupted; shutting down")
        if ticks % SUMMARY_EVERY_TICKS:
            # A service that is restarted more often than the summary interval
            # would otherwise never emit one at all.
            logger.info("summary (partial): %s", summary.render(consecutive_failures))
        return EXIT_OK
    finally:
        lock.release()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="crypto-price-scraper",
        description=(
            "Poll free crypto APIs into one CSV per coin per trading day, "
            "archiving each day's CSV once the day ends."
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help=(
            "poll once and exit. A diagnostic, not a deployment: no scheduler "
            "can run a job every 10 seconds, so a scheduled --once gives you "
            "poll_seconds minutes, not seconds. Use loop mode."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help="config file to use (default: config.ini beside main.py)",
    )
    parser.add_argument("--verbose", action="store_true", help="log at DEBUG level")
    parser.add_argument(
        "--no-lock",
        action="store_true",
        help="skip the single-instance lock (manual testing only)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def _install_sigterm_handler() -> None:
    """Turn SIGTERM into KeyboardInterrupt so the lock is always released."""
    if not hasattr(signal, "SIGTERM"):
        return

    def _handler(signum, frame):  # pragma: no cover - signal delivery
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, _handler)
    except (ValueError, OSError):
        # Not on the main thread, or unsupported here; not fatal.
        pass


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        # stderr alone is not enough. The scheduled task runs pythonw.exe, where
        # sys.stderr is None, so print() would be a no-op and the task would
        # exit 1 in total silence -- registered, "Ready", restarting three
        # times, and collecting nothing, with no evidence anywhere. The crash
        # log is the one channel that survives a failure this early.
        print(f"error: {exc}", file=sys.stderr)
        log_unhandled_exception(exc, BASE_DIR)
        return EXIT_CONFIG

    try:
        setup_logging(cfg.logging, verbose=args.verbose)
        ensure_dirs(cfg.storage.data_dir, cfg.logging.log_dir)
    except Exception as exc:  # noqa: BLE001 - startup must never traceback silently
        print(f"error: could not start logging: {exc}", file=sys.stderr)
        log_unhandled_exception(exc, cfg.base_dir)
        return EXIT_CONFIG

    # Deferred until the configured handlers exist. Emitted before the
    # "starting" line so a misconfiguration is the first thing in the log.
    for message in cfg.warnings:
        logger.warning("%s", message)

    logger.info(
        "starting: config=%s data_dir=%s interval=%ds coins=%s now_utc=%s",
        cfg.source_path,
        cfg.storage.data_dir,
        cfg.collection.poll_seconds,
        ",".join(sorted(cfg.collection.coins)),
        timeutil.iso_z(timeutil.utcnow()),
    )

    if args.once and cfg.collection.poll_seconds < 60:
        # Only the loop reads poll_seconds, so a scheduled --once silently
        # delivers whatever the scheduler's floor is -- one minute for cron, and
        # Task Scheduler refuses sub-minute triggers outright. Someone who set
        # poll_seconds=10 and scheduled --once would believe they had ten-second
        # data while getting sixty-second data.
        logger.warning(
            "--once ignores poll_seconds=%d; it polls exactly once per invocation, "
            "so the real cadence is whatever runs it (cron floors at 60s). "
            "Use loop mode for sub-minute resolution.",
            cfg.collection.poll_seconds,
        )

    client = PriceFetcher(cfg.api, cfg.collection)
    lock = (
        NullLock()
        if args.no_lock
        else InstanceLock(
            cfg.storage.data_dir,
            wait_seconds=cfg.storage.lock_wait_seconds,
            logger=logger,
        )
    )

    _install_sigterm_handler()
    try:
        if args.once:
            return run_once(cfg, client=client, lock=lock)
        return run_forever(cfg, client=client, lock=lock)
    except KeyboardInterrupt:
        logger.info("interrupted")
        return EXIT_OK
    except Exception as exc:  # noqa: BLE001 - last line of defence
        logger.exception("unhandled error")
        log_unhandled_exception(exc, cfg.base_dir)
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    sys.exit(main())
