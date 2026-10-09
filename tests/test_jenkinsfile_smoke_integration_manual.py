"""Offline contracts for the manual Readonly integration pilot Pipeline."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PIPELINE = PROJECT_ROOT / "Jenkinsfile.smoke-integration-manual"
READONLY_RUNNER = PROJECT_ROOT / "scripts" / "run_website_smoke_readonly_v1.py"
FULL_RUNNER = "scripts/run_website_smoke_v1.py"
USER_CAUSE = "hudson.model.Cause$UserIdCause"


def _manual_causes_are_allowed(causes: object, *, read_failed: bool = False) -> bool:
    """Offline model of the Pipeline's fail-closed cause-set contract."""
    if read_failed or not isinstance(causes, list) or not causes:
        return False
    try:
        return all(
            isinstance(cause, dict)
            and isinstance(cause.get("_class"), str)
            and cause.get("_class") == USER_CAUSE
            for cause in causes
        )
    except Exception:
        return False


def _proof_matches(
    proof: dict[str, str] | None,
    *,
    gate: str,
    build_number: str,
    workspace: str,
    checkout_sha: str,
    expected_sha: str,
    results_path: str = "",
) -> bool:
    """Model exact, current-build proof matching used by the Groovy Pipeline."""
    if proof is None or not re.fullmatch(r"[0-9a-f]{40}", checkout_sha):
        return False
    if checkout_sha != expected_sha:
        return False
    expected = {
        "gate": gate,
        "build_number": build_number,
        "workspace": workspace,
        "checkout_sha": checkout_sha,
        "results_path": results_path,
    }
    return proof == expected


def _archive_allowed(
    *,
    secret_proof: dict[str, str] | None,
    results_proof: dict[str, str] | None,
    build_number: str,
    workspace: str,
    checkout_sha: str,
    expected_sha: str,
    results_path: str | None,
    result_file_exists: bool,
    inherited_secret_gate_passed: bool = False,
) -> bool:
    # Deliberately ignore the inherited Boolean; only current-build proof files count.
    if not results_path or not result_file_exists:
        return False
    return _proof_matches(
        secret_proof,
        gate="SECRET_LEAKAGE_CHECK",
        build_number=build_number,
        workspace=workspace,
        checkout_sha=checkout_sha,
        expected_sha=expected_sha,
    ) and _proof_matches(
        results_proof,
        gate="RESULT_SCHEMA_VALIDATION",
        build_number=build_number,
        workspace=workspace,
        checkout_sha=checkout_sha,
        expected_sha=expected_sha,
        results_path=results_path,
    )


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


def test_manual_trigger_gate_requires_every_cause_to_be_exactly_user_id_cause() -> None:
    content = pipeline_text()
    assert "causes = currentBuild.getBuildCauses()" in content
    assert "causes instanceof List" in content
    assert "causes.isEmpty()" in content
    assert "causes.every" in content
    assert "cause instanceof Map" in content
    assert "cause.get('_class')" in content
    assert "cause.get('_class') == 'hudson.model.Cause$UserIdCause'" in content
    assert "currentBuild.getBuildCauses().any" not in content
    assert "toString().contains('UserIdCause')" not in content
    assert "could not verify build cause data" in content


@pytest.mark.parametrize(
    ("causes", "read_failed", "expected"),
    [
        ([{"_class": USER_CAUSE}], False, True),
        ([{"_class": USER_CAUSE}, {"_class": "hudson.triggers.TimerTrigger$TimerTriggerCause"}], False, False),
        ([{"_class": USER_CAUSE}, {"_class": "hudson.triggers.SCMTrigger$SCMTriggerCause"}], False, False),
        ([{"_class": USER_CAUSE}, {"_class": "hudson.model.Cause$UpstreamCause"}], False, False),
        ([{"_class": USER_CAUSE}, {"_class": "org.jenkinsci.plugins.workflow.cps.replay.ReplayCause"}], False, False),
        ([{"_class": USER_CAUSE}, {"_class": "com.sonyericsson.rebuild.RebuildCause"}], False, False),
        ([{"_class": "hudson.triggers.TimerTrigger$TimerTriggerCause"}], False, False),
        ([], False, False),
        ([{"_class": "example.UnknownCause"}], False, False),
        ([{}], False, False),
        ([{"_class": "org.jenkinsci.plugins.workflow.cps.replay.ReplayCause"}], False, False),
        ([{"_class": "com.sonyericsson.rebuild.RebuildCause"}], False, False),
        (None, True, False),
    ],
)
def test_manual_cause_contract_matrix(
    causes: object, read_failed: bool, expected: bool
) -> None:
    assert _manual_causes_are_allowed(causes, read_failed=read_failed) is expected


