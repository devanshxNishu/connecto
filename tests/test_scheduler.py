"""Scheduler tests: the pure builders (exact content) and the installers, driven by fake runners.

Nothing here can reach the real crontab, LaunchAgents folder or Task Scheduler: every test runs with a private HOME, a
PATH that holds nothing real, and either an injected fake runner or a fake ``crontab`` script placed on that PATH.
"""
from __future__ import annotations

import csv
import io
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import types
from pathlib import Path, PureWindowsPath

import pytest

from connecto import scheduler
from connecto.paths import Home
from connecto.scheduler import (CRON_BEGIN, CRON_END, LABEL, SchedResult, build_cron_block, build_launchd_plist,
                               build_schtasks_commands, build_schtasks_delete, command, install, launchd_plist_path,
                               merge_cron, parse_schtasks_query, parse_times, status, task_names, uninstall)

PY = "/usr/bin/python3"
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="uses /bin/sh scripts")


# ----------------------------------------------------------------------------- sandbox + fakes
@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    """A private HOME, a PATH with nothing real on it, and a fixed uid: no test can touch the machine it runs on."""
    fake_home = tmp_path / "userhome"
    fake_home.mkdir()
    bindir = tmp_path / "emptybin"
    bindir.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setattr(scheduler, "_uid", lambda: 501)
    return fake_home


@pytest.fixture
def home(tmp_path):
    return Home(tmp_path / "Alex Smith" / ".connecto")          # a folder name with a space, on purpose


class FakeRunner:
    """Records every call; subclasses answer via ``respond(argv, kwargs) -> (rc, stdout, stderr)`` or raise."""

    def __init__(self, script=None):
        self.calls: list[tuple[list[str], dict]] = []
        self.script = list(script or [])                        # [(argv prefix, (rc, out, err) | Exception)]

    def respond(self, argv, kwargs):
        for prefix, result in self.script:
            if argv[:len(prefix)] == list(prefix):
                if isinstance(result, BaseException):
                    raise result
                return result
        return (0, "", "")

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        rc, out, err = self.respond(list(argv), kwargs)
        return subprocess.CompletedProcess(argv, rc, out, err)

    @property
    def argvs(self):
        return [c[0] for c in self.calls]


class FakeCrontab(FakeRunner):
    def __init__(self, text=None, read_error=None, write_error=None):
        super().__init__()
        self.text = text                                        # None = the user has no crontab
        self.read_error, self.write_error = read_error, write_error
        self.writes: list[str] = []

    def respond(self, argv, kwargs):
        if argv == ["crontab", "-l"]:
            if self.read_error:
                return self.read_error
            return (1, "", "no crontab for tester\n") if self.text is None else (0, self.text, "")
        if argv == ["crontab", "-"]:
            if self.write_error:
                return self.write_error
            assert kwargs.get("input") is not None, "crontab - must receive the new table on stdin"
            assert isinstance(kwargs["input"], bytes) and not kwargs.get("text"), "the user's table is handled as bytes"
            self.text = kwargs["input"].decode("utf-8", errors="surrogateescape")
            self.writes.append(self.text)
            return (0, "", "")
        if argv == ["crontab", "-r"]:
            self.text = None
            return (0, "", "")
        return (2, "", "usage")


class FakeLaunchctl(FakeRunner):
    def __init__(self, loaded=False, bootstrap_rc=0, load_rc=0, bootout_rc=None):
        super().__init__()
        self.loaded, self.bootstrap_rc, self.load_rc, self.bootout_rc = loaded, bootstrap_rc, load_rc, bootout_rc

    def respond(self, argv, kwargs):
        verb = argv[1]
        if verb == "bootout":
            if self.bootout_rc:
                return (self.bootout_rc, "", "Boot-out failed")
            was, self.loaded = self.loaded, False
            return (0, "", "") if was else (113, "", "Boot-out failed: 113: Could not find specified service")
        if verb == "bootstrap":
            if self.bootstrap_rc:
                return (self.bootstrap_rc, "", "Bootstrap failed: 5: Input/output error")
            self.loaded = True
            return (0, "", "")
        if verb == "load":
            if self.load_rc:
                return (self.load_rc, "", "launchctl: load failed")
            self.loaded = True
            return (0, "", "")
        if verb == "unload":
            self.loaded = False
            return (0, "", "")
        if verb == "list":
            rows = ["PID\tStatus\tLabel", "123\t0\tcom.apple.Finder"]
            if self.loaded:
                rows.append(f"-\t0\t{LABEL}")
            return (0, "\n".join(rows) + "\n", "")
        return (2, "", "usage")


