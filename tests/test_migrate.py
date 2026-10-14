"""Migration tests: v1 (yc-founder-connect) -> Connecto.

Every test builds its own v1 folder (config, state database, founders.csv) under tmp_path and a fresh Connecto home; the
real home folder is never consulted (Path.home() is redirected).
"""
from __future__ import annotations

import csv
import hashlib
import json
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from connecto import config, limits, migrate, netguard, notes
from connecto.migrate import MigrationReport, convert_config, migrate_v1
from connecto.models import linkedin_key, priority_of
from connecto.paths import Home
from connecto.sources import SourceContext, build_source, csv_file
from connecto.sources.base import FetchReport, Sink
from connecto.sources.yc import batch_priority
from connecto.store import TERMINAL, Store

FIXTURE = Path(__file__).parent / "fixtures" / "v1_config.yaml"

# v1's own table definitions (yc-founder-connect/state.py), so the test database has the real v1 shape
V1_SCHEMA = """
CREATE TABLE IF NOT EXISTS invites (
    linkedin_url TEXT PRIMARY KEY, founder_id TEXT, full_name TEXT, company TEXT, batch TEXT,
    status TEXT NOT NULL, note TEXT, detail TEXT, attempted_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_invites_status_time ON invites(status, updated_at);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, finished_at TEXT, mode TEXT,
    sent INTEGER DEFAULT 0, attempted INTEGER DEFAULT 0, stop_reason TEXT
);
"""
CSV_COLUMNS = ["founder_id", "first_name", "full_name", "title", "company", "company_slug", "batch", "batch_code",
               "company_status", "one_liner", "industry", "subindustry", "industries", "tags", "regions", "location",
               "team_size", "top_company", "website", "company_url", "long_description", "founder_status",
               "linkedin_url", "twitter_url", "bio", "parser", "scraped_at"]

T1 = ("Hi {first_name}, congrats on {company} getting into YC ({batch_code})! I'm {me_first_name}, {me_about}, building "
      "{me_project}[[ - {me_project_blurb}]]. Would love to connect and learn from your journey.")
T2 = ("Hi {first_name} - saw {company} in the YC {batch_code} batch, really impressive. I'm {me_first_name}, building "
      "{me_project}[[ ({me_project_blurb})]]. Would be great to connect with founders who've been through it.")
T3 = ("Hi {first_name}, {company} caught my eye in YC {batch_code}. I'm {me_first_name}, {me_about}, working on "
      "{me_project}[[ - {me_project_blurb}]]. Would love to connect and follow how {company} grows.")
T4 = ("Hi {first_name}, {company} caught my eye in YC {batch_code}. I'm {me_first_name}, building "
      "{me_project}[[ - {me_project_blurb} -]] and I applied to YC for {me_applied_batch} but didn't get in. Would love "
      "to connect and learn from you.")


# ----------------------------------------------------------------------------- helpers
@pytest.fixture(autouse=True)
def private_home(tmp_path, monkeypatch):
    """Path.home() -> an empty folder, so a developer's real v1 launchd job can never leak into an assertion."""
    fake = tmp_path / "userhome"
    fake.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake))
    return fake


def v1_cfg() -> dict:
    return yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))


def invite(url, status="sent", updated="2026-10-01T10:45:12", attempted=None, note="", detail="", name="", company="", batch=""):
    return (url, "fid", name, company, batch, status, note, detail, attempted or updated, updated)


def build_state(path: Path, rows=()) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(V1_SCHEMA)
    conn.executemany("INSERT INTO invites(linkedin_url, founder_id, full_name, company, batch, status, note, detail, "
                     "attempted_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", list(rows))
    conn.commit()
    conn.close()
    return path


def make_v1(tmp_path, *, rows=None, csv_text=None, progress=None, edit=None, name="yc-founder-connect",
            state_rel="state/linkedin.sqlite", csv_rel="data/founders.csv", progress_rel="data/scrape_progress.json") -> Path:
    """A v1 folder: the fixture config (optionally edited), a state database when `rows` is given, a founders.csv and a
    scrape_progress.json when their content is given."""
    v1 = tmp_path / name
    v1.mkdir()
    cfg = v1_cfg()
    if edit:
        edit(cfg)
    (v1 / "config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    if rows is not None:
        build_state(v1 / state_rel, rows)
    if csv_text is not None:
        (v1 / csv_rel).parent.mkdir(parents=True, exist_ok=True)
        (v1 / csv_rel).write_text(csv_text, encoding="utf-8")
    if progress is not None:
        (v1 / progress_rel).parent.mkdir(parents=True, exist_ok=True)
        (v1 / progress_rel).write_text(json.dumps(progress, indent=0), encoding="utf-8")
    return v1


def snapshot(root: Path) -> dict:
    out = {}
    for p in sorted(root.rglob("*")):
        st = p.stat()
        out[str(p.relative_to(root))] = (p.is_dir(), st.st_size, st.st_mtime_ns,
                                         hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "")
    return out


def v2_invites(home: Home) -> dict:
    with Store(home.db_path) as st:
        return {r["key"]: dict(r) for r in st.conn.execute("SELECT * FROM invites")}


def v2_progress(home: Home) -> dict:
    with Store(home.db_path) as st:
        return {r["item"]: dict(r) for r in st.conn.execute("SELECT * FROM progress WHERE source='yc'")}


def put_v2_invite(home: Home, url, status, updated, note="v2 note"):
    with Store(home.db_path) as st:
        st.conn.execute("INSERT INTO invites(key, linkedin_url, full_name, company, status, note, detail, note_kind, "
                        "attempted_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (linkedin_key(url), url, "", "", status, note, "", "template", updated, updated))
        st.conn.commit()


def csv_row(**kw) -> str:
    import io
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=CSV_COLUMNS, lineterminator="\n")
    w.writerow({c: kw.get(c, "") for c in CSV_COLUMNS})
    return out.getvalue()


def founders_csv(*rows) -> str:
    return ",".join(CSV_COLUMNS) + "\n" + "".join(rows)


JANE = "https://www.linkedin.com/in/jane-roe/"
RAVI = "https://www.linkedin.com/in/ravi-k/"
ANN = "https://www.linkedin.com/in/ann-lee/"
MARY = "https://www.linkedin.com/in/Mary-Smith/"


# ----------------------------------------------------------------------------- the fixture
def test_fixture_is_a_sanitised_v1_config():
    text = FIXTURE.read_text(encoding="utf-8")
    v1 = v1_cfg()
    assert list(v1) == ["me", "targets", "yc", "linkedin"]
    assert v1["me"]["first_name"] == "Alex" and v1["me"]["project"] == "Acme"
    assert "linkedin.com/in/" not in text and "@" not in text
    assert any("{applied_batch}" in t and "{project_blurb}" in t for t in v1["linkedin"]["note_templates"])


# ----------------------------------------------------------------------------- convert_config
def test_convert_the_fixture_exactly():
    v2, warnings = convert_config(v1_cfg())
    assert warnings == []
    assert v2 == {
        "version": 2,
        "me": {"first_name": "Alex", "about": "a second-time founder", "project": "Acme",
               "project_blurb": "a tool that does X for Y", "applied_batch": "S26"},
        "sources": [{"type": "yc", "batches": ["Summer 2026", "Fall 2026"], "industries": ["Fintech", "Healthcare"],
                     "tags": ["Artificial Intelligence"], "regions": ["India"], "keywords": ["payments"],
                     "exclude_keywords": ["crypto"], "company_status": ["Active"], "max_team_size": 20,
                     "engine": "requests", "workers": 2, "delay_seconds": [1.0, 2.5]}],
        "targets": {"titles_regex": r"(?i)\bCEO\b", "exclude_titles_regex": "(?i)advisor|investor"},
        "order": "priority",
        "message": {"send_note": True, "plan": "premium", "templates": [T1, T2, T3, T4]},
        "limits": {"daily": [10, 15], "weekly": 80, "delay_seconds": [60, 180], "stop_after_errors": 3, "per_run": [5, 9]},
        "schedule": {"active_days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"], "active_hours": ["09:30", "21:30"],
                     "start_jitter_minutes": 40},
        "browser": {"channel": "chrome", "headless": False},
        "notify": True,
        "acknowledged_risks": False,
    }


