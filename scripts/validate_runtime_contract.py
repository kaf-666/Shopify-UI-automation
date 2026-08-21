"""离线验证 Jenkins Readiness 运行契约。

覆盖不依赖真实站点的关键边界：代理优先级与校验、Signed Request 环境
注入 / exact-host 隔离 / 缺失与过期分类、结果字段与 fatal schema。
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.browser import load_settings, resolve_proxy
from utils.result import CaseResult, RunResult, iso_now
from utils.site_access import (
    SiteAccessError,
    SignedRequestPolicy,
    parse_env_headers,
    validate_signature_headers,
)
from pages.cart_drawer import CartDrawer


def check(ok: bool, label: str) -> bool:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    return ok


def validate_quantity_property_regression() -> bool:
    """验证 CartDrawer 业务读取不会回退到静态 value attribute。"""
    site_config = {
        "base_url": "https://example.invalid",
        "pages": {
            "cart": {
                "url": "/cart",
                "selectors": {"cart_item": {"by": "css", "value": ".cart__item"}},
            }
        },
    }
    browser = None
    playwright = None
    try:
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<div id="CartDrawer"><div class="cart__item">'
            '<input id="qty" name="updates[]" value="1">'
            "</div></div>"
        )
        drawer = CartDrawer(page, site_config, "desktop")
        before = drawer.get_item_quantity(0)
        page.locator("#qty").evaluate("(el) => { el.value = '2'; }")
        value_attribute = page.locator("#qty").get_attribute("value")
        input_value = page.locator("#qty").input_value()
        business_quantity = drawer.get_item_quantity(0)
        return all(
            (
                before == "1",
                value_attribute == "1",
                input_value == "2",
                business_quantity == "2",
            )
        )
    except Exception as exc:  # noqa: BLE001 — validator reports a compact failure
        print(f"  FAIL  quantity property regression: {type(exc).__name__}")
        return False
    finally:
        if browser is not None:
            browser.close()
        if playwright is not None:
            playwright.stop()


def main() -> int:
    ok = True
    settings = load_settings()
    ok = check(resolve_proxy(settings, environ={}) is None, "proxy disabled by default") and ok

    injected = resolve_proxy(
        settings,
        environ={
            "SHOPIFY_PROXY_SERVER": "http://jenkins-proxy:8080",
            "SHOPIFY_PROXY_USERNAME": "ci-user",
            "SHOPIFY_PROXY_PASSWORD": "ci-password",
        },
    )
    ok = check(
        injected == {
            "server": "http://jenkins-proxy:8080",
            "username": "ci-user",
            "password": "ci-password",
        },
        "proxy environment injection",
    ) and ok

    try:
        resolve_proxy(settings, environ={"SHOPIFY_PROXY_SERVER": "not-a-proxy"})
        ok = check(False, "invalid proxy rejected") and ok
    except Exception as exc:
        ok = check(getattr(exc, "category", "") == "PROXY_CONFIG_ERROR", "invalid proxy rejected") and ok

    env = {
        "MONDRESSY_US_SHOPIFY_SIGNATURE": "sig-test-value",
        "MONDRESSY_US_SHOPIFY_SIGNATURE_INPUT": 'sig1=("@authority");expires=4102444800',
        "MONDRESSY_US_SHOPIFY_SIGNATURE_AGENT": '"https://shopify.com"',
    }
    headers = parse_env_headers(env)
    validate_signature_headers(headers)
    policy = SignedRequestPolicy(headers, ["mondressy.com", "www.mondressy.com"], source="env")
    ok = check(bool(policy.request_headers("https://mondressy.com/cart.js")), "exact allowlist host receives Signed Request") and ok
    for url in (
        "https://evil-mondressy.com/cart.js",
        "https://mondressy.com.attacker.com/cart.js",
    ):
        ok = check(not policy.request_headers(url), f"lookalike host rejected: {url.split('/')[2]}") and ok

    try:
        parse_env_headers({})
        ok = check(False, "missing Signed Request env rejected") and ok
    except SiteAccessError as exc:
        ok = check(exc.category == "SIGNED_REQUEST_MISSING", "missing Signed Request env rejected") and ok

    try:
        validate_signature_headers(
            {
                "Signature": "sig",
                "Signature-Input": 'sig1=("@authority");expires=1',
                "Signature-Agent": '"https://shopify.com"',
            }
        )
        ok = check(False, "expired Signed Request rejected") and ok
    except SiteAccessError as exc:
        ok = check(exc.category == "SIGNED_REQUEST_EXPIRED", "expired Signed Request rejected") and ok

    case = CaseResult("FAKE-01", "fake", "FAIL", iso_now(), iso_now(), 1, evidence_capture_error="screenshot capture failed")
    fatal = RunResult(
        run_id="fake",
        site="mondressy",
        base_url="https://mondressy.com",
        started_at=iso_now(),
        finished_at=iso_now(),
        duration_ms=1,
        overall_status="FAIL",
        runtime={},
        summary={"pass": 0, "fail": 1, "blocked": 0, "total": 1},
        fatal_error={"classification": "CONFIG_ERROR", "message": "synthetic"},
    )
    case_dict = case.to_dict()
    fatal_dict = fatal.to_dict()
    ok = check("evidence_capture_error" in case_dict, "evidence_capture_error persists") and ok
    ok = check(fatal_dict["fatal_error"]["classification"] == "CONFIG_ERROR", "fatal_error schema persists") and ok

    ok = check(
        validate_quantity_property_regression(),
        "CartDrawer reads live quantity property when attribute is stale",
    ) and ok

    print(f"Runtime Contract Validation: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
