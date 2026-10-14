"""Run Connecto by itself, once or twice a day, on macOS, Linux and Windows.

Connecto opens a *visible* browser window (hiding it is possible but discouraged), so every scheduler used here starts
the job inside the signed-in user's own desktop session: a launchd LaunchAgent on macOS, a cron entry on Linux, an
"interactive only" scheduled task on Windows. The scheduled command is always::

    <python> -m connecto --home <home> run --scheduled

Every OS interaction is split in two:

* a *pure builder* that returns exactly what will be written or run (``build_launchd_plist``, ``build_cron_block`` /
  ``merge_cron``, ``build_schtasks_commands``) - unit-tested for exact content; and
* a thin installer (``install`` / ``uninstall`` / ``status``) that executes it through an injectable ``runner``
  (default ``subprocess.run``), so tests can use a fake runner and never touch the real LaunchAgents folder, crontab or
  Task Scheduler.

There is one Connecto schedule per user account (fixed launchd label, one marked cron block, ``Connecto-HHMM`` task
names): installing again replaces the previous one, whatever its times were. ``dry_run=True`` never calls the runner and
never writes a file; it only returns what *would* happen.
"""
from __future__ import annotations

import csv
import io
import os
import plistlib
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, NamedTuple

from .paths import Home
from .util import atomic_write, parse_hhmm

LABEL = "com.connecto.daily"                 # launchd label; Windows task names are "Connecto-HHMM"
TASK_PREFIX = "Connecto-"
CRON_BEGIN = "# >>> connecto >>>"
CRON_END = "# <<< connecto <<<"
LAUNCHD_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
SCHTASKS_TR_MAX = 261                       # schtasks refuses a /TR value longer than this
SCHTASKS_QUERY = ["schtasks", "/Query", "/FO", "CSV", "/NH"]

DESKTOP_NOTE = ("Cron jobs need a desktop session: Connecto opens a visible browser window (it uses DISPLAY=:0 unless "
                "DISPLAY is already set), so nothing will run while you're logged out.")

_TASK_NAME = re.compile(r"^Connecto-(\d{2})(\d{2})$")
_CRON_TIME = re.compile(r"^\s*(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+\*\s")


@dataclass
class SchedResult:
    ok: bool
    messages: list[str] = field(default_factory=list)
    commands: list[list[str]] = field(default_factory=list)


# ================================================================== times and the command
def parse_times(spec) -> list[tuple[int, int]]:
    """``"10:40,18:20"`` (or a list of ``"HH:MM"`` strings / ``(hour, minute)`` pairs) -> sorted, de-duplicated
    ``[(10, 40), (18, 20)]``. Raises ``ValueError`` on anything else, and when no time is left."""
    if spec is None:
        raise ValueError("no times given (expected HH:MM, e.g. 10:40,18:20)")
    try:
        items = [spec] if isinstance(spec, str) else list(spec)
    except TypeError:
        raise ValueError(f"bad times {spec!r} (expected HH:MM, e.g. 10:40,18:20)") from None
    found: set[tuple[int, int]] = set()
    for item in items:
        if isinstance(item, str):
            for token in (t for t in re.split(r"[,;\s]+", item.strip()) if t):
                found.add(parse_hhmm(token))
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            try:
                h, m = int(item[0]), int(item[1])
            except (TypeError, ValueError):
                raise ValueError(f"bad time {item!r} (expected HH:MM)") from None
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError(f"bad time {item!r}")
            found.add((h, m))
        else:
            raise ValueError(f"bad time {item!r} (expected HH:MM)")
    if not found:
        raise ValueError("no times given (expected HH:MM, e.g. 10:40,18:20)")
    return sorted(found)


def command(home: Home, python: str | None = None) -> list[str]:
    """The argv every scheduler runs. ``python`` defaults to the interpreter running Connecto now (a venv's python is
    deliberately not resolved, or its site-packages would be lost)."""
    return [str(python or sys.executable or "python3"), "-m", "connecto", "--home", str(home.root), "run", "--scheduled"]


def _hhmm(h: int, m: int) -> str:
    return f"{h:02d}:{m:02d}"


def _fmt_times(times: list[tuple[int, int]]) -> str:
    parts = [_hhmm(h, m) for h, m in times]
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