def test_the_converted_fixture_normalises_without_a_single_warning():
    v2, _ = convert_config(v1_cfg())
    cfg, warnings = config.normalize(v2)
    assert warnings == []
    assert cfg["schedule"]["active_days"] == limits.DAY_NAMES                 # daily: every day, Sunday too
    assert cfg["schedule"]["times"] == ["10:40", "18:20"]                      # v1's default slots survive as Connecto's
    assert cfg["limits"]["per_run"] == [5, 9] and cfg["limits"]["weekly"] == 80
    assert cfg["acknowledged_risks"] is False                                  # has to be accepted again
    assert config.note_limit(cfg) == 300 and cfg["message"]["plan"] == "premium"
    assert [s["type"] for s in config.enabled_sources(cfg)] == ["yc"]
    problems = config.problems(cfg)
    assert len(problems) == 1 and "terms-of-service" in problems[0]             # the only thing left to do: accept the note


def test_templates_are_upgraded_and_render_the_way_v1_did():
    cfg, _ = config.normalize(convert_config(v1_cfg())[0])
    templates = cfg["message"]["templates"]
    for old in ("{project}", "{project_blurb}", "{applied_batch}"):
        assert not any(old in t for t in templates)
    assert all(notes.template_problems(t) == [] for t in templates)
    person = {"first_name": "Jane", "full_name": "Jane Roe", "company": "Beta Corp", "batch_code": "S26"}
    ctx = notes.build_context(person, cfg["me"])
    assert notes.fit(templates[0], ctx, 300) == (
        "Hi Jane, congrats on Beta Corp getting into YC (S26)! I'm Alex, a second-time founder, building Acme - a tool "
        "that does X for Y. Would love to connect and learn from your journey.")
    assert notes.fit(templates[3], ctx, 300) == (
        "Hi Jane, Beta Corp caught my eye in YC S26. I'm Alex, building Acme - a tool that does X for Y - and I applied "
        "to YC for S26 but didn't get in. Would love to connect and learn from you.")
    # too long for the limit: the blurb aside goes first, exactly like v1's "drop the project blurb" step
    assert notes.fit(templates[0], ctx, 120) == "Hi Jane, congrats on Beta Corp getting into YC (S26)! I'm Alex, a second-time founder, building Acme."
    # no blurb: the aside disappears; no applied batch: the template that needs it is skipped for everyone
    no_blurb = notes.build_context(person, {**cfg["me"], "project_blurb": ""})
    assert notes.fit(templates[1], no_blurb, 300).endswith("building Acme. Would be great to connect with founders who've been through it.")
    assert notes.fit(templates[3], notes.build_context(person, {**cfg["me"], "applied_batch": ""}), 300) is None


@pytest.mark.parametrize("value,expected", [
    (300, {"plan": "premium"}), (500, {"plan": "premium"}),
    (299, {"plan": "free", "max_note_chars": 299}), (150, {"plan": "free", "max_note_chars": 150}),
    (200, {"plan": "free"}),              # the free plan's own limit: not stored, or it would cap the note after an upgrade
    ("250", {"plan": "free", "max_note_chars": 250}),
    (None, {}), ("", {}),
])
def test_max_note_chars_picks_the_plan(value, expected):
    msg = convert_config({"linkedin": {"max_note_chars": value}})[0].get("message", {})
    assert msg == expected
    cfg, _ = config.normalize(convert_config({"linkedin": {"max_note_chars": value}})[0])
    if expected:
        assert config.note_limit(cfg) == expected.get("max_note_chars", 300 if expected["plan"] == "premium" else 200)


def test_max_note_chars_that_is_not_a_number_is_a_warning():
    v2, warnings = convert_config({"linkedin": {"max_note_chars": "lots"}})
    assert "message" not in v2 and any("max_note_chars" in w for w in warnings)


def test_order_mapping():
    assert convert_config({"linkedin": {"order": "newest_first"}})[0]["order"] == "priority"
    assert convert_config({"linkedin": {"order": "random"}})[0]["order"] == "random"
    assert "order" not in convert_config({})[0]
    v2, warnings = convert_config({"linkedin": {"order": "alphabetical"}})
    assert v2["order"] == "priority" and any("alphabetical" in w for w in warnings)


def test_limits_schedule_and_browser_mapping():
    v2, warnings = convert_config({"linkedin": {
        "daily_cap": [6, 8], "per_run_cap": [3, 4], "weekly_cap": 40, "delay_between_profiles_seconds": [90, 200],
        "stop_after_consecutive_errors": 2, "active_hours": ["08:00", "20:00"], "random_start_delay_minutes": 15,
        "notify": False, "send_note": False, "browser_channel": "msedge", "headless": False}})
    assert warnings == []
    assert v2["limits"] == {"daily": [6, 8], "per_run": [3, 4], "weekly": 40, "delay_seconds": [90, 200], "stop_after_errors": 2}
    assert v2["schedule"] == {"active_days": limits.DAY_NAMES, "active_hours": ["08:00", "20:00"], "start_jitter_minutes": 15}
    assert v2["notify"] is False and v2["message"] == {"send_note": False} and v2["browser"] == {"channel": "msedge", "headless": False}


def test_an_empty_per_run_cap_is_left_out_with_a_warning():
    v2, warnings = convert_config({"linkedin": {"per_run_cap": None, "daily_cap": [10, 12]}})
    assert "per_run" not in v2["limits"] and any("per_run_cap" in w and "4-7" in w for w in warnings)


def test_an_explicitly_empty_active_hours_means_no_restriction_but_a_missing_one_keeps_the_default():
    assert convert_config({"linkedin": {"active_hours": None}})[0]["schedule"]["active_hours"] == []
    assert "active_hours" not in convert_config({"linkedin": {}})[0]["schedule"]
    cfg, _ = config.normalize(convert_config({"linkedin": {"active_hours": []}})[0])
    assert cfg["schedule"]["active_hours"] == [] and limits.limits_from_config(cfg).within_active_hours(datetime(2026, 10, 6, 3, 0))


def test_the_source_block_leaves_out_empty_settings():
    v2, warnings = convert_config({"me": {"first_name": "A"}, "targets": {"batches": [], "industries": [], "tags": None,
                                                                         "only_top_companies": False, "max_team_size": 0,
                                                                         "titles_regex": "", "only_active_founders": True}})
    assert warnings == []
    assert v2["sources"] == [{"type": "yc"}] and "targets" not in v2
    assert convert_config({})[0]["sources"] == [{"type": "yc"}]


def test_source_block_values_and_list_syntax():
    v2, _ = convert_config({"targets": {"batches": "S26, F26", "industries": "fintech;health", "only_top_companies": True,
                                        "max_team_size": "15", "only_active_founders": False, "company_status": ["Active"]}})
    assert v2["sources"] == [{"type": "yc", "batches": ["S26", "F26"], "industries": ["fintech", "health"],
                              "company_status": ["Active"], "only_top_companies": True, "max_team_size": 15,
                              "include_former_founders": True}]
    _, warnings = convert_config({"targets": {"max_team_size": "many"}})
    assert any("max_team_size" in w for w in warnings)


def test_the_pre_targets_v1_layout_is_still_understood():
    v2, warnings = convert_config({"linkedin": {"batches": ["W26"], "only_active_founders": False, "skip_titles_regex": "(?i)advisor"}})
    assert warnings == []
    assert v2["sources"] == [{"type": "yc", "batches": ["W26"], "include_former_founders": True}]
    assert v2["targets"] == {"exclude_titles_regex": "(?i)advisor"}
    # an explicit (even empty) targets.batches wins over the old location, exactly as in v1
    both = convert_config({"targets": {"batches": []}, "linkedin": {"batches": ["W26"]}})[0]
    assert both["sources"] == [{"type": "yc"}]


def test_yc_scraping_settings_stay_inside_the_safety_ceilings():
    ceiling = limits.HARD["yc_workers_max"]
    v2, warnings = convert_config({"yc": {"workers": 6, "engine": "playwright", "delay_seconds": [2.5, 1]}})
    assert v2["sources"][0] == {"type": "yc", "engine": "playwright", "workers": ceiling, "delay_seconds": [1.0, 2.5]}
    assert any("workers" in w and "limited" in w for w in warnings)
    assert convert_config({"yc": {"workers": 0}})[0]["sources"][0]["workers"] == 1
    _, warnings = convert_config({"yc": {"workers": "x", "engine": "selenium", "delay_seconds": "fast"}})
    assert len([w for w in warnings if w.startswith("yc.")]) == 3


