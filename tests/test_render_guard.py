"""The route guard of the rendering browser (``render: true``), driven by stand-ins for what Playwright hands a handler.

What Chromium itself does with a redirect - follow it without asking again - is tested against a real browser in
``test_sources.py`` (``browser`` marker); here every decision the guard makes is checked on its own.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from connecto.netguard import resolve
from connecto.sources import webpage as web_mod

PUBLIC = "93.184.216.34"


class Response:
    def __init__(self, status: int = 200, headers: dict | None = None):
        self.status = status
        self.headers = headers or {}


class Route:
    """What Playwright hands a route handler, as far as the guard uses it."""

    def __init__(self, url: str, *, method: str = "GET", resource_type: str = "script", first=None, headers: dict | None = None):
        self.request = SimpleNamespace(url=url, method=method, resource_type=resource_type,
                                       headers=headers if headers is not None else {"accept": "*/*", "host": "ok.test", "authorization": "secret",
                                                                                   "content-length": "0"})
        self.first = first if first is not None else Response()
        self.calls: list[tuple] = []

    def fetch(self, **kwargs):
        self.calls.append(("fetch", kwargs))
        if isinstance(self.first, Exception):
            raise self.first
        return self.first

    def fulfill(self, **kwargs):
        self.calls.append(("fulfill", kwargs))

    def abort(self):
        self.calls.append(("abort",))

    def continue_(self):
        self.calls.append(("continue",))

    @property
    def kinds(self) -> list[str]:
        return [call[0] for call in self.calls]


class Context:
    """The browser context: ``context.request.get`` makes the requests for the later hops of a redirect."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requested: list[tuple[str, dict]] = []
        self.request = SimpleNamespace(get=self._get)

    def _get(self, url, **kwargs):
        self.requested.append((url, kwargs))
        answer = self.responses.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def resolver_for(**table):
    table = {k.replace("_", "."): v for k, v in table.items()}
    asked: list[str] = []

    def resolver(host, port=None):
        asked.append(host)
        answer = table[host] if host in table else resolve(host, port)
        if isinstance(answer, Exception):
            raise answer
        return answer

    resolver.asked = asked
    return resolver


RESOLVER = resolver_for(ok_test=[PUBLIC], cdn_test=["93.184.216.35"], internal_test=["10.0.0.9"], nowhere_test=OSError("no such host"))


def guard(allow_private=False, context=None, resolver=RESOLVER):
    return web_mod.make_route_guard(allow_private, resolver=resolver, context=context)


# ----------------------------------------------------------------------------- what is let through, and how
def test_an_allowed_request_is_made_by_the_guard_itself_and_never_handed_to_the_browser():
    route = Route("https://ok.test/app.js")
    guard()(route)
    assert route.kinds == ["fetch", "fulfill"]
    assert route.calls[0][1] == {"max_redirects": 0}                    # (never the client's own redirect following)
    assert route.calls[1][1] == {"response": route.first}


def test_requests_to_internal_hosts_and_other_schemes_are_refused_without_being_made():
    handler = guard()
    for url in ("https://internal.test/admin", "http://169.254.169.254/latest/meta-data/", "http://127.0.0.1:8765/api/status",
                "http://localhost/", "http://[::1]/", "http://2130706433/", "http://[::ffff:10.0.0.1]/", "ftp://ok.test/file",
                "file:///etc/passwd", "ws://ok.test/socket", "wss://ok.test/socket", "chrome://settings", "javascript:alert(1)",
                "http://user:pw@ok.test/", "https://nowhere.test/"):
        route = Route(url)
        handler(route)
        assert route.kinds == ["abort"], url


def test_data_blob_and_about_urls_are_the_browsers_own_business():
    handler = guard()
    for url in ("data:text/plain,hi", "blob:https://ok.test/1234", "about:blank"):
        route = Route(url)
        handler(route)
        assert route.kinds == ["continue"], url


def test_images_fonts_media_and_event_streams_are_not_fetched_at_all():
    handler = guard()
    for kind in ("image", "font", "media", "texttrack", "eventsource"):
        route = Route("https://ok.test/x", resource_type=kind)
        handler(route)
        assert route.kinds == ["abort"], kind
    for kind in ("document", "script", "xhr", "fetch", "stylesheet", "other"):
        route = Route("https://ok.test/x", resource_type=kind)
        handler(route)
        assert route.kinds == ["fetch", "fulfill"], kind


