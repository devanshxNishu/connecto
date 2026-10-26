"""The setup page's server: sign-in token, request checks, secrets, saving settings, people, previews and background jobs.

A real server runs on 127.0.0.1 (random port) for each test and is talked to over HTTP. Nothing here starts a browser,
reaches LinkedIn, touches the real scheduler, or contacts a real AI service.
"""
from __future__ import annotations

import csv
import http.client
import io
import json
import logging
import os
import socket
import stat
import threading
import time
from types import SimpleNamespace

import pytest

import fake_ai_server as fake
from connecto import runner, scheduler
from connecto.limits import HARD
from connecto.store import Store
from connecto.ui import server as srv

from helpers import add_people, make_cfg, make_home, person
from ui_helpers import LINK, SECRET, Client, start_server, stop_server

# ----------------------------------------------------------------------------- fixtures
@pytest.fixture()
def home(tmp_path):
    return make_home(tmp_path)


@pytest.fixture()
def ui(home, clean_env):
    client = start_server(home)                               # (the link has been opened: this client holds a session)
    yield client
    stop_server(client)


@pytest.fixture()
def fresh(home, clean_env):
    """A server that nobody has signed in to yet: the printed link has not been used."""
    client = start_server(home, sign_in=False)
    yield client
    stop_server(client)


def test_queue_api_search_pagination_and_skip_undo(ui):
    save_ready(ui)
    with Store(ui.home.db_path) as store:
        store.upsert_people([person(f"q{i}", f"Queue Person {i}", company="Acme") for i in range(25)], "manual", "manual")
    page = ui.get("/api/queue?q=ACME&limit=10&offset=20")
    assert page.status == 200 and len(page.json["people"]) == 5
    assert page.json["queued"] == page.json["total"] == 25
    row = page.json["people"][0]
    assert row["note"] and row["chars"] <= page.json["note_limit"]
    assert ui.post("/api/people/skip", {"text": row["url"]}).json["skipped"] == 1
    assert ui.get("/api/queue").json["queued"] == 24
    assert ui.post("/api/people/skip", {"text": row["url"], "undo": True}).json["undone"] == 1
    assert ui.get("/api/queue").json["queued"] == 25


@pytest.mark.parametrize("suffix", ["limit=oops", "offset=NaN"])
def test_queue_api_rejects_invalid_pagination(ui, suffix):
    assert ui.get("/api/queue?" + suffix).status == 400


@pytest.mark.parametrize("credentials", [{"cookie": None}, {"header": None}, {"header": "wrong"}])
def test_queue_requires_both_session_and_api_secret(ui, credentials):
    assert ui.get("/api/queue", **credentials).status in (401, 403)


def save_ready(ui: Client, **over) -> dict:
    """Save a config that is ready to send (name set, risks accepted)."""
    cfg = make_cfg(**over)
    reply = ui.post("/api/config", {"config": cfg})
    assert reply.status == 200, reply.text
    return reply.json["config"]


def wait_job(ui: Client, timeout: float = 10) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = ui.get("/api/job").json
        if data["job"] and data["job"]["state"] != "running":
            return data
        time.sleep(0.02)
    raise AssertionError("the job did not finish in time")


# ============================================================================= the page and the token
def test_the_server_only_listens_on_this_computer(ui):
    assert ui.httpd.server_address[0] == "127.0.0.1"


def test_the_page_is_only_given_to_someone_with_the_link(fresh):
    assert fresh.get("/", cookie=None, header=None).status == 403
    assert fresh.get("/?t=wrong", cookie=None, header=None).status == 403
    assert fresh.get("/api/state").status == 401                                  # nobody holds a session yet
    redirect = fresh.get(f"/?t={LINK}", cookie=None, header=None)
    assert redirect.status == 302 and redirect.headers["Location"] == "/"
    set_cookie = redirect.headers["Set-Cookie"]
    name, _, rest = set_cookie.partition("=")
    session = rest.split(";")[0]
    assert name == srv.cookie_name(fresh.port) == fresh.cookie_name and name.endswith(f"_{fresh.port}")
    assert "HttpOnly" in set_cookie and "SameSite=Strict" in set_cookie and "Path=/" in set_cookie
    assert len(session) >= 40 and session not in (LINK, SECRET)               # the cookie is not the link and not the secret
    # the redirect target is served to whoever now holds the cookie (the header is only needed by /api calls)
    page = fresh.get("/", cookie=session, header=None)
    assert page.status == 200 and page.headers["Content-Type"].startswith("text/html")
    assert f'name="connecto-secret" content="{SECRET}"' in page.text           # the page can read the secret it sends back
    assert LINK not in page.text and session not in page.text                  # ... and nothing else
    assert "__NONCE__" not in page.text and "__SECRET__" not in page.text and "__VERSION__" not in page.text
    assert fresh.get("/", cookie=SECRET, header=None).status == 403            # the secret is not a cookie
    assert fresh.get("/", cookie=LINK, header=None).status == 403              # neither is the link


def test_the_link_works_once(fresh):
    first = fresh.sign_in()
    assert first.get("/").status == 200 and first.get("/api/state").status == 200
    again = fresh.get(f"/?t={LINK}", cookie=None, header=None)
    assert again.status == 403 and "works only once" in again.text and "Set-Cookie" not in again.headers
    assert fresh.get("/api/state").status == 200                                # the first session is untouched
    # someone who is signed in already and opens the old link again is simply taken to the page
    back = fresh.get(f"/?t={LINK}", header=None)
    assert back.status == 302 and back.headers["Location"] == "/" and "Set-Cookie" not in back.headers
    assert fresh.get(f"/?t=wrong", cookie=None, header=None).status == 403          # (and a link that never was one stays refused)


def test_a_wrong_link_does_not_use_the_real_one_up(fresh):
    for guess in ("", "x", "L" * 42, "L" * 44, LINK[:-1] + "M", LINK.lower()):
        assert fresh.get(f"/?t={guess}", cookie=None, header=None).status == 403, guess
    assert fresh.sign_in().get("/api/state").status == 200


def test_asking_about_the_page_does_not_use_the_link_up(fresh):
    assert fresh.request("HEAD", f"/?t={LINK}", cookie=None, header=None).status == 403
    assert fresh.sign_in().get("/api/state").status == 200


def test_the_link_the_cookie_and_the_secret_are_three_different_things(ui):
    assert len({LINK, SECRET, ui.session}) == 3
    assert ui.get("/api/state").status == 200
    for cookie, header in ((LINK, SECRET), (SECRET, SECRET), (ui.session, LINK), (ui.session, ui.session),
                           (ui.session, ""), ("", SECRET), (LINK, LINK)):
        assert ui.get("/api/state", cookie=cookie, header=header).status == 401, (cookie, header)


def test_two_copies_on_one_machine_do_not_overwrite_each_others_cookie(tmp_path, clean_env):
    a, b = start_server(make_home(tmp_path / "a")), start_server(make_home(tmp_path / "b"))
    try:
        assert a.cookie_name != b.cookie_name
        both = f"{a.cookie_name}={a.session}; {b.cookie_name}={b.session}"            # a browser sends every cookie of the host
        assert a.get("/api/state", cookie=None, headers={"Cookie": both}).status == 200
        assert b.get("/api/state", cookie=None, headers={"Cookie": both}).status == 200
        assert a.get("/api/state", cookie=None, headers={"Cookie": f"{b.cookie_name}={b.session}"}).status == 401
        assert a.get("/api/state", cookie=None, headers={"Cookie": f"{a.cookie_name}={b.session}"}).status == 401
    finally:
        stop_server(a)
        stop_server(b)


def test_another_cookie_with_our_name_cannot_lock_you_out(ui):
    name, good = ui.cookie_name, ui.session
    for jar in (f"{name}=junk; {name}={good}", f"{name}={good}; {name}=junk", f"other=1; {name}={good}; x=2"):
        assert ui.get("/api/state", cookie=None, headers={"Cookie": jar}).status == 200, jar
    assert ui.get("/api/state", cookie=None, headers={"Cookie": f"{name}=junk; {name}=junk2"}).status == 401
    assert ui.get("/api/state", cookie=None, headers={"Cookie": f"x{name}={good}"}).status == 401
    assert ui.get("/api/state", cookie=None, headers={"Cookie": f"{name[:-1]}={good}"}).status == 401


