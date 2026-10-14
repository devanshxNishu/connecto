"""Unit tests for the foundation modules: models, limits, config, store, targets, notes, lock, notify."""
import json
import logging
import os
import random
from datetime import datetime, time as dtime, timedelta

import pytest
import yaml

from connecto import config, limits, notes
from connecto.models import label, linkedin_key, make_person, normalize_linkedin, person_key
from connecto.notify import build_command
from connecto.paths import Home
from connecto.store import TERMINAL, Store
from connecto.targets import Targets, count_by, term_matches
from connecto.util import (_EXTRA_SECRET_NAMES, RedactFilter, as_list, clean_text, csv_safe, env_origin, is_secret_name,
                           load_env_into_process, managed_env_names, mask_secret, normalize_origin, read_env_file,
                           read_env_origins, register_secret_name, remove_env_var, set_env_var, split_first_name)


# ----------------------------------------------------------------------------- models
@pytest.mark.parametrize("raw,expected", [
    ("https://www.linkedin.com/in/jane-doe/", "https://www.linkedin.com/in/jane-doe/"),
    ("linkedin.com/in/jane-doe", "https://www.linkedin.com/in/jane-doe/"),
    ("https://in.linkedin.com/in/jane-doe?trk=x#y", "https://www.linkedin.com/in/jane-doe/"),
    ("http://linkedin.com/in/Jane-Doe/details/experience/", "https://www.linkedin.com/in/Jane-Doe/"),
    ("//www.linkedin.com/in/abc123", "https://www.linkedin.com/in/abc123/"),
    ("https://www.linkedin.com/company/acme/", ""),
    ("https://www.linkedin.com/sales/lead/ACwAAA", ""),
    ("https://notlinkedin.com/in/jane", ""),
    ("https://evil.example/?u=linkedin.com/in/jane", ""),
    ("https://www.linkedin.com/in/", ""),
    ("", ""), (None, ""),
])
def test_normalize_linkedin(raw, expected):
    assert normalize_linkedin(raw) == expected


def test_key_is_case_insensitive_and_decoded():
    assert linkedin_key("https://www.linkedin.com/in/Jane-Doe/") == linkedin_key("linkedin.com/in/jane-doe")
    assert linkedin_key("https://www.linkedin.com/in/%E5%BC%A0%E4%BC%9F/") == "https://www.linkedin.com/in/张伟/"


# A browser turns "\" into "/" and resolves ".", ".." and their encoded forms, so each of these would open some page other
# than the "profile" it pretends to be - and, as a *different key*, slip past "never contact" and the history of sent invitations.
LOOK_ALIKES = [
    "https://www.linkedin.com/in/.\\direct-two/",
    "https://www.linkedin.com/in/x\\..\\..\\mynetwork\\",
    "https://www.linkedin.com/in/../company/acme/",
    "https://www.linkedin.com/in/%2e%2e/", "https://www.linkedin.com/in/%2E%2E/", "https://www.linkedin.com/in/%2e/",
    "https://www.linkedin.com/in/.", "https://www.linkedin.com/in/..", "https://www.linkedin.com/in/jane.doe/",
    "https://www.linkedin.com/in/jane%2Fdoe/", "https://www.linkedin.com/in/jane%5Cdoe/", "https://www.linkedin.com/in/jane%5cdoe/",
    "https://www.linkedin.com/in/jane%252e/",                       # encoded twice: a percent sign is not a name character
    "https://www.linkedin.com/in/jane doe/", "https://www.linkedin.com/in/jane%20doe/",
    "https://www.linkedin.com/in/jane;x=1/", "https://www.linkedin.com/in/jane%00/", "https://www.linkedin.com/in/jane‮txt/",
    "https://www.linkedin.com/in/jane​/", "https://www.linkedin.com/in/jane%EF%BF%BD/",
    "https://www.linkedin.com/in/" + "a" * 201 + "/",
    "https://www.linkedin.com/pub/john/..\\1a/2b3/4c5", "https://www.linkedin.com/pub/john/./1a/2b3/4c5",
    "https://www.linkedin.com/pub/john/%2e%2e/2b3/4c5",
]


@pytest.mark.parametrize("raw", LOOK_ALIKES)
def test_addresses_a_browser_would_resolve_to_another_page_are_not_profiles(raw):
    assert normalize_linkedin(raw) == "" and linkedin_key(raw) == ""
    assert make_person(linkedin_url=raw, full_name="Jane Doe") == {}


