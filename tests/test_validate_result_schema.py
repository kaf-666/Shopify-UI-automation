"""Result schema contract tests for Full and Readonly Website Smoke suites."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts import validate_result_schema as schema


def _case(case_id: str, status: str = "PASS") -> dict:
    return {
        "case_id": case_id,
        "name": case_id,
        "status": status,
        "started_at": "2026-08-28T00:00:00.000+08:00",
        "finished_at": "2026-08-28T00:00:00.001+08:00",
        "duration_ms": 1,
        "detail": "ok" if status == "PASS" else "blocked cart mutation: POST /cart/add.js",
        "failure_classification": None,
        "blocked_by": [],
        "evidence": [],
        "evidence_capture_error": None,
    }


def _summary(cases: list[dict]) -> dict:
    return {
        "pass": sum(case["status"] == "PASS" for case in cases),
        "fail": sum(case["status"] == "FAIL" for case in cases),
        "blocked": sum(case["status"] == "BLOCKED" for case in cases),
        "total": len(cases),
    }


def _viewport(
    viewport: str = "desktop",
    cases: list[dict] | None = None,
    case_ids: tuple[str, ...] | list[str] | None = None,
) -> dict:
    if cases is None:
        case_ids = schema.WEBSITE_READONLY_CASE_IDS if case_ids is None else case_ids
        cases = [_case(case_id) for case_id in case_ids]
    return {
        "viewport": viewport,
        "browser": {"engine": "chromium" if viewport == "desktop" else "webkit"},
        "status": "PASS",
        "started_at": "2026-08-28T00:00:00.000+08:00",
        "finished_at": "2026-08-28T00:00:00.001+08:00",
        "duration_ms": 1,
        "summary": _summary(cases),
        "pre_clean": {"status": "PASS", "detail": "readonly_not_required"},
        "cleanup": {"status": "PASS", "detail": "readonly_not_required"},
        "cases": cases,
    }


def _readonly_result(viewports: list[dict] | None = None) -> dict:
    viewports = [_viewport()] if viewports is None else viewports
    all_cases = [case for viewport in viewports for case in viewport["cases"]]
    return {
        "schema_version": "1.1",
        "run_id": "readonly-fixture",
        "site": "mondressy",
        "base_url": "https://mondressy.com",
        "started_at": "2026-08-28T00:00:00.000+08:00",
        "finished_at": "2026-08-28T00:00:00.001+08:00",
        "duration_ms": 1,
        "overall_status": "PASS",
        "runtime": {"fixture": True},
        "summary": _summary(all_cases),
        "viewports": viewports,
        "fatal_error": None,
    }


def _full_result() -> dict:
    viewport = _viewport(case_ids=schema.WEBSITE_CASE_IDS)
    all_cases = viewport["cases"]
    return {
        "schema_version": "1.1",
        "run_id": "full-fixture",
        "site": "mondressy",
        "base_url": "https://mondressy.com",
        "started_at": "2026-08-28T00:00:00.000+08:00",
        "finished_at": "2026-08-28T00:00:00.001+08:00",
        "duration_ms": 1,
        "overall_status": "PASS",
        "runtime": {"fixture": True},
        "summary": _summary(all_cases),
        "viewports": [viewport],
        "fatal_error": None,
    }


def _write_result(tmp_path: Path, data: dict) -> Path:
    run_dir = tmp_path / "readonly-fixture"
    run_dir.mkdir()
    result_path = run_dir / "results.json"
    result_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return result_path


def _refresh_summaries(data: dict) -> None:
    all_cases = []
    for viewport in data["viewports"]:
        viewport["summary"] = _summary(viewport["cases"])
        viewport["status"] = "FAIL" if any(case["status"] != "PASS" for case in viewport["cases"]) else "PASS"
        all_cases.extend(viewport["cases"])
    data["summary"] = _summary(all_cases)
    data["overall_status"] = "FAIL" if any(viewport["status"] == "FAIL" for viewport in data["viewports"]) else "PASS"


def test_readonly_suite_has_separate_root_and_frozen_case_contract() -> None:
    assert schema.ARTIFACT_ROOTS["website_smoke_v1"] != schema.ARTIFACT_ROOTS["website_smoke_readonly_v1"]
    assert schema.ARTIFACT_ROOTS["website_smoke_readonly_v1"].as_posix().endswith(
        "artifacts/website-smoke-readonly-v1"
    )
    assert tuple(schema.SUITE_CASE_IDS["website_smoke_readonly_v1"]) == (
        "RSMOKE-DIRECT-01",
        "RSMOKE-DIRECT-02",
        "RSMOKE-SEARCH-01",
        "RSMOKE-SEARCH-02",
        "RSMOKE-SEARCH-03",
        "RSMOKE-SEARCH-04",
        "RSMOKE-HOME-01",
        "RSMOKE-NAV-01",
        "RSMOKE-PLP-01",
        "RSMOKE-PDP-01",
        "RSMOKE-PDP-02",
    )


def test_readonly_desktop_fixture_passes(tmp_path: Path) -> None:
    result_path = _write_result(tmp_path, _readonly_result())
    assert schema.validate_json(result_path.parent, "website_smoke_readonly_v1")


def test_readonly_both_fixture_passes_with_top_total_22(tmp_path: Path) -> None:
    data = _readonly_result([_viewport("desktop"), _viewport("mobile")])
    assert data["summary"]["total"] == 22
    result_path = _write_result(tmp_path, data)
    assert schema.validate_json(result_path.parent, "website_smoke_readonly_v1")


def test_readonly_missing_case_fails_schema(tmp_path: Path) -> None:
    data = _readonly_result()
    data["viewports"][0]["cases"].pop()
    _refresh_summaries(data)
    result_path = _write_result(tmp_path, data)
    assert not schema.validate_json(result_path.parent, "website_smoke_readonly_v1")


def test_readonly_extra_case_fails_schema(tmp_path: Path) -> None:
    data = _readonly_result()
    data["viewports"][0]["cases"].append(_case("RSMOKE-EXTRA-01"))
    _refresh_summaries(data)
    result_path = _write_result(tmp_path, data)
    assert not schema.validate_json(result_path.parent, "website_smoke_readonly_v1")


def test_readonly_wrong_order_fails_schema(tmp_path: Path) -> None:
    data = _readonly_result()
    cases = data["viewports"][0]["cases"]
    cases[0], cases[1] = cases[1], cases[0]
    result_path = _write_result(tmp_path, data)
    assert not schema.validate_json(result_path.parent, "website_smoke_readonly_v1")


def test_readonly_duplicate_case_id_fails_schema(tmp_path: Path) -> None:
    data = _readonly_result()
    cases = data["viewports"][0]["cases"]
    cases[1]["case_id"] = cases[0]["case_id"]
    result_path = _write_result(tmp_path, data)
    assert not schema.validate_json(result_path.parent, "website_smoke_readonly_v1")


@pytest.mark.parametrize("field", ["pre_clean", "cleanup"])
def test_readonly_stub_boundary_is_enforced(tmp_path: Path, field: str) -> None:
    data = _readonly_result()
    data["viewports"][0][field]["detail"] = "api_clear_after_verified_residual_item"
    result_path = _write_result(tmp_path, data)
    assert not schema.validate_json(result_path.parent, "website_smoke_readonly_v1")


def test_readonly_business_fail_is_structurally_valid(tmp_path: Path) -> None:
    data = _readonly_result()
    mutation_case = data["viewports"][0]["cases"][-1]
    mutation_case["status"] = "FAIL"
    mutation_case["failure_classification"] = "READONLY_MUTATION_VIOLATION"
    mutation_case["evidence_capture_error"] = "synthetic capture unavailable"
    _refresh_summaries(data)
    result_path = _write_result(tmp_path, data)
    assert schema.validate_json(result_path.parent, "website_smoke_readonly_v1")


def test_readonly_summary_mismatch_fails_schema(tmp_path: Path) -> None:
    data = _readonly_result()
    data["summary"]["total"] = 10
    result_path = _write_result(tmp_path, data)
    assert not schema.validate_json(result_path.parent, "website_smoke_readonly_v1")


def test_readonly_fatal_without_viewports_is_compatible(tmp_path: Path) -> None:
    data = _readonly_result([])
    data["overall_status"] = "FAIL"
    data["fatal_error"] = {"classification": "RUNTIME_ERROR", "message": "synthetic fatal"}
    result_path = _write_result(tmp_path, data)
    assert schema.validate_json(result_path.parent, "website_smoke_readonly_v1")


def test_full_fixture_still_passes_website_smoke_v1_contract(tmp_path: Path) -> None:
    result_path = _write_result(tmp_path, _full_result())
    assert schema.validate_json(result_path.parent, "website_smoke_v1")


def test_readonly_cli_suite_accepts_explicit_fixture(tmp_path: Path) -> None:
    data = copy.deepcopy(_readonly_result())
    result_path = _write_result(tmp_path, data)
    assert schema.main(
        ["--suite", "website_smoke_readonly_v1", "--results", str(result_path)]
    ) == 0
