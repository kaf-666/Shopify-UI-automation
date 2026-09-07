"""Offline tests for canonical Shopify product links from collection cards."""

from __future__ import annotations

import pytest

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
    def __init__(self, href: str) -> None:
        self.href = href

    @property
    def first(self):
        return self

    def get_attribute(self, name: str):
        return self.href if name == "href" else None


class _FakeCard:
    def __init__(self, href: str) -> None:
        self.link = _FakeLink(href)

    def locator(self, selector: str):
        assert selector == "a.grid-product__link"
        return self.link


class _FakeCards:
    def __init__(self, href: str) -> None:
        self.card = _FakeCard(href)

    def count(self) -> int:
        return 1

    def nth(self, index: int):
        assert index == 0
        return self.card


class _FakePage:
    def __init__(self, href: str) -> None:
        self.cards = _FakeCards(href)
        self.url = "https://shop.example/collections/dresses"
        self.goto_url = None

    def locator(self, selector: str):
        assert selector == ".product-card"
        return self.cards

    def goto(self, url: str, **_kwargs) -> None:
        self.goto_url = url
        self.url = url


def test_open_product_navigates_to_canonical_product_route() -> None:
    page = _FakePage("/collections/dresses/products/bar")
    config = {
        "base_url": "https://shop.example",
        "pages": {
            "collection": {
                "selectors": {
                    "product_card": {"by": "css", "value": ".product-card"}
                }
            }
        },
    }

    result = CollectionPage(page, config, "desktop").open_product(0)

    assert page.goto_url == "https://shop.example/products/bar"
    assert result == "https://shop.example/products/bar"


def test_open_product_keeps_existing_canonical_route() -> None:
    page = _FakePage("/products/bar")
    config = {
        "base_url": "https://shop.example",
        "pages": {
            "collection": {
                "selectors": {
                    "product_card": {"by": "css", "value": ".product-card"}
                }
            }
        },
    }

    result = CollectionPage(page, config, "desktop").open_product(0)

    assert result == "https://shop.example/products/bar"
