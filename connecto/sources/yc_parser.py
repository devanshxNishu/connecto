"""Pure parsing helpers for YC company pages. No network access here, so it is unit-testable.

Observed structure of https://www.ycombinator.com/companies/<slug> (Oct 2026):

    <div class="my-4 text-2xl font-bold ...">Active Founders</div>      <- section heading
    <div class="ycdc-card-new ...">                                     <- one card per founder
        <div class="hidden gap-4 md:flex">                              <- desktop layout
            <div class="min-w-0 flex-1">
                <div class="text-xl font-bold">Paul Witten</div>        <- name
                <a aria-label="Twitter account" href="https://x.com/...">
                <a aria-label="LinkedIn profile" href="https://linkedin.com/in/...">
                <div class="text-gray-600">Founder/CEO</div>            <- title
                <div class="prose ...">bio</div>
        <div class="group space-y-3 md:hidden">                         <- mobile duplicate
            <div class="text-lg font-bold">Paul Witten</div>
            <div class="text-sm text-gray-600">Founder/CEO</div>

Other `.ycdc-card-new` cards exist on the page (company header card with an
`a[aria-label="Company website"]`, "Company Launches" cards, news cards) and must be ignored.
Top companies (Stripe, Coinbase, ...) have founder cards but no "Active Founders" heading.
"""
from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, asdict
from typing import Iterable

from bs4 import BeautifulSoup, Tag
from bs4.element import CData, NavigableString

from ..models import LINKEDIN_PERSONAL_PATHS, linkedin_key, normalize_linkedin  # noqa: F401  (re-exported)
from ..util import split_first_name  # noqa: F401  (re-exported)

BATCH_SEASON_CODES = {"winter": "W", "spring": "X", "summer": "S", "fall": "F"}
SEASON_FROM_CODE = {v: k.capitalize() for k, v in BATCH_SEASON_CODES.items()}


@dataclass
class Founder:
    full_name: str
    title: str = ""
    bio: str = ""
    linkedin_url: str = ""
    twitter_url: str = ""
    founder_status: str = "active"  # active | former | unknown
    parser: str = "card"            # card | fallback

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- helpers
def batch_short(batch: str) -> str:
    """'Summer 2026' -> 'S26', 'Winter 2025' -> 'W25', 'Spring 2025' -> 'X25', 'Fall 2025' -> 'F25'."""
    m = re.match(r"^\s*(winter|spring|summer|fall)\s+(\d{4})\s*$", batch or "", re.I)
    if not m:
        return (batch or "").strip()
    return f"{BATCH_SEASON_CODES[m.group(1).lower()]}{m.group(2)[2:]}"


def batch_long(code: str) -> str:
    """'S26' -> 'Summer 2026'. Full names pass through unchanged."""
    s = (code or "").strip()
    m = re.match(r"^([WXSF])(\d{2})$", s, re.I)
    if m:
        season = SEASON_FROM_CODE[m.group(1).upper()]
        return f"{season} 20{m.group(2)}"
    m = re.match(r"^(winter|spring|summer|fall)\s*[-_ ]?\s*(\d{4})$", s, re.I)
    if m:
        return f"{m.group(1).capitalize()} {m.group(2)}"
    return s


def parse_batch_list(spec: str | Iterable[str] | None) -> set[str]:
    """Accepts 'S26,Fall 2026, w25' or a list; returns full batch names. Empty -> empty set (= all)."""
    if not spec:
        return set()
    if isinstance(spec, str):
        items = re.split(r"[,;]", spec)
    else:
        items = list(spec)
    return {batch_long(x) for x in items if x and x.strip()}


# Every question asked about a card looks at a bounded part of it, once. A page is somebody else's text: cards inside cards
# inside cards, a heading with a hundred thousand empty elements in it - asking a CSS engine, or reading "all the text of
# this element", for each card of such a page takes minutes.
MAX_CARDS = 300                 # a company page has a handful of founders; more cards than this are not read
MAX_NEIGHBOURS = 100            # how many earlier siblings are looked at for a heading
MAX_NODES_PER_CARD = 500        # how much of one card (or of the container found by climbing) is looked at
MAX_TEXT = 1_000                # characters of text taken from one element
_TEXT_TYPES = (NavigableString, CData)       # what get_text() reads: not comments, scripts or style sheets


def _nodes(el: Tag):
    return itertools.islice(el.descendants, MAX_NODES_PER_CARD)


def _text(el: Tag | None, limit: int = MAX_TEXT) -> str:
    """The text at the start of an element, whitespace collapsed: its first MAX_NODES_PER_CARD nodes, its first `limit` characters."""
    if el is None:
        return ""
    pieces, size = [], 0
    for node in _nodes(el):
        if type(node) in _TEXT_TYPES:
            piece = " ".join(str(node).split())
            if piece:
                pieces.append(piece)
                size += len(piece) + 1
                if size > limit:
                    break
    return " ".join(pieces)[:limit]


def _has_class(el: Tag, *classes: str) -> bool:
    cls = set(el.get("class") or [])
    return all(c in cls for c in classes)


