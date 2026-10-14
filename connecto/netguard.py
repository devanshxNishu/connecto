"""Downloading from an address that somebody else chose.

Connecto reads web pages whose addresses come from a config file, a spreadsheet or a redirect that a stranger's server
sent. This is the only place that opens those connections, so the rules live in one spot:

* only ``http://`` and ``https://``, never a user name or password in the address;
* the host name is looked up **once**, every address it gives must be an ordinary public one (unless the person turned
  ``allow_private`` on, on purpose), and the connection goes to one of those checked addresses - never to the name, so a DNS
  server that answers differently the second time (DNS rebinding) gains nothing;
* a redirect (at most 3) is checked in the same way before it is followed;
* proxy settings in the environment are not used: a proxy looks the name up for itself, which would make the check
  meaningless;
* the answer is capped in size and in time - a server that sends one byte every few seconds cannot hold a run for hours.

It uses nothing but the standard library (``http.client`` and ``ssl``): the pieces that would otherwise do the looking-up,
redirecting and proxying behind our back are exactly what is being kept out of the way.
"""
from __future__ import annotations

import functools
import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
import urllib.request
import zlib
from dataclasses import dataclass
from typing import Callable
from urllib.parse import quote, urljoin, urlsplit

MAX_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 3
TIMEOUT = 30                       # seconds without a single byte
TOTAL_SECONDS = 120                # seconds for the whole download, redirects included
MAX_URL_CHARS = 4_096
MAX_ADDRESSES = 6                  # how many of a name's addresses are tried, one after another
REDIRECT_CODES = (301, 302, 303, 307, 308)
READ_PIECE = 64 * 1024

Resolver = Callable[[str, "int | None"], "list[str]"]


class FetchError(ValueError):
    """A page could not be fetched (the message is meant for the person running Connecto)."""


class Refused(FetchError):
    """The address itself is not allowed: not a web address, or an internal one. Trying again cannot change that."""


# ----------------------------------------------------------------------------- which addresses are "internal"
_BLOCKED_V4 = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24",
    "192.0.2.0/24", "192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4",
    "240.0.0.0/4"))
_BLOCKED_V6 = tuple(ipaddress.ip_network(n) for n in (
    "::/96", "64:ff9b:1::/48", "100::/64", "2001::/23", "2001:db8::/32", "2002::/16", "3fff::/20", "5f00::/16", "fc00::/7",
    "fe80::/10", "fec0::/10", "ff00::/8"))
# IPv6 forms that carry an IPv4 address in their last 32 bits: what counts is that address (a NAT64 prefix is needed on
# networks without IPv4, so it is not refused as a whole - only when it leads to an internal address).
_CARRIES_V4 = tuple(ipaddress.ip_network(n) for n in ("::ffff:0:0/96", "::ffff:0:0:0/96", "64:ff9b::/96"))


def blocked_address(text) -> bool:
    """True for anything that is not an ordinary public internet address: loopback, private, link-local (including the
    169.254.169.254 metadata service), shared/CGNAT, multicast, reserved, unspecified, documentation ranges, and the IPv6
    ways of writing any of those (IPv4-mapped, IPv4-compatible, NAT64, 6to4, Teredo, unique-local, site-local).

    The ranges are listed here rather than left to ``ipaddress.is_global`` alone: that table has changed between Python
    versions, and this check must not."""
    try:
        ip = ipaddress.ip_address(str(text).strip("[]"))
    except ValueError:
        return True                                            # not an address at all: never connect to it
    if ip.version == 6 and any(ip in net for net in _CARRIES_V4):
        ip = ipaddress.IPv4Address(int(ip) & 0xFFFF_FFFF)
    blocked = _BLOCKED_V4 if ip.version == 4 else _BLOCKED_V6
    return any(ip in net for net in blocked) or not ip.is_global


