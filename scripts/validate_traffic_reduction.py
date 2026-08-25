"""Offline validation for Experiment A telemetry request blocking."""

from __future__ import annotations

import ast
import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.result import write_results_json  # noqa: E402
from utils.site_access import SignedRequestPolicy  # noqa: E402
from utils.traffic_inventory import TrafficInventory  # noqa: E402
from utils.traffic_reduction import (  # noqa: E402
    BLOCKING_TYPE,
    EXPERIMENT_TELEMETRY_V1,
    TrafficReductionPolicy,
    TrafficReductionRule,
    create_traffic_reduction_policy,
)


SIGNATURE_FIXTURES = {
    "Signature": "offline-signature-fixture",
    "Signature-Input": 'sig1=("@authority");expires=4102444800',
    "Signature-Agent": '"https://offline.example"',
}


def check(ok: bool, label: str) -> bool:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    return ok


def validate_source_contract() -> bool:
    source = (PROJECT_ROOT / "utils" / "traffic_reduction.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    forbidden_reads = {"headers", "post_data", "body", "all_headers"}
    attributes = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    return all(
        (
            "abort" in calls,
            "fallback" in calls,
            "fulfill" not in calls,
            "continue_" not in calls,
            not attributes.intersection(forbidden_reads),
        )
    )


def validate_static_rules() -> dict[str, bool]:
    policy = create_traffic_reduction_policy(EXPERIMENT_TELEMETRY_V1)

    def blocked(url: str, resource_type: str = "fetch") -> bool:
        return policy.match_rule(url, resource_type) is not None

    return {
        "confirmed_telemetry": all(
            (
                blocked("https://analytics.google.com/g/collect?private=value"),
                blocked("https://www.facebook.com/tr/?private=value", "beacon"),
                blocked("https://e.clarity.ms/collect", "xhr"),
                blocked("https://analytics.tiktok.com/i18n/pixel/shopify.js", "xhr"),
                blocked("https://ad.doubleclick.net/ccm/s/collect"),
            )
        ),
        "unknown": not blocked("https://unknown.example/collect"),
        "shopify_runtime": all(
            (
                not blocked("https://monorail-edge.shopifysvc.com/v1/produce"),
                not blocked("https://otlp-http-production.shopifysvc.com/v1/metrics"),
            )
        ),
        "cdn_shopify": not blocked("https://cdn.shopify.com/theme.js", "script"),
        "core_document": not blocked("https://mondressy.com/", "document"),
        "paypal": all(
            (
                not blocked("https://www.paypal.com/sdk/js", "script"),
                not blocked("https://www.paypal.com/xoplatform/logger/api/logger", "xhr"),
            )
        ),
        "checkout": not blocked("https://checkout.pci.shopifyinc.com/api/session", "xhr"),
        "cart": not blocked("https://mondressy.com/cart/change.js", "xhr"),
        "search": not blocked("https://mondressy.com/search/suggest?q=private", "fetch"),
        "reviews": not blocked("https://widget.kudosi.ai/runtime.js", "script"),
        "not_high_confidence": all(
            (
                not blocked("https://static.cloudflareinsights.com/beacon.min.js", "script"),
                not blocked("https://ct.pinterest.com/v3/", "fetch"),
                not blocked("https://bat.bing.com/action/0", "beacon"),
            )
        ),
        "image_font_media": all(
            not blocked("https://analytics.google.com/g/collect", resource_type)
            for resource_type in ("image", "font", "media")
        ),
    }


class _LocalHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 — stdlib handler contract
        signed = all(
            self.headers.get(name) == value
            for name, value in SIGNATURE_FIXTURES.items()
        )
        self.server.observed.append(  # type: ignore[attr-defined]
            {"path": self.path.split("?", 1)[0], "signed": signed}
        )
        payload = b"<html><body>offline routing fixture</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *_args) -> None:
        return


def validate_routing_composition() -> tuple[dict[str, bool], dict, list[dict]]:
    results = {
        "blocking_off": False,
        "signed_request": False,
        "telemetry_abort": False,
        "non_target": False,
        "inventory": False,
        "summary": False,
    }
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LocalHandler)
    server.observed = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    playwright = None
    browser = None
    policy = None
    inventory = None
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=True)

        off_context = browser.new_context()
        off_policy = create_traffic_reduction_policy(None)
        off_policy.attach(off_context)
        off_page = off_context.new_page()
        response = off_page.goto(f"{base_url}/telemetry")
        results["blocking_off"] = (
            response is not None
            and response.ok
            and off_policy.attached_contexts == 0
            and off_policy.blocked_requests == 0
        )
        off_context.close()

        enabled_observed_start = len(server.observed)  # type: ignore[attr-defined]
        context = browser.new_context()
        page = context.new_page()
        signed_policy = SignedRequestPolicy(
            SIGNATURE_FIXTURES,
            ["127.0.0.1"],
            source="offline-fixture",
        )
        signed_policy.attach(context)
        fixture_rule = TrafficReductionRule(
            name="offline_telemetry_fixture",
            hosts=("127.0.0.1",),
            exact_paths=("/telemetry",),
        )
        policy = TrafficReductionPolicy(
            EXPERIMENT_TELEMETRY_V1,
            rules=(fixture_rule,),
        )
        policy.attach(context)
        inventory = TrafficInventory(first_party_hosts={"127.0.0.1"})
        inventory.attach_context(context, "desktop", active_page=page)

        page.goto(f"{base_url}/business")
        non_target_ok = page.evaluate(
            "async url => (await fetch(url)).ok", f"{base_url}/unknown"
        )
        telemetry_aborted = page.evaluate(
            "async url => { try { await fetch(url); return false; } catch (_) { return true; } }",
            f"{base_url}/telemetry?private=query-value",
        )

        observed = list(server.observed)[enabled_observed_start:]  # type: ignore[attr-defined]
        results["signed_request"] = (
            signed_policy.stats["injected"] >= 2
            and all(item["signed"] for item in observed)
        )
        results["telemetry_abort"] = (
            telemetry_aborted
            and not any(item["path"] == "/telemetry" for item in observed)
            and policy.matched_requests == policy.blocked_requests == 1
        )
        results["non_target"] = non_target_ok
        blocked_record = next(
            (record for record in inventory.records if record["path"] == "/telemetry"),
            None,
        )
        results["inventory"] = bool(
            blocked_record and blocked_record["request_failed"]
        )
        summary = policy.build_summary()
        results["summary"] = all(
            (
                summary["experiment"] == EXPERIMENT_TELEMETRY_V1,
                summary["blocking_type"] == BLOCKING_TYPE,
                summary["matched_requests"] == 1,
                summary["blocked_requests"] == 1,
                summary["by_host"] == {"127.0.0.1": 1},
                summary["by_rule"] == {"offline_telemetry_fixture": 1},
                summary["by_resource_type"] == {"fetch": 1},
            )
        )
        context.close()
        return results, summary, inventory.records
    finally:
        if browser is not None:
            browser.close()
        if playwright is not None:
            playwright.stop()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def validate_artifact_and_secret_safety(summary: dict, records: list[dict]) -> dict[str, bool]:
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "traffic" / "blocking-summary.json"
        write_results_json(summary, path)
        persisted = json.loads(path.read_text(encoding="utf-8"))
        serialized = json.dumps(
            {"blocking_summary": persisted, "traffic_requests": records},
            ensure_ascii=False,
        )
    lowered = serialized.lower()
    forbidden_terms = (
        "signature",
        "signature-input",
        "signature-agent",
        "authorization",
        "cookie",
    )
    return {
        "artifact": all(
            key in persisted
            for key in (
                "experiment",
                "matched_requests",
                "blocked_requests",
                "by_host",
                "by_rule",
                "by_resource_type",
            )
        ),
        "secret_safety": (
            not any(term in lowered for term in forbidden_terms)
            and not any(value in serialized for value in SIGNATURE_FIXTURES.values())
            and "query-value" not in serialized
        ),
    }


