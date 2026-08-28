"""Website Smoke Readonly V1 的离线契约测试。"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from tests.website_smoke_readonly_v1_cases import (
    READONLY_CASE_IDS,
    READONLY_JOURNEY_CASES,
    WebsiteSmokeReadonlyV1Runner,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
READONLY_CASE_FILE = PROJECT_ROOT / "tests" / "website_smoke_readonly_v1_cases.py"


def _runner() -> WebsiteSmokeReadonlyV1Runner:
    return WebsiteSmokeReadonlyV1Runner(
        SimpleNamespace(page=object()),
        {},
        "desktop",
    )


def test_readonly_case_registry_has_exact_contract_and_order() -> None:
    runner = _runner()
    assert tuple(runner._cases) == READONLY_CASE_IDS
    assert len(runner._cases) == 11
    assert len(set(runner._cases)) == 11


def test_readonly_journey_counts_and_case_order() -> None:
    runner = _runner()
    assert tuple(runner._journey_cases("direct")) == READONLY_JOURNEY_CASES["direct"]
    assert tuple(runner._journey_cases("search")) == READONLY_JOURNEY_CASES["search"]
    assert tuple(runner._journey_cases("browse")) == READONLY_JOURNEY_CASES["browse"]
    assert len(READONLY_JOURNEY_CASES["direct"]) == 2
    assert len(READONLY_JOURNEY_CASES["search"]) == 4
    assert len(READONLY_JOURNEY_CASES["browse"]) == 5


def test_readonly_dependencies_do_not_cross_journeys() -> None:
    runner = _runner()
    journey_by_case = {
        case_id: journey
        for journey, case_ids in READONLY_JOURNEY_CASES.items()
        for case_id in case_ids
    }
    for case_id, (_name, dependencies, journey, _fn) in runner._cases.items():
        assert journey_by_case[case_id] == journey
        assert all(journey_by_case[dependency] == journey for dependency in dependencies)


def test_readonly_case_implementation_has_no_mutating_calls_or_checkout() -> None:
    source = READONLY_CASE_FILE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden_call_attributes = {
        "add_to_cart",
        "pre_clean_cart",
        "cleanup_cart",
        "remove_and_wait_empty",
    }
    calls = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert not forbidden_call_attributes.intersection(calls)
    assert "CheckoutPage" not in source
    assert "/cart/add" not in source
    assert "/cart/change" not in source
    assert "/cart/update" not in source
    assert "/cart/clear" not in source


class _FakeButton:
    def __init__(self) -> None:
        self.click_calls = 0

    def count(self) -> int:
        return 1

    def is_visible(self) -> bool:
        return True

    def is_enabled(self) -> bool:
        return True

    def click(self) -> None:
        self.click_calls += 1
        raise AssertionError("Readonly ATC check must not click")


class _FakeProduct:
    def __init__(self) -> None:
        self.button = _FakeButton()
        self.readiness_calls = 0

    def get_title(self) -> str:
        return "Example Dress"

    def get_price(self) -> str:
        return "$99"

    def wait_purchase_ready(self):
        self.readiness_calls += 1
        return 2, 3, True

    def get_selected_color(self) -> str:
        return "Black"

    def get_selected_size(self) -> str:
        return "M"

    def add_to_cart_button(self):
        return self.button


def test_pdp02_only_reads_atc_state_without_clicking() -> None:
    runner = _runner()
    product = _FakeProduct()
    runner.state["browse_prod"] = product

    detail = runner._c_pdp02()

    assert "atc_locator=True" in detail
    assert "atc_visible=True" in detail
    assert "atc_enabled=True" in detail
    assert product.button.click_calls == 0
    assert product.readiness_calls == 1
