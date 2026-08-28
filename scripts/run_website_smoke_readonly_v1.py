"""Website Smoke Readonly V1 CLI：独立的 11 Case / viewport 入口。

用法：
    python scripts/run_website_smoke_readonly_v1.py
    python scripts/run_website_smoke_readonly_v1.py --viewport desktop
    python scripts/run_website_smoke_readonly_v1.py --viewport mobile
    python scripts/run_website_smoke_readonly_v1.py --viewport both

产物：
    artifacts/website-smoke-readonly-v1/<run_id>/results.json
    artifacts/website-smoke-readonly-v1/<run_id>/<viewport>/<CASE_ID>-failure.png

Readonly 不执行购物车前置清理、购物车收尾或 Checkout。每个 viewport
使用独立 BrowserRuntime，并在 Signed Request route 之后挂载 fail-closed
ReadonlyMutationGuard。
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
from tests.website_smoke_readonly_v1_cases import WebsiteSmokeReadonlyV1Runner
from utils.browser import close_browser, create_browser, load_site_config, load_settings
from utils.config import resolve_url
from utils.errors import CliConfigError, sanitize_message
from utils.readonly_mutation_guard import ReadonlyMutationGuard
from utils.result import (
    ResultWriteError,
    RunResult,
    ViewportResult,
    iso_now,
    make_run_id,
    write_results_json,
)
from utils.suite_runner import guarded_main

ARTIFACT_ROOT = PROJECT_ROOT / "artifacts" / "website-smoke-readonly-v1"
READONLY_CASES_PER_VIEWPORT = 11


def run_viewport(
    viewport: str,
    artifact_dir: Path,
) -> Tuple[List, WebsiteSmokeReadonlyV1Runner, dict]:
    """Run one viewport and always dispose its BrowserRuntime."""
    runtime = create_browser(viewport)
    guard: Optional[ReadonlyMutationGuard] = None
    try:
        guard = ReadonlyMutationGuard()
        # create_browser() has already registered Signed Request first;
        # guard fallback therefore returns non-matches to that route.
        guard.attach(runtime.context)
        runtime_meta = runtime.metadata()
        site = runtime.site_config or BasePage.load_site_config(site_name=runtime.site_name)
        runner = WebsiteSmokeReadonlyV1Runner(
            runtime,
            site,
            viewport,
            artifact_dir=artifact_dir,
            mutation_guard=guard,
        )
        results = runner.run_all()
        return results, runner, runtime_meta
    finally:
        try:
            close_browser(runtime)
        finally:
            if guard is not None:
                guard.detach()


def count_statuses(results: List) -> Dict[str, int]:
    return {status: sum(1 for result in results if result.status == status) for status in ("PASS", "FAIL", "BLOCKED")}


def _guard_violations(runner) -> List[dict]:
    guard = getattr(runner, "mutation_guard", None)
    if guard is None:
        return []
    return guard.violations()


def _first_violation_detail(violations: List[dict]) -> str:
    if not violations:
        return "readonly mutation violation"
    return ReadonlyMutationGuard.safe_detail(violations[0])


def print_viewport(viewport: str, results: List, runner=None) -> None:
    label = "Chromium" if viewport == "desktop" else "WebKit + iPhone 14"
    print(f"[{viewport.title()} / {label}]")
    counts = count_statuses(results)
    for result in results:
        print(f"{result.case_id:<24} {result.status:<8} {result.name}")
        if result.detail:
            print(f"{'':<24} {'':<8} {sanitize_message(result.detail)}")
    violations = _guard_violations(runner) if runner is not None else []
    print(
        f"{'':<24} PASS={counts['PASS']} FAIL={counts['FAIL']} "
        f"BLOCKED={counts['BLOCKED']} TOTAL={len(results)}"
    )
    print(f"{'':<24} Readonly Mutation Violations: {len(violations)}")
    print()


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
    counts = {
        "pass": sum(viewport.summary.get("pass", 0) for viewport in viewports),
        "fail": sum(viewport.summary.get("fail", 0) for viewport in viewports),
        "blocked": sum(viewport.summary.get("blocked", 0) for viewport in viewports),
    }
    total = sum(viewport.summary.get("total", 0) for viewport in viewports)
    result = RunResult(
        run_id=run_id,
        site=site,
        base_url=base_url,
        started_at=started_at,
        finished_at=iso_now(),
        duration_ms=int((time.perf_counter() - started) * 1000),
        overall_status=(
            "PASS"
            if not fatal_error and all(viewport.status == "PASS" for viewport in viewports)
            else "FAIL"
        ),
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
    if isinstance(exc, CliConfigError):
        return getattr(exc, "category", "CONFIG_ERROR"), 2
    return "RUNTIME_ERROR", 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Mondressy Website Smoke Readonly V1")
    parser.add_argument("--viewport", choices=["desktop", "mobile", "both"], default="both")
    args = parser.parse_args(argv)

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
    viewport_results: List[ViewportResult] = []
    runtime_by_viewport: Dict[str, dict] = {}
    mutation_fatal: Optional[dict] = None
    mutation_violation_total = 0

    try:
        settings = load_settings()
        site = str(settings.get("default_site") or "")
        site_config = load_site_config(site)
        base_url = resolve_url(site_config.get("base_url"), "site.base_url")
    except Exception as exc:
        classification, exit_code = _fatal_classification(exc)
        _write_run_result(
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
        return exit_code

    viewports = ["desktop", "mobile"] if args.viewport == "both" else [args.viewport]

    print("=== Mondressy Website Smoke Readonly V1 ===")
    print()
    try:
        for viewport in viewports:
            viewport_started_at = iso_now()
            viewport_started = time.perf_counter()
            results, runner, runtime_meta = run_viewport(viewport, artifact_dir)
            runtime_by_viewport[viewport] = runtime_meta

            if len(results) != READONLY_CASES_PER_VIEWPORT:
                raise RuntimeError(
                    f"readonly case contract mismatch: expected={READONLY_CASES_PER_VIEWPORT} "
                    f"actual={len(results)}"
                )

            violations = _guard_violations(runner)
            guard = getattr(runner, "mutation_guard", None)
            out_of_scope = guard.out_of_scope_violations() if guard is not None else []
            mutation_violation_total += len(violations)
            if out_of_scope and mutation_fatal is None:
                mutation_fatal = {
                    "classification": "READONLY_MUTATION_VIOLATION",
                    "message": (
                        "blocked cart mutation outside Case scope: "
                        f"{_first_violation_detail(out_of_scope).removeprefix('blocked cart mutation: ')}"
                    ),
                }

            counts = count_statuses(results)
            viewport_status = (
                "PASS"
                if counts["FAIL"] == 0 and counts["BLOCKED"] == 0 and not violations
                else "FAIL"
            )
            viewport_results.append(
                ViewportResult(
                    viewport=viewport,
                    browser=runtime_meta,
                    status=viewport_status,
                    started_at=viewport_started_at,
                    finished_at=iso_now(),
                    duration_ms=int((time.perf_counter() - viewport_started) * 1000),
                    summary={
                        "pass": counts["PASS"],
                        "fail": counts["FAIL"],
                        "blocked": counts["BLOCKED"],
                        "total": len(results),
                    },
                    pre_clean={"status": "PASS", "detail": "readonly_not_required"},
                    cleanup={"status": "PASS", "detail": "readonly_not_required"},
                    cases=results,
                )
            )
            print_viewport(viewport, results, runner)
    except Exception as exc:  # framework fatal: retain completed viewport data
        classification, exit_code = _fatal_classification(exc)
        _write_run_result(
            artifact_dir,
            run_id,
            site,
            base_url,
            started_at,
            started,
            viewport_results,
            runtime={"viewports": runtime_by_viewport},
            fatal_error={"classification": classification, "message": sanitize_message(exc)},
        )
        print(f"FATAL_ERROR [{classification}]: {sanitize_message(exc)}")
        return exit_code

    total_counts = {
        "pass": sum(viewport.summary["pass"] for viewport in viewport_results),
        "fail": sum(viewport.summary["fail"] for viewport in viewport_results),
        "blocked": sum(viewport.summary["blocked"] for viewport in viewport_results),
    }
    total = sum(len(viewport.cases) for viewport in viewport_results)
    overall = (
        "PASS"
        if not mutation_fatal and all(viewport.status == "PASS" for viewport in viewport_results)
        else "FAIL"
    )

    if not _write_run_result(
        artifact_dir,
        run_id,
        site,
        base_url,
        started_at,
        started,
        viewport_results,
        runtime={"viewports": runtime_by_viewport},
        fatal_error=mutation_fatal,
    ):
        return 1

    print("=== Summary ===")
    print()
    for viewport in viewport_results:
        label = "Desktop Chromium" if viewport.viewport == "desktop" else "Mobile WebKit / iPhone 14"
        print(label)
        print(f"PASS:     {viewport.summary['pass']}")
        print(f"FAIL:     {viewport.summary['fail']}")
        print(f"BLOCKED:  {viewport.summary['blocked']}")
        print()
    print("Total")
    print(f"PASS:     {total_counts['pass']}")
    print(f"FAIL:     {total_counts['fail']}")
    print(f"BLOCKED:  {total_counts['blocked']}")
    print(f"TOTAL:    {total}")
    print()
    print(f"Readonly Mutation Violations: {mutation_violation_total}")
    print()
    if mutation_fatal:
        print(
            f"FATAL_ERROR [{mutation_fatal['classification']}]: "
            f"{sanitize_message(mutation_fatal['message'])}"
        )
        print()
    print("Results:")
    print(f"artifacts/website-smoke-readonly-v1/{run_id}/results.json")

    return 0 if overall == "PASS" else 1


if __name__ == "__main__":
    sys.exit(guarded_main(main))