YC_FETCH_CUSTOM = {"companies_json": "https://mirror.example/all.json", "company_page": "https://mirror.example/c/{slug}",
                   "cache_hours": 6, "user_agent": "MyBot/1.0 (+https://example.com/bot)"}


def test_yc_fetch_locations_are_carried_over_only_when_the_user_changed_them():
    v2, warnings = convert_config({"yc": YC_FETCH_CUSTOM})
    assert v2["sources"] == [{"type": "yc", **YC_FETCH_CUSTOM}]
    # a changed place to read the YC directory from is carried over, but never silently: the warning names the server
    assert [w.split(":")[0] for w in warnings] == ["yc.companies_json", "yc.company_page"]
    assert all("mirror.example" in w and "remove it from config.yaml" in w for w in warnings)
    # v1's shipped defaults are left out, so Connecto's own (kept up to date) defaults apply to them
    shipped = {k: v1_cfg()["yc"][k] for k in ("companies_json", "company_page", "cache_hours", "user_agent")}
    v2, warnings = convert_config({"yc": shipped})
    assert warnings == [] and v2["sources"] == [{"type": "yc"}]
    # every setting is judged on its own; text and fractional numbers of hours work, and 0 ("always refresh") is a choice
    assert convert_config({"yc": {"cache_hours": "1.5", "user_agent": "  padded  "}})[0]["sources"] == \
        [{"type": "yc", "cache_hours": 1.5, "user_agent": "padded"}]
    assert convert_config({"yc": {"cache_hours": 24.0}})[0]["sources"] == [{"type": "yc"}]
    assert convert_config({"yc": {"cache_hours": 0}})[0]["sources"] == [{"type": "yc", "cache_hours": 0}]
    assert convert_config({"yc": {"companies_json": "", "company_page": None, "user_agent": ""}})[0]["sources"] == [{"type": "yc"}]


@pytest.mark.parametrize("key,value,fragment", [
    ("companies_json", "ftp://mirror.example/all.json", "web address"),
    ("companies_json", "all.json", "web address"),
    ("companies_json", ["https://a.example/x.json"], "expected text"),
    ("company_page", "https://mirror.example/company", "{slug}"),
    ("company_page", "mirror.example/{slug}", "web address"),
    ("user_agent", "line one\nline two", "control character"),
    ("user_agent", 42, "expected text"),
    ("cache_hours", "soon", "number of hours"),
    ("cache_hours", -1, "number of hours"),
    ("cache_hours", float("nan"), "number of hours"),
    ("cache_hours", 10 ** 9, "number of hours"),
    ("cache_hours", [1], "number of hours"),
])
def test_an_unusable_yc_fetch_setting_is_dropped_with_a_warning(key, value, fragment):
    v2, warnings = convert_config({"yc": {key: value}})
    assert v2["sources"] == [{"type": "yc"}]
    assert len(warnings) == 1 and warnings[0].startswith(f"yc.{key}:") and fragment in warnings[0]


def test_the_yc_source_understands_every_setting_the_migration_writes():
    v1 = v1_cfg()
    v1["yc"].update(YC_FETCH_CUSTOM, workers=1, delay_seconds=[3, 5])
    cfg, warnings = config.normalize(convert_config(v1)[0])
    assert warnings == []
    src = build_source(config.enabled_sources(cfg)[0], SourceContext(home=None, cfg=cfg, store=None, limits=None))
    assert (src.companies_json, src.user_agent, src.cache_hours) == (YC_FETCH_CUSTOM["companies_json"], YC_FETCH_CUSTOM["user_agent"], 6.0)
    assert src.page_url("beta-corp") == "https://mirror.example/c/beta-corp"
    assert (src.engine, src.workers, src.delay) == ("requests", 1, (3.0, 5.0))
    assert src.filter.batches == {"Summer 2026", "Fall 2026"} and src.filter.max_team_size == 20
    assert src.filter.industries == ["fintech", "healthcare"] and src.filter.include_former_founders is False
    assert src.filter.keywords == ["payments"] and src.filter.exclude_keywords == ["crypto"]


def test_unknown_settings_warn_but_v1_layout_defaults_do_not():
    v2, warnings = convert_config({
        "extra": 1,
        "me": {"first_name": "A", "email": "a@example.com"},
        "targets": {"mystery": 1},
        "yc": {"proxy": "x", "output_csv": "data/elsewhere.csv", "progress_json": "data/scrape_progress.json"},
        "linkedin": {"state_db": "db/custom.sqlite", "profile_dir": "browser_profile", "founders_csv": "data/founders.csv",
                     "foo": 1},
    })
    assert sorted(warnings) == sorted([
        "unknown setting 'extra' ignored",
        "me.email: not used by Connecto; ignored",
        "targets.mystery: not used by Connecto; ignored",
        "yc.proxy: not used by Connecto; ignored",
        "yc.output_csv: a v1 file location, not carried over (Connecto keeps its own files in its home folder)",
        "linkedin.state_db: a v1 file location, not carried over (Connecto keeps its own files in its home folder)",
        "linkedin.foo: not used by Connecto; ignored",
    ])
    assert "email" not in v2["me"]                                               # never copied: it could end up in an AI prompt


def test_headless_is_carried_over_with_a_warning():
    v2, warnings = convert_config({"linkedin": {"headless": True}})
    assert v2["browser"]["headless"] is True and any("headless" in w for w in warnings)


def test_template_problems_are_reported():
    v2, warnings = convert_config({"me": {"first_name": "A", "project": "", "applied_batch": ""},
                                   "linkedin": {"note_templates": ["Hi {first_name}, I'm building {project}.",
                                                                  "Hi {first_name}, {oops",
                                                                  "I applied for {applied_batch} as {me_first_name}.", "  "]}})
    assert v2["message"]["templates"] == ["Hi {first_name}, I'm building {me_project}.", "Hi {first_name}, {oops",
                                          "I applied for {me_applied_batch} as {me_first_name}."]
    assert any("templates[2]" in w and "{ or }" in w for w in warnings)
    assert any("templates[1]" in w and "{me_project}" in w and "me.project is empty" in w for w in warnings)
    assert any("templates[3]" in w and "{me_applied_batch}" in w for w in warnings)


def test_a_single_template_string_is_accepted():
    assert convert_config({"linkedin": {"note_templates": "Hi {first_name}, {project} here."}})[0]["message"]["templates"] == \
        ["Hi {first_name}, {me_project} here."]


def test_a_block_that_is_not_a_mapping_is_ignored_with_a_warning():
    v2, warnings = convert_config({"me": "Alex", "linkedin": ["x"]})
    assert v2["me"] == {} and any(w.startswith("me:") for w in warnings) and any(w.startswith("linkedin:") for w in warnings)


@pytest.mark.parametrize("bad", [None, [], "text", 5])
def test_convert_config_needs_a_mapping(bad):
    with pytest.raises(ValueError):
        convert_config(bad)


def test_an_empty_applied_batch_is_not_carried_over():
    me = convert_config({"me": {"first_name": "A", "project": "", "applied_batch": ""}})[0]["me"]
    assert me == {"first_name": "A", "project": ""}


# ----------------------------------------------------------------------------- migrate_v1: settings
def test_migrate_writes_a_config_connecto_can_load(tmp_path):
    v1 = make_v1(tmp_path, rows=[])
    before = snapshot(v1)
    home = Home(tmp_path / "home")
    report = migrate_v1(v1, home)
    assert isinstance(report, MigrationReport)
    assert report.config_path == home.config_path and home.config_path.is_file()
    assert report.templates == 4 and report.warnings == []
    assert report.invites_imported == 0 and report.people_imported == 0
    cfg, warnings = config.load(home, with_warnings=True)
    assert warnings == [] and cfg["me"]["first_name"] == "Alex" and cfg["acknowledged_risks"] is False
    assert cfg["sources"][0]["batches"] == ["Summer 2026", "Fall 2026"] and cfg["message"]["templates"] == [T1, T2, T3, T4]
    notes_text = " ".join(report.notes)
    assert "Log in to LinkedIn once more" in notes_text and "terms-of-service" in notes_text and "cron" in notes_text
    assert not home.db_path.exists()                                            # nothing to import: no database created
    assert snapshot(v1) == before                                               # v1 is exactly as it was


