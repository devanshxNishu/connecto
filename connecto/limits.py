"""Hard safety ceilings and the quota arithmetic.

Whatever a config file (or the web UI) asks for is clamped to ``HARD`` before it reaches the sender, and the arithmetic
below clamps once more at the point of use, so even a ``Limits`` built by hand cannot exceed a ceiling. The ceilings are
deliberately below what LinkedIn tolerates: its own invitation limit is roughly 100 per week, and accounts that sit
at it, or that behave in lock-step, are the ones that get restricted. There is no switch to raise them.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import datetime, time

from .util import real_number, whole_number

# --- absolute ceilings (code, not config) ---------------------------------------------------------------------------
HARD = {
    "daily_max": 30,            # invitations per calendar day
    "weekly_max": 100,          # invitations per rolling 7 days
    "per_run_max": 20,          # invitations per run
    "delay_min_seconds": 30,    # pause between two profiles
    "errors_stop_max": 5,       # consecutive unexplained errors before a run must stop
    "visits_per_run_max": 40,   # profiles one run may open, whether or not an invitation follows
    "idle_stop_max": 8,         # profiles opened in a row without sending anything: the run stops
    "search_pages_per_run": 10,
    "search_pages_per_day": 20,
    "yc_workers_max": 2,
}
MAX_DELAY_SECONDS = 3600        # a pause between two profiles longer than this is a typo, not a choice

# --- defaults a new user gets ----------------------------------------------------------------------------------------
DEFAULTS = {
    "daily": [8, 12],
    "weekly": 60,
    "per_run": [4, 7],
    "delay": [60, 180],
    "active_hours": ["09:00", "20:30"],
    "active_days": ["mon", "tue", "wed", "thu", "fri", "sat"],
    "stop_after_errors": 3,
    "start_jitter_minutes": 30,
}

DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _finite(value, default: float) -> float:
    """``value`` as a float; ``default`` when it is not a number at all or is NaN (an infinity is left for the caller's clamp)."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return default if number != number else number


def _ordered(pair, default: tuple[int, int]) -> tuple[int, int]:
    """A (low, high) pair of whole numbers in the right order, whatever a hand-built ``Limits`` was given."""
    try:
        lo, hi = int(pair[0]), int(pair[1])
    except (TypeError, ValueError, IndexError, OverflowError):
        lo, hi = default
    return (lo, hi) if lo <= hi else (hi, lo)


def visit_cap(allowed: int) -> int:
    """The most profiles one run may open when it may send ``allowed`` invitations: room for people who turn out to be
    connected already or gone, never enough to browse through a whole list."""
    return max(1, min(HARD["visits_per_run_max"], max(0, int(allowed)) * 3 + 3))


@dataclass(frozen=True)
class Limits:
    daily: tuple[int, int] = (8, 12)
    weekly: int = 60
    per_run: tuple[int, int] = (4, 7)
    delay: tuple[float, float] = (60.0, 180.0)
    active_hours: tuple[time, time] | None = (time(9, 0), time(20, 30))
    active_days: frozenset = frozenset(range(6))
    stop_after_errors: int = 3
    start_jitter_minutes: float = 30.0
    warnings: tuple[str, ...] = ()

    # ------------------------------------------------------------------ arithmetic
    def daily_target(self, day: datetime | None = None, salt: str = "connecto") -> int:
        """A target inside the daily range, the same all day and different tomorrow (deterministic per date)."""
        day = day or datetime.now()
        rng = random.Random(day.strftime("%Y-%m-%d") + "-" + salt)
        lo, hi = _ordered(self.daily, DEFAULTS["daily"])
        return max(0, min(rng.randint(lo, hi), HARD["daily_max"]))

    def per_run_target(self, rng: random.Random | None = None) -> int:
        lo, hi = _ordered(self.per_run, DEFAULTS["per_run"])
        return max(0, min((rng or random).randint(lo, hi), HARD["per_run_max"]))

    def pause_between(self, rng: random.Random | None = None) -> float:
        """Seconds to wait between two profiles: inside the configured range, never under the ceiling's minimum (and
        never over an hour, which is a typo and not a choice)."""
        floor, ceiling = float(HARD["delay_min_seconds"]), float(MAX_DELAY_SECONDS)
        try:
            first, second = self.delay[0], self.delay[1]
        except (TypeError, IndexError, KeyError):
            first = second = floor
        lo, hi = sorted((_finite(first, floor), _finite(second, floor)))
        lo = min(max(lo, floor), ceiling)
        hi = min(max(hi, lo), ceiling)
        return (rng or random).uniform(lo, hi)

    def is_active_day(self, now: datetime | None = None) -> bool:
        now = now or datetime.now()
        return now.weekday() in self.active_days

    def within_active_hours(self, now: datetime | None = None) -> bool:
        if not self.active_hours:
            return True
        now = now or datetime.now()
        return self.active_hours[0] <= now.time() <= self.active_hours[1]

    def can_send_now(self, now: datetime | None = None) -> tuple[bool, str]:
        now = now or datetime.now()
        if not self.is_active_day(now):
            return False, f"{DAY_NAMES[now.weekday()]} is not one of the active days"
        if not self.within_active_hours(now):
            a, b = self.active_hours
            return False, f"outside active hours ({a:%H:%M}-{b:%H:%M})"
        return True, ""

    def remaining(self, sent_today: int, sent_7d: int, now: datetime | None = None,
                  rng: random.Random | None = None, limit_override: int = 0) -> tuple[int, str]:
        """How many invitations this run may send, and why not if zero. ``limit_override`` (``--limit``) can only lower
        the number; a negative one means "none"."""
        weekly = min(self.weekly, HARD["weekly_max"])
        today_left = self.daily_target(now) - sent_today
        week_left = weekly - sent_7d
        allowed = max(0, min(today_left, week_left, self.per_run_target(rng)))
        reason = ""
        if today_left <= 0:
            reason = f"daily target reached ({self.daily_target(now)} today)"
        elif week_left <= 0:
            reason = f"weekly cap reached ({weekly} in the last 7 days)"
        if limit_override:
            allowed = min(allowed, max(0, min(int(limit_override), HARD["per_run_max"]))) if allowed else 0
        return allowed, reason


# ----------------------------------------------------------------------------- reading numbers from a settings file
def _pair(value, default, lo_bound: int, hi_bound: int, name: str, warnings: list[str]) -> tuple[int, int]:
    try:
        if isinstance(value, (list, tuple)):
            lo, hi = whole_number(value[0]), whole_number(value[1])
        else:
            lo = hi = whole_number(value)                    # one number = a fixed value
    except (TypeError, ValueError, IndexError, KeyError):
        warnings.append(f"{name}: expected [low, high]; using {default}")
        lo, hi = int(default[0]), int(default[1])
    if lo > hi:
        lo, hi = hi, lo
    clamped_lo, clamped_hi = max(lo_bound, min(lo, hi_bound)), max(lo_bound, min(hi, hi_bound))
    if (clamped_lo, clamped_hi) != (lo, hi):
        warnings.append(f"{name}: {[lo, hi]} limited to {[clamped_lo, clamped_hi]} (safety ceiling {hi_bound})")
    return clamped_lo, clamped_hi


def _parse_time(value, default: str, name: str, warnings: list[str]) -> time:
    try:
        if isinstance(value, bool):
            raise ValueError("not a time")
        if isinstance(value, int) and 0 <= value <= 23:
            return time(value, 0)                      # 9 -> 09:00
        h, m = str(value).strip().split(":")
        return time(int(h), int(m))
    except (ValueError, AttributeError, TypeError):
        warnings.append(f"{name}: bad time {value!r}; using {default}")
        h, m = default.split(":")
        return time(int(h), int(m))


def limits_from_config(cfg: dict) -> Limits:
    """Build clamped Limits from the ``limits:`` and ``schedule:`` blocks of a config. Never raises on bad values."""
    lim = cfg.get("limits") if isinstance(cfg, dict) else None
    sched = cfg.get("schedule") if isinstance(cfg, dict) else None
    lim = dict(lim) if isinstance(lim, dict) else {}
    sched = dict(sched) if isinstance(sched, dict) else {}
    w: list[str] = []

    daily = _pair(lim.get("daily", DEFAULTS["daily"]), DEFAULTS["daily"], 1, HARD["daily_max"], "limits.daily", w)

    try:
        weekly = whole_number(lim.get("weekly", DEFAULTS["weekly"]))
    except (TypeError, ValueError):
        w.append(f"limits.weekly: not a number; using {DEFAULTS['weekly']}")
        weekly = DEFAULTS["weekly"]
    if weekly > HARD["weekly_max"]:
        w.append(f"limits.weekly: {weekly} limited to {HARD['weekly_max']} (safety ceiling)")
        weekly = HARD["weekly_max"]
    weekly = max(1, weekly)

    per_run = _pair(lim.get("per_run", DEFAULTS["per_run"]), DEFAULTS["per_run"], 1, HARD["per_run_max"], "limits.per_run", w)

    raw_delay = lim.get("delay_seconds", DEFAULTS["delay"])
    try:
        dl, dh = real_number(raw_delay[0]), real_number(raw_delay[1])
    except (TypeError, ValueError, IndexError, KeyError):
        w.append(f"limits.delay_seconds: expected [low, high]; using {DEFAULTS['delay']}")
        dl, dh = map(float, DEFAULTS["delay"])
    if dl > dh:
        dl, dh = dh, dl
    if dl < HARD["delay_min_seconds"]:
        w.append(f"limits.delay_seconds: minimum raised to {HARD['delay_min_seconds']}s (safety ceiling)")
        dl = float(HARD["delay_min_seconds"])
        dh = max(dh, dl + 15)
    if dh > MAX_DELAY_SECONDS:
        w.append(f"limits.delay_seconds: maximum lowered to {MAX_DELAY_SECONDS}s")
        dl, dh = min(dl, float(MAX_DELAY_SECONDS)), float(MAX_DELAY_SECONDS)

    hours = sched.get("active_hours", DEFAULTS["active_hours"])
    active_hours: tuple[time, time] | None
    if not hours:
        active_hours = None
    else:
        try:
            a, b = hours
        except (TypeError, ValueError):
            a, b = DEFAULTS["active_hours"]
            w.append("schedule.active_hours: expected ['HH:MM', 'HH:MM']; using defaults")
        active_hours = (_parse_time(a, DEFAULTS["active_hours"][0], "schedule.active_hours", w),
                        _parse_time(b, DEFAULTS["active_hours"][1], "schedule.active_hours", w))
        if active_hours[0] >= active_hours[1]:
            w.append("schedule.active_hours: start must be before end; using defaults")
            active_hours = (_parse_time(DEFAULTS["active_hours"][0], "09:00", "", w),
                            _parse_time(DEFAULTS["active_hours"][1], "20:30", "", w))

    days_cfg = sched.get("active_days", DEFAULTS["active_days"])
    if isinstance(days_cfg, str):
        days_cfg = [d for d in re.split(r"[\s,;|]+", days_cfg) if d]
    elif not isinstance(days_cfg, (list, tuple, set, frozenset)):
        days_cfg = []
    days = {DAY_NAMES.index(str(d).strip().lower()[:3]) for d in days_cfg if str(d).strip().lower()[:3] in DAY_NAMES}
    if not days:
        w.append("schedule.active_days: none valid; using Mon-Sat")
        days = set(range(6))

    try:
        errs = whole_number(lim.get("stop_after_errors", DEFAULTS["stop_after_errors"]))
    except (TypeError, ValueError):
        errs = DEFAULTS["stop_after_errors"]
    if errs > HARD["errors_stop_max"]:
        w.append(f"limits.stop_after_errors: {errs} limited to {HARD['errors_stop_max']} (safety ceiling)")
    errs = max(1, min(errs, HARD["errors_stop_max"]))

    try:
        jitter = real_number(sched.get("start_jitter_minutes", DEFAULTS["start_jitter_minutes"]))
    except (TypeError, ValueError):
        jitter = float(DEFAULTS["start_jitter_minutes"])
    jitter = max(0.0, min(jitter, 120.0))

    return Limits(daily=daily, weekly=weekly, per_run=per_run, delay=(dl, dh), active_hours=active_hours,
                  active_days=frozenset(days), stop_after_errors=errs, start_jitter_minutes=jitter, warnings=tuple(w))
