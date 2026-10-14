"""The documentation must stay true: every command it shows exists, every link leads somewhere, the numbers match the code."""
import argparse
import re
import shlex
from pathlib import Path

import pytest
import yaml

from connecto import cli, limits

ROOT = Path(__file__).resolve().parent.parent
PAGES = sorted([*ROOT.glob("*.md"), *ROOT.glob("docs/*.md"), *ROOT.glob("examples/*.md"), *ROOT.glob("skills/*/SKILL.md"),
                *ROOT.glob(".github/**/*.md")])
PROG = cli.build_parser().prog
FENCE = re.compile(r"^```[\w-]*\n(.*?)^```", re.S | re.M)
INLINE = re.compile(r"`([^`\n]+)`")
LINK = re.compile(r"\]\(([^)\s]+)\)")
ALIASES = {"status"}                                       # `status` is another name for `report`


def parser_facts():
    parser = cli.build_parser()
    commands = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction)).choices
    return parser, commands


def option_table(sp):
    """option string -> the values it accepts (None when it takes any value or none)."""
    table = {}
    for action in sp._actions:
        for flag in action.option_strings:
            table[flag] = list(action.choices) if action.choices else None
    return table


def shown_commands(text):
    """Every `<prog> ...` command in the code blocks and inline code of a page, as (the text, its tokens after the name)."""
    spans = [line for block in FENCE.findall(text) for line in block.splitlines()]
    spans += INLINE.findall(FENCE.sub("", text))
    for span in spans:
        span = re.sub(r"<[^>]*>", "X", span.replace("\\|", "|"))                  # <your folder> and <profile link> are placeholders
        for found in re.finditer(r"(?<![\w./-])" + re.escape(PROG) + r"\s+(\S.*)", span):
            if "#" in span[:found.start()]:                                         # a comment such as the cron markers, not a command
                continue
            try:
                tokens = shlex.split(found.group(1), comments=True)
            except ValueError:                                                      # a quote that closes elsewhere: prose, not a command
                continue
            for stop in ("&&", ";", "|", ">"):
                if stop in tokens:
                    tokens = tokens[:tokens.index(stop)]
            if tokens:
                yield span.strip(), tokens


def problems_with(tokens):
    parser, commands = parser_facts()
    top = option_table(parser)
    i = 0
    while i < len(tokens) and tokens[i].startswith("-"):                            # connecto --home X run ...
        flag = tokens[i].split("=")[0]
        if flag not in top:
            return [f"unknown option {flag}"]
        i += 1 + (0 if "=" in tokens[i] or flag in ("--version", "-h", "--help") else 1)
    if i >= len(tokens):
        return []
    name = tokens[i]
    if name not in commands:
        return [f"there is no command {name!r}"]
    sp = commands[name]
    options = {**top, **option_table(sp)}
    found = []
    choices = next((a.choices for a in sp._actions if not a.option_strings and a.choices), None)
    after = tokens[i + 1:]
    if choices and after and not after[0].startswith("-"):
        bad = [part for part in after[0].split("|") if part not in choices and part != "X"]
        if bad:
            found.append(f"{name}: {bad[0]!r} is not one of {', '.join(choices)}")
    for j, token in enumerate(after):
        if not token.startswith("--"):
            continue
        flag = token.split("=")[0]
        if flag not in options:
            found.append(f"{name}: unknown option {flag}")
        elif options[flag] and j + 1 < len(after) and not after[j + 1].startswith("-"):
            value = token.split("=", 1)[1] if "=" in token else after[j + 1]
            if value not in options[flag] and value != "X":
                found.append(f"{name}: {flag} {value!r} is not one of {', '.join(map(str, options[flag]))}")
    return found


def test_the_pages_are_found():
    names = {p.relative_to(ROOT).as_posix() for p in PAGES}
    assert {"README.md", "CONTRIBUTING.md", "SECURITY.md", "CLAUDE.md", "docs/safety.md", "examples/README.md"} <= names
    assert any(n.startswith("skills/") for n in names)


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_every_command_a_page_shows_exists_with_the_options_it_uses(page):
    wrong = []
    for span, tokens in shown_commands(page.read_text(encoding="utf-8")):
        wrong += [f"{span!r}: {problem}" for problem in problems_with(tokens)]
    assert not wrong, "\n".join(wrong)


