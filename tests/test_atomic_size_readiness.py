"""Real DOM size semantics under metadata RPC timeouts, on both browser engines."""
from copy import deepcopy
import time

import pytest
from playwright.sync_api import Locator, TimeoutError as PlaywrightTimeoutError, sync_playwright

from pages.base_page import BasePage
from pages.size_option_resolver import SizeOptionResolver
from pages.product_page import ProductPage


@pytest.fixture(scope='module', params=['chromium', 'webkit'])
def page(request):
    with sync_playwright() as p:
        browser = getattr(p, request.param).launch(headless=True)
        page = browser.new_page(viewport={'width':390,'height':664})
        yield page
        browser.close()


def resolver(page, *, model='SIZE_MODEL_01', markup=None):
    config = deepcopy(BasePage.load_site_config(site_name='mondressy')['pages']['product'])
    config['size_resolver']['models'] = [m for m in config['size_resolver']['models'] if m['id'] == model]
    page.set_content(markup or '''<form id="purchase"></form><div class="my-infiniteoptions-container sizeoption" role="group" aria-label="Size">
        <label><input hidden type="radio" name="properties[Size]" form="purchase" value="2">2</label>
        <label><input hidden type="radio" name="properties[Size]" form="purchase" value="4" checked>4</label></div>''')
    return SizeOptionResolver(page, config, lambda:page.locator('#purchase'))


def test_ready_size_survives_short_metadata_rpc_timeout(page, monkeypatch):
    size = resolver(page)
    # The controls exist and business selection can operate them. Simulate the
    # short get_attribute RPC timeouts seen under the Jenkins renderer load.
    assert len(size.normal_available_options()) == 2
    original = Locator.get_attribute
    def slow_metadata(self, name, *, timeout=None):
        if timeout is not None and timeout <= 50:
            raise PlaywrightTimeoutError('synthetic metadata transport timeout')
        return original(self, name, timeout=timeout)
    monkeypatch.setattr(Locator, 'get_attribute', slow_metadata)
    state = size.readiness_snapshot(deadline=time.monotonic()+1)
    assert state['size_group_detected'] and state['normal_size_available'] == 1
    assert size.selected_value(deadline=time.monotonic()+1) == '4'


@pytest.mark.parametrize('strategy,markup', [
    ('native', '<input type=radio value=2>'),
    ('native', '<input hidden type=radio value=2>'),
    ('label', '<label for="s:a[]">2</label><input id="s:a[]" hidden type=radio value=2>'),
    ('label', '<label>2<input hidden type=radio value=2></label>'),
    ('label', '<label style="display:contents">2<input hidden type=radio value=2></label>'),
    ('label', '<label style="display:none">2<input hidden type=radio value=2></label>'),
    ('label', '<label>2<input disabled hidden type=radio value=2></label>'),
    ('label', '<label aria-disabled=true>2<input hidden type=radio value=2></label>'),
    ('label', '<div class=disabled><label>2<input hidden type=radio value=2></label></div>'),
    ('native', '<input class=disabled type=radio value=2>'),
    ('native', '<div class=disabled><input type=radio value=2></div>'),
    ('native', '<div class=disabled-extra><input type=radio value=2></div>'),
    ('native', '<fieldset disabled><legend><input type=radio value=2></legend></fieldset>'),
    ('native', '<fieldset disabled><input type=radio value=2></fieldset>'),
    ('native', '<div aria-disabled=true><input type=radio value=2></div>'),
    ('native', '<div aria-disabled=true><input aria-disabled=false type=radio value=2></div>'),
    ('native', '<div aria-disabled=true><input role=heading type=radio value=2></div>'),
    ('native', '<input aria-disabled=true type=radio value=2>'),
    ('label', '<details><label>2<input hidden type=radio value=2></label></details>'),
    ('label', '<details open><label>2<input hidden type=radio value=2></label></details>'),
    ('label', '<div style="content-visibility:hidden"><label>2<input hidden type=radio value=2></label></div>'),
    ('label', '<label style="display:contents;visibility:hidden"><span style="visibility:visible">2</span><input hidden type=radio value=2></label>'),
    ('label', '<label><input hidden checked type=radio value="Free Custom Size">Custom</label>'),
    ('label', '<label><input hidden checked type=radio value="Free Custom Size">Custom</label><label><input hidden type=radio value=2>2</label>'),
])
def test_atomic_availability_matches_business_semantics(page, strategy, markup):
    config = {'size_resolver':{'models':[{
        'id':'TEST', 'group_selector':'#sizes', 'option_selector':'input[type=radio]',
        'control_strategy':strategy, 'disabled_class_tokens':['disabled']} ]}}
    page.set_content('<form id=purchase><div id=sizes>'+markup+'</div></form>')
    size = SizeOptionResolver(page, config, lambda:page.locator('#purchase'))
    available = size.available_options()
    normal = [o for o in available if not o.custom_size]
    expected_selected = size.selected_value()
    state = size.readiness_snapshot(deadline=time.monotonic()+1)
    assert state['size_probe_status'] == 'OK'
    assert bool(state['size_option_available']) == bool(available)
    assert bool(state['normal_size_available']) == bool(normal)
    assert state['selected_size'] == expected_selected
    assert size.selected_value(deadline=time.monotonic()+1) == expected_selected