def test_the_safety_ceilings_apply_to_migrated_numbers(tmp_path):
    def wild(cfg):
        li = cfg["linkedin"]
        li.update(weekly_cap=500, daily_cap=[50, 60], per_run_cap=[40, 50], delay_between_profiles_seconds=[5, 10],
                  stop_after_consecutive_errors=99)
    home = Home(tmp_path / "home")
    report = migrate_v1(make_v1(tmp_path, rows=[], edit=wild), home)
    lim = limits.limits_from_config(config.load(home))
    assert lim.weekly == 100 and lim.daily == (30, 30) and lim.per_run == (20, 20)
    assert lim.delay[0] == 30 and lim.stop_after_errors == limits.HARD["errors_stop_max"]
    assert len([w for w in report.warnings if "limited" in w or "raised" in w]) >= 4


def test_an_existing_connecto_config_is_kept_as_a_backup(tmp_path):
    home = Home(tmp_path / "home")
    config.save(home, {**config.defaults(), "me": {"first_name": "Existing", "about": "", "project": "", "project_blurb": ""}})
    old_text = home.config_path.read_text(encoding="utf-8")
    report = migrate_v1(make_v1(tmp_path, rows=[]), home)
    backup = home.root / "config.yaml.v1-backup"
    assert backup.read_text(encoding="utf-8") == old_text
    assert config.load(home)["me"]["first_name"] == "Alex"
    assert any("config.yaml.v1-backup" in n for n in report.notes)

    # a second migration must not destroy that first backup
    second = make_v1(tmp_path, rows=[], name="v1-again", edit=lambda c: c["me"].update(first_name="Alexandra"))
    report = migrate_v1(second, home)
    assert backup.read_text(encoding="utf-8") == old_text
    assert (home.root / "config.yaml.v1-backup.2").is_file() and "Alex" in (home.root / "config.yaml.v1-backup.2").read_text()
    assert config.load(home)["me"]["first_name"] == "Alexandra" and any("config.yaml.v1-backup.2" in n for n in report.notes)


def test_running_it_twice_changes_nothing_the_second_time(tmp_path):
    rows = [invite(JANE), invite(RAVI, "already_connected", "2026-10-02T09:00:00")]
    v1 = make_v1(tmp_path, rows=rows, csv_text=founders_csv(csv_row(full_name="Jane Roe", linkedin_url=JANE, company="Beta")))
    home = Home(tmp_path / "home")
    first = migrate_v1(v1, home, import_people=True)
    assert first.invites_imported == 2 and first.people_imported == 1
    config_before = home.config_path.read_bytes()
    state_before = v2_invites(home)
    with Store(home.db_path) as st:
        people_before = st.people()
    second = migrate_v1(v1, home, import_people=True)
    assert second.invites_imported == 0 and second.people_imported == 0
    assert v2_invites(home) == state_before and home.config_path.read_bytes() == config_before
    with Store(home.db_path) as st:
        assert st.people() == people_before
    assert sorted(p.name for p in home.root.glob("config.yaml*")) == ["config.yaml"]       # no backup piles up
    assert any("already holds these settings" in n for n in second.notes)


# ----------------------------------------------------------------------------- migrate_v1: invitation history
STATE_ROWS = [
    invite(JANE, "sent", "2026-10-01T10:45:12", note="Hi Jane, congrats!", detail="", name="Jane Roe", company="Beta Corp", batch="Summer 2026"),
    invite(RAVI, "already_connected", "2026-10-02T09:00:00", name="Ravi K"),
    invite(ANN, "error", "2026-10-03T09:30:00", detail="timeout"),
    invite(MARY, "sent", "2026-10-03T11:00:00", note="Hi Mary", attempted="2026-10-02T11:00:00"),
    invite("https://www.linkedin.com/in/pat-p/", "already_pending", "2026-10-03T12:00:00"),
    invite("https://www.linkedin.com/in/lee-l/", "limit_reached", "2026-10-03T13:00:00", detail="weekly invitation limit"),
    invite("https://www.linkedin.com/in/dry-d/", "dry_run", "2026-10-03T14:00:00"),
    invite("https://www.linkedin.com/in/skip-s/", "skipped", "2026-10-03T15:00:00"),
    invite("https://www.linkedin.com/company/acme/", "sent", "2026-10-03T16:00:00"),              # not a person's profile
]


def test_every_v1_invitation_is_copied_so_nobody_is_invited_twice(tmp_path):
    v1 = make_v1(tmp_path, rows=STATE_ROWS, name="yc founder #1 (old) 100%")        # odd characters in the path on purpose
    home = Home(tmp_path / "home")
    report = migrate_v1(v1, home)
    assert report.invites_imported == 8 and report.warnings == []
    assert any("8 records copied" in n and "1 without a usable LinkedIn profile URL" in n for n in report.notes)
    rows = v2_invites(home)
    assert len(rows) == 8
    jane = rows[linkedin_key(JANE)]
    assert (jane["status"], jane["note"], jane["detail"], jane["note_kind"]) == ("sent", "Hi Jane, congrats!", "", "template")
    assert (jane["attempted_at"], jane["updated_at"]) == ("2026-10-01T10:45:12", "2026-10-01T10:45:12")
    assert (jane["linkedin_url"], jane["full_name"], jane["company"]) == (JANE, "Jane Roe", "Beta Corp")
    mary = rows[linkedin_key(MARY)]                                                    # keyed case-insensitively, URL kept as it was
    assert linkedin_key(MARY) == "https://www.linkedin.com/in/mary-smith/" and mary["linkedin_url"] == MARY
    assert (mary["attempted_at"], mary["updated_at"]) == ("2026-10-02T11:00:00", "2026-10-03T11:00:00")
    assert rows[linkedin_key(ANN)]["detail"] == "timeout"
    with Store(home.db_path) as st:
        # the people v1 contacted are final outcomes Connecto will never retry; the unfinished ones may be retried
        assert st.done_keys() == {linkedin_key(u) for u in (JANE, RAVI, MARY, "https://www.linkedin.com/in/pat-p/",
                                                           "https://www.linkedin.com/in/skip-s/")}
        assert {r["status"] for r in rows.values()} <= TERMINAL | {"error", "limit_reached", "dry_run"}
        # timestamps are kept, so v1's sends still count against today's / this week's quota
        assert st.sent_since(datetime(2026, 10, 1)) == 2 and st.sent_since(datetime(2026, 10, 3)) == 1


