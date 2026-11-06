"""Exercise actual PLP link activation on both supported browser engines."""
import pytest
from playwright.sync_api import sync_playwright

from pages.collection_page import CollectionPage


@pytest.fixture(params=["chromium", "webkit"])
def browser(request):
    with sync_playwright() as pw:
        browser = getattr(pw, request.param).launch()
        yield browser
        browser.close()


@pytest.mark.parametrize("new_page", [False, True])
def test_product_card_click_adopts_the_actual_page(browser, monkeypatch, new_page):
    context = browser.new_context()
    requests = []

    def serve(route):
        requests.append(route.request.url)
        route.fulfill(status=200, content_type="text/html", body="<h1>Product</h1>")

    context.route("**/*", serve)
    page = context.new_page()
    page.goto("https://shop.example/collections/dresses")
    target = ' target="_blank"' if new_page else ""
    page.set_content(f'<div class="card"><a class="product" href="/products/foo"{target}>Open</a></div>')
    config = {"base_url": "https://shop.example", "pages": {"collection": {"selectors": {
        "product_card": {"by": "css", "value": ".card"},
        "product_link": {"by": "css", "value": ".product"}}}}}
    monkeypatch.setattr(CollectionPage, "wait_for_products_ready", lambda self, index=0: {})
    collection = CollectionPage(page, config, "desktop")

    assert collection.open_product() == "https://shop.example/products/foo"
    assert (collection.page is not page) is new_page
    assert "https://shop.example/products/foo" in requests
    context.close()


def test_prevented_click_cannot_pass_using_direct_navigation(browser, monkeypatch):
    context = browser.new_context()
    context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body="<h1>Page</h1>"))
    page = context.new_page()
    page.goto("https://shop.example/collections/dresses")
    page.set_content('<div class="card"><a class="product" href="/products/foo" onclick="event.preventDefault()">Open</a></div>')
    config = {"base_url": "https://shop.example", "pages": {"collection": {"selectors": {
        "product_card": {"by": "css", "value": ".card"},
        "product_link": {"by": "css", "value": ".product"}}}}}
    monkeypatch.setattr(CollectionPage, "wait_for_products_ready", lambda self, index=0: {})
    monkeypatch.setattr("pages.collection_page.PAGE_NAV_TIMEOUT_MS", 400)
    with pytest.raises(RuntimeError, match="did not navigate"):
        CollectionPage(page, config, "desktop").open_product()
    context.close()
