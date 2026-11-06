from types import SimpleNamespace

import pytest

from utils.browser import close_browser


def test_close_browser_propagates_runtime_teardown_failure():
    def fail():
        raise RuntimeError("synthetic close failure")

    with pytest.raises(RuntimeError, match="synthetic close failure"):
        close_browser(SimpleNamespace(close=fail))


def test_close_browser_allows_missing_runtime():
    close_browser(None)
