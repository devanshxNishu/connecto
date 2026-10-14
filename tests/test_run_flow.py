"""run_send end to end: the real runner and the real browser code against the mock LinkedIn (headless Chromium).

Nothing here touches LinkedIn. The waits are shrunk (``browser.PACE``) and the between-profile pauses are skipped
(``RunOptions.sleep``), but every decision - quotas, stop conditions, pauses, notes - is the production code path.
"""
from __future__ import annotations

import dataclasses
import time
from datetime import datetime

import pytest

from connecto import browser, runner
from connecto.runner import EXIT_NEEDS_YOU, EXIT_OK, RunOptions
from connecto.store import Store
from connecto.util import set_env_var

import fake_ai_server as fake
from engine_world import world  # noqa: F401  (a fixture, used by name)
from helpers import FAST, add_people, make_cfg, person

pytestmark = pytest.mark.browser


# ----------------------------------------------------------------------------- the happy path and every kind of profile
def test_run_sends_to_every_kind_of_profile_and_records_each_outcome(world):
    world.add("direct-connect", "paul-witten-39579715a", "pending-person", "connected-person", "follow-only",
              "how-know-person", "email-person", "no-such-profile")
    res = world.run()
    assert res.exit_code == EXIT_OK and res.stop_reason == "finished queue", world.log_text()
    assert world.statuses() == {
        "direct-connect": "sent", "paul-witten-39579715a": "sent", "pending-person": "already_pending",
        "connected-person": "already_connected", "follow-only": "no_connect_option", "how-know-person": "sent",
        "email-person": "needs_email", "no-such-profile": "not_found"}
    assert res.sent == 3 and res.attempted == 8 and res.outcomes["sent"] == 3
    assert sorted(world.sent_names()) == ["Dana Direct", "Howie Know", "Paul Witten"]
    for sent in world.mock.sent:                                            # a personal note, from the template, per person
        first = sent["name"].split()[0]
        assert sent["note"].startswith(f"Hi {first}, I'm Asha (a product designer). Would love to connect"), sent
        assert len(sent["note"]) <= 300
    with Store(world.home.db_path) as st:                                   # the run was recorded
        run = st.recent_runs(1)[0]
        assert (run["mode"], run["sent"], run["attempted"], run["stop_reason"]) == ("send", 3, 8, "finished queue")
        assert st.sent_today() == 3


def test_a_second_run_never_contacts_the_same_people_again(world):
    world.add("direct-connect", "direct-two")
    assert world.run().sent == 2
    again = world.run()
    assert again.sent == 0 and again.stop_reason == "empty queue"
    assert len(world.mock.sent) == 2


def test_notes_off_sends_plain_invitations(world):
    world.add("direct-connect", "direct-two")
    cfg = make_cfg(message={"send_note": False})
    res = world.run(cfg)
    assert res.sent == 2 and [s["note"] for s in world.mock.sent] == ["", ""]
    with Store(world.home.db_path) as st:
        assert {i["note_kind"] for i in st.recent_invites(10)} == {"plain"}


def test_a_template_that_cannot_be_filled_falls_back_to_a_plain_invitation(world):
    world.add("direct-connect")
    cfg = make_cfg(message={"templates": ["Hi {first_name}, about {nothing_like_this}"]})
    res = world.run(cfg)
    assert res.sent == 1 and world.mock.sent[0]["note"] == ""