def _ipv4_number(part: str) -> int | None:
    """One part of an address written as numbers: decimal, ``0x`` hexadecimal or ``0`` octal; None if it is not one."""
    if not part:
        return None
    base = 10
    if part[:2].lower() == "0x":
        base, part = 16, part[2:]
    elif len(part) > 1 and part[0] == "0":
        base, part = 8, part[1:]
    if not part:
        return 0
    if any(c not in "0123456789abcdef"[:base] for c in part.lower()):
        return None
    return int(part, base)


def numeric_host(host: str) -> str | None:
    """The IPv4 address a browser reads in a host written as numbers - ``2130706433``, ``0x7f.1``, ``127.1``,
    ``0177.0.0.1`` all mean 127.0.0.1 - in its usual dotted form; None for a host that is not written that way.

    This is the IPv4 parser of the WHATWG URL standard. Left to the system, these forms are read as addresses on Linux and
    macOS but looked up as names on Windows, so the guard reads them itself, the same way everywhere."""
    parts = host.split(".")
    if len(parts) > 1 and parts[-1] == "":
        parts.pop()                                            # (one dot at the end is allowed: "127.0.0.1.")
    last = parts[-1]
    if not (last.isascii() and last.isdigit()) and _ipv4_number(last) is None:
        return None                                            # it does not end in a number: a name
    numbers = [_ipv4_number(p) for p in parts]                 # (each at most 63 characters: the IDNA check came first)
    if len(numbers) > 4 or None in numbers:
        return None
    if any(n > 255 for n in numbers[:-1]) or numbers[-1] >= 256 ** (5 - len(numbers)):
        return None
    value = numbers[-1] + sum(n * 256 ** (3 - i) for i, n in enumerate(numbers[:-1]))
    return str(ipaddress.IPv4Address(value))


def resolve(host: str, port: int | None = None) -> list[str]:
    """Every address ``host`` has, in the order the system prefers them (tests replace this)."""
    found: list[str] = []
    for info in socket.getaddrinfo(host, port or 0, type=socket.SOCK_STREAM):
        address = str(info[4][0]).split("%", 1)[0]
        if address not in found:
            found.append(address)
    return found


# ----------------------------------------------------------------------------- checking an address
@dataclass(frozen=True)
class Target:
    url: str                       # as it will be reported (fragment removed)
    scheme: str
    host: str                      # the name as it goes on the wire (ASCII)
    port: int
    path: str                      # what follows "GET " - "/team?page=2"
    addresses: tuple[str, ...]     # what the name resolved to; every one of them has been checked


_PATH_SAFE = "/%:@!$&'()*+,;="          # (what a path may hold besides the letters, digits and "-._~" that quote() leaves alone)


def _request_target(parts) -> str:
    path = quote(parts.path or "/", safe=_PATH_SAFE)
    return path + ("?" + quote(parts.query, safe=_PATH_SAFE + "?") if parts.query else "")


def check_url(url, allow_private: bool = False, resolver: Resolver | None = None) -> Target:
    """The checked target for ``url``, or ``Refused`` / ``FetchError`` with the reason.

    http/https only, no credentials in the URL, and - unless ``allow_private`` - the host must resolve only to public
    addresses. A host written as numbers (``2130706433``, ``0x7f.1``) is read as the address a browser reads there, and
    names are looked up here, so ``localhost`` and names that point at 10.x, 192.168.x or 169.254.x are all caught; the
    connection later goes to these very addresses and not to the name.
    """
    text = str(url or "").strip()
    if not text or len(text) > MAX_URL_CHARS or any(ord(c) < 33 or ord(c) == 127 for c in text):
        raise Refused("not a valid URL")
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError as exc:
        raise Refused(f"not a valid URL ({exc})") from None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise Refused(f"only http:// and https:// URLs are allowed (got {parts.scheme or 'none'}://)")
    if "@" in parts.netloc:
        raise Refused("URLs with a user name or password in them are not allowed")
    host = parts.hostname
    if not host:
        raise Refused("the URL has no host name")
    try:
        wire_host = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise Refused("not a valid URL (the host name is not valid)") from None
    port = port or (443 if scheme == "https" else 80)
    wire_host = numeric_host(wire_host) or wire_host            # 2130706433, 0x7f.1: the address, as a browser reads it
    try:
        addresses = list((resolver or resolve)(wire_host, port))
    except OSError as exc:
        raise FetchError(f"cannot look up {host}: {exc}") from None
    if not addresses:
        raise FetchError(f"cannot look up {host}")
    if not allow_private:
        for address in addresses:
            if blocked_address(address):
                raise Refused(f"{host} points to a private or internal address ({address}); refused "
                              "(set allow_private: true on this source if you really mean it)")
    return Target(url=parts._replace(fragment="").geturl(), scheme=scheme, host=wire_host, port=port,
                  path=_request_target(parts), addresses=tuple(addresses))


