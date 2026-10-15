"""Title filters written as regular expressions: a pattern that can run for minutes on a short text is refused, and a title is
matched on its first characters only. The settings file may have come from a forum post and the titles from strangers."""
from __future__ import annotations

import signal
import time
from contextlib import contextmanager

import pytest

from connecto import config, regexsafe
from connecto.targets import Targets


@contextmanager
def within(seconds: float = 5):
    if not hasattr(signal, "setitimer"):          # Windows: no interrupt, the time check after the match still applies
        yield
        return

    def give_up(*_):
        raise AssertionError(f"still matching after {seconds:g} seconds")

    previous = signal.signal(signal.SIGALRM, give_up)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


GOOD = [
    r"(?i)\b(ceo|cto|founder)\b", r"(?i)head of (design|product)", r"^(Co-)?Founder", r"(?i)\b(?:chief|head|vp)\s+(?:of\s+)?\w+",
    r".*founder.*", r"(?:\d{1,3}\.){3}\d{1,3}", r"(?i)advisor|investor", r"(?i)\bCEO\b", r"(?i)\b(co-?)?founder\b",
    r"(?i)(?:senior|sr\.?)\s+(?:software\s+)?engineer", r"\bhead.*of.*design\b", r"(?:a|b)*c", r"[a-z]+\s*[a-z]+", r"(?i)(?:chief|head){1,2}\s+officer",
]
BAD = {
    r"(a+)+$": "repeats something that is itself repeated", r"(.*)*founder": "repeats something that is itself repeated",
    r"(a*)*": "repeats something that is itself repeated", r"(\w+\s?)+$": "repeats something that is itself repeated",
    r"(a+){2,5}": "repeats something that is itself repeated", r"(x+x+)+y": "repeats something that is itself repeated",
    r"(?:a{1,3})+": "repeats something that is itself repeated", r"([a-z]+\s*)*$": "repeats something that is itself repeated",
    r"(a|aa)+$": "alternatives without end", r"(a|b|ab)*c": "alternatives without end", r"(ceo|cto)+": "alternatives without end",
    r".*a.*b.*c.*d.*e": "more than 3 open-ended repeats", r"\w+\s+\w+\s+\w+\s+\w+": "more than 3 open-ended repeats",
    "x" * 201: "longer than 200 characters", "(": "not a valid regular expression", "[z-a]": "not a valid regular expression",
}


@pytest.mark.parametrize("pattern", GOOD)
def test_ordinary_title_patterns_are_accepted(pattern):
    assert regexsafe.problem(pattern) == "" and regexsafe.check(pattern) == pattern


@pytest.mark.parametrize("pattern,why", list(BAD.items()))
def test_patterns_that_can_run_for_minutes_are_refused_with_a_reason(pattern, why):
    assert why in regexsafe.problem(pattern)
    with pytest.raises(ValueError, match=why):
        regexsafe.check(pattern)


def test_the_slow_patterns_really_are_slow_and_the_accepted_ones_are_not():
    """The refusal is worth something: ``(.*)*founder`` takes seconds on a 26-letter title, the worst accepted pattern does not."""
    import re
    started = time.perf_counter()
    assert re.search(r"(.*)*founder", "a" * 24) is None
    assert time.perf_counter() - started > 0.5, "this machine runs the classic slow pattern fast; the test proves nothing here"
    worst = ".*.*.*x"
    assert regexsafe.problem(worst) == ""
    started = time.perf_counter()
    with within():
        assert regexsafe.search(worst, "a" * 10_000) is False
    assert time.perf_counter() - started < 0.5


def test_a_title_is_matched_on_its_first_characters_only():
    assert regexsafe.search(r"(?i)founder", "Co-founder at " + "x" * 100)
    assert not regexsafe.search(r"(?i)founder", "x" * regexsafe.MAX_TEXT_CHARS + " founder")
    assert regexsafe.search(r"^$", "") and not regexsafe.search("a", None)


def test_the_filters_of_a_run_refuse_a_slow_pattern_and_keep_working_with_a_good_one():
    with pytest.raises(ValueError, match=r"titles_regex: it repeats something"):
        Targets(titles_regex="(.*)*founder")
    with pytest.raises(ValueError, match=r"exclude_titles_regex: it repeats alternatives"):
        Targets(exclude_titles_regex="(a|aa)+$")
    t = Targets.from_config({"targets": {"titles_regex": r"(?i)\bceo\b", "exclude_titles_regex": r"(?i)advis[eo]r"}})
    assert t.matches({"title": "CEO & Co-founder"}) and not t.matches({"title": "CEO and advisor"}) and not t.matches({"title": "CTO"})


@pytest.mark.parametrize("field", ["titles_regex", "exclude_titles_regex"])
def test_a_hostile_title_is_judged_quickly_by_a_pattern_with_three_open_repeats(field):
    t = Targets(**{field: r".*founder.*ceo.*x"})
    start = time.perf_counter()
    with within():
        for _ in range(200):
            t.matches({"title": "founder ceo " * 200})
    assert time.perf_counter() - start < 3


def test_a_slow_pattern_in_the_settings_is_ignored_with_a_warning_that_says_what_that_means():
    cfg, warnings = config.normalize({"targets": {"titles_regex": "(.*)*founder", "exclude_titles_regex": "(a+)+$"}})
    assert cfg["targets"]["titles_regex"] == "" and cfg["targets"]["exclude_titles_regex"] == ""
    assert any(w.startswith("targets.titles_regex:") and "repeats something" in w and "ignored" in w and "NOT" not in w for w in warnings)
    assert any(w.startswith("targets.exclude_titles_regex:") and "NOT left out" in w for w in warnings)
    cfg, warnings = config.normalize({"targets": {"titles_regex": r"(?i)\bfounder\b", "exclude_titles_regex": "(?i)advisor"}})
    assert cfg["targets"]["titles_regex"] == r"(?i)\bfounder\b" and not warnings
