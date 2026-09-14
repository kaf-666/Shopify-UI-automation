"""Header / Navigation page object with configuration-selected strategies.

Selectors and interaction behavior are both site-profile data.  The public
``open_collection()`` API stays unchanged while the configured desktop/mobile
strategy selects the actual interaction model.
"""

from __future__ import annotations

import time
import re
from typing import List, Optional
from urllib.parse import urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import expect

from pages.base_page import BasePage

MODE_DESKTOP = "MEGA_MENU_HOVER"
MODE_MOBILE = "DRAWER_ACCORDION"
MODE_DIRECT_LINK = "DIRECT_LINK"
MODE_DRAWER_DIRECT = "DRAWER_DIRECT"

MOBILE_MENU_ROOT_TIMEOUT_MS = 5_000
MOBILE_MENU_RETRY_ATTEMPTS = 4
MOBILE_MENU_RETRY_INTERVAL_MS = 250
MOBILE_MENU_TARGET_WAIT_MS = 4_000


class NavigationStrategyError(RuntimeError):
    """Raised when a page object receives an unsupported navigation strategy."""


class NavigationStrategy:
    """Small behavior contract implemented by each supported interaction model."""

    key = ""
    mode = ""

    def is_menu_open(self, navigation: "NavigationPage") -> bool:
        raise NotImplementedError

    def open_menu(self, navigation: "NavigationPage") -> None:
        raise NotImplementedError

    def close_menu(self, navigation: "NavigationPage") -> None:
        return None

    def wait_ready(self, navigation: "NavigationPage", timeout_ms: int) -> None:
        raise NotImplementedError


class MegaMenuHoverStrategy(NavigationStrategy):
    key = "mega_menu_hover"
    mode = MODE_DESKTOP

    def is_menu_open(self, navigation: "NavigationPage") -> bool:
        return navigation._target_visible()

    def open_menu(self, navigation: "NavigationPage") -> None:
        navigation._open_desktop_hover_menu()

    def wait_ready(self, navigation: "NavigationPage", timeout_ms: int) -> None:
        navigation._wait_desktop_ready(timeout_ms)


class DirectLinkStrategy(NavigationStrategy):
    key = "direct_link"
    mode = MODE_DIRECT_LINK

    def is_menu_open(self, navigation: "NavigationPage") -> bool:
        return navigation._target_visible()

    def open_menu(self, navigation: "NavigationPage") -> None:
        navigation._open_direct_link_menu()

    def wait_ready(self, navigation: "NavigationPage", timeout_ms: int) -> None:
        navigation._wait_desktop_ready(timeout_ms)


class DrawerAccordionStrategy(NavigationStrategy):
    key = "drawer_accordion"
    mode = MODE_MOBILE

    def is_menu_open(self, navigation: "NavigationPage") -> bool:
        return navigation._drawer_is_open()

    def open_menu(self, navigation: "NavigationPage") -> None:
        navigation._open_drawer_accordion_menu()

    def close_menu(self, navigation: "NavigationPage") -> None:
        navigation._close_drawer_menu()

    def wait_ready(self, navigation: "NavigationPage", timeout_ms: int) -> None:
        navigation._wait_drawer_ready(timeout_ms)


class DrawerDirectStrategy(NavigationStrategy):
    key = "drawer_direct"
    mode = MODE_DRAWER_DIRECT

    def is_menu_open(self, navigation: "NavigationPage") -> bool:
        return navigation._drawer_is_open()

    def open_menu(self, navigation: "NavigationPage") -> None:
        navigation._open_drawer_direct_menu()

    def close_menu(self, navigation: "NavigationPage") -> None:
        navigation._close_drawer_menu()

    def wait_ready(self, navigation: "NavigationPage", timeout_ms: int) -> None:
        navigation._wait_drawer_ready(timeout_ms)


STRATEGY_TYPES = {
    strategy.key: strategy
    for strategy in (
        MegaMenuHoverStrategy,
        DirectLinkStrategy,
        DrawerAccordionStrategy,
        DrawerDirectStrategy,
    )
}


def resolve_navigation_strategy(value: str) -> NavigationStrategy:
    """Return a supported strategy or fail explicitly without a fallback."""

    key = str(value or "").strip().lower()
    strategy_type = STRATEGY_TYPES.get(key)
    if strategy_type is None:
        raise NavigationStrategyError(f"unsupported navigation strategy: {key or '<missing>'}")
    return strategy_type()


