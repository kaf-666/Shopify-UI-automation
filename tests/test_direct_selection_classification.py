from types import SimpleNamespace

import pytest

from flows.shopping_flow import FlowError
from tests.website_smoke_v1_cases import WebsiteSmokeV1Runner


@pytest.mark.parametrize("failing_step,category", [
    ("select_color", "DIRECT_PDP_COLOR_SELECTION_FAILURE"),
    ("select_size", "DIRECT_PDP_SIZE_SELECTION_FAILURE"),
    ("add_to_cart", "DIRECT_PDP_ADD_TO_CART_FAILURE"),
])
def test_direct_selection_reports_the_failing_operation(failing_step, category):
    def fail():
        raise TimeoutError("synthetic timeout")

    flow = SimpleNamespace(select_color=lambda: "Pink", select_size=lambda: "2",
                           add_to_cart=lambda: None, capture_cart_state=lambda: None)
    setattr(flow, failing_step, fail)
    runner = object.__new__(WebsiteSmokeV1Runner)
    runner.flow = flow
    with pytest.raises(FlowError) as error:
        runner._c_direct02()
    assert error.value.category == category
