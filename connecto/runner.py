"""The send run: limits -> queue -> profiles -> notes -> send, with every stop condition in one place.

A run stops at the first of: the Stop button, the run's quota is used, the active-hours window closes, a pause is set
(also from another window, while the run is going), too many profiles opened, several profiles in a row without anything
to send, LinkedIn shows its invitation-limit dialog, the session is logged out, LinkedIn asks for a security check,
N unexplained errors in a row, an invitation LinkedIn did not confirm, the browser window goes away, or the run lock is
removed from under the run. After a limit
dialog, a security check or repeated errors the next scheduled runs stay paused for a while so nothing keeps knocking
on a closed door.
"""
from __future__ import annotations

import dataclasses
import logging
import os
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from . import config as cfgmod
from . import planner
from .browser import (DEFAULT_BASE_URL, BrowserUnavailable, LoggedOut, NeedsAttention, browser_closed, close_browser,
                      close_dialogs, ensure_logged_in, human_scroll, launch_browser)
from .connect import Outcome, process_profile
from .limits import HARD, Limits, visit_cap
from .lock import LockHeld, LockUnavailable, RunLock
from .models import label
from .notify import notify
from .paths import Home
from .sources import fetch_sources
from .store import Store, waiting
from .util import load_env_into_process

LOG = logging.getLogger("connecto")

EXIT_OK, EXIT_NEEDS_YOU, EXIT_CONFIG, EXIT_BUSY = 0, 1, 2, 3
PAUSE_AFTER_LIMIT = timedelta(days=2)
PAUSE_AFTER_ATTENTION = timedelta(days=1)
PAUSE_AFTER_ERRORS = timedelta(days=1)
NOTE_BLOCK_RETRY = timedelta(days=7)
FIRST_FETCH_COMPANIES = 80       # a first run with an empty pool fetches this many items, enough to start
SCHEDULED_FETCH_ITEMS = 60       # each scheduled run tops the pool up by at most this many items
DRY_RUN_PROFILES = 5             # a dry run without --limit looks at this many people (at the real pace)
# Why a pause was set. Only a "check" pause (LinkedIn asked for a security check) is lifted by signing in again.
PAUSE_USER, PAUSE_LIMIT, PAUSE_CHECK, PAUSE_ERRORS = "user", "limit", "check", "errors"
LOCK_LOST = "the run lock was removed or replaced while this run was going on (a second run could be starting)"


@dataclass
class RunOptions:
    dry_run: bool = False
    limit: int = 0                      # send at most N this run (still capped by the daily/weekly limits)
    ignore_hours: bool = False
    ignore_pause: bool = False
    scheduled: bool = False             # started by the OS scheduler: jitter the start, notify, top up the pool
    retry_errors: bool = False
    fetch: bool | None = None           # None = automatic (empty pool, or scheduled + schedule.fetch_before_send)
    use_ai: bool = True                 # still needs ai.enabled in the config
    base_url: str = DEFAULT_BASE_URL    # test hook
    headless: bool | None = None        # test hook; None = browser.headless from the config
    limits: Limits | None = None        # programmatic override (tests); the CLI always goes through the config clamps
    sleep: Callable[[float], None] = time.sleep
    seed: int | None = None
    log: Callable[[str], None] | None = None
    stop: Callable[[], bool] = lambda: False     # cooperative cancel (the setup page's Stop button)


@dataclass
class RunResult:
    exit_code: int = EXIT_OK
    sent: int = 0
    attempted: int = 0
    stop_reason: str = ""
    dry_run: bool = False
    outcomes: dict = field(default_factory=dict)


def has_display() -> bool:
    """A visible browser needs a desktop session. macOS and Windows always have one; on Linux check the env."""
    if sys.platform.startswith("linux"):
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return True


def _pause_info(store: Store) -> tuple[datetime, str] | None:
    until = store.kv_get("paused_until")
    if not until:
        return None
    try:
        when = datetime.fromisoformat(until)
    except ValueError:
        return None
    if when <= datetime.now():
        return None
    return when, store.kv_get("paused_reason", "")


def pause_kind(store: Store) -> str:
    """Why the current pause was set (``user`` when it is not known - the cautious reading)."""
    return store.kv_get("paused_kind", "") or PAUSE_USER


