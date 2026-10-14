"""People from LinkedIn's own people search, read through the same logged-in browser that sends the invitations.

You can give plain keyword queries, or - the most flexible way to use LinkedIn's filters (location, industry, current
company, ...) - paste the URL of a people search you set up yourself. Connecto only *reads* the result pages, slowly:
a handful of pages per run and a hard daily page budget. It stops the moment LinkedIn asks for a verification.

LinkedIn randomises its class names, so nothing here is matched by CSS class. The page is read as text: every link to a
profile inside ``<main>`` with its ``innerText`` (see ``EXTRACT_JS``), and ``parse_result_cards`` - a pure function -
turns those lines into people. If LinkedIn changes the layout, that function and ``tests/mock_search.py`` are what to fix.
"""
from __future__ import annotations

import random
import re
from datetime import datetime
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

from ..browser import (DEFAULT_BASE_URL, LoggedOut, NeedsAttention, at_checkpoint, close_browser, ensure_logged_in,
                       human_pause, human_scroll, launch_browser, looks_logged_out)
from ..limits import HARD
from ..models import linkedin_key, make_person, normalize_linkedin, person_key
from ..util import clean_text
from .base import Sink, Source, float_pair, split_headline

SEARCH_PATH = "/search/results/people/"

# Every link to a profile inside <main>, with the text a person would see on it. Class names are never used.
EXTRACT_JS = """() => Array.from(document.querySelectorAll('main a[href*="/in/"]'))
  .map(a => ({href: a.href, text: (a.innerText || '').trim()}))"""

_NO_RESULTS = re.compile(r"\bno results found\b", re.I)
_DEGREE_LINE = re.compile(r"^[•·\-–]?\s*(1st|2nd|3rd)\+?(?:\s+degree connection)?\s*$", re.I)
_NAME_AND_DEGREE = re.compile(r"^(?P<name>.+?)\s*[•·]\s*(?P<deg>1st|2nd|3rd)\+?(?:\s+degree connection)?\s*$", re.I)
_CURRENT = re.compile(r"^current\s*:\s*(.+)$", re.I)
_LABELLED = re.compile(r"^(?:past|previous|skills|summary|education|about|experience)\s*:", re.I)
_NOISE = [re.compile(p, re.I) for p in (
    r"^view\b.*\bprofile\b",                        # View Asha's profile / View Asha Verma’s full profile
    r"^status is\b",                                # Status is online / reachable / offline
    r"^[•·]?\s*premium\b", r"^verified\b", r"^open to work\b", r"^#?opentowork$", r"^hiring$",
    r"\bmutual connections?\b", r"\bis a mutual connection\b", r"\bfollows? this (person|member)\b",
    r"^[\d.,]+\s*[kKmM]?\+?\s+followers?$", r"^(following|follow|connect|message|pending|send message|save)$",
    r"^(provides|offers) services\b", r"^\d+\s+(other\s+)?(shared\s+)?connections?$",
)]


# ----------------------------------------------------------------------------- URLs
def validate_search_url(url: str) -> str:
    """The URL (fragment removed) if it is a LinkedIn people-search page, else ValueError."""
    text = str(url or "").strip()
    if not text or any(ord(c) < 33 for c in text):
        raise ValueError("not a valid URL")
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise ValueError("not a valid URL") from None
    if (parts.scheme.lower() != "https" or "@" in parts.netloc or (parts.hostname or "").lower() != "www.linkedin.com"
            or port not in (None, 443)):
        raise ValueError("only https://www.linkedin.com/search/results/people/... URLs are accepted")
    path = parts.path
    # Exactly the people-search page. A browser turns "\" into "/" and resolves "." and ".." (also when encoded), so any
    # longer path could be made to open some other LinkedIn page; everything a search needs is in the query.
    if path not in (SEARCH_PATH, SEARCH_PATH.rstrip("/")):
        raise ValueError("this is not a LinkedIn people search (expected /search/results/people/?keywords=...)")
    return urlunsplit(("https", "www.linkedin.com", path, parts.query, ""))