# ----------------------------------------------------------------------------- who is opened: only the person on the list
def test_a_stored_look_alike_address_is_never_opened_and_a_skipped_person_stays_skipped(world):
    """The reviewers' attack: `/in/.\\direct-two/` is a different *key* but the browser opens `/in/direct-two/`."""
    from connecto.manual import skip_urls
    world.add("direct-connect", "direct-two")
    with Store(world.home.db_path) as st:
        assert skip_urls(st, ["https://www.linkedin.com/in/direct-two/"])[0]
        for alias in ("https://www.linkedin.com/in/.\\direct-two/", "https://www.linkedin.com/in/x\\..\\direct-two\\",
                      "https://www.linkedin.com/in/%2e%2e/direct-two/"):         # rows an older version could have stored
            st.conn.execute("INSERT INTO people(key, linkedin_url, full_name, source_type, source_id, data, added_at, updated_at)"
                            " VALUES(?,?,?,?,?,?,?,?)", (alias.lower(), alias, "Dee Two", "manual", "manual",
                                                         '{"full_name": "Dee Two", "priority": "5"}', "2026-01-01", "2026-01-01"))
        st.conn.commit()
    res = world.run(dry_run=True)
    assert res.attempted == 1 and world.mock.visits == ["/in/direct-connect/"], world.log_text()      # nobody else was opened
    assert world.statuses() == {"direct-connect": "dry_run", "direct-two": "skipped"}


def test_a_redirect_to_something_that_is_not_a_profile_is_refused_before_anything_is_clicked(world):
    world.add("leads-to-feed", "direct-connect")
    res = world.run()
    assert world.statuses() == {"leads-to-feed": "error", "direct-connect": "sent"} and res.sent == 1, world.log_text()
    assert "not a profile" in world.log_text() and world.sent_names() == ["Dana Direct"]


def test_a_changed_address_may_lead_to_someone_new_but_never_to_somebody_you_skipped(world):
    from connecto.manual import skip_urls
    world.add("moved-person", "moved-fresh")
    with Store(world.home.db_path) as st:
        skip_urls(st, ["https://www.linkedin.com/in/direct-two/"])                # the person "moved-person" now leads to
    res = world.run()
    assert world.statuses()["moved-person"] == "error" and "different person" in world.log_text()
    assert world.statuses()["moved-fresh"] == "sent" and world.sent_names() == ["Tri Direct"] and res.sent == 1


# ----------------------------------------------------------------------------- quotas
def test_the_per_run_limit_is_respected(world):
    world.add("direct-connect", "direct-two", "direct-three")
    res = world.run(limit=2)
    assert res.sent == 2 and res.stop_reason == "quota for this run reached"
    assert world.statuses() == {"direct-connect": "sent", "direct-two": "sent"}       # the third waits for the next run
    assert world.run(limit=2).sent == 1 and sorted(world.sent_names()) == ["Dana Direct", "Dee Two", "Tri Direct"]


def test_the_daily_target_stops_a_run_before_it_opens_the_browser(world, monkeypatch):
    world.add("direct-connect")
    tiny = dataclasses.replace(FAST, daily=(1, 1), weekly=100, per_run=(5, 5))
    with Store(world.home.db_path) as st:
        st.record(person("someone-earlier"), "sent")
    monkeypatch.setattr(runner, "launch_browser", lambda *a, **k: pytest.fail("no browser should start"))
    res = world.run(limits=tiny)
    assert res.sent == 0 and "daily target reached" in res.stop_reason and world.mock.sent == []


def test_the_weekly_cap_stops_a_run(world, monkeypatch):
    world.add("direct-connect")
    capped = dataclasses.replace(FAST, weekly=2)
    with Store(world.home.db_path) as st:
        st.record(person("a-earlier"), "sent")
        st.record(person("b-earlier"), "sent")
    monkeypatch.setattr(runner, "launch_browser", lambda *a, **k: pytest.fail("no browser should start"))
    res = world.run(limits=capped)
    assert "weekly cap reached" in res.stop_reason


# ----------------------------------------------------------------------------- dry run
def test_dry_run_sends_nothing_and_leaves_everyone_in_the_queue(world):
    world.add("direct-connect", "paul-witten-39579715a", "pending-person")
    res = world.run(dry_run=True)
    assert res.exit_code == EXIT_OK and world.mock.sent == []
    assert world.statuses() == {"direct-connect": "dry_run", "paul-witten-39579715a": "dry_run", "pending-person": "already_pending"}
    assert "note (" in world.log_text()                                      # it shows what would have been said
    assert world.kv("paused_until") is None
    with Store(world.home.db_path) as st:
        assert st.sent_today() == 0 and st.recent_runs(1)[0]["mode"] == "dry-run"
    real = world.run()                                                       # a dry run does not use anyone up
    assert real.sent == 2 and sorted(world.sent_names()) == ["Dana Direct", "Paul Witten"]


