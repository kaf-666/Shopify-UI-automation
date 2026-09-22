"""Website Smoke V1 入口：三条购买 Journey 的日常编排 Smoke。

用法：
    python scripts/run_website_smoke_v1.py                   # both, settings.default_site
    python scripts/run_website_smoke_v1.py --viewport desktop
    python scripts/run_website_smoke_v1.py --viewport mobile
    python scripts/run_website_smoke_v1.py --site mondressy --viewport both
    python scripts/run_website_smoke_v1.py --viewport both --traffic-inventory
    python scripts/run_website_smoke_v1.py --viewport both --traffic-inventory \
        --traffic-reduction telemetry-v1

产物：
    artifacts/website-smoke-v1/<site>/<run_id>/results.json
    artifacts/website-smoke-v1/<site>/<run_id>/<viewport>/<CASE_ID>-failure.png

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
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pages.base_page import BasePage
from tests.website_smoke_v1_cases import WebsiteSmokeV1Runner
from utils.artifacts import (
    artifact_display_path,
    canonical_site_name,
    site_scoped_artifact_dir,
)
from utils.browser import close_browser, create_browser, load_settings
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
from utils.readonly_mutation_guard import (
    TransactionalMutationPolicy,
    merge_transactional_mutation_summaries,
)
from utils.mutation_fingerprint import format_mutation_fingerprint_report
from utils.site_config_validator import WEBSITE_SMOKE_V1, validate_site_config
from utils.suite_runner import guarded_main
from utils.traffic_inventory import TrafficInventory
from utils.traffic_reduction import (
    EXPERIMENT_TELEMETRY_V1,
    TrafficReductionPolicy,
    create_traffic_reduction_policy,
)

ARTIFACT_ROOT = PROJECT_ROOT / "artifacts" / "website-smoke-v1"


def _runner_mutation_summary(runner) -> dict:
    """Return a normalized policy summary, including legacy test doubles."""
    method = getattr(runner, "mutation_summary", None)
    if callable(method):
        value = method()
        if isinstance(value, dict):
            return value
    return {
        "mode": "TRANSACTIONAL_SAFE",
        "status": "PASS",
        "expected_mutation": 0,
        "unexpected_mutation": 0,
        "high_risk_mutation": 0,
        "blocked_mutation": 0,
        "by_path": [],
    }


def _traffic_first_party_hosts(site_config: dict, base_url: str) -> tuple[str, ...]:
    """Use the active site's explicit allowlist for observation classification."""
    access = site_config.get("access") or {}
    raw_hosts = access.get("allowed_hosts") if isinstance(access, dict) else None
    hosts = tuple(
        str(host).strip().lower()
        for host in (raw_hosts or [])
        if str(host).strip()
    )
    if hosts:
        return hosts
    host = (urlsplit(base_url).hostname or "").lower()
    if host:
        return (host,)
    raise CliConfigError("unable to derive first-party hosts", category="INVALID_ACCESS_CONFIG")


def run_viewport(
    viewport: str,
    artifact_dir: Path,
    traffic_inventory: Optional[TrafficInventory] = None,
    traffic_reduction: Optional[TrafficReductionPolicy] = None,
    site_name: Optional[str] = None,
    mutation_policy: Optional[TransactionalMutationPolicy] = None,
) -> Tuple[List, WebsiteSmokeV1Runner, dict]:
    runtime = create_browser(viewport, site_name=site_name)
    try:
        if traffic_reduction is not None:
            # Signed Request was registered by create_browser(). This handler
            # is intentionally registered second and falls back on non-match.
            traffic_reduction.attach(runtime.context)
        if traffic_inventory is not None:
            try:
                traffic_inventory.attach_context(runtime.context, viewport, runtime.page)
            except Exception as exc:  # observation must not affect business execution
                traffic_inventory.record_error("attach_context", exc)
        if mutation_policy is not None:
            # Register after Signed Request, traffic reduction, and inventory
            # so expected mutations fall through the complete route chain,
            # while high-risk requests are aborted before reaching Shopify.
            mutation_policy.attach(runtime.context)
        runtime_meta = runtime.metadata()
        if traffic_reduction is not None and traffic_reduction.enabled:
            runtime_meta["traffic_reduction"] = traffic_reduction.runtime_summary()
        site = runtime.site_config or BasePage.load_site_config(site_name=runtime.site_name)
        runner = WebsiteSmokeV1Runner(
            runtime,
            site,
            viewport,
            artifact_dir=artifact_dir,
            traffic_inventory=traffic_inventory,
            mutation_policy=mutation_policy,
        )
        results = runner.run_all()
        return results, runner, runtime_meta
    finally:
        if mutation_policy is not None:
            mutation_policy.detach()
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
        mutation = _runner_mutation_summary(runner)
        print(
            f"{'':<20} Mutation: {mutation.get('status')} "
            f"EXPECTED={mutation.get('expected_mutation', 0)} "
            f"UNEXPECTED={mutation.get('unexpected_mutation', 0)} "
            f"HIGH_RISK={mutation.get('high_risk_mutation', 0)}"
        )
    print()


