"""Header / Navigation 页面对象（选择器驱动，无硬编码定位符）。

导航模型（真实探测）：
    Desktop : MEGA_MENU_HOVER
        桌面端主导航为 Gm mega menu（ul.site-nav.gm-menu），
        顶层项含 mega 子菜单；真实 hover 顶层项展开子菜单。
    Mobile  : DRAWER_ACCORDION
        Brooklyn #NavDrawer 抽屉内嵌 Gm 移动菜单，
        点击汉堡打开抽屉，点击顶层项展开子菜单（click_toggle）。

打开状态判定：
    Desktop 子菜单：gm-submenu 容器可见。
    Mobile 抽屉：drawer--is-open 类（过渡期 is_visible 会误报，按 class 判定）。

目标 Collection 按 site config 中的稳定 pathname / selector 定位，
不依赖文本层级或 nth-child。
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

MOBILE_MENU_ROOT_TIMEOUT_MS = 5_000
MOBILE_MENU_RETRY_ATTEMPTS = 4
MOBILE_MENU_RETRY_INTERVAL_MS = 250
MOBILE_MENU_TARGET_WAIT_MS = 4_000


class NavigationPage(BasePage):
    """Header 导航页面对象：桌面 mega 菜单与移动抽屉的真实展开 / 点击路径。"""

    PAGE_NAME = "navigation"

    # ------------------------------------------------------------------ 模式
    def current_mode(self) -> str:
        """返回当前端的导航交互模型。"""
        return MODE_DESKTOP if self.viewport == "desktop" else MODE_MOBILE

    def _menu_scope(self) -> str:
        """返回当前端菜单作用域 CSS（目标链接定位 / 顶层项解析共用）。"""
        name = "desktop_menu" if self.viewport == "desktop" else "mobile_menu"
        return str(self.resolve_selector(name)["value"])

    # ------------------------------------------------------------------ 控件
    def header(self):
        """返回站点头部定位器（header#SiteHeader）。"""
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
        """返回当前端顶层菜单项（li.gm-item）定位器集合。"""
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

    def _target_top_li(self):
        """返回目标链接所在顶层菜单项的 JSHandle；找不到返回 None。"""
        target = self.target_link().first
        if target.count() == 0:
            return None
        return target.evaluate_handle(
            """(a) => {
                let n = a;
                while (n && !(n.classList && n.classList.contains('gm-level-0'))) {
                    n = n.parentElement;
                }
                return n;
            }"""
        )

    def _target_top_link(self):
        """返回目标顶层菜单项的直接链接定位器（hover / 点击展开用）。"""
        if self.viewport == "mobile":
            return self._mobile_target_top_link()
        handle = self._target_top_li()
        if handle is None:
            return None
        link = handle.as_element().query_selector(":scope > a")
        return link

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
        return self.primary_menu().locator(selector).first

    def _mobile_target_top_link(self):
        """使用 Locator 重新解析移动端顶层入口，避免跨 rerender 持有节点。"""
        target = self.target_link().first
        if target.count():
            top_li = target.locator(
                "xpath=ancestor::li[contains(concat(' ', normalize-space(@class), ' '), ' gm-level-0 ')][1]"
            )
            direct_link = top_li.locator(":scope > a").first
            if direct_link.count():
                return direct_link
        # 目标可能在父级展开后才插入 DOM；空 Locator 是合法的暂态，
        # open_menu() 会在有限窗口内重新查询，而不是立即判定为失败。
        return self._mobile_parent_locator()

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
        """当前端菜单层级是否已打开。

        Desktop：目标顶层项的子菜单（gm-submenu）可见。
        Mobile：抽屉 drawer--is-open 类存在。
        """
        if self.viewport == "desktop":
            handle = self._target_top_li()
            if handle is None:
                return False
            submenu = handle.as_element().query_selector(":scope .gm-submenu")
            if submenu is None:
                return False
            return submenu.is_visible()
        drawer = self.locator("mobile_drawer").first
        return "drawer--is-open" in (drawer.get_attribute("class") or "").split()

    def _wait_open_state(self, timeout_ms: int = 10_000) -> None:
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if self.is_menu_open():
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

    def open_menu(self) -> None:
        """按当前端真实交互打开商品导航层级并使目标链接可见。

        Desktop：真实 hover 目标顶层项（重试 ≤3 次——主题 hover 展开
        并非每次触发，以目标链接可见为准）。
        Mobile：抽屉未开时点击汉堡；随后真实点击目标顶层项展开
        子菜单（click_toggle），以目标链接可见为准。
        """
        if self.viewport == "desktop":
            # Gm 菜单在页面加载后可能重渲染（节点被替换），
            # 每次 hover 尝试重新查询目标链接，避免手持失效 ElementHandle。
            for _attempt in range(3):
                link = self._target_top_link()
                if link is None:
                    raise RuntimeError("target collection not found in desktop menu")
                try:
                    link.hover()
                except Exception:
                    # 节点在 hover 前被替换：重新查询后再试
                    continue
                try:
                    self._wait_target_visible(timeout_ms=4000)
                    return
                except TimeoutError:
                    continue
            raise TimeoutError("desktop mega menu did not open after 3 hover attempts")
        if not self.is_menu_open():
            trigger = self.menu_trigger()
            if not trigger.is_visible():
                raise RuntimeError("mobile menu trigger is not visible")
            trigger.click()
            self._wait_open_state()

        # Drawer open 只代表外层状态；菜单子树可能还在异步挂载。
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

    def close_menu(self) -> None:
        """关闭移动端导航抽屉（桌面端 hover 菜单无关闭控件，跳过）。"""
        if self.viewport != "mobile":
            return
        close_btn = self.locator("mobile_close").first
        if close_btn.count() and close_btn.is_visible():
            close_btn.click()
            expect(self.locator("mobile_drawer").first).not_to_have_class(
                re.compile(r"(?:^|\s)drawer--is-open(?:\s|$)"), timeout=10_000
            )

    # ------------------------------------------------------------ 目标导航
    def target_path(self) -> str:
        """返回配置的目标 Collection 路径。"""
        cfg = self.page_config()
        return str((cfg.get("smoke_collection") or {}).get("path") or "")

    def wait_ready(self, timeout_ms: int = 12_000) -> None:
        """等待当前端导航容器和入口完成渲染。"""
        expect(self.header()).to_be_visible(timeout=timeout_ms)
        if self.viewport == "desktop":
            expect(self.primary_items().first).to_be_visible(timeout=timeout_ms)
        else:
            expect(self.menu_trigger()).to_be_visible(timeout=timeout_ms)

    def _wait_url_path(self, path: str, timeout_ms: int = 15_000) -> None:
        try:
            self.page.wait_for_url(
                lambda url: urlparse(str(url)).path == path,
                timeout=timeout_ms,
            )
        except PlaywrightTimeoutError as exc:
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
