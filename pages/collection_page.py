"""商品列表（PLP）页面对象。

包含商品网格 / 卡片读取、颜色快捷筛选（两级结构：
可展开族 EXPANDABLE 与直接筛选族 DIRECT）、排序操作与商品快照辅助。
"""

from __future__ import annotations

import math
import time
from typing import Callable, List, Optional
from urllib.parse import parse_qs, urljoin, urlparse

from pages.base_page import BasePage
from utils.browser import PAGE_NAV_TIMEOUT_MS


COLLECTION_PRODUCTS_TIMEOUT_MS = 15_000
COLLECTION_PRODUCTS_POLL_MS = 250
COLLECTION_PRODUCTS_STABLE_SNAPSHOTS = 2


class CollectionProductsReadinessError(TimeoutError):
    """商品列表未在 bounded timeout 内进入稳定可操作状态。"""


class CollectionPage(BasePage):
    """商品列表页对象：网格/卡片、颜色快捷筛选（EXPANDABLE / DIRECT）与排序。"""

    PAGE_NAME = "collection"

    def open(self) -> None:
        super().open(ready_selector="product_grid")

    # ------------------------------------------------------------- 网格基础
    def product_grid(self):
        """返回商品网格容器定位器。"""
        return self.locator("product_grid").first

    def product_cards(self):
        """返回商品卡定位器集合。"""
        return self.locator("product_card")

    def visible_product_cards(self):
        """返回当前可见、可作为 index 目标的商品卡集合。"""
        return self.product_cards().filter(visible=True)

    def product_links(self):
        """返回配置在商品卡内部的全部主商品链接。"""
        return self._card_descendant(self.product_cards(), "product_link")

    def _card_descendant(self, card, selector_name: str):
        """Resolve a configured product-card descendant without theme classes.

        Card selectors are deliberately site-profile data because themes often
        change their card DOM independently of collection behavior.  CSS and
        XPath are supported here because both can be scoped to a card locator.
        """
        selector = self.resolve_selector(selector_name)
        by = str(selector.get("by") or "css").lower()
        value = str(selector.get("value") or "")
        if by == "css":
            return card.locator(value)
        if by == "xpath":
            return card.locator(f"xpath={value}")
        raise RuntimeError(
            f"collection selector '{selector_name}' must use css or xpath for card-relative lookup"
        )

    def product_link(self, card):
        """Return the configured primary product link for one card."""
        return self._card_descendant(card, "product_link").first

    def product_title(self, card):
        """Return the configured user-visible product title for one card."""
        return self._card_descendant(card, "product_title").first

    def product_count(self) -> int:
        """返回当前页可见商品卡数量。"""
        return self.product_cards().count()

    @staticmethod
    def _visible_count(locator) -> int:
        """Count currently visible matches without caching DOM nodes."""
        return locator.filter(visible=True).count()

    def _optional_loading_snapshot(self) -> tuple[bool, int, int]:
        """Read an optional configured loading/skeleton locator for diagnostics."""
        selectors = self.page_config().get("selectors") or {}
        selector_name = next(
            (name for name in ("loading", "skeleton") if selectors.get(name)), None
        )
        if selector_name is None:
            return False, 0, 0
        loading = self.locator(selector_name)
        return True, loading.count(), self._visible_count(loading)

    def _products_readiness_snapshot(self, index: int) -> dict:
        """Read one live collection/product snapshot for the requested card index."""
        grid = self.product_grid()
        cards = self.product_cards()
        visible_cards = self.visible_product_cards()
        links = self.product_links()

        grid_count = grid.count()
        card_count = cards.count()
        visible_card_count = visible_cards.count()
        link_count = links.count()
        target_link_count = 0
        target_link_visible = False
        target_href = ""
        target_href_valid = False

        if visible_card_count > index:
            target_link = self.product_link(visible_cards.nth(index))
            target_link_count = target_link.count()
            if target_link_count:
                target_link_visible = target_link.is_visible()
                target_href = str(target_link.get_attribute("href") or "").strip()
                if target_href:
                    try:
                        self._canonical_product_href(target_href)
                        target_href_valid = True
                    except RuntimeError:
                        pass

        loading_configured, loading_count, loading_visible_count = (
            self._optional_loading_snapshot()
        )
        try:
            ready_state = str(self.page.evaluate("document.readyState"))
        except Exception:
            ready_state = "UNKNOWN"

        return {
            "url": str(getattr(self.page, "url", "") or "")
            .split("?", 1)[0]
            .split("#", 1)[0],
            "document_ready_state": ready_state,
            "target_index": index,
            "grid_count": grid_count,
            "grid_visible": bool(grid_count and grid.is_visible()),
            "product_card_count": card_count,
            "product_card_visible_count": visible_card_count,
            "product_link_count": link_count,
            "product_link_visible_count": self._visible_count(links),
            "target_link_count": target_link_count,
            "target_link_visible": target_link_visible,
            "target_href": target_href,
            "target_href_valid": target_href_valid,
            "loading_configured": loading_configured,
            "loading_count": loading_count,
            "loading_visible_count": loading_visible_count,
        }

    @staticmethod
    def _products_snapshot_ready(snapshot: dict) -> bool:
        index = int(snapshot["target_index"])
        return bool(
            snapshot["grid_count"] > 0
            and snapshot["grid_visible"]
            and snapshot["product_card_visible_count"] >= index + 1
            and snapshot["target_link_count"] > 0
            and snapshot["target_link_visible"]
            and snapshot["target_href"]
            and snapshot["target_href_valid"]
        )

    @staticmethod
    def _emit_products_readiness_diagnostic(
        snapshot: dict,
        *,
        elapsed_ms: int,
        poll_attempts: int,
        consecutive_ready_snapshots: int,
        diagnostics_hook: Optional[Callable[[dict], None]],
    ) -> None:
        """Emit a safe diagnostic snapshot without affecting readiness behavior."""
        if diagnostics_hook is None:
            return
        safe_snapshot = dict(snapshot)
        safe_snapshot.update(
            {
                "elapsed_ms": elapsed_ms,
                "poll_attempts": poll_attempts,
                "consecutive_ready_snapshots": consecutive_ready_snapshots,
            }
        )
        try:
            diagnostics_hook(safe_snapshot)
        except Exception:
            pass

    def wait_for_products_ready(
        self,
        index: int = 0,
        *,
        timeout_ms: int = COLLECTION_PRODUCTS_TIMEOUT_MS,
        poll_interval_ms: int = COLLECTION_PRODUCTS_POLL_MS,
        stable_snapshots: int = COLLECTION_PRODUCTS_STABLE_SNAPSHOTS,
        diagnostics_hook: Optional[Callable[[dict], None]] = None,
    ) -> dict:
        """Wait for one requested product link to be stably actionable.

        Every poll re-resolves Playwright Locators against the live DOM.  The
        total number of polls and wall-clock budget are bounded; the method
        never reloads the page or retries the surrounding business case.
        """
        if index < 0:
            raise IndexError(f"Product index must be non-negative: {index}")
        if timeout_ms < 0:
            raise ValueError("timeout_ms must be non-negative")
        if poll_interval_ms <= 0:
            raise ValueError("poll_interval_ms must be positive")
        if stable_snapshots <= 0:
            raise ValueError("stable_snapshots must be positive")

        started = time.monotonic()
        deadline = started + timeout_ms / 1000
        max_attempts = max(1, math.ceil(timeout_ms / poll_interval_ms) + 1)
        consecutive_ready = 0
        snapshot: dict = {}
        attempts = 0

        for attempts in range(1, max_attempts + 1):
            snapshot = self._products_readiness_snapshot(index)
            if self._products_snapshot_ready(snapshot):
                consecutive_ready += 1
            else:
                consecutive_ready = 0

            elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
            self._emit_products_readiness_diagnostic(
                snapshot,
                elapsed_ms=elapsed_ms,
                poll_attempts=attempts,
                consecutive_ready_snapshots=consecutive_ready,
                diagnostics_hook=diagnostics_hook,
            )
            if consecutive_ready >= stable_snapshots:
                result = dict(snapshot)
                result.update(
                    {
                        "elapsed_ms": elapsed_ms,
                        "poll_attempts": attempts,
                        "consecutive_ready_snapshots": consecutive_ready,
                    }
                )
                return result

            now = time.monotonic()
            if now >= deadline:
                break
            remaining_ms = max(1, int((deadline - now) * 1000))
            self.page.wait_for_timeout(min(poll_interval_ms, remaining_ms))

        elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
        raise CollectionProductsReadinessError(
            " ".join(
                [
                    f"url={snapshot.get('url', '')}",
                    f"readyState={snapshot.get('document_ready_state', 'UNKNOWN')}",
                    f"target_index={index}",
                    f"grid_count={snapshot.get('grid_count', 0)}",
                    f"grid_visible={snapshot.get('grid_visible', False)}",
                    f"product_card_count={snapshot.get('product_card_count', 0)}",
                    "product_card_visible_count="
                    f"{snapshot.get('product_card_visible_count', 0)}",
                    f"product_link_count={snapshot.get('product_link_count', 0)}",
                    "product_link_visible_count="
                    f"{snapshot.get('product_link_visible_count', 0)}",
                    f"target_href={snapshot.get('target_href', '')}",
                    f"target_href_valid={snapshot.get('target_href_valid', False)}",
                    f"loading_configured={snapshot.get('loading_configured', False)}",
                    f"loading_count={snapshot.get('loading_count', 0)}",
                    f"loading_visible_count={snapshot.get('loading_visible_count', 0)}",
                    f"elapsed_ms={elapsed_ms}",
                    f"poll_attempts={attempts}",
                    f"consecutive_ready_snapshots={consecutive_ready}",
                ]
            )
        )

    def filter_control(self):
        """返回筛选区域定位器（inline panel）。"""
        return self.locator("filter").first

    def sort_control(self):
        """返回排序下拉控件定位器（select#SortBy）。"""
        return self.locator("sort").first

    @staticmethod
    def _canonical_product_href(href: str) -> str:
        """Normalize a Shopify product link while preserving query/fragment."""
        raw_href = str(href or "").strip()
        parsed = urlparse(raw_href)
        marker = "/products/"
        product_start = parsed.path.find(marker)
        if product_start < 0 or product_start + len(marker) >= len(parsed.path):
            raise RuntimeError("Product card link is not a product URL")
        canonical_path = parsed.path[product_start:]
        return parsed._replace(path=canonical_path).geturl()

    def open_product(self, index: int = 0) -> str:
        """打开第 index 张商品卡并返回最终 URL。"""
        self.wait_for_products_ready(index)
        cards = self.visible_product_cards()
        total = cards.count()
        if index < 0 or index >= total:
            raise IndexError(f"Product index out of range: {index} (product_count={total})")
        href = self.product_link(cards.nth(index)).get_attribute("href")
        if not href:
            raise RuntimeError(f"No product link found on card {index}")
        canonical_href = self._canonical_product_href(href)
        parsed = urlparse(canonical_href)
        if parsed.scheme:
            url = canonical_href
        else:
            url = urljoin(f"{self.base_url().rstrip('/')}/", canonical_href)
        self.page.goto(url, wait_until="domcontentloaded", timeout=PAGE_NAV_TIMEOUT_MS)
        return self.page.url

    # ------------------------------------------------------ 商品快照
    @staticmethod
    def _clean_handle(href: Optional[str]) -> str:
        """将商品卡链接归一化为路径（去掉 _pos/_fid 等查询参数）。"""
        if not href:
            return ""
        return urlparse(href).path

    def product_identifiers(self, n: int = 10) -> List[str]:
        """返回当前顺序下前 N 个商品 handle（仅商品卡链接）。"""
        handles = []
        cards = self.product_cards()
        for i in range(min(n, cards.count())):
            href = self.product_link(cards.nth(i)).get_attribute("href")
            clean = self._clean_handle(href)
            if clean and clean not in handles:
                handles.append(clean)
        return handles

    def product_titles(self, n: int = 10) -> List[str]:
        """返回前 N 张商品卡的用户可见标题（空白归一化）。"""
        titles = []
        cards = self.product_cards()
        for i in range(min(n, cards.count())):
            loc = self.product_title(cards.nth(i))
            if loc.count():
                titles.append(" ".join(loc.inner_text().split()))
        return titles

    def product_order_ascending(self, n: int = 10) -> bool:
        """前 N 个可见标题是否按 A→Z 排序（casefold + 空白归一化）。"""
        titles = [t.casefold() for t in self.product_titles(n) if t]
        return all(titles[i] <= titles[i + 1] for i in range(len(titles) - 1))

    # -------------------------------------------------------- 颜色快捷筛选
    def color_family_options(self):
        """返回颜色族条目定位器集合。"""
        return self.locator("color_family_options")

    def color_families(self) -> List[str]:
        """从 family-<Name> class token 解析颜色族名（排除状态类）。"""
        names = []
        options = self.color_family_options()
        for i in range(options.count()):
            cls = options.nth(i).get_attribute("class") or ""
            for token in cls.split():
                if token.startswith("family-"):
                    name = token[len("family-"):]
                    if name and name not in names:
                        names.append(name)
        return names

    def color_family_names(self) -> List[str]:
        """返回颜色族的显示名称（.color-name 文本，去重）。"""
        names = []
        loc = self.locator("color_family_name")
        for i in range(loc.count()):
            text = " ".join(loc.nth(i).inner_text().split())
            if text and text not in names:
                names.append(text)
        return names

    def color_options(self):
        """具体颜色链接（展开后可见）。

        主题会把展开后的选项渲染进一个带 `appended` class 的新容器，
        而模板容器保持 hidden，因此 a.option_circle 会匹配两份——
        始终过滤为可见的那份。
        """
        return self.locator("color_options").filter(visible=True)

    def color_family_mode(self, name: str) -> str:
        """识别颜色族的真实交互模式。

        基于 DOM 结构判断，不按颜色名称硬编码：
        - EXPANDABLE：存在二级颜色 box 且 box 内有具体颜色选项，需展开后点击。
        - DIRECT：无二级选项，family 自身（内部链接）就是最终筛选入口。
        - UNKNOWN：结构无法识别。
        """
        fam = self.page.locator(f".color-family-item.family-{name}").first
        if fam.count() == 0:
            return "UNKNOWN"
        box_opts = fam.locator(".color-list-box a.option_circle").count()
        direct_link = fam.locator('a[href*="filter.v.option.color"]').count()
        if box_opts > 0:
            return "EXPANDABLE"
        if direct_link > 0:
            return "DIRECT"
        return "UNKNOWN"

    def get_selected_color_filter(self) -> Optional[str]:
        """从 URL query 读取当前 filter.v.option.color 值。"""
        values = parse_qs(urlparse(self.page.url).query).get("filter.v.option.color")
        return values[0] if values else None

    def expand_color_family(self, value: str) -> None:
        """展开颜色族以显示二级具体颜色（仅适用于 EXPANDABLE 模式）。

        主题的 pointerenter 展开事件并非每次 hover 都会触发，
        因此重试相同真实用户手势，直到观察到 family-active 状态。
        """
        mode = self.color_family_mode(value)
        if mode != "EXPANDABLE":
            raise ValueError(
                f"Color family '{value}' is {mode} and does not require expansion."
            )
        fam = self.page.locator(f".color-family-item.family-{value}").first
        if fam.count() == 0:
            raise ValueError(f"Color family not found: {value}")
        for _attempt in range(3):
            if self.viewport == "mobile":
                fam.click()
            else:
                fam.hover()
            try:
                self.page.wait_for_function(
                    """(v) => {
                        const f = [...document.querySelectorAll('.color-family-item')]
                            .find(e => e.classList.contains('family-' + v));
                        if (!f) return false;
                        return f.classList.contains('family-active')
                            || [...f.querySelectorAll('a.option_circle')].some(a => a.offsetParent !== null);
                    }""",
                    arg=value,
                    timeout=5000,
                )
                return
            except Exception:
                continue
        raise RuntimeError(f"Color family could not be expanded: {value}")

    def _wait_collection_stable(self) -> None:
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=30000)
        except Exception:
            pass
        self.product_cards().first.wait_for(state="visible", timeout=15000)

    @staticmethod
    def classify_color_behavior(
        before_url: str, before_ids: List[str], after_url: str, after_ids: List[str]
    ) -> str:
        """分类点击颜色后的行为：SAME_COLLECTION_FILTER | COLOR_LANDING_PAGE_NAVIGATION | NO_EFFECT | ERROR。"""
        before_path = urlparse(before_url).path
        after = urlparse(after_url)
        if after.path != before_path:
            return "COLOR_LANDING_PAGE_NAVIGATION"
        if "filter.v.option.color" in parse_qs(after.query):
            return "SAME_COLLECTION_FILTER"
        if after_url == before_url and after_ids == before_ids:
            return "NO_EFFECT"
        return "SAME_COLLECTION_FILTER"

    def apply_color_filter(self, value: str) -> dict:
        """按颜色名称执行 PLP 颜色筛选（统一入口）。

        自动识别颜色族模式并走真实交互路径：
        - EXPANDABLE：展开 family，点击二级具体颜色。
        - DIRECT：直接点击 family 自身的筛选入口。

        返回结构化结果：{family, mode, selected_option, behavior}。
        """
        before_url = self.page.url
        before_ids = self.product_identifiers(10)
        mode = self.color_family_mode(value)
        selected = value

        if mode == "EXPANDABLE":
            self.expand_color_family(value)
            encoded = value.replace(" ", "+")
            opt = self.page.locator(
                f'a.option_circle[href*="filter.v.option.color={encoded}"]'
            ).filter(visible=True).first
            if opt.count() == 0:
                opt = self.color_options().first  # 回退：展开后的第一个可见具体颜色
            if opt.count() == 0:
                return {"family": value, "mode": mode, "selected_option": None, "behavior": "NO_EFFECT"}
            selected = self._option_value(opt)
            opt.click()
        elif mode == "DIRECT":
            target = self.page.locator(
                f".color-family-item.family-{value} .family-item-content"
            ).first
            if target.count() == 0:
                return {"family": value, "mode": mode, "selected_option": None, "behavior": "NO_EFFECT"}
            target.click()
        else:
            return {"family": value, "mode": mode, "selected_option": None, "behavior": "NO_EFFECT"}

        # 链接导航在 WebKit 上可能是异步的：先等 URL 变化，再等页面稳定，
        # 避免读取到旧页面数据。
        try:
            self.page.wait_for_function(
                "(u) => location.href !== u", arg=before_url, timeout=15000
            )
        except Exception:
            pass
        self._wait_collection_stable()
        after_url = self.page.url
        after_ids = self.product_identifiers(10)
        behavior = self.classify_color_behavior(before_url, before_ids, after_url, after_ids)
        return {"family": value, "mode": mode, "selected_option": selected, "behavior": behavior}

    @staticmethod
    def _option_value(opt) -> Optional[str]:
        """从可见具体颜色链接提取颜色名（class 内 option_circle <Name> token）。"""
        cls = opt.get_attribute("class") or ""
        for token in cls.split():
            if token != "option_circle" and not token.startswith("color_type"):
                return token
        return None

    def selected_state_mechanisms(self, value: str) -> dict:
        """检测某个颜色族所有可观察的选中状态信号。"""
        fam = self.page.locator(f".color-family-item.family-{value}").first
        return {
            "family_class": fam.get_attribute("class") if fam.count() else None,
            "family_active": bool(
                fam.count() and "family-active" in (fam.get_attribute("class") or "")
            ),
            "selected_box_visible": bool(
                fam.count()
                and fam.locator(".selected-color-box").count()
                and fam.locator(".selected-color-box").first.is_visible()
            ),
            "aria_selected_options": self.page.locator(
                "a.option_circle[aria-selected='true'], a.option_circle[aria-pressed='true']"
            ).count(),
        }

    # ------------------------------------------------------------------ 排序
    def sort_options(self) -> List[dict]:
        """返回排序下拉的全部选项（label + value），跳过占位项。"""
        opts = []
        loc = self.sort_control().locator("option")
        for i in range(loc.count()):
            o = loc.nth(i)
            label = " ".join(o.inner_text().split())
            if label and label.lower() != "sort":  # 跳过占位选项
                opts.append({"label": label, "value": o.get_attribute("value")})
        return opts

    def get_current_sort(self) -> str:
        """返回排序下拉当前选中值。"""
        return self.sort_control().input_value()

    def select_sort(self, value: str) -> None:
        """选择排序项并等待真实生效。

        表单提交式导航在 WebKit 上是异步的：先等 URL 出现
        sort_by=<value>，再等页面稳定，避免读到旧页面数据。
        """
        self.sort_control().select_option(value)
        self.page.wait_for_function(
            "(v) => location.search.includes('sort_by=' + v)",
            arg=value,
            timeout=15000,
        )
        self._wait_collection_stable()