def test_the_page_is_locked_down_by_a_content_security_policy_with_a_fresh_nonce(ui):
    first, second = ui.get("/"), ui.get("/")
    csp = first.headers["Content-Security-Policy"]
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp and "form-action 'none'" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    nonce = csp.split("script-src 'nonce-")[1].split("'")[0]
    assert f'<script nonce="{nonce}">' in first.text and f'<style nonce="{nonce}">' in first.text
    assert nonce not in second.headers["Content-Security-Policy"]             # a new nonce every time
    assert first.headers["X-Frame-Options"] == "DENY" and first.headers["Cache-Control"] == "no-store"
    assert first.headers["X-Content-Type-Options"] == "nosniff"


def test_the_page_has_no_inline_handlers_that_the_policy_would_block(ui):
    html = ui.get("/").text
    for needle in (" onclick=", " onload=", " onerror=", " style=", "javascript:"):
        assert needle not in html, f"{needle!r} would be blocked by the content security policy"


def test_head_requests_get_headers_and_no_body(ui):
    reply = ui.request("HEAD", "/")
    assert reply.status == 200 and reply.data == b""


# ============================================================================= who may call the API
def test_every_api_call_needs_both_the_cookie_and_the_header(ui):
    assert ui.get("/api/state").status == 200
    assert ui.get("/api/state", cookie=None).status == 401
    assert ui.get("/api/state", header=None).status == 401
    assert ui.get("/api/state", header="x" * 43).status == 401
    assert ui.get("/api/state", cookie="x" * 43).status == 401
    assert ui.post("/api/pause", {"days": 1}, cookie=None).status == 401
    assert ui.post("/api/shutdown", {}, header=None).status == 401
    assert ui.thread.is_alive()                                               # the refused shutdown did nothing


def test_an_unauthorised_answer_reveals_nothing(ui):
    reply = ui.get("/api/state", cookie=None)
    assert reply.json == {"error": "Not signed in to the setup page. Open the link `connecto ui` printed."}
    assert reply.headers["Connection"] == "close"


def test_the_host_header_must_be_ours_which_defeats_dns_rebinding(ui):
    for host in ("evil.example", "evil.example:80", "127.0.0.1", "127.0.0.1:1", f"attacker.test:{ui.port}", ""):
        assert ui.get("/api/state", host=host).status == 403, host
        assert ui.get("/", host=host).status == 403, host
    assert ui.get("/api/state", host=f"localhost:{ui.port}").status == 200
    assert ui.get("/api/state", host=f"LOCALHOST:{ui.port}").status == 200


def test_cross_site_requests_are_refused_and_no_cors_is_ever_offered(ui):
    base = f"http://127.0.0.1:{ui.port}"
    assert ui.get("/api/state", headers={"Origin": "http://evil.example"}).status == 403
    assert ui.get("/api/state", headers={"Origin": "null"}).status == 403
    assert ui.get("/api/state", headers={"Sec-Fetch-Site": "cross-site"}).status == 403
    assert ui.get("/api/state", headers={"Sec-Fetch-Site": "same-site"}).status == 403
    assert ui.post("/api/pause", {"days": 1}, headers={"Origin": "http://evil.example"}).status == 403
    ok = ui.get("/api/state", headers={"Origin": base, "Sec-Fetch-Site": "same-origin"})
    assert ok.status == 200
    assert not any(name.lower().startswith("access-control-") for name in ok.headers.keys())
    assert ui.request("OPTIONS", "/api/state", headers={"Origin": "http://evil.example",
                                                         "Access-Control-Request-Method": "POST"}).status == 405


def test_unknown_routes_and_methods(ui):
    assert ui.get("/api/nope").status == 404
    assert ui.get("/nope").status == 404
    assert ui.post("/api/state", {}).status == 405
    for method in ("PUT", "DELETE", "PATCH", "OPTIONS"):
        assert ui.request(method, "/api/state").status == 405


def test_bodies_must_be_small_json_objects(ui):
    assert ui.post("/api/pause", raw_body=b'{"days": 1}', content_type="text/plain").status == 415
    assert ui.post("/api/pause", raw_body=b'{"days": 1}', content_type="application/x-www-form-urlencoded").status == 415
    assert ui.post("/api/pause", raw_body=b"{not json").status == 400
    assert ui.post("/api/pause", raw_body=b"[1, 2]").status == 400
    assert ui.post("/api/pause", raw_body=b"\xff\xfe").status == 400
    assert ui.post("/api/pause", raw_body=b'{"days": 1}', headers={"Content-Length": "abc"}).status == 400


NOT_NUMBERS = [b'{"days": NaN}', b'{"days": Infinity}', b'{"days": -Infinity}', b'{"days": 1e999}', b'{"days": -1e999}',
               b'{"days": 1E+999}', b'{"days": [1e999]}', b'{"days": {"a": 1e400}}']
NOT_JSON_AT_ALL = [b"[" * 200_000, b'{"a":' * 100_000 + b"1" + b"}" * 100_000, b'{"a": 1' + b"0" * 6000 + b"}",
                   b'{"a": "unfinished', b"\x00", b"null", b"true", b'"text"', b"5"]


@pytest.mark.parametrize("raw", NOT_NUMBERS)
def test_numbers_a_computer_cannot_hold_are_a_clear_400_on_every_route(ui, raw):
    for path in ("/api/pause", "/api/config", "/api/job/start", "/api/people/add", "/api/ai/key", "/api/render"):
        reply = ui.post(path, raw_body=raw)
        assert reply.status == 400 and "numbers must be finite" in reply.json["error"], (path, reply.text)


@pytest.mark.parametrize("raw", NOT_JSON_AT_ALL)
def test_a_body_that_is_not_a_json_object_is_a_400_never_a_crash(ui, raw):
    reply = ui.post("/api/pause", raw_body=raw)
    assert reply.status == 400 and "Something went wrong" not in reply.text, reply.text
    assert reply.json["error"].startswith("Body "), reply.text
    assert ui.get("/api/state").status == 200                                       # and the server carries on


def test_the_body_reader_says_what_is_wrong():
    for raw, message in ((b"{", "Body is not valid JSON."), (b"[1]", "Body must be a JSON object."),
                         (b"[" * 100_000, "nested too deeply"), (b'{"a": NaN}', "numbers must be finite"),
                         (b'{"a": 1e999}', "numbers must be finite"), (b'{"a": -1e999}', "numbers must be finite"),
                         (b"\xff", "Body is not valid JSON.")):
        with pytest.raises(ValueError, match=message):
            srv.parse_body(raw)
    assert srv.parse_body(b"") == {} and srv.parse_body(b"  {} ") == {}
    assert srv.parse_body(b'{"a": 1.5, "b": [2e3, -0.0, 1e308], "c": 12345678901234567890}') == {
        "a": 1.5, "b": [2000.0, -0.0, 1e308], "c": 12345678901234567890}


@pytest.mark.parametrize("error", [OverflowError("too big"), RecursionError("too deep"), ValueError("odd"), TypeError("odd")])
def test_a_request_that_makes_a_route_fail_like_a_bad_request_is_a_400_not_a_500(ui, monkeypatch, error):
    def boom(self):
        raise error
    monkeypatch.setattr(srv.App, "state", boom)
    reply = ui.get("/api/state")
    assert reply.status == 400 and reply.json["error"].startswith("Bad request"), reply.text


def test_a_number_too_big_for_days_or_limit_is_a_400(ui):
    save_ready(ui)
    for days in ("1" + "0" * 5000, "1e999", "inf", "nan", None, [1], {"a": 1}):
        assert ui.post("/api/pause", {"days": days}).status == 400, days
    for days in (1e300, 10 ** 400, "1" + "0" * 400):                                 # finite, merely huge: capped at sixty days
        assert ui.post("/api/pause", {"days": days}).status == 200, days
    assert ui.get("/api/state").json["status"]["paused_until"][:4] < "2100"
    for limit in ("1" + "0" * 5000, "1e999", "inf", [3], {"a": 1}):
        assert ui.post("/api/job/start", {"kind": "dry_run", "limit": limit}).status == 400, limit


