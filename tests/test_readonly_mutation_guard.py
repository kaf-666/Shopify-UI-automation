"""Offline tests for the fail-closed Readonly mutation guard."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from utils.readonly_mutation_guard import ReadonlyMutationGuard


class _FakeRoute:
    def __init__(self, url: str, method: str) -> None:
        self.request = SimpleNamespace(url=url, method=method)
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
