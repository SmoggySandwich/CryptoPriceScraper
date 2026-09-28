"""UTC clock, trading-day labels and wall-clock grid alignment.

Everything here is timezone-aware UTC.  Local time is never consulted: the
dataset's day boundary is UTC midnight by design, and UTC has no daylight
saving, so the boundary never shifts under us.

The rule that keeps this honest is that a *single* instant drives both the
day label in a filename and the timestamp written into that file.  Callers
take one ``utcnow()`` per tick and thread it through, rather than each layer
calling the clock for itself.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from pathlib import Path

#: Repository root: the directory containing ``main.py``.
#:
#: Config paths resolve against this, never against the process CWD.  Both
#: Task Scheduler and cron start the process somewhere else entirely (on
#: Windows the CWD is ``C:\\Windows\\System32``), so a CWD-relative path would
#: work in a terminal and fail silently once scheduled.
BASE_DIR = Path(__file__).resolve().parent.parent

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: ``now.strftime`` format, kept in one place so filenames and row timestamps
#: cannot drift apart.
_DAY_FORMAT = "%Y-%m-%d"


def utcnow() -> datetime:
    """Return the current instant as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def iso_z(dt: datetime) -> str:
    """Format an instant as ISO 8601 UTC with second precision.

    >>> iso_z(datetime(2026, 9, 27, 0, 5, 3, tzinfo=UTC))
    '2026-09-27T00:05:03Z'
    """
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def day_label(dt: datetime) -> str:
    """Return the trading-day label (``2026-09-27``) that an instant falls in."""
    return dt.astimezone(UTC).strftime(_DAY_FORMAT)


def parse_day_label(label: str) -> date | None:
    """Parse a day label, returning ``None`` if it is not a valid calendar date.

    The regex check matters as much as the parse: ``date.fromisoformat`` accepts
    a handful of shapes we do not want to treat as our own filenames.
    """
    if not _DAY_RE.match(label):
        return None
    try:
        return date.fromisoformat(label)
    except ValueError:
        return None


def csv_path(data_dir: Path, day: str, symbol: str) -> Path:
    """Path of the live CSV for one coin on one trading day."""
    return Path(data_dir) / f"{day}_{symbol}.csv"


def zip_path(data_dir: Path, day: str, symbol: str) -> Path:
    """Path of the archive that replaces a day's CSV once the day is over."""
    return Path(data_dir) / f"{day}_{symbol}.csv.zip"


def next_grid_slot(now: datetime, period_seconds: int) -> datetime:
    """Return the next wall-clock grid instant strictly after ``now``.

    With a 300 second period this yields 00:00, 00:05, 00:10 and so on, and the
    result is *always* in the future because it is recomputed from the clock
    rather than accumulated from the previous tick.

    That property is what makes a missed window harmless.  After a nine hour
    suspend, ``previous_tick + period`` would ask for ~108 immediate polls to
    "catch up"; this returns exactly one future slot instead, so a catch-up
    burst is structurally impossible.
    """
    if period_seconds <= 0:
        raise ValueError("period_seconds must be positive")
    epoch = now.timestamp()
    return datetime.fromtimestamp(
        (int(epoch // period_seconds) + 1) * period_seconds, UTC
    )


def seconds_until(target: datetime, now: datetime) -> float:
    """Seconds from ``now`` to ``target``, clamped at zero."""
    return max(0.0, (target - now).total_seconds())
