"""A tiny fake OpenAI-compatible provider for the AI tests: stdlib only, bound to 127.0.0.1, scriptable.

    srv = FakeAIServer().start()
    srv.script(rate_limited(1), ok("hello"))      # the next two chat requests get these answers, then the default
    client = AIClient(srv.base_url, "key", "model")
    ...
    srv.chat_requests()[0].json["messages"]       # everything the client sent is recorded
    srv.stop()
"""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

DEFAULT_HOOK = "Building payments infrastructure for small businesses at Acme Pay is really interesting work."


@dataclass
class Recorded:
    method: str
    path: str
    headers: dict          # lower-cased header names
    raw: str
    json: object = None    # parsed body, or None

    def messages_text(self) -> str:
        """Every message content of a chat request, concatenated (what the model would read)."""
        msgs = (self.json or {}).get("messages", []) if isinstance(self.json, dict) else []
        return "\n".join(str(m.get("content", "")) for m in msgs)


@dataclass
class Reply:
    status: int = 200
    body: object = None                      # dict/list -> JSON, str -> raw text, None -> empty
    headers: dict = field(default_factory=dict)
    delay: float = 0.0                       # seconds the server waits before answering
    fn: Callable[[Recorded], "Reply"] | None = None   # decide the reply from the request


# ----------------------------------------------------------------------------- reply builders
def completion(content, finish_reason: str = "stop") -> dict:
    return {"id": "chatcmpl-test", "object": "chat.completion", "model": "test-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                         "finish_reason": finish_reason}]}


def ok(text: str = DEFAULT_HOOK, finish_reason: str = "stop") -> Reply:
    return Reply(200, completion(text, finish_reason))


def parts(*texts: str) -> Reply:
    return Reply(200, completion([{"type": "text", "text": t} for t in texts]))


def think(thought: str, answer: str) -> Reply:
    return ok(f"<think>{thought}</think>\n\n{answer}")


def empty() -> Reply:
    return ok("")


def ran_out_of_tokens() -> Reply:
    """What a reasoning model returns when it spends the whole budget thinking."""
    return Reply(200, completion(None, "length"))


def error(status: int, message: str, headers: dict | None = None) -> Reply:
    return Reply(status, {"error": {"message": message, "type": "test_error"}}, headers or {})


def unauthorized() -> Reply:
    return error(401, "Incorrect API key provided.")


def rate_limited(retry_after: float | str | None = 1) -> Reply:
    headers = {} if retry_after is None else {"Retry-After": str(retry_after)}
    return error(429, "Rate limit reached for requests", headers)


def server_error(status: int = 500) -> Reply:
    return error(status, "The server had an error while processing your request")


def malformed() -> Reply:
    return Reply(200, "this is not json {{{")


def slow(seconds: float, text: str = DEFAULT_HOOK) -> Reply:
    reply = ok(text)
    reply.delay = seconds
    return reply


def no_json_mode() -> Reply:
    return error(400, "'response_format' of type 'json_object' is not supported with this model.")


def model_missing(name: str = "nope") -> Reply:
    return error(404, f"The model `{name}` does not exist or you do not have access to it.")


def by_request(fn: Callable[[Recorded], Reply]) -> Reply:
    return Reply(fn=fn)


# ----------------------------------------------------------------------------- the server
class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):          # keep the test output quiet
        pass

    def do_GET(self):
        self._serve("GET")

    def do_POST(self):
        self._serve("POST")

    def _serve(self, method: str) -> None:
        fake: FakeAIServer = self.server.fake          # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = None
        rec = Recorded(method, self.path, {k.lower(): v for k, v in self.headers.items()}, raw, parsed)
        path = self.path.split("?")[0].rstrip("/")
        with fake._lock:
            fake.requests.append(rec)
            if method == "GET" and path.endswith("/models"):
                reply = fake.models_reply or Reply(200, {"object": "list", "data": list(fake.models)})
            elif method == "POST" and path.endswith("/chat/completions"):
                reply = fake._script.popleft() if fake._script else fake.default_chat
            else:
                reply = error(404, f"no route {method} {path}")
        if reply.fn is not None:
            reply = reply.fn(rec)
        if reply.delay:
            time.sleep(reply.delay)
        self._send(reply)

    def _send(self, reply: Reply) -> None:
        body = reply.body
        if body is None:
            payload = b""
        elif isinstance(body, str):
            payload = body.encode("utf-8")
        else:
            payload = json.dumps(body).encode("utf-8")
        try:
            self.send_response(reply.status)
            headers = {"Content-Type": "application/json", "Content-Length": str(len(payload)), **reply.headers}
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(payload)
        except OSError:                    # the client gave up (timeout test): nothing to do
            pass


class FakeAIServer:
    def __init__(self):
        self.requests: list[Recorded] = []
        self.models: list[dict] = [{"id": "test-model", "object": "model"}]
        self.models_reply: Reply | None = None
        self.default_chat: Reply = ok()
        self._script: deque[Reply] = deque()
        self._lock = threading.Lock()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.daemon_threads = True
        self._httpd.fake = self                                    # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def start(self) -> "FakeAIServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=2)

    # ------------------------------------------------------------------ scripting and inspection
    def script(self, *replies: Reply) -> "FakeAIServer":
        with self._lock:
            self._script.extend(replies)
        return self

    def chat_requests(self) -> list[Recorded]:
        return [r for r in self.requests if r.method == "POST"]

    def model_requests(self) -> list[Recorded]:
        return [r for r in self.requests if r.method == "GET"]