def set_pause(store: Store, delta: timedelta, reason: str, kind: str = PAUSE_USER) -> None:
    store.kv_set("paused_until", (datetime.now() + delta).isoformat(timespec="minutes"))
    store.kv_set("paused_reason", reason)
    store.kv_set("paused_kind", kind)


def clear_pause(store: Store) -> None:
    store.kv_del("paused_until")
    store.kv_del("paused_reason")
    store.kv_del("paused_kind")


def lift_check_pause(store: Store) -> tuple[datetime, str] | None:
    """After a successful sign-in: lift the pause that exists only because LinkedIn asked for a security check (the
    person has just dealt with it). Any other pause - yours, a limit's, repeated errors' - stays. Returns what was lifted."""
    info = _pause_info(store)
    if info is not None and pause_kind(store) == PAUSE_CHECK:
        clear_pause(store)
        return info
    return None


# ============================================================================= send
def run_send(home: Home, cfg: dict, opts: RunOptions | None = None) -> RunResult:
    opts = opts or RunOptions()
    log = opts.log or LOG.info
    res = RunResult(dry_run=opts.dry_run)
    try:
        requested = int(opts.limit or 0)
    except (TypeError, ValueError, OverflowError):
        requested = -1
    if requested < 0:
        log("Not running: the limit must be a whole number of 1 or more (0 = no extra limit).")
        res.exit_code, res.stop_reason = EXIT_CONFIG, "bad limit"
        return res
    if requested > HARD["per_run_max"]:
        log(f"Setting adjusted: a limit of {requested} is lowered to {HARD['per_run_max']} (safety ceiling).")
    opts = dataclasses.replace(opts, limit=min(requested, HARD["per_run_max"]))
    load_env_into_process(home.env_path)

    problems = cfgmod.problems(cfg)
    if problems:
        for p in problems:
            log(f"Not ready: {p}")
        res.exit_code, res.stop_reason = EXIT_CONFIG, "not configured"
        return res

    lim = opts.limits or cfgmod.limits(cfg)
    for w in lim.warnings:
        log(f"Setting adjusted: {w}")

    if not opts.dry_run and not opts.ignore_hours:
        ok, why = lim.can_send_now()
        if not ok:
            log(f"Not sending now: {why}. (Use --ignore-hours to override.)")
            res.stop_reason = f"outside active time: {why}"
            return res

    if (opts.headless is None and not (cfg.get("browser") or {}).get("headless")) and not has_display():
        log("No desktop session found (DISPLAY is not set). Connecto needs a visible browser window, so it cannot run here.")
        res.exit_code, res.stop_reason = EXIT_NEEDS_YOU, "no display"
        return res

    lock = RunLock(home.lock_path)
    try:
        lock.acquire()
    except LockHeld as exc:
        log(f"{exc}. Nothing to do.")
        res.exit_code, res.stop_reason = EXIT_BUSY, "another run in progress"
        return res
    except LockUnavailable as exc:
        log(str(exc))
        res.exit_code, res.stop_reason = EXIT_CONFIG, "the data folder cannot be locked"
        return res
    try:
        user_stop = opts.stop                      # the Stop button, or the lock being taken from under us: both end the run
        opts = dataclasses.replace(opts, stop=lambda: bool(user_stop()) or lock.lost)
        with Store(home.db_path) as store:
            _run_locked(home, cfg, opts, lim, log, store, res, lock)
    finally:
        lock.release()
    return res


def _sleep_interruptibly(opts: RunOptions, seconds: float) -> bool:
    """Sleep in short slices so a Stop request is honoured. True if we were asked to stop."""
    remaining = float(seconds)
    while remaining > 0:
        if opts.stop():
            return True
        step = min(remaining, 5.0)
        opts.sleep(step)
        remaining -= step
    return bool(opts.stop())


