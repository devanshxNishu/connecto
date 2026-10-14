"""Shared by the setup-page tests: a tiny HTTP client that does what the page does, and a running test server."""
from __future__ import annotations

import http.client
import json
import threading
import time

from connecto.ui import server as srv

LINK = "L" * 43                    # the token in the printed link (works once)
SECRET = "S" * 43                  # what the page's own requests carry in the header


class Reply:
    def __init__(self, status: int, headers, data: bytes):
        self.status, self.headers, self.data = status, headers, data

    @property
    def text(self) -> str:
        return self.data.decode("utf-8", "replace")

    @property
    def json(self):
        return json.loads(self.text)


class Client:
    """Talks to the test server. After ``sign_in()`` it presents both credentials the page has: the session cookie it was given
    when the link was opened, and the secret the page carries."""

    def __init__(self, port: int, link: str = LINK, secret: str = SECRET):
        self.port, self.link, self.secret = port, link, secret
        self.cookie_name = srv.cookie_name(port)
        self.session = ""

    def sign_in(self) -> "Client":
        """Open the printed link the way a browser does and keep the cookie it hands back."""
        reply = self.request("GET", f"/?t={self.link}", cookie=None, header=None)
        assert reply.status == 302 and reply.headers["Location"] == "/", reply.text
        name, _, rest = reply.headers["Set-Cookie"].partition("=")
        assert name == self.cookie_name, name
        self.session = rest.split(";")[0]
        return self

    def request(self, method: str, path: str, body=None, *, raw_body: bytes | None = None, cookie="default", header="default",
                host: str | None = None, content_type: str = "application/json", headers: dict | None = None) -> Reply:
        hdrs = {"Host": f"127.0.0.1:{self.port}" if host is None else host}
        cookie = self.session if cookie == "default" else cookie
        header = self.secret if header == "default" else header
        if cookie:
            hdrs["Cookie"] = f"{self.cookie_name}={cookie}"
        if header:
            hdrs[srv.HEADER] = header
        payload = raw_body if raw_body is not None else (json.dumps(body).encode() if body is not None else None)
        if payload is not None:
            hdrs["Content-Type"] = content_type
        hdrs.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            conn.request(method, path, body=payload, headers=hdrs)
            resp = conn.getresponse()
            return Reply(resp.status, resp.msg, resp.read())
        finally:
            conn.close()

    def get(self, path: str, **kw) -> Reply:
        return self.request("GET", path, **kw)

    def post(self, path: str, body=None, **kw) -> Reply:
        if body is None and "raw_body" not in kw:
            body = {}
        return self.request("POST", path, body, **kw)

    def state(self) -> dict:
        return self.get("/api/state").json


def start_server(home, link: str = LINK, secret: str = SECRET, *, sign_in: bool = True) -> Client:
    """Run the setup-page server for ``home`` on a free local port in a background thread. By default the client has already
    opened the link (so it holds a session); pass ``sign_in=False`` to keep the link for a real browser to open."""
    httpd, app = srv.make_server(home, token=link, secret=secret)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    client = Client(app.port, link, secret)
    client.app, client.home, client.httpd, client.thread = app, home, httpd, thread
    if sign_in:
        client.sign_in()
    return client


def stop_server(client: Client) -> None:
    client.app.jobs.stop()
    deadline = time.time() + 5
    while client.app.jobs.active() is not None and time.time() < deadline:
        time.sleep(0.02)
    client.httpd.shutdown()
    client.httpd.server_close()
    client.thread.join(timeout=3)
