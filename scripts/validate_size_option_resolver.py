"""离线验证多 PDP Size Model Resolver 契约。"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pages.product_page import ProductPage, PurchaseAreaReadinessError
from pages.size_option_resolver import (
    SIZE_MODEL_01,
    SIZE_MODEL_02,
    SIZE_MODEL_03,
    SizeOptionResolver,
)


def check(ok: bool, label: str) -> bool:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    return ok


def product_config() -> dict:
    return {
        "base_url": "https://shop.test",
        "pages": {
            "product": {
                "url": "/products/test",
                "size_resolver": {
                    "models": [
                        {
                            "id": SIZE_MODEL_01,
                            "group_selector": ".sizeoption[role='group']",
                            "option_selector": (
                                "input[type='radio'][name='properties[Size]']"
                            ),
                            "wait_option_selector": (
                                "input[type='radio'][name='properties[Size]']"
                                ":not([value=''])"
                            ),
                            "required_attributes": {"role": "group"},
                            "expected_name": "Size",
                            "custom_size_value": "Free Custom Size",
                        },
                        {
                            "id": SIZE_MODEL_02,
                            "group_selector": (
                                "fieldset[name='Size'][data-handle='size']"
                            ),
                            "option_selector": (
                                "input[type='radio'][name='Size']"
                                "[data-variant-input]"
                            ),
                            "wait_option_selector": (
                                "input[type='radio'][name='Size']"
                                "[data-variant-input]"
                            ),
                            "required_attributes": {
                                "name": "Size",
                                "data-handle": "size",
                            },
                            "expected_name": "Size",
                            "disabled_class_tokens": ["disabled"],
                        },
                    ],
                    "custom_measurement": {
                        "id": SIZE_MODEL_03,
                        "trigger_value": "Free Custom Size",
                        "field_selector": "input[type='text']",
                        "field_names": [
                            "Bust",
                            "Waist",
                            "Hips",
                            "Hollow to Floor",
                            "Height",
                        ],
                    },
                },
                "selectors": {
                    "purchase_area": {"by": "css", "value": "#purchase"},
                    "title": {"by": "css", "value": "#title"},
                    "color": {"by": "css", "value": "#colors"},
                    "add_to_cart": {"by": "css", "value": "#atc"},
                },
            }
        },
    }


def shell(size_markup: str, *, atc_disabled: bool = False) -> str:
    disabled = " disabled" if atc_disabled else ""
    return (
        '<form id="purchase">'
        '<h1 id="title">Synthetic Product</h1>'
        '<fieldset id="colors"><input type="radio" value="Black"></fieldset>'
        f'<button id="atc" type="button"{disabled}>Add to cart</button>'
        "</form>"
        f'<div id="size-host">{size_markup}</div>'
    )


def spb_group(values: list[str], *, custom_fields: bool = False) -> str:
    options = [
        '<label style="display:none">'
        '<input type="radio" form="purchase" '
        'name="properties[Size]" value=""></label>'
    ]
    options.extend(
        '<label>'
        '<input type="radio" form="purchase" name="properties[Size]" '
        f'value="{value}">{value}</label>'
        for value in values
    )
    fields = ""
    if custom_fields:
        fields = "".join(
            '<input type="text" disabled style="display:none" '
            f'placeholder="{name} (inch)">'
            for name in ("Bust", "Waist", "Hips", "Hollow to Floor", "Height")
        )
    return (
        '<div id="spb-root">'
        '<div class="my-infiniteoptions-container sizeoption optionpadding" '
        'role="group" aria-labelledby="Size-0-0">'
        '<label id="Size-0-0">Size</label>'
        f"{''.join(options)}"
        "</div>"
        f"{fields}</div>"
    )


def native_group(
    values: list[str],
    *,
    checked: str | None = None,
    class_disabled: set[str] | None = None,
    form_id: str = "purchase",
) -> str:
    disabled_values = class_disabled or set()
    options = []
    for value in values:
        checked_attr = " checked" if value == checked else ""
        disabled_class = " disabled" if value in disabled_values else ""
        option_id = f"size-{form_id}-{value}"
        options.append(
            '<div class="variant-input" data-index="option2" '
            f'data-value="{value}">'
            f'<input type="radio" form="{form_id}" name="Size" '
            f'value="{value}" data-index="option2" data-variant-input '
            f'class="{disabled_class} label03" id="{option_id}"{checked_attr}>'
            f'<label for="{option_id}" class="label05 variant__button-label">'
            f"{value}</label></div>"
        )
    return (
        '<fieldset class="label02 variant-input-wrap" name="Size" '
        'data-index="option2" data-handle="size">'
        '<legend class="hide">Size</legend>'
        f"{''.join(options)}</fieldset>"
    )


def resolver(page) -> SizeOptionResolver:
    product = ProductPage(page, product_config(), "mobile")
    return SizeOptionResolver(page, product.page_config(), product.purchase_area)


def validate_model_01(page) -> dict[str, bool]:
    results = {
        "detect": False,
        "count": False,
        "placeholder": False,
        "custom": False,
        "custom_fields": False,
        "normal_selection": False,
        "custom_selection_blocked": False,
    }
    page.set_content(shell(spb_group(["2", "4", "6", "Free Custom Size"], custom_fields=True)))
    product = ProductPage(page, product_config(), "mobile")
    size = resolver(page)
    group = size.require_group()
    options = size.options()
    results["detect"] = group.model == SIZE_MODEL_01
    results["count"] = product.available_size_count() == 4
    results["placeholder"] = len(options) == 4 and all(option.value for option in options)
    metadata = size.snapshot()
    results["custom"] = all(
        (
            sum(option.custom_size for option in options) == 1,
            metadata["custom_measurement_model"] == SIZE_MODEL_03,
        )
    )
    fields = size.measurement_fields()
    results["custom_fields"] = len(fields) == 5 and all(
        field.is_disabled() and not field.is_visible() for field in fields
    )
    selected = product.select_size("4")
    results["normal_selection"] = selected == "4" and product.get_selected_size() == "4"
    try:
        product.select_size("Free Custom Size")
    except RuntimeError:
        results["custom_selection_blocked"] = True
    return results


def validate_model_02(page) -> dict[str, bool]:
    results = {
        "detect": False,
        "count": False,
        "initial_selected": False,
        "selection": False,
        "class_disabled": False,
        "scope": False,
    }
    fbt = '<form id="fbt"></form>' + native_group(["XS"], checked="XS", form_id="fbt")
    main = native_group(
        ["S", "M", "L", "6Y"], checked="S", class_disabled={"6Y"}
    )
    page.set_content(shell(fbt + main))
    product = ProductPage(page, product_config(), "mobile")
    size = resolver(page)
    group = size.require_group()
    options = size.options()
    results["detect"] = group.model == SIZE_MODEL_02
    results["scope"] = all(option.value != "XS" for option in options)
    results["count"] = product.available_size_count() == 3
    results["initial_selected"] = product.get_selected_size() == "S"
    disabled = next(option for option in options if option.value == "6Y")
    results["class_disabled"] = not disabled.available and not disabled.locator.is_disabled()
    selected = product.select_size("M")
    results["selection"] = selected == "M" and product.get_selected_size() == "M"
    return results


def validate_one_size(page) -> bool:
    page.set_content(shell(native_group(["One-Size"], checked="One-Size")))
    product = ProductPage(page, product_config(), "mobile")
    return all(
        (
            product.available_size_count() == 1,
            product.first_available_size() == "One-Size",
            product.select_size() == "One-Size",
            product.get_selected_size() == "One-Size",
        )
    )


def validate_rerender_and_switch(page) -> dict[str, bool]:
    results = {"rerender": False, "model_switch": False}
    page.set_content(shell(spb_group(["2"])))
    product = ProductPage(page, product_config(), "mobile")
    size = resolver(page)
    first_group = size.require_group().model

    page.set_content(shell(spb_group(["4", "6", "8"])))
    results["rerender"] = all(
        (
            first_group == SIZE_MODEL_01,
            product.available_size_count() == 3,
            product.select_size("6") == "6",
            product.get_selected_size() == "6",
        )
    )

    page.set_content(shell(native_group(["S", "M", "L"], checked="S")))
    results["model_switch"] = all(
        (
            size.require_group().model == SIZE_MODEL_02,
            product.available_size_count() == 3,
            product.select_size("M") == "M",
            product.get_selected_size() == "M",
        )
    )
    return results


def validate_missing_size(page) -> bool:
    page.set_content(shell(""))
    product = ProductPage(page, product_config(), "mobile")
    if product.available_size_count() != 0:
        return False
    try:
        product.wait_purchase_ready(timeout_ms=250)
    except PurchaseAreaReadinessError as exc:
        detail = str(exc)
        return "size_count_final=0" in detail and "size_group_detected=False" in detail
    return False


def validate_current_failure(page) -> bool:
    page.set_content(shell(native_group(["S", "M", "L"], checked="S")))
    product = ProductPage(page, product_config(), "mobile")
    size = resolver(page)
    return all(
        (
            size.require_group().model == SIZE_MODEL_02,
            product.available_size_count() == 3,
            product.first_available_size() in {"M", "L"},
        )
    )


def main() -> int:
    ok = True
    playwright = None
    browser = None
    try:
        playwright = sync_playwright().start()
        browser = playwright.webkit.launch(headless=True)
        page = browser.new_page()

        model_01 = validate_model_01(page)
        for name, passed in model_01.items():
            ok = check(passed, f"MODEL_01: {name}") and ok

        model_02 = validate_model_02(page)
        for name, passed in model_02.items():
            ok = check(passed, f"MODEL_02: {name}") and ok

        ok = check(validate_one_size(page), "MODEL_02: One-Size") and ok

        dom = validate_rerender_and_switch(page)
        ok = check(dom["rerender"], "DOM rerender uses current MODEL_01") and ok
        ok = check(dom["model_switch"], "DOM model switch re-detects MODEL_02") and ok

        ok = check(validate_missing_size(page), "Persistent missing Size fails") and ok
        ok = check(
            validate_current_failure(page),
            "MON8040196W-shaped native S/M/L regression",
        ) and ok
    except Exception as exc:  # noqa: BLE001 — compact offline diagnostic
        print(f"  FAIL  Size resolver validator: {type(exc).__name__}: {exc}")
        ok = False
    finally:
        if browser is not None:
            browser.close()
        if playwright is not None:
            playwright.stop()

    print(f"Size Option Resolver Validation: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
