"""File-based session locking for single browser instance.

Uses a PID-based lockfile to prevent multiple browser instances
from conflicting on CDP port and Xvfb display.
Stale locks (dead PIDs) are automatically cleaned up.
"""

import logging
import os

logger = logging.getLogger(__name__)

DEFAULT_LOCK_PATH = os.path.join(
    os.environ.get("TMPDIR", os.path.expanduser("~")),
    ".tbp_browser.lock",
)


class SessionLock:
    """Ensure only one browser instance runs at a time."""

    def __init__(self, lock_path=DEFAULT_LOCK_PATH):
        self.lock_path = lock_path
        self._acquired = False

    def _is_pid_alive(self, pid):
        """Check if a process with given PID is still running."""
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True  # Process exists but owned by another user
        except (OSError, ProcessLookupError):
            return False

    def acquire(self):
        """Acquire the lock. Raises RuntimeError if already locked."""
        if os.path.exists(self.lock_path):
            try:
                with open(self.lock_path) as f:
                    old_pid = int(f.read().strip())
                if self._is_pid_alive(old_pid):
                    raise RuntimeError(
                        f"Another browser session is running "
                        f"(PID {old_pid}). Wait for it to finish "
                        f"or remove {self.lock_path}"
                    )
                else:
                    logger.info(
                        "Removing stale lock (PID %d dead)", old_pid
                    )
                    os.remove(self.lock_path)
            except (ValueError, FileNotFoundError):
                try:
                    os.remove(self.lock_path)
                except FileNotFoundError:
                    pass

        try:
            fd = os.open(
                self.lock_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            raise RuntimeError(
                "Another browser session acquired the lock concurrently."
            )
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
        self._acquired = True

    def release(self):
        """Release the lock."""
        if self._acquired:
            try:
                os.remove(self.lock_path)
            except FileNotFoundError:
                pass
            self._acquired = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