def test_the_history_database_is_opened_read_only_and_v1_is_never_written_to(tmp_path, monkeypatch):
    v1 = make_v1(tmp_path, rows=STATE_ROWS, csv_text=founders_csv(csv_row(full_name="Jane Roe", linkedin_url=JANE)))
    before = snapshot(v1)
    opened = []
    real_connect = sqlite3.connect

    def spy(database, *args, **kwargs):
        opened.append((str(database), kwargs.get("uri", False)))
        return real_connect(database, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(sqlite3, "connect", spy)
        migrate_v1(v1, Home(tmp_path / "home"), import_people=True)
    v1_opens = [(db, uri) for db, uri in opened if "linkedin.sqlite" in db]
    assert v1_opens and all(uri and db.startswith("file:") and db.endswith("?mode=ro") for db, uri in v1_opens)
    assert snapshot(v1) == before                                                     # no journal, no touched mtime, no new file
    conn = migrate._open_readonly(v1 / "state" / "linkedin.sqlite")
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("DELETE FROM invites")
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("CREATE TABLE x (a)")
    conn.close()


def test_a_newer_connecto_record_is_never_overwritten(tmp_path):
    home = Home(tmp_path / "home")
    put_v2_invite(home, JANE, "skipped", "2026-10-05T08:00:00")                       # newer than v1's 'sent' (10-01)
    put_v2_invite(home, RAVI, "already_connected", "2026-09-01T08:00:00", note="old")  # older than v1's (10-02): v1 wins
    put_v2_invite(home, ANN, "sent", "2026-09-01T08:00:00")                           # a final outcome is never downgraded to 'error'
    put_v2_invite(home, MARY, "dry_run", "2026-10-06T08:00:00")                       # a newer dry run must not hide v1's real send
    put_v2_invite(home, "https://www.linkedin.com/in/pat-p/", "already_pending", "2026-10-03T12:00:00", note="same time")
    report = migrate_v1(make_v1(tmp_path, rows=STATE_ROWS), home)
    rows = v2_invites(home)
    assert (rows[linkedin_key(JANE)]["status"], rows[linkedin_key(JANE)]["note"]) == ("skipped", "v2 note")
    assert rows[linkedin_key(RAVI)]["status"] == "already_connected" and rows[linkedin_key(RAVI)]["note"] == ""
    assert rows[linkedin_key(RAVI)]["updated_at"] == "2026-10-02T09:00:00"
    assert (rows[linkedin_key(ANN)]["status"], rows[linkedin_key(ANN)]["note"]) == ("sent", "v2 note")
    assert (rows[linkedin_key(MARY)]["status"], rows[linkedin_key(MARY)]["note"]) == ("sent", "Hi Mary")
    assert rows["https://www.linkedin.com/in/pat-p/"]["note"] == "same time"            # equal timestamps: Connecto's record stays
    # imported: RAVI and MARY (replaced) plus the three people Connecto had never heard of; kept: JANE, ANN and pat-p
    assert report.invites_imported == 5 and len(rows) == 8
    assert any("3 already known to Connecto, left alone" in n and "1 without a usable" in n for n in report.notes)


def test_duplicate_urls_in_v1_collapse_to_the_newest_record(tmp_path):
    rows = [invite("https://www.linkedin.com/in/Dee-D/", "error", "2026-10-01T08:00:00"),
            invite("https://www.linkedin.com/in/dee-d/", "sent", "2026-10-02T08:00:00", note="newer")]
    home = Home(tmp_path / "home")
    assert migrate_v1(make_v1(tmp_path, rows=rows), home).invites_imported == 1
    only = list(v2_invites(home).values())
    assert len(only) == 1 and only[0]["status"] == "sent" and only[0]["note"] == "newer"


def test_a_custom_state_db_location_is_used_to_find_the_history(tmp_path):
    v1 = make_v1(tmp_path, rows=[invite(JANE)], state_rel="db/mine.sqlite", edit=lambda c: c["linkedin"].update(state_db="db/mine.sqlite"))
    home = Home(tmp_path / "home")
    report = migrate_v1(v1, home)
    assert report.invites_imported == 1 and list(v2_invites(home)) == [linkedin_key(JANE)]
    assert report.warnings == ["linkedin.state_db: a v1 file location, not carried over (Connecto keeps its own files in its home folder)"]


def test_no_history_database_is_a_warning_not_an_error(tmp_path):
    home = Home(tmp_path / "home")
    report = migrate_v1(make_v1(tmp_path), home)                                      # no state database at all
    assert report.invites_imported == 0 and home.config_path.is_file()
    assert any("may contact them again" in w for w in report.warnings)
    empty = tmp_path / "empty.sqlite"
    sqlite3.connect(empty).close()                                                    # a database without the invites table
    v1 = make_v1(tmp_path, name="other")
    (v1 / "state").mkdir()
    shutil.copy(empty, v1 / "state" / "linkedin.sqlite")
    assert any("may contact them again" in w for w in migrate_v1(v1, Home(tmp_path / "home2")).warnings)


def test_import_state_false_skips_the_history(tmp_path):
    home = Home(tmp_path / "home")
    report = migrate_v1(make_v1(tmp_path, rows=STATE_ROWS), home, import_state=False)
    assert report.invites_imported == 0 and not home.db_path.exists()
    assert any("won't know who v1 already invited" in n for n in report.notes) and report.warnings == []


def test_a_damaged_history_database_stops_the_migration_before_anything_is_written(tmp_path):
    v1 = make_v1(tmp_path)
    (v1 / "state").mkdir()
    (v1 / "state" / "linkedin.sqlite").write_bytes(b"this is not a database" * 50)
    before = snapshot(v1)
    home = Home(tmp_path / "home")
    with pytest.raises(ValueError, match="invitation history"):
        migrate_v1(v1, home)
    assert not home.root.exists() and snapshot(v1) == before


# ----------------------------------------------------------------------------- migrate_v1: people
FOUNDERS = founders_csv(
    csv_row(founder_id="a1", first_name="Jane", full_name="Jane Roe", title="CEO", company="Beta Corp", batch="Summer 2026",
            batch_code="S26", one_liner="Payments for pets", industry="Fintech", tags="Payments|AI", linkedin_url=JANE,
            long_description="Line one\nline two, with \"quotes\""),
    csv_row(founder_id="a2", full_name="Ravi K", title="CTO", company="Beta Corp", batch="Summer 2026", batch_code="S26",
            linkedin_url="linkedin.com/in/Ravi-K?trk=public_profile"),
    csv_row(founder_id="a3", full_name="No Url", company="Gamma", batch="Fall 2026", linkedin_url=""),
    csv_row(founder_id="a4", full_name="Company Page", company="Delta", linkedin_url="https://www.linkedin.com/company/delta/"),
    csv_row(founder_id="a5", full_name="Jane Roe", company="Beta Corp", linkedin_url="https://www.linkedin.com/in/JANE-ROE"),
)


def test_people_are_not_imported_unless_asked_but_the_user_is_told(tmp_path):
    home = Home(tmp_path / "home")
    report = migrate_v1(make_v1(tmp_path, rows=[], csv_text=FOUNDERS), home)
    assert report.people_imported == 0 and not home.db_path.exists()
    assert any("was not imported" in n and "2 people" in n for n in report.notes)


def test_import_people_loads_founders_as_yc_people(tmp_path):
    home = Home(tmp_path / "home")
    report = migrate_v1(make_v1(tmp_path, rows=[], csv_text=FOUNDERS), home, import_people=True)
    assert report.people_imported == 2                                               # jane (listed twice, merged) and ravi
    assert any("2 people loaded from founders.csv" in n and "2 without a LinkedIn profile URL" in n for n in report.notes)
    with Store(home.db_path) as st:
        people = {p["key"]: p for p in st.people("yc")}
        assert st.count_people() == 2
    assert set(people) == {linkedin_key(JANE), linkedin_key(RAVI)}
    jane = people[linkedin_key(JANE)]
    assert (jane["source_type"], jane["source_id"]) == ("yc", "yc")
    assert jane["linkedin_url"] == JANE and jane["full_name"] == "Jane Roe" and jane["first_name"] == "Jane"
    assert (jane["title"], jane["company"], jane["batch"], jane["batch_code"]) == ("CEO", "Beta Corp", "Summer 2026", "S26")
    assert (jane["one_liner"], jane["industry"], jane["tags"]) == ("Payments for pets", "Fintech", "Payments|AI")
    assert "quotes" in jane["long_description"]
    assert jane["priority"] == str(batch_priority("Summer 2026")) != "0"             # what the YC source would have given her
    ravi = people[linkedin_key(RAVI)]
    assert ravi["linkedin_url"] == "https://www.linkedin.com/in/Ravi-K/" and ravi["first_name"] == "Ravi"


def test_people_import_uses_the_v1_configured_csv_and_survives_a_missing_one(tmp_path):
    v1 = make_v1(tmp_path, rows=[], csv_text=FOUNDERS, csv_rel="data/my founders.csv",
                 edit=lambda c: c["linkedin"].update(founders_csv="data/my founders.csv"))
    assert migrate_v1(v1, Home(tmp_path / "home"), import_people=True).people_imported == 2
    report = migrate_v1(make_v1(tmp_path, rows=[], name="no-csv"), Home(tmp_path / "home2"), import_people=True)
    assert report.people_imported == 0 and any("No founders file" in n for n in report.notes)


def test_imported_people_keep_the_newest_batches_first(tmp_path):
    def row(name, batch, code):
        return csv_row(full_name=name, batch=batch, batch_code=code, linkedin_url=f"https://www.linkedin.com/in/{name.lower().replace(' ', '-')}/")

    text = founders_csv(row("Old Winter", "Winter 2026", "W26"), row("New Fall", "Fall 2026", "F26"),
                        row("Mid Summer", "Summer 2026", "S26"), row("Code Only", "", "X26"), row("No Batch", "", ""))
    home = Home(tmp_path / "home")
    migrate_v1(make_v1(tmp_path, rows=[], csv_text=text), home, import_people=True)
    with Store(home.db_path) as st:
        people = {p["full_name"]: p for p in st.people("yc")}
    for name, batch in (("Old Winter", "Winter 2026"), ("New Fall", "Fall 2026"), ("Mid Summer", "Summer 2026"),
                        ("Code Only", "X26"), ("No Batch", "")):
        assert people[name]["priority"] == str(batch_priority(batch)), name       # the same number the YC source writes
    # `order: priority` sends the biggest number first: v1's newest_first (the batch code stands in for a missing batch)
    assert [p["full_name"] for p in sorted(people.values(), key=priority_of, reverse=True)] == \
        ["New Fall", "Mid Summer", "Code Only", "Old Winter", "No Batch"]
    assert config.load(Home(tmp_path / "home"))["order"] == "priority"


def _matching(**overrides):
    """CSV columns of a founder v1's fixture config would have sent to (company-level fields included)."""
    row = dict(founder_id="beta-corp::x", title="CEO", company="Beta Corp", company_slug="beta-corp", batch="Summer 2026",
               batch_code="S26", company_status="Active", one_liner="Payments for pets", industry="Fintech",
               subindustry="Fintech", industries="Fintech", tags="Artificial Intelligence", regions="India",
               location="Bengaluru, India", team_size="4", top_company="false", founder_status="active")
    row.update(overrides)
    return csv_row(**row)


def test_imported_people_pass_the_filters_the_migrated_config_gives_the_yc_source(tmp_path):
    text = founders_csv(_matching(full_name="Jane Roe", linkedin_url=JANE),
                        _matching(full_name="Old Hand", founder_status="former", linkedin_url=RAVI),      # v1: only_active_founders
                        _matching(full_name="Crypto Guy", one_liner="Crypto payments", linkedin_url=ANN),  # v1: exclude_keywords
                        _matching(full_name="Far Away", regions="Europe", location="Berlin", linkedin_url=MARY))
    home = Home(tmp_path / "home")
    migrate_v1(make_v1(tmp_path, rows=[], csv_text=text), home, import_people=True)
    cfg = config.load(home)
    src = build_source(config.enabled_sources(cfg)[0], SourceContext(home=None, cfg=cfg, store=None, limits=None))
    with Store(home.db_path) as st:
        wanted = {p["full_name"]: src.accepts(p) for p in st.people("yc")}
    assert wanted == {"Jane Roe": True, "Old Hand": False, "Crypto Guy": False, "Far Away": False}


def test_a_damaged_founders_list_stops_an_import_before_anything_is_written(tmp_path, monkeypatch):
    v1 = make_v1(tmp_path, rows=STATE_ROWS, csv_text=founders_csv(csv_row(full_name="Jane Roe", linkedin_url=JANE, bio="x" * 2000)))
    before = snapshot(v1)
    home = Home(tmp_path / "home")
    old_limit = csv.field_size_limit(1000)                    # a field longer than the csv module allows: csv.Error
    try:
        with pytest.raises(ValueError, match=r"founders list at .*founders\.csv.*Nothing was changed"):
            migrate_v1(v1, home, import_people=True)
        assert not home.root.exists() and snapshot(v1) == before
        # only a hint when the import was not asked for: no error, and no "founders list was not imported" claim either
        report = migrate_v1(v1, home)
        assert not any("founders list" in n for n in report.notes) and home.config_path.is_file()
    finally:
        csv.field_size_limit(old_limit)

    def refuse(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    ok_v1 = make_v1(tmp_path, rows=[], csv_text=FOUNDERS, name="readable")
    with monkeypatch.context() as m:
        m.setattr(csv_file, "read_regular_file", refuse)       # the file exists but can't be opened
        with pytest.raises(ValueError, match="Permission denied.*Nothing was changed"):
            migrate_v1(ok_v1, Home(tmp_path / "home3"), import_people=True)
    assert not (tmp_path / "home3").exists()


# ----------------------------------------------------------------------------- migrate_v1: which companies v1 had fetched
SLUG_FOUNDERS = founders_csv(
    csv_row(full_name="Jane Roe", company="Beta Corp", company_slug="beta-corp", batch="Summer 2026", batch_code="S26", linkedin_url=JANE),
    csv_row(full_name="Ravi K", company="Beta Corp", company_slug="beta-corp", batch="Summer 2026", batch_code="S26", linkedin_url=RAVI),
    csv_row(full_name="Ann Lee", company="Gamma", company_slug="gamma", batch="Fall 2026", batch_code="F26", linkedin_url=ANN),
)


def fetched(founders=1, with_linkedin=1, at="2026-10-01T10:00:00"):
    return {"scraped_at": at, "founders": founders, "with_linkedin": with_linkedin}


V1_PROGRESS = {
    "beta-corp": fetched(2, 2, "2026-10-01T10:00:05"),
    "gamma": fetched(1, 1, "2026-10-01T10:00:09"),
    "quiet-co": fetched(2, 0),                                       # its founders have no LinkedIn link: nothing to import
    "lost-co": fetched(3, 3),                                        # its founders are NOT in founders.csv
    "broken-co": {"scraped_at": "2026-10-01T10:01:00", "error": "http 500", "founders": 0, "with_linkedin": 0},
    "empty-co": {},
}
V1_FETCHED = ["beta-corp", "gamma", "quiet-co"]


def test_companies_v1_already_fetched_are_marked_as_fetched(tmp_path):
    home = Home(tmp_path / "home")
    v1 = make_v1(tmp_path, rows=[], csv_text=SLUG_FOUNDERS, progress=V1_PROGRESS)
    before = snapshot(v1)
    report = migrate_v1(v1, home, import_people=True)
    rows = v2_progress(home)
    assert sorted(rows) == V1_FETCHED                       # not the failed or empty ones, and not the one without its founders
    beta = rows["beta-corp"]
    assert beta["error"] is None and beta["at"] == "2026-10-01T10:00:05"
    assert json.loads(beta["info"]) == {"founders": 2, "with_linkedin": 2, "imported_from": "v1"}
    with Store(home.db_path) as st:
        assert st.progress_done("yc") == set(V1_FETCHED)    # exactly what the YC source asks for
    assert ("Scraping progress from v1: 3 companies marked as already fetched; 1 will be fetched again "
            "(their founders are not in founders.csv).") in report.notes
    assert report.warnings == [] and snapshot(v1) == before


def test_progress_is_only_imported_together_with_the_people(tmp_path):
    home = Home(tmp_path / "home")
    migrate_v1(make_v1(tmp_path, rows=[], csv_text=SLUG_FOUNDERS, progress=V1_PROGRESS), home)      # people not asked for
    assert not home.db_path.exists()
    # no founders file: marking companies as fetched would make Connecto skip people it never got
    home2 = Home(tmp_path / "home2")
    migrate_v1(make_v1(tmp_path, rows=[], name="no-csv", progress=V1_PROGRESS), home2, import_people=True)
    assert not home2.db_path.exists()


def test_a_founder_in_two_companies_keeps_both_companies_marked(tmp_path):
    text = founders_csv(
        csv_row(full_name="Jane Roe", company="Beta Corp", company_slug="beta-corp", linkedin_url=JANE),
        csv_row(full_name="Jane Roe", company="Gamma", company_slug="gamma", linkedin_url=JANE))
    home = Home(tmp_path / "home")
    report = migrate_v1(make_v1(tmp_path, rows=[], csv_text=text, progress={"beta-corp": fetched(), "gamma": fetched()}),
                        home, import_people=True)
    assert report.people_imported == 1 and sorted(v2_progress(home)) == ["beta-corp", "gamma"]
    assert not any("fetched again" in n for n in report.notes)


def test_a_company_without_linkedin_founders_needs_no_people_to_be_marked(tmp_path):
    home = Home(tmp_path / "home")
    report = migrate_v1(make_v1(tmp_path, rows=[], csv_text=founders_csv(), progress={"quiet-co": fetched(2, 0)}),
                        home, import_people=True)
    assert sorted(v2_progress(home)) == ["quiet-co"] and report.people_imported == 0


def test_connecto_own_progress_records_are_never_replaced(tmp_path):
    home = Home(tmp_path / "home")
    with Store(home.db_path) as st:
        st.progress_mark("yc", "beta-corp", {"founders": 9}, error="http 500")       # Connecto's own failed attempt: to be retried
        st.progress_mark("yc", "gamma", {"founders": 7})
    before = v2_progress(home)
    report = migrate_v1(make_v1(tmp_path, rows=[], csv_text=SLUG_FOUNDERS, progress=V1_PROGRESS), home, import_people=True)
    after = v2_progress(home)
    assert after["beta-corp"] == before["beta-corp"] and after["gamma"] == before["gamma"]
    assert sorted(after) == V1_FETCHED
    with Store(home.db_path) as st:
        assert "beta-corp" not in st.progress_done("yc")                             # it is still due for a retry
    assert any("1 company marked as already fetched; 1 will be fetched again" in n for n in report.notes)


@pytest.mark.parametrize("content", [b"{not json", b"[1, 2]", b"\xff\xfe\xfa\x00", b""])
def test_an_unreadable_progress_file_is_only_a_warning(tmp_path, content):
    v1 = make_v1(tmp_path, rows=[], csv_text=SLUG_FOUNDERS)
    (v1 / "data" / "scrape_progress.json").write_bytes(content)
    home = Home(tmp_path / "home")
    report = migrate_v1(v1, home, import_people=True)
    assert report.people_imported == 3 and v2_progress(home) == {}
    assert any("couldn't read v1's scraping progress" in w and "fetch every matching company itself" in w for w in report.warnings)


def test_progress_is_read_from_where_v1_configured_it(tmp_path):
    v1 = make_v1(tmp_path, rows=[], csv_text=SLUG_FOUNDERS, progress=V1_PROGRESS, progress_rel="data/my progress.json",
                 edit=lambda c: c["yc"].update(progress_json="data/my progress.json"))
    home = Home(tmp_path / "home")
    report = migrate_v1(v1, home, import_people=True)
    assert sorted(v2_progress(home)) == V1_FETCHED
    assert [w for w in report.warnings if w.startswith("yc.progress_json:")]          # the setting itself is v1 layout


def test_a_dry_run_reports_the_progress_it_would_import(tmp_path):
    v1 = make_v1(tmp_path, rows=[], csv_text=SLUG_FOUNDERS, progress=V1_PROGRESS)
    home = Home(tmp_path / "home")
    report = migrate_v1(v1, home, import_people=True, write=False)
    assert not home.root.exists()
    assert ("Scraping progress from v1: 3 companies would be marked as already fetched; 1 will be fetched again "
            "(their founders are not in founders.csv).") in report.notes


def test_a_timestamp_v1_wrote_oddly_does_not_end_up_in_the_database(tmp_path):
    progress = {"beta-corp": fetched(at="yesterday"), "gamma": {"founders": "2", "with_linkedin": "1"},
                "quiet-co": {"scraped_at": "2026-10-01T10:00:00Z", "founders": None, "with_linkedin": -4}}
    home = Home(tmp_path / "home")
    migrate_v1(make_v1(tmp_path, rows=[], csv_text=SLUG_FOUNDERS, progress=progress), home, import_people=True)
    rows = v2_progress(home)
    for slug in ("beta-corp", "gamma"):
        datetime.fromisoformat(rows[slug]["at"])                                      # a real timestamp (the time of the import)
    assert rows["beta-corp"]["at"] != "yesterday"
    assert json.loads(rows["gamma"]["info"]) == {"founders": 2, "with_linkedin": 1, "imported_from": "v1"}
    assert json.loads(rows["quiet-co"]["info"])["founders"] == 0 and json.loads(rows["quiet-co"]["info"])["with_linkedin"] == 0


def test_the_yc_source_skips_the_companies_v1_already_fetched(tmp_path, monkeypatch):
    company = {"slug": "beta-corp", "name": "Beta Corp", "batch": "Summer 2026", "status": "Active", "industry": "Fintech",
               "subindustry": "Fintech", "industries": ["Fintech"], "tags": ["Artificial Intelligence"], "regions": ["India"],
               "all_locations": "Bengaluru, India", "one_liner": "Payments for pets", "team_size": 4}
    pages: list[str] = []

    def fake_get(url, **kwargs):                  # a page that no longer exists: the source records it and moves on
        if url.endswith(".json"):
            pytest.fail("the company list should come from the cache")
        pages.append(url)
        return netguard.Reply(url, 404, {}, b"")

    monkeypatch.setattr(netguard, "get", fake_get)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)

    def fetch(home: Home) -> None:
        cfg = config.load(home)
        (home.data_dir / "yc_companies_all.json").write_text(json.dumps([company]), encoding="utf-8")
        with Store(home.db_path) as store:
            ctx = SourceContext(home=home, cfg=cfg, store=store, limits=limits.limits_from_config(cfg), log=lambda m: None)
            build_source(config.enabled_sources(cfg)[0], ctx).fetch(Sink(store, "yc", "yc", FetchReport("yc")))

    v1 = make_v1(tmp_path, rows=[], csv_text=SLUG_FOUNDERS, progress={"beta-corp": fetched(2, 2)})
    migrated, not_marked = Home(tmp_path / "migrated"), Home(tmp_path / "not-marked")
    migrate_v1(v1, migrated, import_people=True)
    migrate_v1(v1, not_marked)                                               # the same v1, but nothing marked as fetched
    fetch(not_marked)
    assert pages == ["https://www.ycombinator.com/companies/beta-corp"]      # the company matches the filters: it IS fetched ...
    pages.clear()
    fetch(migrated)
    assert pages == []                                                       # ... unless v1 already did it


# ----------------------------------------------------------------------------- migrate_v1: bad input
def test_a_folder_without_config_yaml_is_a_clear_error(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    home = Home(tmp_path / "home")
    with pytest.raises(FileNotFoundError, match="config.yaml"):
        migrate_v1(empty, home)
    (empty / "config.example.yaml").write_text("me: {}\n")                         # the template is not something to migrate from
    with pytest.raises(FileNotFoundError, match="config.yaml"):
        migrate_v1(empty, home)
    with pytest.raises(FileNotFoundError, match="not a folder"):
        migrate_v1(tmp_path / "does-not-exist", home)
    with pytest.raises(FileNotFoundError, match="not a folder"):
        migrate_v1(empty / "config.example.yaml", home)
    assert not home.root.exists()


def test_unreadable_v1_config_is_a_clear_error(tmp_path):
    for i, text in enumerate(["- a\n- list\n", "me: [unclosed\n", "just a string\n"]):
        v1 = tmp_path / f"v1-{i}"
        v1.mkdir()
        (v1 / "config.yaml").write_text(text)
        with pytest.raises(ValueError):
            migrate_v1(v1, Home(tmp_path / "home"))
    assert not (tmp_path / "home").exists()


def test_an_empty_v1_config_migrates_to_the_defaults(tmp_path):
    v1 = tmp_path / "v1"
    v1.mkdir()
    (v1 / "config.yaml").write_text("")
    report = migrate_v1(v1, Home(tmp_path / "home"))
    cfg = config.load(Home(tmp_path / "home"))
    assert cfg["sources"] == [{"type": "yc", "enabled": True}] and report.templates == 0
    assert cfg["schedule"]["active_days"] == limits.DAY_NAMES


def test_the_home_must_not_be_the_v1_folder_itself(tmp_path):
    v1 = make_v1(tmp_path, rows=[])
    before = snapshot(v1)
    with pytest.raises(ValueError, match="v1 stays untouched"):
        migrate_v1(v1, Home(v1))
    assert snapshot(v1) == before


# ----------------------------------------------------------------------------- migrate_v1: dry run and the old launchd job
def test_a_dry_run_writes_nothing_but_reports_what_would_happen(tmp_path):
    v1 = make_v1(tmp_path, rows=STATE_ROWS, csv_text=FOUNDERS)
    before = snapshot(v1)
    home = Home(tmp_path / "home")
    report = migrate_v1(v1, home, import_people=True, write=False)
    assert report.config_path is None and not home.root.exists() and snapshot(v1) == before
    assert report.invites_imported == 8 and report.people_imported == 2 and report.templates == 4
    assert any("Dry run" in n for n in report.notes) and any("would be copied" in n for n in report.notes)


def test_a_dry_run_counts_against_what_connecto_already_has(tmp_path):
    home = Home(tmp_path / "home")
    put_v2_invite(home, JANE, "sent", "2026-10-05T08:00:00")
    db_before = home.db_path.read_bytes()
    report = migrate_v1(make_v1(tmp_path, rows=STATE_ROWS), home, write=False)
    assert report.invites_imported == 7 and home.db_path.read_bytes() == db_before and not home.config_path.exists()


def test_v1s_own_daily_job_is_flagged_so_the_two_never_send_together(tmp_path, private_home):
    plist = private_home / "Library" / "LaunchAgents" / "com.yc-founder-connect.daily.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("<plist/>")
    v1 = make_v1(tmp_path, rows=[])
    report = migrate_v1(v1, Home(tmp_path / "home"))
    assert any("schedule.py uninstall" in w and str(plist) in w for w in report.warnings)
    plist.unlink()
    assert migrate_v1(v1, Home(tmp_path / "home2")).warnings == []


def test_report_defaults():
    r = MigrationReport()
    assert (r.config_path, r.templates, r.invites_imported, r.people_imported, r.warnings, r.notes) == (None, 0, 0, 0, [], [])


# ----------------------------------------------------------------------------- migrate_v1: a v1 folder that is not what it seems
OUTSIDE_PERSON = "https://www.linkedin.com/in/outside-person/"


def _file_outside_the_v1_folder(tmp_path, key: str) -> Path:
    """A valid file of the kind v1's setting ``key`` names, with a person nobody else knows, kept outside the v1 folder."""
    outside = tmp_path / "elsewhere"
    outside.mkdir(exist_ok=True)
    target = outside / f"{key}.dat"
    if key == "state_db":
        build_state(target, [invite(OUTSIDE_PERSON)])
    elif key == "founders_csv":
        target.write_text(founders_csv(csv_row(full_name="Out Side", company="Outside Co", company_slug="outside-co", batch="Summer 2026",
                                               batch_code="S26", linkedin_url=OUTSIDE_PERSON)), encoding="utf-8")
    else:
        target.write_text(json.dumps({"outside-co": fetched(1, 1)}), encoding="utf-8")
    return target


@pytest.mark.parametrize("how", ["an absolute path", "a way up", "a link that leads out"])
@pytest.mark.parametrize("section,key", [("linkedin", "state_db"), ("linkedin", "founders_csv"), ("yc", "progress_json")])
def test_a_v1_config_cannot_make_the_migration_read_outside_the_v1_folder(tmp_path, section, key, how):
    import os
    target = _file_outside_the_v1_folder(tmp_path, key)
    v1_path = tmp_path / "yc-founder-connect"
    spelling = {"an absolute path": str(target), "a way up": os.path.relpath(target, v1_path)}.get(how, "leads-out")

    def edit(cfg):
        cfg[section][key] = spelling

    v1 = make_v1(tmp_path, rows=STATE_ROWS, csv_text=FOUNDERS, progress=V1_PROGRESS, edit=edit)
    if how == "a link that leads out":
        (v1 / "leads-out").symlink_to(target)
    home = Home(tmp_path / "home")
    report = migrate_v1(v1, home, import_people=True)
    assert any(w.startswith(f"{section}.{key}:") and "not inside the v1 folder" in w for w in report.warnings), report.warnings
    with Store(home.db_path) as store:
        stored = "\n".join(store.conn.iterdump()).lower()
    assert "outside-person" not in stored and "outside-co" not in stored


def test_a_v1_folder_whose_files_are_inside_it_is_read_as_before_even_through_a_link(tmp_path):
    v1 = make_v1(tmp_path, rows=STATE_ROWS, csv_text=FOUNDERS, progress=V1_PROGRESS,
                 edit=lambda cfg: cfg["linkedin"].update(founders_csv="data/list.csv"))
    (v1 / "data" / "list.csv").symlink_to(v1 / "data" / "founders.csv")             # a link that stays inside
    report = migrate_v1(v1, Home(tmp_path / "home"), import_people=True)
    assert report.people_imported == 2 and report.invites_imported == 8
    assert not any("not inside the v1 folder" in w for w in report.warnings)


def test_v1_files_that_are_pipes_are_passed_over_not_waited_for(tmp_path):
    import os
    import signal
    if not hasattr(os, "mkfifo"):
        pytest.skip("needs named pipes")
    v1 = make_v1(tmp_path)
    (v1 / "data").mkdir()
    (v1 / "state").mkdir()
    for rel in ("data/founders.csv", "data/scrape_progress.json", "state/linkedin.sqlite"):
        os.mkfifo(v1 / rel)

    def give_up(*_):
        raise AssertionError("the migration is waiting for a pipe")

    previous = signal.signal(signal.SIGALRM, give_up)
    signal.setitimer(signal.ITIMER_REAL, 5)
    try:
        report = migrate_v1(v1, Home(tmp_path / "home"), import_people=True)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    assert report.people_imported == 0 and report.invites_imported == 0


def test_a_founders_list_larger_than_the_limit_stops_an_import_with_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(csv_file, "MAX_BYTES", 500)
    v1 = make_v1(tmp_path, csv_text=FOUNDERS)
    with pytest.raises(ValueError, match=r"founders list at .*larger than.*Nothing was changed"):
        migrate_v1(v1, Home(tmp_path / "home"), import_people=True)
    assert not (tmp_path / "home").exists()


def test_a_founders_list_with_more_rows_than_the_limit_is_read_as_far_as_the_limit_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(csv_file, "MAX_ROWS", 2)
    many = founders_csv(*(csv_row(full_name=f"P{i}", company="Co", linkedin_url=f"https://www.linkedin.com/in/p-{i}/") for i in range(6)))
    report = migrate_v1(make_v1(tmp_path, csv_text=many), Home(tmp_path / "home"), import_people=True)
    assert report.people_imported == 2
    assert any("has more than 2 rows; only the first 2 were read" in w for w in report.warnings)


def test_a_wide_founders_list_is_cut_to_the_columns_that_count(tmp_path, monkeypatch):
    monkeypatch.setattr(csv_file, "MAX_COLUMNS", 3)
    text = "full_name,linkedin_url,company,bio,extra\nJane Roe," + JANE + ",Beta Corp,short bio,more\n"
    report = migrate_v1(make_v1(tmp_path, csv_text=text), Home(tmp_path / "home"), import_people=True)
    assert report.people_imported == 1 and any("unusually large or wide" in w and "columns" in w for w in report.warnings)
    with Store(Home(tmp_path / "home").db_path) as store:
        person = store.people()[0]
    assert person["company"] == "Beta Corp" and "bio" not in person and "extra" not in person


def test_a_progress_file_that_is_too_large_or_nested_too_deeply_costs_a_warning_not_a_crash(tmp_path, monkeypatch):
    v1 = make_v1(tmp_path, csv_text=FOUNDERS)
    (v1 / "data" / "scrape_progress.json").write_text("[" * 200_000, encoding="utf-8")
    report = migrate_v1(v1, Home(tmp_path / "home"), import_people=True)
    assert report.people_imported == 2 and any("couldn't read v1's scraping progress" in w for w in report.warnings)
    (v1 / "data" / "scrape_progress.json").write_text(json.dumps(V1_PROGRESS), encoding="utf-8")
    monkeypatch.setattr(migrate, "MAX_PROGRESS_BYTES", 50)
    report = migrate_v1(v1, Home(tmp_path / "home2"), import_people=True)
    assert any("couldn't read v1's scraping progress" in w and "larger than" in w for w in report.warnings)


def test_only_so_many_v1_invitation_records_are_read(tmp_path, monkeypatch):
    monkeypatch.setattr(migrate, "MAX_INVITE_RECORDS", 3)
    report = migrate_v1(make_v1(tmp_path, rows=STATE_ROWS), Home(tmp_path / "home"))
    assert report.invites_imported <= 3
    assert any("more than 3 records; only the first 3 were read" in w for w in report.warnings)
