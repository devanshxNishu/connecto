"""The setup page in a real (headless) Chromium, against the real server on 127.0.0.1.

What is checked here is what only a browser can tell: the page loads without a single console error or CSP violation, the
first-run gate and the send dialog work, edits are saved and survive a reload, scraped names can never run as code, the
colours are readable in light and dark mode, and nothing overflows on a phone. LinkedIn is never contacted; jobs run a
stand-in for the runner.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
from playwright.sync_api import expect, sync_playwright

from connecto import config as cfgmod
from connecto import runner
from connecto.store import Store

import fake_ai_server as fake
from helpers import add_people, make_cfg, make_home, person
from ui_helpers import LINK, SECRET, start_server, stop_server

pytestmark = pytest.mark.browser

READY = {"me": {"first_name": "Asha", "about": "a product designer"}, "acknowledged_risks": True}
TABS = ["Start", "People", "Message", "Limits & schedule", "History", "Health"]
DRY = "Dry run (2 profiles, sends nothing)"
SEND = "Send a few now ..."


# ----------------------------------------------------------------------------- fixtures
@pytest.fixture(scope="module")
def chromium():
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.launch()
    except Exception as exc:  # noqa: BLE001
        pw.stop()
        pytest.skip(f"Playwright's Chromium is not available here: {exc}")
    yield browser
    browser.close()
    pw.stop()


@pytest.fixture()
def open_ui(chromium, tmp_path, clean_env):
    """open_ui(config=..., prepare=fn(home), width=..., scheme=...) -> a loaded page plus its server, home and any console
    problems. Everything is closed afterwards and a console error or uncaught exception fails the test."""
    sessions: list[SimpleNamespace] = []

    def factory(config: dict | None = None, *, prepare=None, width: int = 1100, height: int = 900, scheme: str = "light"):
        home = make_home(tmp_path / f"session{len(sessions)}")
        if config is not None:
            cfgmod.save(home, cfgmod.normalize(config)[0])
        if prepare:
            prepare(home)
        client = start_server(home, sign_in=False)                 # (the link is for the browser to open - once)
        ctx = chromium.new_context(viewport={"width": width, "height": height}, color_scheme=scheme, accept_downloads=True)
        page = ctx.new_page()
        page.set_default_timeout(10_000)
        expect.set_options(timeout=10_000)
        problems: list[str] = []
        page.on("console", lambda m: problems.append(f"console.{m.type}: {m.text}") if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: problems.append(f"uncaught: {e}"))
        page.goto(f"http://127.0.0.1:{client.port}/?t={LINK}")
        page.wait_for_selector("[role=tab]")
        client.session = next(c["value"] for c in ctx.cookies() if c["name"] == client.cookie_name)   # the test's own API calls
        allowed: list[str] = []
        session = SimpleNamespace(page=page, ctx=ctx, client=client, home=home, problems=problems, allowed=allowed,
                                  allow=allowed.append)
        sessions.append(session)
        return session

    yield factory
    for s in sessions:
        s.ctx.close()
        stop_server(s.client)
    for s in sessions:
        real = [p for p in s.problems if not any(a in p for a in s.allowed)]
        assert not real, "the page logged problems:\n" + "\n".join(real)


def tab(page, name: str):
    page.get_by_role("tab", name=name, exact=True).click()


# ============================================================================= first run
def test_the_first_screen_gates_everything_on_the_safety_note(open_ui):
    ui = open_ui()
    page = ui.page
    assert page.locator("[role=tab]").all_inner_texts() == TABS
    expect(page.locator("[role=tab][aria-selected=true]")).to_have_text("Start")
    go_on = page.get_by_role("button", name="Continue")
    expect(go_on).to_be_disabled()
    for name in (DRY, SEND, "Sign in to LinkedIn"):
        button = page.get_by_role("button", name=name)
        expect(button).to_be_disabled()
        assert "safety note" in button.get_attribute("title")
    page.check("#accept")
    expect(go_on).to_be_enabled()
    go_on.click()
    expect(page.get_by_text("Before you start - please read this")).to_have_count(0)
    assert ui.client.state()["config"]["acknowledged_risks"] is True
    # the next obstacle is named on the button itself
    expect(page.get_by_role("button", name=DRY)).to_be_disabled()
    assert "first name" in page.get_by_role("button", name=DRY).get_attribute("title")


def test_the_safety_note_is_shown_in_full_and_as_plain_text(open_ui):
    page = open_ui().page
    note = page.locator(".riskbox").inner_text()
    assert "User Agreement does not allow automated activity" in note and "never asks for or stores your LinkedIn password" in note


def test_the_start_tab_walks_you_through_the_steps(open_ui):
    page = open_ui(READY).page
    steps = page.locator("ol.steps li")
    assert steps.count() == 7
    expect(steps.nth(0)).to_have_class("done ")
    expect(page.locator("ol.steps li.next b")).to_have_text("Sign in to LinkedIn once")
    # nothing is switched on yet, so fetching means "add a source first" and takes you to the People tab
    page.locator("ol.steps li").nth(3).get_by_role("button", name="Add people").click()
    expect(page.locator("[role=tab][aria-selected=true]")).to_have_text("People")


# ============================================================================= saving settings
def test_settings_are_saved_and_survive_a_reload(open_ui):
    ui = open_ui({"acknowledged_risks": True})
    page = ui.page
    tab(page, "Message")
    page.fill("input[placeholder='Asha']", "Devansh")
    expect(page.locator("#savebar")).to_be_visible()
    page.get_by_role("button", name="Save changes").click()
    expect(page.locator("#savebar")).to_be_hidden()
    expect(page.locator("#toasts")).to_contain_text("Saved")
    assert ui.client.state()["config"]["me"]["first_name"] == "Devansh"
    page.reload()
    page.wait_for_selector("[role=tab]")
    tab(page, "Message")
    assert page.input_value("input[placeholder='Asha']") == "Devansh"


def test_discard_throws_unsaved_changes_away(open_ui):
    ui = open_ui(READY)
    page = ui.page
    tab(page, "Message")
    page.fill("input[placeholder='Asha']", "Someone Else")
    page.get_by_role("button", name="Discard").click()
    expect(page.locator("#savebar")).to_be_hidden()
    expect(page.locator("input[placeholder='Asha']")).to_have_value("Asha")           # (the page reloads the saved settings)
    assert ui.client.state()["config"]["me"]["first_name"] == "Asha"


def test_jobs_wait_for_unsaved_changes_to_be_saved(open_ui, monkeypatch):
    monkeypatch.setattr(runner, "run_send", lambda *a, **k: pytest.fail("must not start with unsaved changes"))
    ui = open_ui(READY)
    page = ui.page
    tab(page, "Message")
    page.fill("input[placeholder='Asha']", "Changed")
    tab(page, "Start")
    page.get_by_role("button", name=DRY).click()
    expect(page.locator("#toasts")).to_contain_text("Save your changes first")
    assert ui.client.state()["job"] is None


def test_the_templates_can_be_previewed_with_a_made_up_person(open_ui):
    page = open_ui(READY).page
    tab(page, "Message")
    first = page.locator(".tpl").first
    expect(first.locator(".note")).to_have_count(1)                          # the template only, nothing filled in yet
    page.get_by_role("button", name="Show examples with a sample person").click()
    expect(first.locator(".hint")).to_contain_text("Priya Shah at Acme Pay (made-up example)")
    expect(first.locator(".note").nth(1)).to_have_text(
        "Hi Priya, I'm Asha, a product designer. Your work at Acme Pay caught my eye - would love to connect and follow what you're building.")


def test_writing_your_own_template_and_seeing_it_filled_in(open_ui):
    ui = open_ui(READY)
    page = ui.page
    tab(page, "Message")
    page.get_by_role("button", name="Write my own").click()
    box = page.locator("textarea[aria-label='Template 1']")
    box.fill("Hi {first_name}, {me_first_name} here - loved what you're doing[[ at {company}]].")
    page.get_by_role("button", name="Preview with a sample person").click()
    expect(page.get_by_text("Hi Priya, Asha here - loved what you're doing at Acme Pay.")).to_be_visible()
    page.get_by_role("button", name="Save changes").click()
    expect(page.locator("#savebar")).to_be_hidden()
    assert ui.client.state()["config"]["message"]["templates"] == ["Hi {first_name}, {me_first_name} here - loved what you're doing[[ at {company}]]."]


# ============================================================================= choosing people
def test_choosing_sources_and_filters(open_ui):
    ui = open_ui(READY)
    page = ui.page
    tab(page, "People")
    page.select_option("#newsource", "yc")
    page.get_by_role("button", name="Add this source").click()
    page.fill("input[placeholder='S26, W26']", "S26, W26")
    page.fill("input[placeholder='Fintech, B2B']", "Fintech, B2B")
    page.select_option("#newsource", "csv")
    page.get_by_role("button", name="Add this source").click()
    expect(page.locator(".source")).to_have_count(2)
    page.fill("input[placeholder='fintech, payments']", "payments")
    page.get_by_role("button", name="Remove").nth(1).click()                    # the CSV source again
    expect(page.locator(".source")).to_have_count(1)
    page.get_by_role("button", name="Save changes").click()
    expect(page.locator("#savebar")).to_be_hidden()
    cfg = ui.client.state()["config"]
    assert [s["type"] for s in cfg["sources"]] == ["yc"]
    assert cfg["sources"][0]["batches"] == ["S26", "W26"] and cfg["sources"][0]["industries"] == ["Fintech", "B2B"]
    assert cfg["targets"]["keywords"] == ["payments"]
    page.reload()
    page.wait_for_selector("[role=tab]")
    tab(page, "People")
    assert page.input_value("input[placeholder='S26, W26']") == "S26, W26"


def test_a_source_can_be_switched_off_without_losing_its_settings(open_ui):
    ui = open_ui({**READY, "sources": [{"type": "yc", "batches": ["S26"]}]})
    page = ui.page
    tab(page, "People")
    page.get_by_role("checkbox", name="On", exact=True).uncheck()
    expect(page.locator(".source.off")).to_have_count(1)
    page.get_by_role("button", name="Save changes").click()
    expect(page.locator("#savebar")).to_be_hidden()
    source = ui.client.state()["config"]["sources"][0]
    assert source["enabled"] is False and source["batches"] == ["S26"]


def test_adding_people_by_hand_and_previewing_who_would_be_invited(open_ui):
    ui = open_ui(READY)
    page = ui.page
    tab(page, "People")
    page.fill("textarea[aria-label='People to add']",
              "Jane Doe, Acme Pay, CEO, https://www.linkedin.com/in/jane-doe-1a2b3c/\nhttps://www.linkedin.com/in/jdoe/")
    page.get_by_role("button", name="Add these people").click()
    expect(page.locator("#toasts")).to_contain_text("Added 2 people")
    expect(page.locator("#toasts")).to_contain_text("1 has no name")
    page.get_by_role("button", name="Show who would be invited").click()
    expect(page.locator("table")).to_contain_text("Jane Doe")
    expect(page.locator(".stat", has_text="waiting to be invited").locator("b")).to_have_text("2")
    assert "Acme Pay" in page.locator("table").inner_text() and "CEO" in page.locator("table").inner_text()
    expect(page.locator(".banner.warn")).to_contain_text("1 of the 2")                    # the nameless one: plain invite
    assert ui.client.state()["queue"]["queued"] == 2
    page.locator("summary", has_text="Never contact these people").click()
    page.fill("textarea[aria-label='People to never contact']", "https://www.linkedin.com/in/jdoe/")
    page.get_by_role("button", name="Never contact").click()
    expect(page.locator("#toasts")).to_contain_text("Will never contact 1 person")
    assert ui.client.state()["queue"]["queued"] == 1


def test_a_preview_is_not_shown_for_settings_you_have_not_saved(open_ui):
    page = open_ui(READY).page
    tab(page, "People")
    page.fill("input[placeholder='fintech, payments']", "x")
    page.get_by_role("button", name="Show who would be invited").click()
    expect(page.get_by_text("You have unsaved changes - save them to see an up-to-date preview.")).to_be_visible()


# ============================================================================= other people's words are only ever text
def test_names_from_other_peoples_pages_are_text_never_markup(open_ui):
    evil = "<img src=x onerror=\"window.__pwned=1\">"

    def prepare(home):
        add_people(home, [person("x-one", evil, company="<b>bold</b>", title="<script>window.__pwned=2</script>")])
        with Store(home.db_path) as st:
            st.record(person("x-two", evil), "sent", note=f"Hi {evil}")
            st.record(person("x-three", "Third"), "error", detail="<svg onload=window.__pwned=3>")

    ui = open_ui(READY, prepare=prepare)
    page = ui.page
    tab(page, "People")
    page.get_by_role("button", name="Show who would be invited").click()
    expect(page.locator("table")).to_contain_text("<img src=x")
    tab(page, "History")
    expect(page.get_by_text("Recent invitations")).to_be_visible()
    expect(page.locator("table")).to_contain_text("<svg onload")                 # (no runs yet, so this is the only table)
    assert page.evaluate("window.__pwned") is None
    assert page.locator("main img, main b, main script, main svg").count() == 0


def test_an_error_message_from_the_server_is_shown_as_text(open_ui):
    ui = open_ui(READY)
    page = ui.page
    ui.allow("status of 400")                       # the server's refusal is logged by Chromium as a failed resource: expected
    tab(page, "People")
    page.fill("textarea[aria-label='People to add']", "<img src=x onerror=window.__pwned=1>")
    page.get_by_role("button", name="Add these people").click()
    expect(page.locator("#toasts")).to_contain_text("no LinkedIn profile link found")
    assert page.evaluate("window.__pwned") is None and page.locator("#toasts img").count() == 0


# ============================================================================= limits
def test_numbers_beyond_the_safety_ceilings_are_pulled_back_and_you_are_told(open_ui):
    ui = open_ui(READY)
    page = ui.page
    tab(page, "Limits & schedule")
    page.locator("[aria-label='Invitations per day'] input").nth(1).fill("99")
    page.get_by_label("Invitations per rolling 7 days (most)").fill("5000")
    page.get_by_role("button", name="Save changes").click()
    expect(page.locator("#toasts")).to_contain_text("Saved, with notes")
    assert page.locator("[aria-label='Invitations per day'] input").nth(1).input_value() == "30"
    assert page.get_by_label("Invitations per rolling 7 days (most)").input_value() == "100"
    cfg = ui.client.state()["config"]
    assert cfg["limits"]["daily"][1] == 30 and cfg["limits"]["weekly"] == 100


def test_active_hours_and_days_are_editable(open_ui):
    ui = open_ui(READY)
    page = ui.page
    tab(page, "Limits & schedule")
    page.get_by_role("checkbox", name="Sun").check()
    page.get_by_role("checkbox", name="Mon").uncheck()
    page.locator("input[aria-label='From']").fill("08:15")
    page.get_by_role("button", name="Save changes").click()
    expect(page.locator("#savebar")).to_be_hidden()
    schedule = ui.client.state()["config"]["schedule"]
    assert "sun" in schedule["active_days"] and "mon" not in schedule["active_days"]
    assert schedule["active_hours"][0] == "08:15"
    page.get_by_role("checkbox", name="Only send between").uncheck()
    page.get_by_role("button", name="Save changes").click()
    expect(page.locator("#savebar")).to_be_hidden()
    assert ui.client.state()["config"]["schedule"]["active_hours"] == []


def test_a_change_made_while_a_save_is_finishing_is_not_thrown_away(open_ui):
    """After a save the page reloads the state from the server. A change made while that answer is still on its way (held
    here on purpose) used to be overwritten by it - the bar offering to save it stayed, but what it saved was the old value."""
    ui = open_ui(READY)
    page = ui.page
    tab(page, "Limits & schedule")
    holding, held = {"on": True}, []

    def reload_of_the_state(route):
        held.append(route) if holding["on"] else route.continue_()

    page.route("**/api/state", reload_of_the_state)               # hold the reload of the state that follows a save
    page.get_by_role("checkbox", name="Sun").check()
    page.get_by_role("button", name="Save changes").click()
    expect(page.locator("#savebar")).to_be_hidden()               # saved; the page is waiting for the reload now
    assert "sun" in ui.client.state()["config"]["schedule"]["active_days"]
    page.get_by_role("checkbox", name="Only send between").uncheck()          # four changes while it waits
    page.get_by_role("checkbox", name="Tue").uncheck()
    page.get_by_role("checkbox", name="Wed").uncheck()
    page.locator("input[aria-label='Run times']").fill("07:15, 19:45")
    expect(page.locator("#savebar")).to_be_visible()
    holding["on"] = False
    for route in held:
        route.continue_()
    expect(page.locator("#toasts")).to_contain_text("Saved")
    expect(page.get_by_role("checkbox", name="Only send between")).not_to_be_checked()
    expect(page.get_by_role("checkbox", name="Tue")).not_to_be_checked()
    expect(page.get_by_role("checkbox", name="Wed")).not_to_be_checked()
    expect(page.locator("input[aria-label='Run times']")).to_have_value("07:15, 19:45")
    expect(page.locator("#savebar")).to_be_visible()              # the changes are still waiting to be saved ...
    page.get_by_role("button", name="Save changes").click()
    expect(page.locator("#savebar")).to_be_hidden()
    schedule = ui.client.state()["config"]["schedule"]            # ... and what is saved is what the person chose
    assert schedule["active_hours"] == [] and "sun" in schedule["active_days"]
    assert "tue" not in schedule["active_days"] and "wed" not in schedule["active_days"] and "thu" in schedule["active_days"]
    assert schedule["times"] == ["07:15", "19:45"]


def test_daily_runs_can_be_turned_on_and_off_from_the_page(open_ui, monkeypatch):
    from connecto import scheduler
    state = {"on": False, "times": None}

    def install(home, times, **kw):
        state.update(on=True, times=[f"{h:02d}:{m:02d}" for h, m in scheduler.parse_times(times)])
        return scheduler.SchedResult(True, ["Daily runs are on."])

    def uninstall(home, **kw):
        state["on"] = False
        return scheduler.SchedResult(True, ["Daily runs are off."])

    monkeypatch.setattr(scheduler, "install", install)
    monkeypatch.setattr(scheduler, "uninstall", uninstall)
    monkeypatch.setattr(scheduler, "status", lambda home, **kw: {"installed": state["on"], "platform": "test", "mechanism": "fake",
                                                               "times": state["times"] or [], "message": "fake", "details": []})
    ui = open_ui(READY)
    page = ui.page
    tab(page, "Limits & schedule")
    page.locator("input[aria-label='Run times']").fill("07:30, 19:00")
    page.get_by_role("button", name="Save changes").click()
    expect(page.locator("#savebar")).to_be_hidden()
    page.get_by_role("button", name="Turn on daily runs").click()
    expect(page.get_by_role("button", name="Update the daily schedule")).to_be_visible()
    assert state["times"] == ["07:30", "19:00"]
    page.get_by_role("button", name="Turn off").click()
    expect(page.get_by_role("button", name="Turn on daily runs")).to_be_visible()


# ============================================================================= history and health
def test_history_lists_invitations_and_downloads_a_csv(open_ui):
    def prepare(home):
        with Store(home.db_path) as st:
            st.record(person("a-one", "Asha One"), "sent", note="Hi Asha!", note_kind="template")
            st.record(person("b-two", '=HYPERLINK("http://x","click")'), "sent", note="Hello")
            st.record(person("c-three", "Chi Three"), "already_pending")
            run = st.start_run("scheduled")
            st.finish_run(run, 2, 3, "run limit reached")

    page = open_ui(READY, prepare=prepare).page
    tab(page, "History")
    expect(page.locator("table").first).to_contain_text("run limit reached")
    expect(page.locator("table").nth(1)).to_contain_text("Asha One")
    assert page.locator("table").nth(1).locator("tbody tr").count() == 3
    with page.expect_download() as info:
        page.get_by_role("button", name="Download everything as CSV").click()
    download = info.value
    assert download.suggested_filename == "connecto-invitations.csv"
    text = open(download.path(), encoding="utf-8").read()
    assert text.splitlines()[0].startswith("status,full_name,company,linkedin_url,note")
    assert "Asha One" in text and "'=HYPERLINK" in text and ",=HYPERLINK" not in text and "\"=HYPERLINK" not in text


def test_the_health_tab_explains_what_to_fix(open_ui):
    page = open_ui().page
    tab(page, "Health")
    page.get_by_role("button", name="Check everything").click()
    expect(page.get_by_text("Not ready to send yet")).to_be_visible()
    expect(page.get_by_text("Fix the red items first.")).to_be_visible()
    expect(page.locator(".check-item").first).to_be_visible()
    assert page.locator(".check-item").count() >= 8
    assert "Fix" in page.locator(".check-item .chip").all_inner_texts()


# ============================================================================= the AI helper card
def test_turning_the_ai_helper_on_shows_its_settings_and_keeps_the_keyboard_focus(open_ui):
    page = open_ui(READY).page
    tab(page, "Message")
    expect(page.get_by_label("Which service")).to_have_count(0)
    page.get_by_role("checkbox", name="Use the AI helper").focus()
    page.keyboard.press("Space")
    expect(page.get_by_label("Which service")).to_be_visible()
    expect(page.get_by_role("link", name="Get a free key")).to_be_visible()
    assert page.evaluate("document.activeElement.getAttribute('data-path')") == "ai.enabled"      # focus did not fall to the page
    page.keyboard.press("Space")
    expect(page.get_by_label("Which service")).to_have_count(0)
    assert page.evaluate("document.activeElement.getAttribute('data-path')") == "ai.enabled"


def test_the_ai_helper_card_saves_a_key_without_ever_showing_it(open_ui):
    server = fake.FakeAIServer().start()
    try:
        ui = open_ui(READY)
        page = ui.page
        tab(page, "Message")
        page.get_by_role("checkbox", name="Use the AI helper").check()
        page.get_by_label("Which service").select_option("custom")
        page.get_by_label("Server address").fill(server.base_url)
        page.get_by_label("Name of the key setting").fill("MY_SERVER_API_KEY")
        page.get_by_label("API key").fill("sk-test-abcdefgh1234")
        page.get_by_role("button", name="Save key").click()
        expect(page.locator("#toasts")).to_contain_text("Key saved")
        expect(page.get_by_placeholder("A key is saved. Paste a new one to replace it.")).to_be_visible()
        assert "sk-test-abcdefgh1234" not in page.content()
        page.get_by_role("button", name="Test connection").click()
        expect(page.get_by_text("OK:", exact=False)).to_be_visible()
        assert "sk-test-abcdefgh1234" not in page.content()
        assert server.requests[-1].headers["authorization"] == "Bearer sk-test-abcdefgh1234"
        page.get_by_role("button", name="Save changes").click()
        expect(page.locator("#savebar")).to_be_hidden()
        ai = ui.client.state()["config"]["ai"]
        assert ai["enabled"] is True and ai["provider"] == "custom" and ai["base_url"] == server.base_url
        assert "sk-test-abcdefgh1234" not in ui.home.config_path.read_text()
        page.get_by_role("button", name="Remove key").click()
        expect(page.get_by_placeholder("Paste your key")).to_be_visible()
    finally:
        server.stop()


def test_describing_who_you_want_to_meet_adds_filters(open_ui):
    server = fake.FakeAIServer().start()
    try:
        ai = {"enabled": True, "provider": "custom", "base_url": server.base_url, "api_key_env": "MY_SERVER_API_KEY", "model": "m"}
        ui = open_ui({**READY, "ai": ai})
        ui.client.post("/api/ai/key", {"env": "MY_SERVER_API_KEY", "value": "sk-test-abcdefgh1234"})
        server.script(fake.ok('{"keywords": ["fintech"], "titles": ["founder"], "locations": ["India"], '
                              '"exclude_keywords": [], "search_queries": ["fintech founder india"]}'))
        page = ui.page
        page.reload()
        page.wait_for_selector("[role=tab]")
        tab(page, "People")
        page.get_by_label("Describe who you want to meet").fill("founders of fintech startups in India")
        page.get_by_role("button", name="Suggest filters").click()
        expect(page.get_by_role("button", name="Add to my filters")).to_be_visible()
        page.get_by_role("button", name="Add to my filters").click()
        page.get_by_role("button", name="Save changes").click()
        expect(page.locator("#savebar")).to_be_hidden()
        cfg = ui.client.state()["config"]
        assert cfg["targets"]["keywords"] == ["fintech"] and cfg["targets"]["locations"] == ["India"]
        assert any(s["type"] == "linkedin_search" and s["queries"] == ["fintech founder india"] for s in cfg["sources"])
    finally:
        server.stop()


# ============================================================================= jobs
def test_a_dry_run_shows_progress_then_the_result(open_ui, monkeypatch):
    release = threading.Event()

    def fake_send(home, cfg, opts):
        opts.log("Opening the first profile")
        release.wait(10)
        opts.log("Opening the second profile")
        return runner.RunResult(sent=2, attempted=2, stop_reason="dry run finished", dry_run=True)

    monkeypatch.setattr(runner, "run_send", fake_send)
    ui = open_ui(READY)
    page = ui.page
    page.get_by_role("button", name=DRY).click()
    expect(page.locator("#chips")).to_contain_text("dry run running")
    expect(page.locator("#joblog")).to_contain_text("Opening the first profile")
    expect(page.get_by_role("button", name="Stop")).to_be_visible()
    busy = page.get_by_role("button", name=SEND)
    expect(busy).to_be_disabled()
    assert "already running" in busy.get_attribute("title")
    release.set()
    expect(page.locator("#toasts")).to_contain_text("Dry run finished: 2 of 2 profiles could be invited. Nothing was sent.")
    expect(page.locator("#joblog")).to_contain_text("Opening the second profile")
    expect(page.locator("#chips")).not_to_contain_text("running")
    expect(page.get_by_role("button", name=SEND)).to_be_enabled()


def test_the_send_dialog_needs_a_deliberate_yes(open_ui, monkeypatch):
    seen = {}
    monkeypatch.setattr(runner, "run_send", lambda home, cfg, opts: seen.update(opts=opts) or runner.RunResult(
        sent=1, attempted=1, stop_reason="run limit reached"))
    ui = open_ui(READY)
    page = ui.page
    dialog = page.locator("dialog")

    def open_dialog():
        page.get_by_role("button", name=SEND).click()
        expect(dialog).to_be_visible()
        assert "Send real invitations now?" in dialog.inner_text()

    open_dialog()
    page.fill("#sendlimit", "2")
    page.press("#sendlimit", "Enter")                                   # Enter must not confirm (or cancel) anything
    expect(dialog).to_be_visible()
    dialog.get_by_role("button", name="Cancel").click()
    expect(dialog).to_be_hidden()
    open_dialog()
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    assert ui.client.state()["job"] is None and not seen
    open_dialog()
    page.fill("#sendlimit", "2")
    dialog.get_by_role("button", name="Send now").click()
    expect(page.locator("#toasts")).to_contain_text("Sent 1 of 1")
    opts = seen["opts"]
    assert opts.dry_run is False and opts.limit == 2 and opts.ignore_hours is False and opts.scheduled is False


def test_stop_asks_a_running_job_to_finish_its_current_profile(open_ui, monkeypatch):
    started = threading.Event()

    def slow(home, cfg, opts):
        started.set()
        while not opts.stop():
            time.sleep(0.01)
        opts.log("Stop noticed")
        return runner.RunResult(stop_reason="stopped by you")

    monkeypatch.setattr(runner, "run_send", slow)
    page = open_ui(READY).page
    page.get_by_role("button", name=DRY).click()
    expect(page.get_by_role("button", name="Stop")).to_be_visible()
    assert started.wait(5)
    page.get_by_role("button", name="Stop").click()
    expect(page.locator("#toasts")).to_contain_text("Dry run stopped")
    expect(page.locator("#joblog")).to_contain_text("Stop noticed")


def test_a_run_that_needs_you_is_shown_with_its_reason(open_ui, monkeypatch):
    monkeypatch.setattr(runner, "run_send", lambda home, cfg, opts: runner.RunResult(
        exit_code=runner.EXIT_NEEDS_YOU, stop_reason="LinkedIn asked for a security check"))
    page = open_ui(READY).page
    page.get_by_role("button", name=DRY).click()
    expect(page.locator("#toasts")).to_contain_text("needs you: LinkedIn asked for a security check")


def test_pausing_state_is_shown_and_can_be_resumed(open_ui):
    def prepare(home):
        with Store(home.db_path) as st:
            runner.set_pause(st, __import__("datetime").timedelta(days=2), "LinkedIn showed a limit warning")

    page = open_ui(READY, prepare=prepare).page
    expect(page.locator(".banner.warn")).to_contain_text("Sending is paused until")
    expect(page.locator(".banner.warn")).to_contain_text("LinkedIn showed a limit warning")
    expect(page.locator("#chips")).to_contain_text("Paused until")
    page.get_by_role("button", name="Resume").click()
    expect(page.locator(".banner.warn")).to_have_count(0)


# ============================================================================= quitting
def test_quit_while_a_run_is_going_says_so_and_keeps_the_page_until_the_run_has_stopped(open_ui, monkeypatch):
    started, finish = threading.Event(), threading.Event()

    def slow(home, cfg, opts):
        started.set()
        while not opts.stop():
            time.sleep(0.01)
        finish.wait(10)                                           # (finishing the profile it was on)
        opts.log("Stop noticed")
        return runner.RunResult(stop_reason="stopped by you")

    monkeypatch.setattr(runner, "run_send", slow)
    ui = open_ui(READY)
    ui.allow("409 (Conflict)")                                   # (the browser logs the refusal it is shown; the page handles it)
    page = ui.page
    page.get_by_role("button", name=DRY).click()
    expect(page.get_by_role("button", name="Stop")).to_be_visible()
    assert started.wait(5)
    page.locator("#quit").click()
    page.locator("dialog").get_by_role("button", name="Quit").click()
    expect(page.locator("#toasts")).to_contain_text("still running")
    expect(page.locator("#toasts")).to_contain_text("press Quit again")
    expect(page.get_by_text("Connecto has stopped")).to_have_count(0)
    assert page.locator("[role=tab]").all_inner_texts() == TABS and ui.client.thread.is_alive()   # the page is still the page
    finish.set()
    expect(page.locator("#toasts")).to_contain_text("Dry run stopped")
    page.locator("#quit").click()
    page.locator("dialog").get_by_role("button", name="Quit").click()
    expect(page.get_by_text("Connecto has stopped")).to_be_visible()
    ui.client.thread.join(timeout=5)
    assert not ui.client.thread.is_alive()


def test_the_link_signs_in_once_and_the_cookie_is_out_of_reach_of_the_pages_own_scripts(open_ui, chromium):
    ui = open_ui(READY)
    cookie = next(c for c in ui.ctx.cookies() if c["name"] == ui.client.cookie_name)
    assert cookie["httpOnly"] is True and cookie["sameSite"] == "Strict" and cookie["value"] not in (LINK, SECRET)
    assert ui.page.evaluate("document.cookie") == ""                       # a script on the page cannot read the session
    other = chromium.new_context()
    try:
        second = other.new_page()
        response = second.goto(f"http://127.0.0.1:{ui.client.port}/?t={LINK}")
        assert response.status == 403 and "works only once" in second.content()
    finally:
        other.close()
    ui.page.reload()                                                       # the first browser is not bothered by that
    ui.page.wait_for_selector("[role=tab]")


def test_quit_stops_the_server_and_says_so(open_ui):
    ui = open_ui(READY)
    page = ui.page
    page.locator("#quit").click()
    page.locator("dialog").get_by_role("button", name="Quit").click()
    expect(page.get_by_text("Connecto has stopped")).to_be_visible()
    ui.client.thread.join(timeout=5)
    assert not ui.client.thread.is_alive()


# ============================================================================= how it looks
def _contrast_report(page) -> list[tuple[str, float]]:
    """Contrast ratio of the text colour against the effective background for a sample of every kind of text on the page."""
    return page.evaluate("""() => {
      const main = document.querySelector('main');
      const samples = [
        ['body text', '<p>text</p>'], ['muted text', '<p class="muted">muted</p>'], ['hint', '<span class="hint">hint</span>'],
        ['default button', '<button class="btn">b</button>'], ['primary button', '<button class="btn primary">b</button>'],
        ['danger button', '<button class="btn danger">b</button>'], ['disabled-looking text', '<span class="hint">h</span>'],
        ['chip', '<span class="chip">c</span>'], ['ok chip', '<span class="chip ok">c</span>'], ['warn chip', '<span class="chip warn">c</span>'],
        ['bad chip', '<span class="chip bad">c</span>'], ['info chip', '<span class="chip info">c</span>'],
        ['warn banner', '<div class="banner warn">w</div>'], ['info banner', '<div class="banner info">i</div>'],
        ['bad banner', '<div class="banner bad">b</div>'], ['ok banner', '<div class="banner ok">o</div>'],
        ['stat label', '<div class="stat"><span>label</span></div>'], ['table header', '<table><thead><tr><th>h</th></tr></thead></table>'],
        ['note', '<div class="note">note</div>'], ['link', '<a href="#">link</a>'],
        ['toast', '<div class="toast">t</div>'], ['ok toast', '<div class="toast ok">t</div>'],
        ['warn toast', '<div class="toast warn">t</div>'], ['bad toast', '<div class="toast bad">t</div>'],
        ['log', '<pre class="log">log</pre>'],
      ];
      const parse = (c) => { const m = c.match(/rgba?\\(([^)]+)\\)/); const p = m[1].split(',').map((x) => parseFloat(x)); return {r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1}; };
      const lum = ({r, g, b}) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }; return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
      const bgOf = (el) => { for (let e = el; e; e = e.parentElement) { const c = parse(getComputedStyle(e).backgroundColor); if (c.a > 0.99) return c; } return parse('rgb(255,255,255)'); };
      const out = [];
      for (const [name, html] of samples) {
        const holder = document.createElement('section'); holder.className = 'card'; holder.innerHTML = html; main.append(holder);
        const el = holder.querySelector('p, span, button, div, th, a, pre') || holder;
        const target = name === 'table header' ? holder.querySelector('th') : (name === 'stat label' ? holder.querySelector('span') : el);
        const fg = parse(getComputedStyle(target).color), bg = bgOf(target);
        const a = lum(fg), b = lum(bg);
        out.push([name, (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)]);
        holder.remove();
      }
      return out;
    }""")


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_text_is_readable_in_light_and_dark_mode(open_ui, scheme):
    page = open_ui(READY, scheme=scheme).page
    weak = [(name, round(ratio, 2)) for name, ratio in _contrast_report(page) if ratio < 4.5]
    assert not weak, f"{scheme} mode: text below the 4.5:1 contrast of WCAG AA: {weak}"


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_page_follows_the_system_colour_scheme(open_ui, scheme):
    page = open_ui(READY, scheme=scheme).page
    background = page.evaluate("getComputedStyle(document.body).backgroundColor")
    assert background == ("rgb(15, 19, 25)" if scheme == "dark" else "rgb(245, 246, 248)")


def test_nothing_overflows_on_a_phone(open_ui):
    def prepare(home):
        add_people(home, [person(f"p-{i}", f"Averyveryverylongfirstname{i} Anextremelylonglastname", company="C" * 60,
                                 title="Chief of everything related to long titles and wrapping text") for i in range(4)])

    ui = open_ui({**READY, "sources": [{"type": "yc"}, {"type": "csv", "path": "/a/very/long/path/to/some/leads/file.csv"}]},
                 prepare=prepare, width=360, height=800)
    page = ui.page
    for name in TABS:
        tab(page, name)
        if name == "People":
            page.get_by_role("button", name="Show who would be invited").click()
            expect(page.locator("table")).to_be_visible()
        if name == "Health":
            page.get_by_role("button", name="Check everything").click()
            expect(page.locator(".check-item").first).to_be_visible()
        page.wait_for_timeout(150)
        overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        assert overflow <= 1, f"the {name} tab is {overflow}px wider than a 360px phone"


def test_every_setting_has_a_label_a_screen_reader_can_read(open_ui):
    config = {**READY, "sources": [{"type": t} for t in ("yc", "csv", "webpage", "linkedin_search")],
              "ai": {"enabled": True, "provider": "custom", "base_url": "http://127.0.0.1:9/v1", "api_key_env": "MY_SERVER_API_KEY"},
              "message": {"templates": ["Hi {first_name}"]}}
    page = open_ui(config).page
    unlabeled = []
    for name in TABS:
        tab(page, name)
        found = page.evaluate("""() => [...document.querySelectorAll('input, select, textarea')]
            .filter((el) => el.type !== 'hidden' && !(el.labels && el.labels.length) && !el.getAttribute('aria-label') && !el.getAttribute('aria-labelledby'))
            .map((el) => el.tagName.toLowerCase() + '[' + (el.getAttribute('data-path') || el.id || el.placeholder || el.type) + ']')""")
        unlabeled += [f"{name}: {f}" for f in found]
    assert not unlabeled, f"settings without a label: {unlabeled}"


def test_tabs_and_dialogs_are_reachable_with_the_keyboard(open_ui):
    page = open_ui(READY).page
    page.get_by_role("tab", name="Message", exact=True).focus()
    page.keyboard.press("Enter")
    expect(page.locator("[role=tab][aria-selected=true]")).to_have_text("Message")
    assert page.evaluate("document.activeElement.id") == "view"                   # focus moves to the content
    page.locator("#quit").focus()
    page.keyboard.press("Enter")
    expect(page.locator("dialog")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("dialog")).to_be_hidden()
