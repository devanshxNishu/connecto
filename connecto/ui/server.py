"""The setup page: one small HTTP server on 127.0.0.1, a JSON API, and one static HTML file.

Everything the page can do is a call to the same functions the command line uses (runner, planner, config, scheduler,
doctor, manual). The server adds only what a browser needs: a session token, validation, and a background-job runner
so a fetch or a send does not block the page.

Security model (the page is local, but a local port is reachable by every other website you have open):
  * bound to 127.0.0.1 only, on a free port, with three different random values per launch: the *link token* in the
    printed link, a *session cookie* and an *API secret*;
  * the link works once. It is exchanged for the session cookie (HttpOnly, SameSite=Strict, named after the port so two
    copies on one machine do not overwrite each other), and after that it opens nothing - a link that was copied from a
    terminal, a screenshot or the shell history is worth nothing once you have used it;
  * the page is handed out only to a request that holds the session cookie, and it carries the API secret in its own HTML;
    every ``/api`` call needs the cookie *and* the ``X-Connecto-Token`` header holding that secret - so another site can
    neither read the page nor forge a request;
  * the ``Host`` header must be 127.0.0.1/localhost on our port (this defeats DNS rebinding), a present ``Origin`` must
    match, state-changing calls must be ``application/json``, bodies are capped, and there is no CORS at all;
  * secrets are write-only: an API key is saved to ``.env`` (0600) and never sent back, logged or put in config.yaml;
  * the page itself builds everything with textContent, because names, titles and notes come from other people's pages.

Quitting never cuts a send off in the middle of a profile: while a job is running, "Quit" only asks it to stop, and Ctrl+C
in the terminal waits for it (a second Ctrl+C leaves at once).
"""
from __future__ import annotations

import collections
import hmac
import json
import logging
import os
import random
import re
import secrets
import threading
import traceback
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .. import __version__
from .. import config as cfgmod
from .. import doctor as doctor_mod
from .. import manual, notes, planner, runner, scheduler, wizard
from ..limits import HARD
from ..lock import RunLock
from ..paths import Home
from ..store import Store
from ..util import (NAME_RULE, RedactFilter, csv_safe, is_secret_name, load_env_into_process, read_env_file,
                    remove_env_var, set_env_var, setup_logging)

LOG = logging.getLogger("connecto")
COOKIE_PREFIX = "connecto_session"
HEADER = "X-Connecto-Token"
MAX_BODY = 1_000_000
MAX_LOG_LINES = 3000
LINE_CHARS = 600                      # a log line is cut to this many characters
IDLE_SECONDS = 15                     # a connection that says nothing for this long is dropped
WAIT_FOR_JOB_SECONDS = 150            # how long Ctrl+C waits for a running job to stop at the next profile
STATIC = Path(__file__).with_name("static") / "index.html"
JOB_KINDS = ("login", "fetch", "dry_run", "run")


def cookie_name(port: int) -> str:
    """Cookies are shared between ports on the same host, so each copy of the page names its cookie after its port."""
    return f"{COOKIE_PREFIX}_{int(port)}"


class NotFinite(ValueError):
    """A number in a request that is NaN, infinite or too large for a computer (1e999)."""


def _reject_constant(name: str):
    raise NotFinite(f"{name} is not allowed")


def _finite_float(text: str) -> float:
    value = float(text)
    if value != value or value in (float("inf"), float("-inf")):
        raise NotFinite(f"the number {text[:20]} is too large")
    return value


_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def _scrubbed(value):
    """``value`` with every lone surrogate in any text (and any key) replaced by U+FFFD. JSON can carry half of an emoji - a
    browser string cut in the middle of a pair - and Python then holds text it can neither write to a file nor send back."""
    if isinstance(value, str):
        return _LONE_SURROGATE.sub("\ufffd", value)
    if isinstance(value, list):
        return [_scrubbed(item) for item in value]
    if isinstance(value, dict):
        return {_scrubbed(key): _scrubbed(item) for key, item in value.items()}
    return value


def parse_body(raw: bytes) -> dict:
    """The JSON object in a request body. ValueError (never anything else) for everything that is not one: bad text, NaN,
    Infinity, 1e999, a number with thousands of digits, a document nested so deeply that Python gives up. Text in it never
    holds a lone surrogate."""
    try:
        body = _scrubbed(json.loads(raw.decode("utf-8") or "{}", parse_constant=_reject_constant, parse_float=_finite_float))
    except NotFinite:
        raise ValueError("Body is not valid JSON: numbers must be finite (no NaN, Infinity or 1e999).") from None
    except (ValueError, UnicodeDecodeError):
        raise ValueError("Body is not valid JSON.") from None
    except RecursionError:
        raise ValueError("Body is not valid JSON (nested too deeply).") from None
    if not isinstance(body, dict):
        raise ValueError("Body must be a JSON object.")
    return body


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ============================================================================= background jobs
_REDACT = RedactFilter()


