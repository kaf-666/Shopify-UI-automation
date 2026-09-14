"""Offline contract tests for the Lavetir site profile."""

from __future__ import annotations

from pages.home_page import HomePage
from pages.navigation import NavigationPage
from pages.search_page import SearchPage
from utils.browser import load_site_config
from utils.config import resolve_url


LAVETIR = load_site_config("lavetir")


def test_lavetir_profile_contains_current_page_groups() -> None:
    assert LAVETIR["schema_version"] == 1
    assert LAVETIR["site"] == "lavetir"
    assert resolve_url(LAVETIR["base_url"], "site.base_url") == "https://www.lavetir.com"
    assert set(LAVETIR["pages"]) == {
        "home",
        "search",
        "navigation",
        "collection",
        "product",
        "cart",
        "checkout",
    }


def test_lavetir_signed_request_access_contract() -> None:
    access = LAVETIR["access"]

    assert access["mode"] == "signed_request"
    assert access["source"] == "env"
    assert access["env"] == {
        "Signature": "LAVETIR_US_SHOPIFY_SIGNATURE",
        "Signature-Input": "LAVETIR_US_SHOPIFY_SIGNATURE_INPUT",
        "Signature-Agent": "LAVETIR_US_SHOPIFY_SIGNATURE_AGENT",
    }
    assert access["allowed_hosts"] == ["lavetir.com", "www.lavetir.com"]


def test_lavetir_capabilities_describe_full_and_readonly_safe_suites() -> None:
    assert LAVETIR["capabilities"] == {
        "suites": {
            "website_smoke_readonly_v1": True,
            "website_smoke_v1": True,
        },
        "navigation": {
            "desktop": "mega_menu_hover",
            "mobile": "drawer_accordion",
        },
        "collection": {"product_cards": True, "filters": False, "sort": False},
        "product": {"color": True, "size": True, "custom_size": True},
    }


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


def test_lavetir_navigation_has_full_step4c_journey_contract() -> None:
    navigation = LAVETIR["pages"]["navigation"]
    selectors = navigation["selectors"]
    required = {
        "header",
        "desktop_menu",
        "desktop_menu_item",
        "desktop_target_parent",
        "mobile_trigger",
        "mobile_drawer",
        "mobile_drawer_open",
        "mobile_menu",
        "mobile_menu_item",
        "mobile_close",
        "mobile_target_parent",
        "target_collection",
    }

    assert required.issubset(selectors)
    assert "desktop_submenu" not in selectors
    assert navigation["smoke_collection"] == {
        "name": "Mother of the Bride Dresses",
        "path": "/collections/mother-of-the-bride-dresses",
        "notes": "Verified collection target in Lavetir desktop/mobile menus; GET-only navigation target",
    }

    for name in required:
        page = NavigationPage(None, LAVETIR, "desktop")
        selector = page.resolve_selector(name)
        assert selector["by"] == "css"
        assert selector["value"]

    for viewport, required_name in (("desktop", "desktop_menu_item"), ("mobile", "mobile_trigger")):
        page = NavigationPage(None, LAVETIR, viewport)
        for name in ("header", required_name):
            selector = page.resolve_selector(name)
            assert selector["by"] == "css"
            assert selector["value"]


def test_lavetir_full_cart_and_checkout_selector_contract() -> None:
    cart = LAVETIR["pages"]["cart"]
    assert cart["url"] == "/cart"
    cart_selectors = cart["selectors"]
    for name in (
        "drawer",
        "cart_item",
        "quantity_input",
        "quantity_plus",
        "quantity_minus",
        "remove",
        "subtotal",
        "checkout_button",
    ):
        assert cart_selectors[name]["by"] == "css"
        assert cart_selectors[name]["value"]
    assert "cart__remove-button" in cart_selectors["remove"]["value"]

    checkout = LAVETIR["pages"]["checkout"]
    checkout_selectors = checkout["selectors"]
    for name in ("root", "contact", "delivery", "shipping_form", "email", "express"):
        assert checkout_selectors[name]["by"] == "css"
        assert checkout_selectors[name]["value"]


def test_lavetir_collection_and_product_readonly_contract() -> None:
    collection = LAVETIR["pages"]["collection"]
    assert collection["url"] == "/collections/mother-of-the-bride-dresses"
    assert collection["selectors"]["product_grid"]["value"] == "#CollectionAjaxContent"
    assert (
        collection["selectors"]["product_card"]["value"]
        == "#CollectionAjaxContent .grid-product"
    )
    assert collection["selectors"]["product_link"]["value"] == "a.grid-product__link"
    assert collection["selectors"]["product_title"]["value"] == ".grid-product__title"

    product = LAVETIR["pages"]["product"]
    assert product["url"] == (
        "/products/a-line-princess-chiffon-scoop-3-4-sleeves-"
        "mother-of-the-bride-dresses-with-appliques-ruffles-12010206a1"
    )
    selectors = product["selectors"]
    assert selectors["purchase_area"]["value"] == "form.product-single__form"
    assert selectors["title"]["value"] == ".product-single__title"
    assert selectors["price"]["value"] == ".product__price"
    assert selectors["gallery"]["value"] == ".product-slideshow"
    assert selectors["color"]["value"] == ".shopify-color-selector"
    assert (
        selectors["add_to_cart"]["value"]
        == 'form.product-single__form button[name="add"]'
    )
    assert product["color_option_control"] == {"strategy": "associated_label"}

    models = product["size_resolver"]["models"]
    assert len(models) == 1
    assert models[0]["id"] == "SIZE_MODEL_04"
    assert models[0]["group_selector"] == ".shopify-size-selector"
    assert models[0]["control_strategy"] == "associated_label"
    assert models[0]["custom_size_value"] == "Custom Size (Inch)"
