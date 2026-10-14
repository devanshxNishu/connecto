"""Browser plumbing: launch the automation browser, check the login, human-paced helpers, dialog helpers.

The automation browser is a *separate*, persistent profile (``<home>/browser/linkedin``): your everyday Chrome is
untouched and your LinkedIn session survives between runs. You log in by hand, once; Connecto never types credentials.

Connecto does not try to disguise itself as a human-driven browser (no stealth patches). It keeps the window visible,
the volume low and the pace slow, and it stops the moment LinkedIn asks for a verification.
Consequence: if you normally sign in with "Continue with Google", Google may refuse to sign in inside an automated
browser - use LinkedIn's email + password (or its one-time code) in this window instead.
"""
from __future__ import annotations

import logging
import random
import re
import time

from .paths import Home
from .sel import SEL, is_checkpoint_url, is_login_url

LOG = logging.getLogger("connecto")
DEFAULT_BASE_URL = "https://www.linkedin.com"

# Multiplier for every human-like wait inside a profile visit. 1.0 in real use; the test-suite sets it near zero.
PACE = 1.0


def pace(lo: int, hi: int) -> int:
    """A random wait in milliseconds between lo and hi, scaled by PACE."""
    return max(0, int(random.randint(lo, hi) * PACE))


class LoggedOut(Exception):
    """The session is not (or no longer) signed in."""


class NeedsAttention(Exception):
    """LinkedIn is asking the human for something (security check, CAPTCHA, restriction notice). Stop everything."""


class BrowserUnavailable(Exception):
    """No usable Chrome/Chromium could be launched."""


