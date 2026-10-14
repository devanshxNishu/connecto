"""Shared test helpers: a throw-away Connecto home with a config and some people in it."""
from __future__ import annotations

import copy

from connecto import config
from connecto.limits import Limits
from connecto.models import make_person
from connecto.paths import Home
from connecto.store import Store

BASE_TEMPLATES = ["Hi {first_name}, I'm {me_first_name} ({me_about}). Would love to connect[[ - saw {company}]]."]


def make_cfg(**over) -> dict:
    """A normalised config that is ready to send (risks accepted, name set) with the browser forced to headless Chromium."""
    raw = {
        "me": {"first_name": "Asha", "about": "a product designer", "project": "NINA", "project_blurb": "an AI stylist"},
        "acknowledged_risks": True,
        "message": {"send_note": True, "plan": "premium", "templates": list(BASE_TEMPLATES)},
        "browser": {"channel": "chromium", "headless": True},
        "notify": False,
    }
    for k, v in over.items():
        raw[k] = {**raw.get(k, {}), **v} if isinstance(v, dict) and isinstance(raw.get(k), dict) else v
    cfg, _ = config.normalize(copy.deepcopy(raw))
    return cfg


FAST = Limits(daily=(50, 50), weekly=100, per_run=(50, 50), delay=(0.0, 0.0), active_hours=None,
              active_days=frozenset(range(7)), stop_after_errors=3, start_jitter_minutes=0.0)


def make_home(tmp_path) -> Home:
    return Home(tmp_path / "home").ensure()


def person(slug: str, name: str | None = None, **fields) -> dict:
    name = name or slug.replace("-", " ").title()
    return make_person(linkedin_url=f"https://www.linkedin.com/in/{slug}/", full_name=name,
                       **{"company": f"{name.split()[0]} Co", "title": "Founder/CEO", **fields})


def add_people(home: Home, people: list[dict], source_type: str = "manual", source_id: str = "manual") -> None:
    with Store(home.db_path) as st:
        st.upsert_people(people, source_type, source_id)
