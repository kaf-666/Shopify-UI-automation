"""Shared Search and Cart state contracts stay selector and capability driven."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from playwright.sync_api import sync_playwright

from pages.cart_drawer import CartDrawer
from pages.search_page import SearchPage
from utils.browser import load_site_config
from utils.site_config_validator import SiteConfigValidationError, validate_site_config_mapping


@pytest.mark.parametrize(
    ("text", "key", "expected"),
    [
        ("Color: Black", "color", "Black"),
        ("Size: US4", "size", "US4"),
        ("Color: Black Size: US4", "color", "Black"),
        ("Color: Black Size: US4", "size", "US4"),
        ("Color: Black\nSize: US4", "color", "Black"),
        ("Color: Black\nSize: US4", "size", "US4"),
        ("Color: Black Size: US4 Rush Order: No", "color", "Black"),
        ("Color: Black Size: US4 Rush Order: No", "size", "US4"),
        (" Color \t : \n Black \t Size \n: \t US4 ", "color", "Black"),
        ("cOlOr: Black sIzE: US4", "size", "US4"),
        ("Size: US4", "color", ""),
        ("Color: Black", "size", ""),
        ("Custom Size: M Size: US4", "size", "US4"),
        ("Color Family: Crimson Color: Black", "color", "Black"),
        ("Size:\n  US4", "size", "US4"),
    ],
)
def test_cart_variant_property_parser(text: str, key: str, expected: str) -> None:
    assert CartDrawer._extract_labeled_property(text, key) == expected


def test_cart_variant_getters_search_across_property_containers() -> None:
    config = deepcopy(load_site_config("mondressy"))
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page()
        page.set_content(
            "<div id='CartDrawer'><div class='cart__item'>"
            "<div class='cart__item--properties'>Custom Size: M</div>"
            "<div class='cart__item--properties'>Color Family: Crimson</div>"
            "<div class='cart__item--properties'>Color: Black</div>"
            "<div class='cart__item--properties'>Size: US4</div>"
            "</div></div>"
        )
        drawer = CartDrawer(page, config, "desktop")
        assert drawer.get_item_color() == "Black"
        assert drawer.get_item_size() == "US4"
    finally:
        browser.close()
        playwright.stop()


def test_legacy_cart_label_parser_remains_compatible() -> None:
    assert CartDrawer._parse_label_value("Size:\n  US4") == "US4"


def test_enter_search_does_not_require_submit_button() -> None:
    config = deepcopy(load_site_config("mondressy"))
    config["pages"]["search"]["selectors"].pop("submit")
    validate_site_config_mapping(config, site_name="mondressy")
    config["pages"]["search"]["submit_strategy"] = "button"
    with pytest.raises(SiteConfigValidationError, match="selectors.submit"):
        validate_site_config_mapping(config, site_name="mondressy")


def test_search_and_cart_open_state_follow_profile_selectors() -> None:
    config = deepcopy(load_site_config("mondressy"))
    config["pages"]["search"]["selectors"]["open_state"]["value"] = "#search.open"
    config["pages"]["cart"]["selectors"]["drawer_open"]["value"] = "#cart[open]"
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page()
        page.set_content('<div id="search">Search</div><div id="cart">Cart</div>')
        search = SearchPage(page, config, "desktop")
        cart = CartDrawer(page, config, "desktop")
        assert not search.is_open()
        assert not cart.is_open()
        page.locator("#search").evaluate("el => el.classList.add('open')")
        page.locator("#cart").evaluate("el => el.setAttribute('open', '')")
        search.wait_open()
        cart.wait_open()
        assert search.is_open() and cart.is_open()
        page.locator("#search").evaluate("el => el.classList.remove('open')")
        search.wait_closed()
    finally:
        browser.close()
        playwright.stop()


def test_optional_predictive_ui_does_not_delay_enter_readiness() -> None:
    config = deepcopy(load_site_config("mondressy"))
    selectors = config["pages"]["search"]["selectors"]
    for name in ("predictive_loading", "predictive_results", "predictive_product"):
        selectors.pop(name, None)
    search = SearchPage(object(), config, "desktop")
    search._wait_submit_ready(timeout_ms=1)
    assert search.predictive_product_count() == 0


def test_optional_overlay_is_closed_with_its_real_control_before_search_entry() -> None:
    config = deepcopy(load_site_config("mondressy"))
    config["pages"]["search"]["selectors"]["blocking_overlay_dismiss"] = {
        "by": "css", "value": "#modal button"
    }
    config["pages"]["search"]["blocking_overlay_wait_ms"] = 500
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page()
        page.set_content(
            '<div id="modal" style="display:none"><button onclick="this.parentElement.remove()">No, thanks</button></div>'
            '<button id="trigger" onclick="document.body.dataset.open = 1">Search</button>'
            '<script>setTimeout(() => document.querySelector("#modal").style.display = "block", 30)</script>'
        )
        search = SearchPage(page, config, "desktop")
        home = SimpleNamespace(
            search_trigger=lambda: page.locator("#trigger"),
            open_search=lambda: page.locator("#trigger").click(),
        )
        search._open_entry(home)
        assert page.locator("#modal").count() == 0
        assert page.locator("body").get_attribute("data-open") == "1"
    finally:
        browser.close()
        playwright.stop()
