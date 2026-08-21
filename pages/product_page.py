"""商品详情（PDP）页面对象。

颜色 / 尺码选择基于真实表单状态（radio.checked）判定，
不使用 CSS class 启发式。提供加购按钮定位与真实加购点击。
"""

from __future__ import annotations

from typing import Optional

from playwright.sync_api import expect

from pages.base_page import BasePage

FREE_SIZE_MARKER = "free custom size"


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

    # ------------------------------------------------------------------- 选项
    def color_group(self):
        """返回颜色选项组定位器（fieldset[name=Color]）。"""
        return self.locator("color").first

    def color_options(self):
        """返回颜色 radio 选项定位器集合。"""
        return self.color_group().locator('input[type="radio"]')

    def size_options(self):
        """返回尺码 radio 选项定位器集合（SPB 控件）。"""
        return self.locator("size")

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
        """返回当前可见、可用且有值的尺码选项数。"""
        return len(self.available_options(self.size_options()))

    def wait_purchase_ready(self, timeout_ms: int = 15_000) -> tuple[int, int, bool]:
        """等待购买区核心控件就绪并返回颜色数、尺码数、ATC 状态。

        页面异步初始化逻辑由 POM 持有；Tests 层只编排业务 Case，不自行
        轮询 DOM。Playwright expect 会在有界 timeout 内等待 DOM 状态。
        """
        expect(self.title()).to_be_visible(timeout=timeout_ms)
        atc = self.add_to_cart_button()
        expect(atc).to_be_visible(timeout=timeout_ms)
        expect(atc).to_be_enabled(timeout=timeout_ms)
        expect(self.color_options().first).to_be_visible(timeout=timeout_ms)
        expect(self.size_options().first).to_be_visible(timeout=timeout_ms)
        return (
            self.available_color_count(),
            self.available_size_count(),
            bool(atc.is_visible() and atc.is_enabled()),
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
        for v, radio in self.available_options(self.size_options()):
            if v.lower() == FREE_SIZE_MARKER:
                continue
            if not radio.is_checked():
                return v
        raise RuntimeError("No available normal size option to select")

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
        radio = self._find_option(
            self.size_options(), value, f"Size option unavailable: {value}"
        )
        radio.check()
        return value

    def get_selected_color(self) -> Optional[str]:
        """返回当前选中的颜色值（基于 radio.checked 真实表单状态）。"""
        for i in range(self.color_options().count()):
            radio = self.color_options().nth(i)
            if radio.is_checked():
                return self._radio_value(radio)
        return None

    def get_selected_size(self) -> Optional[str]:
        """返回当前选中的尺码值（基于 radio.checked 真实表单状态）。"""
        for i in range(self.size_options().count()):
            radio = self.size_options().nth(i)
            if radio.is_checked():
                return self._radio_value(radio)
        return None
