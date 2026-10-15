"""Fixtures shared by several test modules."""
import ipaddress
import socket

import pytest

from connecto import util
from connecto.ai.presets import PRESETS

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", ""}
PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
# Chromium resolves names itself, so Python's socket guard cannot see it: tell it that nothing but this machine exists
# (and not to hand a request to a proxy that would resolve the name for it).
CHROMIUM_OFFLINE = ["--no-proxy-server", "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1 , EXCLUDE localhost"]


def pytest_make_parametrize_id(config, val, argname):
    """A long input gets a short name in the test's ID: its start and its length. pytest puts the ID in an environment
    variable, which Windows refuses beyond 32,767 characters (a test fed 200,000 brackets could not even start there), and a
    report that repeats such an input is unreadable anyway."""
    if isinstance(val, (str, bytes)) and len(val) > 60:
        start = val[:12].decode("latin-1") if isinstance(val, bytes) else val[:12]
        return f"{start.encode('unicode_escape').decode('ascii')}...{len(val)}"
    return None


def _is_local(host) -> bool:
    host = str(host or "").strip("[]").lower()
    return host in LOCAL_HOSTS or host.endswith(".localhost")


def _is_ip_literal(host) -> bool:
    """An address (including the old shorthand forms such as ``2130706433`` or ``0x7f.1``) needs no lookup."""
    text = str(host).strip("[]")
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        pass
    try:
        socket.inet_aton(text)
        return True
    except (OSError, ValueError):
        return False


@pytest.fixture(autouse=True)
def _tests_never_use_the_internet(monkeypatch):
    """Every test talks to stand-in servers on this machine, never to linkedin.com or any real service.

    A test (or a future contribution) that reaches for the real internet fails loudly instead of quietly depending on
    it - or, worse, on somebody's real LinkedIn session. Tests that need name resolution replace ``getaddrinfo``
    themselves, which takes precedence over this guard.
    """
    for name in PROXY_VARIABLES:                      # a proxy would resolve names for us and hide the attempt from the guard
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    real_getaddrinfo, real_connect = socket.getaddrinfo, socket.socket.connect

    def getaddrinfo(host, *args, **kwargs):
        if not _is_local(host) and not _is_ip_literal(host):                  # (an address needs no lookup; connecting is guarded below)
            raise AssertionError(f"a test tried to look up {host!r}: tests must only talk to servers on this machine")
        return real_getaddrinfo(host, *args, **kwargs)

    def connect(self, address):
        host = address[0] if isinstance(address, tuple) else None            # (a path means a local unix socket)
        if host is not None and not _is_local(host):
            raise AssertionError(f"a test tried to connect to {host!r}: tests must only talk to servers on this machine")
        return real_connect(self, address)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", connect)
    try:
        from playwright.sync_api import BrowserType
    except ImportError:                                                        # (browser tests are skipped without it)
        return
    real_launch = BrowserType.launch_persistent_context

    def launch_persistent_context(self, *args, **kwargs):
        kwargs["args"] = [*(kwargs.get("args") or []), *CHROMIUM_OFFLINE]
        return real_launch(self, *args, **kwargs)

    monkeypatch.setattr(BrowserType, "launch_persistent_context", launch_persistent_context)


@pytest.fixture(autouse=True)
def _fresh_managed_env(monkeypatch):
    """Which key names count as "saved in Connecto's own .env" - and which server each was saved for - starts empty in every
    test."""
    monkeypatch.setattr(util, "_MANAGED_ENV", set())
    monkeypatch.setattr(util, "_ENV_ORIGINS", {})


@pytest.fixture()
def clean_env(monkeypatch):
    """No API key from the developer's own environment leaks in, and none a test (or the server) sets leaks out."""
    for name in {p.key_env for p in PRESETS.values() if p.key_env} | {"MY_SERVER_API_KEY", "CONNECTO_TEST_KEY"}:
        monkeypatch.setenv(name, "placeholder")          # remembers the old state ...
        monkeypatch.delenv(name)                         # ... and starts without it


@pytest.fixture()
def ai_key(monkeypatch):
    """ai_key(name, value, origin) makes a key available the way Connecto's own .env file does (set, marked as saved there,
    and tied to the server ``origin`` it was saved for - or to none, which only a server on this computer accepts)."""
    def put(name: str = "CONNECTO_TEST_KEY", value: str = "k-test-123456", origin: str | None = None) -> None:
        monkeypatch.setenv(name, value)
        util.mark_managed_env(name, origin)
    return put