def clean_result(result) -> dict:
    """What a finished job tells the page: numbers, flags and short text with every key value masked - never an object, and
    never more than a line of text per entry (the exception text of a crash is not trusted to be free of secrets)."""
    out: dict = {}
    for key, value in (result.items() if isinstance(result, dict) else ()):
        if value is None or isinstance(value, (bool, int)):
            out[str(key)] = value
        elif isinstance(value, float) and value == value and abs(value) != float("inf"):
            out[str(key)] = value
        elif isinstance(value, str):
            out[str(key)] = _REDACT.redact(value)[:LINE_CHARS]
    return out


class Job:
    def __init__(self, kind: str):
        self.id = secrets.token_hex(4)
        self.kind = kind
        self.state = "running"                  # running | finished | stopped | failed
        self.started = datetime.now().isoformat(timespec="seconds")
        self.finished = ""
        self.result: dict = {}
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self._lines: collections.deque = collections.deque(maxlen=MAX_LOG_LINES)
        self._total = 0
        self._lock = threading.Lock()

    def log(self, message) -> None:
        """A line the job itself prints (a runner's progress): shown on the page *and* written to the run log, where a run
        started from the command line puts it too."""
        for line in self._add(message):
            LOG.info("%s", line, extra={"from_job": True})

    def _add(self, message) -> list[str]:
        text = _REDACT.redact(str(message).rstrip())      # whatever a runner prints, an API key never reaches the page
        lines = [line[:LINE_CHARS] for line in (text.splitlines() or [text]) if line.strip()]
        with self._lock:
            for line in lines:
                self._lines.append(line)
                self._total += 1
        return lines

    def lines_since(self, since: int) -> tuple[list[str], int]:
        with self._lock:
            first = self._total - len(self._lines)
            start = max(int(since), first)
            return list(self._lines)[start - first:], self._total

    def summary(self) -> dict:
        return {"id": self.id, "kind": self.kind, "state": self.state, "started": self.started,
                "finished": self.finished, "result": self.result}


class _JobLogHandler(logging.Handler):
    """Copies the 'connecto' logger's records into the job's log while it runs (login prompts, dialog notes ...). A line the
    job wrote to the log itself is not copied back (it is already there)."""

    def __init__(self, job: Job):
        super().__init__(level=logging.INFO)
        self.job = job
        self.addFilter(RedactFilter())

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(record, "from_job", False):
            return
        try:
            self.job._add(record.getMessage())
        except Exception:  # noqa: BLE001
            pass


class JobRunner:
    """At most one background job at a time (the run lock would refuse a second one anyway)."""

    def __init__(self):
        self.current: Job | None = None
        self._lock = threading.Lock()

    def active(self) -> Job | None:
        job = self.current
        return job if job is not None and job.state == "running" else None

    def start(self, kind: str, target) -> Job:
        with self._lock:
            if self.active() is not None:
                raise ApiError(f"A {self.current.kind.replace('_', ' ')} is already running - wait for it or press Stop.", 409)
            job = Job(kind)
            self.current = job
        handler = _JobLogHandler(job)

        def work() -> None:
            LOG.addHandler(handler)
            try:
                result = clean_result(target(job))
                job.result = result
                code = result.get("exit_code", 0)
                job.state = "stopped" if job.stop.is_set() else ("finished" if code in (0, None) else "failed")
            except Exception as exc:  # noqa: BLE001
                job.log(f"Stopped by an unexpected problem: {type(exc).__name__}: {exc}")
                LOG.debug(traceback.format_exc())
                job.result = clean_result({"exit_code": 1, "error": f"{type(exc).__name__}: {exc}"})
                job.state = "failed"
            finally:
                LOG.removeHandler(handler)
                job.finished = datetime.now().isoformat(timespec="seconds")

        job.thread = threading.Thread(target=work, name=f"connecto-job-{kind}", daemon=True)
        job.thread.start()
        return job

    def stop(self) -> bool:
        """Ask the running job to finish the profile it is on and stop. False if nothing is running."""
        job = self.active()
        if job is None:
            return False
        job.stop.set()
        if job.kind == "login":
            job.log("Stop requested - closing the sign-in window.")
        else:
            job.log("Stop requested - finishing the profile in progress, then stopping.")
        return True

    def wait(self, seconds: float) -> bool:
        """Wait up to ``seconds`` for the running job (if any) to end. True when nothing is running any more."""
        job = self.current
        if job is not None and job.thread is not None:
            job.thread.join(max(0.0, seconds))
        return self.active() is None


