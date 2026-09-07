"""Offline regression tests for the shared navigation page object."""

from __future__ import annotations

import pytest

from pages.navigation import NavigationPage
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


SYNTHETIC_HTML = """
<style>
  #drawer { display: none; }
  #drawer.open { display: block; }
  [hidden] { display: none !important; }
</style>
<header id="site-header">
  <button id="drawer-trigger" type="button">Menu</button>
  <nav id="desktop-menu">
    <ul>
      <li class="top-item">
        <a class="parent-toggle" href="/collections/parent">Parent</a>
        <ul class="submenu">
          <li><a class="target-link" href="/collections/target" hidden>Target</a></li>
        </ul>
      </li>
    </ul>
  </nav>
  <aside id="drawer" class="drawer">
    <button class="close" type="button">Close</button>
    <ul class="menu-root">
      <li class="mobile-item">
        <a class="parent-toggle" href="/collections/parent">Parent</a>
        <div class="mobile-panel">
          <a class="target-link" href="/collections/target" hidden>Target</a>
        </div>
      </li>
    </ul>
  </aside>
</header>
<script>
  const desktopParent = document.querySelector("#desktop-menu .parent-toggle");
  const desktopTarget = document.querySelector("#desktop-menu .target-link");
  desktopParent.addEventListener("mouseenter", () => { desktopTarget.hidden = false; });

  const drawer = document.querySelector("#drawer");
  document.querySelector("#drawer-trigger").addEventListener("click", () => {
    drawer.classList.add("open");
  });
  document.querySelector("#drawer .close").addEventListener("click", () => {
    drawer.classList.remove("open");
  });
  const mobileParent = document.querySelector("#drawer .parent-toggle");
  const mobileTarget = document.querySelector("#drawer .target-link");
  mobileParent.addEventListener("click", (event) => {
    event.preventDefault();
    mobileTarget.hidden = false;
  });
</script>
"""


def _synthetic_config() -> dict:
    return {
        "site": "synthetic",
        "base_url": "https://synthetic.test",
        "pages": {
            "navigation": {
                "smoke_collection": {
                    "name": "Target Collection",
                    "path": "/collections/target",
                },
                "selectors": {
                    "header": {"by": "css", "value": "#site-header"},
                    "desktop_menu": {"by": "css", "value": "#desktop-menu > ul"},
                    "desktop_menu_item": {
                        "by": "css",
                        "value": "#desktop-menu > ul > li.top-item",
                    },
                    "desktop_target_parent": {
                        "by": "css",
                        "value": "#desktop-menu > ul > li.top-item > a.parent-toggle",
                    },
                    "mobile_trigger": {"by": "css", "value": "#drawer-trigger"},
                    "mobile_drawer": {"by": "css", "value": "#drawer"},
                    "mobile_drawer_open": {"by": "css", "value": "#drawer.open"},
                    "mobile_menu": {"by": "css", "value": "#drawer > ul.menu-root"},
                    "mobile_menu_item": {
                        "by": "css",
                        "value": "#drawer > ul.menu-root > li.mobile-item",
                    },
                    "mobile_close": {"by": "css", "value": "#drawer .close"},
                    "mobile_target_parent": {
                        "by": "css",
                        "value": "a.parent-toggle[href='/collections/parent']",
                    },
                    "target_collection": {
                        "by": "css",
                        "value": "a.target-link[href='/collections/target']",
                    },
                },
            }
        },
    }


class _FakePage:
    def __init__(self, url: str = "https://mondressy.com/") -> None:
        self.url = url
        self.waits: list[int] = []

    def wait_for_timeout(self, timeout_ms: int) -> None:
        self.waits.append(timeout_ms)

    def wait_for_url(self, predicate, timeout: int) -> None:
        if predicate(self.url):
            return
        raise PlaywrightTimeoutError(f"url did not match within {timeout}ms")


