"""wizard.py: the guided setup in the terminal, driven by scripted answers (no tty, no network except a fake AI server)."""
import os
import stat
from types import SimpleNamespace

import pytest

from connecto import config as cfgmod
from connecto import wizard
from connecto.paths import Home
from connecto.util import read_env_file

import fake_ai_server as fake

KEY = "AIzaSy-test-key-1234567890"


class Script:
    """Answers the wizard's questions by what the question says. ``answers`` maps a part of the question to the answer
    (a list is used up in order and its last item then repeats); a question nobody scripted gets Enter, i.e. the default.
    The menus ('Choose 1-5') are answered in the order they appear under the key "Choose"."""

    def __init__(self, answers: dict | None = None, secret: str = ""):
        self.answers = {k: (list(v) if isinstance(v, list) else [v]) for k, v in (answers or {}).items()}
        self.secret_answer = secret
        self.asked: list[str] = []
        self.said: list[str] = []

    def ask(self, prompt: str) -> str:
        self.asked.append(prompt)
        for part, queue in self.answers.items():
            if part.lower() in prompt.lower():
                return queue.pop(0) if len(queue) > 1 else queue[0]
        return ""

    def secret(self, prompt: str) -> str:
        self.asked.append(prompt)
        return self.secret_answer

    @property
    def console(self) -> wizard.Console:
        return wizard.Console(ask=self.ask, secret=self.secret, say=self.said.append)

    @property
    def output(self) -> str:
        return "\n".join(self.said)

    def asked_about(self, part: str) -> bool:
        return any(part.lower() in q.lower() for q in self.asked)


@pytest.fixture()
def home(tmp_path):
    return Home(tmp_path / "home")


def run(home, script, **kw):
    return wizard.run_wizard(home, script.console, **kw)


BASICS = {"Do you understand this": "y", "Your first name": "Asha", "One short line about you": "a product designer in Pune"}


# ============================================================================= the whole thing
def test_declining_the_risk_note_changes_nothing(home):
    script = Script({"Do you understand this": "n"})
    assert run(home, script) == 1
    assert not home.config_path.exists() and "nothing was changed" in script.output
    assert not script.asked_about("Your first name")                              # it stopped right there


def test_a_run_that_accepts_every_default_saves_a_ready_config_and_points_to_adding_people(home):
    script = Script(BASICS)
    assert run(home, script) == 0
    cfg = cfgmod.load(home)
    assert cfg["me"]["first_name"] == "Asha" and cfg["me"]["about"] == "a product designer in Pune"
    assert cfg["acknowledged_risks"] is True and cfg["sources"] == [] and cfg["ai"]["enabled"] is False
    assert cfg["message"]["plan"] == "free" and cfgmod.problems(cfg) == []
    assert "connecto login" in script.output and "connecto add <profile link>" in script.output
    assert "connecto fetch" not in script.output                                  # nothing to fetch yet


def test_a_full_run_with_a_source_filters_a_plan_and_a_template(home):
    script = Script({
        **BASICS,
        "building or looking for": "y", "What is it called": "NINA", "One short clause": "an AI stylist",
        "Choose": ["2", "1"],                                                     # premium, then the YC directory
        "own note template": "y", "Template (one line)": "Hi {first_name}, I'm {me_first_name}.",
        "Batches": "S26, W26", "Industries": "Fintech", "Regions": "India", "Words about what": "payments",
        "Add another source": "n",
        "Must mention any": "fintech", "Job title contains": "founder", "Never anyone": "recruiter",
    })
    assert run(home, script) == 0
    cfg = cfgmod.load(home)
    assert (cfg["me"]["project"], cfg["me"]["project_blurb"]) == ("NINA", "an AI stylist")
    assert cfg["message"]["plan"] == "premium" and cfg["message"]["templates"] == ["Hi {first_name}, I'm {me_first_name}."]
    (source,) = cfg["sources"]
    assert source["type"] == "yc" and source["batches"] == ["S26", "W26"] and source["industries"] == ["Fintech"]
    assert source["regions"] == ["India"] and source["keywords"] == ["payments"]
    assert cfg["targets"]["keywords"] == ["fintech"] and cfg["targets"]["titles"] == ["founder"]
    assert cfg["targets"]["exclude_keywords"] == ["recruiter"] and cfg["targets"]["locations"] == []
    assert "connecto fetch" in script.output


