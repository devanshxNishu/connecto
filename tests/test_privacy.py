"""E-mail addresses, phone numbers and links from strangers' text never get stored, sent to an AI provider or put in a note.

Text is written by strangers: "Jane Doe | jane@acme.com | +1 415 555 0100 | linkedin.com/in/jane". Connecto needs a profile
address, a name, a title and a company, and nothing else of that kind may stay. These tests plant such details in every way a
person can get into Connecto - a spreadsheet, the add-people box, a web page, YC's pages, a LinkedIn search, a v1 migration -
and then look at what is stored (the database file itself, byte by byte), what an AI request holds and what a note says.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fake_ai_server as fake  # noqa: E402
from connecto import manual, notes, privacy  # noqa: E402
from connecto.ai.assist import Assistant  # noqa: E402
from connecto.ai.client import AIClient  # noqa: E402
from connecto.migrate import _read_founders_csv  # noqa: E402
from connecto.models import make_person, person_key  # noqa: E402
from connecto.sources import csv_file, linkedin_search, webpage  # noqa: E402
from connecto.sources.yc import YCSource  # noqa: E402
from connecto.sources.yc_parser import Founder  # noqa: E402
from connecto.store import Store  # noqa: E402

JANE = "https://www.linkedin.com/in/jane-doe/"

# what must never appear, in any spelling a test plants
NEEDLES = ("jane@acme.com", "jane.doe@gmail.com", "415 555 0100", "415-555-0100", "4155550100", "20 7946 0958",
           "evil.example", "calendly.com", "jane [at] acme", "janedoe")


def leaks(text: str) -> list[str]:
    low = text.lower()
    return [needle for needle in NEEDLES if needle in low]


# ----------------------------------------------------------------------------- what is recognised
@pytest.mark.parametrize("text,expected", [
    ("Jane Doe | jane@acme.com | +1 415 555 0100 | linkedin.com/in/janedoe | 12 Market St", "Jane Doe | 12 Market St"),
    ("Reach me: jane.doe+yc@acme.co.uk", "Reach me"),
    ("CEO at Acme (jane@acme.com)", "CEO at Acme"),
    ("jane (at) acme (dot) com", ""), ("jane [at] acme.com", ""), ("jane at acme dot com", ""), ("jane{at}acme{dot}io", ""),
    ("Call (415) 555-0100 anytime", "Call anytime"), ("tel: +44 20 7946 0958", ""), ("+91-98765-43210", ""),
    ("415.555.0100", ""), ("4155550100", ""), ("WhatsApp +1 (415) 555-0100", "WhatsApp"),
    ("see https://calendly.com/jane/30min now", "see now"), ("www.acme.com/team", ""), ("x.com/jane_doe", ""),
    ("github.com/jane", ""), ("mailto:jane@acme.com", ""), ("a, jane@x.com, b", "a, b"), ("a · b@c.io · d", "a · d"),
    ("Founder; (jane@x.com); CEO", "Founder; CEO"),
])
def test_contact_details_are_removed_and_the_rest_is_kept_tidy(text, expected):
    assert privacy.scrub_text(text) == expected
    assert privacy.has_contact_details(text)


@pytest.mark.parametrize("text", [
    "Founder & CEO", "Ex-Google, Stripe", "10x engineer", "Building the future of payments (YC S26)", "Series A, $12M raised",
    "Acme.io", "Node.js developer", "Node.js/React engineer", "2015–2020 Stanford", "Class of 2024", "Top 30 under 30",
    "ISO 27001", "24/7 support", "B2B SaaS", "Web3", "100+ employees", "San Francisco, CA 94105", "since 2012",
    "$1,200,000 ARR", "CEO @ Notion.so", "CEO @ Acme", "Founder at Acme", "C++11 2014-2017", "Booking.com", "x.com",
    "Co-founder/CTO", "AT&T", "Jane's Bakery", "linkedin.com", "Head of Growth at Stripe, ex-Uber", "S26 batch, Fintech", "",
    "Grew +5 - 10 - 15 percent a month", "Ranked (10) 9 8 7 6 overall",         # (a few digits and separators are not a phone number)
])
def test_ordinary_text_is_left_exactly_as_it_was(text):
    assert privacy.scrub_text(text) == text and not privacy.has_contact_details(text)


def test_scrubbing_twice_changes_nothing_more():
    once = privacy.scrub_text("Jane | jane@acme.com | +1 415 555 0100 | CEO")
    assert privacy.scrub_text(once) == once == "Jane | CEO"


@pytest.mark.parametrize("text,expected", [
    ("-tel:@http://", ""),                    # what is left once the first detail is out ("tel:@") is one too
    ("·-tel:tel: +", "+"), ("-www.]mailto:", ""),
])
def test_what_is_left_after_a_removal_is_scrubbed_again(text, expected):
    assert privacy.scrub_text(text) == expected and privacy.scrub_text(expected) == expected


def test_a_long_text_is_cut_not_kept_whole():
    assert len(privacy.scrub_text("x" * 10_000)) == privacy.MAX_CHARS
    assert 1_200 <= privacy.MAX_CHARS <= 10_000                      # (longer than any field, far shorter than a page)


@pytest.mark.parametrize("name,is_contact", [
    ("email", True), ("e_mail", True), ("email_address", True), ("emailaddress", True), ("work_email", True), ("email_1", True),
    ("emails", True), ("mail", True), ("mail_2", True), ("gmail", True), ("phone", True), ("phone_number", True),
    ("phonenumber", True), ("phone1", True), ("mobile", True), ("mobile_phone", True), ("cell", True), ("cell_phone", True),
    ("tel", True), ("telephone", True), ("fax", True), ("whatsapp", True), ("skype", True), ("telegram", True), ("sms", True),
    ("home_address", True), ("street_address", True), ("postal_address", True), ("mailing_address", True),
    ("company_name_for_emails", False), ("full_name", False), ("title", False), ("company", False), ("headline", False),
    ("location", False), ("address", False), ("company_address", False), ("hotel_name", False), ("cellar", False),
    ("website", False), ("notes", False), ("contact", False), ("industry", False), ("tags", False),
])
def test_a_field_that_says_it_holds_an_email_or_phone_is_dropped(name, is_contact):
    assert privacy.is_contact_field(name) is is_contact


@pytest.mark.parametrize("value,expected", [
    ("https://acme.com/team?utm=1#x", "https://acme.com/team"), ("acme.com/team", "https://acme.com/team"),
    ("www.acme.com", "https://www.acme.com"), ("http://user:pw@acme.com/a", "http://acme.com/a"),
    ("https://x.com/jane?email=jane@acme.com", "https://x.com/jane"), ("http://www.linkedin.com/company/zeta", "http://www.linkedin.com/company/zeta"),
    ("https://[::1]:8080/x", "https://[::1]:8080/x"),
    ("mailto:jane@acme.com", ""), ("jane@acme.com", ""), ("javascript:alert(1)", ""), ("ftp://acme.com", ""), ("acme", ""),
    ("https://acme.com:99999/", ""), ("", ""), (None, ""), ("https://acme.com/" + "a" * 3000, ""),
    ("https://acme.com/u/jane@acme.com/team", "https://acme.com"), ("https://acme.com/call/4155550100", "https://acme.com"),
    ("https://acme.com/team/2015-2020/about", "https://acme.com/team/2015-2020/about"), ("https://acme.com", "https://acme.com"),
])
def test_a_web_address_field_keeps_a_plain_address_or_nothing(value, expected):
    assert privacy.clean_link(value) == expected


@pytest.mark.parametrize("name,is_link", [
    ("website", True), ("company_website", True), ("homepage", True), ("twitter_url", True), ("linkedinurl", True), ("profile_link", True),
    ("personal_site", True), ("site", True), ("web", True), ("company_linkedin_url", True),
    ("opposite", False), ("composite", False), ("title", False), ("company", False), ("headline", False), ("notes", False), ("", False),
])
def test_which_fields_hold_web_addresses(name, is_link):
    assert privacy.is_link_field(name) is is_link


def test_the_person_s_own_name_is_taken_out_of_a_text():
    person = {"full_name": "Jane Doe", "first_name": "Jane", "last_name": "Doe"}
    assert privacy.without_name("Jane Doe | Founder & CEO at Acme", person) == "Founder & CEO at Acme"
    assert privacy.without_name("CEO, Doe Consulting (Jane)", person) == "CEO, Consulting"
    assert privacy.without_name("Developer advocate", person) == "Developer advocate"             # "Dev" is not "Doe"
    assert privacy.without_name("anything", {}) == "anything" and privacy.without_name("", person) == ""
    assert privacy.without_name("Janet's Bakery, Doeville", person) == "Janet's Bakery, Doeville"      # (whole words only)
    assert privacy.without_name("Vitamin K supplements", {"full_name": "Asha K"}) == "Vitamin K supplements"   # (one letter is no name)
    assert privacy.without_name("CEO Doe Consulting", {"first_name": "Jane", "last_name": "Doe"}) == "CEO Consulting"
    assert privacy.name_words({"full_name": "Jane " + "x" * 400 + " Zed"}) == ["x" * 295, "jane"]      # (a name is read up to a limit)


# ----------------------------------------------------------------------------- the row builder
def test_make_person_drops_contact_fields_and_cleans_the_rest():
    row = make_person(linkedin_url=JANE, full_name="Jane Doe (jane@acme.com)", title="CEO | +1 415 555 0100",
                      company="Acme https://evil.example/pay", headline="Jane Doe | jane.doe@gmail.com | Building payments",
                      EmailAddress="jane@acme.com", PhoneNumber="+1 415 555 0100", mail="jane@acme.com", phone1="4155550100",
                      website="https://acme.com/?email=jane@acme.com", twitter_url="https://x.com/jane?ref=jane@acme.com",
                      bio="Call 415-555-0100 or write jane@acme.com", notes="calendly.com/jane", me_about="x")
    assert row["linkedin_url"] == JANE and row["full_name"] == "Jane Doe" and row["title"] == "CEO"
    assert row["company"] == "Acme" and row["headline"] == "Jane Doe | Building payments"
    assert row["website"] == "https://acme.com/" and row["twitter_url"] == "https://x.com/jane"
    assert row["bio"] == "Call or write" and row["notes"] == "" and row["first_name"] == "Jane"
    assert not [k for k in row if privacy.is_contact_field(k)]
    assert leaks(json.dumps(row)) == []


def test_rows_that_are_already_clean_come_through_unchanged():
    row = make_person(linkedin_url=JANE, full_name="Jane Doe", title="Co-founder & CTO", company="Acme.io",
                      one_liner="Node.js/React tooling for 100+ teams", batch="S26", location="Pune, India")
    assert row["company"] == "Acme.io" and row["one_liner"] == "Node.js/React tooling for 100+ teams" and row["batch"] == "S26"
    assert privacy.scrub_person(row) == row


def test_the_store_removes_contact_details_from_rows_built_any_other_way(tmp_path):
    with Store(tmp_path / "x.sqlite") as store:
        store.upsert_people([{"linkedin_url": JANE, "full_name": "Jane Doe", "headline": "CEO | jane@acme.com",
                              "email": "jane@acme.com", "phone": "+1 415 555 0100", "website": "https://acme.com/?e=jane@acme.com",
                              "bio": "call +1 415 555 0100"}], "manual", "manual")
        person = store.get_person(person_key({"linkedin_url": JANE}))
    assert person["headline"] == "CEO" and person["bio"] == "call" and person["website"] == "https://acme.com/"
    assert "email" not in person and "phone" not in person and leaks(json.dumps(person)) == []


# ----------------------------------------------------------------------------- every way in
def _rows_from_every_source(tmp_path) -> list[dict]:
    rows: list[dict] = []

    csv_path = tmp_path / "leads.csv"
    csv_path.write_text(
        "First Name,Last Name,Title,Company,EmailAddress,PhoneNumber,mail,contact,notes,phone1,LinkedIn\n"
        'Jane (jane@acme.com),Doe,"CEO | +1 415 555 0100",Acme (https://evil.example),jane@acme.com,+1 415 555 0100,jane.doe@gmail.com,'
        '"call jane@acme.com or 4155550100","see calendly.com/jane",+44 20 7946 0958,https://www.linkedin.com/in/jane-doe/\n',
        encoding="utf-8")
    rows += csv_file.read_csv_rows(csv_path)[0]

    rows += manual.parse_people_text("Jane Doe, Acme Pay | +1 415 555 0100, CEO jane@acme.com, https://www.linkedin.com/in/jane-manual/")[0]

    html = ('<html><body><div class="card"><h3>Jane Doe</h3><p>CEO · jane@acme.com · +1 415 555 0100 · linkedin.com/in/janedoe</p>'
            '<a href="https://www.linkedin.com/in/jane-page/">in</a></div>'
            '<script type="application/ld+json">{"@type":"Person","name":"Jane Doe","jobTitle":"CEO jane@acme.com",'
            '"worksFor":"Acme +1 415 555 0100","sameAs":"https://www.linkedin.com/in/jane-ld/"}</script></body></html>')
    rows += webpage.extract_people_ex(html, "https://acme.example/team?email=jane@acme.com")[0]

    founder = Founder(full_name="Jane Doe", title="CEO jane@acme.com", bio="Reach me at jane.doe@gmail.com or 415-555-0100",
                      linkedin_url="https://www.linkedin.com/in/jane-yc/")
    company = {"slug": "acme", "name": "Acme", "batch": "Summer 2026", "one_liner": "Payments, mail jane@acme.com",
               "long_description": "Call +1 415 555 0100 or see www.evil.example/pay https://evil.example/pay", "website": "https://acme.com/?email=jane@acme.com",
               "tags": ["Fintech"], "industries": ["Fintech"]}
    rows.append(YCSource.person(None, company, founder, "https://www.ycombinator.com/companies/acme"))

    cards = [{"href": "https://www.linkedin.com/in/jane-search/",
              "text": "Jane Doe\n• 2nd\nCEO at Acme | jane@acme.com | +1 415 555 0100\nPune, India"}]
    rows += linkedin_search.parse_result_cards(cards, "https://www.linkedin.com", search="ceo")

    founders = tmp_path / "founders.csv"
    founders.write_text("full_name,title,company,linkedin_url,bio,email,phone\n"
                        'Jane Doe,"CEO jane@acme.com",Acme +1 415 555 0100,https://www.linkedin.com/in/jane-v1/,"mail jane.doe@gmail.com",jane@acme.com,4155550100\n',
                        encoding="utf-8")
    rows += _read_founders_csv(founders).people
    return rows


def test_every_source_removes_contact_details_before_a_person_is_kept(tmp_path):
    rows = _rows_from_every_source(tmp_path)
    assert {r["linkedin_url"].split("/in/")[1] for r in rows} == {
        "jane-doe/", "jane-manual/", "jane-page/", "jane-ld/", "jane-yc/", "jane-search/", "jane-v1/"}
    for row in rows:
        assert leaks(json.dumps(row)) == [], row
        assert not [k for k in row if privacy.is_contact_field(k)], row


def test_the_database_file_never_holds_a_contact_detail(tmp_path):
    with Store(tmp_path / "connecto.sqlite") as store:
        store.upsert_people(_rows_from_every_source(tmp_path), "mixed", "test")
        assert store.count_people() == 7
        store.conn.commit()
        stored = [p for p in store.people()]
    assert all(leaks(json.dumps(p)) == [] for p in stored)
    raw = b"".join(path.read_bytes() for path in tmp_path.glob("connecto.sqlite*"))
    assert b"Jane" in raw                                                  # (the files really were read)
    assert leaks(raw.decode("utf-8", errors="replace")) == []


# ----------------------------------------------------------------------------- what an AI provider is told
ME = {"first_name": "Asha", "about": "a designer in Pune", "project": "NINA", "project_blurb": "an AI personal stylist"}
KEY = "sk-test-SECRET-1234567890"


@pytest.fixture()
def server():
    srv = fake.FakeAIServer().start()
    yield srv
    srv.stop()


@pytest.fixture()
def store(tmp_path):
    with Store(tmp_path / "ai.sqlite") as st:
        yield st


def assistant(server, store) -> Assistant:
    client = AIClient(server.base_url, KEY, "test-model", rpm=0, max_retries=0)
    return Assistant(client, store, {"enabled": True, "max_calls_per_day": 60, "temperature": 0.6}, ME)


def test_facts_are_checked_again_where_they_leave_the_machine(server, store):
    sloppy = {"linkedin_url": JANE, "full_name": "Jane Doe", "first_name": "Jane", "last_name": "Doe",
              "title": "Jane Doe, CEO jane@acme.com", "company": "Doe & Sons Payments", "headline": "Jane Doe | +1 415 555 0100 | Building payments",
              "one_liner": "Payments for small businesses - see https://evil.example/pay", "industry": "Fintech",
              "tags": "Fintech | jane.doe@gmail.com | Payments"}                       # (a row nothing cleaned)
    facts = assistant(server, store).facts_for(sloppy)
    assert facts == {"title": "CEO", "company": "Doe & Sons Payments", "headline": "Building payments",
                     "one_liner": "Payments for small businesses - see", "industry": "Fintech", "tags": "Fintech, Payments"}
    assert leaks(json.dumps(facts)) == []
    assert "Jane" not in json.dumps(facts) and "Doe" not in json.dumps({k: v for k, v in facts.items() if k != "company"})


def test_an_ai_request_holds_no_contact_detail_and_no_name(server, store):
    sloppy = {"linkedin_url": JANE, "full_name": "Jane Doe", "first_name": "Jane", "last_name": "Doe", "title": "CEO",
              "company": "Acme Pay", "headline": "Jane Doe | jane@acme.com | +1 415 555 0100 | linkedin.com/in/janedoe | Building payments infrastructure",
              "one_liner": "Payments infrastructure for small businesses, jane.doe@gmail.com", "location": "Zanzibar"}
    assert assistant(server, store).hook_for(sloppy) == fake.DEFAULT_HOOK
    request = server.chat_requests()[0]
    sent = request.messages_text()
    assert "Building payments infrastructure" in sent and "Payments infrastructure for small businesses" in sent
    assert leaks(request.raw) == [] and "Jane" not in request.raw and "Zanzibar" not in request.raw      # (no name, no location)
    assert "a designer in Pune" in sent                                                                   # (the sender's own words are theirs to send)


# ----------------------------------------------------------------------------- what a note says
def test_a_note_never_carries_a_contact_detail_from_a_stranger_s_text():
    sloppy = {"linkedin_url": JANE, "full_name": "Jane Doe", "first_name": "Jane", "title": "CEO",
              "company": "Acme https://evil.example/pay or call +1 415 555 0100", "headline": "mail jane@acme.com"}
    ctx = notes.build_context(sloppy, {"first_name": "Asha"})
    text = notes.fit("Hi {first_name}, I'm {me_first_name}. {title} at {company} - {headline}", ctx, 300)
    assert text == "Hi Jane, I'm Asha. CEO at Acme or call - mail"
    assert leaks(text) == []


def test_a_spreadsheet_column_cannot_stand_in_for_something_the_sender_wrote(tmp_path):
    path = tmp_path / "leads.csv"
    path.write_text("Name,LinkedIn,me_about,me_first_name,Priority\nJane Doe,https://www.linkedin.com/in/jane-doe/,"
                    "see evil.example/pay,Mallory,5\n", encoding="utf-8")
    row = csv_file.read_csv_rows(path)[0][0]
    assert row["csv_me_about"] == "see evil.example/pay" and row["csv_me_first_name"] == "Mallory" and "me_about" not in row
    ctx = notes.build_context(dict(row, me_about="planted", me_project="planted"), {"first_name": "Asha"})
    assert ctx["me_first_name"] == "Asha" and "me_about" not in ctx and "me_project" not in ctx


@pytest.mark.parametrize("cell,expected", [("7", "7"), ("0", "0"), ("999999", "999999"), ("1000000", "999999"),
                                           ("99999999999", "999999"), ("inf", "999999"), ("-5", "0"), ("-inf", "0"),
                                           ("nan", ""), ("high", ""), ("", "")])
def test_a_priority_column_can_never_outrank_somebody_added_by_hand(tmp_path, cell, expected):
    path = tmp_path / "leads.csv"
    path.write_text(f"Name,LinkedIn,Priority\nJane Doe,https://www.linkedin.com/in/jane-doe/,{cell}\n", encoding="utf-8")
    row = csv_file.read_csv_rows(path)[0][0]
    assert row.get("priority", "") == expected
    assert float(row.get("priority") or 0) < float(manual.MANUAL_PRIORITY)
