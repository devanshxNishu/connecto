"""Reading a settings file written by somebody else (or by mistake): no aliases that blow up, no giant files, no surprise
numbers, and a configuration of any shape comes out as a usable one with the problems named."""
from __future__ import annotations

import time

import pytest
import yaml

from connecto import cli, config
from connecto.util import MAX_YAML_BYTES, load_yaml, read_yaml_file, real_number, whole_number

from helpers import make_home

NAN, INF = float("nan"), float("inf")


# ----------------------------------------------------------------------------- the YAML reader
def laughing_matter(depth: int = 9, fan: int = 9) -> str:
    """Nine lines that expand to fan**depth items when aliases are followed (the 'billion laughs' document)."""
    lines = ["l0: &l0 [" + ", ".join(["lol"] * fan) + "]"]
    for i in range(1, depth):
        lines.append(f"l{i}: &l{i} [" + ", ".join([f"*l{i - 1}"] * fan) + "]")
    return "\n".join(lines) + "\n"


def test_aliases_are_refused_so_a_small_file_cannot_expand_into_gigabytes():
    started = time.monotonic()
    with pytest.raises(yaml.YAMLError) as caught:
        load_yaml(laughing_matter())
    assert "aliases" in str(caught.value) and time.monotonic() - started < 1


def test_a_lone_alias_is_refused_too():
    with pytest.raises(yaml.YAMLError):
        load_yaml("a: &x 1\nb: *x\n")


def test_anchors_without_aliases_and_ordinary_documents_still_load():
    assert load_yaml("a: &x 1\nb: 2\n") == {"a": 1, "b": 2}
    assert load_yaml("") is None and load_yaml("- 1\n- two\n") == [1, "two"]


def test_a_file_that_is_too_large_is_refused_before_it_is_parsed(tmp_path):
    big = tmp_path / "config.yaml"
    big.write_bytes(b"# " + b"x" * MAX_YAML_BYTES + b"\n")
    with pytest.raises(ValueError, match="larger than"):
        read_yaml_file(big)
    with pytest.raises(ValueError, match="larger than"):
        load_yaml("# " + "x" * MAX_YAML_BYTES)


def test_very_deep_nesting_is_a_clean_error_not_a_crash():
    with pytest.raises(ValueError, match="nested too deeply"):
        load_yaml("[" * 20_000 + "]" * 20_000)


