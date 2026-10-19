"""Offline contract tests for the manual Shirees Readonly Jenkins pilot."""

from __future__ import annotations

from utils.browser import load_site_config
from utils.errors import sanitize_message, sensitive_env_names


SHIREES = load_site_config("shirees")


def test_shirees_credential_names_are_covered_by_dynamic_error_redaction(monkeypatch) -> None:
    credential_names = (
        "SHIREES_US_SHOPIFY_SIGNATURE",
        "SHIREES_US_SHOPIFY_SIGNATURE_INPUT",
        "SHIREES_US_SHOPIFY_SIGNATURE_AGENT",
    )

    for name in credential_names:
        synthetic_value = f"synthetic-only-{name.lower()}"
        monkeypatch.setenv(name, synthetic_value)
        assert name in sensitive_env_names({name: synthetic_value})
        assert synthetic_value not in sanitize_message(f"request failed: {synthetic_value}")


def test_shirees_pipeline_profile_matches_the_explicit_signed_request_contract() -> None:
    access = SHIREES["access"]

    assert access["mode"] == "signed_request"
    assert access["source"] == "env"
    assert access["env"] == {
        "Signature": "SHIREES_US_SHOPIFY_SIGNATURE",
        "Signature-Input": "SHIREES_US_SHOPIFY_SIGNATURE_INPUT",
        "Signature-Agent": "SHIREES_US_SHOPIFY_SIGNATURE_AGENT",
    }
    assert access["allowed_hosts"] == ["shirees.com", "www.shirees.com"]