def build_search_url(query: str, page: int = 1, base_url: str = DEFAULT_BASE_URL) -> str:
    """The people-search URL for a keyword query; ``page`` > 1 adds ``&page=N``."""
    q = clean_text(query)
    if not q:
        raise ValueError("empty search query")
    url = f"{base_url.rstrip('/')}{SEARCH_PATH}?keywords={quote(q, safe='')}&origin=GLOBAL_SEARCH_HEADER"
    if page and int(page) > 1:
        url += f"&page={int(page)}"
    return url


def with_page(url: str, page: int) -> str:
    """The same search on another result page: only ``page`` is set/replaced, every other parameter is kept as it was."""
    parts = urlsplit(url)
    items = [p for p in parts.query.split("&") if p and p.split("=", 1)[0] != "page"]
    if page and int(page) > 1:
        items.append(f"page={int(page)}")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "&".join(items), ""))


def _rebase(url: str, base_url: str) -> str:
    """Point a linkedin.com URL at ``base_url`` (a no-op in production; lets tests aim at a local stand-in)."""
    if base_url.rstrip("/") == DEFAULT_BASE_URL:
        return url
    parts, base = urlsplit(url), urlsplit(base_url)
    return urlunsplit((base.scheme, base.netloc, parts.path, parts.query, ""))


def _label_for_url(url: str) -> str:
    m = re.search(r"(?:^|[?&])keywords=([^&]*)", urlsplit(url).query)
    return clean_text(unquote(m.group(1).replace("+", " "))) if m and m.group(1) else "linkedin search url"


# ----------------------------------------------------------------------------- parsing (pure)
def _profile_url(href: str, base_url: str) -> str:
    if not href:
        return ""
    resolved = urljoin(base_url, href)
    url = normalize_linkedin(resolved)
    if url:
        return url
    here, there = urlsplit(base_url), urlsplit(resolved)      # a stand-in host serving /in/<slug>
    if here.hostname and there.hostname == here.hostname and there.path.startswith("/in/"):
        return normalize_linkedin("https://www.linkedin.com" + there.path)
    return ""


def _parse_card_text(text: str) -> dict | None:
    lines: list[str] = []
    for raw in (text or "").splitlines():
        line = clean_text(raw)
        if line and not any(rx.search(line) for rx in _NOISE) and (not lines or line != lines[-1]):
            lines.append(line)
    if not lines:
        return None
    degree, name, details = "", "", lines[1:]
    for i, line in enumerate(lines):
        combined = _NAME_AND_DEGREE.match(line)
        if combined:
            name, degree, details = combined.group("name"), combined.group("deg"), lines[i + 1:]
            break
        m = _DEGREE_LINE.match(line)
        if m:
            name = next((x for x in lines[:i] if not _DEGREE_LINE.match(x)), "")
            degree, details = m.group(1), lines[i + 1:]
            break
    if not name:
        name, details = lines[0], lines[1:]
    if not name or name.lower() == "linkedin member" or _DEGREE_LINE.match(name):
        return None
    current, rest = "", []
    for line in details:
        c = _CURRENT.match(line)
        if line.lower() == name.lower() or _LABELLED.match(line):
            continue
        if c:
            current = current or clean_text(c.group(1))
        else:
            rest.append(line)
    return {"name": name, "degree": degree.lower(), "headline": rest[0] if rest else "",
            "location": rest[1] if len(rest) > 1 else "", "current_role": current}


def parse_result_cards(cards: list[dict], base_url: str = DEFAULT_BASE_URL, search: str = "") -> list[dict]:
    """Person rows from ``[{href, text}]`` as returned by ``EXTRACT_JS``.

    The same profile appears in several anchors (name link, avatar link, ...): the one with the most text wins.
    "LinkedIn Member" cards (people outside your network) carry no usable profile and are dropped.
    """
    best: dict[str, tuple[int, str, str]] = {}
    for card in cards or []:
        url = _profile_url(str(card.get("href") or ""), base_url)
        if not url:
            continue
        text = str(card.get("text") or "")
        key = linkedin_key(url)
        if key not in best or len(text.strip()) > best[key][0]:
            best[key] = (len(text.strip()), url, text)
    rows = []
    for _, url, text in best.values():
        parsed = _parse_card_text(text)
        if not parsed:
            continue
        title, company = split_headline(parsed["headline"])
        person = make_person(linkedin_url=url, full_name=parsed["name"], headline=parsed["headline"],
                             location=parsed["location"], degree=parsed["degree"], current_role=parsed["current_role"],
                             title=title, company=company, search=search)
        if person:
            rows.append(person)
    return rows


