"""Tests for the sources: YC directory, CSV files, web pages, LinkedIn search, and the registry / filter / runner glue.

Nothing here touches the real internet: HTTP goes to throw-away servers on 127.0.0.1, and the browser tests (marked
``browser``) drive headless Chromium against tests/mock_search.py.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import threading
import time
from datetime import datetime

import pytest

from connecto.browser import LoggedOut, NeedsAttention
from connecto.limits import HARD, limits_from_config
from connecto.models import make_person, person_key
from connecto.paths import Home
from connecto.sources import (REGISTRY, FetchReport, Sink, Source, SourceContext, build_source, fetch_sources, make_filter,
                             read_csv_rows)
from connecto.sources import linkedin_search as search_mod
from connecto.sources import webpage as web_mod
from connecto.sources import yc as yc_mod
from connecto.store import Store

from mock_search import DISTINCT_SLUGS, FIRST_DEGREE_SLUGS, MockSearch  # noqa: E402 - tests/ is on sys.path
from web_stand_in import LocalServer, Pages, Steer  # noqa: E402

PUBLIC = "93.184.216.34"
FIX = pathlib.Path(__file__).parent / "fixtures"
COMPANIES = json.loads((FIX / "yc_companies.json").read_text(encoding="utf-8"))
BY_SLUG = {c["slug"]: c for c in COMPANIES}
BROWSER_CFG = {"browser": {"channel": "chromium", "headless": True}}


# ----------------------------------------------------------------------------- shared helpers
class Env:
    """A throw-away Connecto home + store, and helpers to build contexts and run one source."""

    def __init__(self, tmp_path, cfg=None):
        self.home = Home(tmp_path / "home").ensure()
        self.cfg = cfg if cfg is not None else dict(BROWSER_CFG)
        self.store = Store(self.home.db_path)
        self.logs: list[str] = []

    def ctx(self, **kw) -> SourceContext:
        return SourceContext(home=self.home, cfg=self.cfg, store=self.store, limits=limits_from_config(self.cfg),
                             log=self.logs.append, **kw)

    def run(self, spec: dict, **ctx_kw) -> tuple[Source, FetchReport]:
        src = build_source(spec, self.ctx(**ctx_kw))
        report = FetchReport(src.source_id)
        src.fetch(Sink(self.store, src.type, src.source_id, report, self.logs.append))
        return src, report


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.store.close()


class FakeYC(LocalServer):
    """Serves the company list and one founder page per company (tests/fixtures/company_page.html, with the founders'
    LinkedIn slugs made unique per company). Counts every request; ``script[slug]`` is a queue of (status, headers)."""

    def __init__(self, delay: float = 0.0):
        super().__init__()
        self.template = (FIX / "company_page.html").read_text(encoding="utf-8")
        self.delay = delay
        self.lock = threading.Lock()
        self.page_requests: list[str] = []
        self.list_requests = 0
        self.script: dict[str, list] = {}
        self.default_status = 200
        self.empty_pages = False
        self.active = self.max_active = 0
        self.headers_seen: list[dict] = []

    def handle(self, h, url):
        self.headers_seen.append({k.lower(): v for k, v in h.headers.items()} | {":path": h.path})
        if url.path == "/api/companies.json":
            with self.lock:
                self.list_requests += 1
            return self.reply(h, 200, json.dumps(COMPANIES), "application/json")
        m = re.fullmatch(r"/companies/([^/]+)", url.path)
        if not m:
            return self.reply(h, 404, "nope")
        slug = m.group(1)
        with self.lock:
            self.page_requests.append(slug)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            status, headers = self.script[slug].pop(0) if self.script.get(slug) else (self.default_status, {})
        try:
            if self.delay:
                time.sleep(self.delay)
            if status != 200:
                return self.reply(h, status, f"status {status}", headers=headers)
            if self.empty_pages:
                return self.reply(h, 200, "<html><body><p>Nothing to see</p></body></html>")
            page = self.template.replace("paul-witten-39579715a", f"paul-{slug}").replace("lucas-irvine", f"lucas-{slug}")
            return self.reply(h, 200, page)
        finally:
            with self.lock:
                self.active -= 1


@pytest.fixture
def yc_server():
    with FakeYC() as server:
        yield server


@pytest.fixture
def steer(monkeypatch):
    """Sends every connection the downloader opens to a stand-in server (see ``web_stand_in.Steer``)."""
    return Steer(monkeypatch)


@pytest.fixture
def localhost_is_public(monkeypatch):
    """The stand-in servers really are on this computer. Make the address check treat the *name* "localhost" as a public
    one (and nothing else: 127.0.0.1 and the rest are still looked up as they are), so that what the guards do with a
    redirect can be seen without the real internet."""
    from connecto import netguard

    real = netguard.resolve
    monkeypatch.setattr(netguard, "resolve", lambda host, port=None: [PUBLIC] if host == "localhost" else real(host, port))


@pytest.fixture
def sleeps(monkeypatch):
    """Record (and skip) every wait the YC fetcher asks for."""
    calls: list[float] = []
    monkeypatch.setattr(yc_mod, "_sleep", lambda s: calls.append(s))
    return calls


def yc_spec(server, **extra) -> dict:
    return {"type": "yc", "companies_json": f"{server.base}/api/companies.json", "allow_private": True,        # (the stand-in is on this computer)
            "company_page": f"{server.base}/companies/{{slug}}", "delay_seconds": [0, 0], "workers": 1, **extra}


# ----------------------------------------------------------------------------- YC: pure helpers
def test_batch_priority_numbers_sort_newest_first():
    p = yc_mod.batch_priority
    assert p("Winter 2026") == 20260 and p("Spring 2025") == 20251 and p("Summer 2026") == 20262 and p("Fall 2026") == 20263
    assert p("S26") == 20262 and p("x25") == 20251 and p("Unspecified") == 0 and p("") == 0 and p(None) == 0
    assert p("Fall 2026") > p("Summer 2026") > p("Winter 2026") > p("Fall 2025") > p("Spring 2025") > p("Winter 2012")


def test_company_view_flattens_a_directory_record():
    v = yc_mod.company_view(BY_SLUG["finlo"])
    assert v["tags"] == "Fintech|Artificial Intelligence|SaaS" and v["industries"] == "Fintech|Payments"
    assert v["team_size"] == "4" and v["top_company"] == "false" and v["batch_code"] == "S26"
    assert v["location"] == "Bengaluru, KA, India" and v["company"] == "Finlo"
    empty = yc_mod.company_view(BY_SLUG["nullco"])
    assert empty["team_size"] == "" and empty["tags"] == "" and empty["regions"] == ""
    assert yc_mod.taxonomy(COMPANIES, "regions")[0] == ("India", 4) or yc_mod.taxonomy(COMPANIES, "regions")[0][1] >= 4


def _row(slug: str, **extra) -> dict:
    return {**yc_mod.company_view(BY_SLUG[slug]), "founder_status": "active", **extra}


@pytest.mark.parametrize("spec,expected", [
    ({}, set(BY_SLUG)),
    ({"batches": ["S26"]}, {"finlo", "medscan", "gridly"}),
    ({"batches": "S26, Fall 2026"}, {"finlo", "medscan", "gridly", "threadly", "paylane", "kiranaai"}),
    ({"industries": ["fintech"]}, {"finlo", "paylane", "legacy-bank"}),                  # sub-industry / industries too
    ({"industries": ["payments"]}, {"finlo"}),
    ({"tags": ["ai"]}, {"finlo", "paylane", "kiranaai"}),                                # alias: Artificial Intelligence
    ({"regions": ["india"]}, {"finlo", "medscan", "kiranaai", "cropwise"}),
    ({"regions": ["Europe"]}, {"threadly", "oldco"}),
    ({"keywords": ["wardrobe"]}, {"threadly"}),
    ({"keywords": ["x-rays"]}, {"medscan"}),                                              # long description
    ({"keywords": ["ai"], "exclude_keywords": ["payments"]}, {"medscan", "paylane", "kiranaai"}),
    ({"company_status": ["acquired", "Public"]}, {"oldco", "legacy-bank"}),
    ({"only_top_companies": True}, {"oldco", "legacy-bank"}),
    ({"max_team_size": 5}, {"finlo", "threadly", "kiranaai", "quill"}),                  # unknown size (nullco) is not "small"
    ({"batches": ["S26"], "tags": ["ai"], "regions": ["india"]}, {"finlo"}),
])
def test_accepts_applies_the_company_filters_to_stored_rows(env, spec, expected):
    src = build_source({"type": "yc", **spec}, env.ctx())
    assert {slug for slug in BY_SLUG if src.accepts(_row(slug))} == expected
    # the prefilter over the directory records is the very same logic
    assert {c["slug"] for c in src.matching_companies(COMPANIES)} == expected


def test_accepts_former_founders_and_missing_fields(env):
    assert not build_source({"type": "yc"}, env.ctx()).accepts(_row("finlo", founder_status="former"))
    assert build_source({"type": "yc", "include_former_founders": True}, env.ctx()).accepts(_row("finlo", founder_status="former"))
    assert build_source({"type": "yc"}, env.ctx()).accepts({"founder_status": "unknown"})
    # a row that lacks what a filter needs is not accepted, whatever else it says
    bare = {"founder_status": "active", "batch": "Summer 2026"}
    assert build_source({"type": "yc", "batches": ["S26"]}, env.ctx()).accepts(bare)
    for needs in ({"regions": ["india"]}, {"tags": ["ai"]}, {"industries": ["fintech"]}, {"max_team_size": 10},
                  {"company_status": ["active"]}, {"only_top_companies": True}, {"keywords": ["ai"]}):
        assert not build_source({"type": "yc", **needs}, env.ctx()).accepts(bare), needs
    assert "S26" in build_source({"type": "yc", "batches": ["S26"], "tags": ["ai"]}, env.ctx()).describe()


# ----------------------------------------------------------------------------- YC: fetching
def test_company_filters_run_before_any_founder_page_is_fetched(env, yc_server, sleeps):
    spec = yc_spec(yc_server, batches=["S26", "Fall 2026"], tags=["ai"], regions=["india"])
    src, report = env.run(spec)
    assert yc_server.page_requests == ["kiranaai", "finlo"]            # 2 of 12 companies, newest batch first
    assert any("2 of 12" in n for n in report.notes)
    people = env.store.people("yc")
    assert len(people) == 4 and report.added == 4 and report.seen == 4
    assert report.skipped == 2                                         # the former founder without a LinkedIn link, per page
    paul = next(p for p in people if p["company_slug"] == "finlo" and p["full_name"] == "Paul Witten")
    assert paul["linkedin_url"] == "https://www.linkedin.com/in/paul-finlo/"
    assert paul["first_name"] == "Paul" and paul["title"] == "Founder/CEO" and paul["company"] == "Finlo"
    assert paul["batch"] == "Summer 2026" and paul["batch_code"] == "S26" and paul["priority"] == "20262"
    assert paul["tags"] == "Fintech|Artificial Intelligence|SaaS" and paul["regions"] == "India|South Asia"
    assert paul["industry"] == "Fintech" and paul["subindustry"] == "Fintech -> Payments"
    assert paul["team_size"] == "4" and paul["top_company"] == "false" and paul["company_status"] == "Active"
    assert paul["location"] == "Bengaluru, KA, India" and paul["one_liner"] == "Instant payouts for marketplaces"
    assert paul["founder_status"] == "active" and paul["twitter_url"] == "https://x.com/pdubz96"
    assert paul["company_url"] == "https://www.ycombinator.com/companies/finlo" and paul["website"] == "https://finlo.example"
    assert paul["bio"].startswith("Cofounder/CEO Tire Swing") and paul["source_type"] == "yc" and paul["source_id"] == "yc"
    assert next(p for p in people if p["company_slug"] == "kiranaai")["priority"] == "20263"


def test_without_filters_every_company_is_fetched_newest_first(env, yc_server, sleeps):
    env.run(yc_spec(yc_server))
    assert yc_server.page_requests == ["kiranaai", "paylane", "threadly", "finlo", "gridly", "medscan", "cropwise",
                                       "orbitx", "nullco", "quill", "legacy-bank", "oldco"]
    assert len(env.store.people("yc")) == 24


def test_limit_caps_companies_per_run(env, yc_server, sleeps):
    _, report = env.run(yc_spec(yc_server), limit=5)
    assert yc_server.page_requests == ["kiranaai", "paylane", "threadly", "finlo", "gridly"]
    assert any("5 this run" in n for n in report.notes)


def test_progress_makes_runs_resumable_and_refresh_starts_over(env, yc_server, sleeps):
    spec = yc_spec(yc_server)
    env.run(spec, limit=4)
    assert len(yc_server.page_requests) == 4
    env.run(spec)
    assert len(yc_server.page_requests) == 12 and len(set(yc_server.page_requests)) == 12   # nothing fetched twice
    env.run(spec)
    assert len(yc_server.page_requests) == 12                                               # all done: no page requests
    assert env.store.progress_done("yc") == set(BY_SLUG)
    assert yc_server.list_requests == 1                                                     # the list came from the cache
    _, report = env.run(spec, refresh=True)
    assert len(yc_server.page_requests) == 24 and yc_server.list_requests == 2
    assert report.added == 0 and len(env.store.people("yc")) == 24                          # re-fetched, not duplicated


def test_failed_pages_back_off_then_are_retried_on_the_next_run(env, yc_server, sleeps):
    yc_server.script["finlo"] = [(500, {})] * 4
    spec = yc_spec(yc_server, batches=["S26"], tags=["ai"])
    _, report = env.run(spec)
    assert yc_server.page_requests == ["finlo"] * 4                      # first try + 3 retries
    waits = [s for s in sleeps if s > 0]
    assert len(waits) == 3 and 4.0 <= waits[0] <= 6.5 and 16.0 <= waits[1] <= 26.0 and 48.0 <= waits[2] <= 78.0
    assert any("http 500" in e for e in report.errors)
    assert env.store.progress_done("yc") == set()
    env.run(spec)                                                        # the server has recovered
    assert yc_server.page_requests == ["finlo"] * 5 and env.store.progress_done("yc") == {"finlo"}


def test_429_honours_retry_after_and_then_succeeds(env, yc_server, sleeps):
    yc_server.script["finlo"] = [(429, {"Retry-After": "90"}), (503, {})]
    _, report = env.run(yc_spec(yc_server, batches=["S26"], tags=["ai"]))
    waits = [s for s in sleeps if s > 0]
    assert yc_server.page_requests == ["finlo"] * 3
    assert waits[0] == 90 and 16.0 <= waits[1] <= 26.0
    assert report.errors == [] and env.store.progress_done("yc") == {"finlo"}


def test_a_client_error_is_asked_for_once_and_a_failed_connection_says_why(env, yc_server, sleeps):
    for status in (400, 401, 405, 418, 451):
        yc_server.script["finlo"] = [(status, {})] * 4
        yc_server.page_requests.clear()
        _, report = env.run(yc_spec(yc_server, batches=["S26"], tags=["ai"]))
        assert yc_server.page_requests == ["finlo"], status                 # (a 4xx other than 429 will not change by asking again)
        assert any(f"http {status}" in e for e in report.errors)
    assert [s for s in sleeps if s > 0] == []
    spec = yc_spec(yc_server, batches=["S26"], tags=["ai"], company_page="http://127.0.0.1:9/{slug}")        # nothing listens there
    _, report = env.run(spec)
    assert any("finlo: network error" in e and "could not load http://127.0.0.1:9/finlo (ConnectionRefusedError)" in e for e in report.errors)
    assert len([s for s in sleeps if s > 0]) == 3                          # (a refused connection is retried, with a growing wait)


def test_five_403s_in_a_row_stop_the_run_and_point_to_the_browser_engine(env, yc_server, sleeps):
    yc_server.default_status = 403
    _, report = env.run(yc_spec(yc_server))
    assert len(yc_server.page_requests) == 5                              # stopped, did not hammer on
    assert any("engine: playwright" in n for n in report.notes)
    assert any("403" in e for e in report.errors)
    assert [s for s in sleeps if s > 0] == []                              # a 403 is never retried with a wait
    assert env.store.progress_done("yc") == set()


def test_a_success_resets_the_403_streak(env, yc_server, sleeps):
    blocked = ["kiranaai", "paylane", "threadly", "finlo", "medscan", "cropwise", "orbitx", "nullco"]
    yc_server.script = {slug: [(403, {})] for slug in blocked}           # 4 blocked, gridly ok, 4 blocked, quill ok, ...
    _, report = env.run(yc_spec(yc_server))
    assert len(yc_server.page_requests) == 12
    assert not any("engine: playwright" in n for n in report.notes)
    assert env.store.progress_done("yc") == {"gridly", "quill", "legacy-bank", "oldco"}
    assert any("8 pages failed" in n for n in report.notes)


def test_a_vanished_company_page_is_noted_and_not_retried(env, yc_server, sleeps):
    yc_server.script["oldco"] = [(404, {})]
    spec = yc_spec(yc_server, batches=["W12", "S15"])
    _, report = env.run(spec)
    assert report.errors == [] and any("404" in n for n in report.notes)
    assert env.store.progress_done("yc") == {"oldco", "legacy-bank"}
    env.run(spec)
    assert yc_server.page_requests.count("oldco") == 1


def test_workers_are_clamped_to_the_hard_ceiling(env, sleeps):
    with FakeYC(delay=0.05) as server:
        src = build_source(yc_spec(server, workers=10), env.ctx())
        assert src.workers == HARD["yc_workers_max"] == 2
        assert build_source(yc_spec(server, workers=0), env.ctx()).workers == 2   # 0 / missing means the default
        src.fetch(Sink(env.store, "yc", "yc", FetchReport("yc")))
        assert len(server.page_requests) == 12 and 1 <= server.max_active <= 2
    assert len(env.store.people("yc")) == 24


def test_company_list_cache_and_stale_fallback(env, yc_server):
    src = build_source(yc_spec(yc_server), env.ctx())
    data, how = src.load_companies()
    assert how == "downloaded" and len(data) == 12 and yc_server.list_requests == 1
    assert src.load_companies()[1] == "cache" and yc_server.list_requests == 1
    assert src.load_companies(force=True)[1] == "downloaded" and yc_server.list_requests == 2
    cache = env.home.data_dir / "yc_companies_all.json"
    old = time.time() - 3 * 86400
    os.utime(cache, (old, old))                                           # expired, and the source is unreachable now
    dead = build_source(yc_spec(yc_server, companies_json="http://127.0.0.1:9/none.json"), env.ctx())
    data, how = dead.load_companies()
    assert how.startswith("stale cache") and len(data) == 12
    cache.unlink()
    with pytest.raises(web_mod.FetchError, match="could not load"):
        dead.load_companies()


def test_the_yc_source_is_refused_a_private_address_unless_told_otherwise(env, yc_server, sleeps):
    spec = {k: v for k, v in yc_spec(yc_server).items() if k != "allow_private"}
    src = build_source(spec, env.ctx())
    with pytest.raises(web_mod.FetchError, match="private or internal address"):
        src.load_companies()
    assert yc_server.list_requests == 0
    (env.home.data_dir / "yc_companies_all.json").write_text(json.dumps(COMPANIES), encoding="utf-8")       # a list is there ...
    _, report = env.run(spec, limit=3)                                                                       # ... but its pages are not allowed
    assert yc_server.page_requests == [] and yc_server.list_requests == 0
    assert report.errors and all("private or internal address" in e for e in report.errors)
    assert [s for s in sleeps if s > 0] == []                                                                # (no waiting and retrying: it cannot change)
    assert env.store.progress_done("yc") == set()


def test_a_company_page_that_redirects_somewhere_internal_is_not_followed(env, yc_server, sleeps, steer, localhost_is_public):
    with Pages() as inside:
        inside.html("/secret", "<html>secret</html>")
        yc_server.script["finlo"] = [(302, {"Location": f"http://127.0.0.1:{inside.port}/secret"})]
        steer.to(yc_server.port)
        spec = {"type": "yc", "companies_json": f"http://localhost:{yc_server.port}/api/companies.json",
                "company_page": f"http://localhost:{yc_server.port}/companies/{{slug}}", "delay_seconds": [0, 0], "workers": 1}
        _, report = env.run(spec, limit=12)
        assert inside.hits == []
        assert any("finlo: network error" in e and "private or internal address" in e for e in report.errors)
        assert "finlo" not in env.store.progress_done("yc") and "medscan" in env.store.progress_done("yc")


def test_the_company_list_must_be_a_json_list_of_a_sensible_size(env, monkeypatch):
    with Pages() as srv:
        deep = "[" * 100_000 + "]" * 100_000
        for path, body, ctype in (("/object.json", '{"companies": []}', "application/json"), ("/page.html", "<html>hi</html>", "text/html"),
                                  ("/deep.json", deep, "application/json"), ("/empty.json", "", "application/json"),
                                  ("/list.json", json.dumps(COMPANIES), "text/plain")):
            srv.routes[path] = (200, body, ctype, {})
        srv.routes["/gone.json"] = (404, "no", "text/plain", {})

        def load(path):
            return build_source({"type": "yc", "companies_json": f"{srv.base}{path}", "allow_private": True}, env.ctx()).load_companies(force=True)

        for path in ("/object.json", "/page.html", "/deep.json", "/empty.json"):
            with pytest.raises(ValueError):
                load(path)
        with pytest.raises(ValueError, match="not a JSON array"):
            load("/object.json")
        with pytest.raises(ValueError, match="nested too deeply"):
            load("/deep.json")
        with pytest.raises(web_mod.FetchError, match="http 404"):
            load("/gone.json")
        assert len(load("/list.json")[0]) == 12                              # (what the server calls it does not matter)
        monkeypatch.setattr(yc_mod, "MAX_LIST_BYTES", 1000)
        data, how = load("/list.json")                                       # too big now: the list kept from before is used
        assert len(data) == 12 and how.startswith("stale cache") and "the company list is larger than 1000 bytes" in how
        (env.home.data_dir / "yc_companies_all.json").unlink()
        with pytest.raises(web_mod.FetchError, match="the company list is larger than 1000 bytes"):
            load("/list.json")


def test_the_company_list_is_given_more_room_and_time_than_an_ordinary_page(env, yc_server, monkeypatch):
    from connecto import netguard

    seen: dict = {}
    real = netguard.get

    def spy(url, **kwargs):
        seen.update(kwargs)
        return real(url, **kwargs)

    monkeypatch.setattr(netguard, "get", spy)
    build_source(yc_spec(yc_server), env.ctx()).load_companies(force=True)
    assert seen["what"] == "the company list" and seen["allow_private"] is True
    assert seen["max_bytes"] == yc_mod.MAX_LIST_BYTES > netguard.MAX_BYTES
    assert seen["total_seconds"] == yc_mod.LIST_SECONDS > netguard.TOTAL_SECONDS
    assert seen["timeout"] > netguard.TIMEOUT


def test_the_real_sized_company_list_is_accepted(env):
    """The directory's list is some 10 MB: more than a web page may be, and less than the list's own limit."""
    from connecto import netguard

    companies = [{"slug": f"company-{n}", "name": f"Company {n}", "batch": "Summer 2026", "one_liner": "x" * 100,
                  "long_description": "y" * 800, "tags": ["AI"], "regions": ["India"], "team_size": 4} for n in range(7000)]
    text = json.dumps(companies)
    assert netguard.MAX_BYTES < len(text) < yc_mod.MAX_LIST_BYTES
    with Pages() as srv:
        srv.routes["/big.json"] = (200, text, "application/json", {})
        src = build_source({"type": "yc", "companies_json": f"{srv.base}/big.json", "allow_private": True}, env.ctx())
        data, how = src.load_companies(force=True)
    assert how == "downloaded" and len(data) == 7000


def test_a_refused_or_broken_download_falls_back_to_the_list_that_was_kept(env, yc_server):
    cache = env.home.data_dir / "yc_companies_all.json"
    cache.write_text(json.dumps(COMPANIES[:3]), encoding="utf-8")
    old = time.time() - 3 * 86400
    os.utime(cache, (old, old))
    spec = {k: v for k, v in yc_spec(yc_server).items() if k != "allow_private"}
    data, how = build_source(spec, env.ctx()).load_companies()
    assert len(data) == 3 and how.startswith("stale cache") and "private or internal address" in how


def test_the_yc_user_agent_is_one_line_of_text(env, yc_server, sleeps):
    spec = yc_spec(yc_server, user_agent="Mozilla/5.0\r\nX-Injected: yes\r\n\r\nGET /evil HTTP/1.1")
    src = build_source(spec, env.ctx())
    assert src.user_agent == "Mozilla/5.0 X-Injected: yes GET /evil HTTP/1.1"
    env.run(spec, limit=2)
    assert yc_server.headers_seen and all("x-injected" not in h for h in yc_server.headers_seen)
    assert {h["user-agent"] for h in yc_server.headers_seen} == {"Mozilla/5.0 X-Injected: yes GET /evil HTTP/1.1"}


def test_yc_pages_are_asked_for_plainly(env, yc_server, sleeps):
    env.run(yc_spec(yc_server), limit=2)
    pages = [seen for seen in yc_server.headers_seen if seen[":path"].startswith("/companies/")]
    assert len(pages) == 2
    for seen in pages:
        assert seen["connection"] == "close" and seen["accept-encoding"] == "gzip" and seen["accept-language"] == "en-US,en;q=0.9"
        assert not {"cookie", "authorization", "referer"} & set(seen)


def test_a_layout_change_is_reported_and_not_remembered(env, yc_server, sleeps):
    yc_server.empty_pages = True
    _, report = env.run(yc_spec(yc_server), limit=4)
    assert any("layout" in e for e in report.errors)
    assert env.store.progress_done("yc") == set()                        # fetched again once the parser is fixed


@pytest.mark.browser
def test_playwright_engine_reads_pages_in_a_real_browser(env, yc_server, sleeps):
    pytest.importorskip("playwright")
    src, report = env.run(yc_spec(yc_server, engine="playwright", batches=["S26"], tags=["ai"]), headless=True)
    assert src.needs_browser and src.workers == 1
    assert yc_server.page_requests == ["finlo"]
    assert len(env.store.people("yc")) == 2 and report.errors == []
    assert (env.home.browser_dir / "yc").is_dir()                        # its own profile, never the LinkedIn one


# ----------------------------------------------------------------------------- CSV
def _csv(tmp_path, text: str | bytes, name: str = "people.csv") -> pathlib.Path:
    path = tmp_path / name
    path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
    return path


def test_linkedin_connections_export_with_its_notes_preamble():
    rows, notes = read_csv_rows(FIX / "leads.csv")
    assert [r["full_name"] for r in rows] == ["Jane Doe", "Raj Singh", "Zoë Müller"]
    jane, raj, zoe = rows
    assert jane["linkedin_url"] == "https://www.linkedin.com/in/jane-doe-123/"
    assert jane["company"] == "Acme Inc" and jane["title"] == "CTO" and jane["connected_on"] == "12 Jan 2024"
    assert jane["first_name"] == "Jane" and jane["last_name"] == "Doe"
    assert raj["company"] == "Foo, Bar & Co"                                        # quoted comma survives
    assert zoe["linkedin_url"] == "https://www.linkedin.com/in/zoe-mueller/" and zoe["first_name"] == "Zoë"
    assert all(r["degree"] == "1st" for r in rows)                                  # that export lists existing connections
    for r in rows:                                                                   # contact details are never stored
        assert not any("email" in k or "phone" in k for k in r) and "@" not in " ".join(r.values())
    assert "1 row had no LinkedIn profile URL" in notes
    assert any(n.startswith("1 row only had a company, school or Sales Navigator") for n in notes)
    assert any("never stored" in n and "Email Address" in n for n in notes)


APOLLO = (
    "First Name,Last Name,Title,Company,Company Name for Emails,Email,Person Linkedin Url,Company Linkedin Url,City,State,"
    "Country,Mobile Phone,Twitter Url,Seniority,Departments\n"
    "Ana,Lopez,VP Engineering,Zeta,Zeta SA,ana@zeta.example,http://www.linkedin.com/in/analopez,"
    "http://www.linkedin.com/company/zeta,Madrid,,Spain,+34 600 000 000,https://twitter.com/ana,vp,engineering\n"
    "Bob,Ng,Head of Sales,Yotta,Yotta Ltd,bob@yotta.example,,http://www.linkedin.com/company/yotta,Taipei,,Taiwan,,,head,sales\n"
)


def test_apollo_export_keeps_extras_and_drops_contact_columns(tmp_path):
    rows, notes = read_csv_rows(_csv(tmp_path, APOLLO))
    assert len(rows) == 1
    ana = rows[0]
    assert ana["linkedin_url"] == "https://www.linkedin.com/in/analopez/" and ana["full_name"] == "Ana Lopez"
    assert ana["company"] == "Zeta" and ana["title"] == "VP Engineering" and ana["location"] == "Madrid, Spain"
    assert ana["seniority"] == "vp" and ana["departments"] == "engineering" and ana["twitter_url"] == "https://twitter.com/ana"
    assert ana["company_name_for_emails"] == "Zeta SA"
    assert ana["company_linkedin_url"] == "http://www.linkedin.com/company/zeta"       # only the person's URL is the URL
    assert "ana@zeta.example" not in json.dumps(ana) and "+34" not in json.dumps(ana)
    assert any("Email" in n and "Mobile Phone" in n and "never stored" in n for n in notes)
    assert any(n.startswith("1 row only had a company") for n in notes)                # Bob: company URL, no person URL


SALESNAV = (
    "Lead First Name,Lead Last Name,Lead Job Title,Company Name,Company Linkedin Url,Sales Navigator Profile Url,"
    "Linkedin Profile Url,Location,Connection Degree\n"
    'Carla,Mendes,Chief Product Officer,Orbit,https://www.linkedin.com/company/orbit/,'
    '"https://www.linkedin.com/sales/lead/ACwAAB123,NAME_SEARCH,xyz",'
    "https://www.linkedin.com/in/carla-mendes-77?miniProfileUrn=urn%3Ali%3Afs_miniProfile%3AACoAAB,"
    '"Lisbon, Portugal",2nd\n'
    'Dan,Ito,CTO,Quark,https://www.linkedin.com/company/quark/,"https://www.linkedin.com/sales/lead/ACwAAC456,NAME_SEARCH,abc",,'
    '"Tokyo, Japan",3rd\n'
)


def test_sales_navigator_tool_export_prefers_the_personal_url_over_sales_nav_links(tmp_path):
    rows, notes = read_csv_rows(_csv(tmp_path, SALESNAV))
    assert len(rows) == 1
    carla = rows[0]
    assert carla["linkedin_url"] == "https://www.linkedin.com/in/carla-mendes-77/"
    assert carla["full_name"] == "Carla Mendes" and carla["title"] == "Chief Product Officer" and carla["company"] == "Orbit"
    assert carla["location"] == "Lisbon, Portugal" and carla["connection_degree"] == "2nd"
    assert carla["sales_navigator_profile_url"].startswith("https://www.linkedin.com/sales/lead/")   # kept as an extra
    assert any(n.startswith("1 row only had a company, school or Sales Navigator") for n in notes)


@pytest.mark.parametrize("delimiter", [";", "\t", "|", ","])
def test_delimiter_is_sniffed_and_bom_and_crlf_are_tolerated(tmp_path, delimiter):
    lines = [["Name", "Company", "Profile URL", "Mobile"],
             ["Eva Rossi", "Nova", "linkedin.com/in/eva-rossi", "+39 333 1"],
             ["Luigi Verdi", "Nova", "https://www.linkedin.com/in/luigi-verdi/", ""]]
    text = "\r\n".join(delimiter.join(c for c in line) for line in lines) + "\r\n"
    path = _csv(tmp_path, b"\xef\xbb\xbf" + text.encode("utf-8"))
    rows, _ = read_csv_rows(path)
    assert [r["full_name"] for r in rows] == ["Eva Rossi", "Luigi Verdi"]                  # "﻿Name" did not break the header
    assert rows[0]["linkedin_url"] == "https://www.linkedin.com/in/eva-rossi/" and rows[0]["company"] == "Nova"
    assert all("mobile" not in r for r in rows)


def test_a_profile_url_hiding_in_any_cell_is_found(tmp_path):
    text = ('Name,Notes,Source\n'
            'Dana Kim,"met at the conf, see https://www.linkedin.com/in/dana-kim-9/ for details",event\n'
            "Eli Park,https://linkedin.com/in/eli-park,\n"
            "Fay Roy,no link here,x\n")
    rows, notes = read_csv_rows(_csv(tmp_path, text))
    assert [(r["full_name"], r["linkedin_url"]) for r in rows] == [
        ("Dana Kim", "https://www.linkedin.com/in/dana-kim-9/"), ("Eli Park", "https://www.linkedin.com/in/eli-park/")]
    assert rows[0]["notes"].startswith("met at the conf") and rows[0]["source"] == "event"   # text around the URL is kept
    assert "notes" not in rows[1]                                                            # a cell that was only the URL is not
    assert notes == ["1 row had no LinkedIn profile URL"]


def test_column_overrides_and_unknown_override_notes(tmp_path):
    text = "Contact Person,Profile,Org,Misc\nGia Bell,https://www.linkedin.com/in/gia-bell,Helix,x\n"
    path = _csv(tmp_path, text)
    plain, plain_notes = read_csv_rows(path)
    assert plain[0]["full_name"] == "" and any("no name" in n for n in plain_notes)         # not guessable from these headers
    rows, notes = read_csv_rows(path, columns={"full_name": "Contact Person", "linkedin_url": "profile", "company": "ORG",
                                               "title": "Nope"})
    assert rows[0]["full_name"] == "Gia Bell" and rows[0]["company"] == "Helix" and rows[0]["misc"] == "x"
    assert any("'Nope'" in n and "not found" in n for n in notes)


def test_other_encodings_and_bad_bytes_do_not_break_the_import(tmp_path):
    latin = _csv(tmp_path, "Name,URL\nJosé García,https://www.linkedin.com/in/jose-garcia\n".encode("cp1252"), "latin.csv")
    assert read_csv_rows(latin)[0][0]["full_name"] == "José García"
    utf16 = _csv(tmp_path, "Name,URL\nÅsa Berg,https://www.linkedin.com/in/asa-berg\n".encode("utf-16"), "utf16.csv")
    assert read_csv_rows(utf16)[0][0]["full_name"] == "Åsa Berg"
    bad = _csv(tmp_path, b"Name,URL\nBad \x81\x8d Name,https://www.linkedin.com/in/bad-bytes\n"
                         b"Good Name,https://www.linkedin.com/in/good-name\n", "bad.csv")
    rows, _ = read_csv_rows(bad)
    assert len(rows) == 2 and rows[1]["full_name"] == "Good Name"
    assert read_csv_rows(_csv(tmp_path, b"", "empty.csv")) == ([], ["the file is empty"])


def test_urls_only_duplicates_and_reserved_column_names(tmp_path):
    rows, notes = read_csv_rows(_csv(tmp_path, "https://www.linkedin.com/in/a-one\nhttps://www.linkedin.com/in/b-two\n"))
    assert [r["linkedin_url"] for r in rows] == ["https://www.linkedin.com/in/a-one/", "https://www.linkedin.com/in/b-two/"]
    assert "no header row found - read every line as a person" in notes and any("2 rows have no name" in n for n in notes)
    text = ("Name,URL,Key,Source Type,Priority\nAmy,linkedin.com/in/amy,k1,x,7\n\nAmy Again,https://www.linkedin.com/in/AMY/,k2,y,1\n")
    rows, notes = read_csv_rows(_csv(tmp_path, text, "dup.csv"))
    assert len(rows) == 1 and "1 row repeated a person already in the file" in notes
    assert rows[0]["csv_key"] == "k1" and rows[0]["csv_source_type"] == "x" and rows[0]["priority"] == "7"   # no clash with store fields
    assert "key" not in rows[0] and "source_type" not in rows[0]


def test_csv_source_resolves_paths_and_reports_problems(env, tmp_path, monkeypatch):
    import shutil

    shutil.copy(FIX / "leads.csv", env.home.imports_dir / "leads.csv")
    cfg = {**env.cfg, "sources": [{"type": "csv", "path": "leads.csv"}]}
    first = fetch_sources(env.home, cfg, env.store, log=env.logs.append)
    assert [(r.source, r.added, r.seen) for r in first] == [("csv:leads.csv", 3, 3)]
    assert any("no LinkedIn profile URL" in n for n in first[0].notes)
    person = env.store.people("csv")[0]
    assert person["source_id"] == "csv:leads.csv" and person["source_type"] == "csv"
    again = fetch_sources(env.home, cfg, env.store, log=env.logs.append)
    assert (again[0].added, again[0].updated, again[0].seen) == (0, 0, 3)               # idempotent

    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "mine.csv").write_text("Name,URL\nIvy Lu,linkedin.com/in/ivy-lu\n")
    src = build_source({"type": "csv", "path": "~/mine.csv"}, env.ctx())
    assert src.resolve_path() == tmp_path / "mine.csv" and src.source_id == "csv:mine.csv"
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "here.csv").write_text("Name,URL\nJo Wu,linkedin.com/in/jo-wu\n")
    monkeypatch.chdir(tmp_path / "sub")
    assert build_source({"type": "csv", "path": "here.csv"}, env.ctx()).resolve_path().resolve() == \
        (tmp_path / "sub" / "here.csv").resolve()                                           # then the current folder
    _, report = env.run({"type": "csv", "path": "nope.csv"})
    assert "not found" in report.errors[0] and report.added == 0
    _, report = env.run({"type": "csv"})
    assert "no `path`" in report.errors[0]
    _, report = env.run({"type": "csv", "path": str(tmp_path / "sub")})
    assert report.errors and "folder" in report.errors[0]


# ----------------------------------------------------------------------------- web pages
TEAM_URL = "https://finlo.example/team"
TEAM_HTML = (FIX / "team_page.html").read_text(encoding="utf-8")


@pytest.fixture
def web_sleeps(monkeypatch):
    calls: list[float] = []
    monkeypatch.setattr(web_mod, "_sleep", lambda s: calls.append(s))
    return calls


def _names(html: str, base: str = "https://x.example/") -> set[str]:
    return {r["full_name"] for r in web_mod.extract_people(html, base)}


def test_extract_people_from_a_realistic_team_page():
    rows, stats = web_mod.extract_people_ex(TEAM_HTML, TEAM_URL)
    people = {r["full_name"]: r for r in rows}
    assert len(rows) == 9 and set(people) == {"Asha Verma", "Ravi Kulkarni", "Meera Iyer", "Karan Malhotra", "Priya Nair",
                                              "Dr. Maya Okafor-Lind", "Sana Sheikh", "Lakshmi Rao", "Tara Bose"}
    assert stats == {"links": 9, "nameless": 1}                      # the footer link has no name anywhere near it
    for r in rows:
        assert r["source_url"] == TEAM_URL and r["page_title"] == "Our Team | Finlo"
        assert "/company/" not in r["linkedin_url"] and "share" not in r["linkedin_url"] and "pulse" not in r["linkedin_url"]
    asha = people["Asha Verma"]                                      # icon-only link, name in a heading, listed twice + JSON-LD
    assert asha["linkedin_url"] == "https://www.linkedin.com/in/asha-verma-1a2b3c/" and asha["headline"] == "Co-founder & CEO"
    assert asha["title"] == "Co-founder and CEO" and asha["first_name"] == "Asha"
    ravi = people["Ravi Kulkarni"]                                   # link text is just "LinkedIn": the name comes from .member__name
    assert ravi["headline"] == "CTO at Finlo | ex-Razorpay" and (ravi["title"], ravi["company"]) == ("CTO", "Finlo")
    meera = people["Meera Iyer"]                                     # only the aria-label names her
    assert meera["headline"] == "Design Lead @ PayNest" and (meera["title"], meera["company"]) == ("Design Lead", "PayNest")
    assert people["Karan Malhotra"]["headline"] == "Founder at Lumen Labs"           # title attribute
    assert people["Priya Nair"]["headline"] == "UX Researcher"                       # avatar alt text
    maya = people["Dr. Maya Okafor-Lind"]                            # credentials cut, old-style /pub/ URL kept
    assert maya["linkedin_url"] == "https://www.linkedin.com/pub/maya-okafor-lind/1a/2b3/4c5/" and maya["headline"] == "Chief Scientist"
    sana, lakshmi = people["Sana Sheikh"], people["Lakshmi Rao"]    # list items: name is the link text, role follows it
    assert (sana["title"], sana["company"]) == ("Design Manager", "Cedar")
    assert (lakshmi["title"], lakshmi["company"]) == ("Partner", "Seed Fund")
    tara = people["Tara Bose"]                                       # only in JSON-LD (sameAs on in.linkedin.com)
    assert tara["linkedin_url"] == "https://www.linkedin.com/in/tara-bose-9s8t7u/"
    assert (tara["title"], tara["company"], tara["headline"]) == ("Principal Designer", "Quill", "Principal Designer at Quill")
    assert "Omar Farouk" not in people                               # no link, no person
    assert len({person_key(r) for r in rows}) == 9
    assert all(len(r["headline"]) <= 160 for r in rows)


def test_names_are_taken_from_the_nearest_card_and_never_from_a_neighbour():
    two_cards = ('<div class="grid"><div class="c"><h3>Alice Smith</h3><a href="https://www.linkedin.com/in/alice-s"><svg></svg></a></div>'
                 '<div class="c"><a href="https://www.linkedin.com/in/bob-t"><svg></svg></a></div></div>')
    rows, stats = web_mod.extract_people_ex(two_cards, "https://x.example/")
    assert [r["full_name"] for r in rows] == ["Alice Smith"] and stats["nameless"] == 1       # Bob is not called Alice
    page_heading_only = '<main><h1>About Me</h1><a href="https://www.linkedin.com/in/solo-person"><i></i></a></main>'
    assert _names(page_heading_only) == set()                                                  # "About Me" is not a name
    assert _names('<main><h1>Meet the Team</h1><h2>Rae Chen</h2><a href="https://www.linkedin.com/in/rae-chen">in</a></main>') == {"Rae Chen"}
    # the heading nearest to the link wins over one further away
    nested = ('<section><h2>Sam Ortiz</h2><div><h4>Kim Lee</h4><p>CEO</p><a href="https://www.linkedin.com/in/kim-lee">'
              '<svg></svg></a></div></section>')
    assert _names(nested) == {"Kim Lee"}
    # scripts and styles inside a card never leak into the headline
    card = ('<div><h3>Una Vale</h3><style>.x{color:red}</style><script>var a = "noise"</script><p>Head of Ops</p>'
            '<a href="https://www.linkedin.com/in/una-vale"><svg></svg></a></div>')
    assert web_mod.extract_people(card, "https://x.example/")[0]["headline"] == "Head of Ops"


@pytest.mark.parametrize("text,ok", [
    ("Jane Doe", True), ("Dr. Maya Okafor-Lind", True), ("Anne-Marie O'Brien", True), ("Zoë Müller", True),
    ("Jan van der Berg", True), ("J. R. R. Tolkien", True), ("Иван Петров", True),
    ("LinkedIn", False), ("Jane", False), ("View Profile", False), ("Meet the Team", False), ("Chief Technology Officer", False),
    ("Contact Us", False), ("Jane Doe 3rd", False), ("a b c d e", False), ("Follow us on LinkedIn", False), ("", False),
])
def test_looks_like_name(text, ok):
    assert web_mod.looks_like_name(text) is ok


@pytest.mark.parametrize("label,name", [
    ("Jane Doe on LinkedIn", "Jane Doe"), ("LinkedIn profile of Jane Doe", "Jane Doe"), ("View Jane Doe's profile", "Jane Doe"),
    ("Connect with Jane Doe on LinkedIn", "Jane Doe"), ("Jane Doe | LinkedIn", "Jane Doe"), ("Jane Doe’s LinkedIn profile", "Jane Doe"),
    ("LinkedIn: Jane Doe", "Jane Doe"), ("Jane Doe, PhD", "Jane Doe"), ("Jane Doe (she/her)", "Jane Doe"),
    ("Jane Doe – Founder & CEO", "Jane Doe"), ("LinkedIn", ""), ("LinkedIn profile", ""), ("Follow us", ""), (None, ""),
])
def test_name_from_label(label, name):
    assert web_mod.name_from_label(label) == name


def test_json_ld_variants_and_broken_blocks():
    html = ('<script type="application/ld+json">[{"@type": ["Thing", "Person"], "name": "Lia Fox", "jobTitle": "Editor",'
            '"worksFor": [{"@type": "Organization", "name": "The Gazette"}], "url": "https://www.linkedin.com/in/lia-fox"},'
            '{"@type": "Person", "name": "No Link", "sameAs": "https://twitter.com/nolink"},'
            '{"@type": "Organization", "name": "Acme", "sameAs": ["https://www.linkedin.com/company/acme"]}]</script>'
            '<script type="application/ld+json">{"broken": </script><script type="application/ld+json"></script>')
    rows = web_mod.extract_people(html, "https://x.example/")
    assert len(rows) == 1
    assert (rows[0]["full_name"], rows[0]["title"], rows[0]["company"]) == ("Lia Fox", "Editor", "The Gazette")
    assert rows[0]["linkedin_url"] == "https://www.linkedin.com/in/lia-fox/"
    assert web_mod.extract_people("", "https://x.example/") == [] and web_mod.extract_people(b"<html></html>", "") == []


# ---- the SSRF guard
@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://127.0.0.1:8080/x", "http://localhost/", "http://10.0.0.5/admin", "http://192.168.1.10/",
    "http://172.16.0.1/", "http://169.254.169.254/latest/meta-data/", "http://[::1]/", "http://0.0.0.0/",
    "http://2130706433/", "http://0x7f.1/", "http://100.64.0.1/", "http://[::ffff:127.0.0.1]/", "https://[fe80::1]/",
])
def test_private_and_internal_addresses_are_refused(url):
    with pytest.raises(ValueError, match="private or internal"):
        web_mod.safe_http_url(url)
    assert web_mod.safe_http_url(url, allow_private=True)                      # only an explicit opt-in lets them through


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://example.com/x", "javascript:alert(1)", "gopher://example.com/", "data:text/html,hi",
    "http://user:secret@example.com/", "https://@example.com/", "http://", "", "   ", "http://exa mple.com/",
])
def test_non_web_urls_and_embedded_credentials_are_always_refused(url):
    with pytest.raises(ValueError):
        web_mod.safe_http_url(url, allow_private=True)


def test_dns_names_are_checked_by_what_they_resolve_to():
    def resolver(host, port=None):
        return {"intranet.test": ["10.1.2.3"], "mixed.test": ["93.184.216.34", "127.0.0.1"], "public.test": ["93.184.216.34"],
                "v6.test": ["2606:2800:220:1:248:1893:25c8:1946"]}[host]

    assert web_mod.safe_http_url("https://public.test/a#frag", resolver=resolver) == "https://public.test/a"
    assert web_mod.safe_http_url("https://v6.test/", resolver=resolver)
    for host in ("intranet.test", "mixed.test"):
        with pytest.raises(ValueError, match="private or internal"):
            web_mod.safe_http_url(f"https://{host}/", resolver=resolver)
    with pytest.raises(ValueError, match="cannot look up"):
        web_mod.safe_http_url("https://nowhere.test/", resolver=lambda h, p=None: (_ for _ in ()).throw(OSError("no such host")))


# ---- the downloader
def test_fetch_html_reads_a_page_with_an_honest_user_agent_and_follows_a_few_redirects():
    with Pages() as srv:
        srv.html("/team", TEAM_HTML)
        srv.redirect("/old", "/team")
        srv.redirect("/a", "/b")
        srv.redirect("/b", "/c")
        srv.redirect("/c", f"{srv.base}/team")
        final, html = web_mod.fetch_html(f"{srv.base}/old", allow_private=True)
        assert final == f"{srv.base}/team" and "Meet the Team" in html
        assert web_mod.fetch_html(f"{srv.base}/a", allow_private=True)[0] == f"{srv.base}/team"      # exactly 3 redirects: fine
        srv.redirect("/r0", "/r1")
        srv.redirect("/r1", "/r2")
        srv.redirect("/r2", "/r3")
        srv.redirect("/r3", "/r4")
        srv.html("/r4", "<html></html>")
        with pytest.raises(web_mod.FetchError, match="too many redirects"):
            web_mod.fetch_html(f"{srv.base}/r0", allow_private=True)                                 # a 4th is refused
        assert all(ua == f"Mozilla/5.0 (compatible; Connecto/{web_mod.__version__}; +https://github.com/)" for ua in srv.user_agents)
        assert "Connecto/" in srv.user_agents[0] and "compatible" in srv.user_agents[0]


def test_fetch_html_refuses_non_html_missing_oversized_and_broken_responses():
    with Pages() as srv:
        srv.routes["/doc.pdf"] = (200, b"%PDF-1.4 ...", "application/pdf", {})
        srv.routes["/json"] = (200, '{"a": 1}', "application/json", {})
        srv.routes["/plain"] = (200, "<html>looks like a page</html>", "text/plain", {})
        srv.routes["/typeless"] = (200, "<html>looks like a page</html>", "", {})
        srv.html("/big", "<html>" + "x" * 5000 + "</html>")
        srv.html("/boom", "oops", status=500)
        srv.redirect("/nowhere", "", 302)
        srv.routes["/latin"] = (200, "<html><title>Café</title></html>".encode("latin-1"), "text/html; charset=iso-8859-1", {})
        with pytest.raises(web_mod.FetchError, match="not an HTML page"):
            web_mod.fetch_html(f"{srv.base}/doc.pdf", allow_private=True)
        with pytest.raises(web_mod.FetchError, match="not an HTML page"):
            web_mod.fetch_html(f"{srv.base}/json", allow_private=True)
        for path, kind in (("/plain", "text/plain"), ("/typeless", "missing")):
            with pytest.raises(web_mod.FetchError, match=f"not an HTML page \\(content type {kind}\\)"):
                web_mod.fetch_html(f"{srv.base}{path}", allow_private=True)
        with pytest.raises(web_mod.FetchError, match="larger than 1000 bytes"):
            web_mod.fetch_html(f"{srv.base}/big", allow_private=True, max_bytes=1000)
        with pytest.raises(web_mod.FetchError, match="http 500"):
            web_mod.fetch_html(f"{srv.base}/boom", allow_private=True)
        with pytest.raises(web_mod.FetchError, match="redirected nowhere"):
            web_mod.fetch_html(f"{srv.base}/nowhere", allow_private=True)
        with pytest.raises(web_mod.FetchError, match="could not load"):
            web_mod.fetch_html("http://127.0.0.1:9/", allow_private=True)
        assert "Café" in web_mod.fetch_html(f"{srv.base}/latin", allow_private=True)[1]
        assert len(web_mod.fetch_html(f"{srv.base}/big", allow_private=True)[1]) > 5000          # the cap is 5 MB by default


# ---- the source
def test_webpage_source_end_to_end(env, web_sleeps):
    with Pages() as srv:
        srv.html("/team", TEAM_HTML)
        srv.html("/other", '<body><div class="person"><h3>Zed Alpha</h3><p>Advisor</p>'
                           '<a href="https://www.linkedin.com/in/zed-alpha">x</a></div></body>')
        srv.html("/empty", "<html><body>Nothing here</body></html>")
        spec = {"type": "webpage", "allow_private": True,
                "urls": [f"{srv.base}/team", f"{srv.base}/other", f"{srv.base}/empty", f"{srv.base}/gone"]}
        report = fetch_sources(env.home, {**env.cfg, "sources": [spec]}, env.store, log=env.logs.append)[0]
        assert report.source == "webpage:127.0.0.1"
        assert report.added == 10 and report.seen == 10 and report.skipped == 0
        assert any("/empty" in n and "render: true" in n for n in report.notes)          # the hint for JavaScript pages
        assert any("/gone" in e and "http 404" in e for e in report.errors)
        assert any("1 LinkedIn links on 127.0.0.1 had no findable name" in n for n in report.notes)
        assert report.notes[0] == "read 3 of 4 pages, 10 people"
        assert len(web_sleeps) == 3 and all(1.0 <= s <= 2.0 for s in web_sleeps)         # a pause between URLs, never a burst
        assert srv.hits.count("/team") == 1                                               # one request per URL
        people = env.store.people("webpage")
        assert len(people) == 10 and {p["source_id"] for p in people} == {"webpage:127.0.0.1"}
        zed = next(p for p in people if p["full_name"] == "Zed Alpha")
        assert zed["source_url"] == f"{srv.base}/other" and zed["headline"] == "Advisor"


def test_webpage_source_refuses_private_hosts_by_default_and_honours_limit_and_stop(env, web_sleeps):
    _, report = env.run({"type": "webpage", "urls": ["http://127.0.0.1:9/team", "http://10.0.0.5/x"]})
    assert len(report.errors) == 2 and all("private or internal" in e for e in report.errors) and report.added == 0
    with Pages() as srv:
        srv.html("/a", '<div><h3>Ann One</h3><a href="https://www.linkedin.com/in/ann-one">in</a></div>')
        srv.html("/b", '<div><h3>Bea Two</h3><a href="https://www.linkedin.com/in/bea-two">in</a></div>')
        spec = {"type": "webpage", "allow_private": True, "urls": f"{srv.base}/a  {srv.base}/b"}     # whitespace-separated is fine
        env.run(spec, limit=1)
        assert srv.hits == ["/a"]
        env.run(spec, stop=lambda: True)
        assert srv.hits == ["/a"]
    _, report = env.run({"type": "webpage"})
    assert "no `urls`" in report.errors[0]
    src = build_source({"type": "webpage", "urls": ["https://Team.Example.com/x", "https://b.example"], "render": True}, env.ctx())
    assert src.source_id == "webpage:team.example.com" and src.needs_browser and "rendered" in src.describe()
    assert not build_source({"type": "webpage", "urls": ["https://a.example"]}, env.ctx()).needs_browser


@pytest.mark.browser
def test_render_mode_reads_pages_built_with_javascript(env, web_sleeps):
    pytest.importorskip("playwright")
    js_page = ('<html><head><title>JS Team</title></head><body><div id="team">Loading...</div><script>'
               "document.getElementById('team').innerHTML = '<div class=\"card\"><h3>Jess Park</h3><p>CTO at Nimbus</p>"
               "<a href=\"https://www.linkedin.com/in/jess-park-12/\"><svg></svg></a></div>';</script></body></html>")
    with Pages() as srv:
        srv.html("/js", js_page)
        spec = {"type": "webpage", "urls": [f"{srv.base}/js"], "allow_private": True}
        _, plain = env.run(spec)                                           # plain HTTP sees only "Loading..."
        assert plain.added == 0 and any("render: true" in n for n in plain.notes)
        src, report = env.run({**spec, "render": True}, headless=True)
        assert report.errors == [] and report.added == 1
        jess = env.store.people("webpage")[0]
        assert (jess["full_name"], jess["title"], jess["company"]) == ("Jess Park", "CTO", "Nimbus")
        assert (env.home.browser_dir / "webpage").is_dir()                  # its own profile, never the LinkedIn one


CARD = ("document.getElementById('team').innerHTML = '<div class=\\'card\\'><h3>Jess Park</h3><p>CTO at Nimbus</p>"
        "<a href=\\'https://www.linkedin.com/in/jess-park-12/\\'><svg></svg></a></div>';")


def card_page(extra_head: str = "", extra_body: str = "") -> str:
    """A page that builds one person's card with JavaScript (so the browser has to run it), plus whatever else a test adds."""
    return (f'<html><head><title>JS Team</title>{extra_head}</head><body><div id="team">Loading...</div>{extra_body}'
            f"<script>{CARD}</script></body></html>")


