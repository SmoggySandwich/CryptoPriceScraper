"""CSV writing and the daily rollover: archive yesterday, append to today.

The rollover is *stateless*.  Rather than tracking "the previous day" in memory
or in a state file, every tick scans the data directory and archives any CSV
whose filename date is strictly before today.

That choice is forced by the ``--once`` run mode: the process exits every five
minutes, so an in-memory notion of yesterday is worthless, and a state file
would be one more thing to go stale or corrupt.  Deriving the work from the
filesystem's own naming means loop mode, one-shot mode and crash recovery all
execute the same code path, and running it twice is harmless.

Two rules make that idempotence real:

* An existing archive is **never overwritten, only merged**.  If the system
  clock steps backwards, a day label can be reused; overwriting would silently
  destroy the rows archived the first time.
* A CSV is deleted only *after* its bytes are safely inside a verified archive.
"""

from __future__ import annotations

import csv
import logging
import os
import re
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from .timeutil import csv_path, iso_z, parse_day_label, zip_path

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps store import-free
    from .fetch import Observation

CSV_COLUMNS: tuple[str, ...] = (
    "timestamp_utc",
    "price_usd",
    "market_cap_usd",
    "vol_24h_usd",
    "source",
)

#: Anchored on purpose.  A ``glob("*.csv")`` would also match temp files and
#: hand-made names; requiring the exact shape means anything we do not
#: recognise is reported rather than quietly swept.
CSV_NAME_RE = re.compile(r"^(?P<day>\d{4}-\d{2}-\d{2})_(?P<symbol>[A-Z0-9]{2,15})\.csv$")

#: Orphaned temp files older than this are cleaned up opportunistically.
_TEMP_MAX_AGE_SECONDS = 3600.0

_DEFAULT_LOGGER = logging.getLogger("scraper.store")


class _LockedError(Exception):
    """A file could not be read because another handle holds it open."""


@dataclass(frozen=True)
class ArchiveResult:
    """What a sweep did, for logging and for tests to assert against."""

    zipped: tuple[Path, ...] = ()
    merged: tuple[Path, ...] = ()
    #: Archived successfully, but the source CSV was still locked so it remains
    #: on disk and will be retried next tick.
    retained: tuple[Path, ...] = ()
    failed: tuple[Path, ...] = ()

    @property
    def archived(self) -> int:
        return len(self.zipped) + len(self.merged) + len(self.retained)

    @property
    def acted(self) -> bool:
        return bool(self.zipped or self.merged or self.retained or self.failed)


def ensure_dirs(*paths: Path) -> None:
    """Create the runtime directories, if they are not already there."""
    for path in paths:
        Path(path).mkdir(parents=True, exist_ok=True)


def _format_number(value: float | None) -> str:
    """Render a number for a CSV cell.

    ``repr`` is used because it round-trips exactly and stays uniform: prices
    arrive as JSON integers (``84386``) and must leave as ``84386.0``, while a
    sub-cent token price must survive as ``1.2345e-07``.  ``%.2f`` would flatten
    the latter to ``0.00``, and rounding would too.

    A missing value becomes an empty field, never ``0``: blank means "not
    published by this source", which is a different claim from a real zero.
    """
    if value is None:
        return ""
    return f"{float(value)!r}"


def append_observations(
    data_dir: Path,
    day: str,
    observed_at,
    observations: Sequence["Observation"],
    *,
    fsync: bool = True,
    logger: logging.Logger = _DEFAULT_LOGGER,
) -> list[Path]:
    """Append one row per observation to that day's per-coin CSV.

    Each file is opened, written, flushed, fsynced and closed within this call.
    Keeping a handle open across ticks would be faster and would also be a bug:
    on Windows a process cannot delete or rename a file that *any* handle holds
    open, including its own, so the rollover could never archive its own CSV.
    """
    data_dir = Path(data_dir)
    timestamp = iso_z(observed_at)
    if timestamp[:10] != day:
        # Should be unreachable: callers thread a single instant through. If it
        # ever fires, a row would be stranded in a file the sweep is about to
        # archive, so make it loud rather than mysterious.
        logger.error("row timestamp %s falls outside day label %s", timestamp, day)

    written: list[Path] = []
    for observation in observations:
        path = csv_path(data_dir, day, observation.symbol)
        existed = path.exists()
        needs_header = not existed or path.stat().st_size == 0

        with path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            if needs_header:
                writer.writerow(CSV_COLUMNS)
            writer.writerow(
                (
                    timestamp,
                    _format_number(observation.price_usd),
                    _format_number(observation.market_cap_usd),
                    _format_number(observation.vol_24h_usd),
                    observation.source,
                )
            )
            # flush() before fsync() matters: fsync alone can miss data still
            # sitting in the Python-level buffer.
            handle.flush()
            if fsync:
                os.fsync(handle.fileno())

        if not existed:
            # On POSIX a brand new directory entry is not durable until the
            # directory itself is fsynced; Linux has no NTFS-style metadata
            # journal. Windows has no such requirement but tolerates the call,
            # so this stays unconditional rather than branching.
            _fsync_dir(path.parent)

        written.append(path)
    return written


