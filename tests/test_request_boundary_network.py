"""Real loopback HTTP(S) evidence: no production hosts or credentials."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit, parse_qs
import ssl

import pytest
from playwright.sync_api import sync_playwright

from utils.request_boundary import ensure_request_boundary, RequestBoundaryError
from utils.readonly_mutation_guard import ReadonlyMutationGuard, TransactionalMutationPolicy
from utils.site_access import SignedRequestPolicy


@pytest.fixture
def servers():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            path = urlsplit(self.path).path
            seen.append((self.command, path, self.headers.get("Signature"), self.headers.get("Cookie")))
            query = parse_qs(urlsplit(self.path).query)
            if path == "/cross":
                target = f"https://localhost:{tls.server_port}/forbidden"
            elif path == "/downgrade":
                target = f"http://127.0.0.1:{plain.server_port}/forbidden"
            elif path == "/port":
                target = f"https://127.0.0.1:{plain.server_port}/forbidden"
            elif path == "/middle":
                target = "/cross"
            elif path == "/loop":
                target = "/loop"
            elif path.startswith("/chain/"):
                target = "/chain/" + str(int(path.rsplit("/", 1)[1]) + 1)
            elif path == "/invalid":
                target = "javascript:void(0)"
            elif path == "/userinfo":
                target = f"https://dummy@127.0.0.1:{tls.server_port}/forbidden"
            elif path in {"/cart/add.js", "/readonly-hop"}:
                target = "/account/login" if path == "/cart/add.js" else "/cart/add.js"
            elif path in {"/start", "/checkout"}:
                target = query.get("target", ["/landing"])[0]
            else:
                target = None
            status = int(query.get("status", ["302"])[0])
            self.send_response(status if target or path == "/no-location" else 200)
            self.send_header("Content-Type", "text/html")
            if target:
                self.send_header("Location", target)
                self.send_header("Set-Cookie", "synthetic_session=present; Path=/")
            self.end_headers()
            self.wfile.write(b'<form method="post" action="/checkout"><button>Checkout</button></form><img src="relative.png">')

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = respond

    plain = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    tls = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    cert = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    cert.load_cert_chain(Path(__file__).parent / "fixtures/loopback.crt")
    tls.socket = cert.wrap_socket(tls.socket, server_side=True)
    threads = [Thread(target=server.serve_forever, daemon=True) for server in (plain, tls)]
    for thread in threads:
        thread.start()
    yield f"https://127.0.0.1:{tls.server_port}", seen
    for server in (plain, tls):
        server.shutdown()
        server.server_close()
    for thread in threads:
        thread.join()


@pytest.fixture(params=["chromium", "webkit"])
def context(request):
    with sync_playwright() as pw:
        browser = getattr(pw, request.param).launch()
        ctx = browser.new_context(ignore_https_errors=True, service_workers="block")
        yield ctx
        ctx.close()
        browser.close()


def boundary(context, origin, guard=None, *, signed=True):
    transport = ensure_request_boundary(context)
    if signed:
        class FixturePolicy(SignedRequestPolicy):
            def _host_allowed(self, url):
                # Only the fixture's dynamic TLS port; production stays on 443.
                return urlsplit(url).scheme == "https" and urlsplit(url).netloc == urlsplit(origin).netloc
        FixturePolicy({"Signature": "SYNTHETIC"}, ["127.0.0.1"]).attach(context)
    if guard:
        guard.attach(context)
    return transport


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_get_redirect_preserves_final_url_cookie_relative_resources(context, servers, status):
    origin, seen = servers
    transport = boundary(context, origin)
    page = context.new_page()
    transport.navigate(page, lambda: page.goto(f"{origin}/start?status={status}"))
    assert page.url == origin + "/landing"
    page.wait_for_load_state("load")
    assert any(path == "/relative.png" for _, path, _, _ in seen)
    landing = [row for row in seen if row[1] == "/landing"]
    assert len(landing) == 1 and "synthetic_session=present" in landing[0][3]
    transport.assert_clean()


@pytest.mark.parametrize("path", ["cross", "middle", "downgrade", "port", "loop", "invalid", "userinfo", "no-location", "chain/0"])
def test_forbidden_targets_and_loops_stop_without_crashing(context, servers, path):
    origin, seen = servers
    transport = boundary(context, origin)
    page = context.new_page()
    with pytest.raises(RequestBoundaryError):
        transport.navigate(page, lambda: page.goto(f"{origin}/{path}"))
    assert not any(p == "/forbidden" for _, p, _, _ in seen)
    transport.navigate(page, lambda: page.goto(origin + "/"))
    assert page.url == origin + "/"


@pytest.mark.parametrize("status", [301, 302, 303])
def test_real_checkout_post_sent_once(context, servers, status):
    origin, seen = servers
    guard = TransactionalMutationPolicy(["127.0.0.1"], origin)
    transport = boundary(context, origin, guard)
    page = context.new_page()
    transport.navigate(page, lambda: page.goto(origin + "/"))
    page.locator("form").evaluate("(el, status) => el.action='/checkout?status='+status", status)
    from pages.cart_drawer import CartDrawer
    config = {"pages": {"cart": {"selectors": {
        "drawer": {"by": "css", "value": "body"},
        "checkout_button": {"by": "css", "value": "button"},
    }}}}
    CartDrawer(page, config).checkout()
    assert page.url == origin + "/landing"
    assert [(m, p) for m, p, _, _ in seen if m == "POST"] == [("POST", "/checkout")]
    assert "synthetic_session=present" in page.evaluate("document.cookie")
    assert guard.summary()["status"] == "PASS"
    transport.assert_clean()


@pytest.mark.parametrize("signed", [True, False])
@pytest.mark.parametrize("status", [307, 308])
@pytest.mark.parametrize("mode", ["readonly", "full"])
def test_redirected_mutation_never_reaches_prohibited_server(context, servers, signed, status, mode):
    origin, seen = servers
    guard = ReadonlyMutationGuard() if mode == "readonly" else TransactionalMutationPolicy(["127.0.0.1"], origin)
    transport = boundary(context, origin, guard, signed=signed)
    page = context.new_page()
    transport.navigate(page, lambda: page.goto(origin + "/"))
    path = "/readonly-hop" if mode == "readonly" else "/cart/add.js"
    result = page.evaluate("""async url => {
      try { await fetch(url, {method:'POST'}); return 'SENT'; } catch { return 'BLOCKED'; }
    }""", f"{origin}{path}?status={status}")
    assert result == "BLOCKED"
    assert [(m, p) for m, p, _, _ in seen if m == "POST"] == [("POST", path)]
    assert guard.violation_count() == 1 if mode == "readonly" else guard.summary()["high_risk_mutation"] == 1
    with pytest.raises(RequestBoundaryError):
        transport.assert_clean()


@pytest.mark.parametrize("status", [307, 308])
def test_preserved_post_redirect_is_not_replayed(context, servers, status):
    origin, seen = servers
    transport = boundary(context, origin)
    page = context.new_page()
    transport.navigate(page, lambda: page.goto(origin + "/"))
    page.locator("form").evaluate("(el, status) => el.action='/checkout?status='+status", status)
    with pytest.raises(RequestBoundaryError, match="REDIRECT_UNSUPPORTED"):
        transport.navigate(page, lambda: page.get_by_role("button", name="Checkout").click())
    assert [(m, p) for m, p, _, _ in seen if m == "POST"] == [("POST", "/checkout")]
    assert not any(p == "/landing" for _, p, _, _ in seen)


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_all_writing_methods_recheck_the_redirect_target(context, servers, method):
    origin, seen = servers
    guard = ReadonlyMutationGuard()
    transport = boundary(context, origin, guard, signed=False)
    page = context.new_page()
    transport.navigate(page, lambda: page.goto(origin + "/"))
    result = page.evaluate("""async ({method,url}) => {
      try { await fetch(url, {method}); return 'SENT'; } catch { return 'BLOCKED'; }
    }""", {"method": method, "url": origin + "/readonly-hop?status=307"})
    assert result == "BLOCKED" and guard.violation_count() == 1
    assert [(m, p) for m, p, _, _ in seen if m == method] == [(method, "/readonly-hop")]


def test_untrusted_initial_request_strips_signature_case_variants(context, servers):
    origin, seen = servers
    transport = boundary(context, origin)
    context.set_extra_http_headers({"sIgNaTuRe": "UNTRUSTED_SYNTHETIC"})
    page = context.new_page()
    target = origin.replace("127.0.0.1", "localhost") + "/"
    transport.navigate(page, lambda: page.goto(target))
    assert seen and all(signature is None for _, _, signature, _ in seen)


def test_subresource_redirect_fails_without_sending_next_hop(context, servers):
    origin, seen = servers
    transport = boundary(context, origin)
    page = context.new_page()
    transport.navigate(page, lambda: page.goto(origin + "/"))
    status = page.evaluate("async () => { try { await fetch('/start'); return 'SENT'; } catch { return 'BLOCKED'; } }")
    assert status == "BLOCKED"
    assert not any(path == "/landing" for _, path, _, _ in seen)
    with pytest.raises(RequestBoundaryError, match="REDIRECT_UNSUPPORTED"):
        transport.assert_clean()
    event = transport.summary()["events"][0]
    assert event["source_path"] == "/start" and event["target_path"] == "/landing"
    assert event["resource_type"] == "fetch" and event["http_status"] == 302
    assert "SYNTHETIC" not in str(transport.summary())


def test_legacy_signed_api_read_cannot_follow_redirects(context, servers, monkeypatch):
    from types import SimpleNamespace
    from scripts import run_smoke_cases as legacy
    origin, seen = servers
    transport = boundary(context, origin)
    monkeypatch.setattr(legacy, "CART_JS", origin + "/cross")
    runtime = SimpleNamespace(context=context, access_policy=transport.access_policy)
    assert legacy.residual_cart_items(runtime) == -1
    assert [row[1] for row in seen] == ["/cross"]


def test_popup_redirect_is_coordinated_without_replaying_the_click(context, servers):
    origin, seen = servers
    transport = boundary(context, origin)
    page = context.new_page()
    transport.navigate(page, lambda: page.goto(origin + "/"))
    page.set_content('<a href="/start" target="_blank">Product</a>')
    transport.navigate(page, lambda: page.get_by_role("link", name="Product").click())
    assert len(context.pages) == 2 and context.pages[-1].url == origin + "/landing"
    assert len([row for row in seen if row[1] == "/start"]) == 1
    transport.assert_clean()
