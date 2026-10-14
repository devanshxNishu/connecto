"""Text that strangers write - web pages, spreadsheets, pasted lists, a model's reply - must never make Connecto stop
answering.

A regular expression that retries from every position of a long run of letters turns 200 KB of text into minutes of
work (a single long word pasted into the "add people" box did exactly that), and a page whose elements are nested
thousands deep can do the same to code that looks at "the ancestors of this link". Every parser below is fed such input
and has to answer within a second. A call that has not answered after a few seconds is cut off, so a regression is a
failing test and not a suite that hangs.
"""
from __future__ import annotations

import json
import signal
import time
from contextlib import contextmanager

import pytest
from bs4 import BeautifulSoup

from connecto import manual, notes, privacy
from connecto.ai import guard
from connecto.models import make_person, normalize_linkedin
from connecto.sources import csv_file, webpage, yc_parser

BUDGET = 1.0            # seconds a parser may take
HARD_STOP = 4           # seconds after which a call is abandoned
N = 100_000


class TooSlow(Exception):
    pass


@contextmanager
def within(seconds: float = HARD_STOP):
    if not hasattr(signal, "setitimer"):          # Windows: no interrupt, the time check below still applies
        yield
        return

    def give_up(*_):
        raise TooSlow(f"still working after {seconds:g} seconds")

    previous = signal.signal(signal.SIGALRM, give_up)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def quickly(call, *args, budget: float = BUDGET):
    """Run call(*args); fail if it is cut off or takes longer than `budget`. Returns its result (or the error it raised
    on its own, as an exception *value*: a clean refusal is an answer)."""
    started = time.perf_counter()
    with within():
        try:
            result = call(*args)
        except TooSlow:
            raise
        except RecursionError as exc:
            pytest.fail(f"{call.__name__} ran out of stack on hostile input: {exc}")
        except Exception as exc:  # noqa: BLE001 - what matters here is how long the answer took
            result = exc
    took = time.perf_counter() - started
    assert took < budget, f"{getattr(call, '__name__', call)} took {took:.2f}s (budget {budget:g}s)"
    return result


