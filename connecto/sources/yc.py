"""The Y Combinator directory: companies from the public yc-oss mirror, founders from each company's own page.

Pipeline
  1. Company list: https://yc-oss.github.io/api/companies/all.json (public, refreshed daily, 6k+ companies), cached in
     ``<home>/data/yc_companies_all.json``.
  2. The company-level filters (batches, industries, tags, regions, keywords, status, team size, ...) are applied to that
     list FIRST, so "AI startups in India, last two batches" fetches a few hundred pages, not six thousand.
  3. Founders: each remaining company's page lists its founders with LinkedIn links (``yc_parser``). Newest batch first,
     resumable (``progress`` table), polite (<= 2 workers, jittered delay, backoff on 429/5xx, stop on repeated 403).

Every founder is stored with the company's fields, so ``accepts(row)`` can re-apply the same filters later (for example
after the config changed) without fetching anything again.
"""
from __future__ import annotations

import json
import random
import re
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Mapping
from urllib.parse import quote

from .. import netguard
from ..limits import HARD
from ..models import make_person
from ..targets import SPLIT, any_term_in, haystack
from ..util import as_list, atomic_write, clean_text
from .base import Sink, Source, float_pair
from .yc_parser import Founder, batch_long, batch_short, parse_batch_list, parse_founders

DEFAULT_COMPANIES_JSON = "https://yc-oss.github.io/api/companies/all.json"
DEFAULT_COMPANY_PAGE = "https://www.ycombinator.com/companies/{slug}"
DEFAULT_USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/129.0.0.0 Safari/537.36")
CACHE_FILE = "yc_companies_all.json"
MAX_LIST_BYTES = 48 * 1024 * 1024      # the directory's list (some 10 MB today) - and no more than this, whatever a mirror sends
LIST_SECONDS = 300
SEASON_INDEX = {"winter": 0, "spring": 1, "summer": 2, "fall": 3}
BACKOFF = (5, 20, 60)          # seconds before retry 1, 2, 3 for 429 / 5xx / network errors (each x 0.8-1.3)
MAX_403_STREAK = 5
_BATCH_RE = re.compile(r"^\s*(winter|spring|summer|fall)\s+(\d{4})\s*$", re.I)


def _sleep(seconds: float) -> None:
    """Indirection so tests can run without waiting."""
    if seconds > 0:
        time.sleep(seconds)


# ----------------------------------------------------------------------------- company helpers (pure)
def batch_priority(batch: str | None) -> int:
    """Recency number: year * 10 + season (Winter 0, Spring 1, Summer 2, Fall 3). 'Fall 2026' -> 20263. Unknown -> 0."""
    m = _BATCH_RE.match(batch_long(batch or ""))
    return int(m.group(2)) * 10 + SEASON_INDEX[m.group(1).lower()] if m else 0


def _as_list(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value if v]


def _team_size(value) -> str:
    try:
        return str(int(float(value)))
    except (TypeError, ValueError):
        return ""


def company_view(company: dict) -> dict[str, str]:
    """A yc-oss company record as the flat strings a stored person row carries (same keys, same filters)."""
    batch = clean_text(company.get("batch"))
    return {
        "company": clean_text(company.get("name")),
        "company_slug": clean_text(company.get("slug")),
        "batch": batch,
        "batch_code": batch_short(batch),
        "company_status": clean_text(company.get("status")),
        "one_liner": clean_text(company.get("one_liner")),
        "industry": clean_text(company.get("industry")),
        "subindustry": clean_text(company.get("subindustry")),
        "industries": SPLIT.join(_as_list(company.get("industries"))),
        "tags": SPLIT.join(_as_list(company.get("tags"))),
        "regions": SPLIT.join(_as_list(company.get("regions"))),
        "location": clean_text(company.get("all_locations")),
        "team_size": _team_size(company.get("team_size")),
        "top_company": "true" if company.get("top_company") else "false",
        "website": clean_text(company.get("website")),
        "company_url": clean_text(company.get("url")),
        "long_description": clean_text(company.get("long_description"), 600),
    }


def taxonomy(companies: list[dict], field_name: str, top: int = 60) -> list[tuple[str, int]]:
    """The values YC uses for industry / subindustry / industries / tags / regions / batch / status, with counts."""
    counts: dict[str, int] = {}
    for c in companies:
        for v in _as_list(c.get(field_name)):
            counts[v] = counts.get(v, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:top]


