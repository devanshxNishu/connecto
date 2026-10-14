"""A tiny stand-in for LinkedIn's people search, so ``linkedin_search`` can be exercised end to end without LinkedIn.

It reproduces the structure observed on the real page in Oct 2026 (nothing here is matched by class name, because the
classes are random on every request): every result is wrapped by an ``<a href=".../in/<slug>?miniProfileUrn=...">`` whose
``innerText`` lines are name, a "• 2nd" degree line, headline, location and an optional "Current: ..." line, mixed with
accessibility noise ("View X's profile", "Status is online", "Premium", mutual connections). The same person has a second
(avatar) anchor with little text; Connect / Message buttons sit outside the anchor.

``/search/results/people/?keywords=..&page=N``: 3 pages of 4 cards (one "LinkedIn Member" card without a link, one
1st-degree person, one person repeated on a later page), then "No results found". ``/feed/`` is the logged-in landing page.
"""
from __future__ import annotations

import random
import string
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

MEMBER = "member"   # a card for someone outside your network: no profile link at all

# slug, name, degree, headline, location, current, noise lines inside the anchor
PEOPLE = {
    "asha-verma-1a2b3c": ("Asha Verma", "2nd", "Head of Design at Finlo | Ex-Razorpay", "Bengaluru, Karnataka, India",
                          "Head of Design at Finlo", ["Status is online", "Premium", "Ravi Kulkarni and 3 other mutual connections"]),
    "ravi-kulkarni-9z8y7x": ("Ravi Kulkarni", "3rd+", "Product Designer", "Pune, Maharashtra, India", None,
                             ["Status is reachable"]),
    "meera-iyer-4d5e6f": ("Meera Iyer", "2nd", "Design Lead @ PayNest", "Mumbai, Maharashtra, India",
                          "Design Lead at PayNest", ["Meera and Sana Sheikh are mutual connections"]),
    "karan-malhotra-7g8h9i": ("Karan Malhotra", "2nd", "Founder at Lumen Labs", "New Delhi, Delhi, India",
                              "Founder at Lumen Labs", []),
    "priya-nair-0j1k2l": ("Priya Nair", "1st", "UX Researcher at Orbit", "Kochi, Kerala, India", None, ["Status is online"]),
    "dev-patel-3m4n5o": ("Dev Patel", "3rd+", "Creative Technologist", "Ahmedabad, Gujarat, India", None, []),
    "sana-sheikh-6p7q8r": ("Sana Sheikh", "2nd", "Design Manager at Cedar", "Hyderabad, Telangana, India",
                           "Design Manager at Cedar", ["1 mutual connection"]),
    "tara-bose-9s8t7u": ("Tara Bose", "2nd", "Principal Designer at Quill", "Kolkata, West Bengal, India", None, []),
    "omar-farouk-5v4w3x": ("Omar Farouk", "3rd+", "Angel investor", "Dubai, United Arab Emirates", None, ["Premium"]),
    "lakshmi-rao-2y1z0a": ("Lakshmi Rao", "2nd", "Partner at Seed Fund", "Chennai, Tamil Nadu, India",
                           "Partner at Seed Fund", []),
}
PAGES = {
    1: ["asha-verma-1a2b3c", "ravi-kulkarni-9z8y7x", MEMBER, "meera-iyer-4d5e6f"],
    2: ["karan-malhotra-7g8h9i", "priya-nair-0j1k2l", "dev-patel-3m4n5o", "sana-sheikh-6p7q8r"],
    3: ["asha-verma-1a2b3c", "tara-bose-9s8t7u", "omar-farouk-5v4w3x", "lakshmi-rao-2y1z0a"],
}
# Distinct people with a profile link: 10 (9 once the 1st-degree Priya Nair is skipped).
DISTINCT_SLUGS = sorted({s for page in PAGES.values() for s in page if s != MEMBER})
FIRST_DEGREE_SLUGS = ["priya-nair-0j1k2l"]

CSS = ("*{box-sizing:border-box} body{font-family:sans-serif} main{max-width:760px;margin:auto} li{list-style:none;"
       "border-bottom:1px solid #ddd;padding:10px} .vh{position:absolute;width:1px;height:1px;overflow:hidden;"
       "clip:rect(0 0 0 0);white-space:nowrap}")

FEED = ("<!doctype html><html><head><title>(3) Feed | LinkedIn</title></head><body><nav aria-label='Primary'>Home</nav>"
        "<main><h1>Feed</h1><p>post</p></main></body></html>")
LOGIN = "<!doctype html><html><head><title>LinkedIn Login</title></head><body><main><h1>Sign in</h1></main></body></html>"
CHECKPOINT = ("<!doctype html><html><head><title>Security Verification | LinkedIn</title></head><body><main>"
              "<h1>Let's do a quick security check</h1><p>Please confirm it's you.</p></main></body></html>")
NOT_FOUND = "<!doctype html><html><head><title>Page not found</title></head><body><main>This page doesn't exist</main></body></html>"