def test_half_an_emoji_in_a_request_is_replaced_instead_of_being_saved_as_poison(ui, home):
    body = (b'{"config": {"acknowledged_risks": true, "me": {"first_name": "A\\ud800", "about": "ok \\ud83d\\ude00 and \\udc00"},'
            b' "message": {"templates": ["Hi {first_name}\\ud83d!"]}}}')
    reply = ui.post("/api/config", raw_body=body)
    assert reply.status == 200, reply.text
    saved = reply.json["config"]
    assert saved["me"]["first_name"] == "A\ufffd" and saved["me"]["about"] == "ok \U0001f600 and \ufffd"      # (a whole emoji is kept)
    assert saved["message"]["templates"] == ["Hi {first_name}\ufffd!"]
    assert ui.get("/api/state").status == 200                              # the page still loads
    assert "\\ud8" not in home.config_path.read_text().lower() and "\\udc" not in home.config_path.read_text().lower()
    for path, raw in (("/api/people/add", b'{"text": "https://www.linkedin.com/in/abc-def/ Zoe\\ud800"}'),
                      ("/api/render", b'{"config": {"message": {"templates": ["Hi\\ud800"]}}}'),
                      ("/api/people/skip", b'{"text": "https://www.linkedin.com/in/abc-def/\\ud800"}')):
        assert ui.post(path, raw_body=raw).status in (200, 400), path
    assert ui.get("/api/people").status == 200 and ui.get("/api/history").status == 200 and ui.get("/api/export.csv").status == 200


def test_a_stray_half_emoji_that_is_already_in_a_file_cannot_keep_the_page_from_loading(ui, home):
    home.config_path.write_text('version: 2\nme: {first_name: Asha, about: "x\\uD800y"}\nacknowledged_risks: true\n')
    state = ui.get("/api/state")                                           # (a database cannot hold one; a settings file can)
    assert state.status == 200 and state.json["config"]["me"]["about"] == "x\ud800y"
    assert ui.post("/api/render", {"config": state.json["config"]}).status == 200
    assert ui.post("/api/config", {"config": state.json["config"]}).status == 200
    assert ui.get("/api/state").json["config"]["me"]["first_name"] == "Asha"


def test_the_answers_are_plain_ascii_and_never_invalid_json(ui, monkeypatch):
    save_ready(ui, me={"first_name": "Zoë", "about": "café, 東京 \U0001f600"})
    raw = ui.get("/api/state")
    assert raw.data.isascii() and raw.json["config"]["me"]["first_name"] == "Zoë"
    assert raw.json["config"]["me"]["about"] == "café, 東京 \U0001f600"
    monkeypatch.setattr(srv.App, "state", lambda self: {"x": float("nan")})
    broken = ui.get("/api/state")
    assert "NaN" not in broken.text and broken.status == 400                  # never a body that no browser can parse


def test_an_oversized_body_is_refused_before_it_is_read(ui):
    conn = http.client.HTTPConnection("127.0.0.1", ui.port, timeout=15)
    try:
        conn.putrequest("POST", "/api/config", skip_host=True)
        for k, v in {"Host": f"127.0.0.1:{ui.port}", "Cookie": f"{ui.cookie_name}={ui.session}", srv.HEADER: SECRET,
                     "Content-Type": "application/json", "Content-Length": str(srv.MAX_BODY + 1)}.items():
            conn.putheader(k, v)
        conn.endheaders()                                  # no body is ever sent: the server must not wait for one
        resp = conn.getresponse()
        assert resp.status == 413 and resp.getheader("Connection") == "close"
    finally:
        conn.close()


@pytest.fixture()
def impatient(home, clean_env, monkeypatch):
    """A server that drops a connection after a fraction of a second of silence (the real one waits fifteen seconds)."""
    monkeypatch.setattr(srv, "IDLE_SECONDS", 0.4)
    client = start_server(home)
    yield client
    stop_server(client)


def _talk(port: int, data: bytes, wait: float = 5.0) -> bytes:
    """Send ``data`` and then say nothing; return all the server sent before it closed (b'<still open>' added if it did not).

    An answer can come in more than one piece (the headers, then the body), so this reads until the server hangs up."""
    received = b""
    with socket.create_connection(("127.0.0.1", port), timeout=wait) as sock:
        sock.sendall(data)
        try:
            while piece := sock.recv(4096):
                received += piece
        except socket.timeout:
            return received + b"<still open>"
    return received


def test_the_idle_time_is_fifteen_seconds():
    assert srv.IDLE_SECONDS == 15


def test_a_connection_that_never_finishes_its_request_is_dropped(impatient):
    started = time.monotonic()
    assert _talk(impatient.port, b"GET /api/state HTTP/1.1\r\nHost: x") == b""         # silence, then the server hangs up
    assert time.monotonic() - started < 4
    assert _talk(impatient.port, b"") == b""                                          # connecting and saying nothing is no better
    assert impatient.get("/api/state").status == 200


def test_a_request_body_that_stops_coming_is_answered_with_a_408(impatient):
    head = (f"POST /api/pause HTTP/1.1\r\nHost: 127.0.0.1:{impatient.port}\r\nCookie: {impatient.cookie_name}={impatient.session}\r\n"
            f"{srv.HEADER}: {SECRET}\r\nContent-Type: application/json\r\nContent-Length: 50\r\n\r\n{{\"days\"")
    answer = _talk(impatient.port, head.encode())
    assert answer.startswith(b"HTTP/1.1 408") and b"did not arrive in time" in answer, answer
    assert impatient.get("/api/state").status == 200


def test_an_unexpected_failure_is_a_calm_500_without_internals(ui, monkeypatch, caplog):
    def boom(self):
        raise RuntimeError("secret internal detail /home/someone/x")
    monkeypatch.setattr(srv.App, "state", boom)
    with caplog.at_level(logging.CRITICAL):
        reply = ui.get("/api/state")
    assert reply.status == 500 and reply.json["error"].startswith("Something went wrong (RuntimeError)")
    assert "secret internal detail" not in reply.text and "Traceback" not in reply.text


def test_bad_numbers_in_a_query_are_a_400_not_a_crash(ui):
    assert ui.get("/api/preview?sample=abc").status == 400
    assert ui.get("/api/people?limit=x").status == 400


# ============================================================================= state and settings
def test_state_on_a_fresh_home(ui):
    state = ui.get("/api/state").json
    assert state["has_config"] is False and state["status"] is None and state["job"] is None
    assert state["queue"] == {"matching": 0, "queued": 0}
    assert state["config"]["acknowledged_risks"] is False and state["problems"]
    assert state["ceilings"] == HARD and state["note_limit"] == 200
    assert {"gemini", "groq", "openrouter", "ollama", "custom"} <= {p["id"] for p in state["ai"]["providers"]}
    assert "LinkedIn's User Agreement" in state["risk_text"]
    assert state["default_templates"] and state["source_types"] == ["yc", "csv", "webpage", "linkedin_search"]
    assert state["busy_elsewhere"] is False and state["schedule"]["installed"] in (True, False)


def test_saving_settings_normalises_and_clamps_to_the_ceilings(ui, home):
    reply = ui.post("/api/config", {"config": {
        "me": {"first_name": "Asha"}, "acknowledged_risks": True, "mystery": 1,
        "limits": {"daily": [500, 900], "weekly": 9999, "per_run": [99, 99], "delay_seconds": [1, 2], "stop_after_errors": 50}}})
    assert reply.status == 200
    cfg = reply.json["config"]
    assert cfg["limits"]["daily"][1] <= HARD["daily_max"] and cfg["limits"]["weekly"] <= HARD["weekly_max"]
    assert cfg["limits"]["per_run"][1] <= HARD["per_run_max"] and cfg["limits"]["delay_seconds"][0] >= HARD["delay_min_seconds"]
    assert cfg["limits"]["stop_after_errors"] <= HARD["errors_stop_max"]
    assert any("mystery" in w for w in reply.json["warnings"])
    assert reply.json["problems"] == []
    on_disk = home.config_path.read_text()
    assert "mystery" not in on_disk and "Asha" in on_disk
    assert ui.get("/api/state").json["config"] == cfg                          # what the next page load shows


def test_saving_settings_rejects_a_body_that_is_not_a_config(ui):
    assert ui.post("/api/config", {"config": "nope"}).status == 400
    assert ui.post("/api/config", {"config": [1]}).status == 400
    assert ui.post("/api/config", {}).status == 400


def test_a_hand_edited_broken_settings_file_gives_a_readable_error_not_a_crash(ui, home):
    home.config_path.write_text("limits: [unclosed\n  - : :")
    reply = ui.get("/api/state")
    assert reply.status == 500 and "settings file could not be read" in reply.json["error"]