class FakeSchtasks(FakeRunner):
    UNRELATED = ['"\\Microsoft\\Windows\\Defrag\\ScheduledDefrag","N/A","Ready"',
                 '"\\Connecto-notes","N/A","Ready"',                       # starts with Connecto- but is not ours
                 '"\\OneDrive Standalone Update Task","10/7/2026 9:00:00 AM","Ready"']

    def __init__(self, tasks=(), fail_create=(), fail_delete=(), query_rc=0):
        super().__init__()
        self.tasks = {f"\\{n}": "10/7/2026 {t}:00 AM".format(t=f"{n[-4:-2]}:{n[-2:]}") for n in tasks}
        self.fail_create, self.fail_delete, self.query_rc = set(fail_create), set(fail_delete), query_rc

    def respond(self, argv, kwargs):
        verb = argv[1]
        if verb == "/Create":
            name = argv[argv.index("/TN") + 1]
            if name in self.fail_create:
                return (1, "", "ERROR: Access is denied.")
            self.tasks[f"\\{name}"] = f"10/7/2026 {argv[argv.index('/ST') + 1]}:00 AM"
            return (0, f'SUCCESS: The scheduled task "{name}" has successfully been created.\n', "")
        if verb == "/Delete":
            name = argv[argv.index("/TN") + 1]
            if name in self.fail_delete or f"\\{name}" not in self.tasks:
                return (1, "", "ERROR: The system cannot find the file specified.")
            del self.tasks[f"\\{name}"]
            return (0, f'SUCCESS: The scheduled task "{name}" was successfully deleted.\n', "")
        if verb == "/Query":
            if self.query_rc:
                return (self.query_rc, "", "ERROR: Access is denied.")
            out = io.StringIO()
            w = csv.writer(out, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
            for name, nxt in self.tasks.items():
                w.writerow([name, nxt, "Ready"])
            return (0, out.getvalue() + "\r\n".join(self.UNRELATED) + "\r\n", "")
        return (2, "", "usage")

    @property
    def names(self):
        return sorted(n.lstrip("\\") for n in self.tasks)


def windows_home(root=r"C:\Users\Alex Smith\.connecto"):
    """A Home look-alike with Windows paths (the builders only use .root and .logs_dir)."""
    p = PureWindowsPath(root)
    return types.SimpleNamespace(root=p, logs_dir=p / "logs")


def posix_home(root="/home/alex/.connecto"):
    p = Path(root)
    return types.SimpleNamespace(root=p, logs_dir=p / "logs")


# ----------------------------------------------------------------------------- parse_times
@pytest.mark.parametrize("spec,expected", [
    ("10:40,18:20", [(10, 40), (18, 20)]),
    (" 18:20 , 10:40 ,10:40 ", [(10, 40), (18, 20)]),             # sorted and de-duplicated
    ("10:40 18:20", [(10, 40), (18, 20)]),
    ("10:40;18:20", [(10, 40), (18, 20)]),
    (["09:05", "9:05"], [(9, 5)]),
    (["10:40,18:20"], [(10, 40), (18, 20)]),
    ([(18, 20), (10, 40)], [(10, 40), (18, 20)]),                  # what parse_times itself returns is accepted again
    ("00:00,23:59", [(0, 0), (23, 59)]),
    ("07:30", [(7, 30)]),
])
def test_parse_times_accepts_strings_lists_and_pairs(spec, expected):
    assert parse_times(spec) == expected
    assert parse_times(parse_times(spec)) == expected


@pytest.mark.parametrize("spec", [
    "", "   ", [], (), None, "25:00", "10:60", "24:00", "-1:30", "10", "10:5", "ten:forty", "10:40pm", "10:40,xx",
    [(10, 70)], [(1, 2, 3)], [(24, 0)], [1], [None], 5,
])
def test_parse_times_rejects_bad_input(spec):
    with pytest.raises(ValueError):
        parse_times(spec)


# ----------------------------------------------------------------------------- the command
def test_command_is_the_scheduled_run(home):
    assert command(home, PY) == [PY, "-m", "connecto", "--home", str(home.root), "run", "--scheduled"]
    assert command(home)[0] == sys.executable                      # default: the interpreter running Connecto
    assert command(home, Path("/opt/venv/bin/python"))[0] == "/opt/venv/bin/python"


# ----------------------------------------------------------------------------- launchd
def test_build_launchd_plist_exact_and_plistlib_roundtrip(home):
    py = "/Users/alex smith/venv/bin/python"
    plist = build_launchd_plist(home, "18:20,10:40", python=py)
    root = str(home.root)
    assert plist == {
        "Label": "com.connecto.daily",
        "ProgramArguments": [py, "-m", "connecto", "--home", root, "run", "--scheduled"],
        "WorkingDirectory": root,
        "StartCalendarInterval": [{"Hour": 10, "Minute": 40}, {"Hour": 18, "Minute": 20}],
        "RunAtLoad": False,
        "StandardOutPath": str(home.logs_dir / "launchd.out.log"),
        "StandardErrorPath": str(home.logs_dir / "launchd.err.log"),
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin", "LANG": "en_US.UTF-8",
                                 "CONNECTO_HOME": root},
    }
    back = plistlib.loads(plistlib.dumps(plist))
    assert back == plist and back["RunAtLoad"] is False
    assert " " in back["WorkingDirectory"]                         # spaces are kept as they are: no shell is involved
    assert build_launchd_plist(home, [(7, 5)], PY)["StartCalendarInterval"] == [{"Hour": 7, "Minute": 5}]
    with pytest.raises(ValueError):
        build_launchd_plist(home, "", PY)


def test_launchd_plist_path_lives_under_the_users_launch_agents(sandbox):
    assert launchd_plist_path() == sandbox / "Library" / "LaunchAgents" / "com.connecto.daily.plist"


# ----------------------------------------------------------------------------- cron block
def test_build_cron_block_exact_text():
    block = build_cron_block(posix_home(), "10:40,18:20", PY)
    cd = "cd /home/alex/.connecto && DISPLAY=${DISPLAY:-:0}"
    tail = "/usr/bin/python3 -m connecto --home /home/alex/.connecto run --scheduled >> /home/alex/.connecto/logs/cron.log 2>&1"
    assert block == (
        "# >>> connecto >>>\n"
        "# Managed by Connecto - delete this whole block to stop the daily runs.\n"
        f"40 10 * * * {cd} {tail}\n"
        f"20 18 * * * {cd} {tail}\n"
        "# <<< connecto <<<\n")


def test_build_cron_block_minutes_and_hours_are_not_padded_and_times_sorted():
    lines = build_cron_block(posix_home(), "18:05,09:00,00:30", PY).splitlines()
    assert [ln.split(" * * * ")[0] for ln in lines if ln and not ln.startswith("#")] == ["30 0", "0 9", "5 18"]


def test_build_cron_block_quotes_paths_with_spaces_and_shell_characters():
    h = posix_home("/home/alex smith/it's $HOME/.connecto")
    block = build_cron_block(h, "10:40", "/opt/my python/bin/python3")
    line = block.splitlines()[2]
    root_q = shlex.quote("/home/alex smith/it's $HOME/.connecto")
    assert line == (f"40 10 * * * cd {root_q} && DISPLAY=${{DISPLAY:-:0}} '/opt/my python/bin/python3' -m connecto "
                    f"--home {root_q} run --scheduled >> {shlex.quote('/home/alex smith/it' + chr(39) + 's $HOME/.connecto/logs/cron.log')} 2>&1")
    # what the shell sees is exactly the original strings
    cmd = line.split(" * * * ", 1)[1]
    assert shlex.split(cmd.split("&&", 1)[0])[1] == "/home/alex smith/it's $HOME/.connecto"


