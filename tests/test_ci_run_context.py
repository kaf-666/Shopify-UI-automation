"""Invocation ownership and actual publication behavior; no storefront traffic."""
import json
import os
from copy import deepcopy
from pathlib import Path

import pytest

from scripts import finalize_ci_run as publication, record_stability, validate_result_schema as schema
from scripts import validate_ci_safe_outputs as secrets
from utils import run_context as runs
from utils.safe_artifact_write import write_run_json
from tests.test_validate_result_schema import (
    _full_result, _readonly_result, _viewport, _refresh_summaries,
)


def prepare(monkeypatch, tmp_path, *, site="mondressy", suite="website_smoke_v1", viewport="both"):
    for key, value in {"CI_RUN_ID": "build-102-unique", "GIT_COMMIT_SHA": "b" * 40,
                       "JOB_NAME": "Monitoring/site/smoke", "BUILD_NUMBER": "102",
                       "CI_SCHEMA_GATE": "PASS", "CI_SECRET_GATE": "PASS"}.items():
        monkeypatch.setenv(key, value)
    for module in (publication, record_stability, schema, secrets):
        monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path)
    return runs.prepare_context(suite=suite, site=site, viewport=viewport, workspace=tmp_path)


def result_for(context):
    identity = context["identity"]
    if identity["suite"] == "website_smoke_v1":
        data = _full_result()
        if identity["viewport"] == "both":
            mobile = deepcopy(data["viewports"][0])
            mobile["viewport"] = "mobile"
            mobile["browser"] = {"engine": "webkit"}
            data["viewports"].append(mobile)
    else:
        names = ["desktop", "mobile"] if identity["viewport"] == "both" else [identity["viewport"]]
        data = _readonly_result([_viewport(name) for name in names])
    data.update(run_id=identity["run_id"], site=identity["site"], execution_context=dict(identity))
    _refresh_summaries(data)
    return data


def save_current(context, tmp_path, data=None):
    identity = context["identity"]
    root = runs.claim_context(context, suite=identity["suite"], site=identity["site"],
                              viewport=identity["viewport"], run_id=identity["run_id"], workspace=tmp_path)
    write_run_json(data or result_for(context), root, "results.json")
    return root


def publish(context, code="0"):
    return publication.main(["--run-context", context["context_path"], "--python-exit-code", code,
                             "--jenkins-result", "SUCCESS" if code == "0" else "FAILURE"])


def acknowledge(context):
    return publication.main(["--run-context", context["context_path"], "--acknowledge"])


@pytest.mark.parametrize("site", ["mondressy", "lavetir", "shirees"])
@pytest.mark.parametrize("suite", list(runs.SUITE_ROOTS))
def test_publication_contains_only_this_run_and_history_requires_archive_ack(monkeypatch, tmp_path, site, suite):
    context = prepare(monkeypatch, tmp_path, site=site, suite=suite)
    root = save_current(context, tmp_path)
    old = root.parent / "old-run"
    old.mkdir()
    (old / "results.json").write_text('{"old": true}')
    os.utime(old / "results.json", (2000000000, 2000000000))
    assert publish(context) == 0
    snapshot = tmp_path / context["publish_dir"]
    manifest = json.loads((snapshot / "publication.json").read_text())
    assert manifest["identity"] == context["identity"]
    assert set(manifest["files"]) == {p.relative_to(snapshot).as_posix() for p in snapshot.rglob("*") if p.is_file()}
    assert all("old-run" not in name for name in manifest["files"])
    copied = json.loads((snapshot / context["identity"]["run_id"] / "results.json").read_text())
    assert copied["summary"]["total"] == (30 if suite == "website_smoke_v1" else 22)
    history = root.parent / "stability-history.jsonl"
    assert not history.exists()
    assert acknowledge(context) == 0
    assert acknowledge(context) == 0
    if suite == "website_smoke_v1":
        records = [json.loads(line) for line in history.read_text().splitlines()]
        assert len(records) == 1
        assert records[0]["build_number"] == 102
        assert records[0]["commit_sha"] == "b" * 40
    else:
        assert not history.exists()


def test_early_failure_cannot_reuse_historical_pass_or_overwrite_its_record(monkeypatch, tmp_path, capsys):
    context = prepare(monkeypatch, tmp_path)
    old = tmp_path / "artifacts/website-smoke-v1/mondressy/old-run"
    old.mkdir(parents=True)
    (old / "results.json").write_text(json.dumps(_full_result()))
    record = old / "stability_record.json"
    record.write_bytes(b'{"original_build":101}')
    assert schema.main(["--suite", "website_smoke_v1", "--run-context", context["context_path"],
                        "--results", context["results_path"]]) == 1
    assert record_stability.main(["--run-context", context["context_path"],
                                  "--results", context["results_path"]]) == 1
    assert publish(context) == 1
    assert record.read_bytes() == b'{"original_build":101}'
    assert not (tmp_path / context["publish_dir"]).exists()
    assert "original_build" not in capsys.readouterr().out