@pytest.mark.browser
def test_render_mode_refuses_a_page_that_redirects_to_an_internal_address(env, web_sleeps, localhost_is_public):
    pytest.importorskip("playwright")
    with Pages() as site, Pages() as inside:
        inside.html("/secret", card_page())
        site.redirect("/start", f"http://127.0.0.1:{inside.port}/secret")
        site.redirect("/start-by-name", f"http://[::ffff:127.0.0.1]:{inside.port}/secret")
        spec = {"type": "webpage", "render": True, "urls": [f"http://localhost:{site.port}/start", f"http://localhost:{site.port}/start-by-name"]}
        _, report = env.run(spec, headless=True)
        assert inside.hits == []                                              # never asked
        assert report.added == 0 and len(report.errors) == 2


@pytest.mark.browser
def test_render_mode_refuses_internal_redirects_of_everything_a_page_loads(env, web_sleeps, localhost_is_public):
    pytest.importorskip("playwright")
    with Pages() as site, Pages() as inside:
        for path in ("/lib.js", "/pixel.png", "/frame", "/api.json", "/style.css"):
            site.redirect(path, f"http://127.0.0.1:{inside.port}{path}")
        site.html("/page", card_page(
            extra_head='<link rel="stylesheet" href="/style.css"><script src="/lib.js"></script>',
            extra_body='<img src="/pixel.png"><iframe src="/frame"></iframe>'
                       "<script>fetch('/api.json').then(r => r.text()).catch(e => 'blocked');</script>"))
        _, report = env.run({"type": "webpage", "render": True, "urls": [f"http://localhost:{site.port}/page"]}, headless=True)
        assert report.errors == [] and report.added == 1                       # the page itself is read ...
        assert inside.hits == []                                              # ... and nothing it loads reached the inside
        assert {"/page", "/lib.js", "/api.json", "/frame", "/style.css"} <= set(site.hits)
        assert "/pixel.png" not in site.hits                                  # (pictures are not fetched at all)


