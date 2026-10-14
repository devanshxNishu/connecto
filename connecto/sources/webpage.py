"""People from ordinary web pages - team, speakers, alumni or "about us" pages that link to LinkedIn profiles.

Two halves: ``fetch_html`` (a careful downloader, see ``connecto/netguard.py``: http(s) only, no private or internal
addresses, at most 3 redirects each re-checked, the connection goes to the address that was checked, 5 MB cap, HTML only)
and ``extract_people`` (a pure function: HTML in, person rows out). ``render: true`` loads the page in a headless browser
first, for pages that build their team list with JavaScript; every request that browser makes is checked as well.
"""
from __future__ import annotations

import itertools
import json
import random
import re
import time
import unicodedata
from typing import Callable, Iterable
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from .. import __version__, netguard
from ..models import linkedin_key, make_person, normalize_linkedin
from ..netguard import FetchError, safe_http_url
from ..util import clean_text
from .base import Sink, Source, split_headline

USER_AGENT = f"Mozilla/5.0 (compatible; Connecto/{__version__}; +https://github.com/)"
MAX_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 3
TIMEOUT = 30
MAX_PEOPLE_PER_PAGE = 500
MAX_LINKS_PER_PAGE = 20_000      # a page with more links than this is not a list of people; the rest are not read
MAX_NODES_PER_CARD = 2_000       # how much of one card is searched for a name
NAME_SOURCE_CHARS = 500          # a name is looked for in this much of a piece of text, and no more
HTML_TYPES = ("text/html", "application/xhtml+xml")
REDIRECT_CODES = netguard.REDIRECT_CODES


def _sleep(seconds: float) -> None:
    """Indirection so tests can run without waiting."""
    if seconds > 0:
        time.sleep(seconds)


def _require_html(headers: dict) -> None:
    ctype = str(headers.get("content-type", "")).split(";")[0].strip().lower()
    if ctype not in HTML_TYPES:
        raise FetchError(f"not an HTML page (content type {ctype or 'missing'})")


