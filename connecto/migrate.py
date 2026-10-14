"""Bring a user of the v1 project (yc-founder-connect) over to Connecto.

``convert_config`` is the pure part: a v1 ``config.yaml`` (as a dict) becomes a Connecto config plus a list of warnings
about anything that could not be carried over. ``migrate_v1`` does the file work: it converts and normalises the
settings (so the safety ceilings apply), saves them into the Connecto home, and copies v1's invitation history into
Connecto's database so nobody v1 already invited is invited again. On request it also loads v1's founders list (and which
YC companies v1 had already fetched), so the first run does not scrape the directory again. v1's folder is only ever
*read*: the history database is opened read-only (SQLite ``mode=ro``) and nothing is written inside it.
"""
from __future__ import annotations

import csv
import json
import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from . import config as config_mod
from . import limits as limits_mod
from .models import linkedin_key, make_person, normalize_linkedin, person_key
from .notes import required_vars, template_problems, upgrade_v1_template
from .paths import Home
from .store import TERMINAL, Store
from .util import as_list, now_iso, read_regular_file, read_yaml_file

V1_STATE_DB = "state/linkedin.sqlite"
V1_FOUNDERS_CSV = "data/founders.csv"
V1_PROGRESS_JSON = "data/scrape_progress.json"
V1_LAUNCHD_PLIST = "com.yc-founder-connect.daily.plist"
MAX_PROGRESS_BYTES = 50 * 1024 * 1024       # (v1's own files are a few hundred kilobytes; these are limits, not expectations)
MAX_INVITE_RECORDS = 200_000

_V1_TOP = ("me", "targets", "yc", "linkedin")
_V1_ME = {"first_name", "project", "project_blurb", "applied_batch", "one_line_about"}
_V1_TARGETS = {"batches", "industries", "tags", "keywords", "exclude_keywords", "regions", "company_status",
               "only_top_companies", "titles_regex", "exclude_titles_regex", "only_active_founders", "max_team_size"}
_V1_YC = {"engine", "workers", "delay_seconds", "companies_json", "company_page", "cache_hours", "user_agent"}
# What v1 shipped for the settings above that say where the YC directory is fetched from. Connecto's YC source reads the
# same keys and has the same defaults, so only a value the user CHANGED is carried over (an unchanged one is left out,
# letting Connecto's own - kept up to date - default apply, a browser version in the user agent for instance).
_V1_YC_FETCH_DEFAULTS = {
    "companies_json": "https://yc-oss.github.io/api/companies/all.json",
    "company_page": "https://www.ycombinator.com/companies/{slug}",
    "user_agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/129.0.0.0 Safari/537.36"),
}
_V1_YC_CACHE_HOURS = 24
_MAX_CACHE_HOURS = 24 * 366
_V1_YC_LAYOUT = {"output_csv": V1_FOUNDERS_CSV, "progress_json": V1_PROGRESS_JSON}
_V1_LINKEDIN = {"order", "daily_cap", "per_run_cap", "weekly_cap", "delay_between_profiles_seconds", "active_hours",
                "stop_after_consecutive_errors", "random_start_delay_minutes", "notify", "send_note", "max_note_chars",
                "note_templates", "browser_channel", "headless", "batches", "only_active_founders", "skip_titles_regex"}
_V1_LINKEDIN_LAYOUT = {"profile_dir": "browser_profile", "state_db": V1_STATE_DB, "founders_csv": V1_FOUNDERS_CSV}


@dataclass
class MigrationReport:
    config_path: Path | None = None      # the Connecto config that was written (None for a dry run)
    templates: int = 0                   # note templates carried over
    invites_imported: int = 0            # v1 invitation records copied into Connecto's database
    people_imported: int = 0             # founders loaded from v1's founders.csv
    warnings: list[str] = field(default_factory=list)   # things that did not carry over, or changed meaning
    notes: list[str] = field(default_factory=list)      # what was done / what to do next


# ================================================================== settings
def _as_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if value is None:
        return default
    return bool(value)


def _mapping(parent: dict, key: str, warnings: list[str]) -> dict:
    value = parent.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        warnings.append(f"{key}: expected a block of settings; ignored")
        return {}
    return value


