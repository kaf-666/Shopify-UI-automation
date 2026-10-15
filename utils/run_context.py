"""Explicit CI invocation identity and confined artifact ownership.

Historical/manual tools may still read old results. CI consumers must use this
context and its exact result path; mtimes are never an invocation identity.
"""
import json
import os
import re
import stat
from pathlib import Path, PureWindowsPath

from utils.artifacts import canonical_site_name
from utils.errors import CliConfigError
from utils.result import iso_now
from utils.safe_artifact_write import write_run_json

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SUITE_ROOTS = {
    "website_smoke_v1": "artifacts/website-smoke-v1",
    "website_smoke_readonly_v1": "artifacts/website-smoke-readonly-v1",
}
IDENTITY_KEYS = ("suite", "site", "run_id", "commit_sha", "job_name", "build_number", "viewport")


class RunContextError(CliConfigError):
    def __init__(self, code="RUN_CONTEXT_INVALID"):
        super().__init__(code, category=code)


def ordinary(path, *, directory=False):
    info = Path(path).lstat()
    if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
        raise RunContextError("RUN_ARTIFACT_PATH_REJECTED")


def confined_path(relative, workspace=None, *, must_exist=False, directory=False):
    root = Path(workspace or PROJECT_ROOT).absolute()
    text = str(relative)
    rel = Path(text)
    win = PureWindowsPath(text)
    if (not text or rel.is_absolute() or win.drive or "\\" in text
            or ".." in rel.parts or rel.as_posix() != text):
        raise RunContextError("RUN_ARTIFACT_PATH_REJECTED")
    target = root / rel
    # Validate before resolving: a link must never become an ordinary target.
    for ancestor in reversed((root, *root.parents)):
        ordinary(ancestor, directory=True)
    for part in rel.parts[:-1]:
        root = root / part
        if os.path.lexists(root):
            ordinary(root, directory=True)
    if os.path.lexists(target):
        ordinary(target, directory=directory)
    elif must_exist:
        raise RunContextError("CURRENT_RUN_ARTIFACT_MISSING")
    return target