@dataclass
class YCFilter:
    """The selection a YC source applies - to companies before fetching, and to stored founders at send time."""

    batches: set[str] = field(default_factory=set)
    industries: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    regions: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    company_status: list[str] = field(default_factory=list)
    only_top_companies: bool = False
    max_team_size: int = 0
    include_former_founders: bool = False

    @classmethod
    def from_spec(cls, spec: dict) -> "YCFilter":
        def terms(key: str) -> list[str]:
            return [t.lower() for t in as_list(spec.get(key))]

        try:
            max_team = max(0, int(float(spec.get("max_team_size") or 0)))
        except (TypeError, ValueError):
            max_team = 0
        return cls(
            batches=parse_batch_list(spec.get("batches")),
            industries=terms("industries"), tags=terms("tags"), regions=terms("regions"),
            keywords=terms("keywords"), exclude_keywords=terms("exclude_keywords"),
            company_status=terms("company_status"),
            only_top_companies=str(spec.get("only_top_companies", False)).strip().lower() in ("1", "true", "yes", "on"),
            max_team_size=max_team,
            include_former_founders=str(spec.get("include_former_founders", False)).strip().lower() in ("1", "true", "yes", "on"),
        )

    def company_ok(self, v: Mapping[str, str]) -> bool:
        """Company-level filters over flat strings (a ``company_view`` or a stored person row). A value a filter needs
        but the record lacks means "no": unknown is never assumed to match."""
        if self.batches and batch_long(v.get("batch") or "") not in self.batches:
            return False
        if self.industries and not any_term_in(self.industries, haystack(v, "industry", "subindustry", "industries")):
            return False
        if self.tags and not any_term_in(self.tags, haystack(v, "tags")):
            return False
        if self.regions and not any_term_in(self.regions, haystack(v, "regions", "location")):
            return False
        if self.company_status and (v.get("company_status") or "").strip().lower() not in self.company_status:
            return False
        if self.only_top_companies and (v.get("top_company") or "").strip().lower() not in ("true", "1", "yes"):
            return False
        if self.keywords or self.exclude_keywords:
            text = haystack(v, "company", "one_liner", "long_description", "tags", "industry", "subindustry", "industries")
            if self.keywords and not any_term_in(self.keywords, text):
                return False
            if self.exclude_keywords and any_term_in(self.exclude_keywords, text):
                return False
        if self.max_team_size:
            try:
                if int(float(v.get("team_size") or "")) > self.max_team_size:
                    return False
            except ValueError:
                return False
        return True

    def accepts_row(self, row: Mapping[str, str]) -> bool:
        if (row.get("founder_status") or "").strip().lower() == "former" and not self.include_former_founders:
            return False
        return self.company_ok(row)

    def describe(self) -> str:
        parts = []
        if self.batches:
            parts.append("batches " + ", ".join(batch_short(b) for b in sorted(self.batches, key=batch_priority, reverse=True)))
        for name in ("industries", "tags", "regions", "keywords", "exclude_keywords", "company_status"):
            val = getattr(self, name)
            if val:
                parts.append(f"{name.replace('_', ' ')} {', '.join(val)}")
        if self.only_top_companies:
            parts.append("top companies only")
        if self.max_team_size:
            parts.append(f"team size <= {self.max_team_size}")
        parts.append("incl. former founders" if self.include_former_founders else "active founders only")
        return "; ".join(parts) if len(parts) > 1 else "all batches and industries; " + parts[0]


def _company_list(text: str) -> list[dict]:
    try:
        data = json.loads(text)
    except RecursionError:
        raise ValueError("the company list is nested too deeply to read") from None
    if not isinstance(data, list):
        raise ValueError("the company list is not a JSON array")
    return data


# ----------------------------------------------------------------------------- fetching pages
def _retry_after(reply) -> float:
    try:
        seconds = float(reply.headers.get("retry-after", ""))
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if seconds != seconds else max(0.0, seconds)          # (a nan is no wait at all)