# ================================================================== macOS: launchd
def build_launchd_plist(home: Home, times, python: str | None = None) -> dict:
    """The LaunchAgent as a plistlib-ready dict. ``times`` takes anything ``parse_times`` accepts."""
    root = str(home.root)
    return {
        "Label": LABEL,
        "ProgramArguments": command(home, python),
        "WorkingDirectory": root,
        "StartCalendarInterval": [{"Hour": h, "Minute": m} for h, m in parse_times(times)],
        "RunAtLoad": False,
        "StandardOutPath": str(home.logs_dir / "launchd.out.log"),
        "StandardErrorPath": str(home.logs_dir / "launchd.err.log"),
        "EnvironmentVariables": {"PATH": LAUNCHD_PATH, "LANG": "en_US.UTF-8", "CONNECTO_HOME": root},
    }


def launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


# ================================================================== Linux: cron
def build_cron_block(home: Home, times, python: str | None = None) -> str:
    """The marked crontab block: one line per time, everything shell-quoted, ``%`` escaped (cron turns a bare ``%``
    into a newline). Always ends with a newline."""
    cmd = command(home, python)
    log_path = str(home.logs_dir / "cron.log")
    if any(("\n" in p or "\r" in p) for p in [*cmd, log_path]):
        raise ValueError("a path contains a line break, which cron cannot run")
    prefix = f"cd {shlex.quote(str(home.root))} && DISPLAY=${{DISPLAY:-:0}} {shlex.join(cmd)} >> {shlex.quote(log_path)} 2>&1"
    lines = [CRON_BEGIN, "# Managed by Connecto - delete this whole block to stop the daily runs."]
    for h, m in parse_times(times):
        lines.append(f"{m} {h} * * * {prefix}".replace("%", "\\%"))
    lines.append(CRON_END)
    return "\n".join(lines) + "\n"


def _split_lines(text: str) -> list[str]:
    """Lines with their endings, splitting on newlines only (``str.splitlines`` would also split on form feeds etc.)."""
    return re.findall(r"[^\n]*\n|[^\n]+", text)


def merge_cron(existing: str, block: str | None) -> str:
    """Put ``block`` into the crontab text ``existing`` (replacing our marked block in place, or appending it), or remove
    our block when ``block`` is None. Every other byte is kept as it was; a non-empty result ends with a newline.

    Raises ``ValueError`` if a block is opened but never closed (we will not guess how much of the user's crontab is
    ours) or if ``block`` is not a marked block. A duplicated block is collapsed into one.
    """
    if block is not None:
        if not block.endswith("\n"):
            block += "\n"
        blines = block.splitlines()
        if not blines or blines[0].strip() != CRON_BEGIN or blines[-1].strip() != CRON_END:
            raise ValueError(f"block must start with {CRON_BEGIN!r} and end with {CRON_END!r}")
    lines = _split_lines(existing or "")
    out: list[str] = []
    placed = False
    i = 0
    while i < len(lines):
        if lines[i].strip() == CRON_BEGIN:
            j = i + 1
            while j < len(lines) and lines[j].strip() != CRON_END:
                j += 1
            if j >= len(lines):
                raise ValueError(f"the crontab has a {CRON_BEGIN!r} line but no {CRON_END!r} line after it")
            if block is not None and not placed:
                out.append(block)
                placed = True
            i = j + 1
            continue
        out.append(lines[i])
        i += 1
    if block is not None and not placed:
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        out.append(block)
    text = "".join(out)
    if text and not text.endswith("\n"):
        text += "\n"
    return text


def _cron_block_lines(text: str) -> list[str] | None:
    """The lines between our markers (first complete block), or None."""
    lines = [line.rstrip("\r\n") for line in _split_lines(text or "")]
    for i, line in enumerate(lines):
        if line.strip() == CRON_BEGIN:
            for j in range(i + 1, len(lines)):
                if lines[j].strip() == CRON_END:
                    return lines[i + 1:j]
            return None
    return None


def _cron_times(block_lines: list[str]) -> list[str]:
    found = set()
    for line in block_lines:
        m = _CRON_TIME.match(line)
        if m and int(m.group(2)) < 24 and int(m.group(1)) < 60:
            found.add((int(m.group(2)), int(m.group(1))))
    return [_hhmm(h, m) for h, m in sorted(found)]


# ================================================================== Windows: Task Scheduler
def task_names(times) -> list[str]:
    """``Connecto-HHMM`` for each time."""
    return [f"{TASK_PREFIX}{h:02d}{m:02d}" for h, m in parse_times(times)]