def test_running_it_again_keeps_the_current_answers_and_leaves_a_backup(home):
    first = Script({**BASICS, "Choose": ["1", "1"], "Add another source": "n", "Batches": "S26", "Must mention any": "fintech"})
    run(home, first)
    before = cfgmod.load(home)
    second = Script()                                                              # Enter all the way
    assert run(home, second) == 0
    assert cfgmod.load(home) == before
    assert home.config_path.with_suffix(".yaml.bak").exists()
    assert "Settings already exist" in second.output
    assert not second.asked_about("Do you understand this")                       # already accepted once
    assert second.asked_about("Add another?")                                     # it knows there is a source already


def test_assuming_the_risks_are_accepted_skips_that_question_only(home):
    script = Script({"Your first name": "Asha"})
    assert run(home, script, assume_risks_accepted=True) == 0
    assert not script.asked_about("Do you understand this") and cfgmod.load(home)["acknowledged_risks"] is True


# ============================================================================= sources
def test_a_csv_source_needs_a_file_that_exists(home, tmp_path):
    leads = tmp_path / "leads.csv"
    leads.write_text("linkedin_url\nhttps://www.linkedin.com/in/jane/\n")
    script = Script({**BASICS, "Choose": ["1", "2"], "Path of the CSV": [str(tmp_path / "nope.csv"), str(leads)],
                     "Add another source": "n"})
    # (the plan question comes first, so "1" is the free plan and "2" the CSV source)
    run(home, script)
    assert "That file was not found" in script.output
    assert cfgmod.load(home)["sources"] == [{"type": "csv", "enabled": True, "path": str(leads)}]


def test_a_csv_source_is_dropped_after_three_missing_files(home, tmp_path):
    script = Script({**BASICS, "Choose": ["1", "2"], "Path of the CSV": str(tmp_path / "nope.csv")})
    run(home, script)
    assert cfgmod.load(home)["sources"] == [] and script.output.count("That file was not found") == 3


def test_pressing_enter_instead_of_a_csv_path_skips_the_source(home):
    script = Script({**BASICS, "Choose": ["1", "2"], "Path of the CSV": ""})
    run(home, script)
    assert cfgmod.load(home)["sources"] == []


def test_a_web_page_source_takes_several_addresses(home):
    script = Script({**BASICS, "Choose": ["1", "3"], "Page address": "https://example.com/team, https://example.com/alumni",
                     "Add another source": "n"})
    run(home, script)
    (source,) = cfgmod.load(home)["sources"]
    assert source["type"] == "webpage" and source["urls"] == ["https://example.com/team", "https://example.com/alumni"]


def test_a_linkedin_search_source_takes_searches_and_addresses_until_a_blank_line(home):
    script = Script({**BASICS, "Choose": ["1", "4"],
                     "Search": ["head of design fintech india", "https://www.linkedin.com/search/results/people/?keywords=x", ""],
                     "Add another source": "n"})
    run(home, script)
    (source,) = cfgmod.load(home)["sources"]
    assert source["type"] == "linkedin_search" and source["queries"] == ["head of design fintech india"]
    assert source["urls"] == ["https://www.linkedin.com/search/results/people/?keywords=x"]


def test_several_sources_can_be_added_in_one_go(home):
    script = Script({**BASICS, "Choose": ["1", "1", "3"], "Add another source": ["y", "n"], "Page address": "https://example.com/team"})
    run(home, script)
    assert [s["type"] for s in cfgmod.load(home)["sources"]] == ["yc", "webpage"]


# ============================================================================= the building blocks
def test_a_menu_takes_numbers_names_and_enter_and_asks_again_on_nonsense():
    script = Script({"Choose": ["abc", "9", "0", "3"]})
    con = script.console
    options = [("a", "Apple"), ("b", "Banana"), ("c", "Cherry")]
    assert con.choose("Fruit?", options, "b") == "c"
    assert script.output.count("Please type one of the numbers above.") == 3
    assert wizard.Console(ask=lambda _: "", say=lambda _: None).choose("Fruit?", options, "b") == "b"
    assert wizard.Console(ask=lambda _: "a", say=lambda _: None).choose("Fruit?", options, "b") == "a"        # the name itself
    assert wizard.Console(ask=lambda _: "", say=lambda _: None).choose("Fruit?", options, "unknown") == "a"  # first one


def test_yes_no_questions_repeat_until_they_are_understood():
    answers = iter(["maybe", "perhaps", "YES"])
    said = []
    con = wizard.Console(ask=lambda _: next(answers), say=said.append)
    assert con.yes("Sure?", False) is True and said.count("Please answer y or n.") == 2
    assert wizard.Console(ask=lambda _: "", say=said.append).yes("Sure?", True) is True
    assert wizard.Console(ask=lambda _: "", say=said.append).yes("Sure?", False) is False
    assert wizard.Console(ask=lambda _: "N", say=said.append).yes("Sure?", True) is False


