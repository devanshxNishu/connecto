"""Settings: defaults, load/save, normalisation (clamping to the safety ceilings), and the starter file.

The config is a plain YAML mapping. There are no secrets in it: API keys for the optional AI helper live in the
home folder's ``.env`` and LinkedIn credentials are never stored anywhere (you log in by hand, once, in the
automation browser).
"""
from __future__ import annotations

import copy
import re
import shutil
from pathlib import Path

import yaml

from . import limits as limits_mod
from . import regexsafe
from .paths import Home
from .util import (NAME_RULE, as_list, atomic_write, clean_text, is_secret_name, norm_key, read_yaml_file, real_number,
                   whole_number)

SOURCE_TYPES = ("yc", "csv", "webpage", "linkedin_search")
AI_PROVIDERS = ("gemini", "groq", "openrouter", "ollama", "custom")
ORDERS = ("priority", "random", "newest_added", "oldest_added")
PLANS = ("free", "premium")
PLAN_NOTE_CHARS = {"free": 200, "premium": 300}
LINKEDIN_NOTE_MAX = 300

DEFAULT_TEMPLATES = [
    "Hi {first_name}, I'm {me_first_name}, {me_about}. Your work[[ at {company}]] caught my eye - would love to connect "
    "and follow what you're building.",
    "Hi {first_name} - {me_first_name} here, {me_about}. Always keen to connect with people doing interesting "
    "work[[ at {company}]]. Would be great to have you in my network.",
    "Hi {first_name}, I'm {me_first_name} ({me_about}). Came across your profile[[ and your role as {title}]] - "
    "I'd love to connect.",
]

# Used instead of/in addition to the defaults above when the AI helper is on and you have not written your own templates.
DEFAULT_AI_TEMPLATES = [
    "Hi {first_name}, {hook} I'm {me_first_name}, {me_about} - would love to connect.",
    "Hi {first_name} - {hook} I'm {me_first_name} ({me_about}). Would be great to connect.",
]

DEFAULTS: dict = {
    "version": 2,
    "me": {"first_name": "", "about": "", "project": "", "project_blurb": ""},
    "sources": [],
    "targets": {
        "keywords": [], "exclude_keywords": [], "titles": [], "exclude_titles": [], "locations": [],
        "titles_regex": "", "exclude_titles_regex": "",
    },
    "order": "priority",
    "message": {"send_note": True, "plan": "free", "max_note_chars": 0, "templates": [], "if_note_unavailable": "plain"},
    "ai": {
        "enabled": False, "provider": "gemini", "model": "", "base_url": "", "api_key_env": "",
        "max_calls_per_day": 60, "temperature": 0.6,
    },
    "limits": {
        "daily": limits_mod.DEFAULTS["daily"], "weekly": limits_mod.DEFAULTS["weekly"],
        "per_run": limits_mod.DEFAULTS["per_run"], "delay_seconds": limits_mod.DEFAULTS["delay"],
        "stop_after_errors": limits_mod.DEFAULTS["stop_after_errors"],
    },
    "schedule": {
        "times": ["10:40", "18:20"], "active_hours": limits_mod.DEFAULTS["active_hours"],
        "active_days": limits_mod.DEFAULTS["active_days"],
        "start_jitter_minutes": limits_mod.DEFAULTS["start_jitter_minutes"], "fetch_before_send": True,
    },
    "browser": {"channel": "chrome", "headless": False},
    "notify": True,
    "acknowledged_risks": False,
}


# ----------------------------------------------------------------------------- load / save
def defaults() -> dict:
    return copy.deepcopy(DEFAULTS)


def deep_merge(base: dict, override: dict) -> dict:
    """Return base updated with override. Dicts merge recursively; everything else (lists too) is replaced."""
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_raw(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    data = read_yaml_file(p) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{p} must contain a YAML mapping at the top level")
    return data


def load(home: Home, with_warnings: bool = False):
    """Load, merge with defaults and normalise. Returns cfg (and warnings when asked)."""
    cfg, warnings = normalize(load_raw(home.config_path))
    return (cfg, warnings) if with_warnings else cfg


def save(home: Home, cfg: dict) -> None:
    """Write config.yaml atomically (keeping one .bak of the previous version)."""
    home.ensure()
    cfg, _ = normalize(cfg)
    if home.config_path.exists():
        try:
            shutil.copyfile(home.config_path, home.config_path.with_suffix(".yaml.bak"))
        except OSError:
            pass
    header = ("# Connecto settings. Safe to edit by hand; the setup page (`connecto ui`) rewrites this file.\n"
              "# No secrets belong here: API keys go in .env, and you log in to LinkedIn by hand.\n")
    body = yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True, width=110, default_flow_style=None)
    atomic_write(home.config_path, header + body)


