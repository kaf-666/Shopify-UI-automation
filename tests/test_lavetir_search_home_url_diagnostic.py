"""Offline contracts for the manual Lavetir Search URL diagnostic."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from scripts import diagnose_lavetir_search_home_url as diagnostic
from utils.readonly_mutation_guard import ReadonlyMutationGuard


@pytest.mark.parametrize(
    ("url", "origin", "pathname", "query_present", "fragment_present"),
    [
        ("https://www.lavetir.com/", "https://www.lavetir.com", "/", False, False),
        ("https://lavetir.com/", "https://lavetir.com", "/", False, False),
        ("https://www.lavetir.com/?secret=query-value", "https://www.lavetir.com", "/", True, False),
        ("https://www.lavetir.com/#private-fragment", "https://www.lavetir.com", "/", False, True),
        ("https://www.lavetir.com/?", "https://www.lavetir.com", "/", True, False),
        ("https://www.lavetir.com/#", "https://www.lavetir.com", "/", False, True),
        ("https://www.lavetir.com/en-us/search", "https://www.lavetir.com", "/en-us/search", False, False),
        ("https://www.lavetir.com/user@example.com", "https://www.lavetir.com", "/[REDACTED]", False, False),
    ],
)
def test_safe_url_fields_keep_only_safe_components(
    url, origin, pathname, query_present, fragment_present
):
    fields = diagnostic.safe_url_fields(url)
    assert fields["origin"] == origin
    assert fields["pathname"] == pathname
    assert fields["query_present"] is query_present
    assert fields["fragment_present"] is fragment_present
    assert "secret=query-value" not in str(fields)
    assert "private-fragment" not in str(fields)
    assert "user@example.com" not in str(fields)


def test_homepage_check_stays_exact_for_apex_and_www_aliases():
    base = "https://www.lavetir.com"
    assert diagnostic.homepage_url_check_result("https://www.lavetir.com/", base) == "PASS"
    assert diagnostic.homepage_url_check_result("https://lavetir.com/", base) == "FAIL"
    assert diagnostic.homepage_url_check_result("https://www.lavetir.com/en-us", base) == "FAIL"


@pytest.mark.parametrize(
    ("actual", "expected_relation", "allowed"),
    [
        ("https://lavetir.com/", "APEX_WWW_VARIANT", True),
        ("https://www.lavetir.com.evil.example/", "DIFFERENT_HOST", False),
    ],
)
def test_url_relation_reports_facts_without_accepting_alias(actual, expected_relation, allowed):
    result = diagnostic._url_relation(
        "https://www.lavetir.com",
        actual,
        ["lavetir.com", "www.lavetir.com"],
    )
    assert result["origin_relation"] == expected_relation
    assert result["actual_host_is_configured_allowed_host"] is allowed
    assert result["homepage_url_check_result"] == "FAIL"


def test_safe_url_strips_userinfo_and_never_emits_query_or_fragment_values():
    fields = diagnostic.safe_url_fields(
        "https://account:password@www.lavetir.com/?Signature=private#auth-token"
    )
    rendered = json.dumps(fields)
    assert fields["origin"] == "https://www.lavetir.com"
    assert fields["query_present"] is True
    assert fields["fragment_present"] is True
    assert "account" not in rendered
    assert "password" not in rendered
    assert "Signature" not in rendered
    assert "private" not in rendered
    assert "auth-token" not in rendered


class _FakePage:
    def __init__(self):
        self.handlers = {}
        self.main_frame = SimpleNamespace(url="about:blank")

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        self.handlers[event].remove(handler)


def test_navigation_observer_records_safe_main_frame_timeline():
    page = _FakePage()
    observer = diagnostic.NavigationObserver(page, diagnostic.time.perf_counter())
    observer.attach()
    first = SimpleNamespace(url="https://www.lavetir.com/?private=one")
    page.main_frame = first
    for handler in page.handlers["framenavigated"]:
        handler(first)
    second = SimpleNamespace(url="https://lavetir.com/#private-fragment")
    page.main_frame = second
    for handler in page.handlers["framenavigated"]:
        handler(second)
    observer.detach()

    assert len(observer.timeline) == 2
    assert observer.timeline[0]["query_present"] is True
    assert observer.timeline[1]["fragment_present"] is True
    assert observer.timeline[1]["previous_origin"] == "https://www.lavetir.com"
    assert "private" not in json.dumps(observer.timeline)
    assert "one" not in json.dumps(observer.timeline)


def test_url_evidence_classifies_candidates_without_confirming_root_cause():
    report = {
        "stage_snapshots": [
            {
                "stage": "after_home_navigation",
                "actual_pathname": "/",
                "query_present": True,
                "fragment_present": False,
                "cloudflare_challenge_detected": False,
                "origin_relation": "SAME_HOST",
                "homepage_url_check_result": "FAIL",
                "elapsed_ms": 20,
            },
            {
                "stage": "after_home_ready",
                "actual_pathname": "/",
                "query_present": False,
                "fragment_present": False,
                "cloudflare_challenge_detected": False,
                "origin_relation": "APEX_WWW_VARIANT",
                "homepage_url_check_result": "FAIL",
                "elapsed_ms": 80,
            },
            {
                "stage": "before_home_url_assertion",
                "actual_pathname": "/en-us/",
                "query_present": False,
                "fragment_present": False,
                "cloudflare_challenge_detected": True,
                "origin_relation": "APEX_WWW_VARIANT",
                "homepage_url_check_result": "FAIL",
                "elapsed_ms": 100,
            },
        ],
        "navigation_timeline": [
            {
                "event": "main_frame_navigation",
                "origin": "https://www.lavetir.com",
                "previous_origin": None,
                "pathname": "/",
                "previous_pathname": None,
                "elapsed_ms": 10,
            },
            {
                "event": "main_frame_navigation",
                "origin": "https://lavetir.com",
                "previous_origin": "https://www.lavetir.com",
                "pathname": "/",
                "previous_pathname": "/",
                "elapsed_ms": 70,
            },
            {
                "event": "main_frame_navigation",
                "origin": "https://lavetir.com",
                "previous_origin": "https://lavetir.com",
                "pathname": "/en-us/",
                "previous_pathname": "/",
                "elapsed_ms": 90,
            },
            {"event": "main_document_response", "status": 403},
        ],
    }
    evidence = diagnostic._root_cause_candidates(report, {"pages": {"home": {"url": "/"}}})
    assert evidence["canonical_apex_www_transition_observed"] is True
    assert evidence["apex_www_variant_observed"] is True
    assert evidence["root_path_query_or_fragment_observed"] is True
    assert evidence["locale_path_candidate_unverified"] is True
    assert evidence["navigation_after_home_ready_candidate"] is True
    assert evidence["cloudflare_challenge_indicator_observed"] is True
    assert evidence["main_document_http_error_statuses"] == [403]
    assert "no root cause is confirmed" in evidence["classification_note"]


def test_diagnostic_exception_writes_safe_report_without_exception_message(tmp_path, monkeypatch):
    sentinel = "signature-secret-sentinel"
    cookie_sentinel = "cookie-secret-sentinel"
    token_sentinel = "access-token-sentinel"
    monkeypatch.setenv("LAVETIR_US_SHOPIFY_SIGNATURE", sentinel)
    monkeypatch.setenv("BUILD_NUMBER", "81")

    def fail_before_browser(*args, **kwargs):
        raise RuntimeError(
            f"browser startup failed with {sentinel}; Cookie={cookie_sentinel}; "
            f"Authorization: Bearer {token_sentinel}"
        )

    monkeypatch.setattr(diagnostic, "create_browser", fail_before_browser)
    exit_code, report = diagnostic.run_diagnostic(tmp_path)
    report_files = list(tmp_path.glob("run-*.json"))
    assert exit_code == 1
    assert report["status"] == "FAIL"
    assert report["error_category"] == "RuntimeError"
    assert len(report_files) == 1
    assert report_files[0].name == "run-81.json"
    content = report_files[0].read_text(encoding="utf-8")
    assert sentinel not in content
    assert cookie_sentinel not in content
    assert token_sentinel not in content
    assert "browser startup failed" not in content
    assert "Cookie" not in content
    assert "Authorization" not in content
    assert "token" not in content.lower()


class _FakeRoute:
    def __init__(self, url, method):
        self.request = SimpleNamespace(url=url, method=method)
        self.calls = []

    def abort(self):
        self.calls.append("abort")

    def fallback(self):
        self.calls.append("fallback")

    def continue_(self):
        self.calls.append("continue")


class _FakeContext:
    def __init__(self):
        self.handlers = []

    def route(self, pattern, handler):
        self.handlers.append(handler)


def test_readonly_guard_is_attached_after_access_policy_and_blocks_cart_mutations():
    context = _FakeContext()

    # create_browser installs the access policy first; the diagnostic attaches
    # the mutation guard afterward so ordinary requests can fall back in chain.
    def signed_request_policy(route):
        route.calls.append("signed_request_policy")

    context.route("**/*", signed_request_policy)
    runtime = SimpleNamespace(context=context)
    guard = diagnostic._attach_readonly_guard(runtime)
    assert len(context.handlers) == 2

    blocked = _FakeRoute("https://www.lavetir.com/cart/add.js?token=hidden", "POST")
    context.handlers[1](blocked)
    assert blocked.calls == ["abort"]
    assert guard.violation_count() == 1
    assert guard.violations()[0]["path"] == "/cart/add.js"

    ordinary = _FakeRoute("https://www.lavetir.com/", "GET")
    context.handlers[1](ordinary)
    assert ordinary.calls == ["fallback"]
    guard.detach()


def test_manual_pipeline_uses_credentials_binding_and_has_no_automatic_trigger():
    pipeline = (
        diagnostic.PROJECT_ROOT
        / "jenkins"
        / "Jenkinsfile.lavetir-mobile-search-diagnostic"
    ).read_text(encoding="utf-8")
    assert "triggers {" not in pipeline
    assert "cron(" not in pipeline
    assert "pollSCM(" not in pipeline
    assert "UserIdCause" in pipeline
    assert "withCredentials([" in pipeline
    for credential_id in diagnostic.SIGNED_REQUEST_ENV_NAMES:
        assert f"credentialsId: '{credential_id}'" in pipeline
    assert "scripts/diagnose_lavetir_search_home_url.py" in pipeline
    assert "archiveArtifacts" in pipeline


def test_pipeline_archive_directory_matches_script_default_output_directory():
    pipeline = (
        diagnostic.PROJECT_ROOT
        / "jenkins"
        / "Jenkinsfile.lavetir-mobile-search-diagnostic"
    ).read_text(encoding="utf-8")
    expected_directory = f"artifacts/diagnostics/{diagnostic.DIAGNOSTIC_NAME}"
    expected_report = f'{expected_directory}/run-${{env.BUILD_NUMBER}}.json'

    assert f'def expectedReport = "{expected_report}"' in pipeline
    assert "archiveArtifacts artifacts: expectedReport, allowEmptyArchive: false" in pipeline


def test_pipeline_requires_current_report_after_diagnostic_starts_and_archives_failures():
    pipeline = (
        diagnostic.PROJECT_ROOT
        / "jenkins"
        / "Jenkinsfile.lavetir-mobile-search-diagnostic"
    ).read_text(encoding="utf-8")
    post_actions = pipeline.split("    post {", 1)[1]

    assert "DIAGNOSTIC_EXECUTION_STARTED == 'true'" in pipeline
    assert "fileExists(expectedReport)" in post_actions
    assert "Diagnostic report missing for build" in post_actions
    assert "currentBuild.currentResult != 'FAILURE'" in post_actions
    assert "Diagnostic was not started; preserving the earlier stage result." in post_actions
    assert "allowEmptyArchive: false" in post_actions
    assert "allowEmptyArchive: true" not in post_actions
    assert "DIAGNOSTIC_EXIT_CODE" not in post_actions
    assert "always {" in post_actions


def test_pipeline_clears_only_the_current_build_report_before_execution():
    pipeline = (
        diagnostic.PROJECT_ROOT
        / "jenkins"
        / "Jenkinsfile.lavetir-mobile-search-diagnostic"
    ).read_text(encoding="utf-8")
    diagnostic_stage = pipeline.split("stage('Run Readonly URL Diagnostic')", 1)[1].split(
        "stage('Secret Leakage Check')", 1
    )[0]
    unix_invocation = diagnostic_stage.index("script: '.venv/bin/python")
    windows_invocation = diagnostic_stage.index("script: '.venv\\\\Scripts\\\\python.exe")

    assert "run-${env.BUILD_NUMBER}.json" in diagnostic_stage[:unix_invocation]
    assert "rm -f" in diagnostic_stage[:unix_invocation]
    assert "reportPath.replace('/', '\\\\')" in diagnostic_stage[:windows_invocation]
    assert "del /f /q" in diagnostic_stage[:windows_invocation]


def test_current_build_archive_pattern_matches_a_report_file(tmp_path):
    pipeline = (
        diagnostic.PROJECT_ROOT
        / "jenkins"
        / "Jenkinsfile.lavetir-mobile-search-diagnostic"
    ).read_text(encoding="utf-8")
    build_number = "81"
    relative_dir = f"artifacts/diagnostics/{diagnostic.DIAGNOSTIC_NAME}"
    expected_report = tmp_path / relative_dir / f"run-{build_number}.json"
    expected_report.parent.mkdir(parents=True)
    expected_report.write_text('{"status": "FAIL"}\n', encoding="utf-8")
    report_match = re.search(r'def expectedReport = "([^\"]+)"', pipeline)
    assert report_match is not None
    expected_pattern = report_match.group(1).replace("${env.BUILD_NUMBER}", build_number)
    assert "archiveArtifacts artifacts: expectedReport" in pipeline

    assert list(tmp_path.glob(expected_pattern)) == [expected_report]


def test_report_filename_uses_current_build_number(tmp_path, monkeypatch):
    monkeypatch.setenv("BUILD_NUMBER", "81")
    report_dir = tmp_path / "artifacts" / "diagnostics" / diagnostic.DIAGNOSTIC_NAME

    path = diagnostic._write_report(diagnostic._new_report(), report_dir)

    assert path == report_dir / "run-81.json"


def test_search_page_home_url_business_guard_remains_exact():
    search_page = diagnostic.PROJECT_ROOT.joinpath("pages", "search_page.py").read_text(
        encoding="utf-8"
    )
    assert 'if self.page.url.rstrip("/") != home.base_url().rstrip("/"):' in search_page


def test_script_never_submits_search_or_calls_transactional_flows():
    script = diagnostic.PROJECT_ROOT.joinpath(
        "scripts", "diagnose_lavetir_search_home_url.py"
    ).read_text(encoding="utf-8")
    assert "ensure_search_session_open(home)" in script
    assert ".fill_query(" not in script
    assert ".submit_query(" not in script
    assert ".add_to_cart(" not in script
    assert "checkout_button().click" not in script
