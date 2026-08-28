"""Website Smoke Readonly V1 的只读 Journey 编排。

本 Runner 与 Website Smoke V1 保持独立的 Case 注册表和状态边界：
Direct / Search / Browse 各自从自己的入口开始，只读取页面能力，
不触碰购物车或 Checkout。

固定契约：11 Cases / viewport。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import urlparse

from flows.shopping_flow import FlowError
from pages.collection_page import CollectionPage
from pages.home_page import HomePage
from pages.navigation import NavigationPage
from pages.product_page import ProductPage, PurchaseAreaReadinessError
from pages.search_page import SearchPage, SearchResultNavigationError
from utils.result import CaseResult, iso_now
from utils.readonly_mutation_guard import ReadonlyMutationGuard
from utils.screenshots import capture_case_failure

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_BLOCKED = "BLOCKED"

POSITIVE_QUERY = "dress"
CF_CHALLENGE_MARKERS = ("just a moment", "cf-challenge", "attention required")

READONLY_CASE_IDS = (
    "RSMOKE-DIRECT-01",
    "RSMOKE-DIRECT-02",
    "RSMOKE-SEARCH-01",
    "RSMOKE-SEARCH-02",
    "RSMOKE-SEARCH-03",
    "RSMOKE-SEARCH-04",
    "RSMOKE-HOME-01",
    "RSMOKE-NAV-01",
    "RSMOKE-PLP-01",
    "RSMOKE-PDP-01",
    "RSMOKE-PDP-02",
)

READONLY_JOURNEY_CASES = {
    "direct": READONLY_CASE_IDS[0:2],
    "search": READONLY_CASE_IDS[2:6],
    "browse": READONLY_CASE_IDS[6:11],
}


class WebsiteSmokeReadonlyV1Runner:
    """针对单个 BrowserRuntime 顺序执行 11 个只读 Smoke Case。"""

    def __init__(
        self,
        runtime,
        site_config: dict,
        viewport: str,
        artifact_dir: Optional[Path] = None,
        mutation_guard: Optional[ReadonlyMutationGuard] = None,
    ):
        self.runtime = runtime
        self.viewport = viewport
        self.artifact_dir = Path(artifact_dir) if artifact_dir else None
        self.page = runtime.page
        self.site_config = site_config
        self.state: dict = {}
        self.results: Dict[str, CaseResult] = {}
        self._extra_pages: List = []
        self._journey = ""
        self.mutation_guard = mutation_guard
        if self.mutation_guard is not None:
            self.mutation_guard.set_scope(self.viewport, "runtime", None)

        # Dict insertion order is the public execution order of this contract.
        self._cases: Dict[str, tuple] = {
            # ------------------------------------------------------- Direct
            "RSMOKE-DIRECT-01": (
                "Direct PDP Landing",
                [],
                "direct",
                self._c_direct01,
            ),
            "RSMOKE-DIRECT-02": (
                "Direct PDP Variant + ATC Available",
                ["RSMOKE-DIRECT-01"],
                "direct",
                self._c_direct02,
            ),
            # ------------------------------------------------------- Search
            "RSMOKE-SEARCH-01": (
                "Search Entry Available",
                [],
                "search",
                self._c_search01,
            ),
            "RSMOKE-SEARCH-02": (
                "Search Returns Products",
                ["RSMOKE-SEARCH-01"],
                "search",
                self._c_search02,
            ),
            "RSMOKE-SEARCH-03": (
                "Search Result Opens PDP",
                ["RSMOKE-SEARCH-02"],
                "search",
                self._c_search03,
            ),
            "RSMOKE-SEARCH-04": (
                "Search PDP Variant + ATC Available",
                ["RSMOKE-SEARCH-03"],
                "search",
                self._c_search04,
            ),
            # ------------------------------------------------------- Browse
            "RSMOKE-HOME-01": (
                "Homepage Core Available",
                [],
                "browse",
                self._c_home01,
            ),
            "RSMOKE-NAV-01": (
                "Navigation Opens Collection",
                ["RSMOKE-HOME-01"],
                "browse",
                self._c_nav01,
            ),
            "RSMOKE-PLP-01": (
                "Collection Product Opens PDP",
                ["RSMOKE-NAV-01"],
                "browse",
                self._c_plp01,
            ),
            "RSMOKE-PDP-01": (
                "Purchase Variant Available",
                ["RSMOKE-PLP-01"],
                "browse",
                self._c_pdp01,
            ),
            "RSMOKE-PDP-02": (
                "Add To Cart Available",
                ["RSMOKE-PDP-01"],
                "browse",
                self._c_pdp02,
            ),
        }

    # ---------------------------------------------------------------- helpers
    def _cf_detected(self) -> bool:
        """按挑战页特征判断当前页面是否被 Cloudflare 托管挑战拦截。"""
        try:
            body = self.page.content().lower()[:2000]
            title = (self.page.title() or "").lower()
        except Exception:
            return False
        return any(marker in body or marker in title for marker in CF_CHALLENGE_MARKERS)

    def _capture_evidence(self, case_id: str) -> tuple[List[dict], Optional[str]]:
        if self.artifact_dir is None:
            return [], None
        rel = capture_case_failure(self.page, self.artifact_dir, self.viewport, case_id)
        if rel is not None:
            return [{"type": "screenshot", "path": rel}], None
        return [], "screenshot capture failed"

    def _adopt_business_page(self, actual_page) -> None:
        """切换到真实业务 Page；旧 Page 保留到本次 Context 结束。"""
        if actual_page is None:
            return
        try:
            if actual_page.is_closed():
                return
        except Exception:
            pass
        if actual_page is self.page:
            return
        self._extra_pages.append(self.page)
        self.page = actual_page

    def _run_case(self, case_id: str, name: str, deps: List[str], fn: Callable) -> None:
        """执行一个 Case；依赖只在当前 Journey 内生效。"""
        for dep in deps:
            dep_result = self.results.get(dep)
            if dep_result is None or dep_result.status != STATUS_PASS:
                ts = iso_now()
                self.results[case_id] = CaseResult(
                    case_id,
                    name,
                    STATUS_BLOCKED,
                    ts,
                    ts,
                    0,
                    detail=f"prerequisite not PASS: {dep}",
                    failure_classification="BLOCKED_BY_DEPENDENCY",
                    blocked_by=[dep],
                )
                return

        guard = self.mutation_guard
        violation_start = guard.violation_count() if guard is not None else 0
        if guard is not None:
            guard.set_scope(self.viewport, self._journey, case_id)
        started_ts = iso_now()
        started = time.perf_counter()
        try:
            case_result: Optional[CaseResult]
            try:
                detail = fn()
                duration = int((time.perf_counter() - started) * 1000)
                case_result = CaseResult(
                    case_id,
                    name,
                    STATUS_PASS,
                    started_ts,
                    iso_now(),
                    duration,
                    detail=detail or "",
                )
            except FlowError as exc:
                cf_now = self._cf_detected()
                duration = int((time.perf_counter() - started) * 1000)
                evidence, ev_error = self._capture_evidence(case_id)
                category = "PAGE_LOAD_FAILURE" if cf_now else exc.category
                detail = (
                    f"AUTOMATION_ACCESS: Cloudflare Stability Failure | {exc}"
                    if cf_now
                    else str(exc)
                )
                case_result = CaseResult(
                    case_id,
                    name,
                    STATUS_FAIL,
                    started_ts,
                    iso_now(),
                    duration,
                    detail=detail,
                    failure_classification=category,
                    evidence=evidence,
                    evidence_capture_error=ev_error,
                )
            except Exception as exc:  # noqa: BLE001 — case isolation is the contract
                cf_now = self._cf_detected()
                duration = int((time.perf_counter() - started) * 1000)
                evidence, ev_error = self._capture_evidence(case_id)
                category = "PAGE_LOAD_FAILURE" if cf_now else type(exc).__name__
                detail = (
                    f"AUTOMATION_ACCESS: Cloudflare Stability Failure | "
                    f"{type(exc).__name__}: {exc}"
                    if cf_now
                    else f"{type(exc).__name__}: {exc}"
                )
                case_result = CaseResult(
                    case_id,
                    name,
                    STATUS_FAIL,
                    started_ts,
                    iso_now(),
                    duration,
                    detail=detail,
                    failure_classification=category,
                    evidence=evidence,
                    evidence_capture_error=ev_error,
                )

            violations = guard.violations_since(violation_start) if guard is not None else []
            if violations:
                violation = violations[0]
                evidence = case_result.evidence
                ev_error = case_result.evidence_capture_error
                if not evidence and ev_error is None:
                    evidence, ev_error = self._capture_evidence(case_id)
                self.results[case_id] = CaseResult(
                    case_id,
                    name,
                    STATUS_FAIL,
                    started_ts,
                    iso_now(),
                    int((time.perf_counter() - started) * 1000),
                    detail=ReadonlyMutationGuard.safe_detail(violation),
                    failure_classification="READONLY_MUTATION_VIOLATION",
                    evidence=evidence,
                    evidence_capture_error=ev_error,
                )
            else:
                self.results[case_id] = case_result
        finally:
            if guard is not None:
                guard.set_scope(self.viewport, "runtime", None)

    def _journey_cases(self, journey: str) -> List[str]:
        return [case_id for case_id, (_name, _deps, group, _fn) in self._cases.items() if group == journey]

    def _run_journey(self, journey: str) -> None:
        """按注册表顺序执行 Journey；失败只阻塞其内部后继 Case。"""
        self._journey = journey
        for case_id in self._journey_cases(journey):
            name, deps, _group, fn = self._cases[case_id]
            self._run_case(case_id, name, deps, fn)

    def _close_extra_pages(self) -> None:
        for page in self._extra_pages:
            try:
                page.close()
            except Exception:
                pass
        self._extra_pages = []

    @staticmethod
    def _same_value(left: Optional[str], right: Optional[str]) -> bool:
        return bool(left and right and left.strip().casefold() == right.strip().casefold())

    def _purchase_area_ready(self, prod: ProductPage, category: str) -> tuple[str, str, int, int]:
        """等待并验证 PDP 标题、价格、购买区、选项和 ATC 状态。"""
        try:
            title = prod.get_title().strip()
            price = prod.get_price().strip()
        except Exception as exc:
            raise FlowError(category, f"title/price unreadable: {type(exc).__name__}") from exc
        if not title or not price:
            raise FlowError(category, f"title={bool(title)} price={bool(price)}")
        try:
            color_count, size_count, atc_available = prod.wait_purchase_ready()
        except PurchaseAreaReadinessError as exc:
            raise FlowError(category, str(exc)) from exc
        if color_count <= 0 or size_count <= 0 or not atc_available:
            raise FlowError(
                category,
                f"color_options={color_count} size_options={size_count} atc={atc_available}",
            )
        return title, price, color_count, size_count

    def _select_legal_variant(self, prod: ProductPage, category: str) -> tuple[str, str]:
        """选择合法 Color / Size，并在选择后重新确认 ATC 可用。"""
        try:
            color = prod.select_color()
            size = prod.select_size()
            selected_color = prod.get_selected_color()
            selected_size = prod.get_selected_size()
        except Exception as exc:
            raise FlowError(category, f"variant selection: {type(exc).__name__}: {exc}") from exc
        if not self._same_value(color, selected_color) or not self._same_value(size, selected_size):
            raise FlowError(
                category,
                f"selected color={selected_color!r} size={selected_size!r} "
                f"expected color={color!r} size={size!r}",
            )
        try:
            _colors, _sizes, atc_available = prod.wait_purchase_ready()
        except PurchaseAreaReadinessError as exc:
            raise FlowError(category, f"post-selection readiness: {exc}") from exc
        if not atc_available:
            raise FlowError(category, "Add To Cart is not enabled after legal variant selection")
        self._assert_atc_available(prod, category)
        return color, size

    def _assert_atc_available(self, prod: ProductPage, category: str) -> None:
        """只读取 ATC locator 状态，绝不触发按钮交互。"""
        try:
            button = prod.add_to_cart_button()
            located = bool(button.count())
            visible = bool(located and button.is_visible())
            enabled = bool(visible and button.is_enabled())
        except Exception as exc:
            raise FlowError(category, f"ATC locator unreadable: {type(exc).__name__}") from exc
        if not located or not visible or not enabled:
            raise FlowError(
                category,
                f"atc_locator={located} atc_visible={visible} atc_enabled={enabled}",
            )

    # ------------------------------------------------------------ Journey Direct
    def _c_direct01(self) -> str:
        """Direct PDP Landing：直达商品页并验证购买区已就绪。"""
        prod = ProductPage(self.page, self.site_config, self.viewport)
        self.state["direct_prod"] = prod
        try:
            prod.open()
        except Exception as exc:
            if self._cf_detected():
                raise FlowError(
                    "PAGE_LOAD_FAILURE",
                    "AUTOMATION_ACCESS: Cloudflare Stability Failure | direct PDP open",
                ) from exc
            raise FlowError("DIRECT_PDP_LANDING_FAILURE", f"{type(exc).__name__}: {exc}") from exc
        path = urlparse(self.page.url).path
        if not path.startswith("/products/"):
            raise FlowError("DIRECT_PDP_LANDING_FAILURE", f"url={self.page.url[:120]}")
        title, price, colors, sizes = self._purchase_area_ready(
            prod, "DIRECT_PDP_PURCHASE_AREA_NOT_AVAILABLE"
        )
        self.state["direct_title"] = title
        return (
            f"entry_mode=DIRECT product_path={path} title={title[:40]!r} price={price} "
            f"color_options={colors} size_options={sizes} atc_available=True"
        )

    def _c_direct02(self) -> str:
        """Direct PDP Variant + ATC Available：选择合法变体并读取 ATC 状态。"""
        prod = self.state["direct_prod"]
        _title, _price, colors, sizes = self._purchase_area_ready(
            prod, "DIRECT_PDP_PURCHASE_AREA_NOT_AVAILABLE"
        )
        color, size = self._select_legal_variant(
            prod, "DIRECT_PDP_VARIANT_NOT_AVAILABLE"
        )
        self.state["direct_variant"] = {"color": color, "size": size}
        return (
            f"color={color} size={size} color_options={colors} size_options={sizes} "
            "purchase_area_ready=True atc_visible=True atc_enabled=True"
        )

    # ------------------------------------------------------------ Journey Search
    def _c_search01(self) -> str:
        """Search Entry Available：首页打开 Search，输入框可用。"""
        search = SearchPage(self.page, self.site_config, self.viewport)
        try:
            search.open_from_home()
            if not search.is_open():
                raise FlowError("SEARCH_UI_NOT_OPENED", "search container not open")
            search.validate_input_usable()
        except FlowError:
            raise
        except Exception as exc:
            if self._cf_detected():
                raise FlowError(
                    "PAGE_LOAD_FAILURE",
                    "AUTOMATION_ACCESS: Cloudflare Stability Failure | search open",
                ) from exc
            raise FlowError("SEARCH_ENTRY_NOT_AVAILABLE", f"{type(exc).__name__}: {exc}") from exc
        self.state["search"] = search
        return "search_open=True input_usable=True"

    def _c_search02(self) -> str:
        """Search Returns Products：提交正向查询并确认结果。"""
        search = self.state["search"]
        try:
            recovered = search.submit_query(POSITIVE_QUERY)
            search.wait_predictive_ready()
        except Exception as exc:
            if self._cf_detected():
                raise FlowError(
                    "PAGE_LOAD_FAILURE",
                    "AUTOMATION_ACCESS: Cloudflare Stability Failure | search submit",
                ) from exc
            raise FlowError("SEARCH_SUBMIT_FAILURE", f"{type(exc).__name__}: {exc}") from exc
        count = search.result_count()
        query = search.current_query()
        if count <= 0:
            raise FlowError("SEARCH_RESULTS_NOT_FOUND", f"result_count={count}")
        if query != POSITIVE_QUERY:
            raise FlowError(
                "SEARCH_QUERY_STATE_MISMATCH",
                f"url_q={query!r} expected={POSITIVE_QUERY!r}",
            )
        self.state["search_result_count"] = count
        return f"query={POSITIVE_QUERY} result_count={count} recovery={recovered}"

    def _c_search03(self) -> str:
        """Search Result Opens PDP：接受同页或新标签页的合法 PDP。"""
        search = self.state["search"]
        try:
            product_page = search.open_result(0)
        except SearchResultNavigationError as exc:
            self._adopt_business_page(exc.actual_page)
            raise FlowError("SEARCH_RESULT_OPEN_FAILURE", str(exc)) from exc
        except Exception as exc:
            raise FlowError("SEARCH_RESULT_OPEN_FAILURE", f"{type(exc).__name__}: {exc}") from exc

        if not urlparse(product_page.url).path.startswith("/products/"):
            raise FlowError("SEARCH_PDP_NAVIGATION_FAILURE", f"url={product_page.url[:120]}")
        self._adopt_business_page(product_page)
        prod = ProductPage(product_page, self.site_config, self.viewport)
        try:
            title = prod.get_title().strip()
        except Exception as exc:
            raise FlowError(
                "SEARCH_PDP_NAVIGATION_FAILURE",
                f"title unreadable: {type(exc).__name__}",
            ) from exc
        if not title:
            raise FlowError("SEARCH_PDP_NAVIGATION_FAILURE", "title empty")
        self.state["search_prod"] = prod
        return f"url={product_page.url[:80]} title={title[:50]!r}"

    def _c_search04(self) -> str:
        """Search PDP Variant + ATC Available：只读验证搜索 PDP 购买能力。"""
        prod = self.state["search_prod"]
        _title, _price, colors, sizes = self._purchase_area_ready(
            prod, "SEARCH_PURCHASE_AREA_NOT_AVAILABLE"
        )
        color, size = self._select_legal_variant(
            prod, "SEARCH_PDP_VARIANT_NOT_AVAILABLE"
        )
        self.state["search_variant"] = {"color": color, "size": size}
        return (
            f"color={color} size={size} color_options={colors} size_options={sizes} "
            "purchase_area_ready=True atc_visible=True atc_enabled=True"
        )

    # ------------------------------------------------------------ Journey Browse
    def _c_home01(self) -> str:
        """Homepage Core Available：首页核心元素和导航入口可用。"""
        home = HomePage(self.page, self.site_config, self.viewport)
        try:
            home.open()
            home.wait_core_ready()
            nav = NavigationPage(self.page, self.site_config, self.viewport)
            nav.wait_ready()
        except Exception as exc:
            if self._cf_detected():
                raise FlowError(
                    "PAGE_LOAD_FAILURE",
                    "AUTOMATION_ACCESS: Cloudflare Stability Failure | home open",
                ) from exc
            raise FlowError("HOME_CORE_NOT_AVAILABLE", f"{type(exc).__name__}: {exc}") from exc
        self.state["nav"] = nav
        return "header_present=True logo_present=True navigation_entry=True"

    def _c_nav01(self) -> str:
        """Navigation Opens Collection：导航进入目标 Collection，网格可见。"""
        nav = self.state["nav"]
        try:
            url = nav.open_collection()
            target_path = nav.target_path()
            if urlparse(url).path != target_path:
                raise FlowError(
                    "NAVIGATION_COLLECTION_FAILURE",
                    f"url={url[:120]} expected_path={target_path}",
                )
            coll = CollectionPage(self.page, self.site_config, self.viewport)
            grid = coll.product_grid()
            grid.wait_for(state="visible", timeout=15_000)
            if not (grid.count() and grid.is_visible()):
                raise FlowError("NAVIGATION_COLLECTION_FAILURE", "product grid not visible")
        except FlowError:
            raise
        except Exception as exc:
            if self._cf_detected():
                raise FlowError(
                    "PAGE_LOAD_FAILURE",
                    "AUTOMATION_ACCESS: Cloudflare Stability Failure | navigation",
                ) from exc
            raise FlowError("NAVIGATION_COLLECTION_FAILURE", f"{type(exc).__name__}: {exc}") from exc
        self.state["coll"] = coll
        return f"mode={nav.current_mode()} path={target_path} grid=True"

    def _c_plp01(self) -> str:
        """Collection Product Opens PDP：打开第一个商品卡并读取标题。"""
        coll = self.state["coll"]
        try:
            url = coll.open_product(0)
        except Exception as exc:
            raise FlowError("PLP_PRODUCT_OPEN_FAILURE", f"{type(exc).__name__}: {exc}") from exc
        if not urlparse(url).path.startswith("/products/"):
            raise FlowError("PLP_PRODUCT_OPEN_FAILURE", f"url={url[:120]}")
        prod = ProductPage(self.page, self.site_config, self.viewport)
        try:
            title = prod.get_title().strip()
        except Exception as exc:
            raise FlowError(
                "PLP_PRODUCT_OPEN_FAILURE",
                f"title unreadable: {type(exc).__name__}",
            ) from exc
        if not title:
            raise FlowError("PLP_PRODUCT_OPEN_FAILURE", "title empty")
        self.state["browse_prod"] = prod
        return f"url={url[:80]} title={title[:50]!r}"

    def _c_pdp01(self) -> str:
        """Purchase Variant Available：购买区就绪并选择合法 Color + Size。"""
        prod = self.state["browse_prod"]
        _title, _price, colors, sizes = self._purchase_area_ready(
            prod, "PURCHASE_AREA_FAILURE"
        )
        color, size = self._select_legal_variant(prod, "VARIANT_SELECTION_FAILURE")
        self.state["browse_variant"] = {"color": color, "size": size}
        return (
            f"color={color} size={size} color_options={colors} size_options={sizes} "
            "color_state=checked size_state=checked"
        )

    def _c_pdp02(self) -> str:
        """Add To Cart Available：只验证按钮状态，不触发按钮交互。"""
        prod = self.state["browse_prod"]
        _title, _price, colors, sizes = self._purchase_area_ready(
            prod, "ATC_NOT_AVAILABLE"
        )
        selected_color = prod.get_selected_color()
        selected_size = prod.get_selected_size()
        if not selected_color or not selected_size:
            raise FlowError(
                "ATC_NOT_AVAILABLE",
                f"legal variant not selected: color={selected_color!r} size={selected_size!r}",
            )
        self._assert_atc_available(prod, "ATC_NOT_AVAILABLE")
        return (
            f"color={selected_color} size={selected_size} color_options={colors} "
            "size_options={sizes} purchase_area_ready=True atc_locator=True "
            "atc_visible=True atc_enabled=True"
        )

    # ------------------------------------------------------------------- public
    def run_all(self) -> List[CaseResult]:
        """执行 Direct -> Search -> Browse，不做购物车前置或收尾操作。"""
        self._run_journey("direct")
        self._run_journey("search")
        self._run_journey("browse")
        self._close_extra_pages()
        return [self.results[case_id] for case_id in self._cases]
