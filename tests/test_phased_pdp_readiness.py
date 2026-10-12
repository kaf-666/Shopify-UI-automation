"""Real offline DOM on Chromium/Desktop and WebKit/Mobile; no storefront traffic."""
import pytest
from playwright.sync_api import sync_playwright
from pages.product_page import ProductPage, PurchaseAreaReadinessError
from flows.shopping_flow import ShoppingFlow, FlowError
from tests.test_hidden_radio_variant_support import _size_product_config
from utils.readonly_mutation_guard import ReadonlyMutationGuard
from tests.website_smoke_readonly_v1_cases import WebsiteSmokeReadonlyV1Runner


@pytest.fixture(scope="module", params=["chromium", "webkit"])
def browser(request):
    with sync_playwright() as p:
        b = getattr(p, request.param).launch(headless=True)
        yield b
        b.close()


@pytest.fixture
def product(browser):
    page = browser.new_page(viewport={"width": 390, "height": 844})
    page.set_content('''<form id="purchase"><h1 id="title">Synthetic</h1>
      <div id="colors"><label for="blue">Blue</label><input hidden checked id="blue" name="Color" type="radio" value="Blue"></div>
      <div id="sizes"><label for="m">M</label><input hidden id="m" name="properties[Size]" type="radio" value="M"></div>
      <button id="atc" type="button" disabled>Add</button></form>
      <script>window.clicks=0; atc.onclick=()=>window.clicks++;
      m.onchange=()=>{atc.disabled=!m.checked;};</script>''')
    config = _size_product_config()
    config["pages"]["product"]["selectors"]["price"] = {"by": "css", "value": "#price"}
    page.evaluate("purchase.insertAdjacentHTML('beforeend','<span id=price>$10</span>')")
    prod = ProductPage(page, config, "mobile")
    yield prod
    page.close()


def choose(prod):
    color, size = prod.select_color(), prod.select_size()
    assert (color, size) == ("Blue", "M")
    return color, size


def test_disabled_before_selection_enabled_after_checked(product):
    guard = ReadonlyMutationGuard()
    guard.attach(product.page.context)
    observations = []
    assert product.wait_purchase_ready(phase="pre_selection", diagnostics_hook=observations.append)[2]
    assert product.add_to_cart_button().is_enabled() is False
    color, size = choose(product)
    product.wait_purchase_ready(phase="post_selection", expected_color=color, expected_size=size)
    assert product.get_selected_size() == "M" and product.get_selected_color() == "Blue"
    assert product.page.evaluate("window.clicks") == 0
    assert guard.violations() == []
    assert observations[0]["size_model"] != "UNKNOWN"
    assert observations[0]["readiness_phase"] == "pre_selection"


def test_post_selection_stays_disabled_full_never_clicks(product):
    color, size = choose(product)
    product.page.evaluate("atc.disabled=true")
    with pytest.raises(PurchaseAreaReadinessError, match="atc_enabled"):
        product.wait_purchase_ready(timeout_ms=350, phase="post_selection", expected_color=color, expected_size=size)
    flow = ShoppingFlow(product.page, product.site_config, "mobile")
    flow.state.update(prod=product, color=color, size=size)
    with pytest.raises(FlowError, match="POST_SELECTION_NOT_READY"):
        # Small real deadline for this offline failure; production keeps 15 s.
        original = product.wait_purchase_ready
        product.wait_purchase_ready = lambda **kw: original(timeout_ms=350, **kw)
        flow.add_to_cart()
    assert product.page.evaluate("window.clicks") == 0


def test_delayed_size_model_and_group_recovers_inside_original_budget(product):
    product.page.evaluate('''() => { const group=sizes; group.remove();
        setTimeout(()=>purchase.insertBefore(group,atc),200); }''')
    timeline = []
    product.wait_purchase_ready(timeout_ms=1500, phase="pre_selection", diagnostics_hook=timeline.append)
    assert any(s["size_model"] == "UNKNOWN" for s in timeline)
    assert timeline[-1]["size_group_detected"] is True
    color, size = choose(product)
    product.wait_purchase_ready(timeout_ms=1500, phase="post_selection", expected_color=color, expected_size=size)


def test_group_disappears_during_stability_window_and_recovers(product):
    color, size = choose(product)
    product.page.evaluate('''() => { const group=sizes;
        setTimeout(()=>{group.remove(); setTimeout(()=>purchase.insertBefore(group,atc),120);},50); }''')
    product.wait_purchase_ready(timeout_ms=2000, phase="post_selection", expected_color=color, expected_size=size)
    assert product.get_selected_size() == "M"


def test_checked_state_reset_is_not_a_purchase_pass(product):
    color, size = choose(product)
    product.page.evaluate("setTimeout(()=>{m.checked=false;},50)")
    with pytest.raises(PurchaseAreaReadinessError, match="selected_size"):
        product.wait_purchase_ready(timeout_ms=450, phase="post_selection", expected_color=color, expected_size=size)
    assert product.page.evaluate("window.clicks") == 0


def test_no_available_size_remains_failure(product):
    product.page.evaluate("m.disabled=true")
    with pytest.raises(PurchaseAreaReadinessError, match="failing_conditions=[^ ]*size"):
        product.wait_purchase_ready(timeout_ms=350, phase="pre_selection")


def test_legacy_contract_does_not_silently_relax_disabled_atc(product):
    with pytest.raises(PurchaseAreaReadinessError, match="atc_enabled"):
        product.wait_purchase_ready(timeout_ms=350)


def test_production_readonly_search_case_selects_then_checks_without_mutation(product):
    guard = ReadonlyMutationGuard()
    guard.attach(product.page.context)
    runner = object.__new__(WebsiteSmokeReadonlyV1Runner)
    runner.state = {"search_prod": product}
    detail = runner._c_search04()
    assert "size=M" in detail and "atc_enabled=True" in detail
    assert product.page.evaluate("window.clicks") == 0
    assert guard.violations() == []


def test_custom_trigger_before_normal_size_does_not_hide_saleable_size(product):
    product.page.evaluate('''sizes.insertAdjacentHTML('afterbegin',
        '<label for="custom">Custom Size</label><input hidden id="custom" type="radio" name="properties[Size]" value="Custom Size">')''')
    product.wait_purchase_ready(timeout_ms=1000, phase="pre_selection")
    assert product.first_available_size() == "M"
