"""Validation of untrusted text: the one-line hook a model writes, and the audience JSON it returns.

Everything the model says is treated as hostile input. A hook only reaches a real connection note if it is a short,
plain, grounded sentence; anything else (links, contact details, markup, emoji, questions, refusals, off-topic text
that a poisoned profile might have steered it into) is thrown away and the note simply uses the template without it.
"""
from __future__ import annotations

import ast
import json
import re
import unicodedata

from ..util import clean_text

MAX_HOOK_WORDS = 28
MIN_HOOK_WORDS = 3

# ----------------------------------------------------------------------------- hook rules
_LABEL_WORDS = (r"hook|opening(?:\s+(?:line|sentence))?|opener|line|sentence|output|answer|result|response|"
                r"connection\s+note|note|message")
_LABEL = re.compile(rf"(?i)^[\s*_#>\-–—•·]*(?:{_LABEL_WORDS})\s*(?::|\s[-–—]\s)\s*[\s*_]*")
_BULLET = re.compile(r"^(?:[-–—•·>]+|\*+(?=\s)|\d{1,2}[.)])\s+")
_QUOTES = "\"'“”‘’«»`"
_NONE = re.compile(r"(?i)^(?:none|n/?a|null|nothing|no hook|no line|skip)\b")
_REFUSALS = ("i can't", "i cannot", "i can not", "i'm unable", "i am unable", "unable to", "as an ai",
             "as a language model", "sorry", "i apologize", "cannot provide", "can't provide",
             "not enough information", "insufficient information", "i don't have", "i do not have")
# The phrasing that tries to give a model new orders. Both the model's reply and the facts we are about to send are judged
# by it: a profile that says "forget everything above" is not sent, and a reply that says it is not used. (Kept narrow on
# purpose - "forget everything you know about invoicing" and "my new role" are ordinary profile text.)
_ORDERS = (r"\bignore\b.{0,40}\b(?:previous|prior|above|earlier|all|any|your|the|these|those)\b|\bdisregard\b"
           r"|\bforget\s+(?:everything|all)\s+(?:above|before|else|previous|prior|earlier|so\s+far|that\s+came|you\s+(?:were|have\s+been|had\s+been))\b"
           r"|\bforget\s+(?:the\s+)?(?:previous|prior|above|earlier)\b|\bforget\s+your\s+(?:instructions?|rules?|prompt)\b"
           r"|\bfrom\s+now\s+on\s*,?\s+(?:you|always|only|never|respond|reply|answer|output|say|write|speak|talk|act|ignore|use|call|refer|be|stop|start|begin|follow|obey|focus|praise|mention)\b"
           r"|\byour\s+new\s+(?:task|persona|role|rules?|instructions?)\b|\b(?:override|overwrite)\b.{0,30}\b(?:instructions?|rules?|prompt)\b"
           r"|\byour\s+(?:task|instructions?)\s+(?:is|are)\b|\bpretend\s+(?:to\s+be|you)\b|\brepeat\s+after\s+me\b"
           r"|system\s+prompt|\bjailbreak|prompt\s+injection|\byou\s+are\s+now\b|\bdeveloper\s+mode\b")
_INJECTION = re.compile(rf"(?i){_ORDERS}|\binstructions?\b|\bact\s+as\b|\brole-?play\b")
# Prompt-injection phrasing in text we are about to SEND (profile facts). Narrower than _INJECTION, which judges the
# model's output: an ordinary company blurb may well say "cooking instructions", or "we act as a bridge".
_FACT_INJECTION = re.compile(
    rf"(?i){_ORDERS}"
    r"|\b(?:new|follow|following|these|above|previous|your)\s+instructions?\b|\binstructions?\s+(?:above|below)\b"
    r"|\bas\s+an\s+ai\b")
# What a hook must not be: an invitation to do something (book, click, message me ...). It is an observation about the
# person's work, not a pitch. (Judged on the accent-free copy of the line.)
_CTA = re.compile(
    r"(?i)\b(?:book|schedule|arrange|grab|request)\s+(?:a|an|your|the|my|our)\s+(?:free\s+)?(?:demo|call|meeting|chat|consultation|session|trial)\b"
    r"|\b(?:dm|email|e-mail|text|call|message|contact|ping|add|follow|hit)\s+(?:me|us)\b|\breach\s+(?:out\s+)?to\s+(?:me|us)\b"
    r"|\b(?:click|tap)\b|\bsign\s*up\b|\bfree\s+trial\b|\bpromo\b|\bcoupon\b|\bdiscount\b|\bsubscribe\b|\bwhatsapp\b|\btelegram\b"
    r"|\blink\s+in\s+(?:my\s+)?bio\b|\b(?:check\s+out|visit|see)\s+(?:my|our)\b|\blet'?s\s+(?:chat|talk|connect|jump)\b")