def _nearest_heading(card: Tag) -> str:
    """Text of the closest preceding `div.text-2xl.font-bold` heading, walking up the ancestors."""
    anc: Tag | None = card
    for _ in range(10):
        if anc is None:
            break
        for sib in itertools.islice(anc.previous_siblings, MAX_NEIGHBOURS):
            if isinstance(sib, Tag):
                if sib.name == "div" and _has_class(sib, "text-2xl", "font-bold"):
                    return _text(sib)
                if sib.name in ("h2", "h3"):
                    return _text(sib)
        anc = anc.parent if isinstance(anc.parent, Tag) else None
    return ""


def _first(tags: list[Tag], *tests) -> Tag | None:
    """The first tag (document order) that passes the first test that any tag passes - like trying CSS selectors in turn."""
    for test in tests:
        found = next((t for t in tags if test(t)), None)
        if found is not None:
            return found
    return None


def _div(*classes: str):
    return lambda t: t.name == "div" and _has_class(t, *classes)


def _link_labelled(*labels: str):
    return lambda t: t.name == "a" and t.get("aria-label") in labels


def _social(el: Tag, hosts: tuple[str, ...]) -> str:
    for a in _nodes(el):
        href = (a.get("href") or "") if isinstance(a, Tag) and a.name == "a" else ""
        if href and any(h in href.lower() for h in hosts):
            return href.strip()
    return ""


# --------------------------------------------------------------------------- main entry
def parse_founders(html: str) -> list[Founder]:
    soup = BeautifulSoup(html, "lxml") if _lxml_available() else BeautifulSoup(html, "html.parser")
    founders = _parse_cards(soup)
    if not founders:
        founders = _parse_fallback(soup)
    return _dedupe(founders)


def _parse_cards(soup: BeautifulSoup) -> list[Founder]:
    out: list[Founder] = []
    for card in itertools.islice(soup.select(".ycdc-card-new"), MAX_CARDS):
        inside = [n for n in _nodes(card) if isinstance(n, Tag)]
        if _first(inside, _link_labelled("Company website")) is not None:
            continue  # company header card
        name_el = _first(inside, _div("text-xl", "font-bold"), _div("text-lg", "font-bold"))
        if name_el is None:
            continue
        title_el = _first(inside, _div("text-gray-600"), _div("text-sm", "text-gray-600"))
        social_el = _first(inside, _link_labelled("LinkedIn profile", "Twitter account"))
        if title_el is None and social_el is None:
            continue  # "Company Launches" / news cards also have a bold title
        heading = _nearest_heading(card)
        if heading and "founder" not in heading.lower():
            continue  # a card under "Latest News", "Jobs", "Company Launches", ...
        status = "former" if "former" in heading.lower() else ("active" if heading else "unknown")
        out.append(
            Founder(
                full_name=_text(name_el),
                title=_text(title_el),
                bio=_text(_first(inside, _div("prose"))),
                linkedin_url=normalize_linkedin(_social(card, ("linkedin.com",))),
                twitter_url=_social(card, ("x.com/", "twitter.com/")),
                founder_status=status,
                parser="card",
            )
        )
    return out


def _parse_fallback(soup: BeautifulSoup) -> list[Founder]:
    """If YC redesigns the card markup: anchor on personal LinkedIn links and climb to a name."""
    out: list[Founder] = []
    bold = re.compile(r"font-bold")
    for a in itertools.islice(soup.select('a[href*="linkedin.com/in/"], a[href*="linkedin.com/pub/"]'), MAX_CARDS):
        container: Tag | None = a
        for _ in range(7):
            parent = container.parent if container is not None else None
            if not isinstance(parent, Tag) or parent.name in ("body", "html", "[document]"):
                break                       # never the whole page: that is not a card (its first bold text would name every link)
            container = parent
            if len(_text(container, 26)) > 25:
                break
        if container is None:
            continue
        name_el = next((n for n in _nodes(container)
                        if isinstance(n, Tag) and n.name in ("div", "span", "h2", "h3", "h4", "strong")
                        and bold.search(" ".join(n.get("class") or []))), None)
        name = _text(name_el) if name_el else ""
        if not name:
            img = next((n for n in _nodes(container) if isinstance(n, Tag) and n.name == "img" and n.get("alt")), None)
            name = (img.get("alt") or "").strip() if img else ""
        if not name or len(name) > 80:
            continue
        out.append(
            Founder(
                full_name=name,
                linkedin_url=normalize_linkedin(a.get("href")),
                twitter_url=_social(container, ("x.com/", "twitter.com/")),
                founder_status="unknown",
                parser="fallback",
            )
        )
    return out


def _dedupe(founders: list[Founder]) -> list[Founder]:
    seen: set[str] = set()
    out: list[Founder] = []
    for f in founders:
        key = linkedin_key(f.linkedin_url) or f.full_name.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


def _lxml_available() -> bool:
    try:
        import lxml  # noqa: F401
        return True
    except ImportError:
        return False
