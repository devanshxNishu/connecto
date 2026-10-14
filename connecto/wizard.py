"""``connecto init``: a short guided setup in the terminal. Every question has a default; Enter accepts it.

The same settings can be made in the browser (``connecto ui``) or by editing ``config.yaml``; this is just the
shortest path. Nothing is sent anywhere and no LinkedIn password is ever asked for.
"""
from __future__ import annotations

import getpass
import os
from pathlib import Path
from typing import Callable

from . import config as cfgmod
from .paths import Home
from .util import NAME_RULE, as_list, is_secret_name, set_env_var

RISK_TEXT = """\
Before you start, please read this:

  * Connecto drives YOUR LinkedIn account through a real browser window on YOUR computer, slowly, a few
    invitations a day. LinkedIn's User Agreement does not allow automated activity. Accounts that automate can be
    warned, limited or restricted. Connecto keeps the volume low (hard limits in code, not in settings) and stops
    at the first sign of trouble - but it cannot make the risk zero. It is your account and your decision.
  * Use it to reach people who might actually want to hear from you, with notes that are honest and true.
  * Connecto never asks for or stores your LinkedIn password. You sign in yourself, in its browser window.
  * Everything stays on this computer. The only thing that can leave it is what you choose to send to an AI
    provider: a few public facts (job title, company) - never names, links, e-mails or locations.
"""

SOURCE_MENU = [
    ("yc", "Founders from the Y Combinator directory"),
    ("csv", "People in a spreadsheet (CSV) I already have"),
    ("webpage", "People listed on a web page (team, speakers, alumni ...)"),
    ("linkedin_search", "People from a LinkedIn search"),
    ("none", "Nobody yet - I will add people by hand or later"),
]
AI_MENU = [
    ("gemini", "Google Gemini (free tier)"),
    ("groq", "Groq (free tier)"),
    ("openrouter", "OpenRouter (free models)"),
    ("ollama", "Ollama - a model running on this computer"),
    ("custom", "Another OpenAI-compatible server (LM Studio, vLLM, ...)"),
]