@pytest.mark.browser
def test_render_mode_still_follows_an_ordinary_redirect(env, web_sleeps, localhost_is_public):
    pytest.importorskip("playwright")
    with Pages() as site:
        site.html("/team", card_page())
        site.redirect("/old", "/team", 301)
        _, report = env.run({"type": "webpage", "render": True, "urls": [f"http://localhost:{site.port}/old"]}, headless=True)
        assert report.errors == [] and report.added == 1
        assert [h for h in site.hits if h != "/favicon.ico"] == ["/old", "/team"]


@pytest.mark.browser
def test_render_mode_does_not_let_a_page_open_a_web_socket(env, web_sleeps, localhost_is_public):
    pytest.importorskip("playwright")
    from raw_server import RawServer

    canary = RawServer(lambda conn, request, srv: None).start()
    try:
        with Pages() as site:
            site.html("/page", card_page(extra_body=f"<script>try {{ new WebSocket('ws://127.0.0.1:{canary.port}/'); }} catch (e) {{}}</script>"))
            _, report = env.run({"type": "webpage", "render": True, "urls": [f"http://localhost:{site.port}/page"]}, headless=True)
            assert report.errors == [] and report.added == 1                   # (the script after the attempt did run)
        time.sleep(0.3)
        assert canary.requests == []
    finally:
        canary.stop()


