"""Checkout Entry 探测脚本。

对购物车抽屉的标准 Checkout 入口与 Shopify Checkout 落地页做只读探索：
    标准按钮 / 快捷支付存在性 / 点击后导航模式 / Checkout 核心 UI /
    Order Summary / 商品行。

安全边界：只观察与读取；不填写任何字段、不点击支付 / 快捷支付 /
提交订单类控件，绝对不产生订单。

通过真实 Browser Manager（create_browser）+ ShoppingFlow 建立
真实购物车状态（quantity=1，单商品）。

用法：
    python scripts/probe_checkout_entry.py                # both
    python scripts/probe_checkout_entry.py --viewport desktop
    python scripts/probe_checkout_entry.py --viewport mobile
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
from pages.base_page import BasePage
from utils.browser import close_browser, create_browser

JS_CHECKOUT_CONTROLS = r"""
(() => {
  const d = document.querySelector('#CartDrawer');
  if (!d) return { drawer: 'NONE' };
  const visible = el => {
    const r = el.getBoundingClientRect();
    return el.offsetParent !== null && r.width > 0 && r.height > 0;
  };
  const forms = [...d.querySelectorAll('form')].map(f => ({
    action: f.getAttribute('action') || '',
    method: f.getAttribute('method') || '',
  }));
  const buttons = [...d.querySelectorAll('button, a')]
    .filter(el => visible(el))
    .map(el => ({
      tag: el.tagName.toLowerCase(),
      cls: String(el.className).slice(0, 80),
      text: (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 40),
      href: el.getAttribute('href') || '',
      name: el.getAttribute('name') || '',
      aria: el.getAttribute('aria-label') || '',
      disabled: el.disabled,
    }));
  const express_markers = {
    shop_pay: /shop ?pay/i.test(d.textContent || ''),
    paypal: /paypal/i.test(d.textContent || ''),
    gpay: /google ?pay/i.test(d.textContent || ''),
    apay: /apple ?pay/i.test(d.textContent || ''),
    buy_now: /buy now|express/i.test(d.textContent || ''),
  };
  return { forms, buttons, express_markers };
})()
"""

JS_CHECKOUT_PAGE = r"""
(() => {
  const visible = el => {
    const r = el.getBoundingClientRect();
    return el.offsetParent !== null && r.width > 0 && r.height > 0;
  };
  const root = document.querySelector('[data-step], [class*="checkout"], main, body');
  const sections = [...document.querySelectorAll('section, [class*="section"], [data-step]')]
    .filter(visible)
    .slice(0, 15)
    .map(el => ({
      tag: el.tagName.toLowerCase(),
      cls: String(el.className).slice(0, 70),
      text: (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 60),
    }));
  // 关键语义输入（只探测存在性，不读取值）
  const inputs = [...document.querySelectorAll('input')]
    .filter(visible)
    .slice(0, 12)
    .map(el => ({
      type: el.getAttribute('type') || '',
      name: el.getAttribute('name') || '',
      autocomplete: el.getAttribute('autocomplete') || '',
      aria: el.getAttribute('aria-label') || '',
      placeholder: el.getAttribute('placeholder') || '',
    }));
  const headings = [...document.querySelectorAll('h1, h2, h3')]
    .filter(visible)
    .map(h => ({ tag: h.tagName.toLowerCase(), text: (h.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 60) }))
    .slice(0, 10);
  const line_items = [...document.querySelectorAll('[class*="line-item"], [class*="product"], [class*="cart-item"]')]
    .filter(visible)
    .filter(el => /products?\//.test(el.getAttribute('href') || '') || el.querySelector('a[href*="/products/"]'))
    .slice(0, 6)
    .map(el => ({
      cls: String(el.className).slice(0, 70),
      text: (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 120),
      link: (el.querySelector('a[href*="/products/"]') || {}).href || '',
    }));
  return {
    title: document.title.slice(0, 80),
    root_cls: root ? String(root.className).slice(0, 60) : '',
    sections,
    inputs,
    headings,
    line_items,
  };
})()
"""


def probe_viewport(viewport: str) -> None:
    """对单个视口执行 Checkout Entry 探测并打印结果。"""
    r = create_browser(viewport)
    page = r.page
    site_cfg = BasePage.load_site_config()
    flow = ShoppingFlow(page, site_cfg, viewport, access_policy=r.access_policy)

    print(f"===== Checkout Probe: {viewport.upper()} =====")
    try:
        # ------------------------------------------------------ 前置：真实 1-item 购物车
        flow.pre_clean_cart()
        flow.open_collection()
        flow.open_product()
        flow.validate_purchase_area()
        color = flow.select_color()
        size = flow.select_size()
        flow.capture_pdp_state()
        flow.add_to_cart()
        flow.capture_cart_state()
        print("[cart]", "title=", flow.state["cart_title"][:50], "| color=", color, "| size=", size, "| qty=", flow.state["cart_qty"])

        # ------------------------------------------------------ Cart Drawer Checkout 控件
        controls = page.evaluate(JS_CHECKOUT_CONTROLS)
        print("[drawer_forms]", json.dumps(controls["forms"], ensure_ascii=False))
        print("[drawer_buttons]", json.dumps(controls["buttons"], ensure_ascii=False))
        print("[express_markers]", json.dumps(controls["express_markers"], ensure_ascii=False))

        # ------------------------------------------------------ 标准 Checkout 点击
        checkout_btn = page.locator(
            '#CartDrawer button[name="checkout"], #CartDrawer .cart__checkout, '
            '#CartDrawer a[href*="/checkout"], #CartDrawer button[type="submit"][formaction*="checkout"]'
        ).first
        if not checkout_btn.count():
            print("[checkout_btn] NOT_FOUND")
            return
        print("[checkout_btn]", json.dumps({
            "tag": checkout_btn.evaluate("el => el.tagName.toLowerCase()"),
            "cls": checkout_btn.get_attribute("class") or "",
            "text": " ".join(checkout_btn.inner_text().split())[:40],
            "href": checkout_btn.get_attribute("href") or "",
            "visible": checkout_btn.is_visible(),
            "enabled": checkout_btn.is_enabled(),
        }, ensure_ascii=False))

        start_url = page.url
        checkout_btn.click()
        # 等待离开商品域上下文（轮询 URL，不固定 sleep）
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            url = page.url
            if url != start_url and ("/checkout" in url or "shopify" in url):
                break
            page.wait_for_timeout(300)
        page.wait_for_timeout(3000)  # 让 Checkout 落地页渲染
        print("[after_click] url:", page.url[:140])
        print("[after_click] checkout_page:", json.dumps(page.evaluate(JS_CHECKOUT_PAGE), ensure_ascii=False)[:3000])
    finally:
        close_browser(r)
    print()


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Mondressy Checkout Entry 探测")
    parser.add_argument("--viewport", choices=["desktop", "mobile", "both"], default="both")
    args = parser.parse_args(argv)

    viewports = ["desktop", "mobile"] if args.viewport == "both" else [args.viewport]
    for vp in viewports:
        probe_viewport(vp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