# ----------------------------------------------------------------------------- the things that must stop a run
def test_linkedins_limit_dialog_stops_the_run_and_pauses_the_next_ones(world):
    world.add("direct-connect", "limit-person", "direct-two", "direct-three")
    res = world.run()
    assert res.sent == 1 and res.attempted == 2 and "invitation limit" in res.stop_reason
    assert world.statuses() == {"direct-connect": "sent", "limit-person": "limit_reached"}
    assert world.kv("paused_until")
    assert datetime.fromisoformat(world.kv("paused_until")) > datetime.now()
    # the next run (a scheduled one, say) does not even open a browser while paused
    world.logs.clear()
    again = world.run()
    assert again.sent == 0 and again.stop_reason.startswith("paused") and "Paused until" in world.log_text()
    assert len(world.mock.sent) == 1
    # a person who hit the limit is still wanted afterwards
    assert world.run(ignore_pause=True, limit=1).attempted >= 1


def test_a_security_check_stops_everything_and_asks_for_you(world):
    world.add("direct-connect", "checkpoint-person", "direct-two")
    res = world.run()
    assert res.exit_code == EXIT_NEEDS_YOU and res.stop_reason.startswith("needs you"), world.log_text()
    assert world.sent_names() == ["Dana Direct"] and "direct-two" not in world.statuses()
    assert world.kv("paused_until") and "security check" in (world.kv("paused_reason") or "").lower()


def test_logged_out_stops_the_run_without_touching_anyone(world, monkeypatch):
    world.mock.logged_in = False
    real = browser.ensure_logged_in
    monkeypatch.setattr(runner, "ensure_logged_in", lambda page, base_url, wait_minutes=10, **kw: real(page, base_url, wait_minutes=0.01, **kw))
    world.add("direct-connect")
    res = world.run()
    assert res.exit_code == EXIT_NEEDS_YOU and res.stop_reason.startswith("not logged in") and world.mock.sent == []
    assert world.statuses() == {} and "connecto login" in world.log_text()


def test_a_send_hands_the_stop_button_to_the_wait_for_you_to_sign_in(world, monkeypatch):
    seen = {}

    def waiting(page, base_url, wait_minutes=10, stop=None):
        seen["stop"] = stop
        raise browser.LoggedOut("not signed in (stand-in)")

    monkeypatch.setattr(runner, "ensure_logged_in", waiting)
    world.add("direct-connect")
    pressed = []
    res = world.run(stop=lambda: bool(pressed))
    assert res.exit_code == EXIT_NEEDS_YOU and res.stop_reason.startswith("not logged in") and world.mock.sent == []
    assert callable(seen["stop"]) and seen["stop"]() is False
    pressed.append("Stop")
    assert seen["stop"]() is True                                                  # the button reaches the waiting loop


def test_consecutive_errors_stop_the_run(world, monkeypatch):
    world.add("direct-connect", "direct-two", "direct-three", "paul-witten-39579715a")

    def broken(*a, **k):
        raise RuntimeError("LinkedIn changed everything")

    monkeypatch.setattr(runner, "process_profile", broken)
    res = world.run()
    assert "errors in a row" in res.stop_reason and res.attempted == FAST.stop_after_errors
    assert set(world.statuses().values()) == {"error"} and world.mock.sent == []
    assert "paul-witten-39579715a" not in world.statuses()


def test_an_error_between_successes_does_not_stop_the_run(world, monkeypatch):
    world.add("direct-connect", "direct-two", "direct-three")
    real = runner.process_profile
    calls = []

    def flaky(page, url, *a, **k):
        calls.append(url)
        if len(calls) == 2:
            raise RuntimeError("one-off timeout")
        return real(page, url, *a, **k)

    monkeypatch.setattr(runner, "process_profile", flaky)
    res = world.run()
    assert res.sent == 2 and res.stop_reason == "finished queue"
    assert world.statuses()["direct-two"] == "error"
    assert world.run(retry_errors=True).sent == 1                                  # errors are retried only when asked


