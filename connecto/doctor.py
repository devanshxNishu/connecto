"""``connecto doctor``: is everything in place, and if not, what exactly should you do about it?

Every check returns a ``Check`` with a status (ok / warn / fail / info) and, when something is wrong, a ``fix`` that says
what to type or click. The checks are read-only: they never send anything and never touch LinkedIn. Two of them are
opt-in because they cost something: ``launch`` starts a throw-away headless browser, ``online`` makes one tiny request
to your AI provider.
"""
from __future__ import annotations

import os
import shutil
import stat
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from . import config as cfgmod
from . import scheduler
from .paths import Home

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"
MIN_PYTHON = (3, 10)
BROWSER_LABEL = {"chrome": "Google Chrome", "msedge": "Microsoft Edge", "chromium": "Chromium"}


@dataclass
class Check:
    id: str
    status: str            # ok | warn | fail | info
    title: str
    detail: str = ""
    fix: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def summarize(checks: list[Check]) -> dict:
    """Counts per status plus ``healthy`` (no failed check). Warnings and notes do not make a setup unhealthy."""
    counts = {s: sum(1 for c in checks if c.status == s) for s in (OK, WARN, FAIL, INFO)}
    return {"healthy": counts[FAIL] == 0, **counts}


# ----------------------------------------------------------------------------- individual checks
def check_python() -> Check:
    v = sys.version_info
    text = f"{v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) < MIN_PYTHON:
        return Check("python", FAIL, f"Python {text} is too old", "Connecto needs Python 3.10 or newer.",
                     "Install a current Python from python.org, then reinstall Connecto.")
    return Check("python", OK, f"Python {text}")


def check_home(home: Home) -> Check:
    root = home.root
    if not root.exists():
        return Check("home", INFO, f"Connecto's folder will be created at {root}", "", "Run `connecto init` (or `connecto ui`).")
    probe = root / ".write-test"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return Check("home", FAIL, f"Cannot write to {root}", str(exc), "Choose another folder with --home, or fix its permissions.")
    detail = ""
    if os.name == "posix":
        try:
            mode = stat.S_IMODE(root.stat().st_mode)
            if mode & 0o077:
                return Check("home", WARN, f"{root} can be read by other users",
                             "It holds your logged-in LinkedIn browser profile; treat it like a password.",
                             f"chmod 700 {root}")
        except OSError:
            pass
    return Check("home", OK, f"Folder: {root}", detail)


def check_config(home: Home) -> tuple[Check, dict | None, list[str]]:
    """Returns the check, the normalised config (None when unreadable) and the normalisation warnings."""
    if not home.config_path.exists():
        cfg, warnings = cfgmod.normalize({})
        return (Check("config", WARN, "No settings file yet", f"Expected at {home.config_path}.",
                      "Run `connecto init` (a guided setup) or `connecto ui` (the same, in your browser)."), cfg, warnings)
    try:
        cfg, warnings = cfgmod.load(home, with_warnings=True)
    except Exception as exc:  # noqa: BLE001 - YAML errors, wrong top-level type, unreadable file
        first = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
        return (Check("config", FAIL, "The settings file cannot be read", first,
                      f"Fix {home.config_path} (or move it away and run `connecto init`)."), None, [])
    if warnings:
        return (Check("config", WARN, f"Settings loaded with {len(warnings)} notice{'s' if len(warnings) != 1 else ''}",
                      "\n".join(warnings), "Edit config.yaml or use the setup page to correct them."), cfg, warnings)
    return Check("config", OK, "Settings file is valid", str(home.config_path)), cfg, warnings


def check_ready(cfg: dict) -> Check:
    problems = cfgmod.problems(cfg)
    if problems:
        return Check("ready", FAIL, "Not ready to send yet", "\n".join(problems),
                     "Finish the first steps on the Start tab of `connecto ui`, or run `connecto init`.")
    return Check("ready", OK, "Name and risk acknowledgement are in place")