# ============================================================================= the application (what the routes call)
class App:
    def __init__(self, home: Home, token: str, port: int, *, shutdown=None, secret: str | None = None):
        self.home = home
        self.token = token                                   # in the printed link; good for one sign-in
        self.secret = secret or secrets.token_urlsafe(32)    # what the page's own requests carry in the header
        self.cookie = cookie_name(port)
        self.port = port
        self.jobs = JobRunner()
        self.shutdown = shutdown or (lambda: None)
        self._cfg_lock = threading.Lock()
        self._session = ""                                   # the cookie's value, made when the link is used
        self._auth_lock = threading.Lock()

    # ------------------------------------------------------------------ signing in
    def sign_in(self, link_token: str) -> str | None:
        """Trade the link's token for the session cookie's value. The link works once: None if it is wrong or was used."""
        with self._auth_lock:
            if self._session or not link_token or not hmac.compare_digest(link_token.encode(), self.token.encode()):
                return None
            self._session = secrets.token_urlsafe(32)
            return self._session

    def is_session(self, value: str) -> bool:
        session = self._session
        return bool(session) and bool(value) and hmac.compare_digest(value.encode(), session.encode())

    # ------------------------------------------------------------------ helpers
    def load_cfg(self) -> tuple[dict, list[str]]:
        try:
            return cfgmod.load(self.home, with_warnings=True)
        except Exception as exc:  # noqa: BLE001
            first = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
            raise ApiError(f"The settings file could not be read: {first}", 500) from None

    def store(self) -> Store:
        self.home.ensure()
        return Store(self.home.db_path)

    def key_presence(self, cfg: dict) -> dict[str, bool]:
        """Which key settings have a key. A built-in service's usual variable may also come from your own environment; any
        other name counts only when it is saved in Connecto's .env file, because that is the only place Connecto reads it."""
        from ..ai.presets import PRESETS
        usual = {p.key_env for p in PRESETS.values() if p.key_env}
        saved = read_env_file(self.home.env_path)
        out = {n: bool(saved.get(n) or os.environ.get(n)) for n in usual}
        own = cfg["ai"].get("api_key_env")
        if own and own not in usual:
            out[own] = bool(saved.get(own))
        return dict(sorted(out.items()))

    def providers(self) -> list[dict]:
        from ..ai.presets import PRESETS
        return [{"id": p.id, "label": p.label, "signup_url": p.signup_url, "needs_key": p.needs_key, "local": p.local,
                 "key_env": p.key_env, "free_note": p.free_note, "privacy_note": p.privacy_note,
                 "base_url": p.base_url} for p in PRESETS.values()]

    # ------------------------------------------------------------------ GET /api/state
    def state(self) -> dict:
        cfg, warnings = self.load_cfg()
        status, queue = None, {"matching": 0, "queued": 0}
        if self.home.db_path.exists():
            status = runner.status_report(self.home, cfg)
            with self.store() as store:
                queue = planner.queue_counts(cfg, store)
        job = self.jobs.current
        return {
            "version": __version__, "home": str(self.home.root), "has_config": self.home.config_path.exists(),
            "config": cfg, "warnings": warnings, "problems": cfgmod.problems(cfg), "status": status, "queue": queue,
            "schedule": scheduler.status(self.home), "job": job.summary() if job else None,
            "busy_elsewhere": RunLock(self.home.lock_path).is_held() and self.jobs.active() is None,
            "ai": {"providers": self.providers(), "key_present": self.key_presence(cfg)},
            "ceilings": HARD, "plans": cfgmod.PLAN_NOTE_CHARS, "note_limit": cfgmod.note_limit(cfg),
            "risk_text": wizard.RISK_TEXT, "source_types": list(cfgmod.SOURCE_TYPES),
            "default_templates": cfgmod.DEFAULT_TEMPLATES, "default_ai_templates": cfgmod.DEFAULT_AI_TEMPLATES,
        }

    # ------------------------------------------------------------------ POST /api/config
    def save_config(self, body: dict) -> dict:
        incoming = body.get("config")
        if not isinstance(incoming, dict):
            raise ApiError("Expected {\"config\": {...}}.")
        with self._cfg_lock:
            cfg, warnings = cfgmod.normalize(incoming)
            cfgmod.save(self.home, cfg)
        return {"config": cfg, "warnings": warnings, "problems": cfgmod.problems(cfg), "note_limit": cfgmod.note_limit(cfg)}

    # ------------------------------------------------------------------ AI key / test / suggest
    def ai_key(self, body: dict) -> dict:
        from ..ai.client import AIError, origin_for_new_key
        from ..ai.presets import get_preset
        env = body.get("env")
        env = env.strip() if isinstance(env, str) else ""
        if not is_secret_name(env):
            raise ApiError(f"Invalid key name: use {NAME_RULE}.")
        if body.get("remove") is True:
            removed = remove_env_var(self.home.env_path, env)
            os.environ.pop(env, None)
            return {"saved": False, "removed": removed}
        value = body.get("value")
        value = value.strip() if isinstance(value, str) else ""
        if not value:
            raise ApiError("Paste the key first.")
        # A key under a name of your own is tied to the server it is saved for: the address on the screen, else the saved one.
        address = body.get("base_url")
        if address is not None and not isinstance(address, str):
            raise ApiError("The server address must be text.")
        if not (address or "").strip():
            ai = self.load_cfg()[0]["ai"]
            address = ai.get("base_url") or get_preset(ai["provider"]).base_url
        try:
            origin = origin_for_new_key(env, address)
        except AIError:
            raise ApiError("Fill in the server address first (https://host/v1, or http://localhost:1234/v1 for a server on "
                           "this computer): a key you name yourself is saved for that one server only.") from None
        self.home.ensure()
        try:
            set_env_var(self.home.env_path, env, value, origin=origin)
        except ValueError as exc:
            raise ApiError(f"That key cannot be saved: {exc}") from None
        os.environ[env] = value
        return {"saved": True, "origin": origin or ""}           # (the value is never sent back)

    def _ai_cfg(self) -> dict:
        cfg, _ = self.load_cfg()
        load_env_into_process(self.home.env_path)
        cfg = dict(cfg)
        cfg["ai"] = {**cfg["ai"], "enabled": True}
        return cfg

    def ai_test(self, body: dict) -> dict:
        from ..ai.client import AIClient, AIError
        cfg = self._ai_cfg()
        incoming = body.get("ai")
        if isinstance(incoming, dict):                           # test what is on screen before it is saved
            probe, _ = cfgmod.normalize({"ai": {**incoming, "enabled": True}})
            cfg["ai"] = probe["ai"]
        try:
            client = AIClient.from_config(cfg["ai"])
        except AIError as exc:
            return {"ok": False, "message": str(exc)}
        result = client.test()
        return {"ok": result.ok, "message": result.message, "model": result.model}

    def ai_suggest(self, body: dict) -> dict:
        from ..ai import Assistant
        from ..ai.client import AIError
        description = str(body.get("description") or "").strip()
        cfg = self._ai_cfg()
        with self.store() as store:
            assistant = Assistant.from_config(cfg, store)
            if assistant is None:
                raise ApiError("The AI helper cannot start - open the Message tab and press 'Test connection'.")
            try:
                return {"suggestions": assistant.suggest_audience(description)}
            except AIError as exc:
                raise ApiError(str(exc)) from None

    # ------------------------------------------------------------------ preview / people
    def preview(self, query: dict) -> dict:
        cfg, _ = self.load_cfg()
        sample = max(0, min(int(query.get("sample", ["12"])[0]), 50))
        use_ai = query.get("ai", ["0"])[0] == "1"
        load_env_into_process(self.home.env_path)
        with self.store() as store:
            assistant = None
            if use_ai:
                from ..ai import Assistant
                assistant = Assistant.from_config(cfg, store)
            pv = planner.build_preview(cfg, store, sample=sample, example_notes=3 if sample else 0, assistant=assistant,
                                       rng=random.Random())
        pv["ai_used"] = assistant is not None
        return pv

    def people(self, query: dict) -> dict:
        limit = max(1, min(int(query.get("limit", ["50"])[0]), 200))
        offset = max(0, int(query.get("offset", ["0"])[0]))
        text = (query.get("q", [""])[0] or "").strip().lower()
        only = query.get("status", [""])[0]
        with self.store() as store:
            statuses = store.statuses()
            rows = []
            for p in store.iter_people():
                status = statuses.get(p["key"], "waiting")
                if only and status != only:
                    continue
                if text and text not in " ".join(str(p.get(k, "")) for k in ("full_name", "company", "title", "headline",
                                                                                "location", "linkedin_url")).lower():
                    continue
                rows.append({"name": p.get("full_name", ""), "company": p.get("company", ""),
                             "title": p.get("title") or p.get("headline", ""), "location": p.get("location", ""),
                             "url": p.get("linkedin_url", ""), "source": p.get("source_type", ""), "status": status})
            total = len(rows)
        return {"total": total, "people": rows[offset:offset + limit]}

    def add_people(self, body: dict) -> dict:
        people, problems = manual.parse_people_text(str(body.get("text") or ""))
        if not people:
            raise ApiError(problems[0] if problems else "Paste at least one LinkedIn profile link.")
        with self.store() as store:
            added, updated = manual.add_people(store, people)
        return {"added": added, "updated": updated, "problems": problems,
                "nameless": sum(1 for p in people if not p.get("first_name"))}

    def skip_people(self, body: dict) -> dict:
        urls = re.findall(r"\S+", str(body.get("text") or ""))
        if not urls:
            raise ApiError("Paste at least one LinkedIn profile link.")
        with self.store() as store:
            if body.get("undo"):
                return {"undone": manual.unskip_urls(store, urls)}
            done, rejected = manual.skip_urls(store, urls)
        return {"skipped": len(done), "rejected": rejected}


    # ------------------------------------------------------------------ POST /api/render (template editor preview)
    SAMPLE_PERSON = {"linkedin_url": "https://www.linkedin.com/in/example/", "full_name": "Priya Shah", "first_name": "Priya",
                     "company": "Acme Pay", "title": "Co-founder & CEO", "location": "Pune, India",
                     "one_liner": "Payments infrastructure for small businesses"}
    SAMPLE_HOOK = "Building payments infrastructure for small businesses at Acme Pay is impressive work."

    def render_templates(self, body: dict) -> dict:
        incoming = body.get("config")
        cfg, _ = cfgmod.normalize(incoming if isinstance(incoming, dict) else {})
        person, real = dict(self.SAMPLE_PERSON), False
        if self.home.db_path.exists():
            with self.store() as store:
                for p in store.iter_people():
                    if p.get("first_name") and p.get("company"):
                        person, real = p, True
                        break
        limit = cfgmod.note_limit(cfg)
        ctx = notes.build_context(person, cfg["me"])
        results = []
        for template in cfgmod.templates(cfg):
            uses_hook = notes.uses_var(template, "hook")
            ctx_t = dict(ctx, hook=self.SAMPLE_HOOK) if uses_hook else ctx
            missing = sorted(v for v in notes.required_vars(template) if not (ctx_t.get(v) or "").strip())
            rendered = notes.compose([template], ctx_t, limit)
            results.append({"template": template, "text": rendered.text if rendered else "", "chars": len(rendered.text) if rendered else 0,
                            "ok": rendered is not None, "missing": missing, "uses_hook": uses_hook,
                            "problems": notes.template_problems(template)})
        return {"person": {"name": person.get("full_name", ""), "company": person.get("company", ""),
                           "title": person.get("title", ""), "real": real},
                "limit": limit, "results": results, "builtin": not cfg["message"]["templates"]}

    # ------------------------------------------------------------------ pause / resume / schedule
    def pause(self, body: dict) -> dict:
        try:
            days = max(1, min(int(body.get("days", 7)), 60))
        except (TypeError, ValueError, OverflowError):
            raise ApiError("days must be a number") from None
        with self.store() as store:
            runner.set_pause(store, timedelta(days=days), "paused by you")
        return {"ok": True}

    def resume(self, body: dict) -> dict:
        with self.store() as store:
            runner.clear_pause(store)
            if body.get("notes"):
                store.kv_del("note_blocked_until")
        return {"ok": True}

    def schedule(self, body: dict) -> dict:
        action = str(body.get("action") or "")
        if action == "uninstall":
            res = scheduler.uninstall(self.home)
        elif action == "install":
            cfg, _ = self.load_cfg()
            problems = cfgmod.problems(cfg)
            if problems:
                raise ApiError("Not ready to run by itself yet: " + " ".join(problems))
            times = body.get("times") or cfg["schedule"]["times"]
            if isinstance(times, str):
                times = [t for t in re.split(r"[,\s]+", times) if t]
            res = scheduler.install(self.home, times)
        else:
            raise ApiError("action must be install or uninstall")
        return {"ok": res.ok, "messages": res.messages, "schedule": scheduler.status(self.home)}

    # ------------------------------------------------------------------ doctor / history
    def doctor(self, query: dict) -> dict:
        checks = doctor_mod.run_checks(self.home, online=query.get("online", ["0"])[0] == "1",
                                       launch=query.get("launch", ["0"])[0] == "1")
        return {"summary": doctor_mod.summarize(checks), "checks": [c.to_dict() for c in checks]}

    def history(self) -> dict:
        if not self.home.db_path.exists():
            return {"runs": [], "invites": []}
        with self.store() as store:
            return {"runs": store.recent_runs(15), "invites": store.recent_invites(100)}

    def export_csv(self) -> str:
        import csv
        import io
        buf = io.StringIO()
        fields = ["status", "full_name", "company", "linkedin_url", "note", "detail", "note_kind", "attempted_at", "updated_at"]
        w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        if self.home.db_path.exists():
            with self.store() as store:
                for r in store.recent_invites(limit=1_000_000):
                    w.writerow({k: csv_safe(r.get(k)) for k in fields})
        return buf.getvalue()

    # ------------------------------------------------------------------ jobs
    def start_job(self, body: dict) -> dict:
        kind = str(body.get("kind") or "")
        if kind not in JOB_KINDS:
            raise ApiError(f"kind must be one of {', '.join(JOB_KINDS)}")
        cfg, _ = self.load_cfg()
        if not cfg.get("acknowledged_risks"):
            raise ApiError("Read and accept the safety note on the Start tab first.")
        if kind == "run" and body.get("confirm") is not True:
            raise ApiError("Sending needs your confirmation.", 400)
        if kind in ("run", "dry_run"):
            problems = cfgmod.problems(cfg)
            if problems:
                raise ApiError("Not ready yet: " + " ".join(problems))
        try:
            limit = max(0, min(int(body.get("limit") or 0), HARD["per_run_max"]))
        except (TypeError, ValueError, OverflowError):
            raise ApiError("limit must be a number") from None
        only = body.get("only")
        if only is not None and not isinstance(only, str):
            raise ApiError("only must be text")
        home = self.home

        def work(job: Job) -> dict:
            if kind == "login":
                code = runner.login(home, cfg, log=job.log, wait_minutes=15, stop=job.stop.is_set)
                return {"exit_code": code}
            if kind == "fetch":
                res = runner.run_fetch(home, cfg, only=only or None, limit=0, log=job.log, stop=job.stop.is_set)
                return {"exit_code": res.exit_code, "added": res.sent, "stop_reason": res.stop_reason}
            # Only what the page offers can be asked for: a run keeps a pause that LinkedIn caused (or you set) and does not
            # retry people it already failed on; leaving the active hours has to be a plain `true`, like the confirmation.
            opts = runner.RunOptions(dry_run=kind == "dry_run", limit=limit or (2 if kind == "dry_run" else 0),
                                     ignore_hours=body.get("ignore_hours") is True, use_ai=body.get("use_ai") is not False,
                                     log=job.log, stop=job.stop.is_set)
            res = runner.run_send(home, cfg, opts)
            return {"exit_code": res.exit_code, "sent": res.sent, "attempted": res.attempted, "stop_reason": res.stop_reason,
                    "dry_run": res.dry_run}

        return self.jobs.start(kind, work).summary()

    def job(self, query: dict) -> dict:
        job = self.jobs.current
        if job is None:
            return {"job": None, "lines": [], "next": 0}
        since = int(query.get("since", ["0"])[0] or 0)
        lines, nxt = job.lines_since(since if query.get("id", [job.id])[0] == job.id else 0)
        return {"job": job.summary(), "lines": lines, "next": nxt}


