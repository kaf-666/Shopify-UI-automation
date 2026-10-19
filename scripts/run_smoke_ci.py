"""Shared production smoke protocol; Jenkins owns credentials/stages/archiving.

All subprocesses use argv lists on Unix and Windows. Identity comes from the
fixed Jenkins entry and the current run context, never a latest-result search.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.finalize_ci_run import acknowledge_publication, stage_publication
from utils.run_context import (RunContextError, SUITE_ROOTS,
                               load_context, prepare_context)

SITES = ("mondressy", "lavetir", "shirees")
STEPS = ("prepare", "static", "runtime", "access", "variant-diagnostic",
         "smoke", "secret", "schema", "publish", "acknowledge")


@dataclass(frozen=True)
class Target:
    site: str
    suite: str
    viewport: str

    @classmethod
    def from_environment(cls, env):
        target = cls(env.get("CI_SITE", ""), env.get("CI_SUITE", ""),
                     env.get("CI_VIEWPORT", ""))
        if (target.site not in SITES or target.suite not in SUITE_ROOTS
                or target.viewport not in {"desktop", "mobile", "both"}):
            raise RunContextError("CI_TARGET_INVALID")
        return target

    @property
    def credential_names(self):
        prefix = self.site.upper() + "_US_SHOPIFY_"
        return {header: prefix + suffix for header, suffix in (
            ("Signature", "SIGNATURE"), ("Signature-Input", "SIGNATURE_INPUT"),
            ("Signature-Agent", "SIGNATURE_AGENT"))}

    def commands(self, step, context=None):
        """Return platform-independent argv; callers cannot supply shell text."""
        selection = ["--site", self.site, "--suite", self.suite]
        if step == "static":
            return [["-m", "compileall", "-q", "scripts", "utils", "pages", "flows", "tests", "run.py"],
                    ["scripts/validate_site_config.py", *selection]]
        if step == "runtime":
            return [["scripts/validate_runtime_contract.py"]]
        if step == "access":
            # Preserve the existing two-viewport signed-request self-check.
            return [["scripts/validate_site_access.py", "--site", self.site, "--viewport", "both"]]
        if step == "variant-diagnostic":
            if (self.site, self.suite) != ("lavetir", "website_smoke_readonly_v1"):
                raise RunContextError("CI_DIAGNOSTIC_TARGET_INVALID")
            return [["scripts/diagnose_variant_stability.py", "--site", "lavetir", "--viewport", "mobile",
                     "--entry", "search", "--query", "dress", "--observe-ms", "5000"]]
        if context is None:
            raise RunContextError("CI_CONTEXT_REQUIRED")
        if step == "smoke":
            script = ("scripts/run_website_smoke_v1.py" if self.suite == "website_smoke_v1"
                      else "scripts/run_website_smoke_readonly_v1.py")
            args = [script, "--site", self.site, "--viewport", self.viewport,
                    "--run-id", context["identity"]["run_id"], "--run-context", context["context_path"]]
            if self.suite == "website_smoke_v1" and self.site in {"lavetir", "shirees"}:
                args.append("--traffic-inventory")
            return [args]
        if step == "secret":
            return [["scripts/validate_ci_safe_outputs.py", "--run-context", context["context_path"]]]
        if step == "schema":
            return [["scripts/validate_result_schema.py", "--suite", self.suite,
                     "--run-context", context["context_path"], "--results", context["results_path"]]]
        raise RunContextError("CI_STEP_INVALID")


def current_context(target, workspace, env):
    context = load_context(env.get("CI_CONTEXT_PATH", ""), workspace=workspace, environ=env)
    identity = context["identity"]
    if (identity["site"], identity["suite"], identity["viewport"]) != (
            target.site, target.suite, target.viewport):
        raise RunContextError("CI_TARGET_CONTEXT_MISMATCH")
    return context


def execute(args, *, workspace, environ):
    return subprocess.run([sys.executable, *args], cwd=workspace, env=environ,
                          shell=False, check=False).returncode


def run_step(step, *, workspace=None, environ=None, executor=None):
    workspace = Path(workspace or PROJECT_ROOT)
    env = dict(os.environ if environ is None else environ)
    target = Target.from_environment(env)
    env.update(PYTHONUTF8="1", PYTHONUNBUFFERED="1")
    if step == "prepare":
        # Validate the profile and its credential variable mapping before any
        # access or smoke command. Values are never read or printed here.
        from utils.site_config_validator import validate_site_config
        profile = validate_site_config(target.site, suite=target.suite)
        if profile["access"]["env"] != target.credential_names:
            raise RunContextError("CI_CREDENTIAL_MAPPING_MISMATCH")
        if env.get("CI_CONTEXT_PATH") != f"artifacts/ci/{env.get('CI_RUN_ID', '')}/run_context.json":
            raise RunContextError("CI_CONTEXT_PATH_MISMATCH")
        context = prepare_context(site=target.site, suite=target.suite, viewport=target.viewport,
                                  workspace=workspace, environ=env)
        print(f"CI prepare: {context['context_path']}", flush=True)
        return 0
    context = current_context(target, workspace, env)
    if step == "publish":
        stage_publication(context, python_exit_code=env.get("CI_PYTHON_EXIT_CODE", "UNKNOWN"),
                          jenkins_result=env.get("CI_JENKINS_RESULT", "FAILURE"),
                          workspace=workspace, environ=env)
        return 0
    if step == "acknowledge":
        acknowledge_publication(context, workspace=workspace)
        return 0
    if step == "smoke" and context["state"] != "PREPARED":
        raise RunContextError("RUN_ALREADY_CLAIMED")
    executor = executor or execute
    for args in target.commands(step, context):
        code = executor(args, workspace=workspace, environ=env)
        if code:
            return code
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--step", required=True, choices=STEPS)
    args = parser.parse_args(argv)
    try:
        return run_step(args.step)
    except Exception:
        # No exception values: dependencies/configuration/paths may be private.
        print(f"CI {args.step}: FAIL (CURRENT_CI_STEP_FAILED)", flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
