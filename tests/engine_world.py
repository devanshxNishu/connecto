"""A throw-away Connecto home, a mock LinkedIn and a few helpers to add people and run - shared by the browser tests."""
from __future__ import annotations

import pytest

from connecto import browser, runner
from connecto.runner import RunOptions
from connecto.store import Store

from helpers import FAST, add_people, make_cfg, make_home, person
from mock_linkedin import SCENARIOS, MockLinkedIn

NAMES = {slug: sc["name"] for slug, sc in SCENARIOS.items()}


class World:
    """A throw-away Connecto home, a mock LinkedIn, and a few helpers to add people and run."""

    def __init__(self, home, mock):
        self.home, self.mock, self.logs, self.sleeps = home, mock, [], []

    def add(self, *slugs, **common):
        """Add people in the given order (earlier = higher priority, so the queue order is fixed)."""
        rows = [person(s, NAMES.get(s, s.title()), priority=str(len(slugs) - i), **common) for i, s in enumerate(slugs)]
        add_people(self.home, rows)
        return rows

    def run(self, cfg=None, limits=FAST, **opts):
        opts.setdefault("ignore_hours", True)
        opts.setdefault("sleep", self.sleeps.append)
        opts.setdefault("seed", 1)
        ro = RunOptions(headless=True, base_url=self.mock.base_url, limits=limits, log=self.logs.append, **opts)
        return runner.run_send(self.home, cfg or make_cfg(), ro)

    def statuses(self):
        with Store(self.home.db_path) as st:
            return {i["linkedin_url"].rstrip("/").rsplit("/", 1)[1]: i["status"] for i in st.recent_invites(100)}

    def invite(self, slug):
        """The stored invitation row for the person with this profile slug (None if there is none)."""
        with Store(self.home.db_path) as st:
            for row in st.recent_invites(200):
                if row["linkedin_url"].rstrip("/").endswith("/" + slug):
                    return row
        return None

    def kv(self, key):
        with Store(self.home.db_path) as st:
            return st.kv_get(key)

    def sent_names(self):
        return [s["name"] for s in self.mock.sent]

    def log_text(self):
        return "\n".join(self.logs)


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(browser, "PACE", 0.02)
    with MockLinkedIn() as mock:
        yield World(make_home(tmp_path), mock)
