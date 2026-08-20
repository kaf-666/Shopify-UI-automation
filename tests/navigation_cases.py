"""Home + Navigation Cases：首页核心可用性与真实导航到 Collection 的发现链。

稳定 Case ID（本阶段后冻结，作为未来 Website Smoke 的可复用接口）：

    HOME-01  Homepage Core Available
    NAV-01   Primary Navigation Available
    NAV-02   Navigation Menu Opens
    NAV-03   Navigation Opens Collection
    NAV-04   Collection Product List Available

依赖模型（顺序业务链）：
    HOME-01 -> NAV-01 -> NAV-02 -> NAV-03 -> NAV-04

导航模型（真实探测）：
    Desktop : MEGA_MENU_HOVER（Gm mega menu，hover 顶层项展开子菜单）
    Mobile  : DRAWER_ACCORDION（#NavDrawer 抽屉，点击顶层项展开子菜单）

正式导航禁止 page.goto 冒充：NAV-03 必须走真实菜单点击。
这些 Case 如实暴露站点行为；站点层面的 FAIL 是有效发现，
自动化不得将其改判为 PASS。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from flows.shopping_flow import FlowError
from pages.collection_page import CollectionPage
from pages.home_page import HomePage
from pages.navigation import NavigationPage
from utils.result import CaseResult, iso_now
from utils.screenshots import capture_case_failure

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_BLOCKED = "BLOCKED"

TARGET_PATH = "/collections/wedding-guest-dresses"


class NavigationCaseRunner:
    """针对单个 BrowserRuntime 执行 5 个 Home + Navigation Cases。"""

    def __init__(self, runtime, site_config: dict, viewport: str, artifact_dir: Optional[Path] = None):
        self.runtime = runtime
        self.viewport = viewport
        self.artifact_dir = Path(artifact_dir) if artifact_dir else None
        self.page = runtime.page
        self.site_config = site_config
        self.state: dict = {}
        self.results: Dict[str, CaseResult] = {}

        self._cases: Dict[str, tuple] = {
            "HOME-01": ("Homepage Core Available", [], self._case_home01),
            "NAV-01": ("Primary Navigation Available", ["HOME-01"], self._case_nav01),
            "NAV-02": ("Navigation Menu Opens", ["NAV-01"], self._case_nav02),
            "NAV-03": ("Navigation Opens Collection", ["NAV-02"], self._case_nav03),
            "NAV-04": ("Collection Product List Available", ["NAV-03"], self._case_nav04),
        }

    # ------------------------------------------------------------------- 辅助
    def _nav(self) -> NavigationPage:
        return NavigationPage(self.page, self.site_config, self.viewport)

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
        for case_id, (name, deps, fn) in self._cases.items():
            self._run_case(case_id, name, deps, fn)
        return list(self.results.values())

    # ------------------------------------------------------------ Case 实现
    def _case_home01(self) -> str:
        """Homepage Core Available：首页加载、Header 存在、Logo 可用、主要导航入口存在。"""
        home = HomePage(self.page, self.site_config, self.viewport)
        try:
            home.open()
            # 站点抗干扰：托管挑战可能在加载后同 URL 重载页面，
            # 短暂停留后以状态轮询等待核心元素同时稳定（不重试导航）。
            self.page.wait_for_timeout(2500)
        except Exception as exc:
            raise FlowError(
                "HOME_CORE_NOT_AVAILABLE",
                f"{type(exc).__name__}: {exc} | page_title={self.page.title()[:60]!r}",
            ) from exc
        nav = self._nav()
        # 状态轮询：Header / Logo / 导航入口在重载窗口后仍须全部就绪
        deadline = time.monotonic() + 12
        header_present = logo_present = menu_ok = False
        while time.monotonic() < deadline:
            try:
                header_present = bool(nav.header().count() and nav.header().is_visible())
                logo_present = bool(home.logo().count() and home.logo().is_visible())
                if self.viewport == "desktop":
                    menu_ok = bool(nav.primary_menu().count() and nav.primary_menu().is_visible())
                else:
                    trigger = nav.menu_trigger()
                    menu_ok = bool(trigger.count() and trigger.is_visible())
            except Exception:
                header_present = logo_present = menu_ok = False
            if header_present and logo_present and menu_ok:
                break
            self.page.wait_for_timeout(400)
        if not header_present:
            raise FlowError("HOME_CORE_NOT_AVAILABLE", "header not present/visible")
        if not logo_present:
            raise FlowError("HOME_CORE_NOT_AVAILABLE", "logo not present/visible")
        if not menu_ok:
            raise FlowError("HOME_CORE_NOT_AVAILABLE", "primary navigation entry missing")
        self.state["nav"] = nav
        return f"header_present={header_present} logo_present={logo_present} navigation_entry=True"

    def _case_nav01(self) -> str:
        """Primary Navigation Available：至少一个有效商品发现入口可操作。"""
        nav = self.state["nav"]
        items = nav.primary_items()
        count = items.count()
        if count < 1:
            raise FlowError("NAVIGATION_NOT_AVAILABLE", f"primary item count={count}")
        names = nav.primary_item_names()
        if not names:
            raise FlowError("NAVIGATION_NOT_AVAILABLE", "primary items have no names")
        self.state["primary_count"] = count
        self.state["primary_names"] = names
        return f"mode={nav.current_mode()} items={count} first={names[0][:24]}"

    def _case_nav02(self) -> str:
        """Navigation Menu Opens：按当前端真实交互打开商品导航层级。"""
        nav = self.state["nav"]
        try:
            nav.open_menu()
        except TimeoutError as exc:
            raise FlowError("NAVIGATION_MENU_OPEN_FAILURE", str(exc)) from exc
        except RuntimeError as exc:
            raise FlowError("NAVIGATION_TARGET_NOT_FOUND", str(exc)) from exc
        self.state["menu_open"] = True
        return f"mode={nav.current_mode()} menu_open=True"

    def _case_nav03(self) -> str:
        """Navigation Opens Collection：真实菜单路径进入目标 Collection。"""
        nav = self.state["nav"]
        try:
            url = nav.open_collection()
        except RuntimeError as exc:
            raise FlowError("NAVIGATION_TARGET_NOT_FOUND", str(exc)) from exc
        except TimeoutError as exc:
            raise FlowError("NAVIGATION_COLLECTION_FAILURE", str(exc)) from exc
        if TARGET_PATH not in url:
            raise FlowError("NAVIGATION_COLLECTION_FAILURE", f"url={url[:120]}")
        # Collection 页面主体已加载（商品列表有效性由 NAV-04 验证）；
        # URL pathname 先于渲染完成，等待网格真实可见（状态等待，不固定 sleep）。
        coll = CollectionPage(self.page, self.site_config, self.viewport)
        try:
            grid = coll.product_grid()
            grid.wait_for(state="visible", timeout=15_000)
            grid_ok = bool(grid.count() and grid.is_visible())
        except Exception as exc:
            raise FlowError(
                "NAVIGATION_COLLECTION_FAILURE",
                f"collection page main not loaded: {type(exc).__name__}",
            ) from exc
        if not grid_ok:
            raise FlowError("NAVIGATION_COLLECTION_FAILURE", "product grid not visible")
        self.state["collection_url"] = url
        self.state["coll"] = coll
        return f"path={TARGET_PATH} url={url[:90]} grid=True"

    def _case_nav04(self) -> str:
        """Collection Product List Available：商品网格与卡片真实可用（复用 CollectionPage）。"""
        coll = self.state["coll"]
        count = coll.product_count()
        if count <= 0:
            raise FlowError("COLLECTION_PRODUCTS_NOT_AVAILABLE", f"product_count={count}")
        card = coll.product_cards().first
        link = card.locator("a.grid-product__link").first
        card_ok = bool(card.count() and card.is_visible())
        link_ok = bool(link.count() and link.is_visible() and link.is_enabled())
        if not (card_ok and link_ok):
            raise FlowError(
                "COLLECTION_PRODUCTS_NOT_AVAILABLE",
                f"card_ok={card_ok} link_ok={link_ok}",
            )
        return f"product_count={count} grid=True card_interactive=True"
