import pytest

from utils.errors import sanitize_message
from utils.result import CaseResult


@pytest.mark.parametrize("text", [
    "url=https://store.invalid/products/example?opaque=SECRET&variant=1",
    "Page.goto: https://store.invalid/?opaque=SECRET failed",
    "navigation to 'https://store.invalid/?opaque=SECRET'",
    'navigation to "https://store.invalid/?opaque=SECRET"',
    "url=https://store.invalid/?opaque=SECRET_TRUNCATED",
    "url=https://store.invalid/products/example#SECRET",
])
def test_url_query_values_are_removed_before_console_and_json(text):
    safe = sanitize_message(text)
    assert "SECRET" not in safe and "https://store.invalid/" in safe
    result = CaseResult("RSMOKE-DIRECT-01", "Synthetic", "FAIL", "", "", 1, detail=text)
    assert "SECRET" not in result.detail and "SECRET" not in str(result.to_dict())
    assert sanitize_message(safe) == safe


def test_query_redaction_preserves_safe_endpoint_diagnostics():
    text = 'code=REDIRECT_ORIGIN_DENIED source_path=/products/example query_present=true status=302'
    assert sanitize_message(text) == text