def fetch_html(url: str, *, allow_private: bool = False, max_bytes: int = MAX_BYTES, timeout: float = TIMEOUT,
               resolver=None) -> tuple[str, str]:
    """Download one HTML page. Returns (final_url, html). Raises FetchError / ValueError with a readable reason."""
    reply = netguard.get(url, allow_private=allow_private, max_bytes=max_bytes, timeout=timeout, max_redirects=MAX_REDIRECTS,
                         what="page", check=_require_html, resolver=resolver,
                         headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1"})
    if reply.status != 200:
        raise FetchError(f"http {reply.status}")
    return reply.url, netguard.decode_body(reply.body, reply.headers.get("content-type", ""))


# ----------------------------------------------------------------------------- names
STOP_WORDS = frozenset("""
about team our meet the contact us me my you your we home leadership people company founders founder cofounder co-founder
advisors advisor board staff speakers speaker careers jobs blog news press follow connect linkedin profile profiles view
read more share social links who are get touch join mission vision values story history partners investors mentors
faculty alumni members member directors director officer engineer engineering manager president designer developer
scientist analyst consultant associate intern lead head chief ceo cto cfo coo cmo vp principal architect researcher
professor student executive senior junior product sales marketing operations finance design software technology
technical business general global regional visit website email twitter github facebook instagram youtube page pages bio
biography learn click here see all show hide less next previous back menu search login sign subscribe newsletter
privacy terms policy cookie cookies copyright rights reserved logo photo image icon
""".split())
_NAME_WORD = re.compile(r"^[^\W\d_]+(?:[-'’.][^\W\d_]+)*\.?$")
_SPLIT_NAME = re.compile(r"\s+[-–—|·•:]\s+|\s*[,;:|·•]\s*")
_LABEL_NOISE = [
    re.compile(r"^(?:view|see|visit|open|follow|connect with|connect|find|check out|go to|contact|message)\s+", re.I),
    re.compile(r"^linked\s?in(?:\s+profile)?(?:\s+of|\s+for)?\s*[:\-–—|]?\s*", re.I),
    re.compile(r"[’']s\s+(?:linked\s?in\s*)?(?:profile|page|account)?\s*$", re.I),
    re.compile(r"\s+(?:on|at|via|@)\s+linked\s?in.*$", re.I),
    re.compile(r"\s*[-–—|:]\s*linked\s?in.*$", re.I),
    re.compile(r"\s+linked\s?in(?:\s+profile)?\s*$", re.I),
    re.compile(r"\s+profile\s*$", re.I),
]
GENERIC_SEGMENTS = frozenset({
    "linkedin", "twitter", "x", "email", "e-mail", "github", "facebook", "instagram", "website", "read more", "learn more",
    "view profile", "connect", "follow", "bio", "more", "read bio", "view bio", "linkedin profile", "contact", "profile",
})


def looks_like_name(text: str) -> bool:
    """2-4 words, letters only (plus ' - .), none of them a heading/job-title word like "Team" or "Officer"."""
    words = text.split()
    if not 2 <= len(words) <= 4:
        return False
    return all(_NAME_WORD.match(w) and w.lower().strip(".") not in STOP_WORDS for w in words)


def clean_name(text: str | None) -> str:
    """The person's name inside a piece of text ('Jane Doe, PhD' -> 'Jane Doe'), or '' when it is not a name."""
    # (only the start of a long text is looked at: the patterns below would otherwise try every position of a long run of
    # spaces, or of "(", and read to the end of it each time)
    t = unicodedata.normalize("NFC", re.sub(r"\([^)]*\)", " ", str(text or "")[:NAME_SOURCE_CHARS]))
    for part in _SPLIT_NAME.split(t):
        part = re.sub(r"\s+", " ", part).strip(" -–—")
        if looks_like_name(part):
            return part
    return ""


def name_from_label(label: str | None) -> str:
    """A name out of an aria-label / title / alt text: 'Jane Doe on LinkedIn', 'View Jane Doe's profile', ..."""
    t = clean_text(label)
    for rx in _LABEL_NOISE:
        t = rx.sub("", t).strip()
    return clean_name(t)


def _classes(el: Tag) -> str:
    return " ".join(el.get("class") or []).lower()


_NAME_PLACES = (
    lambda el: el.name in ("h1", "h2", "h3", "h4", "h5", "h6") or el.get("role") == "heading",       # headings
    lambda el: el.get("itemprop") == "name" or "name" in _classes(el),                                # name-like elements
    lambda el: el.name in ("strong", "b") or "bold" in _classes(el),                                 # bold text
)


def _find_name(node: Tag) -> str:
    """Closest-first inside one card: headings, then name-like elements, then bold text, then image alt text.

    Walks the card itself (a bounded number of elements) rather than asking a CSS engine, which looks up through every
    ancestor of the card on each question - quadratic on a page whose elements are nested thousands deep.
    """
    elements = [el for el in itertools.islice(node.descendants, MAX_NODES_PER_CARD) if isinstance(el, Tag)]
    for is_place in _NAME_PLACES:
        for el in elements:
            if is_place(el):
                name = clean_name(el.get_text(" ", strip=True))
                if name:
                    return name
    for el in elements:
        if el.name == "img" and el.get("alt"):
            name = name_from_label(el.get("alt"))
            if name:
                return name
    return ""


def _headline(card: Tag, name: str) -> str:
    """The first short piece of text in the card after the name (e.g. "CTO at Acme"), without link labels."""
    segments = [clean_text(s) for s in card.stripped_strings]
    me = name.lower()
    start = next((i + 1 for i, s in enumerate(segments) if s.lower().startswith(me)), 0)   # "Jane Doe, PhD" counts too
    for seg in segments[start:]:
        seg = seg.strip(" ·•|:,-–—")
        low = seg.lower()
        if (not seg or low.startswith(me) or low in GENERIC_SEGMENTS or len(seg) > 160 or not re.search(r"\w", seg)
                or re.search(r"https?://|www\.|\S@\S", seg)):
            continue
        return seg
    return ""


# ----------------------------------------------------------------------------- JSON-LD
def _walk(node, depth: int = 0) -> Iterable[dict]:
    if depth > 8:
        return
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v, depth + 1)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v, depth + 1)