class _FakeLocator:
    def __init__(
        self,
        count: int = 1,
        visible: bool = True,
        child: "_FakeLocator | None" = None,
    ) -> None:
        self._count = count
        self._visible = visible
        self._child = child
        self.click_calls = 0
        self.hover_calls = 0

    @property
    def first(self) -> "_FakeLocator":
        return self

    def count(self) -> int:
        return self._count

    def is_visible(self) -> bool:
        return self._visible

    def filter(self, *, visible: bool = False) -> "_FakeLocator":
        if not visible:
            return self
        if self._visible:
            return self
        return _FakeLocator(
            count=0,
            visible=self._visible,
        )

    def locator(self, _selector: str) -> "_FakeLocator":
        return self._child or _FakeLocator(count=0, visible=False)

    def click(self) -> None:
        self.click_calls += 1

    def hover(self) -> None:
        self.hover_calls += 1


def _mobile_nav(monkeypatch, *, menu_open: bool = False):
    nav = object.__new__(NavigationPage)
    nav.page = _FakePage()
    nav.viewport = "mobile"
    nav.site_config = {}

    trigger = _FakeLocator()
    monkeypatch.setattr(nav, "is_menu_open", lambda: menu_open)
    monkeypatch.setattr(nav, "menu_trigger", lambda: trigger)
    monkeypatch.setattr(nav, "_wait_open_state", lambda: None)
    monkeypatch.setattr(nav, "_wait_mobile_menu_root", lambda: None)
    return nav, trigger


def test_mobile_target_already_visible_passes_without_parent_click(monkeypatch) -> None:
    nav, trigger = _mobile_nav(monkeypatch)
    nav._target_visible = lambda: True

    nav.open_menu()

    assert trigger.click_calls == 1


def test_mobile_target_delayed_after_parent_click_passes(monkeypatch) -> None:
    nav, _trigger = _mobile_nav(monkeypatch)
    parent = _FakeLocator()
    nav._target_visible = lambda: False
    nav._target_top_link = lambda: parent
    nav._wait_target_visible = lambda timeout_ms: None

    nav.open_menu()

    assert parent.click_calls == 1


def test_mobile_none_target_lookup_is_retried(monkeypatch) -> None:
    nav, _trigger = _mobile_nav(monkeypatch)
    parent = _FakeLocator()
    lookups = iter([None, parent])
    nav._target_visible = lambda: False
    nav._target_top_link = lambda: next(lookups)
    nav._wait_target_visible = lambda timeout_ms: None

    nav.open_menu()

    assert parent.click_calls == 1


def test_mobile_parent_locator_can_appear_on_later_attempt(monkeypatch) -> None:
    nav, _trigger = _mobile_nav(monkeypatch)
    empty_parent = _FakeLocator(count=0, visible=False)
    parent = _FakeLocator()
    lookups = iter([empty_parent, parent])
    nav._target_visible = lambda: False
    nav._target_top_link = lambda: next(lookups)
    nav._wait_target_visible = lambda timeout_ms: None

    nav.open_menu()

    assert parent.click_calls == 1


def test_mobile_permanently_missing_target_fails_with_bounded_diagnostics(monkeypatch) -> None:
    nav, _trigger = _mobile_nav(monkeypatch, menu_open=True)
    nav.target_path = lambda: "/collections/wedding-guest-dresses"
    nav._mobile_parent_selector = lambda: "a.menu-target[href*='/collections/new-collection']"
    target = _FakeLocator(count=0, visible=False)
    parent = _FakeLocator(count=0, visible=False)
    root = _FakeLocator(child=parent)
    nav.target_link = lambda: target
    nav.primary_menu = lambda: root
    nav._target_visible = lambda: False
    nav._target_top_link = lambda: None

    with pytest.raises(RuntimeError) as exc_info:
        nav.open_menu()

    message = str(exc_info.value)
    assert "target collection not found in mobile menu" in message
    assert "target_path='/collections/wedding-guest-dresses'" in message
    assert "parent_path='/collections/new-collection'" in message
    assert "drawer_open=True" in message
    assert "menu_root_visible=True" in message
    assert "target_count=0" in message
    assert "parent_count=0" in message
    assert "attempts=4" in message
    assert len(nav.page.waits) == 4