# ============================================================================= HTTP
def make_handler(app: App):
    allowed_hosts = {f"127.0.0.1:{app.port}", f"localhost:{app.port}"}
    allowed_origins = {f"http://{h}" for h in allowed_hosts}

    class Handler(BaseHTTPRequestHandler):
        server_version = "Connecto"
        sys_version = ""
        protocol_version = "HTTP/1.1"
        timeout = IDLE_SECONDS                      # a client that goes quiet does not hold a thread for ever

        def log_message(self, fmt, *args):          # no access log: URLs carry the token once
            pass

        # -------------------------------------------------------------- plumbing
        def _headers(self, status: int, ctype: str, length: int, extra: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            if status >= 400:                       # an unread request body must not leak into the next request
                self.send_header("Connection", "close")
                self.close_connection = True
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()

        def _send(self, status: int, body: bytes, ctype: str = "text/plain; charset=utf-8", extra: dict | None = None) -> None:
            self._headers(status, ctype, len(body), extra)
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, obj) -> None:
            # Plain ASCII on the wire (every other character is a \\u escape): whatever text the data holds - even half of an
            # emoji that got into a file - can be sent, so one odd character never keeps the page from loading.
            self._send(status, json.dumps(obj, allow_nan=False).encode("ascii"), "application/json; charset=utf-8")

        def _signed_in(self) -> bool:
            """Does the request carry our session cookie? (Any cookie of that name will do: another program on this machine
            can set one with the same name, and the first of two must not decide.)"""
            for header in self.headers.get_all("Cookie") or ():
                for part in header.split(";"):
                    name, _, value = part.strip().partition("=")
                    if name == app.cookie and app.is_session(value):
                        return True
            return False

        def _same(self, a: str, b: str) -> bool:
            return bool(a) and hmac.compare_digest(a.encode(), b.encode())

        def _host_ok(self) -> bool:
            return (self.headers.get("Host") or "").lower() in allowed_hosts

        def _origin_ok(self) -> bool:
            origin = self.headers.get("Origin")
            if origin is not None and origin not in allowed_origins:
                return False
            site = self.headers.get("Sec-Fetch-Site")
            return site is None or site in ("same-origin", "none")

        def _authorised(self) -> bool:
            return self._signed_in() and self._same(self.headers.get(HEADER) or "", app.secret)

        # -------------------------------------------------------------- dispatch
        def _handle(self, method: str) -> None:
            try:
                if not self._host_ok():
                    return self._send(403, b"Forbidden: unexpected Host header.")
                url = urlsplit(self.path)
                if url.path == "/" and method in ("GET", "HEAD"):
                    return self._page(parse_qs(url.query))
                if not url.path.startswith("/api/"):
                    return self._send(404, b"Not found")
                if not self._origin_ok():
                    return self._json(403, {"error": "Request refused (cross-site)."})
                if not self._authorised():
                    return self._json(401, {"error": "Not signed in to the setup page. Open the link `connecto ui` printed."})
                route = ROUTES.get((method, url.path))
                if route is None:
                    return self._json(404 if not any(p == url.path for _, p in ROUTES) else 405, {"error": "No such action."})
                query = parse_qs(url.query)
                body: dict = {}
                if method == "POST":
                    if not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
                        return self._json(415, {"error": "Expected application/json."})
                    try:
                        length = int(self.headers.get("Content-Length") or "0")
                    except ValueError:
                        return self._json(400, {"error": "Bad Content-Length."})
                    if length < 0 or length > MAX_BODY:
                        return self._json(413, {"error": "Request too large."})
                    try:
                        raw = self.rfile.read(length) if length else b""
                    except (TimeoutError, OSError):
                        self.close_connection = True
                        return self._json(408, {"error": "The request did not arrive in time."})
                    try:
                        body = parse_body(raw)
                    except ValueError as exc:
                        return self._json(400, {"error": str(exc)})
                result = route(app, body, query)
                if isinstance(result, tuple):                     # (content-type, bytes, extra headers)
                    ctype, data, extra = result
                    return self._send(200, data, ctype, extra)
                return self._json(200, result)
            except ApiError as exc:
                self._json(exc.status, {"error": str(exc)})
            except (ValueError, TypeError, OverflowError, RecursionError) as exc:
                self._json(400, {"error": f"Bad request: {exc}"})
            except Exception as exc:  # noqa: BLE001
                LOG.error("Setup page error: %s\n%s", exc, traceback.format_exc())
                self._json(500, {"error": f"Something went wrong ({type(exc).__name__}). Details are in the log file."})

        def _page(self, query: dict) -> None:
            given = (query.get("t") or [""])[0]
            signed_in = self._signed_in()
            if given and not signed_in:
                # (HEAD only asks whether the page is there: it must not use the link up.)
                session = app.sign_in(given) if self.command == "GET" else None
                if session is None:
                    return self._send(403, b"Forbidden: that link is not valid any more (a link works only once). "
                                           b"Run `connecto ui` again for a new one.")
                cookie = f"{app.cookie}={session}; Path=/; HttpOnly; SameSite=Strict"
                return self._send(302, b"", extra={"Location": "/", "Set-Cookie": cookie})
            if not signed_in:
                return self._send(403, b"Open the setup page with the link that `connecto ui` printed in your terminal.")
            if given:                                  # the used link opened again by someone who is signed in already
                return self._send(302, b"", extra={"Location": "/"})
            nonce = secrets.token_urlsafe(16)
            html = STATIC.read_text(encoding="utf-8").replace("__NONCE__", nonce).replace("__SECRET__", app.secret) \
                .replace("__VERSION__", __version__)
            csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; connect-src 'self'; "
                   "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8", {"Content-Security-Policy": csp})

        def do_GET(self):
            self._handle("GET")

        def do_HEAD(self):
            self._handle("HEAD")

        def do_POST(self):
            self._handle("POST")

        def do_PUT(self):
            self._send(405, b"Method not allowed")

        do_DELETE = do_PATCH = do_OPTIONS = do_PUT

    return Handler


