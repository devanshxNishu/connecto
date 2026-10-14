from __future__ import annotations

import pathlib

from connecto import models, util
from connecto.sources import yc_parser
from connecto.sources.yc_parser import (
    batch_long,
    batch_short,
    linkedin_key,
    normalize_linkedin,
    parse_batch_list,
    parse_founders,
    split_first_name,
)

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "company_page.html"


def test_parse_founders_from_card_layout():
    founders = parse_founders(FIXTURE.read_text())
    by_name = {f.full_name: f for f in founders}

    # Only the three founder cards: company header, launch and news cards are ignored,
    # and the desktop/mobile duplicate of each founder collapses to one record.
    assert set(by_name) == {"Paul Witten", "Lucas Irvine", "Dr. Maya Okafor-Lind"}

    paul = by_name["Paul Witten"]
    assert paul.title == "Founder/CEO"
    assert paul.linkedin_url == "https://www.linkedin.com/in/paul-witten-39579715a/"
    assert paul.twitter_url == "https://x.com/pdubz96"
    assert paul.founder_status == "active"
    assert paul.bio.startswith("Cofounder/CEO Tire Swing")
    assert paul.parser == "card"

    lucas = by_name["Lucas Irvine"]
    assert lucas.linkedin_url == "https://www.linkedin.com/in/lucas-irvine/"  # query string stripped
    assert lucas.title == "Founder/CTO"

    maya = by_name["Dr. Maya Okafor-Lind"]
    assert maya.founder_status == "former"
    assert maya.linkedin_url == ""  # no LinkedIn on the card -> kept, but flagged for skipping


def test_company_linkedin_page_is_not_a_founder():
    founders = parse_founders(FIXTURE.read_text())
    assert all("linkedin.com/company/" not in f.linkedin_url for f in founders)


def test_fallback_parser_when_cards_are_gone():
    html = """
    <html><body>
      <section>
        <div class="founder">
          <img alt="Jane Doe" src="x.png">
          <span class="name font-bold">Jane Doe</span>
          <a href="https://in.linkedin.com/in/janedoe?trk=abc">LinkedIn</a>
          <a href="https://twitter.com/janedoe">X</a>
        </div>
      </section>
    </body></html>"""
    founders = parse_founders(html)
    assert len(founders) == 1
    assert founders[0].full_name == "Jane Doe"
    assert founders[0].linkedin_url == "https://www.linkedin.com/in/janedoe/"
    assert founders[0].parser == "fallback"


def test_normalize_linkedin_variants():
    assert normalize_linkedin("https://linkedin.com/in/paul-witten-39579715a") == "https://www.linkedin.com/in/paul-witten-39579715a/"
    assert normalize_linkedin("http://www.linkedin.com/in/brianchesky/") == "https://www.linkedin.com/in/brianchesky/"
    assert normalize_linkedin("linkedin.com/in/someone") == "https://www.linkedin.com/in/someone/"
    assert normalize_linkedin("https://uk.linkedin.com/in/someone/?originalSubdomain=uk") == "https://www.linkedin.com/in/someone/"
    assert normalize_linkedin("https://www.linkedin.com/company/airbnb/") == ""
    assert normalize_linkedin("https://www.linkedin.com/school/mit/") == ""
    assert normalize_linkedin("https://twitter.com/someone") == ""
    assert normalize_linkedin("") == ""
    assert normalize_linkedin(None) == ""
    assert normalize_linkedin("https://www.linkedin.com/pub/john-smith/1a/2b3/4c5") == "https://www.linkedin.com/pub/john-smith/1a/2b3/4c5/"


def test_first_name():
    assert split_first_name("Paul Witten") == "Paul"
    assert split_first_name("Dr. Maya Okafor-Lind") == "Maya"
    assert split_first_name("  Nathan   Blecharczyk ") == "Nathan"
    assert split_first_name("Cher") == "Cher"
    assert split_first_name("") == ""


def test_batch_codes():
    assert batch_short("Summer 2026") == "S26"
    assert batch_short("Winter 2025") == "W25"
    assert batch_short("Spring 2025") == "X25"
    assert batch_short("Fall 2025") == "F25"
    assert batch_short("Unspecified") == "Unspecified"
    assert batch_long("S26") == "Summer 2026"
    assert batch_long("x25") == "Spring 2025"
    assert batch_long("Fall 2026") == "Fall 2026"
    assert batch_long("summer-2025") == "Summer 2025"
    assert parse_batch_list("S26, Fall 2026 ;w25") == {"Summer 2026", "Fall 2026", "Winter 2025"}
    assert parse_batch_list("") == set()
    assert parse_batch_list(None) == set()
    assert parse_batch_list(["S26", "F26"]) == {"Summer 2026", "Fall 2026"}


def test_shared_helpers_are_the_foundation_ones():
    # yc_parser no longer defines its own copies: the names stay importable from here, but they are the shared functions.
    assert yc_parser.normalize_linkedin is models.normalize_linkedin
    assert yc_parser.linkedin_key is models.linkedin_key
    assert yc_parser.split_first_name is util.split_first_name
    assert linkedin_key("https://www.linkedin.com/in/Paul-Witten/") == linkedin_key("linkedin.com/in/paul-witten")
