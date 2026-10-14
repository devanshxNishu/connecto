"""The per-profile flow: open a profile, find the Connect control, add the note, send - and report exactly what happened.

Everything is matched by accessible role + visible name (see ``sel.SEL``): LinkedIn's class names are randomised.
This module never decides *whether* to send (that is the runner's job: quotas, hours, queue); it only does one profile.

Three rules keep a click from landing on the wrong thing:

* Clicks happen only inside the profile header whose heading is the name the page's own title gives (``top_card``). If that
  header cannot be found, nothing is clicked.
* A dialog is typed into or sent only if it is the invitation dialog (``SEL.INVITE_DIALOG_TEXT``).
* "Sent" needs evidence from LinkedIn - the header now says Pending, or LinkedIn's own message says so - and the person is
  recorded as ``sending`` *before* the point of no return, so a crash, Ctrl+C or slow page never leaves an invitation
  that was sent but is not counted.
"""
from __future__ import annotations

import logging
import random
import re
import unicodedata
from dataclasses import dataclass
from typing import Callable

from . import browser as _browser
from .browser import (DEFAULT_BASE_URL, LoggedOut, NeedsAttention, at_checkpoint, close_dialogs, human_scroll, visible,
                      visible_dialog)
from .models import PROFILE_HOST, key_from_page_url, linkedin_key, normalize_linkedin
from .sel import SEL, is_login_url

LOG = logging.getLogger("connecto")

# What the runner does with each outcome. Terminal ones are never retried (see store.TERMINAL).
# ``sending`` = Connect/Send was pressed but LinkedIn's confirmation was not seen (it counts toward the limits).
OUTCOMES = ("sent", "dry_run", "already_pending", "already_connected", "no_connect_option", "needs_email",
            "limit_reached", "note_unavailable", "not_found", "error", "sending")

CONFIRM_TIMEOUT_MS = 10_000        # how long the invitation dialog may take to close after Send (tests shorten it)


@dataclass
class Outcome:
    status: str                  # one of OUTCOMES
    detail: str = ""
    note: str = ""               # the note actually sent / that would be sent ("" = no note)
    note_blocked: bool = False   # LinkedIn would not let us add a note (free-plan quota): sent plain / not sent


# ============================================================================= whose page is this, where may we click
def _fold(text) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(text or ""))).strip().casefold()


def same_name(a: str, b: str) -> bool:
    """True when one name is the other with something after it: 'Jane Doe' / 'Jane Doe - Founder at Acme' /
    'Jane Doe (she/her)'. A prefix must end at a word boundary ('Al' is not 'Alan')."""
    a, b = _fold(a), _fold(b)
    if len(a) < 2 or len(b) < 2:
        return False
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if not long_.startswith(short):
        return False
    rest = long_[len(short):]
    return not rest or not rest[0].isalnum()


def page_owner(page) -> str:
    """The name this page declares for itself: its title is '(3) Jane Doe | LinkedIn' or 'Jane Doe - Founder | LinkedIn'."""
    try:
        title = page.title()
    except Exception:  # noqa: BLE001
        return ""
    title = re.sub(r"^\(\d+\+?\)\s*", "", str(title or "")).strip()
    return title.split("|")[0].strip()


@dataclass
class Card:
    """The profile header we may click in: the section whose heading carries the page owner's own name."""
    scope: object                # a Playwright locator
    name: str                    # the name as the heading shows it


def top_card(page) -> Card | None:
    """The header of the profile the page belongs to, or None - in which case nothing may be clicked.

    Found by name, not by position: the heading whose text is the page owner's name (the page's own title), and the
    ``<section>`` around it. A sidebar of "people you may know", with their own Connect buttons, is never in scope."""
    owner = page_owner(page)
    if not owner:
        return None
    try:
        headings = page.locator("main h1, main h2")
        total = headings.count()
    except Exception:  # noqa: BLE001
        return None
    for i in range(min(total, 15)):
        heading = headings.nth(i)
        try:
            if not heading.is_visible():
                continue
            text = heading.inner_text(timeout=2_000)
            if not same_name(text, owner):
                continue
            section = heading.locator("xpath=ancestor::section[1]")
            if section.count() != 1:
                return None            # a header not wrapped in a section: where it ends is a guess, so do not guess
            return Card(section.first, " ".join(text.split()))
        except Exception:  # noqa: BLE001
            continue
    return None