# An address or contact written out in words, to get past the checks for the real thing: "acme dot com", "h t t p s colon
# slash slash", a phone number read out digit by digit ("nine eight seven six five four ...": six number words in a row).
_TLDS = "com|org|net|io|ai|dev|app|xyz|edu|gov|info|biz|tech|cloud|online"
_DIGIT_WORD = r"(?:zero|oh|one|two|three|four|five|six|seven|eight|nine)"
_SPELLED_OUT = re.compile(rf"(?i)\bdot\s+(?:{_TLDS})\b|\bdot\s*\)\s*(?:{_TLDS})\b|\bcolon\s+slash\b|\bslash\s+slash\b"
                          r"|\bh\s*t\s*t\s*p\s*s?\s+colon\b|\bw\s+w\s+w\b"
                          rf"|\b{_DIGIT_WORD}(?:[\s,.-]+{_DIGIT_WORD}\b){{5,}}")
_URL = re.compile(
    r"(?i)(?:https?://|ftp://|www\.|\b[a-z0-9][a-z0-9-]*\.(?:com|org|net|io|ai|co|in|dev|app|xyz|me|edu|gov|ly|so|sh"
    r"|to|tv|info|biz|us|uk|de|fr|cn|jp|tech|cloud|online|site|store|link|page|live|club|top|shop|gg|fm|ms|eu|ca|au)\b)")
# Any "word.word" with a lower-case ending is treated as a domain too, whatever the capitals before the dot ("evil.example",
# "myEvil.zip"). Legal: ".js" names ("Node.js"), and a sentence that lost its space after a full stop ("work.Great": the
# ending is capitalised, so it is not a lower-case ending).
_BARE_DOMAIN = re.compile(r"\b[A-Za-z0-9][A-Za-z0-9-]*\.(?!js\b)[a-z]{2,24}\b")
_BAD_CHARS = frozenset("*_`#<>[]{}\\|~^@")
# Emoji and pictographs: the supplementary pictograph planes, arrows, technical and enclosed symbols, dingbats, and the
# joiners that glue emoji together (variation selector 16, zero width joiner, keycap). Written as escapes on purpose.
_EMOJI = re.compile(r"[\U0001F000-\U0001FAFF\u2190-\u21FF\u2300-\u23FF\u2460-\u24FF\u25A0-\u27BF\u2900-\u297F"
                    r"\u2B00-\u2BFF\u3030\u303D\uFE0F\u200D\u20E3]")
# Prose only: Latin letters (with accents), digits, currency signs and everyday punctuation. This also blocks look-alike
# letters in fake domains and the habit of small multilingual models to slip into another script mid-sentence.
_PROSE = re.compile("^[A-Za-z0-9\u00A1-\u00A5\u00BF\u00C0-\u024F\u20A0-\u20CF\\s.,!:;'\u2019\"\u201C\u201D\u2018()&/%$+\u2013\u2014-]+$")
# Fold look-alike punctuation to plain ASCII before judging a line: full-width forms (U+FF01-FF5E), the ideographic
# full stop and space, and the ellipsis character.
_FOLD = {**{c: c - 0xFEE0 for c in range(0xFF01, 0xFF5F)}, 0x3000: " ", 0x3002: ".", 0xFF61: ".", 0x2026: "..."}
_ANY_WORD = re.compile(r"[^\W_]+(?:['\u2019][^\W_]+)?")
_NUMBER = re.compile(r"\d[\d,.]*\d|\d")
_PHONE_RUN = re.compile(r"\+?\d[\d\s().\-]{5,}\d")
_GREETING = re.compile(r"(?i)^(?:hi|hello|hey|dear|greetings|good\s+(?:morning|afternoon|evening))\b")
_HYPE = re.compile(r"(?i)\b(?:amazing|incredible|inspiring|inspirational|awesome|fantastic|phenomenal|stunning|"
                   r"extraordinary|mind-blowing|world-class|game-changing|groundbreaking|revolutionary|remarkable|outstanding|"
                   r"brilliant|genius|legendary|visionary|trailblazing|pioneering|unparalleled|unmatched|breathtaking|"
                   r"exceptional|spectacular|magnificent|jaw-dropping|best-in-class|top-tier|admire|admirable|obsessed)\b"
                   r"|\b(?:big|huge|massive|biggest)\s+fan\b|\bblown\s+away\b|\binspired\s+by\b|\blove\s+what\s+you\b")
