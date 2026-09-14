"""Offline regression coverage for versioned multi-site profile validation."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

import scripts.run_website_smoke_v1 as full_cli
import utils.browser as browser
from utils.artifacts import site_scoped_artifact_dir
from utils.browser import BrowserConfigError, load_site_config
from utils.site_config_validator import (
    WEBSITE_SMOKE_V1,
    SiteConfigValidationError,
    validate_site_config,
    validate_site_config_mapping,
)
from utils.stability import merge_records


def _profile(site: str = "mondressy") -> dict:
    return deepcopy(load_site_config(site))


def _validate(config: dict, site: str = "mondressy", *, suite: str | None = None) -> dict:
    return validate_site_config_mapping(
        config,
        site_name=site,
        config_path=Path(f"{site}.yaml"),
        suite=suite,
    )


def _assert_error(config: dict, category: str, *, suite: str | None = None) -> None:
    with pytest.raises(SiteConfigValidationError) as exc_info:
        _validate(config, suite=suite)
    assert exc_info.value.category == category


def test_real_site_profiles_validate_without_reading_secrets() -> None:
    mondressy = validate_site_config("mondressy")
    lavetir = validate_site_config("lavetir")

    assert mondressy["schema_version"] == 1
    assert mondressy["capabilities"]["suites"]["website_smoke_v1"] is True
    assert lavetir["schema_version"] == 1
    assert lavetir["capabilities"]["suites"]["website_smoke_v1"] is True


def test_missing_schema_version_is_invalid() -> None:
    config = _profile()
    del config["schema_version"]

    _assert_error(config, "INVALID_SITE_CONFIG")


def test_unsupported_schema_version_fails_with_a_stable_code() -> None:
    config = _profile()
    config["schema_version"] = 99

    _assert_error(config, "UNSUPPORTED_SCHEMA_VERSION")


@pytest.mark.parametrize("field", ["site", "base_url"])
def test_missing_identity_or_base_url_fails_before_runtime(field: str) -> None:
    config = _profile()
    del config[field]

    _assert_error(config, "INVALID_SITE_CONFIG")


def test_unknown_navigation_strategy_has_no_silent_fallback() -> None:
    config = _profile()
    config["capabilities"]["navigation"]["desktop"] = "hover_then_guess"

    _assert_error(config, "UNKNOWN_NAVIGATION_STRATEGY")


def test_cross_viewport_navigation_strategy_is_rejected_before_runtime() -> None:
    config = _profile()
    config["capabilities"]["navigation"]["mobile"] = "mega_menu_hover"

    _assert_error(config, "UNKNOWN_NAVIGATION_STRATEGY")


def test_drawer_accordion_requires_its_configured_parent_selector() -> None:
    config = _profile()
    del config["pages"]["navigation"]["selectors"]["mobile_target_parent"]

    _assert_error(config, "MISSING_CAPABILITY_REQUIREMENT")


def test_readonly_capability_requires_its_product_card_selector_contract() -> None:
    config = _profile()
    del config["pages"]["collection"]["selectors"]["product_link"]

    _assert_error(config, "MISSING_CAPABILITY_REQUIREMENT")


def test_signed_request_requires_explicit_env_mapping() -> None:
    config = _profile()
    config["access"]["env"] = {}

    _assert_error(config, "INVALID_ACCESS_CONFIG")


def test_signed_request_requires_explicit_allowed_hosts() -> None:
    config = _profile()
    del config["access"]["allowed_hosts"]

    _assert_error(config, "INVALID_ACCESS_CONFIG")


def test_path_traversal_is_rejected_at_site_identity_validation() -> None:
    with pytest.raises(SiteConfigValidationError) as exc_info:
        validate_site_config("../lavetir")
    assert exc_info.value.category == "INVALID_SITE_CONFIG"


def test_capability_false_rejects_requested_suite_before_browser_start() -> None:
    config = _profile()
    config["capabilities"]["suites"][WEBSITE_SMOKE_V1] = False

    _assert_error(config, "SITE_CAPABILITY_UNSUPPORTED", suite=WEBSITE_SMOKE_V1)


def test_full_runner_rejects_an_unsupported_profile_before_artifact_or_browser_start(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(full_cli, "ARTIFACT_ROOT", tmp_path / "artifacts")
    monkeypatch.setattr(full_cli, "make_run_id", lambda: "unsupported-suite")
    original_validate = full_cli.validate_site_config

    def reject_full(site: str, *, suite: str | None = None):
        if suite == WEBSITE_SMOKE_V1:
            raise SiteConfigValidationError("synthetic unsupported suite", category="SITE_CAPABILITY_UNSUPPORTED")
        return original_validate(site, suite=suite)

    monkeypatch.setattr(full_cli, "validate_site_config", reject_full)
    monkeypatch.setattr(full_cli, "run_viewport", lambda *_args, **_kwargs: pytest.fail("browser started"))

    assert full_cli.main(["--site", "lavetir", "--viewport", "desktop"]) == 2
    assert "SITE_CAPABILITY_UNSUPPORTED" in capsys.readouterr().out
    assert not (tmp_path / "artifacts").exists()


def test_browser_validates_before_playwright_start(monkeypatch) -> None:
    def invalid_profile(_site_name):
        raise SiteConfigValidationError("synthetic invalid profile", category="INVALID_SITE_CONFIG")

    def browser_must_not_start():
        pytest.fail("Playwright startup must not be attempted for invalid config")

    monkeypatch.setattr(browser, "validate_site_config", invalid_profile)
    monkeypatch.setattr(browser, "sync_playwright", browser_must_not_start)
    settings = {
        "default_site": "mondressy",
        "browsers": {"desktop": {"engine": "chromium", "viewport": {"width": 1, "height": 1}}},
    }

    with pytest.raises(BrowserConfigError) as exc_info:
        browser.create_browser("desktop", settings=settings)
    assert exc_info.value.category == "INVALID_SITE_CONFIG"


def test_artifacts_and_stability_history_keep_sites_isolated(tmp_path) -> None:
    root = tmp_path / "artifacts"
    mondressy_dir = site_scoped_artifact_dir(root, "mondressy", "same-run")
    lavetir_dir = site_scoped_artifact_dir(root, "lavetir", "same-run")
    assert mondressy_dir != lavetir_dir
    assert mondressy_dir.parent.name == "mondressy"
    assert lavetir_dir.parent.name == "lavetir"

    mondressy_record = {"suite": "website-smoke-v1", "site": "mondressy", "run_id": "same-run"}
    lavetir_record = {"suite": "website-smoke-v1", "site": "lavetir", "run_id": "same-run"}
    assert len(merge_records([mondressy_record], [lavetir_record])) == 2