def test_every_spelling_of_one_profile_has_one_key():
    spellings = ["https://www.linkedin.com/in/josé-garcía/",
                 "https://www.linkedin.com/in/josé-garcía/",            # the same letters, accents as separate marks
                 "https://www.linkedin.com/in/jos%C3%A9-garc%C3%ADa/", "https://www.linkedin.com/in/jos%c3%a9-garc%c3%ada",
                 "linkedin.com/in/JOSÉ-GARCÍA?trk=1#x", "https://es.linkedin.com/in/josé-garcía/details/experience/",
                 "//www.linkedin.com/in/josé-garcía/"]
    assert len({linkedin_key(s) for s in spellings}) == 1 and all(normalize_linkedin(s) for s in spellings)
    assert normalize_linkedin(spellings[1]) == "https://www.linkedin.com/in/jos%C3%A9-garc%C3%ADa/"       # plain ASCII to open
    assert linkedin_key(spellings[1]) == "https://www.linkedin.com/in/josé-garcía/"
    assert linkedin_key("https://www.linkedin.com/in/jane_doe-2a/") == "https://www.linkedin.com/in/jane_doe-2a/"
    assert linkedin_key("https://www.linkedin.com/pub/John-Smith/1A/2b3/4C5") == "https://www.linkedin.com/pub/john-smith/1a/2b3/4c5/"


def test_the_key_of_a_page_is_read_from_its_path_alone():
    from connecto.models import key_from_page_url
    assert key_from_page_url("http://127.0.0.1:5000/in/Jane-Doe/?x=1") == linkedin_key("linkedin.com/in/jane-doe")
    assert key_from_page_url("https://www.linkedin.com/in/jane-doe/recent-activity/") == linkedin_key("linkedin.com/in/jane-doe")
    for not_a_profile in ("https://www.linkedin.com/feed/", "https://www.linkedin.com/in/./", "https://www.linkedin.com/in/",
                          "https://www.linkedin.com/company/acme/", "", None, "http://[::1"):
        assert key_from_page_url(not_a_profile) == ""


def test_make_person_cleans_and_derives_names():
    p = make_person(linkedin_url="linkedin.com/in/jane", full_name="  Dr.  Jane ​Doe  ", Company="Acme\n Inc", **{"Job Title": "CEO"})
    assert p["linkedin_url"] == "https://www.linkedin.com/in/jane/"
    assert p["full_name"] == "Dr. Jane Doe"
    assert p["first_name"] == "Jane"
    assert p["company"] == "Acme Inc" and p["job_title"] == "CEO"
    assert make_person(linkedin_url="https://example.com", full_name="x") == {}
    assert label(p).startswith("Dr. Jane Doe - Acme Inc")


@pytest.mark.parametrize("raw,clean", [
    ("CEO\U000e0041\U000e0042", "CEO"),                  # "tag" characters: a way to hide a sentence inside a title
    ("Founder\u2060 & CTO", "Founder & CTO"),             # word joiner
    ("Co\u00adFounder", "CoFounder"),                     # soft hyphen
    ("Head\u061c of", "Head of"), ("a\u180eb", "ab"), ("a\u206ab", "ab"), ("a\ufe0fb", "ab"), ("a\U000e0100b", "ab"),
    ("a\x80b\x9fc", "abc"), ("a\ufff9b\ufffbc", "abc"),
    ("x\x1b[31mred", "x[31mred"),                          # the escape character goes: no terminal control sequence survives
    ("a\x85b", "a b"), ("a\u2028b\u2029c", "a b c"),   # line separators stay as ordinary spaces
    ("Zoë Müller 田中 ऋषि", "Zoë Müller 田中 ऋषि"),         # real names stay whole
])
def test_clean_text_removes_what_cannot_be_seen_and_keeps_what_can(raw, clean):
    assert clean_text(raw) == clean


def test_split_first_name_and_clean_text():
    assert split_first_name("Prof. Ada Lovelace") == "Ada"
    assert split_first_name("") == ""
    assert clean_text("a‮b  c\n", 0) == "ab c"
    assert clean_text("x" * 50, 10).endswith("…") and len(clean_text("x" * 50, 10)) == 10
    assert as_list("a, b;c|d\n e") == ["a", "b", "c", "d", "e"]


# ----------------------------------------------------------------------------- limits
def test_limits_defaults_and_ceilings():
    lim = limits.limits_from_config({})
    assert lim.daily == (8, 12) and lim.weekly == 60 and lim.delay[0] >= 30
    assert lim.warnings == ()

    wild = limits.limits_from_config({"limits": {"daily": [100, 200], "weekly": 1000, "per_run": [50, 60],
                                                 "delay_seconds": [1, 2], "stop_after_errors": 99}})
    assert wild.daily == (30, 30) and wild.weekly == 100 and wild.per_run == (20, 20)
    assert wild.delay[0] == 30 and wild.delay[1] >= 45
    assert wild.stop_after_errors == limits.HARD["errors_stop_max"]
    assert len(wild.warnings) >= 4


def test_limits_garbage_never_raises():
    lim = limits.limits_from_config({"limits": {"daily": "lots", "weekly": "x", "delay_seconds": None},
                                     "schedule": {"active_hours": ["25:61", "zz"], "active_days": ["funday"]}})
    assert lim.daily == (8, 12) and lim.weekly == 60
    assert lim.active_hours is not None and lim.active_days == frozenset(range(6))
    assert lim.warnings