@dataclass
class Found:
    """Where Connect is - or why it is not."""
    trigger: object | None = None    # the locator to click
    how: str = ""
    status: str = ""                 # when there is no trigger: the outcome to record
    detail: str = ""


def _control_is_for(control, name: str) -> bool:
    """A control labelled 'Invite <X> to connect' must be about the person whose header we are in."""
    try:
        label = control.get_attribute("aria-label") or ""
    except Exception:  # noqa: BLE001
        return False
    m = re.match(r"^\s*Invite (.+?) to connect\s*$", label, re.I)
    return True if not m else same_name(m.group(1), name)


def find_connect_trigger(page, card: Card) -> Found:
    """The control that starts an invitation, searched for inside the verified header only. May leave the 'More' menu
    open (the caller closes it)."""
    scope = card.scope
    if visible(scope.get_by_role("button", name=SEL.PENDING)):
        return Found(status="already_pending", detail="the header shows Pending")

    for role, how in (("button", "top-card button"), ("link", "top-card link")):
        direct = scope.get_by_role(role, name=SEL.CONNECT)
        if visible(direct):
            if not _control_is_for(direct.first, card.name):
                return Found(status="error", detail="the Connect control in the header names someone else; nothing clicked")
            return Found(direct.first, how)

    more = scope.get_by_role("button", name=SEL.MORE)
    if not visible(more):
        return Found(status="no_connect_option", detail="the header has no Connect or More control")
    more.first.click()
    page.wait_for_timeout(_browser.pace(700, 1400))
    menus = page.get_by_role("menu")
    if not visible(menus, timeout=4_000):
        return Found(status="no_connect_option", detail="the More menu did not open")
    shown = []
    try:
        shown = [m for m in menus.all() if m.is_visible()]
    except Exception:  # noqa: BLE001
        pass
    if len(shown) != 1:
        return Found(status="error", detail=f"expected one open menu, found {len(shown)}; nothing clicked")
    menu = shown[0]
    item = menu.get_by_role("menuitem", name=SEL.CONNECT)
    if visible(item):
        if not _control_is_for(item.first, card.name):
            return Found(status="error", detail="the Connect item in the menu names someone else; nothing clicked")
        return Found(item.first, "More menu")
    if visible(menu.get_by_role("menuitem", name=SEL.REMOVE_CONNECTION)):
        return Found(status="already_connected", detail="the More menu offers 'Remove connection'")
    if visible(menu.get_by_role("menuitem", name=SEL.PENDING)):
        return Found(status="already_pending", detail="the More menu shows Pending")
    return Found(status="no_connect_option", detail="the More menu has no Connect item")


# ============================================================================= reading what LinkedIn says
def _text_of(locator, limit: int = 600) -> str:
    try:
        return str(locator.inner_text(timeout=1_000))[:limit]
    except Exception:  # noqa: BLE001
        return ""


def _page_messages(page) -> str:
    """What LinkedIn is telling the person right now, apart from the profile itself: alert and status messages (toasts)
    outside <main>, and any dialog that is open. The profile's own text is deliberately never read for this - a bio that
    says "too many invitations" must not change what happened."""
    parts: list[str] = []
    for locator in (page.get_by_role("alert"), page.get_by_role("status"), page.locator("[aria-live]")):
        try:
            elements = locator.all()
        except Exception:  # noqa: BLE001
            continue
        for element in elements[:10]:
            try:
                if element.is_visible() and not element.evaluate("e => !!e.closest('main')"):
                    parts.append(_text_of(element, 400))
            except Exception:  # noqa: BLE001
                continue
    dialog = visible_dialog(page)
    if dialog is not None:
        parts.append(_text_of(dialog))
    return " ".join(p for p in parts if p)


def _shows_pending(page) -> bool:
    """Does the (re-found) header now say Pending?"""
    card = top_card(page)
    if card is None:
        return False
    return visible(card.scope.get_by_role("button", name=SEL.PENDING)) or visible(card.scope.get_by_role("link", name=SEL.PENDING))


