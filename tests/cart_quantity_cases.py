"""Cart Drawer 数量 Cases：真实 UI 数量调整（1 → 2 → 1）与状态一致性。

稳定 Case ID（本阶段后冻结，作为未来 Website Smoke 的可复用接口）：

    CART-QTY-01  Quantity Controls Available
    CART-QTY-02  Quantity Increase
    CART-QTY-03  Quantity State Consistency
    CART-QTY-04  Quantity Decrease

依赖模型（有状态业务链）：
    CART-QTY-01 -> CART-QTY-02 -> CART-QTY-03 -> CART-QTY-04

每 viewport 只建立一次购物车前置状态（PDP -> Color -> Size -> ATC ->
Drawer），随后连续执行 4 条 Case，最后统一 Cleanup（UI Remove 优先，
API 兜底）。

数量交互模型（真实探测）：BUTTON_AND_INPUT
    [-] [1] [+]（js-qty__adjust--minus / input.js-qty__num / js-qty__adjust--plus）
    点击 +/- 触发 POST /cart/change.js，响应后主题更新 DOM。

正式操作必须真实 UI 点击；/cart.js 仅作只读状态验证（Layer B），
不通过 API 修改购物车。这些 Case 如实暴露站点行为。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from flows.shopping_flow import FlowError, ShoppingFlow
from pages.cart_drawer import CartDrawer
from utils.result import CaseResult, iso_now
from utils.screenshots import capture_case_failure

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_BLOCKED = "BLOCKED"

INTERACTION_MODE = "BUTTON_AND_INPUT"


class CartQuantityCaseRunner:
    """针对单个 BrowserRuntime 执行 4 个 Cart Quantity Cases。"""

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
        self.cleanup_status = "PASS"
        self.pre_clean_error = ""
        self.cleanup_error = ""

        self._cases: Dict[str, tuple] = {
            "CART-QTY-01": ("Quantity Controls Available", [], self._case_qty01),
            "CART-QTY-02": ("Quantity Increase", ["CART-QTY-01"], self._case_qty02),
            "CART-QTY-03": ("Quantity State Consistency", ["CART-QTY-02"], self._case_qty03),
            "CART-QTY-04": ("Quantity Decrease", ["CART-QTY-03"], self._case_qty04),
        }

    # ------------------------------------------------------------------- 辅助
    def _drawer(self) -> CartDrawer:
        return CartDrawer(self.page, self.site_config, self.viewport)

    def _prepare_cart(self) -> None:
        """建立购物车前置状态：清空 -> 选品 -> 颜色/尺码 -> 加购 -> 抽屉就绪。

        复用 ShoppingFlow 已验证步骤；业务数量操作不在此阶段执行。
        """
        self.flow.pre_clean_cart()
        self.flow.open_collection()
        self.flow.open_product()
        self.flow.validate_purchase_area()
        self.flow.select_color()
        self.flow.select_size()
        self.flow.capture_pdp_state()
        self.flow.add_to_cart()
        self.flow.capture_cart_state()
        self.state["drawer"] = self._drawer()
        self.state["cart_title"] = self.flow.state["cart_title"]

    def _backend_quantity(self) -> str:
        """读取后端购物车数量；Access 层阻断时抛 CART_STATE_VERIFICATION_UNAVAILABLE。"""
        try:
            qty = self.flow.cart_state_quantity(0)
        except FlowError as exc:
            raise FlowError(
                "CART_STATE_VERIFICATION_UNAVAILABLE", f"/cart.js read failed: {exc}"
            ) from exc
        if qty is None:
            raise FlowError("CART_STATE_VERIFICATION_UNAVAILABLE", "/cart.js item missing")
        return qty

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

    def _capture_evidence(self, case_id: str) -> tuple[List[dict], Optional[str]]:
        if self.artifact_dir is None:
            return [], None
        rel = capture_case_failure(self.page, self.artifact_dir, self.viewport, case_id)
        if rel is None:
            return [], "screenshot capture failed"
        return [{"type": "screenshot", "path": rel}], None

    # -------------------------------------------------------------------- API
    def run_all(self) -> List[CaseResult]:
        # 前置：建立购物车（一次，供 QTY-01~04 连续使用）
        try:
            self._prepare_cart()
        except FlowError as exc:
            self.pre_clean_status = "FAIL"
            self.pre_clean_error = str(exc)
            # 前置失败：QTY-01 如实 FAIL，其余依赖链 BLOCKED
            ts = iso_now()
            self.results["CART-QTY-01"] = CaseResult(
                "CART-QTY-01", "Quantity Controls Available", STATUS_FAIL, ts, ts, 0,
                detail=f"cart setup failed: {exc}",
                failure_classification=exc.category,
            )
            for case_id, (name, _deps, _fn) in list(self._cases.items())[1:]:
                ts = iso_now()
                self.results[case_id] = CaseResult(
                    case_id, name, STATUS_BLOCKED, ts, ts, 0,
                    detail="prerequisite not PASS: CART-QTY-01",
                    failure_classification="BLOCKED_BY_DEPENDENCY",
                    blocked_by=["CART-QTY-01"],
                )
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

    # ------------------------------------------------------------ Case 实现
    def _case_qty01(self) -> str:
        """Quantity Controls Available：抽屉内 1 个商品，初始数量 1，+/ - 控件可用。"""
        drawer = self.state["drawer"]
        try:
            drawer.wait_open()
            drawer.wait_item()
        except Exception as exc:
            raise FlowError(
                "CART_QUANTITY_CONTROLS_NOT_AVAILABLE",
                f"drawer/item not ready: {type(exc).__name__}: {exc}",
            ) from exc
        if drawer.item_count() != 1:
            raise FlowError(
                "CART_QUANTITY_CONTROLS_NOT_AVAILABLE", f"item_count={drawer.item_count()}"
            )
        initial = drawer.get_item_quantity(0)
        if initial != "1":
            raise FlowError(
                "CART_QUANTITY_CONTROLS_NOT_AVAILABLE", f"initial_quantity={initial!r}"
            )
        plus = drawer.quantity_increase_button(0)
        if not (plus.count() and plus.is_visible() and plus.is_enabled()):
            raise FlowError("CART_QUANTITY_CONTROLS_NOT_AVAILABLE", "plus button unavailable")
        minus = drawer.quantity_decrease_button(0)
        if not minus.count():
            raise FlowError("CART_QUANTITY_CONTROLS_NOT_AVAILABLE", "minus button missing")
        minus_state = "disabled" if minus.is_disabled() else "enabled"
        self.state["subtotal_before"] = drawer.get_subtotal()
        return (
            f"interaction_mode={INTERACTION_MODE} initial_quantity={initial} "
            f"plus_available=True minus_state={minus_state}"
        )

    def _case_qty02(self) -> str:
        """Quantity Increase：真实点击 +，UI 数量 1 → 2，后端状态确认。"""
        drawer = self.state["drawer"]
        try:
            drawer.increase_quantity(0)
            drawer.wait_quantity(0, 2)
        except RuntimeError as exc:
            raise FlowError("CART_QUANTITY_INCREASE_FAILURE", str(exc)) from exc
        except TimeoutError as exc:
            raise FlowError("CART_QUANTITY_UPDATE_TIMEOUT", str(exc)) from exc
        backend = self._backend_quantity()
        if backend != "2":
            raise FlowError(
                "CART_QUANTITY_STATE_MISMATCH", f"ui=2 backend={backend}"
            )
        self.state["subtotal_after_increase"] = drawer.get_subtotal()
        return f"before=1 after=2 update_state=confirmed backend={backend}"

    def _case_qty03(self) -> str:
        """Quantity State Consistency：UI 数量与后端购物车状态一致（含 Subtotal 辅助观察）。"""
        drawer = self.state["drawer"]
        ui_qty = drawer.get_item_quantity(0)
        if ui_qty != "2":
            raise FlowError(
                "CART_QUANTITY_STATE_MISMATCH", f"ui={ui_qty!r} expected=2"
            )
        backend = self._backend_quantity()
        if backend != "2":
            raise FlowError(
                "CART_QUANTITY_STATE_MISMATCH", f"ui={ui_qty} backend={backend}"
            )
        before = self.state.get("subtotal_before")
        after = drawer.get_subtotal()
        changed = bool(before and after and before != after)
        return (
            f"ui_quantity={ui_qty} backend_quantity={backend} match=True "
            f"subtotal_before={before} subtotal_after={after} subtotal_changed={changed}"
        )

    def _case_qty04(self) -> str:
        """Quantity Decrease：真实点击 -，UI 数量 2 → 1，后端状态确认。"""
        drawer = self.state["drawer"]
        try:
            drawer.decrease_quantity(0)
            drawer.wait_quantity(0, 1)
        except RuntimeError as exc:
            raise FlowError("CART_QUANTITY_DECREASE_FAILURE", str(exc)) from exc
        except TimeoutError as exc:
            raise FlowError("CART_QUANTITY_UPDATE_TIMEOUT", str(exc)) from exc
        backend = self._backend_quantity()
        if backend != "1":
            raise FlowError(
                "CART_QUANTITY_STATE_MISMATCH", f"ui=1 backend={backend}"
            )
        return f"before=2 after=1 update_state=confirmed backend={backend}"