# Things a line may say about a person's company only if the facts say it first: a prize, a ranking, a funding round, a
# press name, a superlative, a spelled-out figure. (Figures written in digits are held to the facts by ``_invented``.)
_CLAIM = re.compile(
    r"(?i)\b(?:award[- ]?winning|awards?|awarded|prizes?|winners?|won|ranked|rankings?|recogni[sz]ed|featured|backed|funded|funding"
    r"|raised|series\s+[a-e]|unicorn|forbes|techcrunch|bloomberg|trillions?|billions?|millions?|thousands?|hundreds?|dozens?|decades?"
    r"|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|largest|biggest|fastest|leader|number\s+one"
    r"|(?:the|a|an|world'?s|industry'?s|\w+'s)\s+(?:first|only|best|top|leading)"
    r"|one\s+of\s+the\s+(?:best|top|first|most\s+\w+)|first[- ](?:ever|of\s+its\s+kind)|top[- ](?:rated|ranked|notch)"
    r"|leading\s+(?:provider|company|platform|player|brand|firm|voice|authority|expert|name)|record[- ]breaking)\b")

# Words that carry no topic: a hook that only shares these with the facts is not grounded.
_STOP = frozenset("""
about above after again against also always among another around because been before being below between both build
building builds built came come comes could does doing done down during each even ever every from get gets give
gives going good great have having here high into just keen keep know like likely look looking looks made make
makes making many more most much must need needs never next only onto other others over really same seems should
since some something such sure take than that their theirs them then there these they thing things think this those
through truly under until upon using very want wants well were what when where which while will with within without
work worked working works would your yours youre interesting impressive exciting excited cool nice love loved
loving company companies team teams role roles people person world journey career story impact impactful space
field passion passionate mission vision innovative
""".split())
_WORD = re.compile(r"[^\W\d_]{4,}")


def facts_text(facts: dict) -> str:
    """All fact values as one string (what a hook is allowed to be about)."""
    parts: list[str] = []
    for value in (facts or {}).values():
        if isinstance(value, (list, tuple)):
            parts.extend(str(v) for v in value if v)
        elif value:
            parts.append(str(value))
    return " ".join(parts)


def _stem(word: str) -> str:
    if word.endswith(("sses", "shes", "ches", "xes", "zes")) and len(word) > 5:
        return word[:-2]
    if word.endswith("ies") and len(word) > 5:
        return word[:-3] + "y"
    if word.endswith("ing") and len(word) > 6:
        return word[:-3]
    if word.endswith("ed") and len(word) > 5:
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 4:
        return word[:-1]
    return word


def _content_words(text: str) -> set[str]:
    out = set()
    for w in _WORD.findall((text or "").lower()):
        stem = _stem(w)
        if w not in _STOP and stem not in _STOP:
            out.add(stem)
    return out


# A hook is one short sentence. A first line longer than this is not one, and is refused before any pattern looks at it:
# several of them (the domain patterns, the unwrapping below) take time that grows with the square of the length.
MAX_LINE_CHARS = 600


def _unwrap(line: str) -> str:
    """Strip bullets, ``Hook:``-style labels and surrounding quotes (they can nest)."""
    prev = None
    while prev != line:
        prev = line
        line = _BULLET.sub("", line.strip())
        line = _LABEL.sub("", line)
        line = line.strip().strip(_QUOTES).strip()
    return line


def _first_line(text: str) -> str:
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("```") or line.endswith(":"):
            continue            # blank, a code fence, or a preface such as "Here is the line:"
        return line
    return ""


_PUNCTUATION = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201C": '"', "\u201D": '"', "\u2013": "-", "\u2014": "-"})


def _plain(text: str) -> str:
    """The ASCII skeleton of a line, which is what the patterns judge: accents dropped (é -> e), compatibility forms
    unfolded (ﬁ -> fi), curly quotes and long dashes made plain, so that "Ignoré", "évil.example", "let’s connect" or
    "top–rated" cannot slip past a pattern written for the plain spelling."""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).translate(_PUNCTUATION)


