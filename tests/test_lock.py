"""The run lock: one run at a time, however a run ends, however long it is suspended, and whatever happens to the file.

The lock is a kernel file lock, so these tests use real processes where the point is what the operating system does."""
from __future__ import annotations

import errno
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from connecto import lock as lock_module
from connecto.lock import LockHeld, LockUnavailable, RunLock

ROOT = Path(__file__).resolve().parent.parent

HOLDER = """
import sys
sys.path.insert(0, sys.argv[1])
from connecto.lock import RunLock
RunLock(sys.argv[2]).acquire()
print("held", flush=True)
sys.stdin.read()                      # until the parent kills this process
"""


@pytest.fixture()
def path(tmp_path):
    return tmp_path / "run.lock"


def start_holder(path):
    proc = subprocess.Popen([sys.executable, "-c", HOLDER, str(ROOT), str(path)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "held"
    return proc


# ----------------------------------------------------------------------------- exclusive
def test_a_second_run_is_refused_while_the_first_holds_the_lock(path):
    with RunLock(path) as first:
        assert first.is_held() and RunLock(path).is_held()
        with pytest.raises(LockHeld) as caught:
            RunLock(path).acquire()
        assert f"pid {os.getpid()}" in str(caught.value)             # the message says who is running
        assert RunLock(path).holder().startswith(f"pid {os.getpid()} since ")
    assert not RunLock(path).is_held()


def test_the_lock_can_be_taken_again_after_it_was_released(path):
    for _ in range(3):
        with RunLock(path):
            with pytest.raises(LockHeld):
                RunLock(path).acquire()


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="counts open files through /proc")
def test_being_refused_and_asking_do_not_leak_open_files(path):
    def open_files():
        return len(os.listdir("/proc/self/fd"))

    baseline = open_files()
    with RunLock(path):
        for _ in range(25):
            with pytest.raises(LockHeld):
                RunLock(path).acquire()
            RunLock(path).is_held()
            RunLock(path).holder()
        assert open_files() == baseline + 1             # only the file the lock itself holds open
    for _ in range(25):
        RunLock(path).acquire().release()
    assert open_files() == baseline


def test_one_object_cannot_take_the_same_lock_twice(path):
    with RunLock(path) as held:
        with pytest.raises(RuntimeError):
            held.acquire()


def test_when_many_try_at_once_exactly_one_gets_the_lock(path):
    path.parent.mkdir(exist_ok=True)
    winners, losers, gate = [], [], threading.Barrier(8)

    def try_it():
        lock = RunLock(path)
        gate.wait()
        try:
            lock.acquire()
        except LockHeld:
            losers.append(lock)
        else:
            winners.append(lock)
            time.sleep(0.6)               # longer than the others keep trying
            lock.release()

    threads = [threading.Thread(target=try_it) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(winners) == 1 and len(losers) == 7


def test_a_status_display_asking_does_not_make_a_run_that_starts_at_that_moment_fail(path):
    path.write_text("")
    stop, errors = threading.Event(), []

    def keep_asking():
        while not stop.is_set():
            RunLock(path).is_held()

    asker = threading.Thread(target=keep_asking)
    asker.start()
    try:
        for _ in range(60):
            try:
                with RunLock(path):
                    pass
            except LockHeld as exc:       # pragma: no cover - the failure this test is about
                errors.append(exc)
    finally:
        stop.set()
        asker.join(5)
    assert errors == []


# ----------------------------------------------------------------------------- how a lock ends
def test_the_operating_system_releases_the_lock_when_the_holder_is_killed(path):
    holder = start_holder(path)
    try:
        assert RunLock(path).is_held()
        with pytest.raises(LockHeld):
            RunLock(path).acquire()
        holder.kill()                      # no clean shutdown: no release(), no atexit, nothing
        holder.wait(10)
        started = time.monotonic()
        with RunLock(path):                # nothing to wait for and nothing to guess about
            assert time.monotonic() - started < 2
    finally:
        holder.kill()
        holder.wait(10)
    assert not RunLock(path).is_held()


def test_a_lock_that_is_held_is_never_taken_over_however_old_its_file_looks(path):
    """The old scheme took a lock over when its file had not been touched for two minutes - which is what a laptop that was
    asleep looks like. A held lock is held."""
    with RunLock(path):
        long_ago = time.time() - 86_400
        os.utime(path, (long_ago, long_ago))
        with pytest.raises(LockHeld):
            RunLock(path).acquire()
        assert RunLock(path).is_held()


@pytest.mark.skipif(os.name == "nt", reason="permission bits")
def test_the_lock_file_is_private_to_the_user(path):
    with RunLock(path):
        assert os.stat(path).st_mode & 0o777 == 0o600


def test_a_file_that_nobody_holds_is_taken_at_once_even_if_it_is_new_and_names_a_pid(path):
    path.write_text("pid 424242 since just now\ntoken abc\n")
    started = time.monotonic()
    with RunLock(path) as lock:
        assert time.monotonic() - started < 1
        assert not lock.lost
        assert f"pid {os.getpid()}" in RunLock(path).holder()


def test_release_leaves_the_file_but_not_the_name_of_a_holder(path):
    lock = RunLock(path).acquire()
    assert "token" in path.read_text()
    lock.release()
    assert path.exists() and path.read_text() == "" and RunLock(path).holder() == "" and not RunLock(path).is_held()
    lock.release()                         # twice is fine


def test_asking_who_holds_a_lock_that_does_not_exist_creates_nothing(path):
    assert RunLock(path).holder() == "" and not RunLock(path).is_held() and not path.exists()
    assert not RunLock(path.parent / "no" / "such" / "folder" / "run.lock").is_held()


# ----------------------------------------------------------------------------- somebody tidies the folder
def test_a_lock_whose_file_is_deleted_is_lost_and_says_so_straight_away(path):
    with RunLock(path) as lock:
        assert not lock.lost
        path.unlink()
        assert lock.lost
        assert lock.lost                   # and it stays lost


def test_a_lock_that_was_seen_to_be_lost_stays_lost_even_if_the_file_comes_back(path):
    """The run has been told to stop; it must not be told otherwise because somebody put the file back."""
    with RunLock(path) as lock:
        moved = path.parent / "moved"
        os.rename(path, moved)
        assert lock.lost
        os.rename(moved, path)                          # the very same file, as it was
        assert lock.lost


def test_a_lock_whose_file_is_replaced_is_lost(path):
    with RunLock(path) as lock:
        other = path.parent / "other"
        other.write_text("pid 1 since then\n")
        os.replace(other, path)
        assert lock.lost


def test_a_lock_whose_file_is_replaced_by_an_exact_copy_is_lost_too(path):
    """Same text, same token - but a different file, which is not the one that is locked."""
    with RunLock(path) as lock:
        copy = path.parent / "copy"
        copy.write_text(path.read_text())
        os.replace(copy, path)
        assert lock.lost
        assert not RunLock(path).is_held()              # and nobody holds what is at the path now


def test_a_lock_whose_file_is_overwritten_is_lost(path):
    with RunLock(path) as lock:
        path.write_text("pid 1 since then\ntoken not-ours\n")
        assert lock.lost


def test_a_lock_that_was_never_taken_is_not_lost(path):
    assert RunLock(path).lost is False


def test_a_run_that_began_after_the_file_was_deleted_cannot_be_overtaken_by_the_old_one_releasing(path):
    """The first run's release() must not delete or unlock the second run's lock: only what it holds is touched."""
    first = RunLock(path).acquire()
    path.unlink()                                       # somebody tidies the folder while the first run is going on
    second = RunLock(path).acquire()                    # a different file now: nothing stops this one
    assert first.lost and not second.lost
    first.release()
    assert path.exists() and second.is_held() and not second.lost
    assert "token" in path.read_text()
    with pytest.raises(LockHeld):
        RunLock(path).acquire()
    second.release()


def test_a_file_that_vanishes_between_the_open_and_the_lock_is_opened_again(path, monkeypatch):
    real, calls = lock_module._try_lock, []

    def vanishing(fd):
        calls.append(fd)
        if len(calls) == 1:
            path.unlink()                               # gone after we opened it, before we locked it
        return real(fd)

    monkeypatch.setattr(lock_module, "_try_lock", vanishing)
    with RunLock(path) as lock:
        assert len(calls) == 2 and path.exists() and not lock.lost


# ----------------------------------------------------------------------------- where it cannot work
def test_a_file_system_without_file_locks_is_reported_and_leaks_nothing(path, monkeypatch):
    opened = []
    real_open, real_close = os.open, os.close
    monkeypatch.setattr(os, "open", lambda *a, **k: opened.append(real_open(*a, **k)) or opened[-1])
    monkeypatch.setattr(os, "close", lambda fd: (opened.remove(fd) if fd in opened else None, real_close(fd))[1])

    def refuse(fd, how):
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(lock_module.fcntl, "flock", refuse)
    with pytest.raises(LockUnavailable) as caught:
        RunLock(path).acquire()
    assert "locks" in str(caught.value) and "CONNECTO_HOME" in str(caught.value)
    assert opened == []                                 # the file it opened was closed again
    assert RunLock(path).is_held() is False             # and a status display just says nothing is held


def test_a_place_that_cannot_hold_a_lock_file_is_reported(tmp_path):
    with pytest.raises(LockUnavailable):
        RunLock(tmp_path).acquire()                     # a folder, not a file
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(LockUnavailable):
        RunLock(blocker / "inside" / "run.lock").acquire()


# ----------------------------------------------------------------------------- Windows
class FakeMsvcrt:
    """Stands in for the Windows C runtime's locking(): one exclusive byte range per file, refused with EACCES while held."""
    LK_NBLCK, LK_UNLCK = 2, 0
    held: dict = {}
    calls: list = []

    @classmethod
    def locking(cls, fd, mode, nbytes):
        key, position = os.fstat(fd).st_ino, os.lseek(fd, 0, os.SEEK_CUR)
        cls.calls.append((mode, position, nbytes))
        if mode == cls.LK_NBLCK:
            if key in cls.held and cls.held[key] != fd:
                raise OSError(errno.EACCES, "Permission denied")
            cls.held[key] = fd
        else:
            cls.held.pop(key, None)


def test_the_windows_branch_locks_one_byte_far_from_the_text_and_unlocks_it(path, monkeypatch):
    FakeMsvcrt.held, FakeMsvcrt.calls = {}, []
    monkeypatch.setattr(lock_module, "fcntl", None)
    monkeypatch.setattr(lock_module, "msvcrt", FakeMsvcrt)
    with RunLock(path) as lock:
        with pytest.raises(LockHeld):
            RunLock(path).acquire()
        assert RunLock(path).is_held() and not lock.lost
        assert "token" in path.read_text()                     # the text is at the start; the locked byte is not
    assert not RunLock(path).is_held()
    assert FakeMsvcrt.calls and all(position >= 1 << 20 and n == 1 for _, position, n in FakeMsvcrt.calls)      # far beyond the text
    assert any(mode == FakeMsvcrt.LK_UNLCK for mode, _, _ in FakeMsvcrt.calls)
