"""Unit contracts for the checkout Proposal sync observer."""

from __future__ import annotations

import json

from pages.checkout_page import CheckoutPage


class _Request:
    def __init__(self, url: str, payload=None) -> None:
        self.url = url
        self.post_data = json.dumps(payload) if isinstance(payload, dict) else payload


class _Response:
    def __init__(self, request: _Request) -> None:
        self.request = request


class _Page:
    def __init__(self, clock: list[float]) -> None:
        self._clock = clock
        self.handlers = {"request": [], "response": []}
        self.waits: list[int] = []

    def on(self, event: str, handler) -> None:
        self.handlers[event].append(handler)

    def wait_for_timeout(self, ms: int) -> None:
        self.waits.append(ms)
        self._clock[0] += ms / 1000.0

    def emit_request(self, request: _Request) -> None:
        for handler in self.handlers["request"]:
            handler(request)

    def emit_response(self, response: _Response) -> None:
        for handler in self.handlers["response"]:
            handler(response)


class _PageWithoutEvents:
    def wait_for_timeout(self, ms: int) -> None:  # pragma: no cover - not called
        raise AssertionError("wait_cart_sync must not poll without an observer")


_CONFIG = {"base_url": "https://shop.example", "pages": {"checkout": {"selectors": {}}}}
_PROPOSAL_URL = "https://shop.example/checkouts/token/graphql/persisted"


def _checkout(page, monkeypatch, clock):
    monkeypatch.setattr("pages.checkout_page.time.monotonic", lambda: clock[0])
    checkout = CheckoutPage(page, _CONFIG, "mobile")
    checkout.observe_cart_sync()
    return checkout


def test_proposal_settles_after_response_and_quiet_window(monkeypatch) -> None:
    clock = [100.0]
    page = _Page(clock)
    checkout = _checkout(page, monkeypatch, clock)
    request = _Request(_PROPOSAL_URL, {"operationName": "Proposal"})
    page.emit_request(request)
    clock[0] = 100.1
    page.emit_response(_Response(request))
    clock[0] = 102.0

    assert checkout.wait_cart_sync(timeout_ms=1000, quiet_ms=1500) == "SETTLED"
    assert page.waits == []


def test_pending_proposal_times_out_without_failing(monkeypatch) -> None:
    clock = [100.0]
    page = _Page(clock)
    checkout = _checkout(page, monkeypatch, clock)
    page.emit_request(_Request(_PROPOSAL_URL, {"operationName": "Proposal"}))
    clock[0] = 101.0

    assert checkout.wait_cart_sync(timeout_ms=300, quiet_ms=1500) == "TIMEOUT_PENDING"
    assert page.waits


def test_no_proposal_is_reported_not_observed(monkeypatch) -> None:
    clock = [100.0]
    page = _Page(clock)
    checkout = _checkout(page, monkeypatch, clock)

    assert checkout.wait_cart_sync(timeout_ms=250, quiet_ms=1500) == "NOT_OBSERVED"
    assert page.waits


def test_other_graphql_operations_are_ignored(monkeypatch) -> None:
    clock = [100.0]
    page = _Page(clock)
    checkout = _checkout(page, monkeypatch, clock)
    request = _Request(_PROPOSAL_URL, {"operationName": "CountryDetails"})
    page.emit_request(request)
    page.emit_response(_Response(request))
    clock[0] = 100.1

    assert checkout.wait_cart_sync(timeout_ms=250, quiet_ms=1500) == "NOT_OBSERVED"


def test_invalid_post_data_is_ignored(monkeypatch) -> None:
    clock = [100.0]
    page = _Page(clock)
    checkout = _checkout(page, monkeypatch, clock)
    request = _Request(_PROPOSAL_URL, "\x8bnot-json")
    page.emit_request(request)
    page.emit_response(_Response(request))
    clock[0] = 100.1

    assert checkout.wait_cart_sync(timeout_ms=250, quiet_ms=1500) == "NOT_OBSERVED"


def test_page_without_event_api_is_unavailable(monkeypatch) -> None:
    clock = [100.0]
    monkeypatch.setattr("pages.checkout_page.time.monotonic", lambda: clock[0])
    checkout = CheckoutPage(_PageWithoutEvents(), _CONFIG, "mobile")
    checkout.observe_cart_sync()

    assert checkout.wait_cart_sync() == "UNAVAILABLE"