def test_daily_target_is_stable_within_a_day_and_in_range():
    lim = limits.Limits(daily=(10, 15))
    day = datetime(2026, 10, 6, 12)
    assert lim.daily_target(day) == lim.daily_target(day.replace(hour=20))
    assert all(10 <= lim.daily_target(day + timedelta(days=i)) <= 15 for i in range(40))
    assert len({lim.daily_target(day + timedelta(days=i)) for i in range(40)}) > 1


def test_remaining_quota_and_reasons():
    lim = limits.Limits(daily=(10, 10), weekly=30, per_run=(5, 5))
    assert lim.remaining(0, 0) == (5, "")
    assert lim.remaining(8, 8)[0] == 2
    allowed, why = lim.remaining(10, 10)
    assert allowed == 0 and "daily" in why
    allowed, why = lim.remaining(0, 30)
    assert allowed == 0 and "weekly" in why
    assert lim.remaining(0, 0, limit_override=2)[0] == 2
    assert lim.remaining(10, 10, limit_override=2)[0] == 0


def test_active_window_and_days():
    lim = limits.Limits(active_hours=(dtime(9, 0), dtime(18, 0)), active_days=frozenset({0, 1, 2, 3, 4}))
    mon_noon = datetime(2026, 10, 5, 12, 0)
    assert mon_noon.weekday() == 0 and lim.can_send_now(mon_noon) == (True, "")
    assert not lim.can_send_now(mon_noon.replace(hour=22))[0]
    sat = datetime(2026, 10, 10, 12, 0)
    ok, why = lim.can_send_now(sat)
    assert not ok and "sat" in why


# ----------------------------------------------------------------------------- config
def test_normalize_fills_defaults_and_flags_unknowns():
    cfg, warn = config.normalize({"me": {"First Name": "Asha", "about": "a designer"}, "bogus": 1,
                                  "sources": [{"type": "csv", "path": "x.csv"}, {"type": "nope"}, "str"]})
    assert cfg["me"]["first_name"] == "Asha" and cfg["me"]["about"] == "a designer"
    assert [s["type"] for s in cfg["sources"]] == ["csv"]
    assert any("bogus" in w for w in warn) and any("nope" in w for w in warn)
    assert cfg["limits"]["daily"] == [8, 12]
    assert config.note_limit(cfg) == 200
    cfg["message"]["plan"] = "premium"
    assert config.note_limit(cfg) == 300
    cfg["message"]["max_note_chars"] = 120
    assert config.note_limit(cfg) == 120
    assert config.templates(cfg) == config.DEFAULT_TEMPLATES


def test_normalize_rejects_bad_regex_and_clamps_ai():
    cfg, warn = config.normalize({"targets": {"titles_regex": "(unclosed"},
                                  "ai": {"provider": "skynet", "max_calls_per_day": 99999, "temperature": 9,
                                         "api_key_env": "bad name!"}})
    assert cfg["targets"]["titles_regex"] == ""
    assert cfg["ai"]["provider"] == "gemini" and cfg["ai"]["max_calls_per_day"] == 1000 and cfg["ai"]["temperature"] == 1.2
    assert cfg["ai"]["api_key_env"] == ""
    assert len(warn) >= 3


@pytest.mark.parametrize("name,kept", [("MY_SERVER_API_KEY", True), ("TOGETHER_KEY", True), ("SERVICE_TOKEN", True),
                                       ("together_key", False), ("KEY", False), ("PATH", False), ("LD_PRELOAD", False),
                                       ("HTTPS_PROXY", False), ("MONKEY", False), ("bad name!", False)])
def test_the_key_setting_must_be_named_like_a_secret(name, kept):
    cfg, warn = config.normalize({"ai": {"api_key_env": name}})
    assert (cfg["ai"]["api_key_env"] == name) is kept
    assert bool(warn) is (not kept)


def test_save_load_roundtrip_and_backup(tmp_path):
    home = Home(tmp_path / "h")
    cfg = config.defaults()
    cfg["me"]["first_name"] = "Asha"
    cfg["sources"] = [{"type": "yc", "batches": ["S26"]}]
    config.save(home, cfg)
    again = config.load(home)
    assert again["me"]["first_name"] == "Asha" and again["sources"][0]["batches"] == ["S26"]
    config.save(home, again)
    assert home.config_path.with_suffix(".yaml.bak").exists()
    assert oct(os.stat(home.root).st_mode & 0o777) == "0o700" or os.name == "nt"


def test_starter_yaml_is_valid_and_warning_free():
    data = yaml.safe_load(config.starter_yaml("Asha", "a product designer in Pune", "premium"))
    cfg, warn = config.normalize(data)
    assert warn == []
    assert cfg["me"]["first_name"] == "Asha" and cfg["message"]["plan"] == "premium"
    assert config.problems(cfg)  # no sources yet, risks not acknowledged


def test_problems_checklist():
    cfg, _ = config.normalize({"me": {"first_name": "A"}, "sources": [{"type": "csv", "path": "x"}],
                               "acknowledged_risks": True})
    assert config.problems(cfg) == []


