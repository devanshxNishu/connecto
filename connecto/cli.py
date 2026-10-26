"""The ``connecto`` command line. Every command is a thin wrapper over a function that the setup page uses too.

    connecto init          guided setup            connecto ui         the same, in your browser
    connecto login         sign in to LinkedIn once (in Connecto's own browser window)
    connecto fetch         collect people from your sources
    connecto preview       who would be invited, and what each note would say
    connecto run           send (--dry-run first!)  connecto schedule install   run by itself every day
    connecto doctor        is everything in place?  connecto report     what happened so far
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import shutil
import sys
from datetime import timedelta
from pathlib import Path

import yaml

from . import __version__
from . import config as cfgmod
from . import doctor as doctor_mod
from . import manual, planner, runner, scheduler, wizard
from .lock import RunLock
from .paths import Home
from .runner import EXIT_BUSY, EXIT_CONFIG, EXIT_NEEDS_YOU, EXIT_OK, RunOptions
from .store import Store
from .util import clean_text, csv_safe, load_env_into_process, read_regular_file, set_env_var, setup_logging


class CliError(Exception):
    def __init__(self, message: str, code: int = EXIT_CONFIG):
        super().__init__(message)
        self.code = code


# ----------------------------------------------------------------------------- helpers
def _console_safe() -> None:
    """Never crash on a console that cannot show a character (older Windows code pages)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")      # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass


def _context(args, *, need_config: bool = False) -> tuple[Home, dict, list[str]]:
    home = Home(getattr(args, "home", None))
    if need_config and not home.exists():
        raise CliError(f"No settings found in {home.root}.\nRun `connecto init` first (or `connecto ui` for the same in your browser).")
    try:
        cfg, warnings = cfgmod.load(home, with_warnings=True)
    except Exception as exc:  # noqa: BLE001
        first = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
        raise CliError(f"Your settings file could not be read: {first}\n  {home.config_path}") from None
    return home, cfg, warnings


def _show_warnings(warnings: list[str]) -> None:
    for w in warnings:
        print(f"note: {w}", file=sys.stderr)


def _tty() -> bool:
    try:
        return sys.stdin.isatty()
    except Exception:  # noqa: BLE001
        return False


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


# ----------------------------------------------------------------------------- init / ui / risks
def cmd_init(args) -> int:
    home = Home(args.home)
    if args.non_interactive:
        if not args.name:
            raise CliError("--non-interactive needs --name (your first name).")
        cfg = wizard.non_interactive_config(home, name=args.name, about=args.about or "", project=args.project or "",
                                            plan=args.plan, accept_risks=args.accept_risks)
        home.ensure()
        cfgmod.save(home, cfg)
        print(f"Saved {home.config_path}")
        if not cfg["acknowledged_risks"]:
            print("Not ready to send yet: read `connecto risks`, then run again with --accept-risks.")
        return EXIT_OK
    return wizard.run_wizard(home, assume_risks_accepted=args.accept_risks)


def cmd_risks(args) -> int:
    print(wizard.RISK_TEXT)
    return EXIT_OK


def cmd_ui(args) -> int:
    from .ui.server import serve
    return serve(Home(args.home), port=args.port, open_browser=not args.no_open)


# ----------------------------------------------------------------------------- login / fetch / run
def cmd_login(args) -> int:
    home, cfg, warnings = _context(args)
    setup_logging(home.log_path)
    return runner.login(home, cfg, log=logging.getLogger("connecto").info, wait_minutes=args.wait)


def cmd_fetch(args) -> int:
    home, cfg, warnings = _context(args, need_config=True)
    setup_logging(home.log_path)
    _show_warnings(warnings)
    res = runner.run_fetch(home, cfg, only=args.only, limit=args.limit, refresh=args.refresh, skip_browser=args.no_browser,
                           log=logging.getLogger("connecto").info)
    return res.exit_code


