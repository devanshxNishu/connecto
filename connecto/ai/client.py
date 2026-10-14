"""A small, careful client for any OpenAI-compatible ``/chat/completions`` endpoint.

Design rules: the API key travels only in the ``Authorization`` header, only to the server it belongs to (see
``AIClient.from_config``), and is scrubbed from every message, log line and repr; every failure is an ``AIError`` with a
machine-readable ``kind``; the client paces itself below the provider's rate limit and retries only what is worth
retrying (429, 5xx, timeouts, dropped connections). The server on the other end is not trusted: it is never followed to
another address, a reply is read in small pieces under a size and time limit, and whatever it says is cleaned before it
is shown.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import math
import os
import random
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import requests
import requests.auth
import urllib3.exceptions
import urllib3.response

from .. import __version__
from ..util import NAME_RULE, clean_text, env_origin, managed_env_names, normalize_origin, register_secret_name
from .presets import KEY_HOMES, PRESETS, Preset, get_preset, rank_models

LOG = logging.getLogger("connecto")

KINDS = ("config", "auth", "rate_limit", "network", "server", "bad_response", "budget")
RETRYABLE = frozenset({"rate_limit", "server", "network"})
RETRY_AFTER_CAP = 20.0        # never sleep longer than this between attempts; a longer Retry-After fails fast
ERROR_BODY_MAX = 200          # characters of a server error body shown in messages
REASONING_HEADROOM = 700      # extra completion tokens for models that "think" before answering
SWITCH_LIMIT = 3             # other ranked models tried when an auto-chosen one turns out to be gone
MAX_CHAT_BYTES = 256 * 1024          # a chat answer is a few hundred bytes; anything this big is hostile or broken
MAX_LIST_BYTES = 5 * 1024 * 1024     # a model list can be long (OpenRouter's runs to about a megabyte)
MAX_ANSWER_CHARS = 32_768            # the part of a model's text that is looked at at all
DEADLINE_FACTOR = 2                  # one exchange may take at most this many times the per-read timeout in total
READ_PIECE = 8192                    # bytes taken from the connection at a time
_MODEL_ID = re.compile(r"[A-Za-z0-9@][A-Za-z0-9._:/@+~=\-]{0,127}")

_REASONING = re.compile(
    r"gpt-oss|qwen3|qwq|deepseek-r1|(?<![a-z0-9])r1(?![a-z0-9])|reasoner|thinking|magistral|gemini-(?:2\.5|3)"
    r"|gemini-flash-(?:lite-)?latest|glm-4\.[5-9]|(?<![a-z0-9])o[134](?:-|$)", re.I)
_THINK_TAG = re.compile(r"<(/?)think(?:ing)?>", re.I)
_AUTH_400 = re.compile(r"api[ _]?key (?:not valid|expired|invalid)|api_key_invalid|invalid[ _]api[ _]?key"
                       r"|incorrect api key|unauthorized|authentication", re.I)
_MODEL_GONE = re.compile(r"model.{0,120}(?:not found|does not exist|not exist|decommission|not supported|unknown"
                         r"|invalid|deprecated|no longer|not available)|(?:unknown|invalid|unsupported) model", re.I | re.S)
_RESPONSE_FORMAT = re.compile(r"response_format|json_object|json_schema|json mode", re.I)
_RETRY_DELAY = re.compile(r'"retryDelay"\s*:\s*"([0-9.]{1,12})s"')
_DEFAULT_PORT = {"http": 80, "https": 443}


class AIError(Exception):
    """Any failure of the AI helper. ``kind`` is one of KINDS; ``retry_after`` is the server's wait hint in seconds."""

    def __init__(self, kind: str, message: str = "", retry_after: float | None = None, *,
                 status: int | None = None, hint: str = ""):
        super().__init__(message or kind)
        self.kind = kind
        self.retry_after = retry_after
        self.status = status
        self.hint = hint          # internal signal, e.g. "response_format"


@dataclass
class TestResult:
    __test__ = False              # not a pytest test class

    ok: bool
    message: str
    model: str = ""
    latency_ms: int = 0
    models_seen: int = 0


