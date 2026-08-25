"""商品详情（PDP）页面对象。

颜色 / 尺码选择基于真实表单状态（radio.checked）判定。尺码通过
SizeOptionResolver 先识别 Group/Model，再归一化 option；class 仅参与
MODEL_02 的不可用状态识别，不作为 selected state。提供加购按钮定位
与真实加购点击。
"""

from __future__ import annotations

import time
from typing import Optional

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import expect

from pages.base_page import BasePage
from pages.size_option_resolver import (
    DEFAULT_FREE_SIZE_MARKER,
    SizeGroupNotFoundError,
    SizeOptionResolver,
)

FREE_SIZE_MARKER = DEFAULT_FREE_SIZE_MARKER


class PurchaseAreaReadinessError(TimeoutError):
    """购买区未在 bounded timeout 内进入稳定可测试状态。"""


class ProductPage(BasePage):
    """商品详情页对象：标题/价格/图集读取、颜色与尺码选择、真实加购。"""

    PAGE_NAME = "product"

    def open(self) -> None:
        super().open(ready_selector="title")

    # ------------------------------------------------------------------- 读取
    def title(self):
        """返回商品标题定位器。"""
        return self.locator("title").first

    def get_title(self) -> str:
        """返回商品标题文本。"""
        return self.title().inner_text().strip()

    def price(self):
        """返回价格定位器。"""
        return self.locator("price").first

    def get_price(self) -> str:
        """返回价格文本。"""
        return self.price().inner_text().strip()

    def gallery(self):
        """返回图集定位器。"""
        return self.locator("gallery").first

    def purchase_area(self):
        """返回主商品购买表单根节点。"""
        return self.locator("purchase_area").first

    # ------------------------------------------------------------------- 选项
    def color_group(self):
        """返回颜色选项组定位器（fieldset[name=Color]）。"""
        return self.locator("color").first

    def color_options(self):
        """返回颜色 radio 选项定位器集合。"""
        return self.color_group().locator('input[type="radio"]')

    def _size_resolver(self) -> SizeOptionResolver:
        """创建无 DOM 缓存的实时尺码解析器。"""
        return SizeOptionResolver(self.page, self.page_config(), self.purchase_area)

    def add_to_cart_button(self):
        """返回加购按钮定位器。"""
        return self.locator("add_to_cart").first

    def add_to_cart(self) -> None:
        """通过真实 UI 点击加购按钮（本方法不做抽屉断言）。"""
        button = self.add_to_cart_button()
        if not button.is_visible():
            raise RuntimeError("Add To Cart button is not visible")
        if not button.is_enabled():
            raise RuntimeError("Add To Cart button is not enabled")
        button.click()

    @staticmethod
    def _radio_value(radio) -> str:
        """读取 radio 的选项值：优先 value 属性，空时回退关联 label / 父容器文本。"""
        v = radio.get_attribute("value")
        if v and v.strip():
            return v.strip()
        return (
            radio.evaluate(
                """el => {
                    const label = el.closest('label');
                    if (label && label.textContent) return label.textContent.trim();
                    const p = el.parentElement;
                    return p ? p.textContent.trim() : '';
                }"""
            )
            or ""
        ).strip()

    def available_options(self, options):
        """返回 [(值, radio)]：可见、可用且值非空的选项。"""
        result = []
        total = options.count()
        for i in range(total):
            radio = options.nth(i)
            try:
                if not radio.is_visible() or radio.is_disabled():
                    continue
            except Exception:
                continue
            value = self._radio_value(radio)
            if not value:
                continue
            result.append((value, radio))
        return result

    def available_color_count(self) -> int:
        """返回当前可见、可用且有值的颜色选项数。"""
        return len(self.available_options(self.color_options()))

    def available_size_count(self) -> int:
        """返回可用尺码数；兼容计入 Free Custom Size 的历史语义。"""
        try:
            return len(self._size_resolver().available_options())
        except SizeGroupNotFoundError:
            return 0

    @staticmethod
    def _safe_state(check, default=False):
        try:
            return check()
        except Exception:
            return default

    def _readiness_snapshot(self) -> dict:
        """每次用 Locator 重新解析当前 DOM，避免持有 hydration 前旧节点。"""
        root = self.purchase_area()
        title = self.title()
        atc = self.add_to_cart_button()
        size = self._safe_state(self._size_resolver().snapshot, {})
        return {
            "purchase_area_attached": bool(self._safe_state(root.count, 0)),
            "title_visible": bool(self._safe_state(title.is_visible)),
            "color_count": self._safe_state(self.available_color_count, 0),
            "size_count": int(size.get("size_option_available", 0)),
            "size_model": size.get("size_model"),
            "size_group_detected": bool(size.get("size_group_detected", False)),
            "size_option_total": int(size.get("size_option_total", 0)),
            "normal_size_available": int(size.get("normal_size_available", 0)),
            "custom_size_present": bool(size.get("custom_size_present", False)),
            "selected_size": size.get("selected_size"),
            "candidate_group_count": int(size.get("candidate_group_count", 0)),
            "atc_visible": bool(self._safe_state(atc.is_visible)),
            "atc_enabled": bool(self._safe_state(atc.is_enabled)),
        }

    @staticmethod
    def _snapshot_ready(snapshot: dict) -> bool:
        return all(
            (
                snapshot["purchase_area_attached"],
                snapshot["title_visible"],
                snapshot["color_count"] > 0,
                snapshot["size_count"] > 0,
                snapshot["atc_visible"],
                snapshot["atc_enabled"],
            )
        )

    def _wait_for_missing_readiness_condition(self, snapshot: dict, timeout_ms: int) -> None:
        if not snapshot["purchase_area_attached"]:
            self.purchase_area().wait_for(state="attached", timeout=timeout_ms)
        elif not snapshot["title_visible"]:
            self.title().wait_for(state="visible", timeout=timeout_ms)
        elif snapshot["color_count"] == 0:
            self.color_options().first.wait_for(state="visible", timeout=timeout_ms)
        elif snapshot["size_count"] == 0:
            self._size_resolver().wait_for_available(timeout_ms)
        elif not snapshot["atc_visible"]:
            self.add_to_cart_button().wait_for(state="visible", timeout=timeout_ms)
        elif not snapshot["atc_enabled"]:
            expect(self.add_to_cart_button()).to_be_enabled(timeout=timeout_ms)

    def wait_purchase_ready(self, timeout_ms: int = 15_000) -> tuple[int, int, bool]:
        """等待购买区业务条件在一个总 timeout 内同时成立。

        轮询基于 Locator 当前状态；Theme/SPB 替换表单 DOM 后，下一轮会
        自动解析新节点。无固定 sleep、reload 或无条件 retry。
        """
        deadline = time.monotonic() + timeout_ms / 1000
        initial = self._readiness_snapshot()
        final = initial
        while time.monotonic() < deadline:
            final = self._readiness_snapshot()
            if self._snapshot_ready(final):
                return final["color_count"], final["size_count"], True
            remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
            try:
                self._wait_for_missing_readiness_condition(final, remaining_ms)
            except PlaywrightTimeoutError:
                break

        final = self._readiness_snapshot()
        raise PurchaseAreaReadinessError(
            "purchase_area_attached="
            f"{final['purchase_area_attached']} "
            f"size_count_initial={initial['size_count']} "
            f"size_count_final={final['size_count']} "
            f"color_count_initial={initial['color_count']} "
            f"color_count_final={final['color_count']} "
            f"size_model={final['size_model'] or 'UNKNOWN'} "
            f"size_group_detected={final['size_group_detected']} "
            f"size_option_total={final['size_option_total']} "
            f"normal_size_available={final['normal_size_available']} "
            f"custom_size_present={final['custom_size_present']} "
            f"selected_size={final['selected_size'] or 'NONE'} "
            f"candidate_group_count={final['candidate_group_count']} "
            f"atc_visible={final['atc_visible']} "
            f"atc_enabled={final['atc_enabled']} "
            f"readiness_timeout_ms={timeout_ms}"
        )

    def _find_option(self, options, value: str, missing_msg: str):
        """按值查找可用选项，找不到抛出明确异常。"""
        for v, radio in self.available_options(options):
            if v == value:
                return radio
        raise LookupError(missing_msg)

    def first_available_color(self) -> str:
        """返回第一个可见可用且当前未选中的颜色选项值。"""
        for v, radio in self.available_options(self.color_options()):
            if not radio.is_checked():
                return v
        raise RuntimeError("No available color option to select")

    def first_available_size(self) -> str:
        """返回第一个可见可用的普通尺码值（排除 Free Custom Size）。"""
        try:
            return self._size_resolver().first_available_value()
        except SizeGroupNotFoundError as exc:
            raise RuntimeError("No available normal size option to select") from exc

    def select_color(self, value: Optional[str] = None) -> str:
        """选择颜色。

        未指定 value 时自动选择第一个可见可用且未选中的颜色。
        主题把点击绑定在包裹 radio 的 div.variant-input 容器上
        （radio 被覆盖拦截指针事件），因此走真实容器点击，
        再用 radio.checked 验证选择生效。
        """
        if value is None:
            value = self.first_available_color()
        radio = self._find_option(
            self.color_options(), value, f"Color option not found: {value}"
        )
        container = radio.evaluate_handle(
            """el => {
                let p = el.parentElement;
                while (p && !(p.classList && p.classList.contains('variant-input'))) {
                    p = p.parentElement;
                }
                return p || el.parentElement;
            }"""
        )
        container.click()
        if not radio.is_checked():
            raise RuntimeError(f"Color selection did not take effect: {value}")
        return value

    def select_size(self, value: Optional[str] = None) -> str:
        """选择一个可用的普通尺码。

        未指定 value 时自动选择第一个可见且可用的普通尺码，
        并排除 Free Custom Size。
        """
        if value is None:
            value = self.first_available_size()
        return self._size_resolver().select(value)

    def get_selected_color(self) -> Optional[str]:
        """返回当前选中的颜色值（基于 radio.checked 真实表单状态）。"""
        for i in range(self.color_options().count()):
            radio = self.color_options().nth(i)
            if radio.is_checked():
                return self._radio_value(radio)
        return None

    def get_selected_size(self) -> Optional[str]:
        """返回当前选中的尺码值（基于 radio.checked 真实表单状态）。"""
        try:
            return self._size_resolver().selected_value()
        except SizeGroupNotFoundError:
            return None
