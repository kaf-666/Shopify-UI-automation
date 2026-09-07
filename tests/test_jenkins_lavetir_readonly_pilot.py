"""Offline contract tests for the Lavetir Readonly Jenkins pilot."""

from __future__ import annotations

import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PIPELINE = PROJECT_ROOT / "Jenkinsfile.readonly.lavetir"


def _text() -> str:
    return PIPELINE.read_text(encoding="utf-8")


def test_lavetir_pipeline_exists_and_is_manual_only() -> None:
    assert PIPELINE.exists()
    content = _text()

    for pattern in (r"triggers\s*\{", r"cron\s*\(", r"pollSCM\s*\(", r"upstream\s*\("):
        assert re.search(pattern, content, flags=re.IGNORECASE) is None


def test_lavetir_pipeline_is_fixed_to_lavetir_and_keeps_viewport_parameter() -> None:
    content = _text()

    assert "agent any" in content
    assert "skipDefaultCheckout(true)" in content
    assert "disableConcurrentBuilds()" in content
    assert "timestamps()" in content
    assert "timeout(time: 60, unit: 'MINUTES')" in content
    assert "buildDiscarder(" in content
    assert "name: 'SMOKE_VIEWPORT'" in content
    assert "choices: ['both', 'desktop', 'mobile']" in content
    assert "checkout scm" in content
    assert "env.GIT_COMMIT_SHA" in content
    assert "git checkout main" not in content
    assert "git checkout feat/multi-site-pilot" not in content
    assert "echo 'Site: lavetir'" in content
    assert "stage('Site Access')" in content
    assert "stage('Signed Request / Site Access')" not in content
    assert "--site lavetir --viewport both" in content
    assert "--site lavetir --viewport ${params.SMOKE_VIEWPORT}" in content


def test_lavetir_pipeline_binds_only_lavetir_credentials() -> None:
    content = _text()

    for credential_id in (
        "LAVETIR_US_SHOPIFY_SIGNATURE",
        "LAVETIR_US_SHOPIFY_SIGNATURE_INPUT",
        "LAVETIR_US_SHOPIFY_SIGNATURE_AGENT",
    ):
        assert f"credentials('{credential_id}')" in content
    assert "MONDRESSY_US_SHOPIFY_SIGNATURE" not in content
    assert "MONDRESSY_US_SHOPIFY_SIGNATURE_INPUT" not in content
    assert "MONDRESSY_US_SHOPIFY_SIGNATURE_AGENT" not in content
    assert "mondressy" not in content.lower()
    assert "Signed Request: configured by Jenkins Credentials Binding (values redacted)" in content
    assert "Proxy: configured" in content
    assert "Proxy: direct mode" in content


def test_lavetir_pipeline_preserves_readonly_gates_and_boundaries() -> None:
    content = _text()

    assert "scripts/run_website_smoke_readonly_v1.py" in content
    assert "scripts/run_website_smoke_v1.py" not in content
    assert "catchError(buildResult: 'FAILURE', stageResult: 'FAILURE')" in content
    assert "env.READONLY_PYTHON_EXIT_CODE" in content
    assert "scripts/validate_ci_safe_outputs.py" in content
    assert "scripts/validate_result_schema.py --suite website_smoke_readonly_v1" in content
    assert "scripts/record_stability.py" not in content
    assert "STABILITY_" not in content
    assert "archiveArtifacts artifacts: 'artifacts/**'" in content