# ============================================================================= API keys
def test_api_keys_are_write_only(ui, home):
    secret = "sk-test-SECRET-abcdef123456"
    reply = ui.post("/api/ai/key", {"env": "GEMINI_API_KEY", "value": secret})
    assert reply.status == 200 and reply.json == {"saved": True, "origin": ""} and secret not in reply.text
    state = ui.get("/api/state")
    assert secret not in state.text and state.json["ai"]["key_present"]["GEMINI_API_KEY"] is True
    assert f"GEMINI_API_KEY={secret}" in home.env_path.read_text()
    if os.name == "posix":
        assert stat.S_IMODE(home.env_path.stat().st_mode) == 0o600
    save_ready(ui)
    assert secret not in home.config_path.read_text()
    for path in ("/api/doctor", "/api/history", "/api/people", "/api/preview", "/api/export.csv", "/api/job"):
        assert secret not in ui.get(path).text, path
    gone = ui.post("/api/ai/key", {"env": "GEMINI_API_KEY", "remove": True})
    assert gone.json == {"saved": False, "removed": True}
    assert ui.get("/api/state").json["ai"]["key_present"]["GEMINI_API_KEY"] is False
    assert "GEMINI_API_KEY" not in home.env_path.read_text()


@pytest.mark.parametrize("name", ["PATH", "LD_PRELOAD", "gemini_api_key", "X", "A KEY", "GEMINI_API_KEY=x", "GEMINI\nAPI_KEY",
                                  "../KEY", "", "HTTPS_PROXY", "KEY"])
def test_only_things_that_look_like_api_key_names_can_be_written(ui, home, name):
    before = dict(os.environ)
    reply = ui.post("/api/ai/key", {"env": name, "value": "abcdefgh12345678"})
    assert reply.status == 400
    assert dict(os.environ) == before and not home.env_path.exists()


@pytest.mark.parametrize("name", ["GEMINI_API_KEY", "MY_KEY_V2", "SERVICE_TOKEN_2", "DB_PASSWORD", "A_KEY", "TOGETHER_API_KEY",
                                  "KEY", "TOKEN", "gemini_api_key", "PATH", "MONKEY", "KEYBOARD", "X"])
def test_the_setup_page_and_the_command_line_agree_on_which_key_names_are_allowed(ui, home, name, capsys, monkeypatch):
    """One rule (util.is_secret_name) for the shared helper, the command line and the setup page - never two that drift.
    (The page also trims stray spaces around a pasted name, which is covered by its own test.)"""
    import io

    from connecto import cli
    from connecto.util import is_secret_name

    allowed = is_secret_name(name)
    assert (ui.post("/api/ai/key", {"env": name, "value": "abcdefgh12345678"}).status == 200) is allowed
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("abcdefgh12345678\n"))
    code = cli.main(["ai", "key", "--stdin", "--env", name, "--home", str(home.root)])
    capsys.readouterr()
    assert (code == 0) is allowed


@pytest.mark.parametrize("value", ["", "   ", "has a space", "line\nbreak", "tab\tbed", "x" * 600, "ünïcode-key-12345"])
def test_values_that_could_break_the_key_file_are_refused(ui, home, value):
    reply = ui.post("/api/ai/key", {"env": "GEMINI_API_KEY", "value": value})
    assert reply.status == 400
    assert not home.env_path.exists() or "GEMINI_API_KEY" not in home.env_path.read_text()


def test_spaces_and_newlines_around_a_pasted_key_are_trimmed(ui, home):
    reply = ui.post("/api/ai/key", {"env": " GEMINI_API_KEY \n", "value": "  abcdefgh12345678\n"})
    assert reply.status == 200
    assert home.env_path.read_text() == "GEMINI_API_KEY=abcdefgh12345678\n"


def test_replacing_a_key_keeps_other_entries(ui, home):
    home.env_path.write_text("# mine\nOTHER_TOKEN=keepme123\nGEMINI_API_KEY=old-old-old-old\n")
    ui.post("/api/ai/key", {"env": "GEMINI_API_KEY", "value": "new-new-new-new"})
    text = home.env_path.read_text()
    assert "OTHER_TOKEN=keepme123" in text and "# mine" in text
    assert "GEMINI_API_KEY=new-new-new-new" in text and "old-old" not in text


def test_a_key_with_a_name_of_your_own_is_saved_for_one_server_and_goes_nowhere_else(ui, home, ai_server):
    other = fake.FakeAIServer().start()
    try:
        ui.post("/api/ai/key", {"env": "MY_SERVER_API_KEY", "value": "sk-test-abcdefgh1234", "base_url": ai_server.base_url})
        origin = ai_server.base_url.rsplit("/v1", 1)[0]
        assert f"# connecto:origin MY_SERVER_API_KEY {origin}\nMY_SERVER_API_KEY=sk-test-abcdefgh1234" in home.env_path.read_text()
        # the same key, but the settings (or somebody who edited them) point at another server: it is not sent there
        reply = ui.post("/api/ai/test", {"ai": {**custom_ai(ai_server), "base_url": other.base_url}})
        assert reply.json["ok"] is False and "was saved for" in reply.json["message"] and "sk-test-abcdefgh1234" not in reply.text
        assert not other.requests
        assert ui.post("/api/ai/test", {"ai": custom_ai(ai_server)}).json["ok"] is True            # the right one still works
        ui.post("/api/ai/key", {"env": "MY_SERVER_API_KEY", "remove": True})
        assert "connecto:origin" not in home.env_path.read_text()                                   # the record goes with the key
    finally:
        other.stop()


@pytest.mark.parametrize("base_url", ["", 5, ["https://api.example.com/v1"], "http://example.com/v1", "nonsense"])
def test_a_key_that_cannot_be_tied_to_a_server_is_refused(ui, home, base_url):
    save_ready(ui, ai={"enabled": True, "provider": "custom", "base_url": "", "api_key_env": "MY_SERVER_API_KEY"})
    reply = ui.post("/api/ai/key", {"env": "MY_SERVER_API_KEY", "value": "abcdefgh12345678", "base_url": base_url})
    assert reply.status == 400 and "server address" in reply.json["error"]
    assert not home.env_path.exists() or "MY_SERVER_API_KEY" not in home.env_path.read_text()


@pytest.mark.parametrize("body", [
    {"env": "GEMINI_API_KEY", "value": 12345678901234},                                         # a key is text
    {"env": "GEMINI_API_KEY", "value": ["abcdefgh12345678"]},
    {"env": "GEMINI_API_KEY", "value": {"a": "abcdefgh12345678"}},
    {"env": "GEMINI_API_KEY", "value": True},
    {"env": ["GEMINI_API_KEY"], "value": "abcdefgh12345678"},                                    # so is a name
    {"env": 5, "value": "abcdefgh12345678"},
])
def test_a_key_or_a_key_name_that_is_not_text_is_refused(ui, home, body):
    reply = ui.post("/api/ai/key", body)
    assert reply.status == 400
    assert not home.env_path.exists() or "GEMINI_API_KEY" not in home.env_path.read_text()


@pytest.mark.parametrize("remove", ["false", "true", 1, [True], {"a": 1}, None, 0])
def test_only_a_real_true_removes_a_key(ui, home, remove):
    ui.post("/api/ai/key", {"env": "GEMINI_API_KEY", "value": "abcdefgh12345678"})
    reply = ui.post("/api/ai/key", {"env": "GEMINI_API_KEY", "remove": remove})
    assert reply.status == 400
    assert "GEMINI_API_KEY=abcdefgh12345678" in home.env_path.read_text()


# ============================================================================= the AI helper from the page
def _audience_reply() -> fake.Reply:
    return fake.ok(json.dumps({"keywords": ["fintech"], "titles": ["founder"], "locations": ["India"], "exclude_keywords": [],
                               "search_queries": ["fintech founder india"], "yc": {"industries": ["Fintech"]}}))


@pytest.fixture()
def ai_server():
    srv_ = fake.FakeAIServer().start()
    yield srv_
    srv_.stop()


def custom_ai(server) -> dict:
    return {"enabled": True, "provider": "custom", "base_url": server.base_url, "api_key_env": "MY_SERVER_API_KEY",
            "model": "test-model"}


def test_testing_the_ai_connection_uses_the_screen_settings_and_the_saved_key(ui, ai_server):
    saved = ui.post("/api/ai/key", {"env": "MY_SERVER_API_KEY", "value": "sk-test-abcdefgh1234", "base_url": ai_server.base_url})
    assert saved.status == 200 and saved.json["origin"] == ai_server.base_url.rsplit("/v1", 1)[0]
    reply = ui.post("/api/ai/test", {"ai": custom_ai(ai_server)})
    assert reply.status == 200 and reply.json["ok"] is True
    assert ai_server.requests and ai_server.requests[-1].headers["authorization"] == "Bearer sk-test-abcdefgh1234"
    assert "sk-test-abcdefgh1234" not in reply.text


