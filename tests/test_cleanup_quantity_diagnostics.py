"""Counts-only diagnostics, real policy, and production schema contract."""
from copy import deepcopy
from types import SimpleNamespace
import json
import pytest
from tests.test_full_cart_cleanup import FakeAPI, cart, policy, clean, SECRET
from utils.cart_cleanup import cleanup_cart_session
from utils.full_cleanup_contract import validate_cleanup_diagnostics
from flows.shopping_flow import ShoppingFlow, FlowError


class BodyAPI(FakeAPI):
    def post(self, url, **kwargs):
        response = super().post(url, **kwargs)
        response.json = lambda: deepcopy(self.cart)
        return response


def test_clear_body_and_fresh_get_counts_are_safe_and_consistent():
    api = BodyAPI(cart(1))
    report = clean(api)
    assert report["status"] == "PASS"
    d = report["diagnostics"]
    assert d["session_binding"] == "MATCH"
    assert d["precheck_item_count"] == d["precheck_items_length"] == 1
    assert d["clear_response_item_count"] == d["postcheck_item_count"] == 0
    assert d["clear_response_items_length"] == d["postcheck_items_length"] == 0
    assert d["clear_target_match"] is True and d["clear_http_status"] == 200
    assert [r["stage"] for r in d["reads"]] == ["precheck", "postcheck"]
    assert all(r["fresh_request_attempted"] for r in d["reads"])
    assert validate_cleanup_diagnostics(report)
    assert SECRET not in json.dumps(report)


def test_200_with_residual_body_and_get_is_still_fail():
    api = BodyAPI(cart(1)); api.residual = True
    report = clean(api)
    assert report["status"] == "FAIL" and report["safe_error_code"] == "CART_RESIDUAL_ITEMS"
    assert report["diagnostics"]["clear_response_item_count"] == 1
    assert report["diagnostics"]["postcheck_item_count"] == 1
    assert validate_cleanup_diagnostics(report)


def test_clear_body_empty_but_readd_observed_is_fail():
    api = BodyAPI(cart(1)); guard = policy(api)
    original_get = api.get
    def get(url, **kwargs):
        if any(c[0] == "POST" for c in api.calls):
            api.cart = cart(1)
            guard._events.append({"allowed_to_send": True, "path": "/cart/add.js"})
        return original_get(url, **kwargs)
    # Freeze the clear response at receipt, like Playwright APIResponse.json.
    original_post = api.post
    def post(url, **kwargs):
        response = original_post(url, **kwargs)
        body = deepcopy(api.cart)
        response.json = lambda: body
        return response
    api.get, api.post = get, post
    report = clean(api, guard)
    assert report["status"] == "FAIL"
    d = report["diagnostics"]
    assert d["clear_response_item_count"] == 0 and d["postcheck_item_count"] == 1
    assert d["known_cart_write_overlap"] == "OBSERVED" and d["known_cart_write_dispatch_delta"] == 1
    assert sum(c[0] == "POST" for c in api.calls) == 1


def test_residual_does_not_assume_unproven_propagation_delay_or_repeat_post():
    api = BodyAPI(cart(1)); api.residual = True
    report = clean(api)
    # A subsequent read becoming empty is possible but unproven at #36.
    # Keep the observed failure; do not mask it with blind retries.
    api.cart = cart()
    assert report["status"] == "FAIL"
    assert [c[0] for c in api.calls] == ["GET", "POST", "GET"]


def test_wrong_api_context_blocks_before_any_request():
    api, other = BodyAPI(cart(1)), BodyAPI()
    report = clean(api, policy(other))
    assert report["status"] == "BLOCKED" and report["safe_error_code"] == "API_SESSION_MISMATCH"
    assert not api.calls and not other.calls
    assert validate_cleanup_diagnostics(report)
    page = SimpleNamespace(context=SimpleNamespace(request=other))
    flow = ShoppingFlow(page, {"base_url": "https://mondressy.com"}, "mobile", api_request=api)
    with pytest.raises(FlowError, match="API_SESSION_MISMATCH"):
        flow._session_api()


@pytest.mark.parametrize("status", [429, 500])
def test_failed_clear_records_status_and_never_claims_empty(status):
    api = BodyAPI(cart(1)); api.post_status = status
    report = clean(api)
    assert report["status"] == "FAIL" and report["diagnostics"]["clear_http_status"] == status
    assert report["diagnostics"]["postcheck_item_count"] is None
    assert validate_cleanup_diagnostics(report)


@pytest.mark.parametrize("field,value", [("api_session_id", SECRET), ("precheck_item_count", True),
                                        ("known_cart_write_overlap", "PASS"), ("version", 999)])
def test_schema_rejects_forged_or_sensitive_diagnostic(field, value):
    report = clean(BodyAPI(cart(1)))
    report["diagnostics"][field] = value
    assert not validate_cleanup_diagnostics(report)


def test_headers_failure_is_safe_and_never_posts():
    api = BodyAPI(cart(1))
    def headers(url):
        if url.endswith("clear.js"):
            raise RuntimeError(SECRET)
        return {}
    report = cleanup_cart_session(api, "https://mondressy.com", headers, policy(api), journey="browse", viewport="mobile")
    assert report["status"] == "BLOCKED" and report["safe_error_code"] == "API_CLEANUP_ACCESS_UNAVAILABLE"
    assert not report["clear_requested"] and SECRET not in json.dumps(report)