def cmd_run(args) -> int:
    home, cfg, warnings = _context(args, need_config=True)
    setup_logging(home.log_path)
    _show_warnings(warnings)
    if not args.dry_run and not args.scheduled and not args.yes:
        if not _tty():
            raise CliError("This would send real invitations. Run it from a terminal to confirm, or pass --yes. "
                           "(Use --dry-run to look without sending.)")
        limit = f"up to {args.limit}" if args.limit else "a few (within your daily limits)"
        answer = input(f"Send {limit} LinkedIn invitations from your account now? [y/N]: ").strip().lower()
        if answer not in ("y", "yes"):
            print("Nothing sent.")
            return EXIT_OK
    fetch = True if args.fetch else (False if args.no_fetch else None)
    opts = RunOptions(dry_run=args.dry_run, limit=args.limit, ignore_hours=args.ignore_hours,
                      ignore_pause=args.ignore_pause, scheduled=args.scheduled, retry_errors=args.retry_errors,
                      fetch=fetch, use_ai=not args.no_ai, log=logging.getLogger("connecto").info)
    try:
        return runner.run_send(home, cfg, opts).exit_code
    except KeyboardInterrupt:
        print("\nInterrupted. If an invitation was being sent at that moment, that person is recorded as 'sending': counted "
              "toward your limits and not tried again by itself - look at 'Sent invitations' on LinkedIn (`connecto report`).")
        return 130


# ----------------------------------------------------------------------------- preview / report
def cmd_queue(args) -> int:
    home, cfg, _ = _context(args, need_config=True)
    with Store(home.db_path) as store:
        page = planner.review_queue(cfg, store, query=args.search, offset=args.offset, limit=args.limit)
    if args.json:
        print(json.dumps(page, indent=2, ensure_ascii=False))
        return EXIT_OK
    print(f"Queue: {page['total']} match this search, {page['queued']} waiting in total.")
    print("Template notes only; reviewing sends nothing and makes no AI calls.")
    if page["ai_enabled"]:
        print("The AI helper may change the note during a run.")
    for number, p in enumerate(page["people"], start=page["offset"] + 1):
        print(f"\n{number}. {p['name'] or '(no name)'}")
        print("   " + " - ".join(x for x in (p["title"], p["company"], p["location"]) if x))
        print(f"   {p['url']}")
        if p["note"]:
            print(f"   Note ({p['chars']}/{page['note_limit']} chars): {p['note']}")
        else:
            reason = "notes are off" if p["note_reason"] == "notes_off" else "no template fits this person"
            print(f"   Plain invitation: {reason}.")
    if not page["people"]:
        print("No people match this search." if page["queued"] else "Nobody is waiting. Fetch people or add someone by hand.")
    if page["has_more"]:
        print(f"\nMore people: repeat with --offset {page['offset'] + page['limit']} (keep the same search and limit).")
    return EXIT_OK


def cmd_preview(args) -> int:
    home, cfg, warnings = _context(args, need_config=True)
    load_env_into_process(home.env_path)
    with Store(home.db_path) as store:
        assistant = None
        if args.ai:
            from .ai import Assistant
            assistant = Assistant.from_config(cfg, store)
            if assistant is None:
                print("note: the AI helper is off or cannot start, so examples use your templates only.", file=sys.stderr)
        pv = planner.build_preview(cfg, store, sample=args.sample, example_notes=args.notes, assistant=assistant,
                                   retry_errors=args.retry_errors)
    if args.json:
        print(json.dumps(pv, indent=2, ensure_ascii=False))
        return EXIT_OK
    print("Sources")
    for line in pv["sources"] or ["(none switched on - add one, or add people by hand with `connecto add`)"]:
        print(f"  - {line}")
    print(f"Filters: {pv['targets']}")
    print(f"People: {pv['people_total']} stored, {pv['matching']} match, {pv['queued']} waiting "
          f"({pv['handled']} already handled)")
    if pv["queued"]:
        print(f"Pace: about {pv['daily_target']} a day -> roughly {_plural(pv['days_estimate'], 'day')} for everyone waiting")
    for name, top in pv["breakdown"].items():
        print(f"  {name}: " + ", ".join(f"{k} ({n})" for k, n in top))
    if pv["sample"]:
        print("\nA few of the people waiting")
        for p in pv["sample"]:
            bits = " - ".join(x for x in (p["title"], p["company"], p["location"]) if x)
            print(f"  {p['name'] or '(no name)'}: {bits}")
    if pv["example_notes"]:
        print(f"\nWhat the notes would say (limit {pv['note_limit']} characters)")
        for ex in pv["example_notes"]:
            print(f"  To {ex['person']} [{ex['kind']}, {ex['chars']} chars]:\n    {ex['note']}")
    elif pv["queued"]:
        print("\nNo note would be written (notes are off, or no template fits); invitations go out plain.")
    if pv["warnings"] or warnings:
        print("\nWorth a look")
        for w in warnings + pv["warnings"]:
            print(f"  ! {w}")
    return EXIT_OK


