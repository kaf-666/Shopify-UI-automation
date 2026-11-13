"""Regression coverage for post-quantity promotion dialogs in the cart drawer."""

from copy import deepcopy

import pytest
from playwright.sync_api import sync_playwright

from pages.cart_drawer import CartDrawer
from utils.browser import load_site_config


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch(headless=True)
        yield instance
        instance.close()


@pytest.fixture
def page(browser):
    current = browser.new_page()
    yield current
    current.close()


def _shirees_config():
    config = deepcopy(load_site_config("shirees"))
    cart = config["pages"]["cart"]["selectors"]
    cart["drawer"] = {"by": "css", "value": "#drawer"}
    cart["drawer_open"] = {"by": "css", "value": "#drawer.open"}
    cart["cart_item"] = {"by": "css", "value": ".item"}
    cart["quantity_input"] = {"by": "css", "value": ".qty"}
    cart["quantity_plus"] = {"by": "css", "value": ".plus"}
    cart["quantity_minus"] = {"by": "css", "value": ".minus"}
    config["pages"]["home"]["selectors"]["cart"] = {
        "by": "css",
        "value": "#cart-trigger",
    }
    return config


def _set_promotion_page(page, *, cover_dialog=False):
    overlay = (
        '<div style="position:absolute;inset:0;z-index:10"></div>'
        if cover_dialog
        else ""
    )
    page.set_content(
        f"""
        <base href="https://example.test/">
        <button id="cart-trigger"
                onclick="document.querySelector('#drawer').classList.add('open')">
          Cart
        </button>
        <aside id="drawer" class="open">
          <div class="item">
            <input class="qty" value="1">
            <button class="plus"
                    onclick="
                      const input = this.parentElement.querySelector('.qty');
                      input.value = '2';
                      window.__giftBusy = true;
                      fetch('/cart/change.js', {{
                        method: 'POST',
                        headers: {{'Content-Type': 'application/json'}},
                        body: '{{}}'
                      }});
                      setTimeout(() => {{
                        document.querySelector('#drawer').classList.remove('open');
                        document.querySelector('[data-promotion-gift-dialog]').showModal();
                      }}, 100);
                    ">+</button>
            <button class="minus"
                    onclick="this.parentElement.querySelector('.qty').value = '1'">-</button>
          </div>
        </aside>
        <dialog data-promotion-gift-dialog style="position:relative">
          {overlay}
          <button data-gift-skip
                  onclick="this.closest('dialog').close()">No, thanks</button>
        </dialog>
        """
    )
    page.evaluate(
        """() => {
          window.__giftBusy = false;
          window.promotionGift = {busy: () => window.__giftBusy};
          document.querySelector('[data-promotion-gift-dialog]')
            .addEventListener('close', () => { window.__giftBusy = false; });
        }"""
    )


def test_promotion_gift_dialog_is_dismissed_and_drawer_reopened(page):
    _set_promotion_page(page)
    page.evaluate(
        """() => {
          window.__giftBusy = true;
          setTimeout(() => {
            document.querySelector('#drawer').classList.remove('open');
            document.querySelector('[data-promotion-gift-dialog]').showModal();
          }, 100);
        }"""
    )
    drawer = CartDrawer(page, _shirees_config(), "desktop")

    assert drawer.settle_quantity_promotion(timeout_ms=3_000) is True
    assert page.locator("[data-promotion-gift-dialog][open]").count() == 0
    assert page.locator("#drawer.open").count() == 1
    assert page.locator(".item .qty").input_value() == "1"


def test_promotion_gift_click_falls_back_when_pointer_is_intercepted(page):
    _set_promotion_page(page, cover_dialog=True)
    page.evaluate(
        """() => {
          window.__giftBusy = true;
          setTimeout(() => {
            document.querySelector('#drawer').classList.remove('open');
            document.querySelector('[data-promotion-gift-dialog]').showModal();
          }, 100);
        }"""
    )
    drawer = CartDrawer(page, _shirees_config(), "desktop")

    assert drawer.settle_quantity_promotion(timeout_ms=3_000) is True
    assert page.locator("[data-promotion-gift-dialog][open]").count() == 0
    assert page.locator("#drawer.open").count() == 1


def test_promotion_gift_reopens_offscreen_cart_trigger(page):
    _set_promotion_page(page)
    page.eval_on_selector(
        "#cart-trigger",
        "element => { element.style.position = 'fixed'; element.style.left = '-10000px'; }",
    )
    page.evaluate(
        """() => {
          window.__giftBusy = true;
          setTimeout(() => {
            document.querySelector('#drawer').classList.remove('open');
            document.querySelector('[data-promotion-gift-dialog]').showModal();
          }, 100);
        }"""
    )
    drawer = CartDrawer(page, _shirees_config(), "desktop")

    assert drawer.settle_quantity_promotion(timeout_ms=3_000) is True
    assert page.locator("[data-promotion-gift-dialog][open]").count() == 0
    assert page.locator("#drawer.open").count() == 1


def test_increase_quantity_handles_promotion_gift_after_change(page):
    _set_promotion_page(page)

    def fulfill_change(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body='{"items":[{"key":"1:1","variant_id":1,"quantity":2}],"item_count":2}',
        )

    page.route("**/cart/change*", fulfill_change)
    drawer = CartDrawer(page, _shirees_config(), "desktop")

    drawer.increase_quantity(0)

    assert page.locator("[data-promotion-gift-dialog][open]").count() == 0
    assert page.locator("#drawer.open").count() == 1
    assert page.locator(".item .qty").input_value() == "2"


def test_promotion_settle_is_a_noop_without_configured_selector(page):
    config = deepcopy(load_site_config("mondressy"))
    config["pages"]["cart"]["selectors"].pop("promotion_gift_dismiss", None)
    page.set_content(
        """
        <aside id="CartDrawer" class="drawer--is-open">
          <div class="cart__item"><input name="updates[]" value="1"></div>
        </aside>
        <dialog open><button>Close</button></dialog>
        """
    )
    drawer = CartDrawer(page, config, "desktop")

    assert drawer.settle_quantity_promotion(timeout_ms=100) is False
    assert page.locator("dialog[open]").count() == 1
