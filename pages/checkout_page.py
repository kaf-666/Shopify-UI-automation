"""Shopify Checkout 落地页对象（只读验证）。

Checkout 模型（真实探测）：REDIRECT_CHECKOUT（同标签页重定向）
    Cart Drawer 标准按钮 -> 同站 /checkouts/cn/<token>/en-us 会话 URL
    （Shopify hosted checkout，hostname 仍为 mondressy.com）。

安全边界（本对象唯一职责是验证页面可用，绝不产生订单）：
    只读取结构与存在性；不填写邮箱/地址/支付字段，
    不点击快捷支付 / 支付 / 提交订单类控件。

DOM 说明：Shopify Checkout 使用 hash class，不稳定；
    本对象只依赖稳定 id（main#checkout-main / h2#contact 等）与
    语义文本 / role=cell 单元格结构。
"""

from __future__ import annotations

import re
from typing import List, Optional
from urllib.parse import urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import expect

from pages.base_page import BasePage


class CheckoutPage(BasePage):
    """Checkout 落地页对象：上下文判定、核心 UI 存在性与商品摘要读取。"""

    PAGE_NAME = "checkout"

    # ------------------------------------------------------------------ 上下文
    def is_checkout(self) -> bool:
        """当前页面是否处于 Shopify Checkout 上下文（URL + 根容器双重确认）。"""
        url = self.page.url
        url_ok = "/checkout" in url or "checkout" in urlparse(url).hostname
        return url_ok and bool(self.locator("root").first.count())

    def current_url(self) -> str:
        return self.page.url

    def checkout_root(self):
        """返回 Checkout 根容器定位器（main#checkout-main）。"""
        return self.locator("root").first

    # ------------------------------------------------------------------ 核心 UI
    def contact_section(self):
        """返回 Contact 区块标题定位器（h2#contact）。"""
        return self.locator("contact").first

    def delivery_section(self):
        """返回 Delivery 区块标题定位器（h2#deliveryAddress）。"""
        return self.locator("delivery").first

    def shipping_form(self):
        """返回配送地址表单定位器（#shippingAddressForm）。"""
        return self.locator("shipping_form").first

    def email_input(self):
        """返回邮箱输入定位器（input#email；仅存在性验证）。"""
        return self.locator("email").first

    def express_checkout(self):
        """返回快捷支付区块标题定位器（#express-checkout-heading；仅观察）。"""
        return self.locator("express").first

    def core_sections_present(self) -> dict:
        """返回核心区块存在性：{root, contact, delivery, email}。"""
        return {
            "root": bool(self.checkout_root().count()),
            "contact": bool(self.contact_section().count()),
            "delivery": bool(self.delivery_section().count()),
            "email": bool(self.email_input().count()),
        }

    # ------------------------------------------------------------ Order Summary
    def _summary_section(self):
        """返回 Order Summary 区域定位器。

        Shopify Checkout 布局中 Order Summary 位于 main 之外
        （footer / 侧栏），按 "Order summary" 语义标题的祖先 section 定位；
        找不到时回退为文档级 role=cell 检索。
        """
        heading = self.page.get_by_role("heading", name="Order summary").first
        if heading.count():
            sec = heading.locator("xpath=ancestor::section[1]")
            if sec.count():
                return sec
        return None

    def _cells(self):
        """Order Summary 商品行语义单元格（role=cell，非 hash class）。

        Shopify 移动端 Checkout 会渲染两份 Order Summary（折叠抽屉版 +
        侧栏版，一份隐藏），因此按文档级 role=cell 定位，
        可见性判断使用"任一可见"，读取不依赖可见性。
        """
        return self.page.locator('[role="cell"]')

    def cells_visible(self) -> bool:
        """Order Summary 单元格是否任一可见（移动端展开后可见）。"""
        cells = self._cells()
        for i in range(min(cells.count(), 16)):
            try:
                if cells.nth(i).is_visible():
                    return True
            except Exception:
                continue
        return False

    def summary_toggle(self):
        """Order Summary 折叠开关（移动端；可见且文本含 "Order summary"）。"""
        return self.page.locator(
            'button, [role="button"]'
        ).filter(has_text="Order summary").first

    def wait_checkout_settled(self, timeout_ms: int = 20_000) -> None:
        """等待 Checkout 页面进入可交互状态（Contact 区块可见）。

        Shopify Checkout 为 React 应用：URL/根容器先出现，
        表单与折叠控件随后渲染；过早交互会被渲染窗口吞掉。
        """
        expect(self.contact_section()).to_be_visible(timeout=timeout_ms)

    def ensure_order_summary_visible(self) -> None:
        """必要时通过真实 UI 展开 Order Summary（移动端默认折叠）。

        React 渲染窗口可能吞掉首次点击（状态被重置），
        有界重试同一真实手势（≤2 次），以单元格可见为准。
        """
        if self.cells_visible():
            return
        toggle = self.summary_toggle()
        if not (toggle.count() and toggle.is_visible()):
            raise RuntimeError("order summary not visible and no toggle found")
        for _attempt in range(2):
            toggle.click()
            try:
                expect(self._cells().filter(visible=True).first).to_be_visible(timeout=6_000)
                return
            except PlaywrightTimeoutError:
                pass
        raise TimeoutError("order summary did not expand after toggle click")

    def order_summary_text(self) -> str:
        """Order Summary 区域全文（归一化空白；用于存在性与价格观察）。"""
        sec = self._summary_section()
        if sec is not None:
            return " ".join(sec.inner_text().split())
        return " ".join(self.page.locator("body").inner_text().split())

    def has_order_summary(self) -> bool:
        return "Order summary" in self.order_summary_text()

    # ------------------------------------------------------------ 商品摘要
    def _description_cell(self, index: int = 0):
        """返回第 index 个商品描述单元格（文本最长的 role=cell）。

        描述单元格包含标题 + 变体文本，长度显著大于数量 / 价格单元格。
        """
        cells = self._cells()
        best = None
        best_len = -1
        for i in range(cells.count()):
            try:
                text = " ".join(cells.nth(i).inner_text().split())
            except Exception:
                continue
            if len(text) > best_len:
                best_len = len(text)
                best = cells.nth(i)
        return best

    def get_product_title(self, index: int = 0) -> str:
        """返回商品标题（描述单元格内第一个 p 的文本）。"""
        cell = self._description_cell(index)
        if cell is None:
            return ""
        p = cell.locator("p").first
        return " ".join(p.inner_text().split()) if p.count() else ""

    def product_count_readable(self) -> int:
        """返回可读取的商品行数（文本较长的描述单元格数量）。

        数量单元格 / 价格单元格文本短，描述单元格（标题+变体）显著更长，
        用于无显式数量时推断单件商品状态。
        """
        count = 0
        cells = self._cells()
        for i in range(cells.count()):
            try:
                text = " ".join(cells.nth(i).inner_text().split())
            except Exception:
                continue
            if len(text) > 40:
                count += 1
        return count

    def get_product_variant(self, index: int = 0) -> str:
        """返回变体文本：描述单元格除去标题后的剩余文本（如 "Black Size: 2"）。"""
        cell = self._description_cell(index)
        if cell is None:
            return ""
        full = " ".join(cell.inner_text().split())
        title = self.get_product_title(index)
        if title and full.startswith(title):
            return full[len(title):].strip()
        return full

    def get_product_quantity(self, index: int = 0) -> str:
        """返回商品数量：文本以 "Quantity" 标签开头的单元格的值；不可读返回空串。"""
        cells = self._cells()
        for i in range(cells.count()):
            try:
                text = " ".join(cells.nth(i).inner_text().split())
            except Exception:
                continue
            if text.startswith("Quantity"):
                return text[len("Quantity"):].strip()
        return ""

    def get_product_price(self, index: int = 0) -> str:
        """返回商品价格（观察用途）：单元格文本以 $ 开头。"""
        cells = self._cells()
        for i in range(cells.count()):
            try:
                text = " ".join(cells.nth(i).inner_text().split())
            except Exception:
                continue
            if text.startswith("$"):
                return text
        return ""

    def get_subtotal(self) -> str:
        """返回 Cost Summary 中的 Subtotal 金额（观察用途），不可读返回空串。"""
        text = self.order_summary_text()
        m = re.search(r"Subtotal\s*(\$[\d.,]+)", text)
        return m.group(1) if m else ""

    # ------------------------------------------------------------------ 等待
    def wait_checkout_context(self, timeout_ms: int = 25_000) -> None:
        """等待进入 Checkout 上下文：URL 含 /checkout 且根容器出现。"""
        try:
            self.page.wait_for_url(lambda url: "/checkout" in str(url), timeout=timeout_ms)
            expect(self.checkout_root()).to_be_attached(timeout=timeout_ms)
        except PlaywrightTimeoutError as exc:
            raise TimeoutError(
                f"checkout context not reached (url={self.page.url[:120]})"
            ) from exc