class RequestsFetcher:
    """Plain HTTP, through Connecto's guarded downloader (``connecto/netguard.py``): no private or internal addresses unless
    ``allow_private`` is set, redirects checked, size and time capped, no proxy. Retries 429 / 5xx / network errors with
    backoff; never retries 403 (blocked) or 404 (gone) or an address that is refused."""

    def __init__(self, user_agent: str = DEFAULT_USER_AGENT, allow_private: bool = False):
        self.allow_private = allow_private
        self.headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

    def fetch(self, url: str) -> tuple[int, str]:
        status, text = 0, ""
        for attempt in range(len(BACKOFF) + 1):
            wait_extra = 0.0
            try:
                reply = netguard.get(url, allow_private=self.allow_private, headers=self.headers, what="company page")
            except netguard.Refused as exc:
                return -1, str(exc)                                  # (the address itself is not allowed: asking again changes nothing)
            except netguard.FetchError as exc:
                status, text = -1, str(exc)
            else:
                status, text = reply.status, ""
                if status == 200:
                    return 200, netguard.decode_body(reply.body, reply.headers.get("content-type", ""))
                if status in (403, 404, 410) or (400 <= status < 500 and status != 429):
                    return status, ""
                wait_extra = _retry_after(reply)
            if attempt < len(BACKOFF):
                _sleep(max(BACKOFF[attempt] * random.uniform(0.8, 1.3), min(wait_extra, 120.0)))
        return status, text

    def close(self) -> None:
        pass


class PlaywrightFetcher:
    """Loads pages in a real browser (own profile under ``<home>/browser/yc``). Slower, but survives bot challenges.

    Not exercised against the real ycombinator.com (tests drive it against a local stand-in server).
    """

    def __init__(self, home, cfg: dict, headless: bool | None = None):
        from ..browser import launch_browser  # imported lazily: `requests` mode needs no browser

        self._pw, self._ctx, self.page = launch_browser(home, cfg, headless=headless, profile="yc")

    def fetch(self, url: str) -> tuple[int, str]:
        resp = self.page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        status = resp.status if resp else 0
        if "Just a moment" in self.page.title():
            # Cloudflare-style challenge: give the browser time to pass it (or the person watching, to click).
            self.page.wait_for_timeout(8_000)
            status = 403 if "Just a moment" in self.page.title() else 200
        try:
            self.page.wait_for_selector(".ycdc-card-new", timeout=10_000)
        except Exception:
            pass
        return status, self.page.content()

    def close(self) -> None:
        from ..browser import close_browser

        close_browser(self._pw, self._ctx)


# ----------------------------------------------------------------------------- the source
@dataclass
class _RunState:
    fetched: int = 0
    ok: int = 0
    founders: int = 0
    with_linkedin: int = 0
    gone: int = 0
    failed: int = 0
    streak_403: int = 0
    halted: bool = False
    zero_slugs: list = field(default_factory=list)