@pytest.mark.browser
def test_render_mode_does_not_let_a_page_install_a_service_worker(env, web_sleeps, localhost_is_public):
    pytest.importorskip("playwright")
    with Pages() as site:
        site.routes["/sw.js"] = (200, "self.addEventListener('fetch', function () {});", "application/javascript", {})
        site.html("/page", card_page(extra_body="<script>if (navigator.serviceWorker) navigator.serviceWorker.register('/sw.js').catch(function () {});</script>"))
        _, report = env.run({"type": "webpage", "render": True, "urls": [f"http://localhost:{site.port}/page"]}, headless=True)
        assert report.errors == [] and report.added == 1
        time.sleep(0.3)
        assert "/sw.js" not in site.hits


# ============================================================================= LinkedIn search: URLs and parsing
SEARCH = "https://www.linkedin.com/search/results/people/"
LI = "https://www.linkedin.com"


def test_validate_search_url_accepts_only_linkedin_people_searches():
    v = search_mod.validate_search_url
    assert v(SEARCH + "?keywords=design") == SEARCH + "?keywords=design"
    assert v("  https://www.linkedin.com/search/results/people?keywords=x#frag ") == LI + "/search/results/people?keywords=x"
    assert v("HTTPS://WWW.LinkedIn.com/search/results/people/?keywords=a") == SEARCH + "?keywords=a"
    assert v("https://www.linkedin.com:443/search/results/people/?keywords=a") == SEARCH + "?keywords=a"
    pasted = SEARCH + "?geoUrn=%5B%22102713980%22%5D&keywords=design%20lead&origin=FACETED_SEARCH"
    assert v(pasted) == pasted                                   # percent-encoding is kept exactly as pasted
    for bad in (None, "", "   ", "design", "javascript:alert(1)", "file:///etc/passwd",
                "http://www.linkedin.com/search/results/people/?keywords=a",                 # not https
                "ftp://www.linkedin.com/search/results/people/",
                "https://linkedin.com/search/results/people/?keywords=a",                    # not the www host
                "https://www.linkedin.com.evil.example/search/results/people/?keywords=a",
                "https://evil.example/search/results/people/?keywords=a",
                "https://www.linkedin.com@evil.example/search/results/people/",
                "https://user:pw@www.linkedin.com/search/results/people/",
                "https://www.linkedin.com:8443/search/results/people/?keywords=a",
                "https://www.linkedin.com:abc/search/results/people/",
                "https://www.linkedin.com/search/results/companies/?keywords=a",             # a different search
                "https://www.linkedin.com/search/results/peoplexyz/",
                "https://www.linkedin.com/in/someone",
                "https://www.linkedin.com/search/results/people/../../in/someone",
                "https://www.linkedin.com/search/results/people/%2e%2e/feed/",
                # a browser turns "\" into "/" and resolves dots, so none of these may be anything but the search page itself
                "https://www.linkedin.com/search/results/people/..\\..\\..\\feed/",
                "https://www.linkedin.com/search/results/people/.\\x", "https://www.linkedin.com/search/results/people\\..\\feed",
                "https://www.linkedin.com/search/results/people/%5c..%5cfeed/", "https://www.linkedin.com/search/results/people/./",
                "https://www.linkedin.com/search/results/people//", "https://www.linkedin.com/search/results/people/x",
                "https://www.linkedin.com/search/results/people/?keywords=a b"):
        with pytest.raises(ValueError):
            v(bad)