def _flag_unknown(name: str, block: dict, known: set[str], warnings: list[str], layout: dict | None = None) -> None:
    """Warn about keys Connecto does not read. v1-layout keys (file locations ...) only warn when they were changed from
    their v1 default - at the default nothing is lost, because the migration reads v1's files from where v1 keeps them."""
    for key, value in block.items():
        if key in known:
            continue
        if layout is not None and key in layout:
            if value != layout[key]:
                warnings.append(f"{name}.{key}: a v1 file location, not carried over (Connecto keeps its own files in its home folder)")
            continue
        warnings.append(f"{name}.{key}: not used by Connecto; ignored")


def _convert_me(me_in: dict, warnings: list[str]) -> dict:
    me: dict[str, str] = {}
    for src, dst in (("first_name", "first_name"), ("one_line_about", "about"), ("project", "project"),
                     ("project_blurb", "project_blurb"), ("applied_batch", "applied_batch")):
        value = me_in.get(src)
        if value is None or isinstance(value, (dict, list)):
            continue
        text = str(value).strip()
        if text or dst != "applied_batch":          # an empty applied_batch is just the template's placeholder
            me[dst] = text
    _flag_unknown("me", me_in, _V1_ME, warnings)
    return me


def _fetch_setting_problem(key: str, value) -> str:
    """Why a v1 ``yc.companies_json`` / ``company_page`` / ``user_agent`` value can't be used ('' when it can)."""
    if not isinstance(value, str):
        return "expected text"
    text = value.strip()
    if any(ord(ch) < 32 for ch in text):
        return "contains a line break or another control character"
    if key != "user_agent" and not re.fullmatch(r"https?://\S+", text, re.I):
        return "expected a web address starting with http:// or https://"
    if key == "company_page" and "{slug}" not in text:
        return "must contain {slug} (where the company's name goes)"
    return ""


def _convert_yc_fetching(yc_in: dict, src: dict, warnings: list[str]) -> None:
    """Where the YC directory is fetched from (``yc.companies_json`` ...): carried over only when the user changed it."""
    for key, default in _V1_YC_FETCH_DEFAULTS.items():
        value = yc_in.get(key)
        if value is None or value == "":
            continue
        problem = _fetch_setting_problem(key, value)
        if problem:
            warnings.append(f"yc.{key}: {problem}; ignored")
        elif value.strip() != default:
            src[key] = value.strip()
            if key != "user_agent":
                host = urlsplit(value.strip()).hostname or "?"
                warnings.append(f"yc.{key}: carried over, so the YC directory is read from {host} and not from YC's usual places. "
                                "If you did not set that yourself, remove it from config.yaml.")
    hours = yc_in.get("cache_hours")
    if hours is None or hours == "":
        return
    try:
        number = float(hours)
    except (TypeError, ValueError):
        number = -1.0
    if not 0 <= number <= _MAX_CACHE_HOURS:            # (a NaN fails this too)
        warnings.append(f"yc.cache_hours: expected a number of hours between 0 and {_MAX_CACHE_HOURS}; ignored")
    elif number != _V1_YC_CACHE_HOURS:
        src["cache_hours"] = int(number) if number == int(number) else number


def _convert_source(t_in: dict, yc_in: dict, li: dict, warnings: list[str]) -> dict:
    """v1's targets + yc scraping settings -> one ``yc`` source. Empty settings are left out."""
    src: dict = {"type": "yc"}
    batches = t_in["batches"] if "batches" in t_in else li.get("batches")     # v1 also read the pre-`targets:` layout
    for key, value in (("batches", batches), ("industries", t_in.get("industries")), ("tags", t_in.get("tags")),
                       ("regions", t_in.get("regions")), ("keywords", t_in.get("keywords")),
                       ("exclude_keywords", t_in.get("exclude_keywords")), ("company_status", t_in.get("company_status"))):
        items = as_list(value)
        if items:
            src[key] = items
    if _as_bool(t_in.get("only_top_companies"), False):
        src["only_top_companies"] = True
    try:
        team = int(t_in.get("max_team_size") or 0)
    except (TypeError, ValueError):
        warnings.append("targets.max_team_size: not a number; ignored")
        team = 0
    if team > 0:
        src["max_team_size"] = team
    only_active = t_in["only_active_founders"] if "only_active_founders" in t_in else li.get("only_active_founders", True)
    if not _as_bool(only_active, True):
        src["include_former_founders"] = True

    engine = str(yc_in.get("engine") or "").strip().lower()
    if engine in ("requests", "playwright"):
        src["engine"] = engine
    elif engine:
        warnings.append(f"yc.engine: unknown '{engine}' (expected requests or playwright); ignored")
    if yc_in.get("workers") not in (None, ""):
        try:
            workers = max(1, int(yc_in["workers"]))
        except (TypeError, ValueError):
            warnings.append("yc.workers: not a number; ignored")
        else:
            cap = limits_mod.HARD["yc_workers_max"]
            if workers > cap:
                warnings.append(f"yc.workers: {workers} limited to {cap} (safety ceiling)")
                workers = cap
            src["workers"] = workers
    if yc_in.get("delay_seconds") not in (None, "", []):
        try:
            lo, hi = sorted(float(x) for x in yc_in["delay_seconds"])
        except (TypeError, ValueError):
            warnings.append("yc.delay_seconds: expected [low, high]; ignored")
        else:
            src["delay_seconds"] = [lo, hi]
    _convert_yc_fetching(yc_in, src, warnings)
    return src


