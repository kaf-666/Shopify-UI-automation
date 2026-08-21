"""购物车抽屉购物流程。

完整购物链以可复用步骤形式放在本模块：
ShoppingFlow.run()（完整链入口）与 Smoke Cases 调用同一批步骤方法。

流程：前置清理 -> PLP -> PDP -> 颜色/尺码 -> 记录 PDP 状态 ->
加购 -> 抽屉 -> 记录抽屉状态 -> 变体一致性 -> 移除 -> 空状态。

清理原则：UI Remove 是被测行为；/cart/clear.js 只用于前置与异常兜底。
"""

from __future__ import annotations

from typing import Optional

from pages.cart_drawer import CartDrawer
from pages.collection_page import CollectionPage
from pages.product_page import ProductPage
from utils.config import resolve_url
from utils.errors import sanitize_message

MAX_PRODUCT_CANDIDATES = 5


class FlowError(Exception):
    """购物流程失败，携带失败分类代码。"""

    def __init__(self, category: str, message: str):
        super().__init__(sanitize_message(message))
        self.category = category


def normalize(text) -> str:
    """去除首尾空白并折叠连续空白（之后做精确比较，不做模糊匹配）。"""
    return " ".join(str(text or "").split())


def titles_match(pdp_title, cart_title) -> bool:
    """站点展示差异规范化：PDP 标题带 "SKU MONDRESSY 商品名" 前缀，抽屉只显示商品名；要求抽屉商品名是 PDP 标题的精确后缀。"""
    p, c = normalize(pdp_title), normalize(cart_title)
    return p == c or p.endswith(c)


