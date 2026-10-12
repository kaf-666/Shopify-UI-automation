"""Verified, fail-closed cart cleanup using the current browser session.

Only fixed codes and aggregate counts leave this module. No response bodies,
headers or underlying exception messages are included in reports.
"""

from urllib.parse import urlsplit
from uuid import uuid4
import time


class CartStateUnavailable(RuntimeError):
    pass


SAFE_CLEANUP_ERRORS = frozenset({
    "NONE", "CART_STATE_UNVERIFIED", "STOREFRONT_ORIGIN_INVALID",
    "CART_JSON_NOT_OBJECT", "CART_STRUCTURE_INVALID", "CART_LINE_INVALID",
    "CART_QUANTITY_INVALID", "CART_COUNT_INCONSISTENT", "CART_READ_HTTP_UNAVAILABLE",
    "CART_READ_TARGET_CHANGED", "CART_READ_UNAVAILABLE", "API_CLEANUP_POLICY_DENIED",
    "API_CLEANUP_ACCESS_UNAVAILABLE", "API_CLEANUP_RESPONSE_REJECTED",
    "API_CLEANUP_REQUEST_FAILED", "API_CLEANUP_REQUEST_UNAVAILABLE", "CART_RESIDUAL_ITEMS",
    "CLEANUP_REPORT_UNAVAILABLE", "FINAL_CLEANUP_UNAVAILABLE",
    "API_SESSION_MISMATCH",
})


def cleanup_report(journey, viewport):
    return {
        "journey": journey, "viewport": viewport,
        "operation_id": uuid4().hex, "attempt_id": None,
        "precheck_status": "BLOCKED", "cart_item_count": None,
        "clear_requested": False, "api_policy_decision": "NOT_REQUESTED",
        "clear_response_category": "NOT_REQUESTED", "postcheck_status": "NOT_RUN",
        "status": "BLOCKED", "safe_error_code": "CART_STATE_UNVERIFIED",
    }


def _safe_error(error):
    code = str(error)
    return code if code in SAFE_CLEANUP_ERRORS else "CART_READ_UNAVAILABLE"


def validate_cart(data):
    if not isinstance(data, dict):
        raise CartStateUnavailable("CART_JSON_NOT_OBJECT")
    count, items = data.get("item_count"), data.get("items")
    if type(count) is not int or count < 0 or not isinstance(items, list):
        raise CartStateUnavailable("CART_STRUCTURE_INVALID")
    quantities = []
    for item in items:
        if not isinstance(item, dict):
            raise CartStateUnavailable("CART_LINE_INVALID")
        quantity = item.get("quantity")
        if type(quantity) is not int or quantity < 0:
            raise CartStateUnavailable("CART_QUANTITY_INVALID")
        quantities.append(quantity)
    if sum(quantities) != count or (count == 0 and items):
        raise CartStateUnavailable("CART_COUNT_INCONSISTENT")
    return data


def storefront_origin(base):
    try:
        parsed = urlsplit(base)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.path not in {"", "/"}
                or parsed.query or parsed.fragment):
            raise ValueError
        port = parsed.port
        return f"https://{parsed.hostname.lower()}" + (
            f":{port}" if port is not None and port != 443 else ""
        )
    except (TypeError, ValueError):
        raise CartStateUnavailable("STOREFRONT_ORIGIN_INVALID") from None


def read_cart(api, origin, headers):
    url = origin + "/cart.js"
    try:
        response = api.get(url, headers=headers(url), timeout=15000, max_redirects=0)
        if response.status != 200:
            raise CartStateUnavailable("CART_READ_HTTP_UNAVAILABLE")
        # No redirected resource is accepted, even if it returns valid JSON.
        if response.url != url:
            raise CartStateUnavailable("CART_READ_TARGET_CHANGED")
        return validate_cart(response.json())
    except CartStateUnavailable:
        raise
    except Exception:
        raise CartStateUnavailable("CART_READ_UNAVAILABLE") from None


