"""Contracts for checkout summary reads across React DOM replacement."""

from __future__ import annotations

from pages.checkout_page import CheckoutPage


class _Cells:
    def __init__(self, page) -> None:
        self.page = page

    def evaluate_all(self, expression: str):
        assert "querySelectorAll('p')" in expression
        self.page.snapshot_reads += 1
        state = self.page.states[min(self.page.snapshot_reads - 1, len(self.page.states) - 1)]
        return [dict(item) for item in state]

    def count(self):  # pragma: no cover - guards against the removed two-step path
        raise AssertionError("cell count must not precede a descendant text read")

    def nth(self, _index):  # pragma: no cover - guards against stale nth locators
        raise AssertionError("cell nth locator must not survive a React render")


class _Page:
    def __init__(self, states: list[list[dict]]) -> None:
        self.states = states
        self.snapshot_reads = 0
        self.url = "https://shop.example/checkouts/token"

    def locator(self, selector: str):
        assert selector == '[role="cell"]'
        return _Cells(self)


def _checkout(states: list[list[dict]]) -> tuple[CheckoutPage, _Page]:
    page = _Page(states)
    config = {
        "base_url": "https://shop.example",
        "pages": {"checkout": {"selectors": {}}},
    }
    return CheckoutPage(page, config, "mobile"), page


def _summary(title: str = "ALARYA Dress") -> list[dict]:
    return [
        {"text": "Quantity 1", "paragraphs": []},
        {"text": "$179.99", "paragraphs": []},
        {
            "text": f"{title} Aqua Blue Size: 16W",
            "paragraphs": [title, "Aqua Blue Size: 16W"],
        },
    ]


def test_title_is_read_from_one_atomic_live_dom_snapshot() -> None:
    checkout, page = _checkout([_summary()])

    assert checkout.get_product_title() == "ALARYA Dress"
    assert page.snapshot_reads == 1


def test_variant_does_not_requery_title_across_render_boundary() -> None:
    checkout, page = _checkout(
        [
            _summary("Original title"),
            _summary("Replacement title"),
        ]
    )

    assert checkout.get_product_variant() == "Aqua Blue Size: 16W"
    assert page.snapshot_reads == 1


def test_each_getter_requeries_the_current_live_dom() -> None:
    checkout, page = _checkout(
        [
            _summary("First generation"),
            _summary("Second generation"),
        ]
    )

    assert checkout.get_product_title() == "First generation"
    assert checkout.get_product_title() == "Second generation"
    assert page.snapshot_reads == 2


def test_quantity_price_and_readable_count_use_atomic_snapshots() -> None:
    checkout, _page = _checkout([_summary("ALARYA Long Chiffon Mother Dress")])

    assert checkout.get_product_quantity() == "1"
    assert checkout.get_product_price() == "$179.99"
    assert checkout.product_count_readable() == 1


def test_missing_description_paragraph_remains_a_real_failure_signal() -> None:
    checkout, _page = _checkout(
        [[{"text": "unstructured checkout content", "paragraphs": []}]]
    )

    assert checkout.get_product_title() == ""


def test_nonzero_index_is_not_misreported_as_first_product() -> None:
    checkout, _page = _checkout([_summary()])

    assert checkout.get_product_title(1) == ""
    assert checkout.get_product_variant(1) == ""
    assert checkout.get_product_quantity(1) == ""
    assert checkout.get_product_price(1) == ""
