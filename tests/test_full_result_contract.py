"""A passing local Full run requires complete cases and a real mutation audit."""
import time
from types import SimpleNamespace

import pytest

from scripts.run_website_smoke_v1 import _build_viewport_result, _runner_mutation_summary
from scripts.validate_result_schema import WEBSITE_CASE_IDS, _validate_mutation_summary
from utils.readonly_mutation_guard import merge_transactional_mutation_summaries
from utils.result import CaseResult, iso_now


def _runner(summary=None):
    runner = SimpleNamespace(pre_clean_status="PASS", cleanup_status="PASS", pre_clean_error="",
                             cleanup_error="", cleanup_detail="PASS", cleanup_reports=[],
                             cleanup_artifact=None)
    if summary is not None:
        runner.mutation_summary = lambda: summary
    return runner


def _results(ids=WEBSITE_CASE_IDS):
    now = iso_now()
    return [CaseResult(case_id, case_id, "PASS", now, now, 1) for case_id in ids]


PASS_AUDIT = {"mode": "TRANSACTIONAL_SAFE", "status": "PASS", "expected_mutation": 0,
              "unexpected_mutation": 0, "high_risk_mutation": 0, "blocked_mutation": 0,
              "by_path": []}


@pytest.mark.parametrize("ids", [WEBSITE_CASE_IDS[:3], WEBSITE_CASE_IDS[:-1],
                                  WEBSITE_CASE_IDS + [WEBSITE_CASE_IDS[-1]],
                                  list(reversed(WEBSITE_CASE_IDS))])
def test_incomplete_duplicate_or_reordered_cases_cannot_pass(ids):
    result = _build_viewport_result("desktop", _results(ids), _runner(PASS_AUDIT), {},
                                    iso_now(), time.perf_counter())
    assert result.status == "FAIL"


def test_missing_audit_fails_without_inventing_mutation_events():
    audit = _runner_mutation_summary(_runner())
    assert audit["audit_status"] == "MISSING" and audit["status"] == "FAIL"
    assert audit["unexpected_mutation"] == audit["high_risk_mutation"] == 0
    assert _validate_mutation_summary(audit)
    aggregate = merge_transactional_mutation_summaries([PASS_AUDIT, audit])
    assert aggregate["audit_status"] == "MISSING" and aggregate["status"] == "FAIL"
    assert _validate_mutation_summary(aggregate)
    result = _build_viewport_result("desktop", _results(), _runner(), {}, iso_now(), time.perf_counter())
    assert result.status == "FAIL"


def test_complete_cases_and_available_clean_audit_pass():
    result = _build_viewport_result("desktop", _results(), _runner(PASS_AUDIT), {},
                                    iso_now(), time.perf_counter())
    assert result.status == "PASS"
