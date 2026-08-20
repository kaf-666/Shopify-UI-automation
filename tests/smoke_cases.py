"""正式化 Mondressy Smoke Cases。

稳定 Case 注册表（Case ID 是后续阶段的冻结接口）：

    SMOKE-PLP-01  Product List Available
    SMOKE-PLP-02  Product Card Opens PDP
    SMOKE-PDP-01  Purchase Area Available
    SMOKE-PDP-02  Color Selection
    SMOKE-PDP-03  Size Selection
    SMOKE-PDP-04  Add To Cart + Cart Drawer Open
    SMOKE-CART-01 Variant Consistency
    SMOKE-CART-02 Remove + Empty State

依赖模型：
    PLP-01 -> PLP-02 -> PDP-01 -> (PDP-02, PDP-03) -> PDP-04 -> (CART-01, CART-02)
    CART-02 不依赖 CART-01（变体不一致仍要执行移除）。
    PDP-03 不依赖 PDP-02（尺码与颜色相互独立）。

本模块只负责 Case 定义 / 依赖 / 状态统计；
浏览器、代理、站点访问与 DOM 操作由 BrowserRuntime + ShoppingFlow
与 Page Object 负责。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from flows.shopping_flow import FlowError, ShoppingFlow
from utils.result import CaseResult, iso_now
from utils.screenshots import capture_case_failure

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_BLOCKED = "BLOCKED"

PRECONDITION_CART_CLEAN_FAILURE = "PRECONDITION_CART_CLEAN_FAILURE"
BLOCKED_BY_DEPENDENCY = "BLOCKED_BY_DEPENDENCY"

CART_JS = "https://mondressy.com/cart.js"


class SmokeCaseRunner:
    """针对单个 BrowserRuntime 执行 8 个固定 Smoke Cases。"""

    def __init__(self, runtime, site_config: dict, viewport: str, artifact_dir: Optional[Path] = None):
        self.runtime = runtime
        self.viewport = viewport
        self.artifact_dir = Path(artifact_dir) if artifact_dir else None
        self.flow = ShoppingFlow(
            runtime.page, site_config, viewport, access_policy=runtime.access_policy
        )
        self.results: Dict[str, CaseResult] = {}
        self.pre_clean_status = "PASS"
        self.cleanup_status = "PASS"
        self.pre_clean_error = ""
        self.residual_items_before = 0

        self._cases: Dict[str, tuple] = {
            "SMOKE-PLP-01": ("Product List Available", [], self._case_plp01),
            "SMOKE-PLP-02": ("Product Card Opens PDP", ["SMOKE-PLP-01"], self._case_plp02),
            "SMOKE-PDP-01": ("Purchase Area Available", ["SMOKE-PLP-02"], self._case_pdp01),
            "SMOKE-PDP-02": ("Color Selection", ["SMOKE-PDP-01"], self._case_pdp02),
            "SMOKE-PDP-03": ("Size Selection", ["SMOKE-PDP-01"], self._case_pdp03),
            "SMOKE-PDP-04": (
                "Add To Cart + Cart Drawer Open",
                ["SMOKE-PDP-02", "SMOKE-PDP-03"],
                self._case_pdp04,
            ),
            "SMOKE-CART-01": ("Variant Consistency", ["SMOKE-PDP-04"], self._case_cart01),
            "SMOKE-CART-02": ("Remove + Empty State", ["SMOKE-PDP-04"], self._case_cart02),
        }

    # ------------------------------------------------------------------ API
    def run_all(self) -> List[CaseResult]:
        self.residual_items_before = self._cart_item_count()
        try:
            self.flow.pre_clean_cart()
            self.pre_clean_status = "PASS"
        except Exception as exc:
            self.pre_clean_status = "FAIL"
            self.pre_clean_error = f"{type(exc).__name__}: {exc}"
            ts = iso_now()
            for case_id, (name, _deps, _fn) in self._cases.items():
                self.results[case_id] = CaseResult(
                    case_id, name, STATUS_BLOCKED, ts, ts, 0,
                    detail=PRECONDITION_CART_CLEAN_FAILURE,
                    failure_classification=BLOCKED_BY_DEPENDENCY,
                    blocked_by=["pre-clean"],
                )
            return list(self.results.values())

        for case_id, (name, deps, fn) in self._cases.items():
            self._run_case(case_id, name, deps, fn)

        try:
            self.flow.cleanup_cart()
            self.cleanup_status = "PASS"
        except Exception:
            self.cleanup_status = "FAIL"
        return list(self.results.values())

    # -------------------------------------------------------------- 执行分发
    def _run_case(self, case_id: str, name: str, deps: List[str], fn: Callable) -> None:
        for dep in deps:
            dep_result = self.results.get(dep)
            if dep_result is None or dep_result.status != STATUS_PASS:
                ts = iso_now()
                self.results[case_id] = CaseResult(
                    case_id, name, STATUS_BLOCKED, ts, ts, 0,
                    detail=f"prerequisite not PASS: {dep}",
                    failure_classification=BLOCKED_BY_DEPENDENCY,
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
        """FAIL-only screenshot; never overrides the case failure."""
        if self.artifact_dir is None:
            return [], None
        rel = capture_case_failure(self.runtime.page, self.artifact_dir, self.viewport, case_id)
        if rel is None:
            return [], "screenshot capture failed"
        return [{"type": "screenshot", "path": rel}], None

    def _cart_item_count(self) -> int:
        policy = self.runtime.access_policy
        headers = policy.request_headers(CART_JS) if policy else {}
        try:
            resp = self.runtime.context.request.get(CART_JS, headers=headers, timeout=15000)
            if resp.status == 200:
                return int(resp.json().get("item_count", 0))
        except Exception:
            pass
        return -1

    # --------------------------------------------------------------- Case 定义
    def _case_plp01(self) -> str:
        total = self.flow.open_collection()
        grid = self.flow.state["coll"].product_grid()
        if not grid.is_visible():
            raise FlowError("PLP_PRODUCT_LIST_FAILURE", "product_grid not visible")
        if total <= 0:
            raise FlowError("PLP_PRODUCT_LIST_FAILURE", f"product_count={total}")
        return f"product_count={total}"

    def _case_plp02(self) -> str:
        index, prod = self.flow.open_product()
        url = self.runtime.page.url
        title = prod.get_title()
        if "/products/" not in url or not title:
            raise FlowError("PLP_PRODUCT_OPEN_FAILURE", f"url={url[:100]} title_empty={not title}")
        return f"index={index}"

    def _case_pdp01(self) -> str:
        info = self.flow.validate_purchase_area()
        return f"colors={info['colors']} sizes={info['sizes']} atc=true"

    def _case_pdp02(self) -> str:
        color = self.flow.select_color()
        checked = self.flow.state["prod"].get_selected_color()
        if color != checked:
            raise FlowError(
                "COLOR_SELECTION_FAILURE",
                f"Target: {color} Observed checked: {checked}",
            )
        return f"selected={color}"

    def _case_pdp03(self) -> str:
        size = self.flow.select_size()
        checked = self.flow.state["prod"].get_selected_size()
        if size != checked:
            raise FlowError(
                "SIZE_SELECTION_FAILURE",
                f"Target: {size} Observed checked: {checked}",
            )
        return f"selected={size}"

    def _case_pdp04(self) -> str:
        self.flow.capture_pdp_state()
        self.flow.add_to_cart()
        self.flow.capture_cart_state()
        return f"items={self.flow.state['item_count']}"

    def _case_cart01(self) -> str:
        checks = self.flow.validate_variant()
        parts = []
        for key in ("product", "color", "size"):
            parts.append(f"{key}={'match' if checks[key] else 'mismatch'}")
        parts.append(f"qty={self.flow.state['cart_qty']}")
        return " ".join(parts)

    def _case_cart02(self) -> str:
        self.flow.remove_and_wait_empty()
        return ""
