"""Offline coverage for readonly purchase-readiness diagnostics."""

from __future__ import annotations

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

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


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _product(snapshot_reader, wait_for_missing=None) -> ProductPage:
    product = object.__new__(ProductPage)
    product._readiness_snapshot = snapshot_reader
    product._wait_for_missing_readiness_condition = wait_for_missing or (
        lambda _snapshot_value, _timeout_ms: pytest.fail("unexpected readiness wait")
    )
    return product


def test_missing_readiness_conditions_reports_size_loss() -> None:
    snapshot = _snapshot(size_count=0)

    assert ProductPage._missing_readiness_conditions(snapshot) == ["size"]
    assert ProductPage._snapshot_ready(snapshot) is False


def test_missing_readiness_conditions_reports_disabled_atc() -> None:
    snapshot = _snapshot(atc_enabled=False)

    assert ProductPage._missing_readiness_conditions(snapshot) == ["atc_enabled"]
    assert ProductPage._snapshot_ready(snapshot) is False


def test_slow_initial_ready_passes_after_deadline_with_one_snapshot(
    monkeypatch,
) -> None:
    clock = _Clock()
    calls = []

    def read_snapshot() -> dict:
        calls.append("snapshot")
        clock.advance(16.076)
        return _snapshot()

    monkeypatch.setattr("pages.product_page.time.monotonic", clock)
    product = _product(read_snapshot)

    assert product.wait_purchase_ready(timeout_ms=15_000) == (2, 3, True)
    assert calls == ["snapshot"]


def test_fast_initial_ready_uses_one_snapshot_and_no_wait() -> None:
    calls = []

    def read_snapshot() -> dict:
        calls.append("snapshot")
        return _snapshot()

    product = _product(read_snapshot)

    assert product.wait_purchase_ready() == (2, 3, True)
    assert calls == ["snapshot"]


def test_initial_not_ready_then_ready_samples_waits_and_resamples(
    monkeypatch,
) -> None:
    clock = _Clock()
    calls = []
    snapshots = iter((_snapshot(size_count=0), _snapshot()))

    def read_snapshot() -> dict:
        calls.append("snapshot")
        return next(snapshots)

    def wait_for_missing(snapshot: dict, timeout_ms: int) -> None:
        calls.append("wait")
        assert snapshot["size_count"] == 0
        assert timeout_ms > 0
        clock.advance(0.001)

    monkeypatch.setattr("pages.product_page.time.monotonic", clock)
    product = _product(read_snapshot, wait_for_missing)
    timeline = []

    assert product.wait_purchase_ready(
        timeout_ms=100,
        diagnostics_hook=timeline.append,
    ) == (2, 3, True)
    assert calls == ["snapshot", "wait", "snapshot"]
    assert len(timeline) == 2
    assert timeline[0]["size_count"] == 0
    assert timeline[1]["size_count"] == 3


def test_resample_that_crosses_deadline_still_passes_when_ready(
    monkeypatch,
) -> None:
    clock = _Clock()
    calls = []
    snapshots = iter((_snapshot(size_count=0), _snapshot()))

    def read_snapshot() -> dict:
        calls.append("snapshot")
        snapshot = next(snapshots)
        if len(calls) == 3:
            clock.advance(0.020)
        return snapshot

    def wait_for_missing(_snapshot_value: dict, timeout_ms: int) -> None:
        calls.append("wait")
        assert timeout_ms == 15
        clock.advance(0.005)
        raise PlaywrightTimeoutError("synthetic bounded wait expired")

    monkeypatch.setattr("pages.product_page.time.monotonic", clock)
    product = _product(read_snapshot, wait_for_missing)

    assert product.wait_purchase_ready(timeout_ms=15) == (2, 3, True)
    assert clock.now > 0.015
    assert calls == ["snapshot", "wait", "snapshot"]


def test_deadline_with_not_ready_snapshot_fails_without_extra_snapshot(
    monkeypatch,
) -> None:
    clock = _Clock()
    calls = []

    def read_snapshot() -> dict:
        calls.append("snapshot")
        clock.advance(0.015)
        return _snapshot(size_count=0)

    monkeypatch.setattr("pages.product_page.time.monotonic", clock)
    product = _product(read_snapshot)

    with pytest.raises(PurchaseAreaReadinessError) as captured:
        product.wait_purchase_ready(timeout_ms=15)

    assert calls == ["snapshot"]
    assert "failing_conditions=size" in str(captured.value)


def test_selected_size_is_diagnostic_not_readiness_gate() -> None:
    calls = []

    def read_snapshot() -> dict:
        calls.append("snapshot")
        return _snapshot(selected_size=None)

    product = _product(read_snapshot)
    timeline = []

    colors, sizes, atc = product.wait_purchase_ready(
        timeout_ms=1,
        diagnostics_hook=timeline.append,
    )

    assert (colors, sizes, atc) == (2, 3, True)
    assert timeline
    assert timeline[0]["selected_size"] is None
    assert ProductPage._snapshot_ready(timeline[0]) is True
    assert calls == ["snapshot"]


def test_readiness_error_exposes_initial_final_gates() -> None:
    calls = []

    def read_snapshot() -> dict:
        calls.append("snapshot")
        return _snapshot(title_visible=False)

    product = _product(read_snapshot)

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
    assert calls == ["snapshot"]


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
