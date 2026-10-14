"""Small shared helpers: time, atomic file writes, the .env file, logging, text clean-up."""
from __future__ import annotations

import logging
import logging.handlers
import math
import os
import re
import stat
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path

LOG = logging.getLogger("connecto")


# ----------------------------------------------------------------------------- time
def now() -> datetime:
    return datetime.now()


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def parse_hhmm(value: str) -> tuple[int, int]:
    """'09:30' -> (9, 30). Raises ValueError on anything else."""
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(value))
    if not m:
        raise ValueError(f"bad time {value!r} (expected HH:MM)")
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        raise ValueError(f"bad time {value!r}")
    return h, mi


# ----------------------------------------------------------------------------- files
def atomic_write(path: str | os.PathLike, data: str, mode: int | None = None) -> None:
    """Write text to `path` via a temp file in the same directory, so readers never see half a file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(data)
        if mode is not None:
            try:
                os.chmod(tmp, mode)
            except OSError:
                pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _size_words(n: int) -> str:
    return f"{n // (1024 * 1024)} MB" if n >= 1024 * 1024 else f"{max(1, n // 1024)} KB"


def read_regular_file(path: str | os.PathLike, max_bytes: int, *, what: str = "file", hint: str = "") -> bytes:
    """The bytes of an ordinary file of at most ``max_bytes`` - nothing else is read.

    A folder, a pipe, a socket or a device (``/dev/zero`` never ends; a pipe with nobody writing waits forever) is refused
    with a plain ``ValueError``, and so is a file that is too large. The check is made on the file that was opened, not on
    its name, and never more than ``max_bytes + 1`` bytes are read, whatever the size said. ``OSError`` when it can't be
    opened."""
    p = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(p, flags)                                  # (O_NONBLOCK: opening a pipe must not wait for a writer)
    try:
        info = os.fstat(fd)
        if stat.S_ISDIR(info.st_mode):
            raise ValueError(f"{p.name} is a folder, not a {what}")
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"{p.name} is not an ordinary file (a pipe or a device?), so it can't be read as a {what}")
        too_large = f"{p.name} is larger than {_size_words(max_bytes)}{hint}"
        if info.st_size > max_bytes:
            raise ValueError(too_large)
        with os.fdopen(fd, "rb") as fh:
            fd = -1                                         # (closed by the with block)
            data = fh.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError(too_large)
        return data
    finally:
        if fd >= 0:
            os.close(fd)


def tail_lines(path: str | os.PathLike, n: int = 40) -> list[str]:
    path = Path(path)
    if not path.exists():
        return []
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 64 * 1024))
            chunk = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    return chunk.splitlines()[-n:]


# ----------------------------------------------------------------------------- YAML (settings files)
MAX_YAML_BYTES = 1_000_000          # a settings file is a few hundred lines; a megabyte is already a mistake

_PLAIN_INT = re.compile(r"^(?:[-+]?0b[0-1_]+|[-+]?0[0-7_]+|[-+]?(?:0|[1-9][0-9_]*)|[-+]?0x[0-9a-fA-F_]+)$")
_PLAIN_FLOAT = re.compile(r"^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?|\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?"
                          r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$")


def _yaml_loader():
    import yaml

    class SettingsLoader(yaml.SafeLoader):
        """PyYAML's safe loader with two differences. (1) No anchors/aliases: ten short lines of them expand into
        gigabytes ("billion laughs"), and a settings file has no use for them. (2) No YAML 1.1 sexagesimal numbers: the
        unquoted time 10:00 stays text instead of silently becoming the number 600."""

        def compose_node(self, parent, index):
            if self.check_event(yaml.events.AliasEvent):
                raise yaml.constructor.ConstructorError(
                    None, None, "aliases (*name) are not supported in settings files; write the value out", self.peek_event().start_mark)
            return super().compose_node(parent, index)

    SettingsLoader.yaml_implicit_resolvers = {
        first: [(tag, _PLAIN_INT if tag.endswith(":int") else _PLAIN_FLOAT if tag.endswith(":float") else rx)
                for tag, rx in rules]
        for first, rules in yaml.SafeLoader.yaml_implicit_resolvers.items()}
    return SettingsLoader


def load_yaml(text: str):
    """Parse a settings document: plain data only, no aliases, times of day stay text. ValueError when it is too large
    or nested too deeply; yaml.YAMLError when it is not valid YAML."""
    import yaml
    if len(text) > MAX_YAML_BYTES:
        raise ValueError(f"the file is larger than {MAX_YAML_BYTES // 1000} KB - that is not a settings file")
    try:
        return yaml.load(text, Loader=_yaml_loader())          # noqa: S506 - a SafeLoader subclass
    except RecursionError:
        raise ValueError("the file is nested too deeply") from None


def read_yaml_file(path: str | os.PathLike):
    """``load_yaml`` for a file (a leading byte-order mark is fine); looks at the size before reading it."""
    path = Path(path)
    data = read_regular_file(path, MAX_YAML_BYTES, what="settings file", hint=" - that is not a settings file")
    return load_yaml(data.decode("utf-8-sig"))


# ----------------------------------------------------------------------------- the .env file (API keys)
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")             # the conventional shape of an environment variable name
_ENV_VALUE = re.compile(r"^[\x21-\x7e]{1,512}$")  # printable ASCII, no spaces/quotes games
_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")
# A key saved under a name of your own is tied to the one server it was saved for. That is written as a comment line
# right above the key, so the two always move (and are deleted) together:   # connecto:origin NAME https://api.example.com
_ENV_ORIGIN_LINE = re.compile(r"^\s*#\s*connecto:origin\s+([A-Z][A-Z0-9_]*)\s+(\S+)\s*$")
_ORIGIN_VALUE = re.compile(r"^https?://[A-Za-z0-9._\-\[\]:]{1,253}$")
_ENV_LOCK = threading.Lock()                                    # two saves at once (two page requests) must not lose one
# Names that say "this is a secret": FOO_API_KEY, FOO_KEY, GITHUB_TOKEN, DB_PASSWORD ... (word-bounded, so MONKEY or
# KEYBOARD_LAYOUT do not count).
_SECRET_NAME = re.compile(r"(?:^|_)(?:API_?KEY|KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?)(?:$|_)", re.I)
NAME_RULE = "capital letters, digits and underscores, including KEY, TOKEN or SECRET (for example MY_SERVICE_API_KEY)"

# Names saved in Connecto's own .env file (by Connecto, or found there when it was loaded). The AI helper only reads a key
# from such a name - or from the one conventional variable of a built-in service - so a settings file written by somebody
# else cannot make Connecto send an unrelated secret from your environment (GITHUB_TOKEN, AWS_...) to some server.
_MANAGED_ENV: set[str] = set()
# The server each of those keys was saved for (see _ENV_ORIGIN_LINE); a key without an entry has no recorded server.
_ENV_ORIGINS: dict[str, str] = {}


def is_secret_name(name: str) -> bool:
    """A usable name for a key setting: capital letters, digits and underscores that say it holds a key, token or secret
    (``GEMINI_API_KEY``, ``MY_SERVICE_TOKEN``). The one rule behind the command line, the setup page, ``.env`` and the
    settings file; a bare ``KEY`` is too generic to be a name."""
    name = name or ""
    return bool(_ENV_NAME.fullmatch(name)) and "_" in name and bool(_SECRET_NAME.search(name))


def managed_env_names() -> frozenset:
    return frozenset(_MANAGED_ENV)


def mark_managed_env(name: str, origin: str | None = None) -> None:
    _MANAGED_ENV.add(name)
    if origin:
        _ENV_ORIGINS[name] = origin


def env_origin(name: str) -> str | None:
    """The server address a key saved under ``name`` is tied to, or None when none was recorded."""
    return _ENV_ORIGINS.get(name)


def normalize_origin(url: str) -> str:
    """``scheme://host[:port]`` of an address, lower-cased ('' when it is not an http(s) address)."""
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(str(url or "").strip())
        port = parts.port
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    if parts.scheme.lower() not in ("http", "https") or not host:
        return ""
    host = f"[{host}]" if ":" in host else host
    origin = f"{parts.scheme.lower()}://{host}" + (f":{port}" if port else "")
    return origin if _ORIGIN_VALUE.fullmatch(origin) else ""