def test_build_cron_block_escapes_percent_because_cron_turns_it_into_a_newline():
    block = build_cron_block(posix_home("/home/alex/100%/.connecto"), "10:40", PY)
    line = block.splitlines()[2]
    assert "100\\%" in line and "100%" not in line.replace("\\%", "")
    assert all("\\%" in ln for ln in block.splitlines() if ln.startswith("40 10"))


def test_build_cron_block_refuses_a_path_with_a_line_break():
    with pytest.raises(ValueError):
        build_cron_block(posix_home("/home/alex/bad\nname"), "10:40", PY)


@posix_only
def test_the_generated_cron_line_really_runs_in_a_shell(tmp_path):
    """Run the command part of a cron line through /bin/sh (as cron does, with `\\%` turned back into `%`)."""
    h = Home(tmp_path / "it's a $HOME 100%" / ".connecto").ensure()
    fake_python = tmp_path / "py thon" / "python3"
    fake_python.parent.mkdir()
    fake_python.write_text('#!/bin/sh\necho "DISPLAY=$DISPLAY"\nfor a in "$@"; do echo "arg: $a"; done\n')
    fake_python.chmod(0o755)
    line = build_cron_block(h, "10:40", str(fake_python)).splitlines()[2]
    cmd = line.split(" ", 5)[5].replace("\\%", "%")
    env = {k: v for k, v in os.environ.items() if k != "DISPLAY"}
    done = subprocess.run(["/bin/sh", "-c", cmd], capture_output=True, text=True, env=env)
    assert done.returncode == 0 and done.stderr == ""
    logged = (h.logs_dir / "cron.log").read_text().splitlines()
    assert logged == ["DISPLAY=:0", "arg: -m", "arg: connecto", "arg: --home", f"arg: {h.root}", "arg: run", "arg: --scheduled"]


# ----------------------------------------------------------------------------- merge_cron
BLOCK = build_cron_block(posix_home(), "10:40,18:20", PY)
OTHER = "# my jobs\nMAILTO=me@example.com\n0 3 * * * /usr/local/bin/backup --all\n30 4 * * 1 echo 'weekly'\n"


def test_merge_cron_inserts_into_an_empty_crontab():
    assert merge_cron("", BLOCK) == BLOCK
    assert merge_cron(None, BLOCK) == BLOCK


def test_merge_cron_appends_after_existing_lines_byte_for_byte():
    assert merge_cron(OTHER, BLOCK) == OTHER + BLOCK
    assert merge_cron(OTHER.rstrip("\n"), BLOCK) == OTHER + BLOCK      # a missing final newline is supplied


def test_merge_cron_replaces_our_block_in_place_and_nothing_else():
    new = build_cron_block(posix_home(), "07:15", PY)
    existing = "# top\n" + BLOCK + "5 5 * * * after\n"
    merged = merge_cron(existing, new)
    assert merged == "# top\n" + new + "5 5 * * * after\n"
    assert merged.count(CRON_BEGIN) == 1


def test_merge_cron_leaves_other_marked_blocks_alone():
    other = "# >>> something else >>>\n1 1 * * * x\n# <<< something else <<<\n"
    merged = merge_cron(other + BLOCK, build_cron_block(posix_home(), "09:00", PY))
    assert merged.startswith(other) and merged.count("something else") == 2


def test_merge_cron_removes_our_block_only():
    existing = OTHER + BLOCK
    assert merge_cron(existing, None) == OTHER
    assert merge_cron("# a\n" + BLOCK + "# b\n", None) == "# a\n# b\n"
    assert merge_cron(BLOCK, None) == ""                                # nothing left
    assert merge_cron(OTHER, None) == OTHER                             # not there: unchanged
    assert merge_cron(OTHER.rstrip("\n"), None) == OTHER                # ... apart from the final newline
    assert merge_cron("", None) == ""


def test_merge_cron_is_idempotent_and_install_then_remove_restores_the_original():
    once = merge_cron(OTHER, BLOCK)
    assert merge_cron(once, BLOCK) == once
    assert merge_cron(merge_cron(once, BLOCK), None) == OTHER
    assert merge_cron(merge_cron(once, None), None) == OTHER


def test_merge_cron_collapses_a_duplicated_block():
    twice = BLOCK + OTHER + BLOCK
    assert merge_cron(twice, BLOCK) == BLOCK + OTHER
    assert merge_cron(twice, None) == OTHER


def test_merge_cron_never_guesses_when_a_block_is_unterminated():
    broken = OTHER + CRON_BEGIN + "\n40 10 * * * something\n"
    with pytest.raises(ValueError):
        merge_cron(broken, BLOCK)
    with pytest.raises(ValueError):
        merge_cron(broken, None)


def test_merge_cron_a_stray_end_marker_is_just_a_comment():
    existing = f"{CRON_END}\n1 1 * * * x\n"
    assert merge_cron(existing, BLOCK) == existing + BLOCK


def test_merge_cron_keeps_windows_line_endings_and_odd_whitespace():
    existing = "# a\r\n0 1 * * * x\r\n\x0c\r\n" + f"  {CRON_BEGIN}  \r\n1 1 * * * y\r\n{CRON_END}\r\n" + "9 9 * * * z\r\n"
    merged = merge_cron(existing, BLOCK)
    assert merged.startswith("# a\r\n0 1 * * * x\r\n\x0c\r\n") and merged.endswith(BLOCK + "9 9 * * * z\r\n")


@pytest.mark.parametrize("bad", ["", "no markers\n", f"{CRON_BEGIN}\nonly start\n", f"{CRON_END}\n"])
def test_merge_cron_refuses_a_block_without_both_markers(bad):
    with pytest.raises(ValueError):
        merge_cron(OTHER, bad)


