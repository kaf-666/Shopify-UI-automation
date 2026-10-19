"""Shared CI dispatch and real publication semantics; no storefront traffic."""
import json
import os

import pytest

from scripts import run_smoke_ci as ci
from tests.test_ci_run_context import prepare, result_for, save_current
from tests.test_validate_result_schema import _refresh_summaries
from utils import run_context as runs


def environment(context):
    identity = context["identity"]
    return {"CI_SITE": identity["site"], "CI_SUITE": identity["suite"], "CI_VIEWPORT": identity["viewport"],
            "CI_RUN_ID": identity["run_id"], "GIT_COMMIT_SHA": identity["commit_sha"],
            "JOB_NAME": identity["job_name"], "BUILD_NUMBER": str(identity["build_number"]),
            "CI_CONTEXT_PATH": context["context_path"], "CI_PYTHON_EXIT_CODE": "0",
            "CI_JENKINS_RESULT": "SUCCESS", "CI_SCHEMA_GATE": "PASS", "CI_SECRET_GATE": "PASS"}


@pytest.mark.parametrize("site", ci.SITES)
@pytest.mark.parametrize("suite", list(runs.SUITE_ROOTS))
@pytest.mark.parametrize("viewport", ["desktop", "mobile", "both"])
def test_target_matrix_selects_only_its_cli_config_viewport_and_current_paths(monkeypatch, tmp_path, site, suite, viewport):
    context = prepare(monkeypatch, tmp_path, site=site, suite=suite, viewport=viewport)
    env = environment(context)
    calls = []
    def command(args, *, workspace, environ):
        calls.append(args)
        assert workspace == tmp_path
        assert environ["PYTHONUNBUFFERED"] == "1"
        return 0
    for step in ["static", "runtime", "access", "smoke", "secret", "schema"]:
        assert ci.run_step(step, workspace=tmp_path, environ=env, executor=command) == 0
    assert calls[1] == ["scripts/validate_site_config.py", "--site", site, "--suite", suite]
    assert calls[2] == ["scripts/validate_runtime_contract.py"]
    assert calls[3] == ["scripts/validate_site_access.py", "--site", site, "--viewport", "both"]
    runner = "run_website_smoke_v1.py" if suite == "website_smoke_v1" else "run_website_smoke_readonly_v1.py"
    expected = [f"scripts/{runner}", "--site", site, "--viewport", viewport, "--run-id", context["identity"]["run_id"],
                "--run-context", context["context_path"]]
    if suite == "website_smoke_v1" and site in {"lavetir", "shirees"}:
        expected += ["--traffic-inventory"]
    assert calls[4] == expected
    assert calls[5] == ["scripts/validate_ci_safe_outputs.py", "--run-context", context["context_path"]]
    assert calls[6] == ["scripts/validate_result_schema.py", "--suite", suite,
                        "--run-context", context["context_path"], "--results", context["results_path"]]
    assert not any("latest" in argument for call in calls for argument in call)


@pytest.mark.parametrize("site", ci.SITES)
@pytest.mark.parametrize("suite", list(runs.SUITE_ROOTS))
def test_prepare_checks_the_actual_profile_credential_mapping_and_reserves_a_unique_run(monkeypatch, tmp_path, site, suite):
    context = prepare(monkeypatch, tmp_path, site=site, suite=suite)
    env = environment(context)
    env.update(CI_RUN_ID="new-invocation", CI_CONTEXT_PATH="artifacts/ci/new-invocation/run_context.json")
    assert ci.run_step("prepare", workspace=tmp_path, environ=env) == 0
    reserved = runs.load_context(env["CI_CONTEXT_PATH"], workspace=tmp_path, environ=env)
    assert reserved["state"] == "PREPARED"
    assert reserved["identity"]["site"] == site and reserved["identity"]["suite"] == suite
    with pytest.raises(FileExistsError):
        ci.run_step("prepare", workspace=tmp_path, environ=env)


@pytest.mark.parametrize("code", [0, 1, 2])
def test_smoke_exit_codes_are_preserved_and_not_promoted_by_the_dispatcher(monkeypatch, tmp_path, code):
    context = prepare(monkeypatch, tmp_path)
    assert ci.run_step("smoke", workspace=tmp_path, environ=environment(context),
                       executor=lambda *_a, **_k: code) == code


@pytest.mark.parametrize("key,value", [("CI_SITE", "lavetir"), ("CI_SUITE", "website_smoke_readonly_v1"),
                                       ("CI_VIEWPORT", "mobile"), ("GIT_COMMIT_SHA", "a" * 40),
                                       ("BUILD_NUMBER", "100"), ("CI_CONTEXT_PATH", "artifacts/old/run_context.json")])
def test_current_context_cannot_be_retargeted_before_any_child_process(monkeypatch, tmp_path, key, value):
    context = prepare(monkeypatch, tmp_path)
    env = {**environment(context), key: value}
    with pytest.raises((runs.RunContextError, FileNotFoundError)):
        ci.run_step("smoke", workspace=tmp_path, environ=env,
                    executor=lambda *_a, **_k: pytest.fail("a mismatched target must never execute"))


