"""Files that people point Connecto at - a spreadsheet, a list, a settings file - are read only if they are ordinary files,
and never further than their limit: a pipe nobody writes to would wait forever, ``/dev/zero`` never ends."""
from __future__ import annotations

import os
import signal
import stat
import types
from contextlib import contextmanager

import pytest

from connecto.util import MAX_YAML_BYTES, read_regular_file, read_yaml_file

pytestmark = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")


@contextmanager
def no_longer_than(seconds: float = 3):
    """A pipe that is opened the wrong way blocks for ever: stop the test instead of the suite."""
    def give_up(*_):
        raise AssertionError(f"still waiting after {seconds:g} seconds (a pipe or device was opened and read)")

    previous = signal.signal(signal.SIGALRM, give_up)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def test_an_ordinary_file_is_read_as_it_is(tmp_path):
    path = tmp_path / "a.csv"
    path.write_bytes(b"\xef\xbb\xbfname,url\r\nJane,x\n")
    assert read_regular_file(path, 1000) == b"\xef\xbb\xbfname,url\r\nJane,x\n"
    assert read_regular_file(path, len(path.read_bytes())) == path.read_bytes()          # exactly as large as allowed


def test_a_file_that_is_too_large_is_refused_with_its_size_limit_in_the_message(tmp_path):
    path = tmp_path / "big.csv"
    path.write_bytes(b"x" * 2049)
    with pytest.raises(ValueError, match=r"big.csv is larger than 2 KB - split it first"):
        read_regular_file(path, 2048, hint=" - split it first")
    path.write_bytes(b"x" * (3 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="larger than 3 MB"):
        read_regular_file(path, 3 * 1024 * 1024)


def test_a_file_that_grows_after_it_was_measured_is_still_read_only_up_to_the_limit(tmp_path, monkeypatch):
    path = tmp_path / "grows.csv"
    path.write_bytes(b"x" * 5000)
    real = os.fstat

    def small(fd):
        info = real(fd)
        return types.SimpleNamespace(st_mode=info.st_mode, st_size=10)                   # "10 bytes" - then 5000 arrive

    monkeypatch.setattr(os, "fstat", small)
    with pytest.raises(ValueError, match="larger than"):
        read_regular_file(path, 1024)


def test_a_folder_is_refused(tmp_path):
    with pytest.raises(ValueError, match="is a folder"):
        read_regular_file(tmp_path, 1000, what="CSV file")


def test_a_folder_is_called_a_folder_where_it_cannot_be_opened_at_all(tmp_path, monkeypatch):
    real_open = os.open

    def like_windows(path, flags, *rest):              # Windows answers "permission denied" for a folder
        if os.path.isdir(path):
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, flags, *rest)

    monkeypatch.setattr(os, "open", like_windows)
    with pytest.raises(ValueError, match="is a folder, not a CSV file"):
        read_regular_file(tmp_path, 1000, what="CSV file")
    with pytest.raises(FileNotFoundError):              # anything else that cannot be opened is still that error
        read_regular_file(tmp_path / "missing.csv", 1000)


def test_a_named_pipe_is_refused_not_waited_for(tmp_path):
    fifo = tmp_path / "people.csv"
    os.mkfifo(fifo)
    with no_longer_than(), pytest.raises(ValueError, match="not an ordinary file"):
        read_regular_file(fifo, 1000)


@pytest.mark.skipif(not os.path.exists("/dev/null"), reason="needs /dev/null")
def test_a_device_reached_through_a_symlink_is_refused(tmp_path):
    link = tmp_path / "people.csv"
    link.symlink_to("/dev/null")
    with no_longer_than(), pytest.raises(ValueError, match="not an ordinary file"):
        read_regular_file(link, 1000)


def test_a_missing_file_is_an_os_error(tmp_path):
    with pytest.raises(OSError):
        read_regular_file(tmp_path / "nope.csv", 1000)


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc")
def test_no_file_is_left_open_whatever_happened(tmp_path):
    big = tmp_path / "big.csv"
    big.write_bytes(b"x" * 100)
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    before = len(os.listdir("/proc/self/fd"))
    for _ in range(30):
        for path, limit in ((big, 10), (big, 1000), (tmp_path, 10), (fifo, 10), (tmp_path / "missing", 10)):
            try:
                read_regular_file(path, limit)
            except (OSError, ValueError):
                pass
    assert len(os.listdir("/proc/self/fd")) == before


def test_a_settings_file_that_is_a_pipe_or_too_large_is_refused_too(tmp_path):
    fifo = tmp_path / "config.yaml"
    os.mkfifo(fifo)
    with no_longer_than(), pytest.raises(ValueError, match="not an ordinary file"):
        read_yaml_file(fifo)
    big = tmp_path / "big.yaml"
    big.write_bytes(b"# " + b"x" * MAX_YAML_BYTES)
    with pytest.raises(ValueError, match="not a settings file"):
        read_yaml_file(big)
    ok = tmp_path / "ok.yaml"
    ok.write_bytes(b"\xef\xbb\xbfa: 1\n")
    assert read_yaml_file(ok) == {"a": 1}


def test_the_add_command_will_not_read_a_pipe_as_a_list_of_people(tmp_path, monkeypatch, capsys):
    from connecto import cli
    fifo = tmp_path / "list.txt"
    os.mkfifo(fifo)
    monkeypatch.setenv("CONNECTO_HOME", str(tmp_path / "home"))
    with no_longer_than():
        code = cli.main(["add", "--file", str(fifo)])
    assert code != 0 and "not an ordinary file" in capsys.readouterr().err
    assert stat.S_ISFIFO(os.stat(fifo).st_mode)


def test_a_file_that_is_too_large_is_refused_without_reading_any_of_it(tmp_path, monkeypatch):
    path = tmp_path / "big.csv"
    path.write_bytes(b"x" * 5000)

    def never(*_a, **_k):
        raise AssertionError("the file was opened for reading although its size is over the limit")

    monkeypatch.setattr(os, "fdopen", never)
    with pytest.raises(ValueError, match="larger than"):
        read_regular_file(path, 1024)


def test_never_more_than_one_byte_over_the_limit_is_read(tmp_path, monkeypatch):
    path = tmp_path / "grows.csv"
    path.write_bytes(b"x" * 5000)
    real_fdopen, real_fstat, reads = os.fdopen, os.fstat, []

    class Spy:
        def __init__(self, fh):
            self.fh = fh

        def read(self, n=-1):
            reads.append(n)
            return self.fh.read(n)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self.fh.__exit__(*exc)

    def small(fd):
        info = real_fstat(fd)
        return types.SimpleNamespace(st_mode=info.st_mode, st_size=10)

    monkeypatch.setattr(os, "fdopen", lambda *a, **k: Spy(real_fdopen(*a, **k)))
    monkeypatch.setattr(os, "fstat", small)
    with pytest.raises(ValueError):
        read_regular_file(path, 1024)
    assert reads == [1025]


def test_a_csv_that_is_a_pipe_or_a_device_is_refused_not_waited_for(tmp_path):
    from connecto.sources import csv_file
    fifo = tmp_path / "leads.csv"
    os.mkfifo(fifo)
    with no_longer_than(), pytest.raises(ValueError, match="not an ordinary file"):
        csv_file.read_csv_rows(fifo)
    if os.path.exists("/dev/null"):
        link = tmp_path / "nothing.csv"
        link.symlink_to("/dev/null")
        with no_longer_than(), pytest.raises(ValueError, match="not an ordinary file"):
            csv_file.read_csv_rows(link)
