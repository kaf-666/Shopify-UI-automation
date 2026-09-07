"""Offline contract tests for the partial Lavetir Step 4B profile."""

from __future__ import annotations

from pages.home_page import HomePage
from pages.navigation import NavigationPage
from pages.search_page import SearchPage
from utils.browser import load_site_config
from utils.config import resolve_url


LAVETIR = load_site_config("lavetir")


def test_lavetir_profile_contains_only_step4b_page_groups() -> None:
    assert LAVETIR["site"] == "lavetir"
    assert resolve_url(LAVETIR["base_url"], "site.base_url") == "https://www.lavetir.com"
    assert set(LAVETIR["pages"]) == {"home", "search", "navigation"}


def test_lavetir_home_and_search_selector_contract_resolves() -> None:
    home = HomePage(None, LAVETIR, "desktop")
    search = SearchPage(None, LAVETIR, "desktop")

    for name in ("logo", "search", "cart"):
        selector = home.resolve_selector(name)
        assert selector["by"] == "css"
        assert selector["value"]

    for name in (
        "container",
        "input",
        "submit",
        "close",
        "predictive_results",
        "predictive_product",
        "results_grid",
        "result_card",
        "result_link",
    ):
        selector = search.resolve_selector(name)
        assert selector["by"] == "css"
        assert selector["value"]


def test_lavetir_search_trigger_is_resolved_per_viewport() -> None:
    desktop = HomePage(None, LAVETIR, "desktop")
    mobile = HomePage(None, LAVETIR, "mobile")

    assert desktop.resolve_selector("search")["value"] == (
        "a.js-search-header:not(.medium-up--hide)"
    )
    assert mobile.resolve_selector("search")["value"] == (
        "a.js-search-header.medium-up--hide"
    )


def test_lavetir_navigation_has_readiness_selectors_only() -> None:
    navigation = LAVETIR["pages"]["navigation"]
    selectors = navigation["selectors"]
    required = {"header", "desktop_menu", "desktop_menu_item", "mobile_trigger"}
    forbidden_journey = {
        "desktop_submenu",
        "target_collection",
        "mobile_target_parent",
        "mobile_menu",
        "mobile_close",
    }

    assert required.issubset(selectors)
    assert not forbidden_journey.intersection(selectors)

    for viewport, required_name in (("desktop", "desktop_menu_item"), ("mobile", "mobile_trigger")):
        page = NavigationPage(None, LAVETIR, viewport)
        for name in ("header", required_name):
            selector = page.resolve_selector(name)
            assert selector["by"] == "css"
            assert selector["value"]


def test_lavetir_profile_does_not_require_deferred_page_groups() -> None:
    pages = LAVETIR["pages"]
    for deferred in ("collection", "product", "cart", "checkout"):
        assert deferred not in pages
