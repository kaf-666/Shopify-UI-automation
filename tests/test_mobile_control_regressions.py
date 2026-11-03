"""Real-browser regressions for clipped swatches and asynchronous accordions."""
from copy import deepcopy

import pytest
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from pages.navigation import NavigationPage
from pages.product_page import ProductPage
from tests.test_navigation import _synthetic_config
from tests.test_hidden_radio_variant_support import _product_config
from utils.browser import load_site_config


@pytest.fixture(scope="module", params=["chromium", "webkit"])
def browser(request):
    with sync_playwright() as playwright:
        instance = getattr(playwright, request.param).launch(headless=True)
        yield instance
        instance.close()


@pytest.fixture
def page(browser):
    current = browser.new_page(viewport={"width": 390, "height": 664})
    yield current
    current.close()


def test_mondressy_clipped_radio_uses_real_label_even_without_wrapper_box(page):
    page.set_content('''
      <style>
        .variant-input { display: inline-block; width: 0; height: 0; margin: 0 30px; overflow: visible; }
        input { position: absolute; width: 1px; height: 1px; clip: rect(0,0,0,0); }
        label { display: inline-block; width: 40px; height: 40px; background: pink; }
      </style>
      <form id=purchase><div id=colors>
        <div class=variant-input><input id=green name=Color type=radio value=Green checked><label for=green>G</label></div>
        <div class=variant-input><input id=pink name=Color type=radio value=Pink><label for=pink>P</label></div>
      </div><button id=atc type=button>Add</button></form>
    ''')
    config = _product_config(color_control=deepcopy(
        load_site_config('mondressy')['pages']['product'].get('color_option_control', {})))
    product = ProductPage(page, config, 'mobile')
    assert page.locator('#pink').is_visible()  # The old visibility test accepted this clipped box.
    assert not page.locator('.variant-input').first.is_visible()
    assert product.select_color('Pink') == 'Pink'
    assert page.locator('#pink').is_checked()


def test_hidden_associated_label_does_not_fall_back_to_clipped_radio(page):
    page.set_content('''<style>input {position:absolute;width:1px;height:1px;clip:rect(0,0,0,0)}</style>
      <form id=purchase><div id=colors><input id=pink name=Color type=radio value=Pink>
      <label for=pink hidden>Pink</label></div><button id=atc>Add</button></form>''')
    product = ProductPage(page, _product_config(color_control={'strategy': 'associated_label'}), 'mobile')
    assert page.locator('#pink').is_visible()
    assert product.available_color_count() == 0
    assert not product.has_actionable_color()
    with pytest.raises(LookupError, match='Color option not found'):
        product.select_color('Pink')
    assert not page.locator('#pink').is_checked()


def accordion(page, monkeypatch, owner, behavior, *, expanded=False):
    # Use short observation slices, not fixed sleeps: the real theme schedules
    # visibility independently of the test, often beyond the first slice.
    monkeypatch.setattr('pages.navigation.MOBILE_MENU_TARGET_WAIT_MS', 100)
    monkeypatch.setattr('pages.navigation.MOBILE_MENU_CLICK_TIMEOUT_MS', 200)
    monkeypatch.setattr('pages.navigation.MOBILE_MENU_RETRY_INTERVAL_MS', 5)
    attribute = 'aria-expanded="true"' if expanded else 'aria-expanded="false"'
    parent_attr = attribute if owner == 'ancestor' else ''
    toggle_attr = attribute if owner in {'self', 'descendant'} else ''
    page.set_content(f'''
      <style>[hidden] {{display:none !important}}</style>
      <header id=site-header><button id=drawer-trigger>Menu</button>
        <aside id=drawer hidden><button class=close>Close</button>
          <ul class=menu-root><li class=mobile-item>
            <a class=parent-toggle href=/collections/parent {parent_attr}>
              Parent <span id=toggle role=button {toggle_attr}>Expand</span>
            </a><div class=mobile-panel>
              <a class=target-link href=/collections/target hidden>Target</a>
            </div>
          </li></ul>
        </aside>
      </header>
      <script>
        window.clicks=0;
        const drawer=document.querySelector('#drawer');
        const target=document.querySelector('.target-link');
        const state=()=>document.querySelector('[aria-expanded]');
        document.querySelector('#drawer-trigger').onclick=()=>{{drawer.hidden=false;drawer.classList.add('open')}};
        let ready=false;
        document.addEventListener('click', event=>{{
          if(!event.target.closest('.parent-toggle'))return;
          event.preventDefault();window.clicks++;
          {behavior}
        }});
      </script>
    ''')
    config = _synthetic_config()
    config['pages']['navigation']['selectors']['mobile_target_parent']['value'] = '.parent-toggle' if owner == 'descendant' else '#toggle'
    return NavigationPage(page, config, 'mobile')


