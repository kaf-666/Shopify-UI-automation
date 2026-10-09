"""Offline contracts for the manual Readonly integration pilot Pipeline."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PIPELINE = PROJECT_ROOT / "Jenkinsfile.smoke-integration-manual"
READONLY_RUNNER = PROJECT_ROOT / "scripts" / "run_website_smoke_readonly_v1.py"
FULL_RUNNER = "scripts/run_website_smoke_v1.py"
def pipeline_text() -> str:
    return PIPELINE.read_text(encoding="utf-8")


def test_pipeline_is_manual_only_and_requires_a_real_manual_cause() -> None:
    content = pipeline_text()

    assert PIPELINE.is_file()
    assert "stage('Manual Trigger Gate')" in content
    assert "currentBuild.getBuildCauses()" in content
    assert "hudson.model.Cause$UserIdCause" in content
    for pattern in (
        r"triggers\s*\{",
        r"cron\s*\(",
        r"pollSCM\s*\(",
        r"upstream\s*\(",
        r"build\s+job\s*:",
        r"properties\s*\(",
        r"retry\s*\(",
    ):
        assert re.search(pattern, content, flags=re.IGNORECASE) is None


def test_pipeline_allowlists_sites_viewports_and_exact_checkout_sha() -> None:
    content = pipeline_text()

    assert "name: 'SITE'" in content
    assert "choices: ['mondressy', 'lavetir']" in content
    assert "name: 'SMOKE_VIEWPORT'" in content
    assert "choices: ['both', 'desktop', 'mobile']" in content
    assert "name: 'EXPECTED_SHA'" in content
    assert "[0-9a-f]{40}" in content
    assert "checkout scm" in content
    assert "git rev-parse HEAD" in content
    assert "checkoutSha != expectedSha" in content
    assert "integration/smoke-color-and-search-evidence" in content


def test_pipeline_runs_only_readonly_with_explicit_allowlisted_arguments() -> None:
    content = pipeline_text()

    assert "website_smoke_readonly_v1" in content
    assert "scripts/run_website_smoke_readonly_v1.py" in content
    assert FULL_RUNNER not in content
    assert "--site \"$SMOKE_SITE\" --viewport \"$SMOKE_VIEWPORT\"" in content
    assert '--site "%SMOKE_SITE%" --viewport "%SMOKE_VIEWPORT%"' in content
    assert "env.SMOKE_SITE = params.SITE" in content
    assert "env.SMOKE_VIEWPORT = params.SMOKE_VIEWPORT" in content
    assert "stage('Website Smoke Readonly V1')" in content


def test_credentials_are_selected_by_site_and_scoped_to_each_sensitive_stage() -> None:
    content = pipeline_text()

    assert "def signedRequestBindings(String site)" in content
    assert "[mondressy: 'MONDRESSY', lavetir: 'LAVETIR']" in content
    assert "['SIGNATURE', 'SIGNATURE_INPUT', 'SIGNATURE_AGENT']" in content
    assert '"${prefix}_US_SHOPIFY_${suffix}"' in content
    assert "[$class: 'StringBinding', credentialsId: name, variable: name]" in content
    assert "withCredentials(signedRequestBindings(env.SMOKE_SITE))" in content
    assert "withCredentials(signedRequestBindings('mondressy'))" in content
    assert "withCredentials(signedRequestBindings('lavetir'))" in content
    assert "stage('Signed Request / Site Access')" in content
    assert "stage('Secret Leakage Check')" in content
    assert "MONDRESSY_US_SHOPIFY_SIGNATURE" not in content
    assert "LAVETIR_US_SHOPIFY_SIGNATURE" not in content
    assert re.search(r"environment\s*\{", content) is None
    assert "printenv" not in content.lower()
    assert "setenv" not in content.lower()
    assert re.search(r"(?im)^\s*(?:echo|print|println)\s+.*(?:signature|token|cookie|authorization)", content) is None


def test_readonly_guard_secret_gate_and_artifact_boundaries_are_preserved() -> None:
    content = pipeline_text()
    runner = READONLY_RUNNER.read_text(encoding="utf-8")

    assert "ReadonlyMutationGuard" in runner
    assert "mutation-guard-summary.json" in content
    assert "Readonly Mutation Violations:" in content
    assert "totalViolations != 0" in content
    assert "scripts/validate_ci_safe_outputs.py" in content
    assert "env.SECRET_GATE_PASSED = 'true'" in content
    assert "env.SECRET_GATE_PASSED == 'true'" in content
    assert "Artifact archival skipped because the Secret Leakage Check did not pass" in content
    assert "archiveArtifacts artifacts: 'artifacts/**'" in content
    assert "env.ARTIFACT_ARCHIVE_ATTEMPTED = 'true'" in content
    assert "env.ARTIFACT_ARCHIVE_ATTEMPTED != 'true'" in content
    assert "post {" in content
    assert "stage('Artifact Archive')" in content
    # Search failure sidecars are conditional evidence; ordinary PASS needs no sidecar.
    assert "search-url-diagnostic" not in content
    assert "search_url_diagnostic" not in content


def test_runner_failure_is_kept_as_failure_while_later_gates_run() -> None:
    content = pipeline_text()
    runner_stage = content.index("stage('Website Smoke Readonly V1')")
    secret_stage = content.index("stage('Secret Leakage Check')")
    identify_stage = content.index("stage('Identify Current Run Results')")
    mutation_stage = content.index("stage('Readonly Mutation Guard Validation')")
    schema_stage = content.index("stage('Result Validation')")
    archive_stage = content.index("stage('Artifact Archive')")

    assert runner_stage < secret_stage < identify_stage < mutation_stage < schema_stage < archive_stage
    assert "catchError(buildResult: 'FAILURE', stageResult: 'FAILURE')" in content
    assert "runner_status=$?" in content
    assert "runnerExitCode != '0'" in content
    assert "Readonly Runner reported business or framework failures" in content
    # Schema validation occurs before the runner's nonzero status is raised, so both gates execute.
    assert content.index("scripts/validate_result_schema.py --suite website_smoke_readonly_v1 --results") < content.index(
        "runnerExitCode != '0'"
    )


def test_current_run_results_are_required_and_schema_validation_is_explicit() -> None:
    content = pipeline_text()

    assert "deleteDir()" in content
    assert "env.READONLY_PROCESS_STARTED = 'true'" in content
    assert "READONLY_PROCESS_STARTED != 'true'" in content
    assert "SMOKE_RUN_STARTED_NS" in content
    assert "run_id\") != candidate.parent.name" in content
    assert "started_at\"]" in content
    assert "candidate.is_symlink() or candidate.parent.is_symlink()" in content
    assert "resolved.is_relative_to(workspace_root)" in content
    assert "len(matches) != 1" in content
    assert "CURRENT_RESULTS_PATH" in content
    assert "scripts/validate_result_schema.py --suite website_smoke_readonly_v1 --results" in content
    assert "allowEmptyArchive: true" in content  # Only early failures can reach archive without runner results.


def test_embedded_results_gate_selects_a_real_current_run_file(tmp_path: Path) -> None:
    content = pipeline_text()
    match = re.search(r"text:\s*'''(.*?)'''", content, flags=re.DOTALL)
    assert match is not None
    helper = match.group(1)

    site = "mondressy"
    run_id = "20261009_120000"
    results_dir = tmp_path / "artifacts" / "website-smoke-readonly-v1" / site / run_id
    results_dir.mkdir(parents=True)
    results_path = results_dir / "results.json"
    results_path.write_text(
        json.dumps(
            {
                "site": site,
                "run_id": run_id,
                "started_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            }
        ),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.update({"SMOKE_SITE": site, "SMOKE_RUN_STARTED_NS": "1"})
    found = subprocess.run(
        [sys.executable, "-c", helper],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert found.returncode == 0, found.stderr
    assert found.stdout.strip() == results_path.relative_to(tmp_path).as_posix()

    stale_env = env | {"SMOKE_RUN_STARTED_NS": "9999999999999999999"}
    stale = subprocess.run(
        [sys.executable, "-c", helper],
        cwd=tmp_path,
        env=stale_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert stale.returncode != 0
    assert "Current Readonly results.json was not uniquely identified" in stale.stderr


def test_pipeline_keeps_setup_and_runtime_validation_stages() -> None:
    content = pipeline_text()

    for stage in (
        "Manual Trigger Gate",
        "Checkout + SHA Gate",
        "Environment",
        "Install Locked Dependencies",
        "Install Playwright Browsers",
        "Static Validation",
        "Runtime Contract",
        "Signed Request / Site Access",
        "Website Smoke Readonly V1",
        "Secret Leakage Check",
        "Result Validation",
        "Artifact Archive",
    ):
        assert f"stage('{stage}')" in content
    assert "requirements.lock.txt" in content
    assert "playwright install chromium webkit" in content
    assert "scripts/validate_runtime_contract.py" in content
    assert "scripts/validate_site_config.py" in content
    assert "scripts/validate_site_access.py --site" in content
