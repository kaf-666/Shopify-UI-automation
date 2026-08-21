"""Offline validation for Website Smoke V1 Stability Record/History/Summary.

All scenarios use synthetic JSON objects.  No browser, Jenkins server, or
production-site request is needed.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.stability import (
    STABILITY_SCHEMA_VERSION,
    atomic_write_json,
    case_stability,
    contains_forbidden_marker,
    load_history,
    summarize_records,
    write_history_record,
)


def check(ok: bool, label: str, detail: str = "") -> bool:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" ({detail})" if detail else ""))
    return ok


def _case_statuses(cart_status: str = "PASS") -> list[dict[str, str]]:
    case_ids = [
        "WSMOKE-DIRECT-01", "WSMOKE-DIRECT-02", "WSMOKE-DIRECT-03",
        "WSMOKE-SEARCH-01", "WSMOKE-SEARCH-02", "WSMOKE-SEARCH-03", "WSMOKE-SEARCH-04",
        "WSMOKE-HOME-01", "WSMOKE-NAV-01", "WSMOKE-PLP-01", "WSMOKE-PDP-01",
        "WSMOKE-CART-01", "WSMOKE-CART-02", "WSMOKE-CART-03", "WSMOKE-CHECKOUT-01",
    ]
    values = []
    for viewport in ("desktop", "mobile"):
        for case_id in case_ids:
            status = cart_status if viewport == "desktop" and case_id == "WSMOKE-CART-03" else "PASS"
            values.append({"viewport": viewport, "case_id": case_id, "status": status})
    return values


def _record(
    index: int,
    commit: str = "stable-commit",
    *,
    http: dict[str, int] | None = None,
    cart_failure: bool = False,
) -> dict[str, Any]:
    desktop = {"pass": 15, "fail": 0, "blocked": 0}
    mobile = {"pass": 15, "fail": 0, "blocked": 0}
    combined = {"pass": 30, "fail": 0, "blocked": 0}
    failures: list[dict[str, str]] = []
    if cart_failure:
        desktop = {"pass": 14, "fail": 1, "blocked": 0}
        combined = {"pass": 29, "fail": 1, "blocked": 0}
        failures = [{
            "viewport": "desktop",
            "case_id": "WSMOKE-CART-03",
            "status": "FAIL",
            "classification": "AUTOMATION_DEFECT",
        }]
    return {
        "schema_version": STABILITY_SCHEMA_VERSION,
        "suite": "website-smoke-v1",
        "run_id": f"run-{commit}-{index}",
        "eligible": True,
        "eligibility_reason": "complete both run",
        "build_number": index,
        "commit_sha": commit,
        "viewport": "both",
        "trigger": "TIMER",
        "started_at": f"2026-08-21T00:{index:02d}:00+08:00",
        "finished_at": f"2026-08-21T00:{index:02d}:30+08:00",
        "duration_seconds": 30 + index,
        "python_version": "3.12.0",
        "playwright_version": "1.55.0",
        "desktop": desktop,
        "mobile": mobile,
        "combined": combined,
        "pre_clean": "PASS",
        "cleanup": "PASS",
        "http": http or {"403": 0, "429": 0, "5xx": 0},
        "failure_cases": failures,
        "failure_classifications": [item["classification"] for item in failures],
        "case_statuses": _case_statuses("FAIL" if cart_failure else "PASS"),
        "automation_defect_count": len(failures),
        "schema_gate": "PASS",
        "secret_gate": "PASS",
        "artifact_gate": "PASS",
        "python_exit_code": 0 if not cart_failure else 1,
        "jenkins_result": "SUCCESS" if not cart_failure else "FAILURE",
        "overall_status": "PASS" if not cart_failure else "FAIL",
        "source_results": f"artifacts/website-smoke-v1/run-{commit}-{index}/results.json",
    }


def validate_record_shape(record: dict) -> bool:
    required = {
        "schema_version", "suite", "run_id", "eligible", "build_number", "commit_sha",
        "viewport", "trigger", "started_at", "finished_at", "duration_seconds",
        "python_version", "playwright_version", "desktop", "mobile", "combined",
        "pre_clean", "cleanup", "http", "failure_cases", "failure_classifications",
        "schema_gate", "secret_gate", "artifact_gate", "python_exit_code", "jenkins_result",
    }
    ok = check(required.issubset(record), "record required fields")
    ok = check(record.get("suite") == "website-smoke-v1", "record suite") and ok
    ok = check(record.get("viewport") == "both", "record viewport") and ok
    ok = check(set((record.get("http") or {})) == {"403", "429", "5xx"}, "record HTTP metrics") and ok
    ok = check(contains_forbidden_marker(record) is None, "record contains no secret marker") and ok
    return ok


def main() -> int:
    ok_all = True
    print("=== Stability Record / History / Summary Validation ===")
    print()

    stable_records = [_record(index) for index in range(1, 11)]
    result = summarize_records(stable_records, last=10)
    ok_all = check(result["status"] == "STABLE", "Scenario A: 10 successful same-commit builds", result["status"]) and ok_all

    access_records = [_record(index) for index in range(1, 10)]
    access_records.append(_record(10, http={"403": 0, "429": 1, "5xx": 0}))
    result = summarize_records(access_records, last=10)
    ok_all = check(result["status"] == "ACCESS_UNSTABLE", "Scenario B: one 429", result["status"]) and ok_all

    flaky_records = [_record(index, cart_failure=index in (1, 2)) for index in range(1, 11)]
    result = summarize_records(flaky_records, last=10)
    ok_all = check(result["status"] == "FLAKY", "Scenario C: same case has two automation failures", result["status"]) and ok_all

    collecting_records = [_record(index) for index in range(1, 7)]
    result = summarize_records(collecting_records, last=10)
    ok_all = check(result["status"] == "COLLECTING", "Scenario D: six builds", result["status"]) and ok_all

    mixed_records = [_record(index, "old-commit") for index in range(1, 6)]
    mixed_records.extend(_record(index + 5, "new-commit") for index in range(1, 6))
    result = summarize_records(mixed_records, last=10)
    ok_all = check(
        result["status"] == "COLLECTING" and result["baseline_commit"] == "new-commit" and result["eligible_builds"] == 5,
        "Scenario E: mixed commits select newest single baseline",
        f"status={result['status']} baseline={result['baseline_commit']} eligible={result['eligible_builds']}",
    ) and ok_all
    strict_result = summarize_records(mixed_records, last=10, strict_mixed=True)
    ok_all = check(strict_result["status"] == "MIXED_BASELINE", "Scenario E strict mode: mixed baseline", strict_result["status"]) and ok_all

    empty_result = summarize_records([], last=10)
    ok_all = check(empty_result["status"] == "COLLECTING", "Scenario F: missing history", empty_result["status"]) and ok_all

    with tempfile.TemporaryDirectory(prefix="stability_validate_") as temp_dir:
        temp = Path(temp_dir)
        record_path = temp / "stability_record.json"
        history_path = temp / "stability-history.jsonl"
        record = _record(1)
        atomic_write_json(record_path, record)
        write_history_record(history_path, record)
        write_history_record(history_path, record)
        loaded, errors = load_history(history_path)
        ok_all = validate_record_shape(record) and ok_all
        ok_all = check(record_path.exists(), "record atomic write") and ok_all
        ok_all = check(len(loaded) == 1 and not errors, "history UTF-8 JSONL one-build-one-line") and ok_all
        ok_all = check(case_stability(loaded)["WSMOKE-CART-03"]["pass"] == 2, "case stability aggregation") and ok_all
        parsed_line = json.loads(history_path.read_text(encoding="utf-8").splitlines()[0])
        ok_all = check(parsed_line["run_id"] == record["run_id"], "history line is valid JSON object") and ok_all

    print()
    print(f"Stability Validation: {'PASS' if ok_all else 'FAIL'}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
