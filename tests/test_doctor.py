"""doctor.py: every check on its own, the report, and the promise that a broken check never breaks the doctor."""
import os
import stat
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from connecto import config as cfgmod
from connecto import doctor, runner, scheduler
from connecto.doctor import FAIL, INFO, OK, WARN, Check
from connecto.lock import RunLock
from connecto.paths import Home
from connecto.store import Store
from connecto.util import set_env_var

import fake_ai_server as fake
from helpers import add_people, make_cfg, person


@pytest.fixture()
def home(tmp_path):
    return Home(tmp_path / "home")


def by_id(checks, check_id):
    found = [c for c in checks if c.id == check_id]
    assert found, f"no '{check_id}' check in {[c.id for c in checks]}"
    return found[0]


# ============================================================================= the summary and the report
def test_a_setup_is_healthy_unless_something_failed():
    checks = [Check("a", OK, "fine"), Check("b", OK, "fine"), Check("c", WARN, "hm"), Check("d", INFO, "fyi")]
    assert doctor.summarize(checks) == {"healthy": True, "ok": 2, "warn": 1, "fail": 0, "info": 1}
    broken = checks + [Check("e", FAIL, "broken")]
    summary = doctor.summarize(broken)
    assert summary["healthy"] is False and summary["fail"] == 1 and summary["ok"] == 2          # (the count is not clobbered)
    assert doctor.summarize([]) == {"healthy": True, "ok": 0, "warn": 0, "fail": 0, "info": 0}


def test_the_report_is_plain_ascii_and_names_the_fix_for_everything_that_needs_one():
    checks = [Check("a", OK, "Python 3.13", "", "ignored for ok"), Check("b", WARN, "Look at this", "detail one\ndetail two", "do this"),
              Check("c", FAIL, "Broken", "why", "fix it like so"), Check("d", INFO, "Just so you know", "", "optional step")]
    text = doctor.format_report(checks)
    text.encode("ascii")                                                         # a console with an old code page must cope
    assert "[ ok ] Python 3.13" in text and "[warn] Look at this" in text and "[FAIL] Broken" in text and "[info] Just so you know" in text
    assert "        detail one\n        detail two" in text
    assert "-> do this" in text and "-> fix it like so" in text and "-> optional step" in text and "ignored for ok" not in text
    assert text.splitlines()[-1] == "1 ok, 1 to look at, 1 problem. Fix the [FAIL] items first."


def test_the_report_ends_cleanly_when_all_is_well():
    text = doctor.format_report([Check("a", OK, "fine"), Check("b", FAIL, "no")][:1])
    assert text.splitlines()[-1] == "1 ok, 0 to look at, 0 problems."
    assert "problem." in doctor.format_report([Check("b", FAIL, "no")])


# ============================================================================= the individual checks
def test_python_version(monkeypatch):
    assert doctor.check_python().status == OK
    old = SimpleNamespace(version_info=SimpleNamespace(major=3, minor=9, micro=1), executable=sys.executable)
    monkeypatch.setattr(doctor, "sys", old)
    check = doctor.check_python()
    assert check.status == FAIL and "3.9.1" in check.title and "3.10" in check.detail and check.fix


def test_the_folder_check_creates_nothing_and_warns_when_others_can_read_it(home):
    assert doctor.check_home(home).status == INFO and not home.root.exists()
    home.ensure()
    assert doctor.check_home(home).status == OK and not (home.root / ".write-test").exists()
    if os.name == "posix":
        os.chmod(home.root, 0o755)
        check = doctor.check_home(home)
        assert check.status == WARN and f"chmod 700 {home.root}" in check.fix
        assert stat.S_IMODE(home.root.stat().st_mode) == 0o755                      # the check itself changes nothing


def test_the_folder_check_fails_when_it_cannot_write(home, monkeypatch):
    home.ensure()

    def denied(self, *a, **k):
        raise PermissionError("read-only file system")

    monkeypatch.setattr(Path, "write_text", denied)
    check = doctor.check_home(home)
    assert check.status == FAIL and "read-only" in check.detail and "--home" in check.fix


def test_config_check_missing_valid_broken_and_with_notices(home):
    check, cfg, _ = doctor.check_config(home)
    assert check.status == WARN and "connecto init" in check.fix and cfg["acknowledged_risks"] is False

    cfgmod.save(home, make_cfg())
    check, cfg, warnings = doctor.check_config(home)
    assert check.status == OK and cfg["me"]["first_name"] == "Asha" and warnings == []

    home.config_path.write_text("limits: [unclosed\n  - : :")
    check, cfg, warnings = doctor.check_config(home)
    assert check.status == FAIL and cfg is None and str(home.config_path) in check.fix

    home.config_path.write_text("limits:\n  weekly: 5000\nai:\n  provider: skynet\n")
    check, cfg, warnings = doctor.check_config(home)
    assert check.status == WARN and "2 notices" in check.title and len(warnings) >= 2 and cfg["limits"]["weekly"] == 100