# ----------------------------------------------------------------------------- store
def _person(slug, **kw):
    return make_person(linkedin_url=f"https://www.linkedin.com/in/{slug}/", full_name=kw.pop("name", slug.title()), **kw)


def test_store_people_upsert_merge_and_dedupe(tmp_path):
    with Store(tmp_path / "s.sqlite") as st:
        a = _person("jane", company="Acme")
        added, updated = st.upsert_people([a, a, {"full_name": "no url"}], "csv", "leads.csv")
        assert (added, updated) == (1, 0)
        added, updated = st.upsert_people([_person("jane", company="Acme", title="CEO")], "yc", "yc")
        assert (added, updated) == (0, 1)
        p = st.get_person(person_key(a))
        assert p["company"] == "Acme" and p["title"] == "CEO" and p["source_type"] == "csv"  # first source keeps credit
        assert st.count_people() == 1 and st.count_people_by_source() == {"csv": 1}
        assert st.upsert_people([_person("jane", company="Acme", title="CEO")], "csv") == (0, 0)


def test_store_invites_quota_and_terminal_states(tmp_path):
    with Store(tmp_path / "s.sqlite") as st:
        people = [_person(f"p{i}", company="Co") for i in range(4)]
        st.upsert_people(people, "csv")
        st.record(people[0], "sent", note="hello", note_kind="template")
        st.record(people[1], "already_pending")
        st.record(people[2], "error", detail="boom")
        st.record(people[3], "dry_run")
        assert st.sent_today() == 1 and st.sent_last_7_days() == 1
        assert st.done_keys() == {person_key(people[0]), person_key(people[1])}
        assert st.status_of(person_key(people[2])) == "error"
        assert st.summary()["total_sent"] == 1 and st.summary()["by_status"]["error"] == 1
        # a later real outcome replaces dry_run/error
        st.record(people[3], "sent", note="x")
        assert st.sent_today() == 2
        assert {"sent", "already_pending", "skipped"} <= TERMINAL
        assert st.forget_status(person_key(people[2])) is True
        assert st.forget_status(person_key(people[0])) is False   # a sent invite is never forgotten


def test_store_sent_window_boundaries(tmp_path):
    with Store(tmp_path / "s.sqlite") as st:
        p = _person("old")
        st.record(p, "sent")
        st.conn.execute("UPDATE invites SET updated_at=?", ((datetime.now() - timedelta(days=8)).isoformat(timespec="seconds"),))
        st.conn.commit()
        assert st.sent_last_7_days() == 0 and st.sent_today() == 0


def test_store_delete_uncontacted_keeps_history(tmp_path):
    with Store(tmp_path / "s.sqlite") as st:
        a, b = _person("a"), _person("b")
        st.upsert_people([a, b], "csv", "f.csv")
        st.record(a, "sent")
        assert st.delete_uncontacted("csv") == 1
        assert st.count_people() == 1


def test_store_kv_progress_cache_and_ai_budget(tmp_path):
    with Store(tmp_path / "s.sqlite") as st:
        assert st.kv_get("x", "d") == "d"
        st.kv_set("x", "1")
        st.kv_set("x", "2")
        assert st.kv_get("x") == "2"
        st.progress_mark("yc", "acme", {"founders": 2})
        st.progress_mark("yc", "bad", error="http 500")
        assert st.progress_done("yc") == {"acme"}
        st.progress_reset("yc", ["acme"])
        assert st.progress_done("yc") == set()
        assert st.cache_get("k", "hook") is None
        st.cache_set("k", "hook", "hello")
        assert st.cache_get("k", "hook") == "hello"
        assert st.ai_calls_today() == 0
        st.ai_bump(2)
        st.ai_bump()
        assert st.ai_calls_today() == 3
        rid = st.start_run("send")
        st.finish_run(rid, 2, 3, "done")
        assert st.recent_runs(1)[0]["stop_reason"] == "done"


# ----------------------------------------------------------------------------- targets
ROWS = [
    {"full_name": "A", "title": "Founder/CEO", "company": "PayCo", "headline": "", "tags": "Fintech|Payments", "location": "Bengaluru, India", "one_liner": "payments for SMBs"},
    {"full_name": "B", "title": "Recruiter", "company": "HireCo", "headline": "", "tags": "HR", "location": "London, UK", "one_liner": "hiring"},
    {"full_name": "C", "title": "", "headline": "CTO at RetailCo | retail tech", "company": "RetailCo", "location": "Pune, India", "one_liner": "retail analytics"},
]