class ShoppingFlow:
    """购物车抽屉购物流程：以可复用步骤组织完整购物链，供 run() 与 Smoke Cases 共用。"""

    def __init__(self, page, site_config: dict, viewport: str, access_policy=None):
        self.page = page
        self.site_config = site_config
        self.viewport = viewport
        self.access_policy = access_policy
        self.base = resolve_url(site_config.get("base_url"), "site.base_url")
        self.state: dict = {}

    def _api_headers(self, url: str) -> dict:
        """为 APIRequestContext 调用提供 Signed Request 请求头（无策略时为空）。"""
        policy = self.access_policy
        return policy.request_headers(url) if policy is not None else {}

    def pre_clean_cart(self) -> None:
        """测试前置条件（不属于被测行为）。

        最小化 API 调用：先 GET /cart.js 检查，仅当购物车确实有商品时才 POST /cart/clear.js。
        """
        try:
            resp = self.page.request.get(
                f"{self.base}/cart.js",
                headers=self._api_headers(f"{self.base}/cart.js"),
                timeout=15000,
            )
        except Exception as exc:
            raise FlowError("CLEANUP_FAILURE", f"/cart.js check failed: {exc}") from exc
        if resp.status == 429:
            # 无法确认购物车状态时绝不能默认它是干净的。
            raise FlowError(
                "CART_PRECONDITION_UNVERIFIED",
                "/cart.js returned HTTP 429; cart state is unverified",
            )
        if resp.status != 200:
            raise FlowError("CLEANUP_FAILURE", f"/cart.js check returned HTTP {resp.status}")
        try:
            cart = resp.json()
        except Exception as exc:
            raise FlowError("CART_PRECONDITION_UNVERIFIED", "/cart.js response was not valid JSON") from exc
        item_count = int(cart.get("item_count", 0) or 0)
        self.state["pre_clean_item_count"] = item_count
        if item_count == 0:
            return  # 购物车已干净，零副作用
        self.page.wait_for_timeout(5000)  # 拉长 API 调用间隔
        resp = self.page.request.post(
            f"{self.base}/cart/clear.js",
            headers=self._api_headers(f"{self.base}/cart/clear.js"),
            timeout=15000,
        )
        if resp.status != 200:
            raise FlowError(
                "CLEANUP_FAILURE", f"pre-clean /cart/clear.js returned HTTP {resp.status}"
            )

    def cleanup_cart(self) -> None:
        """仅用于异常后的兜底清理；正常路径通过 UI Remove 完成。"""
        # 先验证状态，避免空购物车再次 POST /cart/clear.js。
        try:
            current = self._cart_json()
        except FlowError:
            raise
        if int(current.get("item_count", 0) or 0) == 0:
            return

        for attempt in range(2):
            try:
                self.page.wait_for_timeout(3000)
                resp = self.page.request.post(
                    f"{self.base}/cart/clear.js",
                    headers=self._api_headers(f"{self.base}/cart/clear.js"),
                    timeout=15000,
                )
                if resp.status == 200:
                    return
            except Exception:
                pass
            if attempt == 0:
                self.page.wait_for_timeout(10000)  # 429 限流窗口短暂等待后重试一次
        raise FlowError("CLEANUP_FAILURE", "/cart/clear.js failed twice during cleanup")

    def _cart_json(self) -> dict:
        self.page.wait_for_timeout(3000)  # 拉长 cart API 调用间隔
        resp = self.page.request.get(
            f"{self.base}/cart.js",
            headers=self._api_headers(f"{self.base}/cart.js"),
            timeout=15000,
        )
        if resp.status == 429:
            raise FlowError(
                "CART_PRECONDITION_UNVERIFIED",
                "/cart.js returned HTTP 429; cart state is unverified",
            )
        if resp.status != 200:
            raise FlowError(
                "CART_STATE_VERIFICATION_UNAVAILABLE",
                f"/cart.js returned HTTP {resp.status}",
            )
        try:
            return resp.json()
        except Exception as exc:
            raise FlowError("CART_STATE_VERIFICATION_UNAVAILABLE", "/cart.js response was not valid JSON") from exc

    def cart_state_quantity(self, index: int = 0) -> Optional[str]:
        """读取后端购物车第 index 个商品的数量（/cart.js 只读验证）。

        仅用于状态断言，不修改购物车。读取被 Access 层阻断（如 Cloudflare
        429）时抛出 CART_STATE_VERIFICATION_UNAVAILABLE，由 Case 区分
        验证基础设施问题与真实状态不一致。
        """
        try:
            data = self._cart_json()
        except FlowError as exc:
            raise FlowError(
                exc.category,
                f"/cart.js read failed: {exc}",
            ) from exc
        items = data.get("items") or []
        if index >= len(items):
            return None
        qty = items[index].get("quantity")
        if qty is None:
            raise FlowError(
                "CART_STATE_VERIFICATION_UNAVAILABLE",
                "cart item quantity is missing",
            )
        return str(qty)

    def _safe_open(self, page_obj) -> None:
        try:
            page_obj.open()
        except TimeoutError as exc:
            title = self.page.title()
            if "just a moment" in title.lower():
                raise FlowError(
                    "PAGE_LOAD_FAILURE",
                    f"Cloudflare challenge 页（title={title!r}），等待页面内容超时",
                ) from exc
            raise

    def _suitable(self, prod: ProductPage) -> bool:
        try:
            if not prod.get_title():
                return False
            if prod.color_options().count() <= 0:
                return False
            if prod.size_options().count() <= 0:
                return False
            atc = prod.add_to_cart_button()
            return atc.is_visible() and atc.is_enabled()
        except Exception:
            return False

    # ------------------------------------------------------------------ 步骤
    def open_collection(self) -> int:
        """打开目标商品列表页并返回商品数量。"""
        coll = CollectionPage(self.page, self.site_config, self.viewport)
        self._safe_open(coll)
        total = coll.product_count()
        if total <= 0:
            raise FlowError("PLP_PRODUCT_OPEN_FAILURE", f"product_count={total}")
        self.state["coll"] = coll
        self.state["product_count"] = total
        return total

    def open_product(self):
        """打开第一个适用商品（确定性选择，最多尝试 N 个候选）。"""
        coll = self.state["coll"]
        total = self.state["product_count"]
        for index in range(min(MAX_PRODUCT_CANDIDATES, total)):
            coll.open_product(index)
            candidate = ProductPage(self.page, self.site_config, self.viewport)
            if self._suitable(candidate):
                self.state["used_index"] = index
                self.state["prod"] = candidate
                return index, candidate
        raise FlowError("PRODUCT_NOT_SUITABLE", "前 N 个商品均不满足颜色/尺码/加购条件")

    def validate_purchase_area(self) -> dict:
        """验证购买区域可用（标题/价格/颜色/尺码/加购按钮）。"""
        prod = self.state["prod"]
        title = prod.get_title()
        price = prod.get_price()
        colors = prod.color_options().count()
        sizes = prod.size_options().count()
        atc = prod.add_to_cart_button()
        atc_visible = atc.is_visible()
        atc_enabled = atc.is_enabled()
        ok = (
            bool(title)
            and bool(price)
            and colors > 0
            and sizes > 0
            and atc_visible
            and atc_enabled
        )
        if not ok:
            raise FlowError(
                "PURCHASE_AREA_FAILURE",
                f"title={bool(title)} price={bool(price)} colors={colors} sizes={sizes} "
                f"atc_visible={atc_visible} atc_enabled={atc_enabled}",
            )
        info = {"colors": colors, "sizes": sizes, "atc_enabled": atc_enabled}
        self.state["purchase_area"] = info
        return info

    def select_color(self) -> str:
        """选择第一个可用颜色并返回其值。"""
        color = self.state["prod"].select_color()
        self.state["color"] = color
        return color

    def select_size(self) -> str:
        """选择第一个可用普通尺码并返回其值。"""
        size = self.state["prod"].select_size()
        self.state["size"] = size
        return size

    def capture_pdp_state(self) -> dict:
        """记录加购前的 PDP 状态（标题与 URL）。"""
        prod = self.state["prod"]
        state = {
            "pdp_title": prod.get_title(),
            "pdp_url": self.page.url,
        }
        self.state.update(state)
        return state

    def add_to_cart(self) -> None:
        """真实 UI 加购，等待实际的购物车变更响应。"""
        prod = self.state["prod"]
        try:
            with self.page.expect_response(
                lambda r: "/cart/add" in r.url and r.request.method == "POST",
                timeout=15_000,
            ) as add_resp_info:
                prod.add_to_cart()
            add_resp = add_resp_info.value
            if add_resp.status != 200:
                raise FlowError(
                    "ADD_TO_CART_FAILURE", f"/cart/add.js returned HTTP {add_resp.status}"
                )
        except FlowError:
            raise
        except Exception as exc:
            raise FlowError("ADD_TO_CART_FAILURE", f"{type(exc).__name__}: {exc}") from exc

    def capture_cart_state(self) -> dict:
        """等待抽屉打开与商品行渲染，然后读取抽屉内商品信息。"""
        drawer = CartDrawer(self.page, self.site_config, self.viewport)
        try:
            drawer.wait_open()
        except Exception as exc:
            raise FlowError("CART_DRAWER_NOT_OPENED", f"{type(exc).__name__}: {exc}") from exc
        try:
            drawer.wait_item()
        except Exception as exc:
            raise FlowError(
                "CART_ITEM_NOT_FOUND", f"抽屉已打开但商品行未渲染：{type(exc).__name__}"
            ) from exc
        item_count = drawer.item_count()
        if item_count < 1:
            raise FlowError("CART_ITEM_NOT_FOUND", f"drawer item_count={item_count}")
        state = {
            "item_count": item_count,
            "cart_title": drawer.get_item_title(0),
            "cart_color": drawer.get_item_color(0),
            "cart_size": drawer.get_item_size(0),
            "cart_qty": drawer.get_item_quantity(0),
        }
        self.state.update(state)
        self.state["drawer"] = drawer
        return state

    def validate_variant(self) -> dict:
        """校验 PDP 与抽屉的变体一致性（商品/颜色/尺码/数量）。"""
        checks = {
            "product": titles_match(self.state["pdp_title"], self.state["cart_title"]),
            "color": normalize(self.state["color"]) == normalize(self.state["cart_color"]),
            "size": normalize(self.state["size"]) == normalize(self.state["cart_size"]),
            "quantity": int(self.state["cart_qty"] or 0) == 1,
        }
        mismatch_map = {
            "product": "PRODUCT_MISMATCH",
            "color": "COLOR_MISMATCH",
            "size": "SIZE_MISMATCH",
            "quantity": "QUANTITY_MISMATCH",
        }
        pdp_values = {
            "product": self.state["pdp_title"],
            "color": self.state["color"],
            "size": self.state["size"],
            "quantity": self.state["cart_qty"],
        }
        cart_values = {
            "product": self.state["cart_title"],
            "color": self.state["cart_color"],
            "size": self.state["cart_size"],
            "quantity": self.state["cart_qty"],
        }
        for key, ok in checks.items():
            if not ok:
                raise FlowError(
                    mismatch_map[key],
                    f"{key} mismatch: PDP={pdp_values[key]} Cart={cart_values[key]}",
                )
        return checks

    def remove_and_wait_empty(self) -> None:
        """通过 UI 移除商品并等待抽屉空状态。"""
        drawer = self.state["drawer"]
        try:
            drawer.remove_item(0)
            drawer.wait_empty()
        except Exception as exc:
            raise FlowError("REMOVE_FAILURE", f"{type(exc).__name__}: {exc}") from exc
        if drawer.item_count() != 0:
            raise FlowError("EMPTY_STATE_FAILURE", f"item_count={drawer.item_count()}")

    def run(self) -> dict:
        """完整购物链：与 Smoke Cases 复用同一批步骤方法。"""
        self.pre_clean_cart()
        self.open_collection()
        self.open_product()
        self.validate_purchase_area()
        color = self.select_color()
        size = self.select_size()
        self.capture_pdp_state()
        self.add_to_cart()
        self.capture_cart_state()
        self.validate_variant()
        self.remove_and_wait_empty()
        return {
            "collection_url": self.state["coll"].page_url(),
            "pdp_url": self.state["pdp_url"],
            "product_title": self.state["pdp_title"],
            "selected_color": self.state["color"],
            "selected_size": self.state["size"],
            "cart_title": self.state["cart_title"],
            "cart_color": self.state["cart_color"],
            "cart_size": self.state["cart_size"],
            "cart_quantity": self.state["cart_qty"],
            "used_index": self.state["used_index"],
        }