# ----------------------------------------------------------------------------- the source
class LinkedInSearchSource(Source):
    type = "linkedin_search"
    needs_browser = True

    def __init__(self, spec: dict, ctx):
        super().__init__(spec, ctx)
        q = spec.get("queries")
        self.queries = [clean_text(x) for x in (q.splitlines() if isinstance(q, str) else (q or [])) if clean_text(x)]
        u = spec.get("urls")
        raw_urls = [str(x).strip() for x in (u.split() if isinstance(u, str) else (u or [])) if str(x).strip()]
        self.urls: list[str] = []
        self.bad_urls: list[tuple[str, str]] = []
        for raw in raw_urls:
            try:
                self.urls.append(validate_search_url(raw))
            except ValueError as exc:
                self.bad_urls.append((raw, str(exc)))
        try:
            pages = int(spec.get("max_pages") or 3)
        except (TypeError, ValueError):
            pages = 3
        self.max_pages = max(1, min(pages, HARD["search_pages_per_run"]))
        self.skip_first_degree = str(spec.get("skip_first_degree", True)).strip().lower() in ("1", "true", "yes", "on")
        self.delay = float_pair(spec.get("delay_seconds"), (6.0, 15.0))

    # ------------------------------------------------------------------ identity
    @property
    def source_id(self) -> str:
        first = self.queries[0] if self.queries else (_label_for_url(self.urls[0]) if self.urls else "")
        return ("search:" + re.sub(r"\s+", " ", first).strip().lower()[:40]) if first else "search"

    def owns(self, row: dict) -> bool:
        label = str(row.get("search") or "")
        return not label or label in self.labels()

    def accepts(self, row: dict) -> bool:
        if not self.owns(row):
            return False
        return not (self.skip_first_degree and (row.get("degree") or "").strip().lower() == "1st")

    def labels(self) -> list[str]:
        return list(self.queries) + [_label_for_url(u) for u in self.urls]

    def describe(self) -> str:
        bits = []
        if self.queries:
            bits.append(f"{len(self.queries)} quer{'ies' if len(self.queries) != 1 else 'y'}")
        if self.urls:
            bits.append(f"{len(self.urls)} saved search URL{'s' if len(self.urls) != 1 else ''}")
        return f"LinkedIn search: {' and '.join(bits) or 'nothing set'}, up to {self.max_pages} pages each"

    def searches(self) -> list[tuple[str, object]]:
        """[(label, page_number -> URL)] for every query and pasted URL."""
        base = self.ctx.base_url
        out: list[tuple[str, object]] = []
        for q in self.queries:
            out.append((q, lambda n, q=q: build_search_url(q, n, base)))
        for url in self.urls:
            out.append((_label_for_url(url), lambda n, url=url: _rebase(with_page(url, n), base)))
        return out

    # ------------------------------------------------------------------ daily page budget
    def _budget_key(self) -> str:
        return "search_pages:" + datetime.now().strftime("%Y-%m-%d")

    def pages_today(self) -> int:
        try:
            return max(0, int(self.ctx.store.kv_get(self._budget_key(), "0")))
        except (TypeError, ValueError):
            return 0

    def _spend_page(self) -> None:
        self.ctx.store.kv_set(self._budget_key(), str(self.pages_today() + 1))

    # ------------------------------------------------------------------ the run
    def fetch(self, sink: Sink) -> None:
        report, ctx = sink.report, self.ctx
        for raw, why in self.bad_urls:
            report.errors.append(f"search URL refused ({why}): {raw[:80]}")
        searches = self.searches()
        if not searches:
            if not self.bad_urls:
                report.errors.append("this LinkedIn search source has no `queries` or `urls` set")
            return
        daily = HARD["search_pages_per_day"]
        if self.pages_today() >= daily:
            report.notes.append(f"today's search-page budget is used up ({daily} pages); it resets tomorrow")
            return
        run_cap = HARD["search_pages_per_run"]
        if ctx.limit and ctx.limit > 0:
            run_cap = min(run_cap, ctx.limit)

        owned = ctx.page is None
        pw = bctx = None
        stats = {"pages": 0, "people": 0, "first_degree": 0}
        try:
            if owned:
                pw, bctx, page = launch_browser(ctx.home, ctx.cfg, headless=ctx.headless)
                headless = ctx.headless if ctx.headless is not None else bool((ctx.cfg.get("browser") or {}).get("headless"))
                # Nobody can sign in to a window they cannot see, so a headless run fails fast instead of waiting.
                ensure_logged_in(page, ctx.base_url, wait_minutes=0.0 if headless else 10, stop=self.stopped)
            else:
                page = ctx.page
            self._crawl(page, sink, searches, run_cap, daily, stats)
        finally:
            if owned and (pw is not None or bctx is not None):
                close_browser(pw, bctx)
            report.notes.insert(0, f"read {stats['pages']} search page{'s' if stats['pages'] != 1 else ''}, "
                                   f"{stats['people']} people ({self.pages_today()}/{daily} pages used today)")
            if stats["first_degree"]:
                report.notes.append(f"{stats['first_degree']} existing 1st-degree connections were skipped")

    def _crawl(self, page, sink: Sink, searches, run_cap: int, daily: int, stats: dict) -> None:
        report = sink.report
        lo, hi = self.delay
        for label, build in searches:
            seen: set[str] = set()
            for number in range(1, self.max_pages + 1):
                if self.stopped():
                    report.notes.append("stopped on request")
                    return
                if stats["pages"] >= run_cap:
                    report.notes.append(f"page limit for one run reached ({run_cap}); run again later for more")
                    return
                if self.pages_today() >= daily:
                    report.notes.append(f"today's search-page budget is used up ({daily} pages); it resets tomorrow")
                    return
                if stats["pages"]:
                    human_pause(lo, hi)                       # slow, uneven pace between result pages
                self._spend_page()
                stats["pages"] += 1
                rows, no_results = self._read_page(page, build(number), label)
                fresh = []
                for row in rows:
                    key = person_key(row)
                    if key not in seen:
                        seen.add(key)
                        fresh.append(row)
                if self.skip_first_degree:
                    kept = [r for r in fresh if r.get("degree") != "1st"]
                    stats["first_degree"] += len(fresh) - len(kept)
                    report.skipped += len(fresh) - len(kept)
                else:
                    kept = fresh
                stats["people"] += sink.add(kept)
                self.log(f"LinkedIn search '{label[:40]}' page {number}: {len(fresh)} people")
                if no_results or not fresh:
                    break

    def _read_page(self, page, url: str, label: str) -> tuple[list[dict], bool]:
        lo, hi = self.delay
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        human_pause(min(1.0, lo / 4), min(2.5, hi / 3))        # let the page settle, like a person would
        human_scroll(page)
        self._scroll_results(page)
        if at_checkpoint(page):
            raise NeedsAttention("LinkedIn is asking you to verify your identity - open the browser window and complete it")
        if looks_logged_out(page):
            raise LoggedOut("LinkedIn signed you out while reading search results - log in again and re-run")
        if not urlsplit(page.url).path.startswith("/search/"):
            self.log(f"LinkedIn did not show a search page for '{label[:40]}'; skipped")
            return [], True
        cards = page.evaluate(EXTRACT_JS)
        rows = parse_result_cards(cards if isinstance(cards, list) else [], self.ctx.base_url, search=label)
        try:
            text = page.locator("main").first.inner_text(timeout=3_000)
        except Exception:
            text = ""
        return rows, bool(_NO_RESULTS.search(text)) and not rows

    @staticmethod
    def _scroll_results(page) -> None:
        """Results below the fold load lazily: keep scrolling a little while new profile links keep appearing."""
        last = -1
        for _ in range(6):
            try:
                count = page.locator('main a[href*="/in/"]').count()
            except Exception:
                return
            if count == last:
                return
            last = count
            page.mouse.wheel(0, random.randint(500, 900))
            page.wait_for_timeout(random.randint(500, 1000))