def _raise_if_linkedin_stopped_us(page) -> None:
    """After a Send: a sign-in page or a security check ends the run (the person stays 'sending': unknown)."""
    if is_login_url(page.url):
        raise LoggedOut("LinkedIn signed the session out right after an invitation was sent")
    if at_checkpoint(page):
        raise NeedsAttention("LinkedIn is asking for a security check - complete it in the browser window, then run again")


# ============================================================================= the dialog
def _click_plain_send(dlg, mark: Callable[[str], None]) -> bool:
    """Click 'Send without a note' (or a bare 'Send') if the dialog offers it. False when there is no such button."""
    plain = dlg.get_by_role("button", name=SEL.SEND_WITHOUT_NOTE)
    if not visible(plain):
        plain = dlg.get_by_role("button", name=SEL.SEND)
    if not visible(plain, timeout=3_000):
        return False
    mark("")
    plain.first.click()
    return True


def _note_not_available(note_blocked: bool, why: str) -> Outcome:
    return Outcome("note_unavailable", why, note_blocked=note_blocked)


def handle_dialog_and_send(page, note: str, max_note_chars: int, refit: Callable[[int], str] | None = None,
                           plain_ok: bool = True, before_send: Callable[[str], None] | None = None) -> Outcome:
    """After Connect was clicked: walk the invitation dialog and send. ``note`` may be empty (plain invitation).

    If LinkedIn will not let the account add a note (free-plan quota) the invitation is sent plain when ``plain_ok``;
    otherwise nothing is sent and the outcome is ``note_unavailable`` (the person stays in the queue).

    ``before_send(note)`` is called right before every click that can send, so the caller can record the person as
    ``sending`` first."""
    mark = before_send or (lambda text: None)
    dlg = visible_dialog(page, timeout_ms=8_000)
    if dlg is None:
        # Some accounts send at once, without a dialog: only the page can tell us.
        page.wait_for_timeout(_browser.pace(2_000, 2_000))
        _raise_if_linkedin_stopped_us(page)
        if _shows_pending(page) or SEL.SENT_TEXT.search(_page_messages(page)):
            return Outcome("sent", "sent instantly (no dialog)", "")
        return Outcome("error", "no invite dialog appeared after clicking Connect")

    text = _text_of(dlg, 4_000)
    if not SEL.INVITE_DIALOG_TEXT.search(text):
        close_dialogs(page)
        return Outcome("error", "a dialog that is not the invitation dialog opened after Connect; nothing was sent")
    if SEL.LIMIT_TEXT.search(text):
        close_dialogs(page)
        return Outcome("limit_reached", "LinkedIn showed its invitation-limit dialog")
    if SEL.EMAIL_TEXT.search(text) or dlg.locator('input[type="email"]').count() > 0:
        close_dialogs(page)
        return Outcome("needs_email", "LinkedIn requires this person's email to connect")
    if SEL.HOW_KNOW_TEXT.search(text):
        other = dlg.get_by_text(SEL.OTHER_OPTION)
        if visible(other):
            other.first.click()
            page.wait_for_timeout(_browser.pace(500, 500))
        nxt = dlg.get_by_role("button", name=SEL.CONNECT)
        if visible(nxt):
            nxt.first.click()
            page.wait_for_timeout(_browser.pace(1_200, 1_200))
            dlg = visible_dialog(page, timeout_ms=5_000)
            if dlg is None:
                return Outcome("error", "invite dialog vanished after the 'How do you know' step")
            if not SEL.INVITE_DIALOG_TEXT.search(_text_of(dlg, 4_000)):
                close_dialogs(page)
                return Outcome("error", "a dialog that is not the invitation dialog followed 'How do you know'; nothing was sent")
        else:
            close_dialogs(page)
            return Outcome("no_connect_option", "'How do you know' dialog without a way through")

    sent_note = ""
    note_blocked = False
    if note:
        add = dlg.get_by_role("button", name=SEL.ADD_NOTE)
        add_disabled = False
        try:
            add_disabled = visible(add) and (add.first.is_disabled() or add.first.get_attribute("aria-disabled") == "true")
        except Exception:  # noqa: BLE001
            add_disabled = False
        if visible(add) and not add_disabled:
            add.first.click()
            box = dlg.locator("textarea")
            try:
                box.first.wait_for(state="visible", timeout=5_000)
            except Exception:  # noqa: BLE001
                # No text box: LinkedIn is not letting this account add a note (free-plan quota -> upsell).
                after = visible_dialog(page)
                after_text = _text_of(after) if after is not None else ""
                if after is not None and SEL.LIMIT_TEXT.search(after_text):
                    close_dialogs(page)
                    return Outcome("limit_reached", "limit dialog appeared after 'Add a note'")
                if after is not None and plain_ok and _click_plain_send(after, mark):
                    return _confirm_sent(page, after, "", note_blocked=True,
                                         detail="sent without a note (LinkedIn would not let this account add one)")
                close_dialogs(page)
                if after is not None and not plain_ok:
                    return _note_not_available(True, "LinkedIn would not let this account add a note")
                return Outcome("error", "'Add a note' did not open a text box",
                               note_blocked=bool(SEL.NOTE_BLOCKED_TEXT.search(after_text)))
            limit = max_note_chars
            try:
                attr = box.first.get_attribute("maxlength")
                if attr and int(attr) > 0:
                    limit = min(limit, int(attr))
            except (TypeError, ValueError):
                pass
            text_to_type = note
            if len(text_to_type) > limit:
                text_to_type = refit(limit) if refit else note[: limit - 1].rsplit(" ", 1)[0] + "…"
            box.first.click()
            box.first.press_sequentially(text_to_type, delay=int(random.randint(12, 35) * _browser.PACE))
            page.wait_for_timeout(_browser.pace(800, 1600))
            try:
                typed = box.first.input_value()
            except Exception:  # noqa: BLE001
                typed = None
            if typed is None or " ".join(typed.split()) != " ".join(text_to_type.split()):
                close_dialogs(page)
                return Outcome("error", "the note box did not hold the note that was approved; nothing was sent")
            send = dlg.get_by_role("button", name=SEL.SEND)
            if not visible(send, timeout=3_000):
                close_dialogs(page)
                return Outcome("error", "typed the note but found no Send button")
            mark(text_to_type)
            send.first.click()
            sent_note = text_to_type
        else:
            # The note cannot be added: no 'Add a note' button (renamed? hidden by a quota?) or it is disabled.
            blocked_text = add_disabled or bool(SEL.NOTE_BLOCKED_TEXT.search(_text_of(dlg, 4_000)))
            note_blocked = blocked_text
            if not plain_ok:
                close_dialogs(page)
                return _note_not_available(blocked_text, "this invitation dialog offers no way to add a note"
                                                         if not blocked_text else "LinkedIn would not let this account add a note")
            LOG.info("    (no usable 'Add a note' option on this dialog - sending without a note)")

    if not sent_note:
        if not _click_plain_send(dlg, mark):
            close_dialogs(page)
            return Outcome("error", "invite dialog had no Send button I recognise", note_blocked=note_blocked)
    return _confirm_sent(page, dlg, sent_note, note_blocked=note_blocked, detail="note" if sent_note else "no note")


