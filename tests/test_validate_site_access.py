"""Offline regression tests for the site-aware access validator."""

from __future__ import annotations

from types import SimpleNamespace

import scripts.validate_site_access as cli
import utils.site_access as site_access
from utils.browser import load_site_config
from utils.config import resolve_url
from utils.site_access import NoAccessPolicy, SignedRequestPolicy


class _FakeRequest:
    def __init__(self) -> None:
        self.calls = []

    def get(self, url, headers, timeout):
        self.calls.append((url, headers, timeout))
        return SimpleNamespace(status=200)


class _FakeRuntime:
    def __init__(self, site_name, site_config, policy) -> None:
        self.site_name = site_name
        self.site_config = site_config
        self.access_policy = policy
        self.request = _FakeRequest()
        self.context = SimpleNamespace(request=self.request)


def test_cli_resolution_prefers_explicit_site_and_propagates_it(monkeypatch, capsys) -> None:
    loaded_sites = []
    runtime_sites = []

    monkeypatch.setattr(cli, "load_settings", lambda: {"default_site": "settings-site"})

    def fake_load_site_config(site_name):
        loaded_sites.append(site_name)
        return {"base_url": "https://example.test", "access": {"type": "none"}}

    monkeypatch.setattr(cli, "load_site_config", fake_load_site_config)

    def fake_run_viewport(_viewport, *, site_name=None):
        runtime_sites.append(site_name)
        return True, [f"  Site: {site_name}", "  Policy Type: none"]

    monkeypatch.setattr(cli, "run_viewport", fake_run_viewport)

    def forbidden_self_checks():
        raise AssertionError("none policy must not run signed-request credential checks")

    monkeypatch.setattr(cli, "credential_self_checks", forbidden_self_checks)

    assert cli.main(["--viewport", "desktop"]) == 0
    assert cli.main(["--site", "explicit-site", "--viewport", "desktop"]) == 0
    assert loaded_sites == ["settings-site", "explicit-site"]
    assert runtime_sites == ["settings-site", "explicit-site"]
    assert "Policy Type: none" in capsys.readouterr().out


def test_signed_request_uses_dynamic_cart_url_and_exact_host_isolation(monkeypatch) -> None:
    policy = SignedRequestPolicy(
        {
            "Signature": "synthetic-signature",
            "Signature-Input": 'sig1=("@authority");expires=4102444800',
            "Signature-Agent": '"https://shopify.com"',
        },
        ["example.test", "www.example.test"],
        expires=4102444800,
        source="synthetic",
    )
    runtime = _FakeRuntime(
        "explicit-site",
        {
            "base_url": "https://example.test/",
            "access": {
                "type": "signed_request",
                "allowed_hosts": ["example.test", "www.example.test"],
            },
        },
        policy,
    )
    created = []
    monkeypatch.setattr(
        cli,
        "create_browser",
        lambda viewport, site_name=None: (
            created.append((viewport, site_name)) or runtime
        ),
    )
    monkeypatch.setattr(cli, "close_browser", lambda _runtime: None)

    ok, lines = cli.run_viewport("desktop", site_name="explicit-site")

    assert ok
    assert created == [("desktop", "explicit-site")]
    assert runtime.request.calls == [
        (
            "https://example.test/cart.js",
            {
                "Signature": "synthetic-signature",
                "Signature-Input": 'sig1=("@authority");expires=4102444800',
                "Signature-Agent": '"https://shopify.com"',
            },
            15000,
        )
    ]
    assert "  Site: explicit-site" in lines
    assert "  Policy Type: signed_request" in lines
    assert any("allowed host (example.test): Signed Headers YES" in line for line in lines)
    assert any("prefix lookalike: Signed Headers NO" in line for line in lines)
    assert any("suffix lookalike: Signed Headers NO" in line for line in lines)


def test_none_policy_needs_no_credentials_and_sends_no_headers(monkeypatch) -> None:
    runtime = _FakeRuntime(
        "no-access-site",
        {"base_url": "https://example.test/", "access": {"type": "none"}},
        NoAccessPolicy(),
    )
    created = []
    monkeypatch.setattr(
        cli,
        "create_browser",
        lambda viewport, site_name=None: (
            created.append((viewport, site_name)) or runtime
        ),
    )
    monkeypatch.setattr(cli, "close_browser", lambda _runtime: None)

    ok, lines = cli.run_viewport("mobile", site_name="no-access-site")

    assert ok
    assert created == [("mobile", "no-access-site")]
    assert runtime.request.calls == [("https://example.test/cart.js", {}, 15000)]
    assert "  Policy Type: none" in lines
    assert "  APIRequestContext Signed Request: DISABLED" in lines
    assert "  NoAccessPolicy first-party headers: EMPTY" in lines


def test_signed_request_credential_self_checks_remain_active() -> None:
    checks = cli.credential_self_checks()
    assert any("SIGNED_REQUEST_INCOMPLETE" in line for line in checks)
    assert any("SIGNED_REQUEST_EXPIRED" in line for line in checks)
    assert all("PASS" in line for line in checks)


def test_lavetir_minimal_config_uses_no_access_policy(monkeypatch) -> None:
    config = load_site_config("lavetir")

    assert config["site"] == "lavetir"
    assert resolve_url(config["base_url"], "site.base_url") == "https://www.lavetir.com"

    def forbidden_env_lookup(*_args, **_kwargs):
        raise AssertionError("none policy must not inspect signed-request credentials")

    monkeypatch.setattr(site_access, "parse_env_headers", forbidden_env_lookup)
    policy = site_access.create_site_access_policy("lavetir", config)

    assert isinstance(policy, NoAccessPolicy)
    assert policy.type_name == "none"
    assert policy.request_headers("https://www.lavetir.com/cart.js") == {}