def _convert_templates(li: dict, me: dict, warnings: list[str]) -> list[str]:
    raw = li.get("note_templates")
    if isinstance(raw, str):
        raw = [raw]
    out: list[str] = []
    for item in (raw or []):
        text = str(item or "").strip()
        if not text:
            continue
        out.append(upgrade_v1_template(text))
    for i, template in enumerate(out, 1):
        for problem in template_problems(template):
            warnings.append(f"message.templates[{i}]: {problem}")
        for var in sorted(required_vars(template)):
            if var.startswith("me_") and not me.get(var[3:]):
                warnings.append(f"message.templates[{i}] needs {{{var}}} but me.{var[3:]} is empty, so it will be skipped "
                                "until you fill that in")
    return out


def _convert_message(li: dict, me: dict, warnings: list[str]) -> dict:
    msg: dict = {}
    if "send_note" in li:
        msg["send_note"] = _as_bool(li["send_note"], True)
    raw = li.get("max_note_chars")
    if raw not in (None, ""):
        try:
            chars = int(raw)
        except (TypeError, ValueError):
            warnings.append("linkedin.max_note_chars: not a number; ignored")
        else:
            limit = config_mod.LINKEDIN_NOTE_MAX
            msg["plan"] = "premium" if chars >= limit else "free"
            # an explicit lower limit is kept, unless it is just what the free plan allows anyway (keeping that would
            # silently cap the note after an upgrade to premium)
            if 0 < chars < limit and chars != config_mod.PLAN_NOTE_CHARS["free"]:
                msg["max_note_chars"] = chars
    templates = _convert_templates(li, me, warnings)
    if templates:
        msg["templates"] = templates
    return msg


