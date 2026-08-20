"""Mondressy 选择器探测与双端验证。

读取 configs/sites/mondressy.yaml 中冻结的选择器，
通过真实框架 Browser Manager 在 desktop（Chromium）与
mobile（WebKit + iPhone 14）双端验证。

用法：
    python scripts/probe_mondressy_selectors.py            # 双端
    python scripts/probe_mondressy_selectors.py --viewport desktop
    python scripts/probe_mondressy_selectors.py --viewport mobile
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.browser import (
    PAGE_NAV_TIMEOUT_MS,
    close_browser,
    create_browser,
    resolve_template,
)

SITE_CFG = PROJECT_ROOT / "configs" / "sites" / "mondressy.yaml"
SETTLE_MS = 2500

NOT_OBSERVABLE = "NOT_OBSERVABLE_WITH_EMPTY_CART"


def load_site_config() -> dict:
    if not SITE_CFG.exists():
        raise FileNotFoundError(SITE_CFG)
    with open(SITE_CFG, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict) or not isinstance(data.get("pages"), dict):
        raise ValueError("mondressy.yaml must contain site + pages mapping")
    return data


def resolve_selector(entry, viewport: str) -> Optional[dict]:
    """Selector entries may be flat {by,value} or split {desktop:{...}, mobile:{...}}."""
    if entry is None:
        return None
    if isinstance(entry, dict) and "desktop" in entry and isinstance(entry["desktop"], dict):
        return entry.get(viewport) or entry.get("desktop")
    return entry


def css_count_visible(page, css: str) -> tuple[int, int]:
    loc = page.locator(css)
    count = loc.count()
    visible = 0
    if count:
        for i in range(count):
            try:
                if loc.nth(i).is_visible():
                    visible += 1
            except Exception:
                pass
    return count, visible


def probe_page(runtime, page_cfg: dict, viewport: str, page_name: str) -> List[dict]:
    results = []
    for name, entry in (page_cfg.get("selectors") or {}).items():
        sel = resolve_selector(entry, viewport)
        if sel is None:
            results.append({"name": name, "status": "NO_CONFIG_FOR_VIEWPORT", "count": 0, "visible": 0,
                            "stability": "-", "note": f"no selector configured for {viewport}"})
            continue
        if sel.get("status") == NOT_OBSERVABLE:
            results.append({"name": name, "status": NOT_OBSERVABLE, "count": 0, "visible": 0,
                            "stability": sel.get("stability", "-"),
                            "note": "购物车无商品行：当前阶段禁止产生副作用的操作。"})
            continue
        css = sel.get("value", "")
        count, visible = css_count_visible(runtime.page, css)
        stability = sel.get("stability", "-")
        status = "PASS"
        note = ""
        if name == "product_card":
            ok = count > 1 and visible > 1
            if not ok:
                status = "FAIL"
        elif name == "drawer":
            ok = count >= 1  # drawer may be hidden by default; DOM presence is enough
            if count and not visible:
                note = "hidden by default (DOM present)"
        else:
            ok = count >= 1 and visible >= 1
        if not ok and status != "FAIL":
            status = "FAIL"
        if status == "FAIL":
            note = "count/visible below requirement"
        results.append({"name": name, "status": status, "count": count, "visible": visible,
                        "stability": stability, "note": note})
    return results


def probe_special(runtime, page_name: str) -> dict:
    """Component-type findings per page (desktop/mobile aware)."""
    out = {}
    if page_name == "collection":
        filter_panel = runtime.page.locator(".collection-filter")
        filter_btn = runtime.page.locator("button.js-drawer-open-collection-filters")
        out["filter_type"] = ("desktop inline panel" if filter_panel.count() and filter_panel.first.is_visible()
                              else "drawer trigger")
        out["filter_drawer_trigger_visible"] = bool(filter_btn.count() and filter_btn.first.is_visible())
        out["filter_has_native_select"] = runtime.page.locator(".collection-filter select").count()
        out["sort_type"] = "native select" if runtime.page.locator("select#SortBy").count() else "unknown"
    elif page_name == "product":
        color_radios = runtime.page.locator('fieldset[name="Color"] input[type="radio"]')
        size_radios = runtime.page.locator('input[name="properties[Size]"][type="radio"]')
        out["color_control"] = "radio swatch group"
        out["color_option_count"] = color_radios.count()
        out["color_visible"] = sum(1 for i in range(color_radios.count()) if color_radios.nth(i).is_visible())
        out["color_disabled"] = sum(1 for i in range(color_radios.count()) if color_radios.nth(i).is_disabled())
        out["size_control"] = "SPB radio group"
        out["size_option_count"] = size_radios.count()
        out["size_visible"] = sum(1 for i in range(size_radios.count()) if size_radios.nth(i).is_visible())
        out["size_disabled"] = sum(1 for i in range(size_radios.count()) if size_radios.nth(i).is_disabled())
        btn = runtime.page.locator('form.product-single__form button[name="add"]')
        out["add_to_cart_disabled"] = btn.count() and btn.first.is_disabled()
    elif page_name == "cart":
        out["drawer_dom"] = "present" if runtime.page.locator("#CartDrawer").count() else "absent"
        out["cart_has_items"] = runtime.page.locator(".cart__item, .ajaxcart__product").count() > 0
    return out


def run_viewport(viewport: str, cfg: dict) -> tuple[bool, List[str], dict]:
    base = resolve_template(str(cfg.get("base_url") or ""), "base_url")
    runtime = create_browser(viewport)
    try:
        lines = []
        all_ok = True
        specials = {}
        for page_name, page_cfg in cfg["pages"].items():
            url = base + page_cfg["url"] if not page_cfg["url"].startswith("http") else page_cfg["url"]
            runtime.page.goto(url, wait_until="domcontentloaded", timeout=PAGE_NAV_TIMEOUT_MS)
            runtime.page.wait_for_timeout(SETTLE_MS)
            lines.append(f"[{viewport.title()} / {page_name.title()}]")
            for res in probe_page(runtime, page_cfg, viewport, page_name):
                if res["status"] == "PASS":
                    extra = f" count={res['count']} visible={res['visible']} stability={res['stability']}"
                    if res["note"]:
                        extra += f" ({res['note']})"
                    lines.append(f"  {res['name']:<14} PASS{extra}")
                elif res["status"] == NOT_OBSERVABLE:
                    lines.append(f"  {res['name']:<14} {NOT_OBSERVABLE}  stability={res['stability']} ({res['note']})")
                else:
                    all_ok = False
                    lines.append(f"  {res['name']:<14} {res['status']}  count={res['count']} visible={res['visible']}"
                                 f" stability={res['stability']} ({res['note']})")
            specials[page_name] = probe_special(runtime, page_name)
            lines.append("")
        return all_ok, lines, specials
    finally:
        close_browser(runtime)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Mondressy 选择器探测")
    parser.add_argument("--viewport", choices=["desktop", "mobile", "both"], default="both")
    args = parser.parse_args(argv)

    try:
        cfg = load_site_config()
    except (FileNotFoundError, ValueError, yaml.YAMLError) as exc:
        print(f"Site config load: FAIL ({exc})")
        return 1

    print("=== Mondressy Selector Probe ===")
    print()
    viewports = ["desktop", "mobile"] if args.viewport == "both" else [args.viewport]
    ok_all = True
    all_specials = {}
    for vp in viewports:
        ok, lines, specials = run_viewport(vp, cfg)
        ok_all = ok_all and ok
        all_specials[vp] = specials
        print("\n".join(lines))
    print("=== Component Type Findings ===")
    for vp in viewports:
        print(f"[{vp.title()}]")
        for page, spec in all_specials[vp].items():
            for k, v in spec.items():
                print(f"  {page}.{k}: {v}")
    print()
    print(f"Mondressy 选择器探测: {'PASS' if ok_all else 'FAIL'}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
