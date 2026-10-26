"""cli.py: every command through ``main(argv)``, with the browser and the network replaced by stand-ins.

The most important thing in here is what the command line refuses to do: it never sends real invitations unless a person
confirmed it (a prompt in a terminal, or --yes), and a dry run never asks for anything.
"""
import io
import json
import logging
import os
import stat
from types import SimpleNamespace

import pytest
import yaml

from connecto import __version__, cli, doctor, runner, scheduler, wizard
from connecto import config as cfgmod
from connecto.lock import RunLock
from connecto.paths import Home
from connecto.runner import EXIT_BUSY, EXIT_CONFIG, EXIT_NEEDS_YOU, EXIT_OK
from connecto.store import Store
from connecto.util import read_env_file, set_env_var

import fake_ai_server as fake
from helpers import add_people, make_cfg, person

JANE = "https://www.linkedin.com/in/jane-doe-1a2b3c/"


@pytest.fixture(autouse=True)
def _quiet_logging():
    yield
    logging.getLogger("connecto").handlers.clear()            # (the commands attach handlers to the captured streams)


@pytest.fixture()
def home(tmp_path):
    return Home(tmp_path / "home")


@pytest.fixture()
def ready(home):
    """A home with a saved, ready-to-send settings file."""
    cfgmod.save(home, make_cfg())
    return home


@pytest.fixture()
def tool(home, capsys):
    """tool("add", JANE) -> (exit code, stdout, stderr), always against this test's own home folder."""
    def call(*argv, **kw):
        code = cli.main([*argv, "--home", str(home.root)])
        out, err = capsys.readouterr()
        return code, out, err
    return call


def test_queue_command_search_json_notes_and_pagination(ready, tool):
    with Store(ready.db_path) as store:
        store.upsert_people([person(f"q{i}", f"Queue Person {i}", company="Acme") for i in range(3)], "manual", "manual")
    code, out, _ = tool("queue", "--search", "acme", "--limit", "2", "--json")
    page = json.loads(out)
    assert code == EXIT_OK and page["total"] == 3 and len(page["people"]) == 2
    assert page["people"][0]["note"]
    code, out, _ = tool("queue", "--limit", "2")
    assert code == EXIT_OK and "--offset 2" in out and "Note (" in out
    code, out, _ = tool("queue", "--search", "nobody")
    assert code == EXIT_OK and "No people match this search" in out
    code, out, _ = tool("queue", "--offset", "2", "--limit", "2", "--json")
    assert code == EXIT_OK and len(json.loads(out)["people"]) == 1


def test_queue_command_explains_plain_notes(ready, tool):
    cfgmod.save(ready, make_cfg(message={"send_note": False}))
    with Store(ready.db_path) as store:
        store.upsert_people([person("plain", "Plain Person")], "manual", "manual")
    code, out, _ = tool("queue")
    assert code == EXIT_OK and "Plain invitation: notes are off" in out


# ============================================================================= meta
def test_without_a_command_it_says_where_to_start(home, tool):
    code, out, _ = tool()
    assert code == EXIT_OK and "Nothing is set up yet" in out and "connecto init" in out and "connecto ui" in out
    cfgmod.save(home, make_cfg())
    code, out, _ = tool()
    assert code == EXIT_OK and "Your settings are in" in out and "connecto preview" in out and "connecto doctor" in out


def test_version_and_unknown_commands(capsys):
    with pytest.raises(SystemExit) as stop:
        cli.main(["--version"])
    assert stop.value.code == 0 and f"connecto {__version__}" in capsys.readouterr().out
    with pytest.raises(SystemExit) as stop:
        cli.main(["frobnicate"])
    assert stop.value.code == 2


def test_the_home_folder_can_be_named_before_or_after_the_command_or_in_the_environment(tmp_path, capsys, monkeypatch):
    target = tmp_path / "elsewhere"
    expected = str(Home(target).config_path)
    assert cli.main(["--home", str(target), "config", "path"]) == 0 and capsys.readouterr().out.strip() == expected
    assert cli.main(["config", "path", "--home", str(target)]) == 0 and capsys.readouterr().out.strip() == expected
    monkeypatch.setenv("CONNECTO_HOME", str(target))
    assert cli.main(["config", "path"]) == 0 and capsys.readouterr().out.strip() == expected


def test_a_console_that_cannot_show_a_character_does_not_crash_the_tool(monkeypatch):
    class Narrow(io.StringIO):
        def reconfigure(self, errors=None):
            self.errors_policy = errors

    stream = Narrow()
    monkeypatch.setattr(cli.sys, "stdout", stream)
    cli._console_safe()
    assert stream.errors_policy == "replace"


def test_risks_prints_the_whole_note(tool):
    code, out, _ = tool("risks")
    assert code == EXIT_OK and out.strip() == wizard.RISK_TEXT.strip()


# ============================================================================= init and config
def test_init_without_a_name_is_a_clear_error(tool, home):
    code, _, err = tool("init", "--non-interactive")
    assert code == EXIT_CONFIG and "--name" in err and not home.config_path.exists()