def test_a_byte_order_mark_is_fine(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_bytes(b"\xef\xbb\xbfme:\n  first_name: Asha\n")
    assert read_yaml_file(path) == {"me": {"first_name": "Asha"}}


def test_unquoted_times_are_text_not_sexagesimal_numbers():
    assert load_yaml("t: 9:00\nu: 1:30:15\nv: 12") == {"t": "9:00", "u": "1:30:15", "v": 12}
    assert load_yaml("a: 0x1F\nb: 0b101\nc: 017\nd: 1_000\ne: 2.5\nf: .inf") == {"a": 31, "b": 5, "c": 15, "d": 1000, "e": 2.5, "f": INF}


def test_the_settings_page_and_the_command_line_report_an_unreadable_file_cleanly(tmp_path, capsys):
    home = make_home(tmp_path)
    home.config_path.write_text(laughing_matter(), encoding="utf-8")
    code = cli.main(["--home", str(home.root), "config", "check"])
    err = capsys.readouterr().err
    assert code != 0 and "could not be read" in err and "aliases" in err


# ----------------------------------------------------------------------------- numbers
@pytest.mark.parametrize("value,expected", [(5, 5), ("12", 12), (" 7 ", 7), ("1e3", 1000), (12.9, 12), (-3, -3), (10 ** 30, 10 ** 30)])
def test_whole_number_reads_numbers_and_numeric_text(value, expected):
    assert whole_number(value) == expected


@pytest.mark.parametrize("value", [True, False, NAN, INF, -INF, "nan", "inf", "x", "", "1:30", None, [], {}, "1e400"])
def test_whole_number_refuses_everything_else(value):
    with pytest.raises((TypeError, ValueError)):
        whole_number(value)


@pytest.mark.parametrize("value", [True, NAN, INF, "inf", "nan", "x", None, [], 10 ** 400, "1e400"])
def test_real_number_refuses_what_is_not_a_finite_number(value):
    with pytest.raises((TypeError, ValueError)):
        real_number(value)


def test_real_number_reads_numbers_and_numeric_text():
    assert real_number("2.5") == 2.5 and real_number(3) == 3.0 and real_number(" 1e2 ") == 100.0


# ----------------------------------------------------------------------------- the normaliser takes anything
HOSTILE_SETTINGS = [
    {"me": [1, 2]}, {"me": {"first_name": {"a": 1}}}, {"me": {"first_name": ["x"], "about": None}}, {"me": "Asha"},
    {"targets": "everyone"}, {"targets": {"keywords": {"a": 1}}}, {"targets": {"titles_regex": 5}}, {"targets": {"titles": [None, 5, ["x"]]}},
    {"sources": "yc"}, {"sources": [None, 5, [], {"type": []}]}, {"sources": [{"type": "csv", "path": ["a"]}]},
    {"sources": {"type": "csv"}}, {"sources": [{"type": "webpage", "url": 5}]},
    {"message": "hello"}, {"message": {"templates": "Hi {first_name}"}}, {"message": {"templates": [None, 5, ["x"]]}},
    {"message": {"max_note_chars": "lots"}}, {"message": {"max_note_chars": NAN}}, {"message": {"plan": ["free"]}},
    {"ai": "yes"}, {"ai": {"max_calls_per_day": NAN}}, {"ai": {"temperature": [1]}}, {"ai": {"base_url": 5}},
    {"browser": []}, {"browser": {"channel": 5}}, {"schedule": {"time": {"a": 1}}}, {"schedule": "daily"},
    {"notify": {"a": 1}}, {"order": {"a": 1}}, {"acknowledged_risks": "maybe"}, {"acknowledged_risks": [1]},
    {"limits": {"daily": {"a": 1}}}, {"limits": [1]}, {1: 2}, {None: 1}, {"unknown": {"deep": [1, {"x": NAN}]}},
    {"sources": [{"type": "csv", "path": "x.csv", "extra": NAN}]}, {"me": {"first_name": "x" * 100_000}},
]


@pytest.mark.parametrize("raw", HOSTILE_SETTINGS, ids=[repr(r)[:60] for r in HOSTILE_SETTINGS])
def test_normalize_never_raises_and_everything_downstream_copes(raw):
    cfg, warnings = config.normalize(raw)
    assert isinstance(cfg, dict) and isinstance(warnings, list) and all(isinstance(w, str) for w in warnings)
    config.limits(cfg)
    assert isinstance(config.problems(cfg), list)
    assert config.note_limit(cfg) in range(1, 301)
    assert config.templates(cfg) and all(isinstance(t, str) for t in config.templates(cfg))
    assert isinstance(config.enabled_sources(cfg), list)
    assert len(cfg["me"].get("first_name", "")) <= 300


@pytest.mark.parametrize("raw", [None, [], "text", 5, 5.5, True])
def test_normalize_of_something_that_is_not_a_mapping_gives_the_defaults(raw):
    cfg, _ = config.normalize(raw)
    assert cfg["me"]["first_name"] == "" and cfg["acknowledged_risks"] is False


def test_a_setting_that_cannot_be_read_is_named_and_does_not_take_the_rest_with_it(monkeypatch):
    real = config._normalize

    def picky(cfg):
        if "limits" in cfg and cfg["limits"] == "poison":
            raise RuntimeError("nothing anticipated this")
        return real(cfg)

    monkeypatch.setattr(config, "_normalize", picky)
    cfg, warnings = config.normalize({"me": {"first_name": "Asha"}, "limits": "poison"})
    assert cfg["me"]["first_name"] == "Asha" and any("limits" in w and "default" in w for w in warnings)