# ============================================================================= routes
def _r_state(app, body, q): return app.state()
def _r_save_config(app, body, q): return app.save_config(body)
def _r_render(app, body, q): return app.render_templates(body)
def _r_ai_key(app, body, q): return app.ai_key(body)
def _r_ai_test(app, body, q): return app.ai_test(body)
def _r_ai_suggest(app, body, q): return app.ai_suggest(body)
def _r_preview(app, body, q): return app.preview(q)
def _r_people(app, body, q): return app.people(q)
def _r_add(app, body, q): return app.add_people(body)
def _r_skip(app, body, q): return app.skip_people(body)
def _r_pause(app, body, q): return app.pause(body)
def _r_resume(app, body, q): return app.resume(body)
def _r_schedule(app, body, q): return app.schedule(body)
def _r_doctor(app, body, q): return app.doctor(q)
def _r_history(app, body, q): return app.history()
def _r_job_start(app, body, q): return app.start_job(body)
def _r_job(app, body, q): return app.job(q)
def _r_job_stop(app, body, q): return {"stopping": app.jobs.stop()}


def _r_export(app, body, q):
    return ("text/csv; charset=utf-8", app.export_csv().encode("utf-8", errors="replace"),
            {"Content-Disposition": 'attachment; filename="connecto-invitations.csv"'})