def test_the_stop_button_stops_between_profiles(world):
    world.add("direct-connect", "direct-two", "direct-three")
    flag = {"stop": False}

    def log(msg):
        world.logs.append(msg)
        if "-> sent" in msg:
            flag["stop"] = True

    ro = RunOptions(headless=True, base_url=world.mock.base_url, sleep=lambda s: None, limits=FAST, log=log,
                    ignore_hours=True, seed=1, stop=lambda: flag["stop"])
    res = runner.run_send(world.home, make_cfg(), ro)
    assert res.stop_reason == "stopped" and res.sent == 1 and len(world.mock.sent) == 1


# ----------------------------------------------------------------------------- free-plan personal-note quota
def test_when_linkedin_will_not_take_a_note_the_run_sends_plain_and_remembers(world):
    world.add("note-blocked", "direct-connect", "direct-two")
    res = world.run(make_cfg(message={"plan": "free"}))
    assert res.sent == 3, world.log_text()
    assert [s["note"] for s in world.mock.sent] == ["", "", ""]                      # the first plain because blocked, then no tries
    until = world.kv("note_blocked_until")
    assert until and datetime.fromisoformat(until) > datetime.now()
    assert "Sending plain invitations" in world.log_text()
    # while blocked a later run sends plain invitations straight away
    world.add("direct-three")
    assert world.run().sent == 1 and world.mock.sent[-1]["note"] == ""


def test_policy_stop_leaves_the_person_in_the_queue_when_a_note_cannot_be_added(world):
    world.add("note-blocked", "direct-connect")
    cfg = make_cfg(message={"plan": "free", "if_note_unavailable": "stop"})
    res = world.run(cfg)
    assert res.sent == 0 and "not allowing personal notes" in res.stop_reason and world.mock.sent == []
    assert world.statuses() == {"note-blocked": "note_unavailable"}
    assert world.kv("note_blocked_until")
    again = world.run(cfg)                                                           # known blocked: no browser, still waiting
    assert again.stop_reason == "personal notes unavailable" and world.mock.sent == []


# ----------------------------------------------------------------------------- the optional free-AI opener, in a real run
def test_ai_opener_is_used_when_available_and_the_run_survives_when_it_is_not(world, tmp_path, clean_env):
    srv = fake.FakeAIServer().start()
    try:
        set_env_var(world.home.env_path, "CONNECTO_TEST_KEY", "k-test-123456")        # the run picks it up from the .env file
        ai = {"enabled": True, "provider": "custom", "base_url": srv.base_url, "model": "test-model",
              "api_key_env": "CONNECTO_TEST_KEY", "max_calls_per_day": 50}
        templates = ["Hi {first_name}, {hook} I'm {me_first_name}, {me_about} - would love to connect.",
                     "Hi {first_name}, I'm {me_first_name}, {me_about}. Would love to connect."]
        cfg = make_cfg(ai=ai, message={"templates": templates})
        world.add("direct-connect", company="Acme Pay", one_liner="Payments infrastructure for small businesses")
        res = world.run(cfg)
        assert res.sent == 1 and fake.DEFAULT_HOOK in world.mock.sent[0]["note"], world.log_text()
        assert "AI helper:" in world.log_text()
        prompt = srv.chat_requests()[0].messages_text()
        assert "Dana" not in prompt and "linkedin.com" not in prompt                  # nothing personal left the machine
        with Store(world.home.db_path) as st:
            assert st.recent_invites(1)[0]["note_kind"] == "ai" and st.ai_calls_today() == 1

        srv.default_chat = fake.unauthorized()                                         # the key stops working: still sends
        world.add("direct-two", company="Other Pay", one_liner="Billing software")
        world.logs.clear()
        res = world.run(cfg)
        assert res.sent == 1 and world.mock.sent[-1]["note"].startswith("Hi Dee, I'm Asha")
        assert fake.DEFAULT_HOOK not in world.mock.sent[-1]["note"]

        before = len(srv.chat_requests())                                              # --no-ai never calls the model
        world.add("direct-three", company="Third Pay", one_liner="Invoicing")
        assert world.run(cfg, use_ai=False).sent == 1 and len(srv.chat_requests()) == before
    finally:
        srv.stop()


