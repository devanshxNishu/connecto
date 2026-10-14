"""The Assistant: one validated opening sentence (the ``{hook}`` template variable) per person, plus audience ideas.

Contract for the runner: ``hook_for`` NEVER raises and returns ``str | None``; None simply means "use the template
without a hook". Nothing about the person except a handful of public profile facts ever leaves the machine: never the
name, the profile URL, an email or the location.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import time
from typing import TYPE_CHECKING

from .. import privacy
from ..models import person_key
from ..util import clean_text
from . import guard, prompts
from .client import AIClient, AIError

if TYPE_CHECKING:  # pragma: no cover
    from ..store import Store

LOG = logging.getLogger("connecto")

CACHE_KIND = f"hook:v{prompts.PROMPT_VERSION}"
FACT_FIELDS = ("title", "company", "headline", "one_liner", "industry")   # then "tags"; nothing else is ever sent
FACT_MAX_CHARS = 200
MAX_TAGS = 5
HOOK_MAX_TOKENS = 80
AUDIENCE_MAX_TOKENS = 500
BREAKER_LIMIT = 3          # consecutive auth/config failures before the helper stops for the rest of its life
PAUSE_AFTER = 3            # consecutive other failures before it rests for a while
PAUSE_SECONDS = 300.0
RATE_PAUSE_DEFAULT = 60.0  # how long to rest after a rate limit that did not say how long
PAUSE_CAP = 900.0
CACHE_DAYS = 90            # a saved line is forgotten after this long: people change jobs, and so do the rules it was judged by


class Assistant:
    client: AIClient
    stats: dict

    def __init__(self, client: AIClient, store: "Store", ai_cfg: dict, me: dict, *, clock=time.monotonic):
        self.client = client
        self.store = store
        self.cfg = dict(ai_cfg or {})
        self.me = dict(me or {})
        self.clock = clock
        self.stats = {"calls": 0, "cache_hits": 0, "rejected": 0, "failures": 0, "skipped_budget": 0,
                      "skipped_paused": 0}
        self._sender = {"about": clean_text(self.me.get("about"), 200), "project": clean_text(self.me.get("project"), 200)}
        self._logged: set[str] = set()
        self._hard_failures = 0        # consecutive auth/config failures
        self._soft_failures = 0        # consecutive network/server/rate-limit/bad-answer failures
        self._stopped = False          # circuit breaker: tripped for good
        self._paused_until = 0.0
        try:
            store.cache_prune(CACHE_DAYS)
        except Exception:  # noqa: BLE001 - housekeeping must never keep the helper from starting
            pass

    # ------------------------------------------------------------------ construction
    @classmethod
    def from_config(cls, cfg: dict, store: "Store", env=None) -> "Assistant | None":
        """An Assistant for a normalised config, or None (with one log line saying why) when AI is off or cannot start.
        Makes no network call."""
        try:
            ai = (cfg or {}).get("ai") or {}
            if not ai.get("enabled"):
                LOG.info("AI helper is off (ai.enabled is false): notes use your templates as written.")
                return None
            client = AIClient.from_config(ai, env=env)
            return cls(client, store, ai, cfg.get("me") or {})
        except AIError as exc:
            LOG.warning("AI helper is switched on but cannot start: %s Notes use your templates as written.", exc)
        except Exception as exc:        # a malformed config must not stop a send run either
            LOG.warning("AI helper could not start (%s: %s). Notes use your templates as written.",
                        type(exc).__name__, exc)
        return None

    def describe(self) -> str:
        preset = getattr(self.client, "preset", None)
        who = preset.label if preset is not None else self.client.base_url
        return f"{who}, model {self.client.model or 'chosen automatically'}"

    # ------------------------------------------------------------------ what may leave the machine
    def facts_for(self, person: dict) -> dict:
        """The only things the model is told about a person: title, company, headline, one_liner, industry, tags.

        Stored text was cleaned when it came in; it is checked again here, because this is where it leaves the machine: an
        e-mail address, phone number or link is removed, so is the person's own name (a headline that starts with it), and
        a field that reads like an instruction to an AI ("ignore all instructions ...") is dropped, not sent."""
        facts: dict[str, str] = {}
        for name in FACT_FIELDS:
            value = person.get(name)
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                continue
            text = self._fact_text(clean_text(value, FACT_MAX_CHARS), person, keep_name=(name == "company"))
            if text and not guard.looks_like_injection(text):
                facts[name] = text
        tags = person.get("tags")
        if isinstance(tags, str):
            tags = re.split(r"[|,;\n]", tags)
        if isinstance(tags, (list, tuple)):
            cleaned = [t for t in (clean_text(x, 60) for x in tags if isinstance(x, (str, int, float))) if t][:MAX_TAGS]
            joined = self._fact_text(clean_text(", ".join(cleaned), FACT_MAX_CHARS), person)
            if joined and not guard.looks_like_injection(joined):
                facts["tags"] = joined
        return facts

    @staticmethod
    def _fact_text(text: str, person: dict, keep_name: bool = False) -> str:
        """A fact without contact details and (unless it is a company's name) without the person's own name."""
        text = privacy.scrub_text(text)
        return text if keep_name else privacy.without_name(text, person)

    # ------------------------------------------------------------------ small helpers
    def _budget(self) -> int:
        limit = self.cfg.get("max_calls_per_day")
        try:
            return 60 if limit is None else max(0, int(limit))
        except (TypeError, ValueError, OverflowError):
            return 60

    def _temperature(self) -> float:
        try:
            value = float(self.cfg.get("temperature", 0.6))
        except (TypeError, ValueError, OverflowError):
            return 0.6
        return min(1.2, max(0.0, value)) if math.isfinite(value) else 0.6

    def _digest(self, facts: dict, bucket: int = 0) -> str:
        blob = (json.dumps(facts, sort_keys=True, ensure_ascii=False)
                + json.dumps(self._sender, sort_keys=True, ensure_ascii=False) + prompts.PROMPT_VERSION + f"/{bucket}")
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    def _scrub(self, text: str) -> str:
        redact = getattr(self.client, "_redact", None)
        return redact(text) if callable(redact) else text

    def _log_once(self, key: str, message: str) -> None:
        if key in self._logged:
            LOG.debug(message)
            return
        self._logged.add(key)
        LOG.warning(message)

    def _note_failure(self, exc: AIError) -> None:
        self.stats["failures"] += 1
        self._log_once(exc.kind, f"AI helper problem ({exc.kind}): {exc} - carrying on without an AI line.")
        if exc.kind in ("auth", "config"):
            self._hard_failures += 1
            if self._hard_failures >= BREAKER_LIMIT and not self._stopped:
                self._stopped = True
                LOG.warning("AI helper switched off for the rest of this run after %d failures in a row; fix the "
                            "setting above and run again.", self._hard_failures)
            return
        self._soft_failures += 1
        wait = None
        if exc.kind == "rate_limit":
            wait = min(PAUSE_CAP, exc.retry_after if exc.retry_after is not None else RATE_PAUSE_DEFAULT)
        elif self._soft_failures >= PAUSE_AFTER:
            wait = PAUSE_SECONDS
        if wait:
            self._paused_until = self.clock() + wait

    # ------------------------------------------------------------------ the hook
    def hook_for(self, person: dict, max_chars: int | None = None) -> str | None:
        """One safe opening sentence for this person, or None. Never raises.

        ``max_chars`` is how much room the note template leaves for it (a free LinkedIn note holds only 200
        characters): the model is asked for a shorter line and the answer is validated against that room."""
        try:
            return self._hook_for(person, max_chars)
        except Exception as exc:     # an AI problem must never break a send run
            self.stats["failures"] += 1
            self._log_once("unexpected", "AI helper hit an unexpected problem "
                                         f"({type(exc).__name__}: {self._scrub(str(exc))[:160]}); carrying on without AI lines.")
            return None

    def _hook_for(self, person: dict, max_chars: int | None = None) -> str | None:
        facts = self.facts_for(person)
        if not facts or not guard.groundable(facts):
            return None                                   # too thin for a grounded line: do not spend a call
        # Budget in 20-character steps, validated at the step's floor so a cached line fits every request in its step.
        bucket = 0 if max_chars is None else max(20, min(int(max_chars), 140) // 20 * 20)
        room = bucket or 140
        max_words = None if not bucket else max(5, min(22, bucket // 6))
        key = f"{person_key(person) or person.get('key') or ''}:{self._digest(facts, bucket)}"
        cached = self.store.cache_get(key, CACHE_KIND)
        if cached is not None:
            self.stats["cache_hits"] += 1
            if not cached:
                return None                               # "" = we already know there is no usable hook
            hook = guard.clean_hook(cached, facts, max_chars=room)      # a saved line is judged again, by today's rules
            if hook is None:
                self.stats["rejected"] += 1
            return hook
        if self._stopped or self.clock() < self._paused_until:
            self.stats["skipped_paused"] += 1
            return None
        if self.store.ai_calls_today() >= self._budget():
            self.stats["skipped_budget"] += 1
            return None
        self.store.ai_bump()
        self.stats["calls"] += 1
        try:
            raw = self.client.chat(prompts.hook_messages(self.me, facts, max_words), max_tokens=HOOK_MAX_TOKENS,
                                   temperature=self._temperature())
        except AIError as exc:
            self._note_failure(exc)                       # failures are not cached: try again next time
            return None
        except Exception as exc:  # noqa: BLE001 - a client that breaks in a way nobody foresaw counts like any other failure,
            # so the pause and the switch-off still work (otherwise every person would cost one more doomed request)
            self._note_failure(AIError("bad_response", f"unexpected problem in the AI client ({type(exc).__name__})"))
            return None
        self._hard_failures = self._soft_failures = 0     # the provider answered, whatever it said
        hook = guard.clean_hook(raw, facts, max_chars=room)
        if hook is None:
            self.stats["rejected"] += 1
        self.store.cache_set(key, CACHE_KIND, hook or "")
        return hook

    # ------------------------------------------------------------------ audience suggestions (interactive)
    def suggest_audience(self, description: str) -> dict:
        """Filter suggestions for a plain-English audience (see ``guard.parse_audience`` for the shape).
        Raises AIError on any failure, including an exhausted daily budget."""
        text = clean_text(description, prompts.AUDIENCE_MAX_CHARS)
        if not text:
            raise AIError("config", "Describe the people you want to meet in a sentence or two first.")
        if self.store.ai_calls_today() >= self._budget():
            raise AIError("budget", f"The daily AI limit ({self._budget()} calls) is used up; it resets tomorrow, or "
                                    "raise ai.max_calls_per_day.")
        self.store.ai_bump()
        self.stats["calls"] += 1
        raw = self.client.chat(prompts.audience_messages(text), max_tokens=AUDIENCE_MAX_TOKENS,
                               temperature=min(self._temperature(), 0.3), json_mode=True)
        try:
            result = guard.parse_audience(raw)
        except ValueError:
            raise AIError("bad_response", "The model did not return usable suggestions. Try again, or describe the "
                                          "people a little more simply.") from None
        if not result["yc"] and not any(result[k] for k in guard.LIST_LIMITS):
            raise AIError("bad_response", "The model had no suggestions for that description. Try adding a job title, "
                                          "an industry or a place.")
        return result