def test_testing_the_ai_without_a_key_explains_what_is_missing_and_never_raises(ui):
    reply = ui.post("/api/ai/test", {"ai": {"provider": "gemini"}})
    assert reply.status == 200 and reply.json["ok"] is False and "GEMINI_API_KEY" in reply.json["message"]


def test_a_custom_key_name_only_counts_when_it_is_saved_in_connectos_own_file(ui, home, ai_server, monkeypatch):
    """A variable of your shell is not read for a custom service (a copied settings file could name any of them)."""
    monkeypatch.setenv("MY_SERVER_API_KEY", "from-the-shell-123")
    save_ready(ui, ai=custom_ai(ai_server))
    assert ui.get("/api/state").json["ai"]["key_present"]["MY_SERVER_API_KEY"] is False
    reply = ui.post("/api/ai/test", {"ai": custom_ai(ai_server)})
    assert reply.json["ok"] is False and "connecto ai key --env MY_SERVER_API_KEY" in reply.json["message"]
    assert not ai_server.requests                                              # nothing was sent anywhere
    ui.post("/api/ai/key", {"env": "MY_SERVER_API_KEY", "value": "sk-test-abcdefgh1234", "base_url": ai_server.base_url})
    assert ui.get("/api/state").json["ai"]["key_present"]["MY_SERVER_API_KEY"] is True
    assert ui.post("/api/ai/test", {"ai": custom_ai(ai_server)}).json["ok"] is True


def test_the_ai_helper_turns_a_sentence_into_filters(ui, ai_server):
    ui.post("/api/ai/key", {"env": "MY_SERVER_API_KEY", "value": "sk-test-abcdefgh1234", "base_url": ai_server.base_url})
    save_ready(ui, ai=custom_ai(ai_server))
    ai_server.script(_audience_reply())
    reply = ui.post("/api/ai/suggest", {"description": "founders of fintech startups in India"})
    assert reply.status == 200
    suggestions = reply.json["suggestions"]
    assert suggestions["keywords"] == ["fintech"] and suggestions["search_queries"] == ["fintech founder india"]
    sent = ai_server.chat_requests()[0].messages_text()
    assert "founders of fintech startups in India" in sent
    assert "Priya" not in sent                                                    # nobody's data is in this request


def test_the_ai_helper_cannot_start_without_a_key(ui):
    save_ready(ui, ai={"enabled": True, "provider": "gemini"})
    reply = ui.post("/api/ai/suggest", {"description": "anyone"})
    assert reply.status == 400 and "cannot start" in reply.json["error"]


# ============================================================================= people
def test_adding_listing_and_skipping_people(ui):
    text = ("Jane Doe, Acme Pay, CEO, https://www.linkedin.com/in/jane-doe-1a2b3c/\n"
            "https://www.linkedin.com/in/jdoe/\n"
            "not a link at all")
    reply = ui.post("/api/people/add", {"text": text})
    assert reply.status == 200
    assert (reply.json["added"], reply.json["updated"], reply.json["nameless"]) == (2, 0, 1)
    assert len(reply.json["problems"]) == 1

    people = ui.get("/api/people").json
    assert people["total"] == 2
    jane = next(p for p in people["people"] if p["name"] == "Jane Doe")
    assert (jane["company"], jane["title"], jane["status"], jane["source"]) == ("Acme Pay", "CEO", "waiting", "manual")
    assert ui.get("/api/people?q=acme").json["total"] == 1
    assert ui.get("/api/people?q=nobody-here").json["total"] == 0
    assert len(ui.get("/api/people?limit=1").json["people"]) == 1
    assert ui.get("/api/state").json["queue"] == {"matching": 2, "queued": 2}

    skipped = ui.post("/api/people/skip", {"text": "https://www.linkedin.com/in/jane-doe-1a2b3c/ junk"})
    assert skipped.json == {"skipped": 1, "rejected": ["junk"]}
    assert ui.get("/api/people?status=skipped").json["total"] == 1
    assert ui.get("/api/state").json["queue"]["queued"] == 1
    undone = ui.post("/api/people/skip", {"text": "https://www.linkedin.com/in/jane-doe-1a2b3c/", "undo": True})
    assert undone.json == {"undone": 1} and ui.get("/api/state").json["queue"]["queued"] == 2


def test_adding_or_skipping_nothing_is_a_clear_error(ui):
    assert ui.post("/api/people/add", {"text": ""}).status == 400
    assert ui.post("/api/people/add", {"text": "just words"}).status == 400
    assert ui.post("/api/people/skip", {"text": "   "}).status == 400


# ============================================================================= previews
def test_the_preview_and_the_queue_agree(ui, home):
    save_ready(ui)
    add_people(home, [person("a-one", "Asha One"), person("b-two", "Bo Two"), person("c-three", "Chi Three")])
    preview = ui.get("/api/preview?sample=2").json
    assert (preview["queued"], preview["matching"], preview["ai_used"]) == (3, 3, False)
    assert len(preview["sample"]) == 2 and 1 <= len(preview["example_notes"]) <= 2
    assert all(len(e["note"]) <= preview["note_limit"] for e in preview["example_notes"])
    assert ui.get("/api/preview?sample=0").json["example_notes"] == []
    assert ui.get("/api/state").json["queue"] == {"matching": 3, "queued": 3}


def test_the_preview_says_why_nobody_would_get_a_note(ui, home):
    ui.post("/api/config", {"config": make_cfg(me={"first_name": ""})})
    add_people(home, [person("a-one", "Asha One")])
    warnings = " ".join(ui.get("/api/preview").json["warnings"])
    assert "first name is not set" in warnings and "no name or detail" not in warnings


def test_people_added_by_hand_are_never_dropped_by_the_filters(ui, home):
    save_ready(ui, targets={"keywords": ["quantum"], "titles": ["professor"]})
    add_people(home, [person("a-one", "Asha One", title="Founder")])                         # a hand-added person
    add_people(home, [person("b-two", "Bo Two", title="Founder")], source_type="csv", source_id="x.csv")  # no csv source on
    queue = ui.get("/api/state").json["queue"]
    assert queue == {"matching": 1, "queued": 1}


def test_templates_can_be_tried_with_a_made_up_person_or_a_real_one(ui, home):
    cfg = make_cfg(message={"templates": ["Hi {first_name}, {hook} I'm {me_first_name}.", "Hi {first_name} at {no_such_detail}"]})
    result = ui.post("/api/render", {"config": cfg}).json
    assert result["person"]["real"] is False and result["limit"] == 300
    first, second = result["results"]
    assert first["ok"] and first["uses_hook"] and srv.App.SAMPLE_HOOK in first["text"]
    assert not second["ok"] and second["missing"] == ["no_such_detail"]
    add_people(home, [person("a-one", "Asha One")])
    again = ui.post("/api/render", {"config": cfg}).json
    assert again["person"] == {"name": "Asha One", "company": "Asha Co", "title": "Founder/CEO", "real": True}


def test_the_built_in_templates_are_previewed_when_you_wrote_none(ui):
    result = ui.post("/api/render", {"config": make_cfg(message={"templates": []})}).json
    assert result["builtin"] is True and len(result["results"]) >= 3 and all(r["ok"] for r in result["results"])


# ============================================================================= pause, schedule, doctor, history
def test_pause_and_resume(ui, home):
    assert ui.post("/api/pause", {"days": 3}).json == {"ok": True}
    status = ui.get("/api/state").json["status"]
    assert status["paused_until"] and status["paused_reason"] == "paused by you"
    assert ui.post("/api/pause", {"days": "soon"}).status == 400
    with Store(home.db_path) as st:
        st.kv_set("note_blocked_until", "2999-01-01T00:00:00")
    ui.post("/api/resume", {})
    after = ui.get("/api/state").json["status"]
    assert after["paused_until"] == "" and after["note_blocked_until"] == "2999-01-01T00:00:00"
    ui.post("/api/resume", {"notes": True})
    assert ui.get("/api/state").json["status"]["note_blocked_until"] == ""


def test_pause_is_capped_at_sixty_days(ui, home):
    ui.post("/api/pause", {"days": 99999})
    until = ui.get("/api/state").json["status"]["paused_until"]
    assert until[:4] < "2100"


