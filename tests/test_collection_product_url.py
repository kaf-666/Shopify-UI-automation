"""Offline tests for canonical Shopify product links from collection cards."""

from __future__ import annotations

import pytest
from types import SimpleNamespace
from urllib.parse import urljoin

from pages.collection_page import CollectionPage


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        ("/products/foo", "/products/foo"),
        (
            "/collections/dresses/products/foo",
            "/products/foo",
        ),
        (
            "/collections/dresses/products/foo?variant=123",
            "/products/foo?variant=123",
        ),
        (
            "https://shop.example/collections/dresses/products/foo",
            "https://shop.example/products/foo",
        ),
        (
            "https://shop.example/collections/dresses/products/foo?variant=123",
            "https://shop.example/products/foo?variant=123",
        ),
        (
            "/collections/dresses/products/foo?variant=123#details",
            "/products/foo?variant=123#details",
        ),
        (
            "/collections/random/products/generic-product",
            "/products/generic-product",
        ),
    ],
)
def test_canonical_product_href(href: str, expected: str) -> None:
    assert CollectionPage._canonical_product_href(href) == expected


def test_canonical_product_href_rejects_non_product_url() -> None:
    with pytest.raises(RuntimeError, match="not a product URL"):
        CollectionPage._canonical_product_href("/collections/dresses/pages/about")


class _FakeLink:
    def __init__(self, href: str, page) -> None:
        self.href = href
        self.page = page

    @property
    def first(self):
        return self

    def get_attribute(self, name: str):
        return self.href if name == "href" else None

    def click(self, **_kwargs):
        self.page.clicked = True
        self.page.url = urljoin(self.page.url, self.href)


class _FakeCard:
    def __init__(self, href: str, page) -> None:
        self.link = _FakeLink(href, page)

    def locator(self, selector: str):
        assert selector == ".product-link"
        return self.link


class _FakeCards:
    def __init__(self, href: str, page) -> None:
        self.card = _FakeCard(href, page)

    def count(self) -> int:
        return 1

    def filter(self, *, visible: bool):
        assert visible is True
        return self

    def nth(self, index: int):
        assert index == 0
        return self.card


class _FakePage:
    def __init__(self, href: str) -> None:
        self.cards = _FakeCards(href, self)
        self.url = "https://shop.example/collections/dresses"
        self.goto_url = None
        self.clicked = False
        self.context = SimpleNamespace(on=lambda *_args: None, remove_listener=lambda *_args: None)

    def locator(self, selector: str):
        assert selector == ".product-card"
        return self.cards

    def goto(self, url: str, **_kwargs) -> None:
        self.goto_url = url
        self.url = url


def test_open_product_navigates_to_canonical_product_route(monkeypatch) -> None:
    monkeypatch.setattr(
        CollectionPage, "wait_for_products_ready", lambda self, index=0: {"target_index": index}
    )
    page = _FakePage("/collections/dresses/products/bar")
    config = {
        "base_url": "https://shop.example",
        "pages": {
            "collection": {
                "selectors": {
                    "product_card": {"by": "css", "value": ".product-card"},
                    "product_link": {"by": "css", "value": ".product-link"},
                }
            }
        },
    }

    result = CollectionPage(page, config, "desktop").open_product(0)

    assert page.clicked and page.goto_url is None
    assert result == "https://shop.example/collections/dresses/products/bar"


def test_open_product_keeps_existing_canonical_route(monkeypatch) -> None:
    monkeypatch.setattr(
        CollectionPage, "wait_for_products_ready", lambda self, index=0: {"target_index": index}
    )
    page = _FakePage("/products/bar")
    config = {
        "base_url": "https://shop.example",
        "pages": {
            "collection": {
                "selectors": {
                    "product_card": {"by": "css", "value": ".product-card"},
                    "product_link": {"by": "css", "value": ".product-link"},
                }
            }
        },
    }

    result = CollectionPage(page, config, "desktop").open_product(0)

    assert page.clicked and page.goto_url is None
    assert result == "https://shop.example/products/bar"


@pytest.mark.parametrize("href", ["https://evil.example/products/bar", "//evil.example/products/bar",
                                  "https://shop.example:8443/products/bar",
                                  "https://user@shop.example/products/bar"])
def test_open_product_rejects_external_destination_before_navigation(monkeypatch, href):
    monkeypatch.setattr(CollectionPage, "wait_for_products_ready", lambda self, index=0: {})
    page = _FakePage(href)
    config = {"base_url": "https://shop.example", "pages": {"collection": {"selectors": {
        "product_card": {"by": "css", "value": ".product-card"},
        "product_link": {"by": "css", "value": ".product-link"}}}}}
    with pytest.raises(RuntimeError, match="outside the configured storefront"):
        CollectionPage(page, config, "desktop").open_product(0)
    assert page.goto_url is None