def test_targets_keywords_titles_locations():
    t = Targets.from_config({"targets": {"keywords": ["fintech"], "locations": ["india"]}})
    assert [r["full_name"] for r in ROWS if t.matches(r)] == ["A"]
    t = Targets.from_config({"targets": {"titles": ["founder", "cto"]}})
    assert [r["full_name"] for r in ROWS if t.matches(r)] == ["A", "C"]      # C matched via headline
    t = Targets.from_config({"targets": {"exclude_titles": ["recruiter"], "locations": ["India"]}})
    assert [r["full_name"] for r in ROWS if t.matches(r)] == ["A", "C"]
    t = Targets.from_config({"targets": {"titles_regex": r"(?i)\bceo\b"}})
    assert [r["full_name"] for r in ROWS if t.matches(r)] == ["A"]
    assert Targets.from_config({}).is_empty() and Targets.from_config({}).matches(ROWS[1])
    assert "keywords: fintech" in Targets.from_config({"targets": {"keywords": ["fintech"]}}).describe()


def test_short_terms_match_whole_words_and_aliases_expand():
    assert term_matches("ai", "ai-powered tools") and not term_matches("ai", "retail training")
    assert term_matches("fintech", "financial services") and term_matches("ml", "machine learning platform")
    assert not term_matches("ml", "html")


def test_count_by_splits_joined_fields():
    assert count_by(ROWS, "tags")[0] in (("Fintech", 1), ("HR", 1), ("Payments", 1))
    assert dict(count_by(ROWS, "tags")) == {"Fintech": 1, "Payments": 1, "HR": 1}


# ----------------------------------------------------------------------------- notes
CTX = notes.build_context({"first_name": "Paul", "full_name": "Paul Witten", "company": "Tire Swing", "title": "CEO", "linkedin_url": "u", "key": "k"},
                          {"first_name": "Asha", "about": "a designer", "project": "NINA", "project_blurb": "an AI personal stylist"})


def test_context_namespaces():
    assert CTX["first_name"] == "Paul" and CTX["me_first_name"] == "Asha" and CTX["me_project"] == "NINA"
    assert "key" not in CTX and "linkedin_url" in CTX


def test_required_optional_and_fallback_vars():
    t = "Hi {first_name}[[ at {company}]], {me_about}. {nope|fallback text}"
    assert notes.required_vars(t) == {"first_name", "me_about"}
    assert notes.template_vars(t) == {"first_name", "company", "me_about", "nope"}
    assert notes.eligible(t, CTX)
    assert not notes.eligible("Hi {first_name} {missing}", CTX)
    assert notes.fit(t, CTX, 200) == "Hi Paul at Tire Swing, a designer. fallback text"
    no_company = dict(CTX, company="")
    assert notes.fit(t, no_company, 200) == "Hi Paul, a designer. fallback text"
    assert notes.fit("Hi {missing}", CTX, 100) is None


def test_fit_drops_optional_segments_from_the_end_then_sentences_then_cuts():
    t = "Hi {first_name}, I'm {me_first_name}, building {me_project}[[ - {me_project_blurb}]]. Would love to connect[[ and learn about {company}]]."
    full = notes.fit(t, CTX, 300)
    assert "an AI personal stylist" in full and "learn about Tire Swing" in full
    mid = notes.fit(t, CTX, len(full) - 3)           # only the last optional goes first
    assert "learn about" not in mid and "an AI personal stylist" in mid
    short = notes.fit(t, CTX, 60)
    assert len(short) <= 60 and "stylist" not in short
    cut = notes.fit("Hi {first_name}, " + "word " * 100, CTX, 50)
    assert len(cut) <= 50 and cut.endswith("…") and not cut.endswith(" …")


def test_no_template_injection_through_values_or_format_tricks():
    evil = notes.build_context({"first_name": "{me_first_name}", "company": "[[x]]{first_name}", "full_name": "E"}, {"first_name": "Me"})
    out = notes.fit("Hi {first_name} from {company}", evil, 200)
    assert out == "Hi {me_first_name} from [[x]]{first_name}"          # inserted literally, never re-expanded
    # str.format-style attribute lookups are not placeholders at all
    assert "{first_name.__class__}" in notes.template_problems("Hi {first_name.__class__}")[0] or notes.template_problems("Hi {first_name.__class__}")
    assert notes.fit("Hi {first_name.__class__}", CTX, 100) == "Hi {first_name.__class__}"


def test_compose_filters_by_eligibility_and_prefers_hook():
    templates = ["Hi {first_name}, {hook} Let's connect.", "Hi {first_name}, I'm {me_first_name}.", "Hi {first_name} at {nonexistent}"]
    rng = random.Random(1)
    r = notes.compose(templates, CTX, 200, rng)
    assert r.index == 1                                    # no hook yet: only the plain, eligible one
    with_hook = dict(CTX, hook="Your work on housing compliance stood out.")
    for seed in range(10):
        r = notes.compose(templates, with_hook, 200, random.Random(seed))
        assert r.index == 0 and "housing compliance" in r.text
    assert notes.compose(["{nonexistent}"], CTX, 200) is None
    r = notes.compose(["Hi {first_name}, " + "blah " * 60], CTX, 80)
    assert len(r.text) <= 80 and len(r.refit(40)) <= 40


def test_template_problems():
    assert notes.template_problems("ok {first_name}[[ {company}]]") == []
    assert notes.template_problems("bad [[ open")
    assert notes.template_problems("bad {unclosed")


