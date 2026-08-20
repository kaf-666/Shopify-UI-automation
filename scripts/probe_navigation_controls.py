"""Home / Navigation 控件探测脚本。

对 Mondressy 首页与导航体系做只读 DOM 探索，探测内容：
    Header / Logo / 主导航容器 / 菜单项 / 子菜单 / 移动端 Trigger /
    Drawer / 关闭控件 / Collection 链接。

通过真实 Browser Manager（create_browser）运行，
自动继承代理与 Signed Request 访问策略。

用法：
    python scripts/probe_navigation_controls.py                # both
    python scripts/probe_navigation_controls.py --viewport desktop
    python scripts/probe_navigation_controls.py --viewport mobile
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

from pages.home_page import HomePage
from utils.browser import close_browser, create_browser

SETTLE_MS = 3000
SHORT_MS = 1000

TARGET_PATH = "/collections/wedding-guest-dresses"

JS_HEADER_NAV = r"""
(() => {
  const visible = el => {
    const r = el.getBoundingClientRect();
    return el.offsetParent !== null && r.width > 0 && r.height > 0;
  };
  const header = document.querySelector('header.site-header, header[class*="header"]');
  const gm_menus = [...document.querySelectorAll('ul.gm-menu')].map(n => ({
    cls: String(n.className).slice(0, 120),
    visible: visible(n),
    items: [...n.querySelectorAll(':scope > li')].slice(0, 20).map(li => ({
      cls: String(li.className).slice(0, 60),
      text: (li.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 45),
      link: (() => { const a = li.querySelector(':scope > a'); return a ? { text: (a.textContent||'').trim().replace(/\s+/g,' ').slice(0,45), href: a.getAttribute('href') || '', cls: String(a.className).slice(0, 50) } : null; })(),
      has_submenu: li.querySelector(':scope > ul') !== null,
      submenu_cls: li.querySelector(':scope > ul') ? String(li.querySelector(':scope > ul').className).slice(0, 70) : '',
    })),
  }));
  return {
    header: header ? { tag: header.tagName.toLowerCase(), cls: String(header.className).slice(0, 90), id: header.id || '' } : null,
    gm_menus,
  };
})()
"""

JS_COLLECTION_LINKS = r"""
(() => {
  const visible = el => {
    const r = el.getBoundingClientRect();
    return el.offsetParent !== null && r.width > 0 && r.height > 0;
  };
  // 只列出可见的 collection 链接（桌面端头部主导航）
  const links = [...document.querySelectorAll('a[href*="/collections/"]')]
    .filter(a => visible(a))
    .slice(0, 25)
    .map(a => ({
      text: (a.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 50),
      href: a.getAttribute('href') || '',
      cls: String(a.className).slice(0, 70),
    }));
  return { count: links.length, links };
})()
"""

JS_TARGET_PARENT = r"""
(() => {
  const a = document.querySelector('a[href*="/collections/wedding-guest-dresses"]');
  if (!a) return { found: false };
  const li = a.closest('li');
  const ul = a.closest('ul');
  const walk = (el, depth) => {
    if (!el || depth > 3) return null;
    return {
      tag: el.tagName.toLowerCase(),
      cls: String(el.className).slice(0, 90),
      id: el.id || '',
      role: el.getAttribute('role') || '',
      aria_expanded: el.getAttribute('aria-expanded') || '',
      children: [...el.children].slice(0, 6).map(c => walk(c, depth + 1)).filter(Boolean),
    };
  };
  return {
    found: true,
    link_visible: a.offsetParent !== null,
    link_cls: String(a.className).slice(0, 70),
    parent_li: li ? walk(li, 0) : null,
    parent_ul_cls: ul ? String(ul.className).slice(0, 90) : '',
  };
})()
"""

JS_MOBILE_TRIGGERS = r"""
(() => {
  const visible = el => {
    const r = el.getBoundingClientRect();
    return el.offsetParent !== null && r.width > 0 && r.height > 0;
  };
  const cands = [...document.querySelectorAll('button, a, [role="button"], div')]
    .filter(el => visible(el) && (
      /hamburger|menu|nav/i.test(String(el.className)) ||
      /menu/i.test(el.getAttribute('aria-label') || '') ||
      /menu/i.test(el.getAttribute('aria-controls') || '')
    ))
    .slice(0, 15)
    .map(el => ({
      tag: el.tagName.toLowerCase(),
      cls: String(el.className).slice(0, 90),
      id: el.id || '',
      aria_label: el.getAttribute('aria-label') || '',
      aria_controls: el.getAttribute('aria-controls') || '',
      aria_expanded: el.getAttribute('aria-expanded') || '',
      text: (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 30),
    }));
  return cands;
})()
"""

JS_DRAWER_STATE = r"""
(() => {
  const visible = el => {
    const r = el.getBoundingClientRect();
    return el.offsetParent !== null && r.width > 0 && r.height > 0;
  };
  const drawers = [...document.querySelectorAll('[class*="drawer"], [class*="mobile-nav"], [id*="Nav"]')]
    .slice(0, 10)
    .map(el => ({
      tag: el.tagName.toLowerCase(),
      cls: String(el.className).slice(0, 100),
      id: el.id || '',
      visible: visible(el),
      is_open: /is-open|active/.test(String(el.className)),
      links: [...el.querySelectorAll('a')].slice(0, 25).map(a => ({
        text: (a.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 40),
        href: a.getAttribute('href') || '',
        cls: String(a.className).slice(0, 50),
        visible: visible(a),
      })),
    }));
  const close_cands = [...document.querySelectorAll('button, a')]
    .filter(el => visible(el) && /close|✕|×|关闭/i.test((el.getAttribute('aria-label') || '') + ' ' + (el.textContent || '')))
    .slice(0, 5)
    .map(el => ({ tag: el.tagName.toLowerCase(), cls: String(el.className).slice(0, 70), aria: el.getAttribute('aria-label') || '' }));
  return { drawers, close_cands };
})()
"""


def probe_viewport(viewport: str) -> None:
    """对单个视口执行 Home / Navigation 探测并打印结果。"""
    r = create_browser(viewport)
    page = r.page
    site_cfg = HomePage.load_site_config()
    home = HomePage(page, site_cfg, viewport)

    print(f"===== Navigation Probe: {viewport.upper()} =====")
    try:
        home.open()
        page.wait_for_timeout(SETTLE_MS)
        # 站点抗干扰：托管挑战可能在加载后同 URL 重载页面（短暂空壳），
        # 等待真实 header 出现；10s 内未恢复则视为本次探测中断。
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                ok = page.evaluate(
                    "() => !!document.querySelector('header') && document.title.trim().length > 0"
                )
            except Exception:
                ok = False
            if ok:
                break
            page.wait_for_timeout(500)
        print("[home]", page.title()[:70], "|", page.url)
        print("[logo]", "visible=", home.logo().is_visible() if home.logo().count() else "N/A")
        print("[search_trigger]", "count=", home.search_trigger().count())
        print("[cart_trigger]", "count=", home.cart_trigger().count())

        print("[header_nav]", json.dumps(page.evaluate(JS_HEADER_NAV), ensure_ascii=False)[:3000])
        print("[collection_links]", json.dumps(page.evaluate(JS_COLLECTION_LINKS), ensure_ascii=False)[:2500])
        print("[target_parent]", json.dumps(page.evaluate(JS_TARGET_PARENT), ensure_ascii=False)[:1800])

        if viewport == "desktop":
            # 目标链接若在展开层中：真实 hover 其父级菜单项，观察子菜单
            target = page.locator(f'a[href*="{TARGET_PATH}"]').filter(visible=True).first
            print("[desktop_target_visible]", target.is_visible() if target.count() else "N/A")
            if target.count() and not target.is_visible():
                parent_li = target.evaluate_handle(
                    "el => { let p = el; while (p && p.tagName !== 'LI') p = p.parentElement; return p; }"
                )
                parent_link = parent_li.as_element().query_selector(":scope > a")
                print("[desktop_hover] hovering parent")
                if parent_link is not None:
                    parent_link.hover()
                    page.wait_for_timeout(1200)
                    print("[desktop_hover] target visible after hover:", target.is_visible())
                submenu = parent_li.as_element().query_selector(":scope > ul")
                if submenu is not None:
                    print("[desktop_submenu]", json.dumps({
                        "cls": submenu.get_attribute("class"),
                        "items": [(" ".join(a.inner_text().split())[:40], a.get_attribute("href")) for a in submenu.query_selector_all("a")][:12],
                    }, ensure_ascii=False))
            else:
                print("[desktop_target] directly visible in header")
        else:
            # 移动端：探测 hamburger / drawer
            print("[mobile_triggers]", json.dumps(page.evaluate(JS_MOBILE_TRIGGERS), ensure_ascii=False))
            # 尝试最常见的菜单按钮：icon-hamburger / aria-controls 含 nav
            trigger = page.locator(
                'button[aria-controls*="Nav" i], a[aria-controls*="Nav" i], '
                '.icon-hamburger, [class*="mobile-nav-toggle"], button[class*="menu"]'
            ).first
            if trigger.count():
                print("[mobile_trigger]", json.dumps({
                    "tag": trigger.evaluate("el => el.tagName.toLowerCase()"),
                    "cls": trigger.get_attribute("class") or "",
                    "aria_controls": trigger.get_attribute("aria-controls") or "",
                }, ensure_ascii=False))
                try:
                    trigger.click()
                    page.wait_for_timeout(SHORT_MS)
                except Exception as exc:
                    print("[mobile_trigger_click] FAILED:", type(exc).__name__, str(exc)[:120])
                print("[drawer_state]", json.dumps(page.evaluate(JS_DRAWER_STATE), ensure_ascii=False)[:3000])

                # 目标链接在 drawer 中：真实展开其一级分类
                target = page.locator(f'a[href*="{TARGET_PATH}"]').first
                print("[drawer_target_visible]", target.is_visible() if target.count() else "N/A")
                if target.count() and not target.is_visible():
                    parent_li = target.evaluate_handle(
                        "el => { let p = el; while (p && p.tagName !== 'LI') p = p.parentElement; return p; }"
                    )
                    li = parent_li.as_element()
                    print("[drawer_parent_li]", json.dumps({
                        "cls": li.get_attribute("class") or "",
                        "has_button": li.locator("button, .collapsible-trigger, [class*=collapsible]").count(),
                    }, ensure_ascii=False))
                    expander = li.locator("button, .collapsible-trigger, [class*=collapsible]").first
                    if expander.count():
                        try:
                            expander.click()
                            page.wait_for_timeout(SHORT_MS)
                        except Exception as exc:
                            print("[drawer_expand] FAILED:", type(exc).__name__, str(exc)[:120])
                        print("[drawer_target_after_expand]", target.is_visible() if target.count() else "N/A")
                        sub = li.locator("ul").first
                        if sub.count():
                            print("[drawer_submenu_items]", json.dumps(
                                [(" ".join(a.inner_text().split())[:40], a.get_attribute("href")) for a in sub.locator("a").all()][:12],
                                ensure_ascii=False))
    finally:
        close_browser(r)
    print()


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Mondressy Home / Navigation 探测")
    parser.add_argument("--viewport", choices=["desktop", "mobile", "both"], default="both")
    args = parser.parse_args(argv)

    viewports = ["desktop", "mobile"] if args.viewport == "both" else [args.viewport]
    for vp in viewports:
        probe_viewport(vp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
