"""Single-instance lock over the data directory.

Two writers appending to the same CSV could interleave, and two sweeps racing
over the same rollover would be worse.  The lock is scoped to the data
directory rather than the config file: two configs pointing at one ``data_dir``
are exactly the dangerous case, while two configs with different data
directories are legitimately independent.

The lock is a kernel byte-range lock on ``data/.lock`` -- ``msvcrt.locking`` on
Windows, ``fcntl.flock`` on POSIX.  Both are owned by the open file descriptor,
so the kernel releases them when the process dies, whether by crash, by
``kill -9`` or by Task Scheduler's execution-time limit.  That property is why
this is preferred over an O_EXCL lockfile with PID liveness checks: there is no
staleness heuristic to get wrong, and no PID-reuse bug.

The lock file itself is never deleted.  Removing it would let a third process
create a fresh file while a second still holds a handle to the old one, which
reopens the very race the lock exists to close.  A permanent zero-byte
``data/.lock`` is the correct end state.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

if os.name == "nt":  # pragma: no cover - platform branch
    import msvcrt
else:  # pragma: no cover - platform branch
    import fcntl

logger = logging.getLogger("scraper.locking")

#: How often to re-try a contended lock.
_POLL_INTERVAL_SECONDS = 0.25


class LockNotAcquired(Exception):
    """The instance lock is held by another process."""


class InstanceLock:
    """A context manager owning the data directory's lock."""

    def __init__(
        self,
        data_dir: Path,
        *,
        wait_seconds: float = 10.0,
        sleep=time.sleep,
        logger: logging.Logger = logger,
    ) -> None:
        self.path = Path(data_dir) / ".lock"
        self._wait_seconds = wait_seconds
        self._sleep = sleep
        self._logger = logger
        self._fd: int | None = None

    @staticmethod
    def _lock(fd: int) -> None:
        if os.name == "nt":
            # The locked region may extend past EOF, so a zero-byte file is fine.
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(fd: int) -> None:
        if os.name == "nt":
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)

    def acquire(self) -> bool:
        """Try to take the lock, waiting briefly. ``True`` if we own it now."""
        if self._fd is not None:
            return True

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        deadline = time.monotonic() + self._wait_seconds

        while True:
            try:
                self._lock(fd)
            except OSError as exc:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    os.close(fd)
                    self._logger.info("another instance holds %s (%s)", self.path.name, exc)
                    return False
                self._sleep(min(_POLL_INTERVAL_SECONDS, remaining))
                continue
            self._fd = fd
            return True

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            self._unlock(fd)
        except OSError:
            pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def __enter__(self) -> "InstanceLock":
        if not self.acquire():
            raise LockNotAcquired(str(self.path))
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()


class NullLock:
    """A lock that always succeeds, for ``--no-lock`` and for tests."""

    def acquire(self) -> bool:
        return True

    def release(self) -> None:
        return None

    def __enter__(self) -> "NullLock":
        return self

    def __exit__(self, *exc_info) -> None:
        return None
