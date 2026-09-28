"""Offline checks for NavigationCaseRunner's page and collection contracts."""

from types import SimpleNamespace
from pathlib import Path

import pytest

from tests.navigation_cases import NavigationCaseRunner
from pages.navigation import NavigationCollectionError
from flows.shopping_flow import FlowError


class _Card:
    def count(self):
        return 1

    def is_visible(self):
        return True


class _Link:
    def count(self):
        return 1

    def is_visible(self):
        return True

    def is_enabled(self):
        return True


class _Collection:
    def __init__(self):
        self.card = _Card()
        self.link = _Link()
        self.waited = False

    def wait_for_products_ready(self, index):
        assert index == 0
        self.waited = True

    def product_count(self):
        return 2

    def product_cards(self):
        return SimpleNamespace(first=self.card)

    def visible_product_cards(self):
        return SimpleNamespace(first=self.card)

    def product_link(self, card):
        assert card is self.card
        return self.link


@pytest.mark.parametrize("site", ["mondressy", "lavetir"])
def test_nav04_uses_collection_page_contract(site):
    from utils.browser import load_site_config

    runner = NavigationCaseRunner(SimpleNamespace(page=object()), load_site_config(site), "desktop")
    collection = _Collection()
    runner.state["coll"] = collection
    assert "card_interactive=True" in runner._case_nav04()
    assert collection.waited


def test_nav03_adopts_popup_for_collection_and_evidence(monkeypatch):
    home = SimpleNamespace(url="https://example.test/")
    popup = SimpleNamespace(url="https://example.test/collections/target")
    runner = NavigationCaseRunner(SimpleNamespace(page=home), {}, "desktop", Path("artifacts/test"))
    runner.state["nav"] = SimpleNamespace(
        page=popup,
        open_collection=lambda: popup.url,
        target_path=lambda: "/collections/target",
    )

    class Collection:
        def __init__(self, page, *_args):
            assert page is popup

        def product_grid(self):
            return SimpleNamespace(wait_for=lambda **_kwargs: None, count=lambda: 1, is_visible=lambda: True)

    monkeypatch.setattr("tests.navigation_cases.CollectionPage", Collection)
    assert "grid=True" in runner._case_nav03()
    assert runner.page is popup
    seen = []
    monkeypatch.setattr(
        "tests.navigation_cases.capture_case_failure",
        lambda page, *_args: seen.append(page) or "mobile/NAV-03-failure.png",
    )
    runner._capture_evidence("NAV-03")
    assert seen == [popup]


def test_nav03_wrong_popup_is_used_for_failure_evidence(monkeypatch):
    home = SimpleNamespace(url="https://example.test/")
    popup = SimpleNamespace(url="https://example.test/collections/wrong")
    runner = NavigationCaseRunner(SimpleNamespace(page=home), {}, "desktop", Path("artifacts/test"))

    def wrong_target():
        raise NavigationCollectionError("wrong target", actual_page=popup)

    runner.state["nav"] = SimpleNamespace(page=popup, open_collection=wrong_target)
    with pytest.raises(FlowError, match="wrong target"):
        runner._case_nav03()
    assert runner.page is popup
    seen = []
    monkeypatch.setattr(
        "tests.navigation_cases.capture_case_failure",
        lambda page, *_args: seen.append(page) or "desktop/NAV-03-failure.png",
    )
    runner._capture_evidence("NAV-03")
    assert seen == [popup]
