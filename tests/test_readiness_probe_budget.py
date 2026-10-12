"""#174 budget regression and real-DOM parity with business color availability."""
import pytest
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from pages.product_page import ProductPage, PurchaseAreaReadinessError
from tests.test_product_page_readiness_diagnostics import _Clock
from tests.test_hidden_radio_variant_support import _product_config


def budget_product(cls, clock):
    prod = object.__new__(cls)
    class Locator:
        def count(self): return 1
        def is_visible(self, **kw): return True
        def is_enabled(self, **kw): return True
    class Colors:
        def count(self): return 1
        def nth(self, i): return type("Radio", (), {"is_disabled": lambda s, **kw: False})()
        def evaluate_all(self, script, config):
            clock.advance(.05)
            return 0
    class Size:
        def readiness_snapshot(self, deadline=None):
            clock.advance(.1)
            ready = clock.now >= .9
            return {"size_model": "SIZE_MODEL_04" if ready else None,
                    "size_group_detected": ready, "size_option_available": int(ready),
                    "size_option_total": 10 if ready else 0, "normal_size_available": int(ready),
                    "selected_size": None, "candidate_group_count": int(ready)}
        def wait_for_available(self, timeout_ms):
            clock.advance(timeout_ms / 1000)
            if clock.now < .9: raise PlaywrightTimeoutError("size pending")
    prod.purchase_area = prod.title = prod.add_to_cart_button = lambda: Locator()
    prod.color_options = lambda: Colors()
    prod._color_option_control_config = lambda: {}
    def legacy_control(*a, **kw):
        clock.advance(.8)
        return object()
    prod._color_option_control_if_available = legacy_control
    prod._size_resolver = lambda: Size()
    return prod


def test_ready_within_same_deadline_despite_delayed_size_and_slow_protocol(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr("pages.product_page.time.monotonic", clock)
    prod = budget_product(ProductPage, clock)
    timeline = []
    assert prod.wait_purchase_ready(timeout_ms=1500, phase="pre_selection", diagnostics_hook=timeline.append) == (1, 1, True)
    assert clock.now < 1.5
    assert timeline[0]["color_probe_skipped"] and timeline[0]["atc_probe_skipped"]
    assert "color_check_ms" not in timeline[0]["readiness_cost_ms"]
    assert timeline[-1]["size_group_detected"] and timeline[-1]["color_ready"]


def test_missing_size_does_not_probe_color_or_atc(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr("pages.product_page.time.monotonic", clock)
    prod = budget_product(ProductPage, clock)
    prod.has_actionable_color = lambda **kw: pytest.fail("color must wait for size")
    prod.add_to_cart_button = lambda: type("Button", (), {"count": lambda s: pytest.fail("ATC probe must wait for size")})()
    snapshot = prod._readiness_snapshot()
    assert snapshot["size_count"] == 0 and snapshot["color_probe_skipped"]
    assert prod._missing_readiness_conditions(snapshot, "pre_selection") == ["size"]


def test_exhausted_budget_is_not_reported_as_observed_color_unavailability(monkeypatch):
    clock = _Clock(); clock.now = 1
    monkeypatch.setattr("pages.product_page.time.monotonic", clock)
    prod = budget_product(ProductPage, clock)
    prod._readiness_deadline = 1.05
    prod.has_actionable_color = lambda **kw: pytest.fail("no probe after deadline")
    snapshot = prod._readiness_snapshot()
    assert snapshot["probe_budget_exhausted"] and snapshot["color_probe_skipped"]
    assert "probe_budget_exhausted" in prod._missing_readiness_conditions(snapshot)
    assert prod._snapshot_ready(snapshot) is False


@pytest.fixture(scope="module", params=["chromium", "webkit"])
def page(request):
    with sync_playwright() as p:
        browser = getattr(p, request.param).launch(headless=True)
        page = browser.new_page(viewport={"width": 1440 if request.param == "chromium" else 390, "height": 900})
        yield page
        browser.close()


@pytest.mark.parametrize("strategy,markup", [
    ("", '<input type=radio value=Blue>'),
    ("associated_label", '<label for=c>Blue</label><input id=c hidden type=radio value=Blue>'),
    ("label", '<label>Blue<input hidden type=radio value=Blue></label>'),
    ("label", '<input hidden type=radio value=Blue>'),
    ("label", '<label for=c>Blue</label><input id=c hidden disabled type=radio value=Blue>'),
    ("label", '<fieldset disabled><label for=c>Blue</label><input id=c hidden type=radio value=Blue></fieldset>'),
    ("label", '<label for=c aria-disabled=true>Blue</label><input id=c hidden type=radio value=Blue>'),
    ("native", '<input aria-disabled=true type=radio value=Blue>'),
    ("", '<div class=sold-out><input type=radio value=Blue></div>'),
    ("", '<div class=sold-out-extra><input type=radio value=Blue></div>'),
    ("variant_input", '<div class=variant-input><input hidden type=radio value=Blue></div>'),
    ("variant_input", '<div class="variant-input unavailable"><input type=radio value=Blue></div>'),
    ("label", '<label style="display:none" for=c>Blue</label><input id=c type=radio value=Blue>'),
    ("label", '<label style="display:contents" for=c>Blue</label><input id=c hidden type=radio value=Blue>'),
    ("label", '<label for="c:a[]">Blue</label><input id="c:a[]" hidden type=radio value=Blue>'),
    ("", '<div role=group aria-disabled=true><input type=radio value=Blue></div>'),
    ("", '<div aria-disabled=true><input aria-disabled=false type=radio value=Blue></div>'),
    ("label", '<label aria-disabled=true>Blue<input hidden type=radio value=Blue></label>'),
    ("", '<div aria-disabled=true><input role=heading type=radio value=Blue></div>'),
])
def test_atomic_probe_matches_existing_business_availability(page, strategy, markup):
    page.set_content('<div id=colors>'+markup+'</div>')
    product = ProductPage(page, _product_config(color_control={"strategy": strategy}), "mobile")
    expected = bool(product.available_options(product.color_options(), product._color_option_control_config()))
    assert product.has_actionable_color() is expected


def test_atomic_probe_re_resolves_replaced_color_nodes(page):
    page.set_content('<div id=colors><label for=c>Blue</label><input id=c hidden type=radio value=Blue></div>')
    product = ProductPage(page, _product_config(color_control={"strategy":"label"}), "mobile")
    assert product.has_actionable_color()
    page.evaluate("colors.innerHTML='<input hidden disabled type=radio value=Blue>'")
    assert product.has_actionable_color() is False
