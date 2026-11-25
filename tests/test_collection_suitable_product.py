"""Candidate fallback for dynamic collection ordering."""
from types import SimpleNamespace

from pages.collection_page import CollectionPage


class _FakeCollection(CollectionPage):
    def __init__(self):
        self.collection = "https://shop.example/collections/dresses"
        self.page = SimpleNamespace(url=self.collection, goto=self._goto)
        self.opened = []
        self.navigations = []
        self.wait_calls = []

    def _goto(self, url, **_kwargs):
        self.navigations.append(url)
        self.page.url = url

    def wait_for_products_ready(self, index=0, **_kwargs):
        self.wait_calls.append(index)
        return {"target_index": index}

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


def test_candidate_return_keeps_disposal_mark_until_grace(monkeypatch):
    from utils.request_boundary import RequestBoundary

    collection = _FakeCollection()
    context = SimpleNamespace(route=lambda *_: None, pages=[])
    collection.page.context = context
    boundary = RequestBoundary(context)
    context._shopify_request_boundary = boundary
    events = []
    boundary.begin_page_disposal = lambda pages: events.append(("begin", list(pages)))
    boundary.end_page_disposal = lambda pages: events.append(("end", list(pages)))
    boundary.begin_navigation_reads = lambda pages: events.append(("nav_begin", list(pages)))
    boundary.end_navigation_reads = lambda pages: events.append(("nav_end", list(pages)))
    collection.page.wait_for_timeout = lambda ms: events.append(("wait", ms))

    def fake_navigation(page, action, **_kwargs):
        events.append(("navigate", page.url))
        return action()

    monkeypatch.setattr("utils.request_boundary.run_navigation", fake_navigation)

    try:
        collection.open_suitable_product(lambda _current: False, max_candidates=2)
    except RuntimeError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected a bounded candidate failure")

    assert events == [
        ("nav_begin", [collection.page]),
        ("begin", [collection.page]),
        ("navigate", "https://shop.example/products/0"),
        ("wait", 1_500),
        ("end", [collection.page]),
        ("wait", 1_500),
        ("nav_end", [collection.page]),
    ]


class _DelayedCardsCollection(_FakeCollection):
    def __init__(self):
        super().__init__()
        self.card_count = 0

    def wait_for_products_ready(self, index=0, **_kwargs):
        self.wait_calls.append(index)
        self.card_count = 3
        return {"target_index": index}

    def visible_product_cards(self):
        return SimpleNamespace(count=lambda: self.card_count)


def test_waits_for_collection_cards_before_counting():
    collection = _DelayedCardsCollection()

    index, page = collection.open_suitable_product(
        lambda _current: True, max_candidates=1
    )

    assert index == 0
    assert page is collection.page
    assert collection.wait_calls == [0]
    assert collection.opened == [0]