def _confirm_sent(page, dlg, sent_note: str, note_blocked: bool, detail: str) -> Outcome:
    """Send was pressed. "Sent" needs the dialog to go away, nothing to complain, and LinkedIn to show that the
    invitation exists (the header says Pending, or its own message says so); anything less is ``sending``: not
    confirmed, still counted."""
    try:
        dlg.wait_for(state="hidden", timeout=CONFIRM_TIMEOUT_MS)
    except Exception:  # noqa: BLE001
        _raise_if_linkedin_stopped_us(page)
        still = visible_dialog(page)
        after = _text_of(still) if still is not None else ""
        close_dialogs(page)                         # never presses Send: only dismisses
        if SEL.LIMIT_TEXT.search(after):
            return Outcome("limit_reached", "limit dialog appeared after Send", note_blocked=note_blocked)
        return Outcome("sending", "Send was pressed but the dialog stayed open; the invitation may or may not have gone out "
                                  "- look at 'Sent invitations' on LinkedIn", sent_note, note_blocked)
    page.wait_for_timeout(_browser.pace(1_200, 2_200))
    _raise_if_linkedin_stopped_us(page)
    said = _page_messages(page)
    if SEL.LIMIT_TEXT.search(said):
        return Outcome("limit_reached", "LinkedIn showed its limit message after Send", note_blocked=note_blocked)
    if _shows_pending(page) or SEL.SENT_TEXT.search(said):      # positive evidence beats a stray error message
        return Outcome("sent", detail, sent_note, note_blocked)
    if SEL.SEND_ERROR_TEXT.search(said):
        return Outcome("error", "LinkedIn showed an error after Send", note_blocked=note_blocked)
    return Outcome("sending", "the dialog closed but LinkedIn showed no confirmation (no Pending, no 'invitation sent' "
                              "message); the invitation may or may not have gone out - look at 'Sent invitations' on LinkedIn",
                   sent_note, note_blocked)