def build_schtasks_commands(home: Home, times, python: str | None = None) -> list[list[str]]:
    """One ``schtasks /Create`` argv per time. The ``/TR`` value is the command line built with
    ``subprocess.list2cmdline`` (quotes only what needs it, doubles trailing backslashes); ``/IT`` = only while the
    user is logged on, which is what a visible browser needs."""
    tr = subprocess.list2cmdline(command(home, python))
    return [["schtasks", "/Create", "/F", "/SC", "DAILY", "/ST", _hhmm(h, m), "/TN", f"{TASK_PREFIX}{h:02d}{m:02d}",
             "/TR", tr, "/IT"] for h, m in parse_times(times)]


def build_schtasks_delete(names: Iterable[str]) -> list[list[str]]:
    """One ``schtasks /Delete /F /TN <name>`` argv per task name (see ``task_names``)."""
    return [["schtasks", "/Delete", "/F", "/TN", n] for n in names]


def parse_schtasks_query(output: str) -> list[dict]:
    """Our tasks in the CSV from ``schtasks /Query /FO CSV /NH``: ``[{"name": "Connecto-1040", "time": "10:40",
    "next_run": "...", "state": "Ready"}]``. Only root-level tasks named exactly ``Connecto-HHMM`` count, so a task of
    someone else's that merely starts with "Connecto" is never touched."""
    found = []
    for row in csv.reader(io.StringIO((output or "").lstrip("\ufeff"))):
        if not row:
            continue
        raw = row[0].strip()
        if not raw.startswith("\\"):
            continue
        m = _TASK_NAME.match(raw[1:])
        if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
            found.append({"name": raw[1:], "time": f"{m.group(1)}:{m.group(2)}",
                          "next_run": row[1].strip() if len(row) > 1 else "",
                          "state": row[2].strip() if len(row) > 2 else ""})
    return found


# ================================================================== running things
class _Out(NamedTuple):
    rc: int
    out: str
    err: str

    @property
    def text(self) -> str:
        """The program's own message, safe to print (a byte that is not UTF-8 shows as ``?``)."""
        return (self.err or self.out or "").strip().encode("utf-8", errors="replace").decode("utf-8")


class _ToolMissing(Exception):
    def __init__(self, tool: str):
        super().__init__(tool)
        self.tool = tool


