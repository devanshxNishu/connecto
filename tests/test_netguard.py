"""Tests for ``connecto/netguard.py`` - the one place Connecto opens a connection to an address somebody else chose.

Everything runs against stand-in servers on this machine. Where a test needs a *public-looking* name it supplies a resolver
that answers with a public address, and steers the one function that opens sockets (``netguard._connect``) to a stand-in
server: what is then asserted is the address each connection was *meant* for.
"""
from __future__ import annotations

import gzip
import ipaddress
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import zlib
from types import SimpleNamespace

import pytest

from connecto import netguard
from connecto.netguard import FetchError, Refused

from raw_server import RawServer, drip_body, drip_headers, endless_body
from web_stand_in import Pages, Steer

PUBLIC = "93.184.216.34"
PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"


# ----------------------------------------------------------------------------- helpers
@pytest.fixture
def steer(monkeypatch):
    return Steer(monkeypatch)


@pytest.fixture
def raw():
    """raw(handler) -> a started RawServer, stopped when the test ends."""
    servers: list[RawServer] = []

    def make(handler) -> RawServer:
        server = RawServer(handler).start()
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.stop()


def names(**table):
    """A resolver that knows these names (``names(a_test=[...])`` is for "a.test"; use ``table`` for others) and lets
    addresses and "localhost" resolve as they are."""
    table = {k.replace("_", "."): v for k, v in table.items()}

    def resolver(host, port=None):
        return table[host] if host in table else netguard.resolve(host, port)

    return resolver


def within(seconds: float, fn, *args, **kwargs):
    """Run ``fn`` in a thread; fail (rather than hang the whole run) when it is still going after ``seconds``."""
    box: dict = {}

    def run():
        try:
            box["value"] = fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - handed back to the test below
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        pytest.fail(f"still running after {seconds} s")
    if "error" in box:
        raise box["error"]
    return box["value"]


def http_bytes(body: bytes = b"", *headers: str, status: str = "200 OK") -> bytes:
    return (f"HTTP/1.1 {status}\r\n" + "".join(h + "\r\n" for h in headers) + "\r\n").encode("latin-1") + body


def serving(data: bytes):
    """A RawServer handler that sends ``data`` and hangs up."""
    def handle(conn, request, srv):
        srv.send(conn, data)
    return handle


# ----------------------------------------------------------------------------- which addresses are internal
BLOCKED = [
    # IPv4: every special-purpose block, first and last address
    "0.0.0.0", "0.1.2.3", "10.0.0.1", "10.255.255.255", "100.64.0.1", "100.127.255.255", "127.0.0.1", "127.255.255.254",
    "169.254.169.254", "172.16.0.1", "172.31.255.255", "192.0.0.1", "192.0.2.1", "192.88.99.1", "192.168.0.1",
    "192.168.255.255", "198.18.0.1", "198.19.255.255", "198.51.100.1", "203.0.113.1", "224.0.0.1", "239.255.255.255",
    "240.0.0.1", "255.255.255.255",
    # IPv6: its own special ranges ...
    "::", "::1", "100::1", "2001::1", "2001:db8::1", "3fff::1", "5f00::1", "fc00::1", "fd12:3456:789a::1", "fe80::1",
    "fe80::1%eth0", "fec0::1", "ff02::1", "64:ff9b:1::1",
    # ... and every way of writing an IPv4 address inside one
    "::ffff:127.0.0.1", "::ffff:7f00:1", "::ffff:10.0.0.1", "::ffff:169.254.169.254", "::ffff:0:127.0.0.1",
    "::127.0.0.1", "::10.0.0.1", "::8.8.8.8",
    "64:ff9b::7f00:1", "64:ff9b::a00:1", "64:ff9b::a9fe:a9fe", "64:ff9b::c0a8:101",
    "2002:7f00:1::1", "2002:a00:1::1", "2002:808:808::1",
    # not an address at all: never connected to
    "", "localhost", "1.2.3", "999.1.1.1", "1.2.3.4.5", "010.0.0.1", "not-an-address", "::g",
]
ALLOWED = [
    "1.1.1.1", "8.8.8.8", PUBLIC, "172.15.255.255", "172.32.0.1", "100.63.255.255", "100.128.0.1", "192.0.1.1",
    "198.17.255.255", "198.20.0.1", "223.255.255.255", "2606:4700:4700::1111", PUBLIC_V6, "2a00:1450:4001::200e",
    "::ffff:8.8.8.8", "::ffff:0:8.8.8.8", "64:ff9b::808:808", "[2606:4700:4700::1111]",
    # the addresses just before and after each internal block are ordinary ones: a block may not creep over its edge
    "1.0.0.1", "9.255.255.255", "11.0.0.0", "100.63.255.255", "100.128.0.0", "126.255.255.255", "128.0.0.0",
    "169.253.255.255", "169.255.0.0", "172.15.255.255", "172.32.0.0", "191.255.255.255", "192.0.1.0", "192.0.1.255",
    "192.0.3.0", "192.88.98.255", "192.88.100.0", "192.167.255.255", "192.169.0.0", "198.17.255.255", "198.20.0.0",
    "198.51.99.255", "198.51.101.0", "203.0.112.255", "203.0.114.0", "223.255.255.255",
    "2000::1", "2001:200::1", "2001:db7::1", "2001:db9::1", "2001:ffff::1", "2003::1", "3ffe:ffff::1", "3fff:1000::1",
    "5eff::1", "5f01::1", "fbff::1", "fe00::1", "fe7f::1", "100:0:0:1::1", "64:ff9b:2::1",
]


@pytest.mark.parametrize("address", BLOCKED)
def test_internal_addresses_are_blocked(address):
    assert netguard.blocked_address(address) is True


@pytest.mark.parametrize("address", ALLOWED)
def test_ordinary_public_addresses_are_not(address):
    assert netguard.blocked_address(address) is False


def test_the_table_of_blocked_ranges_stands_on_its_own(monkeypatch):
    """Python's own ``is_global`` has changed from one release to the next: every listed range must be refused even if it
    said "global" for all of them."""
    for cls in (ipaddress.IPv4Address, ipaddress.IPv6Address):
        monkeypatch.setattr(cls, "is_global", property(lambda self: True))
    for net in list(netguard._BLOCKED_V4) + list(netguard._BLOCKED_V6):
        assert netguard.blocked_address(str(net[0])) and netguard.blocked_address(str(net[-1])), net
    for text in ("::ffff:0:0/96", "::ffff:0:0:0/96", "64:ff9b::/96"):                # the forms that carry an IPv4 address
        net = ipaddress.ip_network(text)
        assert netguard.blocked_address(str(net[0x0A000001])) and not netguard.blocked_address(str(net[0x08080808])), text
    assert netguard.blocked_address("127.0.0.1") and not netguard.blocked_address("8.8.8.8")


def test_the_table_alone_decides_every_listed_address_whatever_python_says(monkeypatch):
    """With ``is_global`` saying "global" for everything, each address above that is internal must still be refused - so
    every range in the table is needed - and each ordinary one must still be let through - so none of them is too wide."""
    for cls in (ipaddress.IPv4Address, ipaddress.IPv6Address):
        monkeypatch.setattr(cls, "is_global", property(lambda self: True))
    assert [a for a in BLOCKED if not netguard.blocked_address(a)] == []
    assert [a for a in ALLOWED if netguard.blocked_address(a)] == []


