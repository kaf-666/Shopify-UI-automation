"""Read-only BrowserContext request inventory for Website Smoke V1.

The collector only subscribes to Playwright network/page events. It never
intercepts, modifies, fulfils, aborts, or retries a request. Request and
response bodies and complete header mappings are deliberately out of scope.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit


CLASSIFICATIONS = (
    "REQUIRED",
    "LIKELY_REQUIRED",
    "OPTIONAL",
    "SAFE_TO_BLOCK_CANDIDATE",
    "UNKNOWN",
)
CONFIDENCE_LEVELS = ("HIGH", "MEDIUM", "LOW")
FIRST_PARTY_HOSTS = ("mondressy.com", "www.mondressy.com")
OPTIONAL_RESOURCE_TYPES = {"image", "font", "media"}
STATIC_RESOURCE_TYPES = {"script", "stylesheet", "image", "font", "media"}
STATIC_SUFFIXES = (
    ".js",
    ".css",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".avif",
    ".svg",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".mp4",
    ".webm",
)
SENSITIVE_ENV_NAMES = (
    "MONDRESSY_US_SHOPIFY_SIGNATURE",
    "MONDRESSY_US_SHOPIFY_SIGNATURE_INPUT",
    "MONDRESSY_US_SHOPIFY_SIGNATURE_AGENT",
    "SHOPIFY_PROXY_PASSWORD",
    "PLAYWRIGHT_PROXY_PASSWORD",
)
FORBIDDEN_OUTPUT_TERMS = (
    "signature-input",
    "signature-agent",
    "signature",
    "authorization",
    "cookie",
    "mondressy_us_shopify_signature_input",
    "mondressy_us_shopify_signature_agent",
    "mondressy_us_shopify_signature",
)


@dataclass(frozen=True)
class ClassificationResult:
    classification: str
    reason: str
    confidence: str


class RequestClassifier:
    """Centralized, conservative Phase 1 request classification."""

    _telemetry_hosts = (
        "google-analytics.com",
        "www.google-analytics.com",
        "analytics.google.com",
        "googletagmanager.com",
        "www.googletagmanager.com",
        "connect.facebook.net",
        "www.facebook.com",
        "doubleclick.net",
        "stats.g.doubleclick.net",
        "analytics.tiktok.com",
        "static.hotjar.com",
        "script.hotjar.com",
        "in.hotjar.com",
        "clarity.ms",
        "www.clarity.ms",
        "ct.pinterest.com",
    )
    _telemetry_path_markers = (
        "/g/collect",
        "/collect",
        "/tr/",
        "/pixel",
        "/beacon",
        "/analytics",
        "/telemetry",
        "/session-replay",
    )
    _shopify_runtime_hosts = (
        "cdn.shopify.com",
        "shopify.com",
        "shopifycdn.net",
    )
    _checkout_runtime_hosts = (
        "checkout.shopify.com",
        "pay.shopify.com",
        "shop.app",
    )
    _app_runtime_markers = (
        "infiniteoptions",
        "product-options",
        "product_options",
        "spb",
        "variant",
        "cart-drawer",
        "predictive-search",
    )

    @staticmethod
    def _host_matches(host: str, candidates: Iterable[str]) -> bool:
        return any(host == item or host.endswith("." + item) for item in candidates)

    @staticmethod
    def _required_path(path: str) -> bool:
        if path == "/cart.js":
            return True
        if re.match(r"^/cart/(?:add|change|clear)(?:\.js)?(?:/|$)", path):
            return True
        if path.startswith(("/checkout", "/checkouts/")):
            return True
        if path == "/search" or path.startswith("/search/"):
            return True
        return False

    def classify(self, record: dict) -> ClassificationResult:
        method = str(record.get("method") or "").upper()
        resource_type = str(record.get("resource_type") or "other").lower()
        host = str(record.get("host") or "").lower()
        path = str(record.get("path") or "/").lower()
        first_party = bool(record.get("first_party"))

        if resource_type == "document":
            return ClassificationResult("REQUIRED", "document navigation", "HIGH")
        if self._required_path(path):
            return ClassificationResult("REQUIRED", "core storefront endpoint", "HIGH")

        telemetry_host = self._host_matches(host, self._telemetry_hosts)
        telemetry_path = any(marker in path for marker in self._telemetry_path_markers)
        if telemetry_host and telemetry_path:
            return ClassificationResult(
                "SAFE_TO_BLOCK_CANDIDATE", "known telemetry endpoint", "HIGH"
            )
        if telemetry_host and resource_type in {"script", "image", "xhr", "fetch", "ping"}:
            return ClassificationResult(
                "SAFE_TO_BLOCK_CANDIDATE", "known telemetry host", "MEDIUM"
            )

        checkout_scope = record.get("case_id") == "WSMOKE-CHECKOUT-01"
        checkout_runtime = first_party or self._host_matches(
            host, self._checkout_runtime_hosts + self._shopify_runtime_hosts
        )
        if checkout_scope and checkout_runtime and resource_type in {
            "script",
            "stylesheet",
            "xhr",
            "fetch",
        }:
            return ClassificationResult("REQUIRED", "checkout runtime", "MEDIUM")

        if resource_type in OPTIONAL_RESOURCE_TYPES:
            return ClassificationResult(
                "OPTIONAL", f"resource_type={resource_type}", "MEDIUM"
            )

        if first_party and resource_type in {"xhr", "fetch"}:
            return ClassificationResult("LIKELY_REQUIRED", "same-origin data request", "MEDIUM")
        if first_party and resource_type in {"script", "stylesheet"}:
            return ClassificationResult("LIKELY_REQUIRED", "same-origin theme asset", "HIGH")
        if self._host_matches(host, self._shopify_runtime_hosts) and resource_type in {
            "script",
            "stylesheet",
            "xhr",
            "fetch",
        }:
            return ClassificationResult("LIKELY_REQUIRED", "Shopify runtime", "MEDIUM")
        if any(marker in (host + path) for marker in self._app_runtime_markers):
            return ClassificationResult("LIKELY_REQUIRED", "storefront app runtime", "MEDIUM")
        if method != "GET" and first_party:
            return ClassificationResult("LIKELY_REQUIRED", "same-origin mutation", "MEDIUM")
        return ClassificationResult("UNKNOWN", "purpose not established", "LOW")


def _redact_forbidden_terms(value: str) -> str:
    text = str(value or "")
    assignment = "|".join(
        re.escape(term) for term in sorted(FORBIDDEN_OUTPUT_TERMS, key=len, reverse=True)
    )
    text = re.sub(
        rf"(?i)(?:{assignment})\s*[:=]\s*[^\s,;]+",
        "[REDACTED_FIELD]=[REDACTED_VALUE]",
        text,
    )
    for term in sorted(FORBIDDEN_OUTPUT_TERMS, key=len, reverse=True):
        text = re.sub(re.escape(term), "[REDACTED_FIELD]", text, flags=re.I)
    for env_name in SENSITIVE_ENV_NAMES:
        secret = os.environ.get(env_name)
        if secret:
            text = text.replace(secret, "[REDACTED_VALUE]")
    return text


def _sanitize_path(path: str) -> str:
    if not path:
        return "/"
    segments = path.split("/")
    checkout_index = next(
        (index for index, segment in enumerate(segments) if segment.lower() in {"checkout", "checkouts"}),
        None,
    )
    checkout_token_redacted = False
    safe_segments = []
    for index, segment in enumerate(segments):
        candidate = segment
        lower = candidate.lower()
        if checkout_index is not None and index > checkout_index and not checkout_token_redacted:
            if lower in {"cn", "information", "shipping", "payment", "thank_you"}:
                safe_segments.append(candidate)
                continue
            candidate = "[REDACTED_CHECKOUT_TOKEN]"
            checkout_token_redacted = True
        elif "@" in candidate or "%40" in lower:
            candidate = "[REDACTED_PATH_VALUE]"
        elif re.fullmatch(r"[A-Za-z0-9_-]{48,}", candidate or ""):
            candidate = "[REDACTED_PATH_VALUE]"
        safe_segments.append(_redact_forbidden_terms(candidate))
    safe = "/".join(safe_segments)
    return (safe or "/")[:1024]


def sanitize_url(url: str) -> dict:
    """Return a query-free, fragment-free URL projection safe for artifacts."""

    try:
        parsed = urlsplit(str(url or ""))
    except ValueError:
        parsed = urlsplit("")
    scheme = str(parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        host = ""
        path = "/[REDACTED_NON_HTTP_URL]"
        has_query = bool(parsed.query)
    else:
        host = _redact_forbidden_terms(str(parsed.hostname or "").lower())
        path = _sanitize_path(parsed.path or "/")
        has_query = bool(parsed.query)
    normalized = f"{scheme}://{host}{path}"
    return {
        "scheme": scheme,
        "host": host,
        "path": path,
        "has_query": has_query,
        "url_hash": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
    }


def sanitize_failure_text(value: object) -> Optional[str]:
    """Remove URLs, secret field names/values and bound output size."""

    if value is None:
        return None
    text = str(value)

    def replace_url(match) -> str:
        safe = sanitize_url(match.group(0))
        return f"{safe['scheme']}://{safe['host']}{safe['path']}"

    text = re.sub(r"https?://[^\s'\"<>]+", replace_url, text, flags=re.I)
    text = _redact_forbidden_terms(text)
    return " ".join(text.split())[:500]


class TrafficInventory:
    """In-memory observer attached explicitly to one or more BrowserContexts."""

    def __init__(
        self,
        first_party_hosts: Iterable[str] = FIRST_PARTY_HOSTS,
        classifier: Optional[RequestClassifier] = None,
    ) -> None:
        self.first_party_hosts = {str(host).lower() for host in first_party_hosts}
        self.classifier = classifier or RequestClassifier()
        self.records: list[dict] = []
        self.errors: list[dict] = []
        self._request_records: dict[Any, dict] = {}
        self._pages: dict[Any, dict] = {}
        self._active_pages: dict[str, Any] = {}
        self._scopes: dict[str, dict] = {}
        self._seen_case_ids: set[str] = set()
        self._sequence = 0
        self._page_sequence = 0

    @property
    def status(self) -> str:
        return "ERROR" if self.errors else "COMPLETE"

    def record_error(self, operation: str, exc: BaseException) -> None:
        self.errors.append(
            {
                "operation": re.sub(r"[^A-Za-z0-9_.-]", "_", str(operation))[:80],
                "error_type": type(exc).__name__,
            }
        )

    def attach_context(self, context, viewport: str, active_page=None) -> None:
        """Subscribe to read-only events on a context; no interception is used."""

        viewport_key = str(viewport).lower()
        self._scopes.setdefault(
            viewport_key,
            {"journey": "infrastructure", "case_id": None, "scope_name": "STARTUP"},
        )
        context.on("page", lambda page: self._on_page(page, viewport_key))
        context.on("request", lambda request: self._on_request(request, viewport_key))
        context.on("response", lambda response: self._on_response(response, viewport_key))
        context.on(
            "requestfinished", lambda request: self._on_request_finished(request, viewport_key)
        )
        context.on("requestfailed", lambda request: self._on_request_failed(request, viewport_key))
        if active_page is not None:
            self.set_active_page(active_page, viewport_key)

    def _on_page(self, page, viewport: str) -> None:
        try:
            self.register_page(page, viewport, "unknown")
        except Exception as exc:
            self.record_error("page_event", exc)

    def set_scope(
        self,
        viewport: str,
        *,
        journey: str,
        case_id: Optional[str] = None,
        scope_name: Optional[str] = None,
    ) -> None:
        viewport_key = str(viewport).lower()
        journey_key = str(journey or "infrastructure").lower()
        if journey_key not in {"direct", "search", "browse", "infrastructure"}:
            journey_key = "infrastructure"
        safe_case = str(case_id) if case_id and re.fullmatch(r"WSMOKE-[A-Z0-9-]+", str(case_id)) else None
        safe_scope = re.sub(r"[^A-Za-z0-9_.-]", "_", str(scope_name or ""))[:80] or None
        self._scopes[viewport_key] = {
            "journey": journey_key,
            "case_id": safe_case,
            "scope_name": safe_scope,
        }
        if safe_case:
            self._seen_case_ids.add(safe_case)

    def register_page(self, page, viewport: str, state: str = "unknown") -> str:
        if page in self._pages:
            return self._pages[page]["page_id"]
        self._page_sequence += 1
        page_info = {
            "page_id": f"page-{self._page_sequence:02d}",
            "viewport": str(viewport).lower(),
            "state": state if state in {"active", "superseded", "closed", "unknown"} else "unknown",
        }
        self._pages[page] = page_info
        try:
            page.on("close", lambda: self._mark_page_closed(page))
        except Exception as exc:  # pragma: no cover - real Playwright defensive path
            self.record_error("page_close_listener", exc)
        return page_info["page_id"]

    def set_active_page(self, page, viewport: str) -> None:
        viewport_key = str(viewport).lower()
        self.register_page(page, viewport_key)
        previous = self._active_pages.get(viewport_key)
        if previous is not None and previous is not page:
            previous_info = self._pages.get(previous)
            if previous_info and previous_info["state"] != "closed":
                previous_info["state"] = "superseded"
        self._pages[page]["state"] = "active"
        self._active_pages[viewport_key] = page

    def _mark_page_closed(self, page) -> None:
        info = self._pages.get(page)
        if info:
            info["state"] = "closed"

    def _page_for_request(self, request, viewport: str) -> tuple[str, str]:
        try:
            page = request.frame.page
        except Exception:
            return "unknown", "unknown"
        self.register_page(page, viewport)
        info = self._pages[page]
        return info["page_id"], info["state"]

    def _on_request(self, request, viewport: str) -> None:
        try:
            safe_url = sanitize_url(request.url)
            page_id, page_state = self._page_for_request(request, viewport)
            scope = dict(
                self._scopes.get(
                    viewport,
                    {"journey": "infrastructure", "case_id": None, "scope_name": "UNATTRIBUTED"},
                )
            )
            self._sequence += 1
            record = {
                "sequence": self._sequence,
                "viewport": viewport,
                "journey": scope["journey"],
                "case_id": scope["case_id"],
                "scope_name": scope["scope_name"],
                "page_id": page_id,
                "page_state": page_state,
                "method": str(request.method or "").upper(),
                "resource_type": str(request.resource_type or "other").lower(),
                **safe_url,
                "first_party": safe_url["host"] in self.first_party_hosts,
                "status": None,
                "content_type": None,
                "cache_control": None,
                "etag_present": False,
                "last_modified_present": False,
                "classification": "UNKNOWN",
                "classification_reason": "purpose not established",
                "confidence": "LOW",
                "repeat_count": 1,
                "cache_repeat_candidate": False,
                "request_finished": False,
                "request_failed": False,
                "failure_text": None,
            }
            classified = self.classifier.classify(record)
            record.update(
                {
                    "classification": classified.classification,
                    "classification_reason": classified.reason,
                    "confidence": classified.confidence,
                }
            )
            self.records.append(record)
            self._request_records[request] = record
        except Exception as exc:  # callbacks may never affect business execution
            self.record_error("request_event", exc)

    def _ensure_request(self, request, viewport: str) -> Optional[dict]:
        record = self._request_records.get(request)
        if record is None:
            self._on_request(request, viewport)
            record = self._request_records.get(request)
        return record

    @staticmethod
    def _response_header(response, name: str) -> Optional[str]:
        try:
            return response.header_value(name)
        except Exception:
            return None

    def _on_response(self, response, viewport: str) -> None:
        try:
            record = self._ensure_request(response.request, viewport)
            if record is None:
                return
            content_type = self._response_header(response, "content-type")
            cache_control = self._response_header(response, "cache-control")
            record.update(
                {
                    "status": int(response.status),
                    "content_type": sanitize_failure_text(content_type),
                    "cache_control": sanitize_failure_text(cache_control),
                    "etag_present": bool(self._response_header(response, "etag")),
                    "last_modified_present": bool(
                        self._response_header(response, "last-modified")
                    ),
                }
            )
        except Exception as exc:
            self.record_error("response_event", exc)

    def _on_request_finished(self, request, viewport: str) -> None:
        try:
            record = self._ensure_request(request, viewport)
            if record is not None:
                record["request_finished"] = True
        except Exception as exc:
            self.record_error("request_finished_event", exc)

    def _on_request_failed(self, request, viewport: str) -> None:
        try:
            record = self._ensure_request(request, viewport)
            if record is None:
                return
            failure = request.failure
            record.update(
                {
                    "request_failed": True,
                    "failure_text": sanitize_failure_text(failure),
                }
            )
        except Exception as exc:
            self.record_error("request_failed_event", exc)

    @staticmethod
    def _normalized_key(record: dict) -> tuple[str, str, str]:
        return (
            str(record.get("scheme") or ""),
            str(record.get("host") or ""),
            str(record.get("path") or "/"),
        )

    @staticmethod
    def _cache_signal(record: dict) -> bool:
        cache_control = str(record.get("cache_control") or "").lower()
        return any(token in cache_control for token in ("max-age", "public", "immutable")) or bool(
            record.get("etag_present") or record.get("last_modified_present")
        )

    @staticmethod
    def _dynamic_path(path: str) -> bool:
        lower = str(path or "").lower()
        return (
            lower == "/cart.js"
            or lower.startswith(("/cart/", "/search", "/checkout", "/checkouts/", "/account"))
        )

    def _cache_eligible(self, record: dict) -> bool:
        method_ok = record.get("method") == "GET"
        status = record.get("status")
        status_ok = isinstance(status, int) and (200 <= status < 300 or status == 304)
        resource_type = str(record.get("resource_type") or "")
        path = str(record.get("path") or "")
        static_tendency = resource_type in STATIC_RESOURCE_TYPES or path.lower().endswith(STATIC_SUFFIXES)
        return all(
            (
                method_ok,
                status_ok,
                not record.get("request_failed"),
                static_tendency,
                not self._dynamic_path(path),
                self._cache_signal(record),
            )
        )

    def _apply_repeat_analysis(self) -> None:
        counts = Counter(self._normalized_key(record) for record in self.records)
        eligible_counts = Counter(
            self._normalized_key(record)
            for record in self.records
            if self._cache_eligible(record)
        )
        for record in self.records:
            key = self._normalized_key(record)
            record["repeat_count"] = counts[key]
            record["cache_repeat_candidate"] = (
                self._cache_eligible(record) and eligible_counts[key] >= 2
            )

    @staticmethod
    def _counts(records: Iterable[dict], field: str, missing: str = "UNATTRIBUTED") -> dict:
        counter = Counter(str(record.get(field) or missing) for record in records)
        return dict(sorted(counter.items()))

    @staticmethod
    def _fixed_counts(
        records: Iterable[dict], field: str, fixed: Iterable[str], missing: str
    ) -> dict:
        counts = Counter(str(record.get(field) or missing) for record in records)
        result = {name: counts.pop(name, 0) for name in fixed}
        result.update(dict(sorted(counts.items())))
        return result

    @staticmethod
    def _percentage(count: int, total: int) -> float:
        return round((count / total * 100.0), 2) if total else 0.0

    def build_summary(self) -> dict:
        self._apply_repeat_analysis()
        total = len(self.records)
        host_counts = Counter(str(record.get("host") or "unknown") for record in self.records)
        repeated: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
        for record in self.records:
            repeated[self._normalized_key(record)].append(record)

        repeated_rows = []
        for (scheme, host, path), records in repeated.items():
            if len(records) < 2:
                continue
            resource_type = Counter(r["resource_type"] for r in records).most_common(1)[0][0]
            classification = Counter(r["classification"] for r in records).most_common(1)[0][0]
            repeated_rows.append(
                {
                    "key": f"{host}{path}",
                    "scheme": scheme,
                    "host": host,
                    "path": path,
                    "count": len(records),
                    "resource_type": resource_type,
                    "classification": classification,
                }
            )
        repeated_rows.sort(key=lambda row: (-row["count"], row["key"]))

        classification_counts = Counter(record["classification"] for record in self.records)
        classification_summary = {
            name: {
                "request_count": classification_counts[name],
                "percentage": self._percentage(classification_counts[name], total),
            }
            for name in CLASSIFICATIONS
        }
        cache_records = [record for record in self.records if record["cache_repeat_candidate"]]
        static_records = [
            record
            for record in self.records
            if record.get("method") == "GET"
            and (
                record.get("resource_type") in STATIC_RESOURCE_TYPES
                or str(record.get("path") or "").lower().endswith(STATIC_SUFFIXES)
            )
            and not self._dynamic_path(str(record.get("path") or ""))
            and isinstance(record.get("status"), int)
            and (200 <= record["status"] < 300 or record["status"] == 304)
            and not record.get("request_failed")
        ]
        static_counts = Counter(self._normalized_key(record) for record in static_records)
        repeated_static_records = [
            record
            for record in static_records
            if static_counts[self._normalized_key(record)] >= 2
        ]
        repeated_static_keys = {
            self._normalized_key(record) for record in repeated_static_records
        }
        cache_signal_records = [record for record in static_records if self._cache_signal(record)]
        cache_signal_keys = {
            self._normalized_key(record) for record in cache_signal_records
        }
        cache_groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
        for record in cache_records:
            cache_groups[self._normalized_key(record)].append(record)
        cache_top = []
        for (scheme, host, path), records in cache_groups.items():
            cache_top.append(
                {
                    "key": f"{host}{path}",
                    "scheme": scheme,
                    "host": host,
                    "path": path,
                    "count": len(records),
                    "resource_type": Counter(
                        record["resource_type"] for record in records
                    ).most_common(1)[0][0],
                    "classification": Counter(
                        record["classification"] for record in records
                    ).most_common(1)[0][0],
                }
            )
        cache_top.sort(key=lambda row: (-row["count"], row["key"]))

        superseded = [record for record in self.records if record["page_state"] == "superseded"]
        first_party_count = sum(1 for record in self.records if record["first_party"])
        return {
            "traffic_inventory_status": self.status,
            "total_requests": total,
            "failed_requests": sum(1 for record in self.records if record["request_failed"]),
            "by_viewport": self._fixed_counts(
                self.records, "viewport", ("desktop", "mobile"), "unknown"
            ),
            "by_journey": self._fixed_counts(
                self.records,
                "journey",
                ("direct", "search", "browse", "infrastructure"),
                "infrastructure",
            ),
            "by_case": {
                **{
                    case_id: 0
                    for case_id in sorted(self._seen_case_ids)
                },
                **self._counts(self.records, "case_id"),
            },
            "by_resource_type": self._fixed_counts(
                self.records,
                "resource_type",
                ("document", "script", "stylesheet", "image", "font", "media", "xhr", "fetch", "other"),
                "other",
            ),
            "party": {
                "first_party": first_party_count,
                "third_party": total - first_party_count,
            },
            "top_hosts": [
                {
                    "host": host,
                    "request_count": count,
                    "percentage": self._percentage(count, total),
                }
                for host, count in host_counts.most_common(20)
            ],
            "repeated_resources": repeated_rows[:30],
            "classification": classification_summary,
            "cache_repeat_candidates": {
                "request_count": len(cache_records),
                "unique_resources": len(cache_groups),
                "top_resources": cache_top[:30],
            },
            "cache_repeat_candidate_requests": len(cache_records),
            "cache_repeat_unique_resources": len(cache_groups),
            "static_cache_analysis": {
                "successful_static_requests": len(static_records),
                "successful_static_unique_resources": len(static_counts),
                "repeated_static_requests": len(repeated_static_records),
                "repeated_static_unique_resources": len(repeated_static_keys),
                "cache_signal_requests": len(cache_signal_records),
                "cache_signal_unique_resources": len(cache_signal_keys),
            },
            "superseded_page_requests": {
                "total": len(superseded),
                "by_viewport": self._counts(superseded, "viewport"),
                "by_page_id": self._counts(superseded, "page_id"),
                "by_host": self._counts(superseded, "host", "unknown"),
                "by_resource_type": self._counts(superseded, "resource_type", "other"),
            },
            "total_superseded_page_requests": len(superseded),
            "pages": sorted(
                (dict(info) for info in self._pages.values()),
                key=lambda info: info["page_id"],
            ),
            "collector_errors": list(self.errors),
        }

    @staticmethod
    def _summary_text(summary: dict) -> str:
        lines = [
            "=== Traffic Inventory ===",
            "",
            f"Status:                         {summary['traffic_inventory_status']}",
            f"Total Requests:                 {summary['total_requests']}",
            f"Desktop:                       {summary['by_viewport'].get('desktop', 0)}",
            f"Mobile:                        {summary['by_viewport'].get('mobile', 0)}",
            f"First Party:                   {summary['party']['first_party']}",
            f"Third Party:                   {summary['party']['third_party']}",
            "",
            "Classification:",
        ]
        for name in CLASSIFICATIONS:
            value = summary["classification"][name]
            lines.append(
                f"  {name:<28} {value['request_count']:>7}  {value['percentage']:>6.2f}%"
            )
        cache = summary["cache_repeat_candidates"]
        static_cache = summary["static_cache_analysis"]
        superseded = summary["superseded_page_requests"]
        lines.extend(
            [
                "",
                "Cache Repeat Candidates:",
                f"  Requests                     {cache['request_count']:>7}",
                f"  Unique Resources             {cache['unique_resources']:>7}",
                f"  Repeated Static Requests     {static_cache['repeated_static_requests']:>7}",
                f"  Repeated Static Unique       {static_cache['repeated_static_unique_resources']:>7}",
                f"  Cache Signal Requests        {static_cache['cache_signal_requests']:>7}",
                f"  Cache Signal Unique          {static_cache['cache_signal_unique_resources']:>7}",
                "",
                "Superseded Page Requests:",
                f"  Total                        {superseded['total']:>7}",
            ]
        )
        return "\n".join(lines) + "\n"

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)

    def write_artifacts(self, traffic_dir: Path) -> dict:
        """Finalize and atomically persist sanitized JSONL/JSON/text artifacts."""

        target = Path(traffic_dir)
        try:
            summary = self.build_summary()
            requests_text = "".join(
                json.dumps(record, ensure_ascii=False, sort_keys=False) + "\n"
                for record in self.records
            )
            self._atomic_write(target / "requests.jsonl", requests_text)
            self._atomic_write(
                target / "summary.json",
                json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
            )
            self._atomic_write(target / "summary.txt", self._summary_text(summary))
            return summary
        except Exception as exc:
            self.record_error("artifact_write", exc)
            return self.build_summary()

    def console_summary(self, summary: Optional[dict] = None) -> str:
        return self._summary_text(summary or self.build_summary())
