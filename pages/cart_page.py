"""购物车页面对象。

空购物车是合法状态：cart_items() / quantity_inputs() / remove_links()
返回空定位器集。本对象不包含购物车变更方法。
"""

from __future__ import annotations

from pages.base_page import BasePage
from utils.browser import PAGE_NAV_TIMEOUT_MS


class CartPage(BasePage):
    PAGE_NAME = "cart"

    def open(self) -> None:
        # 抽屉 DOM 常驻页面（默认隐藏，直到打开）
        self.page.goto(
            self.page_url(), wait_until="domcontentloaded", timeout=PAGE_NAV_TIMEOUT_MS
        )
        self.locator("drawer").first.wait_for(
            state="attached", timeout=PAGE_NAV_TIMEOUT_MS
        )

    def drawer(self):
        return self.locator("drawer").first

    def cart_items(self):
        return self.locator("cart_item")

    def quantity_inputs(self):
        return self.locator("quantity")

    def remove_links(self):
        return self.locator("remove")
