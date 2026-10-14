"""planner.py: who is next, the note composer (with and without the AI helper), and the preview."""
import random

import pytest

from connecto import config, notes, planner
from connecto.ai import Assistant
from connecto.models import person_key
from connecto.store import Store

import fake_ai_server as fake
from helpers import make_cfg, person


@pytest.fixture()
def store(tmp_path):
    with Store(tmp_path / "s.sqlite") as st:
        yield st


@pytest.fixture()
def ai_server():
    srv = fake.FakeAIServer().start()
    yield srv
    srv.stop()


def _people(store, n=6, **common):
    rows = [person(f"p{i}", f"Person{i} Test", priority=str(i % 3), **common) for i in range(n)]
    store.upsert_people(rows, "manual", "manual")
    return rows


# ----------------------------------------------------------------------------- candidates / ordering
def test_candidates_skip_handled_and_errors_unless_asked(store):
    rows = _people(store, 5)
    store.record(rows[0], "sent")
    store.record(rows[1], "already_pending")
    store.record(rows[2], "error")
    store.record(rows[3], "dry_run")
    store.record(rows[4], "note_unavailable")
    cfg = make_cfg()
    keys = {p["key"] for p in planner.candidates(cfg, store)}
    assert keys == {person_key(rows[3]), person_key(rows[4])}                  # retryable statuses stay in the queue
    assert person_key(rows[2]) in {p["key"] for p in planner.candidates(cfg, store, retry_errors=True)}
    assert len(planner.candidates(cfg, store, include_handled=True, retry_errors=True)) == 5


def test_a_stored_address_that_is_not_a_plain_profile_address_is_never_queued(store):
    """An older version kept addresses such as ``/in/.\\jane/`` (a browser opens ``/in/jane/``). They must never be opened."""
    _people(store, 2)
    store.conn.execute(
        "INSERT INTO people(key, linkedin_url, full_name, source_type, source_id, data, added_at, updated_at) "
        "VALUES('https://www.linkedin.com/in/.\\p0/', 'https://www.linkedin.com/in/.\\p0/', 'Alias', 'manual', 'manual', "
        "'{\"full_name\": \"Alias\"}', '2026-01-01T00:00:00', '2026-01-01T00:00:00')")
    store.conn.commit()
    assert store.count_people() == 3
    queued = {p["linkedin_url"] for p in planner.candidates(make_cfg(), store, include_handled=True, retry_errors=True)}
    assert queued == {"https://www.linkedin.com/in/p0/", "https://www.linkedin.com/in/p1/"}


def test_candidates_apply_targets_and_source_filters(store):
    store.upsert_people([person("a", company="PayCo", location="Pune, India", one_liner="fintech payments"),
                         person("b", company="ShoeCo", location="London, UK", one_liner="shoes")], "csv", "leads.csv")
    cfg = make_cfg(sources=[{"type": "csv", "path": "leads.csv"}], targets={"keywords": ["fintech"], "locations": ["India"]})
    assert [p["company"] for p in planner.candidates(cfg, store)] == ["PayCo"]
    store.upsert_people([person("c", company="OldCo", batch="Winter 2020", industry="Fintech")], "yc", "yc")
    cfg2 = make_cfg(sources=[{"type": "yc", "batches": ["S26"]}])
    assert "OldCo" not in [p["company"] for p in planner.candidates(cfg2, store)]       # yc filter excludes that batch
    cfg3 = make_cfg()                                                                   # no yc source configured at all
    assert "OldCo" not in [p["company"] for p in planner.candidates(cfg3, store)]       # people of a removed source: unwanted


