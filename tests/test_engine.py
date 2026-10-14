"""The send engine's safety rules, one behaviour per test, against the mock LinkedIn in a real (headless) Chromium.

Every test here is about something that must *not* happen: an invitation that is sent but not counted, a click on the
wrong person or the wrong dialog, a run that keeps going after LinkedIn (or the person) said stop. The mock never
resembles the live site exactly (see ``mock_linkedin``); what is checked is that Connecto's own decisions are right
whatever the page does.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta

import pytest

from connecto import connect, runner
from connecto.browser import BrowserUnavailable  # noqa: F401  (documented here: the other way a run can end early)
from connecto.limits import HARD, Limits, visit_cap
from connecto.runner import EXIT_CONFIG, EXIT_NEEDS_YOU, EXIT_OK
from connecto.store import Store

from engine_world import world  # noqa: F401  (a fixture, used by name)
from helpers import FAST, make_cfg, person

pytestmark = pytest.mark.browser


def sent_count(world) -> int:
    with Store(world.home.db_path) as st:
        return st.sent_today()


# ============================================================================= what is previewed is what is sent
def test_the_notes_the_preview_shows_are_the_notes_the_run_sends(world):
    from connecto import planner
    templates = ["Hi {first_name}, I'm {me_first_name}, {me_about}. Would love to connect.",
                 "Hello {first_name} - {me_first_name} here ({me_about}). Let's connect.",
                 "{first_name}, I'm {me_first_name} - {me_about}. Happy to be in touch."]
    cfg = make_cfg(message={"templates": templates})
    slugs = [f"dir-{i}" for i in range(1, 9)]
    world.add(*slugs)
    with Store(world.home.db_path) as st:
        preview = planner.build_preview(cfg, st, sample=10, example_notes=10)
    expected = {ex["person"]: ex["note"] for ex in preview["example_notes"]}
    assert len(expected) == len(slugs) and len(set(expected.values())) > 1
    res = world.run(cfg, seed=987654)                                   # (a different random source than the preview's)
    assert res.sent == len(slugs)
    for slug in slugs:
        row = world.invite(slug)
        assert row["note"] == expected[row["full_name"]], slug
    assert sorted(s["note"] for s in world.mock.sent) == sorted(expected.values())


# ============================================================================= waiting, pausing, stopping
def test_a_pause_set_from_another_window_while_the_run_waits_stops_it_before_the_next_profile(world):
    world.add("dir-1", "dir-2", "dir-3")

    def sleeper(seconds):
        world.sleeps.append(seconds)
        with Store(world.home.db_path) as st:                        # "Pause" pressed in the setup page, mid-wait
            runner.set_pause(st, timedelta(days=1), "paused in another window", runner.PAUSE_USER)

    res = world.run(sleep=sleeper)
    assert res.stop_reason == "paused: paused in another window" and res.sent == 1 and res.attempted == 1
    assert world.mock.visits == ["/in/dir-1/"] and world.statuses() == {"dir-1": "sent"}


def test_a_pause_that_arrives_during_a_scheduled_runs_start_delay_is_honoured(world, monkeypatch):
    world.add("dir-1")
    jittery = dataclasses.replace(FAST, start_jitter_minutes=5)
    monkeypatch.setattr(runner, "launch_browser", lambda *a, **k: pytest.fail("the browser must not even start while paused"))

    def sleeper(seconds):
        world.sleeps.append(seconds)
        with Store(world.home.db_path) as st:
            runner.set_pause(st, timedelta(days=1), "set while waiting", runner.PAUSE_USER)

    res = world.run(limits=jittery, scheduled=True, sleep=sleeper)
    assert res.stop_reason == "paused: set while waiting" and world.mock.visits == [] and world.mock.sent == []


def test_a_scheduled_runs_start_delay_cannot_carry_it_past_the_end_of_the_active_hours(world, monkeypatch):
    world.add("dir-1")
    window = {"open": True}
    monkeypatch.setattr(Limits, "can_send_now",
                        lambda self, now=None: (True, "") if window["open"] else (False, "outside active hours (09:00-20:30)"))

    def sleeper(seconds):
        world.sleeps.append(seconds)
        window["open"] = False

    res = world.run(limits=dataclasses.replace(FAST, start_jitter_minutes=5), scheduled=True, ignore_hours=False, sleep=sleeper)
    assert res.stop_reason.startswith("outside active time") and world.mock.visits == [] and world.mock.sent == []


def test_a_run_whose_lock_file_is_deleted_during_a_wait_stops_before_the_next_profile(world):
    """Somebody tidies the data folder mid-run: another run could start the moment the file is gone, so this one stops."""
    world.add("dir-1", "dir-2", "dir-3")

    def tidy_up(seconds):
        world.sleeps.append(seconds)
        world.home.lock_path.unlink(missing_ok=True)

    res = world.run(sleep=tidy_up)
    assert res.stop_reason == runner.LOCK_LOST and res.exit_code == EXIT_NEEDS_YOU
    assert res.sent == 1 and res.attempted == 1 and world.mock.visits == ["/in/dir-1/"] and len(world.sleeps) == 1


def test_a_lock_lost_before_the_first_profile_opens_nobody(world, monkeypatch):
    world.add("dir-1", "dir-2")
    real_launch = runner.launch_browser

    def launch_then_somebody_tidies_up(*args, **kwargs):
        opened = real_launch(*args, **kwargs)
        world.home.lock_path.unlink(missing_ok=True)                  # during the start-up, before any profile
        return opened

    monkeypatch.setattr(runner, "launch_browser", launch_then_somebody_tidies_up)
    res = world.run()
    assert res.stop_reason == runner.LOCK_LOST and res.exit_code == EXIT_NEEDS_YOU and res.attempted == 0
    assert world.mock.visits == [] and world.mock.sent == [] and world.statuses() == {}


def test_a_run_whose_lock_file_is_replaced_during_a_wait_stops_too(world):
    world.add("dir-1", "dir-2")

    def swap(seconds):
        world.sleeps.append(seconds)
        other = world.home.root / "run.lock.new"
        other.write_text(world.home.lock_path.read_text())           # even a copy with the same text is another file
        other.replace(world.home.lock_path)

    res = world.run(sleep=swap)
    assert res.stop_reason == runner.LOCK_LOST and res.exit_code == EXIT_NEEDS_YOU and world.mock.visits == ["/in/dir-1/"]


def test_a_scheduled_runs_start_delay_ends_when_the_lock_is_lost(world):
    world.add("dir-1")

    def tidy_up(seconds):
        world.sleeps.append(seconds)
        world.home.lock_path.unlink(missing_ok=True)

    res = world.run(limits=dataclasses.replace(FAST, start_jitter_minutes=5), scheduled=True, sleep=tidy_up)
    assert res.stop_reason == runner.LOCK_LOST and res.exit_code == EXIT_NEEDS_YOU
    assert world.mock.visits == [] and world.mock.sent == []


def test_a_data_folder_that_cannot_be_locked_means_no_run_and_a_clear_message(world, monkeypatch):
    import errno
    from connecto import lock as lock_module
    world.add("dir-1")
    monkeypatch.setattr(lock_module.fcntl, "flock", lambda fd, how: (_ for _ in ()).throw(OSError(errno.ENOLCK, "No locks available")))
    res = world.run()
    assert res.exit_code == EXIT_CONFIG and "cannot be locked" in res.stop_reason
    assert "CONNECTO_HOME" in world.log_text() and world.mock.visits == []


def test_the_stop_button_pressed_before_the_first_profile_opens_nobody(world):
    world.add("dir-1", "dir-2")
    res = world.run(stop=lambda: True)
    assert res.stop_reason == "stopped" and res.attempted == 0 and world.mock.visits == [] and world.statuses() == {}


def test_the_active_hours_window_closing_during_a_wait_ends_the_run(world, monkeypatch):
    world.add("dir-1", "dir-2", "dir-3")
    window = {"open": True}

    def can_send_now(self, now=None):
        return (True, "") if window["open"] else (False, "outside active hours (09:00-20:30)")

    monkeypatch.setattr(Limits, "can_send_now", can_send_now)

    def sleeper(seconds):
        world.sleeps.append(seconds)
        window["open"] = False                                        # the clock passes 20:30 while we wait

    res = world.run(ignore_hours=False, sleep=sleeper)
    assert res.stop_reason == "active-hours window closed" and res.sent == 1 and world.mock.visits == ["/in/dir-1/"]


def test_the_stop_button_pressed_during_the_wait_is_honoured_before_the_next_profile(world):
    world.add("dir-1", "dir-2")
    flag = {"stop": False}

    def sleeper(seconds):
        world.sleeps.append(seconds)
        flag["stop"] = len(world.sleeps) >= 2

    res = world.run(sleep=sleeper, stop=lambda: flag["stop"])
    assert res.stop_reason == "stopped" and res.sent == 1 and world.mock.visits == ["/in/dir-1/"]


def test_the_pause_between_profiles_is_never_shorter_than_the_ceiling_even_in_a_dry_run(world):
    world.add("dir-1", "dir-2", "dir-3")
    res = world.run(dry_run=True)                                    # FAST asks for no pause at all
    assert res.attempted == 3 and world.mock.sent == []
    assert sum(world.sleeps) >= 2 * HARD["delay_min_seconds"], world.sleeps


def test_a_pause_stops_a_dry_run_too(world):
    world.add("dir-1")
    with Store(world.home.db_path) as st:
        runner.set_pause(st, timedelta(days=1), "LinkedIn said stop", runner.PAUSE_LIMIT)
    res = world.run(dry_run=True)
    assert res.stop_reason == "paused: LinkedIn said stop" and world.mock.visits == []
    assert world.run(dry_run=True, ignore_pause=True).attempted == 1


# ============================================================================= how much one run may look at
def test_a_run_that_keeps_finding_nobody_to_invite_stops_instead_of_browsing_the_list(world):
    n = HARD["idle_stop_max"]
    world.add(*[f"pend-{i}" for i in range(n + 4)], "dir-1")
    res = world.run()
    assert res.attempted == n and res.sent == 0 and res.exit_code == EXIT_OK
    assert "without an invitation to send" in res.stop_reason
    assert "dir-1" not in world.statuses() and world.mock.visits[-1] == f"/in/pend-{n - 1}/"
    assert world.kv("paused_until") is None                           # nothing is wrong: the next run simply carries on


def test_an_invitation_between_the_idle_profiles_resets_the_count(world):
    n = HARD["idle_stop_max"]
    world.add(*[f"pend-{i}" for i in range(n - 1)], "dir-1", *[f"pend-{i}" for i in range(n, 2 * n - 1)], "dir-2")
    res = world.run()
    assert res.sent == 2 and res.stop_reason == "finished queue" and res.attempted == 2 * n


def test_a_run_never_opens_more_profiles_than_its_size_allows(world):
    n = HARD["idle_stop_max"]
    cap = visit_cap(3)
    assert cap == 12 < 2 * n
    names = []
    for block in range(3):                                            # seven idle profiles, then one invitation, three times
        names += [f"pend-{block * 10 + i}" for i in range(n - 1)] + [f"dir-{block + 1}"]
    world.add(*names)
    res = world.run(limit=3)
    assert res.attempted == cap and res.sent == 1 and "profiles opened this run" in res.stop_reason, world.log_text()
    assert len(world.mock.visits) == cap


def test_profiles_without_a_connect_button_count_as_trouble_and_stop_the_run(world):
    world.add("fol-1", "fol-2", "fol-3", "fol-4", "dir-1")
    res = world.run()
    assert res.exit_code == EXIT_NEEDS_YOU and "errors in a row" in res.stop_reason and res.attempted == FAST.stop_after_errors
    assert world.statuses() == {"fol-1": "no_connect_option", "fol-2": "no_connect_option", "fol-3": "no_connect_option"}
    assert world.kv("paused_kind") == runner.PAUSE_ERRORS and world.mock.sent == []


def test_people_with_no_connect_option_are_tried_again_only_when_asked(world):
    world.add("fol-1", "fol-2", "dir-1")
    assert world.run().sent == 1
    assert world.statuses()["fol-1"] == "no_connect_option"
    world.mock.visits.clear()
    again = world.run()
    assert again.stop_reason == "empty queue" and world.mock.visits == []                 # LinkedIn's page may have changed, not them
    retry = world.run(retry_errors=True)
    assert retry.attempted == 2 and sorted(world.mock.visits) == ["/in/fol-1/", "/in/fol-2/"]


def test_a_dry_run_that_meets_repeated_errors_stops_but_sets_no_pause(world):
    world.add("fol-1", "fol-2", "fol-3", "dir-1")
    res = world.run(dry_run=True)
    assert "errors in a row" in res.stop_reason and res.exit_code == EXIT_NEEDS_YOU
    assert world.kv("paused_until") is None


def test_the_per_run_limit_is_clamped_to_the_ceiling_and_nonsense_is_refused(world):
    world.add(*[f"dir-{i}" for i in range(1, 26)])
    res = world.run(limit=500)
    assert res.sent == HARD["per_run_max"] and "lowered to" in world.log_text()
    world.logs.clear()
    bad = world.run(limit=-3)
    assert bad.exit_code == EXIT_CONFIG and bad.stop_reason == "bad limit" and "whole number" in world.log_text()
    assert len(world.mock.sent) == HARD["per_run_max"]


# ============================================================================= the browser window going away
def _closing_browser(monkeypatch, after_profiles: int):
    """Wrap launch_browser so a test can close the whole browser (a person closing the window) at a chosen moment."""
    holder = {}
    real = runner.launch_browser

    def launch(*a, **k):
        pw, ctx, page = real(*a, **k)
        holder["ctx"] = ctx
        return pw, ctx, page

    monkeypatch.setattr(runner, "launch_browser", launch)
    return holder


def test_closing_the_browser_window_ends_the_run_without_blaming_the_next_person(world, monkeypatch):
    world.add("dir-1", "dir-2", "dir-3")
    holder = _closing_browser(monkeypatch, 1)

    def sleeper(seconds):
        world.sleeps.append(seconds)
        holder["ctx"].close()                                          # the person closes the window while we wait

    res = world.run(sleep=sleeper)
    assert res.exit_code == EXIT_NEEDS_YOU and res.stop_reason == "the browser window was closed", world.log_text()
    assert world.statuses() == {"dir-1": "sent"}                       # dir-2 was never really tried: no error recorded for them
    assert res.attempted == 2 and world.mock.visits == ["/in/dir-1/"]


def test_the_feed_visit_between_profiles_notices_a_closed_window_too(world, monkeypatch):
    world.add(*[f"dir-{i}" for i in range(1, 13)])
    holder = _closing_browser(monkeypatch, 0)

    def scroll_then_gone(page, steps=None):
        holder["ctx"].close()
        raise RuntimeError("Target page, context or browser has been closed")

    monkeypatch.setattr(runner, "human_scroll", scroll_then_gone)
    res = world.run(limit=12)
    assert res.stop_reason == "the browser window was closed" and res.exit_code == EXIT_NEEDS_YOU, world.log_text()
    assert set(world.statuses().values()) == {"sent"}
    assert res.attempted == len(world.statuses())                    # noticed on the spot: nobody else was even started


def test_a_window_closed_after_connect_was_pressed_leaves_the_person_as_sending(world, monkeypatch):
    world.add("dir-1", "dir-2")
    holder = _closing_browser(monkeypatch, 0)
    real = connect.handle_dialog_and_send

    def close_then_continue(page, *a, **k):
        holder["ctx"].close()
        return real(page, *a, **k)

    monkeypatch.setattr(connect, "handle_dialog_and_send", close_then_continue)
    res = world.run()
    assert res.stop_reason == "the browser window was closed" and res.exit_code == EXIT_NEEDS_YOU
    assert world.statuses() == {"dir-1": "sending"} and sent_count(world) == 1            # unknown, so counted - and not retried


# ============================================================================= an invitation is recorded BEFORE it can go out
def test_the_person_is_recorded_as_sending_and_counted_before_linkedin_receives_the_invitation(world):
    slugs = ["dir-1", "inst-1", "no-add-note"]                     # a typed note, an instant send, a plain send
    world.add(*slugs)
    seen = []

    def on_sent(received):
        slug = slugs[len(seen)]
        row = world.invite(slug)
        seen.append((slug, row["status"], row["note"], received["note"], sent_count(world)))

    world.mock.on_sent = on_sent
    res = world.run()
    assert res.sent == 3 and [s for s, *_ in seen] == slugs
    for index, (slug, status, recorded_note, received_note, counted) in enumerate(seen, start=1):
        assert status == "sending", slug                              # at the moment the invitation reached LinkedIn
        assert recorded_note == received_note, slug                   # ... and the record already said what was really sent
        assert counted == index, slug                                 # ... and it was already counted
    assert seen[0][3] != "" and seen[1][3] == "" and seen[2][3] == ""
    assert world.statuses() == {"dir-1": "sent", "inst-1": "sent", "no-add-note": "sent"}


def test_an_interrupted_send_is_never_lost_ctrl_c_included(world, monkeypatch):
    world.add("dir-1", "dir-2")

    def interrupted(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(connect, "handle_dialog_and_send", interrupted)
    res = world.run()
    assert res.exit_code == 130 and res.stop_reason == "interrupted"
    assert world.statuses() == {"dir-1": "sending"} and sent_count(world) == 1


def test_an_exception_after_connect_was_pressed_is_recorded_as_sending_and_stops_the_run(world, monkeypatch):
    world.add("dir-1", "dir-2")

    def boom(*a, **k):
        raise RuntimeError("Playwright went away\nsecond line")

    monkeypatch.setattr(connect, "handle_dialog_and_send", boom)
    res = world.run()
    assert res.exit_code == EXIT_NEEDS_YOU and res.stop_reason.startswith("an invitation was sent but LinkedIn did not confirm")
    assert world.statuses() == {"dir-1": "sending"} and "interrupted after Connect/Send was pressed" in world.log_text()
    assert "second line" not in world.log_text()


def test_an_exception_before_any_click_is_an_ordinary_error_and_the_run_carries_on(world, monkeypatch):
    world.add("dir-1", "dir-2")
    real = connect.top_card
    calls = []

    def flaky(page):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("flaked")
        return real(page)

    monkeypatch.setattr(connect, "top_card", flaky)
    res = world.run()
    assert world.statuses() == {"dir-1": "error", "dir-2": "sent"} and res.sent == 1 and sent_count(world) == 1


# ============================================================================= "sent" needs proof
def test_a_send_that_linkedin_never_acknowledges_stops_the_run_and_still_counts(world):
    world.add("ghost-send", "dir-1")
    res = world.run()
    assert res.exit_code == EXIT_NEEDS_YOU and res.stop_reason.startswith("an invitation was sent but LinkedIn did not confirm")
    assert world.statuses() == {"ghost-send": "sending"} and res.sent == 0 and res.attempted == 1
    assert sent_count(world) == 1 and world.mock.sent == []
    assert "Look at 'Sent invitations'" in world.log_text() and "--retry-errors" in world.log_text()
    # the next run does not try that person again by itself ...
    later = world.run()
    assert later.sent == 1 and world.statuses() == {"ghost-send": "sending", "dir-1": "sent"}
    # ... and the count included them
    assert sent_count(world) == 2


def test_a_person_left_as_sending_is_tried_again_only_with_retry_errors(world):
    world.add("ghost-send")
    world.run()
    world.mock.visits.clear()
    assert world.run().stop_reason == "empty queue" and world.mock.visits == []
    again = world.run(retry_errors=True)
    assert world.mock.visits == ["/in/ghost-send/"] and again.stop_reason.startswith("an invitation was sent but")


def test_a_pending_header_on_a_later_visit_settles_a_sending_person(world):
    """If the invitation did go out, the next look at the profile says Pending and the record is corrected."""
    world.add("pend-1")
    with Store(world.home.db_path) as st:
        st.record({"linkedin_url": "https://www.linkedin.com/in/pend-1/", "full_name": "Pat Pending"}, "sending", "", "unconfirmed")
    assert world.run().stop_reason == "empty queue"
    res = world.run(retry_errors=True)
    assert world.statuses() == {"pend-1": "already_pending"} and res.sent == 0


def test_a_dialog_that_lingers_is_not_proof_but_a_slow_one_is_still_waited_for(world, monkeypatch):
    world.add("slow-close")
    monkeypatch.setattr(connect, "CONFIRM_TIMEOUT_MS", 400)
    res = world.run()
    assert world.statuses() == {"slow-close": "sending"} and res.exit_code == EXIT_NEEDS_YOU
    assert len(world.mock.sent) == 1                                      # it did go out - and was not sent a second time
    assert sent_count(world) == 1


def test_a_slow_dialog_is_confirmed_when_the_page_catches_up(world):
    world.add("slow-close")
    res = world.run()
    assert world.statuses() == {"slow-close": "sent"} and res.sent == 1 and len(world.mock.sent) == 1


def test_an_invitation_confirmed_only_by_linkedins_message_counts_as_sent(world):
    world.add("toast-only")
    res = world.run()
    assert world.statuses() == {"toast-only": "sent"} and res.sent == 1


def test_an_invitation_that_leaves_no_trace_on_the_page_is_not_called_sent(world):
    world.add("silent-send", "dir-1")
    res = world.run()
    assert world.statuses() == {"silent-send": "sending"} and res.exit_code == EXIT_NEEDS_YOU
    assert "no confirmation" in world.log_text() and len(world.mock.sent) == 1


def test_an_error_message_after_send_is_an_error_not_a_success_and_the_run_carries_on(world):
    world.add("toast-error", "dir-1")
    res = world.run()
    assert world.statuses() == {"toast-error": "error", "dir-1": "sent"} and world.mock.sent[0]["name"] == "Dana Direct 1"
    assert res.sent == 1 and "showed an error after Send" in world.log_text()


def test_a_limit_dialog_that_answers_send_pauses_everything_and_counts_nothing(world, monkeypatch):
    monkeypatch.setattr(connect, "CONFIRM_TIMEOUT_MS", 600)
    world.add("limit-after", "dir-1")
    res = world.run()
    assert world.statuses() == {"limit-after": "limit_reached"} and "invitation limit" in res.stop_reason
    assert world.kv("paused_kind") == runner.PAUSE_LIMIT and sent_count(world) == 0 and world.mock.sent == []


def test_a_limit_message_that_answers_send_does_the_same(world):
    world.add("limit-toast", "dir-1")
    res = world.run()
    assert world.statuses() == {"limit-toast": "limit_reached"} and "invitation limit" in res.stop_reason
    assert world.kv("paused_kind") == runner.PAUSE_LIMIT and sent_count(world) == 0 and world.mock.sent == []


def test_a_security_check_right_after_send_stops_the_run_and_the_person_stays_counted(world):
    world.add("check-after-send", "dir-1")
    res = world.run()
    assert res.exit_code == EXIT_NEEDS_YOU and res.stop_reason.startswith("needs you")
    assert world.statuses() == {"check-after-send": "sending"} and sent_count(world) == 1
    assert world.kv("paused_kind") == runner.PAUSE_CHECK and len(world.mock.sent) == 1


def test_being_signed_out_right_after_send_stops_the_run_and_the_person_stays_counted(world):
    world.add("logout-after", "dir-1")
    res = world.run()
    assert res.exit_code == EXIT_NEEDS_YOU and res.stop_reason.startswith("logged out")
    assert world.statuses() == {"logout-after": "sending"} and sent_count(world) == 1 and world.kv("paused_until") is None


def test_text_in_a_profile_never_stands_in_for_what_linkedin_says(world):
    """The bio talks about invitation limits and things going wrong; the invitation is still a normal one."""
    world.add("bio-trap")
    res = world.run()
    assert world.statuses() == {"bio-trap": "sent"} and res.sent == 1 and world.kv("paused_until") is None


def test_an_unrelated_widget_complaining_inside_the_page_does_not_turn_an_unknown_into_an_error(world):
    """No proof either way, and a "couldn't load posts" box inside the profile: still 'unknown', never 'failed'."""
    world.add("silent-widget", "dir-1")
    res = world.run()
    assert world.statuses() == {"silent-widget": "sending"} and res.exit_code == EXIT_NEEDS_YOU and sent_count(world) == 1


# ============================================================================= only the right person, only the right dialog
def test_a_block_with_its_own_connect_button_ahead_of_the_real_header_is_never_pressed(world):
    world.add("decoy-first", "dir-1")
    res = world.run()
    assert world.sent_names() == ["Dora Decoy", "Dana Direct 1"] and res.sent == 2
    assert "DECOY" not in [s["note"] for s in world.mock.sent]


def test_a_profile_whose_name_is_in_an_h1_works(world):
    world.add("h1-person")
    assert world.run().sent == 1 and world.sent_names() == ["Hana Heading"]


@pytest.mark.parametrize("slug", ["no-title", "wrong-title"])
def test_a_page_that_does_not_say_whose_it_is_is_never_clicked(world, slug):
    world.add(slug, "dir-1")
    res = world.run()
    assert world.statuses() == {slug: "error", "dir-1": "sent"} and world.sent_names() == ["Dana Direct 1"]
    assert "own header" in world.log_text() and res.sent == 1


def test_a_connect_control_that_names_someone_else_is_never_clicked(world):
    world.add("other-label", "dir-1")
    res = world.run()
    assert world.statuses() == {"other-label": "error", "dir-1": "sent"} and "names someone else" in world.log_text()
    assert world.sent_names() == ["Dana Direct 1"] and res.sent == 1


def test_a_dialog_that_is_not_the_invitation_dialog_is_closed_and_nothing_is_sent(world):
    world.add("foreign-dialog", "dir-1")
    res = world.run()
    assert world.statuses() == {"foreign-dialog": "error", "dir-1": "sent"} and "not the invitation dialog" in world.log_text()
    assert world.sent_names() == ["Dana Direct 1"] and res.sent == 1            # ("SHARED" would show up if its Send was pressed)


def test_a_how_do_you_know_step_that_leads_to_another_dialog_sends_nothing(world):
    world.add("howknow-foreign", "dir-1")
    res = world.run()
    assert world.statuses() == {"howknow-foreign": "error", "dir-1": "sent"} and "followed 'How do you know'" in world.log_text()
    assert "SHARED" not in [s["note"] for s in world.mock.sent] and res.sent == 1


def test_a_header_that_is_not_inside_a_section_is_not_guessed_at(world):
    world.add("no-section", "dir-1")
    res = world.run()
    assert world.statuses() == {"no-section": "error", "dir-1": "sent"} and "own header" in world.log_text() and res.sent == 1


def test_the_connect_button_of_a_suggested_person_is_never_pressed(world):
    """Every mock page carries a 'More profiles for you' block with its own Connect button."""
    world.add("fol-1", "pend-1", "conn-1")
    res = world.run()
    assert world.mock.sent == [] and res.sent == 0


def test_a_note_box_that_changes_what_was_typed_blocks_the_send(world):
    world.add("mangle-note", "dir-1")
    res = world.run()
    assert world.statuses() == {"mangle-note": "error", "dir-1": "sent"} and "did not hold the note that was approved" in world.log_text()
    assert world.sent_names() == ["Dana Direct 1"] and res.sent == 1


def test_a_renamed_add_note_button_sends_a_plain_invitation_by_default(world):
    world.add("no-add-note")
    res = world.run()
    assert world.statuses() == {"no-add-note": "sent"} and world.mock.sent == [{"name": "Nia Nonote", "note": ""}]
    assert world.kv("note_blocked_until") is None and res.sent == 1


def test_policy_stop_with_a_renamed_add_note_button_sends_nothing(world):
    world.add("no-add-note", "dir-1")
    res = world.run(make_cfg(message={"if_note_unavailable": "stop"}))
    assert world.statuses() == {"no-add-note": "note_unavailable"} and world.mock.sent == []
    assert "offers no way to add a note" in res.stop_reason and world.kv("note_blocked_until") is None
    assert sent_count(world) == 0


def test_a_changed_address_may_lead_to_an_errored_person_only_when_errors_are_being_retried(world):
    world.add("moved-person")
    with Store(world.home.db_path) as st:
        st.record(person("direct-two", "Dee Two"), "error", "", "earlier trouble")      # the person "moved-person" now leads to
    world.run(limit=1)
    assert world.statuses()["moved-person"] == "error" and "different person" in world.log_text() and world.mock.sent == []
    allowed = world.run(limit=1, retry_errors=True)
    assert world.sent_names() == ["Dee Two"] and allowed.sent == 1 and world.statuses()["moved-person"] == "sent"
