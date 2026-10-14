"""Contact details never enter Connecto's data, an AI request or a note.

People are described by text that strangers wrote: a web page, a spreadsheet cell, a LinkedIn headline. Some of it holds
an e-mail address, a phone number or a link ("Jane Doe | jane@acme.com | +1 415 555 0100 | linkedin.com/in/jane"). Connecto
only needs a profile address, a name, a title and a company, so everything else of that kind is removed at the door
(``scrub_person``, called by ``models.make_person`` and again by the store), left out of what an AI provider is told
(``assist.facts_for``) and left out of a note (``notes.build_context``).

This is pattern matching, not magic: it catches the usual written forms, including "name [at] site [dot] com". It cannot
recognise a street address or a name that is spelled in some unusual way, and it does not try to.

Every pattern is linear in the length of the text (bounded repetitions, no nested quantifiers) - the text is somebody
else's.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

# ----------------------------------------------------------------------------- what counts as contact details
_LOCAL = r"[\w.+%'-]{1,64}"
_LABEL = r"[\w-]{1,63}"
_AT = r"[\[({<]\s?(?:at|@)\s?[\])}>]"
_DOT = r"[\[({<]\s?(?:dot|\.)\s?[\])}>]"

_EMAIL = re.compile(rf"{_LOCAL}@{_LABEL}(?:\.{_LABEL})+", re.I)
_EMAIL_BRACKETED = re.compile(rf"{_LOCAL}\s?{_AT}\s?{_LABEL}(?:\s?(?:{_DOT}|\.)\s?{_LABEL}){{1,5}}", re.I)
_EMAIL_SPELLED = re.compile(rf"\b{_LOCAL}\s+at\s+{_LABEL}(?:\s+dot\s+{_LABEL}){{1,5}}\b", re.I)

_SCHEME = re.compile(r"(?<![\w-])(?:https?://|ftp://|mailto:|tel:|sms:|skype:|whatsapp:|javascript:|data:)\S*", re.I)
_WWW = re.compile(r"(?<![\w-])www\.\S+", re.I)
_PROFILE_SITES = re.compile(
    r"(?<![\w.-])(?:[\w-]{1,63}\.){0,4}(?:linkedin\.com|lnkd\.in|twitter\.com|x\.com|facebook\.com|fb\.com|instagram\.com"
    r"|github\.com|t\.me|wa\.me|calendly\.com|bit\.ly|linktr\.ee|tiktok\.com|youtube\.com|youtu\.be)/\S*", re.I)

_PHONE_PLUS = re.compile(r"(?<![\w+])\+\d[\d\s().-]{6,18}\d")
_PHONE_AREA = re.compile(r"(?<![\w+])\(\d{2,4}\)[\s.-]?\d[\d\s.-]{4,12}\d")
_PHONE_GROUPS = re.compile(r"(?<![\w.+-])\d{3,4}[\s.-]\d{3,4}[\s.-]\d{3,4}(?:[\s.-]\d{2,4})?(?![\w-])")
_PHONE_RUN = re.compile(r"(?<![\w.+-])\d{10,15}(?![\w-])")
_MIN_PHONE_DIGITS = 8

_EMPTY_BRACKETS = re.compile(r"\(\s*\)|\[\s*\]|<\s*>|\{\s*\}")
_SEPARATOR = re.compile(r"( ?[|·•;,] ?)")
_EDGE_SEPARATORS = " |,;:·•-–—/\\"

# Words that give a spreadsheet column or a field away as e-mail or phone: email, email_address, emailaddress, work_email,
# mail_1, gmail, phone, phonenumber, mobile, whatsapp, tel, cell, fax, home_address ...  A column that is *about* e-mails
# ("company_name_for_emails") is not one.
_CONTACT_WORDS = ("mail", "phone", "mobile", "whatsapp", "telegram", "skype")
_CONTACT_TOKENS = frozenset({"tel", "cell", "fax", "sms"})
_ADDRESS_KINDS = frozenset({"home", "street", "postal", "mailing", "billing"})
_ABOUT = frozenset({"for", "to", "of"})

# Fields that are web addresses by nature (twitter_url, website, company_linkedin_url ...). They keep a plain http(s) address
# (no user name, query or fragment) - or nothing.
_LINK_ENDINGS = ("url", "urls", "link", "links", "website", "homepage")
_LINK_WORDS = frozenset({"site", "web"})


def is_link_field(name: str) -> bool:
    name = str(name or "")
    return name.endswith(_LINK_ENDINGS) or name.rsplit("_", 1)[-1] in _LINK_WORDS


# Fields that describe the row itself and are not free text.
_NOT_TEXT = frozenset({"key", "linkedin_url", "source_type", "source_id", "added_at", "updated_at", "parser", "scraped_at",
                       "priority", "founder_id", "company_slug"})


def is_contact_field(name: str) -> bool:
    """Does this field name (``norm_key`` form) say the field holds an e-mail address, a phone number or a home address?"""
    tokens = [re.sub(r"\d+$", "", t) for t in str(name or "").split("_") if t]
    for i, token in enumerate(tokens):
        if i and tokens[i - 1] in _ABOUT:
            continue
        if token in _CONTACT_TOKENS or any(word in token for word in _CONTACT_WORDS):
            return True
        if token in ("address", "addr") and i and tokens[i - 1] in _ADDRESS_KINDS:
            return True
    return False


def _digits(match: re.Match) -> bool:
    return sum(ch.isdigit() for ch in match.group(0)) >= _MIN_PHONE_DIGITS


_TRIGGER = re.compile(r"www\.|(?:mailto|tel|sms|skype|whatsapp|javascript|data):|[\[({<]\s?(?:at|@)|\bdot\b", re.I)
_NOT_DIGITS = re.compile(r"\D+")


def _might_hold_contact_details(text: str) -> bool:
    """A cheap look that rules out nearly all ordinary text before any pattern runs."""
    if "@" in text or "://" in text or ("/" in text and "." in text) or _TRIGGER.search(text):
        return True
    return len(_NOT_DIGITS.sub("", text)) >= _MIN_PHONE_DIGITS


def _tidy(text: str) -> str:
    """What is left after removals: no empty brackets, no doubled or dangling separators, one space between words."""
    text = re.sub(r"\s+", " ", _EMPTY_BRACKETS.sub(" ", text)).strip()
    parts = _SEPARATOR.split(text)                      # text, separator, text, separator, ...
    kept: list[str] = []
    for i in range(0, len(parts), 2):
        token = parts[i].strip(_EDGE_SEPARATORS)
        if token:
            if kept and i:
                mark = parts[i - 1].strip()
                kept.append(mark + " " if mark in (",", ";") else f" {mark} ")
            kept.append(token)
    return "".join(kept)


MAX_CHARS = 4_000          # fields are at most 1,200 characters long by the time they get here; more is cut, never scanned
MAX_NAME_CHARS = 300       # (a name is a few words; a "name" that is a page of text is not looked for word by word)


def _scrub_once(text: str) -> str:
    out = text
    for pattern in (_SCHEME, _WWW, _PROFILE_SITES, _EMAIL, _EMAIL_BRACKETED, _EMAIL_SPELLED):
        out = pattern.sub(" ", out)
    for pattern in (_PHONE_PLUS, _PHONE_AREA, _PHONE_GROUPS, _PHONE_RUN):
        out = pattern.sub(lambda m: m.group(0) if not _digits(m) else " ", out)
    return text if out == text else _tidy(out)


MAX_ROUNDS = 3             # (what is left once a detail is cut out can, in a contrived text, make up a new one)


def scrub_text(text) -> str:
    """The text without e-mail addresses, phone numbers and web addresses (whitespace and separators tidied)."""
    text = "" if text is None else str(text)[:MAX_CHARS]
    for _ in range(MAX_ROUNDS):
        if not text or not _might_hold_contact_details(text):
            break
        cleaned = _scrub_once(text)
        if cleaned == text:
            break
        text = cleaned
    return text


def has_contact_details(text) -> bool:
    return scrub_text(text) != ("" if text is None else str(text))[:MAX_CHARS]


def clean_link(value) -> str:
    """A web-address field: a plain ``http(s)://host/path`` or ``''``. No user name, query string or fragment (that is where
    a ``?email=jane@acme.com`` hides), no other scheme. ``acme.com/team`` is taken to mean ``https://acme.com/team``."""
    text = "" if value is None else str(value).strip()
    if not text or len(text) > 2000:
        return ""
    if "://" not in text:
        if text.split("/", 1)[0].count("@") or ":" in text.split("/", 1)[0] or "." not in text.split("/", 1)[0]:
            return ""                                          # "mailto:jane@acme.com", "jane@acme.com", "acme": not an address
        text = "https://" + text
    try:
        parts = urlsplit(text)
        host = parts.hostname or ""
    except ValueError:
        return ""
    if parts.scheme.lower() not in ("http", "https") or not host or any(ord(c) < 33 for c in text):
        return ""
    netloc = host if ":" not in host else f"[{host}]"
    try:
        if parts.port:
            netloc += f":{parts.port}"
    except ValueError:
        return ""
    path = parts.path
    if path and scrub_text(path) != path[:MAX_CHARS]:
        path = ""                                              # (an e-mail address or phone number in the path: keep the site only)
    return f"{parts.scheme.lower()}://{netloc}{path if path.startswith('/') or not path else '/' + path}"


