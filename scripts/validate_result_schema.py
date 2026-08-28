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
from pathlib import Path
from typing import List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.screenshots import capture_case_failure

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


def check(ok: bool, label: str, detail: str = "") -> bool:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    return ok


def latest_run_dir(root: Path) -> Path:
    if not root.exists():
        raise FileNotFoundError(f"artifact root not found: {root}")
    runs = sorted(p for p in root.iterdir() if p.is_dir())
    if not runs:
        raise FileNotFoundError(f"no run artifact dirs under {root}")
    return runs[-1]


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


def _validate_evidence(run_dir: Path, case: dict) -> bool:
    ok_all = True
    evidence = case.get("evidence") or []
    for ev in evidence:
        path = str(ev.get("path") or "")
        path_obj = Path(path)
        ok_all = check(bool(path), f"{case.get('case_id')} evidence path present") and ok_all
        ok_all = check(not path_obj.is_absolute(), f"{case.get('case_id')} evidence relative", path) and ok_all
        ok_all = check(".." not in path.replace("\\", "/").split("/"), f"{case.get('case_id')} evidence no traversal") and ok_all
        ok_all = check(not re.match(r"^[A-Za-z]:[\\/]", path), f"{case.get('case_id')} evidence no drive letter") and ok_all
        ok_all = check((run_dir / path).exists(), f"{case.get('case_id')} evidence file exists", path) and ok_all

    capture_error = case.get("evidence_capture_error")
    if case.get("status") == "FAIL":
        ok_all = check(
            bool(evidence) or bool(capture_error),
            f"{case.get('case_id')} FAIL has evidence or capture error",
        ) and ok_all
    if capture_error:
        ok_all = check(not evidence, f"{case.get('case_id')} capture error isolates evidence") and ok_all
    if case.get("status") == "PASS":
        ok_all = check(not evidence and not capture_error, f"{case.get('case_id')} PASS evidence empty") and ok_all
    return ok_all


def validate_json(run_dir: Path, suite: str) -> bool:
    result_path = run_dir / "results.json"
    ok_all = True
    with open(result_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    for field in RUN_REQUIRED:
        ok_all = check(field in data, f"run field present: {field}") and ok_all

    ok_all = _summary_ok(data.get("summary", {}), "run") and ok_all
    ok_all = check(data.get("overall_status") in ("PASS", "FAIL"), "overall_status value") and ok_all

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

        cases = vp.get("cases", [])
        all_cases.extend(cases)
        ids = [c.get("case_id") for c in cases]
        total_case_count += len(cases)
        ok_all = check(
            tuple(ids) == tuple(expected_ids),
            f"{vp_name} case IDs complete and ordered",
            f"count={len(ids)}",
        ) and ok_all
        ok_all = check(len(ids) == len(set(ids)), f"{vp_name} case IDs unique") and ok_all
        for case in cases:
            for field in CASE_REQUIRED:
                # evidence_capture_error is optional for old artifacts, but required
                # for newly written results (schema_version >= 1.1).
                required = field != "evidence_capture_error" or str(data.get("schema_version", "1.0")) >= "1.1"
                if required:
                    ok_all = check(field in case, f"{case.get('case_id')} field: {field}") and ok_all
            ok_all = check(case.get("status") in VALID_CASE_STATUSES, f"{case.get('case_id')} status value") and ok_all
            ok_all = _validate_evidence(run_dir, case) and ok_all
        ok_all = _summary_matches_cases(vp.get("summary", {}), cases, str(vp_name)) and ok_all
        for base_name in ("pre_clean", "cleanup"):
            value = vp.get(base_name) or {}
            ok_all = _validate_readonly_lifecycle(
                suite, str(vp_name), cases, value, base_name, len(expected_ids)
            ) and ok_all

    expected_total = len(expected_ids) * len(viewports)
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