def test_a_host_is_looked_up_once_however_many_requests_go_to_it():
    resolver = resolver_for(ok_test=[PUBLIC], internal_test=["10.0.0.9"])
    handler = guard(resolver=resolver)
    for _ in range(5):
        handler(Route("https://ok.test/a.js"))
        handler(Route("https://internal.test/a.js"))
    assert resolver.asked == ["ok.test", "internal.test"]


def test_with_allow_private_everything_web_goes_through_but_other_schemes_do_not():
    handler = guard(allow_private=True)
    for url in ("http://127.0.0.1:8000/x", "https://internal.test/admin", "http://localhost/"):
        route = Route(url)
        handler(route)
        assert route.kinds == ["continue"], url
    for url in ("file:///etc/passwd", "ftp://ok.test/", "ws://127.0.0.1/"):
        route = Route(url)
        handler(route)
        assert route.kinds == ["abort"], url
    route = Route("http://127.0.0.1/pixel.png", resource_type="image")
    handler(route)
    assert route.kinds == ["abort"]


def test_a_request_that_fails_is_refused_and_nothing_escapes_the_handler():
    for failure in (RuntimeError("driver gone"), TimeoutError("slow")):
        route = Route("https://ok.test/x", first=failure)
        guard()(route)
        assert route.kinds == ["fetch", "abort"]

    class Gone(Route):
        def abort(self):
            raise RuntimeError("the page is closed")

    guard()(Gone("https://internal.test/"))                              # (aborting a request for a page that is gone)


# ----------------------------------------------------------------------------- redirects, one checked hop at a time
def test_a_redirect_is_followed_by_the_guard_after_checking_where_it_leads():
    final = Response(200)
    context = Context(final)
    route = Route("https://ok.test/old", first=Response(302, {"location": "https://cdn.test/new?x=1"}))
    guard(context=context)(route)
    assert route.kinds == ["fetch", "fulfill"] and route.calls[1][1] == {"response": final}     # the browser gets the end of the chain
    [(url, kwargs)] = context.requested
    assert url == "https://cdn.test/new?x=1" and kwargs["max_redirects"] == 0
    assert kwargs["headers"] == {"accept": "*/*"}                       # not the host, and not a credential the page set


def test_a_relative_location_is_resolved_against_the_current_address():
    context = Context(Response(302, {"location": "/c"}), Response(200))
    route = Route("https://ok.test/dir/a", first=Response(301, {"location": "b"}))
    guard(context=context)(route)
    assert [u for u, _ in context.requested] == ["https://ok.test/dir/b", "https://ok.test/c"]
    assert route.kinds == ["fetch", "fulfill"]
    context = Context(Response(302, {"location": "d"}), Response(200))              # (the second hop is relative to the first, not to the request)
    route = Route("https://ok.test/dir/a", first=Response(301, {"location": "/other/b"}))
    guard(context=context)(route)
    assert [u for u, _ in context.requested] == ["https://ok.test/other/b", "https://ok.test/other/d"]


@pytest.mark.parametrize("target", [
    "http://169.254.169.254/latest/meta-data/", "http://127.0.0.1:8765/api/run", "http://localhost:8765/", "http://[::1]/",
    "https://internal.test/admin", "http://2130706433/", "http://[::ffff:127.0.0.1]/", "http://0x7f.1/",
    "ftp://ok.test/x", "file:///etc/passwd", "javascript:alert(1)", "http://user:pw@ok.test/", "//internal.test/x",
])
def test_a_redirect_to_an_internal_address_is_refused_and_never_requested(target):
    context = Context(Response(200))
    route = Route("https://ok.test/start", first=Response(302, {"location": target}))
    guard(context=context)(route)
    assert route.kinds == ["fetch", "abort"] and context.requested == []


def test_a_redirect_from_a_later_hop_is_checked_as_well():
    context = Context(Response(302, {"location": "http://169.254.169.254/"}), Response(200))
    route = Route("https://ok.test/start", first=Response(302, {"location": "https://cdn.test/second"}))
    guard(context=context)(route)
    assert [u for u, _ in context.requested] == ["https://cdn.test/second"]                     # the third was never requested
    assert route.kinds == ["fetch", "abort"]


