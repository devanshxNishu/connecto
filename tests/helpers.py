"""Shared test helpers: a throw-away Connecto home with a config and some people in it."""
from __future__ import annotations

import copy
import errno
import sys

import pytest

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


# Windows does not let anybody delete, rename or replace a file that a program has open, so there a run's lock file cannot
# be taken away from under it. The tests that do exactly that to check what a run makes of it only apply elsewhere.
deletes_an_open_file = pytest.mark.skipif(sys.platform == "win32",
                                          reason="Windows refuses to delete or replace a file that is open")


def refuse_file_locks(monkeypatch) -> None:
    """Make the run lock meet a file system without file locks, whichever kind of lock this system uses."""
    from connecto import lock as lock_module

    def refuse(*_):
        raise OSError(errno.ENOLCK, "No locks available")

    if lock_module.fcntl is not None:
        monkeypatch.setattr(lock_module.fcntl, "flock", refuse)
    else:                                                       # Windows
        monkeypatch.setattr(lock_module.msvcrt, "locking", refuse)
