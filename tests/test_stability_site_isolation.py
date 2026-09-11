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