def identity_valid(identity):
    try:
        return (
            isinstance(identity, dict) and set(identity) == set(IDENTITY_KEYS)
            and identity["suite"] in SUITE_ROOTS
            and canonical_site_name(identity["site"]) == identity["site"]
            and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", identity["run_id"]))
            and identity["run_id"] not in {".", ".."}
            and bool(re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", identity["commit_sha"]))
            and isinstance(identity["job_name"], str) and 0 < len(identity["job_name"]) <= 512
            and not any(c in identity["job_name"] for c in "\r\n\x00")
            and type(identity["build_number"]) is int and identity["build_number"] > 0
            and identity["viewport"] in {"desktop", "mobile", "both"}
        )
    except (KeyError, TypeError, ValueError, CliConfigError):
        return False


def expected_paths(identity):
    run = f"{SUITE_ROOTS[identity['suite']]}/{identity['site']}/{identity['run_id']}"
    control = f"artifacts/ci/{identity['run_id']}"
    return {"run_dir": run, "results_path": f"{run}/results.json",
            "context_path": f"{control}/run_context.json", "publish_dir": f"{control}/publish"}


def assert_current_build(context, environ=None):
    env = os.environ if environ is None else environ
    identity = context["identity"]
    for key, name in (("commit_sha", "GIT_COMMIT_SHA"), ("job_name", "JOB_NAME"),
                      ("build_number", "BUILD_NUMBER"), ("run_id", "CI_RUN_ID")):
        if str(identity[key]) != str(env.get(name, "")):
            raise RunContextError("RUN_BUILD_IDENTITY_MISMATCH")


def prepare_context(*, suite, site, viewport, workspace=None, environ=None):
    env = os.environ if environ is None else environ
    try:
        identity = {"suite": suite, "site": canonical_site_name(site), "viewport": viewport,
                    "run_id": env.get("CI_RUN_ID", ""), "commit_sha": env.get("GIT_COMMIT_SHA", ""),
                    "job_name": env.get("JOB_NAME", ""), "build_number": int(env.get("BUILD_NUMBER", ""))}
    except (ValueError, CliConfigError):
        raise RunContextError() from None
    if not identity_valid(identity):
        raise RunContextError()
    paths = expected_paths(identity)
    context = {"version": 1, "identity": identity, **paths,
               "state": "PREPARED", "created_at": iso_now()}
    path = confined_path(paths["context_path"], workspace)
    # Reserve a new control directory, even if an earlier invocation failed.
    path.parent.mkdir(parents=True, exist_ok=False)
    write_run_json(context, path.parent, "run_context.json")
    return context


def load_context(path, *, workspace=None, environ=None):
    source = confined_path(path, workspace, must_exist=True)
    try:
        context = json.loads(source.read_text(encoding="utf-8"))
        if (not isinstance(context, dict) or context.get("version") != 1
                or not identity_valid(context.get("identity"))):
            raise RunContextError()
        for key, value in expected_paths(context["identity"]).items():
            if context.get(key) != value:
                raise RunContextError("RUN_ARTIFACT_PATH_REJECTED")
        if context["context_path"] != str(path):
            raise RunContextError("RUN_CONTEXT_PATH_MISMATCH")
    except (OSError, ValueError, TypeError):
        raise RunContextError() from None
    assert_current_build(context, environ)
    return context


def claim_context(context, *, suite, site, viewport, run_id, workspace=None):
    identity = context["identity"]
    if (identity["suite"], identity["site"], identity["viewport"], identity["run_id"]) != (
            suite, site, viewport, run_id):
        raise RunContextError("RUN_INVOCATION_MISMATCH")
    if context.get("state") != "PREPARED":
        raise RunContextError("RUN_ALREADY_CLAIMED")
    root = confined_path(context["run_dir"], workspace)
    # A run directory is never reused, even if it contains only a partial run.
    root.mkdir(parents=True, exist_ok=False)
    context["state"] = "RUNNING"
    save_context(context, workspace=workspace)
    return root


def save_context(context, *, workspace=None):
    path = confined_path(context["context_path"], workspace, must_exist=True)
    write_run_json(context, path.parent, "run_context.json")


def validate_context_results(context, result_path, *, suite=None, workspace=None):
    if str(result_path) != context["results_path"]:
        raise RunContextError("RUN_RESULT_PATH_MISMATCH")
    if suite is not None and context["identity"]["suite"] != suite:
        raise RunContextError("RUN_SUITE_MISMATCH")
    source = confined_path(context["results_path"], workspace, must_exist=True)
    try:
        results = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RunContextError("CURRENT_RESULTS_INVALID") from None
    identity = context["identity"]
    if (not isinstance(results, dict) or results.get("execution_context") != identity
            or results.get("site") != identity["site"] or results.get("run_id") != identity["run_id"]):
        raise RunContextError("RUN_RESULT_IDENTITY_MISMATCH")
    names = [v.get("viewport") for v in results.get("viewports", []) if isinstance(v, dict)]
    requested = ["desktop", "mobile"] if identity["viewport"] == "both" else [identity["viewport"]]
    if len(names) != len(set(names)) or any(name not in requested for name in names):
        raise RunContextError("RUN_VIEWPORT_MISMATCH")
    if results.get("fatal_error") is None and set(names) != set(requested):
        raise RunContextError("RUN_VIEWPORT_MISMATCH")
    return results


def ordinary_files(root):
    """Enumerate ordinary files, rejecting links before descending directories."""
    ordinary(root, directory=True)
    for path in sorted(Path(root).iterdir()):
        is_directory = stat.S_ISDIR(path.lstat().st_mode)
        ordinary(path, directory=is_directory)
        if is_directory:
            yield from ordinary_files(path)
        else:
            yield path
