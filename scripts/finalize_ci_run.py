"""Validate, snapshot and scan exactly one invocation before Jenkins archives it.

History is acknowledged only after archiveArtifacts succeeds. On any error this
command returns nonzero; Jenkins publishes its own constant failure JSON only.
"""
import argparse
import json
import os
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.validate_ci_safe_outputs import scan_files
from scripts.validate_result_schema import validate_json
from utils.run_context import (load_context, validate_context_results, confined_path,
                               ordinary_files, save_context, RunContextError)
from utils.safe_artifact_write import write_run_json
from utils.stability import build_stability_record, write_history_record, HISTORY_FILENAME


def stage_publication(context, *, python_exit_code, jenkins_result, workspace=None, environ=None):
    workspace = Path(workspace or PROJECT_ROOT)
    env = dict(os.environ if environ is None else environ)
    if context.get("state") != "RUNNING":
        raise RunContextError("CURRENT_RUN_NOT_EXECUTED")
    if env.get("CI_SCHEMA_GATE") != "PASS" or env.get("CI_SECRET_GATE") != "PASS":
        raise RunContextError("CURRENT_RUN_GATES_INCOMPLETE")
    result = validate_context_results(context, context["results_path"], workspace=workspace)
    identity = context["identity"]
    if (python_exit_code not in {"0", "1", "2"}
            or (result["overall_status"] == "PASS") != (python_exit_code == "0")):
        raise RunContextError("CURRENT_RUN_EXIT_MISMATCH")
    root = confined_path(context["run_dir"], workspace, must_exist=True, directory=True)
    # Reject all linked evidence before schema checks can read any sidecar.
    list(ordinary_files(root))
    if not validate_json(root, identity["suite"]):
        raise RunContextError("CURRENT_RESULT_SCHEMA_FAILED")

    if identity["suite"] == "website_smoke_v1" and identity["viewport"] == "both":
        record_env = {**env, "GIT_COMMIT_SHA": identity["commit_sha"],
                      "BUILD_NUMBER": str(identity["build_number"]),
                      "STABILITY_PYTHON_EXIT_CODE": python_exit_code,
                      "STABILITY_JENKINS_RESULT": jenkins_result,
                      "STABILITY_TRIGGER": env.get("CI_TRIGGER", "UNKNOWN"),
                      "STABILITY_SCHEMA_GATE": "PASS", "STABILITY_SECRET_GATE": "PASS",
                      "STABILITY_ARTIFACT_GATE": "PASS"}
        record = build_stability_record(result, root / "results.json",
                                        requested_viewport="both", environ=record_env)
        record["execution_context"] = dict(identity)
        record["source_results"] = context["results_path"]
        write_run_json(record, root, "stability_record.json")

    publication_root = confined_path(context["publish_dir"], workspace)
    publication_root.mkdir(exist_ok=False)
    snapshot_root = publication_root / identity["run_id"]
    snapshot_root.mkdir()
    for source in ordinary_files(root):
        relative = source.relative_to(root)
        target = snapshot_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)

    staged_results = json.loads((snapshot_root / "results.json").read_text(encoding="utf-8"))
    if staged_results != result or not validate_json(snapshot_root, identity["suite"]):
        raise RunContextError("RUN_CHANGED_DURING_SNAPSHOT")

    context.update(state="FINALIZED", python_exit_code=int(python_exit_code))
    save_context(context, workspace=workspace)
    write_run_json(context, publication_root, "run_context.json")
    files = sorted(p.relative_to(publication_root).as_posix() for p in ordinary_files(publication_root))
    publication = {"version": 1, "status": "READY", "identity": dict(identity),
                   "schema_gate": "PASS", "secret_gate": "PASS",
                   "files": sorted([*files, "publication.json"])}
    write_run_json(publication, publication_root, "publication.json")
    # Last write to the publication snapshot is above. No suffix is exempt.
    hits, errors, count = scan_files(list(ordinary_files(publication_root)), env)
    if hits or errors:
        raise RunContextError("PUBLICATION_SECRET_SCAN_FAILED")
    context["state"] = "SCANNED"
    save_context(context, workspace=workspace)
    print(f"CI publication: READY ({count} scanned files)")
    return publication


def acknowledge_publication(context, *, workspace=None):
    workspace = Path(workspace or PROJECT_ROOT)
    if context.get("state") not in {"SCANNED", "PUBLISHED"}:
        raise RunContextError("PUBLICATION_NOT_FINALIZED")
    publication_root = confined_path(context["publish_dir"], workspace, must_exist=True, directory=True)
    list(ordinary_files(publication_root))
    publication = json.loads((publication_root / "publication.json").read_text(encoding="utf-8"))
    if publication.get("status") != "READY" or publication.get("identity") != context["identity"]:
        raise RunContextError("PUBLICATION_IDENTITY_MISMATCH")
    identity = context["identity"]
    if identity["suite"] == "website_smoke_v1" and identity["viewport"] == "both":
        record = json.loads((publication_root / identity["run_id"] / "stability_record.json").read_text(encoding="utf-8"))
        if record.get("execution_context") != identity:
            raise RunContextError("STABILITY_IDENTITY_MISMATCH")
        history = confined_path(f"{Path(context['run_dir']).parent.as_posix()}/{HISTORY_FILENAME}", workspace)
        write_history_record(history, record)
    context["state"] = "PUBLISHED"
    save_context(context, workspace=workspace)
    print("CI publication: ACKNOWLEDGED")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-context", required=True)
    parser.add_argument("--python-exit-code", default="UNKNOWN")
    parser.add_argument("--jenkins-result", choices=("SUCCESS", "FAILURE", "UNSTABLE", "ABORTED", "NOT_BUILT"), default="FAILURE")
    parser.add_argument("--acknowledge", action="store_true")
    args = parser.parse_args(argv)
    try:
        context = load_context(args.run_context, workspace=PROJECT_ROOT)
        if args.acknowledge:
            acknowledge_publication(context, workspace=PROJECT_ROOT)
        else:
            stage_publication(context, python_exit_code=args.python_exit_code,
                              jenkins_result=args.jenkins_result, workspace=PROJECT_ROOT)
    except Exception:
        # Never print exception values, credential matches, or partial paths.
        print("CI publication: FAIL (CURRENT_RUN_PUBLICATION_FAILED)")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
