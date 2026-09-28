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
from dataclasses import dataclass
from pathlib import Path

from scraper import __version__, timeutil
from scraper.config import Config, ConfigError, load_config
from scraper.fetch import PriceFetcher
from scraper.locking import InstanceLock, NullLock
from scraper.logging_setup import log_unhandled_exception, setup_logging
from scraper.store import append_observations, archive_stale_csvs, ensure_dirs

logger = logging.getLogger("scraper.main")

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_LOCKED = 2
EXIT_NO_DATA = 3
EXIT_UNEXPECTED = 4


@dataclass(frozen=True)
class TickResult:
    """What one tick saw and did."""

    observed: int
    written: int
    archived: int
    missing: tuple[str, ...]
    prices: tuple[tuple[str, float, str], ...] = ()


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
        logger.info(
            "poll ok: %s",
            " ".join(f"{obs.symbol}={obs.price_usd:g}@{obs.source}" for obs in observations),
        )
    else:
        logger.warning("poll wrote no rows for %s", day)
    if missing:
        logger.warning("no price available for: %s", ", ".join(sorted(missing)))

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
    try:
        while True:
            # An immediate first poll: no reason to wait a full interval just
            # because the process started mid-slot.
            tick(cfg, client=client, now_fn=now_fn, logger=logger)
            ticks += 1
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
        help="poll once and exit, for Task Scheduler or cron",
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
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    try:
        setup_logging(cfg.logging, verbose=args.verbose)
        ensure_dirs(cfg.storage.data_dir, cfg.logging.log_dir)
    except Exception as exc:  # noqa: BLE001 - startup must never traceback silently
        print(f"error: could not start logging: {exc}", file=sys.stderr)
        log_unhandled_exception(exc, cfg.base_dir)
        return EXIT_CONFIG

    logger.info(
        "starting: config=%s data_dir=%s interval=%ds coins=%s now_utc=%s",
        cfg.source_path,
        cfg.storage.data_dir,
        cfg.collection.poll_seconds,
        ",".join(sorted(cfg.collection.coins)),
        timeutil.iso_z(timeutil.utcnow()),
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
