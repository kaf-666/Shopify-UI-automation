"""V2 regressions use the real Full CLI, runner, writer and schema validator."""
import json
import os
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

from flows.shopping_flow import FlowError
from scripts import run_website_smoke_v1 as cli
from scripts.validate_result_schema import validate_json, _validate_api_cleanup
from tests.test_full_cart_cleanup import (
    BASE, SECRET, FakeAPI, cart, policy, make_runtime, install_journeys,
)
from tests.website_smoke_v1_cases import WebsiteSmokeV1Runner
from utils.cart_cleanup import cleanup_cart_session
from utils.result import ResultWriteError
from utils.safe_artifact_write import write_run_json


@pytest.mark.parametrize("mode,invocations,responses,status", [
    ("headers", 0, 0, "BLOCKED"), ("denied", 0, 0, "BLOCKED"),
    ("unknown", 1, 0, "FAIL"), ("200", 1, 1, "PASS"), ("500", 1, 1, "FAIL"),
])
def test_api_lifecycle_counts(mode, invocations, responses, status):
    api = FakeAPI(cart(1))
    guard = policy(api, "https://lavetir.com" if mode == "denied" else BASE)
    api.post_error = mode == "unknown"
    api.post_status = 500 if mode == "500" else 200
    def headers(url):
        if mode == "headers" and url.endswith("/cart/clear.js"):
            raise RuntimeError(SECRET)
        return {"Signature": SECRET}
    report = cleanup_cart_session(api, BASE, headers, guard, journey="browse", viewport="desktop")
    summary = guard.summary()
    audit = summary["api_cleanup"]
    assert report["status"] == status
    assert sum(c[0] == "POST" for c in api.calls) == invocations
    assert summary["expected_mutation"] == invocations
    assert audit["authorization_allowed"] == (0 if mode == "denied" else 1)
    assert audit["authorization_denied"] == (1 if mode == "denied" else 0)
    assert audit["post_invocations"] == invocations
    assert audit["responses_received"] == responses
    assert audit["outcome_unknown"] == (1 if mode == "unknown" else 0)
    assert _validate_api_cleanup(audit)
    assert SECRET not in json.dumps([report, summary, guard.events()])


def test_api_method_unavailable_is_not_an_invocation():
    class UnavailableAPI(FakeAPI):
        @property
        def post(self):
            raise RuntimeError(SECRET)
    api = UnavailableAPI(cart(1))
    guard = policy(api)
    report = cleanup_cart_session(api, BASE, lambda _url: {}, guard,
                                  journey="browse", viewport="desktop")
    assert report["status"] == "BLOCKED" and not report["clear_requested"]
    assert report["safe_error_code"] == "API_CLEANUP_REQUEST_UNAVAILABLE"
    assert guard.summary()["api_cleanup"]["post_invocations"] == 0
    assert guard.summary()["expected_mutation"] == 0
    assert SECRET not in json.dumps([report, guard.summary()])