# ----------------------------------------------------------------------------- Windows builders
def test_build_schtasks_commands_exact_with_spaces_and_backslashes():
    py = r"C:\Program Files\Python313\python.exe"
    cmds = build_schtasks_commands(windows_home(), "18:20,10:40", python=py)
    tr = subprocess.list2cmdline([py, "-m", "connecto", "--home", r"C:\Users\Alex Smith\.connecto", "run", "--scheduled"])
    assert tr == '"C:\\Program Files\\Python313\\python.exe" -m connecto --home "C:\\Users\\Alex Smith\\.connecto" run --scheduled'
    assert cmds == [
        ["schtasks", "/Create", "/F", "/SC", "DAILY", "/ST", "10:40", "/TN", "Connecto-1040", "/TR", tr, "/IT"],
        ["schtasks", "/Create", "/F", "/SC", "DAILY", "/ST", "18:20", "/TN", "Connecto-1820", "/TR", tr, "/IT"],
    ]


def test_build_schtasks_commands_plain_paths_need_no_quotes_and_a_trailing_backslash_is_doubled():
    plain = build_schtasks_commands(windows_home(r"C:\Connecto"), "09:05", python=r"C:\Py\python.exe")[0]
    assert plain[plain.index("/TR") + 1] == r"C:\Py\python.exe -m connecto --home C:\Connecto run --scheduled"
    assert plain[plain.index("/TN") + 1] == "Connecto-0905" and plain[plain.index("/ST") + 1] == "09:05"
    drive_root = build_schtasks_commands(windows_home("C:\\"), "09:05", python=r"C:\Py\python.exe")[0]
    assert "--home C:\\ run" in drive_root[drive_root.index("/TR") + 1]   # no spaces -> no quoting -> backslash as is
    spaced = build_schtasks_commands(types.SimpleNamespace(root="C:\\My Folder\\", logs_dir=None), "09:05", r"C:\Py\python.exe")[0]
    assert '"C:\\My Folder\\\\"' in spaced[spaced.index("/TR") + 1]       # the closing quote must not eat the backslash


def test_task_names_and_delete_commands():
    assert task_names("18:20,10:40,07:05") == ["Connecto-0705", "Connecto-1040", "Connecto-1820"]
    assert build_schtasks_delete(["Connecto-1040", "Connecto-1820"]) == [
        ["schtasks", "/Delete", "/F", "/TN", "Connecto-1040"], ["schtasks", "/Delete", "/F", "/TN", "Connecto-1820"]]
    assert build_schtasks_delete([]) == []


def test_parse_schtasks_query_only_finds_our_root_level_tasks():
    out = ('\ufeff"\\Connecto-1040","10/7/2026 10:40:00 AM","Ready"\r\n'
           '"\\Connecto-1820","N/A","Disabled"\r\n'
           '"\\Microsoft\\Windows\\Connecto-1000","N/A","Ready"\r\n'      # nested folder: not ours
           '"\\Connecto-notes","N/A","Ready"\r\n'
           '"\\Connecto-2500","N/A","Ready"\r\n'                          # not a time
           '"\\OtherConnecto-1040","N/A","Ready"\r\n'
           '\r\nINFO: something else\r\n')
    assert parse_schtasks_query(out) == [
        {"name": "Connecto-1040", "time": "10:40", "next_run": "10/7/2026 10:40:00 AM", "state": "Ready"},
        {"name": "Connecto-1820", "time": "18:20", "next_run": "N/A", "state": "Disabled"}]
    assert parse_schtasks_query("") == [] and parse_schtasks_query("INFO: no tasks\r\n") == []


# ----------------------------------------------------------------------------- install / uninstall / status: macOS
def test_install_darwin_writes_the_plist_then_bootout_and_bootstrap(home, sandbox):
    run = FakeLaunchctl()
    res = install(home, "10:40,18:20", platform="darwin", python=PY, runner=run)
    path = sandbox / "Library" / "LaunchAgents" / "com.connecto.daily.plist"
    assert res.ok
    assert run.argvs == [["launchctl", "bootout", "gui/501", str(path)], ["launchctl", "bootstrap", "gui/501", str(path)]]
    assert res.commands == run.argvs
    assert plistlib.loads(path.read_bytes()) == build_launchd_plist(home, "10:40,18:20", PY)
    assert home.logs_dir.is_dir()                                      # launchd will not start a job whose log folder is missing
    assert res.messages[0] == "Installed: Connecto will run at 10:40 and 18:20 every day while you're logged in."
    assert run.loaded


def test_install_darwin_replaces_an_earlier_schedule(home, sandbox):
    run = FakeLaunchctl()
    install(home, "10:40,18:20", platform="darwin", python=PY, runner=run)
    res = install(home, "12:00", platform="darwin", python=PY, runner=run)
    path = launchd_plist_path()
    assert res.ok and res.messages[0] == "Installed: Connecto will run at 12:00 every day while you're logged in."
    assert plistlib.loads(path.read_bytes())["StartCalendarInterval"] == [{"Hour": 12, "Minute": 0}]
    assert run.argvs[2:] == [["launchctl", "bootout", "gui/501", str(path)], ["launchctl", "bootstrap", "gui/501", str(path)]]


def test_install_darwin_falls_back_to_launchctl_load_on_older_macos(home):
    run = FakeLaunchctl(bootstrap_rc=125)
    res = install(home, "10:40", platform="darwin", python=PY, runner=run)
    assert res.ok and [a[1] for a in run.argvs] == ["bootout", "bootstrap", "load"]
    assert run.argvs[-1] == ["launchctl", "load", str(launchd_plist_path())]


def test_install_darwin_reports_a_launchctl_refusal(home):
    run = FakeLaunchctl(bootstrap_rc=5, load_rc=1)
    res = install(home, "10:40", platform="darwin", python=PY, runner=run)
    assert not res.ok and "launchctl: load failed" in res.messages[0]
    assert [a[1] for a in run.argvs] == ["bootout", "bootstrap", "load"]


def test_install_darwin_without_launchctl(home):
    run = FakeRunner([(["launchctl"], FileNotFoundError("launchctl"))])
    res = install(home, "10:40", platform="darwin", python=PY, runner=run)
    assert not res.ok and "launchctl" in res.messages[0]


