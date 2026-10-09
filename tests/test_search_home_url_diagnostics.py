"""Offline contracts for failure-only, sanitized Search URL evidence."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import pages.search_page as search_page_module
from flows.shopping_flow import FlowError
from pages.search_page import SearchPage
from tests.website_smoke_readonly_v1_cases import WebsiteSmokeReadonlyV1Runner
from tests.website_smoke_v1_cases import WebsiteSmokeV1Runner
from utils.readonly_mutation_guard import ReadonlyMutationGuard
from utils.search_url_diagnostics import (
    DIAGNOSTIC_ATTRIBUTE,
    build_search_home_url_diagnostic,
)


class _FakeTrigger:
    def __init__(self, visible: bool = True) -> None:
        self.visible = visible

    def is_visible(self) -> bool:
        return self.visible


class _FakeHome:
    def __init__(self, base_url: str = "https://www.lavetir.com") -> None:
        self._base_url = base_url
        self.trigger = _FakeTrigger()

    def base_url(self) -> str:
        return self._base_url

    def search_trigger(self):
        return self.trigger

    def open_search(self) -> None:
        raise AssertionError("an already-open Search session should not click")


class _FakePage:
    def __init__(self, url: str) -> None:
        self.url = url
        self.context = SimpleNamespace(
            browser=SimpleNamespace(browser_type=SimpleNamespace(name="webkit"))
        )

    def title(self) -> str:
        return "Lavetir Search"

    def evaluate(self, expression: str):
        assert expression == "document.readyState"
        return "complete"

    def content(self) -> str:
        return "<html><body>store</body></html>"


class _NoopExpect:
    def to_be_visible(self, **_kwargs) -> None:
        return None

    def to_be_enabled(self, **_kwargs) -> None:
        return None

    def to_be_editable(self, **_kwargs) -> None:
        return None


class _FakeInput:
    pass


def _diagnostic(
    actual_url: str,
    configured_url: str = "https://www.lavetir.com",
    *,
    page: _FakePage | None = None,
    case_id: str = "RSMOKE-SEARCH-01",
) -> dict:
    page = page or _FakePage(actual_url)
    return build_search_home_url_diagnostic(
        page=page,
        actual_url=actual_url,
        configured_url=configured_url,
        viewport="mobile",
        browser="webkit",
        case_id=case_id,
        home=_FakeHome(configured_url),
    )


def test_normal_home_path_produces_no_failure_diagnostic(monkeypatch) -> None:
    page = _FakePage("https://www.lavetir.com/")
    search = SearchPage(page, {"base_url": "https://www.lavetir.com"}, "mobile")
    search.is_open = lambda: True
    search.wait_open = lambda **_kwargs: None
    search.input = lambda: _FakeInput()
    monkeypatch.setattr(search_page_module, "expect", lambda _locator: _NoopExpect())
    monkeypatch.setattr(
        search_page_module,
        "build_search_home_url_diagnostic",
        lambda **_kwargs: pytest.fail("normal Home must not collect failure evidence"),
    )

    search.ensure_search_session_open(_FakeHome(), case_id="RSMOKE-SEARCH-01")


def test_apex_and_www_mismatch_keeps_safe_actual_host() -> None:
    result = _diagnostic("https://lavetir.com/")

    assert result["actual_origin"] == "https://lavetir.com"
    assert result["configured_origin"] == "https://www.lavetir.com"
    assert result["url_mismatch_type"] == "HOST_MISMATCH"
    assert result["mismatch_components"]["host"] is True


def test_query_and_fragment_presence_are_recorded_without_values() -> None:
    raw = "https://www.lavetir.com/?email=private@example.test&token=query-secret#fragment-secret"
    result = _diagnostic(raw)
    serialized = json.dumps(result)

    assert result["query_present"] is True
    assert result["fragment_present"] is True
    assert result["url_mismatch_type"] == "QUERY_MISMATCH"
    for secret in ("private@example.test", "query-secret", "fragment-secret"):
        assert secret not in serialized


def test_non_home_path_is_identified_and_sensitive_path_value_is_filtered() -> None:
    result = _diagnostic(
        "https://www.lavetir.com/search/customer/alice@example.test"
    )

    assert result["url_mismatch_type"] == "PATH_MISMATCH"
    assert result["actual_pathname"] == "/search/customer/{token}"
    assert "alice@example.test" not in str(result)


def test_home_assertion_keeps_the_original_runtime_error_and_attaches_evidence() -> None:
    page = _FakePage("https://lavetir.com/?campaign=secret")
    search = SearchPage(page, {"base_url": "https://www.lavetir.com"}, "mobile")

    with pytest.raises(RuntimeError) as captured:
        search.ensure_search_session_open(
            _FakeHome(), case_id="RSMOKE-SEARCH-01", browser="webkit"
        )

    assert type(captured.value) is RuntimeError
    assert str(captured.value) == "Search session can only be opened from Home"
    evidence = getattr(captured.value, DIAGNOSTIC_ATTRIBUTE)
    assert evidence["case_id"] == "RSMOKE-SEARCH-01"
    assert evidence["actual_origin"] == "https://lavetir.com"
    assert evidence["query_present"] is True
    assert "campaign=secret" not in str(evidence)


def test_collector_failure_does_not_mask_original_home_assertion(monkeypatch) -> None:
    page = _FakePage("https://www.lavetir.com/search")
    search = SearchPage(page, {"base_url": "https://www.lavetir.com"}, "mobile")

    def fail_collector(**_kwargs):
        raise ValueError("synthetic-collector-secret")

    monkeypatch.setattr(search_page_module, "build_search_home_url_diagnostic", fail_collector)

    with pytest.raises(RuntimeError) as captured:
        search.ensure_search_session_open(
            _FakeHome(), case_id="WSMOKE-SEARCH-01", browser="webkit"
        )

    assert str(captured.value) == "Search session can only be opened from Home"
    evidence = getattr(captured.value, DIAGNOSTIC_ATTRIBUTE)
    assert evidence["diagnostic_collection_status"] == "FAILED"
    assert evidence["diagnostic_collection_error_type"] == "ValueError"
    assert "synthetic-collector-secret" not in str(evidence)


def test_credentials_never_enter_console_json_or_exception(monkeypatch, tmp_path, capsys) -> None:
    secret = "synthetic-lavetir-signature-secret"
    cookie_value = "synthetic-cookie-value"
    monkeypatch.setenv("LAVETIR_US_SHOPIFY_SIGNATURE", secret)
    raw_url = f"https://www.lavetir.com/private/{secret}?token={secret}#{secret}"
    page = _FakePage(raw_url)
    page.title = lambda: (
        f"Customer {secret} private@example.test Cookie={cookie_value} "
        f"Signature={secret} token={secret} phone=+1-555-123-4567"
    )
    search = SearchPage(page, {"base_url": "https://www.lavetir.com"}, "mobile")

    with pytest.raises(RuntimeError) as captured:
        search.ensure_search_session_open(
            _FakeHome(), case_id="RSMOKE-SEARCH-01", browser="webkit"
        )

    evidence = getattr(captured.value, DIAGNOSTIC_ATTRIBUTE)
    serialized = json.dumps(evidence)
    assert secret not in str(captured.value)
    assert secret not in serialized
    assert cookie_value not in serialized
    assert "private@example.test" not in serialized
    path = tmp_path / "mobile" / "RSMOKE-SEARCH-01-search-home-url.json"
    from utils.search_url_diagnostics import write_search_home_url_diagnostic

    write_search_home_url_diagnostic(evidence, tmp_path, "mobile", "RSMOKE-SEARCH-01")
    assert secret not in path.read_text(encoding="utf-8")
    assert secret not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("runner_type", "case_id", "category"),
    [
        (WebsiteSmokeV1Runner, "WSMOKE-SEARCH-01", "SEARCH_TRIGGER_FAILURE"),
        (WebsiteSmokeReadonlyV1Runner, "RSMOKE-SEARCH-01", "SEARCH_ENTRY_NOT_AVAILABLE"),
    ],
)
def test_full_and_readonly_classifications_and_results_evidence_are_preserved(
    runner_type, case_id: str, category: str, monkeypatch, tmp_path
) -> None:
    page = _FakePage("https://www.lavetir.com/search?private=query")
    runtime = SimpleNamespace(
        page=page,
        access_policy=None,
        engine="webkit",
        metadata=lambda: {"engine": "webkit", "device": "iPhone 14"},
    )
    runner = runner_type(
        runtime,
        {"base_url": "https://www.lavetir.com"},
        "mobile",
        artifact_dir=tmp_path,
    )
    module = (
        "tests.website_smoke_v1_cases"
        if runner_type is WebsiteSmokeV1Runner
        else "tests.website_smoke_readonly_v1_cases"
    )
    monkeypatch.setattr(f"{module}.capture_case_failure", lambda *_args: None)
    raw = RuntimeError("Search session can only be opened from Home")
    setattr(
        raw,
        DIAGNOSTIC_ATTRIBUTE,
        _diagnostic(page.url, case_id=case_id),
    )

    def fail_case():
        raise FlowError(category, str(raw)) from raw

    runner._run_case(case_id, "Search Entry", [], fail_case)
    result = runner.results[case_id]

    assert result.status == "FAIL"
    assert result.failure_classification == category
    assert "Search session can only be opened from Home" in result.detail
    assert result.evidence == [
        {
            "type": "search_home_url_diagnostic",
            "path": f"mobile/{case_id}-search-home-url.json",
        }
    ]
    sidecar = tmp_path / "mobile" / f"{case_id}-search-home-url.json"
    assert json.loads(sidecar.read_text(encoding="utf-8"))["case_id"] == case_id
    assert "private=query" not in sidecar.read_text(encoding="utf-8")


def test_readonly_mutation_guard_still_blocks_cart_mutation() -> None:
    class _Route:
        request = SimpleNamespace(
            url="https://www.lavetir.com/cart/add.js?token=private",
            method="POST",
        )
        calls: list[str] = []

        def abort(self) -> None:
            self.calls.append("abort")

        def fallback(self) -> None:
            self.calls.append("fallback")

    guard = ReadonlyMutationGuard()
    route = _Route()
    guard._handle_route(route)

    assert route.calls == ["abort"]
    assert guard.violation_count() == 1
    assert guard.violations()[0]["path"] == "/cart/add.js"
