"""Bounded readiness for the checkout Order Summary subtree."""
from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright

from pages.checkout_page import CheckoutPage

_CONFIG = {"pages": {"checkout": {"selectors": {}}}}


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


def test_waits_for_order_summary_that_renders_after_contact(page):
    page.set_content(
        '<main id="checkout-main"><h2 id="contact">Contact</h2></main>'
    )
    page.evaluate(
        """() => {
          setTimeout(() => {
            document.querySelector('main').insertAdjacentHTML(
              'beforeend',
              '<section><h2>Order summary</h2><div role="cell">Dress</div></section>'
            );
          }, 250);
        }"""
    )
    checkout = CheckoutPage(page, _CONFIG, "mobile")

    checkout.wait_order_summary(timeout_ms=3_000)

    assert checkout.has_order_summary()
    assert page.locator('[role="cell"]').count() == 1


def test_missing_order_summary_stays_a_real_timeout(page):
    page.set_content(
        '<main id="checkout-main"><h2 id="contact">Contact</h2></main>'
    )
    checkout = CheckoutPage(page, _CONFIG, "mobile")

    with pytest.raises(TimeoutError, match="order summary"):
        checkout.wait_order_summary(timeout_ms=100)