@pytest.fixture()
def fake_scheduler(monkeypatch):
    calls = SimpleNamespace(install=[], uninstall=0)

    def install(home, times, **kw):
        calls.install.append(list(times) if not isinstance(times, str) else times)
        return scheduler.SchedResult(True, ["Installed (fake)."])

    def uninstall(home, **kw):
        calls.uninstall += 1
        return scheduler.SchedResult(True, ["Removed (fake)."])

    monkeypatch.setattr(scheduler, "install", install)
    monkeypatch.setattr(scheduler, "uninstall", uninstall)
    monkeypatch.setattr(scheduler, "status", lambda home, **kw: {"installed": bool(calls.install), "platform": "test",
                                                               "mechanism": "fake", "times": [], "message": "fake",
                                                               "details": []})
    return calls


def test_daily_runs_cannot_be_turned_on_before_the_setup_is_ready(ui, fake_scheduler):
    reply = ui.post("/api/schedule", {"action": "install"})
    assert reply.status == 400 and "Not ready" in reply.json["error"] and fake_scheduler.install == []


def test_turning_daily_runs_on_and_off(ui, fake_scheduler):
    cfg = save_ready(ui)
    on = ui.post("/api/schedule", {"action": "install"}).json
    assert on["ok"] is True and on["messages"] == ["Installed (fake)."] and on["schedule"]["installed"] is True
    assert fake_scheduler.install[-1] == cfg["schedule"]["times"]
    ui.post("/api/schedule", {"action": "install", "times": "07:00, 19:30"})
    assert fake_scheduler.install[-1] == ["07:00", "19:30"]
    assert ui.post("/api/schedule", {"action": "uninstall"}).json["ok"] is True and fake_scheduler.uninstall == 1
    assert ui.post("/api/schedule", {"action": "explode"}).status == 400


def test_the_health_check_reports_the_real_state(ui, home):
    data = ui.get("/api/doctor").json
    assert data["summary"]["healthy"] is False                                       # a fresh home is not ready
    assert all({"id", "status", "title", "detail", "fix"} <= set(c) for c in data["checks"])
    ready = next(c for c in data["checks"] if c["id"] == "ready")
    assert ready["status"] == "fail" and "first name" in ready["detail"]
    assert data["summary"]["fail"] >= 1 and data["summary"]["ok"] >= 1


def test_history_and_the_csv_download(ui, home):
    assert ui.get("/api/history").json == {"runs": [], "invites": []}
    with Store(home.db_path) as st:
        st.record({"linkedin_url": "https://www.linkedin.com/in/evil/", "full_name": '=HYPERLINK("http://x","hi")',
                   "company": "+cmd|' /C calc'!A0"}, "sent", note="Hi there", detail="")
        run_id = st.start_run("scheduled")
        st.finish_run(run_id, 1, 1, "run limit reached")
    history = ui.get("/api/history").json
    assert history["runs"][0]["stop_reason"] == "run limit reached" and history["invites"][0]["status"] == "sent"
    export = ui.get("/api/export.csv")
    assert export.status == 200 and export.headers["Content-Type"].startswith("text/csv")
    assert "attachment" in export.headers["Content-Disposition"]
    row = next(csv.DictReader(io.StringIO(export.text)))
    assert row["full_name"].startswith("'=") and row["company"].startswith("'+")       # shown, never executed
    assert row["detail"] == "" and row["note"] == "Hi there"                           # empty stays empty


# ============================================================================= background jobs
def test_a_job_streams_its_log_and_reports_the_result(ui, monkeypatch):
    save_ready(ui)
    seen = {}

    def fake_send(home, cfg, opts):
        seen["opts"] = opts
        opts.log("opening LinkedIn")
        opts.log("two profiles looked fine")
        return runner.RunResult(exit_code=0, sent=2, attempted=2, stop_reason="run limit reached", dry_run=True)

    monkeypatch.setattr(runner, "run_send", fake_send)
    started = ui.post("/api/job/start", {"kind": "dry_run"}).json
    assert started["kind"] == "dry_run" and started["state"] in ("running", "finished")
    done = wait_job(ui)
    assert done["job"]["state"] == "finished" and done["job"]["result"]["sent"] == 2
    assert done["lines"] == ["opening LinkedIn", "two profiles looked fine"] and done["next"] == 2
    assert ui.get("/api/job?since=1").json["lines"] == ["two profiles looked fine"]
    assert ui.get("/api/job?since=2").json["lines"] == []
    assert seen["opts"].dry_run is True and seen["opts"].limit == 2                  # a dry run looks at two profiles
    assert seen["opts"].scheduled is False


def test_every_job_needs_the_safety_note_and_a_sending_job_needs_a_ready_setup(ui, monkeypatch):
    monkeypatch.setattr(runner, "run_send", lambda *a, **k: pytest.fail("must not run"))
    monkeypatch.setattr(runner, "login", lambda *a, **k: pytest.fail("must not run"))
    monkeypatch.setattr(runner, "run_fetch", lambda *a, **k: pytest.fail("must not run"))
    for kind in ("login", "fetch", "dry_run", "run"):
        reply = ui.post("/api/job/start", {"kind": kind, "confirm": True})
        assert reply.status == 400 and "safety note" in reply.json["error"], kind
    ui.post("/api/config", {"config": make_cfg(me={"first_name": ""})})               # risks accepted, no name
    reply = ui.post("/api/job/start", {"kind": "dry_run"})
    assert reply.status == 400 and "Not ready" in reply.json["error"] and "first name" in reply.json["error"]


def test_a_real_send_needs_an_explicit_true(ui, monkeypatch):
    save_ready(ui)
    monkeypatch.setattr(runner, "run_send", lambda *a, **k: pytest.fail("must not run"))
    for confirm in (None, False, "yes", 1, "true"):
        body = {"kind": "run"} if confirm is None else {"kind": "run", "confirm": confirm}
        reply = ui.post("/api/job/start", body)
        assert reply.status == 400 and "confirmation" in reply.json["error"], confirm
    assert ui.post("/api/job/start", {"kind": "teleport"}).status == 400
    assert ui.get("/api/job").json == {"job": None, "lines": [], "next": 0}


def test_the_options_the_page_sends_reach_the_runner_and_the_limit_is_clamped(ui, monkeypatch):
    save_ready(ui)
    seen = {}
    monkeypatch.setattr(runner, "run_send", lambda home, cfg, opts: seen.update(opts=opts) or runner.RunResult())
    ui.post("/api/job/start", {"kind": "run", "confirm": True, "limit": 9999})
    wait_job(ui)
    assert seen["opts"].limit == HARD["per_run_max"] and seen["opts"].dry_run is False
    assert seen["opts"].ignore_hours is False and seen["opts"].ignore_pause is False and seen["opts"].use_ai is True
    seen.clear()
    ui.post("/api/job/start", {"kind": "run", "confirm": True, "limit": -5, "ignore_hours": True, "use_ai": False})
    wait_job(ui)
    assert seen["opts"].limit == 0 and seen["opts"].ignore_hours is True and seen["opts"].use_ai is False
    assert ui.post("/api/job/start", {"kind": "run", "confirm": True, "limit": "lots"}).status == 400


def test_a_request_cannot_lift_a_pause_or_retry_failed_people_and_leaving_the_hours_needs_a_plain_true(ui, monkeypatch):
    save_ready(ui)
    seen = {}
    monkeypatch.setattr(runner, "run_send", lambda home, cfg, opts: seen.update(opts=opts) or runner.RunResult())
    for extra in ({"ignore_pause": True}, {"retry_errors": True}, {"ignore_pause": 1, "retry_errors": "yes"}):
        ui.post("/api/job/start", {"kind": "run", "confirm": True, **extra})
        wait_job(ui)
        assert seen["opts"].ignore_pause is False and seen["opts"].retry_errors is False, extra
    for odd in ("yes", "true", "false", 1, [True], {"a": 1}, 0, None):
        ui.post("/api/job/start", {"kind": "run", "confirm": True, "ignore_hours": odd})
        wait_job(ui)
        assert seen["opts"].ignore_hours is False, odd
    ui.post("/api/job/start", {"kind": "run", "confirm": True, "ignore_hours": True})
    wait_job(ui)
    assert seen["opts"].ignore_hours is True


