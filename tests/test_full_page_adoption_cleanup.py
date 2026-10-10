"""Page adoption regressions exercise real Flow cleanup, Full CLI and schema."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from flows.shopping_flow import FlowError
from scripts.validate_result_schema import validate_json
from tests import test_full_cleanup_v2 as harness
from tests.test_full_cart_cleanup import BASE, SECRET, FakeAPI, make_runtime, cart, policy
from tests.website_smoke_v1_cases import WebsiteSmokeV1Runner


class Page:
    def __init__(self, context):
        self.context = context
        self.closed = False
        self.url = BASE + "/products/offline-product"

    def is_closed(self):
        return self.closed

    def close(self):
        self.closed = True

    def content(self):
        return ""

    def title(self):
        return "Offline product"

    @property
    def request(self):
        raise RuntimeError(SECRET)  # Cleanup must use Context API, never Page fallback.


def runtime_with_page(api):
    runtime = make_runtime(api)
    runtime.page = Page(runtime.context)
    runtime.access_policy = SimpleNamespace(request_headers=lambda _url: {"Signature": SECRET})
    return runtime


@pytest.mark.parametrize("viewport", ["desktop", "mobile"])
def test_same_context_adoption_retains_dependencies_and_cleans_after_old_page_close(viewport):
    api = FakeAPI(cart(1))
    runtime = runtime_with_page(api)
    guard = policy(api)
    runner = WebsiteSmokeV1Runner(runtime, {"base_url": BASE}, viewport, mutation_policy=guard)
    initial = runner.flow
    page = Page(runtime.context)
    runner._adopt_business_page(page)
    assert runner.page is runner.flow.page is page
    assert initial.api_request is runner.flow.api_request is runtime.context.request
    assert initial.mutation_policy is runner.flow.mutation_policy is guard
    assert initial.access_policy is runner.flow.access_policy is runtime.access_policy
    runtime.page.close()
    runner._journey = "browse"
    runner._journey_cleanup()
    report = runner.cleanup_reports[-1]
    assert report["status"] == "PASS" and report["postcheck_status"] == "PASS"
    assert api.cart["item_count"] == 0
    assert [c[0] for c in api.calls] == ["GET", "POST", "GET"]
    assert all(call[2]["headers"] == {"Signature": SECRET} for call in api.calls)
    audit = guard.summary()["api_cleanup"]
    assert audit["authorization_allowed"] == audit["post_invocations"] == 1
    assert audit["records"][0]["viewport"] == viewport
    assert audit["records"][0]["cleanup_stage"] == "browse"
    assert SECRET not in json.dumps([report, guard.summary()])


@pytest.mark.parametrize("mode,code", [
    ("closed", "PAGE_CLOSED"), ("foreign", "PAGE_CONTEXT_MISMATCH"),
    ("unknown", "PAGE_SESSION_UNVERIFIED"),
    ("missing_api", "CONTEXT_API_UNAVAILABLE"), ("changed_api", "CONTEXT_API_UNAVAILABLE"),
    ("missing_policy", "MUTATION_POLICY_UNAVAILABLE"),
])
def test_rejected_adoption_never_changes_flow_or_uses_other_session(mode, code):
    api = FakeAPI(cart(1))
    runtime = runtime_with_page(api)
    runner = WebsiteSmokeV1Runner(runtime, {"base_url": BASE}, "desktop", mutation_policy=policy(api))
    original = runner.flow
    page = Page(runtime.context)
    if mode == "closed":
        page.close()
    elif mode == "foreign":
        page.context = SimpleNamespace(request=FakeAPI(cart(99)))
    elif mode == "unknown":
        class UnknownPage(Page):
            def is_closed(self):
                raise RuntimeError(SECRET)
        page = UnknownPage(runtime.context)
    elif mode == "missing_api":
        runner._api_request = None
    elif mode == "changed_api":
        runtime.context.request = FakeAPI(cart(99))
    else:
        runner._mutation_policy = None
    with pytest.raises(FlowError) as error:
        runner._adopt_business_page(page)
    assert error.value.category == "BUSINESS_PAGE_ADOPTION_REJECTED"
    assert str(error.value) == code and SECRET not in str(error.value)
    assert runner.flow is original and runner.page is runtime.page
    assert not runner._extra_pages and not api.calls


@pytest.mark.parametrize("mode", ["pass", "case_fail", "clear_fail", "denied", "headers", "residual", "disposed"])
def test_search_new_page_to_checkout_cleanup_through_production_cli(monkeypatch, tmp_path, capsys, mode):
    monkeypatch.setattr(harness, "make_runtime", runtime_with_page)
    monkeypatch.setattr("pages.product_page.ProductPage.get_title", lambda _self: "Offline product")
    original_case = WebsiteSmokeV1Runner._run_case
    adopted = []

    def run_case(self, case_id, name, deps, action):
        if case_id == "WSMOKE-SEARCH-03":
            page = Page(self.runtime.context)
            self.state["search"] = SimpleNamespace(open_result=lambda _index: page)
            old = self.page
            def search_action():
                detail = self._c_search03()  # Real Search -> new Page -> adoption call chain.
                assert self.flow.api_request is self.runtime.context.request
                assert self.flow.mutation_policy is self._mutation_policy
                assert self.flow.page is page
                old.close()
                adopted.append(self)
                return detail
            action = search_action
        if case_id == "WSMOKE-CHECKOUT-01":
            api = self.runtime.context.request
            if mode == "residual":
                api.residual = True
            elif mode == "disposed":
                api.unavailable = True
        return original_case(self, case_id, name, deps, action)

    monkeypatch.setattr(WebsiteSmokeV1Runner, "_run_case", run_case)
    code, data, path, sessions, closed = harness.run_cli(
        monkeypatch, tmp_path, both=True,
        mode=mode if mode not in {"residual", "disposed"} else "pass")
    assert code == (0 if mode == "pass" else 1)
    assert len(adopted) == len(sessions) == len(closed) == 2
    assert sessions[0][1] is not sessions[1][1]
    assert validate_json(path.parent, "website_smoke_v1")
    for vp, runner in zip(data["viewports"], adopted):
        assert next(c for c in vp["cases"] if c["case_id"] == "WSMOKE-SEARCH-03")["status"] == "PASS"
        audit_path = path.parent / vp["cleanup"]["audit_path"]
        assert audit_path.is_file() and not Path(vp["cleanup"]["audit_path"]).is_absolute()
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        assert audit["journeys"] == vp["cleanup"]["journeys"]
        browse = audit["journeys"][-1]
        assert browse["journey"] == "browse"
        expected = "PASS" if mode in {"pass", "case_fail"} else "FAIL" if mode in {"clear_fail", "residual"} else "BLOCKED"
        assert browse["status"] == expected
        assert runner.flow.api_request is runner.runtime.context.request
        assert runner.flow.mutation_policy is runner._mutation_policy
        if expected == "PASS":
            assert browse["postcheck_status"] == "PASS"
            assert runner.runtime.context.request.cart["item_count"] == 0
            records = audit["mutation_summary"]["api_cleanup"]["records"]
            assert len(records) == 3
            assert all(r["viewport"] == vp["viewport"] for r in records)
            assert [r["cleanup_stage"] for r in records] == ["direct", "search", "browse"]
        if mode in {"denied", "headers"}:
            assert not any(c[0] == "POST" for c in runner.runtime.context.request.calls)
    assert SECRET not in json.dumps(data) + capsys.readouterr().out


@pytest.mark.parametrize("missing", ["api", "policy"])
def test_initial_missing_dependency_rejects_adoption_and_cleanup_fails_closed(missing):
    api = FakeAPI(cart(1))
    runtime = runtime_with_page(api)
    if missing == "api":
        runtime.context.request = None
    runner = WebsiteSmokeV1Runner(runtime, {"base_url": BASE}, "desktop",
                                 mutation_policy=None if missing == "policy" else policy(api))
    with pytest.raises(FlowError, match="UNAVAILABLE"):
        runner._adopt_business_page(Page(runtime.context))
    runner._journey = "browse"
    runner._journey_cleanup()
    assert runner.cleanup_status == "BLOCKED"
    assert not any(c[0] == "POST" for c in api.calls)


def test_real_playwright_context_identity_and_foreign_page_rejection():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        other = browser.new_context()
        try:
            original = context.new_page()
            runtime = SimpleNamespace(page=original, context=context, access_policy=None)
            guard = policy()
            runner = WebsiteSmokeV1Runner(runtime, {"base_url": BASE}, "desktop", mutation_policy=guard)
            page = context.new_page()
            runner._adopt_business_page(page)
            original.close()
            assert runner.flow.page is page and not page.is_closed()
            assert runner.flow.api_request is context.request
            assert runner.flow.mutation_policy is guard
            with pytest.raises(FlowError, match="PAGE_CONTEXT_MISMATCH"):
                runner._adopt_business_page(other.new_page())
            assert runner.page is page and runner.flow.api_request is context.request
        finally:
            other.close()
            context.close()
            browser.close()
