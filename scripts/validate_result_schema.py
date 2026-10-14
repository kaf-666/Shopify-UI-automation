"""校验 Legacy / Website Smoke V1 / Website Smoke Readonly V1 的 results.json 契约。

默认行为保持向后兼容：无参数时验证 ``artifacts/smoke`` 最近一次结果。
Website Smoke V1 可使用：

    python scripts/validate_result_schema.py --suite website_smoke_v1
    python scripts/validate_result_schema.py --suite website_smoke_v1 --results <path>

Website Smoke Readonly V1 可使用：

    python scripts/validate_result_schema.py --suite website_smoke_readonly_v1
    python scripts/validate_result_schema.py --suite website_smoke_readonly_v1 --results <path>

该脚本默认离线运行，不要求 Signed Request、代理或真实站点可访问。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path, PureWindowsPath
from typing import List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.screenshots import capture_case_failure
from utils.full_cleanup_contract import uses_v3, validate_progress, validate_operations

ARTIFACT_ROOTS = {
    "legacy": PROJECT_ROOT / "artifacts" / "smoke",
    "website_smoke_v1": PROJECT_ROOT / "artifacts" / "website-smoke-v1",
    "website_smoke_readonly_v1": PROJECT_ROOT / "artifacts" / "website-smoke-readonly-v1",
}

LEGACY_CASE_IDS = [
    "SMOKE-PLP-01", "SMOKE-PLP-02", "SMOKE-PDP-01", "SMOKE-PDP-02",
    "SMOKE-PDP-03", "SMOKE-PDP-04", "SMOKE-CART-01", "SMOKE-CART-02",
]
WEBSITE_CASE_IDS = [
    "WSMOKE-DIRECT-01", "WSMOKE-DIRECT-02", "WSMOKE-DIRECT-03",
    "WSMOKE-SEARCH-01", "WSMOKE-SEARCH-02", "WSMOKE-SEARCH-03", "WSMOKE-SEARCH-04",
    "WSMOKE-HOME-01", "WSMOKE-NAV-01", "WSMOKE-PLP-01", "WSMOKE-PDP-01",
    "WSMOKE-CART-01", "WSMOKE-CART-02", "WSMOKE-CART-03", "WSMOKE-CHECKOUT-01",
]
WEBSITE_READONLY_CASE_IDS = (
    "RSMOKE-DIRECT-01", "RSMOKE-DIRECT-02",
    "RSMOKE-SEARCH-01", "RSMOKE-SEARCH-02", "RSMOKE-SEARCH-03", "RSMOKE-SEARCH-04",
    "RSMOKE-HOME-01", "RSMOKE-NAV-01", "RSMOKE-PLP-01", "RSMOKE-PDP-01",
    "RSMOKE-PDP-02",
)

SUITE_CASE_IDS = {
    "legacy": LEGACY_CASE_IDS,
    "website_smoke_v1": WEBSITE_CASE_IDS,
    "website_smoke_readonly_v1": WEBSITE_READONLY_CASE_IDS,
}

RUN_REQUIRED = [
    "schema_version", "run_id", "site", "base_url", "started_at",
    "finished_at", "duration_ms", "overall_status", "runtime", "summary", "viewports",
]
VIEWPORT_REQUIRED = [
    "viewport", "browser", "status", "summary", "pre_clean", "cleanup", "cases",
]
CASE_REQUIRED = [
    "case_id", "name", "status", "started_at", "finished_at", "duration_ms",
    "detail", "failure_classification", "evidence", "evidence_capture_error",
]
VALID_CASE_STATUSES = {"PASS", "FAIL", "BLOCKED"}
VALID_BASE_STATUSES = {"PASS", "FAIL", "BLOCKED"}
VALID_EVIDENCE_TYPES = {
    "screenshot",
    "search_home_url_diagnostic",
    "quantity_timeline",
}
_CAPTURE_WRITE_ERROR_RE = re.compile(
    r"(?:URL_DIAGNOSTIC_SIDECAR_WRITE_FAILED|QUANTITY_DIAGNOSTIC_WRITE_FAILED):"
    r"[A-Za-z][A-Za-z0-9_]{0,63}"
)
_SAFE_CAPTURE_ERROR_LITERALS = {
    "SCREENSHOT_CAPTURE_FAILED",
    # Historical runners wrote this fixed, non-sensitive message.
    "screenshot capture failed",
}


def check(ok: bool, label: str, detail: str = "") -> bool:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    return ok


def latest_run_dir(root: Path) -> Path:
    if not root.exists():
        raise FileNotFoundError(f"artifact root not found: {root}")
    result_paths = [path for path in root.rglob("results.json") if path.is_file()]
    if not result_paths:
        raise FileNotFoundError(f"no results.json artifacts under {root}")
    latest = max(
        result_paths,
        key=lambda path: (path.stat().st_mtime_ns, str(path)),
    )
    return latest.parent


def _summary_ok(summary: dict, label: str) -> bool:
    values = {key: summary.get(key, 0) for key in ("pass", "fail", "blocked")}
    total = summary.get("total", -1)
    return check(
        all(isinstance(v, int) for v in values.values())
        and isinstance(total, int)
        and sum(values.values()) == total,
        f"{label} summary arithmetic",
        f"pass={values['pass']} fail={values['fail']} blocked={values['blocked']} total={total}",
    )


def _summary_matches_cases(summary: dict, cases: list, label: str) -> bool:
    """Validate arithmetic and ensure every status count reflects the case list."""
    ok_all = _summary_ok(summary, label)
    actual = {
        "pass": sum(case.get("status") == "PASS" for case in cases),
        "fail": sum(case.get("status") == "FAIL" for case in cases),
        "blocked": sum(case.get("status") == "BLOCKED" for case in cases),
    }
    for status, count in actual.items():
        ok_all = check(
            summary.get(status) == count,
            f"{label} summary {status} matches cases",
            f"summary={summary.get(status)} cases={count}",
        ) and ok_all
    ok_all = check(
        summary.get("total") == len(cases),
        f"{label} summary total matches cases",
        f"summary={summary.get('total')} cases={len(cases)}",
    ) and ok_all
    return ok_all


def _validate_mutation_summary(value: object) -> bool:
    """Validate the optional Full-suite transactional mutation audit."""
    if isinstance(value, dict) and "api_cleanup" in value:
        if not _validate_api_cleanup(value["api_cleanup"]):
            return check(False, "API cleanup audit lifecycle")
    if value is None:
        # Keep validation compatible with historical Full artifacts written
        # before the transactional policy field was introduced.
        return True
    if not isinstance(value, dict):
        return check(False, "mutation_summary mapping")

    ok_all = check(
        value.get("mode") == "TRANSACTIONAL_SAFE",
        "mutation_summary mode",
        "expected=TRANSACTIONAL_SAFE",
    )
    counts = {}
    for key in (
        "expected_mutation",
        "unexpected_mutation",
        "high_risk_mutation",
        "blocked_mutation",
    ):
        raw = value.get(key)
        valid = isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0
        ok_all = check(valid, f"mutation_summary {key}", "non-negative integer") and ok_all
        counts[key] = raw if valid else 0
    expected_status = "PASS" if counts["unexpected_mutation"] == 0 and counts["high_risk_mutation"] == 0 else "FAIL"
    ok_all = check(
        value.get("status") == expected_status,
        "mutation_summary status",
        f"expected={expected_status}",
    ) and ok_all
    rows = value.get("by_path", [])
    ok_all = check(isinstance(rows, list), "mutation_summary by_path list") and ok_all
    if isinstance(rows, list):
        for index, row in enumerate(rows):
            valid_row = isinstance(row, dict)
            if valid_row:
                valid_row = (
                    row.get("classification") in {
                        "EXPECTED_MUTATION",
                        "KNOWN_BLOCKED_SIDE_EFFECT",
                        "UNEXPECTED_MUTATION",
                        "HIGH_RISK_MUTATION",
                    }
                    and str(row.get("method") or "") in {"POST", "PUT", "PATCH", "DELETE"}
                    and str(row.get("path") or "").startswith("/")
                    and isinstance(row.get("count"), int)
                    and not isinstance(row.get("count"), bool)
                    and row.get("count") >= 0
                )
                if valid_row and "host_class" in row:
                    valid_row = row.get("host_class") in {"FIRST_PARTY", "THIRD_PARTY", "UNKNOWN"}
                if valid_row and "same_origin" in row:
                    valid_row = isinstance(row.get("same_origin"), bool) or row.get("same_origin") == "UNKNOWN"
                if valid_row and "sanitized_path" in row:
                    valid_row = (
                        isinstance(row.get("sanitized_path"), str)
                        and row.get("sanitized_path", "").startswith("/")
                        and row.get("path") == row.get("sanitized_path")
                    )
                if valid_row and "path_hash" in row:
                    valid_row = (
                        isinstance(row.get("path_hash"), str)
                        and (row.get("path_hash") == "UNKNOWN" or bool(re.fullmatch(r"[0-9a-f]{12}", row.get("path_hash", ""))))
                    )
                if valid_row and "query_present" in row:
                    valid_row = isinstance(row.get("query_present"), bool) or row.get("query_present") == "UNKNOWN"
                if valid_row and "reason" in row:
                    valid_row = row.get("reason") in {
                        "API_CLEANUP_EXPECTED_MUTATION",
                        "EXPECTED_CART_MUTATION",
                        "EXPECTED_TRANSACTIONAL_MUTATION",
                        "FIRST_PARTY_WATI_ADD_TO_CART_EVENT",
                        "FIRST_PARTY_LOCALIZATION_CONTEXT",
                        "MONDRESSY_WEBREX_LOCALIZATION_CONTEXT",
                        "HIGH_RISK_PRECEDENCE",
                        "PATH_NOT_ALLOWED",
                        "REDACTED",
                    }
                for viewport_key in ("desktop_count", "mobile_count", "blocked_count"):
                    if valid_row and viewport_key in row:
                        count_value = row.get(viewport_key)
                        valid_row = (
                            count_value == "UNKNOWN"
                            or (
                                isinstance(count_value, int)
                                and not isinstance(count_value, bool)
                                and count_value >= 0
                            )
                        )
                for boolean_key in (
                    "blocked",
                    "recognized",
                    "allowed_to_send",
                    "gating_failure",
                ):
                    if valid_row and boolean_key in row:
                        valid_row = isinstance(row.get(boolean_key), bool)
            ok_all = check(valid_row, f"mutation_summary by_path[{index}] contract") and ok_all
    if "known_blocked_side_effect" in value:
        raw = value.get("known_blocked_side_effect")
        valid = isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0
        ok_all = check(
            valid,
            "mutation_summary known_blocked_side_effect",
            "non-negative integer",
        ) and ok_all
    if "api_cleanup" in value and isinstance(rows, list):
        attempts = value["api_cleanup"]["post_invocations"]
        api_count = sum(row.get("count", 0) for row in rows
                        if isinstance(row, dict) and row.get("reason") == "API_CLEANUP_EXPECTED_MUTATION"
                        and type(row.get("count")) is int)
        ok_all = check(api_count == attempts and counts["expected_mutation"] >= attempts,
                       "API cleanup invocation count matches mutation audit") and ok_all
    return ok_all


def _validate_readonly_lifecycle(
    suite: str, viewport: str, cases: list, value: dict, field: str, expected_count: int
) -> bool:
    """Readonly completed viewports must retain non-cart lifecycle stubs."""
    if suite != "website_smoke_readonly_v1" or len(cases) != expected_count:
        return True
    expected_detail = "readonly_not_required"
    ok_all = check(
        value.get("status") == "PASS",
        f"{viewport} {field} readonly status",
        "expected=PASS",
    )
    ok_all = check(
        value.get("detail") == expected_detail,
        f"{viewport} {field} readonly detail",
        f"expected={expected_detail}",
    ) and ok_all
    return ok_all


def _schema_version_at_least(value: object, required: tuple[int, int]) -> bool:
    match = re.fullmatch(r"(\d+)\.(\d+)(?:\.\d+)?", str(value or "1.0"))
    return bool(match and (int(match.group(1)), int(match.group(2))) >= required)


def _safe_capture_error(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    codes = value.split("; ")
    return bool(codes) and all(
        code in _SAFE_CAPTURE_ERROR_LITERALS
        or _CAPTURE_WRITE_ERROR_RE.fullmatch(code) is not None
        for code in codes
    )


def _expected_evidence_path(
    evidence_type: object, viewport: object, case_id: object
) -> str | None:
    if viewport not in {"desktop", "mobile"} or not isinstance(case_id, str):
        return None
    suffix = {
        "screenshot": "-failure.png",
        "search_home_url_diagnostic": "-search-home-url.json",
        "quantity_timeline": "-quantity-timeline.json",
    }.get(evidence_type)
    if suffix is None:
        return None
    if evidence_type == "search_home_url_diagnostic" and not case_id.startswith(
        ("WSMOKE-SEARCH-", "RSMOKE-SEARCH-")
    ):
        return None
    if evidence_type == "quantity_timeline" and case_id != "WSMOKE-CART-03":
        return None
    return f"{viewport}/{case_id}{suffix}"


def _validate_evidence(
    run_dir: Path,
    case: dict,
    *,
    viewport: object = None,
    strict_capture_error_codes: bool = False,
) -> bool:
    ok_all = True
    evidence_value = case.get("evidence")
    evidence_is_list = isinstance(evidence_value, list)
    ok_all = check(evidence_is_list, f"{case.get('case_id')} evidence list") and ok_all
    evidence = evidence_value if evidence_is_list else []
    for index, ev in enumerate(evidence):
        prefix = f"{case.get('case_id')} evidence[{index}]"
        record_is_valid = isinstance(ev, dict) and set(ev) == {"type", "path"}
        ok_all = check(record_is_valid, f"{prefix} record shape") and ok_all
        if not record_is_valid:
            continue

        evidence_type = ev.get("type")
        valid_type = isinstance(evidence_type, str) and evidence_type in VALID_EVIDENCE_TYPES
        ok_all = check(valid_type, f"{prefix} type") and ok_all
        path = ev.get("path")
        valid_path_value = isinstance(path, str) and bool(path.strip())
        ok_all = check(valid_path_value, f"{prefix} path present") and ok_all
        if not valid_path_value:
            continue

        try:
            path_obj = Path(path)
            windows_path = PureWindowsPath(path)
        except (TypeError, ValueError):
            ok_all = check(False, f"{prefix} path syntax") and ok_all
            continue

        normalized_path = path.replace("\\", "/")
        is_absolute = (
            path_obj.is_absolute()
            or windows_path.is_absolute()
            or bool(windows_path.drive)
        )
        ok_all = check(not is_absolute, f"{prefix} relative") and ok_all
        has_traversal = ".." in normalized_path.split("/")
        ok_all = check(not has_traversal, f"{prefix} no traversal") and ok_all
        expected_path = (
            _expected_evidence_path(evidence_type, viewport, case.get("case_id"))
            if valid_type
            else None
        )
        ok_all = check(
            expected_path is not None and normalized_path == expected_path,
            f"{prefix} path matches type and case",
        ) and ok_all

        candidate = run_dir / path_obj
        try:
            resolved_run_dir = run_dir.resolve(strict=True)
            resolved_candidate = candidate.resolve(strict=True)
            within_run = resolved_candidate.is_relative_to(resolved_run_dir)
            is_file = resolved_candidate.is_file()
        except (OSError, RuntimeError, ValueError):
            within_run = False
            is_file = False
        ok_all = check(within_run, f"{prefix} remains inside run") and ok_all
        ok_all = check(is_file, f"{prefix} file exists") and ok_all

    capture_error = case.get("evidence_capture_error")
    capture_error_type_valid = capture_error is None or isinstance(capture_error, str)
    ok_all = check(
        capture_error_type_valid,
        f"{case.get('case_id')} capture error type",
    ) and ok_all
    has_capture_error = bool(capture_error)
    if strict_capture_error_codes and capture_error is not None:
        ok_all = check(
            not capture_error or _safe_capture_error(capture_error),
            f"{case.get('case_id')} capture error uses safe code",
        ) and ok_all
    if case.get("status") == "FAIL":
        ok_all = check(
            bool(evidence) or has_capture_error,
            f"{case.get('case_id')} FAIL has evidence or capture error",
        ) and ok_all
    if case.get("status") == "PASS":
        ok_all = check(
            not evidence and not has_capture_error,
            f"{case.get('case_id')} PASS evidence empty",
        ) and ok_all
    return ok_all


def _validate_api_cleanup(value):
    if not isinstance(value, dict):
        return False
    keys = ("authorization_allowed", "authorization_denied", "post_invocations",
            "responses_received", "outcome_unknown")
    if set(value) != {*keys, "records"} or not isinstance(value["records"], list):
        return False
    records = value["records"]
    for record in records:
        legacy_keys = {"authorization", "dispatch", "response", "http_status", "viewport"}
        new_keys = legacy_keys | {"operation_id", "cleanup_stage", "attempt_id"}
        if not isinstance(record, dict) or set(record) not in (legacy_keys, new_keys):
            return False
        if set(record) == new_keys:
            operation = record["operation_id"]
            if (type(record["attempt_id"]) is not int or record["attempt_id"] <= 0
                    or (operation is None and record["cleanup_stage"] is not None)
                    or (operation is not None and (not isinstance(operation, str)
                        or not re.fullmatch(r"[0-9a-f]{32}", operation)
                        or record["cleanup_stage"] not in {"pre_clean", "direct", "search", "browse", "cleanup"}))):
                return False
        if (record["authorization"] not in {"ALLOWED", "DENIED"}
                or record["dispatch"] not in {"NOT_ATTEMPTED", "ATTEMPTED"}
                or record["response"] not in {"NOT_RECEIVED", "OUTCOME_UNKNOWN", "RESPONSE_RECEIVED"}
                or record["viewport"] not in {None, "desktop", "mobile"}):
            return False
        if record["dispatch"] == "NOT_ATTEMPTED":
            if record["response"] != "NOT_RECEIVED" or record["http_status"] is not None:
                return False
        elif record["authorization"] != "ALLOWED" or record["response"] == "NOT_RECEIVED":
            return False
        if record["response"] == "RESPONSE_RECEIVED":
            if type(record["http_status"]) is not int or not 100 <= record["http_status"] <= 599:
                return False
        elif record["http_status"] is not None:
            return False
    expected = {
        "authorization_allowed": sum(r["authorization"] == "ALLOWED" for r in records),
        "authorization_denied": sum(r["authorization"] == "DENIED" for r in records),
        "post_invocations": sum(r["dispatch"] == "ATTEMPTED" for r in records),
        "responses_received": sum(r["response"] == "RESPONSE_RECEIVED" for r in records),
        "outcome_unknown": sum(r["response"] == "OUTCOME_UNKNOWN" for r in records),
    }
    return all(type(value[k]) is int and value[k] == expected[k] for k in keys)


def _validate_cleanup_audit(run_dir: Path, viewport: object, value: dict, *,
                            viewport_data=None, fatal=None, overall=None) -> bool:
    """Validate the opt-in Full cleanup extension; old reports stay compatible."""
    vp = viewport_data or {}
    mutation = vp.get("mutation_summary")
    v3 = uses_v3(value, mutation)
    if "journeys" not in value and "audit_path" not in value:
        return check(not v3, "CLEANUP_AUDIT_CONSISTENCY_ERROR")
    if v3:
        if not (validate_progress(value, vp, vp.get("cases", []), vp.get("partial", False),
                                  fatal, overall, WEBSITE_CASE_IDS)
                and validate_operations(value, mutation, viewport)):
            return check(False, "CLEANUP_AUDIT_CONSISTENCY_ERROR")
    from utils.cart_cleanup import SAFE_CLEANUP_ERRORS, cleanup_report

    reports = value.get("journeys")
    valid = isinstance(reports, list) and bool(reports)
    if not valid:
        return check(False, "Full cleanup audit records")
    journeys = []
    for report in reports:
        expected_keys = set(cleanup_report("", ""))
        if not v3:
            expected_keys -= {"operation_id", "attempt_id"}
        from utils.full_cleanup_contract import validate_cleanup_diagnostics
        shape = (isinstance(report, dict)
                 and set(report) in (expected_keys, expected_keys | {"diagnostics"})
                 and validate_cleanup_diagnostics(report))
        valid = shape and valid
        if not shape:
            continue
        text_fields = ("journey", "viewport", "status", "precheck_status",
                       "api_policy_decision", "clear_response_category",
                       "postcheck_status", "safe_error_code")
        if not all(isinstance(report[key], str) for key in text_fields):
            valid = False
            continue
        count = report["cart_item_count"]
        valid = all((
            report["journey"] in {"pre_clean", "direct", "search", "browse"},
            report["viewport"] == viewport,
            report["status"] in VALID_BASE_STATUSES,
            report["precheck_status"] in {"PASS", "BLOCKED"},
            count is None or (type(count) is int and count >= 0),
            type(report["clear_requested"]) is bool,
            report["api_policy_decision"] in {"NOT_REQUESTED", "DENIED", "API_CLEANUP_EXPECTED_MUTATION"},
            report["clear_response_category"] in {"NOT_REQUESTED", "HTTP_200", "HTTP_OR_REDIRECT_FAILURE", "REQUEST_FAILED"},
            report["postcheck_status"] in {"NOT_RUN", "PASS", "FAIL", "BLOCKED"},
            report["safe_error_code"] in SAFE_CLEANUP_ERRORS,
        )) and valid
        if report["status"] == "PASS":
            valid = (report["precheck_status"] == "PASS" and report["postcheck_status"] == "PASS"
                     and report["safe_error_code"] == "NONE" and type(count) is int) and valid
        else:
            valid = report["safe_error_code"] != "NONE" and valid
        if report["clear_requested"]:
            valid = (report["api_policy_decision"] == "API_CLEANUP_EXPECTED_MUTATION"
                     and type(count) is int and count > 0) and valid
            if report["status"] == "PASS":
                valid = report["clear_response_category"] == "HTTP_200" and valid
        elif report["status"] == "PASS":
            valid = (count == 0) and valid
        if not report["clear_requested"]:
            valid = report["clear_response_category"] == "NOT_REQUESTED" and valid
        journeys.append(report["journey"])
    valid = len(journeys) == len(set(journeys)) and valid
    expected_status = ("FAIL" if any(r.get("status") == "FAIL" for r in reports if isinstance(r, dict))
                       else "BLOCKED" if any(r.get("status") == "BLOCKED" for r in reports if isinstance(r, dict))
                       else "PASS")
    path = value.get("audit_path")
    if path is None:
        valid = (value.get("status") == "FAIL" and value.get("detail") == "CLEANUP_AUDIT_WRITE_FAILED") and valid
    else:
        valid = value.get("status") == expected_status and valid
        if value.get("status") == "PASS" and not v3:
            valid = journeys == ["pre_clean", "direct", "search", "browse"] and valid
        if path != f"{viewport}/cart-cleanup.json":
            valid = False
        else:
            try:
                resolved = (run_dir / path).resolve(strict=True)
                valid = resolved.is_relative_to(run_dir.resolve(strict=True)) and resolved.is_file() and valid
                if valid:
                    audit = json.loads(resolved.read_text(encoding="utf-8"))
                    valid = audit.get("viewport") == viewport and audit.get("status") == value.get("status") and audit.get("journeys") == reports
                    if v3:
                        valid = (audit.get("contract_version") == 3 and
                                 audit.get("progress") == value.get("progress") and valid)
            except (OSError, ValueError, RuntimeError, TypeError, AttributeError):
                valid = False
    return check(valid, "Full cleanup audit contract")


def validate_json(run_dir: Path, suite: str) -> bool:
    result_path = run_dir / "results.json"
    ok_all = True
    with open(result_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    for field in RUN_REQUIRED:
        ok_all = check(field in data, f"run field present: {field}") and ok_all

    ok_all = _summary_ok(data.get("summary", {}), "run") and ok_all
    ok_all = check(data.get("overall_status") in ("PASS", "FAIL"), "overall_status value") and ok_all
    ok_all = _validate_mutation_summary(data.get("mutation_summary")) and ok_all

    fatal = data.get("fatal_error")
    if fatal is not None:
        ok_all = check(isinstance(fatal, dict), "fatal_error mapping") and ok_all
        if isinstance(fatal, dict):
            ok_all = check(bool(fatal.get("classification")), "fatal_error classification") and ok_all
            ok_all = check(bool(fatal.get("message")), "fatal_error message") and ok_all
            ok_all = check(data.get("overall_status") == "FAIL", "fatal run is FAIL") and ok_all

    expected_ids = SUITE_CASE_IDS[suite]
    viewports = data.get("viewports", [])
    allowed_viewport_counts = (0, 1, 2) if fatal is not None else (1, 2)
    ok_all = check(len(viewports) in allowed_viewport_counts, "viewports count", str(len(viewports))) and ok_all
    if suite in ("website_smoke_v1", "website_smoke_readonly_v1"):
        ok_all = check(all(v.get("viewport") in ("desktop", "mobile") for v in viewports), "Website viewport names") and ok_all

    total_case_count = 0
    all_cases = []
    for vp in viewports:
        vp_name = vp.get("viewport")
        for field in VIEWPORT_REQUIRED:
            ok_all = check(field in vp, f"{vp_name} field: {field}") and ok_all
        ok_all = check(vp.get("status") in ("PASS", "FAIL"), f"{vp_name} status value") and ok_all
        for base_name in ("pre_clean", "cleanup"):
            value = vp.get(base_name) or {}
            ok_all = check(isinstance(value, dict), f"{vp_name} {base_name} mapping") and ok_all
            if isinstance(value, dict):
                ok_all = check(value.get("status") in VALID_BASE_STATUSES, f"{vp_name} {base_name} status") and ok_all
                if suite == "website_smoke_v1" and base_name == "cleanup":
                    ok_all = _validate_cleanup_audit(run_dir, vp_name, value, viewport_data=vp,
                                                     fatal=fatal, overall=data.get("overall_status")) and ok_all

        cases = vp.get("cases", [])
        partial = vp.get("partial", False)
        ok_all = check(type(partial) is bool and (not partial or (suite == "website_smoke_v1" and fatal is not None and vp.get("status") == "FAIL")),
                       f"{vp_name} partial requires fatal FAIL") and ok_all
        if suite == "website_smoke_v1":
            unsafe = (any((vp.get(k) or {}).get("status") != "PASS" for k in ("pre_clean", "cleanup"))
                      or (vp.get("mutation_summary") or {}).get("status", "PASS") != "PASS"
                      or any(c.get("status") != "PASS" for c in cases))
            ok_all = check(not unsafe or vp.get("status") == "FAIL", f"{vp_name} cross-layer status") and ok_all
            cleanup = vp.get("cleanup") or {}
            if cleanup.get("audit_path"):
                try:
                    audit_file = (run_dir / cleanup["audit_path"]).resolve(strict=True)
                    if (cleanup["audit_path"] != f"{vp_name}/cart-cleanup.json"
                            or not audit_file.is_relative_to(run_dir.resolve(strict=True))):
                        raise ValueError
                    audit = json.loads(audit_file.read_text(encoding="utf-8"))
                    ok_all = check(audit.get("mutation_summary") == vp.get("mutation_summary"),
                                   f"{vp_name} audit mutation summary matches") and ok_all
                except (OSError, ValueError, TypeError):
                    ok_all = check(False, f"{vp_name} audit readable") and ok_all
        all_cases.extend(cases)
        ids = [c.get("case_id") for c in cases]
        total_case_count += len(cases)
        ok_all = check(
            (tuple(ids) == tuple(cid for cid in expected_ids if cid in ids)
             if partial else tuple(ids) == tuple(expected_ids)),
            f"{vp_name} case IDs complete and ordered",
            f"count={len(ids)}",
        ) and ok_all
        ok_all = check(len(ids) == len(set(ids)), f"{vp_name} case IDs unique") and ok_all
        for case in cases:
            for field in CASE_REQUIRED:
                # evidence_capture_error is optional for old artifacts, but required
                # for newly written results (schema_version >= 1.1).
                required = field != "evidence_capture_error" or _schema_version_at_least(
                    data.get("schema_version", "1.0"), (1, 1)
                )
                if required:
                    ok_all = check(field in case, f"{case.get('case_id')} field: {field}") and ok_all
            ok_all = check(case.get("status") in VALID_CASE_STATUSES, f"{case.get('case_id')} status value") and ok_all
            ok_all = _validate_evidence(
                run_dir,
                case,
                viewport=vp_name,
                strict_capture_error_codes=_schema_version_at_least(
                    data.get("schema_version", "1.0"), (1, 1)
                ),
            ) and ok_all
        ok_all = _summary_matches_cases(vp.get("summary", {}), cases, str(vp_name)) and ok_all
        for base_name in ("pre_clean", "cleanup"):
            value = vp.get(base_name) or {}
            ok_all = _validate_readonly_lifecycle(
                suite, str(vp_name), cases, value, base_name, len(expected_ids)
            ) and ok_all
        if vp.get("mutation_summary") is not None:
            ok_all = _validate_mutation_summary(vp.get("mutation_summary")) and ok_all

    if suite == "website_smoke_v1":
        unsafe_run = (any(v.get("status") != "PASS" for v in viewports)
                      or (data.get("mutation_summary") or {}).get("status", "PASS") != "PASS")
        ok_all = check(not unsafe_run or data.get("overall_status") == "FAIL", "run cross-layer status") and ok_all
    expected_total = sum(len(v.get("cases", [])) if v.get("partial") is True
                         else len(expected_ids) for v in viewports)
    ok_all = check(total_case_count == expected_total, "case count matches viewport count", f"{total_case_count}/{expected_total}") and ok_all
    top_summary = data.get("summary", {})
    ok_all = _summary_matches_cases(top_summary, all_cases, "run") and ok_all
    ok_all = check(top_summary.get("total") == total_case_count, "top summary total matches cases") and ok_all

    # Security: values / local paths must not enter artifacts. Header names may
    # occur in diagnostics, but known secret sources and local binding markers may not.
    blob = json.dumps(data, ensure_ascii=False)
    forbidden = [
        ".shopify-monitor",
        "C:\\Users\\",
        "MONDRESSY_US_SHOPIFY_SIGNATURE=",
        "Signature: ",
        "Signature-Input: ",
        "Signature-Agent: ",
    ]
    for needle in forbidden:
        ok_all = check(needle not in blob, f"JSON does not contain secret marker: {needle}") and ok_all
    return ok_all


class _FakePage:
    """离线测试 screenshot helper，不启动浏览器也能覆盖文件隔离逻辑。"""

    def screenshot(self, path: str, full_page: bool = False):
        Path(path).write_bytes(b"synthetic screenshot")


def validate_screenshot_helper() -> bool:
    ok_all = True
    tmp = Path(tempfile.mkdtemp(prefix="sa_validate_"))
    try:
        page = _FakePage()
        rel = capture_case_failure(page, tmp, "desktop", "SMOKE-FAKE-01")
        ok_all = check(rel == "desktop/SMOKE-FAKE-01-failure.png", "synthetic FAIL screenshot relative path", str(rel)) and ok_all
        ok_all = check((tmp / "desktop" / "SMOKE-FAKE-01-failure.png").exists(), "synthetic FAIL screenshot file created") and ok_all
        bad_file = tmp / "not-a-dir.txt"
        bad_file.write_text("x", encoding="utf-8")
        rel2 = capture_case_failure(page, bad_file, "desktop", "SMOKE-FAKE-02")
        ok_all = check(rel2 is None, "evidence capture error isolation", "returned None") and ok_all
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return ok_all


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Result schema validation")
    parser.add_argument("--suite", choices=sorted(ARTIFACT_ROOTS), default="legacy")
    parser.add_argument("--results", help="explicit results.json path")
    args = parser.parse_args(argv)

    print("=== Result Schema Validation ===")
    print()
    try:
        result_path = Path(args.results) if args.results else latest_run_dir(ARTIFACT_ROOTS[args.suite]) / "results.json"
        run_dir = result_path.parent
        if not result_path.exists():
            raise FileNotFoundError(result_path)
    except FileNotFoundError as exc:
        print(f"  FAIL  {exc}")
        return 1

    print(f"[{run_dir.name}] suite={args.suite}")
    ok_all = validate_json(run_dir, args.suite)
    print()
    print("[Screenshot Helper (offline isolated)]")
    ok_all = validate_screenshot_helper() and ok_all
    print()
    print(f"Result Schema Validation: {'PASS' if ok_all else 'FAIL'}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