def test_the_checker_itself_notices_a_wrong_command_option_or_choice():
    assert problems_with(["run", "--dry-run", "--limit", "2"]) == []
    assert problems_with(["--home", "X", "schedule", "install|uninstall|status", "--times", "10:40"]) == []
    assert problems_with(["ai", "key", "--env", "NAME"]) == []
    assert problems_with(["fetch", "--only", "yc"]) == []
    assert problems_with(["launch"]) and problems_with(["run", "--dry-runs"]) and problems_with(["schedule", "remove"])
    assert problems_with(["fetch", "--only", "everything"]) and problems_with(["--unknown"])


def test_every_command_is_described_somewhere():
    _, commands = parser_facts()
    shown = {tokens[0] for page in PAGES for _, tokens in shown_commands(page.read_text(encoding="utf-8"))
             if tokens and tokens[0] in commands}
    assert set(commands) - ALIASES - shown == set(), "commands no page mentions"


def slug(heading: str) -> str:
    """GitHub's anchor for a heading: lower case, punctuation dropped, spaces turned into hyphens."""
    text = re.sub(r"[^\w\- ]", "", heading.strip().lower().replace("`", ""))
    return text.replace(" ", "-")


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_every_link_leads_somewhere(page):
    text = FENCE.sub("", page.read_text(encoding="utf-8"))
    broken = []
    for target in LINK.findall(text):
        if re.match(r"(https?:|mailto:)", target):
            continue
        path, _, anchor = target.partition("#")
        destination = (page.parent / path).resolve() if path else page
        if not destination.exists():
            broken.append(target)
        elif anchor and destination.suffix == ".md":
            headings = re.findall(r"^#{1,6}\s+(.+?)\s*$", destination.read_text(encoding="utf-8"), re.M)
            if anchor not in {slug(h) for h in headings}:
                broken.append(target)
    assert not broken, f"links that lead nowhere: {broken}"


def cells(text: str, label: str):
    row = next(line for line in text.splitlines() if re.match(rf"\|\s*{label}", line))
    return [c.strip() for c in row.strip().strip("|").split("|")]


@pytest.mark.parametrize("name", ["README.md", "docs/safety.md"])
def test_the_limits_in_the_tables_are_the_limits_in_the_code(name):
    text = (ROOT / name).read_text(encoding="utf-8")
    hard, default = limits.HARD, limits.DEFAULTS
    day, week, run = cells(text, "Invitations per day"), cells(text, r"Invitations per (?:rolling )?(?:week|7 days)"), cells(text, "Invitations per run")
    pause, errors = cells(text, "Pause between profiles"), cells(text, "Errors in a row")
    assert day[1].startswith(f"{default['daily'][0]}-{default['daily'][1]}") and day[2] == str(hard["daily_max"])
    assert week[1] == str(default["weekly"]) and week[2] == str(hard["weekly_max"])
    assert run[1] == f"{default['per_run'][0]}-{default['per_run'][1]}" and run[2] == str(hard["per_run_max"])
    assert str(hard["delay_min_seconds"]) in pause[2] and f"{default['delay'][0]}-{default['delay'][1]}" in pause[1].replace("1-3 minutes", "60-180")
    assert errors[1] == str(default["stop_after_errors"]) and errors[2] == str(hard["errors_stop_max"])


def test_the_search_and_yc_ceilings_in_the_docs_are_the_ones_in_the_code():
    safety = (ROOT / "docs/safety.md").read_text(encoding="utf-8")
    pages = cells(safety, "LinkedIn search pages")
    assert f"{limits.HARD['search_pages_per_run']} / {limits.HARD['search_pages_per_day']}" in pages[2]
    workers = cells(safety, "Parallel requests")
    assert workers[1] == workers[2] == str(limits.HARD["yc_workers_max"])


def test_the_skill_is_well_formed_and_keeps_the_ground_rules():
    for path in ROOT.glob("skills/*/SKILL.md"):
        text = path.read_text(encoding="utf-8")
        _, front, body = text.split("---", 2)
        meta = yaml.safe_load(front)
        assert meta["name"] == path.parent.name == PROG
        assert 40 < len(meta["description"]) <= 1024 and "<" not in meta["description"] and ">" not in meta["description"]
        for rule in ("Never send", "Never handle credentials", "Never take an API key in chat", "Never raise"):
            assert rule in body, rule


def test_the_notes_for_coding_agents_keep_their_ground_rules():
    text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    for rule in ("Never perform a real send", "Never handle LinkedIn credentials", "Never raise the safety ceilings",
                 "Keys live in `.env` only", "No stealth", "stop and ask", "never touch the real internet"):
        assert rule.lower() in text.lower(), rule
