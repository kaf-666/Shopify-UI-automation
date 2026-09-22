from __future__ import annotations

import hashlib

import pytest

from utils.mutation_fingerprint import (
    format_mutation_fingerprint_report,
    safe_endpoint_fingerprint,
    sanitize_pathname,
)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/normal/static/path", "/normal/static/path"),
        ("/foo/123456789012", "/foo/{id}"),
        ("/foo/123e4567-e89b-12d3-a456-426614174000", "/foo/{uuid}"),
        ("/foo/abcdef1234567890abcdef1234567890", "/foo/{hex}"),
        ("/foo/customer@example.com", "/foo/{email}"),
        ("/foo/AbCDef_123456789_xyz", "/foo/{token}"),
        (
            "/products/very-long-static-product-handle-with-numbers-12010206p",
            "/products/very-long-static-product-handle-with-numbers-12010206p",
        ),
        ("/foo/person%2540private%252Eexample%252Ecom", "/foo/{email}"),
        ("/foo/bar?id=123&token=abc", "/foo/bar"),
        ("/foo/bar#section", "/foo/bar"),
        ("/cart/add.js", "/cart/add.js"),
        ("/checkouts/cn/private-checkout-token/information", "/checkouts/cn/{token}/information"),
    ],
)
def test_sanitize_pathname(path: str, expected: str) -> None:
    assert sanitize_pathname(path) == expected


def test_fingerprint_strips_query_and_fragment_without_hashing_them() -> None:
    endpoint = "/foo/bar"
    secret_url = (
        "https://mondressy.com/foo/bar?token=synthetic-secret-query"
        "&customer=private@example.test#private-fragment"
    )
    fingerprint = safe_endpoint_fingerprint(
        secret_url,
        "POST",
        ("mondressy.com", "www.mondressy.com"),
        canonical_origin="https://mondressy.com",
    )

    assert fingerprint == {
        "method": "POST",
        "host_class": "FIRST_PARTY",
        "same_origin": True,
        "sanitized_path": endpoint,
        "path_hash": hashlib.sha256(endpoint.encode()).hexdigest()[:12],
        "query_present": True,
    }
    assert "synthetic-secret-query" not in str(fingerprint)
    assert "private@example.test" not in str(fingerprint)
    assert "private-fragment" not in str(fingerprint)


def test_fingerprint_reports_distinct_first_party_origin_safely() -> None:
    fingerprint = safe_endpoint_fingerprint(
        "https://www.mondressy.com/apps/example/action/123456789012",
        "POST",
        ("mondressy.com", "www.mondressy.com"),
        canonical_origin="https://mondressy.com",
    )
    assert fingerprint["host_class"] == "FIRST_PARTY"
    assert fingerprint["same_origin"] is False
    assert fingerprint["sanitized_path"] == "/apps/example/action/{id}"
    assert fingerprint["query_present"] is False


def test_console_report_is_gated_and_emits_only_allowlisted_fields() -> None:
    assert format_mutation_fingerprint_report(
        {"unexpected_mutation": 0, "high_risk_mutation": 0, "by_path": []}
    ) == []

    lines = format_mutation_fingerprint_report(
        {
            "unexpected_mutation": 1,
            "high_risk_mutation": 0,
            "by_path": [
                {
                    "classification": "UNEXPECTED_MUTATION",
                    "method": "POST",
                    "host_class": "FIRST_PARTY",
                    "same_origin": True,
                    "path": "/apps/example/action/123456789012",
                    "sanitized_path": "/apps/example/action/{id}",
                    "path_hash": "0123456789ab",
                    "query_present": True,
                    "reason": "PATH_NOT_ALLOWED",
                    "count": 1,
                    "desktop_count": 0,
                    "mobile_count": 1,
                    "blocked_count": 1,
                    "request_body": "synthetic-body-secret",
                    "authorization": "synthetic-auth-secret",
                }
            ],
        }
    )
    report = "\n".join(lines)
    assert lines[0] == "MUTATION_UNEXPECTED_FINGERPRINT_BEGIN"
    assert lines[-1] == "MUTATION_UNEXPECTED_FINGERPRINT_END"
    assert "sanitized_path=/apps/example/action/{id}" in report
    assert "path_hash=0123456789ab" in report
    assert "desktop_count=0" in report
    assert "mobile_count=1" in report
    assert "blocked_count=1" in report
    assert "synthetic-body-secret" not in report
    assert "synthetic-auth-secret" not in report
    assert "123456789012" not in report


def test_high_risk_alone_triggers_fingerprint_block() -> None:
    lines = format_mutation_fingerprint_report(
        {
            "unexpected_mutation": 0,
            "high_risk_mutation": 1,
            "by_path": [
                {
                    "classification": "HIGH_RISK_MUTATION",
                    "method": "POST",
                    "host_class": "THIRD_PARTY",
                    "same_origin": False,
                    "path": "/checkouts/private-token/payment",
                    "path_hash": "abcdef012345",
                    "query_present": False,
                    "reason": "HIGH_RISK_PRECEDENCE",
                    "count": 1,
                    "desktop_count": "UNKNOWN",
                    "mobile_count": "UNKNOWN",
                    "blocked_count": 1,
                }
            ],
        }
    )
    report = "\n".join(lines)
    assert lines[0] == "MUTATION_UNEXPECTED_FINGERPRINT_BEGIN"
    assert "classification=HIGH_RISK_MUTATION" in report
    assert "sanitized_path=/checkouts/{token}/payment" in report
    assert "desktop_count=UNKNOWN" in report
    assert "mobile_count=UNKNOWN" in report