# ============================================================================= one profile
def process_profile(page, url: str, note: str, max_note_chars: int, dry_run: bool, base_url: str = DEFAULT_BASE_URL,
                    refit: Callable[[int], str] | None = None, plain_ok: bool = True,
                    may_land_on: Callable[[str], bool] | None = None,
                    before_send: Callable[[str], None] | None = None) -> Outcome:
    """Visit one profile and (unless dry_run) send the invitation. Raises LoggedOut / NeedsAttention to stop the run.

    The address is checked on the way in (only a plain ``/in/<name>/`` profile address is ever opened) and again on the
    way out: the page the browser ended up on must be a profile, and the same one - or one that ``may_land_on`` accepts
    (a person who changed their address; never somebody you chose to skip).

    ``before_send(note)`` is called right before each click that can send - Connect itself (with no note: on accounts that
    send at once nothing can be added), then Send (with the exact text typed) - and the runner records the person as
    ``sending`` there."""
    wanted_url = normalize_linkedin(url)
    if not wanted_url:
        return Outcome("error", "not a plain LinkedIn profile address; not opened")
    wanted = linkedin_key(wanted_url)
    url = wanted_url
    if base_url != DEFAULT_BASE_URL:  # test hook: point the same paths at a mock server
        url = re.sub(r"^https?://[^/]+", base_url, url)

    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(_browser.pace(2_000, 4_000))
    if is_login_url(page.url):
        raise LoggedOut("redirected to login while opening a profile")
    if at_checkpoint(page):
        raise NeedsAttention("LinkedIn is asking for a security check - complete it in the browser window, then run again")
    body_text = page.locator("body").inner_text()[:3000]
    if SEL.NOT_FOUND_TEXT.search(body_text) or "/404" in page.url:
        return Outcome("not_found", "profile page not found")
    landed = key_from_page_url(page.url)
    if not landed:
        return Outcome("error", "LinkedIn showed a page that is not a profile; nothing clicked")
    if landed != wanted and not (wanted.startswith(f"{PROFILE_HOST}/pub/") or (may_land_on and may_land_on(landed))):
        return Outcome("error", "the profile address leads to a different person than the one on the list; nothing clicked")
    try:
        page.locator("main").first.wait_for(state="visible", timeout=15_000)
    except Exception:  # noqa: BLE001
        return Outcome("error", "profile page did not render a <main>")

    close_dialogs(page)
    human_scroll(page)
    page.mouse.wheel(0, -5000)  # back to the top card
    page.wait_for_timeout(_browser.pace(500, 1200))

    card = top_card(page)
    if card is None:
        return Outcome("error", "could not find this profile's own header (a heading with the page owner's name, inside a "
                                "section); nothing clicked")
    found = find_connect_trigger(page, card)
    if found.trigger is None:
        page.keyboard.press("Escape")
        return Outcome(found.status, found.detail or "determined from the profile header")

    if dry_run:
        page.keyboard.press("Escape")
        return Outcome("dry_run", f"would click Connect via {found.how}", note)

    page.wait_for_timeout(_browser.pace(600, 1500))
    if before_send is not None:
        before_send("")                              # Connect may itself send (some accounts), and then with no note: record first
    found.trigger.click()
    return handle_dialog_and_send(page, note, max_note_chars, refit, plain_ok, before_send)
