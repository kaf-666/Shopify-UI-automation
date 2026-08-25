"""页面对象双端验证脚本。

通过真实框架 Browser Manager（utils/browser.py）在两种视口下
验证全部页面对象能力。不执行加购、不改购物车、不跑购物流程。

用法：
    python scripts/validate_page_objects.py                 # both
    python scripts/validate_page_objects.py --viewport desktop
    python scripts/validate_page_objects.py --viewport mobile
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pages.base_page import BasePage
from pages.cart_page import CartPage
from pages.collection_page import CollectionPage
from pages.home_page import HomePage
from pages.product_page import ProductPage
from utils.browser import close_browser, create_browser
from utils.suite_runner import guarded_main


class Check:
    def __init__(self, name: str, ok: bool, detail: str = ""):
        self.name = name
        self.ok = ok
        self.detail = detail


def run_viewport(viewport: str) -> Tuple[List[Check], dict]:
    checks: List[Check] = []
    runtime = create_browser(viewport)
    try:
        page = runtime.page
        site = runtime.site_config or BasePage.load_site_config(site_name=runtime.site_name)

        # ------------------------------------------------------------- 首页
        home = HomePage(page, site, viewport)
        home.open()
        checks += [
            Check("open", True, page.url),
            Check("logo", home.logo().is_visible()),
            Check("search", home.search_trigger().is_visible(), f"selector={home.resolve_selector('search')['value']}"),
            Check("cart", home.cart_trigger().is_visible()),
        ]

        # --------------------------------------------------------- 商品列表
        coll = CollectionPage(page, site, viewport)
        coll.open()
        count = coll.product_count()
        checks += [
            Check("product_grid", coll.product_grid().is_visible()),
            Check("product_count", count > 0, f"count={count}"),
            Check("filter", coll.filter_control().is_visible()),
            Check("sort", coll.sort_control().is_visible()),
        ]

        # ---------------------------------------------------------- 商品详情
        prod = ProductPage(page, site, viewport)
        prod.open()
        title = prod.get_title()
        price = prod.get_price()
        color_n = prod.color_options().count()
        size_n = prod.available_size_count()
        atc = prod.add_to_cart_button()
        checks += [
            Check("title", bool(title), f"value={title[:50]}"),
            Check("price", bool(price), f"value={price}"),
            Check("gallery", prod.gallery().is_visible()),
            Check("color_options", color_n > 1, f"count={color_n}"),
            Check("size_options", size_n > 1, f"count={size_n}"),
            Check("add_to_cart_visible", atc.is_visible()),
            Check("add_to_cart_enabled", atc.is_enabled()),
        ]

        color_val = size_val = None
        try:
            color_val = prod.select_color()
            state = prod.get_selected_color()
            checks.append(Check("select_color", color_val == state, f"selected={color_val} checked_state={state}"))
        except Exception as exc:
            checks.append(Check("select_color", False, f"{type(exc).__name__}: {exc}"))
        try:
            size_val = prod.select_size()
            state = prod.get_selected_size()
            checks.append(Check("select_size", size_val == state, f"selected={size_val} checked_state={state}"))
        except Exception as exc:
            checks.append(Check("select_size", False, f"{type(exc).__name__}: {exc}"))

        # ------------------------------------------------------------ 购物车
        cart = CartPage(page, site, viewport)
        cart.open()
        items = cart.cart_items().count()
        qty = cart.quantity_inputs().count()
        rem = cart.remove_links().count()
        checks += [
            Check("drawer", cart.drawer().count() >= 1),
            Check("empty_state", items == 0, f"cart_items={items} qty_inputs={qty} remove_links={rem}"),
        ]

        info = {
            "title": title, "price": price,
            "selected_color": color_val, "selected_size": size_val,
            "cart_items": items,
        }
        return checks, info
    finally:
        close_browser(runtime)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="页面对象双端验证")
    parser.add_argument("--viewport", choices=["desktop", "mobile", "both"], default="both")
    args = parser.parse_args(argv)  # argparse exits 2 on invalid choice

    print("=== Page Object Validation ===")
    viewports = ["desktop", "mobile"] if args.viewport == "both" else [args.viewport]
    ok_all = True
    infos = {}
    for vp in viewports:
        print()
        checks, info = run_viewport(vp)
        infos[vp] = info
        print(f"[{vp.title()}]")
        for c in checks:
            ok_all = ok_all and c.ok
            line = f"  {c.name:<18} {'PASS' if c.ok else 'FAIL'}"
            if c.detail:
                line += f"  {c.detail}"
            print(line)

    print()
    print("=== PDP Interaction ===")
    for vp in viewports:
        i = infos[vp]
        print(f"[{vp.title()}]")
        print(f"  Selected Color: {i['selected_color']}")
        print(f"  Selected Size:  {i['selected_size']}")
        print(f"  Cart Items:     {i['cart_items']}")
    print()
    print(f"Cart Mutation Performed: NO")
    print()
    print(f"页面对象基础验证: {'PASS' if ok_all else 'FAIL'}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(guarded_main(main))