def test_people_added_by_hand_are_always_wanted_and_go_first(store):
    """Whatever the sources and filters say, someone you added yourself is invited - and before everyone else."""
    store.upsert_people([person("s1", company="PayCo", location="Pune, India", one_liner="fintech payments", priority="20000")],
                        "csv", "leads.csv")
    store.upsert_people([person("h1", company="ShoeCo", location="London, UK", one_liner="shoes", priority="1000000")],
                        "manual", "manual")
    cfg = make_cfg(sources=[{"type": "csv", "path": "leads.csv"}], targets={"keywords": ["fintech"], "locations": ["India"],
                                                                         "exclude_keywords": ["shoes"], "titles": ["astronaut"]})
    assert [p["company"] for p in planner.candidates(cfg, store)] == ["ShoeCo"]        # the sourced one fails the title filter
    loose = make_cfg(sources=[{"type": "csv", "path": "leads.csv"}], targets={"keywords": ["fintech"]})
    ordered = planner.order_candidates(planner.candidates(loose, store), "priority", random.Random(1))
    assert [p["company"] for p in ordered] == ["ShoeCo", "PayCo"]
    # and nothing needs to be switched on for them
    assert [p["company"] for p in planner.candidates(make_cfg(), store)] == ["ShoeCo"]


def test_the_preview_does_not_claim_there_is_no_source_when_people_were_added_by_hand(store):
    assert any("No source of people is switched on yet" in w for w in planner.build_preview(make_cfg(), store)["warnings"])
    store.upsert_people([person("h1")], "manual", "manual")
    assert not any("source" in w.lower() for w in planner.build_preview(make_cfg(), store)["warnings"])


def test_the_preview_says_why_notes_would_be_missing(store):
    bare = {"linkedin_url": "https://www.linkedin.com/in/jdoe/", "full_name": "", "first_name": ""}
    store.upsert_people([bare], "manual", "manual")
    nameless = [w for w in planner.build_preview(make_cfg(), store)["warnings"] if "plain" in w]
    assert nameless and "first name" not in nameless[0].lower()                       # the problem is theirs, not yours
    me_missing = make_cfg(me={"first_name": ""})
    assert any("Your own first name is not set" in w for w in planner.build_preview(me_missing, store)["warnings"])