def test_pipeline_does_not_set_or_shorten_build_retention() -> None:
    content = pipeline_text()

    # The Pipeline does not set a retention policy or claim that removing one
    # preserves a property's prior Declarative-managed value on a reused Job.
    assert "buildDiscarder" not in content
    assert "logRotator" not in content
    assert re.search(r"\b(?:numToKeepStr|artifactNumToKeepStr)\b", content) is None
    assert re.search(r"\bproperties\s*\(", content) is None


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
    assert "currentGateProofFile(gateName)" in content
    assert '".smoke-integration-${safeGateName}-${currentBuild.number}.json"' in content
    assert "'SECRET_LEAKAGE_CHECK'" in content
    assert "'RESULT_SCHEMA_VALIDATION'" in content
    assert "currentRunArchiveEligible()" in content
    assert "Artifact archival skipped because the Readonly Runner did not start" in content
    assert "Artifact archival requires current-build Secret Gate and results proofs" in content
    assert "archiveArtifacts artifacts: 'artifacts/**', allowEmptyArchive: false" in content
    assert "SECRET_GATE_PASSED" not in content
    assert "ARTIFACT_ARCHIVE_ATTEMPTED" not in content
    assert "'ARTIFACT_ARCHIVE_ATTEMPT'" in content
    assert "post {" in content
    assert "stage('Artifact Archive')" in content
    # Search failure sidecars are conditional evidence; ordinary PASS needs no sidecar.
    assert "search-url-diagnostic" not in content
    assert "search_url_diagnostic" not in content


def test_archive_proof_is_bound_to_build_workspace_and_checked_out_sha() -> None:
    content = pipeline_text()

    assert "currentBuild.number" in content
    assert "pwd()" in content
    assert "env.CHECKED_OUT_SHA" in content
    assert "params.EXPECTED_SHA" in content
    assert "fileExists(proofFile)" in content
    assert "readFile(file: proofFile) == expectedProof" in content
    assert "currentRunResultsFileAvailable()" in content
    assert "currentGateProofFile(gateName)" in content
    assert "safeGateName" in content
    assert "currentBuild.number}" in content
    assert "env.SECRET_GATE_PASSED" not in content

    proof_builder = content[content.index("def currentGateProofText"):content.index("def writeCurrentGateProof")]
    for field in (
        "gate_status: 'PASS'",
        "exit_code: 0",
        "build_number: currentBuildNumber",
        "workspace: pwd()",
        "checkout_sha: checkoutSha",
        "results_path: resultsPath",
    ):
        assert field in proof_builder
    assert "signature" not in proof_builder.lower()
    assert "cookie" not in proof_builder.lower()


def test_secret_and_schema_proofs_are_written_only_after_their_gates_pass() -> None:
    content = pipeline_text()
    secret_stage = content[content.index("stage('Secret Leakage Check')"):content.index("stage('Identify Current Run Results')")]
    result_stage = content[content.index("stage('Result Validation')"):content.index("stage('Artifact Archive')")]

    assert secret_stage.index("if (gateExitCode != 0)") < secret_stage.index("writeCurrentGateProof(")
    assert result_stage.index("if (schemaExitCode != 0)") < result_stage.index("writeCurrentGateProof(")
    assert result_stage.index("currentRunResultsFileAvailable()") < result_stage.index("writeCurrentGateProof(")
    assert "Current Readonly results.json is missing after schema validation" in result_stage
    # The schema-success proof is written before the runner's business exit code
    # is raised, preserving evidence for a genuine Readonly failure.
    assert result_stage.index("writeCurrentGateProof(") < result_stage.index("runnerExitCode != '0'")
    archive_stage = content[content.index("stage('Artifact Archive')"):content.index("post {")]
    assert "expression { env.READONLY_PROCESS_STARTED == 'true' }" in archive_stage
    assert "currentRunArchiveEligible()" in archive_stage


