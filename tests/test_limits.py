"""The ceilings and the quota arithmetic: nothing a settings file, the setup page or a hand-built ``Limits`` asks for gets
past ``HARD`` - and nothing that is merely strange (NaN, infinity, text, the wrong shape) makes the reading of a settings
file raise."""
from __future__ import annotations

import random
from datetime import datetime, time as dtime

import pytest

from connecto.limits import DEFAULTS, HARD, MAX_DELAY_SECONDS, Limits, limits_from_config, visit_cap
from connecto.util import load_yaml

NAN, INF = float("nan"), float("inf")


# ----------------------------------------------------------------------------- the pause between two profiles
@pytest.mark.parametrize("delay", [(0, 0), (1, 2), (30, 30), (60, 180), (180, 60), (5, 99999), (-5, 10), (NAN, 50), (40, NAN),
                                   (NAN, NAN), (INF, INF), (-INF, 1), (None, None), ("a", "b")])
def test_the_pause_between_profiles_stays_inside_the_ceilings_whatever_was_asked(delay):
    lim = Limits(delay=delay)
    rng = random.Random(3)
    for _ in range(40):
        pause = lim.pause_between(rng)
        assert HARD["delay_min_seconds"] <= pause <= MAX_DELAY_SECONDS, (delay, pause)


def test_the_pause_is_drawn_from_the_range_that_was_asked_for():
    lim = Limits(delay=(100, 200))
    pauses = [lim.pause_between(random.Random(i)) for i in range(60)]
    assert min(pauses) >= 100 and max(pauses) <= 200 and len({round(p) for p in pauses}) > 10


# ----------------------------------------------------------------------------- how many invitations
def test_a_hand_built_limits_cannot_exceed_a_ceiling():
    wild = Limits(daily=(500, 900), weekly=10_000, per_run=(300, 400), delay=(0, 0))
    day = datetime(2026, 10, 6, 12)
    assert wild.daily_target(day) == HARD["daily_max"]
    assert wild.per_run_target(random.Random(1)) == HARD["per_run_max"]
    assert wild.remaining(0, 0, now=day, rng=random.Random(1))[0] == HARD["per_run_max"]
    allowed, why = wild.remaining(0, HARD["weekly_max"], now=day)
    assert allowed == 0 and "weekly cap" in why
    assert wild.remaining(0, 0, now=day, limit_override=10_000)[0] == HARD["per_run_max"]
    assert wild.remaining(0, 0, now=day, limit_override=-1)[0] == 0
    assert wild.remaining(0, 0, now=day, limit_override=3)[0] == 3
    assert wild.remaining(HARD["daily_max"], 0, now=day)[0] == 0


def test_a_range_given_the_wrong_way_round_still_works():
    lim = Limits(daily=(12, 8), per_run=(7, 4))
    assert 8 <= lim.daily_target(datetime(2026, 10, 6)) <= 12
    assert all(4 <= lim.per_run_target(random.Random(i)) <= 7 for i in range(30))


def test_visit_cap_grows_with_the_quota_but_has_a_ceiling():
    assert [visit_cap(n) for n in (0, 1, 3, 5, 12, 20, 500)] == [3, 6, 12, 18, 39, 40, 40]
    assert visit_cap(-4) == 3 and visit_cap(10 ** 9) == HARD["visits_per_run_max"]


def test_the_new_ceilings_are_in_the_table_of_ceilings():
    assert 0 < HARD["idle_stop_max"] <= 20 and HARD["per_run_max"] < HARD["visits_per_run_max"] <= 60
    assert DEFAULTS["per_run"][1] <= HARD["per_run_max"] and DEFAULTS["daily"][1] <= HARD["daily_max"]


