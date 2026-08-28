"""Offline contract tests for the manual Full / Readonly Jenkins split."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ORIGINAL_PIPELINE = PROJECT_ROOT / "Jenkinsfile"
FULL_PIPELINE = PROJECT_ROOT / "Jenkinsfile.full"
READONLY_PIPELINE = PROJECT_ROOT / "Jenkinsfile.readonly"

COMMON_STAGES = (
    "Checkout",
    "Environment",
    "Install Dependencies",
    "Install Playwright Browsers",
    "Static Validation",
    "Runtime Contract",
    "Signed Request / Site Access",
    "Secret Leakage Check",
    "Result Validation",
)
CREDENTIAL_IDS = (
    "MONDRESSY_US_SHOPIFY_SIGNATURE",
    "MONDRESSY_US_SHOPIFY_SIGNATURE_INPUT",
    "MONDRESSY_US_SHOPIFY_SIGNATURE_AGENT",
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_split_pipeline_files_exist() -> None:
    assert FULL_PIPELINE.exists()
    assert READONLY_PIPELINE.exists()


def test_new_pipelines_are_manual_only() -> None:
    forbidden_trigger_patterns = (
        r"triggers\s*\{",
        r"cron\s*\(",
        r"pollSCM\s*\(",
    )
    for path in (FULL_PIPELINE, READONLY_PIPELINE):
        content = _text(path)
        assert "parameters" in content
        for pattern in forbidden_trigger_patterns:
            assert re.search(pattern, content, flags=re.IGNORECASE) is None, (path, pattern)


def test_full_runner_schema_and_stability_mapping() -> None:
    content = _text(FULL_PIPELINE)
    assert "stage('Website Smoke V1')" in content
    assert "scripts/run_website_smoke_v1.py" in content
    assert "scripts/run_website_smoke_readonly_v1.py" not in content
    assert "--suite website_smoke_v1" in content
    assert "--suite website_smoke_readonly_v1" not in content
    assert "scripts/record_stability.py" in content
    assert "artifacts/**" in content
    assert "validate_ci_safe_outputs.py" in content


def test_readonly_runner_schema_and_stability_boundary() -> None:
    content = _text(READONLY_PIPELINE)
    assert "stage('Website Smoke Readonly V1')" in content
    assert "scripts/run_website_smoke_readonly_v1.py" in content
    assert "scripts/run_website_smoke_v1.py" not in content
    assert "--suite website_smoke_readonly_v1" in content
    assert "--suite website_smoke_v1" not in content
    assert "scripts/record_stability.py" not in content
    assert "STABILITY_" not in content
    assert "artifacts/**" in content
    assert "validate_ci_safe_outputs.py" in content


def test_both_pipelines_keep_common_infrastructure_and_parameters() -> None:
    for path in (FULL_PIPELINE, READONLY_PIPELINE):
        content = _text(path)
        for stage in COMMON_STAGES:
            assert f"stage('{stage}')" in content
        assert "choices: ['both', 'desktop', 'mobile']" in content
        assert "Default target is both." in content
        assert "--viewport both" in content
        assert "--viewport ${params.SMOKE_VIEWPORT}" in content
        assert "skipDefaultCheckout(true)" in content
        assert "disableConcurrentBuilds()" in content
        assert "timestamps()" in content
        assert "timeout(time: 60, unit: 'MINUTES')" in content
        assert "buildDiscarder(" in content
        for credential_id in CREDENTIAL_IDS:
            assert f"credentials('{credential_id}')" in content
        assert "archiveArtifacts artifacts: 'artifacts/**'" in content


def test_smoke_failure_is_caught_before_later_gates() -> None:
    for path, stage_name in (
        (FULL_PIPELINE, "Website Smoke V1"),
        (READONLY_PIPELINE, "Website Smoke Readonly V1"),
    ):
        content = _text(path)
        smoke = content.index(f"stage('{stage_name}')")
        secret = content.index("stage('Secret Leakage Check')")
        result = content.index("stage('Result Validation')")
        assert smoke < secret < result
        assert "catchError(buildResult: 'FAILURE', stageResult: 'FAILURE')" in content
        assert "returnStatus: true" in content


def test_original_jenkinsfile_is_byte_for_byte_unchanged() -> None:
    expected = subprocess.check_output(
        ["git", "show", "HEAD:Jenkinsfile"],
        cwd=PROJECT_ROOT,
    )
    assert ORIGINAL_PIPELINE.read_bytes() == expected


def test_no_explicit_shared_workspace_configuration() -> None:
    content = _text(ORIGINAL_PIPELINE)
    assert "customWorkspace" not in content
    assert re.search(r"(?<!\w)ws\s*\(", content) is None
