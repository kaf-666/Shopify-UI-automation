"""Checkout Entry Cases：从购物车抽屉经标准 Checkout 进入 Shopify Checkout。

稳定 Case ID（本阶段后冻结，作为未来 Website Smoke 的可复用接口）：

    CHECKOUT-01  Checkout Control Available
    CHECKOUT-02  Checkout Entry
    CHECKOUT-03  Checkout Core Available
    CHECKOUT-04  Cart Checkout Consistency

依赖模型（顺序业务链）：
    CHECKOUT-01 -> CHECKOUT-02 -> CHECKOUT-03 -> CHECKOUT-04

每 viewport 只建立一次 1-item 购物车（quantity=1，最简状态），
随后连续执行 4 条 Case；进入 Checkout 后不再返回购物车。

安全边界：只验证 Checkout 入口与页面可用性；绝不填写字段、
不点击快捷支付 / 支付 / 提交订单控件，本阶段不产生任何订单。

Checkout 模型（真实探测）：REDIRECT_CHECKOUT
    标准按钮（button[name=checkout]）-> 同标签页重定向 ->
    同站 /checkouts/cn/<token>/en-us（Shopify hosted checkout）。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import urlparse

from flows.shopping_flow import FlowError, ShoppingFlow, titles_match
from pages.cart_drawer import CartDrawer
from pages.checkout_page import CheckoutPage
from utils.result import CaseResult, iso_now
from utils.screenshots import capture_case_failure

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_BLOCKED = "BLOCKED"

ENTRY_MODE = "REDIRECT_CHECKOUT"


class CheckoutCaseRunner:
    """针对单个 BrowserRuntime 执行 4 个 Checkout Entry Cases。"""

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

        self._cases: Dict[str, tuple] = {
            "CHECKOUT-01": ("Checkout Control Available", [], self._case_c01),
            "CHECKOUT-02": ("Checkout Entry", ["CHECKOUT-01"], self._case_c02),
            "CHECKOUT-03": ("Checkout Core Available", ["CHECKOUT-02"], self._case_c03),
            "CHECKOUT-04": ("Cart Checkout Consistency", ["CHECKOUT-03"], self._case_c04),
        }

    # ------------------------------------------------------------------- 辅助
    def _drawer(self) -> CartDrawer:
        return CartDrawer(self.page, self.site_config, self.viewport)

    def _checkout(self) -> CheckoutPage:
        return CheckoutPage(self.page, self.site_config, self.viewport)

    def _prepare_cart(self) -> None:
        """建立购物车前置状态：清空 -> 选品 -> 颜色/尺码 -> 加购 -> 抽屉就绪。

        复用 ShoppingFlow 已验证步骤；quantity 保持 1（最简 Checkout 状态）。
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
        try:
            self._prepare_cart()
        except FlowError as exc:
            self.pre_clean_status = "FAIL"
            self.pre_clean_error = str(exc)
            ts = iso_now()
            self.results["CHECKOUT-01"] = CaseResult(
                "CHECKOUT-01", "Checkout Control Available", STATUS_FAIL, ts, ts, 0,
                detail=f"cart setup failed: {exc}",
                failure_classification=exc.category,
            )
            for case_id, (name, _deps, _fn) in list(self._cases.items())[1:]:
                ts = iso_now()
                self.results[case_id] = CaseResult(
                    case_id, name, STATUS_BLOCKED, ts, ts, 0,
                    detail="prerequisite not PASS: CHECKOUT-01",
                    failure_classification="BLOCKED_BY_DEPENDENCY",
                    blocked_by=["CHECKOUT-01"],
                )
            return list(self.results.values())

        for case_id, (name, deps, fn) in self._cases.items():
            self._run_case(case_id, name, deps, fn)
        return list(self.results.values())

    # ------------------------------------------------------------ Case 实现
    def _case_c01(self) -> str:
        """Checkout Control Available：标准 Checkout 控件存在、可见、可用。"""
        drawer = self.state["drawer"]
        try:
            drawer.wait_open()
            drawer.wait_item()
        except Exception as exc:
            raise FlowError(
                "CHECKOUT_CONTROL_NOT_AVAILABLE",
                f"drawer/item not ready: {type(exc).__name__}: {exc}",
            ) from exc
        if drawer.item_count() != 1 or drawer.get_item_quantity(0) != "1":
            raise FlowError(
                "CHECKOUT_CONTROL_NOT_AVAILABLE",
                f"item_count={drawer.item_count()} qty={drawer.get_item_quantity(0)!r}",
            )
        btn = drawer.checkout_button()
        if not (btn.count() and btn.is_visible() and btn.is_enabled()):
            raise FlowError("CHECKOUT_STANDARD_CONTROL_NOT_FOUND", "standard checkout button missing")
        element_type = btn.evaluate("el => el.tagName.toLowerCase()")
        text = " ".join(btn.inner_text().split())[:40]
        # 快捷支付仅观察：抽屉内无独立快捷支付按钮（Shopify 快捷支付在 Checkout 页）
        express_present = False
        drawer_text = " ".join(drawer.drawer().inner_text().split()).lower()
        express_present = any(m in drawer_text for m in ("shop pay", "paypal", "buy now"))
        return (
            f"element_type={element_type} text={text!r} "
            f"express_checkout_present={express_present}"
        )

    def _case_c02(self) -> str:
        """Checkout Entry：真实点击标准 Checkout 按钮并进入 Checkout 上下文。"""
        drawer = self.state["drawer"]
        checkout = self._checkout()
        try:
            drawer.checkout()
            checkout.wait_checkout_context()
        except RuntimeError as exc:
            raise FlowError("CHECKOUT_NAVIGATION_FAILURE", str(exc)) from exc
        except TimeoutError as exc:
            raise FlowError("CHECKOUT_CONTEXT_NOT_REACHED", str(exc)) from exc
        url = checkout.current_url()
        parsed = urlparse(url)
        self.state["checkout_url"] = url
        self.state["checkout_host"] = parsed.hostname or ""
        self.state["checkout_path"] = parsed.path
        return (
            f"entry_mode={ENTRY_MODE} host={parsed.hostname} "
            f"path_pattern={parsed.path[:40]} checkout_root=True"
        )

    def _case_c03(self) -> str:
        """Checkout Core Available：Checkout 页面具备继续下单所需基础 UI。"""
        checkout = self._checkout()
        try:
            checkout.wait_checkout_context()
            checkout.wait_checkout_settled()
        except TimeoutError as exc:
            raise FlowError("CHECKOUT_CONTEXT_NOT_REACHED", str(exc)) from exc
        sections = checkout.core_sections_present()
        if not sections["root"]:
            raise FlowError("CHECKOUT_CORE_NOT_AVAILABLE", "checkout root missing")
        if not checkout.has_order_summary():
            raise FlowError(
                "CHECKOUT_ORDER_SUMMARY_NOT_AVAILABLE", "order summary not found"
            )
        if not (sections["contact"] or sections["delivery"] or sections["email"]):
            raise FlowError(
                "CHECKOUT_CORE_NOT_AVAILABLE",
                f"no contact/delivery section: {sections}",
            )
        self.state["checkout"] = checkout
        return (
            f"root=True order_summary=True contact={sections['contact']} "
            f"delivery={sections['delivery']} email={sections['email']}"
        )

    def _case_c04(self) -> str:
        """Cart Checkout Consistency：购物车商品进入 Checkout 后身份与数量一致。"""
        checkout = self.state["checkout"]
        try:
            checkout.wait_checkout_settled()
            checkout.ensure_order_summary_visible()
        except Exception as exc:
            raise FlowError(
                "CHECKOUT_ORDER_SUMMARY_NOT_AVAILABLE",
                f"{type(exc).__name__}: {exc}",
            ) from exc

        # P0：商品身份
        checkout_title = checkout.get_product_title(0)
        pdp_title = self.flow.state["pdp_title"]
        if not checkout_title:
            raise FlowError("CHECKOUT_PRODUCT_MISMATCH", "checkout product title unreadable")
        if not titles_match(pdp_title, checkout_title):
            raise FlowError(
                "CHECKOUT_PRODUCT_MISMATCH",
                f"pdp={pdp_title[:50]!r} checkout={checkout_title[:50]!r}",
            )

        # P0：数量（显式单元格优先；单件且无显式数量时记录 IMPLICIT_SINGLE_ITEM）
        qty = checkout.get_product_quantity(0)
        qty_detail = qty
        if qty and qty != self.flow.state["cart_qty"]:
            raise FlowError(
                "CHECKOUT_QUANTITY_MISMATCH",
                f"cart_qty={self.flow.state['cart_qty']} checkout_qty={qty}",
            )
        if not qty:
            if checkout.product_count_readable() != 1:
                raise FlowError(
                    "CHECKOUT_QUANTITY_MISMATCH",
                    "checkout quantity unreadable and product count != 1",
                )
            qty_detail = "IMPLICIT_SINGLE_ITEM"

        # P1：变体（颜色 / 尺码，按真实 DOM 文本包含判断）
        variant = checkout.get_product_variant(0)
        color_ok = self.flow.state["color"].casefold() in variant.casefold()
        size_ok = self.flow.state["size"].casefold() in variant.casefold()
        if not (color_ok and size_ok):
            raise FlowError(
                "CHECKOUT_VARIANT_MISMATCH",
                f"variant={variant[:60]!r} color={self.flow.state['color']!r} "
                f"size={self.flow.state['size']!r}",
            )

        # Observation：价格 / Subtotal（不作断言）
        price = checkout.get_product_price(0)
        subtotal = checkout.get_subtotal()
        return (
            f"product_match=True quantity={qty_detail} color_match={color_ok} "
            f"size_match={size_ok} price={price} subtotal={subtotal}"
        )