def _jsonld_people(soup: BeautifulSoup, base_url: str) -> list[dict]:
    out = []
    for script in soup.find_all("script", type=re.compile(r"ld\+json", re.I)):
        raw = (script.string or script.get_text() or "").strip()
        raw = re.sub(r"^\s*<!--|-->\s*$|^\s*//<!\[CDATA\[|//\]\]>\s*$", "", raw).strip()
        try:
            data = json.loads(raw)
        except (ValueError, RecursionError):        # (a page can nest arrays a hundred thousand deep)
            continue
        for obj in _walk(data):
            kind = obj.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if "Person" not in [str(k) for k in kinds]:
                continue
            same = obj.get("sameAs")
            candidates = (same if isinstance(same, list) else [same]) + [obj.get("url")]
            url = next((u for u in (normalize_linkedin(urljoin(base_url, str(c))) for c in candidates if c) if u), "")
            if not url:
                continue
            works = obj.get("worksFor")
            works = works[0] if isinstance(works, list) and works else works
            company = works.get("name") if isinstance(works, dict) else (works if isinstance(works, str) else "")
            out.append({"linkedin_url": url, "full_name": clean_text(obj.get("name")),
                        "title": clean_text(obj.get("jobTitle")), "company": clean_text(company)})
    return out


# ----------------------------------------------------------------------------- the extractor
def extract_people_ex(html: str | bytes, base_url: str = "", source_url: str | None = None) -> tuple[list[dict], dict]:
    """(person rows, stats). stats: {'links': personal LinkedIn links seen, 'nameless': links with no findable name}.

    ``base_url`` resolves relative links; ``source_url`` (default: the same) is what each row records as its origin.
    """
    soup = BeautifulSoup(html, "lxml")
    title_tag = soup.find("title")
    page_title = clean_text(title_tag.get_text(" ")) if title_tag else ""
    stats = {"links": 0, "nameless": 0}

    anchors: list[tuple[Tag, str]] = []
    for a in itertools.islice(soup.find_all("a", href=True), MAX_LINKS_PER_PAGE):
        url = normalize_linkedin(urljoin(base_url or "", str(a["href"]).strip()))
        if url:
            anchors.append((a, url))
    stats["links"] = len({linkedin_key(u) for _, u in anchors})

    # element -> the one person linked inside it, or None once it holds several. Climbing stops at an element that already
    # knows the answer - everything above it knows too - so a page nested thousands deep costs a step per element and not
    # one per (link, ancestor) pair.
    holds: dict[int, str | None] = {}
    for a, url in anchors:
        who = linkedin_key(url)
        for ancestor in a.parents:
            if id(ancestor) not in holds:
                holds[id(ancestor)] = who
            elif holds[id(ancestor)] is None or holds[id(ancestor)] == who:
                break
            else:
                holds[id(ancestor)] = None

    people: dict[str, dict] = {}
    nameless: set[str] = set()
    named_inside: dict[int, str] = {}      # card element -> the name found inside it (searched once, however many links share it)
    for a, url in anchors:
        key = linkedin_key(url)
        name = clean_name(a.get_text(" ", strip=True))
        if not name:
            name = next((n for n in (name_from_label(a.get("aria-label")), name_from_label(a.get("title"))) if n), "")
        if not name:
            name = next((n for n in (name_from_label(i.get("alt")) for i in a.find_all("img", alt=True)) if n), "")
        node, card = a, a
        for _ in range(4):                     # climb to the card: stop before an ancestor that holds other people
            parent = node.parent
            if not isinstance(parent, Tag) or isinstance(parent, BeautifulSoup) or parent.name in ("body", "html"):
                break
            if id(parent) in holds and holds[id(parent)] is None:           # it holds somebody else as well
                break
            node = card = parent
            if not name:
                if id(node) not in named_inside:
                    named_inside[id(node)] = _find_name(node)
                name = named_inside[id(node)]
        if not name:
            nameless.add(key)
            continue
        if key in people:
            continue
        people[key] = {"linkedin_url": url, "full_name": name, "headline": _headline(card, name)}
        if len(people) >= MAX_PEOPLE_PER_PAGE:
            break
    stats["nameless"] = len(nameless - set(people))

    for item in _jsonld_people(soup, base_url or ""):
        key = linkedin_key(item["linkedin_url"])
        if key in people:
            row = people[key]
            for field in ("title", "company"):
                if item.get(field):
                    row[field] = item[field]
            if not row.get("full_name") and item.get("full_name"):
                row["full_name"] = item["full_name"]
        elif item.get("full_name") and len(people) < MAX_PEOPLE_PER_PAGE:
            people[key] = dict(item)

    rows = []
    for row in people.values():
        headline = row.get("headline") or ""
        if not headline and (row.get("title") or row.get("company")):
            headline = " at ".join(x for x in (row.get("title"), row.get("company")) if x)
        title, company = row.get("title") or "", row.get("company") or ""
        if not (title or company):
            title, company = split_headline(headline)
        person = make_person(linkedin_url=row["linkedin_url"], full_name=row.get("full_name"), headline=headline[:160],
                             title=title, company=company, source_url=base_url if source_url is None else source_url,
                             page_title=page_title[:160])
        if person:
            rows.append(person)
    return rows, stats


