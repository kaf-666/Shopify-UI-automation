"""Offline contract validation for the read-only Traffic Inventory."""

from __future__ import annotations

import ast
import json
import sys
import tempfile
import threading
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.traffic_inventory import (  # noqa: E402
    CLASSIFICATIONS,
    TrafficInventory,
    sanitize_failure_text,
    sanitize_url,
)


class FakePage:
    def __init__(self) -> None:
        self.listeners = defaultdict(list)

    def on(self, event: str, callback) -> None:
        self.listeners[event].append(callback)

    def close(self) -> None:
        for callback in self.listeners["close"]:
            callback()


class FakeFrame:
    def __init__(self, page: FakePage) -> None:
        self.page = page


class FakeRequest:
    def __init__(
        self,
        page: FakePage,
        url: str,
        *,
        method: str = "GET",
        resource_type: str = "fetch",
        failure: str | None = None,
    ) -> None:
        self.frame = FakeFrame(page)
        self.url = url
        self.method = method
        self.resource_type = resource_type
        self.failure = failure


class FakeResponse:
    def __init__(
        self,
        request: FakeRequest,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.request = request
        self.status = status
        self._headers = {key.lower(): value for key, value in (headers or {}).items()}

    def header_value(self, name: str) -> str | None:
        return self._headers.get(name.lower())


class FakeContext:
    def __init__(self) -> None:
        self.listeners = defaultdict(list)
        self.interception_calls = 0

    def on(self, event: str, callback) -> None:
        self.listeners[event].append(callback)

    def route(self, *_args, **_kwargs) -> None:
        self.interception_calls += 1
        raise AssertionError("Traffic Inventory must not register interception")

    def emit(self, event: str, payload) -> None:
        for callback in self.listeners[event]:
            callback(payload)


def check(ok: bool, label: str) -> bool:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    return ok


def emit_success(
    context: FakeContext,
    page: FakePage,
    url: str,
    *,
    method: str = "GET",
    resource_type: str = "fetch",
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> FakeRequest:
    request = FakeRequest(page, url, method=method, resource_type=resource_type)
    context.emit("request", request)
    context.emit("response", FakeResponse(request, status=status, headers=headers))
    context.emit("requestfinished", request)
    return request


def validate_source_is_observation_only() -> bool:
    source = (PROJECT_ROOT / "utils" / "traffic_inventory.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden_calls = {"route", "abort", "fulfill", "continue_"}
    forbidden_reads = {"post_data", "all_headers", "body", "text"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in forbidden_calls:
                return False
        if isinstance(node, ast.Attribute) and node.attr in forbidden_reads:
            return False
    return True


class LocalHandler(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *_args) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        path = self.path.split("?", 1)[0]
        if path == "/theme.js":
            body = b"window.localInventoryFixture = true;"
            content_type = "application/javascript"
        elif path == "/pixel.png":
            body = b"not-a-real-image"
            content_type = "image/png"
        else:
            body = b"<html><script src='/theme.js'></script><img src='/pixel.png'></html>"
            content_type = "text/html"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "public, max-age=3600")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def validate_real_playwright_events() -> bool:
    server = ThreadingHTTPServer(("127.0.0.1", 0), LocalHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    playwright = None
    browser = None
    context = None
    try:
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        inventory = TrafficInventory(first_party_hosts={"127.0.0.1"})
        inventory.attach_context(context, "desktop", page)
        inventory.set_scope(
            "desktop", journey="direct", case_id="WSMOKE-DIRECT-01", scope_name="CASE"
        )
        page.goto(f"http://127.0.0.1:{server.server_port}/?private=query")
        page.wait_for_load_state("load")
        summary = inventory.build_summary()
        document = next(
            (record for record in inventory.records if record["resource_type"] == "document"),
            None,
        )
        return all(
            (
                not inventory.errors,
                summary["total_requests"] >= 3,
                summary["by_case"].get("WSMOKE-DIRECT-01", 0) >= 3,
                document is not None,
                document and document["classification"] == "REQUIRED",
                document and document["has_query"],
                document and "private" not in json.dumps(document),
            )
        )
    finally:
        if context is not None:
            context.close()
        if browser is not None:
            browser.close()
        if playwright is not None:
            playwright.stop()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def main() -> int:
    ok = True

    safe_url = sanitize_url(
        "https://mondressy.com/search?q=private-dress#private-fragment"
    )
    ok = check(safe_url["path"] == "/search", "URL path retained") and ok
    ok = check(safe_url["has_query"], "query presence retained") and ok
    ok = check(
        "private-dress" not in json.dumps(safe_url)
        and "private-fragment" not in json.dumps(safe_url),
        "query value and fragment removed",
    ) and ok
    ok = check(
        "REDACTED_CHECKOUT_TOKEN"
        in sanitize_url("https://mondressy.com/checkouts/cn/private-token/information")["path"],
        "checkout token removed",
    ) and ok
    ok = check(validate_real_playwright_events(), "real Playwright event integration") and ok

    context = FakeContext()
    active_page = FakePage()
    inventory = TrafficInventory()
    inventory.attach_context(context, "mobile", active_page)
    inventory.set_scope(
        "mobile", journey="search", case_id="WSMOKE-SEARCH-03", scope_name="CASE"
    )

    emit_success(
        context,
        active_page,
        "https://mondressy.com/search?q=dress#results",
        resource_type="fetch",
    )
    emit_success(
        context,
        active_page,
        "https://images.example.test/hero.jpg?customer=private",
        resource_type="image",
    )
    emit_success(
        context,
        active_page,
        "https://unknown.example.test/runtime.js",
        resource_type="script",
    )
    emit_success(
        context,
        active_page,
        "https://www.google-analytics.com/g/collect?client_id=private",
        method="POST",
        resource_type="fetch",
    )
    emit_success(
        context,
        active_page,
        "https://mondressy.com/cart/change.js?line=1",
        method="POST",
        resource_type="fetch",
    )
    emit_success(
        context,
        active_page,
        "https://mondressy.com/checkouts/cn/private-token/information",
        resource_type="document",
    )
    emit_success(
        context,
        active_page,
        "https://mondressy.com/assets/theme.js?v=secret-version",
        resource_type="script",
    )

    cache_headers = {
        "content-type": "application/javascript",
        "cache-control": "public, max-age=31536000, immutable",
        "etag": '"safe-etag"',
    }
    for _ in range(2):
        emit_success(
            context,
            active_page,
            "https://cdn.shopify.com/s/files/theme-static.js?v=private",
            resource_type="script",
            headers=cache_headers,
        )
    for _ in range(2):
        emit_success(
            context,
            active_page,
            "https://mondressy.com/cart.js?timestamp=private",
            resource_type="fetch",
            headers={"cache-control": "public, max-age=60"},
        )

    new_page = FakePage()
    context.emit("page", new_page)
    inventory.set_active_page(new_page, "mobile")
    emit_success(
        context,
        active_page,
        "https://images.example.test/late.webp",
        resource_type="image",
    )

    failed = FakeRequest(
        new_page,
        (
            "https://unknown.example.test/fail?"
            "Authorization=fake-credential&Cookie=fake-cookie"
        ),
        resource_type="xhr",
        failure=(
            "Authorization=fake-credential Cookie=fake-cookie "
            "https://unknown.example.test/fail?Signature=fake-signature"
        ),
    )
    context.emit("request", failed)
    context.emit("requestfailed", failed)

    records = inventory.records
    by_path = {record["path"]: record for record in records}
    ok = check(context.interception_calls == 0, "no request interception registered") and ok
    ok = check(validate_source_is_observation_only(), "module is observation-only") and ok
    ok = check(by_path["/search"]["first_party"], "first-party exact host") and ok
    ok = check(by_path["/search"]["classification"] == "REQUIRED", "/search REQUIRED") and ok
    ok = check(by_path["/hero.jpg"]["classification"] == "OPTIONAL", "image OPTIONAL") and ok
    ok = check(
        by_path["/hero.jpg"]["classification"] != "SAFE_TO_BLOCK_CANDIDATE",
        "image is not automatically safe-to-block",
    ) and ok
    ok = check(
        by_path["/runtime.js"]["classification"] == "UNKNOWN",
        "unknown third party UNKNOWN",
    ) and ok
    ok = check(
        by_path["/g/collect"]["classification"] == "SAFE_TO_BLOCK_CANDIDATE",
        "known telemetry candidate",
    ) and ok
    ok = check(
        by_path["/cart/change.js"]["classification"] == "REQUIRED",
        "/cart/change REQUIRED",
    ) and ok
    checkout_record = next(record for record in records if record["resource_type"] == "document")
    ok = check(checkout_record["classification"] == "REQUIRED", "checkout REQUIRED") and ok
    ok = check(
        by_path["/assets/theme.js"]["classification"] == "LIKELY_REQUIRED",
        "theme JS LIKELY_REQUIRED",
    ) and ok

    summary = inventory.build_summary()
    cache_summary = summary["cache_repeat_candidates"]
    ok = check(cache_summary["request_count"] == 2, "cache repeat request count") and ok
    ok = check(cache_summary["unique_resources"] == 1, "cache repeat unique count") and ok
    ok = check(
        summary["cache_repeat_candidate_requests"] == 2
        and summary["cache_repeat_unique_resources"] == 1,
        "machine-readable cache candidate totals",
    ) and ok
    static_cache = summary["static_cache_analysis"]
    ok = check(
        static_cache["repeated_static_requests"] == 2,
        "repeated static request count",
    ) and ok
    ok = check(
        static_cache["cache_signal_requests"] == 2,
        "static cache signal request count",
    ) and ok
    cart_records = [record for record in records if record["path"] == "/cart.js"]
    ok = check(
        len(cart_records) == 2
        and all(not record["cache_repeat_candidate"] for record in cart_records),
        "dynamic cart endpoint excluded from cache candidates",
    ) and ok
    ok = check(
        summary["superseded_page_requests"]["total"] == 1,
        "superseded page request counted",
    ) and ok
    ok = check(
        summary["total_superseded_page_requests"] == 1,
        "machine-readable superseded total",
    ) and ok
    ok = check(
        summary["by_case"].get("WSMOKE-SEARCH-03") == len(records),
        "case aggregation",
    ) and ok
    ok = check(
        set(summary["classification"]) == set(CLASSIFICATIONS),
        "fixed classification summary",
    ) and ok
    ok = check(
        any(page["state"] == "superseded" for page in summary["pages"])
        and any(page["state"] == "active" for page in summary["pages"]),
        "page active to superseded lifecycle",
    ) and ok

    sanitized_failure = sanitize_failure_text(failed.failure) or ""
    ok = check(
        "fake-credential" not in sanitized_failure
        and "fake-cookie" not in sanitized_failure
        and "fake-signature" not in sanitized_failure,
        "failure text credential values removed",
    ) and ok

    with tempfile.TemporaryDirectory() as temporary:
        traffic_dir = Path(temporary) / "traffic"
        written_summary = inventory.write_artifacts(traffic_dir)
        outputs = "\n".join(
            (traffic_dir / name).read_text(encoding="utf-8")
            for name in ("requests.jsonl", "summary.json", "summary.txt")
        )
        forbidden = (
            "Signature",
            "Signature-Input",
            "Signature-Agent",
            "Authorization",
            "Cookie",
            "MONDRESSY_US_SHOPIFY_SIGNATURE",
            "fake-credential",
            "fake-cookie",
            "fake-signature",
            "private-dress",
            "private-fragment",
            "secret-version",
        )
        ok = check(
            all(value.lower() not in outputs.lower() for value in forbidden),
            "persisted artifacts contain no secret names or fixture values",
        ) and ok
        ok = check(
            written_summary["traffic_inventory_status"] == "COMPLETE",
            "collector status COMPLETE",
        ) and ok
        ok = check(
            all((traffic_dir / name).is_file() for name in ("requests.jsonl", "summary.json", "summary.txt")),
            "all traffic artifacts written",
        ) and ok

    print(f"Traffic Inventory Validation: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
