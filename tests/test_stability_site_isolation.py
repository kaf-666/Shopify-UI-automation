"""Stability records must never combine separate storefront sites."""

from __future__ import annotations

from utils.stability import merge_records, summarize_records


def _record(site: str, index: int) -> dict:
    return {
        "suite": "website-smoke-v1",
        "site": site,
        "run_id": f"shared-run-{index}",
        "source_results": "artifacts/website-smoke-v1/shared/results.json",
        "eligible": True,
        "viewport": "both",
        "commit_sha": "shared-commit",
        "build_number": index,
        "finished_at": f"2026-09-10T00:{index:02d}:00+00:00",
    }


def test_stability_windows_are_scoped_to_one_site() -> None:
    records = [_record("mondressy", index) for index in range(1, 7)]
    records.extend(_record("lavetir", index) for index in range(1, 7))

    mondressy = summarize_records(records, last=10, site="mondressy")
    lavetir = summarize_records(records, last=10, site="lavetir")
    unspecified = summarize_records(records, last=10)

    assert mondressy["site"] == "mondressy"
    assert lavetir["site"] == "lavetir"
    assert mondressy["eligible_builds_on_baseline"] == 6
    assert lavetir["eligible_builds_on_baseline"] == 6
    assert all(record["site"] == "mondressy" for record in mondressy["records"])
    assert all(record["site"] == "lavetir" for record in lavetir["records"])
    assert unspecified["status"] == "MIXED_SITE"
    assert unspecified["records"] == []


def test_record_identity_keeps_same_run_id_from_different_sites() -> None:
    mondressy = _record("mondressy", 1)
    lavetir = _record("lavetir", 1)

    merged = merge_records([mondressy], [lavetir])

    assert len(merged) == 2
    assert {record["site"] for record in merged} == {"mondressy", "lavetir"}


def test_new_ineligible_commit_cannot_reset_existing_window_or_inherit_its_identity():
    old = _record("mondressy", 1)
    fatal = {**_record("mondressy", 2), "commit_sha": "new-fatal-commit",
             "eligible": False, "jenkins_result": "FAILURE"}
    summary = summarize_records([old, fatal], last=1, site="mondressy", strict_mixed=True)
    assert summary["baseline_commit"] == old["commit_sha"]
    assert summary["records"] == [old]
    assert summary["status"] != "MIXED_BASELINE"
    assert summary["latest_execution"] == {
        "commit_sha": "new-fatal-commit", "run_id": fatal["run_id"],
        "eligible": False, "jenkins_result": "FAILURE",
    }
    current = summarize_records([old, fatal], site="mondressy", baseline_commit="new-fatal-commit")
    assert current["status"] == "COLLECTING" and current["records"] == []


def test_eligible_business_failure_selects_new_baseline():
    failed = {**_record("mondressy", 2), "commit_sha": "new-business-failure", "jenkins_result": "FAILURE"}
    summary = summarize_records([_record("mondressy", 1), failed], last=1, site="mondressy")
    assert summary["baseline_commit"] == "new-business-failure"
    assert summary["records"] == [failed] and summary["status"] != "STABLE"


def test_string_eligibility_is_not_accepted():
    malformed = {**_record("mondressy", 1), "eligible": "false"}
    summary = summarize_records([malformed], site="mondressy")
    assert summary["baseline_commit"] == "" and summary["records"] == []
