"""People from a CSV file: LinkedIn's own Connections export, Apollo / Sales Navigator tool exports, or any spreadsheet.

Columns are recognised by their normalised header (``connecto.util.norm_key``), so "Person Linkedin Url", "First Name",
"Job Title" and "Company Name" just work; ``columns: {full_name: "Contact", linkedin_url: "Profile"}`` overrides the
guesswork. Whatever else the file has rides along as extra fields (usable in note templates and keyword filters) - except
e-mail addresses and phone numbers, which are never stored. ``read_csv_rows`` is also what ``connecto add --csv`` uses.
"""
from __future__ import annotations

import codecs
import csv
import io
import os
import re
from pathlib import Path

from .. import privacy
from ..models import make_person, normalize_linkedin, person_key
from ..util import clean_text, norm_key, read_regular_file
from .base import Sink, Source

DELIMITERS = (",", ";", "\t", "|")
MAX_BYTES = 60 * 1024 * 1024
MAX_ROWS = 100_000
MAX_EXTRA_COLUMNS = 80
# What a file of people can look like. A file of 20 MB of commas is a single row of twenty million empty cells, which is
# gigabytes of memory and a minute of work; so each of these is cut, with a note, long before that.
MAX_COLUMNS = 500                    # no list of people has more (Apollo's export has about 80); the rest of a row is ignored
MAX_CELLS = 5_000_000                # all rows together
MAX_LINE_CHARS = 200_000             # a line this long is not a row of people
MAX_ROW_CHARS = 1_000_000            # ... nor is a row that runs over thousands of lines inside quotes
SCAN_ROWS = 40                       # how far down to look for the header row (LinkedIn's export starts with notes)

# Header -> standard field, in priority order (the first header that matches wins when several could).
ALIASES: dict[str, tuple[str, ...]] = {
    "full_name": ("full_name", "fullname", "name", "contact_name", "lead_full_name", "lead_name", "person_name",
                  "profile_name", "display_name", "member_name"),
    "first_name": ("first_name", "firstname", "given_name", "lead_first_name", "person_first_name", "first"),
    "last_name": ("last_name", "lastname", "surname", "family_name", "lead_last_name", "person_last_name", "last"),
    "company": ("company", "company_name", "organization", "organisation", "organization_name", "employer",
                "current_company", "lead_company", "account_name"),
    "title": ("title", "job_title", "jobtitle", "position", "role", "current_title", "lead_job_title", "person_title",
              "current_position", "occupation", "designation"),
    "headline": ("headline", "profile_headline", "tagline"),
    "location": ("location", "person_location", "lead_location", "geography", "area"),
}
LOCATION_PARTS = ("city", "state", "region", "country")
_URL_HEADER = re.compile(r"linkedin|^url$|profile_?(url|link)|^person_url$")
_PERSONAL_HEADER = re.compile(r"person|profile|lead|contact|^url$|^linkedin(_url)?$")
_COMPANYISH_HEADER = re.compile(r"company|sales|navigator|account|org|school")
# Columns with these names would clash with something Connecto itself keeps: they are kept under a "csv_" name instead.
# (So is anything beginning with "me_": that is the sender's own namespace in note templates.)
RESERVED = {"key", "linkedin_url", "source_type", "source_id", "added_at", "updated_at", "parser", "scraped_at",
            "founder_id", "full_name", "first_name", "last_name"}
# A "priority" column (bigger = invited earlier) is yours to use, but it can never outrank somebody you added by hand.
MAX_CSV_PRIORITY = 999_999
# (it must begin where a word begins: see manual._URL for why)
_PROFILE_URL = re.compile(r"(?<![\w-])(?:https?://)?(?:[\w-]+\.)?linkedin\.com/(?:in|pub)/[^\s,;\"'<>)\]]+", re.I)
_ANY_LINKEDIN = re.compile(r"linkedin\.com/", re.I)


# ----------------------------------------------------------------------------- reading
def read_csv_text(path: str | os.PathLike) -> str:
    data = read_regular_file(path, MAX_BYTES, what="CSV file", hint=" - split it first")
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        text = data.decode("utf-16", errors="replace")
    else:
        for enc in ("utf-8-sig", "cp1252"):      # cp1252: what Excel on Windows writes for "CSV (Comma delimited)"
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = data.decode("utf-8", errors="replace")
    return text.replace("\x00", "")