def _r_shutdown(app, body, q):
    job = app.jobs.active()
    if job is not None:             # never cut a run off in the middle of a profile: ask it to stop, and stay open
        app.jobs.stop()
        raise ApiError(f"The {job.kind.replace('_', ' ')} is still running, so the setup page stays open. It was asked to stop "
                       "after the profile it is on - press Quit again in a moment.", 409)
    threading.Thread(target=app.shutdown, daemon=True).start()
    return {"bye": True}


ROUTES = {
    ("GET", "/api/state"): _r_state, ("POST", "/api/config"): _r_save_config,
    ("POST", "/api/render"): _r_render, ("POST", "/api/ai/key"): _r_ai_key, ("POST", "/api/ai/test"): _r_ai_test, ("POST", "/api/ai/suggest"): _r_ai_suggest,
    ("GET", "/api/preview"): _r_preview, ("GET", "/api/people"): _r_people,
    ("POST", "/api/people/add"): _r_add, ("POST", "/api/people/skip"): _r_skip,
    ("POST", "/api/pause"): _r_pause, ("POST", "/api/resume"): _r_resume, ("POST", "/api/schedule"): _r_schedule,
    ("GET", "/api/doctor"): _r_doctor, ("GET", "/api/history"): _r_history, ("GET", "/api/export.csv"): _r_export,
    ("POST", "/api/job/start"): _r_job_start, ("GET", "/api/job"): _r_job, ("POST", "/api/job/stop"): _r_job_stop,
    ("POST", "/api/shutdown"): _r_shutdown,
}


