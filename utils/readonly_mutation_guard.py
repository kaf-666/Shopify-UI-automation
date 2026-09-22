"""Fail-closed routing guard for Readonly storefront runs.

The guard protects only cart mutation endpoints and mutation-capable HTTP
methods. Non-matching requests use Playwright ``route.fallback()`` so a route
registered earlier by the Signed Request policy remains in the chain.
"""

from __future__ import annotations

from collections import Counter
import re
from typing import Iterable, List, Optional
from urllib.parse import urlsplit

from utils.mutation_fingerprint import (
    HOST_CLASSES,
    REASONS,
    UNKNOWN,
    safe_endpoint_fingerprint,
    sanitize_pathname,
)


CART_MUTATION_PATHS = frozenset(
    {
        "/cart/add",
        "/cart/add.js",
        "/cart/change",
        "/cart/change.js",
        "/cart/update",
        "/cart/update.js",
        "/cart/clear",
        "/cart/clear.js",
    }
)
MUTATION_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Website Smoke V1 is transactional-safe rather than readonly. These are
# the only storefront mutations its public flow is allowed to perform.
EXPECTED_TRANSACTIONAL_MUTATION_PATHS = frozenset(
    {
        "/cart",
        "/cart/add",
        "/cart/add.js",
        "/cart/change",
        "/cart/change.js",
        "/cart/update",
        "/cart/update.js",
        "/cart/clear",
        "/cart/clear.js",
    }
)
HIGH_RISK_PATH_PREFIXES = (
    "/account",
    "/accounts",
    "/customer",
    "/customers",
    "/order",
    "/orders",
    "/payment",
    "/payments",
    "/wallet",
    "/wallets",
    "/subscription",
    "/subscriptions",
    "/purchase",
    "/purchases",
    "/pay",
    "/3ds",
    "/3d-secure",
)
HIGH_RISK_CHECKOUT_MARKERS = (
    "/checkouts/",
    "/thank_you",
    "/thank-you",
    "/complete",
)
HIGH_RISK_HOST_MARKERS = (
    "checkout.shopify.com",
    "pay.shopify.com",
    "shop.app",
    "paypal.com",
    "stripe.com",
    "adyen.com",
)
NON_BUSINESS_MUTATION_PATH_PREFIXES = (
    "/.well-known/shopify/",
    "/api/collect",
    "/api/event/collect",
    "/cdn-cgi/rum",
    "/xoplatform/logger/api/logger",
)
LOCALIZATION_CONTEXT_PATH = "/localization"
LOCALIZATION_CONTEXT_REASON = "FIRST_PARTY_LOCALIZATION_CONTEXT"
WATI_ADD_TO_CART_EVENT_PATH = "/apps/wati/addtocartevent"
WATI_ADD_TO_CART_EVENT_REASON = "FIRST_PARTY_WATI_ADD_TO_CART_EVENT"


