"""配置驱动的购物车抽屉页面对象。

封装抽屉状态、商品信息、数量读取、商品移除与空购物车状态判断。

业务断言由上层 Flow / Case Runner 负责，本模块仅提供页面操作能力。
"""

from __future__ import annotations

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
        return self.locator("drawer_open").count() > 0

    def wait_open(self, timeout: int = DRAWER_OPEN_TIMEOUT_MS) -> None:
        """等待配置的抽屉打开状态。"""
        expect(self.locator("drawer_open").first).to_be_visible(timeout=timeout)

    # ----------------------------------------------------------------- 商品项
    def cart_items(self):
        """返回抽屉内商品行定位器集合。"""
        item_sel = self.resolve_selector("cart_item")["value"]
        try:
            drawer = self.drawer()
            if drawer.count():
                return drawer.locator(item_sel)
        except Exception:
            # Minimal synthetic/legacy profiles may only declare cart_item;
            # retain the page-level lookup used by the original object.
            pass
        return self.page.locator(item_sel)

    def wait_item(self, timeout: int = 10_000) -> None:
        """等待抽屉内至少渲染出一个商品行。"""
        self.cart_items().first.wait_for(state="attached", timeout=timeout)

    def item_count(self) -> int:
        """返回抽屉内商品行数量。"""
        return self.cart_items().count()

    def _item(self, index: int = 0):
        items = self.cart_items()
        total = items.count()
        if index < 0 or index >= total:
            raise IndexError(f"Cart item index out of range: {index} (item_count={total})")
        return items.nth(index)

    def _item_locator(self, index: int, selector_name: str, fallback: str):
        """Resolve a configured item control relative to one cart row.

        The fallback keeps the public PageObject compatible with legacy site
        profiles while allowing each site to provide a stable selector-first
        contract for its own cart markup.
        """
        item = self._item(index)
        try:
            selector = self.resolve_selector(selector_name)["value"]
        except Exception:
            selector = fallback
        locator = item.locator(selector).first
        if locator.count() == 0 and selector != fallback:
            locator = item.locator(fallback).first
        return locator

    def get_item_title(self, index: int = 0) -> str:
        """返回第 index 个商品行的标题文本。"""
        item = self._item(index)
        name = self._item_locator(index, "item_title", ".cart__item-name")
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
        qty = self._item_locator(index, "quantity_input", 'input[name="updates[]"]')
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

    @staticmethod
    def _following_property_label(text: str):
        """Return the start of a property label suffix before the next colon.

        Shopify themes commonly render several ``Label: Value`` pairs in one
        text node. Known variant labels are matched exactly; a title-cased
        multi-word suffix also lets unrelated properties (for example,
        ``Rush Order``) terminate the preceding value.
        """
        import re

        value = text.rstrip()
        for label in ("Colour", "Color", "Size"):
            match = re.search(
                rf"(?<!\S)(?P<label>{re.escape(label)})\s*$",
                value,
                re.IGNORECASE,
            )
            if match:
                return match.start("label")

        match = re.search(
            r"(?<!\S)(?P<label>[A-Z][a-z][A-Za-z0-9'’&/.-]*(?:\s+[A-Z][a-z][A-Za-z0-9'’&/.-]*)*)\s*$",
            value,
        )
        return match.start("label") if match else None

    @classmethod
    def _extract_labeled_property(cls, text: str, key: str) -> str:
        """Extract one exact property from combined or separate label/value text.

        The first property must occupy the full text before its colon. Later
        properties are recognized as exact known-label suffixes immediately
        before their colon, so ``Custom Size`` and ``Color Family`` do not
        satisfy a request for ``Size`` or ``Color``.
        """
        import re

        aliases = {
            "color": {"color", "colour"},
            "colour": {"color", "colour"},
            "size": {"size"},
        }
        wanted = aliases.get(" ".join(str(key).split()).casefold(), {
            " ".join(str(key).split()).casefold()
        })
        parts = str(text or "").split(":")

        def matches(label: str) -> bool:
            return " ".join(label.split()).casefold() in wanted

        for index in range(len(parts) - 1):
            segment = parts[index]
            if index == 0:
                # At the beginning, compare the complete label, not a suffix
                # embedded in a longer name such as ``Custom Size``.
                if not matches(segment):
                    continue
            else:
                label_start = cls._following_property_label(segment)
                if label_start is None or not matches(segment[label_start:]):
                    continue

            value = parts[index + 1]
            if index + 2 < len(parts):
                following_label_start = cls._following_property_label(value)
                if following_label_start is not None:
                    value = value[:following_label_start]
            return " ".join(re.sub(r"\s+", " ", value).split())
        return ""

    def _variant_part(self, item, key: str) -> str:
        """从商品行的变体元数据容器读取变体行文本。"""
        try:
            selector = self.resolve_selector("variant_properties")["value"]
        except Exception:
            selector = ".cart__item--variants, .cart__item--properties"
        containers = item.locator(selector)
        if containers.count() == 0 and selector != ".cart__item--variants, .cart__item--properties":
            containers = item.locator(".cart__item--variants, .cart__item--properties")
        for i in range(containers.count()):
            text = containers.nth(i).inner_text()
            value = self._extract_labeled_property(text, key)
            if value:
                return value
        return ""

    def remove_item(self, index: int = 0) -> None:
        """通过真实 UI 点击第 index 个商品行的移除控件。"""
        remove = self._item_locator(
            index,
            "remove",
            ".cart__remove, .cart__remove-button, a[href*='/cart/change'], button[name='remove']",
        )
        if remove.count() == 0:
            raise RuntimeError(f"No remove control found on cart item {index}")
        remove.click()

    # ------------------------------------------------------------ Checkout
    def checkout_button(self):
        """返回标准 Checkout 按钮定位器（button[name=checkout]，排除快捷支付）。"""
        selector = self.resolve_selector("checkout_button")["value"]
        button = self.drawer().locator(selector).first
        if button.count() == 0:
            # Backward-compatible fallback for profiles that stored a fully
            # scoped selector such as ``#CartDrawer button[name=checkout]``.
            button = self.locator("checkout_button").first
        return button

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
