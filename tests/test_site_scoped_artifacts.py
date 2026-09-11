"""Offline coverage for site-scoped Full smoke artifact writes."""

from __future__ import annotations

import json
from types import SimpleNamespace

import scripts.run_website_smoke_v1 as full_cli
from utils.result import CaseResult, iso_now


def _full_pass_results() -> list[CaseResult]:
    now = iso_now()
    return [
        CaseResult(f"FULL-{index:02d}", f"Full {index}", "PASS", now, now, 1)
        for index in range(1, 16)
    ]


def _full_runner() -> SimpleNamespace:
    return SimpleNamespace(
        pre_clean_status="PASS",
        cleanup_status="PASS",
        pre_clean_error=None,
        cleanup_error=None,
        cleanup_detail="PASS",
        search_recovery_used=False,
        cf_interruption=False,
    )


def test_full_cli_writes_to_a_site_scoped_artifact_directory(monkeypatch, tmp_path) -> None:
    artifact_root = tmp_path / "artifacts" / "website-smoke-v1"
    monkeypatch.setattr(full_cli, "ARTIFACT_ROOT", artifact_root)
    monkeypatch.setattr(full_cli, "make_run_id", lambda: "full-offline")
    monkeypatch.setattr(full_cli, "load_settings", lambda: {"default_site": "mondressy"})
    monkeypatch.setattr(
        full_cli,
        "load_site_config",
        lambda site: {"base_url": f"https://{site}.example.test"},
    )
    monkeypatch.setattr(full_cli, "resolve_url", lambda value, _field: value)
    monkeypatch.setattr(
        full_cli,
        "run_viewport",
        lambda _viewport, _artifact_dir, **_kwargs: (
            _full_pass_results(),
            _full_runner(),
            {},
        ),
    )

    assert full_cli.main(["--site", "lavetir", "--viewport", "desktop"]) == 0

    result_path = artifact_root / "lavetir" / "full-offline" / "results.json"
    assert result_path.exists()
    assert json.loads(result_path.read_text(encoding="utf-8"))["site"] == "lavetir"