def test_at_most_three_redirects_are_followed():
    three = Context(Response(302, {"location": "/2"}), Response(302, {"location": "/3"}), Response(200))
    route = Route("https://ok.test/1", first=Response(302, {"location": "/1b"}))
    guard(context=three)(route)
    assert route.kinds == ["fetch", "fulfill"] and len(three.requested) == 3
    four = Context(Response(302, {"location": "/2"}), Response(302, {"location": "/3"}), Response(302, {"location": "/4"}), Response(200))
    route = Route("https://ok.test/1", first=Response(302, {"location": "/1b"}))
    guard(context=four)(route)
    assert route.kinds == ["fetch", "abort"] and len(four.requested) == 3


def test_a_redirect_without_a_place_to_go_or_for_a_request_with_a_body_is_refused():
    nowhere = Context(Response(200))
    route = Route("https://ok.test/x", first=Response(302, {}))
    guard(context=nowhere)(route)
    assert route.kinds == ["fetch", "abort"] and nowhere.requested == []
    context = Context(Response(200))
    route = Route("https://ok.test/x", method="POST", first=Response(307, {"location": "https://cdn.test/y"}))
    guard(context=context)(route)
    assert route.kinds == ["fetch", "abort"] and context.requested == []


def test_a_failure_on_a_later_hop_refuses_the_request():
    route = Route("https://ok.test/x", first=Response(302, {"location": "https://cdn.test/y"}))
    guard(context=Context(RuntimeError("boom")))(route)
    assert route.kinds == ["fetch", "abort"]
    route = Route("https://ok.test/x", first=Response(302, {"location": "https://cdn.test/y"}))
    guard(context=None)(route)                                          # (no context to make later hops with)
    assert route.kinds == ["fetch", "abort"]


def test_every_redirect_status_counts():
    for status in (301, 302, 303, 307, 308):
        route = Route("https://ok.test/x", first=Response(status, {"location": "http://127.0.0.1:1/"}))
        guard(context=Context())(route)
        assert route.kinds == ["fetch", "abort"], status
    for status in (200, 204, 300, 304, 404, 500):                       # not redirects: handed on as they are
        route = Route("https://ok.test/x", first=Response(status, {"location": "http://127.0.0.1:1/"}))
        guard(context=Context())(route)
        assert route.kinds == ["fetch", "fulfill"], status


# ----------------------------------------------------------------------------- web sockets
def test_a_web_socket_is_never_connected_to_its_server():
    def fail(*args, **kwargs):
        pytest.fail("the page's WebSocket was connected to a server")

    web_mod.refuse_web_socket(SimpleNamespace(connect_to_server=fail, connect=fail, url="ws://ok.test/"))


# ----------------------------------------------------------------------------- the page the browser ended up on
class Page:
    def __init__(self, url: str):
        self.url = url
        self.went_to: list[str] = []

    def goto(self, url, **kwargs):
        self.went_to.append(url)

    def wait_for_load_state(self, *args, **kwargs):
        pass

    def content(self) -> str:
        return "<html>page</html>"


def rendering(page: Page, allow_private: bool = False):
    shell = object.__new__(web_mod._Rendering)                          # (without starting a browser)
    shell.page, shell.allow_private = page, allow_private
    return shell


def test_the_address_the_browser_ended_up_on_is_checked_again(monkeypatch):
    from connecto import netguard

    monkeypatch.setattr(netguard, "resolve", lambda host, port=None: [PUBLIC] if host == "ok.test" else resolve(host, port))
    page = Page("https://ok.test/team")
    assert rendering(page).load("https://ok.test/start") == ("https://ok.test/team", "<html>page</html>")
    assert page.went_to == ["https://ok.test/start"]
    for internal in ("http://127.0.0.1:8765/", "http://169.254.169.254/", "http://[::1]/"):
        with pytest.raises(ValueError, match="private or internal"):
            rendering(Page(internal)).load("https://ok.test/start")
        assert rendering(Page(internal), allow_private=True).load("https://ok.test/start")[0] == internal
    with pytest.raises(ValueError, match="private or internal"):
        rendering(page).load("http://10.0.0.1/")                         # (and the address asked for is checked before the browser is sent)
    assert page.went_to == ["https://ok.test/start"]