def cmd_report(args) -> int:
    home = Home(args.home)
    try:
        cfg, warnings = cfgmod.load(home, with_warnings=True)
    except Exception:  # noqa: BLE001 - the history is still worth showing; only the limits shown come from the defaults
        cfg, warnings = cfgmod.normalize({})[0], []
        print(f"note: your settings file could not be read ({home.config_path}), so the limits shown are the defaults.",
              file=sys.stderr)
    if not home.db_path.exists():
        print("Nothing has happened yet." if not args.json else "{}")
        return EXIT_OK
    rep = runner.status_report(home, cfg)
    if args.json:
        print(json.dumps(rep, indent=2, ensure_ascii=False))
        return EXIT_OK
    print(f"Today: {rep['sent_today']} sent (target about {rep['daily_target']}).  Last 7 days: {rep['sent_last_7_days']} "
          f"(weekly cap {rep['weekly_cap']}).  All time: {rep['total_sent']}.")
    print(f"People stored: {rep['people']}  " + ", ".join(f"{k}: {v}" for k, v in rep["people_by_source"].items()))
    if rep["by_status"]:
        print("Outcomes: " + ", ".join(f"{k} {v}" for k, v in sorted(rep["by_status"].items())))
    if rep["paused_until"]:
        print(f"PAUSED until {rep['paused_until'].replace('T', ' ')}: {rep['paused_reason']}")
    if rep["note_blocked_until"]:
        print(f"LinkedIn is limiting personal notes until about {rep['note_blocked_until'][:10]}: invitations go out plain.")
    if rep["ai_calls_today"]:
        print(f"AI helper calls today: {rep['ai_calls_today']}")
    if rep["recent_runs"]:
        print("\nRecent runs")
        for r in rep["recent_runs"]:
            print(f"  {(r['started_at'] or '')[:16].replace('T', ' ')}  {r['mode']:8}  sent {r['sent'] or 0}/{r['attempted'] or 0}"
                  f"  {r['stop_reason'] or 'in progress'}")
    if rep["recent_invites"]:
        print("\nRecent invitations")
        for i in rep["recent_invites"][:10]:
            print(f"  {(i['updated_at'] or '')[:16].replace('T', ' ')}  {i['status']:17} {i['full_name'] or i['linkedin_url']}")
    return EXIT_OK


# ----------------------------------------------------------------------------- schedule
def cmd_schedule(args) -> int:
    # Looking at the schedule and switching it off must work even when the settings file is broken: they need only the folder.
    if args.action == "status":
        st = scheduler.status(Home(args.home))
        print(st["message"])
        for line in st.get("details") or []:
            print(f"  {line}")
        return EXIT_OK
    if args.action == "uninstall":
        res = scheduler.uninstall(Home(args.home), dry_run=args.dry_run)
    else:
        home, cfg, warnings = _context(args)
        if not home.exists():
            raise CliError("No settings found. Run `connecto init` first.")
        problems = cfgmod.problems(cfg)
        if problems and not args.dry_run:
            raise CliError("Not ready to run by itself yet:\n  " + "\n  ".join(problems))
        times = args.times.split(",") if args.times else cfg["schedule"]["times"]
        res = scheduler.install(home, times, dry_run=args.dry_run)
        if res.ok and not args.dry_run and args.times:
            cfg["schedule"]["times"] = [t.strip() for t in times if t.strip()]
            cfgmod.save(home, cfg)
    for m in res.messages:
        print(m)
    return EXIT_OK if res.ok else EXIT_NEEDS_YOU