def run_cli(monkeypatch, tmp_path, *, mode="pass", both=False):
    install_journeys(monkeypatch, checkout_fail=mode == "case_fail")
    original = WebsiteSmokeV1Runner._run_journey
    sessions = []
    closed = []
    def create(viewport, **_kwargs):
        api = FakeAPI()
        if mode == "clear_fail":
            api.post_status = 500
        if mode == "blocked":
            api.get_status = 429
        runtime = make_runtime(api)
        sessions.append((viewport, api))
        if mode == "headers":
            def request_headers(url):
                if url.endswith("/cart/clear.js"):
                    raise RuntimeError(SECRET)
                return {}
            from types import SimpleNamespace
            runtime.access_policy = SimpleNamespace(request_headers=request_headers)
        return runtime
    def journey(self, name):
        fatal_here = mode.startswith("fatal") and (not both or self.viewport == "mobile")
        if name != "browse" or not fatal_here:
            result = original(self, name)
            if mode == "mutation" and name == "browse":
                self._mutation_policy.authorize_api_cleanup(BASE + "/cart/add.js", "POST")
            return result
        self._journey = name
        api = self.runtime.context.request
        api.cart = cart(1, 2)
        for index, cid in enumerate(self._journey_cases(name)[:2]):
            case_name, deps, _journey, _fn = self._cases[cid]
            def action():
                if index == 1:
                    raise FlowError("PDP_VARIANT_UNAVAILABLE", "offline failure")
                return "offline case"
            self._run_case(cid, case_name, deps, action)
        if mode == "fatal_blocked":
            api.unavailable = True
        raise RuntimeError("OFFLINE_FRAMEWORK_FAILURE")
    monkeypatch.setattr(WebsiteSmokeV1Runner, "_run_journey", journey)
    monkeypatch.setattr(cli, "create_browser", create)
    def close(runtime):
        assert list(tmp_path.rglob("cart-cleanup.json")) or mode == "write_fail"
        closed.append(runtime)
        if mode == "close_fail":
            raise RuntimeError("OFFLINE_CLOSE_FAILURE")
    monkeypatch.setattr(cli, "close_browser", close)
    monkeypatch.setattr(cli, "ARTIFACT_ROOT", tmp_path)
    if mode == "denied":
        monkeypatch.setattr("utils.readonly_mutation_guard.TransactionalMutationPolicy._classify",
                            lambda *_a: "UNEXPECTED_MUTATION")
    if mode == "write_fail":
        real_write = cli.write_run_json
        def fail_audit(data, root, relative):
            if relative != "results.json":
                raise ResultWriteError("ARTIFACT_WRITE_REJECTED")
            return real_write(data, root, relative)
        monkeypatch.setattr(cli, "write_run_json", fail_audit)
    code = cli.main(["--site", "mondressy", "--viewport", "both" if both else "desktop"])
    path = next(tmp_path.rglob("results.json"))
    data = json.loads(path.read_text(encoding="utf-8"))
    return code, data, path, sessions, closed


@pytest.mark.parametrize("mode", ["pass", "case_fail", "clear_fail", "blocked", "fatal",
                                 "fatal_blocked", "write_fail", "denied", "headers", "close_fail", "mutation"])
def test_production_end_to_end(monkeypatch, tmp_path, capsys, mode):
    code, data, path, sessions, closed = run_cli(monkeypatch, tmp_path, mode=mode)
    assert code == (0 if mode == "pass" else 1)
    assert validate_json(path.parent, "website_smoke_v1")
    assert data["overall_status"] == ("PASS" if mode == "pass" else "FAIL")
    vp = data["viewports"][0]
    assert vp["status"] == data["overall_status"]
    assert data["summary"]["total"] == len(vp["cases"]) > 0
    assert closed
    if mode in {"denied", "headers", "blocked"}:
        assert not any(c[0] == "POST" for _, api in sessions for c in api.calls)
    if mode.startswith("fatal"):
        assert data["fatal_error"]["message"] == "OFFLINE_FRAMEWORK_FAILURE"
        assert vp["partial"] is True
        assert len(vp["cases"]) < 15
        assert any(c["status"] == "FAIL" for c in vp["cases"])
    if mode == "fatal_blocked":
        assert vp["cleanup"]["status"] == "BLOCKED"
    if mode == "write_fail":
        assert vp["cleanup"]["status"] == "FAIL"
        assert vp["cleanup"]["audit_path"] is None
        assert vp["cleanup"]["detail"] == "CLEANUP_AUDIT_WRITE_FAILED"
    else:
        audit = json.loads((path.parent / vp["cleanup"]["audit_path"]).read_text(encoding="utf-8"))
        assert audit["mutation_summary"] == vp["mutation_summary"] == data["mutation_summary"]
        assert audit["journeys"] == vp["cleanup"]["journeys"]
    assert SECRET not in json.dumps(data) + capsys.readouterr().out


def test_desktop_complete_mobile_partial(monkeypatch, tmp_path):
    code, data, path, sessions, _closed = run_cli(monkeypatch, tmp_path, mode="fatal", both=True)
    assert code == 1 and validate_json(path.parent, "website_smoke_v1")
    desktop, mobile = data["viewports"]
    assert desktop["status"] == "PASS" and len(desktop["cases"]) == 15
    assert "partial" not in desktop
    assert mobile["partial"] is True and mobile["status"] == "FAIL"
    assert 0 < len(mobile["cases"]) < 15
    assert data["summary"]["total"] == 15 + len(mobile["cases"])
    assert sessions[0][1] is not sessions[1][1]
    assert data["mutation_summary"]["expected_mutation"] == sum(
        v["mutation_summary"]["expected_mutation"] for v in data["viewports"])


