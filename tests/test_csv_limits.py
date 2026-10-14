"""What a spreadsheet may be like: how wide, how long a line, how long a row, how many cells. The limits are made tiny here
so that each one can be crossed by a few lines of text; ``test_hostile_input`` feeds 20 MB files at the real ones."""
from __future__ import annotations

import pytest

from connecto.sources import csv_file


def person(i: int) -> str:
    return f"Person {i},https://www.linkedin.com/in/person-{i}/,Co {i},CEO"


def write(tmp_path, text: str):
    path = tmp_path / "leads.csv"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture()
def limits(monkeypatch):
    """Tiny limits; a test that wants one of them tighter sets it itself."""
    monkeypatch.setattr(csv_file, "MAX_COLUMNS", 4)
    monkeypatch.setattr(csv_file, "MAX_LINE_CHARS", 120)
    monkeypatch.setattr(csv_file, "MAX_ROW_CHARS", 400)
    monkeypatch.setattr(csv_file, "MAX_CELLS", 100_000)


def test_a_row_wider_than_the_limit_keeps_its_first_columns_and_says_so(tmp_path, limits):
    path = write(tmp_path, "Name,LinkedIn,Company,Title,Notes,More\nJane,https://www.linkedin.com/in/jane/,Acme,CEO,hello,there\n")
    rows, notes = csv_file.read_csv_rows(path)
    assert len(rows) == 1 and rows[0]["company"] == "Acme" and rows[0]["title"] == "CEO"
    assert "notes" not in rows[0] and "more" not in rows[0]
    assert notes == ["only the first 4 columns of a row were read"]


def test_a_line_longer_than_the_limit_is_cut_and_the_rest_of_the_file_is_still_read(tmp_path, limits):
    long_note = "x" * 500
    path = write(tmp_path, f"Name,LinkedIn,Company,Notes\n{person(1)},{long_note}\n{person(2)}\n")
    rows, notes = csv_file.read_csv_rows(path)
    assert [r["full_name"] for r in rows] == ["Person 1", "Person 2"]
    assert all(len(r.get("notes", "")) < 120 for r in rows)
    assert any("a line of more than 120 characters was cut short" in n for n in notes)


def test_a_cut_line_does_not_swallow_the_next_row_whatever_the_line_endings(tmp_path, limits):
    for ending in ("\n", "\r\n", "\r"):
        text = ending.join(["Name,LinkedIn,Company,Title", person(1) + "," + "x" * 500, person(2), person(3), ""])
        rows, _ = csv_file.read_csv_rows(write(tmp_path, text))
        assert [r["full_name"] for r in rows] == ["Person 1", "Person 2", "Person 3"], repr(ending)


def test_a_row_that_runs_on_over_many_lines_stops_the_reading_but_keeps_what_came_before(tmp_path, limits):
    runaway = '"' + "line of text\n" * 100
    path = write(tmp_path, f"Name,LinkedIn,Company,Title\n{person(1)}\n{person(2)}\n{runaway}\n{person(3)}\n")
    rows, notes = csv_file.read_csv_rows(path)
    assert [r["full_name"] for r in rows] == ["Person 1", "Person 2"]
    assert any("a single row of more than 400 characters stopped the reading there" in n for n in notes)


def test_many_short_rows_are_not_mistaken_for_one_long_one(tmp_path, limits):
    path = write(tmp_path, "Name,LinkedIn,Company,Title\n" + "\n".join(person(i) for i in range(200)) + "\n")
    rows, notes = csv_file.read_csv_rows(path)
    assert len(rows) == 200 and notes == []                       # (200 rows is far more than 400 characters in all)


def test_a_file_with_more_cells_than_the_limit_is_read_as_far_as_the_limit(tmp_path, limits, monkeypatch):
    monkeypatch.setattr(csv_file, "MAX_CELLS", 40)
    path = write(tmp_path, "Name,LinkedIn,Company,Title\n" + "\n".join(person(i) for i in range(50)) + "\n")
    rows, notes = csv_file.read_csv_rows(path)
    assert 5 <= len(rows) < 12
    assert any("reading stopped after" in n and "rows (40 cells)" in n for n in notes)


def test_an_unclosed_quote_is_a_clear_error(tmp_path):
    path = write(tmp_path, 'Name,LinkedIn\n"Jane,https://www.linkedin.com/in/jane/\n' + "x" * 200_000 + "\n")
    with pytest.raises(ValueError, match="quoted cell is never closed"):
        csv_file.read_csv_rows(path)


def test_the_limits_are_generous_enough_for_real_exports_and_small_enough_to_matter():
    assert csv_file.MAX_COLUMNS >= 200 and csv_file.MAX_ROWS * 20 <= csv_file.MAX_CELLS
    assert csv_file.MAX_LINE_CHARS >= 50_000 and csv_file.MAX_ROW_CHARS >= csv_file.MAX_LINE_CHARS
    # (a limit of a billion is no limit: these are what a 60 MB file can cost in memory, not what it may contain)
    assert csv_file.MAX_COLUMNS <= 5_000 and csv_file.MAX_CELLS <= 10_000_000
    assert csv_file.MAX_LINE_CHARS <= 1_000_000 and csv_file.MAX_ROW_CHARS <= 5_000_000


def test_one_row_without_a_name_says_has_not_have(tmp_path):
    rows, notes = csv_file.read_csv_rows(write(tmp_path, "LinkedIn,Company\nhttps://www.linkedin.com/in/jane/,Acme\n"))
    assert len(rows) == 1 and "1 row has no name, so notes can't be personalised for them" in notes
