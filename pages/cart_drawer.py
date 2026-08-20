"""购物车抽屉页面对象。

封装 Mondressy 购物车抽屉（#CartDrawer，Brooklyn 主题）的定位、
状态读取和基础 UI 操作，包括抽屉状态、商品信息、数量读取、
商品移除与空购物车状态判断。

业务断言由上层 Flow / Case Runner 负责，本模块仅提供页面操作能力。
"""

from __future__ import annotations

import time
from typing import Optional

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
        self.page.wait_for_function(
            """() => {
                const d = document.querySelector('#CartDrawer');
                return !!d && d.classList.contains('drawer--is-open');
            }""",
            timeout=timeout,
        )

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
        """返回第 index 个商品行的数量（input[name=updates[]] 的值）。"""
        item = self._item(index)
        qty = item.locator('input[name="updates[]"]').first
        if qty.count() == 0:
            return ""
        return qty.get_attribute("value") or qty.input_value()

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
        """真实 UI 点击 + 并等待数量 input 变化（后端响应后主题更新 DOM）。"""
        btn = self.quantity_increase_button(index)
        if not (btn.count() and btn.is_visible() and btn.is_enabled()):
            raise RuntimeError("quantity increase button not available")
        before = self.get_item_quantity(index)
        btn.click()
        self._wait_quantity_change(index, before)

    def decrease_quantity(self, index: int = 0) -> None:
        """真实 UI 点击 - 并等待数量 input 变化。"""
        btn = self.quantity_decrease_button(index)
        if not (btn.count() and btn.is_visible()):
            raise RuntimeError("quantity decrease button not available")
        if btn.is_disabled():
            raise RuntimeError("quantity decrease button is disabled")
        before = self.get_item_quantity(index)
        btn.click()
        self._wait_quantity_change(index, before)

    def wait_quantity(self, index: int = 0, expected: int = 1, timeout_ms: int = 10_000) -> None:
        """等待第 index 个商品行数量 input 达到期望值。"""
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if self.get_item_quantity(index) == str(expected):
                return
            self.page.wait_for_timeout(200)
        raise TimeoutError(
            f"cart quantity did not reach {expected} (current={self.get_item_quantity(index)!r})"
        )

    def _wait_quantity_change(self, index: int, before: str, timeout_ms: int = 10_000) -> None:
        """等待数量 input 值相对变化（数量更新由 AJAX 响应驱动）。"""
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if self.get_item_quantity(index) != before:
                return
            self.page.wait_for_timeout(200)
        raise TimeoutError(f"cart quantity did not change from {before!r}")

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
        self.page.wait_for_function(
            """() => {
                const d = document.querySelector('#CartDrawer');
                return !!d && d.querySelectorAll('.cart__item').length === 0;
            }""",
            timeout=timeout,
        )
