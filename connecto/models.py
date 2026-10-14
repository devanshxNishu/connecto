"""The one data shape in Connecto: a *person* is a flat dict of strings.

Required: ``linkedin_url`` (canonical ``https://www.linkedin.com/in/<slug>/``) and ``full_name``.
Well-known optional fields: ``first_name last_name title headline company location source_type source_id priority``.
Anything else a source provides (a CSV column, YC's ``batch``/``industry``/``tags``, ...) rides along as an extra
string field: it can be used in message templates as ``{field}`` and is searched by the keyword filters.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable
from urllib.parse import quote, unquote, urlsplit

from . import privacy
from .util import clean_text, norm_key, split_first_name

LINKEDIN_PERSONAL_PATHS = ("/in/", "/pub/")
PROFILE_HOST = "https://www.linkedin.com"
_SLUG_MAX = 200

# Fields that identify/describe the row itself, never searched by keyword filters or offered to the AI.
META_FIELDS = frozenset({
    "key", "linkedin_url", "source_type", "source_id", "added_at", "priority", "twitter_url", "website", "company_url",
    "parser", "scraped_at", "founder_id", "company_slug", "email", "phone",
})
STANDARD_FIELDS = ("full_name", "first_name", "last_name", "title", "headline", "company", "location")


def _clean_slug(raw: str) -> str:
    """One path segment of a profile address as plain text (percent-decoded, NFC) - or ``''``.

    A profile name is letters, marks, digits, ``-`` and ``_`` and nothing else. That is what LinkedIn hands out, and it
    is the only thing that is safe to treat as one fixed name: a browser turns ``\\`` into ``/`` and resolves ``.``,
    ``..`` and ``%2e%2e``, so ``/in/.\\jane/`` would open the same page as ``/in/jane/`` under a different key - and a
    person you chose never to contact would be visited again."""
    slug = unicodedata.normalize("NFC", unquote(raw.strip()))
    if not slug or len(slug) > _SLUG_MAX:
        return ""
    for ch in slug:
        if ch not in "-_" and unicodedata.category(ch)[0] not in "LMN":
            return ""
    return slug


def normalize_linkedin(url: str | None) -> str:
    """Canonical personal-profile URL (``https://www.linkedin.com/in/<slug>/``) or ``''``.

    Company/school pages, Sales Navigator links, query strings, country sub-domains (``in.linkedin.com``) and tracking
    fragments are stripped or rejected: only a personal profile survives. The name part must be plain (see
    ``_clean_slug``); anything with dots, backslashes, spaces or other symbols is not a profile address.
    """
    if not url:
        return ""
    url = str(url).strip()
    if url.startswith("//"):
        url = "https:" + url
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return ""
    path = re.sub(r"/+", "/", parts.path)
    if not path.startswith(LINKEDIN_PERSONAL_PATHS):
        return ""
    segments = [s for s in path.split("/") if s]
    if len(segments) < 2:
        return ""
    kind = segments[0]
    if kind == "pub":
        # Old-style /pub/first-last/1a/2b3/4c5 cannot be mapped to /in/ reliably; keep it as-is (each part checked).
        parts_ok = [_clean_slug(s) for s in segments[1:5]]
        if not all(parts_ok):
            return ""
        return f"{PROFILE_HOST}/pub/" + "/".join(quote(p, safe="-_") for p in parts_ok) + "/"
    slug = _clean_slug(segments[1])
    if not slug:
        return ""
    return f"{PROFILE_HOST}/in/{quote(slug, safe='-_')}/"


def linkedin_key(url: str | None) -> str:
    """Stable identity for de-duplication: the normalized URL, percent-decoded, NFC and lower-cased.

    Two addresses that open the same profile must have one key; ``normalize_linkedin`` has already refused every
    spelling a browser would resolve to something else (``.``, ``..``, ``\\``, encoded dots)."""
    canon = normalize_linkedin(url)
    return unicodedata.normalize("NFC", unquote(canon).lower()) if canon else ""


def key_from_page_url(page_url: str | None) -> str:
    """The person key of the page a browser is showing, by its path alone (so a stand-in host in tests works too).
    ``''`` when that page is not a personal profile."""
    try:
        path = urlsplit(str(page_url or "")).path
    except ValueError:
        return ""
    return linkedin_key(PROFILE_HOST + path)


def make_person(**fields) -> dict:
    """Build a clean person row. Returns ``{}`` when there is no valid LinkedIn profile URL.

    E-mail addresses, phone numbers and web addresses never get in (see ``privacy``): a field that says it holds one is
    dropped, and one that merely mentions one has it removed."""
    url = normalize_linkedin(fields.pop("linkedin_url", ""))
    if not url:
        return {}
    row: dict[str, str] = {"linkedin_url": url}
    for k, v in fields.items():
        if v is None:
            continue
        k = norm_key(k)
        if not k:
            continue
        row[k] = clean_text(v, 600 if k not in ("long_description", "bio", "summary") else 1200)
    row = privacy.scrub_person(row)                        # (drops the fields that are e-mail or phone, cleans the rest)
    full = row.get("full_name") or " ".join(x for x in (row.get("first_name"), row.get("last_name")) if x)
    row["full_name"] = clean_text(full)
    if not row.get("first_name"):
        row["first_name"] = split_first_name(row["full_name"])
    return row


def person_key(row: dict) -> str:
    return linkedin_key(row.get("linkedin_url"))


def label(row: dict) -> str:
    """'Jane Doe - Acme (S26)' style one-liner for logs."""
    parts = [row.get("full_name") or row.get("linkedin_url") or "?"]
    if row.get("company"):
        parts.append(row["company"])
    suffix = row.get("batch_code") or ""
    return " - ".join(parts) + (f" ({suffix})" if suffix else "")


def priority_of(row: dict) -> float:
    try:
        return float(row.get("priority") or 0)
    except (TypeError, ValueError):
        return 0.0


def searchable_text(row: dict, fields: Iterable[str] | None = None) -> str:
    """Lower-cased text of every field a keyword filter may look at (all non-meta string fields)."""
    if fields is None:
        fields = [k for k in row if k not in META_FIELDS]
    return " | ".join(str(row.get(f) or "") for f in fields).lower()