class YCSource(Source):
    type = "yc"

    def __init__(self, spec: dict, ctx):
        super().__init__(spec, ctx)
        self.filter = YCFilter.from_spec(spec)
        engine = str(spec.get("engine") or "requests").strip().lower()
        self.engine = engine if engine in ("requests", "playwright") else "requests"
        self.needs_browser = self.engine == "playwright"  # instance-level: only the browser engine needs one
        try:
            workers = int(spec.get("workers") or 2)
        except (TypeError, ValueError):
            workers = 2
        self.workers = 1 if self.engine == "playwright" else max(1, min(workers, HARD["yc_workers_max"]))
        self.delay = float_pair(spec.get("delay_seconds"), (1.0, 2.5))
        self.companies_json = str(spec.get("companies_json") or DEFAULT_COMPANIES_JSON)
        self.company_page = str(spec.get("company_page") or DEFAULT_COMPANY_PAGE)
        self.user_agent = " ".join(str(spec.get("user_agent") or DEFAULT_USER_AGENT).split())[:300]       # (one line of text)
        self.allow_private = str(spec.get("allow_private", False)).strip().lower() in ("1", "true", "yes", "on")
        try:
            self.cache_hours = float(spec.get("cache_hours", 24))
        except (TypeError, ValueError):
            self.cache_hours = 24.0

    @property
    def source_id(self) -> str:
        return "yc"

    def accepts(self, row: dict) -> bool:
        return self.filter.accepts_row(row)

    def describe(self) -> str:
        return "YC directory: " + self.filter.describe()

    # ------------------------------------------------------------------ company list
    def load_companies(self, force: bool = False) -> tuple[list[dict], str]:
        """(companies, how) where how is 'cache', 'downloaded' or 'stale cache (...)'. Raises if there is nothing to use."""
        cache = self.ctx.home.data_dir / CACHE_FILE
        if cache.exists() and not force:
            age = datetime.now() - datetime.fromtimestamp(cache.stat().st_mtime)
            if age < timedelta(hours=self.cache_hours):
                try:
                    return _company_list(cache.read_text(encoding="utf-8")), "cache"
                except (OSError, ValueError):
                    pass
        try:
            reply = netguard.get(self.companies_json, allow_private=self.allow_private, what="the company list",
                                 headers={"User-Agent": self.user_agent, "Accept": "application/json"},
                                 max_bytes=MAX_LIST_BYTES, timeout=60, total_seconds=LIST_SECONDS)
            if reply.status != 200:
                raise netguard.FetchError(f"http {reply.status}")
            data = _company_list(netguard.decode_body(reply.body, reply.headers.get("content-type", "")))
        except ValueError as exc:                               # (a refused address, a failed download, text that is not a JSON list)
            if cache.exists():
                try:
                    return _company_list(cache.read_text(encoding="utf-8")), f"stale cache (download failed: {exc})"
                except (OSError, ValueError):
                    pass
            raise
        atomic_write(cache, json.dumps(data))
        return data, "downloaded"

    def matching_companies(self, companies: list[dict]) -> list[dict]:
        """Companies passing the company-level filters, newest batch first (then by name), de-duplicated by slug."""
        seen: set[str] = set()
        picked = []
        for c in companies:
            slug = str(c.get("slug") or "").strip()
            if not slug or slug in seen or not self.filter.company_ok(company_view(c)):
                continue
            seen.add(slug)
            picked.append(c)
        picked.sort(key=lambda c: (-batch_priority(c.get("batch")), str(c.get("name") or "").lower(), str(c.get("slug"))))
        return picked

    # ------------------------------------------------------------------ rows
    def page_url(self, slug: str) -> str:
        if "{slug}" not in self.company_page:
            raise ValueError("company_page must contain {slug}")
        return self.company_page.replace("{slug}", quote(slug, safe="-_.~"))

    def person(self, company: dict, founder: Founder, page_url: str) -> dict:
        v = company_view(company)
        return make_person(
            linkedin_url=founder.linkedin_url, full_name=founder.full_name, title=founder.title, company=v["company"],
            location=v["location"], batch=v["batch"], batch_code=v["batch_code"], company_status=v["company_status"],
            one_liner=v["one_liner"], industry=v["industry"], subindustry=v["subindustry"], industries=v["industries"],
            tags=v["tags"], regions=v["regions"], team_size=v["team_size"], top_company=v["top_company"],
            website=v["website"], company_url=v["company_url"] or page_url, long_description=v["long_description"],
            founder_status=founder.founder_status, twitter_url=founder.twitter_url, bio=founder.bio,
            company_slug=v["company_slug"], priority=str(batch_priority(v["batch"])),
        )

    # ------------------------------------------------------------------ the run
    def fetch(self, sink: Sink) -> None:
        report, store, ctx = sink.report, self.ctx.store, self.ctx
        companies, how = self.load_companies(force=ctx.refresh)
        selected = self.matching_companies(companies)
        done = set() if ctx.refresh else store.progress_done("yc")
        todo = [c for c in selected if str(c["slug"]).strip() not in done]
        already = len(selected) - len(todo)
        if ctx.limit and ctx.limit > 0:
            todo = todo[: ctx.limit]
        self.log(f"YC: {len(selected)} of {len(companies)} companies match ({how}); {already} already fetched, "
                 f"{len(todo)} to fetch (engine={self.engine}, workers={self.workers})")
        report.notes.append(f"{len(selected)} of {len(companies)} directory companies match your filters; "
                            f"{already} fetched earlier, {len(todo)} this run")
        if not selected:
            report.notes.append("no company matches - check batches/industries/tags spelling (YC's own words)")
        if not todo:
            return

        fetcher = (PlaywrightFetcher(ctx.home, ctx.cfg, headless=ctx.headless) if self.engine == "playwright"
                   else RequestsFetcher(self.user_agent, self.allow_private))
        state = _RunState()
        try:
            if self.workers <= 1:
                for company in todo:
                    if state.halted or self.stopped():
                        break
                    self._handle(*self._work(company, fetcher), sink, state, len(todo))
            else:
                self._run_pool(todo, fetcher, sink, state)
        finally:
            fetcher.close()
        self._finish(sink, state, len(todo))

    def _run_pool(self, todo: list[dict], fetcher, sink: Sink, state: _RunState) -> None:
        pending = {}
        queue = iter(todo)
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            while True:
                while len(pending) < self.workers and not state.halted and not self.stopped():
                    company = next(queue, None)
                    if company is None:
                        break
                    pending[pool.submit(self._work, company, fetcher)] = company
                if not pending:
                    break
                finished, _ = wait(list(pending), return_when=FIRST_COMPLETED)
                for fut in finished:
                    pending.pop(fut)
                    self._handle(*fut.result(), sink, state, len(todo))  # store writes stay on this thread

    def _work(self, company: dict, fetcher):
        """Runs in a worker thread: only network + parsing, never the database."""
        _sleep(random.uniform(*self.delay))
        slug = str(company["slug"]).strip()
        try:
            url = self.page_url(slug)
            status, html = fetcher.fetch(url)
        except Exception as exc:  # noqa: BLE001 - one bad page must not end the run
            return company, "", -1, str(exc)[:200], []
        return company, url, status, (html[:200] if status < 0 else ""), (parse_founders(html) if status == 200 else [])

    def _handle(self, company: dict, url: str, status: int, err: str, founders: list[Founder],
                sink: Sink, state: _RunState, total: int) -> None:
        store, report = self.ctx.store, sink.report
        slug = str(company["slug"]).strip()
        state.fetched += 1
        if status == 200:
            state.streak_403 = 0
            state.ok += 1
            rows = [self.person(company, f, url) for f in founders]
            with_li = sum(1 for r in rows if r)
            sink.add(rows)
            store.progress_mark("yc", slug, {"founders": len(founders), "with_linkedin": with_li})
            state.founders += len(founders)
            state.with_linkedin += with_li
            if not founders:
                state.zero_slugs.append(slug)
        elif status in (404, 410):
            state.streak_403 = 0
            state.gone += 1
            store.progress_mark("yc", slug, {"founders": 0, "http": status})
        else:
            state.failed += 1
            reason = f"http {status}" if status > 0 else f"network error: {err}"
            store.progress_mark("yc", slug, {"founders": 0}, error=reason)
            if len(report.errors) < 3:
                report.errors.append(f"{slug}: {reason}")
            if status == 403:
                state.streak_403 += 1
                if state.streak_403 >= MAX_403_STREAK and not state.halted:
                    state.halted = True
                    msg = (f"YC is refusing plain HTTP requests ({MAX_403_STREAK} x 403 in a row) - stopped. "
                           "Set `engine: playwright` on this source to load pages in a real browser, then run again.")
                    report.notes.append(msg)
                    report.errors.append(msg)
                    self.log("YC: " + msg)
        if state.fetched % 25 == 0 or state.fetched == total:
            self.log(f"YC: {state.fetched}/{total} companies | {state.founders} founders "
                     f"({state.with_linkedin} with LinkedIn)")

    def _finish(self, sink: Sink, state: _RunState, total: int) -> None:
        report, store = sink.report, self.ctx.store
        report.notes.insert(0, f"fetched {state.fetched} companies: {state.founders} founders, "
                               f"{state.with_linkedin} with a LinkedIn link")
        if state.gone:
            report.notes.append(f"{state.gone} company pages no longer exist (404)")
        if state.failed:
            report.notes.append(f"{state.failed} pages failed and will be retried on the next run")
        if state.fetched < total and not state.halted:
            report.notes.append(f"stopped early after {state.fetched} of {total} companies; run again to continue")
        if state.ok >= 3 and len(state.zero_slugs) == state.ok:
            store.progress_reset("yc", state.zero_slugs)  # so they are fetched again once the parser is fixed
            report.errors.append("YC pages loaded but no founders could be read from any of them - the page layout may "
                                 "have changed (see connecto/sources/yc_parser.py)")