def test_install_darwin_dry_run_changes_nothing(tmp_path, sandbox):
    never = Home(tmp_path / "never-created")
    run = FakeLaunchctl()
    res = install(never, "10:40,18:20", platform="darwin", python=PY, dry_run=True, runner=run)
    path = launchd_plist_path()
    assert res.ok and run.calls == []
    assert res.commands == [["launchctl", "bootout", "gui/501", str(path)], ["launchctl", "bootstrap", "gui/501", str(path)]]
    assert not path.exists() and not (sandbox / "Library").exists() and not never.root.exists()
    assert "10:40 and 18:20" in " ".join(res.messages)


def test_uninstall_darwin_unloads_and_deletes_the_plist(home):
    run = FakeLaunchctl()
    install(home, "10:40", platform="darwin", python=PY, runner=run)
    path = launchd_plist_path()
    run.calls.clear()
    res = uninstall(home, platform="darwin", runner=run)
    assert res.ok and not path.exists() and not run.loaded
    assert run.argvs == [["launchctl", "bootout", "gui/501", str(path)]]
    assert res.messages == ["Removed: Connecto will no longer run by itself."]


def test_uninstall_darwin_uses_unload_when_bootout_fails_and_nothing_installed_is_fine(home):
    install(home, "10:40", platform="darwin", python=PY, runner=FakeLaunchctl())
    run = FakeLaunchctl(bootout_rc=3)
    res = uninstall(home, platform="darwin", runner=run)
    assert res.ok and [a[1] for a in run.argvs] == ["bootout", "unload"] and not launchd_plist_path().exists()
    again = FakeLaunchctl()
    res = uninstall(home, platform="darwin", runner=again)              # no plist any more: address the job by its label
    assert res.ok and res.messages == ["Nothing to remove: Connecto wasn't scheduled."]
    assert again.argvs == [["launchctl", "bootout", f"gui/501/{LABEL}"]]


def test_uninstall_darwin_dry_run_changes_nothing(home):
    install(home, "10:40", platform="darwin", python=PY, runner=FakeLaunchctl())
    run = FakeLaunchctl(loaded=True)
    res = uninstall(home, platform="darwin", dry_run=True, runner=run)
    assert res.ok and run.calls == [] and launchd_plist_path().exists() and run.loaded


def test_status_darwin(home):
    assert status(home, platform="darwin", runner=FakeLaunchctl()) == {
        "installed": False, "platform": "darwin", "mechanism": "launchd", "times": [],
        "message": "Daily runs are not set up.",
        "details": [f"Schedule file: {launchd_plist_path()} (absent)", "launchd: not loaded"]}
    run = FakeLaunchctl()
    install(home, "18:20,10:40", platform="darwin", python=PY, runner=run)
    st = status(home, platform="darwin", runner=run)
    assert st["installed"] and st["times"] == ["10:40", "18:20"] and st["platform"] == "darwin"
    assert st["message"] == "Connecto runs at 10:40 and 18:20 every day while you're logged in."
    assert st["details"][-1].startswith("launchd: loaded")
    run.loaded = False                                                  # file there, but launchd does not have it
    st = status(home, platform="darwin", runner=run)
    assert not st["installed"] and "launchd: not loaded" in st["details"]


# ----------------------------------------------------------------------------- install / uninstall / status: Linux (cron)
def test_install_linux_on_a_fresh_account_writes_just_our_block(home):
    cron = FakeCrontab(None)
    res = install(home, "10:40,18:20", platform="linux", python=PY, runner=cron)
    assert res.ok and cron.argvs == [["crontab", "-l"], ["crontab", "-"]] and res.commands == cron.argvs
    assert cron.text == build_cron_block(home, "10:40,18:20", PY)
    assert cron.calls[1][1]["input"] == cron.text.encode()             # the table goes to `crontab -` on stdin (as bytes)
    assert res.messages[0] == "Installed: Connecto will run at 10:40 and 18:20 every day while you're logged in."
    joined = " ".join(res.messages)
    assert "DISPLAY" in joined and "desktop session" in joined and "visible browser window" in joined
    assert home.logs_dir.is_dir()                                       # the job's `>> logs/cron.log` needs the folder


def test_install_linux_keeps_every_existing_entry(home):
    cron = FakeCrontab(OTHER)
    res = install(home, "10:40", platform="linux", python=PY, runner=cron)
    assert res.ok and cron.text == OTHER + build_cron_block(home, "10:40", PY)
    assert cron.text.startswith(OTHER)


def test_install_linux_twice_replaces_the_block_and_the_same_install_writes_nothing(home):
    cron = FakeCrontab(OTHER)
    install(home, "10:40,18:20", platform="linux", python=PY, runner=cron)
    install(home, "07:15", platform="linux", python=PY, runner=cron)
    assert cron.text == OTHER + build_cron_block(home, "07:15", PY) and cron.text.count(CRON_BEGIN) == 1
    cron.calls.clear()
    res = install(home, "07:15", platform="linux", python=PY, runner=cron)
    assert res.ok and cron.argvs == [["crontab", "-l"]]                # nothing to change: no write


def test_install_linux_never_overwrites_a_crontab_it_could_not_read(home):
    cron = FakeCrontab(OTHER, read_error=(1, "", "You (alex) are not allowed to use this program (crontab)\n"))
    res = install(home, "10:40", platform="linux", python=PY, runner=cron)
    assert not res.ok and cron.argvs == [["crontab", "-l"]] and cron.writes == []
    assert "not allowed" in res.messages[0]
    assert build_cron_block(home, "10:40", PY).splitlines()[2] in res.messages      # the exact line to add by hand


def test_install_linux_without_crontab_prints_the_lines_to_add(home):
    run = FakeRunner([(["crontab"], FileNotFoundError("crontab"))])
    res = install(home, "10:40,18:20", platform="linux", python=PY, runner=run)
    block = build_cron_block(home, "10:40,18:20", PY)
    assert not res.ok and "crontab" in res.messages[0]
    for line in block.splitlines():
        assert line in res.messages                                     # each exact line, ready to copy
    assert "DISPLAY" in " ".join(res.messages)