# ----------------------------------------------------------------------------- people by hand
def cmd_add(args) -> int:
    home, cfg, warnings = _context(args)
    home.ensure()
    people: list[dict] = []
    problems: list[str] = []
    text = "\n".join(args.items)
    if args.name and len(args.items) == 1:
        text = f"{args.name}, {args.company or ''}, {args.title or ''}, {args.items[0]}"
    if text.strip():
        rows, probs = manual.parse_people_text(text)
        people += rows
        problems += probs
    if args.file:
        try:
            data = read_regular_file(Path(args.file).expanduser(), manual.MAX_LIST_FILE_BYTES, what="list of people",
                                     hint=" - that is not a list of people (use --csv for a spreadsheet)")
            people_text = data.decode("utf-8-sig", errors="replace")
        except (OSError, ValueError) as exc:
            raise CliError(f"Cannot read {args.file}: {exc}") from None
        rows, probs = manual.parse_people_text(people_text)
        people += rows
        problems += probs
    if args.csv:
        from .sources import read_csv_rows
        try:
            rows, notes = read_csv_rows(Path(args.csv).expanduser())
        except (OSError, ValueError, csv.Error) as exc:
            raise CliError(f"Cannot read {args.csv}: {exc}") from None
        people += [{**r, "priority": manual.MANUAL_PRIORITY} for r in rows]
        problems += [f"{Path(args.csv).name}: {n}" for n in notes]
    for p in problems:
        print(f"note: {p}", file=sys.stderr)
    if not people:
        raise CliError("No people to add. Give profile links (https://www.linkedin.com/in/...), --file or --csv.")
    with Store(home.db_path) as store:
        added, updated = manual.add_people(store, people)
    nameless = sum(1 for p in people if not p.get("first_name"))
    print(f"Added {_plural(added, 'person', 'people')}" + (f", updated {updated}" if updated else "") + ".")
    if nameless:
        print(f"note: {_plural(nameless, 'person', 'people')} came without a name, so they get a plain invitation (no note). "
              "Add names like: Jane Doe, https://www.linkedin.com/in/jane/")
    return EXIT_OK


def cmd_skip(args) -> int:
    home = Home(args.home)                       # (no settings needed: this only writes to the list of people)
    home.ensure()
    with Store(home.db_path) as store:
        if args.undo:
            n = manual.unskip_urls(store, args.urls)
            print(f"Removed the skip from {_plural(n, 'person', 'people')}.")
            return EXIT_OK
        done, rejected = manual.skip_urls(store, args.urls)
    print(f"Will never contact {_plural(len(done), 'person', 'people')}.")
    for r in rejected:
        print(f"note: not a LinkedIn profile link: {r}", file=sys.stderr)
    return EXIT_OK if done or not rejected else EXIT_CONFIG


MAX_PAUSE_DAYS = 60


def cmd_pause(args) -> int:
    if not 1 <= args.days <= MAX_PAUSE_DAYS:
        raise CliError(f"--days must be between 1 and {MAX_PAUSE_DAYS} (a longer break is better made by simply not running "
                       "Connecto; `connecto schedule uninstall` stops the daily runs).")
    home = Home(args.home)                       # (no settings needed: a broken settings file must not stop you pausing)
    home.ensure()
    with Store(home.db_path) as store:
        runner.set_pause(store, timedelta(days=args.days), clean_text(args.reason or "", 200) or "paused by you", runner.PAUSE_USER)
    print(f"Paused for {_plural(args.days, 'day')}. `connecto resume` lifts it.")
    return EXIT_OK


def cmd_resume(args) -> int:
    home = Home(args.home)
    home.ensure()
    with Store(home.db_path) as store:
        info = runner._pause_info(store)
        kind = runner.pause_kind(store) if info else ""
        runner.clear_pause(store)
        if args.notes:
            store.kv_del("note_blocked_until")
    print("Resumed." + (" Personal notes will be tried again." if args.notes else ""))
    if info and kind != runner.PAUSE_USER:
        print(f"note: that pause was set automatically ({info[1]}). If LinkedIn showed its invitation limit or a security check, "
              "give it a day or two, and start again with `connecto run --dry-run --limit 2`.")
    return EXIT_OK


