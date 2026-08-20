"""浏览器运行时验证入口。

用法：
    python run.py --viewport desktop
    python run.py --viewport mobile

退出码：0 = 通过，1 = 失败，2 = 非法参数。
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional
from urllib.parse import urlsplit

from utils.browser import (
    PAGE_NAV_TIMEOUT_MS,
    VALID_VIEWPORTS,
    BrowserConfigError,
    close_browser,
    create_browser,
    load_settings,
    resolve_base_url,
)


def _safe_proxy_display(server: str) -> str:
    """host:port only — never renders userinfo (credentials)."""
    parts = urlsplit(server)
    host = parts.hostname or ""
    port = parts.port
    return f"{host}:{port}" if port else host


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Shopify UI Automation - browser runtime validation"
    )
    parser.add_argument(
        "--viewport",
        default="desktop",
        help="desktop | mobile (default: desktop)",
    )
    args = parser.parse_args(argv)

    viewport = str(args.viewport).strip().lower()
    if viewport not in VALID_VIEWPORTS:
        print(f"Unsupported viewport: {args.viewport}")
        print(f"Supported: {', '.join(VALID_VIEWPORTS)}")
        return 2

    try:
        settings = load_settings()
        raw_base_url = str(settings.get("base_url") or "")
        base_url = resolve_base_url(raw_base_url)
        site = str(settings.get("default_site") or "")
    except (BrowserConfigError, OSError) as exc:
        print(f"Config load: FAIL ({exc})")
        print()
        print("浏览器运行时验证: FAIL")
        return 1

    print("=== Shopify UI Automation ===")
    print()
    print(f"Site: {site}")
    print(f"Viewport: {viewport}")
    print(f"URL: {raw_base_url}")
    print()

    runtime = None
    try:
        runtime = create_browser(viewport, settings)
    except Exception as exc:
        print(f"Browser launch: FAIL ({type(exc).__name__}: {exc})")
        print()
        print("浏览器运行时验证: FAIL")
        return 1

    nav_ok = False
    try:
        print(f"Browser: {runtime.engine}")
        if runtime.device:
            print(f"Device: {runtime.device}")
        if runtime.viewport_size:
            print(f"Viewport Size: {runtime.viewport_size[0]}x{runtime.viewport_size[1]}")
        if runtime.proxy_server:
            print("Explicit Proxy: enabled")
            print(f"Proxy Server: {_safe_proxy_display(runtime.proxy_server)}")
        else:
            print("Explicit Proxy: disabled")
        print()

        response = runtime.page.goto(
            base_url,
            wait_until="domcontentloaded",
            timeout=PAGE_NAV_TIMEOUT_MS,
        )
        final_url = runtime.page.url
        nav_ok = bool(final_url) and response is not None and response.ok
        print(f"Page loaded: {'PASS' if nav_ok else 'FAIL'}")
        print(f"Final URL: {final_url}")
        if response is not None and not response.ok:
            print(f"HTTP status: {response.status}")
    except Exception as exc:
        print(f"Navigation: FAIL ({type(exc).__name__}: {exc})")
        try:
            final_url = runtime.page.url
            if final_url:
                print(f"Final URL: {final_url}")
        except Exception:
            pass
    finally:
        try:
            close_browser(runtime)
            print("Cleanup: PASS")
        except Exception as exc:
            print(f"Cleanup: FAIL ({exc})")
            return 1

    print()
    print(f"浏览器运行时验证: {'PASS' if nav_ok else 'FAIL'}")
    return 0 if nav_ok else 1


if __name__ == "__main__":
    sys.exit(main())
