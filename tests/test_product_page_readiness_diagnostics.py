"""Offline coverage for readonly purchase-readiness diagnostics."""

from __future__ import annotations

import pytest

from pages.product_page import ProductPage, PurchaseAreaReadinessError
from tests.website_smoke_readonly_v1_cases import WebsiteSmokeReadonlyV1Runner


def _snapshot(**overrides) -> dict:
    snapshot = {
        "purchase_area_attached": True,
        "title_visible": True,
        "color_count": 2,
        "size_count": 3,
        "size_model": "SIZE_MODEL_04",
        "size_group_detected": True,
        "size_option_total": 3,
        "normal_size_available": 3,
        "custom_size_present": False,
        "selected_size": "M",
        "candidate_group_count": 1,
        "atc_visible": True,
        "atc_enabled": True,
    }
    snapshot.update(overrides)
    return snapshot


def test_missing_readiness_conditions_reports_size_loss() -> None:
    snapshot = _snapshot(size_count=0)

    assert ProductPage._missing_readiness_conditions(snapshot) == ["size"]
    assert ProductPage._snapshot_ready(snapshot) is False


def test_missing_readiness_conditions_reports_disabled_atc() -> None:
    snapshot = _snapshot(atc_enabled=False)

    assert ProductPage._missing_readiness_conditions(snapshot) == ["atc_enabled"]
    assert ProductPage._snapshot_ready(snapshot) is False


def test_selected_size_is_diagnostic_not_readiness_gate() -> None:
    product = object.__new__(ProductPage)
    product._readiness_snapshot = lambda: _snapshot(selected_size=None)
    timeline = []

    colors, sizes, atc = product.wait_purchase_ready(
        timeout_ms=1,
        diagnostics_hook=timeline.append,
    )

    assert (colors, sizes, atc) == (2, 3, True)
    assert timeline
    assert timeline[0]["selected_size"] is None
    assert ProductPage._snapshot_ready(timeline[0]) is True


def test_readiness_error_exposes_initial_final_gates() -> None:
    product = object.__new__(ProductPage)
    product._readiness_snapshot = lambda: _snapshot(title_visible=False)

    with pytest.raises(PurchaseAreaReadinessError) as captured:
        product.wait_purchase_ready(timeout_ms=0)

    detail = str(captured.value)
    assert "purchase_area_initial=True" in detail
    assert "purchase_area_final=True" in detail
    assert "title_visible_initial=False" in detail
    assert "title_visible_final=False" in detail
    assert "atc_visible_initial=True" in detail
    assert "atc_visible_final=True" in detail
    assert "atc_enabled_initial=True" in detail
    assert "atc_enabled_final=True" in detail
    assert "failing_conditions=title" in detail


def test_diagnostic_failure_detail_reports_phase_first_gate_and_timeline(
    monkeypatch,
) -> None:
    timeline = [
        {
            "elapsed_ms": 0,
            "purchase_area_attached": True,
            "title_visible": True,
            "color_count": 2,
            "size_count": 3,
            "size_group_detected": True,
            "size_option_total": 3,
            "normal_size_available": 3,
            "selected_size": "M",
            "atc_visible": True,
            "atc_enabled": True,
        },
        {
            "elapsed_ms": 100,
            "purchase_area_attached": True,
            "title_visible": True,
            "color_count": 2,
            "size_count": 0,
            "size_group_detected": True,
            "size_option_total": 3,
            "normal_size_available": 0,
            "selected_size": None,
            "atc_visible": True,
            "atc_enabled": True,
        },
    ]

    class _FailingProduct:
        @staticmethod
        def wait_purchase_ready(diagnostics_hook=None):
            assert diagnostics_hook is not None
            for snapshot in timeline:
                diagnostics_hook(snapshot)
            raise PurchaseAreaReadinessError("failing_conditions=size")

    monkeypatch.setenv("VARIANT_DIAGNOSTIC", "1")
    runner = object.__new__(WebsiteSmokeReadonlyV1Runner)

    with pytest.raises(PurchaseAreaReadinessError) as captured:
        runner._wait_purchase_ready(_FailingProduct(), "post_selection")

    detail = str(captured.value)
    assert "phase=post_selection" in detail
    assert "first_failing_conditions=size" in detail
    assert "first_failure_ms=100" in detail
    assert '"selected_size":null' in detail
    assert "readiness_timeline=" in detail