def test_install_linux_reports_a_refused_write(home):
    cron = FakeCrontab(None, write_error=(1, "", 'crontab: error in line 3: bad minute\n'))
    res = install(home, "10:40", platform="linux", python=PY, runner=cron)
    assert not res.ok and "bad minute" in res.messages[0]
    assert build_cron_block(home, "10:40", PY).splitlines()[2] in res.messages


def test_install_linux_stops_at_a_half_finished_block(home):
    cron = FakeCrontab(OTHER + CRON_BEGIN + "\n40 10 * * * something\n")
    res = install(home, "10:40", platform="linux", python=PY, runner=cron)
    assert not res.ok and cron.writes == [] and CRON_BEGIN in res.messages[0]


def test_install_linux_dry_run_changes_nothing(tmp_path):
    never = Home(tmp_path / "never-created")
    cron = FakeCrontab(OTHER)
    res = install(never, "10:40", platform="linux", python=PY, dry_run=True, runner=cron)
    assert res.ok and cron.calls == [] and cron.text == OTHER and not never.root.exists()
    for line in build_cron_block(never, "10:40", PY).splitlines():
        assert line in res.messages
    assert res.commands == [["crontab", "-l"], ["crontab", "-"]]


def test_uninstall_linux_removes_only_our_block(home):
    cron = FakeCrontab(OTHER)
    install(home, "10:40,18:20", platform="linux", python=PY, runner=cron)
    cron.calls.clear()
    res = uninstall(home, platform="linux", runner=cron)
    assert res.ok and cron.text == OTHER and cron.argvs == [["crontab", "-l"], ["crontab", "-"]]
    assert cron.calls[1][1]["input"] == OTHER.encode()
    assert res.messages == ["Removed: Connecto will no longer run by itself."]


def test_uninstall_linux_drops_the_crontab_when_only_our_block_was_in_it(home):
    cron = FakeCrontab(None)
    install(home, "10:40", platform="linux", python=PY, runner=cron)
    cron.calls.clear()
    res = uninstall(home, platform="linux", runner=cron)
    assert res.ok and cron.text is None and cron.argvs == [["crontab", "-l"], ["crontab", "-r"]]


def test_uninstall_linux_nothing_to_remove(home):
    for cron in (FakeCrontab(None), FakeCrontab(OTHER)):
        res = uninstall(home, platform="linux", runner=cron)
        assert res.ok and res.messages == ["Nothing to remove: Connecto wasn't scheduled."] and cron.writes == []
        assert cron.argvs == [["crontab", "-l"]]


def test_uninstall_linux_failure_paths(home):
    unreadable = FakeCrontab(OTHER, read_error=(1, "", "permission denied\n"))
    res = uninstall(home, platform="linux", runner=unreadable)
    assert not res.ok and unreadable.writes == []
    half = FakeCrontab(CRON_BEGIN + "\nx\n")
    assert not uninstall(home, platform="linux", runner=half).ok and half.writes == []
    refused = FakeCrontab(OTHER + BLOCK, write_error=(1, "", "nope\n"))
    assert not uninstall(home, platform="linux", runner=refused).ok
    missing = FakeRunner([(["crontab"], FileNotFoundError("crontab"))])
    res = uninstall(home, platform="linux", runner=missing)
    assert not res.ok and CRON_BEGIN in " ".join(res.messages)


def test_uninstall_linux_dry_run_changes_nothing(home):
    cron = FakeCrontab(OTHER + BLOCK)
    res = uninstall(home, platform="linux", dry_run=True, runner=cron)
    assert res.ok and cron.calls == [] and cron.text == OTHER + BLOCK


def test_status_linux(home):
    cron = FakeCrontab(None)
    st = status(home, platform="linux", runner=cron)
    assert st["installed"] is False and st["times"] == [] and st["platform"] == "linux" and st["mechanism"] == "cron"
    install(home, "18:05,10:40", platform="linux", python=PY, runner=cron)
    st = status(home, platform="linux", runner=cron)
    assert st["installed"] and st["times"] == ["10:40", "18:05"]
    assert st["message"] == "Connecto runs at 10:40 and 18:05 every day while you're logged in."
    assert any("cron.log" in d for d in st["details"])
    assert not status(home, platform="linux", runner=FakeCrontab(OTHER))["installed"]
    assert not status(home, platform="linux", runner=FakeCrontab(CRON_BEGIN + "\nx\n"))["installed"]
    missing = status(home, platform="linux", runner=FakeRunner([(["crontab"], FileNotFoundError("crontab"))]))
    assert missing["installed"] is False and "cron isn't installed" in missing["message"]
    assert status(home, platform="linux", runner=FakeCrontab(OTHER, read_error=(1, "", "denied")))["installed"] is False