def _text(value) -> str:
    """A program's output as text. A byte that is not UTF-8 survives as a lone surrogate (``surrogateescape``) instead of
    being replaced, so that what was read can be written back byte for byte (see ``_run(raw=True)``)."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="surrogateescape")
    return str(value)


def _run(runner: Callable | None, argv: list[str], log: list[list[str]], *, input: str | None = None,
         raw: bool = False) -> _Out:
    """Run through the (injectable) runner, recording the argv. A missing program raises ``_ToolMissing``; any other
    failure to start is returned as a non-zero result so callers handle one kind of failure.

    ``raw`` is for a table the user keeps (the crontab): bytes in and out, nothing translated, nothing replaced - the
    entries that are not ours must come back exactly as they were, whatever encoding or line endings they use."""
    run = runner or subprocess.run          # looked up now, so a monkeypatched subprocess.run is honoured
    log.append(list(argv))
    kwargs: dict = {"capture_output": True} if raw else {"capture_output": True, "text": True, "errors": "replace"}
    if input is not None:
        kwargs["input"] = input.encode("utf-8", errors="surrogateescape") if raw else input
    try:
        cp = run(list(argv), **kwargs)
    except FileNotFoundError as exc:
        raise _ToolMissing(argv[0]) from exc
    except OSError as exc:
        return _Out(126, "", str(exc))
    if cp is None:
        return _Out(0, "", "")
    return _Out(int(getattr(cp, "returncode", 0) or 0), _text(getattr(cp, "stdout", "")), _text(getattr(cp, "stderr", "")))


def _family(platform: str | None) -> str:
    p = (platform or sys.platform or "").lower()
    if p == "darwin":
        return "darwin"
    if p.startswith("linux"):
        return "linux"
    if p.startswith("win"):
        return "win32"
    return p or "unknown"


def _uid() -> int:
    getuid = getattr(os, "getuid", None)
    return int(getuid()) if getuid else 0


def _unsupported(platform: str, home: Home, python: str | None) -> SchedResult:
    return SchedResult(False, [
        f"Connecto can't set up daily runs by itself on '{platform}' yet.",
        "You can still schedule this command with your system's scheduler, once or twice a day, while you are logged "
        "in to your desktop (the browser window is visible, so it cannot run in the background):",
        shlex.join(command(home, python)),
    ], [])


def _missing(tool: str, commands: list[list[str]]) -> SchedResult:
    return SchedResult(False, [f"Couldn't find the '{tool}' command on this computer, so the daily schedule wasn't changed."],
                       commands)


# ================================================================== install
def install(home: Home, times, *, platform: str | None = None, python: str | None = None, dry_run: bool = False,
            runner: Callable | None = None) -> SchedResult:
    """Schedule ``connecto run --scheduled`` at ``times`` every day. ``runner`` defaults to ``subprocess.run``."""
    try:
        parsed = parse_times(times)
    except ValueError as exc:
        return SchedResult(False, [f"Couldn't read those times ({exc}). Use 24-hour HH:MM, for example 10:40,18:20."], [])
    fam = _family(platform)
    handler = {"darwin": _install_darwin, "linux": _install_linux, "win32": _install_windows}.get(fam)
    if handler is None:
        return _unsupported(fam, home, python)
    return handler(home, parsed, python, dry_run, runner)


def _install_darwin(home: Home, times, python, dry_run, runner) -> SchedResult:
    plist = build_launchd_plist(home, times, python)
    path = launchd_plist_path()
    domain = f"gui/{_uid()}"
    bootout = ["launchctl", "bootout", domain, str(path)]
    bootstrap = ["launchctl", "bootstrap", domain, str(path)]
    when = _fmt_times(times)
    if dry_run:
        return SchedResult(True, [f"Dry run, nothing was changed. Connecto would run at {when} every day while you're logged in.",
                                  f"It would write {path} and load it with launchctl."], [bootout, bootstrap])
    commands: list[list[str]] = []
    try:
        home.ensure()                                     # launchd will not start a job whose log folder is missing
        _run(runner, bootout, commands)                   # replace an older copy; fine if there is none
        atomic_write(path, plistlib.dumps(plist).decode("utf-8"))
        res = _run(runner, bootstrap, commands)
        if res.rc != 0:                                   # older macOS
            res = _run(runner, ["launchctl", "load", str(path)], commands)
    except _ToolMissing as exc:
        return _missing(exc.tool, commands)
    except OSError as exc:
        return SchedResult(False, [f"Couldn't write the schedule file {path}: {exc}"], commands)
    if res.rc != 0:
        return SchedResult(False, [
            f"macOS didn't accept the schedule (launchctl said: {res.text or 'no details'}).",
            f"The schedule file is at {path}; logging out and back in, or running the install again, usually fixes it."],
            commands)
    return SchedResult(True, [
        f"Installed: Connecto will run at {when} every day while you're logged in.",
        "If your Mac is asleep at those times, the run happens when it wakes up.",
        f"Schedule file: {path}"], commands)


def _manual_cron_lines(block: str) -> list[str]:
    return ["You can add these lines to your crontab yourself (run `crontab -e`):", *block.splitlines()]


def _install_linux(home: Home, times, python, dry_run, runner) -> SchedResult:
    try:
        block = build_cron_block(home, times, python)
    except ValueError as exc:
        return SchedResult(False, [f"Couldn't build the cron entry: {exc}."], [])
    when = _fmt_times(times)
    if dry_run:
        return SchedResult(True, [f"Dry run, nothing was changed. Connecto would run at {when} every day while you're logged in.",
                                  "It would add these lines to your crontab:", *block.splitlines(), DESKTOP_NOTE],
                           [["crontab", "-l"], ["crontab", "-"]])
    commands: list[list[str]] = []
    try:
        home.ensure()                                     # the log folder must exist or the redirect in the job fails
        res = _run(runner, ["crontab", "-l"], commands, raw=True)
        if res.rc == 0:
            existing = res.out
        elif re.search(r"no crontab", res.err + res.out, re.I) and not res.out.strip():
            existing = ""                                  # a fresh account: nothing to keep
        else:                                              # unreadable: never overwrite what we could not read
            return SchedResult(False, [f"Couldn't read your crontab ({res.text or 'unknown error'}), so nothing was changed.",
                                       *_manual_cron_lines(block)], commands)
        try:
            merged = merge_cron(existing, block)
        except ValueError as exc:
            return SchedResult(False, [f"Your crontab has a half-finished Connecto entry ({exc}). Delete the line "
                                       f"'{CRON_BEGIN}' and the Connecto lines after it (`crontab -e`), then try again."], commands)
        if merged != existing:
            res = _run(runner, ["crontab", "-"], commands, input=merged, raw=True)
            if res.rc != 0:
                return SchedResult(False, [f"cron refused the schedule ({res.text or 'unknown error'}).",
                                           *_manual_cron_lines(block)], commands)
    except _ToolMissing:
        return SchedResult(False, ["Couldn't find the 'crontab' command, so cron doesn't seem to be installed on this computer.",
                                   *_manual_cron_lines(block), DESKTOP_NOTE], commands)
    except OSError as exc:
        return SchedResult(False, [f"Couldn't prepare Connecto's folder: {exc}"], commands)
    return SchedResult(True, [f"Installed: Connecto will run at {when} every day while you're logged in.",
                              DESKTOP_NOTE, f"Log file: {home.logs_dir / 'cron.log'}"], commands)


def _install_windows(home: Home, times, python, dry_run, runner) -> SchedResult:
    cmds = build_schtasks_commands(home, times, python)
    tr = cmds[0][cmds[0].index("/TR") + 1]
    when = _fmt_times(times)
    if len(tr) > SCHTASKS_TR_MAX:
        return SchedResult(False, [
            f"The command Windows would have to run is {len(tr)} characters long and Task Scheduler accepts at most "
            f"{SCHTASKS_TR_MAX}.", "Use a shorter Connecto folder (for example --home C:\\Connecto) and try again."], [])
    if dry_run:
        return SchedResult(True, [f"Dry run, nothing was changed. Connecto would run at {when} every day while you're logged in.",
                                  "It would create these tasks (and remove older Connecto tasks with other times):",
                                  *task_names(times)], cmds)
    commands: list[list[str]] = []
    try:
        home.ensure()
        failed = []
        for argv in cmds:
            res = _run(runner, argv, commands)
            if res.rc != 0:
                failed.append(f"{argv[argv.index('/TN') + 1]}: {res.text or 'unknown error'}")
        if failed:
            return SchedResult(False, ["Windows didn't accept the schedule:", *failed], commands)
        # tasks from an earlier install at other times would keep running: remove them
        wanted = set(task_names(times))
        listing = _run(runner, SCHTASKS_QUERY, commands)
        stale = [t["name"] for t in parse_schtasks_query(listing.out) if t["name"] not in wanted] if listing.rc == 0 else []
        leftovers = []
        for argv in build_schtasks_delete(stale):
            res = _run(runner, argv, commands)
            if res.rc != 0:
                leftovers.append(argv[-1])
    except _ToolMissing as exc:
        return _missing(exc.tool, commands)
    except OSError as exc:
        return SchedResult(False, [f"Couldn't prepare Connecto's folder: {exc}"], commands)
    msgs = [f"Installed: Connecto will run at {when} every day while you're logged in.",
            "Windows runs it only while you're signed in; a laptop on battery may skip a run, so keep it plugged in."]
    if leftovers:
        msgs.append("Couldn't remove these older Connecto tasks, delete them in Task Scheduler: " + ", ".join(leftovers))
    return SchedResult(not leftovers, msgs, commands)


# ================================================================== uninstall
def uninstall(home: Home, *, platform: str | None = None, dry_run: bool = False,
              runner: Callable | None = None) -> SchedResult:
    """Remove Connecto's schedule. Succeeds (ok=True) when there was nothing to remove."""
    fam = _family(platform)
    handler = {"darwin": _uninstall_darwin, "linux": _uninstall_linux, "win32": _uninstall_windows}.get(fam)
    if handler is None:
        return _unsupported(fam, home, None)
    return handler(home, dry_run, runner)


