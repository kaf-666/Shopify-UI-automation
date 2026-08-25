"""离线验证 Jenkins Readiness 运行契约。

覆盖不依赖真实站点的关键边界：代理优先级与校验、Signed Request 环境
注入 / exact-host 隔离 / 缺失与过期分类、结果字段与 fatal schema。
"""

from __future__ import annotations

import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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
from pages.product_page import ProductPage, PurchaseAreaReadinessError
from pages.search_page import SearchPage, SearchResultNavigationError
from pages.size_option_resolver import SIZE_MODEL_01, SIZE_MODEL_02


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


def _search_site_config(base_url: str) -> dict:
    return {
        "base_url": base_url,
        "pages": {
            "search": {
                "url": "/search",
                "selectors": {
                    "result_link": {"by": "css", "value": "#result"},
                },
            }
        },
    }


def _search_markup(scenario: str) -> str:
    if scenario == "same_page":
        return '<a id="result" href="/products/test">Product</a>'
    if scenario == "new_page":
        return '<a id="result" href="/products/test" target="_blank">Product</a>'
    if scenario == "invalid_destination":
        return '<a id="result" href="/collections/not-a-product" target="_blank">Invalid</a>'
    return '<button id="result" type="button">No navigation</button>'


class _SyntheticSearchHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 — stdlib handler contract
        parsed = urlparse(self.path)
        scenario = (parse_qs(parsed.query).get("scenario") or ["no_navigation"])[0]
        body = _search_markup(scenario) if parsed.path == "/search" else "<h1>Destination</h1>"
        payload = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *_args) -> None:
        return


def _run_search_scenario(browser, scenario: str, base_url: str) -> bool:
    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(
            f"{base_url}/search?scenario={scenario}",
            wait_until="domcontentloaded",
        )
        search = SearchPage(page, _search_site_config(base_url), "mobile")
        if scenario in {"same_page", "new_page"}:
            actual_page = search.open_result(0, timeout_ms=1_000)
            expected_identity = page if scenario == "same_page" else context.pages[-1]
            return (
                actual_page is expected_identity
                and urlparse(actual_page.url).path == "/products/test"
            )

        try:
            search.open_result(0, timeout_ms=300)
        except SearchResultNavigationError as exc:
            actual_path = urlparse(exc.actual_page.url).path
            if scenario == "invalid_destination":
                return actual_path == "/collections/not-a-product"
            return actual_path == "/search" and len(context.pages) == 1
        return False
    finally:
        context.close()


def validate_search_navigation_regressions() -> dict[str, bool]:
    results = {
        "same_page": False,
        "new_page": False,
        "invalid_destination": False,
        "no_navigation": False,
    }
    browser = None
    playwright = None
    server = None
    server_thread = None
    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _SyntheticSearchHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        playwright = sync_playwright().start()
        browser = playwright.webkit.launch(headless=True)
        for scenario in results:
            try:
                results[scenario] = _run_search_scenario(browser, scenario, base_url)
            except Exception as exc:  # noqa: BLE001 — compact offline diagnostic
                print(f"  FAIL  Search {scenario}: {type(exc).__name__}")
    finally:
        if browser is not None:
            browser.close()
        if playwright is not None:
            playwright.stop()
        if server is not None:
            server.shutdown()
            server.server_close()
        if server_thread is not None:
            server_thread.join(timeout=2)
    return results


def _product_site_config() -> dict:
    return {
        "base_url": "https://shop.test",
        "pages": {
            "product": {
                "url": "/products/test",
                "size_resolver": {
                    "models": [
                        {
                            "id": SIZE_MODEL_01,
                            "group_selector": ".sizeoption[role='group']",
                            "option_selector": (
                                "input[type='radio'][name='properties[Size]']"
                            ),
                            "wait_option_selector": (
                                "input[type='radio'][name='properties[Size]']"
                                ":not([value=''])"
                            ),
                            "required_attributes": {"role": "group"},
                            "expected_name": "Size",
                            "custom_size_value": "Free Custom Size",
                        },
                        {
                            "id": SIZE_MODEL_02,
                            "group_selector": (
                                "fieldset[name='Size'][data-handle='size']"
                            ),
                            "option_selector": (
                                "input[type='radio'][name='Size']"
                                "[data-variant-input]"
                            ),
                            "wait_option_selector": (
                                "input[type='radio'][name='Size']"
                                "[data-variant-input]"
                            ),
                            "required_attributes": {
                                "name": "Size",
                                "data-handle": "size",
                            },
                            "expected_name": "Size",
                            "disabled_class_tokens": ["disabled"],
                        },
                    ]
                },
                "selectors": {
                    "purchase_area": {"by": "css", "value": "#purchase"},
                    "title": {"by": "css", "value": "#title"},
                    "color": {"by": "css", "value": "#colors"},
                    "add_to_cart": {"by": "css", "value": "#atc"},
                },
            }
        },
    }