def _env_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8-sig", errors="replace").splitlines()     # (utf-8-sig: a BOM must not hide line one)


def read_env_file(path: str | os.PathLike) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in _env_lines(Path(path)):
        if line.lstrip().startswith("#"):
            continue
        m = _ENV_LINE.match(line)
        if not m:
            continue
        value = m.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[m.group(1)] = value
    return out


def read_env_origins(path: str | os.PathLike) -> dict[str, str]:
    """NAME -> the server address its key was saved for (only entries written by Connecto)."""
    out: dict[str, str] = {}
    for line in _env_lines(Path(path)):
        m = _ENV_ORIGIN_LINE.match(line)
        if m and is_secret_name(m.group(1)) and normalize_origin(m.group(2)) == m.group(2):
            out[m.group(1)] = m.group(2)
    return out


def _is_line_of(line: str, name: str) -> bool:
    m = _ENV_LINE.match(line)
    return bool(m and m.group(1) == name and not line.lstrip().startswith("#"))


def set_env_var(path: str | os.PathLike, name: str, value: str, origin: str | None = None) -> None:
    """Insert or replace NAME=value in the .env file (created 0600). Refuses names/values that could break the file.

    ``origin`` is the server address the key is for; it is recorded next to the key (see ``_ENV_ORIGIN_LINE``) and the key
    is only ever sent there. Saving again without one keeps the address that was recorded."""
    if not is_secret_name(name):                       # (fullmatch inside: "$" would also accept a trailing newline)
        raise ValueError(f"invalid variable name: use {NAME_RULE}")
    if not isinstance(value, str) or not _ENV_VALUE.fullmatch(value):
        raise ValueError("invalid value (printable characters only, no spaces or newlines)")
    if origin is not None:
        origin = normalize_origin(origin)
        if not origin:
            raise ValueError("invalid server address for this key (expected something like https://api.example.com)")
    p = Path(path)
    with _ENV_LOCK:
        lines = _env_lines(p)
        previous = read_env_origins(p).get(name)
        origin = origin or previous
        kept: list[str] = []
        placed = False
        for line in lines:
            m = _ENV_ORIGIN_LINE.match(line)
            if m and m.group(1) == name:
                continue                                # the origin line is rewritten with the key
            if _is_line_of(line, name):
                if placed:
                    continue                            # a duplicate: the reader takes the last one, so none may stay behind
                placed = True
                if origin:
                    kept.append(f"# connecto:origin {name} {origin}")
                kept.append(f"{name}={value}")
                continue
            kept.append(line)
        if not placed:
            if origin:
                kept.append(f"# connecto:origin {name} {origin}")
            kept.append(f"{name}={value}")
        atomic_write(p, "\n".join(kept) + "\n", mode=0o600)
    _MANAGED_ENV.add(name)
    if origin:
        _ENV_ORIGINS[name] = origin
    else:
        _ENV_ORIGINS.pop(name, None)