def _uninstall_darwin(home: Home, dry_run, runner) -> SchedResult:
    path = launchd_plist_path()
    domain = f"gui/{_uid()}"
    # With the file gone but the job still loaded (deleted by hand), address the job by its label instead.
    bootout = ["launchctl", "bootout", domain, str(path)] if path.exists() else ["launchctl", "bootout", f"{domain}/{LABEL}"]
    if dry_run:
        return SchedResult(True, [f"Dry run, nothing was changed. Connecto's daily schedule would be switched off and {path} deleted."],
                           [bootout])
    commands: list[list[str]] = []
    existed = path.exists()
    try:
        res = _run(runner, bootout, commands)
        if res.rc != 0 and existed:
            _run(runner, ["launchctl", "unload", str(path)], commands)    # older macOS
    except _ToolMissing:
        pass                                                              # no launchctl: just remove the file
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        return SchedResult(False, [f"Couldn't delete {path}: {exc}"], commands)
    if existed:
        return SchedResult(True, ["Removed: Connecto will no longer run by itself."], commands)
    return SchedResult(True, ["Nothing to remove: Connecto wasn't scheduled."], commands)


def _uninstall_linux(home: Home, dry_run, runner) -> SchedResult:
    if dry_run:
        return SchedResult(True, ["Dry run, nothing was changed. The Connecto block would be removed from your crontab "
                                  "(everything else stays as it is)."], [["crontab", "-l"], ["crontab", "-"]])
    commands: list[list[str]] = []
    try:
        res = _run(runner, ["crontab", "-l"], commands, raw=True)
        if res.rc == 0:
            existing = res.out
        elif re.search(r"no crontab", res.err + res.out, re.I) and not res.out.strip():
            return SchedResult(True, ["Nothing to remove: Connecto wasn't scheduled."], commands)
        else:
            return SchedResult(False, [f"Couldn't read your crontab ({res.text or 'unknown error'}), so nothing was changed."], commands)
        if CRON_BEGIN not in existing:
            return SchedResult(True, ["Nothing to remove: Connecto wasn't scheduled."], commands)
        try:
            remaining = merge_cron(existing, None)
        except ValueError as exc:
            return SchedResult(False, [f"Your crontab has a half-finished Connecto entry ({exc}). Delete the line "
                                       f"'{CRON_BEGIN}' and the Connecto lines after it (`crontab -e`)."], commands)
        if remaining.strip():
            res = _run(runner, ["crontab", "-"], commands, input=remaining, raw=True)
        else:                                             # only our block was there: leave no empty crontab behind
            res = _run(runner, ["crontab", "-r"], commands)
        if res.rc != 0:
            return SchedResult(False, [f"cron refused the change ({res.text or 'unknown error'})."], commands)
    except _ToolMissing:
        return SchedResult(False, ["Couldn't find the 'crontab' command, so nothing was changed. If you added Connecto's "
                                   f"lines by hand, delete the block between '{CRON_BEGIN}' and '{CRON_END}'."], commands)
    return SchedResult(True, ["Removed: Connecto will no longer run by itself."], commands)