def test_build_search_url_and_with_page():
    b, w = search_mod.build_search_url, search_mod.with_page
    assert b("head of design") == SEARCH + "?keywords=head%20of%20design&origin=GLOBAL_SEARCH_HEADER"
    assert b("  founder  &  CEO ", 3) == SEARCH + "?keywords=founder%20%26%20CEO&origin=GLOBAL_SEARCH_HEADER&page=3"
    assert b("x", 1) == b("x", 0) == SEARCH + "?keywords=x&origin=GLOBAL_SEARCH_HEADER"          # no page= on the first page
    assert b("Zoë Müller") == SEARCH + "?keywords=Zo%C3%AB%20M%C3%BCller&origin=GLOBAL_SEARCH_HEADER"
    assert b("design", 2, "http://127.0.0.1:5000/") == (
        "http://127.0.0.1:5000/search/results/people/?keywords=design&origin=GLOBAL_SEARCH_HEADER&page=2")
    for empty in ("", "   ", None):
        with pytest.raises(ValueError):
            b(empty)
    raw = SEARCH + "?geoUrn=%5B%22102713980%22%5D&keywords=design%20lead&page=2&origin=FACETED_SEARCH#top"
    assert w(raw, 3) == SEARCH + "?geoUrn=%5B%22102713980%22%5D&keywords=design%20lead&origin=FACETED_SEARCH&page=3"
    assert w(raw, 1) == SEARCH + "?geoUrn=%5B%22102713980%22%5D&keywords=design%20lead&origin=FACETED_SEARCH"
    assert w(SEARCH, 2) == SEARCH + "?page=2"
    assert w(SEARCH + "?pageKey=a&page=9", 2) == SEARCH + "?pageKey=a&page=2"                   # only `page` itself is replaced


