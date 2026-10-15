"""runner.py without a browser: every reason a run does nothing, the pause bookkeeping, fetch, and the status numbers."""
from __future__ import annotations

import dataclasses
import sys
from datetime import datetime, timedelta

import pytest

from connecto import config, runner
from connecto.browser import BrowserUnavailable
from connecto.lock import RunLock
from connecto.runner import EXIT_BUSY, EXIT_CONFIG, EXIT_NEEDS_YOU, EXIT_OK, RunOptions
from connecto.store import Store

from helpers import FAST, add_people, deletes_an_open_file, make_cfg, make_home, person, refuse_file_locks


@pytest.fixture()
def home(tmp_path):
    return make_home(tmp_path)


@pytest.fixture()
def no_browser(monkeypatch):
    """Any attempt to start a browser fails the test."""
    def boom(*a, **k):
        raise AssertionError("a browser must not be started here")
    monkeypatch.setattr(runner, "launch_browser", boom)


def run(home, cfg=None, **opts):
    logs: list[str] = []
    opts.setdefault("ignore_hours", True)
    opts.setdefault("limits", FAST)
    res = runner.run_send(home, cfg or make_cfg(), RunOptions(headless=True, sleep=lambda s: None, log=logs.append,
                                                              seed=1, **opts))
    return res, "\n".join(logs)


# ----------------------------------------------------------------------------- reasons a run does nothing
def test_an_unconfigured_setup_is_refused_with_the_reasons(home, no_browser):
    cfg = make_cfg()
    cfg["acknowledged_risks"] = False
    res, text = run(home, cfg)
    assert res.exit_code == EXIT_CONFIG and res.stop_reason == "not configured" and "Not ready" in text
    cfg = make_cfg(me={"first_name": ""})
    res, text = run(home, cfg)
    assert res.exit_code == EXIT_CONFIG and "first name" in text.lower()
    cfg = make_cfg(me={"first_name": ""}, message={"send_note": False})              # no notes: the name is not needed
    assert not config.problems(cfg)


def test_outside_the_active_window_nothing_starts(home, no_browser):
    add_people(home, [person("a")])
    never = dataclasses.replace(FAST, active_days=frozenset())
    res, text = run(home, ignore_hours=False, limits=never)
    assert res.exit_code == EXIT_OK and res.stop_reason.startswith("outside active time") and "--ignore-hours" in text
    with Store(home.db_path) as st:
        assert st.recent_runs(1) == []                                                # not even logged as a run


def test_no_desktop_session_is_reported_not_crashed(home, monkeypatch, no_browser):
    monkeypatch.setattr(runner, "has_display", lambda: False)
    cfg = make_cfg(browser={"headless": False})
    logs = []
    res = runner.run_send(home, cfg, RunOptions(limits=FAST, ignore_hours=True, log=logs.append))
    assert res.exit_code == EXIT_NEEDS_YOU and res.stop_reason == "no display" and "visible browser" in "\n".join(logs)


