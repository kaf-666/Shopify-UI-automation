"""Website Smoke V1 入口：三条购买 Journey 的日常编排 Smoke。

用法：
    python scripts/run_website_smoke_v1.py                   # both
    python scripts/run_website_smoke_v1.py --viewport desktop
    python scripts/run_website_smoke_v1.py --viewport mobile

产物：
    artifacts/website-smoke-v1/<run_id>/results.json
    artifacts/website-smoke-v1/<run_id>/<viewport>/<CASE_ID>-failure.png

执行模型：
    每 viewport 一个 BrowserContext，顺序执行 Direct -> Search -> Browse
    Journey（15 Cases / viewport）；Browse 结束于 Checkout，Context 直接销毁。
    Desktop / Mobile 顺序执行，不并行。

退出码：0 = 30/30 全 PASS，1 = 任一 FAIL/BLOCKED，2 = 非法视口。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pages.base_page import BasePage
from tests.website_smoke_v1_cases import WebsiteSmokeV1Runner
from utils.browser import close_browser, create_browser, load_site_config, load_settings
from utils.config import resolve_url
from utils.errors import CliConfigError, sanitize_message
from utils.result import (
    ResultWriteError,
    RunResult,
    ViewportResult,
    iso_now,
    make_run_id,
    write_results_json,
)
from utils.suite_runner import guarded_main

ARTIFACT_ROOT = PROJECT_ROOT / "artifacts" / "website-smoke-v1"


def run_viewport(viewport: str, artifact_dir: Path) -> Tuple[List, WebsiteSmokeV1Runner, dict]:
    runtime = create_browser(viewport)
    try:
        runtime_meta = runtime.metadata()
        site = runtime.site_config or BasePage.load_site_config(site_name=runtime.site_name)
        runner = WebsiteSmokeV1Runner(runtime, site, viewport, artifact_dir=artifact_dir)
        results = runner.run_all()
        return results, runner, runtime_meta
    finally:
        close_browser(runtime)


def print_viewport(viewport: str, results: List, runner: Optional[WebsiteSmokeV1Runner] = None) -> None:
    print(f"[{viewport.title()} / {'Chromium' if viewport == 'desktop' else 'WebKit + iPhone 14'}]")
    counts = {"PASS": 0, "FAIL": 0, "BLOCKED": 0}
    for r in results:
        counts[r.status] += 1
        print(f"{r.case_id:<20} {r.status:<8} {r.name}")
        if r.detail:
            print(f"{'':<20} {'':<8} {sanitize_message(r.detail)}")
    print(f"{'':<20} PASS={counts['PASS']} FAIL={counts['FAIL']} BLOCKED={counts['BLOCKED']} TOTAL={len(results)}")
    if runner is not None:
        print(
            f"{'':<20} Pre-clean: {runner.pre_clean_status} | Cleanup: {runner.cleanup_status} "
            f"| Search recovery: {runner.search_recovery_used} | CF interruption: {runner.cf_interruption}"
        )
    print()


def count_statuses(results: List) -> Dict[str, int]:
    return {s: sum(1 for r in results if r.status == s) for s in ("PASS", "FAIL", "BLOCKED")}


def _write_run_result(
    artifact_dir: Path,
    run_id: str,
    site: str,
    base_url: str,
    started_at: str,
    started: float,
    viewports: List[ViewportResult],
    runtime: Optional[dict] = None,
    fatal_error: Optional[dict] = None,
) -> bool:
    """统一写正常 / partial / fatal results.json。"""
    counts = {
        "pass": sum(v.summary.get("pass", 0) for v in viewports),
        "fail": sum(v.summary.get("fail", 0) for v in viewports),
        "blocked": sum(v.summary.get("blocked", 0) for v in viewports),
    }
    total = sum(v.summary.get("total", 0) for v in viewports)
    result = RunResult(
        run_id=run_id,
        site=site,
        base_url=base_url,
        started_at=started_at,
        finished_at=iso_now(),
        duration_ms=int((time.perf_counter() - started) * 1000),
        overall_status="PASS" if not fatal_error and all(v.status == "PASS" for v in viewports) else "FAIL",
        runtime=runtime or {},
        summary={**counts, "total": total},
        viewports=viewports,
        fatal_error=fatal_error,
    )
    try:
        write_results_json(result.to_dict(), artifact_dir / "results.json")
        return True
    except ResultWriteError as exc:
        print(f"RESULT_WRITE_FAILURE: {sanitize_message(exc)}")
        return False


def _fatal_classification(exc: BaseException) -> tuple[str, int]:
    """把异常映射为 results classification 与 Exit Code。"""
    if isinstance(exc, CliConfigError):
        return getattr(exc, "category", "CONFIG_ERROR"), 2
    return "RUNTIME_ERROR", 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Mondressy Website Smoke V1")
    parser.add_argument("--viewport", choices=["desktop", "mobile", "both"], default="both")
    args = parser.parse_args(argv)  # argparse exits 2 on invalid choice

    run_id = make_run_id()
    artifact_dir = ARTIFACT_ROOT / run_id
    try:
        artifact_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"ARTIFACT_DIRECTORY_FAILURE: {sanitize_message(exc)}")
        return 2

    started_at = iso_now()
    started = time.perf_counter()
    site = ""
    base_url = ""
    vp_results: List[ViewportResult] = []
    runtime_by_viewport: Dict[str, dict] = {}

    try:
        settings = load_settings()
        site = str(settings.get("default_site") or "")
        site_cfg = load_site_config(site)
        base_url = resolve_url(site_cfg.get("base_url"), "site.base_url")
    except Exception as exc:
        classification, exit_code = _fatal_classification(exc)
        ok = _write_run_result(
            artifact_dir,
            run_id,
            site,
            base_url,
            started_at,
            started,
            [],
            fatal_error={"classification": classification, "message": sanitize_message(exc)},
        )
        print(f"FATAL_ERROR [{classification}]: {sanitize_message(exc)}")
        return exit_code if ok else exit_code

    viewports = ["desktop", "mobile"] if args.viewport == "both" else [args.viewport]

    print("=== Mondressy Website Smoke V1 ===")
    print()
    try:
        for vp in viewports:
            vp_started_ts = iso_now()
            vp_started = time.perf_counter()
            results, runner, runtime_meta = run_viewport(vp, artifact_dir)
            runtime_by_viewport[vp] = runtime_meta
            vp_duration = int((time.perf_counter() - vp_started) * 1000)

            counts = count_statuses(results)
            cases_ok = counts["FAIL"] == 0 and counts["BLOCKED"] == 0
            base_states_ok = (
                runner.pre_clean_status == "PASS" and runner.cleanup_status == "PASS"
            )
            vp_status = "PASS" if cases_ok and base_states_ok else "FAIL"
            vp_results.append(
                ViewportResult(
                    viewport=vp,
                    browser=runtime_meta,
                    status=vp_status,
                    started_at=vp_started_ts,
                    finished_at=iso_now(),
                    duration_ms=vp_duration,
                    summary={
                        "pass": counts["PASS"],
                        "fail": counts["FAIL"],
                        "blocked": counts["BLOCKED"],
                        "total": len(results),
                    },
                    pre_clean={
                        "status": runner.pre_clean_status,
                        "detail": runner.pre_clean_error or "PASS",
                    },
                    cleanup={
                        "status": runner.cleanup_status,
                        "detail": runner.cleanup_error or runner.cleanup_detail,
                    },
                    cases=results,
                )
            )
            print_viewport(vp, results, runner)
    except Exception as exc:  # framework fatal: retain any completed viewport data
        classification, exit_code = _fatal_classification(exc)
        _write_run_result(
            artifact_dir,
            run_id,
            site,
            base_url,
            started_at,
            started,
            vp_results,
            runtime={"viewports": runtime_by_viewport},
            fatal_error={"classification": classification, "message": sanitize_message(exc)},
        )
        print(f"FATAL_ERROR [{classification}]: {sanitize_message(exc)}")
        return exit_code

    total_counts = {
        "pass": sum(v.summary["pass"] for v in vp_results),
        "fail": sum(v.summary["fail"] for v in vp_results),
        "blocked": sum(v.summary["blocked"] for v in vp_results),
    }
    total = sum(len(v.cases) for v in vp_results)
    overall = "PASS" if all(v.status == "PASS" for v in vp_results) else "FAIL"

    if not _write_run_result(
        artifact_dir,
        run_id,
        site,
        base_url,
        started_at,
        started,
        vp_results,
        runtime={"viewports": runtime_by_viewport},
    ):
        return 1

    print("=== Summary ===")
    print()
    for v in vp_results:
        label = "Desktop Chromium" if v.viewport == "desktop" else "Mobile WebKit / iPhone 14"
        print(label)
        print(f"PASS:     {v.summary['pass']}")
        print(f"FAIL:     {v.summary['fail']}")
        print(f"BLOCKED:  {v.summary['blocked']}")
        print()
    print("Total")
    print(f"PASS:     {total_counts['pass']}")
    print(f"FAIL:     {total_counts['fail']}")
    print(f"BLOCKED:  {total_counts['blocked']}")
    print(f"TOTAL:    {total}")
    print()
    print("Results:")
    print(f"artifacts/website-smoke-v1/{run_id}/results.json")

    return 0 if overall == "PASS" else 1


if __name__ == "__main__":
    sys.exit(guarded_main(main))