# ----------------------------------------------------------------------------- normalisation
def _bool(value, default=False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if value is None:
        return default
    return bool(value)


def _int(value, default: int, lo: int, hi: int) -> int:
    try:
        v = whole_number(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _float(value, default: float, lo: float, hi: float) -> float:
    try:
        v = real_number(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _regex(value, name: str, warnings: list[str]) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    why = regexsafe.problem(value)
    if why:
        warnings.append(f"targets.{name}: {why}; ignored"
                        + (" - people whose title matches it are NOT left out" if name.startswith("exclude") else ""))
        return ""
    return value


def normalize(cfg: dict | None) -> tuple[dict, list[str]]:
    """Merge with defaults, coerce types, clamp to the safety ceilings. Returns (config, warnings). Never raises: a
    setting shaped in a way nothing anticipated is dropped (its default applies) and named in a warning."""
    raw = cfg if isinstance(cfg, dict) else {}
    try:
        return _normalize(raw)
    except Exception:  # noqa: BLE001 - find out which top-level settings are to blame, keep the rest
        pass
    kept: dict = {}
    dropped: list[str] = []
    for key, value in raw.items():
        try:
            _normalize({**kept, key: value})
            kept[key] = value
        except Exception:  # noqa: BLE001
            dropped.append(clean_text(key, 40))
    out, warnings = _normalize(kept)
    return out, warnings + [f"{name}: could not be read, so its default is used" for name in dropped]


def _normalize(cfg: dict) -> tuple[dict, list[str]]:
    warnings: list[str] = []
    raw = cfg or {}
    out = deep_merge(defaults(), raw if isinstance(raw, dict) else {})
    known = set(DEFAULTS)
    for k in list(out):
        if k not in known:
            warnings.append(f"unknown setting '{k}' ignored")
            out.pop(k)

    # --- me
    me_in = out.get("me") if isinstance(out.get("me"), dict) else {}
    me = {}
    for k, v in me_in.items():
        nk = norm_key(k)
        if nk and v is not None and not isinstance(v, (dict, list)):
            me[nk] = clean_text(v, 300)
    for k in ("first_name", "about", "project", "project_blurb"):
        me.setdefault(k, "")
    out["me"] = me

    # --- targets
    t_in = out.get("targets") if isinstance(out.get("targets"), dict) else {}
    t = {k: as_list(t_in.get(k)) for k in ("keywords", "exclude_keywords", "titles", "exclude_titles", "locations")}
    t["titles_regex"] = _regex(t_in.get("titles_regex"), "titles_regex", warnings)
    t["exclude_titles_regex"] = _regex(t_in.get("exclude_titles_regex"), "exclude_titles_regex", warnings)
    out["targets"] = t

    # --- order
    if out.get("order") not in ORDERS:
        warnings.append(f"order: expected one of {', '.join(ORDERS)}; using 'priority'")
        out["order"] = "priority"

    # --- message
    m_in = out.get("message") if isinstance(out.get("message"), dict) else {}
    plan = str(m_in.get("plan") or "free").lower()
    if plan not in PLANS:
        warnings.append("message.plan: expected 'free' or 'premium'; using 'free'")
        plan = "free"
    raw_templates = m_in.get("templates") or []
    if isinstance(raw_templates, str):
        raw_templates = [raw_templates]                    # one template written without the list brackets
    elif not isinstance(raw_templates, (list, tuple)):
        warnings.append("message.templates: expected a list of texts; ignored")
        raw_templates = []
    templates = [str(x).strip() for x in raw_templates if isinstance(x, (str, int, float)) and str(x).strip()]
    templates = [t_[:700] for t_ in templates]
    max_chars = _int(m_in.get("max_note_chars"), 0, 0, LINKEDIN_NOTE_MAX)
    if max_chars and max_chars < 40:
        warnings.append("message.max_note_chars: very small; raised to 40")
        max_chars = 40
    unavailable = str(m_in.get("if_note_unavailable") or "plain").lower()
    if unavailable not in ("plain", "stop"):
        warnings.append("message.if_note_unavailable: expected 'plain' or 'stop'; using 'plain'")
        unavailable = "plain"
    out["message"] = {"send_note": _bool(m_in.get("send_note"), True), "plan": plan, "max_note_chars": max_chars,
                      "templates": templates, "if_note_unavailable": unavailable}

    # --- ai
    a_in = out.get("ai") if isinstance(out.get("ai"), dict) else {}
    provider = str(a_in.get("provider") or "gemini").strip().lower()
    if provider not in AI_PROVIDERS:
        warnings.append(f"ai.provider: unknown '{provider}'; expected one of {', '.join(AI_PROVIDERS)}")
        provider = "gemini"
    key_env = str(a_in.get("api_key_env") or "").strip()
    if key_env and not is_secret_name(key_env):
        warnings.append(f"ai.api_key_env: use {NAME_RULE}; ignored")
        key_env = ""
    out["ai"] = {
        "enabled": _bool(a_in.get("enabled"), False), "provider": provider,
        "model": clean_text(a_in.get("model"), 120), "base_url": str(a_in.get("base_url") or "").strip()[:300],
        "api_key_env": key_env, "max_calls_per_day": _int(a_in.get("max_calls_per_day"), 60, 0, 1000),
        "temperature": _float(a_in.get("temperature"), 0.6, 0.0, 1.2),
    }

    # --- limits / schedule (clamped by limits_from_config; we store the clamped numbers back)
    lim = limits_mod.limits_from_config(out)
    warnings.extend(lim.warnings)
    out["limits"] = {
        "daily": list(lim.daily), "weekly": lim.weekly, "per_run": list(lim.per_run),
        "delay_seconds": [int(lim.delay[0]), int(lim.delay[1])], "stop_after_errors": lim.stop_after_errors,
    }
    s_in = out.get("schedule") if isinstance(out.get("schedule"), dict) else {}
    times = []
    for item in as_list(s_in.get("times")):
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", item)
        if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
            times.append(f"{int(m.group(1)):02d}:{m.group(2)}")
        else:
            warnings.append(f"schedule.times: ignoring bad time {item!r}")
    out["schedule"] = {
        "times": times or ["10:40", "18:20"],
        "active_hours": ([f"{lim.active_hours[0]:%H:%M}", f"{lim.active_hours[1]:%H:%M}"] if lim.active_hours else []),
        "active_days": [limits_mod.DAY_NAMES[d] for d in sorted(lim.active_days)],
        "start_jitter_minutes": lim.start_jitter_minutes,
        "fetch_before_send": _bool(s_in.get("fetch_before_send"), True),
    }

    # --- browser
    b_in = out.get("browser") if isinstance(out.get("browser"), dict) else {}
    channel = str(b_in.get("channel") or "chrome").lower()
    if channel not in ("chrome", "chromium", "msedge"):
        warnings.append("browser.channel: expected chrome, msedge or chromium; using chrome")
        channel = "chrome"
    out["browser"] = {"channel": channel, "headless": _bool(b_in.get("headless"), False)}
    out["notify"] = _bool(out.get("notify"), True)
    out["acknowledged_risks"] = _bool(out.get("acknowledged_risks"), False)

    # --- sources
    sources = []
    raw_sources = out.get("sources") or []
    if not isinstance(raw_sources, (list, tuple)):
        warnings.append("sources: expected a list; ignored")
        raw_sources = []
    for i, spec in enumerate(raw_sources):
        if not isinstance(spec, dict):
            warnings.append(f"sources[{i}]: expected a mapping; ignored")
            continue
        typ = str(spec.get("type") or "").strip().lower()
        if typ not in SOURCE_TYPES:
            warnings.append(f"sources[{i}]: unknown type '{typ}' (expected one of {', '.join(SOURCE_TYPES)}); ignored")
            continue
        sources.append({**spec, "type": typ, "enabled": _bool(spec.get("enabled"), True)})
    out["sources"] = sources
    return out, warnings


# ----------------------------------------------------------------------------- helpers over a normalised config
def limits(cfg: dict) -> limits_mod.Limits:
    return limits_mod.limits_from_config(cfg)


def note_limit(cfg: dict) -> int:
    """Characters allowed in the connection note: explicit setting, else by plan (free 200, premium 300)."""
    m = cfg.get("message") or {}
    explicit = int(m.get("max_note_chars") or 0)
    return explicit or PLAN_NOTE_CHARS.get(m.get("plan", "free"), 200)


def templates(cfg: dict) -> list[str]:
    """Your templates; when you have none, the built-in friendly ones (plus the AI ones when the AI helper is on)."""
    own = list((cfg.get("message") or {}).get("templates") or [])
    if own:
        return own
    base = list(DEFAULT_TEMPLATES)
    if (cfg.get("ai") or {}).get("enabled"):
        base += DEFAULT_AI_TEMPLATES
    return base


def enabled_sources(cfg: dict) -> list[dict]:
    return [s for s in cfg.get("sources", []) if s.get("enabled", True)]


def problems(cfg: dict) -> list[str]:
    """Things that stop a *send* run (as opposed to warnings). Empty list = ready."""
    out = []
    me = cfg.get("me") or {}
    if (cfg.get("message") or {}).get("send_note", True) and not me.get("first_name"):
        out.append("Tell Connecto your first name - it signs each note (Message tab, or me.first_name in config.yaml).")
    if not cfg.get("acknowledged_risks"):
        out.append("Read and accept the LinkedIn terms-of-service risk note first (`connecto init` or the setup page).")
    return out


# ----------------------------------------------------------------------------- starter file
def starter_yaml(first_name: str = "", about: str = "", plan: str = "free") -> str:
    """A commented config for `connecto init`. Kept as text (not dumped) so the comments survive."""
    first = first_name.replace('"', "'")
    ab = about.replace('"', "'")
    return f'''# Connecto settings. Safe to edit by hand; the setup page (`connecto ui`) can also edit this file.
# No secrets belong here: API keys go in .env, and you log in to LinkedIn by hand, once.
version: 2

# ---- about you (signs and fills the note). Extra keys you add here are available as {{me_<key>}}.
me:
  first_name: "{first}"
  about: "{ab}"                  # one short line, e.g. "a product designer in Pune"  -> {{me_about}}
  project: ""                    # optional: what you're building / looking for     -> {{me_project}}
  project_blurb: ""              # optional: one short clause about it              -> {{me_project_blurb}}

# ---- where people come from. Add as many as you like (see README): yc | csv | webpage | linkedin_search
sources: []
#  - type: yc
#    batches: [S26, F26]
#    industries: [fintech]
#  - type: csv
#    path: ~/leads.csv
#  - type: webpage
#    urls: ["https://example.com/speakers"]
#  - type: linkedin_search
#    queries: ["head of design fintech india"]

# ---- filters applied to everyone, whatever the source. Empty = no restriction.
targets:
  keywords: []                   # any of these words appear in the person's headline/title/company/tags...
  exclude_keywords: []
  titles: []                     # e.g. [founder, ceo, "head of"]
  exclude_titles: []             # e.g. [recruiter, advisor]
  locations: []                  # e.g. [India, Bangalore]

order: priority                  # priority (best/newest first, shuffled within) | random | newest_added | oldest_added

# ---- the note. {{placeholders}} come from the person ({{first_name}} {{company}} {{title}} ...) and from `me`
# ({{me_first_name}} {{me_about}} ...). [[ optional text with {{company}} ]] is dropped if the value is missing or
# the note is too long. A template that needs a value the person doesn't have is skipped for that person.
message:
  send_note: true
  plan: {plan}                   # free = 200 characters, premium = 300
  templates: []                  # empty = built-in friendly defaults; add your own to sound like you
  if_note_unavailable: plain     # free accounts can only add a few personal notes: plain = send the invite without one,
                                 # stop = pause the run until notes are available again

# ---- optional AI helper: writes one personalised opening line per person. Off by default; works without it.
ai:
  enabled: false
  provider: gemini               # gemini | groq | openrouter | ollama | custom   (all have free options)

# ---- safety limits (clamped to hard ceilings in code: daily 30, weekly 100, per run 20, delay >= 30 s)
limits:
  daily: [8, 12]                 # a random target in this range is picked for each day
  weekly: 60
  per_run: [4, 7]
  delay_seconds: [60, 180]
  stop_after_errors: 3

schedule:
  times: ["10:40", "18:20"]      # used by `connecto schedule install`
  active_hours: ["09:00", "20:30"]
  active_days: [mon, tue, wed, thu, fri, sat]
  start_jitter_minutes: 30

browser:
  channel: chrome                # chrome | msedge | chromium
  headless: false                # keep false: a visible window lets you watch and step in (hidden browsers are easier to flag)

notify: true
acknowledged_risks: false        # set by `connecto init` after you accept the risk note
'''
