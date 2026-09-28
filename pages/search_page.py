"""配置驱动的搜索入口、状态、提交与结果页对象。"""

from __future__ import annotations

import time
from typing import Optional
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import expect

from pages.base_page import BasePage
from pages.home_page import HomePage
from pages.navigation import NavigationPage


class SearchSessionReloaded(Exception):
    """Search 会话被站点中断（页面重载 / predictive 遮罩异常）。"""


class SearchResultNavigationError(TimeoutError):
    """搜索结果未进入合法 PDP，并保留实际业务 Page 供失败证据使用。"""

    def __init__(self, message: str, *, actual_page=None):
        super().__init__(message)
        self.actual_page = actual_page


class SearchPage(BasePage):
    """Search 页面对象：入口、提交、结果页与空态操作。"""

    PAGE_NAME = "search"

    # ------------------------------------------------------------------ 打开
    def open_from_home(self) -> None:
        """从首页打开 Search：打开首页 -> 点击 Trigger -> 等待容器打开。

        站点抗干扰：首页加载后短暂停留再交互（与人工浏览节奏一致，
        降低 Cloudflare 托管挑战触发概率）。
        """
        home = HomePage(self.page, self.site_config, self.viewport)
        home.open()
        home.wait_core_ready()
        self.ensure_search_session_open(home)

    # ------------------------------------------------------------------ 控件
    def container(self):
        """返回配置的搜索容器。"""
        return self.locator("container").first

    def input(self):
        """返回配置的搜索输入框。"""
        return self.locator("input").first

    def submit_button(self):
        """返回配置的可选提交按钮。"""
        return self.locator("submit").first

    def close_button(self):
        """返回配置的关闭按钮。"""
        return self.locator("close").first

    def clear_button(self):
        """返回清除控件定位器。

        本主题无独立 Clear 控件（输入框内无清除按钮），恒返回 None；
        桌面端 Escape 键会清空输入并关闭（见 clear_query）。
        """
        return None

    def entry_strategy(self) -> str:
        configured = self.page_config().get("entry_strategy", "direct_trigger")
        if isinstance(configured, dict):
            return str(configured.get(self.viewport, "direct_trigger"))
        return str(configured)

    def _open_entry(self, home: HomePage) -> None:
        self._dismiss_blocking_overlay()
        strategy = self.entry_strategy()
        if strategy == "direct_trigger":
            expect(home.search_trigger()).to_be_visible(timeout=10_000)
            home.open_search()
        elif strategy == "navigation_drawer":
            NavigationPage(self.page, self.site_config, self.viewport).open_drawer_shell()
        else:
            raise RuntimeError(f"unsupported search entry strategy: {strategy}")

    def _dismiss_blocking_overlay(self) -> None:
        """Use an optional real close control when a marketing modal blocks entry."""
        if not (self.page_config().get("selectors") or {}).get("blocking_overlay_dismiss"):
            return
        dismiss = self.locator("blocking_overlay_dismiss").first
        wait_ms = int(self.page_config().get("blocking_overlay_wait_ms") or 0)
        if wait_ms:
            try:
                dismiss.wait_for(state="visible", timeout=wait_ms)
            except PlaywrightTimeoutError:
                return
        if dismiss.count() and dismiss.is_visible():
            dismiss.click(timeout=3_000)
            expect(dismiss).not_to_be_visible(timeout=3_000)

    # ------------------------------------------------------------------ 状态
    def is_open(self) -> bool:
        """配置的 open-state 选择器是否匹配。"""
        return self.locator("open_state").count() > 0

    def wait_open(self, timeout_ms: int = 10_000) -> None:
        expect(self.locator("open_state").first).to_be_visible(timeout=timeout_ms)

    def wait_closed(self, timeout_ms: int = 10_000) -> None:
        expect(self.locator("open_state")).to_have_count(0, timeout=timeout_ms)

    # ------------------------------------------------------------- 输入 / 提交
    def fill_query(self, query: str) -> None:
        """点击输入框并填入查询词。"""
        if not str(query or "").strip():
            raise ValueError("search query must not be empty")
        inp = self.input()
        inp.click()
        inp.fill(query)

    def submit_query(self, query: Optional[str] = None) -> bool:
        """真实 UI 提交：建议状态就绪后使用配置的提交动作。

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
                if str(self.page_config().get("submit_strategy") or "enter") == "button":
                    self.submit_button().click()
                else:
                    self.input().press("Enter")
                if self._watch_navigation():
                    try:
                        self.wait_results()
                        return recovered
                    except TimeoutError:
                        # 结果页未就绪：可能被 Cloudflare 挑战弹回首页
                        raise SearchSessionReloaded("results page did not settle") from None
                # The navigation can complete between the key press and the
                # wait_for_url subscription (especially when the result page
                # is served from a warm cache). Accept the already-settled
                # same-page state instead of treating a missed event as an
                # overlay/reload failure.
                if "/search" in self.page.url and (
                    query is None or self.current_query() == query
                ):
                    try:
                        self.wait_results()
                        return recovered
                    except TimeoutError:
                        raise SearchSessionReloaded("results page did not settle") from None
            except SearchSessionReloaded as exc:
                last_reason = str(exc)
                # A reload/overlay can race with the navigation watcher while
                # the final /search document is already settling. If the
                # business URL and query are correct, use the result grid as
                # the authoritative completion signal before rebuilding the
                # session.
                if "/search" in self.page.url and (
                    query is None or self.current_query() == query
                ):
                    try:
                        self.wait_results()
                        return recovered
                    except TimeoutError:
                        pass
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
        """按配置观察可选 predictive UI；无该能力时直接走 Enter。"""
        selectors = self.page_config().get("selectors") or {}
        if not selectors.get("predictive_loading"):
            return
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            try:
                loading = self.locator("predictive_loading").count() > 0
                links = self.locator("predictive_product").count() if selectors.get("predictive_product") else 0
            except Exception as exc:
                raise SearchSessionReloaded(f"page reloaded: {type(exc).__name__}") from exc
            if links > 0 or not loading:
                return
            self.page.wait_for_timeout(250)

    def _watch_navigation(self, timeout_ms: int = 8_000) -> bool:
        """等待提交后的 /search 导航；未导航返回 False（调用方走恢复路径）。"""
        try:
            self.page.wait_for_url(lambda url: "/search" in url, timeout=timeout_ms)
            return True
        except PlaywrightTimeoutError:
            return False

    def ensure_search_session_open(
        self, home: Optional[HomePage] = None, timeout_ms: int = 10_000
    ) -> None:
        """确保首页 Search 会话已打开，且输入框可见、可用、可编辑。"""
        home = home or HomePage(self.page, self.site_config, self.viewport)
        if self.page.url.rstrip("/") != home.base_url().rstrip("/"):
            raise RuntimeError("Search session can only be opened from Home")
        if not self.is_open():
            self._open_entry(home)
        self.wait_open(timeout_ms=timeout_ms)
        inp = self.input()
        expect(inp).to_be_visible(timeout=timeout_ms)
        expect(inp).to_be_enabled(timeout=timeout_ms)
        expect(inp).to_be_editable(timeout=timeout_ms)

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
                if self.page.url.rstrip("/") != base:
                    home.open()
                self.ensure_search_session_open(home)
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
        """返回可选 predictive 建议容器。"""
        if not (self.page_config().get("selectors") or {}).get("predictive_results"):
            return None
        return self.locator("predictive_results").first

    def predictive_product_cards(self):
        """返回 predictive 商品链接定位器集合（仅观察用途）。"""
        if not (self.page_config().get("selectors") or {}).get("predictive_product"):
            return None
        return self.locator("predictive_product")

    def predictive_product_count(self) -> int:
        cards = self.predictive_product_cards()
        return cards.count() if cards is not None else 0

    # ---------------------------------------------------------------- 结果页
    def results_grid(self):
        """返回配置的结果网格。"""
        return self.locator("results_grid").first

    def result_cards(self):
        """返回配置的结果商品卡片。"""
        return self.locator("result_card")

    def result_count(self) -> int:
        """返回结果卡片数量（无结果页为 0）。"""
        return self.result_cards().count()

    @staticmethod
    def _is_product_url(url: str) -> bool:
        return urlparse(url).path.startswith("/products/")

    @staticmethod
    def _safe_diagnostic_url(url: str) -> str:
        """去除 query/fragment，避免诊断信息携带请求参数。"""
        parsed = urlparse(str(url or ""))
        if parsed.scheme or parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        return parsed.path

    def open_result(self, index: int = 0, timeout_ms: int = 15_000):
        """点击结果并返回实际 PDP Page，支持 same-page 与 new-page。"""
        current_page = self.page
        context = current_page.context
        link = self.locator("result_link").nth(index)
        source_page_url = self._safe_diagnostic_url(current_page.url)
        target_href = self._safe_diagnostic_url(link.get_attribute("href") or "")
        created_pages = []

        def capture_page(page) -> None:
            if page is not current_page:
                created_pages.append(page)

        context.on("page", capture_page)
        deadline = time.monotonic() + timeout_ms / 1000
        actual_page = None
        try:
            link.click()

            if created_pages:
                actual_page = created_pages[-1]
            elif self._is_product_url(current_page.url):
                return current_page
            else:
                remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
                try:
                    current_page.wait_for_url(
                        self._is_product_url,
                        wait_until="commit",
                        timeout=remaining_ms,
                    )
                    return current_page
                except PlaywrightTimeoutError:
                    if created_pages:
                        actual_page = created_pages[-1]
                    else:
                        raise

            if self._is_product_url(actual_page.url):
                return actual_page
            remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
            actual_page.wait_for_url(
                self._is_product_url,
                wait_until="commit",
                timeout=remaining_ms,
            )
            return actual_page
        except Exception as exc:
            if actual_page is None and created_pages:
                actual_page = created_pages[-1]
            evidence_page = actual_page or current_page
            actual_url = self._safe_diagnostic_url(evidence_page.url)
            message = (
                f"source_page_url={source_page_url} target_href={target_href} "
                f"new_page_created={bool(created_pages)} actual_page_url={actual_url} "
                f"actual_page_count={len(context.pages)}"
            )
            raise SearchResultNavigationError(message, actual_page=evidence_page) from exc
        finally:
            context.remove_listener("page", capture_page)

    def no_result_state(self) -> Optional[str]:
        """返回空态文案（"0 results found ..." 行），无空态返回 None。"""
        if not (self.page_config().get("selectors") or {}).get("no_results"):
            return None
        container = self.locator("no_results").first
        if not container.count():
            return None
        expected = str(self.page_config().get("no_results_text") or "0 results found").casefold()
        for line in container.inner_text().splitlines():
            if expected in line.casefold():
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

    def reopen(self) -> None:
        self.ensure_search_session_open()

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
