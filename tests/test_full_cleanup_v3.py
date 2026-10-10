"""V3 contracts exercise saved output of the production Full CLI and validator."""
import json
from uuid import uuid4

import pytest

from flows.shopping_flow import ShoppingFlow
from scripts.validate_result_schema import validate_json
from tests.test_full_cleanup_v2 import run_cli
from tests.test_full_cart_cleanup import BASE, SECRET, FakeAPI, cart
from tests.website_smoke_v1_cases import WebsiteSmokeV1Runner
from utils.cart_cleanup import cleanup_cart_session


def save(data, path):
    """Keep main and sidecar consistent: negatives must reach audit validation."""
    for vp in data["viewports"]:
        cleanup = vp["cleanup"]
        audit_path = path.parent / cleanup["audit_path"]
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        for key in ("journeys", "status", "contract_version", "progress"):
            if key in cleanup:
                audit[key] = cleanup[key]
            else:
                audit.pop(key, None)
        audit["mutation_summary"] = vp["mutation_summary"]
        audit_path.write_text(json.dumps(audit), encoding="utf-8")
    path.write_text(json.dumps(data), encoding="utf-8")


def early_cli(monkeypatch, tmp_path, *, direct=False, both=False, blocked=False):
    original = WebsiteSmokeV1Runner.run_all
    def run(self):
        if both and self.viewport == "desktop":
            return original(self)
        if direct:
            original_journey = self._run_journey
            def journey(name):
                if name != "direct":
                    return original_journey(name)
                cid = self._journey_cases(name)[0]
                case_name, deps, _journey, _fn = self._cases[cid]
                self._run_case(cid, case_name, deps, lambda: "recorded before fatal")
                self.runtime.context.request.cart = cart(1)
                raise RuntimeError("OFFLINE_DIRECT_FATAL")
            self._run_journey = journey
            return original(self)
        if blocked:
            self.runtime.context.request.unavailable = True
        raise RuntimeError("OFFLINE_EARLY_FATAL")
    monkeypatch.setattr(WebsiteSmokeV1Runner, "run_all", run)
    return run_cli(monkeypatch, tmp_path, both=both)


@pytest.mark.parametrize("direct,both,blocked", [
    (False, False, False), (True, False, False),
    (False, True, False), (False, False, True),
])
def test_real_early_partial_report(monkeypatch, tmp_path, capsys, direct, both, blocked):
    code, data, path, sessions, closed = early_cli(
        monkeypatch, tmp_path, direct=direct, both=both, blocked=blocked)
    assert code == 1 and data["overall_status"] == "FAIL"
    assert data["fatal_error"]["message"] == (
        "OFFLINE_DIRECT_FATAL" if direct else "OFFLINE_EARLY_FATAL")
    vp = data["viewports"][-1]
    assert vp["partial"] is True and vp["status"] == "FAIL"
    assert len(vp["cases"]) == (1 if direct else 0)
    assert vp["cleanup"]["progress"]["recorded_case_ids"] == [c["case_id"] for c in vp["cases"]]
    assert [r["journey"] for r in vp["cleanup"]["journeys"]] == (
        ["pre_clean", "browse"] if direct else ["browse"])
    if direct:
        assert vp["cases"][0]["status"] == "PASS"
    if both:
        assert data["viewports"][0]["status"] == "PASS"
        assert len(data["viewports"][0]["cases"]) == 15
        assert len(sessions) == len(closed) == 2
    audit = path.parent / vp["cleanup"]["audit_path"]
    assert audit.is_file() and json.loads(audit.read_text())["journeys"] == vp["cleanup"]["journeys"]
    assert validate_json(path.parent, "website_smoke_v1")
    assert SECRET not in path.read_text() + audit.read_text() + capsys.readouterr().out


@pytest.mark.parametrize("fake_fatal", [False, True])
@pytest.mark.parametrize("missing", ["pre_clean", "direct", "search", "browse"])
def test_complete_run_cannot_omit_cleanup(monkeypatch, tmp_path, missing, fake_fatal):
    code, data, path, *_ = run_cli(monkeypatch, tmp_path)
    assert code == 0
    vp = data["viewports"][0]
    vp["cleanup"]["journeys"] = [r for r in vp["cleanup"]["journeys"] if r["journey"] != missing]
    if fake_fatal:
        vp["partial"] = True
        vp["status"] = data["overall_status"] = "FAIL"
        data["fatal_error"] = {"classification": "FRAMEWORK_ERROR", "message": "forged fatal"}
    save(data, path)
    assert not validate_json(path.parent, "website_smoke_v1")


def forge_cleanup_pass(data):
    vp = data["viewports"][0]
    vp["status"] = data["overall_status"] = "PASS"
    vp["cleanup"]["status"] = "PASS"
    vp["cleanup"]["detail"] = ""
    for report in vp["cleanup"]["journeys"]:
        report.update(status="PASS", postcheck_status="PASS", safe_error_code="NONE")
        if report["clear_requested"]:
            report["clear_response_category"] = "HTTP_200"


