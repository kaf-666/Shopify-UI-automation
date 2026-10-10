"""Offline contract tests: all network access is a stateful fake API session."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from flows.shopping_flow import FlowError, ShoppingFlow
from scripts import run_website_smoke_v1 as cli
from scripts.validate_result_schema import validate_json
from scripts.validate_result_schema import _validate_cleanup_audit
from utils.cart_cleanup import cleanup_report
from tests.website_smoke_v1_cases import WebsiteSmokeV1Runner
from utils.cart_cleanup import cleanup_cart_session
from utils.readonly_mutation_guard import TransactionalMutationPolicy

BASE = "https://mondressy.com"
SECRET = "private-cookie-token-signature"


def cart(*quantities):
    return {"item_count": sum(quantities), "items": [
        {"quantity": q, "customer": SECRET} for q in quantities
    ], "note": SECRET}


class FakeAPI:
    def __init__(self, data=None):
        self.cart = cart() if data is None else data
        self.calls = []
        self.get_status = 200
        self.post_status = 200
        self.redirect = False
        self.residual = False
        self.unavailable = False
        self.post_error = False
        self.postcheck_error = False

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        assert kwargs["max_redirects"] == 0
        if self.unavailable or (self.postcheck_error and any(c[0] == "POST" for c in self.calls)):
            raise RuntimeError(SECRET)
        return SimpleNamespace(status=self.get_status, url=url,
                               json=lambda: deepcopy(self.cart))

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        assert kwargs["max_redirects"] == 0
        if self.post_error:
            raise RuntimeError(SECRET)
        if self.post_status == 200 and not self.residual and not self.redirect:
            self.cart = cart()
        return SimpleNamespace(status=302 if self.redirect else self.post_status,
                               url=BASE + "/checkout" if self.redirect else url)


class FakeContext:
    def __init__(self, api):
        self.request = api

    def route(self, *_args):
        pass

    def unroute(self, *_args):
        pass


def policy(api=None, base=BASE):
    guard = TransactionalMutationPolicy(["mondressy.com"], canonical_origin=base)
    guard.attach(FakeContext(api))
    return guard


def clean(api, guard=None):
    return cleanup_cart_session(api, BASE, lambda _url: {"Signature": SECRET},
                                guard or policy(api), journey="browse", viewport="mobile")


def test_empty_cart_requires_no_post():
    api = FakeAPI()
    report = clean(api)
    assert report["status"] == "PASS"
    assert report["clear_requested"] is False
    assert [c[0] for c in api.calls] == ["GET"]


def test_multiple_lines_are_cleared_verified_and_audited():
    api = FakeAPI(cart(2, 3))
    guard = policy(api)
    report = clean(api, guard)
    assert report["status"] == "PASS"
    assert report["cart_item_count"] == 5
    assert report["postcheck_status"] == "PASS"
    assert [c[0] for c in api.calls] == ["GET", "POST", "GET"]
    assert api.calls[1][2]["headers"] == {"Signature": SECRET}
    assert guard.summary()["expected_mutation"] == 1
    assert guard.events()[0]["reason"] == "API_CLEANUP_EXPECTED_MUTATION"
    assert SECRET not in json.dumps([report, guard.summary(), guard.events()])


@pytest.mark.parametrize("data", [
    {}, None, [], {"item_count": 0}, {"items": []},
    {"item_count": None, "items": []}, {"item_count": "0", "items": []},
    {"item_count": True, "items": []}, {"item_count": -1, "items": []},
    {"item_count": 1, "items": []}, {"item_count": 0, "items": [{"quantity": 0}]},
    {"item_count": 1, "items": [{"quantity": "1"}]},
    {"item_count": 1, "items": [{"quantity": True}]},
    {"item_count": 1, "items": [{"quantity": -1}]},
    {"item_count": 1, "items": [None]}, {"item_count": 1, "items": [{}]},
    {"item_count": 1, "items": None},
])
def test_invalid_cart_state_blocks_without_post(data):
    api = FakeAPI()
    api.cart = data
    report = clean(api)
    assert report["status"] == "BLOCKED"
    assert not report["clear_requested"]
    assert not any(c[0] == "POST" for c in api.calls)


@pytest.mark.parametrize("status", [429, 403, 500, 302])
def test_cart_read_http_unavailable_blocks(status):
    api = FakeAPI(cart(1))
    api.get_status = status
    assert clean(api)["status"] == "BLOCKED"
    assert len(api.calls) == 1


@pytest.mark.parametrize("attribute,value,expected", [
    ("post_status", 500, "FAIL"), ("redirect", True, "FAIL"),
    ("residual", True, "FAIL"), ("post_error", True, "FAIL"),
    ("postcheck_error", True, "BLOCKED"),
])
def test_failed_clear_or_unverified_postcheck_never_passes(attribute, value, expected):
    api = FakeAPI(cart(1))
    setattr(api, attribute, value)
    report = clean(api)
    assert report["status"] == expected
    assert SECRET not in json.dumps(report)
    assert sum(c[0] == "POST" for c in api.calls) == 1


@pytest.mark.parametrize("url,method", [
    ("https://other.test/cart/clear.js", "POST"),
    ("https://www.mondressy.com/cart/clear.js", "POST"),
    (BASE + "/cart/add.js", "POST"), (BASE + "/checkout", "POST"),
    (BASE + "/cart/clear.js?token=" + SECRET, "POST"),
    (BASE + "/cart/clear.js#" + SECRET, "POST"),
    (BASE + "/cart/clear.js", "GET"),
    ("http://mondressy.com/cart/clear.js", "POST"),
    ("https://user:pass@mondressy.com/cart/clear.js", "POST"),
    (BASE + "/private/" + SECRET, "DELETE"),
])
def test_api_policy_denies_wrong_origin_method_path_and_secret_targets(url, method):
    guard = policy()
    assert guard.authorize_api_cleanup(url, method) == "UNEXPECTED_MUTATION"
    assert guard.summary()["status"] == "FAIL"
    assert guard.events()[0]["blocked"]
    assert SECRET not in json.dumps(guard.summary())


def test_policy_denial_never_sends_post():
    api = FakeAPI(cart(1))
    guard = policy(api, "https://lavetir.com")
    assert clean(api, guard)["status"] == "BLOCKED"
    assert len(api.calls) == 1


def test_detached_policy_is_fail_closed():
    api = FakeAPI(cart(1))
    guard = policy(api)
    guard.detach()
    assert clean(api, guard)["status"] == "BLOCKED"
    assert len(api.calls) == 1


def test_policy_classifier_denial_is_respected():
    class DenyingPolicy(TransactionalMutationPolicy):
        def _classify(self, *_args):
            return self.UNEXPECTED
    api = FakeAPI(cart(1))
    guard = DenyingPolicy(["mondressy.com"], canonical_origin=BASE)
    guard.attach(FakeContext(api))
    assert clean(api, guard)["status"] == "BLOCKED"
    assert not any(c[0] == "POST" for c in api.calls)
    assert guard.summary()["unexpected_mutation"] == 1


@pytest.mark.parametrize("base", ["http://mondressy.com", BASE + "/checkout",
                                 BASE + "?token=" + SECRET, "https://u:p@mondressy.com"])
def test_invalid_configured_origin_blocks_before_network(base):
    api = FakeAPI(cart(1))
    report = cleanup_cart_session(api, base, lambda _url: {}, policy(api),
                                  journey="browse", viewport="desktop")
    assert report["status"] == "BLOCKED"
    assert not api.calls
    assert SECRET not in json.dumps(report)


def test_changed_cart_read_target_is_rejected_even_with_http_200():
    api = FakeAPI(cart(1))
    original = api.get
    def redirect_read(url, **kwargs):
        response = original(url, **kwargs)
        response.url = "https://other.test/cart.js"
        return response
    api.get = redirect_read
    assert clean(api)["status"] == "BLOCKED"
    assert len(api.calls) == 1


def test_closed_page_uses_context_api_and_closed_context_blocks():
    class ClosedPage:
        @property
        def request(self):
            raise RuntimeError(SECRET)
    api = FakeAPI(cart(1))
    flow = ShoppingFlow(ClosedPage(), {"base_url": BASE}, "desktop",
                        api_request=api, mutation_policy=policy(api))
    flow.cleanup_cart()
    assert flow.last_cleanup_report["status"] == "PASS"
    api.unavailable = True
    with pytest.raises(FlowError) as error:
        flow.cleanup_cart()
    assert error.value.category == "CART_PRECONDITION_UNVERIFIED"
    assert flow.last_cleanup_report["status"] == "BLOCKED"
    assert SECRET not in str(error.value)


def make_runtime(api):
    return SimpleNamespace(page=object(), context=FakeContext(api), access_policy=None,
                           site_config={"base_url": BASE}, site_name="mondressy",
                           metadata=lambda: {"engine": "chromium", "device": None})


def install_journeys(monkeypatch, *, checkout_fail=False, empty=False):
    monkeypatch.setattr("tests.website_smoke_v1_cases.capture_case_failure", lambda *_args: None)
    def journey(self, name):
        self._journey = name
        api = self.runtime.context.request
        api.cart = cart() if empty else cart(1, 2)
        for case_id in self._journey_cases(name):
            case_name, deps, _journey, _fn = self._cases[case_id]
            def action(cid=case_id):
                if checkout_fail and cid == "WSMOKE-CHECKOUT-01":
                    raise FlowError("CHECKOUT_CONTEXT_NOT_REACHED", "checkout unavailable")
                return "offline case"
            self._run_case(case_id, case_name, deps, action)
    monkeypatch.setattr(WebsiteSmokeV1Runner, "_run_journey", journey)


@pytest.mark.parametrize("checkout_fail", [False, True])
def test_every_journey_is_cleaned_and_checkout_failure_is_preserved(monkeypatch, tmp_path, checkout_fail):
    install_journeys(monkeypatch, checkout_fail=checkout_fail)
    api = FakeAPI()
    runner = WebsiteSmokeV1Runner(make_runtime(api), {"base_url": BASE}, "desktop",
                                 artifact_dir=tmp_path, mutation_policy=policy(api))
    runner.run_all()
    assert [r["journey"] for r in runner.cleanup_reports] == ["pre_clean", "direct", "search", "browse"]
    assert all(r["status"] == "PASS" for r in runner.cleanup_reports)
    assert sum(c[0] == "POST" for c in api.calls) == 3
    result = runner.results["WSMOKE-CHECKOUT-01"]
    assert result.status == ("FAIL" if checkout_fail else "PASS")
    if checkout_fail:
        assert result.failure_classification == "CHECKOUT_CONTEXT_NOT_REACHED"
    runner.finalize_cleanup()
    assert sum(c[0] == "POST" for c in api.calls) == 3


def test_fatal_journey_still_cleans_in_finally(monkeypatch, tmp_path):
    api = FakeAPI()
    runner = WebsiteSmokeV1Runner(make_runtime(api), {"base_url": BASE}, "desktop",
                                 artifact_dir=tmp_path, mutation_policy=policy(api))
    def fatal(_journey):
        api.cart = cart(1)
        raise RuntimeError("original business failure")
    monkeypatch.setattr(runner, "_run_journey", fatal)
    with pytest.raises(RuntimeError, match="original business failure"):
        runner.run_all()
    assert api.cart["item_count"] == 0
    assert runner.cleanup_reports[-1]["journey"] == "browse"


def test_final_cleanup_internal_error_never_masks_business_failure(monkeypatch, tmp_path):
    api = FakeAPI()
    runner = WebsiteSmokeV1Runner(make_runtime(api), {"base_url": BASE}, "desktop",
                                 artifact_dir=tmp_path, mutation_policy=policy(api))
    def fatal(_journey):
        raise RuntimeError("original business failure")
    def cleanup_error():
        raise RuntimeError(SECRET)
    monkeypatch.setattr(runner, "_run_journey", fatal)
    monkeypatch.setattr(runner, "_journey_cleanup", cleanup_error)
    with pytest.raises(RuntimeError, match="original business failure"):
        runner.run_all()
    assert runner.cleanup_status == "BLOCKED"
    assert SECRET not in json.dumps(runner.cleanup_reports)


def test_valid_clear_response_with_invalid_postcheck_is_blocked():
    api = FakeAPI(cart(1))
    original = api.post
    def clear(url, **kwargs):
        response = original(url, **kwargs)
        api.cart = {"items": []}
        return response
    api.post = clear
    report = clean(api)
    assert report["status"] == "BLOCKED"
    assert report["postcheck_status"] == "BLOCKED"


def test_earlier_failure_cannot_be_hidden_by_later_pass(tmp_path):
    api = FakeAPI(cart(1))
    runner = WebsiteSmokeV1Runner(make_runtime(api), {"base_url": BASE}, "desktop",
                                 artifact_dir=tmp_path, mutation_policy=policy(api))
    api.post_status = 500
    runner._journey = "direct"
    runner._journey_cleanup()
    api.cart = cart()
    runner._journey = "search"
    runner._journey_cleanup()
    runner.finalize_cleanup()
    assert runner.cleanup_status == "FAIL"


@pytest.mark.parametrize("failed_cleanup,checkout_fail", [(False, False), (True, False), (False, True),
                                                          ("blocked", False), ("request_error", False)])
def test_real_cli_results_schema_and_audit_before_close(monkeypatch, tmp_path, capsys,
                                                      failed_cleanup, checkout_fail):
    install_journeys(monkeypatch, checkout_fail=checkout_fail)
    sessions = []
    def create(viewport, **_kwargs):
        api = FakeAPI()
        if failed_cleanup == "blocked":
            api.get_status = 429
        elif failed_cleanup == "request_error":
            api.post_error = True
        elif failed_cleanup:
            api.post_status = 500
        runtime = make_runtime(api)
        sessions.append((viewport, api))
        return runtime
    def close(runtime):
        viewport = next(v for v, api in sessions if api is runtime.context.request)
        files = list(tmp_path.rglob(f"{viewport}/cart-cleanup.json"))
        assert len(files) == 1
        saved = json.loads(files[0].read_text(encoding="utf-8"))
        assert saved["journeys"][-1]["journey"] == "browse"
        assert SECRET not in json.dumps(saved)
    monkeypatch.setattr(cli, "create_browser", create)
    monkeypatch.setattr(cli, "close_browser", close)
    monkeypatch.setattr(cli, "ARTIFACT_ROOT", tmp_path)
    exit_code = cli.main(["--site", "mondressy", "--viewport", "both"])
    assert exit_code == (1 if failed_cleanup or checkout_fail else 0)
    assert len(sessions) == 2 and sessions[0][1] is not sessions[1][1]
    results_file = next(tmp_path.rglob("results.json"))
    saved = json.loads(results_file.read_text(encoding="utf-8"))
    assert validate_json(results_file.parent, "website_smoke_v1")
    assert SECRET not in results_file.read_text(encoding="utf-8")
    assert SECRET not in capsys.readouterr().out
    for viewport in saved["viewports"]:
        cleanup = viewport["cleanup"]
        assert cleanup["status"] == ("BLOCKED" if failed_cleanup == "blocked" else
                                     "FAIL" if failed_cleanup else "PASS")
        assert (results_file.parent / cleanup["audit_path"]).is_file()
        assert viewport["status"] == ("FAIL" if failed_cleanup or checkout_fail else "PASS")


def test_browse_without_atc_is_empty_and_sends_no_post(monkeypatch, tmp_path):
    install_journeys(monkeypatch, empty=True)
    api = FakeAPI()
    runner = WebsiteSmokeV1Runner(make_runtime(api), {"base_url": BASE}, "mobile",
                                 artifact_dir=tmp_path, mutation_policy=policy(api))
    runner.run_all()
    assert runner.cleanup_status == "PASS"
    assert not any(c[0] == "POST" for c in api.calls)


def test_audit_write_failure_is_visible_and_context_still_closes(monkeypatch, tmp_path):
    install_journeys(monkeypatch, empty=True)
    api = FakeAPI()
    runtime = make_runtime(api)
    closed = []
    monkeypatch.setattr(cli, "create_browser", lambda *_args, **_kwargs: runtime)
    monkeypatch.setattr(cli, "close_browser", lambda _runtime: closed.append(True))
    def fail_write(*_args):
        raise OSError(SECRET)
    monkeypatch.setattr(cli, "write_results_json", fail_write)
    _results, runner, _meta = cli.run_viewport("desktop", tmp_path, mutation_policy=policy(api))
    assert closed == [True]
    assert runner.cleanup_status == "FAIL"
    assert runner.cleanup_error == "CLEANUP_AUDIT_WRITE_FAILED"
    assert runner.cleanup_artifact is None


def audit_value(tmp_path):
    reports = []
    for journey in ("pre_clean", "direct", "search", "browse"):
        report = cleanup_report(journey, "desktop")
        report.update(status="PASS", precheck_status="PASS", postcheck_status="PASS",
                      cart_item_count=0, safe_error_code="NONE")
        reports.append(report)
    value = {"status": "PASS", "detail": "CART_VERIFIED_EMPTY", "journeys": reports,
             "audit_path": "desktop/cart-cleanup.json"}
    path = tmp_path / value["audit_path"]
    path.parent.mkdir()
    path.write_text(json.dumps({"viewport": "desktop", "status": "PASS", "journeys": reports}), encoding="utf-8")
    return value


@pytest.mark.parametrize("bad_path", ["../cart-cleanup.json", "C:/private.json",
                                     "mobile/cart-cleanup.json", "desktop/missing.json"])
def test_schema_rejects_wrong_audit_path(tmp_path, bad_path):
    value = audit_value(tmp_path)
    value["audit_path"] = bad_path
    assert not _validate_cleanup_audit(tmp_path, "desktop", value)


@pytest.mark.parametrize("field,bad_value", [
    ("safe_error_code", SECRET), ("status", []), ("cart_item_count", True),
    ("clear_requested", True), ("postcheck_status", "BLOCKED"),
])
def test_schema_rejects_unsafe_or_false_pass_cleanup_records(tmp_path, field, bad_value):
    value = audit_value(tmp_path)
    value["journeys"][-1][field] = bad_value
    assert not _validate_cleanup_audit(tmp_path, "desktop", value)


def test_schema_rejects_missing_or_previous_run_audit(tmp_path):
    value = audit_value(tmp_path)
    path = tmp_path / value["audit_path"]
    path.write_text(json.dumps({"viewport": "mobile", "status": "PASS", "journeys": []}), encoding="utf-8")
    assert not _validate_cleanup_audit(tmp_path, "desktop", value)
    path.unlink()
    assert not _validate_cleanup_audit(tmp_path, "desktop", value)
