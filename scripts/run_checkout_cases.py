"""Checkout Entry Cases 入口。

用法：
    python scripts/run_checkout_cases.py                   # both
    python scripts/run_checkout_cases.py --viewport desktop
    python scripts/run_checkout_cases.py --viewport mobile

产物：
    artifacts/checkout/<run_id>/results.json
    artifacts/checkout/<run_id>/<viewport>/<CASE_ID>-failure.png

退出码：0 = 全部通过，1 = 任一 FAIL/BLOCKED，2 = 非法视口。
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
from tests.checkout_cases import CheckoutCaseRunner
from utils.browser import close_browser, create_browser
from utils.result import (
    ResultWriteError,
    RunResult,
    ViewportResult,
    iso_now,
    make_run_id,
    write_results_json,
)

ARTIFACT_ROOT = PROJECT_ROOT / "artifacts" / "checkout"


def run_viewport(viewport: str, artifact_dir: Path) -> Tuple[List, CheckoutCaseRunner, dict]:
    runtime = create_browser(viewport)
    try:
        runtime_meta = {
            "proxy_enabled": bool(runtime.proxy_server),
            "site_access_policy": (
                runtime.access_policy.type_name if runtime.access_policy else "none"
            ),
        }
        site = BasePage.load_site_config()
        runner = CheckoutCaseRunner(runtime, site, viewport, artifact_dir=artifact_dir)
        results = runner.run_all()
        return results, runner, runtime_meta
    finally:
        close_browser(runtime)


def print_viewport(viewport: str, results: List, runner: Optional[CheckoutCaseRunner] = None) -> None:
    print(f"[{viewport.title()} / {'Chromium' if viewport == 'desktop' else 'WebKit + iPhone 14'}]")
    counts = {"PASS": 0, "FAIL": 0, "BLOCKED": 0}
    for r in results:
        counts[r.status] += 1
        print(f"{r.case_id:<14} {r.status:<8} {r.name}")
        if r.detail:
            print(f"{'':<14} {'':<8} {r.detail}")
    print(f"{'':<14} PASS={counts['PASS']} FAIL={counts['FAIL']} BLOCKED={counts['BLOCKED']} TOTAL={len(results)}")
    if runner is not None:
        print(f"{'':<14} Pre-clean: {runner.pre_clean_status} | Post-checkout cleanup: NOT_REQUIRED_CONTEXT_DISPOSED")
    print()


def count_statuses(results: List) -> Dict[str, int]:
    return {s: sum(1 for r in results if r.status == s) for s in ("PASS", "FAIL", "BLOCKED")}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Mondressy Checkout Entry Cases")
    parser.add_argument("--viewport", choices=["desktop", "mobile", "both"], default="both")
    args = parser.parse_args(argv)  # argparse exits 2 on invalid choice

    run_id = make_run_id()
    artifact_dir = ARTIFACT_ROOT / run_id
    try:
        artifact_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"ARTIFACT_DIRECTORY_FAILURE: {exc}")
        return 1

    site_cfg = BasePage.load_site_config()
    site = str(site_cfg.get("site") or "mondressy")
    base_url = str(site_cfg.get("base_url") or "")

    viewports = ["desktop", "mobile"] if args.viewport == "both" else [args.viewport]
    run_started_ts = iso_now()
    run_started = time.perf_counter()

    print("=== Mondressy Checkout Entry Cases ===")
    print()
    vp_results: List[ViewportResult] = []
    for vp in viewports:
        vp_started_ts = iso_now()
        vp_started = time.perf_counter()
        results, runner, runtime_meta = run_viewport(vp, artifact_dir)
        vp_duration = int((time.perf_counter() - vp_started) * 1000)

        counts = count_statuses(results)
        vp_status = "PASS" if counts["FAIL"] == 0 and counts["BLOCKED"] == 0 else "FAIL"
        vp_results.append(
            ViewportResult(
                viewport=vp,
                browser={
                    "engine": "chromium" if vp == "desktop" else "webkit",
                    "device": None if vp == "desktop" else "iPhone 14",
                },
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
                    "detail": runner.pre_clean_error or "",
                },
                cleanup={
                    "status": "PASS",
                    "detail": "post_checkout_cleanup: NOT_REQUIRED_CONTEXT_DISPOSED",
                },
                cases=results,
            )
        )
        print_viewport(vp, results, runner)

    total_counts = {
        "pass": sum(v.summary["pass"] for v in vp_results),
        "fail": sum(v.summary["fail"] for v in vp_results),
        "blocked": sum(v.summary["blocked"] for v in vp_results),
    }
    total = sum(len(v.cases) for v in vp_results)
    overall = "PASS" if all(v.status == "PASS" for v in vp_results) else "FAIL"

    run_result = RunResult(
        run_id=run_id,
        site=site,
        base_url=base_url,
        started_at=run_started_ts,
        finished_at=iso_now(),
        duration_ms=int((time.perf_counter() - run_started) * 1000),
        overall_status=overall,
        runtime=runtime_meta,
        summary={**total_counts, "total": total},
        viewports=vp_results,
    )

    json_path = artifact_dir / "results.json"
    try:
        write_results_json(run_result.to_dict(), json_path)
    except ResultWriteError as exc:
        print(f"RESULT_WRITE_FAILURE: {exc}")
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
    print(f"artifacts/checkout/{run_id}/results.json")

    return 0 if overall == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
