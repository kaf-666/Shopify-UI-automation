"""Real DOM regressions for #56 combined Color/Size metadata containers."""
import pytest
from playwright.sync_api import sync_playwright

from pages.base_page import BasePage
from pages.cart_drawer import CartDrawer
from flows.shopping_flow import FlowError, ShoppingFlow
from tests.website_smoke_v1_cases import WebsiteSmokeV1Runner


@pytest.fixture(scope="module", params=["chromium", "webkit"])
def browser(request):
    with sync_playwright() as playwright:
        instance = getattr(playwright, request.param).launch(headless=True)
        yield instance
        instance.close()


@pytest.fixture
def page(browser):
    current = browser.new_page()
    yield current
    current.close()


def drawer(page, metadata):
    config = BasePage.load_site_config(site_name="mondressy")
    page.set_content(f'''<div id="CartDrawer" class="drawer--is-open">
      <div class="cart__item"><a class="cart__item-name">Offline product</a>
        {metadata}<input name="updates[]" value="1"></div></div>''')
    return CartDrawer(page, config, "desktop")


@pytest.mark.parametrize("metadata", [
    '<div class="cart__item--variants">Color: As Pic<br>Size: M</div>',
    '<div class="cart__item--variants"><span>Color: As Pic</span> <span>Size: M</span></div>',
    '<div class="cart__item--variants">Size: M<br>Color: As Pic</div>',
    '<div class="cart__item--variants">Color:<br>As Pic<br>Size:<br>M</div>',
    '<div class="cart__item--variants">Color: As Pic</div><div class="cart__item--properties">Size: M</div>',
])
def test_color_and_size_are_independent_exact_values(page, metadata):
    cart = drawer(page, metadata)
    assert cart.get_item_color() == "As Pic"
    assert cart.get_item_size() == "M"


@pytest.mark.parametrize("metadata,color,size", [
    ('Color: Pink', 'Pink', ''), ('Size: XL', '', 'XL'),
    ('Custom Color: Pink', '', ''), ('Color Code: Pink', '', ''),
    ('Description: Multi Color Size: XL', '', ''),
    ('Color: Multi Color<br>Size: 16W', 'Multi Color', '16W'),
    ('Color: Pink<br>Color: Blue<br>Size: M', '', 'M'),
])
def test_missing_unrelated_or_ambiguous_fields_do_not_fake_a_match(page, metadata, color, size):
    cart = drawer(page, f'<div class="cart__item--variants">{metadata}</div>')
    assert cart.get_item_color() == color
    assert cart.get_item_size() == size


@pytest.mark.parametrize("color,size,category", [
    ("As Pic", "M", None), ("Pink", "M", "PRODUCT_COLOR_MISMATCH"),
    ("As Pic", "S", "PRODUCT_SIZE_MISMATCH"),
])
def test_production_capture_and_original_full_consistency_assertion(page, color, size, category):
    cart = drawer(page, f'<div class="cart__item--variants">Color: {color}<br>Size: {size}</div>')
    runner = object.__new__(WebsiteSmokeV1Runner)
    runner.flow = ShoppingFlow(page, cart.site_config, "desktop")
    runner.flow.state.update(pdp_title="Offline product", color="As Pic", size="M")
    runner.flow.capture_cart_state()
    runner.state = {}
    if category is None:
        assert runner._c_cart02() == "product=match color=match size=match qty=1"
    else:
        with pytest.raises(FlowError) as error:
            runner._c_cart02()
        assert error.value.category == category