def test_rebase_points_a_linkedin_url_at_a_stand_in_host():
    url = SEARCH + "?keywords=a&page=2"
    assert search_mod._rebase(url, LI) == url == search_mod._rebase(url, LI + "/")
    assert search_mod._rebase(url, "http://127.0.0.1:9") == "http://127.0.0.1:9/search/results/people/?keywords=a&page=2"


def _card(slug, text, host=LI):
    return {"href": f"{host}/in/{slug}", "text": text}


def test_parse_result_cards_reads_the_lines_a_person_sees():
    cards = [
        # the name link: name, accessibility noise, degree, headline, location, "Current:", more noise
        {"href": LI + "/in/asha-verma-1a2b3c?miniProfileUrn=urn%3Ali%3Afs_miniProfile%3AACoAA1",
         "text": "Asha Verma\nView Asha’s profile\n• 2nd\nHead of Design at Finlo | Ex-Razorpay\nBengaluru, Karnataka, India\n"
                 "Current: Head of Design at Finlo\nStatus is online\nPremium\nRavi Kulkarni and 3 other mutual connections"},
        {"href": LI + "/in/asha-verma-1a2b3c?miniProfileUrn=urn%3Ali%3Afs_miniProfile%3AACoAA1", "text": "Asha Verma"},   # avatar link
        _card("ravi-kulkarni-9z8y7x", "Ravi Kulkarni • 3rd+\nProduct Designer\nPune, Maharashtra, India"),       # name and degree together
        _card("meera-iyer-4d5e6f/", "Meera Iyer\n• 2nd\nDesign Lead @ PayNest\nMumbai, Maharashtra, India\n"
                                    "Meera and Sana Sheikh are mutual connections"),
        _card("priya-nair-0j1k2l", "Priya Nair\n1st degree connection\nUX Researcher at Orbit\nKochi"),
        _card("just-name", "Just Name"),
        _card("member-xyz", "LinkedIn Member\nGraphic Designer\nIndia"),                  # outside your network: unusable
        {"href": LI + "/company/acme/", "text": "Acme\n• 2nd"},                           # not a person
        {"href": LI + "/school/in/mit/", "text": "MIT"},                                  # "/in/" inside another path
        {"href": "", "text": "Nobody"},
        _card("empty-text", ""),
    ]
    rows = search_mod.parse_result_cards(cards, LI, search="design")
    by = {r["linkedin_url"].rstrip("/").rsplit("/", 1)[1]: r for r in rows}
    assert sorted(by) == ["asha-verma-1a2b3c", "just-name", "meera-iyer-4d5e6f", "priya-nair-0j1k2l", "ravi-kulkarni-9z8y7x"]
    asha = by["asha-verma-1a2b3c"]                                                        # the longest anchor text wins
    assert (asha["full_name"], asha["degree"], asha["headline"], asha["location"]) == (
        "Asha Verma", "2nd", "Head of Design at Finlo | Ex-Razorpay", "Bengaluru, Karnataka, India")
    assert (asha["title"], asha["company"], asha["current_role"], asha["search"]) == (
        "Head of Design", "Finlo", "Head of Design at Finlo", "design")
    assert asha["linkedin_url"] == "https://www.linkedin.com/in/asha-verma-1a2b3c/"      # tracking parameters are gone
    ravi = by["ravi-kulkarni-9z8y7x"]
    assert (ravi["full_name"], ravi["degree"], ravi["headline"], ravi["location"]) == (
        "Ravi Kulkarni", "3rd", "Product Designer", "Pune, Maharashtra, India")
    assert ravi["title"] == "" and ravi["company"] == ""                                  # no "at" in the headline: not guessed
    meera = by["meera-iyer-4d5e6f"]
    assert (meera["title"], meera["company"], meera["headline"]) == ("Design Lead", "PayNest", "Design Lead @ PayNest")
    priya = by["priya-nair-0j1k2l"]
    assert (priya["degree"], priya["headline"], priya["location"]) == ("1st", "UX Researcher at Orbit", "Kochi")
    just = by["just-name"]
    assert just["full_name"] == "Just Name" and just["headline"] == "" and just["degree"] == ""
    assert all("mutual" not in " ".join(r.values()).lower() and "status is" not in " ".join(r.values()).lower() for r in rows)


def test_parse_result_cards_edge_cases():
    parse = search_mod.parse_result_cards
    assert parse([]) == [] and parse(None) == [] and parse([{"href": LI + "/in/a-b"}]) == []
    # relative links resolve against the page's own host; a stand-in host only ever serves /in/<slug>
    rows = parse([{"href": "/in/asha-verma-1a2b3c?x=1", "text": "Asha Verma\n• 2nd\nHead of Design at Finlo"}], "http://127.0.0.1:9")
    assert [r["linkedin_url"] for r in rows] == ["https://www.linkedin.com/in/asha-verma-1a2b3c/"]
    assert rows[0]["title"] == "Head of Design" and rows[0]["search"] == ""
    assert parse([{"href": "http://elsewhere.test/in/asha-verma-1a2b3c", "text": "Asha Verma"}], "http://127.0.0.1:9") == []
    # a degree line alone is not a name; the same line twice is read once
    assert parse([_card("x-y", "• 2nd\nHead of Design")]) == []
    rows = parse([_card("two-lines", "Two Lines\nTwo Lines\n• 2nd\nFounder at Foo")])
    assert (rows[0]["full_name"], rows[0]["headline"], rows[0]["degree"]) == ("Two Lines", "Founder at Foo", "2nd")
    # labelled extras ("Past: ...", "Skills: ...") are not the location
    rows = parse([_card("lab-led", "Lab Led\n• 2nd\nCTO at Foo\nPast: Engineer at Bar\nSkills: Python\nGoa, India")])
    assert (rows[0]["headline"], rows[0]["location"]) == ("CTO at Foo", "Goa, India")
    # accessibility noise never becomes a name
    assert parse([_card("noisy", "View Noisy’s profile\nStatus is online\nPremium")]) == []


def test_extract_js_reads_only_profile_links_inside_main():
    assert "main a[href*=" in search_mod.EXTRACT_JS and "innerText" in search_mod.EXTRACT_JS
    assert "class" not in search_mod.EXTRACT_JS.replace("className", "")                 # never matched by (random) class names


# ----------------------------------------------------------------------------- LinkedIn search: the source, no browser
def test_search_source_reads_its_spec_and_describes_itself(env):
    src = build_source({"type": "linkedin_search", "queries": ["Head of  Design", "  ", "founder fintech"],
                        "urls": [SEARCH + "?keywords=cto%20india&geoUrn=%5B%22102713980%22%5D#x",
                                 "https://evil.example/search/results/people/"],
                        "max_pages": 99, "delay_seconds": [0.1, 0.05]}, env.ctx())
    assert src.type == "linkedin_search" and src.needs_browser
    assert src.queries == ["Head of Design", "founder fintech"]
    assert src.urls == [SEARCH + "?keywords=cto%20india&geoUrn=%5B%22102713980%22%5D"]
    assert [raw for raw, _ in src.bad_urls] == ["https://evil.example/search/results/people/"]
    assert src.max_pages == HARD["search_pages_per_run"]                      # a spec can lower the caps, never raise them
    assert src.delay == (0.05, 0.1) and src.skip_first_degree
    assert src.source_id == "search:head of design"
    assert src.labels() == ["Head of Design", "founder fintech", "cto india"]
    assert "2 queries" in src.describe() and "1 saved search URL" in src.describe()
    for odd, pages in ((None, 3), (0, 3), ("abc", 3), (-4, 1), (2, 2), ("5", 5)):
        assert build_source({"type": "linkedin_search", "queries": "x", "max_pages": odd}, env.ctx()).max_pages == pages
    assert build_source({"type": "linkedin_search", "queries": "a\nb\n"}, env.ctx()).queries == ["a", "b"]    # newline-separated
    assert build_source({"type": "linkedin_search", "urls": SEARCH + "?keywords=x"}, env.ctx()).source_id == "search:x"
    assert build_source({"type": "linkedin_search", "urls": SEARCH}, env.ctx()).source_id == "search:linkedin search url"
    assert build_source({"type": "linkedin_search"}, env.ctx()).source_id == "search"
    assert build_source({"type": "linkedin_search"}, env.ctx()).delay == (6.0, 15.0)                        # the default pace


