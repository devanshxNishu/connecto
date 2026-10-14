"""Several programs add people to the same database at once - the setup page, ``connecto add``, a run's source fetch. Nobody may
crash, and nobody's data may be lost."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from connecto.models import person_key
from connecto.store import Store

ROOT = Path(__file__).resolve().parent.parent
PEOPLE = 150
WORKERS = 3

WORKER = r"""
import sys, time
from connecto.store import Store
db, worker, go_at = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
people = list(range(150))
if worker % 2:
    people.reverse()                                   # (they meet in the middle, then go on over the same people)
while time.time() < go_at:
    time.sleep(0.001)
with Store(db) as store:
    for start in range(0, len(people), 3):
        rows = [{"linkedin_url": f"https://www.linkedin.com/in/person-{i}/", "full_name": f"Person {i}",
                 f"seen_by_{worker}": "yes", "shared": f"from {worker}"} for i in people[start:start + 3]]
        store.upsert_people(rows, "test", "race")
print("ok")
"""


def run_workers(db: Path) -> list[subprocess.CompletedProcess]:
    go_at = time.time() + 1.5
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    procs = [subprocess.Popen([sys.executable, "-c", WORKER, str(db), str(i), str(go_at)], cwd=ROOT, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(WORKERS)]
    out = []
    for p in procs:
        stdout, stderr = p.communicate(timeout=120)
        out.append(subprocess.CompletedProcess(p.args, p.returncode, stdout, stderr))
    return out


@pytest.mark.skipif(sys.platform == "win32", reason="the workers are plain Python processes; kept simple")
def test_three_programs_adding_the_same_people_at_once_neither_crash_nor_lose_anything(tmp_path):
    db = tmp_path / "connecto.sqlite"
    Store(db).close()                                                     # (the schema exists before they start)
    results = run_workers(db)
    for r in results:
        assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr[-800:]
    with Store(db) as store:
        assert store.count_people() == PEOPLE
        for person in store.people():
            assert all(person.get(f"seen_by_{w}") == "yes" for w in range(WORKERS)), person       # (every merge survived)
            assert person_key(person) and person["full_name"].startswith("Person ")


def test_a_failure_halfway_leaves_nothing_added_and_nothing_locked(tmp_path):
    db = tmp_path / "x.sqlite"
    with Store(db) as store, Store(db) as other:
        rows = [{"linkedin_url": "https://www.linkedin.com/in/a-one/", "full_name": "A One"},
                {"linkedin_url": "https://www.linkedin.com/in/b-two/", "full_name": "B Two", "weird": object()}]   # (can't be stored)
        with pytest.raises(TypeError):
            store.upsert_people(rows, "test")
        assert store.count_people() == 0 and not store.conn.in_transaction
        other.conn.execute("PRAGMA busy_timeout=300")
        other.upsert_people(rows[:1], "test")                              # (the failed attempt does not hold the database)
        assert other.count_people() == 1


def test_a_slow_list_of_people_does_not_hold_the_database_while_it_is_being_produced(tmp_path):
    db = tmp_path / "x.sqlite"
    with Store(db) as store, Store(db) as other:
        def rows():
            # while the list is still being produced, somebody else can write
            other.upsert_people([{"linkedin_url": "https://www.linkedin.com/in/b-two/", "full_name": "B Two"}], "test")
            yield {"linkedin_url": "https://www.linkedin.com/in/a-one/", "full_name": "A One"}

        store.conn.execute("PRAGMA busy_timeout=300")
        other.conn.execute("PRAGMA busy_timeout=300")
        assert store.upsert_people(rows(), "test") == (1, 0)
        assert store.count_people() == 2