def remove_env_var(path: str | os.PathLike, name: str) -> bool:
    p = Path(path)
    if not p.exists():
        return False
    with _ENV_LOCK:
        kept, removed = [], False
        for line in _env_lines(p):
            m = _ENV_ORIGIN_LINE.match(line)
            if m and m.group(1) == name:
                continue
            if _is_line_of(line, name):
                removed = True
                continue
            kept.append(line)
        atomic_write(p, ("\n".join(kept) + "\n") if kept else "", mode=0o600)
    if removed:
        _MANAGED_ENV.discard(name)
        _ENV_ORIGINS.pop(name, None)
    return removed


def load_env_into_process(path: str | os.PathLike) -> list[str]:
    """Make the keys saved in the .env file available to this process (a variable that is already set wins). Only names
    that look like secrets are used, so the file can never be a way to set PATH or LD_PRELOAD. Returns the names loaded."""
    loaded = []
    values = read_env_file(path)
    origins = read_env_origins(path)
    for k, v in values.items():
        if not is_secret_name(k):
            continue
        _MANAGED_ENV.add(k)
        if k in origins:
            _ENV_ORIGINS[k] = origins[k]
        else:
            _ENV_ORIGINS.pop(k, None)
        if k not in os.environ:
            os.environ[k] = v
            loaded.append(k)
    return loaded


def mask_secret(value: str) -> str:
    if not value:
        return ""
    return ("*" * max(4, len(value) - 4)) + value[-4:] if len(value) > 8 else "*" * len(value)


# ----------------------------------------------------------------------------- logging
# Environment variables whose name says "this is a secret" (see _SECRET_NAME) are masked in every log line, plus any name
# registered here (the AI key setting may be called anything).
_EXTRA_SECRET_NAMES: set[str] = set()


def register_secret_name(name: str) -> None:
    """Treat the environment variable ``name`` as secret in every log line, even if its name does not look like one
    (the AI key setting may be called anything)."""
    if name:
        _EXTRA_SECRET_NAMES.add(str(name))