# ----------------------------------------------------------------------------- AI helper
def cmd_ai(args) -> int:
    from .ai.client import AIClient, AIError, origin_for_new_key
    from .ai.presets import PRESETS, get_preset
    home, cfg, warnings = _context(args)
    load_env_into_process(home.env_path)
    if args.action == "providers":
        for p in PRESETS.values():
            print(f"{p.id:11} {p.label}\n            {p.free_note}\n            {p.privacy_note}"
                  + (f"\n            key: {p.signup_url}" if p.signup_url else ""))
        return EXIT_OK
    if args.action == "key":
        if args.text:
            # (deliberately not echoed back: it may well be the key)
            raise CliError("Do not type the key after the command: it would stay in your shell history and be visible to other "
                           "programs. Run `connecto ai key` and paste it when asked (or use --stdin). If you already typed a "
                           "real key that way, treat it as exposed and make a new one.")
        preset = get_preset(cfg["ai"]["provider"])
        env_name = args.env or cfg["ai"].get("api_key_env") or preset.key_env
        if not env_name:
            raise CliError(f"The {preset.label} setup needs no key. For a custom server, pass --env NAME.")
        if args.remove:
            from .util import remove_env_var
            print(f"Removed {env_name}." if remove_env_var(home.env_path, env_name) else f"{env_name} was not saved.")
            return EXIT_OK
        # A key under a name of your own is tied to the server it is saved for (the one in your settings); a built-in
        # service's own variable needs no such record. (Settled before anything is pasted.)
        try:
            origin = origin_for_new_key(env_name, cfg["ai"].get("base_url") or preset.base_url)
        except AIError as exc:
            raise CliError(f"Set the server address first (ai.base_url in your settings, or the Message tab of `connecto ui`): "
                           f"a key saved under your own name goes to that one server only. ({exc})") from None
        import getpass
        value = (sys.stdin.readline().strip() if args.stdin else getpass.getpass(f"Paste the key for {env_name} (hidden): ").strip())
        if not value:
            raise CliError("No key given.")
        home.ensure()
        try:
            set_env_var(home.env_path, env_name, value, origin=origin)
        except ValueError as exc:
            raise CliError(f"That key cannot be saved: {exc}") from None
        where = f", for {origin} only" if origin else ""
        print(f"Saved {env_name} to {home.env_path} (readable only by you{where}). It is never printed or written to config.yaml.")
        return EXIT_OK
    if args.action == "test":
        try:
            client = AIClient.from_config(cfg["ai"])
        except AIError as exc:
            print(f"Cannot start: {exc}")
            return EXIT_NEEDS_YOU
        result = client.test()
        print(("OK: " if result.ok else "Problem: ") + result.message)
        return EXIT_OK if result.ok else EXIT_NEEDS_YOU
    if args.action == "suggest":
        description = " ".join(args.text).strip()
        if not description:
            raise CliError("Describe the people you want to meet, e.g. `connecto ai suggest \"heads of design at fintech startups in India\"`.")
        from .ai import Assistant
        with Store(home.db_path) as store:
            ai_cfg = {**cfg, "ai": {**cfg["ai"], "enabled": True}}
            assistant = Assistant.from_config(ai_cfg, store)
            if assistant is None:
                raise CliError("The AI helper cannot start - see `connecto ai test`.", EXIT_NEEDS_YOU)
            try:
                result = assistant.suggest_audience(description)
            except AIError as exc:
                raise CliError(str(exc), EXIT_NEEDS_YOU) from None
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if args.apply:
            t = cfg["targets"]
            for key in ("keywords", "titles", "locations", "exclude_keywords"):
                t[key] = list(dict.fromkeys(list(t.get(key) or []) + list(result.get(key) or [])))
            home.ensure()
            cfgmod.save(home, cfg)
            print("Added to your filters (config.yaml). Review them with `connecto config show`.")
        else:
            print("\nThese are suggestions only. Add --apply to put the filters into your settings.")
        return EXIT_OK
    return EXIT_CONFIG