def _run_locked(home: Home, cfg: dict, opts: RunOptions, lim: Limits, log, store: Store, res: RunResult, lock: RunLock) -> None:
    rng = random.Random(opts.seed)
    ncfg = cfg.get("notify", True)
    mode = "dry-run" if opts.dry_run else "send"

    def why_stopped() -> str:
        """The reason for a stop that somebody (or something) asked for."""
        if lock.lost:
            res.exit_code = EXIT_NEEDS_YOU
            return LOCK_LOST
        return "stopped"

    def paused_now() -> tuple[datetime, str] | None:
        """The pause in force this very moment (read afresh every time: it can be set from another window while a run
        is waiting), or None - also when the person said to override it."""
        return None if opts.ignore_pause else _pause_info(store)

    paused = paused_now()
    if paused:
        log(f"Paused until {paused[0]:%Y-%m-%d %H:%M}: {paused[1]}. (connecto run --ignore-pause overrides.)")
        res.stop_reason = f"paused: {paused[1]}"
        return

    if opts.scheduled and not opts.dry_run and lim.start_jitter_minutes > 0:
        minutes = rng.uniform(0, lim.start_jitter_minutes)
        if minutes >= 0.1:
            log(f"Scheduled run: starting in {minutes:.0f} minutes (a different time every day).")
            if _sleep_interruptibly(opts, minutes * 60):
                res.stop_reason = why_stopped()
                return
            if not opts.ignore_hours:
                ok, why = lim.can_send_now()
                if not ok:
                    log(f"Not sending now: {why}.")
                    res.stop_reason = f"outside active time: {why}"
                    return
            paused = paused_now()                      # a pause may have been set while we waited
            if paused:
                log(f"Paused until {paused[0]:%Y-%m-%d %H:%M}: {paused[1]}.")
                res.stop_reason = f"paused: {paused[1]}"
                return

    allowed, reason = lim.remaining(store.sent_today(), store.sent_last_7_days(), rng=rng, limit_override=opts.limit)
    if not opts.dry_run and allowed <= 0:
        log(f"Nothing to do: {reason or 'quota is zero'}. Sent today: {store.sent_today()}, last 7 days: {store.sent_last_7_days()}.")
        res.stop_reason = reason or "quota is zero"
        return
    if opts.dry_run:
        allowed = opts.limit or DRY_RUN_PROFILES
    max_visits = visit_cap(allowed)

    # --- top up the pool of people from the non-browser sources
    pool_empty = store.count_people() == 0
    want_fetch = opts.fetch if opts.fetch is not None else (
        pool_empty or (opts.scheduled and (cfg.get("schedule") or {}).get("fetch_before_send", True)))
    if want_fetch and cfgmod.enabled_sources(cfg):
        try:
            cap = FIRST_FETCH_COMPANIES if pool_empty else (SCHEDULED_FETCH_ITEMS if opts.scheduled else 0)
            reports = fetch_sources(home, cfg, store, skip_browser=True, limit=cap, log=log, stop=opts.stop)
            for r in reports:
                log("  " + r.summary())
        except Exception as exc:  # noqa: BLE001 - a failed top-up must not stop sending what we already have
            log(f"Fetching people failed ({type(exc).__name__}: {exc}); carrying on with the people already stored.")

    queue = planner.candidates(cfg, store, retry_errors=opts.retry_errors, rng=rng)
    log(f"{mode.upper()} | {len(queue)} people ready | this run's quota: {allowed} "
        f"(sent today {store.sent_today()}, last 7 days {store.sent_last_7_days()})")
    has_search = any(s["type"] == "linkedin_search" for s in cfgmod.enabled_sources(cfg))
    if not queue and not (has_search and want_fetch):
        log("Nobody to invite. Add a source or loosen your filters (`connecto preview` shows who is selected).")
        res.stop_reason = "empty queue"
        return

    assistant = None
    if opts.use_ai and (cfg.get("ai") or {}).get("enabled"):
        from .ai import Assistant
        assistant = Assistant.from_config(cfg, store)
    composer = planner.NoteComposer(cfg, assistant=assistant, rng=rng)
    note_limit = cfgmod.note_limit(cfg)
    policy = (cfg.get("message") or {}).get("if_note_unavailable", "plain")

    blocked_until = store.kv_get("note_blocked_until")
    notes_allowed = composer.enabled
    if notes_allowed and blocked_until and datetime.fromisoformat(blocked_until) > datetime.now():
        if policy == "stop":
            log(f"LinkedIn is not allowing personal notes on this account until about {blocked_until[:10]}; "
                "pausing (message.if_note_unavailable is 'stop').")
            res.stop_reason = "personal notes unavailable"
            return
        notes_allowed = False
        log(f"Personal notes are limited on this account until about {blocked_until[:10]}: sending plain invitations.")

    try:
        pw, ctx, page = launch_browser(home, cfg, headless=opts.headless)
    except BrowserUnavailable as exc:
        log(f"Cannot start the browser: {exc}")
        res.exit_code, res.stop_reason = EXIT_NEEDS_YOU, "no browser"
        return

    run_id = store.start_run(mode)
    sent = attempted = consecutive_errors = idle = 0
    stop_reason = "finished queue"
    try:
        try:
            ensure_logged_in(page, opts.base_url, wait_minutes=0.2 if opts.scheduled else 10, stop=opts.stop)
            if not opts.dry_run or store.kv_get("login_ok_at") is None:
                store.kv_set("login_ok_at", datetime.now().isoformat(timespec="minutes"))
        except LoggedOut as exc:
            stop_reason, res.exit_code, queue = f"not logged in: {exc}", EXIT_NEEDS_YOU, []
            log(f"Stopping: {stop_reason}. Run `connecto login` and sign in.")
        except NeedsAttention as exc:
            stop_reason, res.exit_code, queue = f"needs you: {exc}", EXIT_NEEDS_YOU, []
            log(f"Stopping: {stop_reason}")
            set_pause(store, PAUSE_AFTER_ATTENTION, str(exc), PAUSE_CHECK)

        if want_fetch and has_search and res.exit_code == EXIT_OK:
            try:
                for r in fetch_sources(home, cfg, store, only="linkedin_search", page=page, base_url=opts.base_url,
                                       log=log, stop=opts.stop):
                    log("  " + r.summary())
                queue = planner.candidates(cfg, store, retry_errors=opts.retry_errors, rng=rng)
                log(f"{len(queue)} people ready after the LinkedIn search.")
            except LoggedOut as exc:
                stop_reason, res.exit_code, queue = f"needs you: {exc}", EXIT_NEEDS_YOU, []
                log(f"Stopping: {stop_reason}")
            except NeedsAttention as exc:
                stop_reason, res.exit_code, queue = f"needs you: {exc}", EXIT_NEEDS_YOU, []
                log(f"Stopping: {stop_reason}")
                set_pause(store, PAUSE_AFTER_ATTENTION, str(exc), PAUSE_CHECK)
            except Exception as exc:  # noqa: BLE001
                log(f"LinkedIn search failed ({type(exc).__name__}: {exc}); carrying on with the people already stored.")

        def should_stop() -> str | None:
            """Why the run must not open another profile right now, if it must. Asked before every profile and again
            after the wait that precedes it: a minute or two is long enough for the Stop button, the clock, or a pause
            set from another window to change the answer."""
            if opts.stop():
                return why_stopped()
            if sent >= allowed:
                return "quota for this run reached"
            if not opts.dry_run and not opts.ignore_hours and not lim.can_send_now()[0]:
                return "active-hours window closed"
            pause = paused_now()
            if pause:
                log(f"Stopping: paused until {pause[0]:%Y-%m-%d %H:%M}: {pause[1]}.")
                return f"paused: {pause[1]}"
            if attempted >= max_visits:
                return f"{attempted} profiles opened this run (the most a run of this size should open)"
            return None

        for person in queue:
            why = should_stop()
            if why:
                stop_reason = why
                break
            if attempted:
                pause = lim.pause_between(rng)
                log(f"    waiting {pause:.0f}s ...")
                if _sleep_interruptibly(opts, pause):
                    stop_reason = why_stopped()
                    break
                if not opts.dry_run and attempted % rng.randint(4, 6) == 0:
                    try:
                        page.goto(f"{opts.base_url}/feed/", wait_until="domcontentloaded")
                        human_scroll(page, steps=rng.randint(2, 5))
                    except Exception as exc:  # noqa: BLE001 - a hiccup between profiles is not worth stopping for ...
                        if browser_closed(page, exc):     # ... unless the window is gone
                            stop_reason, res.exit_code = "the browser window was closed", EXIT_NEEDS_YOU
                            log(f"Stopping: {stop_reason}.")
                            break
                why = should_stop()
                if why:
                    stop_reason = why
                    break

            attempted += 1
            log(f"[{attempted}/{len(queue)}] {label(person)}")
            composed = composer.compose(person) if notes_allowed else None
            note_text = composed.text if composed else ""
            pressed = []        # filled by mark_sending: Connect or Send has been pressed for this person

            def mark_sending(text: str, _person=person, _composed=composed) -> None:
                """Called right before a click that can send: from here on the invitation may exist, so it is recorded
                (and counted) before anything can go wrong."""
                pressed.append(text)
                kind = (_composed.kind if _composed and text else "") or "plain"
                store.record(_person, "sending", text, "Connect/Send pressed; LinkedIn's confirmation not seen yet", kind)

            try:
                outcome = process_profile(page, person["linkedin_url"], note_text, note_limit, opts.dry_run,
                                          opts.base_url, refit=composed.refit if composed else None,
                                          plain_ok=(policy != "stop" or not note_text),
                                          may_land_on=lambda key: waiting_after_change(store, key, opts.retry_errors),
                                          before_send=None if opts.dry_run else mark_sending)
            except LoggedOut as exc:
                stop_reason, res.exit_code = f"logged out: {exc}", EXIT_NEEDS_YOU
                log(f"Stopping: {stop_reason}")
                break
            except NeedsAttention as exc:
                stop_reason, res.exit_code = f"needs you: {exc}", EXIT_NEEDS_YOU
                log(f"Stopping: {stop_reason}")
                set_pause(store, PAUSE_AFTER_ATTENTION, str(exc), PAUSE_CHECK)
                break
            except Exception as exc:  # noqa: BLE001 - Playwright timeouts etc.
                if browser_closed(page, exc):
                    # Nothing more can be tried, and whatever happened to this person is not recorded as an error:
                    # they were not (necessarily) tried. If Connect or Send had been pressed, they stay `sending`.
                    stop_reason, res.exit_code = "the browser window was closed", EXIT_NEEDS_YOU
                    log(f"Stopping: {stop_reason}.")
                    break
                first_line = (str(exc).splitlines() or [""])[0][:200]
                if pressed:
                    outcome = Outcome("sending", f"interrupted after Connect/Send was pressed ({type(exc).__name__}: {first_line}); "
                                                 "look at 'Sent invitations' on LinkedIn", pressed[-1])
                else:
                    outcome = Outcome("error", f"{type(exc).__name__}: {first_line}")
                try:
                    close_dialogs(page)
                except Exception:  # noqa: BLE001
                    pass

            kind = (composed.kind if composed and outcome.note else "") or ("plain" if outcome.status in ("sent", "sending") else "")
            store.record(person, outcome.status, outcome.note, outcome.detail, kind)
            res.outcomes[outcome.status] = res.outcomes.get(outcome.status, 0) + 1
            log(f"    -> {outcome.status}" + (f" ({outcome.detail})" if outcome.detail else ""))
            if outcome.note and outcome.status in ("sent", "sending", "dry_run"):
                log(f"    note ({len(outcome.note)} chars, {kind}): {outcome.note}")

            if outcome.status == "note_unavailable":              # only reached when the policy is 'stop'
                if outcome.note_blocked and not opts.dry_run:
                    store.kv_set("note_blocked_until", (datetime.now() + NOTE_BLOCK_RETRY).isoformat(timespec="minutes"))
                stop_reason = ("LinkedIn is not allowing personal notes right now (message.if_note_unavailable: stop)"
                               if outcome.note_blocked else
                               "the invitation dialog offers no way to add a note (message.if_note_unavailable: stop)")
                log(f"Stopping: {stop_reason}")
                break
            if outcome.note_blocked and not opts.dry_run:
                store.kv_set("note_blocked_until", (datetime.now() + NOTE_BLOCK_RETRY).isoformat(timespec="minutes"))
                notes_allowed = False
                log("    LinkedIn is not letting this account add personal notes right now (free-plan quota). "
                    "Sending plain invitations for a week; notes will be tried again after that.")

            if outcome.status in ("sent", "dry_run"):
                sent += 1
                consecutive_errors = idle = 0
            elif outcome.status == "limit_reached":
                stop_reason = "LinkedIn invitation limit reached - pausing for a couple of days"
                log(f"Stopping: {stop_reason}")
                if not opts.dry_run:
                    set_pause(store, PAUSE_AFTER_LIMIT, "LinkedIn said the invitation limit was reached", PAUSE_LIMIT)
                break
            elif outcome.status == "sending":
                stop_reason = "an invitation was sent but LinkedIn did not confirm it (see `connecto report`)"
                res.exit_code = EXIT_NEEDS_YOU
                log(f"Stopping: {stop_reason}. It is counted toward your limits. Look at 'Sent invitations' on LinkedIn; "
                    "`connecto run --retry-errors` tries that person again.")
                break
            else:
                idle += 1
                if outcome.status in ("error", "no_connect_option"):
                    consecutive_errors += 1
                    if consecutive_errors >= lim.stop_after_errors:
                        stop_reason = f"{lim.stop_after_errors} errors in a row (LinkedIn UI change? logged out?)"
                        res.exit_code = EXIT_NEEDS_YOU
                        log(f"Stopping: {stop_reason}")
                        if not opts.dry_run:
                            set_pause(store, PAUSE_AFTER_ERRORS, stop_reason, PAUSE_ERRORS)
                        break
                else:
                    consecutive_errors = 0
                if idle >= HARD["idle_stop_max"]:
                    stop_reason = f"{idle} profiles in a row without an invitation to send"
                    log(f"Stopping: {stop_reason} (already connected, unavailable, ...). The next run carries on with the rest.")
                    break
    except KeyboardInterrupt:
        stop_reason = "interrupted"
        res.exit_code = 130
    finally:
        store.finish_run(run_id, sent, attempted, stop_reason)
        close_browser(pw, ctx)

    res.sent, res.attempted, res.stop_reason = sent, attempted, stop_reason
    if assistant is not None:
        s = assistant.stats
        log(f"AI helper: {s['calls']} calls, {s['cache_hits']} cached, {s['rejected']} lines rejected, {s['failures']} failures.")
    log(f"Run finished: {stop_reason}. {'would send' if opts.dry_run else 'sent'} {sent} of {attempted} attempted. "
        f"Totals - today {store.sent_today()}, last 7 days {store.sent_last_7_days()}, all time {store.summary()['total_sent']}.")

    if opts.dry_run:
        return
    if stop_reason.startswith(("not logged in", "logged out")):
        notify("Connecto: sign in needed", "Your LinkedIn session expired - run: connecto login", ncfg)
    elif stop_reason.startswith("needs you"):
        notify("Connecto: needs you", "LinkedIn is asking for a check. Open the browser window, complete it, then run Connecto again.", ncfg)
    elif stop_reason.startswith("LinkedIn invitation limit"):
        notify("Connecto: paused", "LinkedIn's invitation limit was hit. Connecto pauses for two days; consider lowering your daily limit.", ncfg)
    elif "errors in a row" in stop_reason:
        notify("Connecto: needs a look", f"{lim.stop_after_errors} profiles in a row failed - see `connecto report` (LinkedIn UI change?).", ncfg)
    elif stop_reason.startswith("an invitation was sent but"):
        notify("Connecto: please check", "An invitation was sent but LinkedIn did not confirm it. Look at 'Sent invitations' on LinkedIn, "
                                         "then see `connecto report`.", ncfg)
    elif sent and opts.scheduled:
        notify("Connecto", f"Sent {sent} invitation{'s' if sent != 1 else ''} ({store.sent_today()} today, "
                          f"{store.sent_last_7_days()} this week).", ncfg)


