"""首页页面对象（选择器驱动，无硬编码定位符）。"""

from __future__ import annotations

from pages.base_page import BasePage


class HomePage(BasePage):
    """首页页面对象：logo、搜索入口与购物车入口定位。"""
    PAGE_NAME = "home"

    def open(self) -> None:
        super().open(ready_selector="logo")

    def logo(self):
        return self.locator("logo").first

    def search_trigger(self):
        # 分端选择器由 BasePage 自动解析
        return self.locator("search").first

    def open_search(self) -> None:
        """点击当前端对应的 Search 入口（不等待打开状态）。"""
        trigger = self.search_trigger()
        if not trigger.is_visible():
            raise RuntimeError("Search trigger is not visible")
        trigger.click()

    def cart_trigger(self):
        return self.locator("cart").first