def main() -> int:
    ok = True
    off = create_traffic_reduction_policy(None)
    ok = check(not off.enabled, "Traffic reduction default OFF") and ok
    ok = check(validate_source_contract(), "abort-only source contract") and ok

    static = validate_static_rules()
    labels = {
        "confirmed_telemetry": "confirmed telemetry rules match",
        "unknown": "UNKNOWN continues",
        "shopify_runtime": "Shopify runtime continues",
        "cdn_shopify": "cdn.shopify.com continues",
        "core_document": "mondressy.com core document continues",
        "paypal": "PayPal runtime and logger continue",
        "checkout": "Checkout continues",
        "cart": "Cart continues",
        "search": "Search continues",
        "reviews": "Reviews runtime continues",
        "not_high_confidence": "non-HIGH telemetry candidates continue",
        "image_font_media": "image/font/media continue",
    }
    for name, label in labels.items():
        ok = check(static[name], label) and ok

    routing, summary, records = validate_routing_composition()
    ok = check(routing["blocking_off"], "Blocking OFF: requests continue") and ok
    ok = check(routing["signed_request"], "Signed Request Injection: PRESERVED") and ok
    ok = check(routing["telemetry_abort"], "Telemetry Abort: WORKING") and ok
    ok = check(routing["non_target"], "Non-target Request: UNAFFECTED") and ok
    ok = check(routing["inventory"], "Traffic Inventory records aborted request") and ok
    ok = check(routing["summary"], "blocking summary aggregates safely") and ok

    artifact = validate_artifact_and_secret_safety(summary, records)
    ok = check(artifact["artifact"], "blocking-summary.json contract") and ok
    ok = check(artifact["secret_safety"], "Secret safety") and ok

    print(f"Traffic Reduction Validation: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
