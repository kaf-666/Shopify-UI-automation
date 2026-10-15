"""Offline contract tests for the manual Shirees Readonly Jenkins pilot."""

from __future__ import annotations

import re
from pathlib import Path

from utils.browser import load_site_config
from utils.errors import sanitize_message, sensitive_env_names


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PIPELINE = PROJECT_ROOT / "Jenkinsfile.readonly.shirees"
SHIREES = load_site_config("shirees")


def _text() -> str:
    return PIPELINE.read_text(encoding="utf-8")


def test_shirees_pipeline_is_fixed_manual_only_and_keeps_viewport_choice() -> None:
    assert PIPELINE.exists()
    content = _text()

    assert "agent any" in content
    assert "skipDefaultCheckout(true)" in content
    assert "disableConcurrentBuilds()" in content
    assert "timestamps()" in content
    assert "timeout(time: 60, unit: 'MINUTES')" in content
    assert "buildDiscarder(" in content
    assert "name: 'SMOKE_VIEWPORT'" in content
    assert "choices: ['both', 'desktop', 'mobile']" in content
    assert "name: 'SITE'" not in content
    assert "params.SITE" not in content
    assert "--site ${" not in content
    assert "echo 'Site: shirees'" in content
    assert "echo 'Access mode: signed_request'" in content
    assert "echo 'Signed Request credentials: configured'" in content
    assert "'Proxy: configured'" in content
    assert "'Proxy: direct mode'" in content

    for pattern in (r"triggers\s*\{", r"\bcron\s*\(", r"pollSCM\s*\(", r"upstream\s*\("):
        assert re.search(pattern, content, flags=re.IGNORECASE) is None


def test_shirees_pipeline_binds_only_matching_credential_ids() -> None:
    content = _text()
    credential_names = (
        "SHIREES_US_SHOPIFY_SIGNATURE",
        "SHIREES_US_SHOPIFY_SIGNATURE_INPUT",
        "SHIREES_US_SHOPIFY_SIGNATURE_AGENT",
    )

    for name in credential_names:
        assert re.search(
            rf"(?m)^\s*{name}\s*=\s*credentials\('{name}'\)\s*$",
            content,
        )
    assert "MONDRESSY_US_SHOPIFY_SIGNATURE" not in content
    assert "LAVETIR_US_SHOPIFY_SIGNATURE" not in content
    assert all(f"${{env.{name}}}" not in content for name in credential_names)
    writes = [line.strip() for line in content.splitlines() if "writeFile" in line]
    assert len(writes) == 1
    assert writes[0].startswith("writeFile file: 'publication-failure.json', text:")
    assert "failureCode" in writes[0]
    assert "'CURRENT_RESULTS_MISSING'" in content
    assert "'CURRENT_SECRET_SCAN_NOT_PASSED'" in content
    assert "'CURRENT_SCHEMA_NOT_PASSED'" in content


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


def test_shirees_pipeline_runs_ordered_readonly_gates_and_archives_evidence() -> None:
    content = _text()
    stages = (
        "Checkout",
        "Environment",
        "Install Dependencies",
        "Install Playwright Browsers",
        "Static Validation",
        "Runtime Contract",
        "Signed Request / Site Access",
        "Website Smoke Readonly V1",
        "Secret Leakage Check",
        "Result Validation",
    )
    stage_positions = [content.index(f"stage('{stage}')") for stage in stages]
    assert stage_positions == sorted(stage_positions)

    assert "checkout scm" in content
    assert "git rev-parse HEAD" in content
    assert "env.GIT_COMMIT_SHA = checkoutSha" in content
    assert 'echo "Checkout SHA: ${checkoutSha}"' in content
    assert "requirements.lock.txt" in content
    assert "pip install --upgrade" not in content
    assert "playwright install chromium webkit" in content
    assert "scripts/validate_site_config.py --site shirees --suite website_smoke_readonly_v1" in content
    assert r"scripts\\validate_site_config.py --site shirees --suite website_smoke_readonly_v1" in content
    assert "scripts/validate_runtime_contract.py" in content
    assert "scripts/validate_site_access.py --site shirees --viewport both" in content
    assert r"scripts\\validate_site_access.py --site shirees --viewport both" in content
    assert "scripts/run_website_smoke_readonly_v1.py --site shirees --viewport ${params.SMOKE_VIEWPORT}" in content
    assert r"scripts\\run_website_smoke_readonly_v1.py --site shirees --viewport ${params.SMOKE_VIEWPORT}" in content
    assert "scripts/run_website_smoke_v1.py" not in content
    assert "run_checkout_cases.py" not in content
    assert "run_checkout" not in content
    assert "transactional qualification" not in content.lower()
    assert "scripts/validate_ci_safe_outputs.py" in content
    assert "scripts/validate_result_schema.py --suite website_smoke_readonly_v1" in content
    assert "catchError(buildResult: 'FAILURE', stageResult: 'FAILURE')" in content
    from tests.test_jenkins_pipeline_split import assert_scoped_publication
    assert_scoped_publication(content)
    assert "output/**" not in content

    post_always = re.search(r"post\s*\{\s*always\s*\{(?P<body>.*?)\n\s*}\s*}", content, re.DOTALL)
    assert post_always is not None
    assert "scripts/finalize_ci_run.py" in content[content.index("    post {"):]


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