@pytest.mark.parametrize("key,value", [
    ("run_id", "other"), ("site", "lavetir"), ("suite", "website_smoke_readonly_v1"),
    ("commit_sha", "a" * 40), ("build_number", 101), ("job_name", "other-job"), ("viewport", "desktop"),
])
def test_result_identity_mismatch_never_publishes(monkeypatch, tmp_path, key, value):
    context = prepare(monkeypatch, tmp_path)
    data = result_for(context)
    data["execution_context"][key] = value
    save_current(context, tmp_path, data)
    assert publish(context) == 1
    assert not (tmp_path / context["publish_dir"]).exists()


def test_old_build_context_is_rejected_even_if_its_result_is_valid(monkeypatch, tmp_path):
    context = prepare(monkeypatch, tmp_path)
    save_current(context, tmp_path)
    monkeypatch.setenv("BUILD_NUMBER", "103")
    assert publish(context) == 1
    with pytest.raises(runs.RunContextError, match="IDENTITY_MISMATCH"):
        runs.load_context(context["context_path"], workspace=tmp_path)


@pytest.mark.parametrize("parameter,value", [("--results", "artifacts/old/results.json"),
                                             ("--commit-sha", "a" * 40), ("--viewport", "desktop")])
def test_strict_stability_rejects_path_commit_or_viewport_override(monkeypatch, tmp_path, parameter, value):
    context = prepare(monkeypatch, tmp_path)
    save_current(context, tmp_path)
    args = ["--run-context", context["context_path"]]
    if parameter != "--results":
        args += ["--results", context["results_path"]]
    assert record_stability.main([*args, parameter, value]) == 1


@pytest.mark.parametrize("suffix", ["jsonl", "ndjson", "log", "unknown"])
def test_final_scan_blocks_all_candidate_suffixes_and_cannot_be_acknowledged(monkeypatch, tmp_path, capsys, suffix):
    context = prepare(monkeypatch, tmp_path)
    root = save_current(context, tmp_path)
    value = "DUMMY_AUDIT_CREDENTIAL"
    monkeypatch.setenv("MONDRESSY_US_SHOPIFY_SIGNATURE", value)
    (root / f"traffic.{suffix}").write_text('{"value":"' + value + '"}')
    assert publish(context) == 1
    assert acknowledge(context) == 1
    assert not (root.parent / "stability-history.jsonl").exists()
    assert value not in capsys.readouterr().out


@pytest.mark.parametrize("gate,value", [("CI_SECRET_GATE", "FAIL"), ("CI_SECRET_GATE", "NOT_RUN"),
                                      ("CI_SCHEMA_GATE", "FAIL"), ("CI_SCHEMA_GATE", "NOT_RUN")])
def test_failed_or_missing_gates_never_publish(monkeypatch, tmp_path, gate, value):
    context = prepare(monkeypatch, tmp_path)
    save_current(context, tmp_path)
    monkeypatch.setenv(gate, value)
    assert publish(context) == 1
    assert not (tmp_path / context["publish_dir"]).exists()


def test_functional_failure_keeps_current_evidence_and_enters_failure_history(monkeypatch, tmp_path):
    context = prepare(monkeypatch, tmp_path)
    data = result_for(context)
    case = data["viewports"][0]["cases"][0]
    case.update(status="FAIL", failure_classification="PURCHASE_AREA_FAILURE",
                evidence_capture_error="SCREENSHOT_CAPTURE_FAILED")
    _refresh_summaries(data)
    root = save_current(context, tmp_path, data)
    assert publish(context, "1") == 0
    assert acknowledge(context) == 0
    record = json.loads((root.parent / "stability-history.jsonl").read_text())
    assert record["eligible"] is True
    assert record["overall_status"] == "FAIL" and record["jenkins_result"] == "FAILURE"


def test_unreadable_file_fails_scan_even_when_no_credentials_are_present(monkeypatch, tmp_path):
    path = tmp_path / "requests.jsonl"
    path.write_text("{}"); original = Path.open
    def denied(self, *args, **kwargs):
        if self == path:
            raise PermissionError()
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, "open", denied)
    hits, errors, count = secrets.scan_files([path], environ={})
    assert not hits and errors == [path] and count == 1


def test_stream_scan_matches_secret_split_across_chunks(tmp_path):
    path = tmp_path / "requests.jsonl"
    value = "SYNTHETIC_LONG_SIGNATURE"
    path.write_bytes(b"x" * (64 * 1024 - 5) + value.encode())
    hits, errors, count = secrets.scan_files([path], {"SHOPIFY_PROXY_PASSWORD": value})
    assert hits == [("SHOPIFY_PROXY_PASSWORD", path)] and not errors and count == 1