@pytest.mark.parametrize('owner', ['ancestor', 'self', 'descendant'])
def test_delayed_handler_can_retry_only_while_parent_is_still_closed(page, monkeypatch, owner):
    nav = accordion(page, monkeypatch, owner, '''
        if(window.clicks===1){setTimeout(()=>ready=true,20);return;}
        if(ready){state().setAttribute('aria-expanded','true');target.hidden=false;}
    ''')
    nav.open_menu()
    assert page.evaluate('window.clicks') == 2
    assert nav._target_visible()
    assert nav._mobile_open_attempts[0]['wait_error'] == 'AssertionError'
    assert nav._mobile_open_attempts[1]['parent_expanded'] == 'CLOSED'


@pytest.mark.parametrize('owner', ['ancestor', 'self', 'descendant'])
def test_open_accordion_with_delayed_child_is_not_toggled_closed(page, monkeypatch, owner):
    nav = accordion(page, monkeypatch, owner, '''
        const open=state().getAttribute('aria-expanded')!=='true';
        state().setAttribute('aria-expanded',String(open));
        target.hidden=true;
    ''')
    original_wait = nav._wait_target_visible
    observations = 0
    def reveal_after_first_observation(timeout_ms):
        nonlocal observations
        observations += 1
        if observations == 1:
            assert not nav._target_visible()
            raise PlaywrightTimeoutError('child still rendering')
        # Deterministically finish rendering after the first observation
        # window, without depending on browser timer scheduling under CI load.
        page.evaluate('''() => {
            if(document.querySelector('[aria-expanded]').getAttribute('aria-expanded')==='true')
                document.querySelector('.target-link').hidden=false;
        }''')
        return original_wait(timeout_ms)
    monkeypatch.setattr(nav, '_wait_target_visible', reveal_after_first_observation)
    nav.open_menu()
    assert page.evaluate('window.clicks') == 1
    assert nav._target_visible()
    assert any(row['action'] == 'WAIT_OPEN' for row in nav._mobile_open_attempts)


@pytest.mark.parametrize('owner', ['ancestor', 'self', 'descendant'])
def test_replaced_closed_parent_is_resolved_again_before_retry(page, monkeypatch, owner):
    nav = accordion(page, monkeypatch, owner, '''
        if(window.clicks===1){
          const old=document.querySelector('.parent-toggle');
          setTimeout(()=>old.replaceWith(old.cloneNode(true)),20);return;
        }
        state().setAttribute('aria-expanded','true');target.hidden=false;
    ''')
    nav.open_menu()
    assert page.evaluate('window.clicks') == 2
    assert nav._target_visible()


@pytest.mark.parametrize('owner', ['ancestor', 'self', 'descendant'])
def test_open_parent_with_missing_child_remains_a_failure_without_extra_clicks(page, monkeypatch, owner):
    nav = accordion(page, monkeypatch, owner, 'target.hidden=true;', expanded=True)
    with pytest.raises(RuntimeError, match='target collection not found') as failure:
        nav.open_menu()
    assert page.evaluate('window.clicks') == 0
    assert len(nav._mobile_open_attempts) == 4
    assert all(row['action'] == 'WAIT_OPEN' for row in nav._mobile_open_attempts)
    assert 'attempt_log=' in str(failure.value)
    assert 'AssertionError' in str(failure.value)


def test_unreadable_parent_state_does_not_authorize_repeated_toggle(page, monkeypatch):
    nav = accordion(page, monkeypatch, 'unknown', 'target.hidden=true;')
    with pytest.raises(RuntimeError, match='target collection not found'):
        nav.open_menu()
    assert page.evaluate('window.clicks') == 1
    assert nav._mobile_open_attempts[-1]['action'] == 'WAIT_UNKNOWN'


@pytest.mark.parametrize('owner', ['ancestor', 'self', 'descendant'])
def test_drawer_can_mount_before_its_parent_toggle(page, monkeypatch, owner):
    nav = accordion(page, monkeypatch, owner, '''
        state().setAttribute('aria-expanded','true');target.hidden=false;
    ''')
    page.evaluate('''() => {
        const toggle=document.querySelector('#toggle');toggle.hidden=true;
        document.querySelector('#drawer-trigger').onclick=()=>{
            drawer.hidden=false;drawer.classList.add('open');
            setTimeout(()=>toggle.hidden=false,150);
        };
    }''')
    nav.open_menu()
    assert page.evaluate('window.clicks') == 1
    assert nav._target_visible()


def test_attempt_diagnostics_record_exception_types_without_payloads(page, monkeypatch):
    nav = accordion(page, monkeypatch, 'self', '', expanded=True)
    secret='private-cookie-header-payload'
    def fail_wait(timeout_ms):
        raise TypeError(secret)
    monkeypatch.setattr(nav, '_wait_target_visible', fail_wait)
    with pytest.raises(RuntimeError) as failure:
        nav.open_menu()
    assert 'TypeError' in str(failure.value)
    assert secret not in str(failure.value)
    assert page.evaluate('window.clicks') == 0
