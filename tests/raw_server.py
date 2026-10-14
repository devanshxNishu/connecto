"""A raw TCP stand-in for badly behaved servers: a test decides, byte by byte, what is sent and when.

    srv = RawServer(drip_body(40, gap=0.2)).start()
    ... srv.base_url ...                # http://127.0.0.1:<port>/v1
    srv.requests                        # every request received: Request(line, headers, body)
    srv.stop()                          # also drops every connection still open

Stdlib only, bound to 127.0.0.1. The handlers below are the misbehaviours the AI client has to survive.
"""
from __future__ import annotations

import gzip
import json
import socket
import threading
import time
import zlib
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Request:
    line: str
    headers: dict                      # lower-cased names
    body: bytes = b""

    @property
    def path(self) -> str:
        return self.line.split(" ")[1] if " " in self.line else ""


@dataclass
class RawServer:
    handler: Callable[[socket.socket, Request, "RawServer"], None]
    requests: list = field(default_factory=list)
    bytes_sent: int = 0

    def __post_init__(self):
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(16)
        self._connections: list[socket.socket] = []
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)

    @property
    def port(self) -> int:
        return self._listener.getsockname()[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def start(self) -> "RawServer":
        # accept() wakes up every tick to look at ``_stopped``. (Closing a listening socket does NOT wake a thread that is
        # blocked in accept() on Linux: the half-closed "zombie" keeps the port and keeps answering - to this day one
        # later connection - so a test that "stopped" its server could still be talking to something else.)
        self._listener.settimeout(self.TICK)
        self._thread.start()
        return self

    def stop(self) -> None:
        """Really stops: no more connections are accepted, the port is released, every open connection is dropped."""
        self._stopped.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)
        try:
            self._listener.close()
        except OSError:
            pass
        with self._lock:
            conns, self._connections = self._connections, []
        for conn in conns:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass

    # ------------------------------------------------------------------ plumbing
    TICK = 0.05

    def _accept_loop(self) -> None:
        while not self._stopped.is_set():
            try:
                conn, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if self._stopped.is_set():                # it slipped in while we were stopping
                conn.close()
                return
            with self._lock:
                self._connections.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        try:
            request = self._read_request(conn)
            if request is None:
                return
            with self._lock:
                self.requests.append(request)
            self.handler(conn, request, self)
        except OSError:
            pass                                      # the client gave up: nothing to do
        finally:
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _read_request(conn: socket.socket) -> Request | None:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                return None
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        length = int(headers.get("content-length") or 0)
        while len(rest) < length:
            chunk = conn.recv(4096)
            if not chunk:
                break
            rest += chunk
        return Request(lines[0], headers, rest[:length])

    def send(self, conn: socket.socket, data: bytes) -> None:
        conn.sendall(data)
        with self._lock:
            self.bytes_sent += len(data)


# ----------------------------------------------------------------------------- misbehaviours (handlers)
def _answer(text: str = "hello") -> bytes:
    return json.dumps({"choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                    "finish_reason": "stop"}]}).encode()


def redirect_to(url: str, status: int = 307) -> Callable:
    """Answer every request with a redirect to ``url``."""
    def handle(conn, request, srv):
        srv.send(conn, f"HTTP/1.1 {status} Temporary Redirect\r\nLocation: {url}\r\nContent-Length: 0\r\n"
                       "Connection: close\r\n\r\n".encode())
    return handle


def drip_body(total: int, gap: float) -> Callable:
    """A valid status line and headers, then ``total`` body bytes, one every ``gap`` seconds."""
    def handle(conn, request, srv):
        srv.send(conn, f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {total}\r\n\r\n".encode())
        for _ in range(total):
            time.sleep(gap)
            srv.send(conn, b" ")
    return handle


def drip_headers(gap: float, seconds: float) -> Callable:
    """Never finishes the headers: one byte of a header line every ``gap`` seconds, for ``seconds`` seconds."""
    def handle(conn, request, srv):
        srv.send(conn, b"HTTP/1.1 200 OK\r\nX-Slow: ")
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            time.sleep(gap)
            srv.send(conn, b"a")
    return handle


def declared_huge(length: int = 50_000_000) -> Callable:
    """Claims a body of ``length`` bytes (and sends a few)."""
    def handle(conn, request, srv):
        srv.send(conn, f"HTTP/1.1 200 OK\r\nContent-Length: {length}\r\n\r\n".encode() + b" " * 1000)
        time.sleep(0.5)
    return handle


def endless_body(limit: int = 40_000_000, piece: int = 65_536) -> Callable:
    """No Content-Length: sends spaces until the client hangs up (or ``limit`` bytes)."""
    def handle(conn, request, srv):
        srv.send(conn, b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n")
        sent = 0
        while sent < limit:
            srv.send(conn, b" " * piece)
            sent += piece
    return handle


def chunked(text: str = "hello", pieces: int = 3) -> Callable:
    """A correct answer in ``Transfer-Encoding: chunked`` pieces."""
    def handle(conn, request, srv):
        body = _answer(text)
        size = max(1, len(body) // pieces + 1)
        out = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
        for i in range(0, len(body), size):
            part = body[i:i + size]
            out += f"{len(part):x}\r\n".encode() + part + b"\r\n"
        srv.send(conn, out + b"0\r\n\r\n")
    return handle


def gzipped(text: str = "hello") -> Callable:
    """A correct answer, compressed - although the client asked for none."""
    def handle(conn, request, srv):
        body = gzip.compress(_answer(text))
        srv.send(conn, b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Encoding: gzip\r\n"
                       + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    return handle


def gzip_bomb(megabytes: int = 100) -> Callable:
    """About 100 KB on the wire that inflates to ``megabytes`` MB."""
    def handle(conn, request, srv):
        packer = zlib.compressobj(9, zlib.DEFLATED, 31)
        body = b"".join(packer.compress(b" " * 1_000_000) for _ in range(megabytes)) + packer.flush()
        srv.send(conn, b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Encoding: gzip\r\n"
                       + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    return handle


def ok_json(text: str = "hello") -> Callable:
    def handle(conn, request, srv):
        body = _answer(text)
        srv.send(conn, b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                       + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
    return handle