def capped_lines(text: str, spent: list[int] | None = None, cut: set[str] | None = None):
    """The text a line at a time for ``csv.reader``: a line is cut at MAX_LINE_CHARS, and reading stops once a single row
    (``spent[0]`` counts its characters; the reader of the rows sets it back to 0 after each) is longer than MAX_ROW_CHARS."""
    spent = [0] if spent is None else spent
    cut = set() if cut is None else cut
    for line in io.StringIO(text, newline=""):
        if len(line) > MAX_LINE_CHARS:
            line = line[:MAX_LINE_CHARS]               # (csv.reader takes the end of each string for the end of a line)
            cut.add("line")
        spent[0] += len(line)
        if spent[0] > MAX_ROW_CHARS:
            cut.add("row")
            return
        yield line


def parse_csv(text: str, delimiter: str, max_rows: int | None = None, cut: set[str] | None = None) -> list[list[str]]:
    """The rows of a CSV text, within the limits above; what was cut is named in ``cut`` ("line", "row", "columns", "cells")."""
    cut = set() if cut is None else cut
    spent = [0]
    reader = csv.reader(capped_lines(text, spent, cut), delimiter=delimiter)
    out: list[list[str]] = []
    cells = 0
    try:
        for row in reader:
            spent[0] = 0
            if len(row) > MAX_COLUMNS:
                del row[MAX_COLUMNS:]
                cut.add("columns")
            out.append(row)
            cells += len(row)
            if cells > MAX_CELLS:
                cut.add("cells")
                break
            if max_rows and len(out) >= max_rows:
                break
    except csv.Error as exc:
        if "field larger than field limit" in str(exc):
            raise ValueError("a quoted cell is never closed, or is larger than "
                             f"{csv.field_size_limit():,} characters - check the quotes in the file") from None
        raise ValueError(f"this is not a CSV file Connecto can read ({exc})") from None
    return out


def _known_header(nk: str) -> bool:
    if not nk:
        return False
    if _URL_HEADER.search(nk) or privacy.is_contact_field(nk) or nk in LOCATION_PARTS or nk in ("connected_on",):
        return True
    return any(nk in names for names in ALIASES.values())


def _find_header(table: list[list[str]]) -> tuple[int, int]:
    """(row index, recognised columns) of the header row. LinkedIn's Connections export has notes above it."""
    best = (0, 0)
    for i, row in enumerate(table[:SCAN_ROWS]):
        # a cell that is a URL, an e-mail address or a sentence is data, however much it mentions "linkedin"
        cells = [norm_key(c) if len(c) <= 40 and not re.search(r"[/@]|https?:", c) else "" for c in row]
        if sum(1 for c in cells if c) < 2:
            continue
        hits = sum(1 for c in cells if _known_header(c))
        if hits >= 2:
            return i, hits
        if hits > best[1]:
            best = (i, hits)
    if best[1] == 0:       # nothing recognisable: the first row with at least two cells
        for i, row in enumerate(table[:SCAN_ROWS]):
            if sum(1 for c in row if c.strip()) >= 2:
                return i, 0
    return best


def _sniff(text: str) -> tuple[str, list[list[str]]]:
    """Pick the delimiter that yields the most header-like row; comma when nothing stands out."""
    sample = text[:60_000]
    best: tuple[tuple[int, int], str, list[list[str]]] | None = None
    for d in DELIMITERS:
        try:
            table = parse_csv(sample, d, max_rows=SCAN_ROWS + 1)
        except (csv.Error, ValueError):
            continue
        idx, hits = _find_header(table)
        score = (hits, len(table[idx]) if table and idx < len(table) else 0)
        if best is None or score > best[0]:
            best = (score, d, table)
    if best is None or best[0] == (0, 0):
        return ",", []
    return best[1], best[2]