def waiting_after_change(store: Store, key: str, retry_errors: bool = False) -> bool:
    """May a profile whose address changed lead to this person? Only to somebody who is still waiting to be invited -
    never one you chose to skip, nor one who was invited (or is thought to be), nor one this run is not retrying."""
    return waiting(store.status_of(key), retry_errors)


# ============================================================================= fetch
def run_fetch(home: Home, cfg: dict, *, only: str | None = None, limit: int = 0, refresh: bool = False,
              skip_browser: bool = False, log: Callable[[str], None] | None = None, base_url: str = DEFAULT_BASE_URL,
              headless: bool | None = None, stop: Callable[[], bool] = lambda: False) -> RunResult:
    """`connecto fetch`: run the enabled sources and add people to the store. Browser sources need the run lock."""
    log = log or LOG.info
    res = RunResult()
    load_env_into_process(home.env_path)
    if not cfgmod.enabled_sources(cfg):
        log("No sources are switched on yet. Add one (YC directory, a CSV, a web page, a LinkedIn search).")
        res.stop_reason = "no sources"
        return res
    uses_browser = (not skip_browser) and any(
        s["type"] == "linkedin_search" or (s["type"] == "webpage" and s.get("render")) or
        (s["type"] == "yc" and s.get("engine") == "playwright")
        for s in cfgmod.enabled_sources(cfg) if not only or s["type"] == only)
    fetch_lock = RunLock(home.root / "fetch.lock")
    run_lock = RunLock(home.lock_path)
    try:
        fetch_lock.acquire()
    except LockHeld as exc:
        log(f"{exc}.")
        res.exit_code, res.stop_reason = EXIT_BUSY, "another fetch in progress"
        return res
    except LockUnavailable as exc:
        log(str(exc))
        res.exit_code, res.stop_reason = EXIT_CONFIG, "the data folder cannot be locked"
        return res
    try:
        if uses_browser:
            try:
                run_lock.acquire()
            except LockHeld as exc:
                log(f"{exc}. A browser source needs the browser; try again when the run has finished.")
                res.exit_code, res.stop_reason = EXIT_BUSY, "another run in progress"
                return res
            except LockUnavailable as exc:
                log(str(exc))
                res.exit_code, res.stop_reason = EXIT_CONFIG, "the data folder cannot be locked"
                return res
        with Store(home.db_path) as store:
            try:
                reports = fetch_sources(home, cfg, store, only=only, limit=limit, refresh=refresh,
                                        skip_browser=skip_browser, log=log, base_url=base_url, headless=headless,
                                        stop=lambda: bool(stop()) or fetch_lock.lost or (uses_browser and run_lock.lost))
            except (LoggedOut, NeedsAttention) as exc:
                reports = getattr(exc, "reports", [])
                log(f"Stopped: {exc}")
                res.exit_code, res.stop_reason = EXIT_NEEDS_YOU, str(exc)
            for r in reports:
                log(r.summary())
            res.sent = sum(r.added for r in reports)       # "sent" here = people added
            log(f"{store.count_people()} people in your list.")
    finally:
        if uses_browser:
            run_lock.release()
        fetch_lock.release()
    return res