def test_fatal_before_runner_does_not_invent_viewport(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "ARTIFACT_ROOT", tmp_path)
    def unavailable(*_a, **_k):
        raise RuntimeError("OFFLINE_BROWSER_UNAVAILABLE")
    monkeypatch.setattr(cli, "create_browser", unavailable)
    assert cli.main(["--site", "mondressy", "--viewport", "desktop"]) == 1
    path = next(tmp_path.rglob("results.json"))
    data = json.loads(path.read_text())
    assert data["viewports"] == [] and data["fatal_error"]
    assert validate_json(path.parent, "website_smoke_v1")


@pytest.mark.parametrize("mode", ["clear_fail", "blocked", "denied", "case_fail", "fatal", "mutation"])
def test_schema_rejects_false_pass_from_real_report(monkeypatch, tmp_path, mode):
    code, data, path, _sessions, _closed = run_cli(monkeypatch, tmp_path, mode=mode)
    assert code == 1 and validate_json(path.parent, "website_smoke_v1")
    original = deepcopy(data)
    data["viewports"][0]["status"] = "PASS"
    data["overall_status"] = "PASS"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert not validate_json(path.parent, "website_smoke_v1")
    original["overall_status"] = "PASS"
    path.write_text(json.dumps(original), encoding="utf-8")
    assert not validate_json(path.parent, "website_smoke_v1")


def test_schema_old_legal_full_report(monkeypatch, tmp_path):
    _, data, path, _, _ = run_cli(monkeypatch, tmp_path)
    for vp in data["viewports"]:
        vp["cleanup"].pop("journeys")
        vp["cleanup"].pop("audit_path")
        vp["mutation_summary"].pop("api_cleanup")
    data["mutation_summary"].pop("api_cleanup")
    path.write_text(json.dumps(data), encoding="utf-8")
    assert validate_json(path.parent, "website_smoke_v1")
    data["mutation_summary"] = None
    data["viewports"][0]["mutation_summary"] = None
    path.write_text(json.dumps(data), encoding="utf-8")
    assert validate_json(path.parent, "website_smoke_v1")


@pytest.mark.parametrize("status", ["FAIL", "BLOCKED"])
def test_schema_rejects_preclean_failure_with_pass(monkeypatch, tmp_path, status):
    _, data, path, _, _ = run_cli(monkeypatch, tmp_path)
    data["viewports"][0]["pre_clean"]["status"] = status
    path.write_text(json.dumps(data), encoding="utf-8")
    assert not validate_json(path.parent, "website_smoke_v1")


@pytest.mark.parametrize("field", ["expected_mutation", "post_invocations", "responses_received"])
def test_schema_rejects_inconsistent_api_counts(monkeypatch, tmp_path, field):
    _, data, path, _, _ = run_cli(monkeypatch, tmp_path)
    for summary in (data["mutation_summary"], data["viewports"][0]["mutation_summary"]):
        if field == "expected_mutation":
            summary[field] = 0
        else:
            summary["api_cleanup"][field] = 0
    vp = data["viewports"][0]
    audit_path = path.parent / vp["cleanup"]["audit_path"]
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    audit["mutation_summary"] = deepcopy(vp["mutation_summary"])
    audit_path.write_text(json.dumps(audit), encoding="utf-8")
    path.write_text(json.dumps(data), encoding="utf-8")
    assert not validate_json(path.parent, "website_smoke_v1")


def link_or_skip(link, target, *, directory):
    try:
        os.symlink(target, link, target_is_directory=directory)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {type(exc).__name__}")