def _cls(rng: random.Random) -> str:
    return rng.choice(string.ascii_lowercase) + "".join(rng.choice(string.ascii_lowercase + string.digits) for _ in range(6))


def _card(slug: str, rng: random.Random) -> str:
    c = lambda: _cls(rng)  # noqa: E731
    if slug == MEMBER:
        return (f'<li class="{c()}"><div class="{c()}"><div class="{c()}"><span aria-hidden="true">LinkedIn Member</span></div>'
                f'<div class="{c()}">Graphic Designer</div><div class="{c()}">India</div>'
                f'<button type="button" class="{c()}">Connect</button></div></li>')
    name, degree, headline, location, current, noise = PEOPLE[slug]
    href = f"https://www.linkedin.com/in/{slug}?miniProfileUrn=urn%3Ali%3Afs_miniProfile%3AACoAA{rng.randint(10**8, 10**9)}"
    first = name.split()[0]
    inner = [f'<div class="{c()}"><span aria-hidden="true">{name}</span></div>',
             f'<div class="{c()}"><span class="vh">View {first}’s profile</span></div>',
             f'<div class="{c()}">• {degree}</div>',
             f'<div class="{c()}">{headline}</div>',
             f'<div class="{c()}">{location}</div>']
    if current:
        inner.append(f'<div class="{c()}">Current: {current}</div>')
    inner += [f'<div class="vh {c()}">{line}</div>' for line in noise]
    action = "Message" if degree == "1st" else "Connect"
    return (f'<li class="reusable-search__result-container {c()}"><div class="{c()}">'
            f'<div class="{c()}"><a class="{c()}" href="{href}"><img alt="" width="48" height="48" '
            f'src="data:image/gif;base64,R0lGODlhAQABAAAAACw="><span class="vh">{name}</span></a></div>'
            f'<div class="{c()}"><a class="{c()}" href="{href}">{"".join(inner)}</a></div>'
            f'<div class="{c()}"><button type="button" class="{c()}">{action}</button>'
            f'<button type="button" class="{c()}">Message</button></div></div></li>')


def render_results(page: int) -> str:
    rng = random.Random()
    slugs = PAGES.get(page)
    if slugs:
        body = '<ul class="%s">%s</ul>' % (_cls(rng), "".join(_card(s, rng) for s in slugs))
    else:
        body = (f'<div class="{_cls(rng)}"><h2>No results found</h2>'
                '<p>Try shortening or rephrasing your search.</p></div>')
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>Search | LinkedIn</title><style>{CSS}</style></head><body>"
            f'<header><a href="https://www.linkedin.com/feed/">Home</a></header>'
            f'<main class="{_cls(rng)}"><h1>Search results</h1><div><button type="button">People</button>'
            f'<button type="button">Jobs</button></div>'
            f'<a href="https://www.linkedin.com/school/in/mit/">MIT</a>'          # decoy: "/in/" but not a profile
            f"{body}</main>"
            f'<aside><a href="https://www.linkedin.com/in/decoy-outside-main/">Decoy Person</a></aside></body></html>')


class MockSearch:
    """Threaded HTTP server. ``requests`` records every search request; flip ``logged_in`` or set ``checkpoint_page``
    (a page number that answers with a security check) at any time; ``logout_after=N`` signs out after N search pages."""

    def __init__(self, logged_in: bool = True, checkpoint_page: int | None = None, logout_after: int | None = None):
        self.logged_in = logged_in
        self.checkpoint_page = checkpoint_page
        self.logout_after = logout_after          # sign out (redirect to /login) after serving this many search pages
        self.requests: list[dict] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _send(self, code: int, body: str, ctype: str = "text/html; charset=utf-8", location: str = ""):
                data = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                if location:
                    self.send_header("Location", location)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                url = urlparse(self.path)
                query = parse_qs(url.query)
                if url.path.startswith("/feed"):
                    if not server.logged_in:
                        return self._send(302, "", location="/login")
                    return self._send(200, FEED)
                if url.path.startswith("/login"):
                    return self._send(200, LOGIN)
                if url.path.startswith("/checkpoint"):
                    return self._send(200, CHECKPOINT)
                if url.path.startswith("/search/results/people"):
                    try:
                        page = int((query.get("page") or ["1"])[0])
                    except ValueError:
                        page = 1
                    server.requests.append({"path": url.path, "page": page, "keywords": (query.get("keywords") or [""])[0],
                                            "query": url.query})
                    if server.logout_after is not None and len(server.requests) > server.logout_after:
                        server.logged_in = False
                    if not server.logged_in:
                        return self._send(302, "", location="/login")
                    if server.checkpoint_page == page:
                        return self._send(302, "", location="/checkpoint/challenge/abc123")
                    return self._send(200, render_results(page))
                return self._send(404, NOT_FOUND)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "MockSearch":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


if __name__ == "__main__":  # manual poke: python tests/mock_search.py -> open the printed URL
    with MockSearch() as m:
        print(m.base_url + "/search/results/people/?keywords=design")
        threading.Event().wait()
