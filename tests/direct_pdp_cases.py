"""Direct PDP Purchase Cases：无前置页面状态下独立完成核心购买流程。

稳定 Case ID（本阶段后冻结，作为未来 Website Smoke 的可复用接口）：

    DIRECT-PDP-01  Direct PDP Landing
    DIRECT-PDP-02  Direct PDP Variant Selection
    DIRECT-PDP-03  Direct PDP Add To Cart
    DIRECT-PDP-04  Direct PDP Cart Consistency

依赖模型（顺序业务链）：
    DIRECT-PDP-01 -> DIRECT-PDP-02 -> DIRECT-PDP-03 -> DIRECT-PDP-04

核心验证目标：PDP 购买链不依赖 PLP / Home 等页面前置状态。
    BrowserContext 的第一张业务 UI 页面就是 PDP（ProductPage.open()），
    之前只允许 API 级购物车清理（/cart/clear.js），不允许任何站点页面导航。

复用 ProductPage / CartDrawer / ShoppingFlow，本文件只做编排与业务断言。
正式加购必须是真实 UI 点击；/cart/add.js 仅由 ShoppingFlow 内部监听响应。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import urlparse

from flows.shopping_flow import FlowError, ShoppingFlow, normalize, titles_match
from pages.product_page import ProductPage
from utils.result import CaseResult, iso_now
from utils.screenshots import capture_case_failure

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_BLOCKED = "BLOCKED"

CF_CHALLENGE_MARKERS = ("just a moment", "cf-challenge", "attention required")


class DirectPdpCaseRunner:
    """针对单个 BrowserRuntime 执行 4 个 Direct PDP Purchase Cases。"""

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
        self.product_path = ""

        self._cases: Dict[str, tuple] = {
            "DIRECT-PDP-01": ("Direct PDP Landing", [], self._case_pdp01),
            "DIRECT-PDP-02": ("Direct PDP Variant Selection", ["DIRECT-PDP-01"], self._case_pdp02),
            "DIRECT-PDP-03": ("Direct PDP Add To Cart", ["DIRECT-PDP-02"], self._case_pdp03),
            "DIRECT-PDP-04": ("Direct PDP Cart Consistency", ["DIRECT-PDP-03"], self._case_pdp04),
        }

    # ------------------------------------------------------------------- 辅助
    def _classify_access(self, exc: Exception) -> str:
        """区分 Cloudflare / Access 中断与功能失败（按挑战页特征判断）。"""
        try:
            body = self.page.content().lower()[:2000]
            title = (self.page.title() or "").lower()
        except Exception:
            return "DIRECT_PDP_LANDING_FAILURE"
        if any(m in body or m in title for m in CF_CHALLENGE_MARKERS):
            return "DIRECT_PDP_ACCESS_INTERRUPTED"
        return "DIRECT_PDP_LANDING_FAILURE"

    def _capture_evidence(self, case_id: str) -> tuple[List[dict], Optional[str]]:
        if self.artifact_dir is None:
            return [], None
        rel = capture_case_failure(self.page, self.artifact_dir, self.viewport, case_id)
        if rel is None:
            return [], "screenshot capture failed"
        return [{"type": "screenshot", "path": rel}], None

    def _run_case(self, case_id: str, name: str, deps: List[str], fn: Callable) -> None:
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
            duration = int((time.perf_counter() - started) * 1000)
            evidence, ev_error = self._capture_evidence(case_id)
            self.results[case_id] = CaseResult(
                case_id, name, STATUS_FAIL, started_ts, iso_now(), duration,
                detail=str(exc), failure_classification=exc.category,
                evidence=evidence, evidence_capture_error=ev_error,
            )
        except Exception as exc:  # noqa: BLE001 — case isolation is the contract
            duration = int((time.perf_counter() - started) * 1000)
            evidence, ev_error = self._capture_evidence(case_id)
            self.results[case_id] = CaseResult(
                case_id, name, STATUS_FAIL, started_ts, iso_now(), duration,
                detail=f"{type(exc).__name__}: {exc}",
                failure_classification=type(exc).__name__,
                evidence=evidence, evidence_capture_error=ev_error,
            )

    # -------------------------------------------------------------------- API
    def run_all(self) -> List[CaseResult]:
        # 前置：API 级购物车清理（基础设施，不产生业务页面导航）
        try:
            self.flow.pre_clean_cart()
        except FlowError as exc:
            self.pre_clean_status = "FAIL"
            self.pre_clean_error = str(exc)
            ts = iso_now()
            self.results["DIRECT-PDP-01"] = CaseResult(
                "DIRECT-PDP-01", "Direct PDP Landing", STATUS_FAIL, ts, ts, 0,
                detail=f"pre-clean failed: {exc}",
                failure_classification=exc.category,
            )
            self._block_rest("DIRECT-PDP-01")
            return list(self.results.values())

        # Direct Entry：第一张业务 UI 页面就是 PDP
        prod = ProductPage(self.page, self.site_config, self.viewport)
        self.flow.state["prod"] = prod
        self.product_path = urlparse(self.page.url).path  # 占位，open 后更新
        try:
            prod.open()
            self.product_path = urlparse(self.page.url).path
        except Exception as exc:  # noqa: BLE001
            self.pre_clean_status = "PASS"
            ts = iso_now()
            self.results["DIRECT-PDP-01"] = CaseResult(
                "DIRECT-PDP-01", "Direct PDP Landing", STATUS_FAIL, ts, ts, 0,
                detail=f"direct PDP open failed: {type(exc).__name__}: {exc}",
                failure_classification=self._classify_access(exc),
            )
            self._block_rest("DIRECT-PDP-01")
            return list(self.results.values())

        for case_id, (name, deps, fn) in self._cases.items():
            self._run_case(case_id, name, deps, fn)

        # 后置清理：UI Remove 优先，API 兜底；清理结果不影响 Case 结论
        try:
            self.flow.remove_and_wait_empty()
            self.cleanup_status = "PASS"
        except Exception as exc:
            self.cleanup_error = f"{type(exc).__name__}: {exc}"
            try:
                self.flow.cleanup_cart()
                self.cleanup_status = "PASS"
            except Exception as exc2:
                self.cleanup_status = "FAIL"
                self.cleanup_error += f" | api fallback: {type(exc2).__name__}"
        return list(self.results.values())

    def _block_rest(self, failed_case: str) -> None:
        """前置失败：后续 Case 全部 BLOCKED（依赖链）。"""
        blocked = False
        for case_id, (name, _deps, _fn) in self._cases.items():
            if case_id == failed_case:
                blocked = True
                continue
            if not blocked:
                continue
            ts = iso_now()
            self.results[case_id] = CaseResult(
                case_id, name, STATUS_BLOCKED, ts, ts, 0,
                detail=f"prerequisite not PASS: {failed_case}",
                failure_classification="BLOCKED_BY_DEPENDENCY",
                blocked_by=[failed_case],
            )

    # ------------------------------------------------------------ Case 实现
    def _case_pdp01(self) -> str:
        """Direct PDP Landing：直落后 PDP 完整初始化（标题/价格/购买区/选项/加购）。

        SPB 尺码控件与加购按钮在标题渲染后异步初始化，
        因此按"购买区就绪"状态等待（有界轮询），非瞬时断言。
        """
        prod = self.flow.state["prod"]
        title = prod.get_title()
        try:
            price = prod.get_price()
        except Exception:
            price = ""
        if not title or not price:
            raise FlowError(
                "DIRECT_PDP_PURCHASE_AREA_NOT_AVAILABLE",
                f"title={title[:40]!r} price={price!r}",
            )
        # 购买区就绪等待：SPB 尺码选项与加购按钮异步渲染，轮询至就绪（≤15s）
        color_count = size_count = 0
        atc_available = False
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            color_count = sum(1 for _ in prod._available_options(prod.color_options()))
            size_count = sum(1 for _ in prod._available_options(prod.size_options()))
            atc = prod.add_to_cart_button()
            atc_available = bool(atc.count() and atc.is_visible() and atc.is_enabled())
            if color_count > 0 and size_count > 0 and atc_available:
                break
            self.page.wait_for_timeout(500)
        if color_count == 0 or size_count == 0 or not atc_available:
            raise FlowError(
                "DIRECT_PDP_PURCHASE_AREA_NOT_AVAILABLE",
                f"color_options={color_count} size_options={size_count} atc={atc_available}",
            )
        # 就绪后走规范购买区验证（瞬时检查此时必然通过）
        try:
            self.flow.validate_purchase_area()
        except FlowError as exc:
            raise FlowError("DIRECT_PDP_PURCHASE_AREA_NOT_AVAILABLE", str(exc)) from exc
        self.flow.capture_pdp_state()
        # 存储形态标题（textContent）：PDP h1 有 text-transform: capitalize，
        # innerText 是 CSS 渲染后文本；身份比较必须用存储数据层（与购物车一致）
        try:
            self.state["pdp_title_stored"] = " ".join(
                prod.title().evaluate("el => el.textContent").split()
            )
        except Exception:
            self.state["pdp_title_stored"] = title
        self.state["pdp_ready"] = True
        return (
            f"entry_mode=DIRECT product_path={self.product_path} "
            f"title={title[:40]!r} price={price} color_options={color_count} "
            f"size_options={size_count} atc_available=True"
        )

    def _case_pdp02(self) -> str:
        """Direct PDP Variant Selection：直落后变体 UI 状态可正常选择。"""
        try:
            color = self.flow.select_color()
            size = self.flow.select_size()
        except Exception as exc:
            raise FlowError(
                "DIRECT_PDP_VARIANT_SELECTION_FAILURE",
                f"{type(exc).__name__}: {exc}",
            ) from exc
        prod = self.flow.state["prod"]
        color_state = prod.get_selected_color()
        size_state = prod.get_selected_size()
        if color_state != color or size_state != size:
            raise FlowError(
                "DIRECT_PDP_VARIANT_SELECTION_FAILURE",
                f"color={color!r} color_state={color_state!r} "
                f"size={size!r} size_state={size_state!r}",
            )
        self.flow.state["color"] = color
        self.flow.state["size"] = size
        return f"color={color} size={size} color_state=checked size_state=checked"

    def _case_pdp03(self) -> str:
        """Direct PDP Add To Cart：真实 UI 加购，抽屉自动打开且商品行出现。"""
        try:
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
            # 加购按钮被遮挡 / 拦截（如首次访问弹层）：真实用户影响，不穿透点击
            if "intercepts" in str(exc) or "not visible" in str(exc):
                raise FlowError("DIRECT_PDP_UI_BLOCKED", f"{type(exc).__name__}: {exc}") from exc
            raise FlowError("DIRECT_PDP_ADD_TO_CART_FAILURE", f"{type(exc).__name__}: {exc}") from exc
        return "atc=True drawer_open=True cart_item=True"

    def _case_pdp04(self) -> str:
        """Direct PDP Cart Consistency：抽屉内商品与 PDP 所选状态一致。

        商品身份比较使用 PDP 标题的存储形态（textContent）作为备选来源：
        PDP h1 应用 text-transform: capitalize，innerText 与存储标题
        （也是购物车显示形态）仅存在 CSS 大小写差异时不算身份不一致。
        """
        cart_title = self.flow.state["cart_title"]
        rendered = self.flow.state["pdp_title"]
        stored = self.state.get("pdp_title_stored", rendered)
        product_ok = titles_match(rendered, cart_title) or titles_match(stored, cart_title)
        title_note = ""
        if not titles_match(rendered, cart_title) and titles_match(stored, cart_title):
            title_note = " rendered_title=CSS_CAPITALIZE_DIFFERENCE"
        color_ok = normalize(self.flow.state["color"]) == normalize(
            self.flow.state["cart_color"]
        )
        size_ok = normalize(self.flow.state["size"]) == normalize(
            self.flow.state["cart_size"]
        )
        qty_ok = int(self.flow.state["cart_qty"] or 0) == 1

        if not product_ok:
            raise FlowError(
                "DIRECT_PDP_PRODUCT_MISMATCH",
                f"PDP={rendered[:60]!r} Cart={cart_title[:60]!r}",
            )
        if not color_ok:
            raise FlowError(
                "DIRECT_PDP_COLOR_MISMATCH",
                f"color={self.flow.state['color']!r} cart_color={self.flow.state['cart_color']!r}",
            )
        if not size_ok:
            raise FlowError(
                "DIRECT_PDP_SIZE_MISMATCH",
                f"size={self.flow.state['size']!r} cart_size={self.flow.state['cart_size']!r}",
            )
        if not qty_ok:
            raise FlowError(
                "DIRECT_PDP_QUANTITY_MISMATCH",
                f"cart_qty={self.flow.state['cart_qty']!r}",
            )
        return (
            f"product=match color=match size=match "
            f"qty={self.flow.state['cart_qty']}{title_note}"
        )
