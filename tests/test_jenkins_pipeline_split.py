"""Fixed Jenkins entries and their shared adapter preserve production boundaries."""
from pathlib import Path
import re

import pytest
from scripts.run_smoke_ci import Target
from utils.site_config_validator import validate_site_config

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ADAPTER = (PROJECT_ROOT / "ci/smoke_pipeline.groovy").read_text(encoding="utf-8")
ENTRIES = [(site, suite, f"Jenkinsfile.{mode}{suffix}")
           for site, suffix in [("mondressy", ""), ("lavetir", ".lavetir"), ("shirees", ".shirees")]
           for mode, suite in [("readonly", "website_smoke_readonly_v1"), ("full", "website_smoke_v1")]]


def entry(path):
    return (PROJECT_ROOT / path).read_text(encoding="utf-8")


@pytest.mark.parametrize("site,suite,path", ENTRIES)
def test_fixed_entry_matches_profile_credentials_and_does_not_expose_site_parameter(site, suite, path):
    text = entry(path)
    bindings = dict(re.findall(r"(\w+) = credentials\('([^']+)'\)", text))
    config = validate_site_config(site, suite=suite)
    assert bindings == {name: name for name in config["access"]["env"].values()}
    assert f"CI_SITE = '{site}'" in text
    assert f"CI_SUITE = '{suite}'" in text
    assert "params.SITE" not in text and "name: 'SITE'" not in text
    assert "load 'ci/smoke_pipeline.groovy'" in text
    assert "checkout scm" in text
    assert "checkoutIdentity(scmVars)" in text
    assert "git checkout" not in text
    assert Target(site, suite, "both").credential_names == config["access"]["env"]


@pytest.mark.parametrize("site,suite,path", ENTRIES)
def test_entry_keeps_parameters_schedule_stage_names_and_operational_options(site, suite, path):
    text = entry(path)
    diagnostic = site == "lavetir" and "readonly" in path
    assert "choices: ['both', 'desktop', 'mobile']" in text
    assert "Default target is both." in text
    assert re.findall(r"name: '([^']+)'", text) == ["SMOKE_VIEWPORT"] + (["VARIANT_DIAGNOSTIC"] if diagnostic else [])
    assert re.findall(r"cron\('([^']+)'\)", text) == (["H H/4 * * *"] if diagnostic else [])
    expected = ["Checkout", "Environment", "Install Dependencies", "Prepare Run",
                "Install Playwright Browsers", "Static Validation", "Runtime Contract",
                "Site Access" if diagnostic else "Signed Request / Site Access"]
    if diagnostic:
        expected.append("Variant Stability Diagnostic")
    expected += ["Website Smoke Readonly V1" if "readonly" in path else "Website Smoke V1",
                 "Secret Leakage Check", "Result Validation"]
    assert re.findall(r"stage\('([^']+)'\)", text) == expected
    for option in ["agent any", "skipDefaultCheckout(true)", "disableConcurrentBuilds()",
                   "timestamps()", "timeout(time: 60, unit: 'MINUTES')",
                   "buildDiscarder(logRotator(numToKeepStr: '25', artifactNumToKeepStr: '25'))"]:
        assert option in text
    assert not re.search(r"pollSCM\s*\(|upstream\s*\(", text)
    assert "smokeCi.runStep('smoke')" in text
    assert text.index("runStep('smoke')") < text.index("runStep('secret')") < text.index("runStep('schema')")
    assert "smokeCi.publish()" in text
    # Bootstrap failures must still archive only a constant current-build failure.
    assert 'artifacts/ci/${env.CI_RUN_ID}/publication-failure.json' in text
    assert "CURRENT_RESULTS_MISSING" in text
    assert "writeFile file: 'summary.html'" in text
    assert 'artifacts/ci/${env.CI_RUN_ID}/summary.html' in text
    assert "artifacts/**" not in text and "output/**" not in text


def test_adapter_preserves_smoke_failure_status_and_archive_before_acknowledgement():
    assert "catchError(buildResult: 'FAILURE', stageResult: 'FAILURE')" in ADAPTER
    assert 'env.CI_PYTHON_EXIT_CODE = "${code}"' in ADAPTER
    assert "env.CI_SECRET_GATE = code == 0 ? 'PASS' : 'FAIL'" in ADAPTER
    assert "env.CI_SCHEMA_GATE = code == 0 ? 'PASS' : 'FAIL'" in ADAPTER
    assert ADAPTER.index("invoke('publish')") < ADAPTER.index('archiveArtifacts artifacts: "artifacts/ci/${env.CI_RUN_ID}/publish/**"') < ADAPTER.index("invoke('acknowledge')")
    assert "if (publishExit != 0)" in ADAPTER
    assert "if (acknowledgeExit != 0)" in ADAPTER
    assert "fileExists(env.CI_RESULT_PATH)" in ADAPTER
    for code in ["CURRENT_RESULTS_MISSING", "CURRENT_SECRET_SCAN_NOT_PASSED", "CURRENT_SCHEMA_NOT_PASSED", "CURRENT_RUN_PUBLICATION_FAILED"]:
        assert code in ADAPTER
    assert "artifacts/**" not in ADAPTER and "output/**" not in ADAPTER
    assert "writeFile file: 'summary.html', text: failureSummary(failureCode)" in ADAPTER
    assert "Business result: NOT_AVAILABLE" in ADAPTER
    assert 'env["CI_STEP_' not in ADAPTER


def test_adapter_keeps_both_platforms_and_does_not_interpolate_user_parameters_into_shell():
    assert "isUnix()" in ADAPTER and "return sh(" in ADAPTER and "return bat(" in ADAPTER
    assert ".venv/bin/python" in ADAPTER
    assert r".venv\\Scripts\\python.exe" in ADAPTER
    assert "returnStatus: status" in ADAPTER
    assert 'env.CI_VIEWPORT = "${params.SMOKE_VIEWPORT}"' in ADAPTER
    assert "${params." not in ADAPTER[ADAPTER.index("def pythonCommand"):]
    assert "git rev-parse HEAD" in ADAPTER
    assert "requirements.lock.txt" in ADAPTER and "playwright install chromium webkit" in ADAPTER
    assert "pip install --upgrade" not in ADAPTER


def test_manual_integration_keeps_explicit_identity_and_no_timer():
    manual = entry("Jenkinsfile.smoke-integration-manual")
    assert "EXPECTED_SHA" in manual and "Manual Trigger Gate" in manual
    assert not re.search(r"cron\s*\(|pollSCM\s*\(", manual)
    text = entry("Jenkinsfile")
    assert "LEGACY_PIPELINE_DISABLED" in text
    assert "archiveArtifacts" not in text and "record_stability" not in text
    assert "credentials(" not in text and "cron(" not in text
    assert "customWorkspace" not in text
    assert not re.search(r"(?<!\w)ws\s*\(", text)