# ----------------------------------------------------------------------------- login
def test_signing_in_lifts_only_the_pause_that_a_security_check_caused(world):
    """The person has just dealt with the check, so that pause goes. A limit's, repeated errors' or their own pause is
    not about being signed in and stays - with a line saying so."""
    with Store(world.home.db_path) as st:
        runner.set_pause(st, runner.PAUSE_AFTER_ATTENTION, "LinkedIn asked for a security check", runner.PAUSE_CHECK)
    logs = []
    code = runner.login(world.home, make_cfg(), base_url=world.mock.base_url, log=logs.append, headless=True)
    text = "\n".join(logs)
    assert code == EXIT_OK and "Signed in" in text and world.kv("paused_until") is None
    assert "security check caused" in text and "still paused" not in text
    assert world.kv("paused_kind") is None and world.kv("paused_reason") is None

    for kind, delta in ((runner.PAUSE_LIMIT, runner.PAUSE_AFTER_LIMIT), (runner.PAUSE_ERRORS, runner.PAUSE_AFTER_ERRORS),
                        (runner.PAUSE_USER, runner.PAUSE_AFTER_LIMIT)):
        with Store(world.home.db_path) as st:
            runner.set_pause(st, delta, f"why-{kind}", kind)
        logs.clear()
        assert runner.login(world.home, make_cfg(), base_url=world.mock.base_url, log=logs.append, headless=True) == EXIT_OK
        assert world.kv("paused_until") and world.kv("paused_reason") == f"why-{kind}", kind
        assert "still paused" in "\n".join(logs) and f"why-{kind}" in "\n".join(logs) and "connecto resume" in "\n".join(logs)


def test_a_pause_with_no_recorded_kind_is_treated_as_the_persons_own(world):
    """A pause written by an older version has no kind; the cautious reading is that it is not the check's."""
    with Store(world.home.db_path) as st:
        st.kv_set("paused_until", (datetime.now() + runner.PAUSE_AFTER_LIMIT).isoformat(timespec="minutes"))
        st.kv_set("paused_reason", "older version")
    assert runner.login(world.home, make_cfg(), base_url=world.mock.base_url, log=lambda m: None, headless=True) == EXIT_OK
    assert world.kv("paused_until")


def test_the_stop_button_ends_the_wait_for_you_to_sign_in(world):
    world.mock.logged_in = False
    asked, logs = [], []

    def stop():
        asked.append(1)
        return len(asked) >= 2                                   # the first look comes before any waiting: it must not end it

    began = time.monotonic()
    code = runner.login(world.home, make_cfg(), base_url=world.mock.base_url, log=logs.append, headless=True,
                        wait_minutes=0.5, stop=stop)
    assert code == EXIT_NEEDS_YOU and "Not signed in: Sign-in was stopped." in "\n".join(logs)
    assert len(asked) == 2 and time.monotonic() - began < 25                      # (not the half minute it was allowed to wait)


def test_signing_in_by_hand_still_works_when_a_stop_button_is_given_and_not_pressed(world):
    logs = []
    code = runner.login(world.home, make_cfg(), base_url=world.mock.base_url, log=logs.append, headless=True,
                        stop=lambda: False)
    assert code == EXIT_OK and "Signed in" in "\n".join(logs)


def test_login_reports_when_nobody_signed_in(world):
    world.mock.logged_in = False
    logs = []
    code = runner.login(world.home, make_cfg(), base_url=world.mock.base_url, log=logs.append, headless=True,
                        wait_minutes=0.01)
    assert code == EXIT_NEEDS_YOU and "Not signed in" in "\n".join(logs)
