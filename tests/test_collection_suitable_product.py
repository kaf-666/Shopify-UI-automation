"""Candidate fallback for dynamic collection ordering."""
from types import SimpleNamespace

from pages.collection_page import CollectionPage


class _FakeCollection(CollectionPage):
    def __init__(self):
        self.collection = "https://shop.example/collections/dresses"
        self.page = SimpleNamespace(url=self.collection, goto=self._goto)
        self.opened = []
        self.navigations = []

    def _goto(self, url, **_kwargs):
        self.navigations.append(url)
        self.page.url = url

    def visible_product_cards(self):
        return SimpleNamespace(count=lambda: 3)

    def open_product(self, index):
        assert self.page.url == self.collection
        self.opened.append(index)
        self.page.url = f"https://shop.example/products/{index}"
        return self.page.url


def test_suitable_product_skips_single_colour_first_cards():
    collection = _FakeCollection()

    index, page = collection.open_suitable_product(
        lambda current: current.url.endswith("/2"), max_candidates=5
    )

    assert index == 2
    assert page is collection.page
    assert collection.opened == [0, 1, 2]
    assert collection.navigations == [collection.collection, collection.collection]


def test_unsuitable_candidates_raise_after_bound():
    collection = _FakeCollection()

    try:
        collection.open_suitable_product(lambda _current: False, max_candidates=2)
    except RuntimeError as exc:
        assert "No suitable product among first 2 cards" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected a bounded candidate failure")
    assert collection.opened == [0, 1]
    assert collection.navigations == [collection.collection]