def test_upgrade_v1_templates():
    v1 = "Hi {first_name}, I'm {me_first_name}, building {project} - {project_blurb} - and I applied to YC {applied_batch}. {project_blurb}"
    up = notes.upgrade_v1_template(v1)
    assert "{project}" not in up and "{applied_batch}" not in up
    assert "[[ - {me_project_blurb} -]]" in up and "{me_applied_batch}" in up
    ctx = dict(CTX, me_applied_batch="S26")
    assert notes.fit(up, ctx, 300).startswith("Hi Paul, I'm Asha, building NINA - an AI personal stylist - and I applied to YC S26.")


# ----------------------------------------------------------------------------- env file
def test_env_file_roundtrip(tmp_path):
    p = tmp_path / ".env"
    set_env_var(p, "GEMINI_API_KEY", "abcDEF123")
    set_env_var(p, "GROQ_API_KEY", "gsk_xyz")
    set_env_var(p, "GEMINI_API_KEY", "newvalue99")
    assert read_env_file(p) == {"GEMINI_API_KEY": "newvalue99", "GROQ_API_KEY": "gsk_xyz"}
    if os.name != "nt":
        assert os.stat(p).st_mode & 0o777 == 0o600
    assert remove_env_var(p, "GROQ_API_KEY") and "GROQ_API_KEY" not in read_env_file(p)
    assert not remove_env_var(p, "NOT_THERE") and mask_secret("abcdefghijkl") == "********ijkl" and mask_secret("") == ""


@pytest.mark.parametrize("value", ["has space", "new\nline", "trailing\n", "", "x" * 600, "tab\there", "ünï-code"])
def test_env_values_that_could_break_the_file_are_refused(tmp_path, value):
    with pytest.raises(ValueError, match="invalid value"):
        set_env_var(tmp_path / ".env", "OK_API_KEY", value)
    assert not (tmp_path / ".env").exists()


@pytest.mark.parametrize("name", ["bad name", "1ABC", "A", "NAME\n", "NAME_KEY\n", "NAME_KEY=1", "", "a-b_key", "TOKEN KEY",
                                  "gemini_api_key", "KEY", "TOKEN", "SECRET"])
def test_env_names_that_could_break_the_file_are_refused(tmp_path, name):
    with pytest.raises(ValueError, match="invalid variable name"):
        set_env_var(tmp_path / ".env", name, "value-123")
    assert not (tmp_path / ".env").exists()


@pytest.mark.parametrize("name", ["PATH", "LD_PRELOAD", "PYTHONPATH", "HTTPS_PROXY", "HOME", "MONKEY", "KEYBOARD"])
def test_the_env_file_only_holds_secrets_never_other_settings(tmp_path, name):
    """It is exported into the process: it must not be a way to set PATH, LD_PRELOAD, a proxy and the like."""
    assert not is_secret_name(name)
    with pytest.raises(ValueError, match="KEY, TOKEN or SECRET"):
        set_env_var(tmp_path / ".env", name, "value-123")


