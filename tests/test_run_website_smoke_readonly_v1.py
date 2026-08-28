"""Offline tests for Readonly Case/CLI integration."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.run_website_smoke_readonly_v1 as cli
from tests.website_smoke_readonly_v1_cases import (
    READONLY_CASE_IDS,
    WebsiteSmokeReadonlyV1Runner,
)
from utils.readonly_mutation_guard import ReadonlyMutationGuard
from utils.result import CaseResult, iso_now


CLI_FILE = Path(__file__).resolve().parent.parent / "scripts" / "run_website_smoke_readonly_v1.py"


class _FakeRoute:
    def __init__(self, url: str, method: str) -> None:
        self.request = SimpleNamespace(url=url, method=method)
        self.aborted = False

    def abort(self) -> None:
        self.aborted = True


def _pass_results():
    now = iso_now()
    return [
        CaseResult(case_id, case_id, "PASS", now, now, 1)
        for case_id in READONLY_CASE_IDS
    ]


def test_case_pass_is_overridden_by_mutation_violation() -> None:
    guard = ReadonlyMutationGuard()
    runner = WebsiteSmokeReadonlyV1Runner(
        SimpleNamespace(page=object()),
        {},
        "desktop",
        mutation_guard=guard,
    )
    runner._journey = "browse"

    def business_case():
        route = _FakeRoute("https://mondressy.com/cart/add.js", "POST")
        guard._handle_route(route)
        assert route.aborted
        return "business PASS"

    runner._run_case(
        "RSMOKE-PDP-02",
        "Add To Cart Available",
        [],
        business_case,
    )

    result = runner.results["RSMOKE-PDP-02"]
    assert result.status == "FAIL"
    assert result.failure_classification == "READONLY_MUTATION_VIOLATION"
    assert result.detail == "blocked cart mutation: POST /cart/add.js"
    assert len(runner._cases) == 11
    assert guard.violations()[0]["case_id"] == "RSMOKE-PDP-02"


def test_readonly_cli_has_no_direct_cart_or_checkout_calls() -> None:
    source = CLI_FILE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not {
        "add_to_cart",
        "pre_clean_cart",
        "cleanup_cart",
        "cart_state_quantity",
        "remove_and_wait_empty",
    }.intersection(calls)
    assert "CheckoutPage" not in source


class _EmptyGuard:
    def violations(self):
        return []

    def out_of_scope_violations(self):
        return []


class _FakeRunner:
    mutation_guard = _EmptyGuard()


class _OutOfScopeGuard(_EmptyGuard):
    def violations(self):
        return [
            {
                "method": "POST",
                "path": "/cart/add.js",
                "case_id": None,
                "journey": "runtime",
                "viewport": "desktop",
            }
        ]

    def out_of_scope_violations(self):
        return self.violations()


@pytest.mark.parametrize(
    ("requested", "expected_viewports", "expected_total"),
    [
        ("desktop", ["desktop"], 11),
        ("mobile", ["mobile"], 11),
        ("both", ["desktop", "mobile"], 22),
    ],
)
def test_cli_offline_viewport_contract(
    monkeypatch,
    tmp_path,
    requested,
    expected_viewports,
    expected_total,
) -> None:
    artifact_root = tmp_path / "artifacts" / "website-smoke-readonly-v1"
    calls = []

    monkeypatch.setattr(cli, "ARTIFACT_ROOT", artifact_root)
    monkeypatch.setattr(cli, "make_run_id", lambda: "offline")
    monkeypatch.setattr(cli, "load_settings", lambda: {"default_site": "mondressy"})
    monkeypatch.setattr(
        cli,
        "load_site_config",
        lambda _site: {"base_url": "https://mondressy.com"},
    )
    monkeypatch.setattr(cli, "resolve_url", lambda value, _field: value)

    def fake_run_viewport(viewport, artifact_dir):
        calls.append((viewport, artifact_dir))
        return _pass_results(), _FakeRunner(), {"viewport": viewport}

    monkeypatch.setattr(cli, "run_viewport", fake_run_viewport)

    assert cli.main(["--viewport", requested]) == 0
    assert [viewport for viewport, _artifact_dir in calls] == expected_viewports

    result_path = artifact_root / "offline" / "results.json"
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert payload["overall_status"] == "PASS"
    assert payload["summary"]["total"] == expected_total
    assert [item["viewport"] for item in payload["viewports"]] == expected_viewports
    assert all(item["summary"]["total"] == 11 for item in payload["viewports"])
    assert all(item["pre_clean"]["detail"] == "readonly_not_required" for item in payload["viewports"])
    assert all(item["cleanup"]["detail"] == "readonly_not_required" for item in payload["viewports"])


def test_cli_result_write_failure_is_nonzero(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(cli, "ARTIFACT_ROOT", tmp_path / "artifacts")
    monkeypatch.setattr(cli, "make_run_id", lambda: "write-failure")
    monkeypatch.setattr(cli, "load_settings", lambda: {"default_site": "mondressy"})
    monkeypatch.setattr(
        cli,
        "load_site_config",
        lambda _site: {"base_url": "https://mondressy.com"},
    )
    monkeypatch.setattr(cli, "resolve_url", lambda value, _field: value)
    monkeypatch.setattr(
        cli,
        "run_viewport",
        lambda _viewport, _artifact_dir: (_pass_results(), _FakeRunner(), {}),
    )

    def fail_write(*_args, **_kwargs):
        raise cli.ResultWriteError("offline write failure")

    monkeypatch.setattr(cli, "write_results_json", fail_write)

    assert cli.main(["--viewport", "desktop"]) == 1


def test_cli_out_of_scope_mutation_is_run_fatal(monkeypatch, tmp_path) -> None:
    artifact_root = tmp_path / "artifacts" / "website-smoke-readonly-v1"
    monkeypatch.setattr(cli, "ARTIFACT_ROOT", artifact_root)
    monkeypatch.setattr(cli, "make_run_id", lambda: "outside-scope")
    monkeypatch.setattr(cli, "load_settings", lambda: {"default_site": "mondressy"})
    monkeypatch.setattr(
        cli,
        "load_site_config",
        lambda _site: {"base_url": "https://mondressy.com"},
    )
    monkeypatch.setattr(cli, "resolve_url", lambda value, _field: value)
    monkeypatch.setattr(
        cli,
        "run_viewport",
        lambda _viewport, _artifact_dir: (
            _pass_results(),
            SimpleNamespace(mutation_guard=_OutOfScopeGuard()),
            {},
        ),
    )

    assert cli.main(["--viewport", "desktop"]) == 1
    payload = json.loads(
        (artifact_root / "outside-scope" / "results.json").read_text(encoding="utf-8")
    )
    assert payload["overall_status"] == "FAIL"
    assert payload["fatal_error"]["classification"] == "READONLY_MUTATION_VIOLATION"
    assert "POST /cart/add.js" in payload["fatal_error"]["message"]
