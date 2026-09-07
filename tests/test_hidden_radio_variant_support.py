"""Offline regression tests for radio options with visible associated controls."""

from __future__ import annotations

import inspect

import pytest
from playwright.sync_api import sync_playwright

from pages.product_page import ProductPage


@pytest.fixture(scope="module")
def browser():
    playwright = sync_playwright().start()
    instance = playwright.chromium.launch(headless=True)
    try:
        yield instance
    finally:
        instance.close()
        playwright.stop()


@pytest.fixture
def page(browser):
    current = browser.new_page()
    try:
        yield current
    finally:
        current.close()


def _product_config(*, color_control: dict | None = None) -> dict:
    product = {
        "url": "/products/synthetic",
        "selectors": {
            "purchase_area": {"by": "css", "value": "#purchase"},
            "title": {"by": "css", "value": "#title"},
            "color": {"by": "css", "value": "#colors"},
            "add_to_cart": {"by": "css", "value": "#atc"},
        },
    }
    if color_control is not None:
        product["color_option_control"] = color_control
    return {"base_url": "https://synthetic.test", "pages": {"product": product}}


def _size_product_config() -> dict:
    config = _product_config(color_control={"strategy": "associated_label"})
    config["pages"]["product"]["size_resolver"] = {
        "models": [
            {
                "id": "SYNTHETIC_SIZE_RADIO",
                "group_selector": "#sizes",
                "option_selector": (
                    "input[type='radio'][name='properties[Size]']"
                ),
                "wait_option_selector": (
                    "input[type='radio'][name='properties[Size]']"
                ),
                "control_strategy": "associated_label",
                "required_attributes": {},
                "custom_size_value": "Custom Size",
                "disabled_class_tokens": ["disabled", "unavailable"],
            }
        ]
    }
    return config


def _color_markup(
    *, visible_radio: bool = False, with_label: bool = True, disabled: str = ""
) -> str:
    hidden = "" if visible_radio else " hidden"
    label = '<label for="color-blue">Blue</label>' if with_label else ""
    return f"""
        <form id="purchase">
          <h1 id="title">Synthetic Product</h1>
          <div id="colors">
            {label}
            <input id="color-blue" type="radio" name="Color" value="Blue"{hidden} {disabled}>
          </div>
          <button id="atc" type="button">Add to cart</button>
        </form>
    """


def _size_markup(*, disabled: str = "disabled", aria_disabled: str = "") -> str:
    return f"""
        <form id="purchase">
          <h1 id="title">Synthetic Product</h1>
          <div id="colors">
            <input id="color-blue" type="radio" name="Color" value="Blue">
          </div>
          <div id="sizes">
            <label for="size-s">S</label>
            <input id="size-s" type="radio" form="purchase"
                   name="properties[Size]" value="S" hidden>
            <label for="size-m">M</label>
            <input id="size-m" type="radio" form="purchase"
                   name="properties[Size]" value="M" hidden>
            <label for="size-custom">CUSTOM SIZE</label>
            <input id="size-custom" type="radio" form="purchase"
                   name="properties[Size]" value="Custom Size" hidden>
            <label for="size-disabled">L</label>
            <input id="size-disabled" type="radio" form="purchase"
                   name="properties[Size]" value="L" hidden {disabled}{aria_disabled}>
          </div>
          <button id="atc" type="button">Add to cart</button>
        </form>
    """


def test_hidden_color_radio_uses_visible_label_and_checked_state(page) -> None:
    page.set_content(_color_markup())
    product = ProductPage(page, _product_config(color_control={"strategy": "associated_label"}), "desktop")

    assert product.available_color_count() == 1
    assert product.first_available_color() == "Blue"
    assert product.select_color() == "Blue"
    assert product.get_selected_color() == "Blue"

    source = inspect.getsource(ProductPage.select_color)
    assert "force=True" not in source
    assert "evaluate" not in source


def test_visible_color_radio_regression_remains_supported(page) -> None:
    page.set_content(_color_markup(visible_radio=True, with_label=False))
    product = ProductPage(page, _product_config(), "desktop")

    assert product.available_color_count() == 1
    assert product.select_color() == "Blue"
    assert product.get_selected_color() == "Blue"


def test_disabled_hidden_color_radio_is_unavailable(page) -> None:
    page.set_content(_color_markup(disabled="disabled"))
    product = ProductPage(
        page,
        _product_config(color_control={"strategy": "associated_label"}),
        "desktop",
    )

    assert product.available_color_count() == 0


def test_hidden_size_radio_supports_normal_selection_and_excludes_custom(page) -> None:
    page.set_content(_size_markup())
    product = ProductPage(page, _size_product_config(), "desktop")
    resolver = product._size_resolver()

    assert resolver.require_group().model == "SYNTHETIC_SIZE_RADIO"
    assert [option.value for option in resolver.available_options()] == [
        "S",
        "M",
        "Custom Size",
    ]
    assert [option.value for option in resolver.normal_available_options()] == [
        "S",
        "M",
    ]
    assert resolver.snapshot()["custom_size_present"] is True
    assert resolver.first_available_value() == "S"
    assert product.select_size() == "S"
    assert product.get_selected_size() == "S"

    with pytest.raises(RuntimeError, match="Custom Size"):
        product.select_size("Custom Size")


@pytest.mark.parametrize(
    "disabled,aria_disabled",
    [("disabled", ""), ("", 'aria-disabled="true"')],
)
def test_disabled_hidden_size_radio_is_unavailable(
    page, disabled: str, aria_disabled: str
) -> None:
    page.set_content(_size_markup(disabled=disabled, aria_disabled=aria_disabled))
    product = ProductPage(page, _size_product_config(), "desktop")
    resolver = product._size_resolver()

    available = {option.value for option in resolver.available_options()}
    assert "L" not in available
    assert "S" in available


def test_hidden_radio_without_control_is_unavailable(page) -> None:
    page.set_content(_color_markup(with_label=False))
    product = ProductPage(page, _product_config(), "desktop")

    assert product.available_color_count() == 0
    with pytest.raises(RuntimeError, match="No available color"):
        product.first_available_color()
