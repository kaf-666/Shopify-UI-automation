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


def _color_options_markup(options: list[dict], *, hidden: bool = False) -> str:
    inputs = []
    for index, option in enumerate(options):
        option_id = f"color-{index}"
        value = option["value"]
        checked = " checked" if option.get("checked") else ""
        disabled = " disabled" if option.get("disabled") else ""
        hidden_attr = " hidden" if hidden else ""
        inputs.append(
            f'<label for="{option_id}">{value}</label>'
            f'<input id="{option_id}" type="radio" name="Color" '
            f'value="{value}"{hidden_attr}{checked}{disabled}>'
        )
    return f"""
        <form id="purchase">
          <h1 id="title">Synthetic Product</h1>
          <div id="colors">{"".join(inputs)}</div>
          <button id="atc" type="button">Add to cart</button>
        </form>
        <script>
          window.colorClickCounts = new Map();
          document.querySelectorAll('#colors input[type="radio"]').forEach((input) => {{
            window.colorClickCounts.set(input.value, 0);
            input.addEventListener('click', () => {{
              window.colorClickCounts.set(input.value, window.colorClickCounts.get(input.value) + 1);
            }});
          }});
        </script>
    """


def _color_product(page, options: list[dict], *, hidden: bool = False) -> ProductPage:
    page.set_content(_color_options_markup(options, hidden=hidden))
    return ProductPage(
        page,
        _product_config(color_control={"strategy": "associated_label"}),
        "desktop",
    )


def _color_click_count(page, value: str) -> int:
    return page.evaluate("value => window.colorClickCounts.get(value)", value)


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
    readiness = resolver.readiness_snapshot()
    assert readiness["size_model"] == "SYNTHETIC_SIZE_RADIO"
    assert readiness["size_group_detected"] is True
    assert readiness["size_option_total"] == 4
    assert readiness["size_option_available"] == 1
    assert readiness["normal_size_available"] == 1
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


def test_unique_selected_available_color_is_accepted(page) -> None:
    product = _color_product(page, [{"value": "As Pic", "checked": True}])

    assert product.available_color_count() == 1
    assert product.first_available_color() == "As Pic"
    assert product.select_color() == "As Pic"
    assert product.get_selected_color() == "As Pic"
    assert page.locator("#color-0").is_checked()
    assert not page.locator("#color-0").is_disabled()
    assert _color_click_count(page, "As Pic") == 0


def test_unique_unselected_available_color_is_selected(page) -> None:
    product = _color_product(page, [{"value": "Ivory"}])

    assert product.first_available_color() == "Ivory"
    assert product.select_color() == "Ivory"
    assert product.get_selected_color() == "Ivory"
    assert _color_click_count(page, "Ivory") == 1


def test_multiple_colors_prefer_an_unselected_available_color(page) -> None:
    product = _color_product(
        page,
        [
            {"value": "Blue", "checked": True},
            {"value": "Rose"},
        ],
    )

    assert product.first_available_color() == "Rose"
    assert product.select_color() == "Rose"
    assert product.get_selected_color() == "Rose"
    assert _color_click_count(page, "Blue") == 0
    assert _color_click_count(page, "Rose") == 1


def test_partially_disabled_colors_are_excluded_from_auto_selection(page) -> None:
    product = _color_product(
        page,
        [
            {"value": "Unavailable", "disabled": True},
            {"value": "Mauve", "checked": True},
            {"value": "Navy"},
        ],
    )

    assert product.available_color_count() == 2
    assert product.first_available_color() == "Navy"
    assert product.select_color() == "Navy"
    assert product.get_selected_color() == "Navy"
    assert _color_click_count(page, "Unavailable") == 0
    assert _color_click_count(page, "Navy") == 1


def test_all_disabled_colors_fail_closed(page) -> None:
    product = _color_product(
        page,
        [
            {"value": "Blue", "disabled": True},
            {"value": "Rose", "disabled": True},
        ],
    )

    assert product.available_color_count() == 0
    with pytest.raises(RuntimeError, match="No available color"):
        product.first_available_color()
    with pytest.raises(RuntimeError, match="No available color"):
        product.select_color()


def test_missing_colors_fail_closed(page) -> None:
    product = _color_product(page, [])

    assert product.available_color_count() == 0
    with pytest.raises(RuntimeError, match="No available color"):
        product.first_available_color()
    with pytest.raises(RuntimeError, match="No available color"):
        product.select_color()


def test_checked_but_disabled_color_is_not_available(page) -> None:
    product = _color_product(
        page, [{"value": "As Pic", "checked": True, "disabled": True}]
    )

    assert product.get_selected_color() == "As Pic"
    assert product.available_color_count() == 0
    with pytest.raises(RuntimeError, match="No available color"):
        product.first_available_color()
    with pytest.raises(RuntimeError, match="No available color"):
        product.select_color()
    with pytest.raises(LookupError, match="Color option not found: As Pic"):
        product.select_color("As Pic")


def test_hidden_radio_with_visible_label_remains_selectable(page) -> None:
    product = _color_product(page, [{"value": "As Pic"}], hidden=True)

    assert product.available_color_count() == 1
    assert product.select_color() == "As Pic"
    assert page.locator("#color-0").is_checked()
    assert product.get_selected_color() == "As Pic"
    assert _color_click_count(page, "As Pic") == 1


def test_explicitly_selected_color_is_not_clicked_again(page) -> None:
    product = _color_product(page, [{"value": "Red", "checked": True}])

    assert product.select_color("Red") == "Red"
    assert product.get_selected_color() == "Red"
    assert _color_click_count(page, "Red") == 0


def test_selection_return_value_matches_final_checked_color(page) -> None:
    product = _color_product(
        page,
        [
            {"value": "Dusty Pink"},
            {"value": "Lilac"},
        ],
    )

    selected = product.select_color()

    assert selected == "Dusty Pink"
    assert product.get_selected_color() == selected
    assert _color_click_count(page, selected) == 1
