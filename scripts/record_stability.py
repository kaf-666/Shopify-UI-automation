"""Create one safe Website Smoke V1 Stability Record and update JSONL history.

The script consumes an existing ``results.json`` only.  It never opens a
browser or sends a request to the production site.  Missing results are
reported as ``COLLECTING`` and do not turn the functional build into a new
failure mode.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.stability import (
    HISTORY_FILENAME,
    WEBSITE_ARTIFACT_ROOT,
    atomic_write_json,
    build_stability_record,
    contains_forbidden_marker,
    load_archived_records,
    load_history,
    merge_records,
    summarize_records,
    write_history_record,
)


def _latest_results(root: Path) -> Optional[Path]:
    candidates = sorted(root.rglob("results.json")) if root.exists() else []
    if not candidates:
        return None
    return max(candidates, key=lambda path: (path.stat().st_mtime_ns, str(path)))


def _load_results(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("results.json must contain a JSON object")
    return value


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Record Website Smoke V1 stability data")
    parser.add_argument("--results", help="explicit results.json path")
    parser.add_argument("--artifacts-root", default=str(WEBSITE_ARTIFACT_ROOT))
    parser.add_argument("--history", help="JSONL history path")
    parser.add_argument("--viewport", choices=["desktop", "mobile", "both"])
    parser.add_argument("--commit-sha", help="checked-out workspace HEAD SHA")
    parser.add_argument("--schema-gate", choices=["PASS", "FAIL", "NOT_RUN"])
    parser.add_argument("--secret-gate", choices=["PASS", "FAIL", "NOT_RUN"])
    args = parser.parse_args(argv)

    artifacts_root = Path(args.artifacts_root)
    result_path = Path(args.results) if args.results else _latest_results(artifacts_root)
    requested_viewport = args.viewport or os.environ.get("SMOKE_VIEWPORT") or ""
    if result_path is None or not result_path.exists():
        print("Stability Record: no Website Smoke V1 results.json found")
        print("STABILITY_STATUS=COLLECTING")
        return 0

    record_env = dict(os.environ)
    if args.commit_sha is not None:
        record_env["GIT_COMMIT_SHA"] = args.commit_sha
    if args.schema_gate is not None:
        record_env["STABILITY_SCHEMA_GATE"] = args.schema_gate
    if args.secret_gate is not None:
        record_env["STABILITY_SECRET_GATE"] = args.secret_gate

    try:
        results = _load_results(result_path)
        record = build_stability_record(
            results,
            result_path,
            requested_viewport=requested_viewport,
            environ=record_env,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Stability Record: unable to read results ({type(exc).__name__})")
        print("STABILITY_STATUS=COLLECTING")
        return 0

    if record.get("viewport") != "both":
        print(f"Stability Record: skipped non-both viewport ({record.get('viewport')})")
        print("STABILITY_STATUS=COLLECTING")
        return 0

    marker = contains_forbidden_marker(record)
    if marker:
        print(f"Stability Record: forbidden secret marker detected ({marker})")
        return 1

    record_path = result_path.parent / "stability_record.json"
    history_path = Path(args.history) if args.history else artifacts_root / HISTORY_FILENAME
    try:
        atomic_write_json(record_path, record)
        write_history_record(history_path, record)
    except OSError as exc:
        print(f"Stability Record: write failed ({type(exc).__name__})")
        return 1

    history_records, history_errors = load_history(history_path)
    archived_records = load_archived_records(artifacts_root)
    summary = summarize_records(merge_records(history_records, archived_records), last=10)
    print(f"Stability Record: {record_path.as_posix()}")
    print(f"History JSONL: {history_path.as_posix()}")
    print(f"Eligible Builds On Baseline: {summary['eligible_builds_on_baseline']}")
    if history_errors:
        print(f"History parse warnings: {len(history_errors)}")
    print(f"STABILITY_STATUS={summary['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
