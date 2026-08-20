"""购物车抽屉购物 Smoke 入口。

用法：
    python scripts/run_shopping_smoke.py                 # both
    python scripts/run_shopping_smoke.py --viewport desktop
    python scripts/run_shopping_smoke.py --viewport mobile

退出码：0 = 通过，1 = 任一失败，2 = 非法视口。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from flows.shopping_flow import FlowError, ShoppingFlow
from pages.base_page import BasePage
from utils.browser import close_browser, create_browser

ARTIFACT_ROOT = PROJECT_ROOT / "artifacts" / "shopping-smoke"


def save_failure_screenshot(runtime, viewport: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = ARTIFACT_ROOT / stamp / viewport
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "failure.png"
    runtime.page.screenshot(path=str(path))
    return str(path)


def run_viewport(viewport: str) -> tuple[bool, dict, Optional[str]]:
    runtime = create_browser(viewport)
    try:
        site = BasePage.load_site_config()
        flow = ShoppingFlow(
            runtime.page, site, viewport, access_policy=runtime.access_policy
        )
        result = flow.run()
        return True, result, None
    except FlowError as exc:
        shot = None
        try:
            shot = save_failure_screenshot(runtime, viewport)
        except Exception:
            pass
        return False, {"category": exc.category, "message": str(exc)}, shot
    finally:
        # 仅用于失败兜底清理：正常路径已通过 UI 移除
        try:
            runtime.page.request.post("https://mondressy.com/cart/clear.js")
        except Exception:
            pass
        close_browser(runtime)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="购物车抽屉购物 Smoke")
    parser.add_argument("--viewport", choices=["desktop", "mobile", "both"], default="both")
    args = parser.parse_args(argv)  # argparse exits 2 on invalid choice

    viewports = ["desktop", "mobile"] if args.viewport == "both" else [args.viewport]
    print("=== Cart Drawer Shopping Smoke ===")
    ok_all = True
    for vp in viewports:
        print()
        print(f"[{vp.title()}]")
        ok, result, shot = run_viewport(vp)
        ok_all = ok_all and ok
        if ok:
            print(f"  PLP load                 PASS")
            print(f"  Product open             PASS  index={result['used_index']}")
            print(f"  Product: {result['product_title'][:60]}")
            print(f"  Color: {result['selected_color']}")
            print(f"  Size: {result['selected_size']}")
            print(f"  Add To Cart              PASS")
            print(f"  Cart Drawer Open         PASS")
            print(f"  Cart: {result['cart_title'][:60]}")
            print(f"  Color: {result['cart_color']}")
            print(f"  Size: {result['cart_size']}")
            print(f"  Quantity: {result['cart_quantity']}")
            print(f"  Product Consistency      PASS")
            print(f"  Color Consistency        PASS")
            print(f"  Size Consistency         PASS")
            print(f"  Quantity                 PASS")
            print(f"  Remove                   PASS")
            print(f"  Cart Empty               PASS")
            print(f"  Cleanup                  PASS")
        else:
            print(f"  FAIL  category={result['category']}")
            print(f"  {result['message']}")
            if shot:
                print(f"  screenshot: {shot}")

    print()
    print(f"购物车抽屉购物 Smoke: {'PASS' if ok_all else 'FAIL'}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