class NavigationPage(BasePage):
    """Header 导航页面对象：双端菜单的真实展开与 GET-only 点击路径。"""

    PAGE_NAME = "navigation"

    # ------------------------------------------------------------------ 模式
    def strategy_name(self) -> str:
        """Return the configured interaction strategy for this viewport."""
        capabilities = self.site_config.get("capabilities") or {}
        navigation = capabilities.get("navigation") if isinstance(capabilities, dict) else None
        if not isinstance(navigation, dict):
            raise NavigationStrategyError("missing capabilities.navigation")
        value = str(navigation.get(self.viewport) or "").strip().lower()
        if not value:
            raise NavigationStrategyError(f"missing navigation strategy for viewport={self.viewport}")
        return value

    def strategy(self) -> NavigationStrategy:
        """Resolve the current viewport's configured strategy without site-name logic."""
        return resolve_navigation_strategy(self.strategy_name())

    def current_mode(self) -> str:
        """返回当前端的导航交互模型。"""
        return self.strategy().mode

    def _menu_scope(self) -> str:
        """返回当前端菜单作用域 CSS（目标链接定位共用）。"""
        name = "desktop_menu" if self.viewport == "desktop" else "mobile_menu"
        return str(self.resolve_selector(name)["value"])

    # ------------------------------------------------------------------ 控件
    def header(self):
        """返回配置的站点头部定位器。"""
        return self.locator("header").first

    def menu_trigger(self):
        """返回移动端汉堡按钮定位器（桌面端返回 None）。"""
        if self.viewport != "mobile":
            return None
        return self.locator("mobile_trigger").first

    def primary_menu(self):
        """返回当前端主导航菜单容器定位器。"""
        name = "desktop_menu" if self.viewport == "desktop" else "mobile_menu"
        return self.locator(name).first

    def primary_items(self):
        """返回当前端配置的顶层菜单项定位器集合。"""
        name = "desktop_menu_item" if self.viewport == "desktop" else "mobile_menu_item"
        return self.locator(name)

    def primary_item_names(self) -> List[str]:
        """返回顶层菜单项文本（去重、去空；含徽标的文本归一化）。"""
        names = []
        for i in range(self.primary_items().count()):
            text = " ".join(self.primary_items().nth(i).inner_text().split())
            if text and text not in names:
                names.append(text)
        return names

    def target_link(self):
        """返回目标 Collection 链接定位器（按稳定 pathname 匹配）。"""
        return self.primary_menu().locator(self.resolve_selector("target_collection")["value"])

    def _target_top_link(self):
        """返回配置的目标父级入口（hover / 点击展开用）。"""
        if self.viewport == "mobile":
            return self._mobile_parent_locator()
        return self.locator("desktop_target_parent").first

    def _mobile_parent_selector(self) -> Optional[str]:
        """返回移动端目标父级入口 selector；未配置时返回 None。"""
        try:
            selector = self.resolve_selector("mobile_target_parent").get("value")
        except KeyError:
            return None
        return str(selector or "") or None

    def _mobile_parent_locator(self):
        """每次调用都重新解析移动端 accordion 父级入口。"""
        selector = self._mobile_parent_selector()
        if not selector:
            return None
        # Mobile themes can keep a hidden duplicate of the menu while the
        # drawer hydrates.  Selecting ``.first`` may therefore return that
        # duplicate even though a visible parent is already actionable.  Keep
        # the locator live and let each caller re-evaluate visibility on the
        # current DOM; this also preserves the bounded retry behavior while
        # the menu is being replaced.
        return self.primary_menu().locator(selector).filter(visible=True).first

    def _mobile_menu_root_ready(self) -> bool:
        """移动端 drawer 打开后，确认实际菜单子树已挂载且可见。"""
        try:
            root = self.primary_menu()
            return bool(root.count() and root.is_visible())
        except Exception:
            return False

    def _mobile_failure_detail(self, prefix: str, attempts: int) -> str:
        """生成不含凭证的移动导航状态诊断。"""
        try:
            target_path = self.target_path()
        except Exception:
            target_path = "<unavailable>"

        parent_selector = self._mobile_parent_selector()
        parent_match = re.search(
            r"href(?:[*^$|~])?=\s*['\"]([^'\"]+)['\"]",
            parent_selector or "",
        )
        parent_path = parent_match.group(1) if parent_match else (parent_selector or "<not configured>")

        try:
            drawer_open = bool(self.is_menu_open())
        except Exception:
            drawer_open = False

        try:
            root = self.primary_menu()
            root_count = int(root.count())
            root_visible = bool(root_count and root.is_visible())
        except Exception:
            root_count = 0
            root_visible = False

        try:
            target_count, target_visible_count = self._locator_counts(self.target_link())
        except Exception:
            target_count, target_visible_count = 0, 0

        try:
            parent_count, parent_visible_count = self._locator_counts(
                self._mobile_parent_locator()
            )
        except Exception:
            parent_count, parent_visible_count = 0, 0

        return (
            f"{prefix} (target_path={target_path!r} parent_path={parent_path!r} "
            f"drawer_open={drawer_open} menu_root_count={root_count} "
            f"menu_root_visible={root_visible} target_count={target_count} "
            f"target_visible_count={target_visible_count} parent_count={parent_count} "
            f"parent_visible_count={parent_visible_count} attempts={attempts})"
        )

    @staticmethod
    def _locator_counts(locator) -> tuple[int, int]:
        """返回 locator 总数与可见数；诊断读取失败时安全降级为 0。"""
        if locator is None:
            return 0, 0
        try:
            count = int(locator.count())
            if not count:
                return 0, 0
            visible_count = int(locator.filter(visible=True).count())
            return count, visible_count
        except Exception:
            return 0, 0

    # ------------------------------------------------------------------ 状态
    def is_menu_open(self) -> bool:
        """Return whether the configured strategy's menu state is open."""
        return self.strategy().is_menu_open(self)

    def _drawer_is_open(self) -> bool:
        """Return the configured drawer open-state without assuming a site."""
        open_state = self.locator("mobile_drawer_open").first
        return bool(open_state.count() and open_state.is_visible())

    def _wait_open_state(self, timeout_ms: int = 10_000) -> None:
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if self._drawer_is_open():
                return
            self.page.wait_for_timeout(200)
        raise TimeoutError(f"Navigation menu did not open ({self.current_mode()})")

    def _wait_mobile_menu_root(self, timeout_ms: int = MOBILE_MENU_ROOT_TIMEOUT_MS) -> None:
        """等待 drawer 内实际 mobile menu root 挂载并可见。"""
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if self._mobile_menu_root_ready():
                return
            remaining_ms = int(max(0, (deadline - time.monotonic()) * 1000))
            if remaining_ms:
                self.page.wait_for_timeout(min(MOBILE_MENU_RETRY_INTERVAL_MS, remaining_ms))
        raise TimeoutError(
            self._mobile_failure_detail("mobile menu root not ready", attempts=0)
        )

    # ------------------------------------------------------------------ 打开
    def _target_visible(self) -> bool:
        """目标 Collection 链接是否可见（当前端菜单作用域内）。"""
        target = self.target_link().filter(visible=True).first
        return bool(target.count() and target.is_visible())

    def _wait_target_visible(self, timeout_ms: int = 5_000) -> None:
        expect(self.target_link().filter(visible=True).first).to_be_visible(timeout=timeout_ms)

    def _open_desktop_hover_menu(self) -> None:
        """Open a configured mega menu through the real hover interaction."""
        # Themes can replace menu nodes immediately after initial render.  Each
        # retry resolves a fresh locator rather than retaining an old handle.
        for _attempt in range(3):
            link = self._target_top_link()
            if link is None:
                raise RuntimeError("target collection parent not found in desktop menu")
            try:
                link.hover()
            except Exception:
                continue
            try:
                self._wait_target_visible(timeout_ms=4000)
                return
            except TimeoutError:
                continue
        raise TimeoutError("desktop mega menu did not open after 3 hover attempts")

    def _open_direct_link_menu(self) -> None:
        """Validate a visible direct collection link; no hidden fallback interaction."""
        if not self._target_visible():
            self._wait_target_visible(timeout_ms=4_000)

    def _ensure_drawer_open(self) -> None:
        """Open the configured drawer when its strategy needs one."""
        if not self.is_menu_open():
            trigger = self.menu_trigger()
            if trigger is None:
                raise RuntimeError("drawer navigation requires a mobile menu trigger")
            if not trigger.is_visible():
                raise RuntimeError("mobile menu trigger is not visible")
            trigger.click()
            self._wait_open_state()

    def _open_drawer_accordion_menu(self) -> None:
        """Open a drawer and expand its configured accordion parent when needed."""
        self._ensure_drawer_open()
        # Drawer open only marks the outer shell; wait for the mounted menu tree.
        self._wait_mobile_menu_root()
        attempts = 0
        for _attempt in range(MOBILE_MENU_RETRY_ATTEMPTS):
            attempts += 1
            if self._target_visible():
                return
            link = self._target_top_link()
            if link is not None:
                try:
                    # Empty / hidden parent locator is also a合法过渡态；下一轮
                    # 重新查询，覆盖 menu root 已挂载但 parent 尚未出现的竞态。
                    if link.count() and link.is_visible():
                        link.click()
                        self._wait_target_visible(timeout_ms=MOBILE_MENU_TARGET_WAIT_MS)
                        return
                except (PlaywrightTimeoutError, TimeoutError):
                    # Accordion click may not have produced the target yet；bounded
                    # retry will re-query the current DOM.
                    pass
                except Exception:
                    # DOM rerender around click: discard this locator and re-query.
                    pass
            self.page.wait_for_timeout(MOBILE_MENU_RETRY_INTERVAL_MS)

        raise RuntimeError(
            self._mobile_failure_detail(
                "target collection not found in mobile menu",
                attempts=attempts,
            )
        )

    def _open_drawer_direct_menu(self) -> None:
        """Open a drawer whose collection target is directly reachable."""
        self._ensure_drawer_open()
        self._wait_mobile_menu_root()
        if not self._target_visible():
            self._wait_target_visible(timeout_ms=MOBILE_MENU_TARGET_WAIT_MS)

    def open_menu(self) -> None:
        """Open the current strategy's navigation state and reveal its target link."""
        self.strategy().open_menu(self)

    def _close_drawer_menu(self) -> None:
        """Close a configured drawer; non-drawer strategies use a no-op strategy method."""
        close_btn = self.locator("mobile_close").first
        if close_btn.count() and close_btn.is_visible():
            close_btn.click()
            expect(self.locator("mobile_drawer_open").first).to_have_count(
                0, timeout=10_000
            )

    def close_menu(self) -> None:
        """Close the current strategy when it exposes an explicit close action."""
        self.strategy().close_menu(self)

    # ------------------------------------------------------------ 目标导航
    def target_path(self) -> str:
        """返回配置的目标 Collection 路径。"""
        cfg = self.page_config()
        return str((cfg.get("smoke_collection") or {}).get("path") or "")

    def wait_ready(self, timeout_ms: int = 12_000) -> None:
        """Wait for the configured navigation strategy's entry controls."""
        self.strategy().wait_ready(self, timeout_ms)

    def _wait_desktop_ready(self, timeout_ms: int) -> None:
        expect(self.header()).to_be_visible(timeout=timeout_ms)
        expect(self.primary_items().first).to_be_visible(timeout=timeout_ms)

    def _wait_drawer_ready(self, timeout_ms: int) -> None:
        expect(self.header()).to_be_visible(timeout=timeout_ms)
        trigger = self.menu_trigger()
        if trigger is None:
            raise RuntimeError("drawer navigation requires a mobile menu trigger")
        expect(trigger).to_be_visible(timeout=timeout_ms)

    def _wait_url_path(self, path: str, timeout_ms: int = 15_000) -> None:
        # A fast same-page navigation may finish before Playwright subscribes
        # to the URL event. The current URL is authoritative once the click
        # has returned, so avoid turning that race into a false failure.
        if urlparse(str(self.page.url)).path == path:
            return
        try:
            self.page.wait_for_url(
                lambda url: urlparse(str(url)).path == path,
                timeout=timeout_ms,
            )
        except PlaywrightTimeoutError as exc:
            if urlparse(str(self.page.url)).path == path:
                return
            raise TimeoutError(f"navigation to {path} not observed (url={self.page.url[:100]})") from exc

    def open_collection(self) -> str:
        """真实 UI 导航到目标 Collection 并返回最终 URL。

        路径：菜单打开 -> 目标链接可见 -> 真实点击 -> 等待 URL pathname。
        菜单若已关闭（桌面 hover 移开 / 抽屉关闭）会重新展开。
        """
        path = self.target_path()
        if not path:
            raise RuntimeError("smoke_collection.path not configured")
        if not self.is_menu_open():
            self.open_menu()
        target = self.target_link().filter(visible=True).first
        if not (target.count() and target.is_visible()):
            # 重试一次展开（桌面 hover 偶发未触发）
            self.open_menu()
            target = self.target_link().filter(visible=True).first
        if not (target.count() and target.is_visible()):
            raise RuntimeError(f"target collection link not visible: {path}")
        target.click()
        self._wait_url_path(path)
        return self.page.url