# ----------------------------------------------------------------------------- the inputs
BATTERY = {
    "one long word": "a" * N,
    "hyphenated word": "a-" * (N // 2),
    "dashes": "-" * N,
    "digits": "1" * N,
    "digits and dashes": "1-" * (N // 2),
    "spaces": " " * N,
    "newlines": "\n" * N,
    "word and dot": "a." * (N // 2),
    "open double brackets": "[[" * (N // 2),
    "open double brackets with a letter": "[[a" * (N // 3),
    "open parentheses": "(" * N,
    "empty parentheses": "()" * (N // 2),
    "open braces": "{" * N,
    "dashes between spaces": " - " * (N // 3),
    "long word then a profile link": "a" * N + " https://www.linkedin.com/in/x/",
    "profile address over and over": "linkedin.com/in/" * (N // 16),
    "protocol over and over": "https://" * (N // 8),
    "combining accent": "e" + "́" * N,
    "hyphenated name that ends like a domain": "a-" * (N // 2) + ".com",
    "spaces then a word": " " * N + "x",
    "to connect and spaces": "Invite a" + " to connect" * 5 + " " * N + "x",
}
IDS = list(BATTERY)


@pytest.fixture(params=IDS)
def hostile(request):
    return BATTERY[request.param]


# ----------------------------------------------------------------------------- pasted lists and spreadsheets
def test_pasting_hostile_text_into_add_people_is_answered_quickly(hostile):
    quickly(manual.parse_people_text, hostile)


def test_pasting_a_long_word_before_a_link_still_finds_the_link():
    people, problems = quickly(manual.parse_people_text, "a" * N + " https://www.linkedin.com/in/jane-doe/")
    assert [p["linkedin_url"] for p in people] == ["https://www.linkedin.com/in/jane-doe/"] and problems == []


def test_a_very_long_pasted_list_is_cut_at_the_line_limit_and_still_quick():
    text = "\n".join(f"Person {i}, https://www.linkedin.com/in/person-{i}/" for i in range(manual.MAX_LINES + 5000))
    people, problems = quickly(manual.parse_people_text, text, budget=3)
    assert len(people) == manual.MAX_LINES and any("first" in p for p in problems)


@pytest.mark.parametrize("cell_as", ["name", "url", "title", "whole file"])
def test_a_spreadsheet_with_hostile_cells_is_read_quickly(tmp_path, hostile, cell_as):
    path = tmp_path / "leads.csv"
    cell = hostile.replace("\n", " ").replace('"', "'")
    if cell_as == "whole file":
        path.write_text(hostile, encoding="utf-8")
    else:
        row = {"name": f'"{cell}",Acme,CEO,https://www.linkedin.com/in/x/', "url": f'Jane Doe,Acme,CEO,"{cell}"',
               "title": f'Jane Doe,Acme,"{cell}",https://www.linkedin.com/in/x/'}[cell_as]
        path.write_text("Name,Company,Title,LinkedIn\n" + row + "\n", encoding="utf-8")
    quickly(csv_file.read_csv_rows, path)


def test_a_cell_with_a_long_word_before_the_link_still_yields_the_person(tmp_path):
    path = tmp_path / "leads.csv"
    path.write_text("Name,Company,LinkedIn Profile\nJane Doe,Acme," + "a" * 100_000 + " https://www.linkedin.com/in/jane-doe/\n",
                    encoding="utf-8")
    quickly(csv_file.read_csv_rows, path)


# ----------------------------------------------------------------------------- what a model says
FACTS = {"title": "CEO", "company": "Acme Pay", "one_liner": "Payments infrastructure for small businesses", "tags": "Fintech"}


def test_a_model_reply_of_hostile_text_is_judged_quickly(hostile):
    assert quickly(guard.clean_hook, hostile, FACTS) is None


def test_a_long_hyphenated_reply_is_refused_in_a_tenth_of_a_second():
    assert quickly(guard.clean_hook, "a-" * 40_000, FACTS, budget=0.1) is None


def test_a_reply_that_wraps_the_hook_in_label_after_label_is_not_unwrapped_forever():
    reply = "Hook: " * 2000 + "Building payments infrastructure for small businesses."
    assert quickly(guard.clean_hook, reply, FACTS, budget=0.1) is None
    assert guard.clean_hook("Hook: Building payments infrastructure for small businesses.", FACTS)       # one label is fine
    assert guard.clean_hook("- **Hook:** \"Building payments infrastructure for small businesses.\"", FACTS)


def test_audience_json_in_hostile_text_is_judged_quickly(hostile):
    quickly(guard.parse_audience, hostile)


def test_hostile_facts_are_judged_quickly(hostile):
    hook = "Building payments infrastructure for small businesses."
    assert quickly(guard.clean_hook, hook, dict(FACTS, one_liner=hostile[:50_000], tags=hostile[:50_000])) in (None, hook)
    quickly(guard.looks_like_injection, hostile)


# ----------------------------------------------------------------------------- web pages
def test_a_hostile_name_is_cleaned_quickly(hostile):
    quickly(webpage.clean_name, hostile)
    quickly(webpage.name_from_label, hostile)


def test_a_name_is_still_found_at_the_start_of_a_long_text():
    assert webpage.clean_name("Jane Doe, PhD" + " and so on" * 500) == "Jane Doe"
    assert webpage.clean_name("Jane   (she/her)   Doe") == "Jane Doe"
    assert webpage.clean_name("  " + " " * 10_000 + "Jane Doe") == ""             # beyond what is read: not a name here


def page(body: str, head: str = "") -> str:
    return f"<html><head>{head}</head><body>{body}</body></html>"


def test_a_page_with_hostile_text_around_a_profile_link_is_read_quickly(hostile):
    text = hostile.replace("<", "&lt;")
    quickly(webpage.extract_people_ex, page(f'<h3>{text}</h3><a href="https://www.linkedin.com/in/x/">{text}</a>'), "https://example.com/")
    quickly(webpage.extract_people_ex, page(f'<a href="https://www.linkedin.com/in/x/" aria-label="{text}" title="{text}"><img alt="{text}"></a>'),
            "https://example.com/")


def test_a_page_that_is_all_hostile_text_is_read_quickly(hostile):
    quickly(webpage.extract_people_ex, hostile, "https://example.com/")


def test_a_team_page_is_still_read_correctly():
    html = page('<div class="card"><h3>Jane Doe</h3><p>CTO at Acme</p><a href="https://www.linkedin.com/in/jane-doe/">in</a></div>'
                '<div class="card"><h3>John Smith</h3><p>CEO at Acme</p><a href="https://www.linkedin.com/in/john-smith/">in</a></div>')
    rows, stats = webpage.extract_people_ex(html, "https://example.com/team")
    assert {r["full_name"]: r["linkedin_url"] for r in rows} == {
        "Jane Doe": "https://www.linkedin.com/in/jane-doe/", "John Smith": "https://www.linkedin.com/in/john-smith/"}
    assert stats == {"links": 2, "nameless": 0}


def test_a_page_whose_elements_are_nested_thousands_deep_is_read_quickly():
    """Each link has the next one inside its own wrapper: 'who else is in here?' asked of every ancestor of every link is
    quadratic (it took minutes for 5,000 levels)."""
    depth = 5000
    body = "".join(f'<div><a href="https://www.linkedin.com/in/person-{i}/">Person Number {i}</a>' for i in range(depth))
    rows, stats = quickly(webpage.extract_people_ex, page(body + "</div>" * depth), "https://example.com/")
    assert stats["links"] == depth


def test_a_page_nested_very_deep_with_one_person_is_read_quickly():
    html = page("<div>" * 20_000 + '<a href="https://www.linkedin.com/in/deep-one/">Deep One</a>' + "</div>" * 20_000)
    rows, _ = quickly(webpage.extract_people_ex, html, "https://example.com/")
    assert [r["full_name"] for r in rows] == ["Deep One"]


def test_a_page_with_a_hundred_thousand_links_is_read_quickly_and_only_in_part():
    body = "".join(f'<a href="https://www.linkedin.com/in/p-{i}/">Jane Doe</a>' for i in range(webpage.MAX_LINKS_PER_PAGE + 5000))
    rows, stats = quickly(webpage.extract_people_ex, page(body), "https://example.com/", budget=3.5)
    assert stats["links"] == webpage.MAX_LINKS_PER_PAGE and len(rows) == webpage.MAX_PEOPLE_PER_PAGE


def test_thousands_of_people_without_a_name_inside_deeply_nested_wrappers_are_read_quickly():
    """Each of them is looked at inside its own few wrappers; asking a CSS engine to do that looks up through every wrapper of
    the page each time."""
    depth = 3000
    body = "<div>" * depth + "".join(f'<div><a href="https://www.linkedin.com/in/q{i}/"><img></a></div>' for i in range(3000)) + "</div>" * depth
    rows, stats = quickly(webpage.extract_people_ex, page(body), "https://example.com/", budget=3)
    assert rows == [] and stats == {"links": 3000, "nameless": 3000}


def test_one_profile_linked_ten_thousand_times_with_no_name_anywhere_is_read_quickly():
    body = "<div><div>" + '<a href="https://www.linkedin.com/in/x/"><img></a>' * 10_000 + "</div></div>"
    rows, stats = quickly(webpage.extract_people_ex, page(body), "https://example.com/", budget=3)
    assert rows == [] and stats == {"links": 1, "nameless": 1}


def test_the_name_inside_a_card_is_searched_for_once_however_many_links_share_it(monkeypatch):
    calls, real = [], webpage._find_name
    monkeypatch.setattr(webpage, "_find_name", lambda node: calls.append(node) or real(node))
    body = "<div><div>" + '<a href="https://www.linkedin.com/in/x/"><img></a>' * 300 + "</div></div>"
    rows, stats = webpage.extract_people_ex(page(body), "https://example.com/")
    assert rows == [] and stats == {"links": 1, "nameless": 1}
    assert 1 <= len(calls) <= 4 and len({id(node) for node in calls}) == len(calls)       # each element once, not once per link


def test_a_name_far_inside_a_huge_card_is_not_searched_for():
    far = BeautifulSoup("<div>" + "<i></i>" * (webpage.MAX_NODES_PER_CARD + 10) + "<h3>Jane Doe</h3></div>", "lxml").div
    near = BeautifulSoup("<div>" + "<i></i>" * 100 + "<h3>Jane Doe</h3></div>", "lxml").div
    assert webpage._find_name(far) == "" and webpage._find_name(near) == "Jane Doe"


@pytest.mark.parametrize("script", ["[" * 100_000 + "]" * 100_000, '{"a":' * 50_000 + "1" + "}" * 50_000, "[" * 5000])
def test_json_that_is_nested_too_deeply_is_skipped_not_fatal(script):
    html = page("", head=f'<script type="application/ld+json">{script}</script>')
    rows, _ = quickly(webpage.extract_people_ex, html, "https://example.com/")
    assert rows == []


def test_ordinary_json_ld_still_yields_a_person():
    data = {"@type": "Person", "name": "Jane Doe", "jobTitle": "CTO", "sameAs": "https://www.linkedin.com/in/jane-doe/"}
    rows, _ = webpage.extract_people_ex(page("", head=f'<script type="application/ld+json">{json.dumps(data)}</script>'), "https://example.com/")
    assert [(r["full_name"], r["title"]) for r in rows] == [("Jane Doe", "CTO")]


# ----------------------------------------------------------------------------- the YC pages
def founder_card(i: int) -> str:
    return (f'<div class="ycdc-card-new"><div class="text-xl font-bold">Founder Number {i}</div>'
            f'<div class="text-gray-600">CEO</div><a aria-label="LinkedIn profile" href="https://www.linkedin.com/in/f{i}/">in</a></div>')


def old_markup_person(i: int) -> str:
    return f'<div><span class="font-bold">Founder {i}</span><a href="https://www.linkedin.com/in/y{i}/">x</a></div>'


def test_a_yc_page_with_thousands_of_cards_is_read_quickly_and_only_in_part():
    founders = quickly(yc_parser.parse_founders, page("".join(founder_card(i) for i in range(3000))), budget=3)
    assert len(founders) == yc_parser.MAX_CARDS and founders[0].full_name == "Founder Number 0"


def test_a_yc_page_with_tens_of_thousands_of_siblings_before_the_cards_is_read_quickly():
    """Looking for the heading above a card walks back over its earlier siblings - for every card."""
    html = page("<p></p>" * 100_000 + "".join(founder_card(i) for i in range(400)))
    founders = quickly(yc_parser.parse_founders, html, budget=2.5)
    assert len(founders) == yc_parser.MAX_CARDS


def test_a_yc_page_with_cards_inside_cards_inside_cards_is_read_quickly():
    """Every level of the nest contains everything below it: asking each one 'is there a company link in you?' is quadratic."""
    html = page('<div class="ycdc-card-new">' * 400 + "<i></i>" * 30_000 + "</div>" * 400)
    assert quickly(yc_parser.parse_founders, html, budget=3) == []


def test_a_yc_heading_with_tens_of_thousands_of_elements_in_it_is_not_read_again_for_every_card():
    html = page('<div class="text-2xl font-bold">' + "<i></i>" * 100_000 + "Founders</div>"
                + "".join(founder_card(i) for i in range(400)))
    founders = quickly(yc_parser.parse_founders, html, budget=3.5)
    assert len(founders) == yc_parser.MAX_CARDS and all(f.founder_status == "unknown" for f in founders)     # (the words come too late to be read)


def test_the_text_of_an_element_is_the_text_at_its_start():
    far = BeautifulSoup("<div>" + "<i></i>" * (yc_parser.MAX_NODES_PER_CARD + 10) + "Founders</div>", "lxml").div
    near = BeautifulSoup("<div>" + "<i></i>" * 100 + "  Active\n\t Founders </div>", "lxml").div
    long = BeautifulSoup("<div>" + "word " * 1000 + "</div>", "lxml").div
    assert yc_parser._text(far) == "" and yc_parser._text(near) == "Active Founders"
    assert len(yc_parser._text(long)) == yc_parser.MAX_TEXT and yc_parser._text(long, 12) == "word word wo"
    assert yc_parser._text(None) == ""


def test_comments_scripts_and_styles_are_not_part_of_the_text_of_a_card():
    card = BeautifulSoup("<div>Jane <!-- secret --><script>var a = 1;</script><style>p {color: red}</style>Doe <b>CEO</b></div>", "lxml").div
    assert yc_parser._text(card) == card.get_text(" ", strip=True) == "Jane Doe CEO"


def test_a_link_beyond_the_part_of_a_card_that_is_read_is_not_found():
    card = BeautifulSoup("<div>" + "<i></i>" * (yc_parser.MAX_NODES_PER_CARD + 10) + '<a href="https://www.linkedin.com/in/x/">in</a></div>', "lxml").div
    near = BeautifulSoup('<div><a href="https://www.linkedin.com/in/x/">in</a></div>', "lxml").div
    assert yc_parser._social(card, ("linkedin.com",)) == "" and yc_parser._social(near, ("linkedin.com",)).endswith("/in/x/")


def test_a_yc_page_in_the_old_markup_with_thousands_of_links_is_read_quickly_and_only_in_part():
    founders = quickly(yc_parser.parse_founders, page("".join(old_markup_person(i) for i in range(3000))), budget=3)
    assert len(founders) == yc_parser.MAX_CARDS and founders[0].parser == "fallback"


def test_a_yc_page_in_the_old_markup_with_every_link_in_one_big_container_is_read_quickly():
    body = "<div>" + "".join(f'<a href="https://www.linkedin.com/in/y{i}/">x</a>' for i in range(2000)) + "<i></i>" * 60_000 + "</div>"
    assert quickly(yc_parser.parse_founders, page(body), budget=3) == []


def test_a_profile_link_with_no_name_near_it_does_not_borrow_one_from_elsewhere_on_the_page():
    html = page('<div class="font-bold">Acme Pay Careers</div><a href="https://www.linkedin.com/in/someone/">in</a>')
    assert yc_parser.parse_founders(html) == []
    assert [f.full_name for f in yc_parser.parse_founders(page(old_markup_person(1)))] == ["Founder 1"]


def test_a_yc_page_nested_very_deep_is_read_quickly():
    quickly(yc_parser.parse_founders, page("<div>" * 20_000 + "</div>" * 20_000), budget=3)


def test_a_normal_yc_company_page_still_gives_its_founders():
    html = page('<div class="text-2xl font-bold">Active Founders</div>' + founder_card(1) + founder_card(2))
    assert [f.full_name for f in yc_parser.parse_founders(html)] == ["Founder Number 1", "Founder Number 2"]


# ----------------------------------------------------------------------------- note templates
def test_a_hostile_template_is_rendered_quickly(hostile):
    ctx = notes.build_context({"first_name": "Jane", "company": "Acme", "title": "CEO", "full_name": "Jane Doe"}, {"first_name": "Asha"})
    quickly(notes.fit, hostile, ctx, 300)
    quickly(notes.compose, [hostile], ctx, 300)
    quickly(notes.required_vars, hostile)
    quickly(notes.template_problems, hostile)
    quickly(notes.upgrade_v1_template, hostile)


def test_hostile_values_in_a_template_are_rendered_quickly(hostile):
    ctx = notes.build_context({"first_name": "Jane", "company": hostile, "title": hostile, "full_name": "Jane Doe"}, {"first_name": "Asha", "about": hostile})
    quickly(notes.fit, "Hi {first_name}, I'm {me_first_name}, {me_about}[[ - {title} at {company}]].", ctx, 300)


@pytest.mark.parametrize("template,expected", [
    ("a [[b]] c [[d [[e]] f", [(2, 7, "b"), (10, 19, "d [[e")]),
    ("[[ [[ [[", []), ("x]] [[y]]", [(4, 9, "y")]), ("[[]]", [(0, 4, "")]), ("no segments", []),
    ("[[a]][[b]]", [(0, 5, "a"), (5, 10, "b")]),
])
def test_optional_segments_run_to_the_first_closing_brackets(template, expected):
    assert notes.optional_segments(template) == expected


def test_a_template_with_many_unclosed_openers_is_scanned_once():
    segments = quickly(notes.optional_segments, "[[a" * 200_000 + "]]")
    assert segments == [(0, 3 * 200_000 + 2, "a" + "[[a" * 199_999 + "[[a")] or len(segments) == 1


def test_a_template_with_optional_parts_still_renders_and_shortens():
    ctx = notes.build_context({"first_name": "Jane", "company": "Acme", "full_name": "Jane Doe"}, {"first_name": "Asha"})
    template = "Hi {first_name}, I'm {me_first_name}[[ from {company}]][[ and I would love to connect with you]]."
    assert notes.fit(template, ctx, 300) == "Hi Jane, I'm Asha from Acme and I would love to connect with you."
    assert notes.fit(template, ctx, 40) == "Hi Jane, I'm Asha from Acme."[:40] or len(notes.fit(template, ctx, 40)) <= 40
    assert notes.required_vars("Hi {first_name}[[ {company}]] {title|there}") == {"first_name"}


def test_the_v1_template_upgrade_still_works_and_is_bounded():
    assert notes.upgrade_v1_template("Hi {first_name} - {project_blurb}") == "Hi {first_name}[[ - {me_project_blurb}]]"
    assert "{me_project}" in notes.upgrade_v1_template("I am building {project}")
    quickly(notes.upgrade_v1_template, " " * N + "- {project_blurb}")


# ----------------------------------------------------------------------------- addresses
def test_a_hostile_address_is_normalised_quickly(hostile):
    quickly(normalize_linkedin, hostile)
    quickly(normalize_linkedin, "https://www.linkedin.com/in/" + hostile)
    quickly(normalize_linkedin, "https://" + hostile + ".linkedin.com/in/x")


@pytest.mark.parametrize("line,found", [
    ("https://www.linkedin.com/in/jane-doe/", True),
    ("Jane Doe, www.linkedin.com/in/jane-doe", True),
    ("see (linkedin.com/in/jane-doe) for more", True),
    ("name:linkedin.com/in/jane-doe", True),
    ("https://uk.linkedin.com/in/jane-doe", True),
    ("xlinkedin.com/in/jane-doe", False),
    ("notlinkedin.com/in/jane-doe", False),
    ("evil-linkedin.com/in/jane-doe", False),
    ("https://example.com/in/jane-doe", False),
])
def test_only_a_linkedin_host_makes_a_profile_address_in_pasted_text(line, found):
    people, _ = manual.parse_people_text(line)
    assert bool(people) is found


# ----------------------------------------------------------------------------- contact details
CONTACT_TEXT = {
    "at signs": "a@" * (N // 2),
    "an address that never ends": "a@" + "a." * (N // 2),
    "a very long local part": "a" * N + "@acme.com",
    "bracketed at": "a [at] " * (N // 7),
    "spelled-out at and dot": "a at b dot " * (N // 11),
    "the words at and dot": "at dot " * (N // 7),
    "plus signs": "+1 " * (N // 3),
    "plus and a long run of digits": "+" + "1" * N,
    "groups of digits": "123 456 789 " * (N // 12),
    "digits and dashes in threes": "123-" * (N // 4),
    "area codes": "(415) 555 " * (N // 10),
    "www over and over": "www." * (N // 4),
    "mailto over and over": "mailto:" * (N // 7),
    "a profile site over and over": "x.com/" * (N // 6),
    "a site name and spaces": "linkedin.com " * (N // 13),
    "many labels then a profile site": "a." * (N // 2) + "linkedin.com/in/x",
    "brackets": "[(" * (N // 2),
    "separators": "|,;" * (N // 3),
    "empty brackets": "( )" * (N // 3),
}
EVERY_TEXT = {**BATTERY, **CONTACT_TEXT}


@pytest.fixture(params=list(EVERY_TEXT))
def any_text(request):
    return EVERY_TEXT[request.param]


def test_hostile_text_is_scrubbed_of_contact_details_quickly_and_in_part(any_text):
    out = quickly(privacy.scrub_text, any_text)
    assert len(out) <= privacy.MAX_CHARS
    assert quickly(privacy.scrub_text, out) == out                 # (scrubbing what is left finds nothing more)
    quickly(privacy.has_contact_details, any_text)


def test_hostile_text_in_every_field_of_a_person_is_scrubbed_quickly(any_text):
    fields = {name: any_text for name in ("full_name", "title", "company", "headline", "bio", "one_liner", "tags", "x")}
    def build():
        return make_person(linkedin_url="https://www.linkedin.com/in/jane-doe/", **fields)

    row = quickly(build)
    assert row["linkedin_url"] == "https://www.linkedin.com/in/jane-doe/"
    quickly(privacy.scrub_person, dict(fields, linkedin_url="x"))
    ctx = quickly(notes.build_context, dict(fields, first_name=any_text), {"first_name": "Asha"})
    assert len(ctx["bio"]) <= privacy.MAX_CHARS


def test_hostile_names_links_and_field_names_are_handled_quickly(any_text):
    person = {"full_name": any_text, "first_name": any_text, "last_name": any_text}
    quickly(privacy.name_words, person)
    quickly(privacy.without_name, any_text, person)
    quickly(privacy.clean_link, any_text)
    quickly(privacy.clean_link, "https://" + any_text)
    quickly(privacy.clean_link, "https://acme.com/" + any_text[:1500])
    quickly(privacy.is_contact_field, any_text)
    quickly(privacy.is_link_field, any_text)


def test_a_detail_far_into_a_long_text_is_cut_off_not_found_slowly():
    text = "a " * 10_000 + "jane@acme.com"
    out = quickly(privacy.scrub_text, text, budget=0.1)
    assert "jane@acme.com" not in out and len(out) <= privacy.MAX_CHARS


def test_a_detail_after_a_long_word_is_still_found():
    assert privacy.scrub_text("a" * 3000 + " jane@acme.com now") == "a" * 3000 + " now"
    assert privacy.scrub_text("x " * 1000 + "+1 415 555 0100 end") == "x " * 1000 + "end"


# ----------------------------------------------------------------------------- spreadsheets that are mostly nothing
LINK = b"https://www.linkedin.com/in/jane-doe/"
BIG_FILES = {
    "twenty million commas": lambda: b"," * 20_000_000,
    "commas on many lines": lambda: (b"," * 150_000 + b"\n") * 130,
    "a header of a hundred thousand columns": lambda: (",".join(f"h{i}" for i in range(100_000)).encode() + b"\nJane," + LINK + b"\n"),
    "one row that runs over four million lines": lambda: b'"x\n",' * 4_000_000,
    "semicolons and tabs and bars": lambda: (b";\t|" * 3_000_000)[:20_000_000],
    "quotes": lambda: b'""' * 10_000_000,
    "carriage returns": lambda: b"\r" * 20_000_000,
    "a very long name": lambda: b"Jane," + b"a" * 20_000_000 + b"," + LINK + b"\n",
    "empty lines": lambda: b"\n" * 20_000_000,
}


@pytest.mark.parametrize("name", list(BIG_FILES))
def test_a_twenty_megabyte_spreadsheet_with_hardly_anything_in_it_is_read_quickly(tmp_path, name):
    path = tmp_path / "leads.csv"
    path.write_bytes(BIG_FILES[name]())
    quickly(csv_file.read_csv_rows, path, budget=3.0)


def test_an_unclosed_quote_is_a_plain_error_and_quick(tmp_path):
    path = tmp_path / "leads.csv"
    path.write_bytes(b'name,url\n"Jane,' + LINK + b"\n" + b"more text\n" * 2_000_000)
    result = quickly(csv_file.read_csv_rows, path, budget=3.0)
    assert isinstance(result, ValueError) and "quote" in str(result)


def test_a_big_honest_spreadsheet_is_still_read_in_full(tmp_path):
    lines = ["Name,LinkedIn,Company,Title"] + [f"Person {i},https://www.linkedin.com/in/person-{i}/,Co {i},CEO" for i in range(30_000)]
    path = tmp_path / "leads.csv"
    path.write_text("\n".join(lines), encoding="utf-8")
    rows, notes = quickly(csv_file.read_csv_rows, path, budget=5.0)
    assert len(rows) == 30_000 and notes == []