def test_order_priority_is_stable_random_within_ties():
    rows = [dict(person(f"p{i}"), priority=str(i // 4)) for i in range(12)]
    out = planner.order_candidates(rows, "priority", random.Random(1))
    assert [int(p["priority"]) for p in out] == sorted((int(p["priority"]) for p in rows), reverse=True)
    other = planner.order_candidates(rows, "priority", random.Random(2))
    assert [p["key"] if "key" in p else p["linkedin_url"] for p in out] != [p["linkedin_url"] for p in other]
    added = [dict(person(f"q{i}"), added_at=f"2026-01-0{i + 1}") for i in range(3)]
    assert [p["added_at"] for p in planner.order_candidates(added, "newest_added")] == ["2026-01-03", "2026-01-02", "2026-01-01"]
    assert [p["added_at"] for p in planner.order_candidates(added, "oldest_added")] == ["2026-01-01", "2026-01-02", "2026-01-03"]


# ----------------------------------------------------------------------------- composer without AI
def test_composer_templates_only_and_limits():
    cfg = make_cfg(message={"templates": ["Hi {first_name}, I'm {me_first_name}.[[ I saw {company} and loved the work you do there, truly.]]"],
                            "plan": "free"})
    c = planner.NoteComposer(cfg, rng=random.Random(0))
    p = person("jane", "Jane Doe", company="Acme")
    out = c.compose(p)
    assert out.kind == "template" and out.text.startswith("Hi Jane, I'm Asha.") and len(out.text) <= 200
    assert len(out.refit(30)) <= 30
    assert c.compose(p, max_chars=40).text == "Hi Jane, I'm Asha."          # optional part dropped to fit


def test_composer_off_and_ineligible():
    cfg = make_cfg(message={"send_note": False})
    assert planner.NoteComposer(cfg).compose(person("x")) is None
    cfg = make_cfg(message={"templates": ["Hi {first_name} from {nonexistent}"]})
    assert planner.NoteComposer(cfg).compose(person("x")) is None             # the runner then sends a plain invitation


def test_default_templates_work_for_a_bare_person():
    cfg = make_cfg(message={"templates": []})
    bare = {"linkedin_url": "https://www.linkedin.com/in/x/", "full_name": "Xi Jin", "first_name": "Xi"}
    out = planner.NoteComposer(cfg, rng=random.Random(3)).compose(bare)
    assert out and out.text.startswith("Hi Xi") and "Asha" in out.text and len(out.text) <= 300


THREE_TEMPLATES = ["Hi {first_name}, I'm {me_first_name}, {me_about}. Would love to connect.",
                   "Hello {first_name} - {me_first_name} here ({me_about}). Let's connect.",
                   "{first_name}, I'm {me_first_name} - {me_about}. Happy to be in touch."]


def test_a_person_always_gets_the_same_template_whatever_the_random_source():
    cfg = make_cfg(message={"templates": THREE_TEMPLATES})
    people = [person(f"p{i}", f"Person {i}") for i in range(60)]
    first = {p["linkedin_url"]: planner.NoteComposer(cfg, rng=random.Random(1)).compose(p).text for p in people}
    for seed in (2, 3, 99):
        again = {p["linkedin_url"]: planner.NoteComposer(cfg, rng=random.Random(seed)).compose(p).text for p in people}
        assert again == first
    endings = ("Would love to connect.", "Let's connect.", "Happy to be in touch.")
    used = {next(e for e in endings if text.endswith(e)) for text in first.values()}
    assert used == set(endings)                                                    # ... and the people are spread over all of them


def test_a_different_set_of_templates_may_choose_differently_but_still_per_person():
    people = [person(f"p{i}", f"Person {i}") for i in range(30)]
    a = make_cfg(message={"templates": THREE_TEMPLATES})
    b = make_cfg(message={"templates": THREE_TEMPLATES[:2] + ["{first_name}, nice to meet you. I'm {me_first_name}."]})
    same = sum(planner.NoteComposer(a).compose(p).text == planner.NoteComposer(b).compose(p).text for p in people)
    assert same < len(people)


def test_the_preview_shows_the_notes_the_composer_would_write(tmp_path):
    from connecto.store import Store
    cfg = make_cfg(message={"templates": THREE_TEMPLATES})
    with Store(tmp_path / "s.sqlite") as store:
        store.upsert_people([person(f"p{i}", f"Person {i}") for i in range(12)], "manual", "manual")
        pv = planner.build_preview(cfg, store, sample=12, example_notes=12, rng=random.Random(5))
        assert len(pv["example_notes"]) == 12
        by_name = {p["full_name"]: p for p in store.iter_people()}
        for ex in pv["example_notes"]:
            assert ex["note"] == planner.NoteComposer(cfg, rng=random.Random(777)).compose(by_name[ex["person"]]).text


# ----------------------------------------------------------------------------- composer with AI
def _ai_cfg(srv, **over):
    ai = {"enabled": True, "provider": "custom", "base_url": srv.base_url, "model": "test-model",
          "api_key_env": "CONNECTO_TEST_KEY", "max_calls_per_day": 50}
    ai.update(over)
    return make_cfg(ai=ai, message={"plan": "premium", "templates": [
        "Hi {first_name}, {hook} I'm {me_first_name}, {me_about} - would love to connect.",
        "Hi {first_name}, I'm {me_first_name}, {me_about}. Would love to connect."]})


def test_composer_uses_ai_hook_and_falls_back(store, ai_server, ai_key):
    ai_key()
    cfg = _ai_cfg(ai_server)
    assistant = Assistant.from_config(cfg, store)
    assert assistant is not None
    comp = planner.NoteComposer(cfg, assistant=assistant, rng=random.Random(0))
    p = person("acme", "Pat Lee", company="Acme Pay", one_liner="Payments infrastructure for small businesses")
    out = comp.compose(p)
    assert out.kind == "ai" and fake.DEFAULT_HOOK in out.text and out.hook == fake.DEFAULT_HOOK
    assert out.text.startswith("Hi Pat, Building payments")
    # nothing personal was sent to the model: no name, no URL
    prompt = ai_server.chat_requests()[0].messages_text()
    assert "Pat" not in prompt and "linkedin.com" not in prompt and "Acme Pay" in prompt
    # a person with no usable facts: no call, a template without a hook
    thin = person("thin", "Thin Person", company="", title="")
    before = len(ai_server.chat_requests())
    out2 = comp.compose({**thin, "company": "", "title": ""})
    assert out2.kind == "template" and "{hook}" not in out2.text and len(ai_server.chat_requests()) == before


def test_composer_survives_ai_failure(store, ai_server, ai_key):
    ai_key()
    ai_server.default_chat = fake.unauthorized()
    cfg = _ai_cfg(ai_server)
    comp = planner.NoteComposer(cfg, assistant=Assistant.from_config(cfg, store), rng=random.Random(0))
    p = person("acme", "Pat Lee", company="Acme Pay", one_liner="Payments infrastructure")
    out = comp.compose(p)
    assert out.kind == "template" and out.text.startswith("Hi Pat, I'm Asha")


ROOMY = "I'm {me_first_name}, {me_about} - would love to connect."
TIGHT = ("I'm {me_first_name}, {me_about}. I'm building something in this space and would really love to connect "
         "and trade notes.")
CROWDED = ("I'm {me_first_name}, {me_about}. I'm building something in this space and would really love to connect "
           "and trade notes about what you do and what you are learning, plus anything else you would like to talk "
           "about over the next few weeks.")
PAT = dict(company="Acme Pay", one_liner="Payments infrastructure for small businesses")


def _free_plan_cfg(srv, tail):
    cfg = _ai_cfg(srv)
    cfg["message"]["plan"] = "free"                                          # 200 characters
    cfg["message"]["templates"] = ["Hi {first_name}, {hook} " + tail, "Hi {first_name}, I'm {me_first_name}."]
    return cfg


def _budget(cfg, p, limit=200):
    return notes.hook_budget(cfg["message"]["templates"][0], notes.build_context(p, cfg["me"]), limit)


def test_a_hook_that_fits_the_room_the_template_leaves_is_used_whole(store, ai_server, ai_key):
    ai_key()
    cfg = _free_plan_cfg(ai_server, ROOMY)
    p = person("acme", "Pat Lee", **PAT)
    assert _budget(cfg, p) >= len(fake.DEFAULT_HOOK)                         # (the scenario this test is about)
    out = planner.NoteComposer(cfg, assistant=Assistant.from_config(cfg, store), rng=random.Random(5)).compose(p)
    assert out.kind == "ai" and out.hook == fake.DEFAULT_HOOK and fake.DEFAULT_HOOK in out.text and len(out.text) <= 200
    assert "max_words" in ai_server.chat_requests()[0].messages_text()       # the model was told to keep it short


def test_a_hook_longer_than_the_room_is_dropped_not_cut_and_the_note_still_goes_out(store, ai_server, ai_key):
    ai_key()
    cfg = _free_plan_cfg(ai_server, TIGHT)
    p = person("acme", "Pat Lee", **PAT)
    assert planner.MIN_HOOK_CHARS <= _budget(cfg, p) < len(fake.DEFAULT_HOOK)   # room to ask, too little for this answer
    comp = planner.NoteComposer(cfg, assistant=Assistant.from_config(cfg, store), rng=random.Random(5))
    for _ in range(4):
        out = comp.compose(p)
        assert out.kind == "template" and out.hook == "" and out.text == "Hi Pat, I'm Asha."
        assert "Payments" not in out.text and "Building" not in out.text           # not even the first words of the hook
    assert len(ai_server.chat_requests()) == 1                                  # asked once; "too long" is remembered


class _GreedyAssistant:
    """An assistant whose line is far longer than any note can hold. (The real one is stopped by its own checks first; the
    composer must still never send half of a line, and the template it falls back to is chosen like any other.)"""

    def hook_for(self, person, max_chars):
        return "Building " + "very " * 80 + "ambitious payments tools."


def test_the_fallback_after_a_line_that_could_not_be_kept_is_also_chosen_per_person():
    plain = ["Hi {first_name}, I'm {me_first_name}. Would love to connect.", "Hello {first_name}, {me_first_name} here. Let's connect.",
             "{first_name}, I'm {me_first_name}. Happy to be in touch."]
    cfg = make_cfg(message={"plan": "free", "templates": ["Hi {first_name}, {hook} I'm {me_first_name}."] + plain})
    people = [person(f"p{i}", f"Person {i}", company="Acme Pay", one_liner="Payments") for i in range(60)]

    def written(seed):
        comp = planner.NoteComposer(cfg, assistant=_GreedyAssistant(), rng=random.Random(seed))
        return {p["linkedin_url"]: comp.compose(p) for p in people}

    first = written(1)
    assert all(c.kind == "template" and c.hook == "" and "very" not in c.text and len(c.text) <= 200 for c in first.values())
    for seed in (2, 3, 99):
        assert {u: c.text for u, c in written(seed).items()} == {u: c.text for u, c in first.items()}
    endings = ("Would love to connect.", "Let's connect.", "Happy to be in touch.")
    assert {next(e for e in endings if c.text.endswith(e)) for c in first.values()} == set(endings)


def test_when_the_template_leaves_no_room_the_ai_is_not_even_asked(store, ai_server, ai_key):
    ai_key()
    cfg = _free_plan_cfg(ai_server, CROWDED)
    p = person("acme", "Pat Lee", **PAT)
    assert _budget(cfg, p) < planner.MIN_HOOK_CHARS
    comp = planner.NoteComposer(cfg, assistant=Assistant.from_config(cfg, store), rng=random.Random(5))
    for _ in range(3):
        out = comp.compose(p)
        assert out.kind == "template" and out.text == "Hi Pat, I'm Asha." and len(out.text) <= 200
    assert ai_server.chat_requests() == []
    cfg["message"]["plan"] = "premium"                                          # the same template has room on a 300-character plan
    assert _budget(cfg, p, 300) >= planner.MIN_HOOK_CHARS


# ----------------------------------------------------------------------------- preview
def test_build_preview_shapes_and_warnings(store):
    cfg = make_cfg(sources=[{"type": "csv", "path": "x.csv"}])
    pv = planner.build_preview(cfg, store)
    assert pv["queued"] == 0 and pv["people_total"] == 0
    assert any("not been fetched" in w for w in pv["warnings"])
    _people(store, 8, location="Pune, India", tags="Fintech|Payments")
    store.record(person("p0", "Person0 Test"), "sent")
    cfg = make_cfg()
    pv = planner.build_preview(cfg, store, sample=4)
    assert pv["queued"] == 7 and pv["handled"] == 1 and pv["matching"] == 8
    assert len(pv["sample"]) == 4 and pv["example_notes"] and pv["example_notes"][0]["chars"] <= pv["note_limit"]
    assert pv["daily_target"] >= 8 and pv["days_estimate"] >= 1
    assert dict(pv["breakdown"]["tags"])["Fintech"] == 7
    assert "keywords" not in pv["targets"] or pv["targets"]


def test_preview_warns_when_ai_is_on_but_no_template_uses_hook(store):
    cfg = make_cfg(ai={"enabled": True, "provider": "groq"})
    assert any("{hook}" in w for w in planner.build_preview(cfg, store)["warnings"])
    cfg = make_cfg(ai={"enabled": True}, message={"templates": []})
    assert not any("{hook}" in w for w in planner.build_preview(cfg, store)["warnings"])   # built-in AI templates are used
    assert any(notes.uses_var(t, "hook") for t in config.templates(cfg))
