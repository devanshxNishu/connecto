"""The regular expressions people write in their settings (``titles_regex``, ``exclude_titles_regex``).

Python's regular expressions can run for minutes on a text that almost matches: ``(a+)+$`` against thirty letters and a
full stop is the classic, ``(.*)*founder`` against a 26-letter title took 17 seconds. The texts matched here are strangers'
job titles, and settings files get copied from forums, so a pattern is refused when its shape allows that: a repeat inside a
repeat, alternatives inside an open-ended repeat, or more than a few open-ended repeats (``*``, ``+``, ``{n,}``), whose costs
multiply. A title is matched on its first ``MAX_TEXT_CHARS`` characters only.

This checks the *shape* of a pattern; it is not a proof that no pattern can be slow, and it does not make a refused
pattern wrong - ``(?i)\\bhead of (design|product)\\b`` is fine, ``(\\w+\\s?)+$`` is not.
"""
from __future__ import annotations

import re
from functools import lru_cache

try:                                                  # Python 3.11 and later
    from re import _constants as _c
    from re import _parser as _p
except ImportError:                                   # pragma: no cover - Python 3.10
    import sre_constants as _c                        # type: ignore[no-redef]
    import sre_parse as _p                            # type: ignore[no-redef]

MAX_PATTERN_CHARS = 200
MAX_TEXT_CHARS = 150          # (a job title is a few words)
MAX_OPEN_REPEATS = 3          # ".*a.*b.*c.*d.*e" on 200 letters takes seconds; three open repeats take a tenth of one

_REPEATS = tuple(op for op in (_c.MAX_REPEAT, _c.MIN_REPEAT, getattr(_c, "POSSESSIVE_REPEAT", None)) if op is not None)
_ATOMIC = getattr(_c, "ATOMIC_GROUP", None)


def _is_open(high: int) -> bool:
    return high >= _c.MAXREPEAT


def _parts(op, av):
    """The sub-patterns directly inside one node of a parsed pattern."""
    if op in _REPEATS:
        yield av[2]
    elif op == _c.SUBPATTERN:
        yield av[3]
    elif op == _c.BRANCH:
        yield from av[1]
    elif op in (_c.ASSERT, _c.ASSERT_NOT):
        yield av[1]
    elif _ATOMIC is not None and op == _ATOMIC:
        yield av
    elif op == _c.GROUPREF_EXISTS:
        yield av[1]
        if av[2] is not None:
            yield av[2]


def _nodes(sub):
    """Every node of a parsed (sub-)pattern, at any depth."""
    stack = [sub]
    while stack:
        for op, av in stack.pop():
            yield op, av
            stack.extend(_parts(op, av))


def problem(pattern: str) -> str:
    """Why this pattern is not accepted ('' when it is). It is also what tells a mistyped pattern from a good one."""
    pattern = str(pattern or "")
    if len(pattern) > MAX_PATTERN_CHARS:
        return f"it is longer than {MAX_PATTERN_CHARS} characters"
    try:
        tree = _p.parse(pattern)
        re.compile(pattern)
    except (re.error, RecursionError, OverflowError, ValueError) as exc:
        return f"it is not a valid regular expression ({exc})"
    open_repeats = 0
    for op, av in _nodes(tree):
        if op not in _REPEATS:
            continue
        _, high, sub = av
        if _is_open(high):
            open_repeats += 1
        if high <= 1:
            continue
        for inner_op, inner_av in _nodes(sub):
            if inner_op in _REPEATS and inner_av[1] > 1 and (_is_open(high) or _is_open(inner_av[1])):
                return "it repeats something that is itself repeated, like (a+)+, which can take minutes on a short text"
            if inner_op == _c.BRANCH and _is_open(high):
                return "it repeats alternatives without end, like (a|b)+, which can take minutes on a short text"
    if open_repeats > MAX_OPEN_REPEATS:
        return f"it has more than {MAX_OPEN_REPEATS} open-ended repeats (*, + or {{n,}}), which can make it very slow"
    return ""


@lru_cache(maxsize=64)
def _compiled(pattern: str):
    why = problem(pattern)
    if why:
        raise ValueError(why)
    return re.compile(pattern)


def check(pattern: str) -> str:
    """The pattern itself, or ``ValueError`` saying why it is not accepted."""
    _compiled(pattern)
    return pattern


def search(pattern: str, text: str) -> bool:
    """``re.search`` of an accepted pattern in the first MAX_TEXT_CHARS characters of ``text``."""
    return _compiled(pattern).search(str(text or "")[:MAX_TEXT_CHARS]) is not None
