"""Fixed Jenkins entries and their shared adapter preserve production boundaries."""
from pathlib import Path
import re
import subprocess

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


def baseline(path):
    return subprocess.check_output(["git", "show", f"39982f9:{path}"], cwd=PROJECT_ROOT).decode("utf-8")


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
    text, old = entry(path), baseline(path)
    # Existing user/job controls are an interface, including Lavetir's optional diagnostic.
    for source in [text, old]:
        assert "choices: ['both', 'desktop', 'mobile']" in source
        assert "Default target is both." in source
    assert re.findall(r"name: '([^']+)'", text) == re.findall(r"name: '([^']+)'", old)
    assert re.findall(r"cron\('([^']+)'\)", text) == re.findall(r"cron\('([^']+)'\)", old)
    assert re.findall(r"stage\('([^']+)'\)", text) == re.findall(r"stage\('([^']+)'\)", old)
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


def test_original_jenkinsfile_and_manual_integration_are_unchanged():
    for name in ["Jenkinsfile", "Jenkinsfile.smoke-integration-manual"]:
        # Compare Git's canonical bytes, respecting checkout CRLF filters.
        actual = subprocess.check_output(["git", "hash-object", "--path", name, name], cwd=PROJECT_ROOT)
        expected = subprocess.check_output(["git", "rev-parse", f"HEAD:{name}"], cwd=PROJECT_ROOT)
        assert actual == expected
    text = entry("Jenkinsfile")
    assert "customWorkspace" not in text
    assert not re.search(r"(?<!\w)ws\s*\(", text)