@dataclass
class _Reply:
    """What came back from one exchange: the status, the headers and the (size-limited) body."""
    status: int
    headers: object
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8-sig", errors="replace")


class _NoAuth(requests.auth.AuthBase):
    """Adds nothing. Giving a session an ``auth`` stops requests from looking in ``~/.netrc`` and attaching those
    credentials: the key travels in our own header and nowhere else."""

    def __call__(self, r):
        return r


# ----------------------------------------------------------------------------- helpers
def looks_like_reasoning(model_id: str) -> bool:
    """True for models that spend completion tokens thinking before they answer (they need token headroom)."""
    return bool(_REASONING.search(model_id or ""))


def _finite(value, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return default if not math.isfinite(number) else min(high, max(low, number))


def _is_loopback(host: str) -> bool:
    """This computer: ``localhost`` or a loopback address. (Not any other name that claims to be local: a name is only
    as good as the lookup behind it.)"""
    host = (host or "").strip("[]").lower().rstrip(".")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _check_base_url(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        raise AIError("config", "no AI server address (base_url) is set")
    try:
        parts = urlsplit(raw)
        host = parts.hostname or ""
        parts.port  # noqa: B018 - raises ValueError on a bad port
    except ValueError:
        raise AIError("config", "the AI server address (base_url) is not a valid URL") from None
    if parts.scheme not in ("http", "https") or not host:
        raise AIError("config", "the AI server address must look like https://host/v1 (http is only allowed for "
                                "localhost)")
    if parts.scheme == "http" and not _is_loopback(host):
        raise AIError("config", "the AI server address must use https:// (plain http is only allowed for localhost, "
                                "127.0.0.1 or ::1) so your key and prompts are not sent in the clear")
    if parts.username or parts.password:
        raise AIError("config", "the AI server address must not contain a user name or password; keep keys in .env")
    if parts.query or parts.fragment:
        raise AIError("config", "the AI server address must not contain ? or # parts; use the plain .../v1 address")
    path = re.sub(r"/chat/completions/?$", "", parts.path.rstrip("/"), flags=re.I).rstrip("/")
    return f"{parts.scheme}://{parts.netloc}{path}"


def _server_id(url: str):
    """(scheme, host, port) of an address - what decides which server a request reaches - or None for a bad address.
    The default port is filled in, and ``localhost``, ``127.0.0.1`` and ``::1`` count as one machine."""
    try:
        parts = urlsplit(str(url or "").strip())
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower().rstrip(".")
    if scheme not in _DEFAULT_PORT or not host:
        return None
    return scheme, ("<this computer>" if _is_loopback(host) else host), port or _DEFAULT_PORT[scheme]


def _same_server(a: str, b: str) -> bool:
    """Do two addresses point at the same scheme, host and port? (A bad address is nobody's server.)"""
    ida, idb = _server_id(a), _server_id(b)
    return ida is not None and ida == idb


def origin_for_new_key(key_env: str, base_url: str) -> str | None:
    """What to record next to a key that is being saved under ``key_env`` for the server at ``base_url``.

    A built-in service's own variable (GEMINI_API_KEY ...) needs nothing: its name already says where it goes. Any other
    name is tied to the server address it is saved for (``scheme://host[:port]``), and only goes there. Raises
    ``AIError('config')`` when that address is missing or not usable."""
    if key_env in KEY_HOMES:
        return None
    origin = normalize_origin(_check_base_url(base_url))
    if not origin:
        raise AIError("config", "the AI server address is not usable for a key (expected something like "
                                "https://api.example.com/v1)")
    return origin


def _check_key_destination(key_env: str, base_url: str, *, env_is_real: bool, key_present: bool) -> None:
    """The one rule about where a saved key may go. Raises ``AIError('config')`` when ``base_url`` is not it.

    * A built-in service's variable (GEMINI_API_KEY, GROQ_API_KEY, OPENROUTER_API_KEY) goes to that service's own server,
      whatever the settings say - so a settings file written by somebody else cannot send it anywhere else.
    * A key saved under a name of your own goes to the server it was saved for (recorded next to it in ``.env``), or,
      when none was recorded, only to a server on this computer."""
    owner = KEY_HOMES.get(key_env)
    if owner is not None:
        if not _same_server(base_url, owner.base_url):
            raise AIError("config", f"ai.base_url is not {owner.label}'s own server, so your {owner.label} key ({key_env}) will "
                                    "not be sent there. Use the 'custom' service for another server and give it its own key "
                                    f"setting ({NAME_RULE}).")
        return
    if not key_present or not env_is_real:        # (a caller that passes its own environment decided what the client may see)
        return
    recorded = env_origin(key_env)
    where = normalize_origin(base_url) or str(base_url)
    if recorded:
        if not _same_server(base_url, recorded):
            raise AIError("config", f"The key {key_env} was saved for {recorded}, not for {where}, so it will not be sent there. "
                                    f"Save it again for this server (`connecto ai key --env {key_env}`, or the Message tab of "
                                    "`connecto ui`), or put ai.base_url back.")
        return
    if not _is_loopback(urlsplit(base_url).hostname or ""):
        raise AIError("config", f"The key {key_env} is not tied to a server yet, so it is only used with a server on this "
                                f"computer. Save it again for {where} (`connecto ai key --env {key_env}`, or the Message tab "
                                "of `connecto ui`).")


def _check_key(key: str, env_name: str = "") -> str:
    key = (key or "").strip()
    if key and (not key.isascii() or re.search(r"[\s\x00-\x1f\x7f]", key)):
        where = f"{env_name} " if env_name else "the API key "
        raise AIError("config", f"{where}contains spaces, line breaks or unusual characters - copy the key again "
                                "without anything around it")
    return key


def _flatten_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                out.append(part["text"])
        return "".join(out)
    return ""


def strip_think(text: str) -> str:
    """Remove ``<think>...</think>`` reasoning (closed, unclosed or half-open) and return the visible answer.

    One pass over the tags, so a reply made of nothing but ``<think>`` cannot make it slow."""
    text = text or ""
    kept: list[str] = []
    cursor, opened_at = 0, None
    for tag in _THINK_TAG.finditer(text):
        closing = bool(tag.group(1))
        if opened_at is None and not closing:
            kept.append(text[cursor:tag.start()])
            opened_at = tag.start()
        elif opened_at is not None and closing:
            cursor, opened_at = tag.end(), None
    if opened_at is None:
        kept.append(text[cursor:])                      # (an unclosed block ends the answer: everything after it is thought)
    text = "".join(kept)
    last_close = None
    for tag in _THINK_TAG.finditer(text):               # a closing tag with nothing open: the thoughts came first
        if tag.group(1):
            last_close = tag
    if last_close is not None:
        text = text[last_close.end():]
    return text.strip()


def _parse_retry_after(value) -> float | None:
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    try:
        seconds = float(value)
        return max(0.0, seconds) if not math.isnan(seconds) else None
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def _status_in_body(code) -> int:
    """The HTTP-like status a provider puts in an error object inside a 200 answer (anything odd counts as 502)."""
    if isinstance(code, int) and not isinstance(code, bool):
        number = code
    elif isinstance(code, str) and code.isascii() and code.isdigit() and len(code) <= 3:
        number = int(code)
    else:
        return 502
    return number if 400 <= number < 600 else 502


class AIClient:
    """Talk to one OpenAI-compatible server. Safe to share between threads (pacing state is locked)."""

    def __init__(self, base_url: str, api_key: str, model: str = "", *, timeout: float = 30, rpm: int = 10,
                 session=None, max_retries: int = 2, sleep=time.sleep, clock=time.monotonic,
                 preset: Preset | None = None):
        self.base_url = _check_base_url(base_url)
        self._api_key = _check_key(api_key)
        model = (model or "").strip()
        self.model = model[len("models/"):] if model.lower().startswith("models/") else model
        self._auto = False                  # True when the model was discovered, not configured
        self._candidates: list[str] = []    # next-best models to fall back on if an auto-chosen one is gone
        self.timeout = _finite(timeout, 30.0, 0.05, 600.0)
        self.rpm = int(_finite(rpm, 0.0, 0.0, 100_000.0))
        self.max_retries = max(0, min(10, int(max_retries)))
        self.sleep = sleep
        self.clock = clock
        self.preset = preset
        self.loopback = _is_loopback(urlsplit(self.base_url).hostname or "")
        if session is None:
            session = requests.Session()
            session.auth = _NoAuth()           # (no ~/.netrc credentials in place of ours)
            if self.loopback:
                session.trust_env = False      # a local server must never be sent through an HTTP proxy
        self.session = session
        self._min_interval = 60.0 / self.rpm if self.rpm > 0 else 0.0
        self._next_slot = float("-inf")
        self._lock = threading.Lock()
        self._rng = random.Random()

    def __repr__(self) -> str:
        return f"AIClient(base_url={self.base_url!r}, model={self.model!r})"

    # ------------------------------------------------------------------ construction from the config
    @classmethod
    def from_config(cls, ai_cfg: dict, env=None, **kw) -> "AIClient":
        """Build a client from the ``ai:`` block of a normalised config. Raises AIError('config') when it cannot work
        (for example a key is required but its environment variable is empty). Makes no network call.

        Where a key may go is decided here, and never by the settings file alone: see ``_check_key_destination``."""
        cfg = ai_cfg or {}
        real_environment = env is None            # (a caller that passes its own mapping has decided what the client may see)
        env = os.environ if env is None else env
        try:
            preset = get_preset(str(cfg.get("provider") or "gemini"))
        except ValueError as exc:
            raise AIError("config", str(exc)) from None
        base_url = str(cfg.get("base_url") or "").strip() or preset.base_url
        if not base_url:
            raise AIError("config", "ai.base_url is required when ai.provider is 'custom' "
                                    "(for example http://localhost:1234/v1 for LM Studio)")
        base_url = _check_base_url(base_url)
        explicit_env = str(cfg.get("api_key_env") or "").strip()
        key_env = explicit_env or preset.key_env
        # A key only goes where it belongs, whatever the settings file says (it may have been written by somebody else):
        if explicit_env and explicit_env != preset.key_env and real_environment and explicit_env not in managed_env_names():
            raise AIError("config", f"The key setting {explicit_env} is not saved in Connecto's own .env file. Save it there with "
                                    f"`connecto ai key --env {explicit_env}` (or on the Message tab of `connecto ui`). Connecto "
                                    "does not read other environment variables, so a settings file written by someone else "
                                    "cannot make it send an unrelated secret to a server.")
        raw_key = str(env.get(key_env) or "") if key_env else ""
        if key_env:
            _check_key_destination(key_env, base_url, env_is_real=real_environment, key_present=bool(raw_key.strip()))
        key = _check_key(raw_key, key_env) if key_env else ""
        if key_env:
            register_secret_name(key_env)                    # whatever it is called, it never appears in a log line
        if not key and key_env and (preset.needs_key or (preset.id == "custom" and explicit_env)):
            hint = f" You can get a free key at {preset.signup_url}." if preset.signup_url else ""
            raise AIError("config", f"No API key found: the environment variable {key_env} is empty or not set."
                                    f"{hint} Save it as {key_env} in the .env file of your Connecto folder.")
        kw.setdefault("rpm", preset.rpm)
        kw.setdefault("preset", preset)
        kw.setdefault("timeout", 120 if preset.local else 30)
        return cls(base_url, key, str(cfg.get("model") or ""), **kw)

    # ------------------------------------------------------------------ safety helpers
    def _redact(self, text: str) -> str:
        text = str(text)
        if len(self._api_key) >= 4:
            text = text.replace(self._api_key, "***")
        return re.sub(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}", "Bearer ***", text)

    def _snippet(self, text: str, limit: int = ERROR_BODY_MAX) -> str:
        """Text that came from the server (or from an exception), made safe to show: control characters and escape
        sequences removed first - so they cannot be used to split the key and slip it past the next step - then the key
        and bearer tokens replaced, then shortened."""
        text = self._redact(clean_text(str(text or "")[:20_000]))
        return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"

    def _headers(self) -> dict:
        headers = {"User-Agent": f"connecto/{__version__}", "Accept": "application/json",
                   "Accept-Encoding": "identity"}          # (no compression: nothing to inflate into a bomb)
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    # ------------------------------------------------------------------ pacing and retries
    def _pace(self) -> None:
        """Keep at least 60/rpm seconds between generation requests (slots are reserved under a lock)."""
        if self._min_interval <= 0:
            return
        with self._lock:
            now = self.clock()
            start = max(now, self._next_slot)
            self._next_slot = start + self._min_interval
        if start > now:
            self.sleep(start - now)

    def _backoff(self, attempt: int, retry_after: float | None) -> float | None:
        """Seconds to wait before retry number ``attempt + 1``; None = the server asked for longer than we will wait."""
        base = min(RETRY_AFTER_CAP, 0.5 * (2 ** attempt))
        if retry_after is not None:
            if retry_after > RETRY_AFTER_CAP:
                return None
            base = max(base, retry_after)
        return min(RETRY_AFTER_CAP, base + self._rng.uniform(0, 0.25 * base))

    def _unreachable(self) -> str:
        where = urlsplit(self.base_url).netloc
        if self.preset is not None and self.preset.local:
            return f"could not reach {self.preset.label.split(' (')[0]} at {where} - is it running? Start it and try again"
        return f"could not connect to the AI server at {where}; check the address and your internet connection"

    def _classify(self, status: int, body: str, headers=None) -> AIError:
        """Turn an HTTP error answer into an AIError (message safe to show: key redacted, body truncated)."""
        body = (body or "")[:5000]
        low = body.lower()
        detail = self._snippet(_error_message(body))
        hdrs = headers or {}
        retry_after = _parse_retry_after(hdrs.get("Retry-After") or hdrs.get("retry-after"))
        if retry_after is None:
            m = _RETRY_DELAY.search(body)
            retry_after = _parse_retry_after(m.group(1)) if m else None
        if status == 429:
            return AIError("rate_limit", f"the provider's rate limit was reached (HTTP 429): {detail}",
                           retry_after, status=status)
        if status in (401, 403) or (status == 400 and _AUTH_400.search(body)):
            return AIError("auth", f"the provider rejected the API key (HTTP {status}); check the key and that it is "
                                   f"still active: {detail}", status=status)
        if status == 402:
            return AIError("auth", f"the provider says payment or credits are needed (HTTP 402): {detail}", status=status)
        if status in (400, 422) and _RESPONSE_FORMAT.search(body):
            return AIError("bad_response", f"the provider refused the request (HTTP {status}): {detail}", status=status,
                           hint="response_format")
        if (status == 404 and ("model" in low or "no endpoints found" in low)) \
                or (status in (400, 422) and _MODEL_GONE.search(body)):
            return AIError("config", f"model not found: {detail}", status=status)
        if status == 404:
            return AIError("config", f"the AI server has no such address (HTTP 404); ai.base_url usually ends in /v1: "
                                     f"{detail}", status=status)
        if status == 408:
            return AIError("network", f"the AI server timed out (HTTP 408): {detail}", retry_after, status=status)
        if status >= 500:
            return AIError("server", f"the provider had a problem (HTTP {status}): {detail}", retry_after, status=status)
        return AIError("bad_response", f"the provider refused the request (HTTP {status}): {detail}", status=status)

    def _decode(self, reply: _Reply):
        """Parse a 2xx answer. Returns (data, error): providers sometimes put an error inside a 200 answer."""
        text = reply.text
        try:
            data = json.loads(text)
        except (ValueError, RecursionError):
            raise AIError("bad_response", "the provider's answer was not valid JSON: " + self._snippet(text)) from None
        if isinstance(data, dict) and data.get("error") and not data.get("choices") and not data.get("data"):
            err = data["error"]
            code = err.get("code") if isinstance(err, dict) else None
            return None, self._classify(_status_in_body(code), text, reply.headers)
        return data, None

    # ------------------------------------------------------------------ one exchange
    def _read_body(self, resp, deadline: float, cancelled: threading.Event, max_bytes: int) -> bytes:
        """The answer's body, read in small pieces: never more than ``max_bytes``, never after ``deadline``."""
        declared = None
        try:
            declared = int(str(resp.headers.get("Content-Length") or "").strip() or "-1")
        except (AttributeError, ValueError):
            pass
        if declared is not None and declared > max_bytes:
            raise AIError("bad_response", "the provider's answer was far larger than a chat answer can be", hint="no-retry")
        raw = getattr(resp, "raw", None)
        read1 = getattr(raw, "read1", None) if isinstance(raw, urllib3.response.HTTPResponse) else None
        if read1 is not None:                              # (urllib3 2: whatever has arrived, at most READ_PIECE bytes)
            def pieces():
                while True:
                    piece = read1(READ_PIECE, decode_content=True)
                    if not piece:
                        return
                    yield piece
        elif hasattr(resp, "iter_content"):
            def pieces():                                  # (older urllib3: one byte at a time, so a trickle cannot hide)
                return resp.iter_content(chunk_size=1)
        else:
            def pieces():
                return [getattr(resp, "content", b"") or b""]
        chunks: list[bytes] = []
        total = 0
        try:
            for piece in pieces():
                total += len(piece)
                if total > max_bytes:
                    raise AIError("bad_response", "the provider's answer was far larger than a chat answer can be",
                                  hint="no-retry")
                if cancelled.is_set() or time.monotonic() > deadline:
                    raise AIError("network", "the AI server answered too slowly", hint="no-retry")
                chunks.append(piece)
        except urllib3.exceptions.ReadTimeoutError:
            raise AIError("network", f"the AI server did not answer within {self.timeout:g} seconds") from None
        except urllib3.exceptions.DecodeError:
            raise AIError("bad_response", "the provider's answer could not be decoded", hint="no-retry") from None
        except urllib3.exceptions.HTTPError:
            raise AIError("network", "the connection to the AI server broke while it was answering") from None
        return b"".join(chunks)

    def _round_trip(self, method: str, url: str, payload, deadline: float, cancelled: threading.Event,
                    max_bytes: int) -> _Reply:
        kwargs = {"headers": self._headers(), "timeout": self.timeout, "stream": True, "allow_redirects": False}
        if method == "POST":
            resp = self.session.post(url, json=payload, **kwargs)
        else:
            resp = self.session.get(url, **kwargs)
        try:
            return _Reply(int(resp.status_code), resp.headers, self._read_body(resp, deadline, cancelled, max_bytes))
        finally:
            close = getattr(resp, "close", None)
            if callable(close):
                close()

    def _exchange(self, method: str, url: str, payload, max_bytes: int) -> _Reply:
        """One HTTP exchange that cannot outlast ``DEADLINE_FACTOR`` x the timeout, whatever the server does: the
        per-read timeout alone would let a server that trickles one byte every few seconds hold a run for hours. The
        exchange runs in a helper thread; if it is still going when the time is up, it is told to stop and abandoned."""
        limit = DEADLINE_FACTOR * self.timeout
        deadline = time.monotonic() + limit
        cancelled = threading.Event()
        box: dict = {}

        def work() -> None:
            try:
                box["reply"] = self._round_trip(method, url, payload, deadline, cancelled, max_bytes)
            except BaseException as exc:  # noqa: BLE001 - handed to the caller below
                box["error"] = exc

        worker = threading.Thread(target=work, name="connecto-ai-request", daemon=True)
        worker.start()
        worker.join(limit + 1.0)
        if worker.is_alive():
            cancelled.set()
            raise AIError("network", f"the AI server did not finish answering within {limit:g} seconds", hint="no-retry")
        if "error" in box:
            raise box["error"]
        return box["reply"]

    def _request(self, method: str, path: str, payload: dict | None = None, *, max_bytes: int = MAX_CHAT_BYTES):
        """One HTTP exchange with pacing and retries. Returns the decoded JSON; raises AIError."""
        url = self.base_url + path
        attempt = 0
        while True:
            if method == "POST":
                self._pace()
            err: AIError | None = None
            data = None
            try:
                reply = self._exchange(method, url, payload, max_bytes)
            except AIError as exc:
                err = exc
            except requests.exceptions.Timeout:
                err = AIError("network", f"the AI server did not answer within {self.timeout:g} seconds")
            except requests.exceptions.SSLError:
                err = AIError("network", "could not make a secure (TLS) connection to the AI server", hint="no-retry")
            except requests.exceptions.ConnectionError:
                err = AIError("network", self._unreachable())
            except (requests.exceptions.InvalidURL, requests.exceptions.MissingSchema,
                    requests.exceptions.InvalidSchema):
                raise AIError("config", "the AI server address is not valid") from None
            except requests.exceptions.RequestException as exc:
                err = AIError("network", f"the request to the AI server failed ({type(exc).__name__})")
            else:
                if reply.status >= 400:
                    err = self._classify(reply.status, reply.text, reply.headers)
                elif reply.status >= 300:
                    err = AIError("config", f"the AI server answered with a redirect (HTTP {reply.status}). Connecto does not "
                                            "follow redirects - your key and prompts only go where you set. Use the server's "
                                            "final address as ai.base_url.", status=reply.status, hint="no-retry")
                else:
                    data, err = self._decode(reply)
            if err is None:
                return data
            if err.kind in RETRYABLE and err.hint != "no-retry" and attempt < self.max_retries:
                delay = self._backoff(attempt, err.retry_after)
                if delay is not None:
                    LOG.debug("AI request failed (%s); retrying in %.1f s", err.kind, delay)
                    self.sleep(delay)
                    attempt += 1
                    continue
            raise err

    # ------------------------------------------------------------------ models
    def list_models(self) -> list[dict]:
        """``GET /models`` as a list of ``{"id": ..., ...}`` dicts (tolerates list / ``data`` / ``models`` shapes). Ids
        that are not plain model names (or that contain the key) are left out."""
        data = self._request("GET", "/models", max_bytes=MAX_LIST_BYTES)
        items = data.get("data") if isinstance(data, dict) else data
        if items is None and isinstance(data, dict):
            items = data.get("models")
        if not isinstance(items, list):
            raise AIError("bad_response", "the provider's model list was not in the expected format")
        out: list[dict] = []
        for item in items:
            if isinstance(item, str):
                item = {"id": item}
            if not isinstance(item, dict):
                continue
            mid = item.get("id") or item.get("name") or item.get("model")
            if not isinstance(mid, str):
                continue
            mid = mid.strip()
            if _MODEL_ID.fullmatch(mid) and self._redact(mid) == mid:
                out.append({**item, "id": mid})
        return out

    def _pick_model(self, models: list[dict] | None, listing_error: AIError | None) -> str:
        preset = self.preset or PRESETS["custom"]
        ranked = rank_models(preset, models) if models else []
        if not ranked:
            ranked = list(preset.fallback_models)       # only when the listing failed or had nothing usable
        if ranked:
            self._auto, self._candidates = True, ranked[1:1 + SWITCH_LIMIT]
            return ranked[0]
        if listing_error is not None:
            if listing_error.kind in ("config", "bad_response"):
                raise AIError("config", "no model is set and the provider's model list could not be read "
                                        f"({listing_error}); set ai.model in your settings") from None
            raise listing_error
        if preset.id == "ollama":
            raise AIError("config", "Ollama has no chat model installed yet. Install one, for example: "
                                    "ollama pull qwen2.5:3b")
        raise AIError("config", "the provider lists no usable chat model; set ai.model in your settings")

    def ensure_model(self) -> str:
        """The model to use: the configured one, else the best-ranked from ``/models``, else the preset's fallback."""
        if self.model:
            return self.model
        models, listing_error = None, None
        try:
            models = self.list_models()
        except AIError as exc:
            if exc.kind == "auth":
                raise
            listing_error = exc
        self.model = self._pick_model(models, listing_error)
        return self.model

    # ------------------------------------------------------------------ chat
    @staticmethod
    def _extract(data) -> tuple[str, str]:
        """(visible text, finish_reason) from a chat-completions answer."""
        choices = data.get("choices") if isinstance(data, dict) else None
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise AIError("bad_response", "the provider's answer had no choices")
        choice = choices[0]
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        content = message.get("content")
        if content is None:
            content = choice.get("text")
        return strip_think(_flatten_content(content)[:MAX_ANSWER_CHARS]), str(choice.get("finish_reason") or "")[:40]

    def chat(self, messages: list[dict], *, max_tokens: int = 200, temperature: float = 0.6,
             json_mode: bool = False) -> str:
        """Send one chat request and return the visible answer text. Raises AIError."""
        model = self.model or self.ensure_model()
        wanted = int(max_tokens)
        temperature = _finite(temperature, 0.6, 0.0, 2.0)

        def room(m: str) -> int:
            return wanted + (REASONING_HEADROOM if looks_like_reasoning(m) else 0)

        budget = room(model)
        use_json, widened = bool(json_mode), False
        for _ in range(4 + SWITCH_LIMIT):
            payload = {"model": model, "messages": messages, "max_tokens": budget, "temperature": temperature}
            if use_json:
                payload["response_format"] = {"type": "json_object"}
            try:
                data = self._request("POST", "/chat/completions", payload)
            except AIError as exc:
                if use_json and exc.hint == "response_format":
                    use_json = False            # this server has no JSON mode: ask in plain text instead
                    continue
                if self._auto and self._candidates and exc.kind == "config" and str(exc).startswith("model not found"):
                    model = self.model = self._candidates.pop(0)     # a model we chose ourselves vanished: next best
                    budget = room(model)
                    continue
                raise
            text, finish = self._extract(data)
            if text:
                return text
            if finish == "length" and not widened:
                widened, budget = True, min(max(budget * 4, 512), 2048)   # it ran out of tokens while "thinking"
                continue
            raise AIError("bad_response", "the model sent an empty answer" + (" (it ran out of tokens)"
                                                                           if finish == "length" else ""))
        raise AIError("bad_response", "the model sent no usable answer")

    # ------------------------------------------------------------------ connection test (for the setup page)
    def test(self) -> TestResult:
        """Check the address, the key and the model with one tiny request. Never raises."""
        started = self.clock()
        models: list[dict] | None = None
        listing_error: AIError | None = None
        try:
            try:
                models = self.list_models()
            except AIError as exc:
                if exc.kind == "auth":
                    raise
                listing_error = exc
            if not self.model:
                self.model = self._pick_model(models, listing_error)
            self.chat([{"role": "user", "content": "Reply with the single word: ready"}],
                      max_tokens=16, temperature=0.0)
            ms = int(max(0.0, self.clock() - started) * 1000)
            seen = f", {len(models)} models available" if models else ""
            return TestResult(True, f"Connected: {self._snippet(self.model, 120)} answered in {ms} ms{seen}.",
                              self._snippet(self.model, 120), ms, len(models or []))
        except AIError as exc:
            return TestResult(False, self._snippet(str(exc), 600), self._snippet(self.model, 120),
                              int(max(0.0, self.clock() - started) * 1000), len(models or []))
        except Exception as exc:                      # a test button must never crash the page
            return TestResult(False, f"Unexpected problem: {self._snippet(repr(exc))}", self._snippet(self.model, 120), 0,
                              len(models or []))


def _error_message(body: str) -> str:
    """The human part of a provider's JSON error body (falls back to the raw text)."""
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        return body
    if isinstance(data, list) and data:
        data = data[0]
    if isinstance(data, dict):
        err = data.get("error", data)
        if isinstance(err, dict):
            return str(err.get("message") or err.get("detail") or err.get("error") or body)
        if isinstance(err, str):
            return err
        return str(data.get("message") or data.get("detail") or body)
    return body