def archive_stale_csvs(
    data_dir: Path,
    today: str,
    *,
    delete_source: bool = True,
    compresslevel: int = 9,
    retry_delays: tuple[float, ...] = (0.2, 0.4, 0.8, 1.6),
    sleep=time.sleep,
    logger: logging.Logger = _DEFAULT_LOGGER,
) -> ArchiveResult:
    """Zip and remove every CSV dated strictly before ``today``.

    Scans the directory rather than iterating the configured coins, so a coin
    removed from ``config.ini`` cannot orphan its data forever.
    """
    data_dir = Path(data_dir)
    today_date = parse_day_label(today)
    if today_date is None:
        raise ValueError(f"invalid day label: {today!r}")

    _clean_orphan_temp_files(data_dir, logger)

    zipped: list[Path] = []
    merged: list[Path] = []
    retained: list[Path] = []
    failed: list[Path] = []

    for entry in sorted(data_dir.iterdir()):
        if not entry.is_file() or entry.name.startswith("."):
            continue

        match = CSV_NAME_RE.match(entry.name)
        if match is None:
            if entry.suffix == ".csv":
                # A hand-made "2026-9-7_ETH.csv" would otherwise be ignored
                # forever with no explanation.
                logger.warning(
                    "archive: ignoring %s; expected YYYY-MM-DD_SYMBOL.csv", entry.name
                )
            continue

        day = match.group("day")
        day_date = parse_day_label(day)
        if day_date is None or day_date >= today_date:
            continue

        status, destination = _archive_one(
            entry,
            data_dir,
            day,
            match.group("symbol"),
            delete_source=delete_source,
            compresslevel=compresslevel,
            retry_delays=retry_delays,
            sleep=sleep,
            logger=logger,
        )
        if status == "zipped":
            zipped.append(destination)
            logger.info("archive: zipped %s", entry.name)
        elif status == "merged":
            merged.append(destination)
            logger.info("archive: merged %s into existing %s", entry.name, destination.name)
        elif status == "locked":
            retained.append(destination)
        elif status == "failed":
            failed.append(destination)
        # "gone": the file vanished underneath us, nothing to do.

    return ArchiveResult(
        zipped=tuple(zipped),
        merged=tuple(merged),
        retained=tuple(retained),
        failed=tuple(failed),
    )


def _archive_one(
    csv_file: Path,
    data_dir: Path,
    day: str,
    symbol: str,
    *,
    delete_source: bool,
    compresslevel: int,
    retry_delays: tuple[float, ...],
    sleep,
    logger: logging.Logger,
) -> tuple[str, Path]:
    """Archive a single CSV. Returns ``(status, zip_path)``."""
    destination = zip_path(data_dir, day, symbol)

    try:
        raw = _read_bytes_with_retry(csv_file, retry_delays, sleep, logger)
    except _LockedError:
        logger.info(
            "archive: %s is locked by another process; leaving it for the next tick",
            csv_file.name,
        )
        return "locked", destination
    if raw is None:
        return "gone", destination

    if not raw.endswith(b"\n"):
        logger.warning(
            "archive: %s does not end with a newline (%d bytes); the final row may "
            "be truncated. Archiving as-is rather than repairing.",
            csv_file.name,
            len(raw),
        )

    members: dict[str, bytes] = {}
    existed = destination.exists()
    if existed:
        try:
            archived = _read_zip_members(destination)
        except (OSError, zipfile.BadZipFile) as exc:
            # Never overwrite an archive we cannot read: it may be the only
            # surviving copy of that day.
            logger.error(
                "archive: %s exists but is unreadable (%s); leaving %s in place",
                destination.name,
                exc,
                csv_file.name,
            )
            return "failed", destination
        members.update(archived)
        previous = archived.get(csv_file.name)
        if previous is not None:
            raw = _merge_csv(previous, raw)

    members[csv_file.name] = raw

    day_date = parse_day_label(day)
    # Zip stores a naive DOS local-time tuple, so derive it from the filename
    # rather than the file's mtime; that keeps archives byte-reproducible.
    date_time = (day_date.year, day_date.month, day_date.day, 0, 0, 0) if day_date else (1980, 1, 1, 0, 0, 0)

    try:
        _write_zip(members, destination, date_time=date_time, compresslevel=compresslevel)
    except OSError as exc:
        logger.error(
            "archive: could not write %s (%s); %s kept", destination.name, exc, csv_file.name
        )
        return "failed", destination

    if not delete_source:
        return ("merged" if existed else "zipped"), destination

    if not _remove_with_retry(csv_file, retry_delays, sleep, logger):
        # The archive is safe; only the cleanup is pending.
        return "locked", destination
    return ("merged" if existed else "zipped"), destination