@pytest.mark.parametrize("mode", ["clear_fail", "denied", "headers", "unknown"])
def test_real_unsuccessful_api_cannot_be_forged_pass(monkeypatch, tmp_path, capsys, mode):
    if mode == "unknown":
        original = FakeAPI.post
        def post(self, *args, **kwargs):
            self.post_error = True
            return original(self, *args, **kwargs)
        monkeypatch.setattr(FakeAPI, "post", post)
    code, data, path, sessions, _ = run_cli(monkeypatch, tmp_path,
                                          mode="pass" if mode == "unknown" else mode)
    assert code == 1 and validate_json(path.parent, "website_smoke_v1")
    audit = data["viewports"][0]["mutation_summary"]["api_cleanup"]
    if mode in {"headers", "denied"}:
        assert audit["post_invocations"] == 0
        assert not any(c[0] == "POST" for _, api in sessions for c in api.calls)
    if mode == "clear_fail":
        assert len(audit["records"]) == 3
        assert all(r["http_status"] == 500 for r in audit["records"])
    if mode == "unknown":
        assert audit["outcome_unknown"] == 3 and audit["responses_received"] == 0
    forge_cleanup_pass(data)
    save(data, path)
    assert not validate_json(path.parent, "website_smoke_v1")
    output = capsys.readouterr().out
    assert "CLEANUP_AUDIT_CONSISTENCY_ERROR" in output and SECRET not in output


@pytest.mark.parametrize("tamper", ["stage", "viewport", "operation", "attempt", "missing"])
def test_operation_correlations_cannot_be_crossed(monkeypatch, tmp_path, tamper):
    code, data, path, *_ = run_cli(monkeypatch, tmp_path)
    assert code == 0
    vp = data["viewports"][0]
    records = vp["mutation_summary"]["api_cleanup"]["records"]
    if tamper == "stage":
        records[-1]["cleanup_stage"] = "direct"
    elif tamper == "viewport":
        records[-1]["viewport"] = "mobile"
    elif tamper == "operation":
        records[-1]["operation_id"] = records[0]["operation_id"]
    elif tamper == "attempt":
        records[-1]["attempt_id"] = 2
    else:
        # A well-formed zero-call summary must not substantiate attempted cleanup.
        summary = vp["mutation_summary"]["api_cleanup"]
        summary["records"] = []
        for key in ("authorization_allowed", "authorization_denied", "post_invocations",
                    "responses_received", "outcome_unknown"):
            summary[key] = 0
    save(data, path)
    assert not validate_json(path.parent, "website_smoke_v1")


@pytest.mark.parametrize("version", [None, 0, "unknown", 999])
def test_schema_label_cannot_bypass_new_contract(monkeypatch, tmp_path, version):
    _, data, path, *_ = run_cli(monkeypatch, tmp_path, mode="clear_fail")
    forge_cleanup_pass(data)
    data["schema_version"] = version
    save(data, path)
    assert not validate_json(path.parent, "website_smoke_v1")


def test_legitimate_legacy_saved_report(monkeypatch, tmp_path):
    _, data, path, *_ = run_cli(monkeypatch, tmp_path)
    vp = data["viewports"][0]
    for key in ("contract_version", "progress"):
        vp["cleanup"].pop(key)
    for report in vp["cleanup"]["journeys"]:
        report.pop("operation_id")
        report.pop("attempt_id")
    for summary in (data["mutation_summary"], vp["mutation_summary"]):
        for record in summary["api_cleanup"]["records"]:
            for key in ("operation_id", "attempt_id", "cleanup_stage"):
                record.pop(key)
    save(data, path)
    assert validate_json(path.parent, "website_smoke_v1")


def test_api_markers_require_new_contract_even_after_cleanup_markers_removed(monkeypatch, tmp_path):
    _, data, path, *_ = run_cli(monkeypatch, tmp_path)
    cleanup = data["viewports"][0]["cleanup"]
    for key in ("contract_version", "progress"):
        cleanup.pop(key)
    for report in cleanup["journeys"]:
        report.pop("operation_id")
        report.pop("attempt_id")
    data["schema_version"] = "1.0"
    save(data, path)
    assert not validate_json(path.parent, "website_smoke_v1")


def test_real_empty_cart_operations_never_claim_posts(monkeypatch, tmp_path):
    original = WebsiteSmokeV1Runner._run_case
    def case(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        self.runtime.context.request.cart = cart()
        return result
    monkeypatch.setattr(WebsiteSmokeV1Runner, "_run_case", case)
    code, data, path, sessions, _ = run_cli(monkeypatch, tmp_path)
    assert code == 0 and validate_json(path.parent, "website_smoke_v1")
    assert not any(c[0] == "POST" for _, api in sessions for c in api.calls)
    vp = data["viewports"][0]
    assert vp["mutation_summary"]["api_cleanup"]["records"] == []
    assert all(r["attempt_id"] is None and r["status"] == "PASS" and
               not r["clear_requested"] for r in vp["cleanup"]["journeys"])


def test_same_operation_later_verified_success_is_not_blanket_500_failure(monkeypatch, tmp_path):
    # Explicit test harness recovery, not a production retry feature.
    original = ShoppingFlow.cleanup_cart
    observations = []
    def cleanup(self, *args, **kwargs):
        if kwargs.get("journey") != "browse":
            return original(self, *args, **kwargs)
        api = self._session_api()
        operation = uuid4().hex
        for status in (500, 200):
            api.post_status = status
            report = cleanup_cart_session(api, BASE, lambda _url: {}, self.mutation_policy,
                                          journey="browse", viewport=self.viewport,
                                          operation_id=operation)
            observations.append(report)
        self.last_cleanup_report = report
        return "cleaned"
    monkeypatch.setattr(ShoppingFlow, "cleanup_cart", cleanup)
    code, data, path, *_ = run_cli(monkeypatch, tmp_path)
    assert code == 0
    assert [r["status"] for r in observations] == ["FAIL", "PASS"]
    records = data["viewports"][0]["mutation_summary"]["api_cleanup"]["records"][-2:]
    assert [r["http_status"] for r in records] == [500, 200]
    assert [r["attempt_id"] for r in records] == [1, 2]
    assert records[0]["operation_id"] == records[1]["operation_id"]
    assert observations[-1]["postcheck_status"] == "PASS"
    assert validate_json(path.parent, "website_smoke_v1")
