"""The contract every source of people follows.

A *source* knows how to find people (a YC directory scrape, a CSV file, a team web page, a LinkedIn search) and hands
them to a ``Sink`` as person rows built with ``connecto.models.make_person``. A source also answers ``accepts(row)``:
"may this person be written to?" - that is where source-specific selection (YC batches, industries, ...) lives, so the
sender can re-check every stored person against the sources that are *currently enabled*.

Sources must be cheap to construct (no network, no disk scans): the setup page builds them just to describe them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, ClassVar, Iterable

from ..limits import Limits
from ..models import person_key
from ..paths import Home
from ..store import Store
from ..util import clean_text

_HEADLINE_AT = re.compile(r"^(.+?) (?:at|@) (.+?)(?: \||$)")


def split_headline(headline: str | None) -> tuple[str, str]:
    """('Head of Design', 'Finlo') from 'Head of Design at Finlo | Ex-Razorpay'; ('', '') when it doesn't look like that."""
    m = _HEADLINE_AT.match((headline or "").strip())
    if not m:
        return "", ""
    return clean_text(m.group(1)), clean_text(m.group(2))


def float_pair(value, default: tuple[float, float]) -> tuple[float, float]:
    """[low, high] -> (low, high) floats, non-negative and ordered; ``default`` when it is not a usable pair."""
    try:
        lo, hi = float(value[0]), float(value[1])
    except (TypeError, ValueError, IndexError, KeyError):
        lo, hi = default
    lo, hi = max(0.0, lo), max(0.0, hi)
    return (lo, hi) if lo <= hi else (hi, lo)


@dataclass
class FetchReport:
    """What one source did in one run. ``summary()`` is the one human line shown in the CLI and the setup page."""

    source: str
    seen: int = 0
    added: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        bits = [f"{self.seen} found", f"{self.added} new"]
        if self.updated:
            bits.append(f"{self.updated} updated")
        if self.skipped:
            bits.append(f"{self.skipped} skipped")
        line = f"{self.source}: " + ", ".join(bits)
        if self.errors:
            line += f" - {len(self.errors)} error{'s' if len(self.errors) != 1 else ''} ({self.errors[0]})"
        elif self.notes:
            line += f" - {self.notes[0]}"
        return line


@dataclass
class SourceContext:
    """Everything a source may use. ``base_url`` / ``headless`` / ``page`` exist so tests (and the runner, which keeps
    one logged-in browser open for a whole run) can steer the browser sources."""

    home: Home
    cfg: dict
    store: Store
    limits: Limits
    log: Callable[[str], None] = print
    limit: int = 0                       # soft cap on items (companies / pages / results) this run; 0 = source default
    refresh: bool = False
    base_url: str = "https://www.linkedin.com"
    headless: bool | None = None
    page: object | None = None           # an already-open Playwright page to reuse (never launched/closed if given)
    stop: Callable[[], bool] = lambda: False   # cooperative cancel; sources check it between items


class Sink:
    """Where a source puts the people it found. Counts everything into the report."""

    def __init__(self, store: Store, source_type: str, source_id: str, report: FetchReport,
                 log: Callable[[str], None] = print):
        self.store = store
        self.source_type = source_type
        self.source_id = source_id
        self.report = report
        self.log = log

    def add(self, rows: Iterable[dict]) -> int:
        """Store rows (built with ``make_person``). Rows without a valid LinkedIn URL are counted as skipped.

        Returns how many rows were accepted (valid and de-duplicated within this call).
        """
        good: list[dict] = []
        seen_keys: set[str] = set()
        for row in rows:
            key = person_key(row) if row else ""
            if not key:
                self.report.skipped += 1
                continue
            if key in seen_keys:      # the same person twice in one batch (e.g. two anchors, two founder cards)
                continue
            seen_keys.add(key)
            good.append(row)
        if not good:
            return 0
        added, updated = self.store.upsert_people(good, self.source_type, self.source_id)
        self.report.seen += len(good)
        self.report.added += added
        self.report.updated += updated
        return len(good)


class Source:
    """Base class. Subclasses set ``type`` and implement ``fetch``; ``accepts`` is optional."""

    type: ClassVar[str] = ""
    needs_browser: ClassVar[bool] = False

    def __init__(self, spec: dict, ctx: SourceContext):
        self.spec = spec
        self.ctx = ctx

    @property
    def source_id(self) -> str:
        """Stable identifier written to ``people.source_id`` ("yc", "csv:<basename>", "webpage:<host>", ...)."""
        return self.type

    def owns(self, row: dict) -> bool:
        """Could this stored person have come from *this* configured source (rather than a sibling of the same type)?

        Lets "remove one CSV / web page / search from the config" mean its people are no longer contacted, even while
        another source of the same type stays enabled. Rows that carry no such marker are given the benefit of the doubt.
        """
        return True

    def accepts(self, row: dict) -> bool:
        """Who may be *sent to*. Pure and cheap: it runs once per stored person per run. Default: everyone it owns."""
        return self.owns(row)

    def fetch(self, sink: Sink) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def describe(self) -> str:
        return self.type

    # ------------------------------------------------------------------ helpers shared by subclasses
    @property
    def label(self) -> str:
        return self.source_id

    def stopped(self) -> bool:
        try:
            return bool(self.ctx.stop())
        except Exception:
            return False

    def log(self, message: str) -> None:
        try:
            self.ctx.log(message)
        except Exception:
            pass
