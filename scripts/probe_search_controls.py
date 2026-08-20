"""Search 控件探测脚本。

对 Mondressy Search 入口做只读 DOM 探索，探测内容：
    Trigger / 打开后的容器 / Input / Submit / Close / Clear /
    Predictive 容器 / 结果容器 / 结果卡片 / No Result 状态。

通过真实 Browser Manager（create_browser）运行，
自动继承代理与 Signed Request 访问策略。

用法：
    python scripts/probe_search_controls.py                # both
    python scripts/probe_search_controls.py --viewport desktop
    python scripts/probe_search_controls.py --viewport mobile
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pages.home_page import HomePage
from utils.browser import close_browser, create_browser

SETTLE_MS = 3500
SHORT_MS = 1200

# 打开 Search 后：可见的 input 探测（含类型 / 占位符 / 名称）
JS_AFTER_OPEN = r"""
(() => {
  const visible = el => {
    const r = el.getBoundingClientRect();
    return el.offsetParent !== null && r.width > 0 && r.height > 0;
  };
  const inputs = [...document.querySelectorAll('input')]
    .filter(visible)
    .map(el => ({
      type: el.getAttribute('type') || '',
      name: el.getAttribute('name') || '',
      id: el.id || '',
      placeholder: el.getAttribute('placeholder') || '',
      cls: String(el.className).slice(0, 90),
      value: (el.value || '').slice(0, 40),
    }));
  const searchish = [...document.querySelectorAll('[class*="search" i], [id*="search" i]')]
    .filter(visible)
    .slice(0, 30)
    .map(el => ({
      tag: el.tagName.toLowerCase(),
      id: el.id || '',
      cls: String(el.className).slice(0, 110),
      role: el.getAttribute('role') || '',
      aria: (el.getAttribute('aria-label') || '').slice(0, 60),
    }));
  return { url: location.href, inputs, searchish };
})()
"""

# 探测 Predictive / 建议容器（输入后出现）
JS_PREDICTIVE = r"""
(() => {
  const visible = el => {
    const r = el.getBoundingClientRect();
    return el.offsetParent !== null && r.width > 0 && r.height > 0;
  };
  const boxes = [...document.querySelectorAll(
    '[class*="predictive" i], [class*="suggestion" i], [class*="autocomplete" i], ' +
    '[class*="search-results" i], [class*="search__results" i], .search-bar__results'
  )]
    .filter(visible)
    .slice(0, 20)
    .map(el => ({
      tag: el.tagName.toLowerCase(),
      cls: String(el.className).slice(0, 120),
      id: el.id || '',
      links: [...el.querySelectorAll('a')].slice(0, 5).map(a => ({
        text: (a.textContent || '').trim().slice(0, 50),
        href: a.getAttribute('href') || '',
      })),
    }));
  return boxes;
})()
"""

# 结果页 / 结果容器与商品卡片探测
JS_RESULTS = r"""
(() => {
  const visible = el => {
    const r = el.getBoundingClientRect();
    return el.offsetParent !== null && r.width > 0 && r.height > 0;
  };
  const grids = [...document.querySelectorAll('[class*="grid"], [class*="product"]')]
    .filter(el => visible(el) && el.querySelectorAll('a[href*="/products/"]').length >= 2)
    .slice(0, 10)
    .map(el => ({
      cls: String(el.className).slice(0, 120),
      id: el.id || '',
      product_links: el.querySelectorAll('a[href*="/products/"]').length,
    }));
  const cards = [...document.querySelectorAll('a[href*="/products/"]')]
    .filter(visible)
    .slice(0, 8)
    .map(a => ({
      cls: String(a.className).slice(0, 100),
      href: a.getAttribute('href') || '',
      text: (a.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 80),
      img_alt: (a.querySelector('img') || {}).alt || '',
    }));
  const headings = [...document.querySelectorAll('h1, h2')]
    .filter(visible)
    .map(h => ({ tag: h.tagName.toLowerCase(), text: (h.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 80) }))
    .slice(0, 8);
  return { grids, cards, headings };
})()
"""

# No Result 状态探测
JS_NO_RESULT = r"""
(() => {
  const visible = el => {
    const r = el.getBoundingClientRect();
    return el.offsetParent !== null && r.width > 0 && r.height > 0;
  };
  const texts = [...document.querySelectorAll('p, h1, h2, h3, div')]
    .filter(el => visible(el) && /no result|no products|nothing found|没有找到|未找到|empty/i.test(el.textContent || ''))
    .slice(0, 8)
    .map(el => ({
      tag: el.tagName.toLowerCase(),
      cls: String(el.className).slice(0, 100),
      text: (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 90),
    }));
  const cards = [...document.querySelectorAll('a[href*="/products/"]')].filter(visible).length;
  return { empty_hits: texts, visible_product_links: cards };
})()
"""

# Close / Clear 控件探测
JS_CLOSE_CLEAR = r"""
(() => {
  const visible = el => {
    const r = el.getBoundingClientRect();
    return el.offsetParent !== null && r.width > 0 && r.height > 0;
  };
  const cands = [...document.querySelectorAll('button, a, [role="button"]')]
    .filter(visible)
    .filter(el => {
      const t = (el.getAttribute('aria-label') || '') + ' ' + (el.textContent || '');
      return /close|clear|取消|关闭|清除|✕|×/i.test(t);
    })
    .slice(0, 10)
    .map(el => ({
      tag: el.tagName.toLowerCase(),
      cls: String(el.className).slice(0, 100),
      aria: el.getAttribute('aria-label') || '',
      text: (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 30),
    }));
  return cands;
})()
"""


def _no_result_query() -> str:
    """生成低碰撞随机查询：仅字母 / 数字 / 短横线。"""
    return "qa-no-result-" + datetime.now().strftime("%Y%m%d-%H%M%S")


def probe_viewport(viewport: str) -> None:
    """对单个视口执行 Search 全链路探测并打印结果。"""
    r = create_browser(viewport)
    page = r.page
    site_cfg = HomePage.load_site_config()
    home = HomePage(page, site_cfg, viewport)

    print(f"===== Search Probe: {viewport.upper()} =====")
    try:
        # ------------------------------------------------------------ 1. 打开首页
        home.open()
        page.wait_for_timeout(SETTLE_MS)
        print("[home]", page.title()[:70], "|", page.url)

        # ------------------------------------------------------------ 2. Trigger
        trigger = home.search_trigger()
        trig_info = {
            "tag": trigger.evaluate("el => el.tagName.toLowerCase()"),
            "cls": str(trigger.get_attribute("class") or "")[:120],
            "href": trigger.get_attribute("href") or "",
            "visible": trigger.is_visible(),
        }
        print("[trigger]", json.dumps(trig_info, ensure_ascii=False))

        # ------------------------------------------------------------ 3. 点击 Trigger
        before_url = page.url
        try:
            trigger.click()
            page.wait_for_timeout(SHORT_MS)
        except Exception as exc:
            print("[click] FAILED:", type(exc).__name__, str(exc)[:150])
            return
        after_open = page.evaluate(JS_AFTER_OPEN)
        print("[after_open]")
        print("  url:", after_open["url"])
        print("  visible_inputs:", json.dumps(after_open["inputs"], ensure_ascii=False))
        print("  visible_searchish:", json.dumps(after_open["searchish"], ensure_ascii=False))

        # ------------------------------------------------------------ 4. 输入测试
        inputs = after_open["inputs"]
        search_input = None
        for cand in inputs:
            if cand["type"] == "search" or "search" in cand["cls"].lower() or "search" in cand["placeholder"].lower():
                search_input = cand
                break
        if search_input is None and inputs:
            search_input = inputs[0]
        if search_input is None:
            print("[input] NOT_FOUND — 打开后没有可见 input")
            return
        print("[input] selected:", json.dumps(search_input, ensure_ascii=False))

        sel = "input#" + search_input["id"] if search_input["id"] else None
        if sel is None:
            # 按特征回退定位
            sel = (
                'input[type="search"]'
                if any(i["type"] == "search" for i in inputs)
                else "input:visible"
            )
        loc = page.locator(sel).first
        loc.click()
        loc.fill("dress")
        page.wait_for_timeout(SHORT_MS)
        print("[predictive_after_type]")
        print(" ", json.dumps(page.evaluate(JS_PREDICTIVE), ensure_ascii=False))
        print("  clear_close_candidates:", json.dumps(page.evaluate(JS_CLOSE_CLEAR), ensure_ascii=False))

        # ------------------------------------------------------------ 5. Submit（真实 Enter）
        try:
            loc.press("Enter")
        except Exception as exc:
            print("[submit] Enter FAILED:", type(exc).__name__, str(exc)[:150])
            return
        # 等待 URL 或 DOM 变化（不依赖固定 sleep 长等待）
        try:
            page.wait_for_url(lambda u: u != before_url or "/search" in u, timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(SHORT_MS)
        print("[after_submit] url:", page.url)
        print("  results:", json.dumps(page.evaluate(JS_RESULTS), ensure_ascii=False)[:2000])
        print("  no_result:", json.dumps(page.evaluate(JS_NO_RESULT), ensure_ascii=False))

        # ------------------------------------------------------------ 6. 打开第一个结果
        cards = page.locator('a[href*="/products/"]')
        first_card = None
        for i in range(min(cards.count(), 5)):
            cand = cards.nth(i)
            if cand.is_visible():
                first_card = cand
                break
        try:
            if first_card is not None:
                first_card.click()
                page.wait_for_url(lambda u: "/products/" in u, timeout=15000)
                page.wait_for_timeout(SHORT_MS)
                print("[open_product] url:", page.url)
                print("  pdp_title:", page.locator(".product-single__title").first.inner_text()[:80] if page.locator(".product-single__title").count() else "N/A")
            else:
                print("[open_product] NO_RESULT_CARD")
        except Exception as exc:
            print("[open_product] FAILED:", type(exc).__name__, str(exc)[:150])

        # ------------------------------------------------------------ 7. No Result（重新打开 Search）
        try:
            page.goto(home.base_url() + "/", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(SETTLE_MS)
            home = HomePage(page, site_cfg, viewport)
            home.search_trigger().click()
            page.wait_for_timeout(SHORT_MS)
            no_q = _no_result_query()
            loc2 = page.locator(sel).first
            if loc2.count() == 0:
                print("[no_result] input not found after reopen")
            else:
                loc2.fill(no_q)
                loc2.press("Enter")
                page.wait_for_timeout(2000)
                print(f"[no_result] query={no_q}")
                print("  url:", page.url)
                print("  state:", json.dumps(page.evaluate(JS_NO_RESULT), ensure_ascii=False))
        except Exception as exc:
            print("[no_result] FAILED:", type(exc).__name__, str(exc)[:150])

        # ------------------------------------------------------------ 8. Close / Clear 探测
        try:
            page.goto(home.base_url() + "/", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(SETTLE_MS)
            home = HomePage(page, site_cfg, viewport)
            home.search_trigger().click()
            page.wait_for_timeout(SHORT_MS)
            loc3 = page.locator(sel).first
            if loc3.count():
                loc3.fill("dress")
                page.wait_for_timeout(600)
                print("[close_clear] candidates:", json.dumps(page.evaluate(JS_CLOSE_CLEAR), ensure_ascii=False))
                # 尝试 Clear / Close（按 aria-label / 文本真实点击）
                for cand in page.evaluate(JS_CLOSE_CLEAR):
                    label = (cand["aria"] + " " + cand["text"]).strip().lower()
                    if any(k in label for k in ("close", "取消", "关闭", "clear", "清除")):
                        try:
                            page.get_by_label(cand["aria"]).first.click()
                        except Exception:
                            try:
                                page.get_by_text(cand["text"], exact=True).first.click()
                            except Exception as exc:
                                print("[close_clear] click FAILED:", label, type(exc).__name__)
                                continue
                        page.wait_for_timeout(600)
                        print(
                            "[close_clear] clicked:", label,
                            "| input_value_after:",
                            loc3.input_value() if loc3.count() else "N/A",
                        )
                        break
        except Exception as exc:
            print("[close_clear] FAILED:", type(exc).__name__, str(exc)[:150])
    finally:
        close_browser(r)
    print()


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Mondressy Search 控件探测")
    parser.add_argument("--viewport", choices=["desktop", "mobile", "both"], default="both")
    args = parser.parse_args(argv)

    viewports = ["desktop", "mobile"] if args.viewport == "both" else [args.viewport]
    for vp in viewports:
        probe_viewport(vp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