# ----------------------------------------------------------------------------- config / doctor / migrate / export / logout
def cmd_config(args) -> int:
    home, cfg, warnings = _context(args)
    if args.action == "path":
        print(home.config_path)
    elif args.action == "show":
        print(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True, width=110, default_flow_style=None).rstrip())
    else:
        bad = False
        if not home.config_path.exists():
            print(f"There is no settings file yet ({home.config_path}). Run `connecto init`.")
            return EXIT_CONFIG
        for w in warnings:
            print(f"note: {w}")
        for p in cfgmod.problems(cfg):
            print(f"not ready: {p}")
            bad = True
        if not warnings and not bad:
            print("Settings are valid.")
        return EXIT_CONFIG if bad else EXIT_OK
    return EXIT_OK


def cmd_doctor(args) -> int:
    home = Home(args.home)
    checks = doctor_mod.run_checks(home, online=args.online, launch=args.launch)
    if args.json:
        print(json.dumps({"summary": doctor_mod.summarize(checks), "checks": [c.to_dict() for c in checks]},
                         indent=2, ensure_ascii=False))
    else:
        print(doctor_mod.format_report(checks))
    return EXIT_OK if doctor_mod.summarize(checks)["healthy"] else EXIT_NEEDS_YOU


def cmd_migrate(args) -> int:
    from .migrate import migrate_v1
    home = Home(args.home)
    try:
        report = migrate_v1(args.path, home, import_state=not args.no_state, import_people=args.people,
                            write=not args.dry_run)
    except (FileNotFoundError, ValueError) as exc:
        raise CliError(str(exc)) from None
    for note in report.notes:
        print(note)
    for w in report.warnings:
        print(f"warning: {w}")
    return EXIT_OK


def cmd_export(args) -> int:
    home = Home(args.home)
    if not home.db_path.exists():
        raise CliError("Nothing to export yet.")
    with Store(home.db_path) as store:
        rows = store.recent_invites(limit=1_000_000, status=args.status)
    fields = ["status", "full_name", "company", "linkedin_url", "note", "detail", "note_kind", "attempted_at", "updated_at"]
    out = open(args.out, "w", encoding="utf-8", newline="") if args.out else sys.stdout
    try:
        w = csv.DictWriter(out, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: csv_safe(r.get(k)) for k in fields})
    finally:
        if args.out:
            out.close()
    if args.out:
        print(f"Wrote {_plural(len(rows), 'row')} to {args.out}")
    return EXIT_OK


def cmd_logout(args) -> int:
    home = Home(args.home)
    profile = home.browser_dir / "linkedin"
    if RunLock(home.lock_path).is_held():
        raise CliError("A run is in progress; wait for it to finish.", EXIT_BUSY)
    if not profile.exists():
        print("There is no saved LinkedIn session.")
        return EXIT_OK
    if not args.yes:
        if not _tty():
            raise CliError("Pass --yes to confirm deleting the saved LinkedIn session.")
        answer = input(f"Delete Connecto's saved LinkedIn session ({profile})? You will sign in again next time. [y/N]: ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Nothing deleted.")
            return EXIT_OK
    shutil.rmtree(profile, ignore_errors=True)
    print("Saved LinkedIn session deleted. Run `connecto login` to sign in again.")
    return EXIT_OK


