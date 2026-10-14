"""Bounded policy for the public Webrex localization request constructor.

No test sends traffic to a storefront. Browser requests terminate at an
in-memory route after passing through the real mutation policy.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from utils.mutation_fingerprint import format_mutation_fingerprint_report
from utils.readonly_mutation_guard import ReadonlyMutationGuard, TransactionalMutationPolicy


ORIGIN = "https://mondressy.com"
URL = ORIGIN + "/localization?isCallFromWB=true"
REASON = "MONDRESSY_WEBREX_LOCALIZATION_CONTEXT"


def _fields():
    # Public ws-currencyconverter.js u(): FormData, not a customer payload.
    return [
        ("form_type", "localization"),
        ("_method", "put"),
        ("return_to", ORIGIN + "/products/example?_pos=1&_ss=r"),
        ("country_code", "US"),
        ("utf8", "\u2713"),
    ]


def _multipart(fields, boundary="----WebKitFormBoundaryOfflineContract"):
    parts = []
    for name, value in fields:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
            f"\r\n\r\n{value}\r\n"
        )
    return ("".join(parts) + f"--{boundary}--\r\n").encode("utf-8")


class Route:
    def __init__(self, url=URL, method="POST", fields=None):
        self.calls = []
        self.request = SimpleNamespace(
            url=url,
            method=method,
            post_data_buffer=_multipart(_fields() if fields is None else fields),
            header_value=lambda name: "multipart/form-data; boundary=----WebKitFormBoundaryOfflineContract",
        )

    def fallback(self):
        self.calls.append("fallback")

    def abort(self):
        self.calls.append("abort")


def _policy(origin=ORIGIN):
    return TransactionalMutationPolicy(
        ("mondressy.com", "www.mondressy.com", "lavetir.com", "shirees.com"),
        canonical_origin=origin,
    )


@pytest.mark.parametrize("language", [None, "en", "pt-BR", "zh-Hans"])
def test_exact_webrex_localization_is_allowed_and_audited(language):
    fields = _fields() + ([("language_code", language)] if language else [])
    route = Route(fields=fields)
    policy = _policy()
    policy.set_scope("desktop", "direct", "WSMOKE-DIRECT-01", "CASE")
    policy._handle_route(route)
    assert route.calls == ["fallback"]
    event = policy.events()[0]
    assert event["classification"] == "EXPECTED_MUTATION"
    assert event["reason"] == REASON
    assert event["allowed_to_send"] is True
    assert event["blocked"] is False
    diagnostic = event["localization_query_diagnostic"]
    assert diagnostic["recognized_query_key_names"] == ["iscallfromwb"]
    assert diagnostic["query_parameter_count"] == 1
    assert diagnostic["unknown_query_key_count"] == 0
    assert policy.summary()["status"] == "PASS"


@pytest.mark.parametrize("url", [
    "http://mondressy.com/localization?isCallFromWB=true",
    "https://www.mondressy.com/localization?isCallFromWB=true",
    "https://mondressy.com:444/localization?isCallFromWB=true",
    "https://mondressy.com:443/localization?isCallFromWB=true",
    "https://user:secret@mondressy.com/localization?isCallFromWB=true",
    "https://mondressy.com/LOCALIZATION?isCallFromWB=true",
    "https://mondressy.com/localization/?isCallFromWB=true",
    "https://mondressy.com/localization/other?isCallFromWB=true",
    "https://mondressy.com/%6cocalization?isCallFromWB=true",
    URL + "#fragment",
    URL + "&unknown=secret",
    URL + "&isCallFromWB=true",
    URL + "&",
    URL + "=extra",
    URL.replace("=true", "=false"),
    URL.replace("=true", "=TRUE"),
    URL.replace("=true", "=1"),
    URL.replace("=true", "="),
    URL.replace("=true", "=%74rue"),
    URL.replace("isCallFromWB", "%69sCallFromWB"),
    URL.replace("isCallFromWB", "iscallfromwb"),
    URL.replace("mondressy.com", "lavetir.com"),
    URL.replace("mondressy.com", "shirees.com"),
])
def test_localization_query_permission_has_exact_url_boundary(url):
    route = Route(url=url)
    policy = _policy()
    policy._handle_route(route)
    assert route.calls == ["abort"]
    assert policy.summary()["status"] == "FAIL"
    assert policy.summary()["unexpected_mutation"] == 1


@pytest.mark.parametrize("origin", [None, "https://www.mondressy.com", "http://mondressy.com", "https://lavetir.com"])
def test_permission_requires_confirmed_mondressy_canonical_origin(origin):
    policy = _policy(origin)
    route = Route()
    policy._handle_route(route)
    assert route.calls == ["abort"]


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_webrex_flag_does_not_allow_other_mutation_methods(method):
    route = Route(method=method)
    policy = _policy()
    policy._handle_route(route)
    assert route.calls == ["abort"]


@pytest.mark.parametrize("field,value", [
    ("form_type", "customer"), ("_method", "delete"),
    ("country_code", "USA"), ("country_code", "us"),
    ("country_code", "US&token=secret"), ("utf8", "invalid"),
    ("return_to", "https://outside.example/products/example"),
    ("return_to", "//outside.example/products/example"),
    ("return_to", "https://user:secret@mondressy.com/products/example"),
    ("return_to", "http://mondressy.com/products/example"),
    ("return_to", ORIGIN + "/account"),
    ("return_to", ORIGIN + "/checkout"),
    ("return_to", ORIGIN + "/products/../account"),
    ("return_to", ORIGIN + "/products/%2e%2e/account"),
    ("return_to", ORIGIN + "/products/example\\evil"),
])
def test_localization_form_cannot_change_operation_or_redirect_boundary(field, value):
    fields = [(k, value if k == field else v) for k, v in _fields()]
    route = Route(fields=fields)
    policy = _policy()
    policy._handle_route(route)
    assert route.calls == ["abort"]


@pytest.mark.parametrize("fields", [
    _fields()[1:],
    _fields() + [("country_code", "GB")],
    _fields() + [("token-secret-name", "secret-value")],
    _fields() + [("language_code", "en<script>")],
])
def test_missing_duplicate_or_unknown_form_fields_stay_blocked(fields):
    route = Route(fields=fields)
    policy = _policy()
    policy._handle_route(route)
    assert route.calls == ["abort"]
    report = json.dumps(policy.summary()) + "\n".join(format_mutation_fingerprint_report(policy.summary()))
    assert "secret-name" not in report
    assert "secret-value" not in report
    assert "<script>" not in report


@pytest.mark.parametrize("body", [None, b"", b"broken", b"x" * 16385])
def test_unreadable_or_malformed_body_fails_closed(body):
    route = Route()
    route.request.post_data_buffer = body
    policy = _policy()
    policy._handle_route(route)
    assert route.calls == ["abort"]


def test_form_read_error_never_leaks_or_sends_request():
    route = Route()
    def unreadable(_name):
        raise RuntimeError("sensitive-body-or-header")
    route.request.header_value = unreadable
    policy = _policy()
    policy._handle_route(route)
    assert route.calls == ["abort"]
    assert "sensitive" not in json.dumps(policy.summary())


@pytest.mark.parametrize("kind", ["truncated", "duplicate-header", "duplicate-name", "file", "bad-utf8", "nested", "bad-boundary"])
def test_abnormal_multipart_encoding_is_not_treated_as_app_form(kind):
    route = Route()
    body = route.request.post_data_buffer
    if kind == "truncated":
        body = body.rsplit(b"------WebKitFormBoundaryOfflineContract--", 1)[0]
    elif kind == "duplicate-header":
        body = body.replace(b'form-data; name="country_code"',
                            b'form-data; name="country_code"\r\nContent-Disposition: form-data; name="token"')
    elif kind == "duplicate-name":
        body = body.replace(b'name="country_code"', b'name="country_code"; name="token"')
    elif kind == "file":
        body = body.replace(b'name="country_code"', b'name="country_code"; filename="private.txt"')
    elif kind == "bad-utf8":
        body = body.replace(b"\r\nUS\r\n", b"\r\n\xff\r\n")
    elif kind == "nested":
        body = body.replace(b'name="country_code"',
                            b'name="country_code"\r\nContent-Type: multipart/mixed; boundary=inner')
    else:
        route.request.header_value = lambda _name: "multipart/form-data; boundary=missing"
    route.request.post_data_buffer = body
    policy = _policy()
    policy._handle_route(route)
    assert route.calls == ["abort"]
    assert policy.summary()["unexpected_mutation"] == 1


@pytest.mark.parametrize("url", [
    "https://mondressy.com/account/localization?isCallFromWB=true",
    "https://mondressy.com/payments?isCallFromWB=true",
    "https://paypal.com/localization?isCallFromWB=true",
])
def test_high_risk_still_precedes_localization_permission(url):
    route = Route(url=url)
    policy = _policy()
    policy._handle_route(route)
    assert route.calls == ["abort"]
    assert policy.events()[0]["classification"] == "HIGH_RISK_MUTATION"
    assert policy.summary()["high_risk_mutation"] == 1


def test_readonly_cart_protection_is_unchanged():
    guard = ReadonlyMutationGuard()
    route = Route(url=ORIGIN + "/cart/add.js?isCallFromWB=true")
    guard._handle_route(route)
    assert route.calls == ["abort"]
    assert guard.violation_count() == 1


def test_new_fixed_reason_does_not_allow_arbitrary_schema_reason_values():
    from scripts.validate_result_schema import _validate_mutation_summary
    policy = _policy()
    policy._handle_route(Route())
    summary = policy.summary()
    assert _validate_mutation_summary(summary)
    summary["by_path"][0]["reason"] = "UNRECOGNIZED_REASON"
    assert not _validate_mutation_summary(summary)


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_browser_formdata_uses_real_policy_without_network(engine):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch(headless=True)
        context = browser.new_context()
        sent = []
        def sink(route):
            if route.request.method == "POST":
                sent.append(route.request)
            route.fulfill(status=200, body="<html></html>", content_type="text/html")
        # Final sink makes this test entirely offline. Allowed requests still
        # fall back to previously installed routes, preserving policy order.
        context.route("**/*", sink)
        policy = _policy()
        policy.attach(context)
        page = context.new_page()
        page.goto(ORIGIN + "/products/example")
        page.evaluate("""async () => {
          const data = new FormData();
          data.append('form_type', 'localization'); data.append('_method', 'put');
          data.append('return_to', window.location.href);
          data.append('country_code', 'US'); data.append('utf8', '\u2713');
          await fetch('/localization?isCallFromWB=true', {method:'POST', body:data});
          try { await fetch('/localization?isCallFromWB=true&token=PRIVATE', {method:'POST', body:data}); } catch {}
        }""")
        assert len(sent) == 1
        assert policy.summary()["expected_mutation"] == 1
        assert policy.summary()["unexpected_mutation"] == 1
        assert policy.events()[0]["reason"] == REASON
        from scripts.validate_result_schema import _validate_mutation_summary
        assert _validate_mutation_summary(policy.summary())
        report = json.dumps(policy.summary()) + "\n".join(format_mutation_fingerprint_report(policy.summary()))
        assert "PRIVATE" not in report
        assert "offline-signature" not in report
        assert "return_to" not in report
        browser.close()
