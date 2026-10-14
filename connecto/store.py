"""SQLite bookkeeping: the people we know about, what happened to each invitation, run history, small caches.

One ``Store`` object owns one connection - create one per thread / per web request (they are cheap). WAL mode lets the
setup page read while a run writes.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Iterator

from . import privacy
from .models import person_key
from .util import now_iso

# Terminal statuses are never retried automatically. ``sending`` is an invitation that was (maybe) sent but whose
# confirmation was never seen: it counts toward the limits and the person is not tried again unless you ask.
TERMINAL = {"sent", "sending", "already_connected", "already_pending", "not_found", "needs_email", "no_connect_option",
            "skipped"}
RETRYABLE = {"error", "limit_reached", "dry_run", "note_unavailable"}
STATUSES = sorted(TERMINAL | RETRYABLE)
# What `connecto run --retry-errors` tries again: unexplained errors, people who "had no Connect option" (that is
# usually LinkedIn's page changing, not the person), and invitations whose confirmation was never seen (if one did go
# out the profile now says Pending, and the person is recorded as already_pending).
RETRY_WITH_FLAG = {"error", "no_connect_option", "sending"}
# What counts toward the daily and weekly limits.
COUNTED = ("sent", "sending")


def waiting(status: str | None, retry_errors: bool = False) -> bool:
    """Is a person with this invitation status (None: never tried) still waiting to be invited?"""
    if status is None:
        return True
    if status in RETRY_WITH_FLAG:
        return retry_errors
    return status not in TERMINAL

SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
    key         TEXT PRIMARY KEY,
    linkedin_url TEXT NOT NULL,
    full_name   TEXT,
    source_type TEXT,
    source_id   TEXT,
    data        TEXT NOT NULL,
    added_at    TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_people_source ON people(source_type, source_id);
CREATE TABLE IF NOT EXISTS invites (
    key          TEXT PRIMARY KEY,
    linkedin_url TEXT,
    full_name    TEXT,
    company      TEXT,
    status       TEXT NOT NULL,
    note         TEXT,
    detail       TEXT,
    note_kind    TEXT,
    attempted_at TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_invites_status_time ON invites(status, updated_at);
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    mode        TEXT,
    sent        INTEGER DEFAULT 0,
    attempted   INTEGER DEFAULT 0,
    stop_reason TEXT
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS progress (
    source TEXT NOT NULL, item TEXT NOT NULL, info TEXT, error TEXT, at TEXT NOT NULL,
    PRIMARY KEY (source, item)
);
CREATE TABLE IF NOT EXISTS ai_cache (
    key TEXT NOT NULL, kind TEXT NOT NULL, value TEXT, created_at TEXT NOT NULL, PRIMARY KEY (key, kind)
);
CREATE TABLE IF NOT EXISTS ai_usage (day TEXT PRIMARY KEY, calls INTEGER NOT NULL DEFAULT 0);
"""

