import pytest

from tests.website_smoke_v1_cases import checkout_variant_matches


@pytest.mark.parametrize("variant,color,size,expected", [
    ("Aqua Blue Size: 16W", "Aqua Blue", "16W", (True, True)),
    ("Aqua BlueSize: 16W", "Aqua Blue", "16W", (True, True)),
    ("Aqua BlueSize: 16W", "Aqua", "16W", (False, False)),
    ("Aqua BlueSize: 16W", "Aqua Blue", "16", (True, False)),
    ("Oversize: 2", "Over", "2", (False, False)),
    ("Pink / 2", "Pink", "2", (True, True)),
    ("Dark Pink Size: 20", "Pink", "2", (False, False)),
    ("Pink Size: 20", "Pink", "2", (True, False)),
    ("Pink", "Pink", "2", (False, False)),
])
def test_checkout_option_values_are_compared_as_complete_values(variant, color, size, expected):
    assert checkout_variant_matches(variant, color, size) == expected
