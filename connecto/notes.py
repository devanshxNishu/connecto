"""Connection-note templating. Pure functions, unit-tested.

Template syntax
  {first_name}            a value from the person (or {me_first_name} from your `me:` block)
  {company|your company}  with a fallback used when the value is missing
  [[ at {company}]]       an optional segment: dropped when a value in it is missing, and dropped first (last segment
                          first) when the note is too long for LinkedIn's limit

A template whose *required* placeholders (outside [[...]], without a fallback) are all available is *eligible* for that
person; the others are skipped, so one list can mix templates for different kinds of people. Values are inserted in a
single pass, so text copied from someone's profile can never inject template syntax. (We deliberately do not use
``str.format``: it would allow attribute lookups like ``{first_name.__class__}`` in user-edited templates.)
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass

from . import privacy
from .util import name_case

VAR = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)(?:\|([^{}]*))?\}")
RESERVED = {"hook"}                      # filled by the AI helper, never taken from a person's profile data
INTERNAL = {"key", "source_type", "source_id", "added_at", "priority", "parser", "scraped_at", "founder_id"}


# ----------------------------------------------------------------------------- context
def build_context(person: dict, me: dict, extra: dict | None = None) -> dict:
    """One placeholder namespace: person fields (without e-mail addresses, phone numbers and links), your `me:` block as
    ``me_<key>``, plus ``extra`` (e.g. hook). A field of a person can never stand in for one of yours: ``me_*`` names that a
    spreadsheet column happens to have are ignored."""
    ctx: dict[str, str] = {}
    for k, v in person.items():
        if k in INTERNAL or k in RESERVED or k.startswith("me_") or not isinstance(v, (str, int, float)):
            continue
        ctx[k] = privacy.scrub_text(str(v).strip())
    for k in ("first_name", "full_name", "company", "title", "headline", "location"):
        ctx.setdefault(k, "")
    if not ctx["first_name"] and ctx["full_name"]:
        ctx["first_name"] = ctx["full_name"].split(" ")[0]
    ctx["first_name"] = name_case(ctx["first_name"])          # "Hi VISHNU," reads as shouting; "Hi Vishnu," does not
    for k, v in (me or {}).items():
        ctx[f"me_{k}"] = str(v or "").strip()
    for k, v in (extra or {}).items():
        ctx[k] = str(v or "").strip()
    return ctx


# ----------------------------------------------------------------------------- analysis
def optional_segments(template: str) -> list[tuple[int, int, str]]:
    """(start, end, inside) of each ``[[optional]]`` segment, left to right - a segment runs to the first ``]]``.

    A scan, not a regular expression: with ``[[`` written many times and never closed, the pattern ``\\[\\[(.*?)\\]\\]`` tried
    every one of them and read to the end each time.
    """
    out: list[tuple[int, int, str]] = []
    at = 0
    while True:
        start = template.find("[[", at)
        if start < 0:
            return out
        end = template.find("]]", start + 2)
        if end < 0:
            return out                    # nothing is closed from here on, so no later "[[" can be either
        out.append((start, end + 2, template[start + 2:end]))
        at = end + 2


def template_vars(template: str) -> set[str]:
    return {m.group(1) for m in VAR.finditer(template)}


def required_vars(template: str) -> set[str]:
    outside, at = [], 0
    for start, end, _ in optional_segments(template):
        outside.append(template[at:start])
        at = end
    outside.append(template[at:])
    return {m.group(1) for m in VAR.finditer("".join(outside)) if m.group(2) is None}


def uses_var(template: str, name: str) -> bool:
    return name in template_vars(template)


def eligible(template: str, ctx: dict) -> bool:
    return all((ctx.get(v) or "").strip() for v in required_vars(template))


def template_problems(template: str) -> list[str]:
    """Human-readable problems for the editor (unbalanced brackets/braces)."""
    out = []
    if template.count("[[") != template.count("]]"):
        out.append("unbalanced [[ ]] (optional segments must open and close)")
    stripped = VAR.sub("", template)
    if "{" in stripped or "}" in stripped:
        out.append("a { or } that is not a valid {placeholder} (use letters, digits and _)")
    if not template.strip():
        out.append("empty template")
    return out


# ----------------------------------------------------------------------------- rendering
def _subst(text: str, ctx: dict) -> str | None:
    """Replace placeholders; None if a required one has no value and no fallback."""
    missing = False

    def repl(m: re.Match) -> str:
        nonlocal missing
        val = (ctx.get(m.group(1)) or "").strip()
        if val:
            return val
        if m.group(2) is not None:
            return m.group(2)
        missing = True
        return ""

    out = VAR.sub(repl, text)
    return None if missing else out


def _tidy(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" ([,.!?;:])", r"\1", text)
    text = re.sub(r"([,;:])\s*([.!?])", r"\2", text)
    return text.strip()


def _render(template: str, ctx: dict, keep: list[bool]) -> str | None:
    """Render with optional segment i kept iff keep[i] and all its values are available."""
    pieces: list[str] = []
    pos = 0
    for i, (start, end, inside) in enumerate(optional_segments(template)):
        head = _subst(template[pos:start], ctx)
        if head is None:
            return None
        pieces.append(head)
        if keep[i]:
            seg = _subst(inside, ctx)
            if seg is not None:
                pieces.append(seg)
        pos = end
    tail = _subst(template[pos:], ctx)
    if tail is None:
        return None
    pieces.append(tail)
    return _tidy("".join(pieces))


def fit(template: str, ctx: dict, max_chars: int) -> str | None:
    """Render `template` so it is at most `max_chars` long, shortening gracefully; None if ineligible.

    Order: all optional segments in -> drop optional segments from the last backwards -> keep whole sentences ->
    cut at a word boundary with an ellipsis.
    """
    n_opt = len(optional_segments(template))
    keep = [True] * n_opt
    text = _render(template, ctx, keep)
    if text is None:
        return None
    for i in range(n_opt - 1, -1, -1):
        if len(text) <= max_chars:
            return text
        keep[i] = False
        text = _render(template, ctx, keep) or text
    if len(text) <= max_chars:
        return text

    sentences = re.split(r"(?<=[.!?])\s+", text)
    kept = ""
    for s in sentences:
        cand = (kept + " " + s).strip()
        if len(cand) > max_chars:
            break
        kept = cand
    if kept and len(kept) >= max_chars * 0.5:
        return kept

    cut = text[: max_chars - 1]
    cut = cut[: cut.rfind(" ")] if " " in cut else cut
    return cut.rstrip(" ,;:-") + "…"


def min_length(template: str, ctx: dict) -> int | None:
    """Length of the shortest possible rendering (every optional segment dropped); None if the template is ineligible."""
    text = _render(template, ctx, [False] * len(optional_segments(template)))
    return None if text is None else len(text)


def hook_budget(template: str, ctx: dict, max_chars: int, name: str = "hook") -> int:
    """How many characters a value for {name} may have in this template and still fit `max_chars`.
    -1 when the template is ineligible for other reasons."""
    n = min_length(template, dict(ctx, **{name: "§"}))
    return -1 if n is None else max_chars - (n - 1)


@dataclass
class Rendered:
    text: str
    index: int          # which template (index into the list given to compose)
    template: str
    ctx: dict

    def refit(self, max_chars: int) -> str:
        """Same template and values, shortened to a smaller limit (e.g. the textarea's real maxlength)."""
        return fit(self.template, self.ctx, max_chars) or self.text[: max_chars]


def compose(templates: list[str], ctx: dict, max_chars: int, rng: random.Random | None = None,
            prefer_vars: tuple[str, ...] = ("hook",)) -> Rendered | None:
    """Pick one eligible template at random and render it to fit. Templates that use a `prefer_vars` placeholder
    that has a value (the AI hook) are preferred over the rest. Returns None when nothing is eligible."""
    rng = rng or random
    candidates = [(i, t) for i, t in enumerate(templates) if eligible(t, ctx)]
    if not candidates:
        return None
    preferred = [(i, t) for i, t in candidates if any((ctx.get(v) or "").strip() and uses_var(t, v) for v in prefer_vars)]
    pool = preferred or candidates
    i, t = rng.choice(pool)
    text = fit(t, ctx, max_chars)
    if not text:
        return None
    return Rendered(text=text, index=i, template=t, ctx=ctx)


# ----------------------------------------------------------------------------- migration helper
_V1_RENAMES = {"project": "me_project", "applied_batch": "me_applied_batch"}
MAX_V1_TEMPLATE_CHARS = 2_000


def upgrade_v1_template(template: str) -> str:
    """yc-founder-connect v1 templates -> Connecto syntax ({project} -> {me_project}, blurb asides become [[ ]])."""
    t = template[:MAX_V1_TEMPLATE_CHARS]        # (these are short notes; the patterns below are quadratic on a long run of spaces)
    t = re.sub(r"\s*[-–—]\s*\{project_blurb\}\s*[-–—]\s*", "[[ - {me_project_blurb} -]] ", t)
    t = re.sub(r"\s*[-–—]\s*\{project_blurb\}", "[[ - {me_project_blurb}]]", t)
    t = re.sub(r"\s*\(\{project_blurb\}\)", "[[ ({me_project_blurb})]]", t)
    t = t.replace("{project_blurb}", "{me_project_blurb}")
    for old, new in _V1_RENAMES.items():
        t = t.replace("{" + old + "}", "{" + new + "}")
    return t