def _size_radios(count: int) -> str:
    return "".join(
        '<label>'
        f'<input form="purchase" name="properties[Size]" type="radio" '
        f'value="{index}">{index}</label>'
        for index in range(1, count + 1)
    )


def _purchase_markup(*, size_count: int, atc_disabled: bool = False) -> str:
    disabled = " disabled" if atc_disabled else ""
    return (
        '<form id="purchase">'
        '<h1 id="title">Synthetic Product</h1>'
        '<fieldset id="colors"><input type="radio" value="Black"></fieldset>'
        '<div id="sizes" class="sizeoption" role="group" aria-label="Size">'
        f'{_size_radios(size_count)}</div>'
        f'<button id="atc" type="button"{disabled}>Add to cart</button>'
        "</form>"
    )


def validate_pdp_readiness_regressions() -> dict[str, bool]:
    results = {"initialization": False, "persistent_zero": False, "dom_rerender": False}
    browser = None
    playwright = None
    try:
        playwright = sync_playwright().start()
        browser = playwright.webkit.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        config = _product_site_config()

        page.set_content(_purchase_markup(size_count=0))
        page.evaluate(
            "sizes => setTimeout(() => { document.querySelector('#sizes').innerHTML = sizes; }, 50)",
            _size_radios(16),
        )
        product = ProductPage(page, config, "mobile")
        colors, sizes, atc = product.wait_purchase_ready(timeout_ms=1_000)
        results["initialization"] = colors == 1 and sizes == 16 and atc

        page.set_content(_purchase_markup(size_count=0))
        product = ProductPage(page, config, "mobile")
        try:
            product.wait_purchase_ready(timeout_ms=300)
        except PurchaseAreaReadinessError as exc:
            results["persistent_zero"] = "size_count_final=0" in str(exc)

        page.set_content(_purchase_markup(size_count=1, atc_disabled=True))
        page.evaluate(
            "markup => {"
            "setTimeout(() => document.querySelector('#purchase').remove(), 20);"
            "setTimeout(() => { document.body.innerHTML = markup; }, 80);"
            "}",
            _purchase_markup(size_count=16),
        )
        product = ProductPage(page, config, "mobile")
        colors, sizes, atc = product.wait_purchase_ready(timeout_ms=1_000)
        results["dom_rerender"] = colors == 1 and sizes == 16 and atc
        context.close()
    except Exception as exc:  # noqa: BLE001 — compact offline diagnostic
        print(f"  FAIL  PDP readiness regressions: {type(exc).__name__}")
    finally:
        if browser is not None:
            browser.close()
        if playwright is not None:
            playwright.stop()
    return results


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

    search_results = validate_search_navigation_regressions()
    ok = check(search_results["same_page"], "Search navigation: same-page PDP") and ok
    ok = check(search_results["new_page"], "Search navigation: new-page PDP") and ok
    ok = check(
        search_results["invalid_destination"],
        "Search navigation: invalid destination fails",
    ) and ok
    ok = check(search_results["no_navigation"], "Search navigation: no navigation fails") and ok

    pdp_results = validate_pdp_readiness_regressions()
    ok = check(pdp_results["initialization"], "PDP readiness: 0 -> 16 initialization") and ok
    ok = check(pdp_results["persistent_zero"], "PDP readiness: persistent zero fails") and ok
    ok = check(pdp_results["dom_rerender"], "PDP readiness: DOM rerender") and ok

    print(f"Runtime Contract Validation: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
