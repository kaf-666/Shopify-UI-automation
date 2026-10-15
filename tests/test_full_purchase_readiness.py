"""Full purchase-area confirmation survives transient DOM changes offline."""

import pytest
from playwright.sync_api import sync_playwright

from flows.shopping_flow import FlowError, ShoppingFlow
from pages.product_page import ProductPage
from tests.test_hidden_radio_variant_support import _size_product_config
from tests.test_product_page_readiness_diagnostics import _Clock, _snapshot
from tests.website_smoke_v1_cases import WebsiteSmokeV1Runner


@pytest.fixture(scope="module", params=["chromium", "webkit"])
def browser(request):
    with sync_playwright() as playwright:
        browser = getattr(playwright, request.param).launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture
def runner(browser):
    context = browser.new_context()
    context.route("**/*", lambda route: route.abort())
    page = context.new_page()
    page.set_content('''<form id="purchase"><h1 id="title">Synthetic Product</h1>
      <span id="price">$10</span>
      <div id="colors"><label for="blue">Blue</label>
        <input hidden checked id="blue" name="Color" type="radio" value="Blue"></div>
      <div id="sizes"><label for="m">M</label>
        <input hidden id="m" name="properties[Size]" type="radio" value="M"></div>
      <button id="atc" type="button" disabled>Add</button></form>
      <script>
        window.clicks=0;
        document.addEventListener('click', e => {if(e.target.id==='atc') window.clicks++;});
        document.addEventListener('change', () => {
          document.querySelector('#atc').disabled=!document.querySelector('#m')?.checked;
        });
      </script>''')
    config = _size_product_config()
    config["pages"]["product"]["selectors"]["price"] = {"by": "css", "value": "#price"}
    runner = object.__new__(WebsiteSmokeV1Runner)
    runner.page = page
    runner.site_config = config
    runner.viewport = "desktop"
    runner.state = {}
    runner.flow = ShoppingFlow(page, config, "desktop")
    yield runner
    context.close()


def change_after_first_ready(monkeypatch, runner, *, recover):
    original = ProductPage.wait_purchase_ready
    calls = []

    def wait(product, *args, **kwargs):
        value = original(product, *args, **kwargs)
        calls.append(kwargs.get("phase", "legacy"))
        if len(calls) == 1:
            runner.page.evaluate('''recover => {
              const group=document.querySelector('#sizes');
              group.remove();
              const button=document.querySelector('#atc');
              const replacement=button.cloneNode(true);
              replacement.style.display='none';
              button.replaceWith(replacement);
              if(recover) setTimeout(() => {
                replacement.before(group.cloneNode(true));
                replacement.style.display='';
              }, 350);
            }''', recover)
        return value

    monkeypatch.setattr(ProductPage, "wait_purchase_ready", wait)
    monkeypatch.setattr(ProductPage, "open", lambda _product: None)
    return calls


@pytest.mark.parametrize("entry", ["direct", "browse", "search"])
def test_full_case_recovers_after_first_ready(monkeypatch, runner, entry):
    calls = change_after_first_ready(monkeypatch, runner, recover=True)
    if entry == "direct":
        detail = runner._c_direct01()
        assert "pre_selection_ready=True" in detail
        assert runner.flow.state["prod"].add_to_cart_button().is_enabled() is False
    elif entry == "browse":
        product = ProductPage(runner.page, runner.site_config, "desktop")
        runner.state["browse_prod"] = product
        runner.flow.state["prod"] = product
        detail = runner._c_pdp01()
        assert "color=Blue size=M" in detail
        assert product.get_selected_size() == "M"
    else:
        product = ProductPage(runner.page, runner.site_config, "desktop")
        runner.state["search_prod"] = product
        runner.flow.state["prod"] = product
        monkeypatch.setattr(runner.flow, "add_to_cart", lambda: product.wait_purchase_ready(
            phase="post_selection", expected_color="Blue", expected_size="M"))
        monkeypatch.setattr(runner.flow, "capture_cart_state", lambda: None)
        monkeypatch.setattr(runner.flow, "remove_and_wait_empty", lambda: None)
        monkeypatch.setattr(runner, "_cart_consistency", lambda *_args: "consistent=True")
        assert "color=Blue size=M" in runner._c_search04()
    assert calls.count("pre_selection") >= 2
    assert runner.page.locator("#sizes").is_visible()
    assert runner.page.locator("#atc").is_visible()
    assert runner.page.evaluate("window.clicks") == 0


def test_persistent_loss_after_first_ready_still_fails(monkeypatch, runner):
    change_after_first_ready(monkeypatch, runner, recover=False)
    validate = ShoppingFlow.validate_purchase_area

    def bounded(flow, **kwargs):
        return validate(flow, timeout_ms=750, **kwargs)

    monkeypatch.setattr(ShoppingFlow, "validate_purchase_area", bounded)
    with pytest.raises(FlowError) as captured:
        runner._c_direct01()
    assert captured.value.category == "DIRECT_PDP_PURCHASE_AREA_NOT_AVAILABLE"
    assert "size" in str(captured.value)
    assert runner.page.evaluate("window.clicks") == 0


def test_confirmation_does_not_receive_a_fresh_timeout(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr("pages.product_page.time.monotonic", clock)
    product = object.__new__(ProductPage)
    snapshots = 0

    def snapshot():
        nonlocal snapshots
        snapshots += 1
        clock.advance(.8 if snapshots == 1 else .3)
        return _snapshot()

    class Text:
        def inner_text(self, **_kwargs):
            return "Synthetic"

    product._readiness_snapshot = snapshot
    product.title = product.price = lambda: Text()
    flow = ShoppingFlow(None, {"base_url": "https://synthetic.invalid"}, "desktop")
    flow.state["prod"] = product
    with pytest.raises(FlowError, match="confirmation"):
        flow.validate_purchase_area(phase="pre_selection", timeout_ms=1000)
    assert clock.now == pytest.approx(1.1)
    assert "purchase_area" not in flow.state