class RedactFilter(logging.Filter):
    """Replace any secret value that is present in the environment (API keys) with *** in every log line."""

    def __init__(self, names: tuple[str, ...] = ()):
        super().__init__()
        self.names = names

    def _secrets(self) -> list[str]:
        vals = []
        for k, v in os.environ.items():
            if v and len(v) >= 8 and (k in self.names or k in _EXTRA_SECRET_NAMES or _SECRET_NAME.search(k)):
                vals.append(v)
        return vals

    def redact(self, text: str) -> str:
        """``text`` with every secret value found in the environment replaced by ***."""
        for secret in self._secrets():
            if secret in text:
                text = text.replace(secret, "***")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg, record.args = self.redact(record.getMessage()), ()
        except Exception:
            pass
        return True


def setup_logging(log_path: str | os.PathLike | None, console: bool = True, level: int = logging.INFO) -> logging.Logger:
    LOG.handlers.clear()
    LOG.setLevel(level)
    LOG.propagate = False
    redact = RedactFilter()
    if log_path:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
        fh.addFilter(redact)
        LOG.addHandler(fh)
    if console:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter("%(message)s"))
        sh.addFilter(redact)
        LOG.addHandler(sh)
    return LOG


# ----------------------------------------------------------------------------- text
# Characters that are never part of a visible name or title: control codes, zero-width and bidirectional marks, soft
# hyphens, the invisible "tag" block (a way to hide whole sentences in plain sight), variation selectors, and the
# interlinear-annotation marks. (NEL, U+0085, is left to the whitespace collapse below.)
_CONTROL = re.compile(
    r"[\x00-\x08\x0b-\x1f\x7f-\x84\x86-\x9f\xad\u061c\u180e\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufe00-\ufe0f"
    r"\ufeff\ufff9-\ufffb\U000e0000-\U000e007f\U000e0100-\U000e01ef]")


def clean_text(value, max_len: int = 0) -> str:
    """Collapse whitespace, drop control/bidi characters. Used on everything that comes from a web page or CSV."""
    if value is None:
        return ""
    text = _CONTROL.sub("", str(value))
    text = re.sub(r"\s+", " ", text).strip()
    if max_len and len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    return text


def csv_safe(value) -> str:
    """Text for a spreadsheet cell: a value that starts like a formula is prefixed with ' so it is shown, never run."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(("=", "+", "-", "@", "\t", "\r")) else text


_NAME_WORD = re.compile(r"[^\W\d_]+(?:['\u2019.\-][^\W\d_]+)*")


def name_case(name: str) -> str:
    """A name written in one case only, as it reads in a greeting: 'VISHNU' -> 'Vishnu', 'naman' -> 'Naman', 'JEAN-LUC' ->
    'Jean-Luc'. Mixed case ('DeShawn', 'McKay'), initials ('JJ'), scripts without capitals and anything that is not
    shaped like a name (digits, braces, underscores) stay exactly as written."""
    text = name or ""
    if not _NAME_WORD.fullmatch(text) or sum(c.isalpha() for c in text) <= 2 or not (text.isupper() or text.islower()):
        return text
    return re.sub(r"[^\W\d_]+", lambda m: m.group()[0].upper() + m.group()[1:].lower(), text)


def split_first_name(full_name: str) -> str:
    name = clean_text(full_name)
    if not name:
        return ""
    name = re.sub(r"^(dr|mr|mrs|ms|prof)\.?\s+", "", name, flags=re.I)
    return name_case(name.split(" ")[0].strip(",.()"))


def norm_key(name: str) -> str:
    """Column/field name -> snake_case identifier ('Job Title' -> 'job_title')."""
    return re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_")


def as_list(value) -> list[str]:
    """Accept None, 'a, b; c', or a list; return a clean list of non-empty strings."""
    if not value:
        return []
    if isinstance(value, str):
        value = re.split(r"[,;\n|]", value)
    elif isinstance(value, dict):
        return []                                      # a mapping where a list belongs: nothing usable
    elif not isinstance(value, (list, tuple, set, frozenset)):
        value = [value]                                # a lone number: a list of one
    return [str(v).strip() for v in value if str(v).strip()]


def whole_number(value) -> int:
    """A whole number from a number or a numeric string. ValueError for anything else (text, True, NaN, infinity)."""
    if isinstance(value, bool):
        raise ValueError("not a number")
    if isinstance(value, int):
        return value
    number = float(value)                              # TypeError / ValueError for rubbish
    if not math.isfinite(number):
        raise ValueError("not a finite number")
    return int(number)


def real_number(value) -> float:
    """A finite float from a number or a numeric string; ValueError otherwise."""
    if isinstance(value, bool):
        raise ValueError("not a number")
    try:
        number = float(value)
    except OverflowError:
        raise ValueError("too large") from None
    if not math.isfinite(number):
        raise ValueError("not a finite number")
    return number
