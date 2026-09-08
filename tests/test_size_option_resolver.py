"""Synthetic current-DOM convergence tests for the shared size resolver."""

from __future__ import annotations

import time

import pytest
from playwright.sync_api import sync_playwright

import pages.size_option_resolver as resolver_module
from pages.size_option_resolver import SizeOptionResolver


@pytest.fixture(scope="module")
def browser():
    playwright = sync_playwright().start()
    instance = playwright.chromium.launch(headless=True)
    try:
        yield instance
    finally:
        instance.close()
        playwright.stop()


@pytest.fixture
def page(browser):
    current = browser.new_page()
    try:
        yield current
    finally:
        current.close()


def _config(*, control_strategy: str = "associated_label") -> dict:
    return {
        "size_resolver": {
            "models": [
                {
                    "id": "SYNTHETIC_SIZE_RERENDER",
                    "group_selector": "#sizes",
                    "option_selector": (
                        "input[type='radio'][name='properties[Size]']"
                    ),
                    "wait_option_selector": (
                        "input[type='radio'][name='properties[Size]']"
                    ),
                    "control_strategy": control_strategy,
                    "required_attributes": {},
                    "custom_size_value": "Custom Size",
                    "disabled_class_tokens": ["disabled", "unavailable"],
                }
            ]
        }
    }


def _resolver(page, *, control_strategy: str = "associated_label"):
    return SizeOptionResolver(
        page,
        _config(control_strategy=control_strategy),
        lambda: page.locator("#purchase"),
    )


def _size_markup(*, mode: str, hidden: bool = True) -> str:
    hidden_attribute = " hidden" if hidden else ""
    return f"""
        <form id="purchase">
          <div id="sizes">
            <label for="size-s">S</label>
            <input id="size-s" type="radio" name="properties[Size]"
                   value="S"{hidden_attribute}>
            <label for="size-m">M</label>
            <input id="size-m" type="radio" name="properties[Size]"
                   value="M"{hidden_attribute}>
          </div>
        </form>
        <script>
          window.__selection_clicks = 0;
          const purchase = document.querySelector('#purchase');
          const scenario = {mode!r};

          function groupMarkup(selected) {{
            const checkedS = selected === 'S' ? ' checked' : '';
            const checkedM = selected === 'M' ? ' checked' : '';
            const hidden = {hidden_attribute!r};
            return `<div id="sizes">
              <label for="size-s">S</label>
              <input id="size-s" type="radio" name="properties[Size]"
                     value="S" ${{hidden}}${{checkedS}}>
              <label for="size-m">M</label>
              <input id="size-m" type="radio" name="properties[Size]"
                     value="M" ${{hidden}}${{checkedM}}>
            </div>`;
          }}

          function replaceGroup(selected) {{
            document.querySelector('#sizes').outerHTML = groupMarkup(selected);
          }}

          purchase.addEventListener('click', (event) => {{
            const label = event.target.closest('label[for]');
            if (!label) return;
            window.__selection_clicks += 1;
            if (scenario === 'preserve') {{
              setTimeout(() => replaceGroup('S'), 0);
            }} else if (scenario === 'lose-once') {{
              const selected = window.__selection_clicks === 1 ? null : 'S';
              setTimeout(() => replaceGroup(selected), 0);
            }} else if (scenario === 'persistent-loss') {{
              setTimeout(() => replaceGroup(null), 0);
            }}
          }});
        </script>
    """


def _click_count(page) -> int:
    return int(page.evaluate("window.__selection_clicks"))


def test_hidden_radio_selection_waits_for_replaced_group_with_state(page) -> None:
    page.set_content(_size_markup(mode="preserve", hidden=True))
    resolver = _resolver(page)

    assert resolver.select("S") == "S"
    assert resolver.selected_value() == "S"
    assert _click_count(page) == 1


def test_lost_state_retries_fresh_current_control_at_most_once(page, monkeypatch) -> None:
    page.set_content(_size_markup(mode="lose-once", hidden=True))
    monkeypatch.setattr(resolver_module, "SIZE_SELECTION_CONVERGENCE_TIMEOUT_MS", 250)
    resolver = _resolver(page)

    assert resolver.select("S") == "S"
    assert resolver.selected_value() == "S"
    assert _click_count(page) == 2


def test_persistent_state_loss_is_bounded_and_diagnostic(page, monkeypatch) -> None:
    page.set_content(_size_markup(mode="persistent-loss", hidden=True))
    monkeypatch.setattr(resolver_module, "SIZE_SELECTION_CONVERGENCE_TIMEOUT_MS", 150)
    resolver = _resolver(page)

    started = time.monotonic()
    with pytest.raises(RuntimeError) as exc_info:
        resolver.select("S")
    elapsed = time.monotonic() - started

    message = str(exc_info.value)
    assert elapsed < 3
    assert "requested_size=S" in message
    assert "selected_current_dom=NONE" in message
    assert "attempts=2" in message
    assert _click_count(page) == 2


@pytest.mark.parametrize(
    ("hidden", "control_strategy"),
    [(False, "radio"), (True, "associated_label")],
)
def test_no_rerender_visible_and_hidden_controls_regress(page, hidden, control_strategy):
    page.set_content(_size_markup(mode="none", hidden=hidden))
    resolver = _resolver(page, control_strategy=control_strategy)

    assert resolver.select("S") == "S"
    assert resolver.selected_value() == "S"
    if hidden:
        assert _click_count(page) == 1


def test_disabled_aria_disabled_and_custom_size_are_not_selected(page) -> None:
    page.set_content(
        """
        <form id="purchase">
          <div id="sizes">
            <label for="size-s">S</label>
            <input id="size-s" type="radio" name="properties[Size]" value="S" hidden>
            <label for="size-custom">Custom Size</label>
            <input id="size-custom" type="radio" name="properties[Size]"
                   value="Custom Size" hidden>
            <label for="size-disabled">L</label>
            <input id="size-disabled" type="radio" name="properties[Size]"
                   value="L" aria-disabled="true" hidden>
          </div>
        </form>
        """
    )
    resolver = _resolver(page)

    assert [option.value for option in resolver.available_options()] == [
        "S",
        "Custom Size",
    ]
    with pytest.raises(RuntimeError, match="Custom Size"):
        resolver.select("Custom Size")
    with pytest.raises(LookupError, match="L"):
        resolver.select("L")