def convert_config(v1: dict) -> tuple[dict, list[str]]:
    """v1 ``config.yaml`` (as a dict) -> Connecto config (NOT yet normalised) and warnings. Pure: touches no files.

    Settings that v1 did not mention are left out, so Connecto's own (more cautious) defaults apply to them.
    """
    if not isinstance(v1, dict):
        raise ValueError("a v1 config.yaml must be a YAML mapping (lines like `name: value`) at the top level")
    warnings: list[str] = []
    for key in v1:
        if key not in _V1_TOP:
            warnings.append(f"unknown setting '{key}' ignored")
    me_in = _mapping(v1, "me", warnings)
    t_in = _mapping(v1, "targets", warnings)
    yc_in = _mapping(v1, "yc", warnings)
    li = _mapping(v1, "linkedin", warnings)

    me = _convert_me(me_in, warnings)
    _flag_unknown("targets", t_in, _V1_TARGETS, warnings)
    _flag_unknown("yc", yc_in, _V1_YC, warnings, layout=_V1_YC_LAYOUT)
    _flag_unknown("linkedin", li, _V1_LINKEDIN, warnings, layout=_V1_LINKEDIN_LAYOUT)

    cfg: dict = {"version": 2, "me": me, "sources": [_convert_source(t_in, yc_in, li, warnings)]}

    targets = {}
    include = t_in.get("titles_regex")
    exclude = t_in.get("exclude_titles_regex") or li.get("skip_titles_regex")
    if include:
        targets["titles_regex"] = str(include)
    if exclude:
        targets["exclude_titles_regex"] = str(exclude)
    if targets:
        cfg["targets"] = targets

    if "order" in li and li["order"] is not None:
        order = str(li["order"]).strip().lower()
        cfg["order"] = "random" if order == "random" else "priority"
        if order not in ("newest_first", "random"):
            warnings.append(f"linkedin.order: unknown '{order}' (v1 treated it as newest_first); using 'priority'")

    msg = _convert_message(li, me, warnings)
    if msg:
        cfg["message"] = msg

    limits: dict = {}
    for src, dst in (("daily_cap", "daily"), ("weekly_cap", "weekly"), ("delay_between_profiles_seconds", "delay_seconds"),
                     ("stop_after_consecutive_errors", "stop_after_errors")):
        if li.get(src) is not None:
            limits[dst] = li[src]
    if "per_run_cap" in li:
        if li["per_run_cap"] is None:
            warnings.append("linkedin.per_run_cap was empty (no limit per run); Connecto always limits each run, so its "
                            "default of 4-7 invitations per run applies")
        else:
            limits["per_run"] = li["per_run_cap"]
    if limits:
        cfg["limits"] = limits

    # v1 users asked for daily runs, so every day is an active day (Connecto's own default skips Sunday).
    schedule: dict = {"active_days": list(limits_mod.DAY_NAMES)}
    if "active_hours" in li:
        schedule["active_hours"] = list(li["active_hours"]) if isinstance(li["active_hours"], (list, tuple)) \
            else (li["active_hours"] or [])
    if li.get("random_start_delay_minutes") is not None:
        schedule["start_jitter_minutes"] = li["random_start_delay_minutes"]
    cfg["schedule"] = schedule

    browser: dict = {}
    if li.get("browser_channel"):
        browser["channel"] = li["browser_channel"]
    if "headless" in li:
        browser["headless"] = _as_bool(li["headless"], False)
        if browser["headless"]:
            warnings.append("linkedin.headless is on: Connecto is built around a visible browser window (you can watch it and "
                            "step in, and hidden browsers are easier for LinkedIn to flag); consider turning browser.headless off")
    if browser:
        cfg["browser"] = browser
    if "notify" in li:
        cfg["notify"] = _as_bool(li["notify"], True)
    cfg["acknowledged_risks"] = False       # the LinkedIn terms-of-service note has to be accepted again in Connecto
    return cfg, warnings


# ================================================================== v1 invitation history
def _open_readonly(path: Path) -> sqlite3.Connection:
    """Open a SQLite file strictly read-only (``mode=ro``): it cannot be written to, and no journal file is created."""
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _read_v1_invites(db_path: Path) -> list[dict] | None:
    """Every row of v1's ``invites`` table, or None when there is no history to read."""
    if not db_path.is_file():
        return None
    try:
        conn = _open_readonly(db_path)
        try:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='invites'").fetchone():
                return None
            return [dict(r) for r in conn.execute("SELECT * FROM invites LIMIT ?", (MAX_INVITE_RECORDS,))]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise ValueError(f"couldn't read v1's invitation history at {db_path} ({exc}). Nothing was changed; fix or move "
                         "the file, or migrate without the history (Connecto would then not know who v1 invited).") from exc


def _query_existing(home: Home, sql: str) -> list[sqlite3.Row]:
    """Rows of a query on Connecto's own database, read without creating or changing anything ([] when there is none)."""
    if not home.db_path.is_file():
        return []
    try:
        conn = _open_readonly(home.db_path)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def _existing_invites(home: Home) -> dict[str, tuple[str, str]]:
    """Statuses already in Connecto's database."""
    return {r["key"]: (r["status"], r["updated_at"] or "")
            for r in _query_existing(home, "SELECT key, status, updated_at FROM invites")}


def _existing_progress(home: Home) -> set[str]:
    """YC companies Connecto already has a record for (fetched, or failed and due for a retry)."""
    return {r["item"] for r in _query_existing(home, "SELECT item FROM progress WHERE source='yc'")}


