"""Summarize the latest N eligible Website Smoke V1 stability records.

Examples:

    python scripts/summarize_stability.py
    python scripts/summarize_stability.py --last 10
    python scripts/summarize_stability.py --history path/to/stability-history.jsonl

The summary selects one baseline commit (the newest commit in the available
records unless ``--baseline-commit`` is supplied).  Older commits are never
combined into the active window.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.stability import (
    HISTORY_FILENAME,
    WEBSITE_ARTIFACT_ROOT,
    aggregate_status_counts,
    case_stability,
    duration_stats,
    failure_frequencies,
    gate_failure_count,
    http_totals,
    load_archived_records,
    load_history,
    merge_records,
    summarize_records,
)


def _rate(success: int, total: int) -> str:
    return f"{(success / total * 100) if total else 0.0:.2f}% ({success}/{total})"


def _print_frequency(title: str, values: dict[str, int]) -> None:
    print(title)
    if not values:
        print("  none")
        return
    for key, count in sorted(values.items(), key=lambda item: (-item[1], item[0])):
        print(f"  {key}: {count}")


def render_summary(summary: dict) -> None:
    records = summary["records"]
    print("=== Website Smoke V1 Stability Summary ===")
    print()
    print(f"Baseline Commit: {summary['baseline_commit'] or 'UNKNOWN'}")
    print(f"Eligible Builds: {summary['eligible_builds']}")
    print(f"Eligible Builds On Baseline: {summary['eligible_builds_on_baseline']}")
    if summary.get("mixed_commits_ignored"):
        print(f"Mixed Commits Ignored: {summary['mixed_commits_ignored']}")
    print()

    successful = sum(1 for record in records if record.get("jenkins_result") == "SUCCESS")
    print(f"Jenkins Success Rate: {_rate(successful, len(records))}")
    for viewport, label in (("desktop", "Desktop"), ("mobile", "Mobile"), ("combined", "Combined")):
        counts = aggregate_status_counts(records, viewport)
        print(
            f"{label} Pass / Fail / Blocked: "
            f"{counts['pass']} / {counts['fail']} / {counts['blocked']}"
        )

    print()
    print(f"Pre-clean failures: {gate_failure_count(records, 'pre_clean')}")
    print(f"Cleanup failures: {gate_failure_count(records, 'cleanup')}")
    print(f"Schema Gate failures: {gate_failure_count(records, 'schema_gate')}")
    print(f"Secret Gate failures: {gate_failure_count(records, 'secret_gate')}")
    print(f"Artifact Gate failures: {gate_failure_count(records, 'artifact_gate')}")
    http = http_totals(records)
    print(f"403 count: {http['403']}")
    print(f"429 count: {http['429']}")
    print(f"5xx count: {http['5xx']}")

    case_frequency, classification_frequency = failure_frequencies(records)
    print()
    _print_frequency("Failure Case Frequency:", dict(case_frequency))
    _print_frequency("Failure Classification Frequency:", dict(classification_frequency))

    durations = duration_stats(records)
    print()
    print(f"Average Duration: {durations['average']:.3f}s")
    print(f"P95 Duration: {durations['p95']:.3f}s")
    print(f"Min Duration: {durations['min']:.3f}s")
    print(f"Max Duration: {durations['max']:.3f}s")

    case_counts = case_stability(records)
    print()
    for case_id in ("WSMOKE-CART-03", "WSMOKE-CHECKOUT-01"):
        counts = case_counts.get(case_id, {"pass": 0, "fail": 0, "blocked": 0})
        total = sum(counts.values())
        print(
            f"{case_id} stability: PASS {_rate(counts['pass'], total)}, "
            f"FAIL {counts['fail']}, BLOCKED {counts['blocked']}"
        )

    print()
    print(f"STABILITY_STATUS={summary['status']}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize Website Smoke V1 stability")
    parser.add_argument("--last", type=int, default=10, help="number of eligible records to summarize")
    parser.add_argument(
        "--history",
        default=str(WEBSITE_ARTIFACT_ROOT / HISTORY_FILENAME),
        help="stability-history.jsonl path",
    )
    parser.add_argument(
        "--records-root",
        default=str(WEBSITE_ARTIFACT_ROOT),
        help="archived stability_record.json root",
    )
    parser.add_argument("--baseline-commit", help="explicit baseline SHA")
    parser.add_argument(
        "--strict-mixed-baseline",
        action="store_true",
        help="report MIXED_BASELINE instead of selecting the newest single commit",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable summary JSON")
    args = parser.parse_args(argv)
    if args.last <= 0:
        parser.error("--last must be positive")

    history_records, history_errors = load_history(Path(args.history))
    archived_records = load_archived_records(Path(args.records_root))
    summary = summarize_records(
        merge_records(history_records, archived_records),
        last=args.last,
        baseline_commit=args.baseline_commit,
        strict_mixed=args.strict_mixed_baseline,
    )
    summary["history_parse_errors"] = history_errors

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        render_summary(summary)
        if history_errors:
            print()
            print(f"History parse warnings: {len(history_errors)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