def test_init_from_flags_saves_the_settings_but_never_accepts_the_risks_for_you(tool, home):
    code, out, _ = tool("init", "--non-interactive", "--name", "Asha", "--about", "a designer", "--plan", "premium")
    assert code == EXIT_OK and "Saved" in out and "Not ready to send yet" in out and "connecto risks" in out
    cfg = cfgmod.load(home)
    assert cfg["me"]["first_name"] == "Asha" and cfg["message"]["plan"] == "premium" and cfg["acknowledged_risks"] is False
    code, out, _ = tool("config", "check")
    assert code == EXIT_CONFIG and "not ready" in out and "risk" in out.lower()
    code, out, _ = tool("init", "--non-interactive", "--name", "Asha", "--accept-risks")
    assert code == EXIT_OK and "Not ready" not in out
    assert cfgmod.load(home)["message"]["plan"] == "premium"                    # an earlier answer is not reset by a later run
    assert tool("config", "check")[:2] == (EXIT_OK, "Settings are valid.\n")


def test_the_guided_init_hands_over_to_the_wizard(tool, home, monkeypatch):
    seen = []
    monkeypatch.setattr(wizard, "run_wizard", lambda h, con=None, *, assume_risks_accepted=False, **kw: (
        seen.append((h.root, assume_risks_accepted)), 0)[1])
    assert tool("init")[0] == 0 and tool("init", "--accept-risks")[0] == 0
    assert seen == [(home.root.resolve(), False), (home.root.resolve(), True)]


def test_config_path_show_and_check(tool, ready):
    assert tool("config", "path")[1].strip() == str(ready.config_path)
    code, out, _ = tool("config", "show")
    shown = yaml.safe_load(out)
    assert code == EXIT_OK and shown["me"]["first_name"] == "Asha" and shown["limits"]["weekly"] == 60
    assert tool("config")[:2] == (EXIT_OK, "Settings are valid.\n")                # check is the default action


def test_config_check_without_a_file_and_with_a_broken_one(tool, home):
    code, out, _ = tool("config", "check")
    assert code == EXIT_CONFIG and "no settings file yet" in out.lower() and "connecto init" in out
    home.ensure()
    home.config_path.write_text("limits: [unclosed\n  - : :")
    code, _, err = tool("config", "check")
    assert code == EXIT_CONFIG and "settings file could not be read" in err and str(home.config_path) in err


def test_config_check_lists_notices_for_numbers_that_were_pulled_back(tool, home):
    home.ensure()
    home.config_path.write_text("me: {first_name: Asha}\nacknowledged_risks: true\nlimits: {weekly: 5000}\n")
    code, out, _ = tool("config", "check")
    assert code == EXIT_OK and "note:" in out and "Settings are valid." not in out


# ============================================================================= people
def test_adding_people_by_link_name_file_and_csv(tool, home, tmp_path):
    code, out, err = tool("add", JANE, "https://www.linkedin.com/in/jdoe/")
    assert code == EXIT_OK and "Added 2 people." in out and "came without a name" in out
    code, out, _ = tool("add", "--name", "Bo Two", "--company", "Acme", "--title", "CEO", "https://www.linkedin.com/in/bo-two/")
    assert code == EXIT_OK and "Added 1 person." in out and "came without a name" not in out
    people_file = tmp_path / "people.txt"
    people_file.write_text("# friends\nChi Three, https://www.linkedin.com/in/chi-three/\nthis line is not a link\n")
    code, out, err = tool("add", "--file", str(people_file))
    assert code == EXIT_OK and "Added 1 person." in out and "no LinkedIn profile link found" in err
    leads = tmp_path / "leads.csv"
    leads.write_text("name,company,linkedin_url\nDee Four,Four Co,https://www.linkedin.com/in/dee-four/\n")
    code, out, _ = tool("add", "--csv", str(leads))
    assert code == EXIT_OK and "Added 1 person." in out
    with Store(home.db_path) as st:
        everyone = st.people()
        people = {p.get("full_name", ""): p for p in everyone}
    assert len(everyone) == 5                                                       # (two of them have no name, so they share one key below)
    assert set(people) == {"", "Bo Two", "Chi Three", "Dee Four"}
    assert {p["source_type"] for p in everyone} == {"manual"}
    assert people["Bo Two"]["company"] == "Acme" and people["Bo Two"]["title"] == "CEO"
    assert people["Dee Four"]["priority"] == "1000000"                              # still ahead of everything a source finds


def test_adding_nothing_or_an_unreadable_file_is_a_clear_error(tool):
    code, _, err = tool("add")
    assert code == EXIT_CONFIG and "No people to add" in err
    code, _, err = tool("add", "not a link")
    assert code == EXIT_CONFIG and "No people to add" in err
    code, _, err = tool("add", "--file", "/no/such/file.txt")
    assert code == EXIT_CONFIG and "Cannot read /no/such/file.txt" in err
    code, _, err = tool("add", "--csv", "/no/such/file.csv")
    assert code == EXIT_CONFIG and "Cannot read /no/such/file.csv" in err


