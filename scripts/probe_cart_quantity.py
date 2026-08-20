"""Cart Drawer 数量控件探测脚本。

对购物车抽屉的数量调整控件做只读 DOM 探索，探测内容：
    +/- 按钮 / 数量输入框 / 初始值 / Subtotal / 更新请求方式 /
    DOM 重绘行为 / 加载状态。

通过真实 Browser Manager（create_browser）+ ShoppingFlow 建立
真实购物车状态（复用已验证的前置步骤，不重新实现 ATC）。

网络 endpoint 仅用于理解交互机制，不输出任何请求头 / 签名值。

用法：
    python scripts/probe_cart_quantity.py                # both
    python scripts/probe_cart_quantity.py --viewport desktop
    python scripts/probe_cart_quantity.py --viewport mobile
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from flows.shopping_flow import ShoppingFlow
from pages.cart_drawer import CartDrawer
from utils.browser import close_browser, create_browser

JS_QTY_CONTROLS = r"""
(() => {
  const item = document.querySelector('#CartDrawer .cart__item');
  if (!item) return { item: 'NONE' };
  const visible = el => {
    const r = el.getBoundingClientRect();
    return el.offsetParent !== null && r.width > 0 && r.height > 0;
  };
  const qty_wrapper = item.querySelector('.js-qty__wrapper, [class*="qty"]');
  const input = item.querySelector('input[name="updates[]"], input.js-qty__num');
  const buttons = [...item.querySelectorAll('button')].map(b => ({
    cls: String(b.className).slice(0, 80),
    aria: b.getAttribute('aria-label') || '',
    title: b.getAttribute('title') || '',
    text: (b.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 20),
    disabled: b.disabled,
    visible: visible(b),
  }));
  return {
    wrapper_cls: qty_wrapper ? String(qty_wrapper.className).slice(0, 80) : null,
    input: input ? {
      type: input.getAttribute('type') || '',
      name: input.getAttribute('name') || '',
      cls: String(input.className).slice(0, 60),
      value: input.value,
      min: input.getAttribute('min') || '',
      max: input.getAttribute('max') || '',
      step: input.getAttribute('step') || '',
      readonly: input.readOnly,
      disabled: input.disabled,
    } : null,
    buttons,
  };
})()
"""

JS_SUBTOTAL = r"""
(() => {
  const d = document.querySelector('#CartDrawer');
  const visible = el => {
    const r = el.getBoundingClientRect();
    return el.offsetParent !== null && r.width > 0 && r.height > 0;
  };
  const rows = [...d.querySelectorAll('[class*="subtotal"], [class*="total"]')]
    .filter(visible)
    .slice(0, 6)
    .map(el => ({
      cls: String(el.className).slice(0, 70),
      text: (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 60),
      price_els: [...el.querySelectorAll('.money, [class*="price"], span')]
        .filter(p => /\$/.test(p.textContent || ''))
        .slice(0, 3)
        .map(p => ({ cls: String(p.className).slice(0, 50), text: (p.textContent || '').trim() })),
    }));
  return rows;
})()
"""

JS_LOADING = r"""
(() => {
  const d = document.querySelector('#CartDrawer');
  const visible = el => {
    const r = el.getBoundingClientRect();
    return el.offsetParent !== null && r.width > 0 && r.height > 0;
  };
  const cands = [...d.querySelectorAll('[class*="loading"], [class*="updating"], [class*="spinner"], [class*="is-loading"]')]
    .slice(0, 6)
    .map(el => ({ cls: String(el.className).slice(0, 70), visible: visible(el) }));
  return cands;
})()
"""


def probe_viewport(viewport: str) -> None:
    """对单个视口执行 Cart Quantity 探测并打印结果。"""
    r = create_browser(viewport)
    page = r.page
    site_cfg = CartDrawer.load_site_config()
    flow = ShoppingFlow(page, site_cfg, viewport, access_policy=r.access_policy)

    print(f"===== Cart Quantity Probe: {viewport.upper()} =====")
    try:
        # ------------------------------------------------------ 前置：建立真实购物车
        flow.pre_clean_cart()
        flow.open_collection()
        flow.open_product()
        flow.validate_purchase_area()
        color = flow.select_color()
        size = flow.select_size()
        flow.capture_pdp_state()
        flow.add_to_cart()
        flow.capture_cart_state()
        print("[cart]", "title=", flow.state["cart_title"][:50], "| qty=", flow.state["cart_qty"])
        print("[cart] pdp_url=", flow.state["pdp_url"][:80])

        drawer = CartDrawer(page, site_cfg, viewport)
        print("[controls]", json.dumps(page.evaluate(JS_QTY_CONTROLS), ensure_ascii=False))
        print("[subtotal]", json.dumps(page.evaluate(JS_SUBTOTAL), ensure_ascii=False))
        print("[loading]", json.dumps(page.evaluate(JS_LOADING), ensure_ascii=False))

        # ------------------------------------------------------ 网络观察（仅 endpoint）
        def on_req(req):
            if "/cart/" in req.url and req.method in ("POST", "PUT", "PATCH"):
                print(f"[net] {req.method} {req.url[:100]}")
        page.on("request", on_req)

        # ------------------------------------------------------ 点击 +（真实 UI）
        plus = drawer._item(0).locator(
            "button.js-qty__adjust--plus, button[aria-label*='increase' i], button[title*='+']"
        ).first
        if plus.count() == 0:
            print("[plus] NOT_FOUND")
        else:
            print("[plus]", json.dumps({
                "cls": plus.get_attribute("class"),
                "aria": plus.get_attribute("aria-label") or "",
                "visible": plus.is_visible(),
            }, ensure_ascii=False))
            qty_before = drawer.get_item_quantity(0)
            plus.click()
            # 等待 input value 变化（状态等待）
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and drawer.get_item_quantity(0) == qty_before:
                page.wait_for_timeout(200)
            page.wait_for_timeout(1500)
            print("[after_plus] qty:", drawer.get_item_quantity(0), "| subtotal:", json.dumps(page.evaluate(JS_SUBTOTAL), ensure_ascii=False))
            print("[after_plus] loading:", json.dumps(page.evaluate(JS_LOADING), ensure_ascii=False))
            print("[after_plus] item_node_same:", page.evaluate(
                "(() => { const d = document.querySelector('#CartDrawer');"
                "return d ? d.querySelectorAll('.cart__item').length : -1; })()"
            ))

            # ------------------------------------------------------ 点击 -（真实 UI）
            minus = drawer._item(0).locator(
                "button.js-qty__adjust--minus, button[aria-label*='decrease' i]"
            ).first
            if minus.count() == 0:
                print("[minus] NOT_FOUND")
            else:
                print("[minus]", json.dumps({
                    "cls": minus.get_attribute("class"),
                    "aria": minus.get_attribute("aria-label") or "",
                    "visible": minus.is_visible(),
                    "disabled": minus.is_disabled(),
                }, ensure_ascii=False))
                minus.click()
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and drawer.get_item_quantity(0) != "1":
                    page.wait_for_timeout(200)
                page.wait_for_timeout(1500)
                print("[after_minus] qty:", drawer.get_item_quantity(0))
                print("[after_minus] subtotal:", json.dumps(page.evaluate(JS_SUBTOTAL), ensure_ascii=False))

        # ------------------------------------------------------ 后置清理（UI Remove 优先）
        try:
            flow.remove_and_wait_empty()
            print("[cleanup] UI remove ok, empty:", drawer.is_empty())
        except Exception as exc:
            print("[cleanup] UI remove failed:", type(exc).__name__, str(exc)[:100])
            try:
                flow.cleanup_cart()
                print("[cleanup] API fallback ok")
            except Exception as exc2:
                print("[cleanup] API fallback failed:", type(exc2).__name__)
    finally:
        close_browser(r)
    print()


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Mondressy Cart Quantity 探测")
    parser.add_argument("--viewport", choices=["desktop", "mobile", "both"], default="both")
    args = parser.parse_args(argv)

    viewports = ["desktop", "mobile"] if args.viewport == "both" else [args.viewport]
    for vp in viewports:
        probe_viewport(vp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
