"""Stand-in web servers on this machine, for tests of anything that downloads: ``LocalServer`` (subclasses decide the answers)
and ``Pages`` (a table of routes that remembers who asked for what)."""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from connecto import netguard


class LocalServer:
    """Base for the throw-away HTTP servers: subclasses implement ``handle(request_handler, parsed_url)``."""

    def __init__(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def do_GET(self):
                outer.handle(self, urlparse(self.path))

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()

    @staticmethod
    def reply(h, status: int, body: str | bytes = "", ctype: str = "text/html; charset=utf-8", headers: dict | None = None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        h.send_response(status)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            h.send_header(k, v)
        h.end_headers()
        h.wfile.write(data)


class Pages(LocalServer):
    """Serves ``routes[path] = (status, body, content_type, headers)``; remembers who asked for what."""

    def __init__(self):
        super().__init__()
        self.routes: dict[str, tuple] = {}
        self.user_agents: list[str] = []
        self.hits: list[str] = []
        self.requests: list[dict] = []                 # the headers of every request (lower-case names)

    def html(self, path: str, body: str, status: int = 200, headers: dict | None = None):
        self.routes[path] = (status, body, "text/html; charset=utf-8", headers or {})

    def redirect(self, path: str, location: str, status: int = 302):
        self.routes[path] = (status, "", "text/html", {"Location": location})

    def handle(self, h, url):
        self.user_agents.append(h.headers.get("User-Agent", ""))
        self.requests.append({k.lower(): v for k, v in h.headers.items()} | {":path": h.path})
        self.hits.append(url.path)
        status, body, ctype, headers = self.routes.get(url.path, (404, "not found", "text/html", {}))
        self.reply(h, status, body, ctype, headers)


class Steer:
    """Sends every connection netguard opens to a stand-in server on this machine, and remembers where each one was *meant*
    to go (the address that had been checked)."""

    def __init__(self, monkeypatch):
        self.port: int | None = None
        self.meant: list[tuple[str, int]] = []
        self.down: set[str] = set()                    # addresses that refuse the connection
        real = netguard._connect

        def fake(ip, port, timeout):
            self.meant.append((ip, port))
            if ip in self.down:
                raise ConnectionRefusedError(f"the stand-in for {ip} is down")
            return real("127.0.0.1", self.port if self.port is not None else port, timeout)

        monkeypatch.setattr(netguard, "_connect", fake)

    def to(self, port: int) -> "Steer":
        self.port = port
        return self