def test_skip_and_undo(tool, ready):
    tool("add", JANE)
    code, out, _ = tool("skip", JANE, "linkedin.com/in/never-seen")
    assert code == EXIT_OK and "Will never contact 2 people." in out
    code, out, _ = tool("preview")
    assert "0 waiting" in out
    code, out, _ = tool("skip", "--undo", JANE)
    assert code == EXIT_OK and "Removed the skip from 1 person." in out
    code, out, _ = tool("preview")
    assert "1 waiting" in out


def test_skipping_something_that_is_not_a_profile_fails_but_a_mix_succeeds(tool):
    code, _, err = tool("skip", "https://example.com/nope")
    assert code == EXIT_CONFIG and "not a LinkedIn profile link" in err
    code, out, err = tool("skip", JANE, "https://example.com/nope")
    assert code == EXIT_OK and "Will never contact 1 person." in out and "not a LinkedIn profile link" in err


# ============================================================================= looking: preview, report, export
def test_preview_in_words(tool, ready):
    add_people(ready, [person("a-one", "Asha One", location="Pune"), person("b-two", "Bo Two")], source_type="csv", source_id="x.csv")
    cfg = make_cfg(sources=[{"type": "csv", "path": "x.csv"}])
    cfgmod.save(ready, cfg)
    code, out, _ = tool("preview", "--sample", "5", "--notes", "2")
    assert code == EXIT_OK
    assert "People: 2 stored, 2 match, 2 waiting" in out and "Pace: about" in out
    assert "A few of the people waiting" in out and "Asha One" in out
    assert "What the notes would say (limit 300 characters)" in out and "Hi Asha" not in out.split("What the notes")[0]


def test_preview_as_json_is_machine_readable(tool, ready):
    add_people(ready, [person("a-one")])
    code, out, _ = tool("preview", "--json", "--sample", "1")
    data = json.loads(out)
    assert code == EXIT_OK and data["queued"] == 1 and data["note_limit"] == 300 and len(data["sample"]) == 1


def test_preview_with_the_ai_helper_off_says_so_instead_of_pretending(tool, ready):
    add_people(ready, [person("a-one")])
    code, out, err = tool("preview", "--ai")
    assert code == EXIT_OK and "AI helper is off or cannot start" in err and "What the notes would say" in out


def test_preview_without_settings_points_to_init(tool):
    code, _, err = tool("preview")
    assert code == EXIT_CONFIG and "connecto init" in err


def test_report_when_nothing_has_happened_and_after(tool, ready):
    assert tool("report")[:2] == (EXIT_OK, "Nothing has happened yet.\n")
    assert tool("report", "--json")[:2] == (EXIT_OK, "{}\n")
    add_people(ready, [person("a-one", "Asha One"), person("b-two", "Bo Two")])
    with Store(ready.db_path) as st:
        st.record(person("a-one", "Asha One"), "sent", note="Hi!")
        run_id = st.start_run("scheduled")
        st.finish_run(run_id, 1, 1, "run limit reached")
    code, out, _ = tool("report")
    assert code == EXIT_OK and "Today: 1 sent" in out and "All time: 1" in out and "People stored: 2" in out
    assert "Outcomes: sent 1" in out and "Recent runs" in out and "run limit reached" in out and "Asha One" in out
    assert tool("status")[1] == out                                                    # `status` is the same thing
    data = json.loads(tool("report", "--json")[1])
    assert data["total_sent"] == 1 and data["people"] == 2


def test_pause_and_resume_show_in_the_report(tool, ready):
    add_people(ready, [person("a-one")])
    code, out, _ = tool("pause", "--days", "3", "--reason", "taking a break")
    assert code == EXIT_OK and "Paused for 3 days" in out
    assert "PAUSED until" in tool("report")[1] and "taking a break" in tool("report")[1]
    code, out, _ = tool("resume")
    assert code == EXIT_OK and out.strip() == "Resumed." and "PAUSED" not in tool("report")[1]
    with Store(ready.db_path) as st:
        st.kv_set("note_blocked_until", "2999-01-01T00:00:00")
    assert "personal notes" in tool("report")[1]
    assert "Personal notes will be tried again." in tool("resume", "--notes")[1] and "personal notes" not in tool("report")[1]
    assert "Paused for 1 day." in tool("pause", "--days", "1")[1]


@pytest.mark.parametrize("days", ["0", "-3", "61", "99999999999", "1000000"])
def test_a_pause_of_nonsense_length_is_refused_and_nothing_is_paused(tool, ready, days):
    code, _, err = tool("pause", "--days", days)
    assert code == EXIT_CONFIG and "between 1 and 60" in err
    with Store(ready.db_path) as st:
        assert runner._pause_info(st) is None


def test_the_longest_pause_is_sixty_days_and_the_reason_is_cleaned(tool, ready):
    assert tool("pause", "--days", "60", "--reason", "away\x1b[31m\u202e")[0] == EXIT_OK
    with Store(ready.db_path) as st:
        when, why = runner._pause_info(st)
        assert why == "away[31m" and (when - runner.datetime.now()).days >= 59 and runner.pause_kind(st) == runner.PAUSE_USER