@pytest.mark.parametrize("kind", ["directory", "file", "junction", "root"])
def test_real_audit_write_rejects_links(monkeypatch, tmp_path, kind):
    run = tmp_path / "run"
    outside = tmp_path / "outside"
    run.mkdir(); outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("unchanged")
    if kind == "directory":
        link_or_skip(run / "desktop", outside, directory=True)
    elif kind == "root":
        run.rmdir()
        link_or_skip(run, outside, directory=True)
    elif kind == "file":
        (run / "desktop").mkdir()
        link_or_skip(run / "desktop/cart-cleanup.json", sentinel, directory=False)
    else:
        if os.name != "nt":
            pytest.skip("Windows junction requires Windows")
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(run / "desktop"), str(outside)],
                                capture_output=True)
        if result.returncode:
            pytest.skip("Windows junction creation unavailable")
    install_journeys(monkeypatch, empty=True)
    api = FakeAPI()
    monkeypatch.setattr(cli, "create_browser", lambda *_a, **_k: make_runtime(api))
    closed = []
    monkeypatch.setattr(cli, "close_browser", lambda _r: closed.append(True))
    _, runner, _ = cli.run_viewport("desktop", run, mutation_policy=policy(api))
    assert runner.cleanup_status == "FAIL" and runner.cleanup_artifact is None
    assert runner.cleanup_error == "CLEANUP_AUDIT_WRITE_FAILED"
    assert closed and sentinel.read_text() == "unchanged"
    assert not (outside / "cart-cleanup.json").exists()


@pytest.mark.parametrize("relative", ["../outside.json", "/outside.json", "C:/outside.json",
                                      "desktop/../../outside.json", "desktop\\cart-cleanup.json"])
def test_safe_write_rejects_escaping_paths(tmp_path, relative):
    with pytest.raises(ResultWriteError, match="^ARTIFACT_WRITE_REJECTED$"):
        write_run_json({}, tmp_path, relative)
    assert list(tmp_path.iterdir()) == []


def test_safe_atomic_update_and_viewport_isolation(tmp_path):
    write_run_json({"n": 1}, tmp_path, "desktop/cart-cleanup.json")
    write_run_json({"n": 2}, tmp_path, "desktop/cart-cleanup.json")
    write_run_json({"n": 3}, tmp_path, "mobile/cart-cleanup.json")
    assert json.loads((tmp_path / "desktop/cart-cleanup.json").read_text()) == {"n": 2}
    assert json.loads((tmp_path / "mobile/cart-cleanup.json").read_text()) == {"n": 3}
    assert not list(tmp_path.rglob(".full-json-*"))


def test_atomic_write_error_preserves_existing_file(monkeypatch, tmp_path):
    write_run_json({"n": 1}, tmp_path, "results.json")
    def denied(*_a, **_k):
        raise OSError(SECRET)
    monkeypatch.setattr("utils.safe_artifact_write.os.replace", denied)
    with pytest.raises(ResultWriteError, match="^ARTIFACT_WRITE_REJECTED$"):
        write_run_json({"n": 2}, tmp_path, "results.json")
    assert json.loads((tmp_path / "results.json").read_text()) == {"n": 1}
    assert not list(tmp_path.rglob(".full-json-*"))


def test_windows_directory_replacement_is_locked(monkeypatch, tmp_path):
    if os.name != "nt":
        pytest.skip("Windows no-delete handles require Windows")
    write_run_json({"n": 1}, tmp_path, "desktop/cart-cleanup.json")
    replace = os.replace
    attempted = []
    def race(source, target):
        with pytest.raises(OSError):
            (tmp_path / "desktop").rename(tmp_path / "relocated")
        attempted.append(True)
        return replace(source, target)
    monkeypatch.setattr("utils.safe_artifact_write.os.replace", race)
    write_run_json({"n": 2}, tmp_path, "desktop/cart-cleanup.json")
    assert attempted and not (tmp_path / "relocated").exists()
    assert json.loads((tmp_path / "desktop/cart-cleanup.json").read_text()) == {"n": 2}


def test_atomic_update_does_not_write_through_hardlink(tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text("sentinel", encoding="utf-8")
    run = tmp_path / "run"
    run.mkdir()
    os.link(outside, run / "results.json")
    write_run_json({"safe": True}, run, "results.json")
    assert outside.read_text(encoding="utf-8") == "sentinel"
    assert json.loads((run / "results.json").read_text()) == {"safe": True}
