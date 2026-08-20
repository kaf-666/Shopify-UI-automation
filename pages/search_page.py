"""Search 页面对象（选择器驱动，无硬编码定位符）。

Search 模型（真实探测）：HYBRID_SEARCH
    首页头部 Trigger -> 内联搜索容器（is-active 打开）-> 输入 ->
    真实 Enter 提交 -> /search?q=... 独立结果页 -> 结果卡片
    （target=_blank 新标签页打开 PDP）。

打开状态按容器 is-active 类判定：关闭过渡期容器仍占用布局，
is_visible() 会误报，不能作为打开信号。
"""

from __future__ import annotations

import time
from typing import Optional
from urllib.parse import parse_qs, urlparse

from pages.base_page import BasePage
from pages.home_page import HomePage


class SearchSessionReloaded(Exception):
    """Search 会话被站点中断（页面重载 / predictive 遮罩异常）。"""


class SearchPage(BasePage):
    """Search 页面对象：头部内联搜索入口、提交、结果页与空态操作。"""

    PAGE_NAME = "search"

    # ------------------------------------------------------------------ 打开
    def open_from_home(self) -> None:
        """从首页打开 Search：打开首页 -> 点击 Trigger -> 等待容器打开。

        站点抗干扰：首页加载后短暂停留再交互（与人工浏览节奏一致，
        降低 Cloudflare 托管挑战触发概率）。
        """
        home = HomePage(self.page, self.site_config, self.viewport)
        home.open()
        self.page.wait_for_timeout(2500)
        home.open_search()
        self.wait_open()

    # ------------------------------------------------------------------ 控件
    def container(self):
        """返回搜索容器定位器（.site-header__search-container）。"""
        return self.locator("container").first

    def input(self):
        """返回搜索输入框定位器（input#Search）。"""
        return self.locator("input").first

    def submit_button(self):
        """返回提交按钮定位器（button.btn--search，type=submit）。"""
        return self.locator("submit").first

    def close_button(self):
        """返回关闭按钮定位器（button.btn--close-search）。"""
        return self.locator("close").first

    def clear_button(self):
        """返回清除控件定位器。

        本主题无独立 Clear 控件（输入框内无清除按钮），恒返回 None；
        桌面端 Escape 键会清空输入并关闭（见 clear_query）。
        """
        return None

    # ------------------------------------------------------------------ 状态
    def is_open(self) -> bool:
        """容器是否处于打开状态（class 含 is-active）。"""
        cls = self.container().get_attribute("class") or ""
        return "is-active" in cls.split()

    def wait_open(self, timeout_ms: int = 10_000) -> None:
        """等待容器打开（is-active 出现）。"""
        self._wait_class("is-active", present=True, timeout_ms=timeout_ms)

    def wait_closed(self, timeout_ms: int = 10_000) -> None:
        """等待容器关闭（is-active 消失）。"""
        self._wait_class("is-active", present=False, timeout_ms=timeout_ms)

    def _wait_class(self, token: str, present: bool, timeout_ms: int) -> None:
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if (token in (self.container().get_attribute("class") or "").split()) == present:
                return
            self.page.wait_for_timeout(200)
        state = "open" if present else "closed"
        raise TimeoutError(f"Search container did not reach {state} state")

    # ------------------------------------------------------------- 输入 / 提交
    def fill_query(self, query: str) -> None:
        """点击输入框并填入查询词。"""
        inp = self.input()
        inp.click()
        inp.fill(query)

    def submit_query(self, query: Optional[str] = None) -> bool:
        """真实 UI 提交：suggest 就绪后按 Enter（主题提交路径）。

        站点行为（真实探测）：
        - 输入触发 /search/suggest；请求进行中按 Enter 会被吞掉（不导航）
        - Cloudflare 托管挑战可能重载页面（同 URL），输入框同时失焦
        - 空结果查询词无建议渲染：超时兜底后 Enter 走原生表单提交

        会话被重载 / 提交被吞时重建 Search 会话重试一次。
        返回是否发生过恢复（真实记录，用于 Case Detail）。
        """
        if query is not None:
            self.fill_query(query)
        recovered = False
        last_reason = ""
        for attempt in range(2):
            try:
                self._wait_submit_ready()
                if not self.input().is_visible():
                    raise SearchSessionReloaded("search input lost visibility")
                self.input().press("Enter")
                if self._watch_navigation():
                    try:
                        self.wait_results()
                        return recovered
                    except TimeoutError:
                        # 结果页未就绪：可能被 Cloudflare 挑战弹回首页
                        raise SearchSessionReloaded("results page did not settle") from None
            except SearchSessionReloaded as exc:
                last_reason = str(exc)
            if attempt == 0:
                recovered = True
                self._reopen_session()
                if query is not None:
                    self.fill_query(query)
                continue
            raise TimeoutError(
                f"submit interrupted twice by site overlay/reload (last: {last_reason})"
            )

    def _wait_submit_ready(self, timeout_ms: int = 4_000) -> None:
        """等待提交就绪：suggest 完成（建议链接渲染或遮罩清除），超时兜底。

        站点行为：suggest 请求进行中按 Enter 会被吞掉；就绪后 Enter 正常提交。
        空结果查询词可能不渲染建议也不清除遮罩，超时后仍按 Enter
        （此时无拦截，走原生表单提交）。
        """
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            try:
                state = self.page.evaluate(
                    "(() => { const b = document.querySelector('#predictive-search');"
                    "const s = document.querySelector('.predictive__screen');"
                    "return {links: b ? b.querySelectorAll('a').length : 0,"
                    "screen: s ? s.offsetParent !== null : false}; })()"
                )
            except Exception as exc:
                raise SearchSessionReloaded(f"page reloaded: {type(exc).__name__}") from exc
            if state["links"] > 0 or not state["screen"]:
                return
            self.page.wait_for_timeout(250)

    def _watch_navigation(self, timeout_ms: int = 8_000) -> bool:
        """等待提交后的 /search 导航；未导航返回 False（调用方走恢复路径）。"""
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            try:
                if "/search" in self.page.url:
                    return True
            except Exception as exc:
                raise SearchSessionReloaded(f"page reloaded: {type(exc).__name__}") from exc
            self.page.wait_for_timeout(200)
        return False

    def _reopen_session(self) -> None:
        """重建 Search 会话：等待页面稳定后回到首页并打开 Search。

        站点抗干扰：会话中断后站点自身导航可能仍在进行，
        立即 goto 会与站点导航冲突（interrupted by another navigation），
        因此先轮询等待页面稳定再操作。
        """
        home = HomePage(self.page, self.site_config, self.viewport)
        base = home.base_url().rstrip("/")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                self.page.evaluate("document.readyState")
                if self.page.url.rstrip("/") == base:
                    home.open_search()
                    self.wait_open()
                    return
                home.open()
                return
            except Exception:
                self.page.wait_for_timeout(500)
        raise TimeoutError("session reopen failed: page not stable")

    def wait_results(self, timeout_ms: int = 15_000) -> None:
        """等待结果就绪：出现结果卡片（>0）或空态文案（0 results found）。"""
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if self.result_count() > 0 or self.no_result_state() is not None:
                return
            self.page.wait_for_timeout(250)
        raise TimeoutError("Search results did not settle (no grid and no empty state)")

    def current_query(self) -> Optional[str]:
        """从 URL 读取当前 q 参数（已解码），无 q 参数返回 None。"""
        values = parse_qs(urlparse(self.page.url).query).get("q") or []
        return values[0] if values else None

    # ------------------------------------------------------------ Predictive
    def wait_predictive_ready(self, timeout_ms: int = 4_000) -> None:
        """等待 predictive 建议渲染完成（供观察读取；会话被重载时忽略）。"""
        try:
            self._wait_submit_ready(timeout_ms)
        except SearchSessionReloaded:
            pass

    def predictive_results(self):
        """返回 predictive 建议容器定位器（#predictive-search，输入后出现）。"""
        return self.locator("predictive_results").first

    def predictive_product_cards(self):
        """返回 predictive 商品链接定位器集合（仅观察用途）。"""
        return self.locator("predictive_product")

    # ---------------------------------------------------------------- 结果页
    def results_grid(self):
        """返回结果网格定位器（#CollectionAjaxContent）。"""
        return self.locator("results_grid").first

    def result_cards(self):
        """返回结果商品卡片定位器集合（#CollectionAjaxContent .grid-product）。"""
        return self.locator("result_card")

    def result_count(self) -> int:
        """返回结果卡片数量（无结果页为 0）。"""
        return self.result_cards().count()

    def open_result(self, index: int = 0):
        """真实点击第 index 个结果卡片主链接。

        卡片链接 target=_blank，PDP 在新标签页打开；
        等待新页 URL 进入 /products/ 后返回其 Page 对象（调用方负责关闭）。
        """
        link = self.locator("result_link").nth(index)
        with self.page.context.expect_page(timeout=15_000) as new_page_info:
            link.click()
        new_page = new_page_info.value
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and "/products/" not in new_page.url:
            new_page.wait_for_timeout(200)
        return new_page

    def no_result_state(self) -> Optional[str]:
        """返回空态文案（"0 results found ..." 行），无空态返回 None。"""
        container = self.locator("no_results").first
        if not container.count():
            return None
        for line in container.inner_text().splitlines():
            if "0 results found" in line:
                return line.strip()
        return None

    def has_no_results(self) -> bool:
        """结果数量为 0 且存在空态文案。"""
        return self.result_count() == 0 and self.no_result_state() is not None

    # ------------------------------------------------------------ 关闭 / 清除
    def close(self) -> None:
        """点击关闭按钮并等待容器关闭。"""
        self.close_button().click()
        self.wait_closed()

    def clear_query(self) -> str:
        """按 Escape 关闭 Search 并返回关闭后输入框的值。

        真实站点行为：桌面端 Escape 清空输入（完整重置）；
        移动端仅关闭容器、输入值保留（站点设计，非缺陷）。
        """
        self.input().press("Escape")
        self.wait_closed()
        return self.input().input_value()

    def input_value(self) -> str:
        """读取输入框当前值（容器隐藏后仍可读取）。"""
        return self.input().input_value()

    # ---------------------------------------------------------------- 校验
    def validate_input_usable(self) -> None:
        """校验输入框存在、可见、可用且可聚焦，不满足时抛出 RuntimeError。"""
        inp = self.input()
        if not inp.count():
            raise RuntimeError("Search input not present")
        if not inp.is_visible():
            raise RuntimeError("Search input not visible")
        if not inp.is_enabled():
            raise RuntimeError("Search input not enabled")
        inp.focus()
        focused = inp.evaluate("el => document.activeElement === el")
        if not focused:
            raise RuntimeError("Search input not focusable")