def test_resuming_after_an_automatic_pause_says_how_it_came_about(tool, ready):
    with Store(ready.db_path) as st:
        runner.set_pause(st, runner.PAUSE_AFTER_LIMIT, "LinkedIn said the invitation limit was reached", runner.PAUSE_LIMIT)
    code, out, _ = tool("resume")
    assert code == EXIT_OK and "set automatically" in out and "invitation limit" in out and "--dry-run --limit 2" in out
    code, out, _ = tool("resume")
    assert out.strip() == "Resumed."                                                    # nothing to explain the second time


BROKEN_SETTINGS = "limits: &a [1]\nmore: *a\n"


def test_pausing_resuming_skipping_and_stopping_the_schedule_work_with_a_broken_settings_file(tool, home, monkeypatch):
    """The things you reach for when something is wrong must not depend on the settings file being readable."""
    home.ensure()
    home.config_path.write_text(BROKEN_SETTINGS, encoding="utf-8")
    monkeypatch.setattr(scheduler, "status", lambda h, **kw: {"installed": True, "message": "On: 10:40", "details": []})
    monkeypatch.setattr(scheduler, "uninstall", lambda h, dry_run=False, **kw: scheduler.SchedResult(True, ["Daily runs are off."]))
    assert tool("pause", "--days", "2")[0] == EXIT_OK
    with Store(home.db_path) as st:
        assert runner._pause_info(st) is not None
    assert tool("skip", JANE)[0] == EXIT_OK
    assert tool("schedule", "status")[:2] == (EXIT_OK, "On: 10:40\n")
    assert tool("schedule", "uninstall")[:2] == (EXIT_OK, "Daily runs are off.\n")
    with Store(home.db_path) as st:
        st.record(person("a-one"), "sent")
    assert tool("export")[0] == EXIT_OK
    code, out, err = tool("report")
    assert code == EXIT_OK and "Today: 1 sent" in out and "could not be read" in err and "defaults" in err
    assert tool("resume")[0] == EXIT_OK
    for command in (("run", "--dry-run"), ("preview",), ("schedule", "install")):          # these do need a readable file
        code, _, err = tool(*command)
        assert code == EXIT_CONFIG and "could not be read" in err, command


def test_ctrl_c_during_a_run_is_not_called_harmless(tool, ready, monkeypatch):
    def interrupted(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(runner, "run_send", interrupted)
    code, out, _ = tool("run", "--yes")
    assert code == 130 and "'sending'" in out and "Sent invitations" in out


def test_export_to_the_screen_and_to_a_file_is_safe_to_open_in_a_spreadsheet(tool, ready, tmp_path):
    assert tool("export")[0] == EXIT_CONFIG                                               # nothing yet
    with Store(ready.db_path) as st:
        st.record(person("a-one", "Asha One"), "sent", note="Hi Asha")
        st.record(person("b-two", '=HYPERLINK("http://x","click")'), "already_pending")
    code, out, _ = tool("export")
    lines = out.splitlines()
    assert code == EXIT_OK and lines[0].startswith("status,full_name,company,linkedin_url,note")
    assert len(lines) == 3 and "Asha One" in out and "'=HYPERLINK" in out and ",=HYPERLINK" not in out and '"=HYPERLINK' not in out
    target = tmp_path / "out.csv"
    code, out, _ = tool("export", "--out", str(target), "--status", "sent")
    assert code == EXIT_OK and "Wrote 1 row" in out
    assert target.read_text(encoding="utf-8").count("\n") == 2 and "already_pending" not in target.read_text(encoding="utf-8")


# ============================================================================= sending: the safety rules
@pytest.fixture()
def sends(monkeypatch):
    """Replace the real runner: record what the command line asked for instead of opening a browser."""
    calls = []

    def fake_run(home, cfg, opts):
        calls.append(opts)
        return runner.RunResult(exit_code=EXIT_OK, sent=1, attempted=1)

    monkeypatch.setattr(runner, "run_send", fake_run)
    return calls


def test_a_real_send_is_refused_without_a_person_confirming_it(tool, ready, sends, monkeypatch):
    monkeypatch.setattr(cli, "_tty", lambda: False)
    code, _, err = tool("run")
    assert code == EXIT_CONFIG and "would send real invitations" in err and "--yes" in err and "--dry-run" in err
    assert sends == []                                                                  # the runner was never even called


def test_in_a_terminal_a_real_send_asks_first_and_only_yes_goes_ahead(tool, ready, sends, monkeypatch):
    monkeypatch.setattr(cli, "_tty", lambda: True)
    asked = []
    for answer, goes in (("", False), ("n", False), ("no", False), ("maybe", False), ("y", True), ("YES", True)):
        monkeypatch.setattr("builtins.input", lambda prompt, a=answer: asked.append(prompt) or a)
        sends.clear()
        code, out, _ = tool("run", "--limit", "3")
        assert code == EXIT_OK and bool(sends) is goes, answer
        assert ("Nothing sent." in out) is (not goes)
    assert all("up to 3" in p and "LinkedIn invitations from your account" in p for p in asked)


def test_a_dry_run_asks_for_nothing(tool, ready, sends, monkeypatch):
    monkeypatch.setattr(cli, "_tty", lambda: False)
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("a dry run must not prompt"))
    code, _, _ = tool("run", "--dry-run", "--limit", "2")
    assert code == EXIT_OK and len(sends) == 1 and sends[0].dry_run is True and sends[0].limit == 2