# ============================================================================= start-up
def make_server(home: Home, *, port: int = 0, token: str | None = None,
                secret: str | None = None) -> tuple[ThreadingHTTPServer, App]:
    """Bind 127.0.0.1 (never anything wider) and build the app. Port 0 = any free port."""
    token = token or secrets.token_urlsafe(32)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), BaseHTTPRequestHandler)
    app = App(home, token, httpd.server_address[1], shutdown=httpd.shutdown, secret=secret)
    httpd.RequestHandlerClass = make_handler(app)
    httpd.daemon_threads = True
    return httpd, app


def serve(home: Home, *, port: int = 0, open_browser: bool = True) -> int:
    home.ensure()
    setup_logging(home.log_path, console=False)
    load_env_into_process(home.env_path)
    try:
        httpd, app = make_server(home, port=port)
    except OSError as exc:
        print(f"Could not start the setup page on port {port}: {exc}")
        return 2
    url = f"http://127.0.0.1:{app.port}/?t={app.token}"
    print("Connecto setup page is running (only on this computer).\n"
          f"  {url}\n"
          "Open that link if your browser did not open by itself - it works once, so a copy of it is of no use to anyone "
          "else. Press Ctrl+C here, or Quit in the page, to stop.")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        httpd.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        wait_for_job(app)
        httpd.server_close()
    return 0


def wait_for_job(app: App, seconds: float | None = None) -> bool:
    """Leaving the setup page never cuts a job off in the middle of a profile: a running one is asked to stop and given time to
    say so. A second Ctrl+C leaves at once. True if nothing is running when this returns."""
    job = app.jobs.active()
    if job is None:
        return True
    seconds = WAIT_FOR_JOB_SECONDS if seconds is None else seconds
    app.jobs.stop()
    print(f"The {job.kind.replace('_', ' ')} is still running. Waiting for it to stop after the profile it is on "
          "(Ctrl+C again leaves at once and cuts that profile off).")
    try:
        done = app.jobs.wait(seconds)
    except KeyboardInterrupt:
        print("Leaving now.")
        return False
    print("Stopped." if done else f"It had not stopped after {int(seconds)} seconds - leaving anyway.")
    return done
