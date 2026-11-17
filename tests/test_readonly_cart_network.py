"""Verify direct cart writes do not reach a real loopback server."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from playwright.sync_api import sync_playwright

from utils.readonly_mutation_guard import ReadonlyMutationGuard


@pytest.fixture
def server():
    posts = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body>synthetic</body></html>")

        def do_POST(self):
            posts.append(self.path)
            self.send_response(200)
            self.end_headers()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}", posts
    httpd.shutdown()
    httpd.server_close()
    thread.join()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_cart_form_locale_and_encoded_writes_are_blocked(engine, server):
    origin, posts = server
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch()
        context = browser.new_context(service_workers="block")
        guard = ReadonlyMutationGuard()
        guard.attach(context)
        page = context.new_page()
        page.goto(origin + "/")
        paths = ["/cart", "/en-us/cart/add.js", "/cart/%61dd.js", "/cart/add%252ejs"]
        for path in paths:
            result = page.evaluate("async path => { try { return (await fetch(path,{method:'POST'})).status; } catch { return 'BLOCKED'; } }", path)
            assert result == "BLOCKED"
        assert posts == [] and guard.violation_count() == len(paths)
        assert page.evaluate("async () => (await fetch('/products/safe',{method:'POST'})).status") == 200
        assert posts == ["/products/safe"]
        context.close()
        browser.close()