class ReadonlyMutationGuard:
    """Block and record cart mutation requests for one BrowserContext."""

    def __init__(self) -> None:
        self._scope = {"viewport": None, "journey": None, "case_id": None}
        self._violations: List[dict] = []
        self._context = None
        self._attached = False
        self._handler = self._handle_route

    @staticmethod
    def normalize_path(url: str) -> str:
        """Return a lower-case path without query, fragment, or trailing slash."""
        try:
            path = urlsplit(str(url or "")).path or "/"
        except ValueError:
            return ""
        path = "/" + path.lstrip("/")
        path = path.lower()
        return path.rstrip("/") or "/"

    @classmethod
    def matches(cls, url: str, method: str = "GET") -> bool:
        """Return whether *method* and *url* identify a cart mutation."""
        normalized_method = str(method or "").upper()
        return (
            normalized_method in MUTATION_METHODS
            and cls.normalize_path(url) in CART_MUTATION_PATHS
        )

    is_cart_mutation = matches

    def set_scope(
        self,
        viewport: Optional[str],
        journey: Optional[str],
        case_id: Optional[str],
    ) -> None:
        """Set the safe attribution scope for subsequently intercepted requests."""
        self._scope = {
            "viewport": viewport,
            "journey": journey,
            "case_id": case_id,
        }

    def _handle_route(self, route) -> None:
        request = route.request
        method = str(getattr(request, "method", "") or "").upper()
        path = self.normalize_path(getattr(request, "url", ""))
        if self.matches(getattr(request, "url", ""), method):
            self._violations.append(
                {
                    "method": method,
                    "path": path,
                    "case_id": self._scope.get("case_id"),
                    "journey": self._scope.get("journey"),
                    "viewport": self._scope.get("viewport"),
                }
            )
            # Record before aborting. There is deliberately no fallback on a
            # match: the request must not reach Shopify.
            route.abort()
            return

        fallback = getattr(route, "fallback", None)
        if callable(fallback):
            fallback()
            return
        # Small fakes and older Playwright adapters may not expose fallback;
        # continue is the safe final fallback when no earlier route exists.
        route.continue_()

    def attach(self, context) -> None:
        """Attach after any existing route so non-matches fall back to it."""
        if self._attached:
            return
        context.route("**/*", self._handler)
        self._context = context
        self._attached = True

    def detach(self) -> None:
        """Remove the handler when the context remains usable."""
        if not self._attached or self._context is None:
            return
        try:
            self._context.unroute("**/*", self._handler)
        except Exception:
            # Context disposal is still owned by BrowserRuntime; a closed
            # context makes unroute unnecessary.
            pass
        finally:
            self._context = None
            self._attached = False

    def violations(self) -> List[dict]:
        """Return copies of safe violation records."""
        return [dict(item) for item in self._violations]

    def violation_count(self) -> int:
        return len(self._violations)

    def violations_since(self, index: int) -> List[dict]:
        """Return violations captured at or after a prior count."""
        return [dict(item) for item in self._violations[max(0, int(index)):]]

    def has_violations(self) -> bool:
        return bool(self._violations)

    def out_of_scope_violations(self) -> List[dict]:
        """Return violations not attributed to a Readonly Case scope."""
        return [item for item in self.violations() if not item.get("case_id")]

    @staticmethod
    def safe_detail(violation: dict) -> str:
        """Render only method and normalized path for user-facing diagnostics."""
        return f"blocked cart mutation: {violation.get('method', '')} {violation.get('path', '')}"