def test_loading_the_env_file_exports_secret_named_entries_only(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text("LD_PRELOAD=/tmp/evil.so\nPATH=/nowhere\nGOOD_API_KEY=abc12345\nSHELL_TOKEN=from-file\n")
    for name in ("LD_PRELOAD", "GOOD_API_KEY"):
        monkeypatch.setenv(name, "x")
        monkeypatch.delenv(name)                                         # (so the test cleans up whatever gets exported)
    monkeypatch.setenv("SHELL_TOKEN", "from-shell")
    path_before = os.environ["PATH"]
    assert load_env_into_process(p) == ["GOOD_API_KEY"]
    assert os.environ["GOOD_API_KEY"] == "abc12345" and "LD_PRELOAD" not in os.environ and os.environ["PATH"] == path_before
    assert os.environ["SHELL_TOKEN"] == "from-shell"                      # a variable that is already set wins
    assert managed_env_names() == {"GOOD_API_KEY", "SHELL_TOKEN"}         # both are "saved in Connecto's own file"


def test_names_saved_in_the_env_file_are_remembered_as_managed(tmp_path):
    p = tmp_path / ".env"
    assert "A_API_KEY" not in managed_env_names()
    set_env_var(p, "A_API_KEY", "value-12345")
    assert "A_API_KEY" in managed_env_names()
    assert remove_env_var(p, "A_API_KEY") and "A_API_KEY" not in managed_env_names()


def test_env_file_keeps_comments_and_unrelated_lines(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# my keys\nexport OTHER_TOKEN='kept-123'\nGEMINI_API_KEY=old-value-1\n")
    set_env_var(p, "GEMINI_API_KEY", "new-value-2")
    assert p.read_text() == "# my keys\nexport OTHER_TOKEN='kept-123'\nGEMINI_API_KEY=new-value-2\n"
    assert read_env_file(p) == {"OTHER_TOKEN": "kept-123", "GEMINI_API_KEY": "new-value-2"}


@pytest.mark.parametrize("value", [12345, None, b"abc12345", ["abc12345"], 1.5, True])
def test_a_key_that_is_not_text_is_a_value_error_not_a_crash(tmp_path, value):
    with pytest.raises(ValueError, match="invalid value"):
        set_env_var(tmp_path / ".env", "OK_API_KEY", value)
    assert not (tmp_path / ".env").exists()


def test_saving_a_key_again_leaves_no_older_copy_behind_for_the_reader_to_pick(tmp_path):
    """The reader takes the last line for a name, so replacing only the first copy would silently keep the old key."""
    p = tmp_path / ".env"
    p.write_text("A_API_KEY=old-one-111\nOTHER_TOKEN=keep-me-1\nexport A_API_KEY=old-two-222\nA_API_KEY='old-three-333'\n")
    set_env_var(p, "A_API_KEY", "brand-new-9")
    assert read_env_file(p) == {"A_API_KEY": "brand-new-9", "OTHER_TOKEN": "keep-me-1"}
    assert p.read_text().count("A_API_KEY") == 1 and "old-" not in p.read_text()
    p.write_text("A_API_KEY=old-one-111\nA_API_KEY=old-two-222\n")
    assert remove_env_var(p, "A_API_KEY") and read_env_file(p) == {} and "A_API_KEY" not in p.read_text()


def test_a_byte_order_mark_does_not_hide_the_first_variable(tmp_path):
    p = tmp_path / ".env"
    p.write_bytes("\ufeffFIRST_API_KEY=abc12345\nSECOND_API_KEY=def67890\n".encode("utf-8"))
    assert read_env_file(p) == {"FIRST_API_KEY": "abc12345", "SECOND_API_KEY": "def67890"}
    set_env_var(p, "FIRST_API_KEY", "replaced-11")
    assert read_env_file(p) == {"FIRST_API_KEY": "replaced-11", "SECOND_API_KEY": "def67890"}
    assert p.read_text().count("FIRST_API_KEY") == 1                              # replaced, not added next to a hidden copy


def test_saves_made_at_the_same_moment_do_not_lose_each_other(tmp_path):
    import threading
    p = tmp_path / ".env"
    names = [f"SERVICE_{i:02d}_API_KEY" for i in range(24)]
    start = threading.Barrier(len(names))

    def save(name):
        start.wait()
        for round_ in range(6):
            set_env_var(p, name, f"value-{round_}-{name}")

    threads = [threading.Thread(target=save, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert read_env_file(p) == {n: f"value-5-{n}" for n in names}


def test_a_key_can_be_tied_to_the_server_it_was_saved_for(tmp_path):
    p = tmp_path / ".env"
    set_env_var(p, "MY_SERVER_API_KEY", "abc12345", origin="https://API.Example.com:443/v1")        # only scheme://host[:port] is kept
    assert p.read_text() == "# connecto:origin MY_SERVER_API_KEY https://api.example.com:443\nMY_SERVER_API_KEY=abc12345\n"
    assert read_env_origins(p) == {"MY_SERVER_API_KEY": "https://api.example.com:443"}
    assert env_origin("MY_SERVER_API_KEY") == "https://api.example.com:443" and read_env_file(p) == {"MY_SERVER_API_KEY": "abc12345"}
    set_env_var(p, "MY_SERVER_API_KEY", "def67890")                       # a new value for the same server keeps the record
    assert env_origin("MY_SERVER_API_KEY") == "https://api.example.com:443" and p.read_text().count("connecto:origin") == 1
    set_env_var(p, "MY_SERVER_API_KEY", "ghi24680", origin="http://localhost:1234")             # another server replaces it
    assert read_env_origins(p) == {"MY_SERVER_API_KEY": "http://localhost:1234"}
    assert p.read_text().count("MY_SERVER_API_KEY=") == 1 and p.read_text().count("connecto:origin") == 1
    set_env_var(p, "BUILT_IN_API_KEY", "xyz11111")                        # a key with no server recorded has no line
    assert "BUILT_IN_API_KEY" not in read_env_origins(p) and env_origin("BUILT_IN_API_KEY") is None
    assert remove_env_var(p, "MY_SERVER_API_KEY")                         # the record goes with its key
    assert "connecto:origin" not in p.read_text() and env_origin("MY_SERVER_API_KEY") is None


@pytest.mark.parametrize("origin", ["", "nonsense", "ftp://example.com", "https://", "https://exa mple.com",
                                    "javascript:alert(1)", "https://ünï.example", "https://a.example:99999"])
def test_a_server_address_that_is_not_usable_is_refused_and_nothing_is_written(tmp_path, origin):
    with pytest.raises(ValueError, match="invalid server address"):
        set_env_var(tmp_path / ".env", "MY_SERVER_API_KEY", "abc12345", origin=origin)
    assert not (tmp_path / ".env").exists()


def test_the_servers_in_the_env_file_are_loaded_with_the_keys_and_forged_or_stray_lines_are_ignored(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text("# connecto:origin MY_API_KEY https://api.example.com\nMY_API_KEY=abc12345\n"
                 "# connecto:origin PATH https://evil.example\n"                      # not a key name
                 "# connecto:origin OTHER_API_KEY not-an-address\nOTHER_API_KEY=def67890\n"      # not an address
                 "# connecto:origin THIRD_API_KEY https://a.example/with/a/path\nTHIRD_API_KEY=ghi24680\n")   # only an origin counts
    monkeypatch.delenv("MY_API_KEY", raising=False)
    monkeypatch.delenv("OTHER_API_KEY", raising=False)
    monkeypatch.delenv("THIRD_API_KEY", raising=False)
    load_env_into_process(p)
    assert read_env_origins(p) == {"MY_API_KEY": "https://api.example.com"}
    assert env_origin("MY_API_KEY") == "https://api.example.com"
    assert env_origin("OTHER_API_KEY") is None and env_origin("THIRD_API_KEY") is None and env_origin("PATH") is None
    for name in ("MY_API_KEY", "OTHER_API_KEY", "THIRD_API_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("address,origin", [
    ("https://API.Example.com/v1/chat/completions", "https://api.example.com"), ("http://localhost:1234/v1", "http://localhost:1234"),
    ("http://[::1]:8080", "http://[::1]:8080"), ("https://a.example:443", "https://a.example:443"),
    ("https://my_host.internal/v1", "https://my_host.internal"),
    ("", ""), ("ftp://a.example", ""), ("a.example", ""), ("https://", ""), ("https://a.example:99999", ""),
    ("https://ünï.example", ""), ("https://user:pw@a.example", "https://a.example"),
])
def test_the_origin_of_an_address_is_scheme_host_and_port_only(address, origin):
    assert normalize_origin(address) == origin


# ----------------------------------------------------------------------------- secrets never reach a log line
@pytest.mark.parametrize("name,secret", [("GEMINI_API_KEY", "gem-secret-value-1"), ("MY_LLM_KEY", "llm-secret-value-2"),
                                         ("GITHUB_TOKEN", "gh-secret-value-3"), ("DB_PASSWORD", "db-secret-value-4"),
                                         ("api_secret", "lower-secret-value-5")])
def test_values_of_secret_looking_variables_are_masked(monkeypatch, name, secret):
    monkeypatch.setenv(name, secret)
    assert RedactFilter().redact(f"calling with {secret} now") == "calling with *** now"


@pytest.mark.parametrize("name", ["MONKEY", "KEYBOARD_LAYOUT", "PATH", "HOME_DIR", "TOKENIZERS_PARALLELISM_X"])
def test_ordinary_variables_are_left_alone(monkeypatch, name):
    monkeypatch.setenv(name, "ordinary-value-123")
    assert RedactFilter().redact("keep ordinary-value-123") == "keep ordinary-value-123"


def test_short_values_are_not_masked_and_a_registered_name_always_is(monkeypatch):
    monkeypatch.setenv("SHORT_KEY", "abc")
    monkeypatch.setenv("SAUCE", "mysterious-value-77")
    assert RedactFilter().redact("abc and mysterious-value-77") == "abc and mysterious-value-77"
    register_secret_name("SAUCE")
    try:
        assert RedactFilter().redact("abc and mysterious-value-77") == "abc and ***"
    finally:
        _EXTRA_SECRET_NAMES.discard("SAUCE")


def test_the_log_filter_masks_message_and_arguments(monkeypatch):
    monkeypatch.setenv("X_API_KEY", "arg-secret-value-9")
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "key=%s ok", ("arg-secret-value-9",), None)
    assert RedactFilter().filter(record) and record.getMessage() == "key=*** ok"


def test_spreadsheet_cells_that_look_like_formulas_are_defused_but_empty_cells_stay_empty():
    assert csv_safe("=1+1") == "'=1+1" and csv_safe("+cmd") == "'+cmd" and csv_safe("-2") == "'-2" and csv_safe("@sum") == "'@sum"
    assert csv_safe("\tx") == "'\tx" and csv_safe("\rx") == "'\rx"
    assert csv_safe("") == "" and csv_safe(None) == "" and csv_safe("plain") == "plain" and csv_safe(5) == "5"


# ----------------------------------------------------------------------------- notify
def test_notify_commands_are_quoted_per_platform():
    mac = build_command('He said "hi"', 'back\\slash "q"', "darwin")
    assert mac[0] == "osascript" and '\\"hi\\"' in mac[2] and "back\\\\slash" in mac[2]
    assert build_command("t", "m", "linux")[0] == "notify-send"
    win = build_command("t", "it's", "win32")
    assert win[0] == "powershell" and "-EncodedCommand" in win
    import base64
    assert "it''s" in base64.b64decode(win[-1]).decode("utf-16-le")
    assert build_command("t", "m", "plan9") is None
    assert "\n" not in build_command("a\nb", "c\nd", "linux")[2]