def test_yes_and_the_schedule_flag_skip_the_prompt(tool, ready, sends, monkeypatch):
    monkeypatch.setattr(cli, "_tty", lambda: False)
    assert tool("run", "--yes")[0] == EXIT_OK and sends[-1].dry_run is False and sends[-1].scheduled is False
    assert tool("run", "--scheduled")[0] == EXIT_OK and sends[-1].scheduled is True and sends[-1].dry_run is False


def test_every_flag_reaches_the_runner(tool, ready, sends):
    assert tool("run", "--yes", "--limit", "4", "--ignore-hours", "--ignore-pause", "--retry-errors", "--no-ai")[0] == EXIT_OK
    opts = sends[-1]
    assert (opts.limit, opts.ignore_hours, opts.ignore_pause, opts.retry_errors, opts.use_ai, opts.fetch) == (4, True, True, True, False, None)
    tool("run", "--yes", "--fetch")
    assert sends[-1].fetch is True and sends[-1].use_ai is True
    tool("run", "--yes", "--no-fetch")
    assert sends[-1].fetch is False
    tool("run", "--yes")
    assert (sends[-1].limit, sends[-1].ignore_hours, sends[-1].ignore_pause, sends[-1].retry_errors) == (0, False, False, False)


@pytest.mark.parametrize("code", [EXIT_OK, EXIT_NEEDS_YOU, EXIT_BUSY, EXIT_CONFIG])
def test_the_exit_code_of_the_run_is_the_exit_code_of_the_command(tool, ready, monkeypatch, code):
    monkeypatch.setattr(runner, "run_send", lambda home, cfg, opts: runner.RunResult(exit_code=code))
    assert tool("run", "--dry-run")[0] == code


def test_pressing_ctrl_c_stops_politely(tool, ready, monkeypatch):
    def interrupted(home, cfg, opts):
        raise KeyboardInterrupt

    monkeypatch.setattr(runner, "run_send", interrupted)
    code, out, _ = tool("run", "--dry-run")
    assert code == 130 and "Interrupted" in out


def test_running_without_settings_points_to_init_and_never_starts(tool, sends):
    code, _, err = tool("run", "--dry-run")
    assert code == EXIT_CONFIG and "connecto init" in err and sends == []


def test_login_and_fetch_pass_their_options_on(tool, ready, monkeypatch):
    seen = {}
    monkeypatch.setattr(runner, "login", lambda home, cfg, log=None, wait_minutes=15: seen.update(login=wait_minutes) or EXIT_OK)
    monkeypatch.setattr(runner, "run_fetch", lambda home, cfg, **kw: seen.update(fetch=kw) or runner.RunResult(exit_code=EXIT_NEEDS_YOU))
    assert tool("login", "--wait", "2")[0] == EXIT_OK and seen["login"] == 2
    code = tool("fetch", "--only", "yc", "--limit", "5", "--refresh", "--no-browser")[0]
    assert code == EXIT_NEEDS_YOU
    assert (seen["fetch"]["only"], seen["fetch"]["limit"], seen["fetch"]["refresh"], seen["fetch"]["skip_browser"]) == ("yc", 5, True, True)


def test_fetch_rejects_a_source_kind_that_does_not_exist(home):
    with pytest.raises(SystemExit) as stop:
        cli.main(["fetch", "--only", "nonsense", "--home", str(home.root)])
    assert stop.value.code == 2


# ============================================================================= the daily schedule
def test_schedule_status_install_uninstall(tool, ready, monkeypatch):
    calls = []
    monkeypatch.setattr(scheduler, "status", lambda h, **kw: {"installed": True, "message": "On: 10:40, 18:20", "details": ["cron line"]})
    code, out, _ = tool("schedule", "status")
    assert code == EXIT_OK and "On: 10:40, 18:20" in out and "  cron line" in out

    def install(h, times, dry_run=False, **kw):
        calls.append(("install", list(times), dry_run))
        return scheduler.SchedResult(True, ["Daily runs are on."])

    monkeypatch.setattr(scheduler, "install", install)
    monkeypatch.setattr(scheduler, "uninstall", lambda h, dry_run=False, **kw: calls.append(("uninstall", dry_run)) or scheduler.SchedResult(True, ["Daily runs are off."]))
    code, out, _ = tool("schedule", "install")
    assert code == EXIT_OK and "Daily runs are on." in out and calls[-1] == ("install", ["10:40", "18:20"], False)
    code, out, _ = tool("schedule", "install", "--times", "07:30, 19:00")
    assert calls[-1] == ("install", ["07:30", " 19:00"], False)
    assert cfgmod.load(ready)["schedule"]["times"] == ["07:30", "19:00"]                # remembered for next time
    assert tool("schedule", "uninstall")[:2] == (EXIT_OK, "Daily runs are off.\n") and calls[-1] == ("uninstall", False)
    assert tool("schedule", "install", "--dry-run")[0] == EXIT_OK and calls[-1] == ("install", ["07:30", "19:00"], True)