def test_archive_proof_scenarios_fail_closed_and_preserve_runner_failure_evidence() -> None:
    sha = "a" * 40
    workspace = r"C:\jenkins\workspace\pilot"
    results_path = "artifacts/website-smoke-readonly-v1/mondressy/run-1/results.json"
    secret = {
        "gate": "SECRET_LEAKAGE_CHECK",
        "build_number": "7",
        "workspace": workspace,
        "checkout_sha": sha,
        "results_path": "",
    }
    result_proof = {
        "gate": "RESULT_SCHEMA_VALIDATION",
        "build_number": "7",
        "workspace": workspace,
        "checkout_sha": sha,
        "results_path": results_path,
    }

    # Manual, parameter, checkout, SHA, or Secret Gate failures cannot produce
    # a pair of current-build proofs. Each condition must therefore skip archive.
    early_or_secret_failures = (
        ("Manual Gate FAIL", None, None, results_path, True, sha, sha),
        ("parameter Gate FAIL", None, None, results_path, True, sha, sha),
        ("Checkout FAIL", None, None, results_path, True, "", sha),
        ("SHA mismatch", {**secret, "checkout_sha": "b" * 40}, result_proof, results_path, True, "b" * 40, sha),
        ("Secret Gate NOT_RUN", None, result_proof, results_path, True, sha, sha),
        ("Secret Gate FAIL", None, result_proof, results_path, True, sha, sha),
    )
    for _condition, secret_proof, results_gate_proof, candidate_path, result_exists, current_sha, expected_sha in early_or_secret_failures:
        assert not _archive_allowed(
            secret_proof=secret_proof,
            results_proof=results_gate_proof,
            build_number="7",
            workspace=workspace,
            checkout_sha=current_sha,
            expected_sha=expected_sha,
            results_path=candidate_path,
            result_file_exists=result_exists,
        )

    # An inherited SECRET_GATE_PASSED=true has no bearing on proof validation.
    assert not _archive_allowed(
        secret_proof=None,
        results_proof=result_proof,
        build_number="7",
        workspace=workspace,
        checkout_sha=sha,
        expected_sha=sha,
        results_path=results_path,
        result_file_exists=True,
        inherited_secret_gate_passed=True,
    )

    # Stale build/workspace markers and missing current result all fail closed.
    assert not _archive_allowed(
        secret_proof={**secret, "build_number": "6"},
        results_proof=result_proof,
        build_number="7",
        workspace=workspace,
        checkout_sha=sha,
        expected_sha=sha,
        results_path=results_path,
        result_file_exists=True,
    )
    assert not _archive_allowed(
        secret_proof={**secret, "workspace": r"C:\jenkins\workspace\old"},
        results_proof=result_proof,
        build_number="7",
        workspace=workspace,
        checkout_sha=sha,
        expected_sha=sha,
        results_path=results_path,
        result_file_exists=True,
    )
    assert not _archive_allowed(
        secret_proof=secret,
        results_proof=result_proof,
        build_number="7",
        workspace=workspace,
        checkout_sha=sha,
        expected_sha=sha,
        results_path=results_path,
        result_file_exists=False,
    )

    # A failing Readonly runner still has archivable evidence when the Secret
    # Gate and current result/schema contracts have succeeded.
    runner_exit_code = 1
    assert runner_exit_code != 0
    assert _archive_allowed(
        secret_proof=secret,
        results_proof=result_proof,
        build_number="7",
        workspace=workspace,
        checkout_sha=sha,
        expected_sha=sha,
        results_path=results_path,
        result_file_exists=True,
    )


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
    assert "allowEmptyArchive: false" in content


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