# ----------------------------------------------------------------------------- Linux with a real subprocess and a fake crontab on PATH
@pytest.fixture
def fake_crontab_on_path(tmp_path, monkeypatch):
    """A real executable called `crontab` (a /bin/sh script keeping its table in a file), the only thing on PATH."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    table, calls = tmp_path / "crontab.table", tmp_path / "crontab.calls"
    script = bindir / "crontab"
    script.write_text(
        '#!/bin/sh\n'
        'echo "$*" >> "$FAKE_CRONTAB_CALLS"\n'
        'case "$1" in\n'
        '  -l) if [ -f "$FAKE_CRONTAB_TABLE" ]; then /bin/cat "$FAKE_CRONTAB_TABLE"; else echo "no crontab for tester" >&2; exit 1; fi ;;\n'
        '  -r) /bin/rm -f "$FAKE_CRONTAB_TABLE" ;;\n'
        '  -) /bin/cat > "$FAKE_CRONTAB_TABLE" ;;\n'
        '  *) echo usage >&2; exit 2 ;;\n'
        'esac\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setenv("FAKE_CRONTAB_TABLE", str(table))
    monkeypatch.setenv("FAKE_CRONTAB_CALLS", str(calls))
    assert shutil.which("crontab") == str(script)                       # the guard: this is not the real crontab
    return types.SimpleNamespace(table=table, calls=calls)


@posix_only
def test_linux_round_trip_through_a_real_subprocess_and_a_fake_crontab(tmp_path, fake_crontab_on_path):
    h = Home(tmp_path / "Alex Smith" / "100% real" / ".connecto")
    fake_crontab_on_path.table.write_text(OTHER)
    res = install(h, "10:40,18:20", platform="linux", python=PY)        # no runner: the default (subprocess.run)
    assert res.ok
    assert fake_crontab_on_path.table.read_text() == OTHER + build_cron_block(h, "10:40,18:20", PY)
    assert fake_crontab_on_path.calls.read_text().split("\n")[:2] == ["-l", "-"]
    st = status(h, platform="linux")
    assert st["installed"] and st["times"] == ["10:40", "18:20"]
    assert uninstall(h, platform="linux").ok
    assert fake_crontab_on_path.table.read_text() == OTHER
    only_ours = tmp_path / "other.table"
    fake_crontab_on_path.table.unlink()
    assert install(h, "10:40", platform="linux", python=PY).ok and uninstall(h, platform="linux").ok
    assert not fake_crontab_on_path.table.exists()                     # `crontab -r`: no empty crontab left behind
    assert not only_ours.exists()


@posix_only
def test_a_crontab_that_is_not_utf8_comes_back_byte_for_byte(tmp_path, fake_crontab_on_path):
    """Entries that are not Connecto's - a Latin-1 comment, Windows line endings, a stray 0xFF - must survive an install and
    an uninstall exactly; decoding with replacement characters would have rewritten them."""
    h = Home(tmp_path / ".connecto")
    original = b"# caf\xe9 and a stray \xff byte\r\n0 1 * * * /bin/true # \xc3(\r\n@reboot echo hi\n"
    fake_crontab_on_path.table.write_bytes(original)
    assert install(h, "10:40", platform="linux", python=PY).ok
    block = build_cron_block(h, "10:40", PY).encode()
    assert fake_crontab_on_path.table.read_bytes() == original + block
    assert status(h, platform="linux")["times"] == ["10:40"]
    assert install(h, "09:00,18:20", platform="linux", python=PY).ok                   # replacing our block in place
    assert fake_crontab_on_path.table.read_bytes() == original + build_cron_block(h, "09:00,18:20", PY).encode()
    assert uninstall(h, platform="linux").ok
    assert fake_crontab_on_path.table.read_bytes() == original


def test_the_crontab_is_read_and_written_as_bytes(home):
    run = FakeCrontab(OTHER)
    assert install(home, "10:40", platform="linux", python=PY, runner=run).ok
    read, write = run.calls[0][1], run.calls[1][1]
    assert read.get("text") is None and "errors" not in read and isinstance(write["input"], bytes) and not write.get("text")
    assert run.text == OTHER + build_cron_block(home, "10:40", PY)


def test_a_crontab_with_windows_line_endings_is_read_correctly(home):
    run = FakeCrontab(OTHER.replace("\n", "\r\n") + build_cron_block(home, "10:40,18:20", PY).replace("\n", "\r\n"))
    assert status(home, platform="linux", runner=run)["times"] == ["10:40", "18:20"]
    assert uninstall(home, platform="linux", runner=run).ok
    assert run.text == OTHER.replace("\n", "\r\n")


@posix_only
def test_missing_crontab_with_the_default_runner(home, monkeypatch, tmp_path):
    empty = tmp_path / "nothing-here"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    res = install(home, "10:40", platform="linux", python=PY)
    assert not res.ok and "crontab" in res.messages[0]
    assert status(home, platform="linux")["installed"] is False


def test_the_default_runner_is_subprocess_run_looked_up_at_call_time(home, monkeypatch):
    seen = []

    def fake_run(argv, **kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "no crontab for tester") if argv[1] == "-l" \
            else subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert install(home, "10:40", platform="linux", python=PY).ok
    assert seen == [["crontab", "-l"], ["crontab", "-"]]


# ----------------------------------------------------------------------------- install / uninstall / status: Windows
WIN_PY = r"C:\Program Files\Python313\python.exe"


def test_install_windows_creates_one_task_per_time(home):
    run = FakeSchtasks()
    res = install(home, "18:20,10:40", platform="win32", python=WIN_PY, runner=run)
    creates = build_schtasks_commands(home, "10:40,18:20", WIN_PY)
    assert res.ok and run.argvs == creates + [["schtasks", "/Query", "/FO", "CSV", "/NH"]] and res.commands == run.argvs
    assert run.names == ["Connecto-1040", "Connecto-1820"]
    assert res.messages[0] == "Installed: Connecto will run at 10:40 and 18:20 every day while you're logged in."
    assert home.logs_dir.is_dir()


def test_install_windows_replaces_tasks_from_an_earlier_install_and_never_touches_other_tasks(home):
    run = FakeSchtasks(tasks=["Connecto-0900", "Connecto-1040", "Connecto-1820"])
    res = install(home, "10:40", platform="win32", python=WIN_PY, runner=run)
    assert res.ok and run.names == ["Connecto-1040"]
    deletes = [a for a in run.argvs if a[1] == "/Delete"]
    assert deletes == build_schtasks_delete(["Connecto-0900", "Connecto-1820"])
    assert "Connecto-notes" in FakeSchtasks.UNRELATED[1]                    # a lookalike exists in the Query output and survives


def test_install_windows_failure_stops_before_removing_anything(home):
    run = FakeSchtasks(tasks=["Connecto-0900"], fail_create={"Connecto-1820"})
    res = install(home, "10:40,18:20", platform="win32", python=WIN_PY, runner=run)
    assert not res.ok and "Connecto-1820" in " ".join(res.messages) and "Access is denied" in " ".join(res.messages)
    assert not any(a[1] in ("/Delete", "/Query") for a in run.argvs)       # the old schedule is left alone
    assert "Connecto-0900" in run.names


def test_install_windows_reports_tasks_it_could_not_remove(home):
    run = FakeSchtasks(tasks=["Connecto-0900"], fail_delete={"Connecto-0900"})
    res = install(home, "10:40", platform="win32", python=WIN_PY, runner=run)
    assert not res.ok and any("Connecto-0900" in m for m in res.messages)


def test_install_windows_refuses_a_command_longer_than_schtasks_allows(home):
    run = FakeSchtasks()
    res = install(home, "10:40", platform="win32", python="C:\\" + "x" * 300 + "\\python.exe", runner=run)
    assert not res.ok and run.calls == [] and "261" in res.messages[0]


def test_install_windows_without_schtasks(home):
    run = FakeRunner([(["schtasks"], FileNotFoundError("schtasks"))])
    res = install(home, "10:40", platform="win32", python=WIN_PY, runner=run)
    assert not res.ok and "schtasks" in res.messages[0]


def test_install_windows_dry_run_changes_nothing(tmp_path):
    never = Home(tmp_path / "never-created")
    run = FakeSchtasks()
    res = install(never, "10:40,18:20", platform="win32", python=WIN_PY, dry_run=True, runner=run)
    assert res.ok and run.calls == [] and not never.root.exists()
    assert res.commands == build_schtasks_commands(never, "10:40,18:20", WIN_PY)


def test_uninstall_windows_deletes_only_connecto_tasks(home):
    run = FakeSchtasks(tasks=["Connecto-1040", "Connecto-1820"])
    res = uninstall(home, platform="win32", runner=run)
    assert res.ok and res.messages == ["Removed: Connecto will no longer run by itself."]
    assert run.argvs == [["schtasks", "/Query", "/FO", "CSV", "/NH"]] + build_schtasks_delete(["Connecto-1040", "Connecto-1820"])
    assert run.names == []                                              # (the lookalike and system tasks are not in .tasks)


def test_uninstall_windows_failure_paths(home):
    nothing = FakeSchtasks()
    res = uninstall(home, platform="win32", runner=nothing)
    assert res.ok and res.messages == ["Nothing to remove: Connecto wasn't scheduled."] and len(nothing.calls) == 1
    cannot_list = FakeSchtasks(query_rc=1)
    res = uninstall(home, platform="win32", runner=cannot_list)
    assert not res.ok and len(cannot_list.calls) == 1
    stuck = FakeSchtasks(tasks=["Connecto-1040"], fail_delete={"Connecto-1040"})
    res = uninstall(home, platform="win32", runner=stuck)
    assert not res.ok and "Connecto-1040" in " ".join(res.messages)
    res = uninstall(home, platform="win32", runner=FakeRunner([(["schtasks"], FileNotFoundError("schtasks"))]))
    assert not res.ok


def test_uninstall_windows_dry_run_changes_nothing(home):
    run = FakeSchtasks(tasks=["Connecto-1040"])
    res = uninstall(home, platform="win32", dry_run=True, runner=run)
    assert res.ok and run.calls == [] and run.names == ["Connecto-1040"]


def test_status_windows(home):
    run = FakeSchtasks()
    st = status(home, platform="win32", runner=run)
    assert st["installed"] is False and st["times"] == [] and st["platform"] == "win32" and st["mechanism"] == "Task Scheduler"
    install(home, "18:20,10:40", platform="win32", python=WIN_PY, runner=run)
    st = status(home, platform="win32", runner=run)
    assert st["installed"] and st["times"] == ["10:40", "18:20"]
    assert st["message"] == "Connecto runs at 10:40 and 18:20 every day while you're logged in."
    assert [d.split(":")[0] for d in st["details"]] == ["Connecto-1040", "Connecto-1820"]
    assert status(home, platform="win32", runner=FakeSchtasks(query_rc=1))["installed"] is False
    assert status(home, platform="win32", runner=FakeRunner([(["schtasks"], FileNotFoundError("x"))]))["installed"] is False


# ----------------------------------------------------------------------------- everything else
@pytest.mark.parametrize("name", ["plan9", "freebsd13", "cygwin", "aix"])
def test_unknown_platform_gives_guidance_and_touches_nothing(home, name):
    run = FakeRunner()
    res = install(home, "10:40", platform=name, python=PY, runner=run)
    assert not res.ok and res.commands == [] and run.calls == []
    assert shlex.join(command(home, PY)) in res.messages and name in res.messages[0]
    assert not uninstall(home, platform=name, runner=run).ok
    st = status(home, platform=name, runner=run)
    assert st["installed"] is False and st["platform"] == name and run.calls == []


@pytest.mark.parametrize("name,family", [("linux", "linux"), ("linux2", "linux"), ("darwin", "darwin"), ("win32", "win32")])
def test_platform_names_are_matched_loosely(home, name, family):
    assert scheduler._family(name) == family


def test_the_platform_defaults_to_sys_platform(home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    cron = FakeCrontab(None)
    assert install(home, "10:40", python=PY, runner=cron).ok and cron.text.startswith(CRON_BEGIN)


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_bad_times_are_reported_not_raised_and_nothing_runs(home, platform):
    run = FakeRunner()
    res = install(home, "25:99", platform=platform, python=PY, runner=run)
    assert isinstance(res, SchedResult) and not res.ok and run.calls == [] and res.commands == []
    assert "10:40,18:20" in res.messages[0]


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_dry_run_never_calls_the_runner_or_writes_anything(tmp_path, sandbox, platform):
    never = Home(tmp_path / "dry-run-home")
    run = FakeRunner()
    before = sorted(p for p in sandbox.rglob("*"))
    for res in (install(never, "10:40", platform=platform, python=PY, dry_run=True, runner=run),
                uninstall(never, platform=platform, dry_run=True, runner=run)):
        assert res.ok and res.commands and run.calls == []
    assert sorted(p for p in sandbox.rglob("*")) == before and not never.root.exists()


def test_a_home_with_spaces_works_on_every_platform(home, sandbox):
    assert " " in str(home.root)
    assert plistlib.loads(plistlib.dumps(build_launchd_plist(home, "10:40", PY)))["ProgramArguments"][4] == str(home.root)
    line = build_cron_block(home, "10:40", PY).splitlines()[2]
    assert shlex.quote(str(home.root)) in line and line.count("'") >= 6
    tr = build_schtasks_commands(home, "10:40", PY)[0]
    assert tr[tr.index("/TR") + 1].count('"') == 2
