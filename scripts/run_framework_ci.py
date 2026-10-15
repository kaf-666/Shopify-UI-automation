"""Run offline framework checks with isolated environment and publishable reports.

Dependency/browser installation happens before this command. Tests use mocks,
synthetic HTML and loopback servers; no storefront runner is invoked.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.validate_ci_safe_outputs import scan_files
from utils.errors import sanitize_message
from utils.run_context import confined_path, ordinary, ordinary_files

# Only OS/browser runtime settings cross into the test processes. Jenkins/GitHub
# credentials, Shopify signatures, proxies and user pytest options do not.
RUNTIME_ENV = {
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "HOME", "USERPROFILE",
    "TMP", "TEMP", "TMPDIR", "LOCALAPPDATA", "APPDATA", "PROGRAMFILES",
    "PROGRAMFILES(X86)", "PROGRAMDATA", "LD_LIBRARY_PATH", "DISPLAY",
    "XDG_RUNTIME_DIR", "PLAYWRIGHT_BROWSERS_PATH", "LANG", "LC_ALL", "TZ",
    "JENKINS_NODE_COOKIE", "HUDSON_COOKIE", "JENKINS_SERVER_COOKIE", "BUILD_ID",
}


def test_environment(environ):
    result = {key: value for key, value in environ.items() if key.upper() in RUNTIME_ENV}
    result.update(PYTHONUTF8="1", PYTHONUNBUFFERED="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    return result


def check_command(args, workspace, timeout):
    process = subprocess.Popen([sys.executable, *args], cwd=workspace,
                               env=test_environment(os.environ), stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, encoding="utf-8", errors="replace",
                               start_new_session=os.name != "nt")
    try:
        output, _ = process.communicate(timeout=timeout)
        return process.returncode, output
    except subprocess.TimeoutExpired:
        # Stop browser/driver descendants as well as the pytest parent.
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
        else:
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        return 124, "CHECK_TIMEOUT\n"


def sanitize_junit(path):
    ordinary(path)
    tree = ET.parse(path)
    root = tree.getroot()
    if root.tag not in {"testsuites", "testsuite"} or not list(root.iter("testcase")):
        raise ValueError("JUNIT_TESTCASES_MISSING")
    for element in root.iter():
        if element.text:
            element.text = sanitize_message(element.text)
        if element.tail:
            element.tail = sanitize_message(element.tail)
        for key, value in element.attrib.items():
            element.set(key, sanitize_message(value))
    tree.write(path, encoding="utf-8", xml_declaration=True)


def run_checks(report_dir, *, workspace=None, timeout=900):
    workspace = Path(workspace or PROJECT_ROOT)
    if not str(report_dir).startswith("artifacts/framework-ci/"):
        raise ValueError("FRAMEWORK_REPORT_PATH_INVALID")
    root = confined_path(report_dir, workspace, directory=True)
    root.mkdir(parents=True, exist_ok=False)
    junit = root / "pytest.xml"
    commands = [
        ("compile", ["-m", "compileall", "-q", "scripts", "utils", "pages", "flows", "tests", "run.py"]),
        ("pytest", ["-m", "pytest", "-q", "-rs", "--tb=short", "--junitxml", str(junit)]),
        ("runtime-contract", ["scripts/validate_runtime_contract.py"]),
        ("stability", ["scripts/validate_stability.py"]),
    ]
    steps = []
    for name, args in commands:
        print(f"Framework CI: START {name}", flush=True)
        started = time.monotonic()
        try:
            code, output = check_command(args, workspace, timeout)
        except OSError:
            code, output = 125, "CHECK_START_FAILED\n"
        if name == "pytest":
            try:
                sanitize_junit(junit)
            except (OSError, ValueError, ET.ParseError):
                code = code or 1
                output += "\nJUNIT_REPORT_INVALID\n"
        safe_output = sanitize_message(output)
        (root / f"{name}.log").write_text(safe_output, encoding="utf-8")
        steps.append({"check": name, "exit_code": code,
                      "status": "PASS" if code == 0 else "FAIL",
                      "duration_seconds": round(time.monotonic() - started, 2)})
        print(f"Framework CI: {steps[-1]['status']} {name} (exit={code})", flush=True)
        print("\n".join(safe_output.splitlines()[-8:]), flush=True)
    summary = {"version": 1, "suite": "framework_ci", "offline": True,
               "commit_sha": os.environ.get("GIT_COMMIT_SHA", ""),
               "job_name": os.environ.get("JOB_NAME", "local"),
               "build_number": os.environ.get("BUILD_NUMBER", "local"),
               "status": "PASS" if all(step["exit_code"] == 0 for step in steps) else "FAIL",
               "checks": steps}
    (root / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    hits, errors, count = scan_files(list(ordinary_files(root)))
    if hits or errors:
        print("Framework CI: OUTPUT_SCAN_FAILED", flush=True)
        return 2
    print(f"Framework CI: OUTPUT_SCAN_PASS ({count} files)", flush=True)
    return 0 if summary["status"] == "PASS" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-dir", required=True)
    args = parser.parse_args(argv)
    try:
        return run_checks(args.report_dir)
    except Exception:
        print("Framework CI: REPORT_PUBLICATION_FAILED")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
