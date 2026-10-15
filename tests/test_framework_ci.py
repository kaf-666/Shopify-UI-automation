"""Behavioral checks for CI result publication without running nested pytest."""
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from scripts import run_framework_ci as ci
from utils.run_context import RunContextError


def fake_checks(monkeypatch, *, pytest_exit=0, junit=True):
    calls = []
    def command(args, workspace, timeout):
        calls.append(args)
        if "pytest" in args:
            if junit:
                Path(args[-1]).write_text('<testsuites><testsuite tests="1"><testcase name="case"/></testsuite></testsuites>')
            return pytest_exit, "1 test\n"
        return 0, "PASS\n"
    monkeypatch.setattr(ci, "check_command", command)
    return calls


def test_environment_drops_credentials_proxy_and_pytest_injection():
    env = ci.test_environment({"PATH": "runtime", "HOME": "home", "SystemRoot": "windows",
                               "JENKINS_NODE_COOKIE": "process-tree-cookie",
                               "SHIREES_US_SHOPIFY_SIGNATURE": "private", "GH_TOKEN": "private",
                               "HTTPS_PROXY": "private", "PYTEST_ADDOPTS": "--ignore=tests"})
    assert env["PATH"] == "runtime" and env["SystemRoot"] == "windows"
    assert env["JENKINS_NODE_COOKIE"] == "process-tree-cookie"
    assert not any("private" in value for value in env.values())
    assert "PYTEST_ADDOPTS" not in env
    assert env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"


@pytest.mark.parametrize("code", [0, 1, 5])
def test_failure_is_preserved_junit_remains_and_other_checks_run(monkeypatch, tmp_path, code):
    calls = fake_checks(monkeypatch, pytest_exit=code)
    monkeypatch.setenv("GIT_COMMIT_SHA", "a" * 40)
    assert ci.run_checks("artifacts/framework-ci/current", workspace=tmp_path) == int(code != 0)
    root = tmp_path / "artifacts/framework-ci/current"
    summary = json.loads((root / "summary.json").read_text())
    assert summary["status"] == ("PASS" if code == 0 else "FAIL")
    assert summary["commit_sha"] == "a" * 40
    assert len(calls) == 4 and calls[-1] == ["scripts/validate_stability.py"]
    assert summary["checks"][1]["exit_code"] == code
    assert ET.parse(root / "pytest.xml").find(".//testcase").get("name") == "case"


def test_missing_junit_cannot_turn_green_and_old_reports_cannot_be_reused(monkeypatch, tmp_path):
    fake_checks(monkeypatch, junit=False)
    assert ci.run_checks("artifacts/framework-ci/current", workspace=tmp_path) == 1
    assert "JUNIT_REPORT_INVALID" in (tmp_path / "artifacts/framework-ci/current/pytest.log").read_text()
    with pytest.raises(FileExistsError):
        ci.run_checks("artifacts/framework-ci/current", workspace=tmp_path)


def test_unscanned_reports_cannot_be_published(monkeypatch, tmp_path):
    fake_checks(monkeypatch)
    monkeypatch.setattr(ci, "scan_files", lambda paths: ([("SECRET", paths[0])], [], len(paths)))
    assert ci.run_checks("artifacts/framework-ci/current", workspace=tmp_path) == 2


def test_log_and_junit_redaction_preserve_xml(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SHIREES_US_SHOPIFY_SIGNATURE", "PRIVATE_CI_TEST_VALUE")
    def command(args, workspace, timeout):
        if "pytest" in args:
            Path(args[-1]).write_text('<testsuites><testsuite><testcase name="case"><failure>PRIVATE_CI_TEST_VALUE &amp; detail</failure></testcase></testsuite></testsuites>')
        return 1, "PRIVATE_CI_TEST_VALUE"
    monkeypatch.setattr(ci, "check_command", command)
    assert ci.run_checks("artifacts/framework-ci/current", workspace=tmp_path) == 1
    root = tmp_path / "artifacts/framework-ci/current"
    assert "PRIVATE_CI_TEST_VALUE" not in "".join(p.read_text() for p in root.iterdir())
    assert "PRIVATE_CI_TEST_VALUE" not in capsys.readouterr().out
    assert ET.parse(root / "pytest.xml").find(".//failure").text == "[REDACTED] & detail"


@pytest.mark.parametrize("path", ["artifacts/framework-ci/../../old", "artifacts/old", "../outside"])
def test_report_path_is_confined(tmp_path, path):
    with pytest.raises((ValueError, RunContextError)):
        ci.run_checks(path, workspace=tmp_path)