def _write_zip(
    members: dict[str, bytes],
    destination: Path,
    *,
    date_time: tuple[int, int, int, int, int, int],
    compresslevel: int,
) -> None:
    """Write a zip atomically.

    Built in a uniquely named temp file, verified, then moved into place with
    ``os.replace``.  That gives one invariant the rest of the module leans on:
    *if the final name exists, the archive is complete and CRC-valid.*  A crash
    partway through leaves only an ignorable temp file.
    """
    temp = destination.parent / f".{destination.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        with zipfile.ZipFile(
            temp, "w", zipfile.ZIP_DEFLATED, compresslevel=compresslevel
        ) as archive:
            for name in sorted(members):
                info = zipfile.ZipInfo(name, date_time=date_time)
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, members[name])

        with zipfile.ZipFile(temp) as archive:
            bad = archive.testzip()
            if bad is not None:
                raise OSError(f"checksum mismatch for member {bad!r}")

        os.replace(temp, destination)
    finally:
        try:
            temp.unlink()
        except OSError:
            pass


def _read_zip_members(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {
            info.filename: archive.read(info)
            for info in archive.infolist()
            if not info.is_dir()
        }


def _split_csv(raw: bytes) -> tuple[str, list[str]]:
    """Split CSV bytes into ``(header, rows)``, tolerating a missing header."""
    lines = [line for line in raw.decode("utf-8", "replace").splitlines() if line.strip()]
    if not lines:
        return "", []
    if "timestamp_utc" in lines[0]:
        return lines[0], lines[1:]
    # A zero-byte or partially written file has no header; treat every line as
    # data rather than silently discarding the first row.
    return "", lines


def _merge_csv(existing: bytes, incoming: bytes) -> bytes:
    """Union two CSVs that claim the same filename.

    Normally both copies are identical: that is the crash-between-write-and-
    delete case, and the union is a no-op.  But a backwards clock step can
    reuse a day label, and then the archived copy and the new CSV hold genuinely
    different observations.  Picking either one would discard real data, so the
    rows are unioned and de-duplicated by exact content instead.

    Sorting is by the full row, whose first field is an ISO-8601 ``Z``
    timestamp; that sorts lexicographically into chronological order, so the
    result is stable and the merge is idempotent.
    """
    header, rows = _split_csv(existing)
    incoming_header, incoming_rows = _split_csv(incoming)
    header = header or incoming_header

    merged = list(rows)
    seen = set(rows)
    for row in incoming_rows:
        if row not in seen:
            seen.add(row)
            merged.append(row)
    merged.sort()

    lines = ([header] if header else []) + merged
    return ("\n".join(lines) + "\n").encode("utf-8")




def _read_bytes_with_retry(
    path: Path, retry_delays: tuple[float, ...], sleep, logger: logging.Logger
) -> bytes | None:
    """Read a file, retrying transient Windows sharing violations.

    Returns ``None`` if the file disappeared.  Raises :class:`_LockedError` if it
    is still held open after every retry, which means "skip and try next tick".
    """
    last: OSError | None = None
    for delay in (0.0, *retry_delays):
        if delay:
            sleep(delay)
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None
        except PermissionError as exc:  # WinError 32: in use by another handle
            last = exc
        except OSError as exc:
            logger.error("archive: cannot read %s: %s", path.name, exc)
            return None
    raise _LockedError(str(last))


def _remove_with_retry(
    path: Path, retry_delays: tuple[float, ...], sleep, logger: logging.Logger
) -> bool:
    """Delete a file, tolerating transient locks. ``True`` means it is gone."""
    last: OSError | None = None
    for delay in (0.0, *retry_delays):
        if delay:
            sleep(delay)
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            # Another process already removed it; that is success, not failure.
            return True
        except PermissionError as exc:
            last = exc
        except OSError as exc:
            logger.error("archive: cannot remove %s: %s", path.name, exc)
            return False
    logger.warning(
        "archive: %s still locked after %d attempts (%s); will retry next tick",
        path.name,
        len(retry_delays) + 1,
        last,
    )
    return False


def _fsync_dir(path: Path) -> None:
    """Best-effort directory fsync. Unsupported on Windows, hence best-effort."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _clean_orphan_temp_files(data_dir: Path, logger: logging.Logger) -> None:
    """Remove temp files left behind by a crash during an earlier zip."""
    cutoff = time.time() - _TEMP_MAX_AGE_SECONDS
    for entry in data_dir.iterdir():
        if not entry.is_file() or not entry.name.startswith("."):
            continue
        if not entry.name.endswith(".tmp"):
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
                logger.debug("archive: removed orphaned temp file %s", entry.name)
        except OSError:
            pass
