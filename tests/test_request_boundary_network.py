"""Real loopback HTTP(S) evidence: no production hosts or credentials."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit, parse_qs, urlencode
import ssl

import pytest
from playwright.sync_api import sync_playwright

from utils.request_boundary import ensure_request_boundary, RequestBoundaryError
from utils.readonly_mutation_guard import ReadonlyMutationGuard, TransactionalMutationPolicy
from utils.site_access import SignedRequestPolicy


@pytest.fixture
def servers():
    class RequestLog(list):
        bridge_headers = None
        def __init__(self):
            super().__init__()
            self.posts = []
    seen = RequestLog()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            path = urlsplit(self.path).path
            seen.append((self.command, path, self.headers.get("Signature"), self.headers.get("Cookie")))
            if self.command == "POST":
                seen.posts.append((path, {k.lower(): v for k,v in self.headers.items()}))
            query = parse_qs(urlsplit(self.path).query)
            if path == "/products/bridge":
                target = None if "synthetic_session=present" in (self.headers.get("Cookie") or "") else (
                    f"https://localhost:{tls.server_port}/accounts/sur?" + urlencode({
                        "return_to": f"https://127.0.0.1:{tls.server_port}{self.path}",
                        "mode": query.get("mode", ["ok"])[0],
                    }))
            elif path == "/accounts/sur":
                seen.bridge_headers = {k.lower(): v for k, v in self.headers.items()}
                mode = query.get("mode", ["ok"])[0]
                source = query["return_to"][0]
                target = {"account": "/account/login", "different": "/products/different",
                          "loop": self.path, "terminal": None,
                          "cross": "https://example.invalid/forbidden",
                          "append": source + "&shop_return=OPAQUE_SECRET",
                          "overwrite": source.replace("mode=overwrite", "mode=changed"),
                          "duplicate": source + "&mode=changed"}.get(mode, source)
            elif path == "/cross":
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
            elif path == "/measurement/conversion":
                target = query.get("target", ["/measurement/1p-conversion/"])[0]
            elif path == "/pagead/viewthroughconversion/123456789/":
                target = "/pagead/1p-conversion/123456789/"
            elif path in {"/cart/add.js", "/readonly-hop"}:
                target = "/account/login" if path == "/cart/add.js" else "/cart/add.js"
            elif path in {"/start", "/checkout", "/localization"}:
                target = query.get("target", ["/landing"])[0]
                if path == "/localization":
                    target = self.headers.get("X-Synthetic-Target") or query.get("target", ["/products/example"])[0]
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


def enable_fixture_shop_bridge(monkeypatch, origin):
    from utils import request_boundary
    bridge_origin = origin.replace("127.0.0.1", "localhost")
    monkeypatch.setattr(request_boundary.RequestBoundary, "_new_bridge_context",
                        lambda self: self.context.browser.new_context(ignore_https_errors=True))
    monkeypatch.setattr(request_boundary, "is_shop_bridge_target", lambda url:
                        urlsplit(url).scheme == "https" and urlsplit(url).netloc == urlsplit(bridge_origin).netloc
                        and urlsplit(url).path == "/accounts/sur")


def test_unsigned_initial_post_clears_signing_context_defaults(context, servers):
    origin, seen = servers
    transport = boundary(context, origin)
    context.set_extra_http_headers({"sIgNaTuRe": "SECRET", "Signature-Input": "SECRET", "Signature-Agent": "SECRET"})
    page = context.new_page()
    transport.navigate(page, lambda: page.goto(origin + "/"))
    page.evaluate("async url => (await fetch(url,{method:'POST'})).status", origin.replace("127.0.0.1", "localhost") + "/")
    assert len(seen.posts) == 1 and seen.posts[0][0] == "/"
    for header in ("signature", "signature-input", "signature-agent"):
        assert not seen.posts[0][1].get(header)


@pytest.mark.parametrize("mode", ["ok", "append"])
def test_shop_bridge_omits_all_credentials_and_returns_only_to_source(context, servers, monkeypatch, mode):
    origin, seen = servers
    enable_fixture_shop_bridge(monkeypatch, origin)
    context.add_cookies([{"name": "shop_private", "value": "SECRET", "url": origin.replace("127.0.0.1", "localhost")}])
    context.set_extra_http_headers({"Signature-Input": "SECRET", "Signature-Agent": "SECRET", "Authorization": "SECRET"})
    transport = boundary(context, origin, ReadonlyMutationGuard())
    page = context.new_page()
    source = origin + "/products/bridge?source=private&mode=" + mode
    transport.navigate(page, lambda: page.goto(source))
    assert page.url == source + ("&shop_return=OPAQUE_SECRET" if mode == "append" else "")
    assert page.get_by_role("button", name="Checkout").count() == 1
    assert seen.bridge_headers is not None
    for header in ("signature", "signature-input", "signature-agent", "authorization", "referer", "origin"):
        assert header not in seen.bridge_headers
    assert not seen.bridge_headers.get("cookie")
    assert [(m,p) for m,p,_,_ in seen if p == "/accounts/sur"] == [("GET", "/accounts/sur")]
    assert len([row for row in seen if row[1] == "/products/bridge"]) == 2
    transport.assert_clean()
    report = str(transport.summary())
    assert "SECRET" not in report and "private" not in report
    assert transport.summary()["bridge_events"][0]["response_status"] == 302
    assert transport.summary()["bridge_events"][0]["return_accepted"] is True


@pytest.mark.parametrize("mode", ["account", "different", "cross", "loop", "terminal", "overwrite", "duplicate"])
def test_shop_bridge_cannot_leave_the_exact_return_or_render_shop(context, servers, monkeypatch, mode):
    origin, seen = servers
    enable_fixture_shop_bridge(monkeypatch, origin)
    transport = boundary(context, origin)
    page = context.new_page()
    with pytest.raises(RequestBoundaryError, match="REDIRECT_ORIGIN_DENIED"):
        transport.navigate(page, lambda: page.goto(origin + "/products/bridge?mode=" + mode))
    assert not any(p in {"/account/login", "/products/different", "/forbidden"} for _,p,_,_ in seen)
    assert page.get_by_role("button", name="Checkout").count() == 0
    assert len([row for row in seen if row[1] == "/accounts/sur"]) == 1


@pytest.mark.parametrize("url,allowed", [
    ("https://shop.app/accounts/sur?opaque=secret", True),
    ("https://shop.app:443/accounts/sur", True),
    ("http://shop.app/accounts/sur", False),
    ("https://shop.app:444/accounts/sur", False),
    ("https://user@shop.app/accounts/sur", False),
    ("https://shop.app.attacker.invalid/accounts/sur", False),
    ("https://shop.app/accounts/login", False),
    ("https://shop.app/accounts/sur/", False),
    ("https://shop.app/accounts/sur#fragment", False),
    ("https://shop.app/accounts/sur?bad= ", False),
])
def test_shop_bridge_exact_endpoint(url, allowed):
    from utils.request_boundary import is_shop_bridge_target
    assert is_shop_bridge_target(url) is allowed


@pytest.mark.parametrize("source,target,allowed", [
    ("/products/a?variant=1", "/products/a?variant=1&opaque=secret", True),
    ("/products/a?variant=1", "/products/a?opaque=secret", False),
    ("/products/a?variant=1", "/products/a?variant=2", False),
    ("/products/a?variant=1", "/products/a?variant=1&variant=2", False),
    ("/products/a", "/products/b?opaque=secret", False),
    ("/cart", "/cart?opaque=secret", False),
    ("/account/login", "/account/login?opaque=secret", False),
    ("/products/a", "/products/a?opaque=secret#fragment", False),
    ("/products/a", "/products/a?opaque=%0Asecret", False),
    ("/products/a", "/products/a?bad%20name=secret", False),
])
def test_shop_return_is_same_endpoint_with_preserved_unambiguous_query(source,target,allowed):
    from utils.request_boundary import is_shop_storefront_return
    assert is_shop_storefront_return("https://store.invalid"+source, "https://store.invalid"+target) is allowed


def test_resumed_navigation_errors_do_not_expose_return_query(context, servers, monkeypatch):
    origin, seen = servers
    transport = boundary(context, origin)
    page = context.new_page()
    original_goto = page.goto
    def failing_goto(url, **kwargs):
        if urlsplit(url).path == "/landing":
            raise RuntimeError("navigation failed at " + url)
        return original_goto(url, **kwargs)
    monkeypatch.setattr(page, "goto", failing_goto)
    with pytest.raises(RequestBoundaryError, match="RESUMED_NAVIGATION_FAILED") as error:
        transport.navigate(page, lambda: page.goto(origin + "/start?" + urlencode({"target":"/landing?opaque=SECRET"})))
    assert "SECRET" not in str(error.value) and "SECRET" not in str(transport.summary())
    assert not any(path == "/landing" for _,path,_,_ in seen)


@pytest.mark.parametrize("completion", ["next_ui", "suite_end"])
def test_script_navigation_is_finished_without_replaying_next_ui(context, servers, completion):
    origin, seen = servers
    transport = boundary(context, origin, ReadonlyMutationGuard())
    page = context.new_page()
    transport.navigate(page, lambda: page.goto(origin + "/"))
    page.evaluate("url => { location.href=url; }", origin + "/start")
    for _ in range(100):
        if transport.pending:
            break
        page.wait_for_timeout(20)
    assert set(transport.pending) == {page}
    actions = []
    if completion == "next_ui":
        def action():
            actions.append("CLICK")
            return page.goto(origin + "/")
        transport.navigate(page, action)
        assert actions == ["CLICK"] and page.url == origin + "/"
    else:
        from utils.request_boundary import complete_request_navigation
        complete_request_navigation(context)
        assert page.url == origin + "/landing"
    assert len([row for row in seen if row[1] == "/start"]) == 1
    assert len([row for row in seen if row[1] == "/landing"]) == 1
    assert "synthetic_session=present" in page.evaluate("document.cookie")
    transport.assert_clean()


@pytest.mark.parametrize('status',[302,303])
@pytest.mark.parametrize('signed',[False,True])
@pytest.mark.parametrize('family',['measurement','pagead'])
def test_verified_conversion_return_is_discarded_without_a_second_request(context,servers,monkeypatch,status,signed,family):
    from utils import request_boundary
    origin,seen=servers
    original=request_boundary.is_verified_conversion_return
    # Preserve the real protocol checks; replace only this TLS fixture's origin.
    def fixture_conversion(request,status,target):
        from types import SimpleNamespace
        if not request.url.startswith(origin+'/') or not target.startswith(origin+'/'):
            return False
        source_host='https://googleads.g.doubleclick.net' if family=='pagead' else 'https://www.google.com'
        proxy=SimpleNamespace(method=request.method,resource_type=request.resource_type,
            is_navigation_request=request.is_navigation_request,frame=request.frame,
            url=request.url.replace(origin,source_host,1))
        return original(proxy,status,target.replace(origin,'https://www.google.com',1))
    monkeypatch.setattr(request_boundary,'is_verified_conversion_return',fixture_conversion)
    transport=boundary(context,origin,ReadonlyMutationGuard(),signed=signed)
    page=context.new_page()
    transport.navigate(page,lambda:page.goto(origin+'/'))
    page.set_content('<iframe src="/frame"></iframe>')
    frame=page.frames[-1]
    frame.wait_for_load_state('domcontentloaded')
    source_path='/pagead/viewthroughconversion/123456789/' if family=='pagead' else '/measurement/conversion'
    target_path='/pagead/1p-conversion/123456789/' if family=='pagead' else '/measurement/1p-conversion/'
    result=frame.evaluate("""async url=>{try {await fetch(url,{method:'POST',body:'opaque=SECRET'});return 'SENT'}catch{return 'BLOCKED'}}""",
                          origin+source_path+'?opaque=SECRET&status='+str(status))
    assert result=='BLOCKED'
    assert [(m,p) for m,p,_,_ in seen if m=='POST']==[('POST',source_path)]
    assert not any(p==target_path for _,p,_,_ in seen)
    if signed:
        with pytest.raises(RequestBoundaryError,match='REDIRECT_UNSUPPORTED'):
            transport.assert_clean()
    else:
        transport.assert_clean()
    assert transport.summary()['events'][0]['code']==('REDIRECT_UNSUPPORTED' if signed else 'GOOGLE_CONVERSION_RETURN_READ_SUPPRESSED')
    assert 'SECRET' not in str(transport.summary())


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


@pytest.mark.parametrize("mode", ["readonly", "full"])
def test_verified_localization_post_is_not_replayed_or_followed(context, servers, mode):
    origin, seen = servers
    guard = ReadonlyMutationGuard() if mode == "readonly" else TransactionalMutationPolicy(["127.0.0.1"], origin)
    transport = boundary(context, origin, guard)
    page = context.new_page()
    transport.navigate(page, lambda: page.goto(origin + "/"))
    status = page.evaluate("""async origin => {
      const body=new FormData();
      for (const [k,v] of Object.entries({form_type:'localization',_method:'put',country_code:'US',
        utf8:'\u2713',return_to:origin+'/products/example'})) body.append(k,v);
      try {
        const response = await fetch('/localization', {method:'POST',body});
        if (response.ok) document.body.innerHTML = await response.text();
        return 'SENT';
      }
      catch { return 'BLOCKED'; }
    }""", origin)
    assert status == "BLOCKED"
    assert page.get_by_role("button", name="Checkout").count() == 1
    assert [(m,p) for m,p,_,_ in seen if m == "POST"] == [("POST", "/localization")]
    assert not any(p == "/products/example" for _,p,_,_ in seen)
    assert "synthetic_session=present" in page.evaluate("document.cookie")
    transport.assert_clean()
    event = transport.summary()["events"][0]
    assert event["code"] == "LOCALIZATION_RETURN_READ_SUPPRESSED" and event["gating_failure"] is False


@pytest.mark.parametrize("target", ["/account/login", "/products/different"])
def test_localization_cannot_suppress_an_unmatched_or_high_risk_return(context, servers, target):
    origin, seen = servers
    transport = boundary(context, origin, ReadonlyMutationGuard())
    page = context.new_page()
    transport.navigate(page, lambda: page.goto(origin + "/"))
    status = page.evaluate("""async ({origin,target}) => {
      const body=new FormData();
      for (const [k,v] of Object.entries({form_type:'localization',_method:'put',country_code:'US',
        utf8:'\u2713',return_to:origin+'/products/example'})) body.append(k,v);
      try { await fetch('/localization', {method:'POST',body,headers:{'X-Synthetic-Target':target}}); return 'SENT'; }
      catch { return 'BLOCKED'; }
    }""", {"origin":origin,"target":target})
    assert status == "BLOCKED" and not any(p == target for _,p,_,_ in seen)
    with pytest.raises(RequestBoundaryError):
        transport.assert_clean()