def cleanup_cart_session(api, base, headers, policy, *, journey, viewport, operation_id=None):
    report = cleanup_report(journey, viewport)
    if operation_id is not None:
        report["operation_id"] = operation_id
    started = time.monotonic()
    diagnostic = {
        "version": 1, "api_session_id": f"api-{id(api):x}" if api is not None else None,
        "session_binding": "UNVERIFIED", "precheck_item_count": None,
        "precheck_items_length": None, "clear_request_attempted": False,
        "clear_http_status": None, "clear_target_match": None,
        "clear_response_structure_valid": False, "clear_response_item_count": None,
        "clear_response_items_length": None, "postcheck_item_count": None,
        "postcheck_items_length": None, "reads": [], "post_elapsed_ms": None,
        "known_cart_write_overlap": "UNAVAILABLE", "known_cart_write_dispatch_delta": None,
    }
    report["diagnostics"] = diagnostic
    def elapsed():
        return max(0, int((time.monotonic() - started) * 1000))

    def cart_writes():
        try:
            return sum(e.get("allowed_to_send") is True and e.get("path") in {
                "/cart/add", "/cart/add.js", "/cart/change", "/cart/change.js",
                "/cart/update", "/cart/update.js",
            } for e in policy.events())
        except Exception:
            return None

    writes_before = cart_writes()
    def observed_read(stage):
        begin = elapsed()
        record = {"stage": stage, "fresh_request_attempted": True,
                  "started_ms": begin, "finished_ms": None,
                  "item_count": None, "items_length": None}
        diagnostic["reads"].append(record)
        try:
            value = read_cart(api, origin, headers)
            record.update(item_count=value["item_count"], items_length=len(value["items"]))
            return value
        finally:
            record["finished_ms"] = elapsed()
            writes_after = cart_writes()
            if writes_before is not None and writes_after is not None:
                delta = max(0, writes_after - writes_before)
                diagnostic["known_cart_write_dispatch_delta"] = delta
                diagnostic["known_cart_write_overlap"] = "OBSERVED" if delta else "NOT_OBSERVED"
    try:
        context = policy._context
        if context is None or api is None or context.request is not api:
            raise ValueError
        diagnostic["session_binding"] = "MATCH"
    except Exception:
        diagnostic["session_binding"] = "MISMATCH_OR_UNAVAILABLE"
        report["safe_error_code"] = "API_SESSION_MISMATCH"
        return report
    try:
        origin = storefront_origin(base)
        cart = observed_read("precheck")
    except CartStateUnavailable as exc:
        report["safe_error_code"] = _safe_error(exc)
        return report
    report.update(precheck_status="PASS", cart_item_count=cart["item_count"])
    diagnostic.update(precheck_item_count=cart["item_count"], precheck_items_length=len(cart["items"]))
    if cart["item_count"] == 0:
        report.update(status="PASS", postcheck_status="PASS", safe_error_code="NONE")
        return report
    url = origin + "/cart/clear.js"
    try:
        if policy is None:
            raise RuntimeError
        decision = policy.authorize_api_cleanup(
            url, "POST", operation_id=report["operation_id"], cleanup_stage=journey)
        report["attempt_id"] = policy.api_cleanup_attempt_id()
        if decision != "API_CLEANUP_EXPECTED_MUTATION":
            raise RuntimeError
        report["api_policy_decision"] = decision
    except Exception:
        report.update(api_policy_decision="DENIED", safe_error_code="API_CLEANUP_POLICY_DENIED")
        return report
    try:
        signed_headers = headers(url)
    except Exception:
        report["safe_error_code"] = "API_CLEANUP_ACCESS_UNAVAILABLE"
        return report
    try:
        post = api.post
        if not callable(post):
            raise RuntimeError
    except Exception:
        report["safe_error_code"] = "API_CLEANUP_REQUEST_UNAVAILABLE"
        return report
    try:
        policy.record_api_cleanup_attempt()
        report["clear_requested"] = True
        diagnostic["clear_request_attempted"] = True
        post_started = time.monotonic()
        response = post(url, headers=signed_headers, timeout=15000, max_redirects=0)
        diagnostic["post_elapsed_ms"] = max(0, int((time.monotonic() - post_started) * 1000))
        diagnostic["clear_http_status"] = response.status
        diagnostic["clear_target_match"] = response.url == url
        policy.record_api_cleanup_response(response.status)
        if response.status != 200 or response.url != url:
            report.update(status="FAIL", clear_response_category="HTTP_OR_REDIRECT_FAILURE",
                          safe_error_code="API_CLEANUP_RESPONSE_REJECTED")
            return report
    except Exception:
        report.update(status="FAIL", clear_response_category="REQUEST_FAILED",
                      safe_error_code="API_CLEANUP_REQUEST_FAILED")
        return report
    report["clear_response_category"] = "HTTP_200"
    try:
        cleared = validate_cart(response.json())
        diagnostic.update(clear_response_structure_valid=True,
                          clear_response_item_count=cleared["item_count"],
                          clear_response_items_length=len(cleared["items"]))
    except Exception:
        # An unavailable response body is evidence missing, not evidence empty.
        pass
    try:
        remaining = observed_read("postcheck")
    except CartStateUnavailable as exc:
        report.update(postcheck_status="BLOCKED", safe_error_code=_safe_error(exc))
        return report
    diagnostic.update(postcheck_item_count=remaining["item_count"], postcheck_items_length=len(remaining["items"]))
    if remaining["item_count"] != 0:
        report.update(status="FAIL", postcheck_status="FAIL", safe_error_code="CART_RESIDUAL_ITEMS")
        # One additional READ ONLY observation distinguishes persistent
        # residual from a later empty read. It never retries the POST and
        # never converts the original unverified cleanup into PASS.
        time.sleep(0.2)
        try:
            observed_read("residual_confirmation")
        except CartStateUnavailable:
            pass
        return report
    report.update(status="PASS", postcheck_status="PASS", safe_error_code="NONE")
    return report