def test_the_schedule_is_not_installed_for_a_setup_that_could_not_send(tool, home, monkeypatch):
    monkeypatch.setattr(scheduler, "install", lambda *a, **k: pytest.fail("must not install"))
    code, _, err = tool("schedule", "install")
    assert code == EXIT_CONFIG and "No settings found" in err
    cfgmod.save(home, make_cfg(me={"first_name": ""}))
    code, _, err = tool("schedule", "install")
    assert code == EXIT_CONFIG and "Not ready to run by itself yet" in err and "first name" in err.lower()


def test_a_failed_install_is_reported_with_a_non_zero_exit(tool, ready, monkeypatch):
    monkeypatch.setattr(scheduler, "install", lambda *a, **k: scheduler.SchedResult(False, ["cron is not available."]))
    code, out, _ = tool("schedule", "install", "--times", "09:00")
    assert code == EXIT_NEEDS_YOU and "cron is not available." in out
    assert cfgmod.load(ready)["schedule"]["times"] != ["09:00"]                         # nothing is remembered for a failure


def test_a_dry_run_of_the_schedule_changes_nothing(tool, ready):
    code, out, _ = tool("schedule", "install", "--dry-run", "--times", "09:00")
    assert code == EXIT_OK and out.strip()
    assert cfgmod.load(ready)["schedule"]["times"] != ["09:00"]


# ============================================================================= the optional AI helper
def test_ai_providers_are_listed_without_any_secret(tool, clean_env):
    code, out, _ = tool("ai", "providers")
    assert code == EXIT_OK
    for word in ("gemini", "groq", "openrouter", "ollama", "custom", "aistudio.google.com"):
        assert word in out


def test_a_key_is_saved_from_standard_input_and_never_shown(tool, home, monkeypatch, clean_env):
    cfgmod.save(home, make_cfg(ai={"provider": "gemini"}))
    secret = "AIzaSy-test-key-1234567890"
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(secret + "\n"))
    code, out, err = tool("ai", "key", "--stdin")
    assert code == EXIT_OK and "GEMINI_API_KEY" in out and secret not in out + err
    assert read_env_file(home.env_path) == {"GEMINI_API_KEY": secret}
    if os.name == "posix":
        assert stat.S_IMODE(home.env_path.stat().st_mode) == 0o600
    assert secret not in home.config_path.read_text()
    code, out, _ = tool("ai", "key", "--remove")
    assert code == EXIT_OK and "Removed GEMINI_API_KEY." in out and "GEMINI_API_KEY" not in home.env_path.read_text()
    assert "was not saved" in tool("ai", "key", "--remove")[1]


def test_the_key_command_refuses_what_cannot_be_a_key_or_a_key_name(tool, home, monkeypatch, clean_env):
    cfgmod.save(home, make_cfg(ai={"provider": "gemini"}))
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("\n"))
    assert "No key given" in tool("ai", "key", "--stdin")[2]
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("two words\n"))
    code, out, err = tool("ai", "key", "--stdin")
    assert code == EXIT_CONFIG and "cannot be saved" in err and "two words" not in out + err
    for name in ("PATH", "LD_PRELOAD", "HTTPS_PROXY"):                                   # the key file only ever holds keys
        monkeypatch.setattr(cli.sys, "stdin", io.StringIO("abcdefgh12345678\n"))
        code, _, err = tool("ai", "key", "--stdin", "--env", name)
        assert code == EXIT_CONFIG and "cannot be saved" in err and "KEY, TOKEN or SECRET" in err
    assert not home.env_path.exists()
    cfgmod.save(home, make_cfg(ai={"provider": "ollama"}))
    code, _, err = tool("ai", "key", "--stdin")
    assert code == EXIT_CONFIG and "needs no key" in err


@pytest.fixture()
def ai_server():
    server = fake.FakeAIServer().start()
    yield server
    server.stop()


def _custom_ai(home, server, key="sk-test-abcdefgh1234"):
    cfgmod.save(home, make_cfg(ai={"enabled": True, "provider": "custom", "base_url": server.base_url,
                                   "api_key_env": "MY_SERVER_API_KEY", "model": "test-model"}))
    if key:
        set_env_var(home.env_path, "MY_SERVER_API_KEY", key)


