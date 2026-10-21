"""Real synthetic DOM contracts for the observed ready root-Home query."""
import json

import pytest
from playwright.sync_api import sync_playwright

from pages.base_page import BasePage
from pages.home_page import HomePage
from pages.search_page import SearchPage
from utils.search_url_diagnostics import DIAGNOSTIC_ATTRIBUTE


HTML = '''<title>Store</title><header id="SiteHeader">
 <h1 class="site-header__logo">Store</h1>
 <a class="js-search-header" onclick="openSearch()">Search desktop</a>
 <a class="js-search-header medium-up--hide" onclick="openSearch()">Search mobile</a>
 </header><div class="site-header__search-container"><input id="Search"></div>
 <script>window.clicks=0; function openSearch(){window.clicks++;
 document.querySelector('.site-header__search-container').classList.add('is-active');}</script>'''


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch(headless=True)
        yield instance
        instance.close()


@pytest.fixture
def page(browser):
    current = browser.new_page()
    current.route("**/*", lambda route: route.fulfill(body=HTML, content_type="text/html"))
    yield current
    current.close()


def search(page, url, viewport="mobile", policy=True):
    page.goto(url)
    config = BasePage.load_site_config(site_name="lavetir")
    if not policy:
        config["pages"]["search"].pop("home_query_policy")
    return SearchPage(page, config, viewport)


@pytest.mark.parametrize("viewport", ["desktop", "mobile"])
def test_observed_root_query_opens_real_search_without_failure_evidence(page, viewport, monkeypatch):
    current = search(page, "https://www.lavetir.com/?token=private-value", viewport)
    import pages.search_page as module
    monkeypatch.setattr(module, "build_search_home_url_diagnostic",
                        lambda **kwargs: pytest.fail("successful Home must not collect failure evidence"))
    current.ensure_search_session_open(case_id="RSMOKE-SEARCH-01")
    assert current.is_open()
    assert current.input().is_editable()
    assert page.evaluate("window.clicks") == 1


def test_legacy_strict_policy_reproduces_original_error_and_redacts_query(page):
    current = search(page, "https://www.lavetir.com/?token=private-value", policy=False)
    with pytest.raises(RuntimeError, match="^Search session can only be opened from Home$") as caught:
        current.ensure_search_session_open()
    diagnostic = getattr(caught.value, DIAGNOSTIC_ATTRIBUTE)
    assert diagnostic["url_mismatch_type"] == "QUERY_MISMATCH"
    assert "private-value" not in json.dumps(diagnostic)
    assert page.evaluate("window.clicks") == 0


@pytest.mark.parametrize("url", [
    "https://lavetir.com/?token=private-value",
    "https://www.lavetir.com.attacker.test/?token=private-value",
    "https://evil-lavetir.com/?token=private-value",
    "http://www.lavetir.com/?token=private-value",
    "https://www.lavetir.com:444/?token=private-value",
    "https://www.lavetir.com/search?token=private-value",
    "https://www.lavetir.com/account/private-value?token=private-value",
    "https://www.lavetir.com/en-us?token=private-value",
    "https://www.lavetir.com/%2F?token=private-value",
    "https://www.lavetir.com//?token=private-value",
    "https://www.lavetir.com/?token=private-value#private-fragment",
])
def test_query_policy_does_not_admit_another_origin_route_or_fragment(page, url):
    current = search(page, url)
    with pytest.raises(RuntimeError, match="^Search session can only be opened from Home$") as caught:
        current.ensure_search_session_open()
    assert page.evaluate("window.clicks") == 0
    serialized = json.dumps(getattr(caught.value, DIAGNOSTIC_ATTRIBUTE))
    assert "private-value" not in serialized and "private-fragment" not in serialized


@pytest.mark.parametrize("selector", ["h1", "header", "a.js-search-header.medium-up--hide"])
def test_root_query_requires_visible_configured_home_controls(page, selector):
    current = search(page, "https://www.lavetir.com/?token=private-value")
    page.locator(selector).evaluate("element => element.style.display='none'")
    with pytest.raises(RuntimeError, match="^Search session can only be opened from Home$"):
        current.ensure_search_session_open()
    assert page.evaluate("window.clicks") == 0


def test_recovery_uses_same_home_contract_and_preserves_existing_open_session(page, monkeypatch):
    current = search(page, "https://www.lavetir.com/?token=private-value")
    current.ensure_search_session_open()
    monkeypatch.setattr(HomePage, "open", lambda self: pytest.fail("valid Home must not renavigate"))
    current._reopen_session()
    assert page.evaluate("window.clicks") == 1
    assert current.is_open()


@pytest.mark.parametrize("raw", [
    "https://user:private-value@www.lavetir.com/?q=1",
    "https://www.lavetir.com/?q=1#",
    "https://www.lavetir.com/\\?q=1",
    "https://www.lavetir.com/?q=1\n",
])
def test_non_browser_normalized_unsafe_url_is_rejected(page, raw):
    current = search(page, "https://www.lavetir.com/")
    home = HomePage(page, current.site_config, current.viewport)
    assert not current._home_url_matches(raw, home.base_url(), home)


def test_profiles_without_policy_keep_strict_matching(page):
    current = search(page, "https://mondressy.com/?token=private-value")
    current.site_config = BasePage.load_site_config(site_name="mondressy")
    current.site_config["pages"]["search"].pop("home_query_policy", None)
    with pytest.raises(RuntimeError, match="^Search session can only be opened from Home$"):
        current.ensure_search_session_open()


@pytest.mark.parametrize("viewport", ["desktop", "mobile"])
def test_mondressy_root_query_opens_search_with_home_controls(page, viewport):
    page.goto("https://mondressy.com/?opaque=private-value")
    config = BasePage.load_site_config(site_name="mondressy")
    current = SearchPage(page, config, viewport)
    current.ensure_search_session_open()
    assert current.is_open()
    assert current.input().is_editable()
    assert page.evaluate("window.clicks") == 1
    home = HomePage(page, config, viewport)
    assert not current._home_url_matches("https://mondressy.com/search?q=1", home.base_url(), home)
    assert not current._home_url_matches("https://mondressy.com.attacker.test/?q=1", home.base_url(), home)


def test_shirees_allows_only_observed_root_home_query_with_visible_mobile_controls(page):
    page.set_content('''<header class="header__wrapper">
      <div id="logo"><a href="/">Shirees</a></div>
      <button aria-label="Open navigation">Menu</button>
    </header>''')
    config = BasePage.load_site_config(site_name="shirees")
    current = SearchPage(page, config, "mobile")
    home = HomePage(page, config, "mobile")
    configured_url = home.base_url()

    assert current._home_url_matches(
        "https://www.shirees.com/?opaque=value", configured_url, home
    )
    assert not current._home_url_matches(
        "https://www.shirees.com/search?q=opaque", configured_url, home
    )
    assert not current._home_url_matches(
        "https://www.shirees.com.attacker.test/?opaque=value", configured_url, home
    )

    page.locator('button[aria-label="Open navigation"]').evaluate(
        "element => element.style.display = 'none'"
    )
    assert not current._home_url_matches(
        "https://www.shirees.com/?opaque=value", configured_url, home
    )