def scrub_person(row: dict) -> dict:
    """A copy of a person row with contact fields dropped and contact details removed from every text field. Idempotent."""
    out: dict = {}
    for name, value in row.items():
        if name in _NOT_TEXT:
            out[name] = value
        elif is_contact_field(name):
            continue
        elif is_link_field(name):
            link = clean_link(value)
            if link:
                out[name] = link
        elif isinstance(value, str):
            out[name] = scrub_text(value)
        else:
            out[name] = value
    return out


# ----------------------------------------------------------------------------- the person's own name
def name_words(person: dict) -> list[str]:
    """The words of the person's name worth looking for (two letters or more), longest first."""
    seen: dict[str, None] = {}
    for field in ("full_name", "first_name", "last_name"):
        for word in re.findall(r"[^\W\d_]{2,}", str(person.get(field) or "")[:MAX_NAME_CHARS]):
            seen.setdefault(word.lower(), None)
    return sorted(seen, key=lambda w: (-len(w), w))[:8]


def without_name(text: str, person: dict) -> str:
    """``text`` with the person's own name taken out ("Jane Doe | CEO at Acme" -> "CEO at Acme")."""
    words = name_words(person)
    if not text or not words:
        return text
    pattern = re.compile(r"(?<![^\W\d_])(?:" + "|".join(re.escape(w) for w in words) + r")(?![^\W\d_])", re.I)
    out = pattern.sub(" ", text)
    return text if out == text else _tidy(out)