def test_a_key_with_a_name_of_your_own_is_saved_for_the_server_in_your_settings(tool, home, ai_server, monkeypatch, clean_env):
    cfgmod.save(home, make_cfg(ai={"enabled": True, "provider": "custom", "base_url": ai_server.base_url,
                                   "api_key_env": "MY_SERVER_API_KEY", "model": "test-model"}))
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("sk-test-abcdefgh1234\n"))
    code, out, err = tool("ai", "key", "--stdin")
    origin = ai_server.base_url.rsplit("/v1", 1)[0]
    assert code == EXIT_OK and f"for {origin} only" in out and "sk-test-abcdefgh1234" not in out + err
    assert f"# connecto:origin MY_SERVER_API_KEY {origin}\nMY_SERVER_API_KEY=sk-test-abcdefgh1234\n" in home.env_path.read_text()
    assert tool("ai", "test")[0] == EXIT_OK
    # the settings now point at another server: the key is not sent there, and says what to do
    other = fake.FakeAIServer().start()
    try:
        cfgmod.save(home, make_cfg(ai={"enabled": True, "provider": "custom", "base_url": other.base_url,
                                       "api_key_env": "MY_SERVER_API_KEY", "model": "test-model"}))
        code, out, _ = tool("ai", "test")
        assert code == EXIT_NEEDS_YOU and "was saved for" in out and "connecto ai key --env MY_SERVER_API_KEY" in out
        assert other.requests == []
    finally:
        other.stop()


def test_a_built_in_services_key_is_saved_without_a_server_record(tool, home, monkeypatch, clean_env):
    cfgmod.save(home, make_cfg(ai={"provider": "groq"}))
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("gsk_test_1234567890\n"))
    assert tool("ai", "key", "--stdin")[0] == EXIT_OK
    assert home.env_path.read_text() == "GROQ_API_KEY=gsk_test_1234567890\n"


def test_a_custom_key_without_a_server_address_is_refused_before_anything_is_pasted(tool, home, monkeypatch, clean_env):
    import getpass
    monkeypatch.setattr(getpass, "getpass", lambda *a, **k: pytest.fail("asked for the key although it cannot be saved"))
    cfgmod.save(home, make_cfg(ai={"provider": "custom", "base_url": "", "api_key_env": "MY_SERVER_API_KEY"}))
    code, _, err = tool("ai", "key")
    assert code == EXIT_CONFIG and "Set the server address first" in err and not home.env_path.exists()
    cfgmod.save(home, make_cfg(ai={"provider": "custom", "base_url": "http://example.com/v1", "api_key_env": "MY_SERVER_API_KEY"}))
    code, _, err = tool("ai", "key")                                         # plain http to another computer is no address for a key
    assert code == EXIT_CONFIG and "Set the server address first" in err and not home.env_path.exists()


@pytest.mark.parametrize("extra", [[], ["--remove"], ["--stdin"]])
def test_a_key_typed_after_the_command_is_refused_and_not_echoed(tool, home, monkeypatch, clean_env, extra):
    import getpass
    monkeypatch.setattr(getpass, "getpass", lambda *a, **k: "")              # (so a missing refusal shows as "No key given")
    cfgmod.save(home, make_cfg(ai={"provider": "gemini"}))
    typed = "AIzaSy-typed-on-the-command-line-1234"
    code, out, err = tool("ai", "key", typed, *extra)
    assert code == EXIT_CONFIG and "Do not type the key after the command" in err and typed not in out + err
    assert not home.env_path.exists()


def test_ai_test_reports_ok_or_the_problem(tool, home, ai_server, clean_env):
    _custom_ai(home, ai_server)
    code, out, _ = tool("ai", "test")
    assert code == EXIT_OK and out.startswith("OK: ") and "sk-test-abcdefgh1234" not in out
    assert ai_server.requests[-1].headers["authorization"] == "Bearer sk-test-abcdefgh1234"
    ai_server.default_chat = fake.unauthorized()
    code, out, _ = tool("ai", "test")
    assert code == EXIT_NEEDS_YOU and out.startswith("Problem: ")


def test_ai_test_without_a_key_says_what_is_missing(tool, home, clean_env):
    cfgmod.save(home, make_cfg(ai={"enabled": True, "provider": "groq"}))
    code, out, _ = tool("ai", "test")
    assert code == EXIT_NEEDS_YOU and "Cannot start" in out and "GROQ_API_KEY" in out


def test_ai_suggest_prints_filters_and_only_changes_the_settings_when_told_to(tool, home, ai_server, clean_env):
    _custom_ai(home, ai_server)
    reply = fake.ok(json.dumps({"keywords": ["fintech"], "titles": ["founder"], "locations": ["India"], "exclude_keywords": [],
                                "search_queries": ["fintech founder india"]}))
    ai_server.script(reply)
    code, out, _ = tool("ai", "suggest", "founders", "of", "fintech", "startups", "in", "India")
    assert code == EXIT_OK and '"fintech"' in out and "suggestions only" in out
    assert cfgmod.load(home)["targets"]["keywords"] == []                                # nothing saved yet
    ai_server.script(reply)
    code, out, _ = tool("ai", "suggest", "--apply", "founders of fintech startups in India")
    assert code == EXIT_OK and "Added to your filters" in out
    targets = cfgmod.load(home)["targets"]
    assert targets["keywords"] == ["fintech"] and targets["titles"] == ["founder"] and targets["locations"] == ["India"]
    sent = ai_server.chat_requests()[0].messages_text()
    assert "founders of fintech startups in India" in sent and "Asha" not in sent         # nothing about the user or anyone else


