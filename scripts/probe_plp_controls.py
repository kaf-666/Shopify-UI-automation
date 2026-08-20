"""PLP 控件探测（颜色快捷筛选 / 排序 / 价格）。

对 Wedding Guest Dresses 商品列表页控件做只读 DOM 探索。
通过真实 Browser Manager 运行（自动继承代理与 SiteAccessPolicy）。

用法：
    python scripts/probe_plp_controls.py --viewport desktop
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.browser import close_browser, create_browser

COLLECTION = "https://mondressy.com/collections/wedding-guest-dresses"

JS_FAMILIES = (
    "[...document.querySelectorAll('.color-family-item')]"
    ".map(e => { const m = e.className.match(/family-(\\w+)/); return m ? m[1] : null; })"
    ".filter(Boolean)"
)

JS_RED_BOX = r"""
(() => {
  const box = document.querySelector('.color-list-box.family-Red-colors-box');
  if (!box) return { box: 'NONE' };
  return {
    box_class: box.className,
    box_visible: box.offsetParent !== null,
    options: [...box.querySelectorAll('a.option_circle')].slice(0, 10).map(a => ({
      cls: String(a.className).slice(0, 60),
      href: a.getAttribute('href'),
      vis: a.offsetParent !== null,
    })),
  };
})()
"""

JS_SELECTED = r"""
(() => {
  const fam = document.querySelector('.color-family-item.family-Red');
  const opts = [...document.querySelectorAll('.color-item a.option_circle')];
  const items = [...document.querySelectorAll('.color-item')];
  return {
    family_class: fam ? String(fam.className) : 'NONE',
    selected_option_classes: opts.filter(a => /selected|active/i.test(a.className)).slice(0, 5).map(a => String(a.className).slice(0, 60)),
    aria_state_count: opts.filter(a => a.getAttribute('aria-selected') || a.getAttribute('aria-pressed')).length,
    selected_item_classes: items.filter(e => /selected|active/i.test(e.className)).slice(0, 5).map(e => String(e.className).slice(0, 60)),
  };
})()
"""


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="PLP 控件探测")
    parser.add_argument("--viewport", choices=["desktop", "mobile"], default="desktop")
    args = parser.parse_args(argv)

    r = create_browser(args.viewport)
    try:
        page = r.page
        page.goto(COLLECTION, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3500)
        print(f"VIEWPORT: {args.viewport}")
        print("TITLE:", page.title()[:70])

        families = page.evaluate(JS_FAMILIES)
        print("FAMILIES:", families)

        fam = page.locator(".color-family-item.family-Red").first
        print("RED_FAMILY_VISIBLE:", fam.is_visible() if fam.count() else "N/A")
        if fam.count():
            # 依次尝试 hover（下拉式）与点击变体
            fam.hover()
            page.wait_for_timeout(1200)
            hover_state = page.evaluate(
                "(() => { const b = document.querySelector('.color-list-box.family-Red-colors-box'); return b ? b.offsetParent !== null : null; })()"
            )
            print("RED_BOX_AFTER_HOVER:", hover_state)
            if not hover_state:
                fam.locator(".color-img-box").first.click()
                page.wait_for_timeout(1200)
                click_state = page.evaluate(
                    "(() => { const b = document.querySelector('.color-list-box.family-Red-colors-box'); return b ? b.offsetParent !== null : null; })()"
                )
                print("RED_BOX_AFTER_IMG_CLICK:", click_state)
                if not click_state:
                    fam.locator(".color-name").first.click()
                    page.wait_for_timeout(1200)
                    print(
                        "RED_BOX_AFTER_NAME_CLICK:",
                        page.evaluate(
                            "(() => { const b = document.querySelector('.color-list-box.family-Red-colors-box'); return b ? b.offsetParent !== null : null; })()"
                        ),
                    )
            print("RED_BOX_AFTER_CLICK:", json.dumps(page.evaluate(JS_RED_BOX), ensure_ascii=False))

            first = page.locator(".color-list-box.family-Red-colors-box a.option_circle").first
            print("FIRST_OPTION_HREF:", first.get_attribute("href") if first.count() else "N/A")
            if first.count():
                try:
                    first.click()
                except Exception as exc:
                    print("OPTION_CLICK_FAILED:", type(exc).__name__)
                    # JS 导航回退仅用于探测，正式 Case 不使用：
                    page.goto("https://mondressy.com" + first.get_attribute("href"),
                              wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(4000)
                print("URL_AFTER_COLOR_CLICK:", page.url)
                print("TITLE_AFTER:", page.title()[:70])
                print("SELECTED_STATE:", json.dumps(page.evaluate(JS_SELECTED), ensure_ascii=False))
                print("GRID_COUNT_AFTER_FILTER:", page.locator(".grid-product").count())

        # 排序选项（标签 -> 值）
        sort_opts = page.evaluate(
            "[...document.querySelectorAll('select#SortBy option')].map(o => ({label: o.textContent.trim(), value: o.value}))"
        )
        print("SORT_OPTIONS:", json.dumps(sort_opts, ensure_ascii=False))

        price_opts = page.evaluate(
            "[...document.querySelectorAll('select#price-filter option')].map(o => ({label: o.textContent.trim(), value: o.value}))"
        )
        print("PRICE_OPTIONS:", json.dumps(price_opts, ensure_ascii=False))
        print("PRICE_VISIBLE:", page.locator("select#price-filter").is_visible() if page.locator("select#price-filter").count() else "N/A")
    finally:
        close_browser(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