def check_playwright() -> Check:
    try:
        import playwright  # noqa: F401
        from importlib.metadata import version
        try:
            ver = version("playwright")
        except Exception:  # noqa: BLE001
            ver = ""
        return Check("playwright", OK, "Playwright is installed" + (f" ({ver})" if ver else ""))
    except ImportError:
        return Check("playwright", FAIL, "Playwright is not installed", "It drives the browser.",
                     f"{Path(sys.executable).name} -m pip install playwright")


def _browser_candidates(channel: str, platform: str) -> list[str]:
    """Where a normal install of Chrome / Edge lives (plus PATH names on Linux)."""
    home = Path.home()
    env = os.environ
    if channel == "chrome":
        if platform == "darwin":
            return ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    str(home / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome")]
        if platform.startswith("win"):
            return [str(Path(env.get(v, "")) / "Google/Chrome/Application/chrome.exe")
                    for v in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA") if env.get(v)]
        return [p for p in (shutil.which(n) for n in ("google-chrome", "google-chrome-stable", "chrome")) if p]
    if channel == "msedge":
        if platform == "darwin":
            return ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
        if platform.startswith("win"):
            return [str(Path(env.get(v, "")) / "Microsoft/Edge/Application/msedge.exe")
                    for v in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA") if env.get(v)]
        return [p for p in (shutil.which(n) for n in ("microsoft-edge", "microsoft-edge-stable")) if p]
    return []


def _playwright_chromium() -> str:
    """Path of Playwright's own Chromium if it is downloaded, else ''."""
    try:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        try:
            path = pw.chromium.executable_path
        finally:
            pw.stop()
        return path if path and Path(path).exists() else ""
    except Exception:  # noqa: BLE001
        return ""


def check_browser(cfg: dict, *, platform: str | None = None) -> Check:
    channel = (cfg.get("browser") or {}).get("channel", "chrome")
    platform = platform or sys.platform
    found = [p for p in _browser_candidates(channel, platform) if Path(p).exists()]
    label = BROWSER_LABEL.get(channel, channel)
    if found:
        return Check("browser", OK, f"Browser found ({label})", found[0])
    bundled = _playwright_chromium()
    if bundled and channel == "chromium":
        return Check("browser", OK, "Using Playwright's Chromium", bundled)
    if bundled:
        return Check("browser", WARN, "Using Playwright's Chromium",
                     f"{label} was not found, so Connecto will use Playwright's Chromium instead ({bundled}).",
                     f"Install {label}, or choose Chromium under Browser on the Limits tab (browser.channel).")
    return Check("browser", FAIL, "No browser found", f"Neither {label} nor Playwright's Chromium is installed.",
                 f"Install Google Chrome, or run: {Path(sys.executable).name} -m playwright install chromium")


def check_browser_launch(home: Home, cfg: dict) -> Check:
    """Start a throw-away headless browser (its own profile, never the LinkedIn one) and close it again."""
    from .browser import BrowserUnavailable, close_browser, launch_browser
    profile = "doctor"
    try:
        pw, ctx, page = launch_browser(home, cfg, headless=True, profile=profile)
    except BrowserUnavailable as exc:
        return Check("browser_launch", FAIL, "The browser did not start", str(exc))
    except Exception as exc:  # noqa: BLE001
        return Check("browser_launch", FAIL, "The browser did not start", f"{type(exc).__name__}: {exc}")
    try:
        page.goto("about:blank")
        return Check("browser_launch", OK, "The browser starts and opens a page")
    except Exception as exc:  # noqa: BLE001
        return Check("browser_launch", FAIL, "The browser started but could not open a page", str(exc))
    finally:
        close_browser(pw, ctx)
        shutil.rmtree(home.browser_dir / profile, ignore_errors=True)


def check_display(cfg: dict) -> Check:
    from .runner import has_display
    headless = bool((cfg.get("browser") or {}).get("headless"))
    if headless:
        return Check("display", WARN, "Headless mode is on", "A hidden browser is easier for LinkedIn to flag and you cannot "
                     "watch or help it.", "Set browser.headless to false.")
    if not has_display():
        return Check("display", FAIL, "No desktop session found", "Connecto opens a visible browser window, which needs a "
                     "desktop (DISPLAY is not set).", "Run Connecto on your laptop/desktop, not on a headless server.")
    return Check("display", OK, "A desktop session is available for the browser window")


def check_sources(home: Home, cfg: dict) -> list[Check]:
    from .planner import describe_sources
    from .sources import SourceContext, build_source
    specs = cfgmod.enabled_sources(cfg)
    if not specs:
        return [Check("sources", INFO, "No source of people switched on", "You can still add people by hand.",
                      "`connecto add <profile link>` or add a source in `connecto ui`.")]
    out = []
    lines = describe_sources(cfg)
    for spec, line in zip(specs, lines):
        if "invalid" in line:
            out.append(Check("sources", FAIL, f"Source '{spec.get('type')}' is not valid", line))
        elif spec.get("type") == "csv":
            ctx = SourceContext(home=home, cfg=cfg, store=None, limits=None)       # type: ignore[arg-type]
            path = build_source(spec, ctx).resolve_path()
            if path is not None and path.is_file():
                out.append(Check("sources", OK, line))
            else:
                out.append(Check("sources", FAIL, line, f"The file {spec.get('path') or '(no path)'} was not found.",
                                 "Check the path in your settings."))
        else:
            out.append(Check("sources", OK, line))
    return out


def check_people(home: Home, cfg: dict) -> Check:
    from .planner import build_preview
    from .store import Store
    if not home.db_path.exists():
        return Check("people", INFO, "No people stored yet", "", "Run `connecto fetch`, or `connecto add <profile link>`.")
    with Store(home.db_path) as store:
        pv = build_preview(cfg, store, sample=0, example_notes=0)
    if pv["people_total"] == 0:
        return Check("people", INFO, "No people stored yet", "", "Run `connecto fetch`, or `connecto add <profile link>`.")
    if pv["queued"] == 0:
        return Check("people", WARN, f"{pv['people_total']} people stored, nobody left to invite",
                     "Everyone matching your filters has been handled, or nobody matches them.",
                     "Loosen the filters, add a source, or add people by hand.")
    days = f" - about {pv['days_estimate']} day{'s' if pv['days_estimate'] != 1 else ''} of invitations" if pv["days_estimate"] else ""
    return Check("people", OK, f"{pv['queued']} people ready to invite{days}", f"{pv['people_total']} stored in total.")


def check_ai(home: Home, cfg: dict, *, online: bool = False) -> Check:
    from .ai.client import AIClient, AIError
    from .util import load_env_into_process
    load_env_into_process(home.env_path)               # the key lives in the .env file of Connecto's folder
    ai = cfg.get("ai") or {}
    if not ai.get("enabled"):
        return Check("ai", INFO, "AI helper is off (optional)", "Notes use your templates as written - that works fine.",
                     "Turn it on in `connecto ui` (Message tab) with a free key from Gemini, Groq or OpenRouter, or a local model.")
    try:
        client = AIClient.from_config(ai)
    except AIError as exc:
        return Check("ai", FAIL, "AI helper is on but cannot start", str(exc),
                     "Add the key in `connecto ai key`, or switch the helper off. Notes still work without it.")
    label = f"AI helper: {client.base_url}" + (f", model {client.model}" if client.model else ", model chosen automatically")
    if not online:
        return Check("ai", OK, label, "Key present. Run `connecto doctor --online` to make a test request.")
    result = client.test()
    if result.ok:
        return Check("ai", OK, label, result.message)
    return Check("ai", WARN, "AI helper did not answer", result.message,
                 "Connecto falls back to your templates, so sending still works.")


def check_limits(cfg: dict) -> Check:
    lim = cfgmod.limits(cfg)
    summary = (f"{lim.daily[0]}-{lim.daily[1]} a day, at most {lim.weekly} a week, {lim.delay[0]:.0f}-{lim.delay[1]:.0f} s "
               f"between profiles")
    if lim.warnings:
        return Check("limits", WARN, "Safety limits adjusted", "\n".join(lim.warnings))
    return Check("limits", OK, f"Safety limits: {summary}")


def check_schedule(home: Home) -> Check:
    st = scheduler.status(home)
    if st.get("installed"):
        return Check("schedule", OK, st["message"], "\n".join(st.get("details") or []))
    return Check("schedule", INFO, st["message"], "\n".join(st.get("details") or []),
                 "`connecto schedule install` makes it run by itself every day.")


def check_state(home: Home) -> list[Check]:
    from .lock import RunLock
    from .runner import _pause_info
    from .store import Store
    out = []
    if RunLock(home.lock_path).is_held():
        out.append(Check("running", INFO, "A Connecto run is in progress right now", RunLock(home.lock_path).holder()))
    if not home.db_path.exists():
        return out
    with Store(home.db_path) as store:
        paused = _pause_info(store)
        if paused:
            out.append(Check("paused", WARN, f"Sending is paused until {paused[0]:%Y-%m-%d %H:%M}", paused[1],
                             "`connecto resume` lifts the pause (only do this after you looked at LinkedIn yourself)."))
        blocked = store.kv_get("note_blocked_until")
        if blocked:
            try:
                if datetime.fromisoformat(blocked) > datetime.now():
                    out.append(Check("notes_blocked", WARN, f"LinkedIn is limiting personal notes until about {blocked[:10]}",
                                     "Invitations go out without a note meanwhile (free accounts get only a few notes a month).",
                                     "A Premium plan removes this limit; set message.plan accordingly."))
            except ValueError:
                pass
        runs = store.recent_runs(1)
        if runs:
            r = runs[0]
            when = (r.get("started_at") or "")[:16].replace("T", " ")
            out.append(Check("last_run", INFO, f"Last run {when}: sent {r.get('sent') or 0} of {r.get('attempted') or 0}",
                             r.get("stop_reason") or ""))
    return out


# ----------------------------------------------------------------------------- the whole report
def run_checks(home: Home, *, online: bool = False, launch: bool = False) -> list[Check]:
    """All checks in a sensible order. A check that itself crashes is reported as a warning, never raised."""
    checks: list[Check] = []

    def add(fn, *args, **kw):
        try:
            result = fn(*args, **kw)
        except Exception as exc:  # noqa: BLE001
            checks.append(Check(getattr(fn, "__name__", "check").replace("check_", ""), WARN, "A check could not run",
                                f"{type(exc).__name__}: {exc}"))
            return None
        if isinstance(result, list):
            checks.extend(result)
        elif isinstance(result, Check):
            checks.append(result)
        return result

    add(check_python)
    add(check_home, home)
    res = add(check_config, home)
    if res is None:
        cfg, _ = cfgmod.normalize({})
    else:
        check, cfg, _ = res
        checks.append(check)
        if cfg is None:
            return checks
    add(check_ready, cfg)
    add(check_playwright)
    add(check_browser, cfg)
    if launch:
        add(check_browser_launch, home, cfg)
    add(check_display, cfg)
    add(check_sources, home, cfg)
    add(check_people, home, cfg)
    add(check_ai, home, cfg, online=online)
    add(check_limits, cfg)
    add(check_schedule, home)
    add(check_state, home)
    return checks


MARKS = {OK: "[ ok ]", WARN: "[warn]", FAIL: "[FAIL]", INFO: "[info]"}


def format_report(checks: list[Check]) -> str:
    """Plain-ASCII text (safe on any console)."""
    lines = []
    for c in checks:
        lines.append(f"{MARKS[c.status]} {c.title}")
        for extra in (c.detail.splitlines() if c.detail else []):
            lines.append(f"        {extra}")
        if c.fix and c.status in (WARN, FAIL, INFO):
            lines.append(f"        -> {c.fix}")
    s = summarize(checks)
    lines.append("")
    lines.append(f"{s[OK]} ok, {s[WARN]} to look at, {s[FAIL]} problem{'s' if s[FAIL] != 1 else ''}."
                 + ("" if s["healthy"] else " Fix the [FAIL] items first."))
    return "\n".join(lines)