def _uninstall_windows(home: Home, dry_run, runner) -> SchedResult:
    if dry_run:
        return SchedResult(True, ["Dry run, nothing was changed. Every Connecto-HHMM task would be deleted."], [list(SCHTASKS_QUERY)])
    commands: list[list[str]] = []
    try:
        listing = _run(runner, SCHTASKS_QUERY, commands)
        if listing.rc != 0:
            return SchedResult(False, [f"Couldn't list the scheduled tasks ({listing.text or 'unknown error'}), so nothing was changed."],
                               commands)
        names = [t["name"] for t in parse_schtasks_query(listing.out)]
        failed = []
        for argv in build_schtasks_delete(names):
            res = _run(runner, argv, commands)
            if res.rc != 0:
                failed.append(f"{argv[-1]}: {res.text or 'unknown error'}")
    except _ToolMissing as exc:
        return _missing(exc.tool, commands)
    if failed:
        return SchedResult(False, ["Couldn't remove every Connecto task:", *failed], commands)
    if not names:
        return SchedResult(True, ["Nothing to remove: Connecto wasn't scheduled."], commands)
    return SchedResult(True, ["Removed: Connecto will no longer run by itself."], commands)


# ================================================================== status
def status(home: Home, *, platform: str | None = None, runner: Callable | None = None) -> dict:
    """``{"installed": bool, "platform": ..., "mechanism": ..., "times": ["10:40", ...], "message": one friendly line,
    "details": [...]}``. Read-only; never raises."""
    fam = _family(platform)
    try:
        if fam == "darwin":
            return _status_darwin(home, runner)
        if fam == "linux":
            return _status_linux(home, runner)
        if fam == "win32":
            return _status_windows(home, runner)
    except Exception as exc:  # status must always answer
        return _status_dict(False, fam, "", [], f"Couldn't check the schedule: {exc}", [str(exc)])
    return _status_dict(False, fam, "", [], f"Daily runs aren't supported on '{fam}'.", [])