class Console:
    """Questions and answers. ``ask`` / ``secret`` / ``say`` are injectable so the wizard can be tested without a tty."""

    def __init__(self, ask: Callable[[str], str] | None = None, secret: Callable[[str], str] | None = None,
                 say: Callable[[str], None] | None = None):
        self._ask = ask or input
        self._secret = secret or getpass.getpass
        self.say = say or print

    def text(self, question: str, default: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        answer = self._ask(f"{question}{suffix}: ").strip()
        return answer or default

    def yes(self, question: str, default: bool = False) -> bool:
        hint = "Y/n" if default else "y/N"
        while True:
            answer = self._ask(f"{question} [{hint}]: ").strip().lower()
            if not answer:
                return default
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False
            self.say("Please answer y or n.")

    def choose(self, question: str, options: list[tuple[str, str]], default: str) -> str:
        self.say(question)
        for i, (_, label) in enumerate(options, 1):
            self.say(f"  {i}) {label}")
        keys = [k for k, _ in options]
        default_no = keys.index(default) + 1 if default in keys else 1
        while True:
            answer = self._ask(f"Choose 1-{len(options)} [{default_no}]: ").strip()
            if not answer:
                return keys[default_no - 1]
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return keys[int(answer) - 1]
            if answer in keys:
                return answer
            self.say("Please type one of the numbers above.")

    def secret(self, question: str) -> str:
        return self._secret(f"{question}: ").strip()


# ----------------------------------------------------------------------------- the steps
def accept_risks(con: Console) -> bool:
    con.say(RISK_TEXT)
    return con.yes("Do you understand this and want to continue?", False)


def ask_about_you(con: Console, cfg: dict) -> None:
    me = cfg["me"]
    me["first_name"] = con.text("Your first name (it signs the note)", me.get("first_name", ""))
    me["about"] = con.text("One short line about you, e.g. 'a product designer in Pune' (optional)", me.get("about", ""))
    if con.yes("Is there something you are building or looking for that you want to mention?", bool(me.get("project"))):
        me["project"] = con.text("What is it called / what is it", me.get("project", ""))
        me["project_blurb"] = con.text("One short clause about it (optional)", me.get("project_blurb", ""))
    plan = con.choose("Which LinkedIn account do you have?",
                      [("free", "Free - notes up to 200 characters, and only a few personal notes a month"),
                       ("premium", "Premium / Sales Navigator - notes up to 300 characters")],
                      cfg["message"].get("plan", "free"))
    cfg["message"]["plan"] = plan
    if con.yes("Write your own note template instead of the built-in friendly ones?", bool(cfg["message"]["templates"])):
        con.say("  Use {first_name}, {company}, {title}, {me_first_name}, {me_about}. Text inside [[double brackets]] is "
                "dropped when a value is missing.")
        own = con.text("Template (one line)", (cfg["message"]["templates"] or [""])[0])
        if own:
            cfg["message"]["templates"] = [own]


def ask_source(con: Console, cfg: dict) -> bool:
    """Ask for one source and add it to cfg. False when the person chose none (or gave nothing usable)."""
    kind = con.choose("Who would you like to meet?", SOURCE_MENU, "none")
    if kind == "none":
        return False
    spec: dict = {"type": kind}
    if kind == "yc":
        spec["batches"] = as_list(con.text("Batches, e.g. S26, W26 (empty = all; newest are invited first)"))
        spec["industries"] = as_list(con.text("Industries, e.g. Fintech, B2B (empty = any)"))
        spec["regions"] = as_list(con.text("Regions, e.g. India, Europe (empty = anywhere)"))
        spec["keywords"] = as_list(con.text("Words about what the company does, e.g. payments, climate (empty = any)"))
        spec = {k: v for k, v in spec.items() if v or k == "type"}
    elif kind == "csv":
        for _ in range(3):
            path = con.text("Path of the CSV file (needs a LinkedIn profile link column; Enter to skip)")
            if not path:
                return False
            if Path(path).expanduser().is_file():
                spec["path"] = path
                break
            con.say("  That file was not found. Try again, or press Enter to skip.")
        else:
            return False
    elif kind == "webpage":
        urls = as_list(con.text("Page address(es), separated by commas"))
        if not urls:
            return False
        spec["urls"] = urls
    elif kind == "linkedin_search":
        con.say("  Type a search such as: head of design fintech india  - or paste the address of a LinkedIn people search.")
        queries, urls = [], []
        while True:
            entry = con.text("Search (empty to finish)" if (queries or urls) else "Search")
            if not entry:
                break
            (urls if entry.lower().startswith("http") else queries).append(entry)
        if not (queries or urls):
            return False
        if queries:
            spec["queries"] = queries
        if urls:
            spec["urls"] = urls
    cfg["sources"].append(spec)
    con.say("  Added. More sources and filters can be set later in `connecto ui` or config.yaml.")
    return True


def _list_answer(con: Console, question: str, current: list[str]) -> list[str]:
    answer = con.text(question, ", ".join(current))
    return [] if answer.strip().lower() in ("-", "none", "no") else as_list(answer)


def ask_targets(con: Console, cfg: dict) -> None:
    con.say("Optional filters for everyone, whatever the source (Enter keeps what is shown, '-' clears):")
    t = cfg["targets"]
    t["keywords"] = _list_answer(con, "  Must mention any of these words (title, headline, company, tags)", t["keywords"])
    t["titles"] = _list_answer(con, "  Job title contains any of (e.g. founder, head of)", t["titles"])
    t["locations"] = _list_answer(con, "  Located in any of (e.g. India, Bangalore)", t["locations"])
    t["exclude_keywords"] = _list_answer(con, "  Never anyone mentioning (e.g. recruiter, agency)", t["exclude_keywords"])


def ask_key_name(con: Console, current: str = "") -> str:
    """The name a custom server's key is saved under in the .env file ('' = the server needs no key)."""
    question = f"Name to save its key under ({NAME_RULE}; empty if it needs no key)"
    while True:
        name = con.text(question, current)
        if not name or is_secret_name(name):
            return name
        con.say("  That name does not say it holds a key.")
        current = ""


def ask_ai(con: Console, home: Home, cfg: dict) -> None:
    from .ai.client import AIClient, AIError, origin_for_new_key
    from .ai.presets import get_preset
    con.say("\nOptional: an AI helper can write one personal opening line per person. It only sees public facts "
            "(title, company, a short description) - never names, links or locations. Connecto works fine without it.")
    if not con.yes("Use an AI helper?", bool(cfg["ai"].get("enabled"))):
        cfg["ai"]["enabled"] = False
        return
    provider = con.choose("Which one?", AI_MENU, cfg["ai"].get("provider", "gemini"))
    preset = get_preset(provider)
    ai = cfg["ai"]
    ai["provider"] = provider
    con.say(f"  {preset.free_note}\n  {preset.privacy_note}")
    if provider == "custom":
        ai["base_url"] = con.text("Server address, e.g. http://localhost:1234/v1", ai.get("base_url", ""))
        ai["model"] = con.text("Model name (empty = choose automatically)", ai.get("model", ""))
        env_name = ask_key_name(con, ai.get("api_key_env", ""))
        ai["api_key_env"] = env_name
    else:
        env_name = preset.key_env
        if provider == "ollama":
            con.say("  Make sure Ollama is running and a model is pulled, e.g. `ollama pull llama3.2`.")
    if env_name and (preset.needs_key or provider == "custom"):
        if preset.signup_url:
            con.say(f"  Get a free key at {preset.signup_url}")
        key = con.secret(f"Paste the key for {env_name} (hidden; saved only in {home.env_path.name}; Enter to skip)")
        if key:
            try:
                # (a key under a name of your own is saved for this one server and goes nowhere else)
                origin = origin_for_new_key(env_name, ai.get("base_url") or preset.base_url)
                home.ensure()
                set_env_var(home.env_path, env_name, key, origin=origin)
                os.environ[env_name] = key
                con.say(f"  Saved to {home.env_path} (readable only by you).")
            except AIError:
                con.say("  The key was not saved: the server address above is not usable for it. Fix the address and run "
                        "`connecto ai key` again.")
            except ValueError as exc:
                con.say(f"  That key could not be saved: {exc}")
    ai["enabled"] = True
    if con.yes("Test the connection now? (one tiny request)", True):
        try:
            result = AIClient.from_config(ai).test()
            con.say(f"  {'OK' if result.ok else 'Problem'}: {result.message}")
            if not result.ok and not con.yes("Keep the AI helper switched on anyway?", False):
                ai["enabled"] = False
        except Exception as exc:  # noqa: BLE001
            con.say(f"  Could not test: {exc}")
            if not con.yes("Keep the AI helper switched on anyway?", False):
                ai["enabled"] = False


def ask_schedule(con: Console, cfg: dict) -> list[str] | None:
    if not con.yes("\nRun by itself every day (while you are logged in to your computer)?", False):
        return None
    times = as_list(con.text("At which times? 24-hour HH:MM, comma separated", ", ".join(cfg["schedule"]["times"])))
    return times or cfg["schedule"]["times"]


# ----------------------------------------------------------------------------- the whole wizard
def run_wizard(home: Home, con: Console | None = None, *, assume_risks_accepted: bool = False,
               install_schedule: Callable[[Home, list[str]], object] | None = None) -> int:
    """Returns 0 when a config was saved, 1 when the person chose not to continue."""
    con = con or Console()
    home.ensure()
    existing = home.config_path.exists()
    if existing:
        cfg = cfgmod.load(home)
        con.say(f"Settings already exist at {home.config_path}. Your current answers are the defaults below; a backup "
                "(config.yaml.bak) is kept.\n")
    else:
        cfg = cfgmod.defaults()
        cfg, _ = cfgmod.normalize(cfg)
        con.say("Welcome to Connecto - a careful, local-first networking assistant. This takes about two minutes.\n")

    if not (assume_risks_accepted or cfg.get("acknowledged_risks")):
        if not accept_risks(con):
            con.say("No problem - nothing was changed.")
            return 1
    cfg["acknowledged_risks"] = True

    ask_about_you(con, cfg)
    if not cfg["sources"] or con.yes(f"\nYou already have {len(cfg['sources'])} source(s). Add another?", False):
        added = ask_source(con, cfg)
        while added and con.yes("Add another source?", False):
            added = ask_source(con, cfg)
    ask_targets(con, cfg)
    ask_ai(con, home, cfg)
    times = ask_schedule(con, cfg)
    if times:
        cfg["schedule"]["times"] = times

    cfg, warnings = cfgmod.normalize(cfg)
    cfgmod.save(home, cfg)
    con.say(f"\nSaved {home.config_path}")
    for w in warnings:
        con.say(f"  note: {w}")

    if times:
        from . import scheduler
        res = (install_schedule or (lambda h, t: scheduler.install(h, t)))(home, times)
        for message in getattr(res, "messages", []):
            con.say("  " + message)
    con.say(next_steps(bool(cfg["sources"])))
    return 0


def next_steps(has_sources: bool) -> str:
    people = ("  2. connecto fetch                     collect people from your sources" if has_sources else
              "  2. connecto add <profile link>        add the people you want to meet (or add a source in `connecto ui`)")
    return f"""
Next steps:
  1. connecto login                     sign in to LinkedIn once, in Connecto's own browser window
{people}
  3. connecto preview                   see who would be invited and what each note would say
  4. connecto run --dry-run --limit 2   watch it work without sending anything
  5. connecto run --limit 3             send your first few
Prefer clicking? `connecto ui` opens the same setup in your browser. `connecto doctor` checks that everything is in place.
"""


def non_interactive_config(home: Home, *, name: str, about: str = "", project: str = "", plan: str | None = None,
                           accept_risks: bool = False) -> dict:
    """Settings from flags only (scripts, CI): starts from the existing config if there is one."""
    cfg = cfgmod.load(home) if home.config_path.exists() else cfgmod.normalize({})[0]
    cfg["me"].update({"first_name": name or cfg["me"].get("first_name", ""), "about": about or cfg["me"].get("about", "")})
    if project:
        cfg["me"]["project"] = project
    if plan in cfgmod.PLANS:                       # (no --plan: keep what is already there)
        cfg["message"]["plan"] = plan
    if accept_risks:
        cfg["acknowledged_risks"] = True
    return cfgmod.normalize(cfg)[0]