# ============================================================================= login / report
def login(home: Home, cfg: dict, base_url: str = DEFAULT_BASE_URL, log: Callable[[str], None] | None = None,
          headless: bool | None = None, wait_minutes: float = 15, stop: Callable[[], bool] | None = None) -> int:
    """Open the automation browser so you can sign in by hand. Connecto never types credentials. ``stop`` (the setup page's
    Stop button) ends the wait for you to sign in."""
    log = log or LOG.info
    lock = RunLock(home.lock_path)
    try:
        lock.acquire()
    except LockHeld as exc:
        log(f"{exc}.")
        return EXIT_BUSY
    except LockUnavailable as exc:
        log(str(exc))
        return EXIT_CONFIG
    try:
        try:
            pw, ctx, page = launch_browser(home, cfg, headless=headless)
        except BrowserUnavailable as exc:
            log(f"Cannot start the browser: {exc}")
            return EXIT_NEEDS_YOU
        try:
            ensure_logged_in(page, base_url, wait_minutes=wait_minutes, stop=stop)
            log("Signed in. The session is saved in Connecto's own browser profile; you can close the window.")
            with Store(home.db_path) as store:
                lifted = lift_check_pause(store)
                store.kv_set("login_ok_at", datetime.now().isoformat(timespec="minutes"))
                if lifted:
                    log(f"The pause that LinkedIn's security check caused ({lifted[1]}) is lifted - you have just dealt with it.")
                left = _pause_info(store)
                if left:
                    log(f"Sending is still paused until {left[0]:%Y-%m-%d %H:%M}: {left[1]}. `connecto resume` lifts it - "
                        "but if LinkedIn showed a limit or a check, give it time first.")
            return EXIT_OK
        except (LoggedOut, NeedsAttention) as exc:
            log(f"Not signed in: {exc}")
            return EXIT_NEEDS_YOU
        finally:
            close_browser(pw, ctx)
    finally:
        lock.release()


def status_report(home: Home, cfg: dict) -> dict:
    """Numbers for `connecto report` and the setup page."""
    lim = cfgmod.limits(cfg)
    with Store(home.db_path) as store:
        s = store.summary()
        paused = _pause_info(store)
        return {
            "sent_today": s["sent_today"], "daily_target": lim.daily_target(),
            "sent_last_7_days": s["sent_last_7_days"], "weekly_cap": lim.weekly,
            "total_sent": s["total_sent"], "by_status": s["by_status"], "people": s["people"],
            "people_by_source": store.count_people_by_source(),
            "paused_until": paused[0].isoformat(timespec="minutes") if paused else "",
            "paused_reason": paused[1] if paused else "",
            "paused_kind": pause_kind(store) if paused else "",
            "note_blocked_until": store.kv_get("note_blocked_until", ""),
            "login_ok_at": store.kv_get("login_ok_at", ""),
            "recent_runs": store.recent_runs(8),
            "recent_invites": store.recent_invites(25),
            "ai_calls_today": store.ai_calls_today(),
        }
