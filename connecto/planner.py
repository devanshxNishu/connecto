"""Planning without a browser: who is next, what each note would say, what the queue looks like.

The sender (runner.py) and the previews (CLI and setup page) share these functions, so what you preview is what gets sent.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from . import config as cfgmod
from . import notes
from .models import person_key, priority_of
from .sources import SourceContext, build_source, make_filter
from .store import Store, waiting
from .targets import Targets, count_by

MIN_HOOK_CHARS = 45          # below this there is no room for a meaningful AI line
MAX_HOOK_CHARS = 140


# ----------------------------------------------------------------------------- who is next
def order_candidates(people: list[dict], order: str = "priority", rng: random.Random | None = None) -> list[dict]:
    """Shuffle, then (stably) sort by the chosen order, so equal-priority people come in random order."""
    rng = rng or random.Random()
    people = list(people)
    rng.shuffle(people)
    if order == "priority":
        people.sort(key=lambda p: -priority_of(p))
    elif order == "newest_added":
        people.sort(key=lambda p: p.get("added_at", ""), reverse=True)
    elif order == "oldest_added":
        people.sort(key=lambda p: p.get("added_at", ""))
    return people


def candidates(cfg: dict, store: Store, *, retry_errors: bool = False, rng: random.Random | None = None,
               include_handled: bool = False) -> list[dict]:
    """People who may be invited next: added by hand, or wanted by an enabled source and matching `targets`, and not
    already handled.

    Errors are not retried unless asked (they usually mean a LinkedIn UI change or a bad profile); `limit_reached` and
    `dry_run` outcomes are retried naturally."""
    wanted = make_filter(cfg)
    targets = Targets.from_config(cfg)
    statuses = store.statuses()
    out = []
    for person in store.iter_people():
        if not person_key(person):
            continue            # a stored address that is not a plain profile address (an older version kept some): never opened
        if not include_handled and not waiting(statuses.get(person["key"]), retry_errors):
            continue
        if not wanted(person):
            continue
        # People you added by hand are your own explicit choice: the filters (meant for sorting through big lists) never
        # drop them - a pasted profile with only a link has no title or company to match a keyword filter anyway.
        if person.get("source_type") != "manual" and not targets.matches(person):
            continue
        out.append(person)
    return order_candidates(out, cfg.get("order", "priority"), rng)


def queue_counts(cfg: dict, store: Store, *, retry_errors: bool = False) -> dict:
    """How many people match the sources and filters, and how many of those still wait for an invitation.

    Cheap (no notes are composed), so the setup page can show it on every refresh."""
    matching = candidates(cfg, store, retry_errors=retry_errors, rng=random.Random(0), include_handled=True)
    statuses = store.statuses()
    queued = sum(1 for person in matching if waiting(statuses.get(person["key"]), retry_errors))
    return {"matching": len(matching), "queued": queued}


def describe_sources(cfg: dict) -> list[str]:
    ctx = SourceContext(home=None, cfg=cfg, store=None, limits=None)       # type: ignore[arg-type]
    lines = []
    for spec in cfgmod.enabled_sources(cfg):
        try:
            lines.append(build_source(spec, ctx).describe())
        except Exception as exc:  # noqa: BLE001
            lines.append(f"{spec.get('type', '?')}: invalid ({exc})")
    return lines


# ----------------------------------------------------------------------------- what the note says
@dataclass
class Composed:
    text: str
    kind: str                       # "template" | "ai"
    rendered: notes.Rendered | None = None
    hook: str = ""

    def refit(self, max_chars: int) -> str:
        """Same note re-shortened to a smaller limit (e.g. the textarea's real maxlength)."""
        return self.rendered.refit(max_chars) if self.rendered else self.text[:max_chars]


class NoteComposer:
    """Turns a person into the note that would be sent: pick an eligible template, fill it, fit LinkedIn's limit.

    With an AI assistant, templates containing ``{hook}`` get a validated one-line opener written for that person, sized to
    the room the template leaves. If the helper is off, fails, or has nothing grounded to say, a template without a hook
    is used instead - the note is always complete.
    """

    def __init__(self, cfg: dict, assistant=None, rng: random.Random | None = None):
        self.cfg = cfg
        self.templates = cfgmod.templates(cfg)
        self.me = cfg.get("me") or {}
        self.limit = cfgmod.note_limit(cfg)
        self.assistant = assistant
        self.rng = rng or random.Random()          # only for a person without a profile address (never one from the queue)
        self.hook_templates = [t for t in self.templates if notes.uses_var(t, "hook")]

    def _chooser(self, person: dict) -> random.Random:
        """Which of the eligible templates a person gets is decided by who they are, not by chance: the preview then shows
        the note a run would send, and somebody tried again tomorrow is not written to differently."""
        key = person.get("key") or person_key(person)
        return random.Random(key) if key else self.rng

    @property
    def enabled(self) -> bool:
        return bool((self.cfg.get("message") or {}).get("send_note", True))

    def compose(self, person: dict, *, use_ai: bool = True, max_chars: int | None = None) -> Composed | None:
        if not self.enabled:
            return None
        limit = max_chars or self.limit
        ctx = notes.build_context(person, self.me)
        hook = ""
        if use_ai and self.assistant is not None and self.hook_templates:
            probe = dict(ctx, hook="x")
            budgets = [notes.hook_budget(t, ctx, limit) for t in self.hook_templates if notes.eligible(t, probe)]
            budget = max(budgets) if budgets else -1
            if budget >= MIN_HOOK_CHARS:
                hook = self.assistant.hook_for(person, max_chars=min(budget, MAX_HOOK_CHARS)) or ""
        ctx_with = dict(ctx, hook=hook) if hook else ctx
        rendered = notes.compose(self.templates, ctx_with, limit, self._chooser(person))
        if rendered is not None and hook and notes.uses_var(rendered.template, "hook") and hook not in rendered.text:
            # The hook did not survive shortening: do not send half a sentence; use a template that needs no hook.
            plain = [t for t in self.templates if not notes.uses_var(t, "hook")]
            rendered = notes.compose(plain, ctx, limit, self._chooser(person))
            hook = ""
        if rendered is None:
            return None
        used_hook = bool(hook) and notes.uses_var(rendered.template, "hook")
        return Composed(text=rendered.text, kind="ai" if used_hook else "template", rendered=rendered,
                        hook=hook if used_hook else "")


# ----------------------------------------------------------------------------- queue review
def review_queue(cfg: dict, store: Store, *, query: str = "", offset: int = 0, limit: int = 20) -> dict:
    """A searchable page of eligible people, with offline notes. Never starts AI or a browser.

    A fixed seed keeps pages stable while the stored people and settings are unchanged. A run still shuffles ties;
    reviewing a page neither reserves its people nor changes the sender's order or eligibility rules.
    """
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    queue = candidates(cfg, store, rng=random.Random(0))
    text = query.strip().casefold()
    fields = ("full_name", "company", "title", "headline", "location", "linkedin_url")
    matches = [p for p in queue if not text or text in " ".join(str(p.get(k, "")) for k in fields).casefold()]
    # A skip or another run may remove the last item on the current page. Show the last remaining page instead.
    if offset >= len(matches):
        offset = max(0, ((len(matches) - 1) // limit) * limit)
    composer = NoteComposer(cfg)
    people = []
    for person in matches[offset:offset + limit]:
        note = composer.compose(person, use_ai=False)
        people.append({
            "name": person.get("full_name", ""), "title": person.get("title") or person.get("headline", ""),
            "company": person.get("company", ""), "location": person.get("location", ""),
            "source": person.get("source_type", ""), "url": person.get("linkedin_url", ""),
            "note": note.text if note else "", "chars": len(note.text) if note else 0,
            "note_reason": "" if note else ("notes_off" if not composer.enabled else "no_template"),
        })
    return {"people": people, "total": len(matches), "queued": len(queue), "offset": offset, "limit": limit,
            "has_more": offset + limit < len(matches), "note_limit": composer.limit,
            "ai_enabled": bool((cfg.get("ai") or {}).get("enabled"))}


# ----------------------------------------------------------------------------- the preview
def _estimate_days(queued: int, daily_target: int, active_days: int) -> int:
    if queued <= 0:
        return 0
    per_week = max(1, daily_target * max(1, active_days))
    return max(1, round(queued / per_week * 7))


def template_warnings(cfg: dict) -> list[str]:
    out = []
    for t in cfgmod.templates(cfg):
        for problem in notes.template_problems(t):
            out.append(f"template '{t[:40]}...': {problem}")
    if (cfg.get("ai") or {}).get("enabled") and (cfg.get("message") or {}).get("templates") and \
            not any(notes.uses_var(t, "hook") for t in cfg["message"]["templates"]):
        out.append("The AI helper is on, but none of your templates contains {hook}, so it will not be used. "
                   "Add {hook} to one template (for example: \"Hi {first_name}, {hook} I'm {me_first_name}...\").")
    return out


def build_preview(cfg: dict, store: Store, *, sample: int = 12, example_notes: int = 3, assistant=None,
                  retry_errors: bool = False, rng: random.Random | None = None) -> dict:
    """Everything the 'who would be selected, and what would we say' screen needs. No browser, no sending."""
    rng = rng or random.Random(0)
    lim = cfgmod.limits(cfg)
    matching_all = candidates(cfg, store, retry_errors=retry_errors, rng=random.Random(0), include_handled=True)
    statuses = store.statuses()
    queue = [p for p in matching_all if waiting(statuses.get(p["key"]), retry_errors)]
    daily = lim.daily_target()
    warnings = list(lim.warnings) + template_warnings(cfg)
    sources = describe_sources(cfg)
    by_source = store.count_people_by_source()
    if not sources and not by_source.get("manual"):
        warnings.append("No source of people is switched on yet, and nobody was added by hand.")
    elif sources and store.count_people() == 0:
        warnings.append("Your sources have not been fetched yet - run `connecto fetch` (or press Fetch in the setup page).")
    elif not matching_all:
        warnings.append("Nobody matches your sources and filters. Loosen the filters or add another source.")

    breakdown = {}
    for field_name in ("batch", "industry", "tags", "regions", "location", "company"):
        top = count_by(queue, field_name, top=6)
        if top and len(top) > 0 and (field_name not in ("company", "location") or len(top) > 1):
            breakdown[field_name] = top

    sample_people = rng.sample(queue, min(sample, len(queue)))
    composer = NoteComposer(cfg, assistant=assistant, rng=rng)
    plain_only = 0
    if composer.enabled and queue:
        offline = NoteComposer(cfg, assistant=None, rng=random.Random(0))
        plain_only = sum(1 for person in queue if offline.compose(person, use_ai=False) is None)
        if plain_only and not (cfg.get("me") or {}).get("first_name"):
            warnings.append("Your own first name is not set yet (Message tab), so no note can be written - invitations would "
                            "go out plain.")
        elif plain_only:
            warnings.append(f"{plain_only} of the {len(queue)} waiting people have no name or detail that any of your templates "
                            "can use, so they would get a plain invitation without a note. (Add names when you add people, "
                            "or use a template that needs fewer details.)")
    examples = []
    for person in sample_people[:example_notes]:
        c = composer.compose(person)
        if c:
            examples.append({"person": person.get("full_name", ""), "kind": c.kind, "note": c.text, "chars": len(c.text)})
    return {
        "targets": Targets.from_config(cfg).describe(),
        "sources": sources,
        "people_total": store.count_people(),
        "people_by_source": by_source,
        "matching": len(matching_all),
        "handled": len(matching_all) - len(queue),
        "queued": len(queue),
        "daily_target": daily,
        "days_estimate": _estimate_days(len(queue), daily, len(lim.active_days)),
        "breakdown": breakdown,
        "sample": [{"name": p.get("full_name", ""), "title": p.get("title") or p.get("headline", ""),
                    "company": p.get("company", ""), "location": p.get("location", ""),
                    "source": p.get("source_type", ""), "url": p.get("linkedin_url", "")} for p in sample_people],
        "example_notes": examples,
        "note_limit": cfgmod.note_limit(cfg),
        "plain_only": plain_only,
        "warnings": warnings,
    }
