"""站点访问策略层。

集中管理站点访问凭证（Shopify Signed Request）、Host 白名单、
页面请求头注入与 APIRequestContext 请求头提供。

安全要求：绝不打印凭证内容，仅暴露脱敏摘要。
Signed Request 只注入白名单域名；APIRequestContext 不经过
BrowserContext 的 route 拦截，需要显式使用 request_headers()。
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

SECRET_VAR_MAP = {
    "MONDRESSY_US_SHOPIFY_SIGNATURE": "Signature",
    "MONDRESSY_US_SHOPIFY_SIGNATURE_INPUT": "Signature-Input",
    "MONDRESSY_US_SHOPIFY_SIGNATURE_AGENT": "Signature-Agent",
}
DEFAULT_ALLOWED_HOSTS = ("mondressy.com", "www.mondressy.com")
EXPIRY_WARN_SECONDS = 7 * 24 * 3600


class SiteAccessError(Exception):
    """站点访问策略失败，携带失败分类代码。"""

    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


def parse_expires(signature_input: str) -> Optional[int]:
    """从 Signature-Input 值提取 expires= 时间戳。"""
    match = re.search(r"expires=(\d+)", str(signature_input or ""))
    return int(match.group(1)) if match else None


def parse_secret_file(path) -> Dict[str, str]:
    """以文本方式读取 .ps1 环境文件（绝不执行）→ {请求头: 值}。"""
    p = Path(path)
    if not p.exists():
        raise SiteAccessError("SIGNED_REQUEST_SECRET_FILE_NOT_FOUND", f"secret file not found: {p}")
    headers: Dict[str, str] = {}
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise SiteAccessError("SIGNED_REQUEST_PARSE_FAILURE", str(exc)) from exc
    for var, header in SECRET_VAR_MAP.items():
        m = re.search(rf"\$env:{var}\s*=\s*'([^']*)'", text)
        if m:
            headers[header] = m.group(1)
    return headers


def validate_signature_headers(headers: Dict[str, str]) -> Optional[int]:
    """校验凭证集合完整性并返回过期时间戳。"""
    required = ["Signature", "Signature-Input", "Signature-Agent"]
    missing = [k for k in required if not headers.get(k)]
    if missing:
        raise SiteAccessError("SIGNED_REQUEST_INCOMPLETE", "Missing:\n" + "\n".join(missing))
    expires = parse_expires(headers["Signature-Input"])
    if expires is not None and time.time() >= expires:
        fmt = datetime.fromtimestamp(expires, tz=timezone.utc).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        raise SiteAccessError("SIGNED_REQUEST_EXPIRED", f"Expires: {fmt}\nCurrent Time: {now}")
    return expires


class SiteAccessPolicy:
    """基础策略：无访问凭证（空实现）。"""

    type_name = "none"

    def attach(self, context) -> None:
        """基础策略为空操作。"""
        return None

    def request_headers(self, url: str) -> dict:
        return {}

    def masked_summary(self) -> dict:
        return {"type": self.type_name}


class NoAccessPolicy(SiteAccessPolicy):
    """无访问凭证站点的默认策略（attach 与请求头均为空操作）。"""

    type_name = "none"


class SignedRequestPolicy(SiteAccessPolicy):
    """仅向白名单域名注入 Shopify Signed Request 请求头；APIRequestContext 通过 request_headers() 显式复用。"""

    type_name = "signed_request"

    def __init__(
        self,
        headers: Dict[str, str],
        allowed_hosts: List[str],
        expires: Optional[int] = None,
    ):
        self._headers = dict(headers)
        self._allowed_hosts = tuple(h.lower() for h in allowed_hosts)
        self._expires = expires
        self.stats = {"injected": 0, "untouched": 0}

    def _host_allowed(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return host in self._allowed_hosts

    def attach(self, context) -> None:
        """对本 context 每个请求做路由级注入（仅白名单域名）。"""

        def handler(route):
            if self._host_allowed(route.request.url):
                self.stats["injected"] += 1
                route.continue_(headers={**route.request.headers, **self._headers})
            else:
                self.stats["untouched"] += 1
                route.continue_()

        context.route("**/*", handler)

    def request_headers(self, url: str) -> dict:
        """返回 url 对应的签名请求头——非白名单域名返回空字典。"""
        return dict(self._headers) if self._host_allowed(url) else {}

    def masked_summary(self) -> dict:
        return {
            "type": self.type_name,
            "allowed_hosts": sorted(self._allowed_hosts),
            "credentials": "loaded",
            "expires": (
                datetime.fromtimestamp(self._expires, tz=timezone.utc).isoformat()
                if self._expires
                else None
            ),
        }


def create_site_access_policy(site_name: str, site_config: dict) -> SiteAccessPolicy:
    """工厂方法：站点配置 → 策略实例（唯一分发点）。"""
    access = site_config.get("access") or {}
    policy_type = str(access.get("type") or "none").lower()
    if policy_type != "signed_request":
        return NoAccessPolicy()
    secret_file = access.get("secret_file") or access.get("secret_env")
    allowed_hosts = access.get("allowed_hosts") or list(DEFAULT_ALLOWED_HOSTS)
    if not secret_file:
        raise SiteAccessError(
            "SITE_ACCESS_CONFIG_FAILURE",
            "signed_request 类型缺少 secret_file/secret_env 配置",
        )
    headers = parse_secret_file(secret_file)
    expires = validate_signature_headers(headers)
    if expires is not None and (expires - time.time()) <= EXPIRY_WARN_SECONDS:
        print(
            "[site_access] 凭证即将过期："
            f"expires={datetime.fromtimestamp(expires, tz=timezone.utc).isoformat()}"
        )
    return SignedRequestPolicy(headers, allowed_hosts, expires=expires)
