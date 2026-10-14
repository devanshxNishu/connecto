"""manual.py: adding people by hand, and the "never contact" list."""
import pytest

from connecto import manual, planner
from connecto.models import person_key
from connecto.store import Store

from helpers import make_cfg, person

JANE = "https://www.linkedin.com/in/jane-doe-1a2b3c/"
parse = manual.parse_people_text


@pytest.fixture()
def store(tmp_path):
    with Store(tmp_path / "s.sqlite") as st:
        yield st


# ----------------------------------------------------------------------------- reading what was pasted
def test_a_link_on_its_own_is_a_person_without_a_name():
    people, problems = parse(JANE)
    assert problems == [] and len(people) == 1
    one = people[0]
    assert one["linkedin_url"] == JANE and one["full_name"] == "" and one["first_name"] == ""
    assert one["priority"] == manual.MANUAL_PRIORITY


@pytest.mark.parametrize("line", [
    "Jane Doe, " + JANE,
    JANE + " | Jane Doe",
    "Jane Doe - " + JANE,
    "Jane Doe; " + JANE,
    "\"Jane Doe\" <" + JANE + ">",
])
def test_the_name_may_come_before_or_after_the_link(line):
    (one,), problems = parse(line)
    assert problems == [] and one["full_name"] == "Jane Doe" and one["first_name"] == "Jane"


@pytest.mark.parametrize("line", [
    "Jane Doe, Acme Pay, CEO, " + JANE,
    JANE + " | Jane Doe | Acme Pay | CEO",
    "Jane Doe\tAcme Pay\tCEO\t" + JANE,
])
def test_company_and_title_follow_the_name(line):
    (one,), _ = parse(line)
    assert (one["full_name"], one["company"], one["title"]) == ("Jane Doe", "Acme Pay", "CEO")


def test_blank_lines_and_comments_are_ignored_and_bad_lines_do_not_stop_the_good_ones():
    text = f"""
# my list
{JANE}

this line has no link
https://www.linkedin.com/company/acme/
https://example.com/in/someone
Bo Two, https://www.linkedin.com/in/bo-two/
"""
    people, problems = parse(text)
    assert [p["linkedin_url"] for p in people] == [JANE, "https://www.linkedin.com/in/bo-two/"]
    assert len(problems) == 3 and all(p.startswith("Line ") for p in problems)
    assert "Line 5" in problems[0] and "no LinkedIn profile link" in problems[0]            # (the first line is blank)


def test_the_same_person_pasted_twice_is_one_person():
    people, _ = parse(f"{JANE}\nJane Doe, {JANE.upper().replace('HTTPS', 'https')}\n{JANE}?utm_source=x")
    assert len(people) == 1


def test_tracking_parameters_and_sub_pages_are_dropped_from_the_link():
    (one,), _ = parse("https://in.linkedin.com/in/jane-doe-1a2b3c/details/experience/?trk=abc#top")
    assert one["linkedin_url"] == JANE


def test_an_email_address_is_never_stored_and_never_mistaken_for_a_name():
    (one,), _ = parse(f"jane@acme.com, Jane Doe, {JANE}")
    assert one["full_name"] == "Jane Doe" and "@" not in " ".join(one.values())
    (nameless,), _ = parse(f"jane@acme.com {JANE}")
    assert nameless["full_name"] == "" and "@" not in " ".join(nameless.values())


@pytest.mark.parametrize("junk", ["12345", "x" * 120, "see https://example.com/page"])
def test_something_that_is_not_a_name_is_not_used_as_one(junk):
    (one,), _ = parse(f"{junk}, {JANE}")
    assert one["full_name"] == ""


def test_extra_spaces_around_pasted_values_are_tidied():
    (one,), _ = parse(f"  Jane   Doe  ,  Acme   Pay ,   CEO  , {JANE}")
    assert (one["full_name"], one["company"], one["title"]) == ("Jane Doe", "Acme Pay", "CEO")


def test_very_long_pastes_are_cut_at_a_limit_with_a_note():
    text = "\n".join(f"https://www.linkedin.com/in/person-{i}/" for i in range(manual.MAX_LINES + 5))
    people, problems = parse(text)
    assert len(people) == manual.MAX_LINES and any(str(manual.MAX_LINES) in p for p in problems)


