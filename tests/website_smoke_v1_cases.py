"""Website Smoke V1 Cases：三条真实用户购买 Journey 的编排 Smoke。

稳定 Case ID（本阶段冻结，作为日常执行接口）：

    Journey Direct（深链购买，执行顺序第一，保持 Direct PDP = 第一业务 UI 页面）：
        WSMOKE-DIRECT-01  Direct PDP Landing
        WSMOKE-DIRECT-02  Direct PDP Variant + Add To Cart
        WSMOKE-DIRECT-03  Direct PDP Cart Consistency

    Journey Search（搜索购买）：
        WSMOKE-SEARCH-01  Search Entry Available
        WSMOKE-SEARCH-02  Search Returns Products
        WSMOKE-SEARCH-03  Search Result Opens PDP
        WSMOKE-SEARCH-04  Search Product Purchase

    Journey Browse（主购买链：Home -> Navigation -> PLP -> PDP -> Cart -> Checkout）：
        WSMOKE-HOME-01    Homepage Core Available
        WSMOKE-NAV-01     Navigation Opens Collection
        WSMOKE-PLP-01     Collection Product Opens PDP
        WSMOKE-PDP-01     Purchase Variant Available
        WSMOKE-CART-01    Add To Cart + Drawer
        WSMOKE-CART-02    Cart Variant Consistency
        WSMOKE-CART-03    Cart Quantity Update
        WSMOKE-CHECKOUT-01  Checkout Entry + Core + Consistency

编排原则：
    - 每 viewport 一个 BrowserContext，顺序执行 Direct -> Search -> Browse；
      减少会话数量与访问压力，Browse 最后进入 Checkout 后 Context 直接销毁。
    - Journey 之间相互独立：任一 Journey FAIL 不 BLOCK 其他 Journey；
      仅 Journey 内部 Case 使用依赖链。
    - 全部复用冻结 Page Object / ShoppingFlow；本文件只做编排与业务断言。
    - 来源能力（供追溯）：DIRECT-PDP-01..04 / SEARCH-01..04 / HOME-01 /
      NAV-01..04 / SMOKE-PLP/PDP/CART / CART-QTY-02..04 / CHECKOUT-02..04。
    - Checkout 安全边界不变：不填表、不点快捷支付、不下单。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import urlparse

from flows.shopping_flow import FlowError, ShoppingFlow, normalize, titles_match
from pages.cart_drawer import CartDrawer
from pages.checkout_page import CheckoutPage
from pages.collection_page import CollectionPage
from pages.home_page import HomePage
from pages.navigation import NavigationPage
from pages.product_page import ProductPage
from pages.search_page import SearchPage
from utils.result import CaseResult, iso_now
from utils.screenshots import capture_case_failure

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_BLOCKED = "BLOCKED"

POSITIVE_QUERY = "dress"

CF_CHALLENGE_MARKERS = ("just a moment", "cf-challenge", "attention required")


class WebsiteSmokeV1Runner:
    """针对单个 BrowserRuntime 顺序执行三条购买 Journey（15 Cases）。"""

    def __init__(self, runtime, site_config: dict, viewport: str, artifact_dir: Optional[Path] = None):
        self.runtime = runtime
        self.viewport = viewport
        self.artifact_dir = Path(artifact_dir) if artifact_dir else None
        self.page = runtime.page
        self.site_config = site_config
        self.flow = ShoppingFlow(
            runtime.page, site_config, viewport, access_policy=runtime.access_policy
        )
        self.state: dict = {}
        self.results: Dict[str, CaseResult] = {}
        self.pre_clean_status = "PASS"
        self.pre_clean_error = ""
        self.cleanup_status = "PASS"
        self.cleanup_error = ""
        self.cleanup_detail = "context_disposed_after_checkout"
        self.search_recovery_used = False
        self.cf_interruption = False
        self._extra_pages: List = []
        self._journey = ""

        self._cases: Dict[str, tuple] = {
            # ------------------------------------------------------- Direct
            "WSMOKE-DIRECT-01": ("Direct PDP Landing", [], "direct", self._c_direct01),
            "WSMOKE-DIRECT-02": ("Direct PDP Variant + Add To Cart", ["WSMOKE-DIRECT-01"], "direct", self._c_direct02),
            "WSMOKE-DIRECT-03": ("Direct PDP Cart Consistency", ["WSMOKE-DIRECT-02"], "direct", self._c_direct03),
            # ------------------------------------------------------- Search
            "WSMOKE-SEARCH-01": ("Search Entry Available", [], "search", self._c_search01),
            "WSMOKE-SEARCH-02": ("Search Returns Products", ["WSMOKE-SEARCH-01"], "search", self._c_search02),
            "WSMOKE-SEARCH-03": ("Search Result Opens PDP", ["WSMOKE-SEARCH-02"], "search", self._c_search03),
            "WSMOKE-SEARCH-04": ("Search Product Purchase", ["WSMOKE-SEARCH-03"], "search", self._c_search04),
            # ------------------------------------------------------- Browse
            "WSMOKE-HOME-01": ("Homepage Core Available", [], "browse", self._c_home01),
            "WSMOKE-NAV-01": ("Navigation Opens Collection", ["WSMOKE-HOME-01"], "browse", self._c_nav01),
            "WSMOKE-PLP-01": ("Collection Product Opens PDP", ["WSMOKE-NAV-01"], "browse", self._c_plp01),
            "WSMOKE-PDP-01": ("Purchase Variant Available", ["WSMOKE-PLP-01"], "browse", self._c_pdp01),
            "WSMOKE-CART-01": ("Add To Cart + Drawer", ["WSMOKE-PDP-01"], "browse", self._c_cart01),
            "WSMOKE-CART-02": ("Cart Variant Consistency", ["WSMOKE-CART-01"], "browse", self._c_cart02),
            "WSMOKE-CART-03": ("Cart Quantity Update", ["WSMOKE-CART-02"], "browse", self._c_cart03),
            "WSMOKE-CHECKOUT-01": ("Checkout Entry + Core + Consistency", ["WSMOKE-CART-03"], "browse", self._c_checkout01),
        }

    # ------------------------------------------------------------------- 辅助
    def _cf_detected(self) -> bool:
        """按挑战页特征判断当前页面是否被 Cloudflare 托管挑战拦截。"""
        try:
            body = self.page.content().lower()[:2000]
            title = (self.page.title() or "").lower()
        except Exception:
            return False
        return any(m in body or m in title for m in CF_CHALLENGE_MARKERS)

    def _capture_evidence(self, case_id: str) -> tuple[List[dict], Optional[str]]:
        if self.artifact_dir is None:
            return [], None
        rel = capture_case_failure(self.page, self.artifact_dir, self.viewport, case_id)
        if rel is None:
            return [], "screenshot capture failed"
        return [{"type": "screenshot", "path": rel}], None

    def _mark_cf(self) -> None:
        self.cf_interruption = True

    def _run_case(self, case_id: str, name: str, deps: List[str], fn: Callable) -> None:
        # 仅同一 Journey 内部依赖
        for dep in deps:
            dep_result = self.results.get(dep)
            if dep_result is None or dep_result.status != STATUS_PASS:
                ts = iso_now()
                self.results[case_id] = CaseResult(
                    case_id, name, STATUS_BLOCKED, ts, ts, 0,
                    detail=f"prerequisite not PASS: {dep}",
                    failure_classification="BLOCKED_BY_DEPENDENCY",
                    blocked_by=[dep],
                )
                return
        started_ts = iso_now()
        started = time.perf_counter()
        try:
            detail = fn()
            duration = int((time.perf_counter() - started) * 1000)
            self.results[case_id] = CaseResult(
                case_id, name, STATUS_PASS, started_ts, iso_now(), duration, detail=detail or ""
            )
        except FlowError as exc:
            # Cloudflare 判定仅针对当前 Case（页面当前处于挑战页时才归因访问层）
            cf_now = self._cf_detected()
            if cf_now:
                self._mark_cf()
            duration = int((time.perf_counter() - started) * 1000)
            evidence, ev_error = self._capture_evidence(case_id)
            category = "PAGE_LOAD_FAILURE" if cf_now else exc.category
            detail = (
                f"AUTOMATION_ACCESS: Cloudflare Stability Failure | {exc}" if cf_now else str(exc)
            )
            self.results[case_id] = CaseResult(
                case_id, name, STATUS_FAIL, started_ts, iso_now(), duration,
                detail=detail, failure_classification=category,
                evidence=evidence, evidence_capture_error=ev_error,
            )
        except Exception as exc:  # noqa: BLE001 — case isolation is the contract
            cf_now = self._cf_detected()
            if cf_now:
                self._mark_cf()
            duration = int((time.perf_counter() - started) * 1000)
            evidence, ev_error = self._capture_evidence(case_id)
            category = "PAGE_LOAD_FAILURE" if cf_now else type(exc).__name__
            detail = (
                f"AUTOMATION_ACCESS: Cloudflare Stability Failure | {type(exc).__name__}: {exc}"
                if cf_now
                else f"{type(exc).__name__}: {exc}"
            )
            self.results[case_id] = CaseResult(
                case_id, name, STATUS_FAIL, started_ts, iso_now(), duration,
                detail=detail, failure_classification=category,
                evidence=evidence, evidence_capture_error=ev_error,
            )

    def _journey_cases(self, journey: str) -> List[str]:
        return [cid for cid, (_, _n, j, _f) in self._cases.items() if j == journey]

    def _run_journey(self, journey: str) -> None:
        """按注册表顺序执行某 Journey 的全部 Case（内部依赖链）。"""
        self._journey = journey
        for case_id in self._journey_cases(journey):
            name, deps, _j, fn = self._cases[case_id]
            self._run_case(case_id, name, deps, fn)

    def _journey_cleanup(self) -> None:
        """Journey 结束后兜底清理：抽屉有残留时 API 清空（不影响 Case 结论）。"""
        try:
            quantity = self.flow.cart_state_quantity(0)
        except FlowError as exc:
            self.cleanup_status = "BLOCKED" if exc.category == "CART_PRECONDITION_UNVERIFIED" else "FAIL"
            self.cleanup_error = f"{exc.category}: {exc}"
            return
        except Exception as exc:
            self.cleanup_status = "FAIL"
            self.cleanup_error = f"{type(exc).__name__}: {exc}"
            return

        # cart_state_quantity() 的 None 只表示没有第一个 item；不能把
        # None 当作“有残留”而无条件 POST /cart/clear.js。
        if quantity is None or quantity == "0":
            self.cleanup_detail = "cart_already_empty_no_clear_request"
            return
        try:
            self.flow.cleanup_cart()
            self.cleanup_detail = "api_clear_after_verified_residual_item"
        except Exception as exc:
            self.cleanup_status = "BLOCKED" if getattr(exc, "category", "") == "CART_PRECONDITION_UNVERIFIED" else "FAIL"
            self.cleanup_error = f"journey cleanup: {type(exc).__name__}: {exc}"

    # ------------------------------------------------------------ 一致性辅助
    def _capture_selected_variant(self) -> None:
        """记录 PDP 当前选中颜色 / 尺码到 flow state（基于 radio.checked）。"""
        prod = self.flow.state["prod"]
        color = prod.get_selected_color()
        size = prod.get_selected_size()
        if not color or not size:
            raise FlowError(
                "VARIANT_SELECTION_FAILURE",
                f"selected color={color!r} size={size!r}",
            )
        self.flow.state["color"] = color
        self.flow.state["size"] = size

    def _cart_consistency(self, mismatch_prefix: str, stored_title: str) -> str:
        """验证购物车与 PDP 状态一致（商品/颜色/尺码/数量=1）。

        商品身份：PDP h1 应用 text-transform: capitalize，innerText 与
        存储标题（购物车显示形态）存在 CSS 大小写差异时不算身份不一致。
        """
        rendered = self.flow.state["pdp_title"]
        cart_title = self.flow.state["cart_title"]
        product_ok = titles_match(rendered, cart_title) or titles_match(stored_title, cart_title)
        note = ""
        if not titles_match(rendered, cart_title) and titles_match(stored_title, cart_title):
            note = " rendered_title=CSS_CAPITALIZE_DIFFERENCE"
        color_ok = normalize(self.flow.state["color"]) == normalize(self.flow.state["cart_color"])
        size_ok = normalize(self.flow.state["size"]) == normalize(self.flow.state["cart_size"])
        qty_ok = int(self.flow.state["cart_qty"] or 0) == 1
        if not product_ok:
            raise FlowError(
                f"{mismatch_prefix}_PRODUCT_MISMATCH",
                f"PDP={rendered[:60]!r} Cart={cart_title[:60]!r}",
            )
        if not color_ok:
            raise FlowError(
                f"{mismatch_prefix}_COLOR_MISMATCH",
                f"color={self.flow.state['color']!r} cart_color={self.flow.state['cart_color']!r}",
            )
        if not size_ok:
            raise FlowError(
                f"{mismatch_prefix}_SIZE_MISMATCH",
                f"size={self.flow.state['size']!r} cart_size={self.flow.state['cart_size']!r}",
            )
        if not qty_ok:
            raise FlowError(
                f"{mismatch_prefix}_QUANTITY_MISMATCH",
                f"cart_qty={self.flow.state['cart_qty']!r}",
            )
        return f"product=match color=match size=match qty={self.flow.state['cart_qty']}{note}"

    def _stored_title(self, prod: ProductPage, fallback: str) -> str:
        """读取 PDP 标题存储形态（textContent），失败回退渲染形态。"""
        try:
            return " ".join(prod.title().evaluate("el => el.textContent").split())
        except Exception:
            return fallback

    # -------------------------------------------------------------------- API
    def run_all(self) -> List[CaseResult]:
        # 前置：API 级购物车清理（基础设施，不产生业务页面导航）
        try:
            self.flow.pre_clean_cart()
        except FlowError as exc:
            self.pre_clean_status = "BLOCKED" if exc.category == "CART_PRECONDITION_UNVERIFIED" else "FAIL"
            self.pre_clean_error = f"{exc.category}: {exc}"
            self._block_all_cases("pre-clean")
            return list(self.results.values())

        # 执行顺序：Direct -> Search -> Browse（Browse 最后进入 Checkout 后销毁 Context）
        self._run_journey("direct")
        self._journey_cleanup()
        self._run_journey("search")
        self._journey_cleanup()
        self._run_journey("browse")
        # Browse 结束于 Checkout；无需返回购物车清理（Context 由 CLI dispose）

        # 关闭多余旧页面，保持单 active business page
        for p in self._extra_pages:
            try:
                p.close()
            except Exception:
                pass
        self._extra_pages = []
        return list(self.results.values())

    def _block_all_cases(self, blocked_by: str) -> None:
        """前置清理无法确认时阻止所有 Case，避免在未知购物车状态上运行。"""
        ts = iso_now()
        classification = "CART_PRECONDITION_UNVERIFIED" if self.pre_clean_status == "BLOCKED" else "PRECONDITION_CART_CLEAN_FAILURE"
        for case_id, (name, _deps, _journey, _fn) in self._cases.items():
            self.results[case_id] = CaseResult(
                case_id,
                name,
                STATUS_BLOCKED,
                ts,
                ts,
                0,
                detail=self.pre_clean_error or "pre-clean failed",
                failure_classification=classification,
                blocked_by=[blocked_by],
            )

    # ============================================================ Journey Direct
    def _c_direct01(self) -> str:
        """Direct PDP Landing：第一张业务 UI 页面即 PDP，购买区完整初始化。"""
        prod = ProductPage(self.page, self.site_config, self.viewport)
        self.flow.state["prod"] = prod
        try:
            prod.open()
        except Exception as exc:
            if self._cf_detected():
                self._mark_cf()
                raise FlowError(
                    "PAGE_LOAD_FAILURE",
                    "AUTOMATION_ACCESS: Cloudflare Stability Failure | direct PDP open",
                ) from exc
            raise FlowError("DIRECT_PDP_LANDING_FAILURE", f"{type(exc).__name__}: {exc}") from exc
        self.state["direct_path"] = urlparse(self.page.url).path
        title = prod.get_title()
        try:
            price = prod.get_price()
        except Exception:
            price = ""
        if not title or not price:
            raise FlowError("DIRECT_PDP_PURCHASE_AREA_NOT_AVAILABLE", f"title={title[:40]!r} price={price!r}")
        color_count, size_count, atc_available = prod.wait_purchase_ready()
        if color_count == 0 or size_count == 0 or not atc_available:
            raise FlowError(
                "DIRECT_PDP_PURCHASE_AREA_NOT_AVAILABLE",
                f"color_options={color_count} size_options={size_count} atc={atc_available}",
            )
        try:
            self.flow.validate_purchase_area()
        except FlowError as exc:
            raise FlowError("DIRECT_PDP_PURCHASE_AREA_NOT_AVAILABLE", str(exc)) from exc
        self.flow.capture_pdp_state()
        self.state["direct_stored_title"] = self._stored_title(prod, title)
        return (
            f"entry_mode=DIRECT product_path={self.state['direct_path']} title={title[:40]!r} "
            f"price={price} color_options={color_count} size_options={size_count} atc_available=True"
        )

    def _c_direct02(self) -> str:
        """Direct PDP Variant + Add To Cart：变体可选、真实加购、抽屉打开。"""
        try:
            color = self.flow.select_color()
            size = self.flow.select_size()
            self.flow.add_to_cart()
            self.flow.capture_cart_state()
        except FlowError as exc:
            category = exc.category
            if category == "ADD_TO_CART_FAILURE":
                category = "DIRECT_PDP_ADD_TO_CART_FAILURE"
            elif category in ("CART_DRAWER_NOT_OPENED", "CART_ITEM_NOT_FOUND"):
                category = "DIRECT_PDP_CART_NOT_OPENED"
            raise FlowError(category, str(exc)) from exc
        except Exception as exc:
            raise FlowError("DIRECT_PDP_ADD_TO_CART_FAILURE", f"{type(exc).__name__}: {exc}") from exc
        return f"color={color} size={size} atc=True drawer_open=True cart_item=True"

    def _c_direct03(self) -> str:
        """Direct PDP Cart Consistency：抽屉内商品与 PDP 所选状态一致，随后 UI 移除。"""
        detail = self._cart_consistency(
            "DIRECT_PDP", self.state.get("direct_stored_title", self.flow.state["pdp_title"])
        )
        try:
            self.flow.remove_and_wait_empty()
        except Exception as exc:
            raise FlowError("CLEANUP_FAILURE", f"UI remove failed: {type(exc).__name__}: {exc}") from exc
        return f"{detail} remove=True empty=True"

    # ============================================================ Journey Search
    def _c_search01(self) -> str:
        """Search Entry Available：首页进入 Search，容器打开且输入框可用。"""
        search = SearchPage(self.page, self.site_config, self.viewport)
        try:
            search.open_from_home()
        except Exception as exc:
            if self._cf_detected():
                self._mark_cf()
                raise FlowError(
                    "PAGE_LOAD_FAILURE",
                    "AUTOMATION_ACCESS: Cloudflare Stability Failure | search open",
                ) from exc
            raise FlowError("SEARCH_TRIGGER_FAILURE", f"{type(exc).__name__}: {exc}") from exc
        if not search.is_open():
            raise FlowError("SEARCH_UI_NOT_OPENED", "search container not open")
        try:
            search.validate_input_usable()
        except RuntimeError as exc:
            raise FlowError("SEARCH_INPUT_NOT_AVAILABLE", str(exc)) from exc
        self.state["search"] = search
        return "search_mode=HYBRID_SEARCH container_open=True input_usable=True"

    def _c_search02(self) -> str:
        """Search Returns Products：提交 dress，结果数量 > 0 且 URL query 正确。"""
        search = self.state["search"]
        try:
            recovered = search.submit_query(POSITIVE_QUERY)
        except Exception as exc:
            if self._cf_detected():
                self._mark_cf()
                raise FlowError(
                    "PAGE_LOAD_FAILURE",
                    "AUTOMATION_ACCESS: Cloudflare Stability Failure | search submit",
                ) from exc
            raise FlowError("SEARCH_SUBMIT_FAILURE", f"{type(exc).__name__}: {exc}") from exc
        self.search_recovery_used = self.search_recovery_used or recovered
        search.wait_predictive_ready()
        predictive = search.predictive_product_cards().count()
        count = search.result_count()
        url_query = search.current_query()
        if count <= 0:
            raise FlowError("SEARCH_RESULTS_NOT_FOUND", f"result_count={count}")
        if url_query != POSITIVE_QUERY:
            raise FlowError(
                "SEARCH_QUERY_STATE_MISMATCH",
                f"url_q={url_query!r} expected={POSITIVE_QUERY!r}",
            )
        return (
            f"query={POSITIVE_QUERY} result_count={count} "
            f"predictive_products={predictive} recovery={recovered}"
        )

    def _c_search03(self) -> str:
        """Search Result Opens PDP：真实点击结果卡片（target=_blank 新标签页）。"""
        search = self.state["search"]
        try:
            new_page = search.open_result(0)
        except Exception as exc:
            raise FlowError("SEARCH_RESULT_OPEN_FAILURE", f"{type(exc).__name__}: {exc}") from exc
        if "/products/" not in new_page.url:
            raise FlowError(
                "SEARCH_PDP_NAVIGATION_FAILURE", f"url={new_page.url[:120]}"
            )
        # 后续操作绑定新 PDP 页
        self._extra_pages.append(self.page)
        self.page = new_page
        self.flow = ShoppingFlow(
            new_page, self.site_config, self.viewport, access_policy=self.runtime.access_policy
        )
        prod = ProductPage(new_page, self.site_config, self.viewport)
        self.flow.state["prod"] = prod
        try:
            title = prod.get_title()
        except Exception as exc:
            raise FlowError("SEARCH_PDP_NAVIGATION_FAILURE", f"title unreadable: {type(exc).__name__}") from exc
        if not title:
            raise FlowError("SEARCH_PDP_NAVIGATION_FAILURE", "title empty")
        self.state["search_prod"] = prod
        return f"url={new_page.url[:80]} title={title[:50]!r}"

    def _c_search04(self) -> str:
        """Search Product Purchase：搜索结果商品完整进入购买链，随后 UI 移除。"""
        prod = self.state["search_prod"]
        color_count, size_count, atc_available = prod.wait_purchase_ready()
        if color_count == 0 or size_count == 0 or not atc_available:
            raise FlowError(
                "SEARCH_PURCHASE_AREA_NOT_AVAILABLE",
                f"color_options={color_count} size_options={size_count} atc={atc_available}",
            )
        try:
            self.flow.capture_pdp_state()
            color = self.flow.select_color()
            size = self.flow.select_size()
            self.flow.add_to_cart()
            self.flow.capture_cart_state()
        except FlowError:
            raise
        stored = self._stored_title(prod, self.flow.state["pdp_title"])
        detail = self._cart_consistency("SEARCH", stored)
        try:
            self.flow.remove_and_wait_empty()
        except Exception as exc:
            raise FlowError("CLEANUP_FAILURE", f"UI remove failed: {type(exc).__name__}: {exc}") from exc
        return f"color={color} size={size} {detail} remove=True empty=True"

    # ============================================================ Journey Browse
    def _c_home01(self) -> str:
        """Homepage Core Available：首页核心元素（Header/Logo/导航入口）。"""
        try:
            HomePage(self.page, self.site_config, self.viewport).open()
        except Exception as exc:
            if self._cf_detected():
                self._mark_cf()
                raise FlowError(
                    "PAGE_LOAD_FAILURE",
                    "AUTOMATION_ACCESS: Cloudflare Stability Failure | home open",
                ) from exc
            raise FlowError("HOME_CORE_NOT_AVAILABLE", f"{type(exc).__name__}: {exc}") from exc
        home = HomePage(self.page, self.site_config, self.viewport)
        try:
            home.wait_core_ready()
        except Exception as exc:
            raise FlowError("HOME_CORE_NOT_AVAILABLE", f"home readiness: {type(exc).__name__}: {exc}") from exc
        nav = NavigationPage(self.page, self.site_config, self.viewport)
        try:
            nav.wait_ready()
        except Exception as exc:
            raise FlowError("HOME_CORE_NOT_AVAILABLE", f"navigation readiness: {type(exc).__name__}: {exc}") from exc
        entry = True
        self.state["nav"] = nav
        return f"header_present=True logo_present=True navigation_entry={entry}"

    def _c_nav01(self) -> str:
        """Navigation Opens Collection：真实导航菜单进入目标 Collection 且网格可用。

        来源能力：NAV-01 / NAV-02 / NAV-03 / NAV-04（压缩）。
        Desktop: hover "New In" -> mega 子菜单 -> Wedding Guest Dresses
        Mobile:  汉堡 -> Drawer -> 展开 "New In" -> Wedding Guest Dresses
        """
        nav = self.state["nav"]
        mode = nav.current_mode()
        try:
            url = nav.open_collection()
        except Exception as exc:
            if self._cf_detected():
                self._mark_cf()
                raise FlowError(
                    "PAGE_LOAD_FAILURE",
                    "AUTOMATION_ACCESS: Cloudflare Stability Failure | navigation",
                ) from exc
            raise FlowError("NAVIGATION_COLLECTION_FAILURE", f"{type(exc).__name__}: {exc}") from exc
        target_path = nav.target_path()
        if urlparse(url).path != target_path:
            raise FlowError("NAVIGATION_COLLECTION_FAILURE", f"url={url[:120]} expected_path={target_path}")
        coll = CollectionPage(self.page, self.site_config, self.viewport)
        try:
            grid = coll.product_grid()
            grid.wait_for(state="visible", timeout=15_000)
            grid_ok = bool(grid.count() and grid.is_visible())
        except Exception as exc:
            raise FlowError("NAVIGATION_COLLECTION_FAILURE", f"grid: {type(exc).__name__}") from exc
        if not grid_ok:
            raise FlowError("NAVIGATION_COLLECTION_FAILURE", "product grid not visible")
        self.state["coll"] = coll
        return f"mode={mode} path={target_path} grid=True"

    def _c_plp01(self) -> str:
        """Collection Product Opens PDP：第一个稳定商品卡真实点击进入 PDP。"""
        coll = self.state["coll"]
        try:
            url = coll.open_product(0)
        except Exception as exc:
            raise FlowError("PLP_PRODUCT_OPEN_FAILURE", f"{type(exc).__name__}: {exc}") from exc
        if "/products/" not in url:
            raise FlowError("PLP_PRODUCT_OPEN_FAILURE", f"url={url[:120]}")
        prod = ProductPage(self.page, self.site_config, self.viewport)
        self.flow.state["prod"] = prod
        try:
            title = prod.get_title()
        except Exception as exc:
            raise FlowError("PLP_PRODUCT_OPEN_FAILURE", f"title unreadable: {type(exc).__name__}") from exc
        if not title:
            raise FlowError("PLP_PRODUCT_OPEN_FAILURE", "title empty")
        self.state["browse_prod"] = prod
        self.state["browse_stored_title"] = self._stored_title(prod, title)
        return f"url={url[:80]} title={title[:50]!r}"

    def _c_pdp01(self) -> str:
        """Purchase Variant Available：购买区就绪，真实选择 Color + Size。"""
        prod = self.state["browse_prod"]
        color_count, size_count, atc_available = prod.wait_purchase_ready()
        if color_count == 0 or size_count == 0 or not atc_available:
            raise FlowError(
                "PURCHASE_AREA_FAILURE",
                f"color_options={color_count} size_options={size_count} atc={atc_available}",
            )
        try:
            self.flow.validate_purchase_area()
            self.flow.capture_pdp_state()
            color = self.flow.select_color()
            size = self.flow.select_size()
        except FlowError as exc:
            raise FlowError("VARIANT_SELECTION_FAILURE", str(exc)) from exc
        self._capture_selected_variant()
        return f"color={color} size={size} color_state=checked size_state=checked"

    def _c_cart01(self) -> str:
        """Add To Cart + Drawer：真实 UI 加购，抽屉打开且商品行出现。"""
        try:
            self.flow.add_to_cart()
            self.flow.capture_cart_state()
        except FlowError as exc:
            raise FlowError("ADD_TO_CART_FAILURE", str(exc)) from exc
        return f"atc=True drawer_open=True cart_item=True"

    def _c_cart02(self) -> str:
        """Cart Variant Consistency：抽屉商品与 PDP 选择一致（商品/颜色/尺码/数量）。"""
        return self._cart_consistency(
            "PRODUCT", self.state.get("browse_stored_title", self.flow.state["pdp_title"])
        )

    def _c_cart03(self) -> str:
        """Cart Quantity Update：真实 +/- 完成 1 -> 2 -> 1，UI 与后端状态一致。

        来源能力：CART-QTY-02 / CART-QTY-03 / CART-QTY-04（压缩）。
        Subtotal 仅作观察记录，不作为断言依据。
        """
        drawer = self.flow.state["drawer"]
        try:
            subtotal_before = drawer.get_subtotal()
            drawer.increase_quantity(0)
            drawer.wait_quantity(0, 2)
            ui_after_inc = drawer.get_item_quantity(0)
            backend_after_inc = self.flow.cart_state_quantity(0)
            if ui_after_inc != "2" or backend_after_inc != "2":
                raise FlowError(
                    "CART_QUANTITY_STATE_MISMATCH",
                    f"ui={ui_after_inc} backend={backend_after_inc}",
                )
            subtotal_mid = drawer.get_subtotal()
            drawer.decrease_quantity(0)
            drawer.wait_quantity(0, 1)
            ui_after_dec = drawer.get_item_quantity(0)
            backend_after_dec = self.flow.cart_state_quantity(0)
            if ui_after_dec != "1" or backend_after_dec != "1":
                raise FlowError(
                    "CART_QUANTITY_STATE_MISMATCH",
                    f"ui={ui_after_dec} backend={backend_after_dec}",
                )
        except FlowError:
            raise
        except RuntimeError as exc:
            raise FlowError("CART_QUANTITY_INCREASE_FAILURE", str(exc)) from exc
        except TimeoutError as exc:
            raise FlowError("CART_QUANTITY_UPDATE_TIMEOUT", str(exc)) from exc
        subtotal_after = drawer.get_subtotal()
        return (
            f"1->2 ui={ui_after_inc} backend={backend_after_inc} "
            f"2->1 ui={ui_after_dec} backend={backend_after_dec} "
            f"subtotal={subtotal_before}->{subtotal_mid}->{subtotal_after}"
        )

    def _c_checkout01(self) -> str:
        """Checkout Entry + Core + Consistency：标准 Checkout 进入并验证核心 UI 与商品一致。

        来源能力：CHECKOUT-02 / CHECKOUT-03 / CHECKOUT-04（压缩）。
        安全边界：不填写任何字段、不点击快捷支付 / 支付 / 提交订单控件。
        """
        drawer = self.flow.state["drawer"]
        checkout = CheckoutPage(self.page, self.site_config, self.viewport)
        try:
            drawer.checkout()
            checkout.wait_checkout_context()
            checkout.wait_checkout_settled()
        except RuntimeError as exc:
            raise FlowError("CHECKOUT_NAVIGATION_FAILURE", str(exc)) from exc
        except TimeoutError as exc:
            raise FlowError("CHECKOUT_CONTEXT_NOT_REACHED", str(exc)) from exc
        sections = checkout.core_sections_present()
        if not sections["root"]:
            raise FlowError("CHECKOUT_CORE_NOT_AVAILABLE", "checkout root missing")
        if not checkout.has_order_summary():
            raise FlowError("CHECKOUT_ORDER_SUMMARY_NOT_AVAILABLE", "order summary not found")
        if not (sections["contact"] or sections["delivery"] or sections["email"]):
            raise FlowError(
                "CHECKOUT_CORE_NOT_AVAILABLE", f"no contact/delivery section: {sections}"
            )
        try:
            checkout.ensure_order_summary_visible()
        except Exception as exc:
            raise FlowError(
                "CHECKOUT_ORDER_SUMMARY_NOT_AVAILABLE",
                f"{type(exc).__name__}: {exc}",
            ) from exc

        # 商品一致性：身份（P0）+ 数量（P0）+ 变体（P1，可读取时验证）
        checkout_title = checkout.get_product_title(0)
        pdp_title = self.flow.state["pdp_title"]
        stored = self.state.get("browse_stored_title", pdp_title)
        title_note = ""
        if not checkout_title:
            raise FlowError("CHECKOUT_PRODUCT_MISMATCH", "checkout product title unreadable")
        if titles_match(pdp_title, checkout_title):
            pass
        elif titles_match(stored, checkout_title):
            title_note = " rendered_title=CSS_CAPITALIZE_DIFFERENCE"
        else:
            raise FlowError(
                "CHECKOUT_PRODUCT_MISMATCH",
                f"pdp={pdp_title[:50]!r} checkout={checkout_title[:50]!r}",
            )
        qty = checkout.get_product_quantity(0)
        qty_detail = qty
        if qty and qty != self.flow.state["cart_qty"]:
            raise FlowError(
                "CHECKOUT_QUANTITY_MISMATCH",
                f"cart_qty={self.flow.state['cart_qty']} checkout_qty={qty}",
            )
        if not qty:
            if checkout.product_count_readable() != 1:
                raise FlowError("CHECKOUT_QUANTITY_MISMATCH", "quantity unreadable and count != 1")
            qty_detail = "IMPLICIT_SINGLE_ITEM"
        variant = checkout.get_product_variant(0)
        color_ok = self.flow.state["color"].casefold() in variant.casefold()
        size_ok = self.flow.state["size"].casefold() in variant.casefold()
        if not (color_ok and size_ok):
            raise FlowError(
                "CHECKOUT_VARIANT_MISMATCH",
                f"variant={variant[:60]!r} color={self.flow.state['color']!r} size={self.flow.state['size']!r}",
            )
        price = checkout.get_product_price(0)
        subtotal = checkout.get_subtotal()
        return (
            f"checkout_context=True core=True order_summary=True "
            f"product=match quantity={qty_detail} color_match={color_ok} size_match={size_ok} "
            f"price={price} subtotal={subtotal}{title_note}"
        )
