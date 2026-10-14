"""Single-instance run lock.

Two Chrome instances on one profile corrupt it, and two senders would double the pace. The lock is a *kernel* file lock
(``flock`` on macOS and Linux, ``msvcrt.locking`` on Windows) held on an open file for as long as the run lasts:

* the operating system drops it the moment the process ends - however it ends (crash, kill, power cut) - so there is no
  "stale lock" to guess about, and a run that is merely slow or suspended (a laptop asleep for an hour) keeps it and
  cannot be overtaken by a second one;
* a second run, in this process or another, is refused while it is held;
* the file also says who holds it (pid, start time) for the message "another run is in progress", and carries a random
  token. :attr:`RunLock.lost` is true as soon as the file at the path is no longer the one that was locked (somebody
  deleted or replaced it while the run was going on - after which a second run could start); the run then stops.

The file is never deleted. Deleting a lock file that another process may be about to open is how two processes end up
holding "the" lock on two different files.

We never probe a pid with ``os.kill(pid, 0)``: on Windows that call *terminates* the process.
"""
from __future__ import annotations

import errno
import os
import secrets
import time
from pathlib import Path

try:                       # macOS, Linux
    import fcntl
except ImportError:        # pragma: no cover - Windows
    fcntl = None           # type: ignore[assignment]
try:                       # Windows
    import msvcrt
except ImportError:
    msvcrt = None          # type: ignore[assignment]

# Windows locks a byte range, and the range cannot be read by anybody else: lock one byte far beyond the text.
WINDOWS_LOCK_OFFSET = 1 << 30
ATTEMPTS = 4               # a status display probes the lock for an instant; a real run tries again rather than give up
ATTEMPT_PAUSE = 0.05


class LockHeld(Exception):
    def __init__(self, path: Path, info: str = ""):
        super().__init__(f"another Connecto run is in progress ({info or path})")
        self.path = path
        self.info = info


class LockUnavailable(Exception):
    """The lock cannot be taken at all here (a file system without file locks, a folder that cannot be written)."""

    def __init__(self, path: Path, reason: str):
        super().__init__(f"Connecto cannot lock {path} ({reason}). Its data folder must be on a disk of this computer that "
                         "supports file locks; set CONNECTO_HOME to such a folder.")
        self.path = path
        self.reason = reason


# ----------------------------------------------------------------------------- the operating system's side
def _try_lock(fd: int) -> bool:
    """Take the kernel lock without waiting. False when somebody else holds it. Anything else that goes wrong (a file system
    that cannot lock) is raised as OSError, for the caller to turn into LockUnavailable."""
    if fcntl is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                return False
            raise
        return True
    if msvcrt is not None:  # pragma: no cover - exercised through a stand-in in the tests
        os.lseek(fd, WINDOWS_LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EDEADLK, errno.EAGAIN):
                return False
            raise
        finally:
            os.lseek(fd, 0, os.SEEK_SET)
        return True
    raise OSError(errno.ENOSYS, "this platform has no file locks")  # pragma: no cover


def _unlock(fd: int) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        elif msvcrt is not None:  # pragma: no cover
            os.lseek(fd, WINDOWS_LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass            # closing the file releases it anyway


class RunLock:
    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self._fd: int | None = None
        self._token = ""
        self._lost = False

    # ------------------------------------------------------------------ state
    def holder(self) -> str:
        """Who holds the lock, as written in the file ("pid 123 since ..."); "" when nobody does or it is not known."""
        try:
            with open(self.path, "rb") as fh:
                first = fh.read(300).decode("utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        return first[0].strip() if first and first[0].startswith("pid ") else ""

    def is_held(self) -> bool:
        """Does a run hold this lock right now? For status displays: the lock is taken for an instant to find out, so the
        answer is no reservation - and nothing is created when there is no file."""
        if self._fd is not None:
            return True
        try:
            fd = os.open(self.path, os.O_RDWR)
        except OSError:
            try:
                fd = os.open(self.path, os.O_RDONLY)
            except OSError:
                return False
        try:
            try:
                if _try_lock(fd):
                    _unlock(fd)
                    return False
            except OSError:
                return False                    # a file system without locks: nothing can be said, nothing is held by us
            return True
        finally:
            os.close(fd)

    @property
    def lost(self) -> bool:
        """True once the file at the path is no longer the file this lock was taken on (or no longer carries our token):
        the lock is gone for every practical purpose, a second run may be starting, and this one must stop."""
        if self._fd is None:
            return False
        if self._lost:
            return True
        try:
            here, mine = os.stat(self.path), os.fstat(self._fd)
            same_file = (here.st_ino, here.st_dev) == (mine.st_ino, mine.st_dev)
            with open(self.path, "rb") as fh:
                token_ok = ("token " + self._token) in fh.read(300).decode("utf-8", errors="replace")
        except OSError:
            same_file = token_ok = False
        self._lost = not (same_file and token_ok)
        return self._lost

    # ------------------------------------------------------------------ acquire / release
    def acquire(self) -> "RunLock":
        if self._fd is not None:
            raise RuntimeError("this lock is already held by this object")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise LockUnavailable(self.path, exc.strerror or type(exc).__name__) from exc
        for _ in range(3):                                   # (a file deleted between our open and our lock: open it again)
            try:
                fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            except OSError as exc:
                raise LockUnavailable(self.path, exc.strerror or type(exc).__name__) from exc
            try:
                taken = False
                for attempt in range(ATTEMPTS):
                    if _try_lock(fd):
                        taken = True
                        break
                    if attempt + 1 < ATTEMPTS:
                        time.sleep(ATTEMPT_PAUSE)
                if not taken:
                    raise LockHeld(self.path, self.holder())
                if self._is_the_file_at_the_path(fd):
                    self._write_owner(fd)
                    self._fd, self._lost = fd, False
                    return self
            except LockHeld:
                os.close(fd)
                raise
            except OSError as exc:
                os.close(fd)
                raise LockUnavailable(self.path, exc.strerror or type(exc).__name__) from exc
            _unlock(fd)
            os.close(fd)
        raise LockHeld(self.path, self.holder())             # pragma: no cover - the file kept changing under us

    def _is_the_file_at_the_path(self, fd: int) -> bool:
        try:
            here, mine = os.stat(self.path), os.fstat(fd)
        except OSError:
            return False
        return (here.st_ino, here.st_dev) == (mine.st_ino, mine.st_dev)

    def _write_owner(self, fd: int) -> None:
        self._token = secrets.token_hex(8)
        text = f"pid {os.getpid()} since {time.strftime('%Y-%m-%d %H:%M:%S')}\ntoken {self._token}\n"
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, text.encode("utf-8"))

    def release(self) -> None:
        """Give the lock up. Only the lock this object holds is touched: if the file was replaced meanwhile, the file that
        is there now belongs to somebody else and is left alone."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            try:
                os.ftruncate(fd, 0)                          # nobody's name stays on a lock nobody holds
            except OSError:
                pass
            _unlock(fd)
        finally:
            os.close(fd)

    def __enter__(self) -> "RunLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()
