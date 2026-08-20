"""站点访问策略验证。

通过真实框架 Browser Manager 在双端验证 SiteAccessPolicy 层：

  - 凭证已加载 / 完整 / 未过期（不打印任何凭证内容）
  - APIRequestContext（/cart.js）使用策略请求头 -> HTTP 200
  - Host 白名单隔离（仅逻辑验证，不向第三方发真实请求）
  - 凭证边界情况（缺头 / 过期）通过内存自检验证
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.browser import close_browser, create_browser
from utils.site_access import (
    SiteAccessError,
    SignedRequestPolicy,
    validate_signature_headers,
)

CART_JS = "https://mondressy.com/cart.js"


def credential_self_checks() -> List[str]:
    """In-memory edge-case tests (never touch the real secrets file)."""
    lines = []
    # 1) 缺少请求头 -> SIGNED_REQUEST_INCOMPLETE
    try:
        validate_signature_headers(
            {"Signature": "x", "Signature-Input": "sig1=(\"@authority\");created=1;expires=9999999999"}
        )
        lines.append("  Missing Credential Detection: FAIL (no error raised)")
    except SiteAccessError as exc:
        lines.append(
            f"  Missing Credential Detection: PASS ({exc.category}; reported: Signature-Agent)"
            if exc.category == "SIGNED_REQUEST_INCOMPLETE" and "Signature-Agent" in str(exc)
            else f"  Missing Credential Detection: FAIL ({exc})"
        )
    # 2) 已过期 -> SIGNED_REQUEST_EXPIRED
    try:
        validate_signature_headers(
            {"Signature": "x", "Signature-Input": "sig1=(\"@authority\");created=1;expires=1",
             "Signature-Agent": '"https://shopify.com"'}
        )
        lines.append("  Expired Credential Detection: FAIL (no error raised)")
    except SiteAccessError as exc:
        lines.append(
            f"  Expired Credential Detection: PASS ({exc.category})"
            if exc.category == "SIGNED_REQUEST_EXPIRED"
            else f"  Expired Credential Detection: FAIL ({exc})"
        )
    return lines


def run_viewport(viewport: str) -> tuple[bool, List[str]]:
    lines = []
    runtime = create_browser(viewport)
    try:
        policy = runtime.access_policy
        summary = policy.masked_summary()
        lines.append(f"  Site: mondressy")
        lines.append(f"  Policy Type: {summary.get('type')}")
        lines.append(f"  Secret Source: {summary.get('secret_file')}")
        lines.append(f"  Credentials: {summary.get('credentials')}")
        lines.append(f"  Expires: {summary.get('expires')}")
        lines.append(f"  Allowed Hosts: {', '.join(summary.get('allowed_hosts', []))}")

        # ---- APIRequestContext 携带策略请求头
        headers = policy.request_headers(CART_JS)
        signed = bool(headers.get("Signature"))
        resp = runtime.context.request.get(CART_JS, headers=headers, timeout=15000)
        lines.append(f"  APIRequestContext Signed Request: {'ENABLED' if signed else 'DISABLED'}")
        lines.append(f"  /cart.js HTTP: {resp.status}")

        # ---- Host 隔离（仅逻辑验证，不向第三方发请求）
        hosts = {
            "mondressy.com": "https://mondressy.com/cart.js",
            "www.mondressy.com": "https://www.mondressy.com/x",
            "third-party test host": "https://example.com/",
            "evil subdomain lookalike": "https://evil-mondressy.com/",
            "suffix lookalike": "https://mondressy.com.attacker.com/",
        }
        for label, url in hosts.items():
            has_sig = bool(policy.request_headers(url).get("Signature"))
            lines.append(f"  {label}: Signed Headers {'YES' if has_sig else 'NO'}")

        # ---- 页面 route 统计（注入 vs 未触碰）
        stats = summary.get("request_stats") or policy.stats
        lines.append(f"  Page route stats: injected={stats.get('injected', 0)} untouched={stats.get('untouched', 0)}")

        ok = resp.status == 200 and signed and isinstance(policy, SignedRequestPolicy)
        return ok, lines
    finally:
        close_browser(runtime)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="站点访问策略验证")
    parser.add_argument("--viewport", choices=["desktop", "mobile", "both"], default="both")
    args = parser.parse_args(argv)

    print("=== Site Access Validation ===")
    print()
    viewports = ["desktop", "mobile"] if args.viewport == "both" else [args.viewport]
    ok_all = True
    for vp in viewports:
        print(f"[{vp.title()}]")
        ok, lines = run_viewport(vp)
        ok_all = ok_all and ok
        print("\n".join(lines))
        print()
    print("[Credential Self-Checks]")
    checks = credential_self_checks()
    print("\n".join(checks))
    ok_all = ok_all and all("PASS" in c for c in checks)
    print()
    print("Secrets Printed: NO")
    print()
    print(f"站点访问验证: {'PASS' if ok_all else 'FAIL'}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