def _unique_keys(header: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    keys = []
    for i, h in enumerate(header):
        nk = norm_key(h) or f"col_{i + 1}"
        seen[nk] = seen.get(nk, 0) + 1
        keys.append(nk if seen[nk] == 1 else f"{nk}_{seen[nk]}")
    return keys


def _personal_url(cell: str) -> str:
    url = normalize_linkedin(cell.strip())
    if url:
        return url
    m = _PROFILE_URL.search(cell or "")
    return normalize_linkedin(m.group(0)) if m else ""


def _csv_priority(text: str) -> str:
    """The priority column's value as a plain number between 0 and MAX_CSV_PRIORITY - or '' when it is not a number."""
    try:
        number = float(text)
    except ValueError:
        return ""
    if number != number:                       # NaN
        return ""
    return text if 0 <= number <= MAX_CSV_PRIORITY else str(MAX_CSV_PRIORITY if number > 0 else 0)


# ----------------------------------------------------------------------------- the reader
def read_csv_rows(path: str | os.PathLike, columns: dict | None = None,
                  delimiter: str | None = None) -> tuple[list[dict], list[str]]:
    """Parse a CSV into person rows (``make_person`` dicts) and human-readable notes about what was skipped.

    Raises OSError / ValueError / csv.Error when the file cannot be read at all.
    """
    text = read_csv_text(path)
    if delimiter:
        sniffed = parse_csv(text[:60_000], delimiter, max_rows=SCAN_ROWS + 1)
    else:
        delimiter, sniffed = _sniff(text)
    cut: set[str] = set()
    table = parse_csv(text, delimiter, max_rows=MAX_ROWS + SCAN_ROWS + 2, cut=cut)
    notes: list[str] = []
    if "line" in cut:
        notes.append(f"a line of more than {MAX_LINE_CHARS:,} characters was cut short")
    if "columns" in cut:
        notes.append(f"only the first {MAX_COLUMNS} columns of a row were read")
    if "cells" in cut:
        notes.append(f"the file is very large: reading stopped after {len(table):,} rows ({MAX_CELLS:,} cells)")
    if "row" in cut:
        notes.append(f"a single row of more than {MAX_ROW_CHARS:,} characters stopped the reading there")
    if not table:
        return [], notes + ["the file is empty"]

    h_idx, hits = _find_header(sniffed or table)
    header_row = table[h_idx] if h_idx < len(table) else []
    headerless = hits == 0 and any(_personal_url(c) for c in header_row)
    if headerless:
        header, data_rows = [f"col_{i + 1}" for i in range(len(header_row))], table
        notes.append("no header row found - read every line as a person")
    else:
        header, data_rows = header_row, table[h_idx + 1:]
    keys = _unique_keys(header)
    if len(data_rows) > MAX_ROWS:
        data_rows = data_rows[:MAX_ROWS]
        notes.append(f"only the first {MAX_ROWS} rows were read")

    # ---- which column plays which part
    role_idx: dict[str, int] = {}
    for role, names in ALIASES.items():
        for name in names:
            if name in keys:
                role_idx[role] = keys.index(name)
                break
    forced_url: list[int] = []
    for field_name, wanted in (columns or {}).items():
        want = str(wanted or "").strip()
        field_key = norm_key(field_name)
        if not want or not field_key:
            continue
        found = next((i for i, h in enumerate(header) if h.strip().lower() == want.lower() or norm_key(h) == norm_key(want)), None)
        if found is None:
            notes.append(f"column {want!r} (for {field_key}) was not found in the file")
        elif field_key == "linkedin_url":
            forced_url.append(found)
        else:
            role_idx[field_key] = found
    url_cols = forced_url or sorted(
        (i for i, k in enumerate(keys) if _URL_HEADER.search(k)),
        key=lambda i: (0 if _PERSONAL_HEADER.search(keys[i]) and not _COMPANYISH_HEADER.search(keys[i]) else 1, i))
    part_idx = [keys.index(k) for k in LOCATION_PARTS if k in keys] if "location" not in role_idx else []
    dropped = [header[i].strip() or keys[i] for i, k in enumerate(keys) if privacy.is_contact_field(k)]
    if dropped:
        notes.append("contact columns ignored and never stored: " + ", ".join(dropped))
    consumed = set(role_idx.values()) | set(part_idx)
    extra_cols = [i for i, k in enumerate(keys) if i not in consumed and not privacy.is_contact_field(k)][:MAX_EXTRA_COLUMNS]

    counts = {"no_url": 0, "non_personal": 0, "no_name": 0, "dup": 0}
    rows: list[dict] = []
    seen: set[str] = set()
    for raw in data_rows:
        if not any(c.strip() for c in raw):
            continue

        def cell(i: int | None) -> str:
            return clean_text(raw[i]) if i is not None and i < len(raw) else ""

        url, url_col = "", None
        for i in url_cols:
            url = _personal_url(cell(i)) if i < len(raw) else ""
            if url:
                url_col = i
                break
        if not url:
            for i, c in enumerate(raw):
                url = _personal_url(c)
                if url:
                    url_col = i
                    break
        if not url:
            counts["non_personal" if any(_ANY_LINKEDIN.search(c or "") for c in raw) else "no_url"] += 1
            continue

        first, last, full = cell(role_idx.get("first_name")), cell(role_idx.get("last_name")), cell(role_idx.get("full_name"))
        location = cell(role_idx.get("location"))
        if not location and part_idx:
            parts: list[str] = []
            for i in part_idx:
                v = cell(i)
                if v and v not in parts:
                    parts.append(v)
            location = ", ".join(parts)
        fields: dict[str, str] = {"full_name": full, "first_name": first, "last_name": last,
                                  "company": cell(role_idx.get("company")), "title": cell(role_idx.get("title")),
                                  "headline": cell(role_idx.get("headline")), "location": location}
        for i in extra_cols:
            if i == url_col and not re.search(r"\w", _PROFILE_URL.sub("", raw[i] if i < len(raw) else "")):
                continue                      # the column that only held the profile URL: it is already the person's URL
            v = cell(i)
            if v:
                k = keys[i]
                fields["csv_" + k if k in RESERVED or k.startswith("me_") else k] = v
        if fields.get("connected_on"):
            fields["degree"] = "1st"          # only LinkedIn's own Connections export has this column
        if "priority" in fields:
            fields["priority"] = _csv_priority(fields["priority"])
        person = make_person(linkedin_url=url, **{k: v for k, v in fields.items() if v})
        if not person:
            counts["no_url"] += 1
            continue
        key = person_key(person)
        if key in seen:
            counts["dup"] += 1
            continue
        seen.add(key)
        if not person.get("full_name"):
            counts["no_name"] += 1
        rows.append(person)

    def plural(n: int, text: str) -> str:
        return f"{n} row{'s' if n != 1 else ''} {text}"

    if counts["no_url"]:
        notes.append(plural(counts["no_url"], "had no LinkedIn profile URL"))
    if counts["non_personal"]:
        notes.append(plural(counts["non_personal"], "only had a company, school or Sales Navigator link, not a personal profile"))
    if counts["no_name"]:
        notes.append(plural(counts["no_name"], ("has" if counts["no_name"] == 1 else "have") + " no name, so notes can't be personalised for them"))
    if counts["dup"]:
        notes.append(plural(counts["dup"], "repeated a person already in the file"))
    return rows, notes


# ----------------------------------------------------------------------------- the source
class CSVSource(Source):
    type = "csv"

    def __init__(self, spec: dict, ctx):
        super().__init__(spec, ctx)
        self.path_text = str(spec.get("path") or "").strip()
        cols = spec.get("columns")
        self.columns = dict(cols) if isinstance(cols, dict) else {}
        delim = spec.get("delimiter")
        self.delimiter = "\t" if str(delim or "").lower() in ("tab", "\\t") else (str(delim)[:1] if delim else None)

    @property
    def source_id(self) -> str:
        base = re.split(r"[\\/]", self.path_text.rstrip("\\/"))[-1] if self.path_text else ""
        return f"csv:{base}" if base else "csv"

    def owns(self, row: dict) -> bool:
        sid = str(row.get("source_id") or "")
        return not sid.startswith("csv:") or sid == self.source_id

    def describe(self) -> str:
        return f"CSV file {self.path_text or '(no path set)'}"

    def resolve_path(self) -> Path | None:
        """~ is expanded; a relative path is looked for in <home>/data/imports first, then the current folder."""
        if not self.path_text:
            return None
        p = Path(os.path.expanduser(self.path_text))
        if p.is_absolute():
            return p if p.exists() else None
        for base in (self.ctx.home.imports_dir, Path.cwd()):
            if (base / p).exists():
                return base / p
        return None

    def fetch(self, sink: Sink) -> None:
        report = sink.report
        if not self.path_text:
            report.errors.append("this CSV source has no `path` set")
            return
        path = self.resolve_path()
        if path is None:
            report.errors.append(f"file not found: {self.path_text} (looked in {self.ctx.home.imports_dir} and the "
                                 "current folder)")
            return
        try:
            rows, notes = read_csv_rows(path, self.columns, self.delimiter)
        except (OSError, ValueError, csv.Error) as exc:
            report.errors.append(f"could not read {path.name}: {exc}")
            return
        report.notes.extend(notes)
        sink.add(rows)
        self.log(f"CSV {path.name}: {len(rows)} people")
