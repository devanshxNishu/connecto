"""How Connecto recognises things on LinkedIn pages.

LinkedIn's class names and ids are randomised on every build, so nothing here is matched by CSS class. Everything is
matched by accessible role + visible name (what a screen reader would announce) or by dialog text. When LinkedIn changes
its UI, this file is the first place to look: reproduce with `connecto run --dry-run --limit 1`, inspect the accessibility
tree in DevTools, adjust the patterns, and mirror the new structure in tests/mock_linkedin.py.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit


class SEL:
    # --- buttons / menu items (matched against the accessible name)
    CONNECT = re.compile(r"^\s*(Connect|Invite .+ to connect)\s*$", re.I)
    PENDING = re.compile(r"^\s*(Pending|Withdraw( invitation)?)\b", re.I)
    MORE = re.compile(r"^\s*More( actions)?\s*$", re.I)
    MESSAGE = re.compile(r"^\s*Message\b", re.I)
    REMOVE_CONNECTION = re.compile(r"^\s*Remove connection\s*$", re.I)
    ADD_NOTE = re.compile(r"^\s*Add a (free )?note\s*$", re.I)
    SEND_WITHOUT_NOTE = re.compile(r"^\s*Send without a note\s*$", re.I)
    SEND = re.compile(r"^\s*(Send|Send invitation|Send now)\s*$", re.I)
    DISMISS = re.compile(r"^\s*(Dismiss|Close|Cancel|Not now|Got it)\s*$", re.I)
    OTHER_OPTION = re.compile(r"^\s*Other\s*$", re.I)

    # --- dialog / page text
    LIMIT_TEXT = re.compile(
        r"(weekly|monthly) invitation limit|reached the (weekly )?limit|you.ve reached the limit|too many invitations", re.I)
    NOTE_BLOCKED_TEXT = re.compile(
        r"personali[sz]ed (invitation|note|message)s?.{0,80}(limit|upgrade|premium)|(limit|upgrade|premium).{0,80}personali[sz]ed|"
        r"unlock (more )?(personali[sz]ed|notes)|reached .{0,40}(monthly|weekly) .{0,40}(note|personali[sz]ed)", re.I)
    EMAIL_TEXT = re.compile(r"enter (their|the|an) email|verify this member knows you|email address to connect", re.I)
    HOW_KNOW_TEXT = re.compile(r"how do you know", re.I)
    NOT_FOUND_TEXT = re.compile(
        r"this page doesn.t exist|page not found|profile (is )?not available|hmm, we can.t reach this profile", re.I)
    CHECKPOINT_TEXT = re.compile(
        r"security check|verify you.re (not a robot|a human)|unusual activity|we.ve (temporarily )?restricted|"
        r"account has been (temporarily )?restricted|confirm it.s you|let.s do a quick", re.I)
    SEND_ERROR_TEXT = re.compile(r"something went wrong|couldn.t send|try again later", re.I)
    # What the invitation dialog talks about. A dialog that mentions none of this is something else (a promo, a share
    # prompt) and nothing in it is ever typed into or sent.
    INVITE_DIALOG_TEXT = re.compile(r"invitation|\binvite\b|\bconnect(ion)?\b|how do you know|add a note|personali[sz]ed", re.I)
    # What LinkedIn says (in a message at the edge of the page, not in the profile) once an invitation has gone out.
    SENT_TEXT = re.compile(r"invitation (was )?sent|your invitation to .{1,100} was sent|invite sent|request sent|"
                           r"sent (an |your )?invitation", re.I)

    # --- URLs: matched against the path, from its start. (A person called "login-guide" lives at /in/login-guide/;
    # that is not the sign-in page.)
    LOGIN_PATH = re.compile(r"^/(login|authwall|uas/login|signup)(/|$)", re.I)
    CHECKPOINT_PATH = re.compile(r"^/(checkpoint|challenge)(/|$)", re.I)


def url_path(url: str) -> str:
    try:
        return urlsplit(str(url or "")).path
    except ValueError:
        return ""


def is_login_url(url: str) -> bool:
    return SEL.LOGIN_PATH.match(url_path(url)) is not None


def is_checkpoint_url(url: str) -> bool:
    return SEL.CHECKPOINT_PATH.match(url_path(url)) is not None
