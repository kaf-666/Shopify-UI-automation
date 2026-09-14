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
import os
from pathlib import Path
from typing import Dict, List, Mapping, Optional
from urllib.parse import urlparse

from utils.errors import CliConfigError

SIGNATURE_HEADERS = ("Signature", "Signature-Input", "Signature-Agent")
EXPIRY_WARN_SECONDS = 7 * 24 * 3600


class SiteAccessError(CliConfigError):
    """站点访问策略失败，携带失败分类代码。"""

    def __init__(self, category: str, message: str):
        super().__init__(message, category=category)


def parse_expires(signature_input: str) -> Optional[int]:
    """从 Signature-Input 值提取 expires= 时间戳。"""
    match = re.search(r"expires=(\d+)", str(signature_input or ""))
    return int(match.group(1)) if match else None


def _configured_env_names(env_names: Optional[Mapping[str, str]]) -> Dict[str, str]:
    """Require an explicit header-to-environment mapping for each site."""

    if not isinstance(env_names, Mapping):
        raise SiteAccessError("SITE_ACCESS_CONFIG_FAILURE", "signed_request env mapping is required")
    names: Dict[str, str] = {}
    for header in SIGNATURE_HEADERS:
        name = str(env_names.get(header) or "").strip()
        if not name:
            raise SiteAccessError(
                "SITE_ACCESS_CONFIG_FAILURE",
                f"signed_request env mapping is missing: {header}",
            )
        names[header] = name
    return names


def parse_secret_file(path, env_names: Mapping[str, str]) -> Dict[str, str]:
    """以文本方式读取 .ps1 环境文件（绝不执行）→ {请求头: 值}。"""
    p = Path(path)
    if not p.exists():
        raise SiteAccessError("SIGNED_REQUEST_SECRET_FILE_NOT_FOUND", "secret file not found")
    headers: Dict[str, str] = {}
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise SiteAccessError("SIGNED_REQUEST_PARSE_FAILURE", "secret file could not be read") from exc
    for header, variable in _configured_env_names(env_names).items():
        m = re.search(rf"\$env:{re.escape(variable)}\s*=\s*'([^']*)'", text)
        if m:
            headers[header] = m.group(1)
    return headers


def parse_env_headers(
    environ: Optional[Mapping[str, str]] = None,
    env_names: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """从进程环境读取 Signed Request，不回显任何凭证值。"""

    env = environ if environ is not None else os.environ
    names = _configured_env_names(env_names)
    headers = {header: str(env.get(name) or "") for header, name in names.items()}
    missing = [name for header, name in names.items() if not headers.get(header)]
    if missing:
        raise SiteAccessError(
            "SIGNED_REQUEST_MISSING",
            "required environment variables missing: " + ", ".join(missing),
        )
    return headers


def validate_signature_headers(headers: Dict[str, str]) -> Optional[int]:
    """校验凭证集合完整性并返回过期时间戳。"""
    missing = [k for k in SIGNATURE_HEADERS if not headers.get(k)]
    if missing:
        raise SiteAccessError("SIGNED_REQUEST_INCOMPLETE", "required headers missing: " + ", ".join(missing))
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
        source: str = "env",
    ):
        self._headers = dict(headers)
        self._allowed_hosts = tuple(h.lower() for h in allowed_hosts)
        self._expires = expires
        self._source = source
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
            "source": self._source,
            "expires": (
                datetime.fromtimestamp(self._expires, tz=timezone.utc).isoformat()
                if self._expires
                else None
            ),
        }


def _validate_allowed_hosts(raw_hosts) -> List[str]:
    """只接受 exact hostname allowlist，不接受通配符或 suffix 匹配。"""

    if not isinstance(raw_hosts, (list, tuple)) or not raw_hosts:
        raise SiteAccessError("SITE_ACCESS_CONFIG_FAILURE", "allowed_hosts must be a non-empty list")
    hosts: List[str] = []
    for raw in raw_hosts:
        host = str(raw or "").strip().lower()
        if (
            not host
            or "*" in host
            or "/" in host
            or ":" in host
            or host.startswith(".")
            or host.endswith(".")
        ):
            raise SiteAccessError("SITE_ACCESS_CONFIG_FAILURE", "allowed_hosts contains an invalid exact host")
        if host not in hosts:
            hosts.append(host)
    return hosts


def create_site_access_policy(site_name: str, site_config: dict) -> SiteAccessPolicy:
    """工厂方法：站点配置 → 策略实例（唯一分发点）。"""
    access = site_config.get("access") or {}
    if not isinstance(access, dict):
        raise SiteAccessError("SITE_ACCESS_CONFIG_FAILURE", "access config must be a mapping")
    policy_type = str(access.get("mode") or access.get("type") or "").strip().lower()
    if policy_type == "none":
        return NoAccessPolicy()
    if policy_type != "signed_request":
        raise SiteAccessError("SITE_ACCESS_CONFIG_FAILURE", "access mode must be explicitly configured")
    allowed_hosts = _validate_allowed_hosts(
        access.get("allowed_hosts")
    )
    source = str(access.get("source") or "").strip().lower()
    secret_file = access.get("secret_file")
    if not source:
        raise SiteAccessError("SITE_ACCESS_CONFIG_FAILURE", "signed_request source must be explicit")
    env_names = access.get("env") or access.get("environment")

    if source in ("env", "environment"):
        headers = parse_env_headers(
            env_names=env_names
        )
    elif source in ("file", "secret_file"):
        if not secret_file:
            raise SiteAccessError("SITE_ACCESS_CONFIG_FAILURE", "file source requires an explicit secret_file")
        headers = parse_secret_file(secret_file, env_names)
    elif source in ("env_or_file", "environment_or_file"):
        if secret_file:
            try:
                headers = parse_env_headers(
                    env_names=env_names
                )
            except SiteAccessError:
                headers = parse_secret_file(secret_file, env_names)
        else:
            headers = parse_env_headers(
                env_names=env_names
            )
    else:
        raise SiteAccessError("SITE_ACCESS_CONFIG_FAILURE", f"unsupported signed_request source: {source}")

    expires = validate_signature_headers(headers)
    if expires is not None and (expires - time.time()) <= EXPIRY_WARN_SECONDS:
        print(
            "[site_access] 凭证即将过期："
            f"expires={datetime.fromtimestamp(expires, tz=timezone.utc).isoformat()}"
        )
    return SignedRequestPolicy(headers, allowed_hosts, expires=expires, source=source)
