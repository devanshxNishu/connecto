"""What the store says about an invitation: which statuses are final, which count toward the limits, and that a record of
"sent" is never overwritten by anything a later visit found."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

from connecto import planner
from connecto.models import person_key
from connecto.store import COUNTED, RETRY_WITH_FLAG, RETRYABLE, STATUSES, TERMINAL, Store, waiting

from helpers import add_people, make_cfg, make_home, person


@pytest.fixture()
def store(tmp_path):
    with Store(tmp_path / "s.sqlite") as st:
        yield st


# ----------------------------------------------------------------------------- who is still waiting
def test_every_status_is_either_final_or_retryable_and_sending_is_known():
    assert set(STATUSES) == TERMINAL | RETRYABLE and not (TERMINAL & RETRYABLE)
    assert "sending" in TERMINAL and "sending" in RETRY_WITH_FLAG and "sending" in COUNTED and "sent" in COUNTED


@pytest.mark.parametrize("status,plain,retrying", [
    (None, True, True), ("limit_reached", True, True), ("dry_run", True, True), ("note_unavailable", True, True),
    ("error", False, True), ("no_connect_option", False, True), ("sending", False, True),
    ("sent", False, False), ("already_connected", False, False), ("already_pending", False, False),
    ("not_found", False, False), ("needs_email", False, False), ("skipped", False, False),
])
def test_who_is_still_waiting_to_be_invited(status, plain, retrying):
    assert waiting(status) is plain and waiting(status, retry_errors=True) is retrying


def test_the_queue_leaves_out_unconfirmed_and_unconnectable_people_unless_errors_are_retried(tmp_path):
    home = make_home(tmp_path)
    slugs = ["a-new", "b-error", "c-noconnect", "d-sending", "e-sent", "f-skipped", "g-limit"]
    add_people(home, [person(s) for s in slugs])
    with Store(home.db_path) as st:
        for slug, status in (("b-error", "error"), ("c-noconnect", "no_connect_option"), ("d-sending", "sending"),
                             ("e-sent", "sent"), ("f-skipped", "skipped"), ("g-limit", "limit_reached")):
            st.record(person(slug), status)
        names = lambda people: sorted(p["linkedin_url"].rstrip("/").rsplit("/", 1)[1] for p in people)   # noqa: E731
        cfg = make_cfg()
        assert names(planner.candidates(cfg, st)) == ["a-new", "g-limit"]
        assert names(planner.candidates(cfg, st, retry_errors=True)) == ["a-new", "b-error", "c-noconnect", "d-sending", "g-limit"]
        assert planner.queue_counts(cfg, st) == {"matching": 7, "queued": 2}
        assert planner.queue_counts(cfg, st, retry_errors=True) == {"matching": 7, "queued": 5}


# ----------------------------------------------------------------------------- "sent" is final
@pytest.mark.parametrize("later", ["error", "sending", "limit_reached", "dry_run", "already_pending", "already_connected", "skipped",
                                   "not_found", "no_connect_option", "needs_email", "note_unavailable"])
def test_a_recorded_send_is_never_overwritten(store, later):
    p = person("jane-doe")
    store.record(p, "sent", "Hello Jane", "note", "template")
    store.record(p, later, "other note", "other detail", "ai")
    row = store.recent_invites(1)[0]
    assert (row["status"], row["note"], row["detail"], row["note_kind"]) == ("sent", "Hello Jane", "note", "template")
    assert store.status_of(person_key(p)) == "sent"


def test_sent_can_be_recorded_again_to_correct_the_details_and_sending_becomes_sent(store):
    p = person("jane-doe")
    store.record(p, "sending", "typed note", "Connect/Send pressed")
    assert store.status_of(person_key(p)) == "sending"
    store.record(p, "sent", "typed note", "note", "template")
    store.record(p, "sent", "typed note", "confirmed later", "template")
    row = store.recent_invites(1)[0]
    assert row["status"] == "sent" and row["detail"] == "confirmed later"


@pytest.mark.parametrize("first", ["sending", "error", "limit_reached", "dry_run"])
def test_anything_short_of_sent_can_still_change(store, first):
    p = person("jane-doe")
    store.record(p, first)
    store.record(p, "already_pending", "", "found pending")
    assert store.status_of(person_key(p)) == "already_pending"


def test_forgetting_a_status_never_forgets_a_send(store):
    sent, unsure = person("sent-one"), person("unsure-one")
    store.record(sent, "sent")
    store.record(unsure, "sending")
    assert store.forget_status(person_key(sent)) is False and store.status_of(person_key(sent)) == "sent"
    assert store.forget_status(person_key(unsure)) is True and store.status_of(person_key(unsure)) is None


# ----------------------------------------------------------------------------- what counts toward the limits
def test_unconfirmed_invitations_count_toward_the_daily_and_weekly_numbers(store):
    for slug, status in (("a", "sent"), ("b", "sending"), ("c", "error"), ("d", "already_pending"), ("e", "dry_run"),
                         ("f", "limit_reached"), ("g", "skipped"), ("h", "no_connect_option")):
        store.record(person(slug), status)
    assert store.sent_today() == 2 and store.sent_last_7_days() == 2
    assert store.summary()["total_sent"] == 1 and store.summary()["by_status"]["sending"] == 1


def test_the_windows_are_a_calendar_day_and_seven_days(store):
    now = datetime(2026, 10, 7, 15, 0)
    for slug, status, when in (("today", "sent", now.replace(hour=1)), ("yesterday", "sending", now - timedelta(days=1)),
                               ("week-old", "sent", now - timedelta(days=6, hours=23)), ("too-old", "sent", now - timedelta(days=8))):
        store.record(person(slug), status)
        store.conn.execute("UPDATE invites SET updated_at=? WHERE key=?", (when.isoformat(timespec="seconds"), person_key(person(slug))))
    store.conn.commit()
    assert store.sent_today(now) == 1 and store.sent_last_7_days(now) == 3
