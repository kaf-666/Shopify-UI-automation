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
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.browser import close_browser, create_browser, load_site_config, load_settings
from utils.config import resolve_url
from utils.site_access import (
    SiteAccessError,
    validate_signature_headers,
)
from utils.suite_runner import guarded_main

SIGNATURE_HEADER_NAMES = ("Signature", "Signature-Input", "Signature-Agent")


def _access_type(site_config: dict) -> str:
    access = site_config.get("access") or {}
    if not isinstance(access, dict):
        return ""
    return str(access.get("type") or "none").strip().lower()


def _allowed_hosts(policy_summary: dict, site_config: dict) -> List[str]:
    raw_hosts = policy_summary.get("allowed_hosts") or []
    if not raw_hosts:
        access = site_config.get("access") or {}
        raw_hosts = access.get("allowed_hosts") or []
    if not isinstance(raw_hosts, (list, tuple)):
        return []
    return [str(host).strip().lower() for host in raw_hosts if str(host).strip()]


def _unrelated_host(allowed_hosts: List[str]) -> str:
    for candidate in ("example.com", "example.org", "example.net"):
        if candidate not in allowed_hosts:
            return candidate
    return "not-allowed.example"


def _negative_host_urls(cart_url: str, allowed_hosts: List[str]) -> List[tuple[str, str]]:
    primary_host = allowed_hosts[0] if allowed_hosts else (urlsplit(cart_url).hostname or "first-party")
    scheme = urlsplit(cart_url).scheme or "https"
    return [
        ("unrelated third-party test host", f"{scheme}://{_unrelated_host(allowed_hosts)}/"),
        ("prefix lookalike", f"{scheme}://evil-{primary_host}/"),
        ("suffix lookalike", f"{scheme}://{primary_host}.attacker.com/"),
    ]


def _host_isolation_checks(
    policy,
    policy_type: str,
    cart_url: str,
    allowed_hosts: List[str],
) -> tuple[bool, List[str]]:
    lines: List[str] = []
    ok = True
    scheme = urlsplit(cart_url).scheme or "https"

    if policy_type == "signed_request":
        if not allowed_hosts:
            lines.append("  Allowed Host Isolation: FAIL (no allowed hosts)")
            ok = False
        for host in allowed_hosts:
            headers = dict(policy.request_headers(f"{scheme}://{host}/cart.js") or {})
            has_signature = bool(headers.get("Signature"))
            lines.append(
                f"  allowed host ({host}): Signed Headers {'YES' if has_signature else 'NO'}"
            )
            ok = ok and has_signature
    elif policy_type == "none":
        headers = dict(policy.request_headers(cart_url) or {})
        no_access_headers = not headers and not any(
            name in headers for name in SIGNATURE_HEADER_NAMES
        )
        lines.append(
            f"  first-party host: Signed Headers {'YES' if headers else 'NO'}"
        )
        lines.append(
            f"  NoAccessPolicy first-party headers: {'EMPTY' if no_access_headers else 'UNEXPECTED'}"
        )
        ok = ok and no_access_headers
    else:
        lines.append(f"  Host Isolation: FAIL (unsupported policy type: {policy_type})")
        ok = False

    for label, url in _negative_host_urls(cart_url, allowed_hosts):
        headers = dict(policy.request_headers(url) or {})
        has_signature = bool(headers.get("Signature"))
        lines.append(f"  {label}: Signed Headers {'YES' if has_signature else 'NO'}")
        ok = ok and not has_signature
    return ok, lines


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


def run_viewport(viewport: str, site_name: Optional[str] = None) -> tuple[bool, List[str]]:
    lines = []
    runtime = create_browser(viewport, site_name=site_name)
    try:
        policy = runtime.access_policy
        resolved_site = str(runtime.site_name or site_name or "")
        lines.append(f"  Site: {resolved_site}")
        if policy is None:
            lines.append("  Policy Type: unavailable")
            return False, lines

        summary = policy.masked_summary() or {}
        policy_type = str(
            getattr(policy, "type_name", "") or summary.get("type", "")
        ).strip().lower()
        lines.append(f"  Policy Type: {policy_type}")
        lines.append(f"  Secret Source: {summary.get('source', 'not_applicable')}")
        lines.append(
            f"  Credentials: {summary.get('credentials', 'not_applicable')}"
        )
        lines.append(f"  Expires: {summary.get('expires')}")
        allowed_hosts = _allowed_hosts(summary, runtime.site_config or {})
        lines.append(
            f"  Allowed Hosts: {', '.join(allowed_hosts) if allowed_hosts else 'not_applicable'}"
        )

        site_config = runtime.site_config or {}
        base_url = resolve_url(site_config.get("base_url"), "site.base_url")
        cart_url = f"{base_url}/cart.js"

        # ---- APIRequestContext 携带策略请求头
        headers = dict(policy.request_headers(cart_url) or {})
        signed = all(headers.get(name) for name in SIGNATURE_HEADER_NAMES)
        resp = runtime.context.request.get(cart_url, headers=headers, timeout=15000)
        lines.append(
            f"  APIRequestContext Signed Request: {'ENABLED' if signed else 'DISABLED'}"
        )
        if policy_type == "none":
            request_access_ok = not headers
        elif policy_type == "signed_request":
            request_access_ok = signed
        else:
            request_access_ok = False
        lines.append(f"  /cart.js HTTP: {resp.status}")

        # ---- Host 隔离（仅逻辑验证，不向第三方发请求）
        host_ok, host_lines = _host_isolation_checks(
            policy,
            policy_type,
            cart_url,
            allowed_hosts,
        )
        lines.extend(host_lines)

        # ---- 页面 route 统计（注入 vs 未触碰）
        stats = summary.get("request_stats") or getattr(policy, "stats", {})
        lines.append(f"  Page route stats: injected={stats.get('injected', 0)} untouched={stats.get('untouched', 0)}")

        ok = resp.status == 200 and request_access_ok and host_ok
        return ok, lines
    finally:
        close_browser(runtime)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="站点访问策略验证")
    parser.add_argument("--viewport", choices=["desktop", "mobile", "both"], default="both")
    parser.add_argument(
        "--site",
        default=None,
        help="site name (default: settings.default_site)",
    )
    args = parser.parse_args(argv)

    settings = load_settings()
    site = args.site or str(settings.get("default_site") or "")
    site_config = load_site_config(site)
    resolve_url(site_config.get("base_url"), "site.base_url")
    configured_access_type = _access_type(site_config)

    print("=== Site Access Validation ===")
    print()
    viewports = ["desktop", "mobile"] if args.viewport == "both" else [args.viewport]
    ok_all = True
    for vp in viewports:
        print(f"[{vp.title()}]")
        ok, lines = run_viewport(vp, site_name=site)
        ok_all = ok_all and ok
        print("\n".join(lines))
        print()
    print("[Credential Self-Checks]")
    if configured_access_type == "signed_request":
        checks = credential_self_checks()
        print("\n".join(checks))
        ok_all = ok_all and all("PASS" in c for c in checks)
    else:
        print("  Credential Self-Checks: NOT_APPLICABLE (policy type: none)")
    print()
    print("Secrets Printed: NO")
    print()
    print(f"站点访问验证: {'PASS' if ok_all else 'FAIL'}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(guarded_main(main))
