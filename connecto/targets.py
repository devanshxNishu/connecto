"""Who to connect with: filters over person rows.

``targets:`` in the config applies to every person whatever their source; source-specific selection (YC batches,
a LinkedIn search query, ...) lives in the source's own block. Every filter is optional; an empty list means "no
restriction". Short terms (<= 3 characters, like ``ai`` or ``ml``) match whole words only, so ``ai`` finds
"AI-powered" but not "retail", and ``ALIASES`` maps shorthand people type to the wording sources actually use.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import regexsafe
from .models import META_FIELDS, searchable_text
from .util import as_list

SPLIT = "|"

# Shorthand -> the wording used in directories / headlines. Each term also matches itself.
ALIASES = {
    "ai": ["artificial intelligence", "generative ai", "ai"],
    "genai": ["generative ai"],
    "ml": ["machine learning"],
    "llm": ["llm", "large language model"],
    "devtools": ["developer tools"],
    "dev tools": ["developer tools"],
    "health": ["healthcare", "health"],
    "healthtech": ["healthcare", "health tech"],
    "biotech": ["biotech", "biotechnology", "bio"],
    "crypto": ["crypto", "web3", "blockchain"],
    "web3": ["web3", "crypto", "blockchain"],
    "climate": ["climate", "cleantech", "clean energy"],
    "ecommerce": ["e-commerce", "ecommerce"],
    "e-commerce": ["e-commerce", "ecommerce"],
    "edtech": ["education", "edtech"],
    "fintech": ["fintech", "financial"],
    "robotics": ["robotics", "robots"],
    "hardware": ["hardware", "hard tech"],
    "vc": ["venture capital", "vc"],
    "pm": ["product manager", "product management", "pm"],
    "ux": ["ux", "user experience"],
    "hr": ["human resources", "talent", "hr"],
}

LOCATION_FIELDS = ("location", "regions", "all_locations", "city", "state", "country", "region")


def _terms(value) -> list[str]:
    return [t.lower() for t in as_list(value)]


def _variant_in(variant: str, text: str) -> bool:
    if len(variant) <= 3:
        return re.search(r"(?<![a-z0-9])" + re.escape(variant) + r"(?![a-z0-9])", text) is not None
    return variant in text


def term_matches(term: str, text: str) -> bool:
    term = term.lower()
    return any(_variant_in(v, text) for v in {term, *ALIASES.get(term, [])})


def any_term_in(terms: list[str], text: str) -> bool:
    return any(term_matches(t, text) for t in terms)


def haystack(row: dict, *fields: str) -> str:
    return " | ".join((row.get(f) or "") for f in fields).lower()


def title_text(row: dict) -> str:
    return (row.get("title") or "").strip() or (row.get("headline") or "").strip()


@dataclass
class Targets:
    keywords: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    titles: list[str] = field(default_factory=list)
    exclude_titles: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    titles_regex: str = ""
    exclude_titles_regex: str = ""

    def __post_init__(self):
        for name in ("titles_regex", "exclude_titles_regex"):
            if getattr(self, name):
                try:
                    regexsafe.check(getattr(self, name))
                except ValueError as exc:
                    raise ValueError(f"{name}: {exc}") from None

    @classmethod
    def from_config(cls, cfg: dict) -> "Targets":
        t = (cfg or {}).get("targets") or {}
        return cls(
            keywords=_terms(t.get("keywords")), exclude_keywords=_terms(t.get("exclude_keywords")),
            titles=_terms(t.get("titles")), exclude_titles=_terms(t.get("exclude_titles")),
            locations=_terms(t.get("locations")),
            titles_regex=t.get("titles_regex") or "", exclude_titles_regex=t.get("exclude_titles_regex") or "",
        )

    def is_empty(self) -> bool:
        return not any((self.keywords, self.exclude_keywords, self.titles, self.exclude_titles, self.locations,
                        self.titles_regex, self.exclude_titles_regex))

    def matches(self, row: dict) -> bool:
        if self.keywords or self.exclude_keywords:
            text = searchable_text(row)
            if self.keywords and not any_term_in(self.keywords, text):
                return False
            if self.exclude_keywords and any_term_in(self.exclude_keywords, text):
                return False
        if self.locations and not any_term_in(self.locations, haystack(row, *LOCATION_FIELDS)):
            return False
        if self.titles or self.exclude_titles or self.titles_regex or self.exclude_titles_regex:
            title = title_text(row)
            tl = title.lower()
            if self.titles and not any_term_in(self.titles, tl):
                return False
            if self.exclude_titles and any_term_in(self.exclude_titles, tl):
                return False
            if self.titles_regex and not regexsafe.search(self.titles_regex, title):
                return False
            if self.exclude_titles_regex and regexsafe.search(self.exclude_titles_regex, title):
                return False
        return True

    def describe(self) -> str:
        parts = []
        for name in ("keywords", "exclude_keywords", "titles", "exclude_titles", "locations"):
            val = getattr(self, name)
            if val:
                parts.append(f"{name.replace('_', ' ')}: {', '.join(val)}")
        if self.titles_regex:
            parts.append(f"titles ~ /{self.titles_regex}/")
        if self.exclude_titles_regex:
            parts.append(f"titles !~ /{self.exclude_titles_regex}/")
        return "; ".join(parts) if parts else "everyone from your sources"


def count_by(rows: list[dict], field_name: str, top: int = 15) -> list[tuple[str, int]]:
    """Counts of a (possibly |-joined) field across rows, e.g. which industries the matched people are in."""
    counts: dict[str, int] = {}
    for r in rows:
        for v in (r.get(field_name) or "").split(SPLIT):
            v = v.strip()
            if v:
                counts[v] = counts.get(v, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:top]


__all__ = ["Targets", "term_matches", "any_term_in", "haystack", "count_by", "ALIASES", "META_FIELDS", "SPLIT"]