def _supersedes(new_status: str, new_at: str, cur_status: str, cur_at: str) -> bool:
    """May an imported v1 record replace what Connecto already has for the same person?

    A final outcome (sent, already connected, ...) is never replaced by a retryable one (error, dry run, ...), and always
    replaces a retryable one: final outcomes are never retried, so losing one could mean inviting someone twice. Between
    two of the same kind the strictly newer record wins, so a newer Connecto row is never overwritten.
    """
    new_final, cur_final = new_status in TERMINAL, cur_status in TERMINAL
    if cur_final != new_final:
        return new_final
    return new_at > cur_at


@dataclass
class _InvitePlan:
    rows: list[tuple] = field(default_factory=list)
    unusable: int = 0          # no valid LinkedIn profile URL (or no status): cannot be matched to a person
    kept: int = 0              # Connecto already had this person, same or newer


def _plan_invites(v1_rows: list[dict], existing: dict[str, tuple[str, str]]) -> _InvitePlan:
    plan = _InvitePlan()
    current = dict(existing)
    chosen: dict[str, tuple] = {}
    for row in v1_rows:                      # (the outcome does not depend on the order: see _supersedes)
        key = linkedin_key(row.get("linkedin_url"))
        status = str(row.get("status") or "").strip()
        if not key or not status:
            plan.unusable += 1
            continue
        updated = str(row.get("updated_at") or row.get("attempted_at") or "")
        attempted = str(row.get("attempted_at") or row.get("updated_at") or "")
        if key in current and not _supersedes(status, updated, *current[key]):
            plan.kept += 1
            continue
        current[key] = (status, updated)
        chosen[key] = (key, normalize_linkedin(row.get("linkedin_url")), row.get("full_name") or "", row.get("company") or "",
                       status, row.get("note") or "", row.get("detail") or "", "template", attempted, updated)
    plan.rows = list(chosen.values())
    return plan


_UPSERT_INVITE = """INSERT INTO invites(key, linkedin_url, full_name, company, status, note, detail, note_kind, attempted_at, updated_at)
VALUES(?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(key) DO UPDATE SET linkedin_url=excluded.linkedin_url, full_name=excluded.full_name, company=excluded.company,
  status=excluded.status, note=excluded.note, detail=excluded.detail, note_kind=excluded.note_kind,
  attempted_at=excluded.attempted_at, updated_at=excluded.updated_at"""


# ================================================================== v1 founders.csv and scraping progress
@dataclass
class _FoundersFile:
    people: list[dict] = field(default_factory=list)    # the distinct people (one per LinkedIn profile)
    skipped: int = 0                                     # rows without a usable LinkedIn profile URL
    slugs: set[str] = field(default_factory=set)         # company slugs the people belong to
    notes: list[str] = field(default_factory=list)       # what was cut (a file bigger than anything v1 writes)


def _read_founders_csv(path: Path) -> _FoundersFile:
    """v1's founders.csv. A person listed on several rows (same profile URL) becomes one - later non-empty values win and
    the first URL spelling is kept, just as ``upsert_people`` would merge them. ``ValueError`` when it can't be read.
    Read like any spreadsheet of people: an ordinary file only, with the same limits on size, rows and columns."""
    from .sources import csv_file
    out = _FoundersFile()
    people: dict[str, dict] = {}
    cut: set[str] = set()
    try:
        table = csv_file.parse_csv(csv_file.read_csv_text(path), ",", max_rows=csv_file.MAX_ROWS + 2, cut=cut)   # (+ the header; + 1 to see if there is more)
    except (OSError, ValueError, csv.Error) as exc:
        raise ValueError(f"couldn't read v1's founders list at {path} ({exc}). Nothing was changed; fix or move the file, or "
                         "migrate without the people (Connecto's YC source fetches founders itself).") from exc
    if len(table) > csv_file.MAX_ROWS + 1:
        table = table[:csv_file.MAX_ROWS + 1]
        out.notes.append(f"{path.name} has more than {csv_file.MAX_ROWS:,} rows; only the first {csv_file.MAX_ROWS:,} were read")
    if cut:
        out.notes.append(f"{path.name} is unusually large or wide; parts of it were cut ({', '.join(sorted(cut))})")
    header = table[0] if table else []
    for cells in table[1:]:
        if not cells:
            continue
        raw = dict(zip(header, cells))
        person = make_person(**{k: v for k, v in raw.items() if k and isinstance(v, str)})
        key = person_key(person) if person else ""
        if not key:
            out.skipped += 1
            continue
        if person.get("company_slug", "").strip():
            out.slugs.add(person["company_slug"].strip())
        if key in people:
            people[key] = {**people[key], **{k: v for k, v in person.items() if v not in ("", None) and k != "linkedin_url"}}
        else:
            people[key] = person
    out.people = list(people.values())
    return out