# ----------------------------------------------------------------------------- reading a settings file
HOSTILE = [
    None, [], "x", 5, {"limits": None}, {"limits": []}, {"limits": "fast"}, {"schedule": 5}, {"schedule": []},
    {"limits": {"daily": NAN}}, {"limits": {"daily": [INF, 1e308]}}, {"limits": {"daily": [NAN, 5]}},
    {"limits": {"daily": [10 ** 400, 1]}}, {"limits": {"daily": [1, 2, 3]}}, {"limits": {"daily": {"a": 1}}},
    {"limits": {"daily": "1e400"}}, {"limits": {"daily": [True, False]}}, {"limits": {"weekly": NAN}},
    {"limits": {"weekly": INF}}, {"limits": {"weekly": True}}, {"limits": {"weekly": -5}}, {"limits": {"weekly": 0}},
    {"limits": {"per_run": [0, 0]}}, {"limits": {"per_run": [-3, 99]}}, {"limits": {"per_run": "5"}},
    {"limits": {"delay_seconds": [NAN, INF]}}, {"limits": {"delay_seconds": "30-60"}}, {"limits": {"delay_seconds": [1, 10 ** 9]}},
    {"limits": {"delay_seconds": ["1:30", "3:00"]}}, {"limits": {"delay_seconds": [-INF, INF]}},
    {"limits": {"stop_after_errors": "many"}}, {"limits": {"stop_after_errors": INF}}, {"limits": {"stop_after_errors": 0}},
    {"limits": {"stop_after_errors": 99}},
    {"schedule": {"active_hours": ["9", 17]}}, {"schedule": {"active_hours": [True, 5]}}, {"schedule": {"active_hours": "09:00-17:00"}},
    {"schedule": {"active_hours": [17, 9]}}, {"schedule": {"active_hours": [24, 99]}}, {"schedule": {"active_hours": {"a": 1}}},
    {"schedule": {"active_days": {"mon": 1}}}, {"schedule": {"active_days": 3}}, {"schedule": {"active_days": "mon,wat"}},
    {"schedule": {"active_days": [None, 5, ["mon"]]}},
    {"schedule": {"start_jitter_minutes": NAN}}, {"schedule": {"start_jitter_minutes": -5}},
    {"schedule": {"start_jitter_minutes": 10 ** 6}}, {"schedule": {"start_jitter_minutes": "soon"}},
]


@pytest.mark.parametrize("cfg", HOSTILE, ids=[repr(c)[:50] for c in HOSTILE])
def test_limits_from_any_config_never_raise_and_stay_inside_the_ceilings(cfg):
    lim = limits_from_config(cfg)
    assert 1 <= lim.daily[0] <= lim.daily[1] <= HARD["daily_max"]
    assert 1 <= lim.weekly <= HARD["weekly_max"]
    assert 1 <= lim.per_run[0] <= lim.per_run[1] <= HARD["per_run_max"]
    assert HARD["delay_min_seconds"] <= lim.delay[0] <= lim.delay[1] <= MAX_DELAY_SECONDS
    assert 1 <= lim.stop_after_errors <= HARD["errors_stop_max"]
    assert 0 <= lim.start_jitter_minutes <= 120
    assert lim.active_days and lim.active_days <= frozenset(range(7))
    assert lim.active_hours is None or lim.active_hours[0] < lim.active_hours[1]
    pause = lim.pause_between(random.Random(1))
    assert HARD["delay_min_seconds"] <= pause <= MAX_DELAY_SECONDS


def test_what_had_to_be_pulled_back_is_said_in_the_warnings():
    lim = limits_from_config({"limits": {"daily": [100, 200], "weekly": 1000, "per_run": [50, 60], "delay_seconds": [1, 2],
                                         "stop_after_errors": 99}})
    text = " ".join(lim.warnings)
    for name in ("limits.daily", "limits.weekly", "limits.per_run", "limits.delay_seconds", "limits.stop_after_errors"):
        assert name in text, name


def test_a_delay_of_more_than_an_hour_is_lowered_with_a_warning():
    lim = limits_from_config({"limits": {"delay_seconds": [60, 90_000]}})
    assert lim.delay[1] == MAX_DELAY_SECONDS and any("lowered" in w for w in lim.warnings)


def test_times_of_day_written_without_quotes_stay_times_of_day():
    """YAML 1.1 reads 9:00 as the number 540 (sexagesimal); a settings file must not."""
    cfg = load_yaml("schedule:\n  active_hours: [9:00, 17:30]\nlimits:\n  delay_seconds: [1:30, 3:00]\n")
    assert cfg["schedule"]["active_hours"] == ["9:00", "17:30"]
    lim = limits_from_config(cfg)
    assert lim.active_hours == (dtime(9, 0), dtime(17, 30))
    assert lim.delay == (60.0, 180.0) and any("delay_seconds" in w for w in lim.warnings)     # "1:30" is not a number of seconds


@pytest.mark.parametrize("hours,expected", [([9, 17], (dtime(9, 0), dtime(17, 0))), (["9:05", "17:45"], (dtime(9, 5), dtime(17, 45))),
                                            (["09:00", "20:30"], (dtime(9, 0), dtime(20, 30)))])
def test_hours_may_be_written_as_a_bare_hour_or_as_hours_and_minutes(hours, expected):
    assert limits_from_config({"schedule": {"active_hours": hours}}).active_hours == expected


def test_an_empty_active_hours_means_all_day():
    lim = limits_from_config({"schedule": {"active_hours": []}})
    assert lim.active_hours is None and lim.within_active_hours(datetime(2026, 10, 6, 3, 0))