@pytest.mark.parametrize('site', ['mondressy','lavetir','shirees'])
def test_live_profiles_keep_form_association_and_reject_other_products(page, site):
    config = BasePage.load_site_config(site_name=site)['pages']['product']
    classes = 'my-infiniteoptions-container sizeoption shopify-size-selector'
    group = f'<div class="{classes}" role=group aria-label=Size data-size-type=general_size>'
    page.set_content('<form id=purchase></form><form id=other></form>'+group+
        '<label><input hidden type=radio name="properties[Size]" form=other value=4>4</label></div>')
    size=SizeOptionResolver(page,config,lambda:page.locator('#purchase'))
    assert size.readiness_snapshot(deadline=time.monotonic()+1)['size_group_detected'] is False
    page.locator('input').evaluate("el=>el.setAttribute('form','purchase')")
    state=size.readiness_snapshot(deadline=time.monotonic()+1)
    assert state['size_group_detected'] and state['normal_size_available'] == 1
    assert state['size_model'] == size.detect().model


def test_current_dom_model_and_checked_state_are_resolved_again_after_replacement(page):
    config=BasePage.load_site_config(site_name='mondressy')['pages']['product']
    page.set_content('<form id=purchase><div id=sizes class="my-infiniteoptions-container sizeoption" role=group aria-label=Size><label><input hidden type=radio name="properties[Size]" checked value=2>2</label></div></form>')
    size=SizeOptionResolver(page,config,lambda:page.locator('#purchase'))
    assert size.readiness_snapshot(deadline=time.monotonic()+1)['size_model'] == 'SIZE_MODEL_01'
    page.locator('#sizes').evaluate('''el=>el.outerHTML='<fieldset name="Size" data-handle="size"><legend>Size</legend><label><input hidden type=radio name=Size data-variant-input checked value=6>6</label></fieldset>' ''')
    state=size.readiness_snapshot(deadline=time.monotonic()+1)
    assert state['size_model'] == 'SIZE_MODEL_02' and state['selected_size'] == '6'
    assert size.selected_value(deadline=time.monotonic()+1) == '6'


def test_dom_probe_error_is_distinct_from_absent_group_and_fails_closed(page, monkeypatch):
    size=resolver(page)
    def failed(): raise PlaywrightTimeoutError('private renderer error')
    monkeypatch.setattr(size,'_readiness_dom',failed)
    state=size.readiness_snapshot(deadline=time.monotonic()+1)
    assert state['size_probe_status'] == 'DOM_READ_ERROR'
    product=ProductPage(page,BasePage.load_site_config(site_name='mondressy'),'mobile')
    product.purchase_area=lambda:page.locator('#purchase')
    product._size_resolver=lambda:size
    snapshot=product._readiness_snapshot()
    assert snapshot['size_probe_status'] == 'DOM_READ_ERROR'
    assert 'size_probe_error' in product._missing_readiness_conditions(snapshot,'pre_selection')
    timeline=[]
    product._emit_readiness_diagnostic(snapshot,time.monotonic(),timeline.append)
    assert timeline[0]['size_probe_status'] == 'DOM_READ_ERROR'
    assert 'private' not in str(timeline)


def test_zero_or_overrun_deadline_never_returns_late_pass(page, monkeypatch):
    size=resolver(page)
    original=size._readiness_dom
    monkeypatch.setattr(size,'_readiness_dom',lambda:pytest.fail('no RPC after deadline'))
    assert size.readiness_snapshot(deadline=time.monotonic()-1)['size_probe_status'] == 'DEADLINE_EXHAUSTED'
    def late():
        state=original();time.sleep(.08);return state
    monkeypatch.setattr(size,'_readiness_dom',late)
    assert size.readiness_snapshot(deadline=time.monotonic()+.02)['size_option_available'] == 0
    with pytest.raises(PlaywrightTimeoutError):size.wait_for_available(20)