def extract_people(html: str | bytes, base_url: str = "") -> list[dict]:
    """Every person with a personal LinkedIn link on the page, as person rows (de-duplicated by profile)."""
    return extract_people_ex(html, base_url)[0]


# ----------------------------------------------------------------------------- browser rendering (render: true)
SKIPPED_TYPES = frozenset({"image", "media", "font", "texttrack", "eventsource"})     # never needed to read who is on a page


def make_route_guard(allow_private: bool, resolver=None, context=None) -> Callable:
    """A Playwright route handler for every request the rendering browser makes.

    A page can send a browser anywhere - a script, an image, a frame, a redirect. Requests to private or internal hosts (and
    to anything but http/https) are refused. Beyond that, unless ``allow_private``, the request is made by Playwright's own
    client and a redirect is followed *here*, one hop at a time, after checking where it leads: handed to Chromium as it
    is, the redirect would be followed without asking again. (``context`` is the browser context, for the later hops.)
    Known limit: the name is looked up once here and once more when the request is made, so a DNS server that answers
    differently the second time (DNS rebinding) is not defended against - the downloader for plain pages is.
    """
    verdict: dict[tuple[str, str], bool] = {}

    def allowed(url: str) -> bool:
        parts = urlsplit(url)
        key = (parts.scheme, parts.hostname or "")
        ok = verdict.get(key)
        if ok is None:
            try:
                safe_http_url(url, allow_private, resolver)
                ok = True
            except ValueError:
                ok = False
            verdict[key] = ok
        return ok

    def refuse(route) -> None:
        try:
            route.abort()
        except Exception:  # noqa: BLE001 - the page may be gone already
            pass

    def handler(route, request=None) -> None:
        request = request or route.request
        url = request.url
        if urlsplit(url).scheme in ("data", "blob", "about"):
            return route.continue_()
        if not allowed(url) or getattr(request, "resource_type", "") in SKIPPED_TYPES:
            return refuse(route)                       # (ftp:, file:, ws: ... never leave the browser)
        if allow_private:
            return route.continue_()
        try:
            response = route.fetch(max_redirects=0)
            hops = 0
            while response.status in REDIRECT_CODES:
                location = response.headers.get("location")
                if not location or hops >= MAX_REDIRECTS or request.method != "GET":
                    return refuse(route)
                hops += 1
                url = urljoin(url, location)
                if not allowed(url):
                    return refuse(route)
                headers = {k: v for k, v in dict(request.headers).items() if k.lower() not in ("host", "authorization", "content-length")}
                response = context.request.get(url, max_redirects=0, headers=headers)
            return route.fulfill(response=response)
        except Exception:  # noqa: BLE001 - a request that cannot be made safely is not made
            return refuse(route)

    return handler


def refuse_web_socket(socket_route) -> None:
    """A page's WebSocket is routed here and never connected to its server: it stays open, and silent, and nothing leaves
    the browser. (Nothing on a team page needs one, and the address checks above cannot see a WebSocket. It is not closed
    explicitly because Playwright's synchronous API cannot be called from inside this kind of handler.)"""
    return None