class TransactionalMutationPolicy:
    """Fail-closed mutation policy for the safe Full Smoke suite.

    Expected cart and verified first-party context mutations are allowed and
    recorded. Any first-party mutation outside that small allowlist, plus all
    checkout/payment/account mutations, is aborted before it can reach the
    storefront. Third-party telemetry POSTs are outside the storefront
    mutation boundary and are left to the existing TrafficInventory observer.
    """

    EXPECTED = "EXPECTED_MUTATION"
    KNOWN_BLOCKED_SIDE_EFFECT = "KNOWN_BLOCKED_SIDE_EFFECT"
    UNEXPECTED = "UNEXPECTED_MUTATION"
    HIGH_RISK = "HIGH_RISK_MUTATION"

    def __init__(
        self,
        first_party_hosts: Iterable[str],
        canonical_origin: Optional[str] = None,
    ) -> None:
        self.first_party_hosts = {
            str(host).strip().lower() for host in first_party_hosts if str(host).strip()
        }
        self.canonical_origin = canonical_origin
        self._scope = {"viewport": None, "journey": None, "case_id": None, "scope_name": None}
        self._events: List[dict] = []
        self._context = None
        self._attached = False
        self._handler = self._handle_route

    @staticmethod
    def _host(url: str) -> str:
        try:
            return str(urlsplit(str(url or "")).hostname or "").lower()
        except ValueError:
            return ""

    @classmethod
    def _is_high_risk(cls, path: str, host: str) -> bool:
        if any(path == prefix or path.startswith(prefix + "/") for prefix in HIGH_RISK_PATH_PREFIXES):
            return True
        risk_segments = {
            prefix.lstrip("/")
            for prefix in HIGH_RISK_PATH_PREFIXES
            if prefix.count("/") == 1
        }
        if any(segment in risk_segments for segment in path.split("/") if segment):
            return True
        if any(marker in path for marker in HIGH_RISK_CHECKOUT_MARKERS):
            return True
        return any(host == marker or host.endswith("." + marker) for marker in HIGH_RISK_HOST_MARKERS)

    @staticmethod
    def _graphql_payload_is_mutation(request) -> bool:
        """Detect a GraphQL state-changing operation without recording payloads."""
        try:
            payload = request.post_data
        except Exception:
            payload = None
        if not payload:
            try:
                payload = request.post_data_json
            except Exception:
                payload = None
        if isinstance(payload, dict):
            operation = payload.get("operationName")
            if isinstance(operation, str) and operation.strip().lower().startswith("mutation"):
                return True
            payload = payload.get("query") or payload.get("extensions") or ""
        text = str(payload or "").lower()
        # A missing body is treated as unknown/non-GraphQL by the caller. The
        # explicit token check is deliberately narrow to avoid logging values.
        return bool(text and ("mutation" in text or "operationname\\\":\\\"mutation" in text))

    def _is_localization_context(
        self, url: str, method: str, host: str, path: str
    ) -> bool:
        """Match the verified first-party localization context request only."""
        if str(method or "").upper() != "POST" or host not in self.first_party_hosts:
            return False
        try:
            parsed = urlsplit(str(url or ""))
        except ValueError:
            return False
        return path == LOCALIZATION_CONTEXT_PATH and not parsed.query

    def _is_known_blocked_wati_event(
        self, url: str, method: str, host: str, path: str
    ) -> bool:
        """Recognize only the exact same-origin WATI add-to-cart app proxy."""
        if (
            str(method or "").upper() != "POST"
            or host not in self.first_party_hosts
            or path != WATI_ADD_TO_CART_EVENT_PATH
        ):
            return False
        fingerprint = safe_endpoint_fingerprint(
            url,
            method,
            self.first_party_hosts,
            canonical_origin=self.canonical_origin,
        )
        return (
            fingerprint.get("host_class") == "FIRST_PARTY"
            and fingerprint.get("same_origin") is True
            and fingerprint.get("query_present") is False
        )

    def _classification_reason(
        self, url: str, method: str, host: str, path: str, category: str
    ) -> Optional[str]:
        if category == self.EXPECTED and self._is_localization_context(url, method, host, path):
            return LOCALIZATION_CONTEXT_REASON
        return None

    def _safe_reason(self, category: str, url: str, method: str, host: str, path: str) -> str:
        """Describe an existing classification with a fixed safe enum only."""
        existing = self._classification_reason(url, method, host, path, category)
        if existing:
            return existing
        if category == self.KNOWN_BLOCKED_SIDE_EFFECT:
            return WATI_ADD_TO_CART_EVENT_REASON
        if category == self.HIGH_RISK:
            return "HIGH_RISK_PRECEDENCE"
        if category == self.EXPECTED:
            if path in EXPECTED_TRANSACTIONAL_MUTATION_PATHS or path in {"/checkout", "/checkouts"}:
                return "EXPECTED_CART_MUTATION" if path.startswith("/cart") else "EXPECTED_TRANSACTIONAL_MUTATION"
            return "EXPECTED_TRANSACTIONAL_MUTATION"
        return "PATH_NOT_ALLOWED"

    def _classify(self, url: str, method: str, request=None) -> Optional[str]:
        normalized_method = str(method or "").upper()
        if normalized_method not in MUTATION_METHODS:
            return None
        host = self._host(url)
        path = ReadonlyMutationGuard.normalize_path(url)
        if any(
            path == prefix.rstrip("/")
            or path.startswith(prefix if prefix.endswith("/") else prefix + "/")
            for prefix in NON_BUSINESS_MUTATION_PATH_PREFIXES
        ):
            return None

        # Shopify storefront and hosted Checkout GraphQL endpoints use POST
        # for read queries. Only a payload that explicitly declares a GraphQL
        # mutation enters the safety policy; its body is never persisted.
        if path.endswith("/graphql.json") or path.endswith("/graphql/persisted"):
            if request is None or not self._graphql_payload_is_mutation(request):
                return None

        first_party = host in self.first_party_hosts
        # Ignore non-storefront telemetry and app POSTs unless they target a
        # clearly high-risk payment/order host or path.
        if not first_party and not self._is_high_risk(path, host):
            return None
        if self._is_high_risk(path, host):
            return self.HIGH_RISK
        if self._is_known_blocked_wati_event(url, normalized_method, host, path):
            return self.KNOWN_BLOCKED_SIDE_EFFECT
        if path in EXPECTED_TRANSACTIONAL_MUTATION_PATHS or path in {"/checkout", "/checkouts"}:
            return self.EXPECTED
        if self._is_localization_context(url, normalized_method, host, path):
            return self.EXPECTED
        return self.UNEXPECTED

    def set_scope(
        self,
        viewport: Optional[str],
        journey: Optional[str],
        case_id: Optional[str],
        scope_name: Optional[str] = None,
    ) -> None:
        self._scope = {
            "viewport": viewport,
            "journey": journey,
            "case_id": case_id,
            "scope_name": scope_name,
        }

    def _handle_route(self, route) -> None:
        request = route.request
        url = getattr(request, "url", "")
        method = str(getattr(request, "method", "") or "").upper()
        category = self._classify(
            url, method, request
        )
        fallback = getattr(route, "fallback", None)
        if category is None:
            if callable(fallback):
                fallback()
            else:
                route.continue_()
            return

        normalized_path = ReadonlyMutationGuard.normalize_path(url)
        host = self._host(url)
        fingerprint = safe_endpoint_fingerprint(
            url,
            method,
            self.first_party_hosts,
            canonical_origin=self.canonical_origin,
        )
        # Preserve the existing normalized path representation in results;
        # only the newly added hash uses the original query-stripped pathname.
        fingerprint["sanitized_path"] = sanitize_pathname(normalized_path)
        reason = self._safe_reason(category, url, method, host, normalized_path)
        event = {
            "classification": category,
            **fingerprint,
            # Keep the historical schema's path field, but persist only the
            # sanitized pathname. The raw URL/path is used above for policy
            # decisions and is never copied into the event.
            "path": fingerprint["sanitized_path"],
            "reason": reason,
            "classification_reason": reason,
            **self._scope,
            "blocked": category != self.EXPECTED,
            "recognized": category in {self.EXPECTED, self.KNOWN_BLOCKED_SIDE_EFFECT},
            "allowed_to_send": category == self.EXPECTED,
            "gating_failure": category in {self.UNEXPECTED, self.HIGH_RISK},
        }
        self._events.append(event)
        if category == self.EXPECTED:
            if callable(fallback):
                fallback()
            else:
                route.continue_()
            return
        route.abort()

    def attach(self, context) -> None:
        if self._attached:
            return
        context.route("**/*", self._handler)
        self._context = context
        self._attached = True

    def detach(self) -> None:
        if not self._attached or self._context is None:
            return
        try:
            self._context.unroute("**/*", self._handler)
        except Exception:
            pass
        finally:
            self._context = None
            self._attached = False

    def events(self) -> List[dict]:
        return [dict(event) for event in self._events]

    def summary(self) -> dict:
        counts = Counter(event["classification"] for event in self._events)
        grouped = {}
        for event in self._events:
            key = (
                event.get("classification"),
                event.get("method"),
                event.get("host_class"),
                event.get("same_origin"),
                event.get("sanitized_path"),
                event.get("path_hash"),
                event.get("query_present"),
                event.get("reason"),
            )
            row = grouped.setdefault(
                key,
                {
                    "classification": event.get("classification"),
                    "method": event.get("method"),
                    "host_class": event.get("host_class", UNKNOWN),
                    "same_origin": event.get("same_origin", UNKNOWN),
                    "path": event.get("sanitized_path", "/"),
                    "sanitized_path": event.get("sanitized_path", "/"),
                    "path_hash": event.get("path_hash", UNKNOWN),
                    "query_present": event.get("query_present", UNKNOWN),
                    "reason": event.get("reason", "REDACTED"),
                    "count": 0,
                    "desktop_count": 0,
                    "mobile_count": 0,
                    "blocked_count": 0,
                    "blocked": bool(event.get("blocked")),
                    "recognized": bool(event.get("recognized")),
                    "allowed_to_send": bool(event.get("allowed_to_send")),
                    "gating_failure": bool(event.get("gating_failure")),
                    "_viewport_unknown": False,
                },
            )
            row["count"] += 1
            viewport = event.get("viewport")
            if viewport == "desktop":
                row["desktop_count"] += 1
            elif viewport == "mobile":
                row["mobile_count"] += 1
            else:
                row["_viewport_unknown"] = True
            row["blocked_count"] += int(bool(event.get("blocked")))

        paths = []
        for key in sorted(grouped, key=lambda value: tuple(str(item) for item in value)):
            row = grouped[key]
            if row.pop("_viewport_unknown"):
                row["desktop_count"] = UNKNOWN
                row["mobile_count"] = UNKNOWN
            paths.append(row)
        unexpected = counts[self.UNEXPECTED]
        high_risk = counts[self.HIGH_RISK]
        return {
            "mode": "TRANSACTIONAL_SAFE",
            "status": "PASS" if unexpected == 0 and high_risk == 0 else "FAIL",
            "expected_mutation": counts[self.EXPECTED],
            "known_blocked_side_effect": counts[self.KNOWN_BLOCKED_SIDE_EFFECT],
            "unexpected_mutation": unexpected,
            "high_risk_mutation": high_risk,
            "blocked_mutation": sum(1 for event in self._events if event["blocked"]),
            "by_path": paths,
        }