@pytest.mark.parametrize("empty", ["", "   \n\n", None])
def test_nothing_pasted_is_nothing_added(empty):
    assert parse(empty) == ([], [])


# ----------------------------------------------------------------------------- storing them
def test_added_people_are_stored_as_hand_added_and_re_adding_updates(store):
    people, _ = parse(f"{JANE}\nBo Two, https://www.linkedin.com/in/bo-two/")
    assert manual.add_people(store, people) == (2, 0)
    assert {p["source_type"] for p in store.people()} == {"manual"}
    better, _ = parse(f"Jane Doe, Acme Pay, CEO, {JANE}")
    assert manual.add_people(store, better) == (0, 1)                         # the name and company are filled in
    jane = store.get_person(person_key({"linkedin_url": JANE}))
    assert (jane["full_name"], jane["company"], jane["first_name"]) == ("Jane Doe", "Acme Pay", "Jane")
    assert manual.add_people(store, better) == (0, 0)                         # nothing new this time


def test_someone_a_source_already_found_becomes_yours_when_you_add_them_by_hand(store):
    """The filters of a source must not drop somebody you asked for."""
    store.upsert_people([person("jane-doe-1a2b3c", "Jane Doe", company="Old Co", priority="20000")], "yc", "yc")
    key = person_key({"linkedin_url": JANE})
    assert store.get_person(key)["source_type"] == "yc"
    mine, _ = parse(f"Jane Doe, New Co, {JANE}")
    assert manual.add_people(store, mine) == (0, 1)
    jane = store.get_person(key)
    assert jane["source_type"] == "manual" and jane["priority"] == manual.MANUAL_PRIORITY
    cfg = make_cfg(targets={"keywords": ["nothing-matches-this"]})
    assert [p["full_name"] for p in planner.candidates(cfg, store)] == ["Jane Doe"]


def test_a_source_that_finds_your_hand_added_person_later_does_not_take_them_back_or_demote_them(store):
    manual.add_people(store, parse(f"Jane Doe, {JANE}")[0])
    store.upsert_people([person("jane-doe-1a2b3c", "Jane Doe", company="Acme Pay", priority="20000")], "yc", "yc")
    jane = store.get_person(person_key({"linkedin_url": JANE}))
    assert jane["source_type"] == "manual" and jane["priority"] == manual.MANUAL_PRIORITY
    assert jane["company"] == "Acme Pay"                                                  # (the extra detail is still welcome)


# ----------------------------------------------------------------------------- never contact
def test_skipping_removes_someone_from_the_queue_and_undo_brings_them_back(store):
    manual.add_people(store, parse(f"Jane Doe, {JANE}\nBo Two, https://www.linkedin.com/in/bo-two/")[0])
    cfg = make_cfg()
    assert len(planner.candidates(cfg, store)) == 2
    done, rejected = manual.skip_urls(store, [JANE, "linkedin.com/in/bo-two", "https://example.com/nope"])
    assert len(done) == 2 and rejected == ["https://example.com/nope"]
    assert planner.candidates(cfg, store) == []
    assert store.status_of(person_key({"linkedin_url": JANE})) == "skipped"
    assert manual.unskip_urls(store, [JANE]) == 1
    assert [p["full_name"] for p in planner.candidates(cfg, store)] == ["Jane Doe"]
    assert manual.unskip_urls(store, [JANE]) == 0                                         # already undone


def test_skipping_somebody_the_store_has_never_seen_still_protects_them(store):
    done, _ = manual.skip_urls(store, [JANE])
    assert len(done) == 1
    manual.add_people(store, parse(f"Jane Doe, {JANE}")[0])
    assert planner.candidates(make_cfg(), store) == []


@pytest.mark.parametrize("status", ["sent", "already_connected", "already_pending"])
def test_people_who_are_already_in_your_network_are_left_alone_by_skip(store, status):
    one = person("jane-doe-1a2b3c", "Jane Doe")
    store.upsert_people([one], "manual", "manual")
    store.record(one, status)
    done, rejected = manual.skip_urls(store, [JANE])
    assert done == [] and rejected == [] and store.status_of(person_key(one)) == status


def test_undo_only_touches_skips_never_real_history(store):
    one = person("jane-doe-1a2b3c", "Jane Doe")
    store.record(one, "sent")
    assert manual.unskip_urls(store, [JANE]) == 0 and store.status_of(person_key(one)) == "sent"