def safe_http_url(url, allow_private: bool = False, resolver: Resolver | None = None) -> str:
    """``url`` (fragment removed) if it is safe to request, else ``ValueError`` with the reason."""
    return check_url(url, allow_private, resolver).url


# ----------------------------------------------------------------------------- connecting
def _connect(ip: str, port: int, timeout: float) -> socket.socket:
    """One TCP connection to exactly this address. (The one place a socket is opened: tests steer it to a stand-in.)"""
    sock = socket.socket(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect((ip, port))
    except BaseException:
        sock.close()
        raise
    return sock


def _open(addresses, port: int, timeout: float, deadline: float) -> socket.socket:
    error: OSError | None = None
    for ip in addresses[:MAX_ADDRESSES]:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        try:
            sock = _connect(ip, port, min(timeout, left))
        except OSError as exc:
            error = exc
            continue
        # From here on the socket's timeout is the read timeout only. The time limit for the whole download is the
        # watchdog's: a socket timeout cut to the time left would end at the same moment and give the wrong explanation.
        sock.settimeout(timeout)
        return sock
    raise error or TimeoutError("ran out of time before a connection could be made")


@functools.lru_cache(maxsize=1)
def _tls_context() -> ssl.SSLContext:
    """The usual checks (certificate chain and host name); the system's certificates, plus the ones ``certifi`` brings
    (a Python installed from python.org on a Mac has none of its own)."""
    context = ssl.create_default_context()
    # Never older than TLS 1.2, whatever this Python's or this system's defaults say (some builds report "minimum
    # supported" and leave the floor to the system's OpenSSL settings).
    if context.minimum_version != ssl.TLSVersion.MAXIMUM_SUPPORTED and context.minimum_version < ssl.TLSVersion.TLSv1_2:
        context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        import certifi

        context.load_verify_locations(cafile=certifi.where())
    except (ImportError, OSError):
        pass
    return context


class _Watchdog:
    """Hangs up when the time is up. A read timeout only fires when *nothing* arrives, so a server that sends a byte every
    few seconds - of its headers, of a TLS handshake, of a page - could otherwise keep one download (and the run behind it)
    going for hours."""

    def __init__(self, seconds: float):
        self.seconds = seconds
        self.ends = time.monotonic() + seconds
        self.expired = False
        self._sock = None
        self._lock = threading.Lock()
        self._timer = threading.Timer(seconds, self._fire)
        self._timer.daemon = True
        self._timer.start()

    def watch(self, sock) -> None:
        with self._lock:
            self._sock = sock
            late = self.expired
        if late:
            _hang_up(sock)

    def _fire(self) -> None:
        with self._lock:
            self.expired = True
            sock = self._sock
        if sock is not None:
            _hang_up(sock)

    def stop(self) -> None:
        self._timer.cancel()

    def time_is_up(self) -> bool:
        """The timer has fired, or is due to: a connection attempt cut to the time left can fail a moment before it."""
        return self.expired or time.monotonic() >= self.ends


def _hang_up(sock) -> None:
    try:
        socket.socket.shutdown(sock, socket.SHUT_RDWR)         # (not SSLSocket.shutdown: that also drops the TLS state under a reader)
    except OSError:
        pass


class _Tls(http.client.HTTPSConnection):
    """HTTPS over the connection we opened: certificate and host name are checked against the *name*, not the address."""

    watchdog: _Watchdog | None = None

    def connect(self) -> None:
        http.client.HTTPConnection.connect(self)               # TCP, through the pinned address (see _connection)
        tls = self._context.wrap_socket(self.sock, server_hostname=self.host, do_handshake_on_connect=False)
        self.sock = tls
        if self.watchdog is not None:
            self.watchdog.watch(tls)                           # (the handshake comes with the first request, which is a read as well)


def _connection(target: Target, timeout: float, deadline: float, dog: _Watchdog) -> http.client.HTTPConnection:
    def create(address, connect_timeout=None, source_address=None):        # what http.client calls to open its socket
        sock = _open(target.addresses, target.port, timeout, deadline)
        dog.watch(sock)
        return sock

    if target.scheme == "https":
        conn: http.client.HTTPConnection = _Tls(target.host, target.port, timeout=timeout, context=_tls_context())
        conn.watchdog = dog
    else:
        conn = http.client.HTTPConnection(target.host, target.port, timeout=timeout)
    conn._create_connection = create
    return conn


# ----------------------------------------------------------------------------- one download
@dataclass
class Reply:
    url: str                       # the address that was finally read (after redirects)
    status: int
    headers: dict[str, str]        # lower-case names; a repeated header is joined with ", "
    body: bytes                    # only for status 200


def size_label(n: int) -> str:
    return f"{n // (1024 * 1024)} MB" if n >= 1024 * 1024 else f"{n} bytes"


def _proxy_configured() -> bool:
    try:
        return any(scheme != "no" for scheme in urllib.request.getproxies())
    except Exception:  # noqa: BLE001 - only used to explain a failure
        return False


def _broken(exc: BaseException, dog: _Watchdog, url: str, what: str, reading: bool) -> FetchError:
    if dog.time_is_up():
        return FetchError(f"{what} took more than {dog.seconds:g} seconds to download")
    name = type(exc).__name__
    if reading:
        return FetchError(f"download broke off ({name})")
    hint = ("; this computer is set up to use a proxy, and Connecto connects to web pages directly, without one"
            if _proxy_configured() else "")
    return FetchError(f"could not load {url} ({name}){hint}")


def _headers_of(resp: http.client.HTTPResponse) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, value in resp.getheaders():
        key = name.lower()
        out[key] = f"{out[key]}, {value}" if key in out else value
    return out


def _decoder(encoding: str | None):
    coding = (encoding or "identity").strip().lower()
    if coding == "identity":
        return None
    if coding in ("gzip", "x-gzip", "deflate"):
        return zlib.decompressobj(47)                          # (a zlib or a gzip header, whichever comes)
    raise FetchError(f"the server answered with {coding}-compressed data, which Connecto cannot read")


def _inflate(decoder, piece: bytes, room: int) -> bytes:
    """The decoded ``piece``, at most ``room + 1`` bytes of it: one more than allowed means "too much". (What does not fit is
    left in the decoder, and the caller stops there. ``room`` is never negative: a max_length of 0 would mean "no limit".)"""
    return decoder.decompress(piece, room + 1)


def _read_body(resp: http.client.HTTPResponse, headers: dict[str, str], max_bytes: int, what: str, url: str,
               dog: _Watchdog) -> bytes:
    def too_big() -> FetchError:
        return FetchError(f"{what} is larger than {size_label(max_bytes)}")

    try:
        declared = int(headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared > max_bytes:
        raise too_big()
    decoder = _decoder(headers.get("content-encoding"))
    chunks: list[bytes] = []
    arrived = total = 0
    try:
        while True:
            piece = resp.read1(READ_PIECE)
            if not piece:
                break
            arrived += len(piece)
            if arrived > max_bytes:
                raise too_big()
            if decoder is not None:
                piece = _inflate(decoder, piece, max_bytes - total)
            total += len(piece)
            if total > max_bytes:
                raise too_big()
            chunks.append(piece)
    except FetchError:
        raise
    except zlib.error:
        raise FetchError(f"{what} could not be decompressed") from None
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise _broken(exc, dog, url, what, reading=True) from None
    if dog.expired:
        raise _broken(TimeoutError(), dog, url, what, reading=True)
    if (declared and arrived < declared) or (decoder is not None and not decoder.eof):
        raise FetchError("download broke off (the connection closed before the whole page arrived)")
    return b"".join(chunks)


def _exchange(target: Target, headers: dict[str, str], *, timeout: float, deadline: float, dog: _Watchdog,
              max_bytes: int, what: str, check: Callable[[dict], None] | None) -> tuple[Reply, str | None]:
    """One request to one checked target. Returns the reply and, for a redirect, where it points."""
    conn = _connection(target, timeout, deadline, dog)
    try:
        try:
            conn.request("GET", target.path, headers=headers)
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise _broken(exc, dog, target.url, what, reading=False) from None
        reply_headers = _headers_of(resp)
        if resp.status in REDIRECT_CODES:
            location = reply_headers.get("location", "").strip()
            if not location:
                raise FetchError(f"{what} redirected nowhere")
            return Reply(target.url, resp.status, reply_headers, b""), location
        if resp.status != 200:
            return Reply(target.url, resp.status, reply_headers, b""), None
        if check is not None:
            check(reply_headers)
        return Reply(target.url, 200, reply_headers, _read_body(resp, reply_headers, max_bytes, what, target.url, dog)), None
    finally:
        conn.close()


def _location(raw: str) -> str:
    """A redirect's ``Location`` as text: http.client reads header bytes as latin-1, servers mean UTF-8; a space is
    written by some servers and corrected here, as browsers do."""
    try:
        raw = raw.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return raw.replace(" ", "%20")


def get(url: str, *, allow_private: bool = False, headers: dict[str, str] | None = None, max_bytes: int = MAX_BYTES,
        timeout: float = TIMEOUT, total_seconds: float = TOTAL_SECONDS, max_redirects: int = MAX_REDIRECTS,
        what: str = "page", check: Callable[[dict], None] | None = None, resolver: Resolver | None = None) -> Reply:
    """GET ``url`` and return the reply (the body only for status 200). Raises ``FetchError`` (or ``Refused``).

    ``check(headers)`` sees the headers of a 200 answer before any of the body is read and may raise ``FetchError``
    (e.g. "not an HTML page"). ``max_bytes`` caps what arrives, compressed and decompressed alike."""
    wanted = {"Accept-Encoding": "gzip", "Connection": "close", **(headers or {})}
    dog = _Watchdog(total_seconds)
    deadline = time.monotonic() + total_seconds
    try:
        current, hops = url, 0
        while True:
            target = check_url(current, allow_private, resolver)
            reply, location = _exchange(target, wanted, timeout=timeout, deadline=deadline, dog=dog, max_bytes=max_bytes,
                                        what=what, check=check)
            if location is None:
                return reply
            if hops >= max_redirects:
                raise FetchError(f"too many redirects (more than {max_redirects})")
            hops += 1
            current = urljoin(target.url, _location(location))
    finally:
        dog.stop()


def decode_body(body: bytes, content_type: str = "") -> str:
    """Text from a page's bytes: the charset the server named, else UTF-8, else a guess."""
    named = re.search(r"charset=([\w.-]+)", content_type or "", re.I)
    for encoding in ([named.group(1)] if named else []) + ["utf-8"]:
        try:
            return body.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    from bs4 import UnicodeDammit

    return UnicodeDammit(body, is_html=True).unicode_markup or body.decode("utf-8", errors="replace")