_ROW_META = ("linkedin_url", "source_type", "source_id")


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ================================================================== people
    def upsert_people(self, rows: Iterable[dict], source_type: str, source_id: str = "", *,
                      take_over: bool = False) -> tuple[int, int]:
        """Insert or merge people (de-duplicated by LinkedIn URL). Returns (added, updated).

        ``take_over``: a person who is already stored from another source now belongs to this one (used when you add
        somebody by hand who was also found by a source: you asked for them, so that source's filters no longer apply).
        Whoever you added by hand keeps their place at the front, whatever a source later says about them."""
        added = updated = 0
        now = now_iso()
        rows = list(rows)                                      # (a slow generator must not hold the write lock)
        cur = self.conn.cursor()
        # The write lock is taken before anything is looked at: another program adding people at the same moment (the setup
        # page and `connecto add`, say) can neither insert the person between our look and our insert, nor change what we
        # are about to merge into.
        if not self.conn.in_transaction:
            cur.execute("BEGIN IMMEDIATE")
        try:
            added, updated = self._upsert_people(cur, rows, source_type, source_id, take_over, now)
        except BaseException:
            self.conn.rollback()
            raise
        self.conn.commit()
        return added, updated

    def _upsert_people(self, cur, rows: Iterable[dict], source_type: str, source_id: str, take_over: bool, now: str):
        added = updated = 0
        for row in rows:
            key = person_key(row)
            if not key:
                continue
            row = privacy.scrub_person(row)                    # (the last door: whoever built the row, no contact details are kept)
            data = {k: v for k, v in row.items() if k not in _ROW_META and v not in (None, "")}
            existing = cur.execute("SELECT data, source_type, source_id FROM people WHERE key=?", (key,)).fetchone()
            if existing is None:
                cur.execute(
                    "INSERT INTO people(key, linkedin_url, full_name, source_type, source_id, data, added_at, updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (key, row["linkedin_url"], row.get("full_name", ""), source_type, source_id,
                     json.dumps(data, ensure_ascii=False), now, now))
                added += 1
                continue
            old = json.loads(existing["data"])
            if existing["source_type"] == "manual" and source_type != "manual":
                data.pop("priority", None)
            merged = {**old, **data}
            promote = take_over and (existing["source_type"] or "") != source_type
            if merged != old or promote:
                cur.execute("UPDATE people SET data=?, full_name=?, source_type=?, source_id=?, updated_at=? WHERE key=?",
                            (json.dumps(merged, ensure_ascii=False), merged.get("full_name", ""),
                             source_type if promote else existing["source_type"], source_id if promote else existing["source_id"],
                             now, key))
                updated += 1
        return added, updated

    def _row_to_person(self, r: sqlite3.Row) -> dict:
        d = json.loads(r["data"])
        d.update({"key": r["key"], "linkedin_url": r["linkedin_url"], "source_type": r["source_type"] or "",
                  "source_id": r["source_id"] or "", "added_at": r["added_at"]})
        return d

    def people(self, source_type: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM people", ()
        if source_type:
            sql, args = sql + " WHERE source_type=?", (source_type,)
        return [self._row_to_person(r) for r in self.conn.execute(sql + " ORDER BY added_at, key", args)]

    def iter_people(self) -> Iterator[dict]:
        for r in self.conn.execute("SELECT * FROM people ORDER BY added_at, key"):
            yield self._row_to_person(r)

    def get_person(self, key: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM people WHERE key=?", (key,)).fetchone()
        return self._row_to_person(r) if r else None

    def count_people(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM people").fetchone()[0])

    def count_people_by_source(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT source_type, COUNT(*) AS n FROM people GROUP BY source_type").fetchall()
        return {r["source_type"] or "": int(r["n"]) for r in rows}

    def delete_uncontacted(self, source_type: str | None = None, source_id: str | None = None) -> int:
        """Remove people nobody has been invited yet (optionally only from one source). Returns how many."""
        sql = "DELETE FROM people WHERE key NOT IN (SELECT key FROM invites WHERE status IN (%s))" % ",".join("?" * len(TERMINAL))
        args: list = list(TERMINAL)
        if source_type:
            sql += " AND source_type=?"
            args.append(source_type)
        if source_id:
            sql += " AND source_id=?"
            args.append(source_id)
        n = self.conn.execute(sql, args).rowcount
        self.conn.commit()
        return int(n)

    # ================================================================== invites
    def record(self, person: dict, status: str, note: str = "", detail: str = "", note_kind: str = "") -> None:
        key = person.get("key") or person_key(person)
        now = now_iso()
        # (An invitation recorded as sent is never overwritten by anything else: whatever a later visit found, it was sent.)
        self.conn.execute(
            """INSERT INTO invites(key, linkedin_url, full_name, company, status, note, detail, note_kind, attempted_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET status=excluded.status, note=excluded.note, detail=excluded.detail,
                 note_kind=excluded.note_kind, updated_at=excluded.updated_at
               WHERE invites.status != 'sent' OR excluded.status = 'sent'""",
            (key, person.get("linkedin_url", ""), person.get("full_name", ""), person.get("company", ""),
             status, note, detail, note_kind, now, now))
        self.conn.commit()

    def forget_status(self, key: str) -> bool:
        n = self.conn.execute("DELETE FROM invites WHERE key=? AND status IN "
                              "('skipped','error','limit_reached','dry_run','note_unavailable','no_connect_option','sending')",
                              (key,)).rowcount
        self.conn.commit()
        return bool(n)

    def status_of(self, key: str) -> str | None:
        r = self.conn.execute("SELECT status FROM invites WHERE key=?", (key,)).fetchone()
        return r["status"] if r else None

    def statuses(self) -> dict[str, str]:
        return {r["key"]: r["status"] for r in self.conn.execute("SELECT key, status FROM invites")}

    def done_keys(self) -> set[str]:
        ph = ",".join("?" * len(TERMINAL))
        return {r["key"] for r in self.conn.execute(f"SELECT key FROM invites WHERE status IN ({ph})", tuple(TERMINAL))}

    def sent_since(self, since: datetime) -> int:
        r = self.conn.execute("SELECT COUNT(*) AS n FROM invites WHERE status IN ('sent','sending') AND updated_at>=?",
                              (since.isoformat(timespec="seconds"),)).fetchone()
        return int(r["n"])

    def sent_today(self, now: datetime | None = None) -> int:
        now = now or datetime.now()
        return self.sent_since(now.replace(hour=0, minute=0, second=0, microsecond=0))

    def sent_last_7_days(self, now: datetime | None = None) -> int:
        now = now or datetime.now()
        return self.sent_since(now - timedelta(days=7))

    def recent_invites(self, limit: int = 50, status: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM invites", []
        if status:
            sql += " WHERE status=?"
            args.append(status)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        args.append(int(limit))
        return [dict(r) for r in self.conn.execute(sql, args)]

    def summary(self) -> dict:
        by_status = {r["status"]: int(r["n"]) for r in
                     self.conn.execute("SELECT status, COUNT(*) AS n FROM invites GROUP BY status")}
        return {
            "by_status": by_status,
            "sent_today": self.sent_today(),
            "sent_last_7_days": self.sent_last_7_days(),
            "total_sent": by_status.get("sent", 0),
            "people": self.count_people(),
        }

    # ================================================================== runs
    def start_run(self, mode: str) -> int:
        cur = self.conn.execute("INSERT INTO runs(started_at, mode) VALUES(?,?)", (now_iso(), mode))
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, sent: int, attempted: int, stop_reason: str) -> None:
        self.conn.execute("UPDATE runs SET finished_at=?, sent=?, attempted=?, stop_reason=? WHERE id=?",
                          (now_iso(), sent, attempted, stop_reason, run_id))
        self.conn.commit()

    def recent_runs(self, n: int = 10) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (int(n),))]

    # ================================================================== key/value
    def kv_get(self, k: str, default=None):
        r = self.conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r["v"] if r else default

    def kv_set(self, k: str, v: str) -> None:
        self.conn.execute("INSERT INTO kv(k, v, updated_at) VALUES(?,?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v, "
                          "updated_at=excluded.updated_at", (k, str(v), now_iso()))
        self.conn.commit()

    def kv_del(self, k: str) -> None:
        self.conn.execute("DELETE FROM kv WHERE k=?", (k,))
        self.conn.commit()

    # ================================================================== source progress (resumable scrapes)
    def progress_done(self, source: str) -> set[str]:
        return {r["item"] for r in self.conn.execute("SELECT item FROM progress WHERE source=? AND error IS NULL", (source,))}

    def progress_mark(self, source: str, item: str, info: dict | None = None, error: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO progress(source, item, info, error, at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(source, item) DO UPDATE SET info=excluded.info, error=excluded.error, at=excluded.at",
            (source, item, json.dumps(info or {}), error, now_iso()))
        self.conn.commit()

    def progress_reset(self, source: str, items: Iterable[str] | None = None) -> None:
        if items is None:
            self.conn.execute("DELETE FROM progress WHERE source=?", (source,))
        else:
            self.conn.executemany("DELETE FROM progress WHERE source=? AND item=?", [(source, i) for i in items])
        self.conn.commit()

    # ================================================================== AI cache and daily budget
    def cache_get(self, key: str, kind: str) -> str | None:
        r = self.conn.execute("SELECT value FROM ai_cache WHERE key=? AND kind=?", (key, kind)).fetchone()
        return r["value"] if r else None

    def cache_set(self, key: str, kind: str, value: str) -> None:
        self.conn.execute("INSERT INTO ai_cache(key, kind, value, created_at) VALUES(?,?,?,?) "
                          "ON CONFLICT(key, kind) DO UPDATE SET value=excluded.value, created_at=excluded.created_at",
                          (key, kind, value, now_iso()))
        self.conn.commit()

    def cache_prune(self, days: int) -> int:
        """Forget every cached answer older than ``days`` days (of any kind: answers kept for an older prompt go too)."""
        cutoff = (datetime.now() - timedelta(days=max(0, int(days)))).isoformat(timespec="seconds")
        cur = self.conn.execute("DELETE FROM ai_cache WHERE created_at < ?", (cutoff,))
        self.conn.commit()
        return cur.rowcount

    def cache_clear(self, kind: str | None = None) -> None:
        if kind:
            self.conn.execute("DELETE FROM ai_cache WHERE kind=?", (kind,))
        else:
            self.conn.execute("DELETE FROM ai_cache")
        self.conn.commit()

    def ai_calls_today(self) -> int:
        r = self.conn.execute("SELECT calls FROM ai_usage WHERE day=?", (datetime.now().strftime("%Y-%m-%d"),)).fetchone()
        return int(r["calls"]) if r else 0

    def ai_bump(self, n: int = 1) -> None:
        day = datetime.now().strftime("%Y-%m-%d")
        self.conn.execute("INSERT INTO ai_usage(day, calls) VALUES(?,?) ON CONFLICT(day) DO UPDATE SET calls=calls+?",
                          (day, n, n))
        self.conn.commit()
