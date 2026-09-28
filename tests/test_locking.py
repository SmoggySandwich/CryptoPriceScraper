"""Single-instance lock behaviour.

Note for anyone running this on Linux for the first time: the ``fcntl.flock``
branch is exercised here, and this suite is the intended way to confirm it.
``flock`` conflicts even between two descriptors in the *same* process, because
the lock belongs to the open file description rather than to the process, which
is what makes the contention test meaningful without spawning a subprocess.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scraper.locking import InstanceLock, LockNotAcquired, NullLock


class InstanceLockTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def make(self, **kwargs) -> InstanceLock:
        kwargs.setdefault("wait_seconds", 0.0)
        kwargs.setdefault("sleep", lambda _seconds: None)
        lock = InstanceLock(self.data, **kwargs)
        # Every lock this helper hands out is released during teardown. Without
        # this the open handle makes the temp-directory cleanup fail on Windows
        # with WinError 32 -- the very behaviour the lock is built around.
        self.addCleanup(lock.release)
        return lock

    def test_acquire_creates_the_lock_file_and_succeeds(self):
        lock = self.make()
        self.assertTrue(lock.acquire())
        self.assertTrue((self.data / ".lock").exists())

    def test_second_lock_on_the_same_directory_is_refused(self):
        first, second = self.make(), self.make()
        self.assertTrue(first.acquire())
        self.assertFalse(second.acquire())

    def test_release_allows_reacquisition(self):
        first = self.make()
        self.assertTrue(first.acquire())
        first.release()
        self.assertTrue(self.make().acquire())

    def test_lock_file_is_never_deleted(self):
        # Deleting it would let a third process create a fresh file while a
        # second still holds a handle to the old one, reopening the race.
        lock = self.make()
        lock.acquire()
        lock.release()
        self.assertTrue((self.data / ".lock").exists())

    def test_acquire_is_idempotent_for_the_same_instance(self):
        lock = self.make()
        self.assertTrue(lock.acquire())
        self.assertTrue(lock.acquire())

    def test_release_without_acquire_is_harmless(self):
        self.make().release()

    def test_waiting_times_out_and_reports_failure(self):
        first = self.make()
        self.assertTrue(first.acquire())

        slept: list[float] = []
        second = InstanceLock(self.data, wait_seconds=0.5, sleep=slept.append)
        self.assertFalse(second.acquire())
        # It must have actually waited rather than failing instantly.
        self.assertTrue(slept)

    def test_context_manager_raises_when_contended(self):
        first = self.make()
        first.acquire()
        with self.assertRaises(LockNotAcquired):
            with self.make():
                self.fail("should not have entered the context")

    def test_context_manager_releases_on_exit(self):
        with self.make():
            pass
        self.assertTrue(self.make().acquire())


class NullLockTests(unittest.TestCase):
    def test_always_acquires_and_never_raises(self):
        lock = NullLock()
        self.assertTrue(lock.acquire())
        lock.release()
        with lock:
            pass


if __name__ == "__main__":
    unittest.main()
