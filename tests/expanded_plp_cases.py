"""扩展 PLP Cases：颜色快捷筛选 + 排序。

稳定 Case ID（本阶段后冻结）：

    PLP-COLOR-01  Color Options Available
    PLP-COLOR-02  Color Filter Apply
    PLP-COLOR-03  Color Selected State
    PLP-COLOR-04  Color Behavior Consistency
    PLP-COLOR-05  Color Filter Clear / Toggle
    PLP-SORT-01   Sort Option Apply
    PLP-SORT-02   Restore Featured

依赖模型：
    COLOR-01 -> COLOR-02 -> {COLOR-03, COLOR-05}
    COLOR-01 -> COLOR-04
    SORT-01 -> SORT-02
    颜色链与排序链完全独立。

颜色模型：颜色族存在 EXPANDABLE（展开二级颜色）与 DIRECT（直接筛选）
两种合法模式，PLP-COLOR-04 验证的是每个族都能按自身模式完成有效筛选。

这些 Case 会如实暴露站点行为；站点层面的 FAIL 是有效发现，
自动化不得将其改判为 PASS。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from flows.shopping_flow import FlowError
from pages.collection_page import CollectionPage
from utils.result import CaseResult, iso_now
from utils.screenshots import capture_case_failure

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_BLOCKED = "BLOCKED"

BASE_COLLECTION = "/collections/wedding-guest-dresses"
SORT_TARGET = "title-ascending"
SORT_DEFAULT = "manual"


class PlpCaseRunner:
    """针对单个 BrowserRuntime 执行 7 个扩展 PLP Cases。"""

    def __init__(self, runtime, site_config: dict, viewport: str, artifact_dir: Optional[Path] = None):
        self.runtime = runtime
        self.viewport = viewport
        self.artifact_dir = Path(artifact_dir) if artifact_dir else None
        self.page = runtime.page
        self.site_config = site_config
        self.state: dict = {}
        self.results: Dict[str, CaseResult] = {}

        self._cases: Dict[str, tuple] = {
            "PLP-COLOR-01": ("Color Options Available", [], self._case_color01),
            "PLP-COLOR-02": ("Color Filter Apply", ["PLP-COLOR-01"], self._case_color02),
            "PLP-COLOR-03": ("Color Selected State", ["PLP-COLOR-02"], self._case_color03),
            "PLP-COLOR-04": ("Color Behavior Consistency", ["PLP-COLOR-01"], self._case_color04),
            "PLP-COLOR-05": ("Color Filter Clear / Toggle", ["PLP-COLOR-02"], self._case_color05),
            "PLP-SORT-01": ("Sort Option Apply", [], self._case_sort01),
            "PLP-SORT-02": ("Restore Featured", ["PLP-SORT-01"], self._case_sort02),
        }

    # ------------------------------------------------------------------- 辅助方法
    def _coll(self) -> CollectionPage:
        return CollectionPage(self.page, self.site_config, self.viewport)

    def _open_base(self) -> CollectionPage:
        coll = self._coll()
        coll.open()  # page_url() has no query -> clean base collection
        return coll

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

    # ------------------------------------------------------------ 颜色 Case
    def _case_color01(self) -> str:
        coll = self._open_base()
        families = coll.color_families()
        names = coll.color_family_names()
        if len(families) < 2:
            raise FlowError("COLOR_OPTIONS_UNAVAILABLE", f"family count={len(families)}")
        if any(not n for n in names) or len(set(names)) != len(names):
            raise FlowError("COLOR_OPTIONS_UNAVAILABLE", f"names invalid: {names}")
        coll.expand_color_family(families[0])  # proves at least one is interactive
        self.state["color_families"] = families
        return f"count={len(families)} colors={','.join(names[:5])}"

    def _case_color02(self) -> str:
        coll = self._open_base()
        before_ids = coll.product_identifiers(10)
        self.state["color_base_ids"] = before_ids
        families = self.state["color_families"]
        tested = families[0]
        result = coll.apply_color_filter(tested)
        self.state["tested_color"] = tested
        behavior = result["behavior"]
        if behavior == "COLOR_LANDING_PAGE_NAVIGATION":
            raise FlowError(
                "COLOR_FILTER_UNEXPECTED_NAVIGATION",
                f"tested={tested} after_url={self.page.url[:120]}",
            )
        if behavior == "NO_EFFECT":
            raise FlowError("COLOR_FILTER_NO_EFFECT", f"tested={tested}")
        return f"tested={tested} behavior={behavior}"

    def _case_color03(self) -> str:
        coll = self._coll()  # still on the filtered page from COLOR-02
        tested = self.state["tested_color"]
        url_selected = coll.get_selected_color_filter()
        mech = coll.selected_state_mechanisms(tested)
        if url_selected != tested:
            raise FlowError(
                "COLOR_SELECTED_STATE_MISSING",
                f"url_selected={url_selected} expected={tested}",
            )
        visible_signal = (
            mech["family_active"]
            or mech["selected_box_visible"]
            or mech["aria_selected_options"] > 0
        )
        if not visible_signal:
            raise FlowError(
                "COLOR_SELECTED_STATE_MISSING",
                f"no visible selected state: family_active={mech['family_active']} "
                f"selected_box_visible={mech['selected_box_visible']} "
                f"aria_selected_options={mech['aria_selected_options']}",
            )
        return f"tested={tested} mechanism=url+visible"

    def _case_color04(self) -> str:
        """验证所有颜色族都能按自身合法交互模式完成有效筛选。

        允许 EXPANDABLE 与 DIRECT 两种模式并存；
        只要求每个族最终都能产生有效筛选（URL 携带 filter 参数）。
        """
        families = self.state["color_families"]
        matrix = {}
        for fam in families:
            coll = self._open_base()  # 每个族从干净的基础页开始，避免状态互相污染
            result = coll.apply_color_filter(fam)
            matrix[fam] = {"mode": result["mode"], "behavior": result["behavior"]}
        self.state["color_behavior_matrix"] = matrix
        no_effect = [k for k, v in matrix.items() if v["behavior"] == "NO_EFFECT"]
        other_bad = [
            k for k, v in matrix.items()
            if v["behavior"] not in ("SAME_COLLECTION_FILTER", "NO_EFFECT")
        ]
        if no_effect:
            raise FlowError("COLOR_FILTER_NO_EFFECT", f"no_effect_colors={no_effect}")
        if other_bad:
            raise FlowError("COLOR_FILTER_NAVIGATION_ERROR", f"error_colors={other_bad}")
        return f"consistent={len(families)}/{len(families)}"

    def _case_color05(self) -> str:
        coll = self._coll()
        tested = self.state["tested_color"]
        # 确保被测筛选确实生效（前面的 Case 可能已导航离开，例如 COLOR-04 的全量扫描）。
        if coll.get_selected_color_filter() != tested:
            coll = self._open_base()
            coll.apply_color_filter(tested)

        # 1) 切换尝试：再次点击同一个具体颜色
        toggle_failed = False
        try:
            coll.expand_color_family(tested)
            encoded = tested.replace(" ", "+")
            opt = self.page.locator(
                f'a.option_circle[href*="filter.v.option.color={encoded}"]'
            ).filter(visible=True).first
            if opt.count():
                opt.click()
                coll._wait_collection_stable()
                if coll.get_selected_color_filter() is None:
                    return "toggle-off (clicking the same color removed the filter)"
                toggle_failed = True
        except Exception as exc:  # noqa: BLE001
            toggle_failed = True

        # 2) 查找显式清除 / 重置控件
        clear_candidates = [
            'a[href*="clear"]',
            '[class*="filter-clear"]',
            '[class*="clear-filter"]',
            'button:has-text("Clear")',
            'a:has-text("Clear all")',
            'a:has-text("清除")',
        ]
        for sel in clear_candidates:
            loc = self.page.locator(sel).first
            if loc.count() and loc.is_visible():
                loc.click()
                coll._wait_collection_stable()
                if coll.get_selected_color_filter() is None:
                    return f"clear-control ({sel})"
        raise FlowError(
            "COLOR_FILTER_CLEAR_UNAVAILABLE",
            f"no UI clear path; toggle_failed={toggle_failed} "
            f"still_filtered={coll.get_selected_color_filter()}",
        )

    # ------------------------------------------------------------- 排序 Case
    def _case_sort01(self) -> str:
        coll = self._open_base()
        before_ids = coll.product_identifiers(10)
        self.state["sort_base_ids"] = before_ids
        opts = coll.sort_options()
        values = [o["value"] for o in opts]
        if SORT_TARGET not in values:
            raise FlowError("SORT_CONTROL_FAILURE", f"title-ascending unavailable: {values}")
        coll.select_sort(SORT_TARGET)

        # 1) 控件状态
        if coll.get_current_sort() != SORT_TARGET:
            raise FlowError(
                "SORT_STATE_MISMATCH",
                f"select={coll.get_current_sort()} expected={SORT_TARGET}",
            )
        # 2) URL 状态
        if f"sort_by={SORT_TARGET}" not in self.page.url:
            raise FlowError("SORT_URL_MISMATCH", f"url={self.page.url[:120]}")
        # 3) 商品顺序变化
        after_ids = coll.product_identifiers(10)
        if after_ids == before_ids:
            raise FlowError("SORT_PRODUCT_ORDER_UNCHANGED", "ids identical after sort")
        # 4) title-ascending 的 A→Z 顺序加强验证
        if not coll.product_order_ascending(10):
            raise FlowError("SORT_ORDER_INCORRECT", f"titles not ascending: {coll.product_titles(4)}")
        return f"selected={SORT_TARGET}"

    def _case_sort02(self) -> str:
        coll = self._coll()  # still sorted from SORT-01
        base_ids = self.state["sort_base_ids"]
        coll.select_sort(SORT_DEFAULT)
        if coll.get_current_sort() != SORT_DEFAULT:
            raise FlowError(
                "SORT_DEFAULT_RESTORE_FAILURE",
                f"select={coll.get_current_sort()} expected={SORT_DEFAULT}",
            )
        after_ids = coll.product_identifiers(10)
        if after_ids != base_ids:
            raise FlowError("SORT_DEFAULT_RESTORE_FAILURE", "product order not restored")
        obs = "EXPLICIT_MANUAL" if "sort_by=manual" in self.page.url else "CANONICAL"
        self.state["default_sort_observation"] = obs
        return f"default_sort_query_state={obs}"