def test_only_one_job_runs_at_a_time_and_stop_is_cooperative(ui, monkeypatch):
    save_ready(ui)
    started = threading.Event()

    def slow_send(home, cfg, opts):
        started.set()
        while not opts.stop():
            time.sleep(0.01)
        opts.log("noticed the stop button")
        return runner.RunResult(exit_code=0, stop_reason="stopped by you")

    monkeypatch.setattr(runner, "run_send", slow_send)
    assert ui.post("/api/job/start", {"kind": "run", "confirm": True}).status == 200
    assert started.wait(5)
    second = ui.post("/api/job/start", {"kind": "dry_run"})
    assert second.status == 409 and "already running" in second.json["error"]
    state = ui.get("/api/state").json
    assert state["job"]["state"] == "running" and state["busy_elsewhere"] is False
    assert ui.post("/api/job/stop", {}).json == {"stopping": True}
    done = wait_job(ui)
    assert done["job"]["state"] == "stopped" and any("noticed the stop button" in line for line in done["lines"])
    assert ui.post("/api/job/stop", {}).json == {"stopping": False}                 # nothing left to stop
    assert ui.post("/api/job/start", {"kind": "dry_run"}).status == 200            # and a new one may start now
    assert started.wait(5) and ui.post("/api/job/stop", {}).json == {"stopping": True}
    assert wait_job(ui)["job"]["state"] == "stopped"


def test_a_run_that_needs_you_is_reported_as_failed_with_its_reason(ui, monkeypatch):
    save_ready(ui)
    monkeypatch.setattr(runner, "run_send", lambda home, cfg, opts: runner.RunResult(
        exit_code=runner.EXIT_NEEDS_YOU, stop_reason="logged out of LinkedIn"))
    ui.post("/api/job/start", {"kind": "run", "confirm": True})
    done = wait_job(ui)
    assert done["job"]["state"] == "failed" and done["job"]["result"]["stop_reason"] == "logged out of LinkedIn"


def test_a_crashing_job_is_reported_without_a_traceback(ui, monkeypatch, caplog):
    save_ready(ui)

    def boom(home, cfg, opts):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(runner, "run_send", boom)
    ui.post("/api/job/start", {"kind": "dry_run"})
    done = wait_job(ui)
    assert done["job"]["state"] == "failed" and "kaboom" in done["job"]["result"]["error"]
    assert not any("Traceback" in line for line in done["lines"])


def test_login_and_fetch_jobs_call_the_matching_runner_functions(ui, monkeypatch):
    save_ready(ui, sources=[{"type": "csv", "path": "x.csv"}])
    calls, stops, sources = [], [], []
    monkeypatch.setattr(runner, "login", lambda home, cfg, log=None, wait_minutes=0, stop=None: (
        log("sign in please"), calls.append("login"), stops.append(stop), 0)[3])
    monkeypatch.setattr(runner, "run_fetch", lambda home, cfg, only=None, limit=0, log=None, stop=None: (
        calls.append("fetch"), stops.append(stop), sources.append(only), runner.RunResult(sent=5))[3])
    ui.post("/api/job/start", {"kind": "login"})
    first = wait_job(ui)
    assert first["job"]["state"] == "finished" and first["lines"] == ["sign in please"]
    login_job = ui.app.jobs.current
    ui.post("/api/job/start", {"kind": "fetch"})
    second = wait_job(ui)
    assert second["job"]["state"] == "finished" and second["job"]["result"]["added"] == 5
    assert calls == ["login", "fetch"] and sources == [None]
    fetch_job = ui.app.jobs.current
    for job, stop in zip((login_job, fetch_job), stops):                              # each is wired to its own Stop button
        assert callable(stop) and stop() is False
        job.stop.set()
        assert stop() is True


def test_the_source_to_fetch_must_be_text(ui, monkeypatch):
    save_ready(ui, sources=[{"type": "csv", "path": "x.csv"}])
    seen = []
    monkeypatch.setattr(runner, "run_fetch", lambda home, cfg, only=None, limit=0, log=None, stop=None: (
        seen.append(only), runner.RunResult())[1])
    for bad in (["csv"], 5, {"a": 1}, True):
        reply = ui.post("/api/job/start", {"kind": "fetch", "only": bad})
        assert reply.status == 400 and "only must be text" in reply.json["error"], bad
    assert seen == [] and ui.get("/api/job").json["job"] is None
    for given, expected in (("csv", "csv"), ("", None), (None, None)):
        ui.post("/api/job/start", {"kind": "fetch", "only": given})
        wait_job(ui)
        assert seen[-1] == expected


def test_a_secret_never_reaches_the_job_log_however_it_gets_there(ui, monkeypatch):
    save_ready(ui)
    secret = "sk-live-supersecretvalue-98765"
    monkeypatch.setenv("CONNECTO_TEST_KEY", secret)

    def noisy(home, cfg, opts):
        opts.log(f"printed directly: {secret}")
        logging.getLogger("connecto").warning("logged: %s", secret)
        return runner.RunResult()

    monkeypatch.setattr(runner, "run_send", noisy)
    ui.post("/api/job/start", {"kind": "dry_run"})
    done = wait_job(ui)
    assert len(done["lines"]) == 2 and not any(secret in line for line in done["lines"])
    assert all("***" in line for line in done["lines"])


def test_what_a_job_reports_is_cut_down_to_numbers_and_masked_short_text(ui, monkeypatch):
    save_ready(ui)
    secret = "sk-live-supersecretvalue-98765"
    monkeypatch.setenv("CONNECTO_TEST_KEY", secret)
    monkeypatch.setattr(runner, "run_send", lambda home, cfg, opts: runner.RunResult(
        exit_code=runner.EXIT_NEEDS_YOU, stop_reason=f"needs you: the key {secret} was refused " + "x" * 2000))
    ui.post("/api/job/start", {"kind": "dry_run"})
    result = wait_job(ui)["job"]["result"]
    assert result["exit_code"] == runner.EXIT_NEEDS_YOU and result["sent"] == 0
    assert secret not in json.dumps(result) and "the key *** was refused" in result["stop_reason"]
    assert len(result["stop_reason"]) <= srv.LINE_CHARS


def test_a_crash_reports_its_kind_and_message_with_every_key_value_masked(ui, monkeypatch):
    save_ready(ui)
    secret = "sk-live-supersecretvalue-98765"
    monkeypatch.setenv("CONNECTO_TEST_KEY", secret)

    def boom(home, cfg, opts):
        raise RuntimeError(f"cannot reach https://example.test/?key={secret}")

    monkeypatch.setattr(runner, "run_send", boom)
    ui.post("/api/job/start", {"kind": "dry_run"})
    done = wait_job(ui)
    assert done["job"]["state"] == "failed" and done["job"]["result"]["exit_code"] == 1
    assert done["job"]["result"]["error"] == "RuntimeError: cannot reach https://example.test/?key=***"
    assert secret not in json.dumps(done)


def test_clean_result_keeps_only_simple_values():
    nan, inf = float("nan"), float("inf")
    messy = {"exit_code": 3, "sent": 0, "dry_run": True, "stop_reason": "paused", "note": None, "ratio": 0.5, "n": nan, "m": -inf,
             "nested": {"a": 1}, "items": [1, 2], "blob": b"bytes", 7: "seven", "s" * 3: "x" * 1000}
    assert srv.clean_result(messy) == {"exit_code": 3, "sent": 0, "dry_run": True, "stop_reason": "paused", "note": None,
                                       "ratio": 0.5, "7": "seven", "sss": "x" * srv.LINE_CHARS}
    for odd in (None, [], "text", 5, ("a", 1), object()):
        assert srv.clean_result(odd) == {}


def test_what_a_job_prints_is_also_written_to_the_run_log_once(ui, monkeypatch):
    save_ready(ui)
    log, written = logging.getLogger("connecto"), []

    class Collect(logging.Handler):
        def emit(self, record):
            written.append(record.getMessage())

    collector, level = Collect(level=logging.INFO), log.level
    log.addHandler(collector)
    log.setLevel(logging.INFO)

    def send(home, cfg, opts):
        opts.log("printed by the runner")
        opts.log("two\nlines")
        opts.log("   ")                                                                 # (nothing to say)
        log.info("logged directly")
        return runner.RunResult()

    monkeypatch.setattr(runner, "run_send", send)
    try:
        ui.post("/api/job/start", {"kind": "dry_run"})
        done = wait_job(ui)
        assert done["lines"] == ["printed by the runner", "two", "lines", "logged directly"]      # each once on the page
        assert written == ["printed by the runner", "two", "lines", "logged directly"]            # ... and once in the log
    finally:
        log.removeHandler(collector)
        log.setLevel(level)


def test_a_long_or_multi_line_job_message_is_cut_into_short_lines(ui, monkeypatch):
    save_ready(ui)
    monkeypatch.setattr(runner, "run_send", lambda home, cfg, opts: (opts.log("a" * 5000 + "\nshort"), runner.RunResult())[1])
    ui.post("/api/job/start", {"kind": "dry_run"})
    assert wait_job(ui)["lines"] == ["a" * srv.LINE_CHARS, "short"]


