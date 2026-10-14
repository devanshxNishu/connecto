"""The guard in conftest.py: tests can reach servers on this machine and nothing else.

This matters because the product drives a real LinkedIn account. The suite proves its own promise here: if the guard
ever stops working, these tests fail before a careless test (or contribution) can touch a real service.
"""
import http.server
import socket
import threading

import pytest
import requests


@pytest.fixture()
def local_server():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"hello from this machine"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()
    server.server_close()
    thread.join(5)


def test_servers_on_this_machine_still_work(local_server):
    assert requests.get(local_server, timeout=5).text == "hello from this machine"
    assert requests.get(local_server.replace("127.0.0.1", "localhost"), timeout=5).status_code == 200


@pytest.mark.parametrize("host", ["www.linkedin.com", "linkedin.com", "example.com", "generativelanguage.googleapis.com"])
def test_looking_up_a_real_name_fails_loudly(host):
    with pytest.raises(AssertionError, match="tests must only talk to servers on this machine"):
        socket.getaddrinfo(host, 443)
    with pytest.raises(AssertionError, match="tests must only talk to servers on this machine"):
        requests.get(f"https://{host}/", timeout=2)


@pytest.mark.parametrize("address", ["93.184.216.34", "10.0.0.5", "8.8.8.8"])
def test_connecting_to_a_real_address_fails_loudly(address):
    with pytest.raises(AssertionError, match="tests must only talk to servers on this machine"):
        socket.create_connection((address, 80), timeout=1)


@pytest.mark.browser
def test_the_test_browser_cannot_leave_this_machine_either(local_server, tmp_path):
    from playwright.sync_api import Error, sync_playwright

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(tmp_path / "profile"), headless=True)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(local_server)
            assert "hello from this machine" in page.inner_text("body")
            for url in ("https://www.linkedin.com/", "https://example.com/", "http://93.184.216.34/"):
                with pytest.raises(Error, match="ERR_NAME_NOT_RESOLVED|ERR_NAME_RESOLUTION_FAILED|ERR_CONNECTION|ERR_ADDRESS"):
                    page.goto(url, timeout=10_000)
        finally:
            ctx.close()
