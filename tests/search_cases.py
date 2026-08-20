"""Search Cases：商品发现入口（头部内联搜索 + /search 结果页）。

稳定 Case ID（本阶段后冻结，作为未来 Website Smoke 的可复用接口）：

    SEARCH-01  Search Trigger Opens
    SEARCH-02  Search Input Available
    SEARCH-03  Keyword Search Submit
    SEARCH-04  Search Result Opens PDP
    SEARCH-05  No Result State
    SEARCH-06  Search Clear / Close / Reset

依赖模型：
    SEARCH-01 -> SEARCH-02 -> SEARCH-03 -> SEARCH-04
    SEARCH-05 / SEARCH-06 独立重建 Search 会话
    （Positive 链失败不阻塞 No Result 与 Reset Case）。

Search 模型（真实探测）：HYBRID_SEARCH
    首页 Trigger -> 内联容器（is-active）-> 输入 -> 真实 Enter ->
    /search?q=... 结果页 -> 结果卡片（target=_blank 新标签页 PDP）。
    Predictive Search: YES（#predictive-search），Phase 8B 仅作观察。

这些 Case 如实暴露站点行为；站点层面的 FAIL 是有效发现，
自动化不得将其改判为 PASS。
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from flows.shopping_flow import FlowError
from pages.home_page import HomePage
from pages.product_page import ProductPage
from pages.search_page import SearchPage
from utils.result import CaseResult, iso_now
from utils.screenshots import capture_case_failure

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_BLOCKED = "BLOCKED"

# 正式 Positive Search 查询词：Mondressy 主营 dresses，命中率高且不绑定固定商品。
# 查询词位于 Runner（Suite 配置），不硬编码在 Page Object。
POSITIVE_QUERY = "dress"

SEARCH_MODE = "HYBRID_SEARCH"


class SearchCaseRunner:
    """针对单个 BrowserRuntime 执行 6 个 Search Cases。"""

    def __init__(self, runtime, site_config: dict, viewport: str, artifact_dir: Optional[Path] = None):
        self.runtime = runtime
        self.viewport = viewport
        self.artifact_dir = Path(artifact_dir) if artifact_dir else None
        self.page = runtime.page
        self.site_config = site_config
        self.state: dict = {}
        self.results: Dict[str, CaseResult] = {}

        self._cases: Dict[str, tuple] = {
            "SEARCH-01": ("Search Trigger Opens", [], self._case_search01),
            "SEARCH-02": ("Search Input Available", ["SEARCH-01"], self._case_search02),
            "SEARCH-03": ("Keyword Search Submit", ["SEARCH-02"], self._case_search03),
            "SEARCH-04": ("Search Result Opens PDP", ["SEARCH-03"], self._case_search04),
            "SEARCH-05": ("No Result State", [], self._case_search05),
            "SEARCH-06": ("Search Clear / Close / Reset", [], self._case_search06),
        }

    # ------------------------------------------------------------------- 辅助
    def _search(self) -> SearchPage:
        return SearchPage(self.page, self.site_config, self.viewport)

    def _open_home_search(self) -> SearchPage:
        """重建 Search 会话：从首页重新打开（SEARCH-05/06 独立会话入口）。"""
        search = self._search()
        search.open_from_home()
        return search

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

    @staticmethod
    def _no_result_query() -> str:
        """生成低碰撞随机查询：仅字母 / 数字 / 短横线。"""
        return "qa-no-result-" + datetime.now().strftime("%Y%m%d-%H%M%S")

    # -------------------------------------------------------------------- API
    def run_all(self) -> List[CaseResult]:
        for case_id, (name, deps, fn) in self._cases.items():
            self._run_case(case_id, name, deps, fn)
        return list(self.results.values())

    # ------------------------------------------------------------ Case 实现
    def _case_search01(self) -> str:
        """Search Trigger Opens：首页真实点击 Trigger，Search UI 进入可用状态。"""
        try:
            search = self._open_home_search()
        except Exception as exc:
            # 首页可能被 Cloudflare 托管挑战拦截（trigger 未渲染）
            raise FlowError(
                "SEARCH_TRIGGER_FAILURE",
                f"{type(exc).__name__}: {exc} | page_title={self.page.title()[:60]!r}",
            ) from exc
        self.state["search_mode"] = SEARCH_MODE
        return f"search_mode={SEARCH_MODE} container_open=True"

    def _case_search02(self) -> str:
        """Search Input Available：输入框存在、可见、可用、可聚焦。"""
        search = self._search()
        try:
            if not search.is_open():
                search.open_from_home()  # 防御：依赖链中断时重建会话
            search.validate_input_usable()
        except RuntimeError as exc:
            raise FlowError("SEARCH_INPUT_NOT_AVAILABLE", str(exc)) from exc
        except Exception as exc:
            raise FlowError(
                "SEARCH_INPUT_NOT_AVAILABLE", f"{type(exc).__name__}: {exc}"
            ) from exc
        inp = search.input()
        return (
            f"input_type={inp.get_attribute('type')} "
            f"placeholder={inp.get_attribute('placeholder')}"
        )

    def _case_search03(self) -> str:
        """Keyword Search Submit：输入 dress 后真实 Enter 提交。

        PASS 条件：URL 携带一致 q 参数，且出现正式 Search Results（result_count > 0）。
        Predictive 商品结果仅作观察记录。
        """
        search = self._search()
        if not search.is_open():
            search.open_from_home()  # 防御：依赖链中断时重建会话
        search.fill_query(POSITIVE_QUERY)
        search.wait_predictive_ready()
        predictive_count = search.predictive_product_cards().count()
        try:
            recovered = search.submit_query()
        except TimeoutError as exc:
            raise FlowError("SEARCH_SUBMIT_FAILURE", str(exc)) from exc
        query = search.current_query()
        if query != POSITIVE_QUERY:
            raise FlowError(
                "SEARCH_QUERY_STATE_MISMATCH",
                f"url_q={query} expected={POSITIVE_QUERY}",
            )
        count = search.result_count()
        if count <= 0:
            raise FlowError("SEARCH_RESULTS_NOT_FOUND", f"result_count={count}")
        self.state["positive_count"] = count
        self.state["positive_url"] = self.page.url
        self.state["predictive_count"] = predictive_count
        self.state["submit_recovered"] = recovered
        return (
            f"query={query} result_count={count} predictive_products={predictive_count} "
            f"recovery={recovered}"
        )

    def _case_search04(self) -> str:
        """Search Result Opens PDP：真实点击第一个结果卡片。

        卡片 target=_blank，PDP 在新标签页打开；
        PASS 条件：新页 URL 属于 /products/... 且 PDP 标题非空。
        """
        search = self._search()
        count = search.result_count()
        if count <= 0:
            raise FlowError("SEARCH_RESULT_OPEN_FAILURE", f"result_count={count}")
        new_page = None
        try:
            try:
                new_page = search.open_result(0)
            except Exception as exc:
                raise FlowError(
                    "SEARCH_RESULT_OPEN_FAILURE",
                    f"{type(exc).__name__}: {exc}",
                ) from exc
            if "/products/" not in new_page.url:
                raise FlowError(
                    "SEARCH_PDP_NAVIGATION_FAILURE",
                    f"url={new_page.url[:120]}",
                )
            prod = ProductPage(new_page, self.site_config, self.viewport)
            title = prod.get_title()
            if not title:
                raise FlowError("SEARCH_PDP_NAVIGATION_FAILURE", "PDP title empty")
            self.state["pdp_url"] = new_page.url
            self.state["pdp_title"] = title
            return f"url={new_page.url[:90]} title={title[:60]}"
        finally:
            if new_page is not None:
                try:
                    new_page.close()
                except Exception:  # noqa: BLE001
                    pass

    def _case_search05(self) -> str:
        """No Result State：独立重建 Search 会话，提交随机查询。

        PASS 条件：结果数量为 0，且页面提供用户可理解的空态文案。
        """
        search = self._open_home_search()
        query = self._no_result_query()
        try:
            search.fill_query(query)
            recovered = search.submit_query()
        except TimeoutError as exc:
            raise FlowError("SEARCH_SUBMIT_FAILURE", str(exc)) from exc
        count = search.result_count()
        empty = search.no_result_state()
        if count != 0:
            raise FlowError(
                "SEARCH_NO_RESULT_STATE_MISSING",
                f"result_count={count} expected=0",
            )
        if empty is None:
            raise FlowError(
                "SEARCH_NO_RESULT_STATE_MISSING",
                "no empty-state text found on results page",
            )
        self.state["no_result_query"] = query
        return f"query={query} result_count=0 empty_state={empty[:70]} recovery={recovered}"

    def _case_search06(self) -> str:
        """Search Clear / Close / Reset：验证用户退出 / 重置 Search 的真实能力。

        站点结构：无独立 Clear 控件；关闭按钮可关闭容器；
        桌面端 Escape 清空输入并关闭（完整重置），移动端仅关闭（输入保留，站点设计）。
        PASS 条件：关闭按钮与 Escape 均能关闭，重开后容器可用；
        各端重置语义作为观察如实记录在 Detail。
        """
        search = self._open_home_search()
        home = HomePage(self.page, self.site_config, self.viewport)

        # 1) 关闭按钮路径：输入 -> 关闭 -> 重开
        search.fill_query(POSITIVE_QUERY)
        try:
            search.close_button().click()
            search.wait_closed()
        except Exception as exc:
            raise FlowError("SEARCH_RESET_FAILURE", f"close button: {exc}") from exc
        home.open_search()
        try:
            search.wait_open()
        except Exception as exc:
            raise FlowError("SEARCH_RESET_FAILURE", f"reopen after close: {exc}") from exc
        value_after_reopen = search.input_value()

        # 2) Escape 路径：输入 -> Escape -> 检查容器关闭与输入值
        search.fill_query(POSITIVE_QUERY)
        try:
            search.input().press("Escape")
            search.wait_closed()
        except Exception as exc:
            raise FlowError("SEARCH_RESET_FAILURE", f"escape close: {exc}") from exc
        value_after_escape = search.input_value()

        # 3) 最终重开：输入框必须可见可用
        home.open_search()
        try:
            search.wait_open()
            search.validate_input_usable()
        except Exception as exc:
            raise FlowError("SEARCH_RESET_FAILURE", f"final reopen: {exc}") from exc

        retained = value_after_escape == POSITIVE_QUERY
        return (
            f"close_button=ok escape=ok reopen_input_usable=True "
            f"value_after_reopen={value_after_reopen!r} "
            f"value_after_escape={value_after_escape!r} "
            f"query_retained_after_escape={retained}"
        )