def test_search_source_accepts_and_owns(env):
    src = build_source({"type": "linkedin_search", "queries": ["head of design"]}, env.ctx())
    assert src.accepts({"search": "head of design", "degree": "2nd"})
    assert not src.accepts({"search": "head of design", "degree": "1st"})        # already connected: nothing to invite
    assert not src.accepts({"search": "founder fintech", "degree": "2nd"})       # found by a search that is no longer set
    assert src.accepts({"degree": "2nd"}) and src.owns({})                       # rows without a label: benefit of the doubt
    for off in ("false", "no", "0", False):
        assert not build_source({"type": "linkedin_search", "queries": ["x"], "skip_first_degree": off}, env.ctx()).skip_first_degree
    keep = build_source({"type": "linkedin_search", "queries": ["head of design"], "skip_first_degree": False}, env.ctx())
    assert keep.accepts({"search": "head of design", "degree": "1st"})
    url_src = build_source({"type": "linkedin_search", "urls": [SEARCH + "?keywords=cto%20india"]}, env.ctx())
    assert url_src.accepts({"search": "cto india", "degree": "3rd"}) and not url_src.accepts({"search": "head of design"})


def test_search_source_without_searches_or_with_a_used_up_budget_never_opens_a_browser(env, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a browser must not be opened")

    monkeypatch.setattr(search_mod, "launch_browser", boom)
    _, report = env.run({"type": "linkedin_search"})
    assert "no `queries` or `urls`" in report.errors[0]
    _, report = env.run({"type": "linkedin_search", "urls": ["https://evil.example/x", "http://www.linkedin.com/search/results/people/"]})
    assert len(report.errors) == 2 and all("search URL refused" in e for e in report.errors) and report.added == 0
    env.store.kv_set("search_pages:" + datetime.now().strftime("%Y-%m-%d"), str(HARD["search_pages_per_day"]))
    _, report = env.run({"type": "linkedin_search", "queries": ["design"]})
    assert report.errors == [] and report.added == 0
    assert any("budget is used up" in n for n in report.notes)


# ----------------------------------------------------------------------------- the filter over stored people
def stored(source_type: str, source_id: str = "", **fields) -> dict:
    return {"source_type": source_type, "source_id": source_id or source_type, **fields}


def test_make_filter_always_wants_people_added_by_hand():
    wanted = make_filter({"sources": []})
    assert wanted(stored("manual"))
    assert not wanted(stored("yc")) and not wanted(stored("csv", "csv:a.csv")) and not wanted({})
    assert make_filter({})(stored("manual"))                                    # even a config with no `sources` key


def test_make_filter_follows_the_enabled_sources_of_the_persons_own_type():
    wanted = make_filter({"sources": [
        {"type": "yc", "batches": ["S26"]},
        {"type": "yc", "enabled": False, "batches": ["F26"]},                    # disabled: it widens nothing
        {"type": "csv", "path": "/data/leads.csv"},
        {"type": "csv", "path": "/data/old.csv", "enabled": False},
        {"type": "webpage", "urls": ["https://a.example/team"]},
        {"type": "linkedin_search", "queries": ["head of design"]},
        {"type": "no-such-source"}, {"nonsense": True},                         # malformed entries are ignored, not fatal
    ]})
    # YC: the company filters apply to people stored before the filter changed
    assert wanted(_row("finlo", source_type="yc", source_id="yc"))
    assert not wanted(_row("threadly", source_type="yc", source_id="yc"))
    assert not wanted(_row("finlo", source_type="yc", source_id="yc", founder_status="former"))
    # CSV: only the files still configured and enabled
    assert wanted(stored("csv", "csv:leads.csv")) and wanted(stored("csv", "csv"))
    assert not wanted(stored("csv", "csv:old.csv")) and not wanted(stored("csv", "csv:never-heard-of.csv"))
    # web pages: by the page a person was read from
    assert wanted(stored("webpage", "webpage:a.example", source_url="https://a.example/team"))
    assert wanted(stored("webpage", "webpage:a.example"))                       # no origin recorded: benefit of the doubt
    assert not wanted(stored("webpage", "webpage:b.example", source_url="https://b.example/team"))
    # searches: by the query that found them, and not people you are already connected to
    assert wanted(stored("linkedin_search", "search:head of design", search="head of design", degree="2nd"))
    assert not wanted(stored("linkedin_search", "search:head of design", search="head of design", degree="1st"))
    assert not wanted(stored("linkedin_search", "search:old", search="old query", degree="2nd"))
    assert not wanted(stored("some-future-type"))


def test_removing_one_csv_stops_its_people_while_a_sibling_keeps_its_own():
    both = make_filter({"sources": [{"type": "csv", "path": "a.csv"}, {"type": "csv", "path": "b.csv"}]})
    only_b = make_filter({"sources": [{"type": "csv", "path": "b.csv"}]})
    a, b = stored("csv", "csv:a.csv"), stored("csv", "csv:b.csv")
    assert both(a) and both(b)
    assert not only_b(a) and only_b(b)
    two_searches = make_filter({"sources": [{"type": "linkedin_search", "queries": ["one"]},
                                            {"type": "linkedin_search", "queries": ["two"], "skip_first_degree": False}]})
    assert two_searches(stored("linkedin_search", search="one", degree="2nd"))
    assert not two_searches(stored("linkedin_search", search="one", degree="1st"))          # "one" skips 1st-degree people
    assert two_searches(stored("linkedin_search", search="two", degree="1st"))              # "two" keeps them


def test_the_filter_is_built_without_touching_disk_or_network(monkeypatch):
    import socket

    def no_network(*a, **k):
        raise AssertionError("make_filter must not use the network")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    wanted = make_filter({"sources": [{"type": "yc"}, {"type": "csv", "path": "/nonexistent/x.csv"},
                                      {"type": "webpage", "urls": ["https://a.example"]},
                                      {"type": "linkedin_search", "queries": ["x"]}]})
    assert wanted(stored("csv", "csv:x.csv")) and wanted(_row("finlo", source_type="yc"))


# ----------------------------------------------------------------------------- registry, sink, report, runner
def test_registry_builds_each_type_cheaply_and_rejects_unknown_ones():
    assert set(REGISTRY) == {"yc", "csv", "webpage", "linkedin_search"}
    bare = SourceContext(home=None, cfg={}, store=None, limits=None)                      # type: ignore[arg-type]
    for typ, cls in REGISTRY.items():
        src = build_source({"type": typ.upper()}, bare)                                  # no disk, no network, any case
        assert isinstance(src, cls) and src.type == typ and src.source_id and src.describe()
    assert [t for t, c in REGISTRY.items() if c.needs_browser] == ["linkedin_search"]
    for bad in ({"type": "nope"}, {}, None, {"type": ""}):
        with pytest.raises(ValueError, match="unknown source type"):
            build_source(bad, bare)


def test_sink_counts_what_it_stores_and_skips_what_it_cannot(env):
    report = FetchReport("test")
    sink = Sink(env.store, "csv", "csv:t.csv", report)
    ann = make_person(linkedin_url="https://www.linkedin.com/in/ann-one", full_name="Ann One")
    bea = make_person(linkedin_url="linkedin.com/in/bea-two", full_name="Bea Two", headline="CTO")
    assert sink.add([ann, bea, ann, {}, make_person(linkedin_url="https://www.linkedin.com/company/x", full_name="X")]) == 2
    assert (report.seen, report.added, report.updated, report.skipped) == (2, 2, 0, 2)    # the repeat in one batch is free
    assert sink.add([]) == 0
    assert sink.add([{**bea, "location": "Pune"}]) == 1
    assert (report.seen, report.added, report.updated) == (3, 2, 1)
    assert {p["source_id"] for p in env.store.people("csv")} == {"csv:t.csv"}


def test_report_summary_is_one_readable_line():
    r = FetchReport("yc")
    assert r.summary() == "yc: 0 found, 0 new"
    r.seen, r.added, r.updated, r.skipped = 12, 9, 2, 1
    r.notes.append("2 companies had no founders")
    assert r.summary() == "yc: 12 found, 9 new, 2 updated, 1 skipped - 2 companies had no founders"
    r.errors += ["boom", "bang"]
    assert r.summary() == "yc: 12 found, 9 new, 2 updated, 1 skipped - 2 errors (boom)"       # errors outrank notes


@pytest.fixture
def scripted(monkeypatch):
    """A stand-in source type: ``action`` is ok | boom | attention | loggedout; ``browser`` marks needs_browser."""
    seen: dict = {"calls": [], "contexts": []}

    class Scripted(Source):
        type = "scripted"

        def __init__(self, spec, ctx):
            super().__init__(spec, ctx)
            self.name, self.action = spec.get("name", "one"), spec.get("action", "ok")
            self.needs_browser = bool(spec.get("browser"))
            seen["contexts"].append(ctx)

        @property
        def source_id(self):
            return f"scripted:{self.name}"

        def fetch(self, sink):
            seen["calls"].append(self.name)
            if self.action == "boom":
                raise RuntimeError("kaboom\nsecond line")
            if self.action == "attention":
                raise NeedsAttention("verify it is you")
            if self.action == "loggedout":
                raise LoggedOut("signed out")
            sink.add([make_person(linkedin_url=f"https://www.linkedin.com/in/{self.name}-person", full_name=f"{self.name} Person")])

    monkeypatch.setitem(REGISTRY, "scripted", Scripted)
    return seen


def test_fetch_sources_runs_each_enabled_source_and_isolates_failures(env, scripted):
    cfg = {"sources": [{"type": "scripted", "name": "a"}, {"type": "scripted", "name": "b", "action": "boom"},
                       {"type": "mystery"}, {"type": "scripted", "name": "c", "enabled": False},
                       {"type": "scripted", "name": "d"}]}
    reports = fetch_sources(env.home, cfg, env.store, log=env.logs.append)
    assert [r.source for r in reports] == ["scripted:a", "scripted:b", "mystery", "scripted:d"]
    assert scripted["calls"] == ["a", "b", "d"]                                  # a failure never stops the next source
    assert reports[0].added == 1 and reports[0].errors == []
    assert reports[1].errors == ["RuntimeError: kaboom"] and any("failed" in m for m in env.logs)     # first line only
    assert "unknown source type" in reports[2].errors[0]
    assert {p["full_name"] for p in env.store.people("scripted")} == {"a Person", "d Person"}


def test_fetch_sources_only_skip_browser_and_stop(env, scripted):
    cfg = {"sources": [{"type": "scripted", "name": "a"}, {"type": "scripted", "name": "b", "browser": True},
                       {"type": "csv", "path": "/nonexistent.csv"}]}
    assert [r.source for r in fetch_sources(env.home, cfg, env.store, only="csv", log=env.logs.append)] == ["csv:nonexistent.csv"]
    assert scripted["calls"] == []
    reports = fetch_sources(env.home, cfg, env.store, only="scripted", skip_browser=True, log=env.logs.append)
    assert [r.source for r in reports] == ["scripted:a"] and scripted["calls"] == ["a"]
    assert any("scripted:b" in m and "needs a browser" in m for m in env.logs)
    scripted["calls"].clear()
    assert fetch_sources(env.home, cfg, env.store, only="scripted", stop=lambda: True, log=env.logs.append) == []
    assert scripted["calls"] == []
    reports = fetch_sources(env.home, cfg, env.store, only="scripted", stop=lambda: bool(scripted["calls"]), log=env.logs.append)
    assert [r.source for r in reports] == ["scripted:a"] and scripted["calls"] == ["a"]        # stopped between sources


@pytest.mark.parametrize("action,exc_type", [("attention", NeedsAttention), ("loggedout", LoggedOut)])
def test_linkedin_asking_for_the_person_is_recorded_and_then_propagated(env, scripted, action, exc_type):
    cfg = {"sources": [{"type": "scripted", "name": "a"}, {"type": "scripted", "name": "b", "action": action},
                       {"type": "scripted", "name": "c"}]}
    out: list = []
    with pytest.raises(exc_type) as info:
        fetch_sources(env.home, cfg, env.store, log=env.logs.append, reports_out=out)
    assert scripted["calls"] == ["a", "b"]                                       # the caller must stop: c never runs
    assert [r.source for r in info.value.reports] == ["scripted:a", "scripted:b"] == [r.source for r in out]
    assert info.value.reports[1].errors[0].startswith(exc_type.__name__ + ": ")
    assert info.value.reports[0].added == 1                                      # what was found before is kept
    assert {p["full_name"] for p in env.store.people("scripted")} == {"a Person"}


def test_fetch_sources_hands_its_options_to_every_source(env, scripted):
    page, stop = object(), (lambda: False)
    fetch_sources(env.home, {"sources": [{"type": "scripted"}]}, env.store, limit=7, refresh=True, headless=True,
                  base_url="http://127.0.0.1:1", stop=stop, page=page, log=env.logs.append)
    ctx = scripted["contexts"][0]
    assert (ctx.limit, ctx.refresh, ctx.headless, ctx.base_url) == (7, True, True, "http://127.0.0.1:1")
    assert ctx.page is page and ctx.stop is stop and ctx.home is env.home and ctx.store is env.store
    assert ctx.limits is not None and ctx.log == env.logs.append
    fetch_sources(env.home, {"sources": [{"type": "scripted"}]}, env.store, log=env.logs.append)
    ctx = scripted["contexts"][1]
    assert (ctx.limit, ctx.refresh, ctx.headless, ctx.base_url, ctx.page) == (0, False, None, "https://www.linkedin.com", None)


# ============================================================================= LinkedIn search against the mock (Chromium)
TODAY_KEY = lambda: "search_pages:" + datetime.now().strftime("%Y-%m-%d")  # noqa: E731
FAST = {"delay_seconds": [0.05, 0.1]}
DESIGN = {"type": "linkedin_search", "queries": ["design"], **FAST}


@pytest.fixture
def quick(monkeypatch):
    """No human-like scrolling or waiting (the pauses are recorded instead); everything else is the real browser code."""
    pytest.importorskip("playwright")
    pauses: list[tuple[float, float]] = []
    monkeypatch.setattr(search_mod, "human_pause", lambda lo, hi: pauses.append((lo, hi)))
    monkeypatch.setattr(search_mod, "human_scroll", lambda page, steps=None: None)
    return pauses


def _slug(person: dict) -> str:
    return person["linkedin_url"].rstrip("/").rsplit("/", 1)[1]


@pytest.mark.browser
def test_search_reads_result_pages_until_they_run_out(env, quick):
    with MockSearch() as mock:
        _, report = env.run({**DESIGN, "max_pages": 5}, headless=True, base_url=mock.base_url)
    assert [r["page"] for r in mock.requests] == [1, 2, 3, 4]                       # page 4: "No results found" ends it
    assert all(r["path"] == "/search/results/people/" and r["keywords"] == "design" for r in mock.requests)
    assert report.errors == [] and (report.added, report.seen, report.skipped) == (9, 9, 1)
    assert report.notes[0] == "read 4 search pages, 9 people (4/20 pages used today)"
    assert "1 existing 1st-degree connections were skipped" in report.notes
    people = env.store.people("linkedin_search")
    assert sorted(map(_slug, people)) == sorted(s for s in DISTINCT_SLUGS if s not in FIRST_DEGREE_SLUGS)
    assert {p["source_id"] for p in people} == {"search:design"} and {p["search"] for p in people} == {"design"}
    asha = next(p for p in people if _slug(p) == "asha-verma-1a2b3c")                 # on pages 1 and 3: stored once
    assert (asha["full_name"], asha["degree"], asha["location"]) == ("Asha Verma", "2nd", "Bengaluru, Karnataka, India")
    assert (asha["headline"], asha["title"], asha["company"]) == ("Head of Design at Finlo | Ex-Razorpay", "Head of Design", "Finlo")
    assert asha["current_role"] == "Head of Design at Finlo"
    meera = next(p for p in people if _slug(p) == "meera-iyer-4d5e6f")
    assert (meera["title"], meera["company"]) == ("Design Lead", "PayNest")
    assert not any("decoy" in p["linkedin_url"] or "member" in p["full_name"].lower() for p in people)
    assert env.store.kv_get(TODAY_KEY(), "0") == "4"
    assert quick.count((0.05, 0.1)) == 3                                              # the configured pause between pages


@pytest.mark.browser
def test_search_default_is_three_pages_and_first_degree_people_can_be_kept(env, quick):
    with MockSearch() as mock:
        _, report = env.run({**DESIGN, "skip_first_degree": False}, headless=True, base_url=mock.base_url)
    assert [r["page"] for r in mock.requests] == [1, 2, 3]
    assert (report.added, report.skipped) == (10, 0) and not any("1st-degree" in n for n in report.notes)
    people = {_slug(p): p for p in env.store.people("linkedin_search")}
    assert people["priya-nair-0j1k2l"]["degree"] == "1st" and len(people) == 10 == len(DISTINCT_SLUGS)


@pytest.mark.browser
def test_search_respects_max_pages_the_run_limit_and_the_daily_budget(env, quick):
    full = HARD["search_pages_per_day"]
    with MockSearch() as mock:
        pw, bctx, page = search_mod.launch_browser(env.home, env.cfg, headless=True)       # one browser for all four runs
        try:
            def run(spec, **kw):
                mock.requests.clear()
                return env.run(spec, headless=True, base_url=mock.base_url, page=page, **kw)[1]

            report = run({**DESIGN, "max_pages": 2})
            assert [r["page"] for r in mock.requests] == [1, 2] and report.added == 6      # Asha Ravi Meera Karan Dev Sana
            assert env.store.kv_get(TODAY_KEY(), "0") == "2"
            env.store.kv_set(TODAY_KEY(), "0")
            report = run({**DESIGN, "max_pages": 5}, limit=2)
            assert [r["page"] for r in mock.requests] == [1, 2]
            assert any("page limit for one run reached (2)" in n for n in report.notes)
            env.store.kv_set(TODAY_KEY(), str(full - 1))                                   # one page of budget left
            report = run({**DESIGN, "max_pages": 5})
            assert [r["page"] for r in mock.requests] == [1] and env.store.kv_get(TODAY_KEY()) == str(full)
            assert any("budget is used up" in n for n in report.notes)
            assert report.notes[0].endswith(f"({full}/{full} pages used today)")
            report = run(DESIGN)                                                           # nothing left: no request at all
            assert mock.requests == [] and any("budget is used up" in n for n in report.notes)
        finally:
            search_mod.close_browser(pw, bctx)


@pytest.mark.browser
def test_search_signed_out_stops_with_loggedout_before_reading_anything(env, quick):
    with MockSearch(logged_in=False) as mock:
        cfg = {**env.cfg, "sources": [DESIGN]}
        with pytest.raises(LoggedOut) as info:
            fetch_sources(env.home, cfg, env.store, base_url=mock.base_url, headless=True, log=env.logs.append)
    assert mock.requests == []                                                        # the login check came first
    (report,) = info.value.reports
    assert report.source == "search:design" and report.errors[0].startswith("LoggedOut")
    assert env.store.people("linkedin_search") == [] and env.store.kv_get(TODAY_KEY(), "0") == "0"


@pytest.mark.browser
def test_search_hands_the_stop_button_to_the_wait_for_you_to_sign_in(env, quick, monkeypatch):
    seen = {}

    def waiting(page, base_url, wait_minutes=10, stop=None):
        seen["stop"] = stop
        raise LoggedOut("not signed in (stand-in)")

    monkeypatch.setattr(search_mod, "ensure_logged_in", waiting)
    pressed = []
    with MockSearch(logged_in=False) as mock:
        with pytest.raises(LoggedOut):
            env.run(DESIGN, headless=True, base_url=mock.base_url, stop=lambda: bool(pressed))
    assert mock.requests == [] and callable(seen["stop"]) and seen["stop"]() is False
    pressed.append("Stop")
    assert seen["stop"]() is True


@pytest.mark.browser
def test_search_stops_at_a_verification_page_and_keeps_what_it_had(env, quick):
    with MockSearch(checkpoint_page=2) as mock:
        cfg = {**env.cfg, "sources": [{**DESIGN, "max_pages": 4}]}
        with pytest.raises(NeedsAttention) as info:
            fetch_sources(env.home, cfg, env.store, base_url=mock.base_url, headless=True, log=env.logs.append)
    assert [r["page"] for r in mock.requests] == [1, 2]                               # never a page 3 behind the check
    assert info.value.reports[0].errors[0].startswith("NeedsAttention")
    assert sorted(map(_slug, env.store.people("linkedin_search"))) == [
        "asha-verma-1a2b3c", "meera-iyer-4d5e6f", "ravi-kulkarni-9z8y7x"]             # page 1 is already saved
    assert env.store.kv_get(TODAY_KEY()) == "2"                                       # and the page that triggered it counted


@pytest.mark.browser
def test_search_noticing_a_sign_out_mid_run_raises_loggedout(env, quick):
    with MockSearch(logout_after=1) as mock:
        cfg = {**env.cfg, "sources": [{**DESIGN, "max_pages": 4}]}
        with pytest.raises(LoggedOut) as info:
            fetch_sources(env.home, cfg, env.store, base_url=mock.base_url, headless=True, log=env.logs.append)
    assert [r["page"] for r in mock.requests] == [1, 2]
    assert len(env.store.people("linkedin_search")) == 3 and info.value.reports[0].added == 3


@pytest.mark.browser
def test_a_pasted_search_url_keeps_every_filter_and_only_changes_the_page(env, quick):
    pasted = SEARCH + "?geoUrn=%5B%22102713980%22%5D&keywords=design%20lead&origin=FACETED_SEARCH&sid=abc"
    with MockSearch() as mock:
        _, report = env.run({"type": "linkedin_search", "urls": [pasted + "#frag"], "max_pages": 2, **FAST},
                            headless=True, base_url=mock.base_url)
    assert [r["query"] for r in mock.requests] == [pasted.split("?", 1)[1], pasted.split("?", 1)[1] + "&page=2"]
    assert all(r["keywords"] == "design lead" for r in mock.requests)                 # %5B..%5D and %20 reached the server intact
    assert report.errors == [] and report.added == 6
    assert {p["search"] for p in env.store.people("linkedin_search")} == {"design lead"}
    assert {p["source_id"] for p in env.store.people("linkedin_search")} == {"search:design lead"}


@pytest.mark.browser
def test_search_reuses_the_runners_page_and_leaves_it_open(env, quick):
    with MockSearch() as mock:
        pw, bctx, page = search_mod.launch_browser(env.home, env.cfg, headless=True)
        try:
            reports = fetch_sources(env.home, {**env.cfg, "sources": [DESIGN]}, env.store, base_url=mock.base_url,
                                    headless=True, page=page, log=env.logs.append)
            assert reports[0].errors == [] and reports[0].added == 9
            assert [r["page"] for r in mock.requests] == [1, 2, 3]                    # no login check of its own
            assert not page.is_closed()
            page.goto(mock.base_url + "/feed/")                                       # still the caller's, still usable
            assert "Feed" in page.title()
        finally:
            search_mod.close_browser(pw, bctx)


@pytest.mark.browser
def test_search_two_queries_each_get_their_own_pages_and_label(env, quick):
    with MockSearch() as mock:
        _, report = env.run({"type": "linkedin_search", "queries": ["design", "founder"], "max_pages": 1, **FAST},
                            headless=True, base_url=mock.base_url)
    assert [(r["keywords"], r["page"]) for r in mock.requests] == [("design", 1), ("founder", 1)]
    assert report.errors == [] and report.added == 3                                  # the same 3 people twice: stored once
    assert len(env.store.people("linkedin_search")) == 3
    assert env.store.kv_get(TODAY_KEY()) == "2"
