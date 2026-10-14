"""The recipes in examples/ are what people copy first, so they must keep working as the code changes."""
import json
import re
from pathlib import Path

import pytest
import yaml

from connecto import config as cfgmod
from connecto import notes
from connecto.manual import parse_people_text
from connecto.sources import SourceContext, build_source, read_csv_rows

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
CONFIGS = sorted(EXAMPLES.glob("*.yaml"))
PERSON = {"full_name": "Maya Example", "first_name": "Maya", "company": "Acme Pay", "title": "Co-founder & CEO"}
HOOK = "Congratulations on getting Acme Pay's payments API into production."
KEY_SHAPES = re.compile(r"AIza[0-9A-Za-z_-]{20,}|sk-[A-Za-z0-9]{20,}|gsk_[A-Za-z0-9]{20,}|BEGIN [A-Z ]*PRIVATE KEY")


def load(path: Path):
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict), f"{path.name} must be a YAML mapping"
    return cfgmod.normalize(raw)


def test_the_examples_exist():
    assert len(CONFIGS) >= 6
    assert (EXAMPLES / "leads.example.csv").exists() and (EXAMPLES / "people.example.txt").exists()


@pytest.mark.parametrize("path", CONFIGS, ids=lambda p: p.name)
def test_an_example_is_understood_exactly_as_written(path):
    cfg, warnings = load(path)
    assert warnings == [], f"{path.name}: something in it is unknown, ignored or clamped: {warnings}"
    assert cfg["sources"], f"{path.name} shows no source"
    # Nothing is switched on for the person: the only thing left to do is to read and accept the risk note themselves.
    assert cfg["acknowledged_risks"] is False
    assert len(cfgmod.problems(cfg)) == 1 and "risk" in cfgmod.problems(cfg)[0].lower()
    assert cfg["me"]["first_name"], f"{path.name} should show a first name to replace"


@pytest.mark.parametrize("path", CONFIGS, ids=lambda p: p.name)
def test_an_example_holds_no_secret_and_never_points_at_one(path):
    text = path.read_text(encoding="utf-8")
    assert not KEY_SHAPES.search(text)
    raw = yaml.safe_load(text)
    assert not (raw.get("ai") or {}).get("api_key_env"), "an example should leave the key name to Connecto"
    assert "acknowledged_risks" not in raw and "headless" not in text


@pytest.mark.parametrize("path", CONFIGS, ids=lambda p: p.name)
def test_every_source_in_an_example_can_be_built(path):
    cfg, _ = load(path)
    ctx = SourceContext(home=None, cfg=cfg, store=None, limits=None)          # building a source touches nothing
    for spec in cfg["sources"]:
        assert build_source(spec, ctx).describe()


@pytest.mark.parametrize("path", CONFIGS, ids=lambda p: p.name)
def test_every_template_in_an_example_is_valid_and_fits_the_note(path):
    cfg, _ = load(path)
    limit = cfgmod.note_limit(cfg)
    ctx = notes.build_context(PERSON, cfg["me"], {"hook": HOOK})
    for template in cfgmod.templates(cfg):
        assert notes.template_problems(template) == [], template
        # "fits" must mean fits whole: Connecto would otherwise cut a too-long note short, with an ellipsis, in front of a person.
        shortest = notes.min_length(template, ctx)
        assert shortest is not None and shortest <= limit, f"{path.name}: even without its optional parts it is {shortest} characters: {template}"
        text = notes.fit(template, ctx, limit)
        assert text and len(text) <= limit and not text.endswith("\u2026"), f"{path.name}: {template}"
        assert "{" not in text and "[[" not in text


def test_the_ai_examples_switch_the_helper_on_and_the_others_leave_it_off():
    for path in CONFIGS:
        cfg, _ = load(path)
        assert cfg["ai"]["enabled"] is path.name.startswith(("free-ai", "local-ai")), path.name
    gemini, _ = load(EXAMPLES / "free-ai-gemini.yaml")
    ollama, _ = load(EXAMPLES / "local-ai-ollama.yaml")
    assert gemini["ai"]["provider"] == "gemini" and ollama["ai"]["provider"] == "ollama"
    assert any(notes.uses_var(t, "hook") for t in gemini["message"]["templates"])


def test_the_sample_spreadsheet_is_read_and_its_email_addresses_are_dropped():
    rows, notes_about_the_file = read_csv_rows(EXAMPLES / "leads.example.csv")
    assert notes_about_the_file == ["contact columns ignored and never stored: Email"]       # it says so, too
    assert [r["full_name"] for r in rows] == ["Maya Example", "Jonas Sample", "Priya Placeholder"]
    assert rows[0]["company"] == "Acme Pay" and rows[0]["title"] == "Co-founder & CEO"
    assert all(r["linkedin_url"].startswith("https://www.linkedin.com/in/example-") for r in rows)
    assert "@" not in json.dumps(rows) and "example.com" not in json.dumps(rows)


def test_the_sample_list_of_people_is_read_without_complaints():
    people, problems = parse_people_text((EXAMPLES / "people.example.txt").read_text(encoding="utf-8"))
    assert problems == []
    assert [p.get("full_name") for p in people] == ["Maya Example", "Jonas Sample", ""]       # a bare link has no name to guess
    assert people[1]["company"] == "Northwind Labs" and people[1]["title"] == "Head of Design"
    assert people[2]["linkedin_url"].endswith("/in/example-priya/")


def test_the_examples_readme_lists_every_file_and_the_files_it_lists_exist():
    readme = (EXAMPLES / "README.md").read_text(encoding="utf-8")
    for path in EXAMPLES.iterdir():
        if path.name != "README.md":
            assert path.name in readme, f"{path.name} is not described in examples/README.md"
    for name in re.findall(r"\]\(([\w.-]+\.(?:yaml|csv|txt))\)", readme):
        assert (EXAMPLES / name).exists(), name