def test_ai_suggest_needs_a_description_and_a_working_helper(tool, home, clean_env):
    cfgmod.save(home, make_cfg())
    code, _, err = tool("ai", "suggest")
    assert code == EXIT_CONFIG and "Describe the people you want to meet" in err
    code, _, err = tool("ai", "suggest", "anyone")
    assert code == EXIT_NEEDS_YOU and "cannot start" in err


# ============================================================================= checking, moving, leaving
def test_doctor_exit_code_follows_whether_anything_failed(tool, home, monkeypatch):
    monkeypatch.setattr(doctor, "run_checks", lambda h, online=False, launch=False: [doctor.Check("a", doctor.OK, "fine"),
                                                                                     doctor.Check("b", doctor.WARN, "hm", "", "do x")])
    code, out, _ = tool("doctor")
    assert code == EXIT_OK and "[ ok ] fine" in out and "-> do x" in out
    monkeypatch.setattr(doctor, "run_checks", lambda h, online=False, launch=False: [doctor.Check("a", doctor.FAIL, "broken")])
    code, out, _ = tool("doctor")
    assert code == EXIT_NEEDS_YOU and "[FAIL] broken" in out
    code, out, _ = tool("doctor", "--json")
    data = json.loads(out)
    assert code == EXIT_NEEDS_YOU and data["summary"]["healthy"] is False and data["checks"][0]["id"] == "a"


def test_doctor_passes_its_options_on(tool, monkeypatch):
    seen = []
    monkeypatch.setattr(doctor, "run_checks", lambda h, online=False, launch=False: seen.append((online, launch)) or [])
    tool("doctor", "--online", "--launch")
    tool("doctor")
    assert seen == [(True, True), (False, False)]


def test_migrate_reports_what_it_did_and_what_did_not_carry_over(tool, tmp_path, monkeypatch):
    import connecto.migrate as migrate
    seen = {}

    def fake_migrate(path, home, *, import_state, import_people, write):
        seen.update(path=path, state=import_state, people=import_people, write=write)
        return migrate.MigrationReport(notes=["Settings converted."], warnings=["Applied-batch text was dropped."])

    monkeypatch.setattr(migrate, "migrate_v1", fake_migrate)
    code, out, _ = tool("migrate-v1", str(tmp_path / "v1"), "--people", "--dry-run", "--no-state")
    assert code == EXIT_OK and "Settings converted." in out and "warning: Applied-batch text was dropped." in out
    assert seen == {"path": str(tmp_path / "v1"), "state": False, "people": True, "write": False}
    monkeypatch.setattr(migrate, "migrate_v1", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("no config.yaml in /x")))
    code, _, err = tool("migrate-v1", "/x")
    assert code == EXIT_CONFIG and "no config.yaml in /x" in err


def test_logout_deletes_only_the_saved_session_and_only_when_sure(tool, home, monkeypatch):
    assert tool("logout")[1].strip() == "There is no saved LinkedIn session."
    profile = home.browser_dir / "linkedin"
    profile.mkdir(parents=True)
    (profile / "Cookies").write_text("session")
    other = home.browser_dir / "doctor"
    other.mkdir()
    monkeypatch.setattr(cli, "_tty", lambda: False)
    code, _, err = tool("logout")
    assert code == EXIT_CONFIG and "--yes" in err and profile.exists()                  # no terminal, no confirmation: nothing happens
    monkeypatch.setattr(cli, "_tty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert "Nothing deleted." in tool("logout")[1] and profile.exists()
    with RunLock(home.lock_path):
        code, _, err = tool("logout", "--yes")
        assert code == EXIT_BUSY and "run is in progress" in err and profile.exists()
    code, out, _ = tool("logout", "--yes")
    assert code == EXIT_OK and "deleted" in out and not profile.exists() and other.exists()


def test_the_setup_page_command_hands_over_to_the_server(tool, home, monkeypatch):
    import connecto.ui.server as server
    seen = {}
    monkeypatch.setattr(server, "serve", lambda h, port=0, open_browser=True: seen.update(home=h.root, port=port, open=open_browser) or 0)
    assert tool("ui", "--port", "8123", "--no-open")[0] == 0
    assert seen == {"home": home.root.resolve(), "port": 8123, "open": False}
    tool("ui")
    assert seen["port"] == 0 and seen["open"] is True


def test_words_and_options_that_belong_to_no_command_are_still_refused(tmp_path):
    """(``ai suggest`` takes the words argparse leaves over before Python 3.13; nothing else may.)"""
    for argv in (["run", "stray"], ["ai", "suggest", "--bogus"], ["ai", "key", "stray", "--nope"], ["doctor", "--nope"]):
        with pytest.raises(SystemExit) as exc:
            cli.main([*argv, "--home", str(tmp_path / "home")])
        assert exc.value.code == 2, argv
