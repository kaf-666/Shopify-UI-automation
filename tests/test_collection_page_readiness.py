"""Contract tests for shared collection product readiness."""

from __future__ import annotations

import pytest

from pages.collection_page import (
    CollectionPage,
    CollectionProductsReadinessError,
)


def _cards(count: int, *, href: str = "/products/item", links: bool = True) -> list[dict]:
    return [
        {
            "visible": True,
            "links": ([{"visible": True, "href": href}] if links else []),
        }
        for _ in range(count)
    ]


def _state(
    cards: list[dict],
    *,
    grid_count: int = 1,
    grid_visible: bool = True,
    ready_state: str = "complete",
) -> dict:
    return {
        "grid": [
            {"visible": grid_visible}
            for _ in range(grid_count)
        ],
        "cards": cards,
        "ready_state": ready_state,
    }


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _Locator:
    def __init__(self, resolver):
        self._resolver = resolver

    def _items(self) -> list[dict]:
        return list(self._resolver())

    @property
    def first(self):
        return self.nth(0)

    def nth(self, index: int):
        return _Locator(lambda: self._items()[index : index + 1])

    def locator(self, selector: str):
        assert selector == ".product-link"
        return _Locator(
            lambda: [link for item in self._items() for link in item.get("links", [])]
        )

    def filter(self, *, visible: bool):
        assert visible is True
        return _Locator(lambda: [item for item in self._items() if item.get("visible")])

    def count(self) -> int:
        return len(self._items())

    def is_visible(self) -> bool:
        items = self._items()
        return bool(items and items[0].get("visible"))

    def get_attribute(self, name: str):
        assert name == "href"
        items = self._items()
        return items[0].get("href") if items else None


class _Page:
    def __init__(self, states: list[dict], clock: _Clock) -> None:
        self.states = states
        self.clock = clock
        self.state_index = 0
        self.url = "https://shop.example/collections/dresses"
        self.goto_url = None
        self.locator_calls = 0
        self.waits: list[int] = []

    @property
    def state(self) -> dict:
        return self.states[min(self.state_index, len(self.states) - 1)]

    def locator(self, selector: str):
        self.locator_calls += 1
        if selector == ".grid":
            return _Locator(lambda: self.state["grid"])
        if selector == ".card":
            return _Locator(lambda: self.state["cards"])
        raise AssertionError(f"unexpected selector: {selector}")

    def evaluate(self, expression: str):
        assert expression == "document.readyState"
        return self.state["ready_state"]

    def wait_for_timeout(self, timeout_ms: int) -> None:
        self.waits.append(timeout_ms)
        self.clock.advance(timeout_ms / 1000)
        if self.state_index + 1 < len(self.states):
            self.state_index += 1

    def goto(self, url: str, **_kwargs) -> None:
        self.goto_url = url
        self.url = url


def _collection(states: list[dict], monkeypatch) -> tuple[CollectionPage, _Page, _Clock]:
    clock = _Clock()
    monkeypatch.setattr("pages.collection_page.time.monotonic", clock)
    page = _Page(states, clock)
    config = {
        "base_url": "https://shop.example",
        "pages": {
            "collection": {
                "selectors": {
                    "product_grid": {"by": "css", "value": ".grid"},
                    "product_card": {"by": "css", "value": ".card"},
                    "product_link": {"by": "css", "value": ".product-link"},
                }
            }
        },
    }
    return CollectionPage(page, config, "mobile"), page, clock


def _wait(collection: CollectionPage, **kwargs) -> dict:
    return collection.wait_for_products_ready(
        timeout_ms=kwargs.pop("timeout_ms", 1_000),
        poll_interval_ms=kwargs.pop("poll_interval_ms", 100),
        stable_snapshots=kwargs.pop("stable_snapshots", 2),
        **kwargs,
    )


def test_delayed_product_mount_becomes_ready(monkeypatch) -> None:
    collection, _page, _clock = _collection(
        [_state([]), _state(_cards(20)), _state(_cards(20))], monkeypatch
    )

    result = _wait(collection)

    assert result["product_card_count"] == 20
    assert result["product_link_count"] == 20
    assert result["poll_attempts"] == 3


def test_persistent_zero_times_out_with_structured_diagnostic(monkeypatch) -> None:
    collection, _page, _clock = _collection([_state([])], monkeypatch)

    with pytest.raises(CollectionProductsReadinessError) as captured:
        _wait(collection, timeout_ms=300)

    detail = str(captured.value)
    assert "target_index=0" in detail
    assert "grid_count=1" in detail
    assert "grid_visible=True" in detail
    assert "product_card_count=0" in detail
    assert "product_link_count=0" in detail
    assert "elapsed_ms=300" in detail
    assert "poll_attempts=4" in detail
    assert "consecutive_ready_snapshots=0" in detail