# Symbols a line may hold besides ASCII: the inverted marks, the currency signs, and the curly quotes and dashes.
_ALLOWED_SYMBOLS = frozenset("\u00A1\u00A2\u00A3\u00A4\u00A5\u00BF\u2018\u2019\u201C\u201D\u2013\u2014")


def _plain_letters_only(line: str) -> bool:
    """True when every character that is not ASCII is an accented ASCII letter (é, ñ, ü) or an allowed symbol. A letter that
    only looks like one (dotless i, slashed o, ligatures, Cyrillic, Greek) is how look-alike words and domains are written."""
    for c in line:
        if c.isascii() or c in _ALLOWED_SYMBOLS or "\u20A0" <= c <= "\u20CF":
            continue
        base = unicodedata.normalize("NFD", c)[0]
        if not (base.isascii() and base.isalpha()):
            return False
    return True


# Words after which a full stop does not end a sentence ("Acme Inc. builds payments", "e.g. for").
_ABBREVIATIONS = frozenset({"inc", "ltd", "llc", "co", "corp", "plc", "gmbh", "dr", "mr", "ms", "mrs", "st", "no", "vs", "etc", "jr",
                            "sr", "e.g", "i.e", "u.s", "u.k", "a.i", "b.v", "s.a", "pty", "ag"})
_BREAK = re.compile(r"(\S+?)([.!]+)\s+(?=\S)")


def _several_sentences(line: str) -> bool:
    """True when something follows a full stop or exclamation mark inside the line: a hook is one sentence."""
    return any(not (m.group(2) == "." and m.group(1).lower() in _ABBREVIATIONS) for m in _BREAK.finditer(line))


_ARTICLE = re.compile(r"^(?:one of the|the|an|a|world'?s|industry'?s|\w+'s)\s+")


def _unsupported_claim(line: str, facts: str) -> bool:
    """True when the line makes a claim (a prize, a ranking, funding, a superlative, a spelled-out figure) that the facts do not."""
    known = " ".join(re.sub(r"[-\u2013\u2014]", " ", _plain(facts)).lower().split())
    for m in _CLAIM.finditer(_plain(line)):
        claim = _ARTICLE.sub("", " ".join(re.sub(r"[-\u2013\u2014]", " ", m.group()).lower().split()))
        if not re.search(rf"(?<![^\W_]){re.escape(claim)}(?![^\W_])", known):
            return True
    return False


def _has_phone(text: str) -> bool:
    return any(sum(c.isdigit() for c in m.group()) >= 7 for m in _PHONE_RUN.finditer(text))


def _invented(line: str, facts: str) -> bool:
    """True when the line states a number, or names something capitalised mid-sentence, that the facts never mention."""
    low = facts.lower()
    known = {w for w in re.findall(r"[^\W_]+", low)}
    known |= {_stem(w) for w in known}
    for num in _NUMBER.findall(line):
        if num.rstrip(".,") not in facts:
            return True
    for m in _ANY_WORD.finditer(line):
        word = re.split(r"['\u2019]", m.group())[0]
        if m.start() == 0 or word == "I" or not word[0].isupper():
            continue
        before = line[:m.start()].rstrip()
        if before and before[-1] in ".!?":
            continue                                   # first word of a later sentence
        if word.lower() not in known and _stem(word.lower()) not in known:
            return True
    return False


def looks_like_injection(text: str) -> bool:
    """True when profile text reads like an instruction aimed at an AI model (it is then never sent to one)."""
    return bool(_FACT_INJECTION.search(text or ""))


def groundable(facts: dict) -> bool:
    """True when the facts hold at least one topical word (4+ letters, not a filler word) a hook could be grounded in."""
    return bool(_content_words(facts_text(facts)))