# ----------------------------------------------------------------------------- parser
def _add_home_option(parser: argparse.ArgumentParser, *, top: bool) -> None:
    """`--home` works before the command (`connecto --home X run`) and after it (`connecto run --home X`).

    Each parser gets its own option object. Only the top-level one has a default: a sub-command that was not given
    `--home` must stay silent, otherwise its default would overwrite the value given before the command."""
    parser.add_argument("--home", metavar="PATH", default=None if top else argparse.SUPPRESS,
                        help="Connecto's folder (default: $CONNECTO_HOME or ~/.connecto)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="connecto", description="Connecto - a careful, local-first networking assistant for LinkedIn.",
                                epilog="Start with `connecto init` (or `connecto ui`). `connecto doctor` checks your setup.")
    _add_home_option(p, top=True)
    p.add_argument("--version", action="version", version=f"connecto {__version__}")
    sub = p.add_subparsers(dest="command", metavar="<command>")

    def add(name, func, help_, **kw):
        sp = sub.add_parser(name, help=help_, description=help_, **kw)
        _add_home_option(sp, top=False)
        sp.set_defaults(func=func)
        return sp

    sp = add("init", cmd_init, "guided setup in the terminal")
    sp.add_argument("--non-interactive", action="store_true", help="use only the flags below, ask nothing")
    sp.add_argument("--name", help="your first name")
    sp.add_argument("--about", help="one short line about you")
    sp.add_argument("--project", help="what you are building / looking for (optional)")
    sp.add_argument("--plan", choices=cfgmod.PLANS, help="your LinkedIn plan (note length); default: keep what is set (free)")
    sp.add_argument("--accept-risks", action="store_true", help="I have read `connecto risks` and accept them")

    add("risks", cmd_risks, "print the LinkedIn terms-of-service risk note")
    sp = add("ui", cmd_ui, "open the setup page in your browser (local, private)")
    sp.add_argument("--port", type=int, default=0, help="port (default: any free one)")
    sp.add_argument("--no-open", action="store_true", help="do not open a browser window automatically")

    sp = add("login", cmd_login, "sign in to LinkedIn once, in Connecto's own browser window")
    sp.add_argument("--wait", type=float, default=15, help="minutes to wait for you to sign in (default 15)")

    sp = add("fetch", cmd_fetch, "collect people from your sources")
    sp.add_argument("--only", choices=cfgmod.SOURCE_TYPES, help="only this kind of source")
    sp.add_argument("--limit", type=int, default=0, help="stop after about N items (companies / pages)")
    sp.add_argument("--refresh", action="store_true", help="ignore what was fetched before")
    sp.add_argument("--no-browser", action="store_true", help="skip sources that need the LinkedIn browser")

    sp = add("preview", cmd_preview, "who would be invited, and what each note would say (sends nothing)")
    sp.add_argument("--sample", type=int, default=12, help="how many waiting people to list")
    sp.add_argument("--notes", type=int, default=3, help="how many example notes to write")
    sp.add_argument("--ai", action="store_true", help="also use the AI helper for the examples (spends a few calls)")
    sp.add_argument("--retry-errors", action="store_true")
    sp.add_argument("--json", action="store_true")

    sp = add("queue", cmd_queue, "search waiting people and review each template note (sends nothing)")
    sp.add_argument("--search", default="", help="find a name, company, title, location or profile link")
    sp.add_argument("--limit", type=int, default=20, help="people per page (1-100, default 20)")
    sp.add_argument("--offset", type=int, default=0, help="number of matching people to skip")
    sp.add_argument("--json", action="store_true", help="print the page as JSON")

    sp = add("run", cmd_run, "send invitations (within your limits)")
    sp.add_argument("--dry-run", action="store_true", help="visit profiles and show what would happen; send nothing")
    sp.add_argument("--limit", type=int, default=0, help="send at most N this run")
    sp.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    sp.add_argument("--scheduled", action="store_true", help="(used by the daily schedule) wait a random time, notify")
    sp.add_argument("--ignore-hours", action="store_true", help="run outside the active hours/days")
    sp.add_argument("--ignore-pause", action="store_true", help="run although a pause is active")
    sp.add_argument("--retry-errors", action="store_true", help="try profiles that failed earlier")
    sp.add_argument("--fetch", action="store_true", help="fetch people first")
    sp.add_argument("--no-fetch", action="store_true", help="do not fetch people first")
    sp.add_argument("--no-ai", action="store_true", help="do not use the AI helper this run")

    for name in ("report", "status"):
        sp = add(name, cmd_report, "what has been sent, and what is coming")
        sp.add_argument("--json", action="store_true")

    sp = add("schedule", cmd_schedule, "run by itself every day")
    sp.add_argument("action", choices=("install", "uninstall", "status"))
    sp.add_argument("--times", help="24-hour times, comma separated, e.g. 10:40,18:20 (default: from your settings)")
    sp.add_argument("--dry-run", action="store_true", help="show what would change, change nothing")

    sp = add("add", cmd_add, "add people by hand: profile links, a text file, or a CSV")
    sp.add_argument("items", nargs="*", help="profile links, or lines like 'Jane Doe, https://www.linkedin.com/in/jane/'")
    sp.add_argument("--name", help="with exactly one link: the person's name")
    sp.add_argument("--company")
    sp.add_argument("--title")
    sp.add_argument("--file", help="a text file with one person per line")
    sp.add_argument("--csv", help="a CSV file with a LinkedIn profile link column")

    sp = add("skip", cmd_skip, "never contact these profiles")
    sp.add_argument("urls", nargs="+")
    sp.add_argument("--undo", action="store_true", help="remove the skip again")

    sp = add("pause", cmd_pause, "stop sending for a while")
    sp.add_argument("--days", type=int, default=7)
    sp.add_argument("--reason")
    sp = add("resume", cmd_resume, "lift a pause")
    sp.add_argument("--notes", action="store_true", help="also try personal notes again")

    sp = add("ai", cmd_ai, "the optional AI helper: providers, key, test, audience suggestions")
    sp.add_argument("action", choices=("providers", "key", "test", "suggest"))
    sp.add_argument("text", nargs="*", help="for `suggest`: describe who you want to meet")
    sp.add_argument("--env", help="for `key`: the environment variable name to save under")
    sp.add_argument("--stdin", action="store_true", help="for `key`: read the key from standard input")
    sp.add_argument("--remove", action="store_true", help="for `key`: delete the saved key")
    sp.add_argument("--apply", action="store_true", help="for `suggest`: put the suggested filters into your settings")

    sp = add("config", cmd_config, "show where the settings live, print or check them")
    sp.add_argument("action", choices=("path", "show", "check"), nargs="?", default="check")

    sp = add("doctor", cmd_doctor, "check that everything is in place")
    sp.add_argument("--online", action="store_true", help="also test the AI helper (one tiny request)")
    sp.add_argument("--launch", action="store_true", help="also start a throw-away browser to prove it works")
    sp.add_argument("--json", action="store_true")

    sp = add("migrate-v1", cmd_migrate, "bring over the older yc-founder-connect settings and history")
    sp.add_argument("path", help="the yc-founder-connect folder (the one with config.yaml in it)")
    sp.add_argument("--dry-run", action="store_true", help="show what would happen, write nothing")
    sp.add_argument("--no-state", action="store_true", help="do not copy the invitation history")
    sp.add_argument("--people", action="store_true", help="also load its founders.csv so YC need not be scraped again")

    sp = add("export", cmd_export, "write the invitation history to a CSV file")
    sp.add_argument("--out", help="file to write (default: standard output)")
    sp.add_argument("--status", help="only this outcome, e.g. sent")

    sp = add("logout", cmd_logout, "delete Connecto's saved LinkedIn session")
    sp.add_argument("--yes", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    _console_safe()
    parser = build_parser()
    args, extra = parser.parse_known_args(argv)
    if extra:
        # Before Python 3.13, argparse leaves words that come after an option between two positionals unparsed:
        # in `connecto ai suggest --apply founders of fintech` they belong to the description, nothing else does.
        if getattr(args, "command", None) == "ai" and not any(e.startswith("-") for e in extra):
            args.text = [*(args.text or []), *extra]
        else:
            parser.error("unrecognized arguments: " + " ".join(extra))
    if not getattr(args, "func", None):
        home = Home(args.home)
        print(f"Connecto {__version__} - a careful, local-first networking assistant.\n")
        if home.exists():
            print("Your settings are in " + str(home.root) + ".\n  connecto preview      who is waiting\n  connecto run --dry-run --limit 2\n"
                  "  connecto ui           the setup page\n  connecto doctor       check everything\n  connecto --help       all commands")
        else:
            print("Nothing is set up yet. Start with:\n  connecto init         guided setup in the terminal\n"
                  "  connecto ui           the same, in your browser")
        return EXIT_OK
    try:
        return int(args.func(args) or 0)
    except CliError as exc:
        print(f"connecto: {exc}", file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
