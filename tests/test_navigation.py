"""Offline regression tests for the shared navigation page object."""

from __future__ import annotations

import pytest

from pages.navigation import NavigationPage
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError


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
    nav._mobile_parent_selector = lambda: "a.gm-target[href*='/collections/new-collection']"
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