def test_has_display_by_platform(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert runner.has_display() is False
    monkeypatch.setenv("DISPLAY", ":0")
    assert runner.has_display() is True
    monkeypatch.delenv("DISPLAY")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert runner.has_display() is True
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv("WAYLAND_DISPLAY")
    assert runner.has_display() is True


def test_a_second_run_while_one_is_active_backs_off(home, monkeypatch):
    add_people(home, [person("a")])

    def none(*a, **k):
        raise BrowserUnavailable("x")
    monkeypatch.setattr(runner, "launch_browser", none)
    with RunLock(home.lock_path):
        res, text = run(home)
    assert res.exit_code == EXIT_BUSY and "another" in res.stop_reason and "Nothing to do" in text
    res, _ = run(home, fetch=False)                                                  # lock was released: now it proceeds
    assert res.stop_reason == "no browser" and not RunLock(home.lock_path).is_held()


def test_nobody_in_the_queue_is_not_an_error(home, no_browser):
    res, text = run(home)
    assert res.exit_code == EXIT_OK and res.stop_reason == "empty queue" and "Nobody to invite" in text


def test_a_missing_browser_is_a_clear_message(home, monkeypatch):
    add_people(home, [person("a")])

    def none(*a, **k):
        raise BrowserUnavailable("no chrome here")
    monkeypatch.setattr(runner, "launch_browser", none)
    res, text = run(home)
    assert res.exit_code == EXIT_NEEDS_YOU and res.stop_reason == "no browser" and "no chrome here" in text
    with Store(home.db_path) as st:
        assert st.recent_runs(1) == []                                                # nothing started, nothing to record


# ----------------------------------------------------------------------------- pauses
def test_pause_roundtrip_and_expiry(home):
    with Store(home.db_path) as st:
        assert runner._pause_info(st) is None
        runner.set_pause(st, timedelta(days=2), "because")
        when, why = runner._pause_info(st)
        assert why == "because" and timedelta(days=1, hours=23) < when - datetime.now() <= timedelta(days=2)
        st.kv_set("paused_until", (datetime.now() - timedelta(minutes=5)).isoformat())      # an old pause no longer counts
        assert runner._pause_info(st) is None
        st.kv_set("paused_until", "garbage")
        assert runner._pause_info(st) is None
        runner.set_pause(st, timedelta(hours=1), "again")
        runner.clear_pause(st)
        assert runner._pause_info(st) is None and st.kv_get("paused_reason") is None


def test_a_paused_account_is_left_alone_unless_told_otherwise(home, monkeypatch):
    add_people(home, [person("a")])
    with Store(home.db_path) as st:
        runner.set_pause(st, timedelta(days=1), "LinkedIn said stop")
    def none(*a, **k):
        raise BrowserUnavailable("x")
    started = []
    monkeypatch.setattr(runner, "launch_browser", lambda *a, **k: started.append(1) or none())
    res, text = run(home)
    assert res.stop_reason == "paused: LinkedIn said stop" and "Paused until" in text and started == []
    res, text = run(home, dry_run=True, fetch=False)          # a dry run opens real profiles too: the pause holds for it
    assert res.stop_reason == "paused: LinkedIn said stop" and "Paused until" in text and started == []
    res, text = run(home, ignore_pause=True)
    assert res.stop_reason == "no browser" and started == [1]                         # got past the pause
    res, text = run(home, dry_run=True, fetch=False, ignore_pause=True)
    assert res.stop_reason == "no browser" and started == [1, 1]


def test_notes_known_unavailable_with_policy_stop_stops_before_the_browser(home, no_browser):
    add_people(home, [person("a")])
    with Store(home.db_path) as st:
        st.kv_set("note_blocked_until", (datetime.now() + timedelta(days=3)).isoformat(timespec="minutes"))
    res, text = run(home, make_cfg(message={"if_note_unavailable": "stop"}))
    assert res.stop_reason == "personal notes unavailable" and "if_note_unavailable" in text


# ----------------------------------------------------------------------------- scheduled runs
def test_a_scheduled_run_waits_a_random_time_first_and_can_be_stopped_while_waiting(home, no_browser, monkeypatch):
    add_people(home, [person("a")])
    slept: list[float] = []
    jittery = dataclasses.replace(FAST, start_jitter_minutes=10)
    logs: list[str] = []
    ro = RunOptions(headless=True, limits=jittery, scheduled=True, ignore_hours=True, seed=1, log=logs.append,
                    sleep=slept.append, stop=lambda: len(slept) >= 2)
    res = runner.run_send(home, make_cfg(), ro)
    assert res.stop_reason == "stopped" and len(slept) == 2 and all(0 < s <= 5 for s in slept)
    assert any("starting in" in m for m in logs)
    # a manual run never waits: it goes straight to the browser
    slept.clear()

    def none(*a, **k):
        raise BrowserUnavailable("x")
    manual = RunOptions(headless=True, limits=jittery, ignore_hours=True, seed=1, log=lambda m: None, sleep=slept.append)
    monkeypatch.setattr(runner, "launch_browser", none)
    res = runner.run_send(home, make_cfg(), manual)
    assert slept == [] and res.stop_reason == "no browser"


# ----------------------------------------------------------------------------- the pool of people
CSV = "Full Name,LinkedIn URL,Company,Title\nAnna Ray,https://www.linkedin.com/in/anna-ray/,Rayco,CEO\nBo Lee,https://www.linkedin.com/in/bo-lee/,Leeco,CTO\n"


def test_an_empty_pool_is_filled_from_the_csv_before_anything_is_sent(home, tmp_path, monkeypatch):
    path = tmp_path / "leads.csv"
    path.write_text(CSV, encoding="utf-8")
    cfg = make_cfg(sources=[{"type": "csv", "path": str(path)}])

    def none(*a, **k):
        raise BrowserUnavailable("stop here")
    monkeypatch.setattr(runner, "launch_browser", none)
    res, text = run(home, cfg)
    assert res.stop_reason == "no browser" and "2 people ready" in text
    with Store(home.db_path) as st:
        assert st.count_people() == 2 and st.count_people_by_source() == {"csv": 2}


def test_run_fetch_adds_people_once_and_releases_its_locks(home, tmp_path):
    path = tmp_path / "leads.csv"
    path.write_text(CSV, encoding="utf-8")
    cfg = make_cfg(sources=[{"type": "csv", "path": str(path)}])
    logs: list[str] = []
    res = runner.run_fetch(home, cfg, log=logs.append)
    assert res.exit_code == EXIT_OK and res.sent == 2 and "2 people in your list" in "\n".join(logs)
    assert runner.run_fetch(home, cfg, log=logs.append).sent == 0                      # nothing new the second time
    assert not RunLock(home.root / "fetch.lock").is_held() and not RunLock(home.lock_path).is_held()


def test_run_fetch_without_sources_explains_itself(home):
    logs: list[str] = []
    res = runner.run_fetch(home, make_cfg(), log=logs.append)
    assert res.stop_reason == "no sources" and "No sources" in "\n".join(logs)


def test_run_fetch_backs_off_when_another_fetch_is_running(home, tmp_path):
    path = tmp_path / "leads.csv"
    path.write_text(CSV, encoding="utf-8")
    cfg = make_cfg(sources=[{"type": "csv", "path": str(path)}])
    with RunLock(home.root / "fetch.lock"):
        res = runner.run_fetch(home, cfg, log=lambda m: None)
    assert res.exit_code == EXIT_BUSY


@deletes_an_open_file
def test_a_fetch_is_told_to_stop_when_its_lock_file_goes_away_or_the_person_says_stop(home, monkeypatch):
    seen = {}

    def fake_fetch_sources(home, cfg, store, **kw):
        stop = kw["stop"]
        seen["before"] = stop()
        (home.root / "fetch.lock").unlink()
        seen["after"] = stop()
        return []

    monkeypatch.setattr(runner, "fetch_sources", fake_fetch_sources)
    cfg = make_cfg(sources=[{"type": "csv", "path": "leads.csv"}])
    runner.run_fetch(home, cfg, log=lambda m: None)
    assert seen == {"before": False, "after": True}
    runner.run_fetch(home, cfg, log=lambda m: None, stop=lambda: True)
    assert seen["before"] is True


@deletes_an_open_file
def test_a_fetch_that_uses_the_browser_is_told_to_stop_when_the_run_lock_goes_away(home, monkeypatch):
    seen = {}

    def fake_fetch_sources(home, cfg, store, **kw):
        seen["before"] = kw["stop"]()
        home.lock_path.unlink()
        seen["after"] = kw["stop"]()
        return []

    monkeypatch.setattr(runner, "fetch_sources", fake_fetch_sources)
    runner.run_fetch(home, make_cfg(sources=[{"type": "linkedin_search", "queries": ["founder fintech"]}]), log=lambda m: None)
    assert seen == {"before": False, "after": True}


def test_a_fetch_in_a_folder_that_cannot_be_locked_says_so(home, monkeypatch):
    refuse_file_locks(monkeypatch)
    logs: list[str] = []
    res = runner.run_fetch(home, make_cfg(sources=[{"type": "csv", "path": "leads.csv"}]), log=logs.append)
    assert res.exit_code == EXIT_CONFIG and "cannot be locked" in res.stop_reason and "CONNECTO_HOME" in "\n".join(logs)
    res = runner.run_fetch(home, make_cfg(sources=[{"type": "linkedin_search", "queries": ["x"]}]), log=logs.append)
    assert res.exit_code == EXIT_CONFIG


def test_login_in_a_folder_that_cannot_be_locked_exits_with_a_settings_problem(home, monkeypatch):
    refuse_file_locks(monkeypatch)
    monkeypatch.setattr(runner, "launch_browser", lambda *a, **k: pytest.fail("no browser without the lock"))
    logs: list[str] = []
    assert runner.login(home, make_cfg(), log=logs.append) == EXIT_CONFIG and "CONNECTO_HOME" in "\n".join(logs)


def test_a_browser_source_needs_the_run_lock(home):
    cfg = make_cfg(sources=[{"type": "linkedin_search", "queries": ["founder fintech"]}])
    with RunLock(home.lock_path):
        res = runner.run_fetch(home, cfg, log=lambda m: None)
    assert res.exit_code == EXIT_BUSY and res.stop_reason == "another run in progress"


# ----------------------------------------------------------------------------- numbers for report / setup page
def test_status_report_shape_and_pause_fields(home):
    add_people(home, [person("a"), person("b"), person("c")])
    with Store(home.db_path) as st:
        st.record(person("a"), "sent", "hello", "note", "template")
        st.record(person("b"), "already_pending")
        runner.set_pause(st, timedelta(days=1), "check")
        st.kv_set("note_blocked_until", "2030-01-01T00:00")
        st.start_run("send")
    rep = runner.status_report(home, make_cfg())
    assert rep["sent_today"] == 1 and rep["total_sent"] == 1 and rep["people"] == 3
    assert rep["by_status"] == {"sent": 1, "already_pending": 1} and rep["people_by_source"] == {"manual": 3}
    assert rep["paused_reason"] == "check" and rep["paused_until"] and rep["note_blocked_until"] == "2030-01-01T00:00"
    assert rep["weekly_cap"] >= 1 and rep["daily_target"] >= 1 and rep["ai_calls_today"] == 0
    assert len(rep["recent_runs"]) == 1 and rep["recent_invites"][0]["status"] in ("sent", "already_pending")


def test_sleeping_in_slices_honours_stop(home):
    slept: list[float] = []
    ro = RunOptions(sleep=slept.append, stop=lambda: len(slept) >= 3)
    assert runner._sleep_interruptibly(ro, 100) is True and slept == [5.0, 5.0, 5.0]
    slept.clear()
    assert runner._sleep_interruptibly(RunOptions(sleep=slept.append), 12) is False and slept == [5.0, 5.0, 2.0]