def _status_dict(installed: bool, platform: str, mechanism: str, times: list[str], message: str, details: list[str]) -> dict:
    return {"installed": installed, "platform": platform, "mechanism": mechanism, "times": times, "message": message,
            "details": details}


def _status_message(installed: bool, times: list[str]) -> str:
    if not installed:
        return "Daily runs are not set up."
    if not times:
        return "Daily runs are set up."
    return f"Connecto runs at {_fmt_times([tuple(int(x) for x in t.split(':')) for t in times])} every day while you're logged in."


def _status_darwin(home: Home, runner) -> dict:
    path = launchd_plist_path()
    exists = path.exists()
    times: list[str] = []
    if exists:
        try:
            with open(path, "rb") as fh:
                plist = plistlib.load(fh)
            slots = plist.get("StartCalendarInterval") or []
            slots = [slots] if isinstance(slots, dict) else slots
            times = [_hhmm(int(s["Hour"]), int(s["Minute"])) for s in slots if "Hour" in s and "Minute" in s]
        except Exception:
            times = []
    details = [f"Schedule file: {path} ({'present' if exists else 'absent'})"]
    loaded = False
    try:
        res = _run(runner, ["launchctl", "list"], [])
        line = next((ln for ln in res.out.splitlines() if ln.split() and ln.split()[-1] == LABEL), None) if res.rc == 0 else None
        loaded = line is not None
        details.append(f"launchd: loaded ({line.strip()})" if line else "launchd: not loaded")
    except _ToolMissing:
        details.append("launchd: the 'launchctl' command was not found")
    installed = exists and loaded
    return _status_dict(installed, "darwin", "launchd", times if installed else [], _status_message(installed, times), details)


def _status_linux(home: Home, runner) -> dict:
    try:
        res = _run(runner, ["crontab", "-l"], [], raw=True)
    except _ToolMissing:
        return _status_dict(False, "linux", "cron", [], "Daily runs are not set up (cron isn't installed).",
                            ["The 'crontab' command was not found."])
    if res.rc != 0:
        why = "No crontab yet." if re.search(r"no crontab", res.err + res.out, re.I) else f"Couldn't read the crontab: {res.text}"
        return _status_dict(False, "linux", "cron", [], "Daily runs are not set up.", [why])
    block = _cron_block_lines(res.out)
    if block is None:
        half = CRON_BEGIN in res.out
        return _status_dict(False, "linux", "cron", [], "Daily runs are not set up.",
                            ["The Connecto entry in your crontab is incomplete (its closing marker is missing)." if half
                             else "There is no Connecto entry in your crontab."])
    times = _cron_times(block)
    return _status_dict(True, "linux", "cron", times, _status_message(True, times),
                        ["Scheduled in your crontab (cron).", f"Log file: {home.logs_dir / 'cron.log'}", DESKTOP_NOTE])


def _status_windows(home: Home, runner) -> dict:
    try:
        res = _run(runner, SCHTASKS_QUERY, [])
    except _ToolMissing:
        return _status_dict(False, "win32", "Task Scheduler", [], "Couldn't check: the 'schtasks' command was not found.", [])
    if res.rc != 0:
        return _status_dict(False, "win32", "Task Scheduler", [], "Couldn't list the scheduled tasks.", [res.text])
    tasks = parse_schtasks_query(res.out)
    times = sorted({t["time"] for t in tasks})
    details = [f"{t['name']}: next run {t['next_run'] or 'unknown'} ({t['state'] or 'unknown state'})" for t in tasks]
    return _status_dict(bool(tasks), "win32", "Task Scheduler", times, _status_message(bool(tasks), times),
                        details or ["There are no Connecto tasks in Task Scheduler."])


__all__ = [
    "LABEL", "SchedResult", "parse_times", "command", "build_launchd_plist", "launchd_plist_path", "build_cron_block",
    "merge_cron", "task_names", "build_schtasks_commands", "build_schtasks_delete", "parse_schtasks_query", "install",
    "uninstall", "status",
]