def print_mutation_fingerprints(mutation_summary: dict) -> None:
    """Print safe endpoint fingerprints only for a failed mutation gate."""
    for line in format_mutation_fingerprint_report(mutation_summary):
        print(line)


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
    mutation_summary: Optional[dict] = None,
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
        mutation_summary=mutation_summary,
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


def _write_traffic_inventory(
    inventory: Optional[TrafficInventory], artifact_dir: Path
) -> None:
    """Persist the optional observer without changing the business exit contract."""
    if inventory is None:
        return
    try:
        traffic_dir = artifact_dir / "traffic"
        summary = inventory.write_artifacts(traffic_dir)
        print()
        print(inventory.console_summary(summary), end="")
        print()
        print("Traffic Artifacts:")
        for name in ("requests.jsonl", "summary.json", "summary.txt"):
            print(f"{artifact_display_path(artifact_dir)}/traffic/{name}")
    except Exception as exc:  # observation output cannot change business exit status
        try:
            inventory.record_error("console_output", exc)
        except Exception:
            pass
        print("Traffic Inventory: ERROR (business result unchanged)")


def _write_traffic_reduction(
    policy: TrafficReductionPolicy, artifact_dir: Path
) -> None:
    """Persist the opt-in experiment's aggregate blocking summary."""
    if not policy.enabled:
        return
    try:
        path = artifact_dir / "traffic" / "blocking-summary.json"
        write_results_json(policy.build_summary(), path)
        print("Traffic Reduction Artifact:")
        print(f"{artifact_display_path(artifact_dir)}/traffic/{path.name}")
    except Exception as exc:
        print(f"TRAFFIC_REDUCTION_ARTIFACT_FAILURE: {sanitize_message(exc)}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Website Smoke V1")
    parser.add_argument("--viewport", choices=["desktop", "mobile", "both"], default="both")
    parser.add_argument(
        "--site",
        default=None,
        help="site name (default: settings.default_site)",
    )
    parser.add_argument(
        "--traffic-inventory",
        action="store_true",
        help="attach the read-only request inventory observer (default: off)",
    )
    parser.add_argument(
        "--traffic-reduction",
        choices=[EXPERIMENT_TELEMETRY_V1],
        default=None,
        help="opt-in request reduction experiment (default: off)",
    )
    args = parser.parse_args(argv)  # argparse exits 2 on invalid choice
    if args.traffic_reduction and not args.traffic_inventory:
        parser.error("--traffic-reduction requires --traffic-inventory")

    run_id = make_run_id()
    started_at = iso_now()
    started = time.perf_counter()
    site = ""
    base_url = ""
    artifact_dir: Optional[Path] = None
    vp_results: List[ViewportResult] = []
    runtime_by_viewport: Dict[str, dict] = {}
    mutation_summaries: List[dict] = []

    try:
        settings = load_settings()
        requested_site = args.site if args.site is not None else str(settings.get("default_site") or "")
        site = canonical_site_name(requested_site)
        site_cfg = validate_site_config(site, suite=WEBSITE_SMOKE_V1)
        base_url = resolve_url(site_cfg.get("base_url"), "site.base_url")
        artifact_dir = site_scoped_artifact_dir(ARTIFACT_ROOT, site, run_id)
    except Exception as exc:
        classification, exit_code = _fatal_classification(exc)
        print(f"FATAL_ERROR [{classification}]: {sanitize_message(exc)}")
        return exit_code

    try:
        artifact_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"ARTIFACT_DIRECTORY_FAILURE: {sanitize_message(exc)}")
        return 2

    traffic_inventory = (
        TrafficInventory(first_party_hosts=_traffic_first_party_hosts(site_cfg, base_url))
        if args.traffic_inventory
        else None
    )
    traffic_reduction = create_traffic_reduction_policy(args.traffic_reduction)

    viewports = ["desktop", "mobile"] if args.viewport == "both" else [args.viewport]

    print(f"=== {site} Website Smoke V1 ===")
    print()
    try:
        for vp in viewports:
            vp_started_ts = iso_now()
            vp_started = time.perf_counter()
            mutation_policy = TransactionalMutationPolicy(
                _traffic_first_party_hosts(site_cfg, base_url),
                canonical_origin=base_url,
            )
            results, runner, runtime_meta = run_viewport(
                vp,
                artifact_dir,
                traffic_inventory=traffic_inventory,
                traffic_reduction=traffic_reduction,
                site_name=site,
                mutation_policy=mutation_policy,
            )
            runtime_by_viewport[vp] = runtime_meta
            vp_duration = int((time.perf_counter() - vp_started) * 1000)

            counts = count_statuses(results)
            mutation_summary = _runner_mutation_summary(runner)
            mutation_summaries.append(mutation_summary)
            cases_ok = counts["FAIL"] == 0 and counts["BLOCKED"] == 0
            base_states_ok = (
                runner.pre_clean_status == "PASS" and runner.cleanup_status == "PASS"
            )
            vp_status = (
                "PASS"
                if cases_ok and base_states_ok and mutation_summary.get("status") == "PASS"
                else "FAIL"
            )
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
                    mutation_summary=mutation_summary,
                )
            )
            print_viewport(vp, results, runner)
    except Exception as exc:  # framework fatal: retain any completed viewport data
        classification, exit_code = _fatal_classification(exc)
        partial_mutation_summary = merge_transactional_mutation_summaries(mutation_summaries)
        print_mutation_fingerprints(partial_mutation_summary)
        _write_run_result(
            artifact_dir,
            run_id,
            site,
            base_url,
            started_at,
            started,
            vp_results,
            runtime={"viewports": runtime_by_viewport},
            mutation_summary=partial_mutation_summary,
            fatal_error={"classification": classification, "message": sanitize_message(exc)},
        )
        print(f"FATAL_ERROR [{classification}]: {sanitize_message(exc)}")
        _write_traffic_inventory(traffic_inventory, artifact_dir)
        _write_traffic_reduction(traffic_reduction, artifact_dir)
        return exit_code

    total_counts = {
        "pass": sum(v.summary["pass"] for v in vp_results),
        "fail": sum(v.summary["fail"] for v in vp_results),
        "blocked": sum(v.summary["blocked"] for v in vp_results),
    }
    total = sum(len(v.cases) for v in vp_results)
    mutation_summary = merge_transactional_mutation_summaries(mutation_summaries)
    overall = (
        "PASS"
        if all(v.status == "PASS" for v in vp_results)
        and mutation_summary.get("status") == "PASS"
        else "FAIL"
    )

    if not _write_run_result(
        artifact_dir,
        run_id,
        site,
        base_url,
        started_at,
        started,
        vp_results,
        runtime={"viewports": runtime_by_viewport},
        mutation_summary=mutation_summary,
    ):
        print_mutation_fingerprints(mutation_summary)
        _write_traffic_inventory(traffic_inventory, artifact_dir)
        _write_traffic_reduction(traffic_reduction, artifact_dir)
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
    print("Mutation")
    print(f"MODE:             {mutation_summary['mode']}")
    print(f"STATUS:           {mutation_summary['status']}")
    print(f"EXPECTED_MUTATION:{mutation_summary['expected_mutation']}")
    print(f"UNEXPECTED_MUTATION: {mutation_summary['unexpected_mutation']}")
    print(f"HIGH_RISK_MUTATION:  {mutation_summary['high_risk_mutation']}")
    print(f"BLOCKED_MUTATION:    {mutation_summary['blocked_mutation']}")
    print()
    print_mutation_fingerprints(mutation_summary)
    if mutation_summary.get("unexpected_mutation", 0) or mutation_summary.get("high_risk_mutation", 0):
        print()
    print("Results:")
    print(f"{artifact_display_path(artifact_dir)}/results.json")

    _write_traffic_inventory(traffic_inventory, artifact_dir)
    _write_traffic_reduction(traffic_reduction, artifact_dir)

    return 0 if overall == "PASS" else 1


if __name__ == "__main__":
    sys.exit(guarded_main(main))