def test_text_questions_show_and_use_the_default():
    prompts = []
    con = wizard.Console(ask=lambda p: prompts.append(p) or "", say=lambda _: None)
    assert con.text("Name", "Asha") == "Asha" and prompts == ["Name [Asha]: "]
    assert wizard.Console(ask=lambda _: "  Bo  ", say=lambda _: None).text("Name", "Asha") == "Bo"


# ============================================================================= the AI helper step
def test_the_ai_step_saves_the_key_privately_and_never_prints_it(home, clean_env):
    script = Script({**BASICS, "Use an AI helper": "y", "Choose": ["1", "5", "1"], "Test the connection": "n"}, secret=KEY)
    # menus in order: plan (free), source (none), AI service (Gemini)
    assert run(home, script) == 0
    cfg = cfgmod.load(home)
    assert cfg["ai"]["enabled"] is True and cfg["ai"]["provider"] == "gemini"
    assert read_env_file(home.env_path) == {"GEMINI_API_KEY": KEY}
    if os.name == "posix":
        assert stat.S_IMODE(home.env_path.stat().st_mode) == 0o600
    assert KEY not in home.config_path.read_text() and KEY not in script.output and KEY not in "\n".join(script.asked)
    assert "readable only by you" in script.output and "aistudio.google.com" in script.output


def test_skipping_the_key_leaves_the_helper_on_but_unsaved(home, clean_env):
    script = Script({**BASICS, "Use an AI helper": "y", "Choose": ["1", "5", "2"], "Test the connection": "n"}, secret="")
    run(home, script)
    assert not home.env_path.exists() and cfgmod.load(home)["ai"]["provider"] == "groq"


def test_an_unusable_key_is_reported_and_the_wizard_carries_on(home, clean_env):
    script = Script({**BASICS, "Use an AI helper": "y", "Choose": ["1", "5", "1"], "Test the connection": "n"}, secret="two words")
    assert run(home, script) == 0
    assert "could not be saved" in script.output and "two words" not in script.output and not home.env_path.exists()


@pytest.fixture()
def ai_server():
    server = fake.FakeAIServer().start()
    yield server
    server.stop()


def _custom(server, **extra):
    return {**BASICS, "Use an AI helper": "y", "Choose": ["1", "5", "5"], "Server address": server.base_url,
            "Model name": "", "Name to save its key under": "MY_SERVER_API_KEY", **extra}


def test_the_ai_step_can_test_a_server_of_your_own(home, clean_env, ai_server):
    script = Script(_custom(ai_server), secret="sk-test-abcdefgh1234")
    assert run(home, script) == 0
    cfg = cfgmod.load(home)
    assert cfg["ai"] == {**cfg["ai"], "enabled": True, "provider": "custom", "base_url": ai_server.base_url,
                         "api_key_env": "MY_SERVER_API_KEY"}
    assert "OK:" in script.output and ai_server.requests[-1].headers["authorization"] == "Bearer sk-test-abcdefgh1234"
    assert "sk-test-abcdefgh1234" not in script.output
    origin = ai_server.base_url.rsplit("/v1", 1)[0]                                    # saved for this server, and only this one
    assert f"# connecto:origin MY_SERVER_API_KEY {origin}\nMY_SERVER_API_KEY=sk-test-abcdefgh1234\n" in home.env_path.read_text()


def test_a_key_for_a_server_address_that_cannot_hold_one_is_not_saved_and_the_wizard_carries_on(home, clean_env):
    script = Script({**BASICS, "Use an AI helper": "y", "Choose": ["1", "5", "5"], "Server address": "http://example.com/v1",
                     "Model name": "", "Name to save its key under": "MY_SERVER_API_KEY", "Test the connection": "n"},
                    secret="sk-test-abcdefgh1234")
    assert run(home, script) == 0
    assert "was not saved" in script.output and "sk-test-abcdefgh1234" not in script.output
    assert not home.env_path.exists()


def test_a_failing_test_can_switch_the_helper_off_again(home, clean_env, ai_server):
    ai_server.default_chat = fake.unauthorized()
    script = Script(_custom(ai_server, **{"Keep the AI helper switched on": "n"}), secret="sk-test-abcdefgh1234")
    run(home, script)
    assert "Problem:" in script.output and cfgmod.load(home)["ai"]["enabled"] is False
    ai_server.requests.clear()
    keep = Script(_custom(ai_server, **{"Keep the AI helper switched on": "y"}), secret="sk-test-abcdefgh1234")
    run(Home(home.root.parent / "other"), keep)
    assert cfgmod.load(Home(home.root.parent / "other"))["ai"]["enabled"] is True


