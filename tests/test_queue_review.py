"""Queue review must agree with sender eligibility without invoking AI or opening a browser."""
import pytest

from connecto import manual, planner
from connecto.store import Store
from helpers import make_cfg, person


@pytest.fixture()
def store(tmp_path):
    with Store(tmp_path / "queue.sqlite") as st:
        yield st


def seed(store, count=23):
    rows = [person(f"review-{i}", f"Person {i}", company="Acme", priority=str(i)) for i in range(count)]
    store.upsert_people(rows, "manual", "manual")
    return rows


def test_review_pages_are_stable_complete_and_use_the_same_notes_as_a_run(store):
    seed(store)
    cfg = make_cfg()
    a = planner.review_queue(cfg, store)
    b = planner.review_queue(cfg, store, offset=20)
    assert a == planner.review_queue(cfg, store)
    assert a["total"] == a["queued"] == 23 and a["has_more"] and not b["has_more"]
    assert len(a["people"]) == 20 and len(b["people"]) == 3
    tail = planner.review_queue(cfg, store, offset=22)
    assert tail["offset"] == 22 and len(tail["people"]) == 1
    assert len({p["url"] for p in a["people"] + b["people"]}) == 23
    for row in a["people"]:
        actual = planner.NoteComposer(cfg).compose(store.get_person(row["url"]), use_ai=False)
        assert row["note"] == actual.text
        assert row["chars"] == len(actual.text) <= a["note_limit"]


@pytest.mark.parametrize("status,eligible", [("sent", False), ("skipped", False), ("error", False),
    ("already_pending", False), ("already_connected", False), ("dry_run", True), ("limit_reached", True),
    ("note_unavailable", True)])
def test_review_respects_handled_and_retryable_statuses(store, status, eligible):
    row = seed(store, 1)[0]
    store.record(row, status)
    assert planner.review_queue(make_cfg(), store)["total"] == int(eligible)


def test_review_honors_sources_targets_and_the_manual_exception(store):
    store.upsert_people([person("wanted", company="Payments"), person("excluded", company="Shoes")], "csv", "leads.csv")
    store.upsert_people([person("disabled", company="Payments")], "webpage", "old-page")
    seed(store, 1)
    cfg = make_cfg(sources=[{"type": "csv", "path": "leads.csv"}], targets={"keywords": ["Payments"]})
    page = planner.review_queue(cfg, store)
    assert {p["company"] for p in page["people"]} == {"Payments", "Acme"}
    assert page["queued"] == 2


@pytest.mark.parametrize("query", ["STRASSE", "munich", "designer", "review-0", "acme", "  Straße  "])
def test_review_searches_all_visible_fields_case_insensitively(store, query):
    store.upsert_people([person("review-0", "Straße Person", company="Acme", title="Designer", location="Munich"),
                         person("other", "Other Person", company="Other", title="Founder")], "manual", "manual")
    page = planner.review_queue(make_cfg(), store, query=query)
    assert page["total"] == 1 and page["queued"] == 2 and page["people"][0]["name"] == "Straße Person"


def test_review_clamps_pages_and_recovers_when_the_last_person_is_skipped(store):
    seed(store, 21)
    cfg = make_cfg()
    last = planner.review_queue(cfg, store, offset=20)["people"][0]
    manual.skip_urls(store, [last["url"]])
    page = planner.review_queue(cfg, store, offset=20)
    assert page["offset"] == 0 and page["total"] == 20
    assert manual.unskip_urls(store, [last["url"]]) == 1
    assert planner.review_queue(cfg, store, offset=20)["people"][0]["url"] == last["url"]
    assert planner.review_queue(cfg, store, limit=0, offset=-3)["limit"] == 1
    assert planner.review_queue(cfg, store, limit=100000)["limit"] == 100
    empty = planner.review_queue(cfg, store, query="no match", offset=100)
    assert empty["total"] == empty["offset"] == 0 and empty["people"] == []


def test_review_explains_plain_invitations_and_never_uses_ai(store, monkeypatch):
    from connecto.ai import Assistant

    def forbidden(*args, **kwargs):
        pytest.fail("Review must not start an AI assistant")

    monkeypatch.setattr(Assistant, "from_config", forbidden)
    seed(store, 1)
    cfg = make_cfg(ai={"enabled": True}, message={"templates": ["Hi {first_name}, {hook}"]})
    page = planner.review_queue(cfg, store)
    assert page["ai_enabled"] and page["people"][0]["note_reason"] == "no_template"
    assert page["people"][0]["chars"] == 0
    cfg["message"]["send_note"] = False
    assert planner.review_queue(cfg, store)["people"][0]["note_reason"] == "notes_off"
    assert store.statuses() == {} and store.recent_runs() == []


def test_review_only_composes_the_requested_page(store, monkeypatch):
    seed(store, 100)
    calls = []
    original = planner.NoteComposer.compose

    def counted(self, person, **kwargs):
        calls.append(person)
        return original(self, person, **kwargs)

    monkeypatch.setattr(planner.NoteComposer, "compose", counted)
    planner.review_queue(make_cfg(), store, offset=40, limit=10)
    assert len(calls) == 10