def merge_transactional_mutation_summaries(summaries: Iterable[dict]) -> dict:
    """Aggregate per-viewport policy summaries without exposing request data."""

    items = [summary for summary in summaries if isinstance(summary, dict)]
    by_fingerprint = {}
    expected = known_blocked = unexpected = high_risk = blocked = 0
    for summary in items:
        expected += int(summary.get("expected_mutation", 0) or 0)
        known_blocked += int(summary.get("known_blocked_side_effect", 0) or 0)
        unexpected += int(summary.get("unexpected_mutation", 0) or 0)
        high_risk += int(summary.get("high_risk_mutation", 0) or 0)
        blocked += int(summary.get("blocked_mutation", 0) or 0)
        for row in summary.get("by_path", []) or []:
            if not isinstance(row, dict):
                continue
            legacy_path = row.get("sanitized_path", row.get("path", "/"))
            safe_fallback = safe_endpoint_fingerprint(
                legacy_path,
                row.get("method", ""),
                (),
            )
            if "query_present" not in row:
                # Historical by_path rows contain only a query-stripped path;
                # they cannot prove whether a query was present.
                safe_fallback["query_present"] = UNKNOWN
            host_class = row.get("host_class")
            if host_class not in HOST_CLASSES:
                host_class = safe_fallback["host_class"]
            same_origin = row.get("same_origin", safe_fallback["same_origin"])
            if not isinstance(same_origin, bool) and same_origin != UNKNOWN:
                same_origin = UNKNOWN
            query_present = row.get("query_present", safe_fallback["query_present"])
            if not isinstance(query_present, bool) and query_present != UNKNOWN:
                query_present = UNKNOWN
            method = str(row.get("method") or "").upper()
            if method not in MUTATION_METHODS:
                method = safe_fallback["method"]
            classification = str(row.get("classification") or "")
            if classification not in {
                TransactionalMutationPolicy.EXPECTED,
                TransactionalMutationPolicy.KNOWN_BLOCKED_SIDE_EFFECT,
                TransactionalMutationPolicy.UNEXPECTED,
                TransactionalMutationPolicy.HIGH_RISK,
            }:
                classification = UNKNOWN
            path = sanitize_pathname(legacy_path)
            path_hash = str(row.get("path_hash") or "")
            if not re.fullmatch(r"[0-9a-f]{12}", path_hash):
                path_hash = safe_fallback["path_hash"]
            reason = row.get("reason") or row.get("classification_reason")
            if reason not in REASONS:
                reason = "REDACTED"
            key = (
                classification,
                method,
                host_class,
                same_origin,
                path,
                path_hash,
                query_present,
                reason,
            )
            count = int(row.get("count", 0) or 0)
            grouped = by_fingerprint.setdefault(
                key,
                {
                    "classification": key[0],
                    "method": key[1],
                    "host_class": key[2],
                    "same_origin": key[3],
                    "path": key[4],
                    "sanitized_path": key[4],
                    "path_hash": key[5],
                    "query_present": key[6],
                    "reason": key[7],
                    "count": 0,
                    "desktop_count": 0,
                    "mobile_count": 0,
                    "blocked_count": 0,
                    "blocked": key[0] != TransactionalMutationPolicy.EXPECTED,
                    "recognized": key[0]
                    in {
                        TransactionalMutationPolicy.EXPECTED,
                        TransactionalMutationPolicy.KNOWN_BLOCKED_SIDE_EFFECT,
                    },
                    "allowed_to_send": key[0] == TransactionalMutationPolicy.EXPECTED,
                    "gating_failure": key[0]
                    in {
                        TransactionalMutationPolicy.UNEXPECTED,
                        TransactionalMutationPolicy.HIGH_RISK,
                    },
                    "_viewport_unknown": False,
                },
            )
            grouped["count"] += count
            for viewport in ("desktop", "mobile"):
                value = row.get(f"{viewport}_count", UNKNOWN)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    grouped[f"{viewport}_count"] += value
                else:
                    grouped["_viewport_unknown"] = True
            blocked_count = row.get("blocked_count")
            if isinstance(blocked_count, int) and not isinstance(blocked_count, bool) and blocked_count >= 0:
                grouped["blocked_count"] += blocked_count
            else:
                grouped["blocked_count"] += count if key[0] != TransactionalMutationPolicy.EXPECTED else 0

    paths = []
    for key in sorted(by_fingerprint, key=lambda value: tuple(str(item) for item in value)):
        row = by_fingerprint[key]
        if row.pop("_viewport_unknown"):
            row["desktop_count"] = UNKNOWN
            row["mobile_count"] = UNKNOWN
        paths.append(row)
    return {
        "mode": "TRANSACTIONAL_SAFE",
        "status": "PASS" if unexpected == 0 and high_risk == 0 else "FAIL",
        "expected_mutation": expected,
        "known_blocked_side_effect": known_blocked,
        "unexpected_mutation": unexpected,
        "high_risk_mutation": high_risk,
        "blocked_mutation": blocked,
        "by_path": paths,
    }
