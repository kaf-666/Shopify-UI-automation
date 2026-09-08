"""Offline tests for the post-selection variant stability diagnostic."""

from __future__ import annotations

import json

from scripts.diagnose_variant_stability import (
    build_diagnostic_payload,
    classify_timeline,
)


def _snapshot(
    elapsed_ms: int,
    selected_size: str | None,
    *,
    group_detected: bool = True,
    option_total: int = 10,
    available_count: int = 9,
    requested_present: bool = True,
    requested_available: bool = True,
    atc_visible: bool = True,
    atc_enabled: bool = True,
) -> dict:
    return {
        "elapsed_ms": elapsed_ms,
        "pathname": "/products/example",
        "selected_color": "Blue",
        "selected_size": selected_size,
        "model": "SIZE_MODEL_04",
        "group_detected": group_detected,
        "option_total": option_total,
        "available_count": available_count,
        "normal_available": available_count,
        "custom_present": True,
        "atc_visible": atc_visible,
        "atc_enabled": atc_enabled,
        "purchase_attached": True,
        "requested_size_present": requested_present,
        "requested_size_available": requested_available,
        "requested_size_checked": selected_size == "M",
    }


def test_stable_selection_for_five_seconds() -> None:
    timeline = [_snapshot(0, "M"), _snapshot(5_000, "M")]

    classifications, reset, dom_events = classify_timeline("direct", "M", timeline)

    assert classifications == ["SELECTION_STABLE"]
    assert reset is None
    assert dom_events["group_presence_changes"] == []


def test_delayed_reset_around_700ms_is_theme_state_reset() -> None:
    timeline = [_snapshot(0, "M"), _snapshot(700, None), _snapshot(5_000, None)]

    classifications, reset, _ = classify_timeline("direct", "M", timeline)

    assert classifications[0] == "POST_SELECTION_THEME_STATE_RESET"
    assert reset is not None
    assert reset["at_ms"] == 700
    assert reset["state_before"]["selected_size"] == "M"
    assert reset["state_after"]["selected_size"] is None


def test_group_replacement_and_reset_is_rerender_state_loss() -> None:
    timeline = [
        _snapshot(0, "M"),
        _snapshot(100, None, group_detected=False, option_total=0, available_count=0),
        _snapshot(200, None),
        _snapshot(5_000, None),
    ]

    classifications, reset, dom_events = classify_timeline("direct", "M", timeline)

    assert classifications[0] == "SIZE_GROUP_RERENDER_STATE_LOSS"
    assert reset is not None
    assert dom_events["group_presence_changes"]
    assert dom_events["option_total_changes"]


def test_disabled_atc_is_variant_atc_state_failure() -> None:
    timeline = [_snapshot(0, "M"), _snapshot(5_000, "M", atc_enabled=False)]

    classifications, reset, _ = classify_timeline("direct", "M", timeline)

    assert classifications == ["VARIANT_ATC_STATE_FAILURE"]
    assert reset is None


def test_json_envelope_has_no_credentials_headers_or_cookies() -> None:
    payload = build_diagnostic_payload(
        run_id="20260908_000000",
        site="example",
        viewport="mobile",
        entry="search",
        query="dress",
        observe_ms=5_000,
        runs=[
            {
                "viewport": "mobile",
                "product_path": "/products/example",
                "title": "Example",
                "requested_size": "M",
                "timeline": [],
                "classification": "SELECTION_STABLE",
            }
        ],
    )
    encoded = json.dumps(payload, ensure_ascii=False).lower()
    assert "credential" not in encoded
    assert "header" not in encoded
    assert "cookie" not in encoded
    assert "password" not in encoded
