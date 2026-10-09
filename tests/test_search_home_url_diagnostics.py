"""Offline contracts for failure-only, sanitized Search URL evidence."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

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
    find_search_home_url_diagnostic,
    write_search_home_url_diagnostic,
)
from utils.result import write_results_json


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


class _FakeRecoveryHome(_FakeHome):
    def __init__(self, _page, site_config: dict, _viewport: str) -> None:
        super().__init__(site_config.get("base_url", "https://www.lavetir.com"))


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

    def wait_for_timeout(self, _timeout_ms: int) -> None:
        return None


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
    assert result["actual_pathname"] == "/[REDACTED]"
    assert result["path_category"] == "SEARCH"
    assert result["path_segment_count"] == 3
    assert result["pathname_redacted"] is True
    assert "alice@example.test" not in str(result)


@pytest.mark.parametrize(
    ("url", "title", "sensitive_values"),
    [
        (
            "https://www.lavetir.com/member/jane-doe",
            "Jane Doe | My Account",
            ("Jane Doe", "jane-doe"),
        ),
        (
            "https://www.lavetir.com/account/jane-doe",
            "Jane Doe | Account",
            ("Jane Doe", "jane-doe"),
        ),
        (
            "https://www.lavetir.com/member/%E5%BC%A0%E4%B8%89",
            "张三 | 我的账户",
            ("张三", "%E5%BC%A0%E4%B8%89"),
        ),
        (
            "https://www.lavetir.com/profile/%E5%B1%B1%E7%94%B0%E5%A4%AA%E9%83%8E",
            "山田太郎 | Profile",
            ("山田太郎", "%E5%B1%B1%E7%94%B0%E5%A4%AA%E9%83%8E"),
        ),
        (
            "https://www.lavetir.com/member/jane.doe%40example.test",
            "Jane Doe | Member",
            ("jane.doe", "example.test"),
        ),
        (
            "https://www.lavetir.com/products/12345678901234567890",
            "Product 12345678901234567890",
            ("12345678901234567890",),
        ),
        (
            "https://www.lavetir.com/session/synthetic-token-value-0123456789abcdef",
            "Session synthetic-token-value-0123456789abcdef",
            ("synthetic-token-value-0123456789abcdef",),
        ),
        (
            "https://www.lavetir.com/?token=query-secret#fragment-secret",
            "Jane Doe | My Account",
            ("Jane Doe", "query-secret", "fragment-secret"),
        ),
    ],
)
def test_serialized_diagnostic_drops_free_text_titles_and_nonroot_paths(
    url: str, title: str, sensitive_values: tuple[str, ...], tmp_path
) -> None:
    page = _FakePage(url)
    page.title = lambda: title
    result = _diagnostic(url, page=page)
    write_search_home_url_diagnostic(
        result, tmp_path, "mobile", "RSMOKE-SEARCH-01"
    )
    serialized = (tmp_path / "mobile" / "RSMOKE-SEARCH-01-search-home-url.json").read_text(
        encoding="utf-8"
    )

    assert result["title_present"] is True
    if urlsplit(url).path in {"", "/"}:
        assert result["actual_pathname"] == "/"
        assert result["pathname_redacted"] is False
    else:
        assert result["actual_pathname"] == "/[REDACTED]"
        assert result["pathname_redacted"] is True
    assert result["page_title_category"] in {
        "HOME", "SEARCH", "COLLECTION", "PRODUCT", "CHALLENGE", "ERROR", "UNKNOWN"
    }
    for value in sensitive_values:
        assert value not in serialized
    assert title not in serialized


@pytest.mark.parametrize(
    ("url", "category"),
    [
        ("https://www.lavetir.com/", "HOME"),
        ("https://www.lavetir.com/search", "SEARCH"),
        ("https://www.lavetir.com/collections/private-collection", "COLLECTION"),
        ("https://www.lavetir.com/products/private-product", "PRODUCT"),
        ("https://www.lavetir.com/cdn-cgi/challenge-platform", "CHALLENGE"),
        ("https://www.lavetir.com/member/jane-doe", "ACCOUNT"),
        ("https://www.lavetir.com/private/jane-doe", "UNKNOWN"),
    ],
)
def test_path_classification_retains_only_root_and_static_route_metadata(
    url: str, category: str
) -> None:
    result = _diagnostic(url)

    assert result["path_category"] == category
    if url.endswith("/"):
        assert result["actual_pathname"] == "/"
        assert result["path_is_root"] is True
        assert result["path_segment_count"] == 0
        assert result["pathname_redacted"] is False
    else:
        assert result["actual_pathname"] == "/[REDACTED]"
        assert result["path_is_root"] is False
        assert result["pathname_redacted"] is True


def test_title_is_only_written_as_a_structured_category() -> None:
    page = _FakePage("https://www.lavetir.com/challenge")
    page.title = lambda: "Cloudflare challenge for Jane Doe"

    result = _diagnostic(page.url, page=page)
    serialized = json.dumps(result)

    assert result["page_title_category"] == "CHALLENGE"
    assert result["title_present"] is True
    assert "Cloudflare challenge for Jane Doe" not in serialized
    assert "Jane Doe" not in serialized


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


@pytest.mark.parametrize(
    ("runner_type", "case_id", "category", "module_name"),
    [
        (
            WebsiteSmokeV1Runner,
            "WSMOKE-SEARCH-01",
            "SEARCH_TRIGGER_FAILURE",
            "tests.website_smoke_v1_cases",
        ),
        (
            WebsiteSmokeReadonlyV1Runner,
            "RSMOKE-SEARCH-01",
            "SEARCH_ENTRY_NOT_AVAILABLE",
            "tests.website_smoke_readonly_v1_cases",
        ),
    ],
)
@pytest.mark.parametrize(
    ("screenshot_ok", "sidecar_ok", "expected_types", "expected_errors"),
    [
        (True, True, ["screenshot", "search_home_url_diagnostic"], None),
        (
            True,
            False,
            ["screenshot"],
            "URL_DIAGNOSTIC_SIDECAR_WRITE_FAILED:OSError",
        ),
        (
            False,
            True,
            ["search_home_url_diagnostic"],
            "SCREENSHOT_CAPTURE_FAILED",
        ),
        (
            False,
            False,
            [],
            "SCREENSHOT_CAPTURE_FAILED; URL_DIAGNOSTIC_SIDECAR_WRITE_FAILED:OSError",
        ),
    ],
)
def test_full_and_readonly_keep_independent_evidence_capture_errors_in_results_json(
    runner_type,
    case_id: str,
    category: str,
    module_name: str,
    screenshot_ok: bool,
    sidecar_ok: bool,
    expected_types: list[str],
    expected_errors: str | None,
    monkeypatch,
    tmp_path,
    capsys,
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
    screenshot_path = f"mobile/{case_id}.png"

    def capture_screenshot(*_args):
        if not screenshot_ok:
            return None
        screenshot = tmp_path / screenshot_path
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        screenshot.write_bytes(b"test screenshot")
        return screenshot_path

    monkeypatch.setattr(f"{module_name}.capture_case_failure", capture_screenshot)
    if not sidecar_ok:
        def fail_sidecar(*_args, **_kwargs):
            raise OSError("sidecar-private-secret")

        monkeypatch.setattr(f"{module_name}.write_search_home_url_diagnostic", fail_sidecar)

    raw = RuntimeError("Search session can only be opened from Home")
    setattr(raw, DIAGNOSTIC_ATTRIBUTE, _diagnostic(page.url, case_id=case_id))

    def fail_case():
        raise FlowError(category, str(raw)) from raw

    runner._run_case(case_id, "Search Entry", [], fail_case)
    result = runner.results[case_id]
    assert result.status == "FAIL"
    assert result.failure_classification == category
    assert [item["type"] for item in result.evidence] == expected_types
    if screenshot_ok:
        assert (tmp_path / screenshot_path).is_file()
    if sidecar_ok:
        sidecar = tmp_path / "mobile" / f"{case_id}-search-home-url.json"
        assert json.loads(sidecar.read_text(encoding="utf-8"))["case_id"] == case_id

    results_path = tmp_path / "results.json"
    write_results_json({"cases": [result.to_dict()]}, results_path)
    serialized = results_path.read_text(encoding="utf-8")
    saved_case = json.loads(serialized)["cases"][0]
    assert saved_case["status"] == "FAIL"
    assert saved_case["failure_classification"] == category
    assert saved_case["evidence_capture_error"] == expected_errors
    assert "sidecar-private-secret" not in serialized
    assert "sidecar-private-secret" not in capsys.readouterr().out


def _home_assertion_error(actual_url: str, case_id: str) -> RuntimeError:
    error = RuntimeError("Search session can only be opened from Home")
    setattr(error, DIAGNOSTIC_ATTRIBUTE, _diagnostic(actual_url, case_id=case_id))
    return error


def _make_recovery_search(monkeypatch, clock_values: list[float]):
    page = _FakePage("https://www.lavetir.com/")
    search = SearchPage(page, {"base_url": "https://www.lavetir.com"}, "mobile")
    monkeypatch.setattr(search_page_module, "HomePage", _FakeRecoveryHome)
    values = iter(clock_values)
    monkeypatch.setattr(search_page_module.time, "monotonic", lambda: next(values))
    return search


def test_reopen_session_attaches_latest_url_diagnostic_to_original_timeout(monkeypatch) -> None:
    case_id = "RSMOKE-SEARCH-02"
    first = _home_assertion_error(
        "https://lavetir.com/search?token=first-private-value", case_id
    )
    latest = _home_assertion_error(
        "https://redirect.example.test/?token=latest-private-value", case_id
    )
    attempts = iter((first, latest))
    search = _make_recovery_search(monkeypatch, [0.0, 0.0, 0.0, 11.0])

    def fail_reopen(*_args, **_kwargs):
        error = next(attempts)
        raise error

    search.ensure_search_session_open = fail_reopen
    with pytest.raises(TimeoutError) as captured:
        search._reopen_session(case_id=case_id, browser="webkit")

    assert str(captured.value) == "session reopen failed: page not stable"
    evidence = find_search_home_url_diagnostic(captured.value)
    assert evidence is not None
    assert evidence["actual_origin"] == "https://redirect.example.test"
    serialized = json.dumps(evidence)
    assert "first-private-value" not in serialized
    assert "latest-private-value" not in serialized


def test_reopen_session_success_after_transient_url_assertion_has_no_failure_evidence(
    monkeypatch,
) -> None:
    case_id = "RSMOKE-SEARCH-02"
    attempts = iter((_home_assertion_error("https://lavetir.com/", case_id), None))
    search = _make_recovery_search(monkeypatch, [0.0, 0.0, 0.0, 11.0])
    calls = 0

    def recover(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        error = next(attempts)
        if error is not None:
            raise error

    search.ensure_search_session_open = recover

    search._reopen_session(case_id=case_id, browser="webkit")

    assert calls == 2
    assert find_search_home_url_diagnostic(None) is None
    assert not hasattr(search, DIAGNOSTIC_ATTRIBUTE)


def test_reopen_session_does_not_reuse_old_url_diagnostic_after_unrelated_error(
    monkeypatch,
) -> None:
    case_id = "RSMOKE-SEARCH-02"
    attempts = iter(
        (
            _home_assertion_error("https://lavetir.com/", case_id),
            ValueError("unrelated-private-secret"),
        )
    )
    search = _make_recovery_search(monkeypatch, [0.0, 0.0, 0.0, 11.0])

    def fail_reopen(*_args, **_kwargs):
        raise next(attempts)

    search.ensure_search_session_open = fail_reopen
    with pytest.raises(TimeoutError) as captured:
        search._reopen_session(case_id=case_id, browser="webkit")

    assert str(captured.value) == "session reopen failed: page not stable"
    assert find_search_home_url_diagnostic(captured.value) is None
    assert "unrelated-private-secret" not in str(captured.value)


@pytest.mark.parametrize(
    ("runner_type", "case_id", "category", "module_name"),
    [
        (
            WebsiteSmokeV1Runner,
            "WSMOKE-SEARCH-02",
            "SEARCH_SUBMIT_FAILURE",
            "tests.website_smoke_v1_cases",
        ),
        (
            WebsiteSmokeReadonlyV1Runner,
            "RSMOKE-SEARCH-02",
            "SEARCH_SUBMIT_FAILURE",
            "tests.website_smoke_readonly_v1_cases",
        ),
    ],
)
def test_full_and_readonly_preserve_submit_classification_for_diagnostic_timeout(
    runner_type,
    case_id: str,
    category: str,
    module_name: str,
    monkeypatch,
    tmp_path,
) -> None:
    page = _FakePage("https://www.lavetir.com/")
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
    monkeypatch.setattr(f"{module_name}.capture_case_failure", lambda *_args: None)
    error = TimeoutError("session reopen failed: page not stable")
    setattr(
        error,
        DIAGNOSTIC_ATTRIBUTE,
        _diagnostic("https://lavetir.com/?token=private-timeout-value", case_id=case_id),
    )

    def fail_case():
        raise FlowError(category, str(error)) from error

    runner._run_case(case_id, "Search Results", [], fail_case)
    result = runner.results[case_id]

    assert result.status == "FAIL"
    assert result.failure_classification == category
    assert "session reopen failed: page not stable" in result.detail
    assert any(item["type"] == "search_home_url_diagnostic" for item in result.evidence)


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
