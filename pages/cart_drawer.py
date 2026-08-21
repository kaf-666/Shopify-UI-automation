"""购物车抽屉页面对象。

封装 Mondressy 购物车抽屉（#CartDrawer，Brooklyn 主题）的定位、
状态读取和基础 UI 操作，包括抽屉状态、商品信息、数量读取、
商品移除与空购物车状态判断。

业务断言由上层 Flow / Case Runner 负责，本模块仅提供页面操作能力。
"""

from __future__ import annotations

import re
import time
from typing import Optional
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect

from pages.base_page import BasePage

DRAWER_OPEN_TIMEOUT_MS = 15_000


class CartDrawer(BasePage):
    """购物车抽屉对象：打开状态、商品信息、数量、移除与空状态。"""

    PAGE_NAME = "cart"  # drawer/cart_item/quantity/remove 选择器位于 cart 页配置下

    # ------------------------------------------------------------ 打开状态
    def drawer(self):
        """返回抽屉容器定位器。"""
        return self.locator("drawer").first

    def is_open(self) -> bool:
        d = self.drawer()
        if d.count() == 0:
            return False
        # 关闭状态的抽屉通过 translateX(100%) 移出视口，is_visible() 会误报；
        # 主题通过 drawer--is-open class 标记真实打开状态，以此为准。
        return "drawer--is-open" in (d.get_attribute("class") or "")

    def wait_open(self, timeout: int = DRAWER_OPEN_TIMEOUT_MS) -> None:
        """等待抽屉真正打开（以 drawer--is-open class 为准）。"""
        expect(self.drawer()).to_have_class(re.compile(r"(?:^|\s)drawer--is-open(?:\s|$)"), timeout=timeout)

    # ----------------------------------------------------------------- 商品项
    def cart_items(self):
        """返回抽屉内商品行定位器集合。"""
        return self.locator("cart_item")

    def wait_item(self, timeout: int = 10_000) -> None:
        """等待抽屉内至少渲染出一个商品行。"""
        item_sel = self.resolve_selector("cart_item")["value"]
        self.page.wait_for_selector(f"#CartDrawer {item_sel}", timeout=timeout)

    def item_count(self) -> int:
        """返回抽屉内商品行数量。"""
        return self.cart_items().count()

    def _item(self, index: int = 0):
        items = self.cart_items()
        total = items.count()
        if index < 0 or index >= total:
            raise IndexError(f"Cart item index out of range: {index} (item_count={total})")
        return items.nth(index)

    def get_item_title(self, index: int = 0) -> str:
        """返回第 index 个商品行的标题文本。"""
        item = self._item(index)
        name = item.locator(".cart__item-name").first
        if name.count():
            return name.inner_text().strip()
        return item.locator("a[href*='/products/']").first.inner_text().strip()

    def get_item_color(self, index: int = 0) -> str:
        """返回第 index 个商品行的颜色值（如 "Color: Pink" → "Pink"）。"""
        return self._variant_part(self._item(index), "color")

    def get_item_size(self, index: int = 0) -> str:
        """返回第 index 个商品行的尺码值（如 "Size: 2" → "2"）。"""
        return self._variant_part(self._item(index), "size")

    def get_item_quantity(self, index: int = 0) -> str:
        """返回第 index 个商品行的实时数量 property。"""
        item = self._item(index)
        qty = item.locator('input[name="updates[]"]').first
        if qty.count() == 0:
            return ""
        return qty.input_value()

    def get_item_quantity_snapshot(self, index: int = 0) -> dict:
        """同时读取静态 value attribute 与实时 input value property。"""
        qty = self.quantity_input(index)
        if qty.count() == 0:
            return {"value_attribute": None, "input_value": ""}
        return {
            "value_attribute": qty.get_attribute("value"),
            "input_value": qty.input_value(),
        }

    def get_item_identity(self, index: int = 0) -> dict:
        """读取购物车行的脱敏身份线索，不记录标题或完整 URL。"""
        item = self._item(index)
        qty = self.quantity_input(index)
        identity = {"ui_index": index}
        for prefix, locator in (("row", item), ("input", qty)):
            for attr in (
                "id",
                "data-id",
                "data-key",
                "data-line-key",
                "data-cart-item-key",
                "data-variant-id",
            ):
                value = locator.get_attribute(attr)
                if value:
                    identity[f"{prefix}_{attr.replace('-', '_')}"] = value

        product_link = item.locator("a[href*='/products/']").first
        if product_link.count():
            href = product_link.get_attribute("href") or ""
            parsed = urlparse(href)
            if parsed.path:
                identity["product_path"] = parsed.path
            variant = (parse_qs(parsed.query).get("variant") or [None])[0]
            if variant:
                identity["product_url_variant_id"] = variant
        return identity

    @staticmethod
    def _parse_label_value(text: str) -> str:
        """解析 "Color: Pink" / "Size:\\n 2" → "Pink" / "2"（精确取值，不做模糊匹配）。"""
        import re

        m = re.match(r"^\s*([^:]+):\s*(.*)$", text, re.DOTALL)
        if not m:
            return ""
        return " ".join(m.group(2).split())

    def _variant_part(self, item, key: str) -> str:
        """从商品行的变体元数据容器读取变体行文本。"""
        containers = item.locator(".cart__item--variants, .cart__item--properties")
        for i in range(containers.count()):
            text = containers.nth(i).inner_text()
            if key.lower() in text.lower():
                return self._parse_label_value(text)
        return ""

    def remove_item(self, index: int = 0) -> None:
        """通过真实 UI 点击第 index 个商品行的移除控件。"""
        item = self._item(index)
        remove = item.locator(
            ".cart__remove, a[href*='/cart/change'], button[name='remove']"
        ).first
        if remove.count() == 0:
            raise RuntimeError(f"No remove control found on cart item {index}")
        remove.click()

    # ------------------------------------------------------------ Checkout
    def checkout_button(self):
        """返回标准 Checkout 按钮定位器（button[name=checkout]，排除快捷支付）。"""
        return self.locator("checkout_button").first

    def checkout(self) -> None:
        """通过抽屉内标准 Checkout 控件进入 Shopify Checkout（真实 UI 点击）。"""
        btn = self.checkout_button()
        if not (btn.count() and btn.is_visible() and btn.is_enabled()):
            raise RuntimeError("standard checkout button not available")
        btn.click()

    # ------------------------------------------------------------ 数量控件
    def quantity_input(self, index: int = 0):
        """返回第 index 个商品行的数量输入框定位器。"""
        sel = self.resolve_selector("quantity_input")["value"]
        return self._item(index).locator(sel).first

    def quantity_increase_button(self, index: int = 0):
        """返回第 index 个商品行的数量 + 按钮定位器。"""
        sel = self.resolve_selector("quantity_plus")["value"]
        return self._item(index).locator(sel).first

    def quantity_decrease_button(self, index: int = 0):
        """返回第 index 个商品行的数量 - 按钮定位器。"""
        sel = self.resolve_selector("quantity_minus")["value"]
        return self._item(index).locator(sel).first

    def increase_quantity(self, index: int = 0) -> None:
        """真实 UI 点击 + 并等待数量更新请求完成与 DOM 变化。"""
        btn = self.quantity_increase_button(index)
        if not (btn.count() and btn.is_visible() and btn.is_enabled()):
            raise RuntimeError("quantity increase button not available")
        before = self.get_item_quantity(index)
        self._click_quantity_control(btn, index, before)

    def decrease_quantity(self, index: int = 0) -> None:
        """真实 UI 点击 - 并等待数量更新请求完成与 DOM 变化。"""
        btn = self.quantity_decrease_button(index)
        if not (btn.count() and btn.is_visible()):
            raise RuntimeError("quantity decrease button not available")
        if btn.is_disabled():
            raise RuntimeError("quantity decrease button is disabled")
        before = self.get_item_quantity(index)
        self._click_quantity_control(btn, index, before)

    def _click_quantity_control(self, btn, index: int, before: str) -> None:
        """等待 change response，并记录不改变业务行为的数量时间线。"""
        started = time.perf_counter()
        diagnostic = {
            "_started_monotonic": started,
            "operation": "increase" if "plus" in (btn.get_attribute("class") or "") else "decrease",
            "ui_index": index,
            "ui_identity": self.get_item_identity(index),
            "events": [],
        }
        diagnostics = getattr(self, "_quantity_diagnostics", None)
        if diagnostics is None:
            diagnostics = []
            self._quantity_diagnostics = diagnostics
        diagnostics.append(diagnostic)

        before_handle = self.quantity_input(index).element_handle()

        def record(field: str) -> dict:
            snapshot = self.get_item_quantity_snapshot(index)
            diagnostic[field] = snapshot["input_value"]
            diagnostic[f"{field}_snapshot"] = snapshot
            diagnostic["events"].append(
                {
                    "event": field,
                    "elapsed_ms": int((time.perf_counter() - started) * 1000),
                    **snapshot,
                }
            )
            return snapshot

        def record_replacement(field: str) -> None:
            current_handle = self.quantity_input(index).element_handle()
            try:
                same_node = bool(
                    current_handle
                    and before_handle
                    and current_handle.evaluate("(node, before) => node === before", before_handle)
                )
            finally:
                if current_handle is not None:
                    current_handle.dispose()
            diagnostic[field] = not same_node

        record("quantity_before")
        try:
            with self.page.expect_response(
                lambda response: "/cart/change" in response.url
                and response.request.method == "POST",
                timeout=15_000,
            ) as response_info:
                btn.click()
                record("quantity_immediate_after_click")
                record_replacement("input_replaced_immediate_after_click")

            response = response_info.value
            diagnostic["cart_change_http_status"] = response.status
            request_data = {}
            try:
                request_data = response.request.post_data_json or {}
            except Exception:
                request_data = {}
            if isinstance(request_data, dict):
                for key in ("line", "quantity", "id"):
                    value = request_data.get(key)
                    if isinstance(value, (str, int, float)):
                        diagnostic[f"request_{key}"] = value

            try:
                payload = response.json()
            except Exception as exc:
                diagnostic["response_json_readable"] = False
                diagnostic["response_json_error"] = type(exc).__name__
                payload = {}
            else:
                diagnostic["response_json_readable"] = isinstance(payload, dict)

            if isinstance(payload, dict):
                response_items = payload.get("items") or []
                diagnostic["response_item_count"] = payload.get("item_count")
                diagnostic["response_items_length"] = len(response_items)
                line = request_data.get("line") if isinstance(request_data, dict) else None
                try:
                    target_index = int(line) - 1 if line is not None else index
                except (TypeError, ValueError):
                    target_index = index
                if 0 <= target_index < len(response_items):
                    target = response_items[target_index]
                    if isinstance(target, dict):
                        diagnostic["response_target_line"] = {
                            key: target.get(key) for key in ("key", "variant_id", "quantity")
                        }
                        diagnostic["response_target_line_quantity"] = target.get("quantity")

            record("quantity_after_change_response")
            record_replacement("input_replaced_after_change_response")
            if response.status != 200:
                raise RuntimeError(f"/cart/change.js returned HTTP {response.status}")
            self._wait_quantity_change(index, before)
            record("quantity_after_change_wait")
            record_replacement("input_replaced_after_change_wait")
        finally:
            if before_handle is not None:
                before_handle.dispose()

    def wait_quantity(self, index: int = 0, expected: int = 1, timeout_ms: int = 10_000) -> None:
        """等待第 index 个商品行数量 input 达到期望值。"""
        expect(self.quantity_input(index)).to_have_value(str(expected), timeout=timeout_ms)
        diagnostics = getattr(self, "_quantity_diagnostics", None) or []
        if diagnostics:
            diagnostic = diagnostics[-1]
            snapshot = self.get_item_quantity_snapshot(index)
            diagnostic["quantity_after_dom_wait"] = snapshot["input_value"]
            diagnostic["quantity_after_dom_wait_snapshot"] = snapshot
            diagnostic["events"].append(
                {
                    "event": "quantity_after_dom_wait",
                    "elapsed_ms": int(
                        (
                            time.perf_counter()
                            - diagnostic.get("_started_monotonic", time.perf_counter())
                        )
                        * 1000
                    ),
                    **snapshot,
                }
            )

    def quantity_diagnostics(self) -> list[dict]:
        """返回本 Drawer 会话中已采集的脱敏数量诊断。"""
        return [
            {key: value for key, value in diagnostic.items() if not key.startswith("_")}
            for diagnostic in (getattr(self, "_quantity_diagnostics", None) or [])
        ]

    def quantity_diagnostic_elapsed_ms(self) -> int:
        """返回最近一次数量操作开始后的毫秒数。"""
        diagnostics = getattr(self, "_quantity_diagnostics", None) or []
        if not diagnostics:
            return 0
        started = diagnostics[-1].get("_started_monotonic", time.perf_counter())
        return int((time.perf_counter() - started) * 1000)

    def record_cart_api_read(
        self,
        index: int,
        before_snapshot: dict,
        after_snapshot: dict,
        assertion_quantity_before: str,
        assertion_quantity_after: str,
        cart_js_result: dict,
        before_elapsed_ms: int,
        after_elapsed_ms: int,
    ) -> None:
        """将 /cart.js 的脱敏行状态关联到最近一次真实数量点击。"""
        diagnostics = getattr(self, "_quantity_diagnostics", None) or []
        if not diagnostics:
            return
        diagnostic = diagnostics[-1]
        diagnostic["quantity_before_cart_api_read"] = before_snapshot.get("input_value")
        diagnostic["quantity_before_cart_api_read_snapshot"] = before_snapshot
        diagnostic["quantity_after_cart_api_read"] = after_snapshot.get("input_value")
        diagnostic["quantity_after_cart_api_read_snapshot"] = after_snapshot
        diagnostic["assertion_quantity_before_cart_api_read"] = assertion_quantity_before
        diagnostic["assertion_quantity_after_cart_api_read"] = assertion_quantity_after
        diagnostic["backend_quantity"] = cart_js_result.get("target_line", {}).get("quantity")
        diagnostic["cart_js"] = cart_js_result

        diagnostic["events"].extend(
            [
                {
                    "event": "quantity_before_cart_api_read",
                    "elapsed_ms": before_elapsed_ms,
                    **before_snapshot,
                },
                {
                    "event": "quantity_after_cart_api_read",
                    "elapsed_ms": after_elapsed_ms,
                    **after_snapshot,
                },
            ]
        )

        response_line = diagnostic.get("response_target_line") or {}
        cart_line = cart_js_result.get("target_line") or {}
        try:
            request_line_matches_ui = int(diagnostic.get("request_line")) == index + 1
        except (TypeError, ValueError):
            request_line_matches_ui = False
        request_id = diagnostic.get("request_id")
        request_id_matches_response = bool(request_id) and str(request_id) in {
            str(response_line.get("key")),
            str(response_line.get("variant_id")),
        }
        key_matches = bool(response_line.get("key")) and str(response_line.get("key")) == str(
            cart_line.get("key")
        )
        variant_matches = bool(response_line.get("variant_id")) and str(
            response_line.get("variant_id")
        ) == str(cart_line.get("variant_id"))
        diagnostic["same_cart_line_verified"] = bool(
            (request_line_matches_ui or request_id_matches_response)
            and key_matches
            and variant_matches
        )
        diagnostic["same_cart_line_evidence"] = {
            "request_line_matches_ui_row": request_line_matches_ui,
            "request_id_matches_response_line": request_id_matches_response,
            "response_key_matches_cart_js": key_matches,
            "response_variant_id_matches_cart_js": variant_matches,
        }

    def _wait_quantity_change(self, index: int, before: str, timeout_ms: int = 10_000) -> None:
        """等待数量 input 值相对变化（数量更新由 AJAX 响应驱动）。"""
        expect(self.quantity_input(index)).not_to_have_value(before, timeout=timeout_ms)

    # ------------------------------------------------------------ Subtotal
    def subtotal(self):
        """返回抽屉底部 Subtotal 金额定位器（最后一个价格元素）。"""
        return self.locator("subtotal").last

    def get_subtotal(self) -> Optional[str]:
        """返回 Subtotal 金额文本（如 "$129.99"），不可读时返回 None。"""
        loc = self.subtotal()
        if not loc.count():
            return None
        try:
            return " ".join(loc.inner_text().split())
        except Exception:
            return None

    def is_empty(self) -> bool:
        """抽屉是否为空（无商品行或空状态可见）。"""
        if self.item_count() == 0:
            return True
        empty_el = self.drawer().locator(".drawer__cart-empty").first
        return empty_el.count() > 0 and empty_el.is_visible()

    def wait_empty(self, timeout: int = 10_000) -> None:
        """等待抽屉商品行全部消失。"""
        expect(self.cart_items()).to_have_count(0, timeout=timeout)