def test_dom_update_uses_live_locator_and_eventually_passes(monkeypatch) -> None:
    states = [
        _state([]),
        _state(_cards(20, href="/products/old")),
        _state(_cards(20, links=False)),
        _state(_cards(20, href="/products/new")),
        _state(_cards(20, href="/products/new")),
    ]
    collection, page, _clock = _collection(states, monkeypatch)

    result = _wait(collection)

    assert result["target_href"] == "/products/new"
    assert result["poll_attempts"] == 5
    assert page.locator_calls >= 15


def test_cards_without_links_are_not_ready(monkeypatch) -> None:
    collection, _page, _clock = _collection(
        [_state(_cards(3, links=False))], monkeypatch
    )

    with pytest.raises(CollectionProductsReadinessError, match="product_link_count=0"):
        _wait(collection, timeout_ms=200)


def test_empty_href_is_not_ready(monkeypatch) -> None:
    collection, _page, _clock = _collection(
        [_state(_cards(3, href=""))], monkeypatch
    )

    with pytest.raises(CollectionProductsReadinessError) as captured:
        _wait(collection, timeout_ms=200)

    assert "target_href=" in str(captured.value)
    assert "target_href_valid=False" in str(captured.value)


@pytest.mark.parametrize("href", ["#", "javascript:void(0)", "/collections/dresses"])
def test_non_product_href_is_not_ready(monkeypatch, href: str) -> None:
    collection, _page, _clock = _collection(
        [_state(_cards(3, href=href))], monkeypatch
    )

    with pytest.raises(CollectionProductsReadinessError, match="target_href_valid=False"):
        _wait(collection, timeout_ms=200)


def test_fast_path_only_pays_one_stability_poll(monkeypatch) -> None:
    collection, page, _clock = _collection(
        [_state(_cards(20)), _state(_cards(20))], monkeypatch
    )

    result = _wait(collection)

    assert result["elapsed_ms"] == 100
    assert result["poll_attempts"] == 2
    assert page.waits == [100]


def test_target_index_requires_enough_cards(monkeypatch) -> None:
    states = [
        _state(_cards(4)),
        _state(_cards(5, href="/products/fifth")),
        _state(_cards(5, href="/products/fifth")),
    ]
    collection, _page, _clock = _collection(states, monkeypatch)

    result = _wait(collection, index=4)

    assert result["target_index"] == 4
    assert result["product_card_count"] == 5
    assert result["target_href"] == "/products/fifth"
    assert result["poll_attempts"] == 3


def test_target_index_selects_visible_cards_not_hidden_raw_matches(monkeypatch) -> None:
    hidden = {"visible": False, "links": [{"visible": False, "href": "/products/hidden"}]}
    visible = {"visible": True, "links": [{"visible": True, "href": "/products/visible"}]}
    collection, _page, _clock = _collection(
        [_state([hidden, visible]), _state([hidden, visible])], monkeypatch
    )

    result = _wait(collection)

    assert result["product_card_count"] == 2
    assert result["product_card_visible_count"] == 1
    assert result["target_href"] == "/products/visible"


def test_stability_window_resets_after_not_ready_snapshot(monkeypatch) -> None:
    states = [
        _state(_cards(2)),
        _state([]),
        _state(_cards(2)),
        _state(_cards(2)),
    ]
    diagnostics: list[dict] = []
    collection, _page, _clock = _collection(states, monkeypatch)

    result = _wait(collection, diagnostics_hook=diagnostics.append)

    assert [item["consecutive_ready_snapshots"] for item in diagnostics] == [1, 0, 1, 2]
    assert result["poll_attempts"] == 4


def test_open_product_waits_then_requeries_target_link(monkeypatch) -> None:
    collection, page, _clock = _collection(
        [
            _state([]),
            _state(_cards(1, href="/collections/dresses/products/final")),
            _state(_cards(1, href="/collections/dresses/products/final")),
        ],
        monkeypatch,
    )
    monkeypatch.setattr("pages.collection_page.COLLECTION_PRODUCTS_POLL_MS", 100)

    result = collection.open_product(0)

    assert page.goto_url == "https://shop.example/products/final"
    assert result == "https://shop.example/products/final"
