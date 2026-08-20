"""校验最近一次 results.json 的 schema 与失败证据。

检查项（仅标准库，无第三方 schema 库）：
  1. 最近一次运行产物结构 + 必填字段（run/viewport/case）
  2. summary 算术（pass + fail + blocked == total）
  3. Case ID 完整性与唯一性（每视口 8 个冻结 ID）
  4. evidence 路径：相对路径、无盘符、无路径穿越
  5. PASS Case 不带证据
  6. 隔离的 FAIL 截图 helper 测试（不访问真实站点）

用法：
    python scripts/validate_result_schema.py
"""

from __future__ import annotations

import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import List

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import json

from utils.browser import close_browser, create_browser
from utils.screenshots import capture_case_failure

ARTIFACT_ROOT = PROJECT_ROOT / "artifacts" / "smoke"
FROZEN_CASE_IDS = [
    "SMOKE-PLP-01", "SMOKE-PLP-02", "SMOKE-PDP-01", "SMOKE-PDP-02",
    "SMOKE-PDP-03", "SMOKE-PDP-04", "SMOKE-CART-01", "SMOKE-CART-02",
]
RUN_REQUIRED = [
    "schema_version", "run_id", "site", "base_url", "started_at",
    "finished_at", "duration_ms", "overall_status", "summary", "viewports",
]
VIEWPORT_REQUIRED = [
    "viewport", "browser", "status", "summary", "pre_clean", "cleanup", "cases",
]
CASE_REQUIRED = [
    "case_id", "name", "status", "started_at", "finished_at", "duration_ms",
    "detail", "failure_classification", "evidence",
]


def latest_run_dir() -> Path:
    runs = sorted(p for p in ARTIFACT_ROOT.iterdir() if p.is_dir())
    if not runs:
        raise FileNotFoundError(f"no run artifact dirs under {ARTIFACT_ROOT}")
    return runs[-1]


def check(ok: bool, label: str, detail: str = "") -> bool:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    return ok


def validate_json(run_dir: Path) -> bool:
    ok_all = True
    with open(run_dir / "results.json", "r", encoding="utf-8") as fh:
        data = json.load(fh)

    for field in RUN_REQUIRED:
        ok_all = check(field in data, f"run field present: {field}") and ok_all

    summary = data.get("summary", {})
    ok_all = check(
        summary.get("pass", 0) + summary.get("fail", 0) + summary.get("blocked", 0)
        == summary.get("total", -1),
        "summary arithmetic",
        f"pass={summary.get('pass')} fail={summary.get('fail')} blocked={summary.get('blocked')} total={summary.get('total')}",
    ) and ok_all
    ok_all = check(data.get("overall_status") in ("PASS", "FAIL"), "overall_status value") and ok_all

    viewports = data.get("viewports", [])
    ok_all = check(len(viewports) >= 1, "viewports present", f"count={len(viewports)}") and ok_all
    for vp in viewports:
        for field in VIEWPORT_REQUIRED:
            ok_all = check(field in vp, f"{vp.get('viewport')} field: {field}") and ok_all
        # Case ID 完整性与唯一性
        ids = [c["case_id"] for c in vp.get("cases", [])]
        ok_all = check(
            sorted(ids) == sorted(FROZEN_CASE_IDS),
            f"{vp.get('viewport')} case IDs complete & unique",
            f"count={len(ids)}",
        ) and ok_all
        for c in vp.get("cases", []):
            for field in CASE_REQUIRED:
                ok_all = check(field in c, f"{c.get('case_id')} field: {field}") and ok_all
            ok_all = check(c.get("status") in ("PASS", "FAIL", "BLOCKED"), f"{c['case_id']} status value") and ok_all
            # PASS 不允许携带证据
            if c.get("status") == "PASS":
                ok_all = check(not c.get("evidence"), f"{c['case_id']} PASS evidence empty") and ok_all
            # 证据路径检查
            for ev in c.get("evidence", []):
                path = ev.get("path", "")
                ok_all = check(not Path(path).is_absolute(), f"{c['case_id']} evidence relative", path) and ok_all
                ok_all = check(".." not in path.split("/"), f"{c['case_id']} evidence no traversal") and ok_all
                ok_all = check(not re.match(r"^[A-Za-z]:[\\/]", path), f"{c['case_id']} evidence no drive letter") and ok_all
        # 视口 summary 算术
        vs = vp.get("summary", {})
        ok_all = check(
            vs.get("pass", 0) + vs.get("fail", 0) + vs.get("blocked", 0) == vs.get("total", -1),
            f"{vp.get('viewport')} summary arithmetic",
        ) and ok_all

    # 安全：JSON 中不允许出现密钥数据 / 绝对路径
    blob = json.dumps(data)
    ok_all = check(".shopify-monitor" not in blob, "no secret file path in JSON") and ok_all
    ok_all = check("Signature-Input" not in blob and "MONDRESSY_US_SHOPIFY" not in blob, "no signature values in JSON") and ok_all
    return ok_all


def validate_screenshot_helper() -> bool:
    ok_all = True
    tmp = Path(tempfile.mkdtemp(prefix="sa_validate_"))
    runtime = None
    try:
        runtime = create_browser("desktop")
        runtime.page.goto("about:blank")
        rel = capture_case_failure(runtime.page, tmp, "desktop", "SMOKE-FAKE-01")
        ok_all = check(rel == "desktop/SMOKE-FAKE-01-failure.png", "synthetic FAIL screenshot relative path", str(rel)) and ok_all
        ok_all = check((tmp / "desktop" / "SMOKE-FAKE-01-failure.png").exists(), "synthetic FAIL screenshot file created") and ok_all
        # 截图错误隔离：产物目录是普通文件时应返回 None 而不是抛异常
        bad_file = tmp / "not-a-dir.txt"
        bad_file.write_text("x", encoding="utf-8")
        try:
            rel2 = capture_case_failure(runtime.page, bad_file, "desktop", "SMOKE-FAKE-02")
            ok_all = check(rel2 is None, "evidence capture error isolation", "returned None") and ok_all
        except Exception as exc:  # noqa: BLE001
            ok_all = check(False, "evidence capture error isolation", f"raised {type(exc).__name__}") and ok_all
    finally:
        if runtime is not None:
            close_browser(runtime)
        shutil.rmtree(tmp, ignore_errors=True)
    return ok_all


def main() -> int:
    print("=== Result Schema Validation ===")
    print()
    ok_all = True
    try:
        run_dir = latest_run_dir()
    except FileNotFoundError as exc:
        print(f"  FAIL  {exc}")
        return 1
    print(f"[{run_dir.name}]")
    ok_all = validate_json(run_dir) and ok_all
    print()
    print("[Screenshot Helper (isolated)]")
    ok_all = validate_screenshot_helper() and ok_all
    print()
    print(f"Result Schema Validation: {'PASS' if ok_all else 'FAIL'}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