# ----------------------------------------------------------------------------- launch
def launch_browser(home: Home, cfg: dict, headless: bool | None = None, profile: str = "linkedin", **context_options):
    """Returns (playwright, context, page). The caller must close the context and stop playwright.
    ``context_options`` are handed on to Playwright (e.g. ``service_workers="block"``)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover
        raise BrowserUnavailable("Playwright is not installed: pip install playwright && python -m playwright install chromium") from exc

    b = cfg.get("browser") or {}
    profile_dir = home.browser_dir / profile
    profile_dir.mkdir(parents=True, exist_ok=True)
    pw = sync_playwright().start()
    kwargs = dict(
        user_data_dir=str(profile_dir),
        headless=bool(b.get("headless", False)) if headless is None else headless,
        no_viewport=True,
        locale="en-US",
        **context_options,
    )
    channel = b.get("channel", "chrome")
    if channel in ("chrome", "msedge"):
        kwargs["channel"] = channel
    try:
        ctx = pw.chromium.launch_persistent_context(**kwargs)
    except Exception as exc:
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        if "channel" in kwargs:  # that browser is not installed -> Playwright's own Chromium
            LOG.warning("Could not launch %s (%s); trying Playwright's Chromium.", channel, first)
            kwargs.pop("channel")
            try:
                ctx = pw.chromium.launch_persistent_context(**kwargs)
            except Exception as exc2:
                pw.stop()
                raise BrowserUnavailable(
                    "No browser could be started. Install Google Chrome, or run: python -m playwright install chromium "
                    f"({str(exc2).strip().splitlines()[0] if str(exc2).strip() else exc2})") from exc2
        else:
            pw.stop()
            raise BrowserUnavailable(f"Could not start Chromium: {first}. Try: python -m playwright install chromium") from exc
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.set_default_timeout(15_000)
    return pw, ctx, page


def close_browser(pw, ctx) -> None:
    for fn in (getattr(ctx, "close", None), getattr(pw, "stop", None)):
        try:
            if fn:
                fn()
        except Exception:
            pass


# ----------------------------------------------------------------------------- login / state checks
def looks_logged_out(page) -> bool:
    """Login/authwall URL, the marketing home page, or a sign-in form on the page."""
    if is_login_url(page.url):
        return True
    path = re.sub(r"^https?://[^/]+", "", page.url).split("?")[0]
    if path in ("", "/"):
        return True
    try:
        return page.locator('input[name="session_key"], input#session_key, form[action*="login"]').count() > 0
    except Exception:
        return False


def at_checkpoint(page) -> bool:
    """A verification / restriction page: the human must deal with it, not us."""
    if is_checkpoint_url(page.url):
        return True
    try:
        body = page.locator("body").inner_text(timeout=3_000)
    except Exception:
        return False
    # Verification pages are short; a long page that merely mentions "unusual activity" is just content.
    return len(body) < 1500 and SEL.CHECKPOINT_TEXT.search(body) is not None


def ensure_logged_in(page, base_url: str = DEFAULT_BASE_URL, wait_minutes: float = 10, stop=None) -> None:
    """Return once the browser is signed in; wait for a person to sign in by hand if it is not (never typing for them).
    ``stop`` is the Stop button: asked every few seconds while waiting, it ends the wait."""
    page.goto(f"{base_url}/feed/", wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(pace(2_500, 2_500))
    if is_checkpoint_url(page.url):
        raise NeedsAttention("LinkedIn is asking you to verify your identity - open the browser window and complete it")
    if not looks_logged_out(page):
        return
    LOG.info("Not logged in. Please sign in to LinkedIn in the browser window that just opened "
             "(Connecto never types credentials). Waiting up to %s minutes...", wait_minutes)
    deadline = time.time() + wait_minutes * 60
    while time.time() < deadline:
        if stop is not None and stop():
            raise LoggedOut("Sign-in was stopped.")
        page.wait_for_timeout(pace(3_000, 3_000))
        if "/feed" in page.url and not looks_logged_out(page):
            LOG.info("Logged in.")
            return
        if not looks_logged_out(page):  # landed somewhere else after login: confirm via the feed
            page.goto(f"{base_url}/feed/", wait_until="domcontentloaded")
            page.wait_for_timeout(pace(2_000, 2_000))
            if not looks_logged_out(page):
                LOG.info("Logged in.")
                return
    raise LoggedOut("Login not completed in time.")


# ----------------------------------------------------------------------------- pacing
def human_pause(lo: float, hi: float) -> None:
    time.sleep(random.uniform(lo, hi))


def human_scroll(page, steps: int | None = None) -> None:
    steps = steps if steps is not None else random.randint(1, 3)
    for _ in range(steps):
        page.mouse.wheel(0, random.randint(150, 600))
        page.wait_for_timeout(pace(600, 1800))
    if random.random() < 0.4:
        page.mouse.wheel(0, -random.randint(100, 400))
        page.wait_for_timeout(pace(400, 1200))


# ----------------------------------------------------------------------------- the window itself
_GONE = re.compile(r"has been closed|target closed|page crashed|browser closed|browser has disconnected", re.I)


def browser_closed(page, exc: BaseException | None = None) -> bool:
    """Has the window (or the whole browser) gone away - closed by a person, or crashed? Then nothing more can be tried,
    and an error that follows says nothing about the person being visited."""
    try:
        if page.is_closed():
            return True
    except Exception:  # noqa: BLE001
        return True
    if exc is None:
        return False
    return type(exc).__name__ == "TargetClosedError" or _GONE.search(str(exc)) is not None


# ----------------------------------------------------------------------------- locator helpers
def visible(locator, timeout: int = 0) -> bool:
    try:
        if timeout:
            locator.first.wait_for(state="visible", timeout=timeout)
            return True
        return locator.count() > 0 and locator.first.is_visible()
    except Exception:
        return False


def visible_dialog(page, timeout_ms: int = 0):
    """The currently visible modal, or None. Profile pages also carry hidden role=dialog nodes (video players), so the
    first dialog in DOM order is not necessarily the one on screen."""
    deadline = time.time() + timeout_ms / 1000
    while True:
        try:
            for dlg in page.get_by_role("dialog").all():
                if dlg.is_visible():
                    return dlg
        except Exception:
            pass
        if time.time() >= deadline:
            return None
        page.wait_for_timeout(250)


def close_dialogs(page) -> None:
    """Dismiss any stray modal (promo, 'follow' prompt, leftover invite dialog) without sending anything."""
    for _ in range(2):
        dlg = visible_dialog(page)
        if dlg is None:
            return
        btn = dlg.get_by_role("button", name=SEL.DISMISS)
        if visible(btn):
            btn.first.click()
        else:
            page.keyboard.press("Escape")
        page.wait_for_timeout(pace(500, 700))
