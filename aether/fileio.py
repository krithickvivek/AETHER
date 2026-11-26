"""Small, standard-library-only helpers for safe local file persistence.

Used by the run history (aether_ui/history.py) and by RunStore (aether/context.py).

  write_text_atomic  Write a file so that readers see either the old content or the new content, never a mix:
                     a temp file with a UNIQUE name in the same folder, flushed and fsynced, then os.replace().
                     A brief PermissionError on the replace (Windows, when another process has the file open
                     for a moment) is retried; any failure removes the temp file and leaves the old file alone.
  read_text_retry    Read a file, retrying a brief PermissionError.
  exclusive_lock     A cross-process, cross-thread mutual-exclusion lock on a sidecar lock file. It uses an OS
                     byte-range lock (msvcrt on Windows, fcntl elsewhere), which the operating system releases
                     when the holder dies, so a crash can never leave a stale lock behind.

Nothing here knows about history or runs. The constants are module-level so tests can shorten them.
"""

import contextlib
import errno
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")

READ_ATTEMPTS = 3        # tries for a read that hits a brief PermissionError
REPLACE_ATTEMPTS = 20    # tries for os.replace (about one second in total)
RETRY_DELAY = 0.05       # seconds between those tries
LOCK_TIMEOUT = 15.0      # seconds to wait for a lock before giving up
LOCK_POLL = 0.02         # seconds between lock attempts

_BUSY = {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, errno.EDEADLK}  # "someone else holds the lock"


class LockTimeout(OSError):
    """The lock could not be taken in time. Nothing was changed."""


def retry_on_permission_error(action: Callable[[], T], attempts: int, delay: float) -> T:
    for attempt in range(attempts):
        try:
            return action()
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)
    raise AssertionError("unreachable")


def read_text_retry(path: Path) -> str:
    """Read a UTF-8 text file. Raises FileNotFoundError, UnicodeDecodeError or another OSError as usual."""
    return retry_on_permission_error(lambda: Path(path).read_text(encoding="utf-8"), READ_ATTEMPTS, RETRY_DELAY)


def write_text_atomic(path: Path, text: str) -> None:
    """Replace `path` with `text` in one step. The folder must exist. Raises OSError on failure, and in that case
    the previous file (if any) is untouched and no temp file is left behind."""
    path = Path(path)
    temp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        retry_on_permission_error(lambda: os.replace(temp, path), REPLACE_ATTEMPTS, RETRY_DELAY)
    except BaseException:  # including KeyboardInterrupt: never leave a half-written temp file
        with contextlib.suppress(OSError):
            temp.unlink()
        raise


# ------------------------------------------------------------------ the lock

if sys.platform == "win32":
    import msvcrt

    def _try_lock(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _thread_lock(key: str) -> threading.Lock:
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.Lock())


@contextlib.contextmanager
def exclusive_lock(lock_path: Path, timeout: float | None = None):
    """Hold an exclusive lock on `lock_path` (a sidecar file, created if needed) for the `with` block.

    Threads of this process queue on a thread lock; other processes are kept out by the OS lock. Raises LockTimeout
    if the lock is not free within `timeout` seconds (default LOCK_TIMEOUT), or another OSError if the lock file
    cannot be created or opened. The folder must exist."""
    lock_path = Path(lock_path)
    deadline = time.monotonic() + (LOCK_TIMEOUT if timeout is None else timeout)
    thread_lock = _thread_lock(os.path.normcase(os.path.abspath(lock_path)))
    if not thread_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise LockTimeout("Another operation is still using this file.")
    try:
        handle = open(lock_path, "a+b")
        try:
            while True:
                try:
                    _try_lock(handle)
                    break
                except OSError as error:
                    if error.errno not in _BUSY:
                        raise
                    if time.monotonic() >= deadline:
                        raise LockTimeout("Another program is still using this file.") from error
                    time.sleep(LOCK_POLL)
            try:
                yield
            finally:
                with contextlib.suppress(OSError):
                    _unlock(handle)
        finally:
            handle.close()  # also releases the OS lock
    finally:
        thread_lock.release()