class WebpageSource(Source):
    type = "webpage"

    def __init__(self, spec: dict, ctx):
        super().__init__(spec, ctx)
        urls = spec.get("urls")
        if isinstance(urls, str):
            urls = urls.split()
        self.urls = [str(u).strip() for u in (urls or []) if str(u).strip()]
        self.render = str(spec.get("render", False)).strip().lower() in ("1", "true", "yes", "on")
        self.allow_private = str(spec.get("allow_private", False)).strip().lower() in ("1", "true", "yes", "on")
        self.needs_browser = self.render        # instance-level: only rendering needs a browser

    @property
    def source_id(self) -> str:
        host = ""
        if self.urls:
            try:
                host = (urlsplit(self.urls[0]).hostname or "").lower()
            except ValueError:
                host = ""
        return f"webpage:{host}" if host else "webpage"

    def owns(self, row: dict) -> bool:
        origin = str(row.get("source_url") or "")
        return not origin or origin in self.urls

    def describe(self) -> str:
        if not self.urls:
            return "Web page (no URL set)"
        more = f" (+{len(self.urls) - 1} more)" if len(self.urls) > 1 else ""
        return f"Web page {self.urls[0]}{more}" + (" - rendered in a browser" if self.render else "")

    def fetch(self, sink: Sink) -> None:
        report = sink.report
        if not self.urls:
            report.errors.append("this web page source has no `urls` set")
            return
        urls = self.urls[: self.ctx.limit] if self.ctx.limit and self.ctx.limit > 0 else self.urls
        browser = _Rendering(self) if self.render else None
        pages = people_total = 0
        try:
            for i, url in enumerate(urls):
                if self.stopped():
                    break
                if i:
                    _sleep(random.uniform(1.0, 2.0))       # one request per URL, never a burst
                try:
                    final_url, html = browser.load(url) if browser else fetch_html(url, allow_private=self.allow_private)
                except ValueError as exc:
                    report.errors.append(f"{url}: {exc}")
                    continue
                except Exception as exc:  # noqa: BLE001 - browser trouble must not hide the other pages
                    report.errors.append(f"{url}: {type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}")
                    continue
                pages += 1
                rows, stats = extract_people_ex(html, final_url, source_url=url)
                n = sink.add(rows)
                people_total += n
                host = urlsplit(final_url).hostname or final_url
                if n:
                    self.log(f"web page {host}: {n} people")
                else:
                    hint = "" if self.render else " (if the page builds its team list with JavaScript, set `render: true`)"
                    report.notes.append(f"no LinkedIn profile links with names found on {url}{hint}")
                if stats["nameless"]:
                    report.notes.append(f"{stats['nameless']} LinkedIn links on {host} had no findable name and were skipped")
        finally:
            if browser:
                browser.close()
        report.notes.insert(0, f"read {pages} of {len(urls)} page{'s' if len(urls) != 1 else ''}, {people_total} people")


class _Rendering:
    """One headless browser (own profile, never the LinkedIn one) shared by all pages of a source run."""

    def __init__(self, source: WebpageSource):
        from ..browser import launch_browser

        ctx = source.ctx
        self.allow_private = source.allow_private
        headless = True if ctx.headless is None else ctx.headless
        self._pw, self._ctx, self.page = launch_browser(ctx.home, ctx.cfg, headless=headless, profile="webpage",
                                                        service_workers="block")      # (a worker's requests would bypass the guard)
        self._ctx.route("**/*", make_route_guard(source.allow_private, context=self._ctx))
        if not source.allow_private:
            self._ctx.route_web_socket("**/*", refuse_web_socket)

    def load(self, url: str) -> tuple[str, str]:
        safe_http_url(url, self.allow_private)
        self.page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        try:
            self.page.wait_for_load_state("networkidle", timeout=8_000)
        except Exception:
            pass
        final = safe_http_url(self.page.url, self.allow_private)   # (the address bar after any redirect)
        return final, self.page.content()

    def close(self) -> None:
        from ..browser import close_browser

        close_browser(self._pw, self._ctx)
