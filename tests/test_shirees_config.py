"""Offline contract tests for the Shirees profile and signed access boundary."""

from __future__ import annotations

import utils.site_access as site_access
from utils.browser import load_site_config
from utils.site_access import SignedRequestPolicy


SHIREES = load_site_config("shirees")


def test_shirees_signed_request_profile_contract() -> None:
    access = SHIREES["access"]

    assert access["mode"] == "signed_request"
    assert access["source"] == "env"
    assert access["env"] == {
        "Signature": "SHIREES_US_SHOPIFY_SIGNATURE",
        "Signature-Input": "SHIREES_US_SHOPIFY_SIGNATURE_INPUT",
        "Signature-Agent": "SHIREES_US_SHOPIFY_SIGNATURE_AGENT",
    }
    assert access["allowed_hosts"] == ["shirees.com", "www.shirees.com"]


def test_shirees_signed_request_is_limited_to_exact_first_party_hosts(monkeypatch) -> None:
    synthetic_headers = {
        "Signature": "synthetic-signature",
        "Signature-Input": 'sig1=("@authority");expires=4102444800',
        "Signature-Agent": '"https://shopify.com"',
    }
    monkeypatch.setattr(
        site_access,
        "parse_env_headers",
        lambda **_kwargs: dict(synthetic_headers),
    )

    policy = site_access.create_site_access_policy("shirees", SHIREES)

    assert isinstance(policy, SignedRequestPolicy)
    assert policy.type_name == "signed_request"
    for host in ("shirees.com", "www.shirees.com"):
        assert policy.request_headers(f"https://{host}/cart.js") == synthetic_headers
    for host in (
        "example.com",
        "evil-shirees.com",
        "shirees.com.attacker.com",
        "google.com",
        "facebook.com",
        "paypal.com",
    ):
        assert policy.request_headers(f"https://{host}/") == {}
