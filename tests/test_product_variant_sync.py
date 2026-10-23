"""Reproduce a checked swatch preceding the submitted Shopify variant."""
import json

import pytest
from playwright.sync_api import sync_playwright

from flows.shopping_flow import ShoppingFlow, FlowError
from pages.product_page import ProductPage, PurchaseAreaReadinessError
from tests.test_hidden_radio_variant_support import _size_product_config


@pytest.fixture(scope="module", params=["chromium", "webkit"])
def browser(request):
    with sync_playwright() as playwright:
        instance = getattr(playwright, request.param).launch(headless=True)
        yield instance
        instance.close()


@pytest.fixture
def product(browser):
    page = browser.new_page(viewport={"width": 390, "height": 664})
    variants = [{"id": 11, "option1": "Dark Green", "available": True},
                {"id": 22, "option1": "Pink", "available": True}]
    page.set_content('''<section data-product-id="1">
      <form id="purchase"><h1 id="title">Dress</h1><span id="price">$155.99</span>
      <div id="colors"><label for="green">Green</label>
        <input hidden id="green" type="radio" name="Color" data-index="option1" value="Dark Green" checked>
        <label for="pink">Pink</label>
        <input hidden id="pink" type="radio" name="Color" data-index="option1" value="Pink"></div>
      <span data-variant-color-label>Dark Green</span>
      <select hidden name="id" data-product-select><option value="11">Green</option><option value="22">Pink</option></select>
      <div id="sizes"><label for="m">M</label><input hidden checked id="m" type="radio" name="properties[Size]" value="M"></div>
      <button type="button" id="atc">Add</button></form>
      <textarea hidden data-variant-json>VARIANTS</textarea></section>
      <script>window.added=[]; atc.onclick=()=>window.added.push(document.querySelector('[name=id]').value);</script>
    '''.replace('VARIANTS', json.dumps(variants)))
    config = _size_product_config()
    config["pages"]["product"]["variant_sync"] = {
        "root_selector": "[data-product-id]",
        "variants_selector": "[data-variant-json]",
        "select_selector": "[data-product-select]",
        "color_label_selector": "[data-variant-color-label]",
    }
    config["pages"]["product"]["selectors"]["price"] = {"by": "css", "value": "#price"}
    yield ProductPage(page, config, "mobile")
    page.close()


def test_waits_for_delayed_master_variant_before_cart_click(product):
    # Native checked changes immediately; the theme finishes asynchronously.
    product.page.evaluate('''pink.onchange=()=>setTimeout(()=>{
      document.querySelector('[name=id]').value='22';
      document.querySelector('[data-variant-color-label]').textContent='Pink';
    },700)''')
    assert product.select_color("Pink") == "Pink"
    timeline = []
    product.wait_purchase_ready(timeout_ms=2500, phase="post_selection",
                                expected_color="Pink", expected_size="M",
                                diagnostics_hook=timeline.append)
    product.add_to_cart()
    assert product.page.evaluate("window.added") == ["22"]
    assert any(s.get("variant_sync_status") == "MISMATCH" for s in timeline)
    assert timeline[-1]["variant_sync_status"] == "MATCH"


def test_stale_master_variant_never_reaches_add_to_cart(product):
    product.select_color("Pink")
    flow = ShoppingFlow(product.page, product.site_config, "mobile")
    flow.state.update(prod=product, color="Pink", size="M")
    wait = product.wait_purchase_ready
    product.wait_purchase_ready = lambda **kw: wait(timeout_ms=450, **kw)
    with pytest.raises(FlowError, match="variant_sync"):
        flow.add_to_cart()
    assert product.page.evaluate("window.added") == []


@pytest.mark.parametrize("change", [
    "document.querySelector('[data-variant-json]').remove()",
    "document.querySelector('[data-variant-json]').value='invalid'",
    "document.querySelector('[name=id]').value=''",
    "document.querySelector('[data-variant-color-label]').textContent='Dark Green'",
])
def test_incomplete_or_inconsistent_theme_state_fails_closed(product, change):
    product.select_color("Pink")
    product.page.evaluate('''() => {
      document.querySelector('[name=id]').value='22';
      document.querySelector('[data-variant-color-label]').textContent='Pink';
    }''')
    product.page.evaluate(change)
    with pytest.raises(PurchaseAreaReadinessError, match="variant_sync"):
        product.wait_purchase_ready(timeout_ms=400, phase="post_selection",
                                    expected_color="Pink", expected_size="M")
    assert product.page.evaluate("window.added") == []


def enable_theme_gate(product):
    product.site_config["pages"]["product"]["variant_sync"]["initialization"] = "archetype_product"


def test_theme_binds_handlers_before_first_color_click(product):
    enable_theme_gate(product)
    product.page.evaluate('''() => {
      window.changes=[];
      setTimeout(()=>{
        pink.onchange=()=>{
          window.changes.push(pink.value);
          document.querySelector('[name=id]').value='22';
          document.querySelector('[data-variant-color-label]').textContent='Pink';
        };
        const form=document.querySelector('#purchase'), root=form.closest('[data-product-id]');
        window.theme={sections:{instances:[{type:'product', container:root,
          cache:{form}, variants:{container:root}}]}};
      },700);
    }''')
    observations=[]
    product.wait_purchase_ready(timeout_ms=2500, phase="pre_selection",
                                diagnostics_hook=observations.append)
    assert any(s["theme_ready_status"] == "INITIALIZING" for s in observations)
    assert observations[-1]["theme_ready_status"] == "READY"
    product.select_color("Pink")
    product.wait_purchase_ready(timeout_ms=1500, phase="post_selection",
                                expected_color="Pink", expected_size="M")
    assert product.page.evaluate("window.changes") == ["Pink"]
    assert product._variant_sync_snapshot("Pink")["submitted_variant_id"] == "22"
    assert product.page.evaluate("window.added") == []


def test_direct_selection_also_waits_for_theme(product):
    enable_theme_gate(product)
    product.page.evaluate('''() => {
      window.earlyClicks=0;
      pink.addEventListener('click',()=>{if(!window.theme) window.earlyClicks++});
      setTimeout(()=>{
        const form=document.querySelector('#purchase'), root=form.closest('[data-product-id]');
        window.theme={sections:{instances:[{type:'product',container:root,
          cache:{form},variants:{container:root}}]}};
      },400);
    }''')
    product.select_color("Pink")
    assert product.get_selected_color() == "Pink"
    assert product.page.evaluate("window.earlyClicks") == 0


@pytest.mark.parametrize("state", ["missing", "stale_form", "other_product"])
def test_uninitialized_or_stale_theme_never_becomes_ready(product, state):
    enable_theme_gate(product)
    if state != "missing":
        product.page.evaluate('''state => {
          const form=document.querySelector('#purchase'), root=form.closest('[data-product-id]');
          window.theme={sections:{instances:[{type:'product',container:root,
            cache:{form:state==='stale_form'?form.cloneNode(true):form},
            variants:{container:state==='other_product'?document.body:root}}]}};
        }''', state)
    with pytest.raises(PurchaseAreaReadinessError, match="theme_ready"):
        product.wait_purchase_ready(timeout_ms=350, phase="pre_selection")
    assert product.get_selected_color() == "Dark Green"
    assert product.page.evaluate("window.added") == []