def test_a_run_started_elsewhere_is_visible_and_blocks_a_second_one(ui, home):
    from connecto.lock import RunLock
    save_ready(ui)
    with RunLock(home.lock_path):
        assert ui.get("/api/state").json["busy_elsewhere"] is True


def test_quit_stops_the_server(ui):
    assert ui.post("/api/shutdown", {}).json == {"bye": True}
    ui.thread.join(timeout=5)
    assert not ui.thread.is_alive()


# ============================================================================= quitting while something is running
def a_run_that_stops_at_the_end_of_its_profile(monkeypatch, *, profile_seconds: float = 0.2):
    """Stand-in for a send: runs until asked to stop, then needs a moment to finish the profile it is on."""
    started, finished = threading.Event(), threading.Event()

    def slow_send(home, cfg, opts):
        started.set()
        while not opts.stop():
            time.sleep(0.01)
        time.sleep(profile_seconds)
        opts.log("finished the profile in progress")
        finished.set()
        return runner.RunResult(exit_code=0, stop_reason="stopped by you")

    monkeypatch.setattr(runner, "run_send", slow_send)
    return started, finished


@pytest.mark.parametrize("kind,body", [("run", {"confirm": True}), ("dry_run", {}), ("fetch", {})])
def test_quit_never_cuts_a_running_job_off_it_asks_it_to_stop_and_stays_open(ui, monkeypatch, kind, body):
    save_ready(ui, sources=[{"type": "csv", "path": "x.csv"}])
    started, finished = a_run_that_stops_at_the_end_of_its_profile(monkeypatch)

    def slow_fetch(home, cfg, only=None, limit=0, log=None, stop=None):
        started.set()
        while not stop():
            time.sleep(0.01)
        time.sleep(0.2)
        finished.set()
        return runner.RunResult()

    monkeypatch.setattr(runner, "run_fetch", slow_fetch)
    assert ui.post("/api/job/start", {"kind": kind, **body}).status == 200
    assert started.wait(5)
    refused = ui.post("/api/shutdown", {})
    assert refused.status == 409 and "still running" in refused.json["error"] and "Quit again" in refused.json["error"]
    assert kind.replace("_", " ") in refused.json["error"]
    assert ui.thread.is_alive() and ui.get("/api/state").status == 200                 # the page is still there
    assert ui.app.jobs.current.stop.is_set()                                           # ... and the job was asked to stop
    assert finished.wait(5)                                                            # it ended the profile it was on, by itself
    done = wait_job(ui)
    assert done["job"]["state"] == "stopped" and any("Stop requested" in line for line in done["lines"])
    assert ui.post("/api/shutdown", {}).json == {"bye": True}                          # now nothing is running: quitting works
    ui.thread.join(timeout=5)
    assert not ui.thread.is_alive()


def test_quit_while_signing_in_asks_the_sign_in_to_end(ui, monkeypatch):
    save_ready(ui)
    waiting = threading.Event()

    def login(home, cfg, log=None, wait_minutes=0, stop=None):
        waiting.set()
        while not stop():
            time.sleep(0.01)
        log("Not signed in: Sign-in was stopped.")
        return runner.EXIT_NEEDS_YOU

    monkeypatch.setattr(runner, "login", login)
    assert ui.post("/api/job/start", {"kind": "login"}).status == 200
    assert waiting.wait(5)
    refused = ui.post("/api/shutdown", {})
    assert refused.status == 409 and "login" in refused.json["error"]
    done = wait_job(ui)
    assert done["job"]["state"] == "stopped" and any("closing the sign-in window" in line for line in done["lines"])
    assert ui.post("/api/shutdown", {}).json == {"bye": True}


def test_leaving_the_terminal_waits_for_a_running_job_to_stop_at_the_end_of_its_profile(ui, monkeypatch, capsys):
    save_ready(ui)
    started, finished = a_run_that_stops_at_the_end_of_its_profile(monkeypatch, profile_seconds=0.3)
    ui.post("/api/job/start", {"kind": "run", "confirm": True})
    assert started.wait(5)
    assert srv.wait_for_job(ui.app, seconds=10) is True
    assert finished.is_set() and ui.app.jobs.active() is None and ui.app.jobs.current.state == "stopped"
    out = capsys.readouterr().out
    assert "run is still running" in out and "Ctrl+C again leaves at once" in out and "Stopped." in out


def test_leaving_gives_up_waiting_after_a_while_and_says_so(ui, monkeypatch, capsys):
    save_ready(ui)
    release, started = threading.Event(), threading.Event()

    def stubborn(home, cfg, opts):
        started.set()
        release.wait(10)                                  # (does not look at the stop button)
        return runner.RunResult()

    monkeypatch.setattr(runner, "run_send", stubborn)
    ui.post("/api/job/start", {"kind": "run", "confirm": True})
    assert started.wait(5)
    began = time.monotonic()
    assert srv.wait_for_job(ui.app, seconds=0.3) is False
    assert 0.25 < time.monotonic() - began < 3
    assert "had not stopped after 0 seconds - leaving anyway" in capsys.readouterr().out
    release.set()


def test_a_second_ctrl_c_leaves_at_once(ui, monkeypatch, capsys):
    save_ready(ui)
    release, started = threading.Event(), threading.Event()
    monkeypatch.setattr(runner, "run_send", lambda home, cfg, opts: (started.set(), release.wait(10), runner.RunResult())[2])
    ui.post("/api/job/start", {"kind": "run", "confirm": True})
    assert started.wait(5)

    def interrupted(self, seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(srv.JobRunner, "wait", interrupted)
    assert srv.wait_for_job(ui.app, seconds=60) is False
    assert "Leaving now." in capsys.readouterr().out
    release.set()


def test_there_is_nothing_to_wait_for_when_nothing_runs(ui, capsys):
    assert srv.wait_for_job(ui.app, seconds=60) is True and ui.app.jobs.wait(5) is True
    assert capsys.readouterr().out == ""
    assert srv.WAIT_FOR_JOB_SECONDS >= 120                                              # a profile can take a minute or two


def test_ctrl_c_at_the_terminal_lets_a_running_send_finish_its_profile_before_the_program_ends(home, clean_env, monkeypatch, capsys):
    cfgmod_save = __import__("connecto.config", fromlist=["save"])
    cfgmod_save.save(home, cfgmod_save.normalize(make_cfg())[0])
    started, finished = a_run_that_stops_at_the_end_of_its_profile(monkeypatch, profile_seconds=0.3)
    real_make = srv.make_server
    made = {}

    def make_and_interrupt(home_, **kw):
        httpd, app = real_make(home_, **kw)
        made["app"], made["httpd"] = app, httpd

        def serve_forever(poll_interval=0.5):
            app.start_job({"kind": "run", "confirm": True})
            assert started.wait(5)
            raise KeyboardInterrupt                                    # the person presses Ctrl+C in the terminal
        httpd.serve_forever = serve_forever
        return httpd, app

    monkeypatch.setattr(srv, "make_server", make_and_interrupt)
    monkeypatch.setattr(srv, "setup_logging", lambda *a, **k: None)     # (it would change logging for every other test)
    assert srv.serve(home, open_browser=False) == 0
    app = made["app"]
    assert finished.is_set() and app.jobs.active() is None and app.jobs.current.state == "stopped"
    out = capsys.readouterr().out
    assert f"http://127.0.0.1:{app.port}/?t={app.token}" in out and "works once" in out
    assert out.index("Stopping...") < out.index("run is still running") < out.index("Stopped.")


def test_the_printed_link_is_the_only_place_the_link_token_appears(home, clean_env, monkeypatch, capsys):
    monkeypatch.setattr(srv, "setup_logging", lambda *a, **k: None)
    made = {}
    real_make = srv.make_server

    def make_and_stop(home_, **kw):
        httpd, app = real_make(home_, **kw)
        made["app"] = app
        httpd.serve_forever = lambda poll_interval=0.5: None
        return httpd, app

    monkeypatch.setattr(srv, "make_server", make_and_stop)
    assert srv.serve(home, open_browser=False) == 0
    app = made["app"]
    out = capsys.readouterr().out
    assert out.count(app.token) == 1 and app.secret not in out and app.sign_in("nope") is None
    assert app.sign_in(app.token) and app.sign_in(app.token) is None                    # works once