@pytest.mark.parametrize("key,value", [("CI_SITE", "unknown"), ("CI_SUITE", "legacy"),
                                       ("CI_VIEWPORT", "both & echo injected"), ("CI_SITE", "../shirees")])
def test_invalid_targets_never_enter_the_command_plan(key, value):
    env = {"CI_SITE": "shirees", "CI_SUITE": "website_smoke_v1", "CI_VIEWPORT": "both", key: value}
    with pytest.raises(runs.RunContextError, match="CI_TARGET_INVALID"):
        ci.Target.from_environment(env)


@pytest.mark.parametrize("suite", list(runs.SUITE_ROOTS))
@pytest.mark.parametrize("business_failure", [False, True])
def test_dispatcher_publication_keeps_only_current_run_and_acknowledges_after_archive(monkeypatch, tmp_path, suite, business_failure):
    context = prepare(monkeypatch, tmp_path, suite=suite)
    data = result_for(context)
    if business_failure:
        data["viewports"][0]["cases"][0].update(status="FAIL", failure_classification="PURCHASE_AREA_FAILURE",
                                                evidence_capture_error="SCREENSHOT_CAPTURE_FAILED")
        _refresh_summaries(data)
    root = save_current(context, tmp_path, data)
    env = {**environment(context), "CI_PYTHON_EXIT_CODE": "1" if business_failure else "0",
           "CI_JENKINS_RESULT": "FAILURE" if business_failure else "SUCCESS"}
    assert ci.run_step("publish", workspace=tmp_path, environ=env) == 0
    publish_root = tmp_path / context["publish_dir"]
    manifest = json.loads((publish_root / "publication.json").read_text())
    assert manifest["identity"] == context["identity"]
    assert manifest["schema_gate"] == manifest["secret_gate"] == "PASS"
    result = json.loads((publish_root / context["identity"]["run_id"] / "results.json").read_text())
    assert result["summary"]["total"] == (30 if suite == "website_smoke_v1" else 22)
    assert (result["overall_status"] == "FAIL") == business_failure
    history = root.parent / "stability-history.jsonl"
    assert not history.exists()
    assert ci.run_step("acknowledge", workspace=tmp_path, environ=env) == 0
    assert history.exists() == (suite == "website_smoke_v1")
    assert runs.load_context(context["context_path"], workspace=tmp_path, environ=env)["state"] == "PUBLISHED"


def test_early_failure_cannot_publish_an_old_pass(monkeypatch, tmp_path):
    context = prepare(monkeypatch, tmp_path)
    old = tmp_path / "artifacts/website-smoke-v1/mondressy/old-pass"
    old.mkdir(parents=True)
    (old / "results.json").write_text(json.dumps(result_for(context)))
    with pytest.raises(runs.RunContextError, match="CURRENT_RUN_NOT_EXECUTED"):
        ci.run_step("publish", workspace=tmp_path, environ=environment(context))
    assert not (tmp_path / context["publish_dir"]).exists()
    assert not (old.parent / "stability-history.jsonl").exists()


@pytest.mark.parametrize("gate", ["CI_SCHEMA_GATE", "CI_SECRET_GATE"])
def test_failed_gate_does_not_release_publication(monkeypatch, tmp_path, gate):
    context = prepare(monkeypatch, tmp_path)
    save_current(context, tmp_path)
    with pytest.raises(runs.RunContextError, match="GATES_INCOMPLETE"):
        ci.run_step("publish", workspace=tmp_path, environ={**environment(context), gate: "FAIL"})
    assert not (tmp_path / context["publish_dir"]).exists()


def test_variant_diagnostic_is_lavetir_readonly_only():
    args = ci.Target("lavetir", "website_smoke_readonly_v1", "both").commands("variant-diagnostic")
    assert args == [["scripts/diagnose_variant_stability.py", "--site", "lavetir", "--viewport", "mobile",
                     "--entry", "search", "--query", "dress", "--observe-ms", "5000"]]
    with pytest.raises(runs.RunContextError):
        ci.Target("shirees", "website_smoke_v1", "both").commands("variant-diagnostic")


def test_child_execution_keeps_arguments_literal_on_the_current_platform(tmp_path):
    output = tmp_path / "arguments with spaces.json"
    tricky = "both & echo INJECTION; $(whoami)"
    assert ci.execute(["-c", "import json,sys;from pathlib import Path;Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))",
                       str(output), tricky], workspace=tmp_path, environ=dict(os.environ)) == 0
    assert json.loads(output.read_text()) == [tricky]


def test_main_reports_only_a_constant_error_on_infrastructure_failure(monkeypatch, capsys):
    def fail(*_a, **_k):
        raise OSError("PRIVATE_CREDENTIAL_VALUE")
    monkeypatch.setattr(ci, "run_step", fail)
    assert ci.main(["--step", "smoke"]) == 2
    assert capsys.readouterr().out == "CI smoke: FAIL (CURRENT_CI_STEP_FAILED)\n"