def clean_hook(text: str, facts: dict, max_chars: int = 140) -> str | None:
    """Return a safe one-sentence hook, or None when the model's text must not be used."""
    if not isinstance(text, str):
        return None
    first = _first_line(text)
    if len(first) > MAX_LINE_CHARS:
        return None
    line = clean_text(_unwrap(first)).translate(_FOLD)
    if not line:
        return None
    judged = _plain(line)                                  # every pattern below reads this copy: no accent hides a word
    low = judged.lower()
    if _NONE.match(low) or any(r in low for r in _REFUSALS) or _INJECTION.search(judged) or _CTA.search(judged):
        return None
    if "?" in line or "？" in line or _URL.search(judged) or _BARE_DOMAIN.search(judged) or _SPELLED_OUT.search(judged):
        return None
    if _EMOJI.search(line) or _has_phone(judged) or not _PROSE.match(line) or not _plain_letters_only(line):
        return None
    if any(c in _BAD_CHARS for c in line) or _GREETING.match(judged) or _HYPE.search(judged):
        return None
    if line[-1] in ",;:-–—":
        return None                      # cut off mid-thought
    if line.endswith(("…", "..")):
        line = line.rstrip(".… ") + "."
    elif line[-1] not in ".!":
        line += "."
    if line[0].islower():
        line = line[0].upper() + line[1:]
    if not MIN_HOOK_WORDS <= len(line.split()) <= MAX_HOOK_WORDS or len(line) > max_chars:
        return None
    if _several_sentences(line):
        return None                      # one sentence, not two
    known_text = facts_text(facts)
    if not (_content_words(line) & _content_words(known_text)):
        return None                      # not grounded in the profile facts: off-topic or steered
    if _invented(line, known_text) or _unsupported_claim(line, known_text):
        return None                      # states a figure, prize or name the facts never mention
    return line


# ----------------------------------------------------------------------------- audience JSON
LIST_LIMITS = {"keywords": 8, "titles": 6, "locations": 5, "exclude_keywords": 5, "search_queries": 3}
YC_KEYS = ("batches", "industries", "tags")
YC_LIMIT = 6
ITEM_MAX = 60
_SEASONS = {"winter": "W", "spring": "X", "summer": "S", "fall": "F", "autumn": "F"}


def _repair(snippet: str) -> str:
    snippet = snippet.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    return re.sub(r",\s*([}\]])", r"\1", snippet)


def _balanced(text: str, start: int) -> str | None:
    """The ``{...}`` slice starting at ``start`` (aware of strings), or None if it never closes."""
    depth, quote, escaped = 0, "", False
    for i in range(start, len(text)):
        ch = text[i]
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _first_json_object(text: str) -> dict | None:
    decoder = json.JSONDecoder()
    start = text.find("{")
    for _ in range(40):
        if start < 0:
            return None
        try:
            obj, _end = decoder.raw_decode(text, start)
            if isinstance(obj, dict):
                return obj
        except ValueError:
            snippet = _balanced(text, start)
            if snippet:
                for attempt in (lambda s: json.loads(_repair(s)), lambda s: ast.literal_eval(_repair(s))):
                    try:
                        obj = attempt(snippet)
                    except (ValueError, SyntaxError, MemoryError, RecursionError):
                        continue
                    if isinstance(obj, dict):
                        return obj
        start = text.find("{", start + 1)
    return None


def _clean_list(value, limit: int) -> list[str]:
    if isinstance(value, str):
        items = re.split(r"[,;\n]", value)
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item is None or isinstance(item, (bool, dict, list, tuple)):
            continue
        text = clean_text(item, ITEM_MAX).rstrip("…").strip(" \"'`")
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _batch_code(text: str) -> str:
    m = re.fullmatch(r"(?i)([swfx])\s*-?\s*(?:20)?(\d{2})", text)
    if m:
        return m.group(1).upper() + m.group(2)
    m = re.fullmatch(r"(?i)(winter|spring|summer|fall|autumn)\s*-?\s*(?:20)?(\d{2})", text)
    return _SEASONS[m.group(1).lower()] + m.group(2) if m else text


def _clean_yc(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    out = {k: _clean_list(value.get(k), YC_LIMIT) for k in YC_KEYS}
    out["batches"] = list(dict.fromkeys(_batch_code(b) for b in out["batches"]))
    return out if any(out.values()) else None


def parse_audience(text: str) -> dict:
    """Pull the first JSON object out of a model reply (fences and chatter allowed) and sanitise it.

    Always returns the six known keys - lists for the five filter keys, and ``yc`` as a dict of three lists or None.
    Raises ValueError when the reply contains no JSON object.
    """
    obj = _first_json_object(text) if isinstance(text, str) else None
    if obj is None:
        raise ValueError("no JSON object found in the model's reply")
    out: dict = {key: _clean_list(obj.get(key), limit) for key, limit in LIST_LIMITS.items()}
    out["yc"] = _clean_yc(obj.get("yc"))
    return out
