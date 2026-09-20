"""Run independent Mobile Readonly PDP readiness gate journeys.

This diagnostic runner deliberately executes only the requested Direct and
Search journeys.  It is not a Website Smoke Observation build: every
iteration owns a fresh BrowserRuntime/context and writes evidence under
``output/`` rather than the formal ``artifacts/`` result contract.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.website_smoke_readonly_v1_cases import (  # noqa: E402
    READONLY_JOURNEY_CASES,
    WebsiteSmokeReadonlyV1Runner,
)
from utils.browser import close_browser, create_browser  # noqa: E402
from utils.readonly_mutation_guard import ReadonlyMutationGuard  # noqa: E402
from utils.errors import sanitize_message  # noqa: E402


def _run_one(site: str, journey: str, iteration: int) -> dict:
    started = time.perf_counter()
    runtime = None
    guard = None
    runner = None
    error = None
    try:
        runtime = create_browser("mobile", site_name=site)
        guard = ReadonlyMutationGuard()
        guard.attach(runtime.context)
        runner = WebsiteSmokeReadonlyV1Runner(
            runtime,
            runtime.site_config,
            "mobile",
            artifact_dir=None,
            mutation_guard=guard,
        )
        readiness_diagnostics = []

        def capture_readiness(prod, phase):
            timeline = []
            try:
                return prod.wait_purchase_ready(diagnostics_hook=timeline.append)
            finally:
                readiness_diagnostics.append({"phase": phase, "samples": timeline})

        # The production runner normally enables this hook only for a failed
        # diagnostic build.  The targeted gate records the same scalar-only
        # snapshots on PASS as evidence; wait semantics are independent of the
        # hook and remain bounded by the same hard deadline.
        runner._wait_purchase_ready = capture_readiness
        runner._run_journey(journey)
        case_ids = READONLY_JOURNEY_CASES[journey]
        cases = [runner.results[case_id].to_dict() for case_id in case_ids]
        violations = guard.violations()
        status = "PASS" if all(case["status"] == "PASS" for case in cases) and not violations else "FAIL"
        return {
            "iteration": iteration,
            "journey": journey,
            "status": status,
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "runtime": runtime.metadata(),
            "cases": cases,
            "mutation_violations": len(violations),
            "mutation_violation_details": [ReadonlyMutationGuard.safe_detail(item) for item in violations],
            "readiness_diagnostics": readiness_diagnostics,
        }
    except Exception as exc:  # noqa: BLE001 - preserve one-run evidence
        error = f"{type(exc).__name__}: {sanitize_message(exc)}"
        return {
            "iteration": iteration,
            "journey": journey,
            "status": "FAIL",
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "runtime": runtime.metadata() if runtime is not None else {},
            "cases": [
                {
                    "case_id": case_id,
                    "status": "NOT_RUN",
                    "detail": error,
                }
                for case_id in READONLY_JOURNEY_CASES[journey]
            ],
            "mutation_violations": len(guard.violations()) if guard is not None else 0,
            "mutation_violation_details": [],
            "readiness_diagnostics": getattr(runner, "_readiness_diagnostics", []),
            "error": error,
        }
    finally:
        if runner is not None:
            try:
                runner._close_extra_pages()
            except Exception:
                pass
        if guard is not None:
            try:
                guard.detach()
            except Exception:
                pass
        close_browser(runtime)


def _run_many(site: str, journey: str, count: int) -> list[dict]:
    return [_run_one(site, journey, index) for index in range(1, count + 1)]


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Targeted Mobile PDP readiness gate")
    parser.add_argument("--site", default="lavetir")
    parser.add_argument("--search-runs", type=int, default=10)
    parser.add_argument("--direct-runs", type=int, default=5)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.search_runs < 0 or args.direct_runs < 0 or (args.search_runs + args.direct_runs) < 1:
        parser.error("at least one run is required and counts must be non-negative")

    started = time.perf_counter()
    records = {
        "search": _run_many(args.site, "search", args.search_runs),
        "direct": _run_many(args.site, "direct", args.direct_runs),
    }
    summary = {
        journey: {
            "pass": sum(record["status"] == "PASS" for record in journey_records),
            "fail": sum(record["status"] != "PASS" for record in journey_records),
            "total": len(journey_records),
        }
        for journey, journey_records in records.items()
    }
    payload = {
        "diagnostic": "targeted_mobile_pdp_readiness_gate",
        "site": args.site,
        "viewport": "mobile",
        "same_sha": True,
        "search_runs": args.search_runs,
        "direct_runs": args.direct_runs,
        "summary": summary,
        "records": records,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = PROJECT_ROOT / "output" / "pdp-readiness-gates" / args.site / f"mobile_{stamp}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"summary": summary, "output": str(output_path)}, ensure_ascii=False))
    return 0 if all(item["fail"] == 0 for item in summary.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