@pytest.mark.parametrize("path", ["../outside", "artifacts/../../outside", "C:/outside", "a\\b", "a//b"])
def test_context_paths_do_not_escape_workspace(tmp_path, path):
    with pytest.raises(runs.RunContextError):
        runs.confined_path(path, tmp_path)


def test_duplicate_context_or_run_directory_cannot_be_reused(monkeypatch, tmp_path):
    context = prepare(monkeypatch, tmp_path)
    save_current(context, tmp_path)
    before = (tmp_path / context["results_path"]).read_bytes()
    with pytest.raises(FileExistsError):
        runs.prepare_context(suite="website_smoke_v1", site="mondressy", viewport="both", workspace=tmp_path)
    with pytest.raises(runs.RunContextError, match="ALREADY_CLAIMED"):
        runs.claim_context(context, **{k: context["identity"][k] for k in ("suite", "site", "viewport", "run_id")}, workspace=tmp_path)
    assert (tmp_path / context["results_path"]).read_bytes() == before


@pytest.mark.parametrize("suite", list(runs.SUITE_ROOTS))
def test_real_cli_embeds_claimed_identity_and_rejects_duplicate_invocation(monkeypatch, tmp_path, suite):
    from scripts import run_website_smoke_v1 as full, run_website_smoke_readonly_v1 as readonly
    context = prepare(monkeypatch, tmp_path, suite=suite)
    cli = full if suite == "website_smoke_v1" else readonly
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(cli, "ARTIFACT_ROOT", tmp_path / runs.SUITE_ROOTS[suite])
    if cli is full:
        from tests.test_full_cart_cleanup import install_journeys, make_runtime, FakeAPI
        install_journeys(monkeypatch)
        monkeypatch.setattr(cli, "create_browser", lambda *_args, **_kwargs: make_runtime(FakeAPI()))
        monkeypatch.setattr(cli, "close_browser", lambda _runtime: None)
    else:
        from tests.test_run_website_smoke_readonly_v1 import _pass_results, _FakeRunner
        monkeypatch.setattr(cli, "run_viewport", lambda *_args, **_kwargs: (_pass_results(), _FakeRunner(), {}))
    args = ["--site", "mondressy", "--viewport", "both", "--run-context", context["context_path"]]
    assert cli.main(args) == 0
    result_path = tmp_path / context["results_path"]
    before = result_path.read_bytes()
    assert json.loads(before)["execution_context"] == context["identity"]
    assert cli.main(args) == 2
    assert result_path.read_bytes() == before
    assert publish(context) == 0


def test_copy_failure_or_unreadable_publication_cannot_update_history(monkeypatch, tmp_path):
    context = prepare(monkeypatch, tmp_path)
    root = save_current(context, tmp_path)
    def fail(*_args, **_kwargs):
        raise OSError("SIMULATED_COPY_FAILURE")
    monkeypatch.setattr(publication.shutil, "copyfile", fail)
    assert publish(context) == 1
    assert acknowledge(context) == 1
    assert not (root.parent / "stability-history.jsonl").exists()


def test_snapshot_change_is_rejected_before_scan_or_ack(monkeypatch, tmp_path):
    context = prepare(monkeypatch, tmp_path)
    root = save_current(context, tmp_path)
    copyfile = publication.shutil.copyfile
    def changed(source, target):
        copyfile(source, target)
        if source.name == "results.json":
            data = json.loads(Path(target).read_text())
            data["execution_context"]["commit_sha"] = "a" * 40
            Path(target).write_text(json.dumps(data))
    monkeypatch.setattr(publication.shutil, "copyfile", changed)
    assert publish(context) == 1
    assert acknowledge(context) == 1
    assert not (root.parent / "stability-history.jsonl").exists()


def test_strict_consumers_accept_current_result_and_ignore_ambient_viewport(monkeypatch, tmp_path):
    context = prepare(monkeypatch, tmp_path)
    root = save_current(context, tmp_path)
    monkeypatch.setenv("SMOKE_VIEWPORT", "mobile")
    assert schema.main(["--suite", "website_smoke_v1", "--run-context", context["context_path"],
                        "--results", context["results_path"]]) == 0
    assert record_stability.main(["--run-context", context["context_path"],
                                  "--results", context["results_path"]]) == 0
    record = json.loads((root / "stability_record.json").read_text())
    assert record["viewport"] == "both" and record["execution_context"] == context["identity"]
    assert record["source_results"] == context["results_path"]
    assert not (root.parent / "stability-history.jsonl").exists()


def test_linked_output_directory_is_rejected_before_traversal(monkeypatch, tmp_path):
    import subprocess
    context = prepare(monkeypatch, tmp_path)
    root = save_current(context, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "private.json"
    sentinel.write_text("private")
    link = root / "linked"
    if os.name == "nt":
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
        assert result.returncode == 0
    else:
        link.symlink_to(outside, target_is_directory=True)
    assert publish(context) == 1
    assert not (tmp_path / context["publish_dir"]).exists()
    assert sentinel.read_text() == "private"
