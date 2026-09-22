"""Offline tests for the fail-closed Readonly mutation guard."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from utils.mutation_fingerprint import format_mutation_fingerprint_report
from utils.readonly_mutation_guard import (
    LOCALIZATION_CONTEXT_REASON,
    ReadonlyMutationGuard,
    TransactionalMutationPolicy,
    merge_transactional_mutation_summaries,
)


class _FakeRoute:
    def __init__(self, url: str, method: str, post_data=None) -> None:
        self.request = SimpleNamespace(url=url, method=method, post_data=post_data)
        self.calls: list[str] = []

    def abort(self) -> None:
        self.calls.append("abort")

    def fallback(self) -> None:
        self.calls.append("fallback")

    def continue_(self) -> None:
        self.calls.append("continue")


class _FakeContext:
    def __init__(self) -> None:
        self.route_calls = []
        self.unroute_calls = []
        self.handler = None

    def route(self, pattern, handler) -> None:
        self.route_calls.append((pattern, handler))
        self.handler = handler

    def unroute(self, pattern, handler) -> None:
        self.unroute_calls.append((pattern, handler))


@pytest.mark.parametrize(
    "url",
    [
        "https://mondressy.com/cart/add",
        "https://mondressy.com/cart/add.js",
        "https://mondressy.com/cart/add.js?id=123",
        "/cart/change.js",
        "/cart/update",
        "/cart/clear.js?x=1",
        "https://MONDRESSY.COM/CART/ADD.JS/?x=1",
    ],
)
def test_cart_mutation_urls_match(url: str) -> None:
    assert ReadonlyMutationGuard.matches(url, "POST")


@pytest.mark.parametrize(
    "url",
    [
        "/cart",
        "/cart.js",
        "/products/test",
        "/search",
        "/collections/all",
        "/products/cart-add-style-dress",
        "https://www.google-analytics.com/collect",
    ],
)
def test_non_cart_urls_do_not_match(url: str) -> None:
    assert not ReadonlyMutationGuard.matches(url, "POST")


def test_get_cart_endpoint_is_not_treated_as_mutation() -> None:
    assert not ReadonlyMutationGuard.matches("/cart/add.js", "GET")
    assert not ReadonlyMutationGuard.matches("/cart/clear", "OPTIONS")


def test_guard_blocks_match_and_falls_back_non_match() -> None:
    context = _FakeContext()
    guard = ReadonlyMutationGuard()
    guard.attach(context)
    guard.set_scope("desktop", "browse", "RSMOKE-PDP-02")

    blocked = _FakeRoute("https://mondressy.com/cart/add.js?id=123", "post")
    context.handler(blocked)
    assert blocked.calls == ["abort"]
    assert guard.violations() == [
        {
            "method": "POST",
            "path": "/cart/add.js",
            "case_id": "RSMOKE-PDP-02",
            "journey": "browse",
            "viewport": "desktop",
        }
    ]

    normal = _FakeRoute("https://mondressy.com/analytics/collect", "POST")
    context.handler(normal)
    assert normal.calls == ["fallback"]
    assert guard.violation_count() == 1

    guard.detach()
    assert len(context.route_calls) == 1
    assert len(context.unroute_calls) == 1


def test_guard_records_safe_path_without_query_or_credentials() -> None:
    guard = ReadonlyMutationGuard()
    guard.set_scope("mobile", "search", "RSMOKE-SEARCH-02")
    route = _FakeRoute(
        "https://user:secret@mondressy.com/cart/update.js?token=secret",
        "PATCH",
    )

    guard._handle_route(route)

    violation = guard.violations()[0]
    assert violation["path"] == "/cart/update.js"
    assert "secret" not in str(violation)
    assert ReadonlyMutationGuard.safe_detail(violation) == "blocked cart mutation: PATCH /cart/update.js"


def test_transactional_policy_allows_expected_cart_mutations() -> None:
    context = _FakeContext()
    policy = TransactionalMutationPolicy(("mondressy.com", "www.mondressy.com"))
    policy.attach(context)
    policy.set_scope("desktop", "browse", "WSMOKE-CART-01", "CASE")

    route = _FakeRoute("https://www.mondressy.com/cart/add.js?token=redacted", "POST")
    context.handler(route)

    assert route.calls == ["fallback"]
    assert policy.events()[0]["classification"] == policy.EXPECTED
    assert policy.events()[0]["blocked"] is False
    summary = policy.summary()
    assert summary["status"] == "PASS"
    assert summary["expected_mutation"] == 1
    assert summary["unexpected_mutation"] == 0
    assert summary["high_risk_mutation"] == 0


@pytest.mark.parametrize(
    "host",
    ["mondressy.com", "www.mondressy.com", "lavetir.com", "www.lavetir.com"],
)
def test_transactional_policy_allows_first_party_localization_context(host: str) -> None:
    context = _FakeContext()
    policy = TransactionalMutationPolicy(
        ("mondressy.com", "www.mondressy.com", "lavetir.com", "www.lavetir.com")
    )
    policy.attach(context)

    route = _FakeRoute(f"https://{host}/localization", "POST")
    context.handler(route)

    assert route.calls == ["fallback"]
    assert policy.events()[0]["classification"] == policy.EXPECTED
    assert policy.events()[0]["classification_reason"] == LOCALIZATION_CONTEXT_REASON
    assert policy.events()[0]["blocked"] is False
    summary = policy.summary()
    assert summary["mode"] == "TRANSACTIONAL_SAFE"
    assert summary["status"] == "PASS"
    assert summary["expected_mutation"] == 1
    assert summary["unexpected_mutation"] == 0
    assert summary["high_risk_mutation"] == 0
    assert summary["blocked_mutation"] == 0
    row = summary["by_path"][0]
    assert row["classification"] == "EXPECTED_MUTATION"
    assert row["method"] == "POST"
    assert row["path"] == "/localization"
    assert row["host_class"] == "FIRST_PARTY"
    assert row["same_origin"] == "UNKNOWN"
    assert row["query_present"] is False
    assert row["reason"] == LOCALIZATION_CONTEXT_REASON
    assert row["count"] == 1
    assert row["blocked_count"] == 0


@pytest.mark.parametrize(
    "url",
    [
        "https://mondressy.com/localization/unsafe",
        "https://mondressy.com/localization/foo",
        "https://mondressy.com/unknown-write",
        "https://mondressy.com/localization?country=US",
    ],
)
def test_transactional_policy_blocks_localization_lookalikes_and_query_variants(url: str) -> None:
    context = _FakeContext()
    policy = TransactionalMutationPolicy(("mondressy.com", "www.mondressy.com"))
    policy.attach(context)

    route = _FakeRoute(url, "POST")
    context.handler(route)

    assert route.calls == ["abort"]
    assert policy.events()[0]["classification"] == policy.UNEXPECTED
    assert policy.events()[0]["blocked"] is True
    assert policy.summary()["status"] == "FAIL"


def test_transactional_policy_localization_normalization_keeps_trailing_slash_safe() -> None:
    context = _FakeContext()
    policy = TransactionalMutationPolicy(("mondressy.com", "www.mondressy.com"))
    policy.attach(context)

    route = _FakeRoute("https://MONDRESSY.COM/LOCALIZATION/", "POST")
    context.handler(route)

    assert route.calls == ["fallback"]
    assert policy.events()[0]["path"] == "/localization"
    assert policy.events()[0]["classification_reason"] == LOCALIZATION_CONTEXT_REASON


def test_transactional_policy_does_not_treat_get_or_third_party_localization_as_expected() -> None:
    context = _FakeContext()
    policy = TransactionalMutationPolicy(("mondressy.com", "www.mondressy.com"))
    policy.attach(context)

    get_route = _FakeRoute("https://mondressy.com/localization", "GET")
    third_party_route = _FakeRoute("https://third-party.example/localization", "POST")
    context.handler(get_route)
    context.handler(third_party_route)

    assert get_route.calls == ["fallback"]
    assert third_party_route.calls == ["fallback"]
    assert policy.events() == []


def test_transactional_policy_high_risk_host_precedes_localization_context_rule() -> None:
    context = _FakeContext()
    policy = TransactionalMutationPolicy(("paypal.com",))
    policy.attach(context)

    route = _FakeRoute("https://paypal.com/localization", "POST")
    context.handler(route)

    assert route.calls == ["abort"]
    assert policy.events()[0]["classification"] == policy.HIGH_RISK
    assert policy.summary()["high_risk_mutation"] == 1


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.mondressy.com/wishlist/add", "UNEXPECTED_MUTATION"),
        ("https://www.mondressy.com/account/login", "HIGH_RISK_MUTATION"),
        ("https://www.mondressy.com/checkouts/token/payment", "HIGH_RISK_MUTATION"),
        ("https://paypal.com/checkout", "HIGH_RISK_MUTATION"),
    ],
)
def test_transactional_policy_aborts_unexpected_and_high_risk_mutations(url: str, expected: str) -> None:
    context = _FakeContext()
    policy = TransactionalMutationPolicy(("mondressy.com", "www.mondressy.com"))
    policy.attach(context)
    policy.set_scope("mobile", "browse", "WSMOKE-CHECKOUT-01", "CASE")

    route = _FakeRoute(url, "POST")
    context.handler(route)

    assert route.calls == ["abort"]
    assert policy.events()[0]["classification"] == expected
    assert policy.events()[0]["blocked"] is True
    assert policy.summary()["status"] == "FAIL"


def test_transactional_policy_ignores_third_party_telemetry() -> None:
    context = _FakeContext()
    policy = TransactionalMutationPolicy(("www.mondressy.com",))
    policy.attach(context)

    route = _FakeRoute("https://www.google-analytics.com/collect", "POST")
    context.handler(route)

    assert route.calls == ["fallback"]
    assert policy.events() == []
    assert policy.summary()["status"] == "PASS"


@pytest.mark.parametrize(
    "path",
    [
        "/.well-known/shopify/monorail/unstable/produce_batch",
        "/.well-known/shopify/fec/produce",
        "/api/collect",
        "/api/event/collect",
        "/cdn-cgi/rum",
    ],
)
def test_transactional_policy_ignores_first_party_platform_telemetry(path: str) -> None:
    context = _FakeContext()
    policy = TransactionalMutationPolicy(("www.mondressy.com",))
    policy.attach(context)
    route = _FakeRoute(f"https://www.mondressy.com{path}", "POST")
    context.handler(route)

    assert route.calls == ["fallback"]
    assert policy.events() == []


def test_transactional_policy_treats_checkout_entry_and_read_graphql_as_safe_scope() -> None:
    context = _FakeContext()
    policy = TransactionalMutationPolicy(("www.mondressy.com",))
    policy.attach(context)

    checkout_entry = _FakeRoute("https://www.mondressy.com/checkouts", "POST")
    context.handler(checkout_entry)
    graphql_read = _FakeRoute(
        "https://www.mondressy.com/api/2026-01/graphql.json",
        "POST",
        '{"query":"query CartPreview { cart { id } }"}',
    )
    context.handler(graphql_read)

    assert checkout_entry.calls == ["fallback"]
    assert graphql_read.calls == ["fallback"]
    assert policy.summary()["expected_mutation"] == 1
    assert policy.summary()["status"] == "PASS"


def test_transactional_policy_blocks_post_to_existing_checkout_session() -> None:
    context = _FakeContext()
    policy = TransactionalMutationPolicy(("www.mondressy.com",))
    policy.attach(context)
    route = _FakeRoute("https://www.mondressy.com/checkouts/session-token", "POST")
    context.handler(route)

    assert route.calls == ["abort"]
    assert policy.summary()["high_risk_mutation"] == 1


def test_transactional_policy_blocks_graphql_state_change() -> None:
    context = _FakeContext()
    policy = TransactionalMutationPolicy(("www.mondressy.com",))
    policy.attach(context)
    route = _FakeRoute(
        "https://www.mondressy.com/api/2026-01/graphql.json",
        "POST",
        '{"query":"mutation SubmitOrder { submitOrder { id } }"}',
    )
    context.handler(route)

    assert route.calls == ["abort"]
    assert policy.summary()["unexpected_mutation"] == 1
    assert policy.summary()["high_risk_mutation"] == 0
    assert policy.summary()["status"] == "FAIL"


def test_mutation_observability_keeps_policy_semantics_and_sanitizes_unknown_endpoint() -> None:
    context = _FakeContext()
    policy = TransactionalMutationPolicy(
        ("mondressy.com", "www.mondressy.com"),
        canonical_origin="https://mondressy.com",
    )
    policy.attach(context)

    for viewport in ("desktop", "mobile"):
        policy.set_scope(viewport, "browse", "WSMOKE-SEARCH-04", "CASE")
        for path, expected, has_query in (
            ("/cart/add.js", "EXPECTED_MUTATION", True),
            ("/cart/change.js", "EXPECTED_MUTATION", False),
            ("/localization", "EXPECTED_MUTATION", False),
            ("/apps/example/action/123456789012", "UNEXPECTED_MUTATION", True),
            (
                "/apps/example/profile/customer@example.test/"
                "123e4567-e89b-12d3-a456-426614174000/123456789012",
                "UNEXPECTED_MUTATION",
                True,
            ),
            ("/customer/998877665544", "HIGH_RISK_MUTATION", True),
            ("/checkouts/session-token/payment", "HIGH_RISK_MUTATION", True),
        ):
            query = "?token=synthetic-query-secret&customer=private@example.test" if has_query else ""
            route = _FakeRoute(
                "https://mondressy.com"
                + path
                + query,
                "POST",
            )
            route.request.headers = {
                "cookie": "synthetic-cookie-secret",
                "Authorization": "synthetic-authorization-secret",
                "Signature": "synthetic-signature-secret",
            }
            route.request.post_data = "synthetic-body-secret"
            context.handler(route)
            event = policy.events()[-1]
            assert event["classification"] == expected
            assert route.calls == (["fallback"] if expected == "EXPECTED_MUTATION" else ["abort"])

    events_text = str(policy.events())
    summary_text = str(policy.summary())
    for sensitive in (
        "synthetic-query-secret",
        "synthetic-cookie-secret",
        "synthetic-authorization-secret",
        "synthetic-signature-secret",
        "synthetic-body-secret",
        "private@example.test",
        "123456789012",
        "customer@example.test",
        "123e4567-e89b-12d3-a456-426614174000",
        "998877665544",
        "session-token",
        "https://mondressy.com",
    ):
        assert sensitive not in events_text
        assert sensitive not in summary_text

    console_report = "\n".join(format_mutation_fingerprint_report(policy.summary()))
    for sensitive in (
        "synthetic-query-secret",
        "synthetic-cookie-secret",
        "synthetic-authorization-secret",
        "synthetic-signature-secret",
        "synthetic-body-secret",
        "customer@example.test",
        "123e4567-e89b-12d3-a456-426614174000",
        "123456789012",
        "998877665544",
        "https://mondressy.com",
    ):
        assert sensitive not in console_report

    unexpected = next(
        row for row in policy.summary()["by_path"]
        if row["classification"] == "UNEXPECTED_MUTATION"
    )
    assert unexpected["sanitized_path"] == "/apps/example/action/{id}"
    assert unexpected["host_class"] == "FIRST_PARTY"
    assert unexpected["same_origin"] is True
    assert unexpected["query_present"] is True
    assert unexpected["reason"] == "PATH_NOT_ALLOWED"
    assert unexpected["count"] == 2
    assert unexpected["desktop_count"] == 1
    assert unexpected["mobile_count"] == 1
    assert unexpected["blocked_count"] == 2


def test_high_risk_precedence_and_fingerprint_reason_are_preserved() -> None:
    context = _FakeContext()
    policy = TransactionalMutationPolicy(("paypal.com",))
    policy.attach(context)
    route = _FakeRoute("https://paypal.com/localization?token=private", "POST")
    context.handler(route)

    event = policy.events()[0]
    assert route.calls == ["abort"]
    assert event["classification"] == "HIGH_RISK_MUTATION"
    assert event["reason"] == "HIGH_RISK_PRECEDENCE"
    assert event["host_class"] == "FIRST_PARTY"
    assert event["same_origin"] == "UNKNOWN"
    assert event["query_present"] is True


def test_merge_aggregates_desktop_and_mobile_counts_by_safe_fingerprint() -> None:
    summaries = []
    for viewport in ("desktop", "mobile"):
        context = _FakeContext()
        policy = TransactionalMutationPolicy(
            ("mondressy.com",),
            canonical_origin="https://mondressy.com",
        )
        policy.attach(context)
        policy.set_scope(viewport, "browse", "WSMOKE-SEARCH-04", "CASE")
        context.handler(
            _FakeRoute(
                "https://mondressy.com/apps/example/action/123456789012?token=private",
                "POST",
            )
        )
        summaries.append(policy.summary())

    merged = merge_transactional_mutation_summaries(summaries)
    row = merged["by_path"][0]
    assert merged["unexpected_mutation"] == 2
    assert merged["blocked_mutation"] == 2
    assert row["count"] == 2
    assert row["desktop_count"] == 1
    assert row["mobile_count"] == 1
    assert row["blocked_count"] == 2
    assert row["path"] == "/apps/example/action/{id}"


def test_transactional_mutation_summaries_merge_without_request_data() -> None:
    merged = merge_transactional_mutation_summaries(
        [
            {
                "mode": "TRANSACTIONAL_SAFE",
                "status": "PASS",
                "expected_mutation": 2,
                "unexpected_mutation": 0,
                "high_risk_mutation": 0,
                "blocked_mutation": 0,
                "by_path": [
                    {"classification": "EXPECTED_MUTATION", "method": "POST", "path": "/cart/add.js", "count": 2}
                ],
            },
            {
                "mode": "TRANSACTIONAL_SAFE",
                "status": "FAIL",
                "expected_mutation": 1,
                "unexpected_mutation": 0,
                "high_risk_mutation": 1,
                "blocked_mutation": 1,
                "by_path": [
                    {"classification": "HIGH_RISK_MUTATION", "method": "POST", "path": "/account/login", "count": 1}
                ],
            },
        ]
    )
    assert merged["status"] == "FAIL"
    assert merged["expected_mutation"] == 3
    assert merged["high_risk_mutation"] == 1
    assert "token" not in str(merged)