def _with_priority(people: list[dict]) -> list[dict]:
    """Give every person the ``priority`` Connecto's YC source would have given them: the newer the batch, the bigger the
    number, so ``order: priority`` (v1's ``newest_first``) keeps sending to the newest batches first."""
    from .sources.yc import batch_priority      # imported on use: only the people import needs the YC source
    return [{**p, "priority": str(batch_priority(p.get("batch") or p.get("batch_code") or ""))} for p in people]


def _read_v1_progress(path: Path, warnings: list[str]) -> dict[str, dict]:
    """The companies v1's scraper finished: ``slug -> {scraped_at, founders, with_linkedin}``. As in v1, an entry that is
    empty or carries an ``error`` is not finished. An unreadable file only costs some fetching again, so it is a warning."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(read_regular_file(path, MAX_PROGRESS_BYTES, what="progress file").decode("utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
    except (OSError, ValueError, RecursionError) as exc:
        warnings.append(f"couldn't read v1's scraping progress at {path} ({exc}); Connecto will fetch every matching company itself.")
        return {}
    return {str(slug).strip(): entry for slug, entry in data.items()
            if str(slug).strip() and isinstance(entry, dict) and entry and not entry.get("error")}


def _v1_file(v1_dir: Path, setting, default: str, key: str, warnings: list[str]) -> Path | None:
    """Where v1 keeps one of its files, as v1's config says - but only inside v1's own folder. A config that names a file
    anywhere else (``/etc/passwd``, ``../../other-project/x``, a link that leads out of the folder) is not followed."""
    text = str(setting or default)
    path = v1_dir / text
    try:
        inside = path.resolve().is_relative_to(v1_dir.resolve())
    except (OSError, RuntimeError, ValueError):
        inside = False
    if not inside:
        warnings.append(f"{key}: {text!r} is not inside the v1 folder, so it was not read")
        return None
    return path


@dataclass
class _ProgressPlan:
    rows: list[tuple] = field(default_factory=list)      # (slug, info as JSON, when) to record as "already fetched"
    refetch: int = 0                                      # v1 finished it, but its founders aren't in the founders list


def _count_of(entry: dict, name: str) -> int:
    try:
        return max(0, int(entry.get(name) or 0))
    except (TypeError, ValueError):
        return 0


def _when(value) -> str:
    try:
        return datetime.fromisoformat(str(value)).isoformat(timespec="seconds")
    except ValueError:
        return now_iso()


def _plan_progress(finished: dict[str, dict], slugs_with_people: set[str], existing: set[str]) -> _ProgressPlan:
    """Which of v1's finished companies Connecto may treat as fetched. Connecto's own records are never replaced. A company
    whose founders (with LinkedIn links) are missing from the founders list is NOT marked: skipping it would lose them for
    good, so it is simply fetched again."""
    plan = _ProgressPlan()
    for slug in sorted(finished):
        if slug in existing:
            continue
        entry = finished[slug]
        with_linkedin = _count_of(entry, "with_linkedin")
        if with_linkedin and slug not in slugs_with_people:
            plan.refetch += 1
            continue
        info = {"founders": _count_of(entry, "founders"), "with_linkedin": with_linkedin, "imported_from": "v1"}
        plan.rows.append((slug, json.dumps(info), _when(entry.get("scraped_at"))))
    return plan


_INSERT_PROGRESS = ("INSERT INTO progress(source, item, info, error, at) VALUES('yc', ?, ?, NULL, ?) "
                    "ON CONFLICT(source, item) DO NOTHING")


# ================================================================== the migration
def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    n = 2
    while path.with_name(f"{path.name}.{n}").exists():
        n += 1
    return path.with_name(f"{path.name}.{n}")


def _count(n: int, singular: str, plural: str | None = None) -> str:
    return f"{n} {singular if n == 1 else (plural or singular + 's')}"


def migrate_v1(v1_dir: str | Path, home: Home, *, import_state: bool = True, import_people: bool = False,
               write: bool = True) -> MigrationReport:
    """Migrate a yc-founder-connect folder into the Connecto ``home``.

    * settings: converted, normalised (safety ceilings apply) and saved as ``config.yaml``; a config that was already
      there is kept as ``config.yaml.v1-backup`` (never overwriting an earlier backup);
    * ``import_state``: v1's invitation history is copied into Connecto's database, so nobody v1 already contacted is
      invited again; Connecto's own newer records are never overwritten;
    * ``import_people``: v1's ``founders.csv`` is loaded as people (source ``yc``, with the ``priority`` the YC source
      gives, so the newest batches still go first), and the companies v1 had already fetched (``scrape_progress.json``)
      are marked as fetched, so the YC directory need not be scraped again. A company whose founders are missing from
      the founders list is left to be fetched again rather than lost;
    * ``write=False`` changes nothing at all and reports what would happen.

    Nothing inside ``v1_dir`` is modified. Raises ``FileNotFoundError`` when ``v1_dir`` has no ``config.yaml`` and
    ``ValueError`` when a v1 file that was asked for (the history database, the founders list) is unreadable.
    """
    v1_dir = Path(v1_dir).expanduser()
    if not v1_dir.is_dir():
        raise FileNotFoundError(f"{v1_dir} is not a folder. Give the path of your yc-founder-connect folder (the one with "
                                "config.yaml in it).")
    v1_config = v1_dir / "config.yaml"
    if not v1_config.is_file():
        raise FileNotFoundError(f"There is no config.yaml in {v1_dir}. Give the path of your yc-founder-connect folder "
                                "(config.example.yaml is only the template, there is nothing to migrate from it).")
    if home.config_path.resolve() == v1_config.resolve():
        raise ValueError("Connecto's home folder is the v1 folder itself; choose another one (--home) so v1 stays untouched.")
    try:
        v1 = read_yaml_file(v1_config) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"{v1_config} is not valid YAML ({exc})") from exc
    if not isinstance(v1, dict):
        raise ValueError(f"{v1_config} must contain a YAML mapping at the top level")

    raw_cfg, warnings = convert_config(v1)
    cfg, normalize_warnings = config_mod.normalize(raw_cfg)
    warnings = warnings + normalize_warnings
    notes: list[str] = []
    li = v1.get("linkedin") if isinstance(v1.get("linkedin"), dict) else {}
    yc_in = v1.get("yc") if isinstance(v1.get("yc"), dict) else {}

    # ---- read everything first, so a damaged v1 file stops the migration before anything is written
    invites_plan = None
    if import_state:
        state_path = _v1_file(v1_dir, li.get("state_db"), V1_STATE_DB, "linkedin.state_db", warnings)
        rows = _read_v1_invites(state_path) if state_path else None
        if rows is None:
            warnings.append(f"There is no v1 invitation history at {state_path or 'the place the v1 settings name'}, so Connecto "
                            "doesn't know who v1 already invited and may contact them again.")
        else:
            if len(rows) >= MAX_INVITE_RECORDS:
                warnings.append(f"v1's invitation history has more than {MAX_INVITE_RECORDS:,} records; only the first "
                                f"{MAX_INVITE_RECORDS:,} were read.")
            invites_plan = _plan_invites(rows, _existing_invites(home))
    else:
        notes.append("v1's invitation history was not imported: Connecto won't know who v1 already invited.")

    csv_path = _v1_file(v1_dir, li.get("founders_csv"), V1_FOUNDERS_CSV, "linkedin.founders_csv", warnings)
    people: list[dict] = []
    people_skipped = 0
    progress_plan: _ProgressPlan | None = None
    if import_people:
        if csv_path is not None and csv_path.is_file():
            founders = _read_founders_csv(csv_path)
            people, people_skipped = _with_priority(founders.people), founders.skipped
            warnings.extend(founders.notes)
            progress_path = _v1_file(v1_dir, yc_in.get("progress_json"), V1_PROGRESS_JSON, "yc.progress_json", warnings)
            finished = _read_v1_progress(progress_path, warnings) if progress_path else {}
            progress_plan = _plan_progress(finished, founders.slugs, _existing_progress(home))
        else:
            notes.append(f"No founders file at {csv_path or 'the place the v1 settings name'}, so no people were loaded; Connecto's "
                         "YC source fetches them itself.")
    elif csv_path is not None and csv_path.is_file():
        try:
            found = _read_founders_csv(csv_path).people
        except ValueError:
            pass                                    # only a hint: a damaged file matters once someone asks for the import
        else:
            notes.append(f"Your v1 founders list ({_count(len(found), 'person', 'people')}) was not imported; Connecto's YC source "
                         "fetches founders itself. Import it to skip the first scrape.")

    # ---- write
    config_path: Path | None = None
    invites_imported = 0
    people_imported = 0
    if write:
        home.ensure()
        config_path = home.config_path
        keep_current = False
        if config_path.exists():
            try:
                keep_current = config_mod.normalize(config_mod.load_raw(config_path))[0] == cfg
            except (OSError, ValueError, yaml.YAMLError):
                keep_current = False
            if keep_current:
                notes.append("config.yaml already holds these settings; it was left as it was.")
            else:
                backup = _unique_path(config_path.with_name(config_path.name + ".v1-backup"))
                shutil.copy2(config_path, backup)
                notes.append(f"You already had a Connecto config: it was replaced and your old one is kept as {backup.name}.")
        if not keep_current:
            config_mod.save(home, cfg)
            notes.append(f"Settings saved to {config_path}.")
        if (invites_plan and invites_plan.rows) or people or (progress_plan and progress_plan.rows):
            with Store(home.db_path) as store:
                if invites_plan and invites_plan.rows:
                    with store.conn:
                        store.conn.executemany(_UPSERT_INVITE, invites_plan.rows)
                    invites_imported = len(invites_plan.rows)
                if people:
                    added, updated = store.upsert_people(people, "yc", "yc")
                    people_imported = added + updated
                if progress_plan and progress_plan.rows:
                    with store.conn:
                        store.conn.executemany(_INSERT_PROGRESS, progress_plan.rows)
    else:
        notes.append("Dry run: nothing was written.")
        invites_imported = len(invites_plan.rows) if invites_plan else 0
        people_imported = len(people)

    if invites_plan is not None:
        extra = []
        if invites_plan.kept:
            extra.append(f"{invites_plan.kept} already known to Connecto, left alone")
        if invites_plan.unusable:
            extra.append(f"{invites_plan.unusable} without a usable LinkedIn profile URL, skipped")
        if not invites_imported and not extra:
            notes.append("v1's invitation history is empty: nothing to copy.")
        else:
            notes.append(f"v1 invitation history: {_count(invites_imported, 'record')} {'copied' if write else 'would be copied'}"
                         + (f" ({'; '.join(extra)})" if extra else "") + ".")
    if import_people and people:
        notes.append(f"{_count(people_imported, 'person', 'people')} {'loaded' if write else 'would be loaded'} from {csv_path.name}"
                     + (f" ({people_skipped} without a LinkedIn profile URL, skipped)." if people_skipped else "."))
    if progress_plan is not None and (progress_plan.rows or progress_plan.refetch):
        parts = [f"{_count(len(progress_plan.rows), 'company', 'companies')} {'marked' if write else 'would be marked'} as already fetched"]
        if progress_plan.refetch:
            parts.append(f"{progress_plan.refetch} will be fetched again (their founders are not in {csv_path.name})")
        notes.append("Scraping progress from v1: " + "; ".join(parts) + ".")

    # ---- what to do next
    notes.append("Log in to LinkedIn once more the first time Connecto runs: it uses its own browser window and v1's "
                 "browser profile is not copied.")
    notes.append("Accept the LinkedIn terms-of-service note again (it is switched off in the new settings).")
    try:
        old_job = Path.home() / "Library" / "LaunchAgents" / V1_LAUNCHD_PLIST
        if old_job.exists():
            warnings.append(f"v1's daily job is still installed ({old_job}); remove it with `python schedule.py uninstall` in "
                            f"{v1_dir} so it doesn't send alongside Connecto.")
    except (OSError, RuntimeError):
        pass
    notes.append("If v1 was scheduled with cron or Task Scheduler, switch that off before scheduling Connecto, so the two "
                 "never run together.")

    return MigrationReport(config_path=config_path, templates=len(cfg["message"]["templates"]),
                           invites_imported=invites_imported, people_imported=people_imported, warnings=warnings,
                           notes=notes)


__all__ = ["MigrationReport", "convert_config", "migrate_v1"]