def test_a_key_name_that_does_not_say_it_holds_a_key_is_asked_for_again(home, clean_env, ai_server):
    script = Script(_custom(ai_server, **{"Name to save its key under": ["LOCAL_LLM", "PATH", "LOCAL_LLM_KEY"]}),
                    secret="sk-test-abcdefgh1234")
    run(home, script)
    assert script.output.count("does not say it holds a key") == 2
    assert cfgmod.load(home)["ai"]["api_key_env"] == "LOCAL_LLM_KEY"
    assert read_env_file(home.env_path) == {"LOCAL_LLM_KEY": "sk-test-abcdefgh1234"}


def test_a_local_model_needs_no_key_at_all(home, clean_env):
    script = Script({**BASICS, "Use an AI helper": "y", "Choose": ["1", "5", "4"], "Test the connection": "n"})
    run(home, script)
    assert cfgmod.load(home)["ai"]["provider"] == "ollama" and not home.env_path.exists()
    assert not any("Paste the key" in q for q in script.asked) and "ollama pull" in script.output


def test_saying_no_to_the_helper_turns_it_off_even_if_it_was_on(home):
    cfgmod.save(home, cfgmod.normalize({"me": {"first_name": "Asha"}, "acknowledged_risks": True, "ai": {"enabled": True}})[0])
    script = Script({"Use an AI helper": "n"})
    run(home, script)
    assert cfgmod.load(home)["ai"]["enabled"] is False


# ============================================================================= running by itself
def test_the_schedule_step_asks_for_times_and_installs_them(home):
    calls = []

    def install(h, times):
        calls.append(times)
        return SimpleNamespace(ok=True, messages=["Daily runs are on."])

    script = Script({**BASICS, "Run by itself every day": "y", "At which times": "07:30, 19:00, 25:99"})
    assert run(home, script, install_schedule=install) == 0
    assert calls == [["07:30", "19:00", "25:99"]]                                  # (the installer validates them itself)
    assert cfgmod.load(home)["schedule"]["times"] == ["07:30", "19:00"]            # the bad one never reaches the settings
    assert "Daily runs are on." in script.output


def test_no_schedule_means_nothing_is_installed(home):
    script = Script(BASICS)
    run(home, script, install_schedule=lambda *a: pytest.fail("must not install anything"))
    assert not script.asked_about("At which times")


# ============================================================================= from flags only
def test_flags_only_setup_for_scripts(home):
    cfg = wizard.non_interactive_config(home, name="Asha", about="a designer", project="NINA", plan="premium", accept_risks=True)
    assert (cfg["me"]["first_name"], cfg["me"]["about"], cfg["me"]["project"]) == ("Asha", "a designer", "NINA")
    assert cfg["message"]["plan"] == "premium" and cfg["acknowledged_risks"] is True
    assert wizard.non_interactive_config(home, name="Asha")["acknowledged_risks"] is False       # never assumed


def test_flags_only_setup_keeps_what_is_already_there(home):
    saved = cfgmod.normalize({"me": {"first_name": "Asha", "about": "a designer", "project": "NINA"},
                              "sources": [{"type": "yc", "batches": ["S26"]}], "message": {"plan": "premium"},
                              "acknowledged_risks": True})[0]
    cfgmod.save(home, saved)
    again = wizard.non_interactive_config(home, name="Asha B")
    assert again["me"]["first_name"] == "Asha B" and again["me"]["about"] == "a designer" and again["me"]["project"] == "NINA"
    assert again["sources"] == saved["sources"] and again["acknowledged_risks"] is True
    assert again["message"]["plan"] == "premium"                                   # no --plan: not reset to the default
    assert wizard.non_interactive_config(home, name="Asha", plan="free")["message"]["plan"] == "free"
    assert wizard.non_interactive_config(home, name="Asha", plan="gold")["message"]["plan"] == "premium"   # nonsense: ignored


def test_the_next_steps_depend_on_whether_there_is_anything_to_fetch():
    assert "connecto fetch" in wizard.next_steps(True) and "connecto add" not in wizard.next_steps(True)
    assert "connecto add" in wizard.next_steps(False) and "connecto fetch" not in wizard.next_steps(False)
    for text in (wizard.next_steps(True), wizard.next_steps(False)):
        assert "connecto login" in text and "--dry-run" in text and "connecto doctor" in text


def test_the_risk_note_says_what_matters():
    text = wizard.RISK_TEXT
    assert "User Agreement does not allow automated activity" in text and "never asks for or stores your LinkedIn password" in text
    assert "hard limits in code" in text