# The IANA special-purpose address registries (RFC 6890 and what has followed), written out a second time on purpose: the
# table in the code is held against this one at both ends of every range, and just outside it.
REGISTRY_V4 = ["0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24",
               "192.0.2.0/24", "192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24",
               "224.0.0.0/4", "240.0.0.0/4"]
REGISTRY_V6 = ["::/96", "64:ff9b:1::/48", "100::/64", "2001::/23", "2001:db8::/32", "2002::/16", "3fff::/20", "5f00::/16",
               "fc00::/7", "fe80::/10", "fec0::/10", "ff00::/8"]
CARRIERS = {"IPv4-mapped": "::ffff:0:0/96", "IPv4-translated": "::ffff:0:0:0/96", "NAT64": "64:ff9b::/96"}


def in_registry(ip) -> bool:
    return any(ip in ipaddress.ip_network(net) for net in (REGISTRY_V4 if ip.version == 4 else REGISTRY_V6))


def around(net: str) -> list:
    """First, middle and last address of a range, and the address just before and just after it."""
    n = ipaddress.ip_network(net)
    first, last = n.network_address, n.broadcast_address
    found = [first, first + (int(last) - int(first)) // 2, last]
    if int(first) > 0:
        found.append(first - 1)
    if int(last) < 2 ** n.max_prefixlen - 1:
        found.append(last + 1)
    return found


@pytest.fixture
def python_says_global(monkeypatch):
    for cls in (ipaddress.IPv4Address, ipaddress.IPv6Address):
        monkeypatch.setattr(cls, "is_global", property(lambda self: True))


@pytest.mark.parametrize("net", REGISTRY_V4 + REGISTRY_V6)
def test_every_range_ends_where_the_registry_says_it_does(net, python_says_global):
    for ip in around(net):
        assert netguard.blocked_address(str(ip)) is in_registry(ip), (net, ip)


@pytest.mark.parametrize("carrier", sorted(CARRIERS))
def test_an_address_written_inside_another_is_judged_by_the_one_inside(carrier, python_says_global):
    prefix = ipaddress.ip_network(CARRIERS[carrier])
    inside = [ip for net in REGISTRY_V4 for ip in around(net)] + [ipaddress.ip_address(a) for a in ("8.8.8.8", "128.0.0.1", "200.1.1.1")]
    for v4 in inside:
        wrapped = ipaddress.IPv6Address(int(prefix.network_address) | int(v4))
        assert netguard.blocked_address(str(wrapped)) is in_registry(v4), (carrier, v4)
    for outside in (prefix.network_address - 1, prefix.broadcast_address + 1):         # (just beside the carrier: not one)
        assert netguard.blocked_address(str(outside)) is in_registry(outside), (carrier, outside)


def test_whatever_python_calls_non_global_is_refused_as_well(monkeypatch):
    """A net under the table: a range that is reserved tomorrow and that the table does not list yet."""
    monkeypatch.setattr(ipaddress.IPv4Address, "is_global", property(lambda self: False))
    monkeypatch.setattr(ipaddress.IPv6Address, "is_global", property(lambda self: False))
    assert netguard.blocked_address("8.8.8.8") and netguard.blocked_address("2606:4700:4700::1111")


def test_nat64_is_judged_by_the_address_inside_it():
    assert not netguard.blocked_address("64:ff9b::808:808")          # (networks without IPv4 need it)
    assert netguard.blocked_address("64:ff9b::7f00:1") and netguard.blocked_address("64:ff9b::a00:1")


# ----------------------------------------------------------------------------- checking an address
def test_check_url_resolves_the_name_once_and_normalises_what_goes_on_the_wire():
    calls = []

    def resolver(host, port=None):
        calls.append((host, port))
        return [PUBLIC, PUBLIC_V6]

    target = netguard.check_url("HTTPS://Exämple.COM/café?q=é&x=a%20b#frag", resolver=resolver)
    assert (target.scheme, target.host, target.port) == ("https", "xn--exmple-cua.com", 443)
    assert target.addresses == (PUBLIC, PUBLIC_V6)                    # in the order the resolver gave them
    assert target.path == "/caf%C3%A9?q=%C3%A9&x=a%20b" and "#" not in target.url
    assert calls == [("xn--exmple-cua.com", 443)]
    assert netguard.check_url("http://a.test", resolver=resolver).path == "/"
    assert netguard.check_url("http://a.test:8080/x", resolver=resolver).port == 8080
    assert netguard.check_url("http://a.test/p%20q?r=%41", resolver=resolver).path == "/p%20q?r=%41"     # existing escapes stay
    assert netguard.check_url("http://[2606:4700:4700::1111]:81/", resolver=lambda h, p=None: [h]).host == "2606:4700:4700::1111"


def test_the_request_line_keeps_what_belongs_in_it_and_escapes_the_rest():
    public = lambda host, port=None: [PUBLIC]
    kept = "/a:@!$&'()*+,;=-._~/b"                                           # everything RFC 3986 allows in a path as it is
    assert netguard.check_url(f"http://a.test{kept}?q={kept}?x=1", resolver=public).path == f"{kept}?q={kept}?x=1"
    assert netguard.check_url('http://a.test/a[b]{c}|d^e"f<g>h`i\\j', resolver=public).path == "/a%5Bb%5D%7Bc%7D%7Cd%5Ee%22f%3Cg%3Eh%60i%5Cj"
    assert netguard.check_url("http://a.test/p?a=[1]&b=|", resolver=public).path == "/p?a=%5B1%5D&b=%7C"
    assert netguard.check_url("http://a.test?x=1", resolver=public).path == "/?x=1"


@pytest.mark.parametrize("url", [
    None, "", "   ", "http://", "http:///x", "file:///etc/passwd", "ftp://a.test/", "javascript:alert(1)", "gopher://a.test/",
    "data:text/html,hi", "mailto:a@b.test", "//a.test/x", "a.test/x", "http://user:secret@a.test/", "http://@a.test/",
    "http://a.test:99999/", "http://a.test:abc/", "http://a b.test/", "http://a.test/x y", "http://a.test/\x00", "http://a.test/\x7f",
    "http://a.test/\r\nHost: evil.test", "http://" + "a" * 70 + ".test/", "http://a..test/", "http://a.test/" + "x" * 5000,
])
def test_things_that_are_not_web_addresses_are_refused_before_anything_is_looked_up(url):
    def resolver(host, port=None):
        pytest.fail("a name was looked up for an address that is not valid")

    with pytest.raises(Refused):
        netguard.check_url(url, allow_private=True, resolver=resolver)


def test_an_internal_address_is_refused_unless_allowed_and_a_failed_lookup_is_not_a_refusal():
    resolver = names(intranet_test=["10.1.2.3"], mixed_test=[PUBLIC, "127.0.0.1"], ok_test=[PUBLIC], v6_test=[PUBLIC_V6],
                     mapped_test=["::ffff:10.0.0.1"], empty_test=[])
    assert netguard.check_url("https://ok.test/", resolver=resolver).addresses == (PUBLIC,)
    assert netguard.check_url("https://v6.test/", resolver=resolver).addresses == (PUBLIC_V6,)
    for host in ("intranet.test", "mixed.test", "mapped.test"):          # one internal answer among the others is enough
        with pytest.raises(Refused, match="points to a private or internal address"):
            netguard.check_url(f"https://{host}/", resolver=resolver)
        assert netguard.check_url(f"https://{host}/", allow_private=True, resolver=resolver)       # only on purpose
    for url in ("http://127.0.0.1/", "http://localhost/", "http://[::1]/", "http://2130706433/", "http://0x7f.1/",
                "http://169.254.169.254/latest/meta-data/", "http://[::ffff:127.0.0.1]/", "http://100.64.0.1/"):
        with pytest.raises(Refused, match="private or internal"):
            netguard.check_url(url)
    for resolver in (names(empty_test=[]), lambda h, p=None: (_ for _ in ()).throw(OSError("no such host"))):
        with pytest.raises(FetchError, match="cannot look up") as caught:
            netguard.check_url("https://empty.test/", resolver=resolver)
        assert not isinstance(caught.value, Refused)                      # (a DNS hiccup may pass; a refusal never will)


@pytest.mark.parametrize("host,address", [("2130706433", "127.0.0.1"), ("0x7f.1", "127.0.0.1"), ("127.1", "127.0.0.1"),
                                          ("0177.0.0.1", "127.0.0.1"), ("0xa9.0xfe.0xa9.0xfe", "169.254.169.254"),
                                          ("3232235777", "192.168.1.1"), ("127.0.0.1.", "127.0.0.1")])
def test_a_host_written_as_numbers_is_read_as_the_address_a_browser_reads_on_every_system(host, address):
    asked = []

    def like_windows(name, port=None):                  # only the usual dotted form is an address; the rest is looked up
        asked.append(name)
        if name.count(".") == 3 and all(part.isdigit() for part in name.split(".")):
            return [name]
        raise OSError("no such host")

    with pytest.raises(Refused, match=rf"private or internal address \({re.escape(address)}\)"):
        netguard.check_url(f"http://{host}/", resolver=like_windows)
    assert asked == [address]


def test_only_a_host_that_is_all_numbers_is_read_as_an_address():
    assert netguard.numeric_host("1572395042") == "93.184.216.34" and netguard.numeric_host("0x") == "0.0.0.0"
    for name in ("example.com", "example.123", "1.2.3.4.5", "1.2.3.256", "4294967296", "08", "::1", "localhost", "0x7g"):
        assert netguard.numeric_host(name) is None, name


def test_refused_is_a_fetch_error_is_a_value_error():
    assert issubclass(Refused, FetchError) and issubclass(FetchError, ValueError)


def test_safe_http_url_returns_the_address_without_its_fragment():
    assert netguard.safe_http_url("https://a.test/x#frag", resolver=names(a_test=[PUBLIC])) == "https://a.test/x"
    with pytest.raises(ValueError, match="private or internal"):
        netguard.safe_http_url("http://10.0.0.5/")


def test_an_address_may_be_as_long_as_the_limit_and_not_one_character_longer():
    public = lambda host, port=None: [PUBLIC]
    start = "http://a.test/"
    exactly = start + "x" * (netguard.MAX_URL_CHARS - len(start))
    assert len(exactly) == netguard.MAX_URL_CHARS and netguard.check_url(exactly, resolver=public).url == exactly
    with pytest.raises(Refused, match="not a valid URL"):
        netguard.check_url(exactly + "x", resolver=public)


def test_resolve_lists_each_address_once_in_the_order_given_without_a_zone_id(monkeypatch):
    asked = []

    def fake(host, port, type=0, **kwargs):
        asked.append((host, port, type))
        return [(socket.AF_INET6, type, 6, "", ("fe80::1%eth0", 443, 0, 2)),
                (socket.AF_INET, type, 6, "", (PUBLIC, 443)),
                (socket.AF_INET, type, 6, "", (PUBLIC, 443)),                     # (the same one again)
                (socket.AF_INET6, type, 6, "", (PUBLIC_V6, 443, 0, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    assert netguard.resolve("a.test", 443) == ["fe80::1", PUBLIC, PUBLIC_V6]
    assert netguard.resolve("b.test") == ["fe80::1", PUBLIC, PUBLIC_V6]                  # (no port: 0)
    assert asked == [("a.test", 443, socket.SOCK_STREAM), ("b.test", 0, socket.SOCK_STREAM)]


# ----------------------------------------------------------------------------- one download
def test_get_returns_the_reply_and_sends_plain_honest_headers():
    with Pages() as srv:
        srv.html("/team", "<html>hello</html>")
        reply = netguard.get(f"{srv.base}/team?x=1#frag", allow_private=True, headers={"User-Agent": "Test/1.0", "Accept": "text/html"})
        assert (reply.url, reply.status, reply.body) == (f"{srv.base}/team?x=1", 200, b"<html>hello</html>")
        assert reply.headers["content-type"].startswith("text/html")           # lower-case names
        sent = srv.requests[0]
        assert sent["host"] == f"127.0.0.1:{srv.port}" and sent[":path"] == "/team?x=1"
        assert sent["user-agent"] == "Test/1.0" and sent["accept"] == "text/html"
        assert sent["accept-encoding"] == "gzip" and sent["connection"] == "close"
        assert not {"cookie", "authorization", "referer", "proxy-authorization"} & set(sent)


def test_get_returns_other_statuses_without_a_body_and_leaves_the_judging_to_the_caller():
    with Pages() as srv:
        srv.html("/boom", "oops", status=500)
        srv.html("/slow", "later", status=429, headers={"Retry-After": "7"})
        boom = netguard.get(f"{srv.base}/boom", allow_private=True)
        slow = netguard.get(f"{srv.base}/slow", allow_private=True)
        nope = netguard.get(f"{srv.base}/nope", allow_private=True)
        assert (boom.status, boom.body, slow.status, slow.headers["retry-after"], nope.status) == (500, b"", 429, "7", 404)
        for status in (201, 206, 300, 304, 410):                          # only a 200 is a page; whatever else is the caller's to judge
            srv.html(f"/s{status}", "some body", status=status)
            other = netguard.get(f"{srv.base}/s{status}", allow_private=True)
            assert (other.status, other.body) == (status, b""), status


def test_a_header_sent_twice_is_one_header_with_both_values(raw):
    server = raw(serving(http_bytes(b"ok", "X-Multi: first", "x-multi: second", "Content-Length: 2", "Content-Type: text/plain")))
    reply = netguard.get(f"http://127.0.0.1:{server.port}/", allow_private=True)
    assert reply.headers["x-multi"] == "first, second" and reply.headers["content-type"] == "text/plain" and reply.body == b"ok"


def test_get_follows_at_most_three_redirects_and_resolves_each_against_the_one_before():
    with Pages() as srv:
        srv.html("/end", "<html>end</html>")
        srv.redirect("/a", "/b", 301)
        srv.redirect("/b", "c", 302)                                    # relative to /b
        srv.redirect("/c", f"{srv.base}/end#frag", 307)
        srv.redirect("/r0", "/r1", 308)
        srv.redirect("/r1", "/r2", 303)
        srv.redirect("/r2", "/r3")
        srv.redirect("/r3", "/end")
        reply = netguard.get(f"{srv.base}/a", allow_private=True)
        assert (reply.url, reply.body) == (f"{srv.base}/end", b"<html>end</html>")
        with pytest.raises(FetchError, match=r"too many redirects \(more than 3\)"):
            netguard.get(f"{srv.base}/r0", allow_private=True)
        assert netguard.get(f"{srv.base}/r1", allow_private=True).body == b"<html>end</html>"      # three hops: fine
        with pytest.raises(FetchError, match=r"too many redirects \(more than 1\)"):
            netguard.get(f"{srv.base}/a", allow_private=True, max_redirects=1)
        with pytest.raises(FetchError, match=r"too many redirects \(more than 0\)"):
            netguard.get(f"{srv.base}/a", allow_private=True, max_redirects=0)


def test_a_relative_redirect_is_resolved_against_the_address_just_requested_not_the_first_one():
    with Pages() as srv:
        srv.redirect("/one/a", "/two/b", 301)
        srv.redirect("/two/b", "c", 302)
        srv.html("/two/c", "<html>in two</html>")
        srv.html("/one/c", "<html>in one</html>")
        reply = netguard.get(f"{srv.base}/one/a", allow_private=True)
        assert (reply.url, reply.body) == (f"{srv.base}/two/c", b"<html>in two</html>")


def test_a_header_that_would_split_the_request_is_never_sent(raw):
    server = raw(serving(http_bytes(b"ok", "Content-Length: 2")))
    for bad in ("fine\r\nX-Injected: yes", "fine\nX-Injected: yes"):
        with pytest.raises(FetchError, match=r"could not load http://127\.0\.0\.1:\d+/ \(ValueError\)"):
            netguard.get(f"http://127.0.0.1:{server.port}/", allow_private=True, headers={"User-Agent": bad})
    assert server.requests == []


def test_a_content_length_that_is_not_a_number_is_ignored_and_the_page_is_read_to_its_end(raw):
    server = raw(serving(http_bytes(b"<html>fine</html>", "Content-Length: lots", "Content-Type: text/html")))
    assert netguard.get(f"http://127.0.0.1:{server.port}/", allow_private=True).body == b"<html>fine</html>"


def test_a_redirect_to_nowhere_or_to_something_that_is_not_a_web_page_is_refused():
    with Pages() as srv:
        srv.redirect("/none", "", 302)
        with pytest.raises(FetchError, match="redirected nowhere"):
            netguard.get(f"{srv.base}/none", allow_private=True, what="page")
        for target in ("ftp://a.test/x", "file:///etc/passwd", "javascript:alert(1)", "gopher://a.test/", "data:text/html,x"):
            srv.redirect("/bad", target)
            with pytest.raises(Refused, match="only http:// and https://"):
                netguard.get(f"{srv.base}/bad", allow_private=True)
        assert srv.hits.count("/none") == 1


def test_redirect_locations_written_loosely_are_understood():
    with Pages() as srv:
        srv.html("/my%20page", "<html>spaced</html>")
        srv.html("/caf%C3%A9", "<html>utf8</html>")
        srv.redirect("/space", "/my page")
        srv.routes["/utf8"] = (302, "", "text/html", {"Location": "/café".encode("utf-8").decode("latin-1")})
        assert netguard.get(f"{srv.base}/space", allow_private=True).body == b"<html>spaced</html>"
        assert netguard.get(f"{srv.base}/utf8", allow_private=True).body == b"<html>utf8</html>"


def test_the_check_hook_sees_the_headers_of_a_200_before_the_body_and_only_then():
    seen = []

    def check(headers):
        seen.append(dict(headers))
        raise FetchError("not what we wanted")

    with Pages() as srv:
        srv.html("/ok", "x" * 100)
        srv.html("/gone", "gone", status=404)
        with pytest.raises(FetchError, match="not what we wanted"):
            netguard.get(f"{srv.base}/ok", allow_private=True, check=check)
        assert len(seen) == 1 and seen[0]["content-type"].startswith("text/html") and seen[0]["content-length"] == "100"
        assert netguard.get(f"{srv.base}/gone", allow_private=True, check=check).status == 404       # (not called for a 404)
        assert len(seen) == 1


def test_the_name_of_the_thing_downloaded_is_in_the_messages():
    with Pages() as srv:
        srv.html("/big", "x" * 5000)
        with pytest.raises(FetchError, match="the company list is larger than 1000 bytes"):
            netguard.get(f"{srv.base}/big", allow_private=True, max_bytes=1000, what="the company list")
    with pytest.raises(FetchError, match="could not load http://127.0.0.1:9/ "):
        netguard.get("http://127.0.0.1:9/", allow_private=True)


def test_size_label():
    assert netguard.size_label(1000) == "1000 bytes" and netguard.size_label(5 * 1024 * 1024) == "5 MB"
    assert netguard.size_label(1024 * 1024 - 1) == "1048575 bytes"


def test_decode_body_uses_the_named_charset_then_utf8_then_a_guess():
    text = "Café – naïve"
    assert netguard.decode_body(text.encode("utf-8"), "text/html") == text
    assert netguard.decode_body(text.encode("utf-8"), "text/html; charset=utf-8") == text
    assert netguard.decode_body("Café".encode("latin-1"), "text/html; charset=iso-8859-1") == "Café"
    assert netguard.decode_body("é".encode("utf-8"), "text/html; charset=iso-8859-1") == "Ã©"           # (what the server says, right or wrong)
    assert netguard.decode_body(text.encode("utf-8"), "text/html; charset=no-such-charset") == text
    assert "Café" in netguard.decode_body("<html>Café</html>".encode("latin-1"), "text/html")           # not UTF-8, no charset: guessed
    assert netguard.decode_body(b"", "") == ""


# ----------------------------------------------------------------------------- the connection goes to the checked address
def test_rebinding_gains_nothing(steer):
    with Pages() as srv:
        srv.html("/ok", "<html>fine</html>")
        steer.to(srv.port)
        looked_up = []
        answers = iter([[PUBLIC], ["127.0.0.1"], ["127.0.0.1"], ["10.0.0.1"]])

        def rebinding(host, port=None):
            looked_up.append(host)
            return next(answers)

        reply = netguard.get(f"http://rebind.test:{srv.port}/ok", resolver=rebinding)
        assert reply.status == 200 and reply.body == b"<html>fine</html>"
        assert looked_up == ["rebind.test"]                                # looked up once ...
        assert steer.meant == [(PUBLIC, srv.port)]                         # ... and the connection was meant for what was checked
        assert srv.requests[0]["host"] == f"rebind.test:{srv.port}"       # (the server still hears the name it was asked for)


def test_one_internal_address_among_the_answers_refuses_the_lot_and_nothing_connects(steer):
    with Pages() as srv:
        steer.to(srv.port)
        for answer in ([PUBLIC, "127.0.0.1"], ["127.0.0.1", PUBLIC], [PUBLIC_V6, "::1"], [PUBLIC, "::ffff:10.0.0.1"], ["64:ff9b::7f00:1"]):
            with pytest.raises(Refused, match="private or internal"):
                netguard.get(f"http://mixed.test:{srv.port}/x", resolver=lambda h, p=None, a=answer: a)
        assert steer.meant == [] and srv.hits == []


def test_the_next_checked_address_is_tried_when_the_first_does_not_answer(steer):
    with Pages() as srv:
        srv.html("/ok", "<html>fine</html>")
        steer.to(srv.port)
        steer.down = {PUBLIC_V6}
        reply = netguard.get(f"http://dual.test:{srv.port}/ok", resolver=lambda h, p=None: [PUBLIC_V6, PUBLIC])
        assert reply.status == 200
        assert steer.meant == [(PUBLIC_V6, srv.port), (PUBLIC, srv.port)]       # the order the system prefers, one after the other


def test_only_a_few_addresses_are_tried_and_a_total_failure_is_reported_plainly(steer):
    steer.down = {f"93.184.216.{n}" for n in range(1, 40)}
    many = [f"93.184.216.{n}" for n in range(1, 40)]
    with pytest.raises(FetchError, match=r"could not load http://many.test/ \(ConnectionRefusedError\)"):
        netguard.get("http://many.test/", resolver=lambda h, p=None: many)
    assert [ip for ip, _ in steer.meant] == many[:6] and netguard.MAX_ADDRESSES == 6          # ("at most six addresses")


def test_every_hop_of_a_redirect_is_checked_and_connected_to_on_its_own_addresses(steer):
    with Pages() as srv:
        srv.redirect("/start", f"http://b.test:{srv.port}/there")
        srv.html("/there", "<html>there</html>")
        steer.to(srv.port)
        reply = netguard.get(f"http://a.test:{srv.port}/start", resolver=names(a_test=[PUBLIC], b_test=["93.184.216.99"]))
        assert reply.body == b"<html>there</html>"
        assert steer.meant == [(PUBLIC, srv.port), ("93.184.216.99", srv.port)]
        assert [r["host"] for r in srv.requests] == [f"a.test:{srv.port}", f"b.test:{srv.port}"]


def test_a_redirect_to_an_internal_address_is_refused_before_anything_connects(steer):
    with Pages() as srv:
        port = srv.port
        steer.to(port)
        srv.redirect("/metadata", "http://169.254.169.254/latest/meta-data/")
        srv.redirect("/loopback", f"http://localhost:{port}/secret")
        srv.redirect("/decimal", f"http://2130706433:{port}/secret")
        srv.redirect("/mapped", f"http://[::ffff:127.0.0.1]:{port}/secret")
        srv.redirect("/intranet", f"http://intranet.test:{port}/secret")
        srv.html("/secret", "<html>secret</html>")
        resolver = names(public_test=[PUBLIC], intranet_test=["10.1.2.3"])
        for path in ("/metadata", "/loopback", "/decimal", "/mapped", "/intranet"):
            steer.meant.clear()
            with pytest.raises(Refused, match="private or internal"):
                netguard.get(f"http://public.test:{port}{path}", resolver=resolver)
            assert steer.meant == [(PUBLIC, port)]                           # the first hop only
        assert "/secret" not in srv.hits
        # the same redirect is followed when the person said so
        assert netguard.get(f"http://public.test:{port}/loopback", resolver=resolver, allow_private=True).body == b"<html>secret</html>"


# ----------------------------------------------------------------------------- no proxies
def test_proxy_settings_are_not_used(monkeypatch, raw):
    canary = raw(serving(http_bytes(b"from the proxy", "Content-Length: 14")))
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, f"http://127.0.0.1:{canary.port}")
    with Pages() as srv:
        srv.html("/ok", "<html>direct</html>")
        assert netguard.get(f"{srv.base}/ok", allow_private=True).body == b"<html>direct</html>"
        assert srv.hits == ["/ok"] and canary.requests == []


def test_a_failure_behind_a_proxy_says_why(monkeypatch):
    monkeypatch.setattr("urllib.request.getproxies", lambda: {"no": "*"})      # (whatever this computer is set up with)
    with pytest.raises(FetchError) as plain:
        netguard.get("http://127.0.0.1:9/", allow_private=True)
    assert "proxy" not in str(plain.value)
    monkeypatch.setattr("urllib.request.getproxies", lambda: {"https": "http://127.0.0.1:3128", "no": "*"})
    with pytest.raises(FetchError, match="set up to use a proxy, and Connecto connects to web pages directly") as behind:
        netguard.get("http://127.0.0.1:9/", allow_private=True)
    assert "ConnectionRefusedError" in str(behind.value)


# ----------------------------------------------------------------------------- what comes back
def test_the_body_is_capped_whether_or_not_the_server_says_how_big_it_is(raw):
    endless = raw(endless_body(limit=60_000_000))
    with pytest.raises(FetchError, match="page is larger than 200000 bytes"):
        within(10, netguard.get, f"http://127.0.0.1:{endless.port}/", allow_private=True, max_bytes=200_000)
    assert endless.bytes_sent < 20_000_000                              # it was hung up on long before the end
    lying = raw(serving(http_bytes(b"x" * 100, "Content-Length: 50000000")))
    with pytest.raises(FetchError, match="larger than 1 MB"):
        netguard.get(f"http://127.0.0.1:{lying.port}/", allow_private=True, max_bytes=1024 * 1024)


def test_a_page_of_exactly_the_limit_is_fine_and_one_byte_more_is_not(raw):
    limit = 5000

    def get(server, **kwargs):
        return netguard.get(f"http://127.0.0.1:{server.port}/", allow_private=True, max_bytes=limit, **kwargs)

    def chunked(body: bytes) -> bytes:
        return f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n"

    for size, ok in ((limit, True), (limit + 1, False)):
        body = b"x" * size
        gz = gzip.compress(body)
        for label, data in (
            ("declared", http_bytes(body, f"Content-Length: {size}")),
            ("undeclared", http_bytes(body)),
            ("chunked", http_bytes(chunked(body), "Transfer-Encoding: chunked")),
            ("compressed", http_bytes(gz, f"Content-Length: {len(gz)}", "Content-Encoding: gzip")),        # (size counts once decoded)
        ):
            server = raw(serving(data))
            if ok:
                assert get(server).body == body, label
            else:
                with pytest.raises(FetchError, match="page is larger than 5000 bytes"):
                    get(server)
    # what *arrives* has the same limit when it is compressed (stored blocks do not shrink, so this one is a little larger
    # than what it holds; no Content-Length, so that only the count of arriving bytes can object)
    def stored(n: int) -> bytes:
        packer = zlib.compressobj(0, zlib.DEFLATED, 31)
        return packer.compress(b"y" * n) + packer.flush()

    room = limit - (len(stored(100)) - 100)
    assert len(stored(room)) == limit and len(stored(room + 1)) == limit + 1
    assert get(raw(serving(http_bytes(stored(room), "Content-Encoding: gzip")))).body == b"y" * room
    with pytest.raises(FetchError, match="page is larger than 5000 bytes"):
        get(raw(serving(http_bytes(stored(room + 1), "Content-Encoding: gzip"))))


def test_inflating_gives_back_at_most_one_byte_more_than_there_is_room_for():
    bomb = zlib.compress(b"\0" * 50_000_000, 9)                          # 50 MB of zeros in about 50 KB
    decoder = zlib.decompressobj(47)
    out = netguard._inflate(decoder, bomb, 1000)
    assert len(out) == 1001                                              # "too much" - and not a byte more was produced
    small = zlib.decompressobj(47)
    assert netguard._inflate(small, zlib.compress(b"hello"), 1000) == b"hello"
    exact = zlib.decompressobj(47)
    assert netguard._inflate(exact, zlib.compress(b"hello"), 5) == b"hello"
    over = zlib.decompressobj(47)
    assert netguard._inflate(over, zlib.compress(b"hello!"), 5) == b"hello!"


def test_gzip_is_understood_and_a_bomb_is_stopped_at_the_cap(raw):
    page = b"<html>" + b"hello " * 2000 + b"</html>"
    packed = gzip.compress(page)
    ok = raw(serving(http_bytes(packed, f"Content-Length: {len(packed)}", "Content-Encoding: gzip", "Content-Type: text/html")))
    reply = netguard.get(f"http://127.0.0.1:{ok.port}/", allow_private=True)
    assert reply.body == page and reply.headers["content-encoding"] == "gzip"
    with pytest.raises(FetchError, match="page is larger than"):
        netguard.get(f"http://127.0.0.1:{ok.port}/", allow_private=True, max_bytes=len(page) - 1)       # decoded size counts
    deflated = zlib.compress(page)
    zl = raw(serving(http_bytes(deflated, f"Content-Length: {len(deflated)}", "Content-Encoding: deflate")))
    assert netguard.get(f"http://127.0.0.1:{zl.port}/", allow_private=True).body == page
    old_name = raw(serving(http_bytes(packed, f"Content-Length: {len(packed)}", "Content-Encoding: X-GZIP")))
    assert netguard.get(f"http://127.0.0.1:{old_name.port}/", allow_private=True).body == page            # (an older name for gzip)
    plain = raw(serving(http_bytes(page, f"Content-Length: {len(page)}", "Content-Encoding: identity")))
    assert netguard.get(f"http://127.0.0.1:{plain.port}/", allow_private=True).body == page
    packer = zlib.compressobj(9, zlib.DEFLATED, 31)
    bomb = packer.compress(b" " * 200_000_000) + packer.flush()           # about 200 KB that inflates to 200 MB
    bombing = raw(serving(http_bytes(bomb, f"Content-Length: {len(bomb)}", "Content-Encoding: gzip")))
    started = time.monotonic()
    with pytest.raises(FetchError, match="page is larger than 1 MB"):
        within(20, netguard.get, f"http://127.0.0.1:{bombing.port}/", allow_private=True, max_bytes=1024 * 1024)
    assert time.monotonic() - started < 10


def test_endless_data_after_the_end_of_a_compressed_page_is_not_kept(raw):
    """A zlib stream remembers whatever follows its end; the compressed bytes are capped as well as the decoded ones."""
    def handle(conn, request, srv):
        srv.send(conn, b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nConnection: close\r\n\r\n" + gzip.compress(b"<html>x</html>"))
        for _ in range(900):
            srv.send(conn, b"\0" * 65_536)

    server = raw(handle)
    started = time.monotonic()
    with pytest.raises(FetchError, match="page is larger than 1000000 bytes"):
        within(20, netguard.get, f"http://127.0.0.1:{server.port}/", allow_private=True, max_bytes=1_000_000)
    assert time.monotonic() - started < 10 and server.bytes_sent < 20_000_000


def test_compression_that_cannot_be_read_or_is_cut_short_is_an_error(raw):
    br = raw(serving(http_bytes(b"\x0b\x02\x80abc", "Content-Length: 6", "Content-Encoding: br")))
    with pytest.raises(FetchError, match="br-compressed data, which Connecto cannot read"):
        netguard.get(f"http://127.0.0.1:{br.port}/", allow_private=True)
    both = raw(serving(http_bytes(b"x", "Content-Length: 1", "Content-Encoding: gzip, br")))
    with pytest.raises(FetchError, match="cannot read"):
        netguard.get(f"http://127.0.0.1:{both.port}/", allow_private=True)
    junk = raw(serving(http_bytes(b"this is not gzip", "Content-Length: 16", "Content-Encoding: gzip")))
    with pytest.raises(FetchError, match="could not be decompressed"):
        netguard.get(f"http://127.0.0.1:{junk.port}/", allow_private=True)
    packed = gzip.compress(b"<html>" + b"x" * 5000 + b"</html>")
    cut = packed[: len(packed) // 2]
    short = raw(serving(http_bytes(cut, f"Content-Length: {len(cut)}", "Content-Encoding: gzip")))
    with pytest.raises(FetchError, match="download broke off"):
        netguard.get(f"http://127.0.0.1:{short.port}/", allow_private=True)


def test_a_download_that_ends_early_is_not_a_page(raw):
    early = raw(serving(http_bytes(b"x" * 100, "Content-Length: 500")))
    with pytest.raises(FetchError, match="download broke off"):
        netguard.get(f"http://127.0.0.1:{early.port}/", allow_private=True)
    chunked = b"5\r\nhello\r\n" + b"5\r\nworld\r\n" + b"0\r\n\r\n"
    fine = raw(serving(http_bytes(chunked, "Transfer-Encoding: chunked")))
    assert netguard.get(f"http://127.0.0.1:{fine.port}/", allow_private=True).body == b"helloworld"
    torn = raw(serving(http_bytes(b"5\r\nhello\r\n5\r\nwor", "Transfer-Encoding: chunked")))
    with pytest.raises(FetchError, match="download broke off"):
        netguard.get(f"http://127.0.0.1:{torn.port}/", allow_private=True)
    nolength = raw(serving(http_bytes(b"read until the server hangs up")))
    assert netguard.get(f"http://127.0.0.1:{nolength.port}/", allow_private=True).body == b"read until the server hangs up"


def test_garbage_where_a_web_server_should_be_is_a_failed_load_not_a_crash(raw):
    for data in (b"\x00\x01\x02garbage\r\n\r\n", b"SSH-2.0-OpenSSH_9.0\r\n", b"HTTP/1.1 abc\r\n\r\n", b"", b"HTTP/1.1 200 OK\r\n" + b"X: y\r\n" * 500):
        server = raw(serving(data))
        with pytest.raises(FetchError, match="could not load"):
            netguard.get(f"http://127.0.0.1:{server.port}/", allow_private=True)


# ----------------------------------------------------------------------------- time
def test_a_server_that_trickles_its_headers_is_cut_off_when_the_time_is_up(raw):
    slow = raw(drip_headers(gap=0.05, seconds=20))
    started = time.monotonic()
    with pytest.raises(FetchError, match=r"page took more than 0\.7 seconds to download"):
        within(10, netguard.get, f"http://127.0.0.1:{slow.port}/", allow_private=True, total_seconds=0.7)
    assert time.monotonic() - started < 4


def test_a_server_that_trickles_its_body_is_cut_off_when_the_time_is_up(raw):
    slow = raw(drip_body(total=1000, gap=0.05))
    started = time.monotonic()
    with pytest.raises(FetchError, match=r"the company list took more than 0\.7 seconds to download"):
        within(10, netguard.get, f"http://127.0.0.1:{slow.port}/", allow_private=True, total_seconds=0.7, what="the company list")
    assert time.monotonic() - started < 4


def test_a_server_that_says_nothing_is_given_up_on_after_the_read_timeout(raw):
    def silent(conn, request, srv):
        time.sleep(5)

    quiet = raw(silent)
    started = time.monotonic()
    with pytest.raises(FetchError, match=r"could not load .* \(TimeoutError\)"):
        within(10, netguard.get, f"http://127.0.0.1:{quiet.port}/", allow_private=True, timeout=0.4, total_seconds=30)
    assert time.monotonic() - started < 4


def test_the_time_limit_covers_the_redirects_too(raw):
    def slow_redirects(conn, request, srv):
        time.sleep(0.35)
        srv.send(conn, http_bytes(b"", f"Location: http://127.0.0.1:{srv.port}/again", "Content-Length: 0", status="302 Found"))

    server = raw(slow_redirects)
    started = time.monotonic()
    with pytest.raises(FetchError, match=r"took more than 0\.6 seconds"):
        within(10, netguard.get, f"http://127.0.0.1:{server.port}/", allow_private=True, total_seconds=0.6)
    assert time.monotonic() - started < 4


def test_the_addresses_are_tried_one_after_another_until_the_time_is_up(monkeypatch):
    attempts = []
    clock = SimpleNamespace(now=0.0)
    # Check the timeout arithmetic without depending on how promptly a busy runner wakes from sleep.
    monkeypatch.setattr(netguard, "time", SimpleNamespace(monotonic=lambda: clock.now))

    def blackhole(ip, port, timeout):
        attempts.append((ip, round(timeout, 1)))
        clock.now += timeout
        raise TimeoutError("timed out")

    monkeypatch.setattr(netguard, "_connect", blackhole)
    resolver = lambda host, port=None: [f"93.184.216.{n}" for n in range(1, 6)]
    with pytest.raises(FetchError):
        netguard.get("http://a.test/", resolver=resolver, timeout=0.3, total_seconds=0.5)
    assert clock.now == pytest.approx(0.5)
    assert attempts == [("93.184.216.1", 0.3), ("93.184.216.2", 0.2)]       # the second one gets only what is left
    attempts.clear()
    with pytest.raises(FetchError):
        netguard.get("http://a.test/", resolver=resolver, total_seconds=0)         # no time at all: no connection is tried
    assert attempts == []


def test_a_forgotten_watchdog_does_not_keep_the_program_from_ending():
    code = "from connecto import netguard; netguard._Watchdog(600)"
    started = time.monotonic()
    subprocess.run([sys.executable, "-c", code], check=True, timeout=60)
    assert time.monotonic() - started < 30


def test_the_watchdog_hangs_up_on_a_socket_it_learns_about_too_late():
    dog = netguard._Watchdog(600)
    dog.stop()
    dog._fire()                                        # expire deterministically before the socket exists
    a, b = socket.socketpair()
    ended = []

    def read():
        try:
            ended.append(a.recv(10) == b"")
        except OSError:
            ended.append(True)                          # Windows reports a read on a shut-down socket as an error

    # A timeout-mode socket waits in select() on Windows even after receive shutdown. Use a blocking reader,
    # with a bounded join and peer cleanup so a broken watchdog still fails without leaving a stuck thread.
    reader = threading.Thread(target=read, daemon=True)
    try:
        dog.watch(a)
        reader.start()
        reader.join(2)
        assert ended == [True]
    finally:
        b.close()
        a.close()
        if reader.ident is not None:
            reader.join(5)
        dog.stop()


def test_once_connected_a_socket_waits_the_read_timeout_and_leaves_the_time_limit_to_the_watchdog():
    with socket.create_server(("127.0.0.1", 0)) as listener:
        sock = netguard._open(["127.0.0.1"], listener.getsockname()[1], timeout=5, deadline=time.monotonic() + 0.3)
        try:
            assert sock.gettimeout() == 5               # (cut to the 0.3 s left, it would end together with the watchdog)
        finally:
            sock.close()


def test_a_failure_when_the_time_is_up_is_explained_as_the_time_limit_even_before_the_timer_fires():
    dog = netguard._Watchdog(0.2)
    dog.stop()                                          # (the timer never fires: only the clock says the time is up)
    time.sleep(0.3)
    assert not dog.expired
    error = netguard._broken(TimeoutError(), dog, "https://a.test/", "page", reading=False)
    assert str(error) == "page took more than 0.2 seconds to download"


def test_a_finished_download_leaves_no_timer_behind():
    with Pages() as srv:
        srv.html("/ok", "<html>fine</html>")
        for _ in range(20):
            netguard.get(f"{srv.base}/ok", allow_private=True, total_seconds=600)
        time.sleep(0.2)
        assert not [t for t in threading.enumerate() if isinstance(t, threading.Timer)]


def test_no_connection_is_left_open_whatever_happened(monkeypatch, raw):
    opened: list[socket.socket] = []
    real = netguard._connect

    def spy(ip, port, timeout):
        sock = real(ip, port, timeout)
        opened.append(sock)
        return sock

    monkeypatch.setattr(netguard, "_connect", spy)

    def refusing(headers):
        raise FetchError("not what we wanted")

    short = raw(serving(http_bytes(b"x" * 10, "Content-Length: 500")))
    with Pages() as srv:
        srv.html("/ok", "<html>fine</html>")
        srv.html("/big", "x" * 5000)
        srv.html("/gone", "gone", status=404)
        srv.redirect("/a", "/b")
        srv.redirect("/b", "/c")
        srv.redirect("/c", "/ok")
        netguard.get(f"{srv.base}/ok", allow_private=True)
        netguard.get(f"{srv.base}/gone", allow_private=True)
        netguard.get(f"{srv.base}/a", allow_private=True)
        for call in (lambda: netguard.get(f"{srv.base}/a", allow_private=True, max_redirects=1),
                     lambda: netguard.get(f"{srv.base}/big", allow_private=True, max_bytes=100),
                     lambda: netguard.get(f"{srv.base}/ok", allow_private=True, check=refusing),
                     lambda: netguard.get(f"http://127.0.0.1:{short.port}/", allow_private=True)):
            with pytest.raises(FetchError):
                call()
    assert len(opened) >= 10
    assert [sock for sock in opened if sock.fileno() != -1] == []


def test_fetch_html_hands_its_limits_on_to_the_downloader(raw):
    from connecto.sources import webpage

    def silent(conn, request, srv):
        time.sleep(5)

    quiet = raw(silent)
    started = time.monotonic()
    with pytest.raises(FetchError, match="could not load"):
        within(10, webpage.fetch_html, f"http://127.0.0.1:{quiet.port}/", allow_private=True, timeout=0.4)
    assert time.monotonic() - started < 4
    page = raw(serving(http_bytes(b"<html>" + b"x" * 3000 + b"</html>", "Content-Type: text/html")))
    with pytest.raises(FetchError, match="page is larger than 1000 bytes"):
        webpage.fetch_html(f"http://127.0.0.1:{page.port}/", allow_private=True, max_bytes=1000)
    with pytest.raises(Refused, match="private or internal"):
        webpage.fetch_html(f"http://127.0.0.1:{page.port}/")
    with pytest.raises(Refused, match=r"fine\.test points to a private or internal address \(10\.1\.2\.3\)"):       # (this resolver is asked)
        webpage.fetch_html("http://fine.test/", resolver=names(fine_test=["10.1.2.3"]))


# ----------------------------------------------------------------------------- several at once
def test_many_downloads_at_once_do_not_get_in_each_others_way(steer):
    with Pages() as srv:
        for n in range(8):
            srv.html(f"/p{n}", f"<html>page {n}</html>")
        steer.to(srv.port)
        results: dict[int, bytes] = {}

        def work(n):
            for _ in range(5):
                results[n] = netguard.get(f"http://t{n}.test:{srv.port}/p{n}", resolver=lambda h, p=None: [PUBLIC]).body

        threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert results == {n: f"<html>page {n}</html>".encode() for n in range(8)}
        assert len(srv.hits) == 40


# ----------------------------------------------------------------------------- https
@pytest.fixture(scope="module")
def certs(tmp_path_factory):
    """A throw-away CA and two server certificates (for ``public.test`` and ``other.test``), made with the openssl program."""
    openssl = shutil.which("openssl")
    if not openssl:
        pytest.skip("the openssl program is not installed")
    folder = tmp_path_factory.mktemp("tls")

    def run(*args):
        subprocess.run([openssl, *args], cwd=folder, check=True, capture_output=True, timeout=60)

    try:
        (folder / "ca.cnf").write_text("[req]\ndistinguished_name=dn\nprompt=no\nx509_extensions=v3_ca\n[dn]\nCN=Connecto test CA\n"
                                       "[v3_ca]\nbasicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\n"
                                       "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always\n")
        # Apple's LibreSSL defaults to explicit EC parameters and SHA-1; current Python/OpenSSL refuses them.
        curve = ["-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                 "-pkeyopt", "ec_param_enc:named_curve", "-sha256"]
        run("req", "-x509", *curve, "-nodes", "-keyout", "ca.key", "-out", "ca.pem", "-days", "2", "-config", "ca.cnf")
        for name in ("public.test", "other.test"):
            (folder / f"{name}.cnf").write_text(f"subjectAltName=DNS:{name}\nbasicConstraints=CA:FALSE\nkeyUsage=critical,digitalSignature\n"
                                                "extendedKeyUsage=serverAuth\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid\n")
            run("req", "-new", *curve, "-nodes", "-keyout", f"{name}.key", "-out", f"{name}.csr", "-subj", f"/CN={name}")
            run("x509", "-req", "-sha256", "-in", f"{name}.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial", "-out", f"{name}.pem",
                "-days", "2", "-extfile", f"{name}.cnf")
    except (subprocess.SubprocessError, OSError) as exc:
        pytest.skip(f"could not make test certificates: {exc}")
    return SimpleNamespace(ca=str(folder / "ca.pem"), pair=lambda name: (str(folder / f"{name}.pem"), str(folder / f"{name}.key")))


class TlsServer:
    """HTTPS on this machine; remembers the server name each client asked for (the SNI)."""

    def __init__(self, certfile: str, keyfile: str, body: bytes = b"<html>secure</html>", pace: float = 0.0):
        self.pace = pace                                                # seconds between the bytes of the answer (0: all at once)
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(certfile, keyfile)
        self.context.sni_callback = lambda sock, name, ctx: self.server_names.append(name)
        self.server_names: list[str | None] = []
        self.requests: list[bytes] = []
        self.body = body
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.listener.settimeout(0.05)
        self.port = self.listener.getsockname()[1]
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stopped.set()
        self.thread.join(5)
        self.listener.close()

    def _serve(self):
        while not self.stopped.is_set():
            try:
                conn, _ = self.listener.accept()
            except (socket.timeout, OSError):
                continue
            try:
                conn.settimeout(5)
                with self.context.wrap_socket(conn, server_side=True) as tls:
                    data = b""
                    while b"\r\n\r\n" not in data:
                        chunk = tls.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                    self.requests.append(data)
                    answer = http_bytes(self.body, f"Content-Length: {len(self.body)}", "Content-Type: text/html")
                    if not self.pace:
                        tls.sendall(answer)
                    else:
                        tls.sendall(answer[:-len(self.body)])                # (the headers, then the page a byte at a time)
                        for i in range(len(self.body)):
                            time.sleep(self.pace)
                            tls.sendall(self.body[i:i + 1])
            except (OSError, ssl.SSLError):
                pass                                                    # (a client that did not like the certificate)
            finally:
                conn.close()


def trusting(certs, monkeypatch):
    context = ssl.create_default_context(cafile=certs.ca)
    monkeypatch.setattr(netguard, "_tls_context", lambda: context)


def test_https_checks_the_certificate_against_the_name_not_the_address(certs, steer, monkeypatch):
    trusting(certs, monkeypatch)
    with TlsServer(*certs.pair("public.test")) as server:
        steer.to(server.port)
        reply = netguard.get(f"https://public.test:{server.port}/team", resolver=lambda h, p=None: [PUBLIC])
        assert reply.status == 200 and reply.body == b"<html>secure</html>"
        assert steer.meant == [(PUBLIC, server.port)]                    # connected to the checked address ...
        assert server.server_names == ["public.test"]                    # ... but asked for the name (and it was verified against it)
        assert b"Host: public.test:" in server.requests[0]


def test_https_refuses_a_certificate_for_another_name(certs, steer, monkeypatch):
    trusting(certs, monkeypatch)
    with TlsServer(*certs.pair("other.test")) as server:
        steer.to(server.port)
        with pytest.raises(FetchError, match=r"could not load https://public.test:\d+/ \(SSLCertVerificationError\)"):
            netguard.get(f"https://public.test:{server.port}/", resolver=lambda h, p=None: [PUBLIC])


def test_https_refuses_a_certificate_nobody_vouches_for(certs, steer, monkeypatch):
    monkeypatch.setattr(netguard, "_tls_context", lambda: ssl.create_default_context())       # (does not know the test CA)
    with TlsServer(*certs.pair("public.test")) as server:
        steer.to(server.port)
        with pytest.raises(FetchError, match=r"\(SSLCertVerificationError\)"):
            netguard.get(f"https://public.test:{server.port}/", resolver=lambda h, p=None: [PUBLIC])


def test_https_ends_when_the_time_is_up_even_while_the_page_trickles(certs, steer, monkeypatch):
    trusting(certs, monkeypatch)
    with TlsServer(*certs.pair("public.test"), body=b"x" * 2000, pace=0.05) as server:        # (the handshake itself is quick)
        steer.to(server.port)
        started = time.monotonic()
        with pytest.raises(FetchError, match=r"page took more than 0\.7 seconds to download"):
            within(10, netguard.get, f"https://public.test:{server.port}/", resolver=lambda h, p=None: [PUBLIC], total_seconds=0.7)
        assert time.monotonic() - started < 4


def test_https_ends_when_the_time_is_up_even_while_the_handshake_trickles(steer, raw):
    def trickle(conn, request, srv):                                    # (the start of a TLS record, then a byte now and then)
        srv.send(conn, b"\x16\x03\x03\x04\x00")
        for _ in range(400):
            time.sleep(0.05)
            srv.send(conn, b"\x00")

    class NotHttp(RawServer):
        @staticmethod
        def _read_request(conn):                                        # a TLS client's hello is not an HTTP request
            return SimpleNamespace(line="", headers={}, body=b"", path="")

    slow = NotHttp(trickle).start()
    try:
        steer.to(slow.port)
        started = time.monotonic()
        with pytest.raises(FetchError, match=r"took more than 0\.7 seconds"):
            within(10, netguard.get, "https://public.test/", resolver=lambda h, p=None: [PUBLIC], total_seconds=0.7)
        assert time.monotonic() - started < 4
    finally:
        slow.stop()


def test_the_default_tls_context_verifies_certificates_and_names():
    netguard._tls_context.cache_clear()
    context = netguard._tls_context()
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname is True
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert netguard._tls_context() is context                          # (made once)