def test_mobile_wrong_destination_still_fails_path_validation(monkeypatch) -> None:
    nav = object.__new__(NavigationPage)
    nav.page = _FakePage("https://mondressy.com/collections/wrong-destination")
    nav.viewport = "mobile"
    nav.site_config = {}
    target = _FakeLocator()
    nav.target_path = lambda: "/collections/wedding-guest-dresses"
    nav.is_menu_open = lambda: True
    nav.open_menu = lambda: None
    nav.target_link = lambda: target

    with pytest.raises(TimeoutError, match="navigation to /collections/wedding-guest-dresses"):
        nav.open_collection()

    assert target.click_calls == 1


def test_desktop_navigation_keeps_hover_flow(monkeypatch) -> None:
    nav = object.__new__(NavigationPage)
    nav.page = _FakePage()
    nav.viewport = "desktop"
    nav.site_config = {}
    link = _FakeLocator()
    nav._target_top_link = lambda: link
    nav._wait_target_visible = lambda timeout_ms: None

    nav.open_menu()

    assert link.hover_calls == 1


def test_mobile_menu_root_waits_for_mount_and_visibility() -> None:
    nav = object.__new__(NavigationPage)
    nav.page = _FakePage()
    nav.viewport = "mobile"
    nav.site_config = {}
    roots = iter([
        _FakeLocator(count=0, visible=False),
        _FakeLocator(count=1, visible=True),
    ])
    nav.primary_menu = lambda: next(roots)
    nav._mobile_failure_detail = lambda prefix, attempts: prefix

    nav._wait_mobile_menu_root(timeout_ms=1_000)

    assert nav.page.waits


def test_synthetic_navigation_supports_distinct_desktop_and_mobile_topologies() -> None:
    """Use neutral HTML to exercise configured hover, accordion, and close paths."""
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=True)
    try:
        config = _synthetic_config()

        desktop_page = browser.new_page()
        desktop_page.set_content(SYNTHETIC_HTML)
        desktop = NavigationPage(desktop_page, config, "desktop")
        desktop.wait_ready()
        assert desktop.current_mode() == "MEGA_MENU_HOVER"
        assert not desktop.is_menu_open()
        desktop.open_menu()
        assert desktop.is_menu_open()
        assert desktop.target_link().filter(visible=True).count() == 1

        mobile_page = browser.new_page()
        mobile_page.set_content(SYNTHETIC_HTML)
        mobile = NavigationPage(mobile_page, config, "mobile")
        mobile.wait_ready()
        assert mobile.current_mode() == "DRAWER_ACCORDION"
        assert not mobile.is_menu_open()
        mobile.open_menu()
        assert mobile.is_menu_open()
        assert mobile.target_link().filter(visible=True).count() == 1
        mobile.close_menu()
        assert not mobile.is_menu_open()
        assert mobile_page.locator("#drawer.open").count() == 0
    finally:
        browser.close()
        playwright.stop()


def test_synthetic_mobile_failure_keeps_bounded_diagnostics() -> None:
    """A neutral DOM with a mismatched target reports state without unbounded retries."""
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page()
        page.set_content(SYNTHETIC_HTML.replace('class="drawer"', 'class="drawer open"'))
        config = _synthetic_config()
        config["pages"]["navigation"]["selectors"]["target_collection"] = {
            "by": "css",
            "value": "a.target-link[href='/collections/missing']",
        }
        nav = NavigationPage(page, config, "mobile")

        def fail_fast(_timeout_ms: int) -> None:
            raise PlaywrightTimeoutError("synthetic target remains hidden")

        nav._wait_target_visible = fail_fast
        with pytest.raises(RuntimeError) as exc_info:
            nav.open_menu()

        message = str(exc_info.value)
        assert "target collection not found in mobile menu" in message
        assert "target_path='/collections/target'" in message
        assert "parent_path='/collections/parent'" in message
        assert "drawer_open=True" in message
        assert "menu_root_count=1" in message
        assert "menu_root_visible=True" in message
        assert "target_count=0" in message
        assert "parent_count=1" in message
        assert "attempts=4" in message
    finally:
        browser.close()
        playwright.stop()
