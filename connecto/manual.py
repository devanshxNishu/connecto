"""Adding people by hand: paste profile links (optionally with a name, company and title) and Connecto stores them.

Accepted line shapes (one person per line; blank lines and ``#`` comments are ignored):

    https://www.linkedin.com/in/jane-doe-1a2b3c/
    Jane Doe, https://www.linkedin.com/in/jane-doe-1a2b3c/
    Jane Doe, Acme Pay, CEO, https://www.linkedin.com/in/jane-doe-1a2b3c/
    https://www.linkedin.com/in/jane-doe-1a2b3c/ | Jane Doe | Acme Pay | CEO

A link on its own is accepted, but Connecto never guesses a first name from a URL (vanity links such as ``/in/jdoe`` would
produce embarrassing greetings), so such people receive a plain invitation without a note.
People added this way are always wanted - whatever the sources and filters say - and are invited first.
"""
from __future__ import annotations

import re

from .models import make_person, normalize_linkedin, person_key
from .store import Store

MANUAL_PRIORITY = "1000000"        # above every automatic priority (YC batch recency is around 20000)
MAX_LINES = 2000
MAX_LIST_FILE_BYTES = 5 * 1024 * 1024        # a list of people is a few kilobytes; a spreadsheet goes in with --csv
# A match must begin where a word begins ((?<![\w-])): without that, a single long word made the search try every position in
# it and read on to the end of the word each time - minutes for 200 KB of text. (It also means "xlinkedin.com/in/..." is not
# taken for a LinkedIn address.)
_URL = re.compile(r"(?<![\w-])(?:https?://)?(?:[\w-]+\.)?linkedin\.com/(?:in|pub)/[^\s,;\"'<>)\]|]+", re.I)
_SPLIT = re.compile(r"[,;\t|]| [-–—] ")
_EDGE = " -–—:|<>()[]\"'`"


def _looks_like_a_name(text: str) -> bool:
    return 1 <= len(text) <= 80 and "@" not in text and not text.isdigit() and not re.search(r"https?://|linkedin\.com", text, re.I)


def parse_people_text(text: str) -> tuple[list[dict], list[str]]:
    """Turn pasted lines into person rows. Returns (people, problems); a bad line never stops the others."""
    people: list[dict] = []
    problems: list[str] = []
    seen: set[str] = set()
    lines = str(text or "").splitlines()
    if len(lines) > MAX_LINES:
        problems.append(f"Only the first {MAX_LINES} lines were read.")
        lines = lines[:MAX_LINES]
    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        found = _URL.search(line)
        if not found:
            problems.append(f"Line {number}: no LinkedIn profile link found ({line[:50]!r}).")
            continue
        url = normalize_linkedin(found.group(0))
        if not url:
            problems.append(f"Line {number}: that is not a link to a personal LinkedIn profile ({found.group(0)[:60]!r}).")
            continue
        rest = line[:found.start()] + "," + line[found.end():]
        parts = [p.strip(_EDGE) for p in _SPLIT.split(rest)]
        parts = [p for p in parts if p and "@" not in p]          # e-mail addresses are never stored
        fields: dict[str, str] = {}
        if parts and _looks_like_a_name(parts[0]):
            fields["full_name"] = parts[0]
            if len(parts) > 1:
                fields["company"] = parts[1][:120]
            if len(parts) > 2:
                fields["title"] = parts[2][:120]
        row = make_person(linkedin_url=url, priority=MANUAL_PRIORITY, **fields)
        key = person_key(row)
        if not row or key in seen:
            continue
        seen.add(key)
        people.append(row)
    return people, problems


def add_people(store: Store, people: list[dict]) -> tuple[int, int]:
    """Store people as hand-added (always wanted, even if a source had already found them). Returns (added, updated)."""
    return store.upsert_people(people, "manual", "manual", take_over=True)


def skip_urls(store: Store, urls: list[str]) -> tuple[list[str], list[str]]:
    """Mark profiles as 'never contact'. Returns (skipped keys, rejected inputs)."""
    done, rejected = [], []
    for raw in urls:
        url = normalize_linkedin(raw)
        if not url:
            rejected.append(raw)
            continue
        row = {"linkedin_url": url}
        key = person_key(row)
        if store.status_of(key) in ("sent", "already_connected", "already_pending"):
            continue                                             # they are in your network already; nothing to protect
        known = store.get_person(key) or {}
        store.record({**known, "linkedin_url": url}, "skipped", "", "skipped by you")
        done.append(key)
    return done, rejected


def unskip_urls(store: Store, urls: list[str]) -> int:
    n = 0
    for raw in urls:
        url = normalize_linkedin(raw)
        if url and store.status_of(person_key({"linkedin_url": url})) == "skipped":
            n += int(store.forget_status(person_key({"linkedin_url": url})))
    return n