def test_ready_means_a_name_and_the_accepted_risks():
    assert doctor.check_ready(make_cfg()).status == OK
    check = doctor.check_ready(make_cfg(me={"first_name": ""}, acknowledged_risks=False))
    assert check.status == FAIL and "first name" in check.detail.lower() and "risk" in check.detail.lower()


def test_playwright_is_found_here():
    check = doctor.check_playwright()
    assert check.status == OK and "Playwright" in check.title


def test_a_missing_playwright_is_a_failure_with_the_install_command(monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright", None)                            # makes `import playwright` raise ImportError
    check = doctor.check_playwright()
    assert check.status == FAIL and "-m pip install playwright" in check.fix


def test_the_browser_check_prefers_the_real_browser_then_the_bundled_one(tmp_path, monkeypatch):
    chrome = tmp_path / "chrome"
    chrome.write_text("")
    monkeypatch.setattr(doctor, "_browser_candidates", lambda channel, platform: [str(chrome)])
    check = doctor.check_browser({"browser": {"channel": "chrome"}})
    assert check.status == OK and "Google Chrome" in check.title and check.detail == str(chrome)

    bundled = tmp_path / "chromium"
    bundled.write_text("")
    monkeypatch.setattr(doctor, "_browser_candidates", lambda channel, platform: [])
    monkeypatch.setattr(doctor, "_playwright_chromium", lambda: str(bundled))
    check = doctor.check_browser({"browser": {"channel": "chrome"}})
    assert check.status == WARN and "Google Chrome was not found" in check.detail and "browser.channel" in check.fix
    assert doctor.check_browser({"browser": {"channel": "msedge"}}).detail.startswith("Microsoft Edge was not found")
    assert doctor.check_browser({"browser": {"channel": "chromium"}}).status == OK            # asked for it: nothing to warn about

    monkeypatch.setattr(doctor, "_playwright_chromium", lambda: "")
    check = doctor.check_browser({"browser": {"channel": "chrome"}})
    assert check.status == FAIL and "playwright install chromium" in check.fix


def test_where_a_normal_browser_install_lives(monkeypatch):
    mac = doctor._browser_candidates("chrome", "darwin")
    assert mac[0] == "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    assert doctor._browser_candidates("msedge", "darwin") == ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
    monkeypatch.setenv("PROGRAMFILES", r"C:\Program Files")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\x\AppData\Local")
    monkeypatch.delenv("PROGRAMFILES(X86)", raising=False)
    win = doctor._browser_candidates("chrome", "win32")
    assert len(win) == 2 and all(p.endswith("chrome.exe") for p in win) and "Program Files" in win[0]
    assert all(p.endswith("msedge.exe") for p in doctor._browser_candidates("msedge", "win32"))
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/google-chrome" if name == "google-chrome" else None)
    assert doctor._browser_candidates("chrome", "linux") == ["/usr/bin/google-chrome"]
    assert doctor._browser_candidates("chromium", "linux") == []


@pytest.mark.parametrize("headless,display,status", [(True, True, WARN), (False, False, FAIL), (False, True, OK)])
def test_a_visible_browser_needs_a_desktop(monkeypatch, headless, display, status):
    monkeypatch.setattr(runner, "has_display", lambda: display)
    check = doctor.check_display({"browser": {"headless": headless}})
    assert check.status == status
    if status == FAIL:
        assert "headless server" in check.fix


def test_sources_are_described_and_a_missing_csv_is_a_failure(home, tmp_path):
    none = doctor.check_sources(home, make_cfg())
    assert [c.status for c in none] == [INFO] and "add people by hand" in none[0].detail
    leads = tmp_path / "leads.csv"
    leads.write_text("linkedin_url\nhttps://www.linkedin.com/in/jane/\n")
    cfg = make_cfg(sources=[{"type": "yc", "batches": ["S26"]}, {"type": "csv", "path": str(leads)},
                            {"type": "csv", "path": str(tmp_path / "gone.csv")}])
    checks = doctor.check_sources(home, cfg)
    assert [c.status for c in checks] == [OK, OK, FAIL] and "gone.csv" in checks[2].detail and checks[2].fix
    cfg["sources"] = [{"type": "no-such-kind", "enabled": True}]
    (broken,) = doctor.check_sources(home, cfg)
    assert broken.status == FAIL and "not valid" in broken.title


def test_the_people_check(home):
    assert doctor.check_people(home, make_cfg()).status == INFO                      # no database yet
    add_people(home, [person("a-one"), person("b-two")])
    check = doctor.check_people(home, make_cfg())
    assert check.status == OK and "2 people ready to invite" in check.title
    with Store(home.db_path) as st:
        st.record(person("a-one"), "sent")
        st.record(person("b-two"), "sent")
    check = doctor.check_people(home, make_cfg())
    assert check.status == WARN and "nobody left to invite" in check.title and check.fix


def test_the_ai_check_without_the_helper_with_and_without_a_key(home, clean_env):
    assert doctor.check_ai(home, make_cfg()).status == INFO
    assert doctor.check_ai(home, make_cfg(ai={"enabled": True, "provider": "gemini"})).status == FAIL
    on = make_cfg(ai={"enabled": True, "provider": "groq"})
    check = doctor.check_ai(home, on)
    assert check.status == FAIL and "GROQ_API_KEY" in check.detail and "connecto ai key" in check.fix
    set_env_var(home.env_path, "GROQ_API_KEY", "gsk_test_1234567890")                  # saved the way `connecto ai key` saves it
    ok = doctor.check_ai(home, on)
    assert ok.status == OK and "api.groq.com" in ok.title and "gsk_test_1234567890" not in ok.title + ok.detail
    ollama = doctor.check_ai(home, make_cfg(ai={"enabled": True, "provider": "ollama"}))
    assert ollama.status == OK                                                           # needs no key at all


def test_the_online_ai_check_makes_one_small_request_and_reports_trouble_gently(home, clean_env):
    server = fake.FakeAIServer().start()
    try:
        set_env_var(home.env_path, "MY_SERVER_API_KEY", "sk-test-abcdefgh1234")
        cfg = make_cfg(ai={"enabled": True, "provider": "custom", "base_url": server.base_url, "api_key_env": "MY_SERVER_API_KEY",
                           "model": "test-model"})
        assert doctor.check_ai(home, cfg).status == OK and not server.requests          # not online: no request at all
        check = doctor.check_ai(home, cfg, online=True)
        assert check.status == OK and server.requests
        server.default_chat = fake.unauthorized()
        server.requests.clear()
        check = doctor.check_ai(home, cfg, online=True)
        assert check.status == WARN and "did not answer" in check.title and "templates" in check.fix       # sending still works
    finally:
        server.stop()


def test_the_limits_check_reports_numbers_that_had_to_be_pulled_back():
    assert doctor.check_limits(make_cfg()).status == OK and "a day" in doctor.check_limits(make_cfg()).title
    wild = make_cfg()
    wild["limits"]["weekly"] = 5000
    wild["limits"]["daily"] = [90, 99]
    check = doctor.check_limits(wild)
    assert check.status == WARN and "adjusted" in check.title and check.detail


def test_the_schedule_check(home, monkeypatch):
    monkeypatch.setattr(scheduler, "status", lambda h, **kw: {"installed": True, "message": "On: 10:40, 18:20", "details": ["a", "b"]})
    check = doctor.check_schedule(home)
    assert check.status == OK and check.detail == "a\nb"
    monkeypatch.setattr(scheduler, "status", lambda h, **kw: {"installed": False, "message": "Off", "details": []})
    check = doctor.check_schedule(home)
    assert check.status == INFO and "schedule install" in check.fix


def test_the_state_check_notices_a_run_a_pause_blocked_notes_and_the_last_run(home):
    assert doctor.check_state(home) == []                                              # a fresh folder: nothing to say
    home.ensure()
    with RunLock(home.lock_path):
        assert by_id(doctor.check_state(home), "running").status == INFO
    with Store(home.db_path) as st:
        runner.set_pause(st, timedelta(days=2), "LinkedIn showed a limit warning")
        st.kv_set("note_blocked_until", (runner.datetime.now() + timedelta(days=9)).isoformat(timespec="seconds"))
        run_id = st.start_run("scheduled")
        st.finish_run(run_id, 3, 4, "run limit reached")
    checks = doctor.check_state(home)
    assert by_id(checks, "paused").status == WARN and "limit warning" in by_id(checks, "paused").detail
    assert by_id(checks, "notes_blocked").status == WARN and "resume" in by_id(checks, "paused").fix
    last = by_id(checks, "last_run")
    assert last.status == INFO and "sent 3 of 4" in last.title and last.detail == "run limit reached"
    with Store(home.db_path) as st:
        runner.clear_pause(st)
        st.kv_set("note_blocked_until", "2001-01-01T00:00:00")
    assert {c.id for c in doctor.check_state(home)} == {"last_run"}                  # old news is not repeated


# ============================================================================= trying the browser
def test_the_launch_check_starts_a_throwaway_browser_and_always_cleans_up(home, monkeypatch):
    home.ensure()
    events = []

    class Page:
        def goto(self, url):
            events.append(("goto", url))
            if broken["goto"]:
                raise RuntimeError("page crashed")

    broken = {"goto": False}
    import connecto.browser as browser
    monkeypatch.setattr(browser, "launch_browser", lambda h, c, headless=None, profile="linkedin": (
        events.append(("launch", profile, headless)) or ("pw", "ctx", Page())))
    monkeypatch.setattr(browser, "close_browser", lambda pw, ctx: events.append(("close", pw, ctx)))
    (home.browser_dir / "doctor").mkdir(parents=True)
    assert doctor.check_browser_launch(home, make_cfg()).status == OK
    assert events[0] == ("launch", "doctor", True) and events[-1] == ("close", "pw", "ctx")      # never the LinkedIn profile
    assert not (home.browser_dir / "doctor").exists()

    broken["goto"] = True
    check = doctor.check_browser_launch(home, make_cfg())
    assert check.status == FAIL and "could not open a page" in check.title and events[-1] == ("close", "pw", "ctx")

    def unavailable(*a, **k):
        raise browser.BrowserUnavailable("No browser could be started.")

    monkeypatch.setattr(browser, "launch_browser", unavailable)
    assert doctor.check_browser_launch(home, make_cfg()).status == FAIL


@pytest.mark.browser
def test_the_launch_check_really_starts_chromium(home):
    check = doctor.check_browser_launch(home, make_cfg())
    assert check.status == OK, check
    assert not (home.browser_dir / "doctor").exists()


# ============================================================================= all of them
@pytest.fixture()
def ready_machine(monkeypatch):
    monkeypatch.setattr(runner, "has_display", lambda: True)
    monkeypatch.setattr(doctor, "_playwright_chromium", lambda: "/fake/chromium")
    monkeypatch.setattr(scheduler, "status", lambda h, **kw: {"installed": False, "message": "Off", "details": []})


def test_a_ready_setup_is_healthy_and_the_report_says_so(home, ready_machine):
    cfgmod.save(home, make_cfg(sources=[{"type": "yc"}], browser={"channel": "chromium", "headless": False}))
    checks = doctor.run_checks(home)
    assert doctor.summarize(checks)["healthy"] is True, doctor.format_report(checks)
    assert [c.id for c in checks][:4] == ["python", "home", "config", "ready"]
    assert doctor.format_report(checks).splitlines()[-1].endswith("0 problems.")


def test_a_fresh_machine_is_not_ready_and_the_report_says_what_to_do_first(home, ready_machine):
    checks = doctor.run_checks(home)
    assert by_id(checks, "config").status == WARN and by_id(checks, "ready").status == FAIL
    assert doctor.summarize(checks)["healthy"] is False and "connecto init" in doctor.format_report(checks)


def test_an_unreadable_settings_file_stops_the_checks_that_need_it(home):
    home.ensure()
    home.config_path.write_text("limits: [unclosed\n  - : :")
    checks = doctor.run_checks(home)
    assert [c.id for c in checks] == ["python", "home", "config"] and checks[-1].status == FAIL


def test_a_check_that_crashes_is_reported_and_the_others_still_run(home, ready_machine, monkeypatch):
    cfgmod.save(home, make_cfg())

    def check_playwright(*a, **k):
        raise RuntimeError("something unexpected")

    monkeypatch.setattr(doctor, "check_playwright", check_playwright)
    checks = doctor.run_checks(home)
    crashed = by_id(checks, "playwright")
    assert crashed.status == WARN and "could not run" in crashed.title and "RuntimeError: something unexpected" in crashed.detail
    assert by_id(checks, "limits").status == OK and by_id(checks, "ready").status == OK


def test_the_costly_checks_only_run_when_asked(home, ready_machine, monkeypatch):
    cfgmod.save(home, make_cfg())
    called = []
    monkeypatch.setattr(doctor, "check_browser_launch", lambda h, c: called.append("launch") or Check("browser_launch", OK, "x"))
    doctor.run_checks(home)
    assert called == []
    assert "browser_launch" in {c.id for c in doctor.run_checks(home, launch=True)} and called == ["launch"]
