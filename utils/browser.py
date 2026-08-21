"""浏览器运行时层。

负责创建并持有两种运行时模式的浏览器会话：

    desktop : Chromium @ 1440x900
    mobile  : WebKit + Playwright "iPhone 14" 设备描述

流程：读取 settings.yaml → 解析视口 → 选择引擎 → 启动浏览器 →
创建 context → 创建 page → 返回运行时对象。

显式代理说明：Chromium / WebKit 不依赖 Windows 系统代理继承，
统一通过 Playwright proxy 参数走同一出口。
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

from utils.config import load_yaml_mapping, resolve_config_value, resolve_url, site_config_path
from utils.errors import CliConfigError
from utils.site_access import SiteAccessPolicy, create_site_access_policy

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = PROJECT_ROOT / "configs" / "settings.yaml"
SITES_DIR = PROJECT_ROOT / "configs" / "sites"
PAGE_NAV_TIMEOUT_MS = 45_000
BROWSER_LAUNCH_TIMEOUT_MS = 30_000
VALID_VIEWPORTS = ("desktop", "mobile")


class BrowserConfigError(CliConfigError):
    """浏览器配置缺失或非法。"""

    def __init__(self, message: str, category: str = "CONFIG_ERROR"):
        super().__init__(message, category=category)


def load_settings() -> dict:
    """读取 configs/settings.yaml 为字典。"""
    return load_yaml_mapping(SETTINGS_PATH, "settings")


def resolve_template(raw: str, field: str = "value") -> str:
    """解析 @url:`...` 模板 → 实际值。"""
    try:
        return resolve_config_value(raw, field)
    except CliConfigError as exc:
        raise BrowserConfigError(str(exc), category=exc.category) from exc


def load_site_config(site_name: str) -> dict:
    """读取 configs/sites/<site_name>.yaml 为字典。"""
    try:
        path = site_config_path(SITES_DIR, site_name)
        return load_yaml_mapping(path, "site config")
    except CliConfigError as exc:
        raise BrowserConfigError(str(exc), category=exc.category) from exc


def resolve_base_url(raw: str) -> str:
    """从 settings.yaml 解析 base_url 模板。"""
    try:
        return resolve_url(raw, "base_url")
    except CliConfigError as exc:
        raise BrowserConfigError(str(exc), category=exc.category) from exc


PROXY_ENV_DEFAULTS = {
    "server": "SHOPIFY_PROXY_SERVER",
    "username": "SHOPIFY_PROXY_USERNAME",
    "password": "SHOPIFY_PROXY_PASSWORD",
    "enabled": "SHOPIFY_PROXY_ENABLED",
}


def _parse_bool(raw, field: str) -> bool:
    value = str(raw).strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise BrowserConfigError(f"invalid boolean for {field}", category="PROXY_CONFIG_ERROR")


def _safe_proxy_server(server: str) -> str:
    """返回 host:port 摘要，不返回 userinfo。"""
    parsed = urlsplit(server)
    host = parsed.hostname or ""
    port = parsed.port
    return f"{host}:{port}" if port else host


def resolve_proxy(settings: dict, environ=None) -> Optional[dict]:
    """按 env -> YAML -> disabled 优先级解析 Playwright proxy。

    环境变量默认使用 ``SHOPIFY_PROXY_SERVER``, ``SHOPIFY_PROXY_USERNAME``
    和 ``SHOPIFY_PROXY_PASSWORD``，Jenkins 可直接绑定这些变量。密码只在
    内存中传递给 Playwright，从不进入 metadata / 日志 / 结果 JSON。
    """

    proxy = settings.get("proxy") or {}
    if not isinstance(proxy, dict):
        raise BrowserConfigError("proxy config must be a mapping", category="PROXY_CONFIG_ERROR")
    env = environ if environ is not None else os.environ
    configured_env = proxy.get("env") or {}
    if not isinstance(configured_env, dict):
        raise BrowserConfigError("proxy.env config must be a mapping", category="PROXY_CONFIG_ERROR")
    names = {
        key: str(configured_env.get(key) or PROXY_ENV_DEFAULTS[key])
        for key in PROXY_ENV_DEFAULTS
    }
    env_server = str(env.get(names["server"]) or "").strip()
    env_username = str(env.get(names["username"]) or "")
    env_password = str(env.get(names["password"]) or "")
    env_enabled_raw = env.get(names["enabled"])

    yaml_server = proxy.get("server")
    yaml_server = resolve_template(yaml_server, "proxy.server") if yaml_server else ""
    server = env_server or yaml_server
    username = env_username or str(proxy.get("username") or "")
    password = env_password or str(proxy.get("password") or "")

    if env_enabled_raw is not None and str(env_enabled_raw).strip():
        enabled = _parse_bool(env_enabled_raw, names["enabled"])
    else:
        enabled = bool(proxy.get("enabled")) or bool(env_server)

    if not enabled:
        return None
    if not server:
        raise BrowserConfigError(
            "proxy is enabled but server is missing",
            category="PROXY_CONFIG_ERROR",
        )
    parsed = urlsplit(server)
    if (
        parsed.scheme.lower() not in ("http", "https", "socks5")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or any(ch.isspace() for ch in server)
    ):
        raise BrowserConfigError(
            "proxy.server must be an http(s)/socks5 URL without embedded credentials",
            category="PROXY_CONFIG_ERROR",
        )
    try:
        _ = parsed.port
    except ValueError as exc:
        raise BrowserConfigError("proxy.server has an invalid port", category="PROXY_CONFIG_ERROR") from exc
    if bool(username) != bool(password):
        raise BrowserConfigError(
            "proxy username and password must be provided together",
            category="PROXY_CONFIG_ERROR",
        )
    result = {"server": server}
    if username and password:
        result.update({"username": username, "password": password})
    return result


@dataclass
class BrowserRuntime:
    """运行中的浏览器会话：page 与其生命周期资源（context/browser/playwright）。"""

    viewport: str
    engine: str
    page: Page
    context: BrowserContext
    browser: Browser
    playwright: Playwright
    device: Optional[str] = None
    viewport_size: Optional[tuple[int, int]] = None
    proxy_server: Optional[str] = None
    access_policy: Optional[SiteAccessPolicy] = None
    site_name: str = ""
    site_config: Optional[dict] = None

    def metadata(self) -> dict:
        """真实 runtime metadata，供结果 JSON / console 使用。"""
        size = None
        if self.viewport_size:
            size = {"width": self.viewport_size[0], "height": self.viewport_size[1]}
        return {
            "viewport": self.viewport,
            "engine": self.engine,
            "device": self.device,
            "viewport_size": size,
            "proxy_enabled": bool(self.proxy_server),
            "proxy_server": _safe_proxy_server(self.proxy_server) if self.proxy_server else None,
            "site": self.site_name,
            "site_access_policy": self.access_policy.type_name if self.access_policy else "none",
        }

    def close(self) -> None:
        """统一销毁：context → browser → playwright 驱动。"""
        errors = []
        try:
            if self.context is not None:
                self.context.close()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"context: {exc}")
        try:
            if self.browser is not None:
                self.browser.close()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"browser: {exc}")
        try:
            if self.playwright is not None:
                self.playwright.stop()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"playwright: {exc}")
        if errors:
            raise RuntimeError("; ".join(errors))


def create_browser(
    viewport: str,
    settings: Optional[dict] = None,
    site_name: Optional[str] = None,
) -> BrowserRuntime:
    """为 viewport（desktop | mobile）启动浏览器并返回运行时对象。

    调用方负责关闭返回的运行时（close_browser / runtime.close）。
    """
    settings = settings if settings is not None else load_settings()
    site_name = site_name or str(settings.get("default_site") or "mondressy")

    viewport_key = str(viewport).lower()
    browsers = settings.get("browsers") or {}
    if not isinstance(browsers, dict):
        raise BrowserConfigError("browsers config must be a mapping", category="BROWSER_CONFIG_ERROR")
    if viewport_key not in browsers:
        raise BrowserConfigError(
            f"unsupported viewport: {viewport} (supported: {', '.join(browsers)})",
            category="CLI_CONFIG_ERROR",
        )
    viewport_cfg = browsers[viewport_key]
    if not isinstance(viewport_cfg, dict):
        raise BrowserConfigError(
            f"browser config for {viewport_key} must be a mapping",
            category="BROWSER_CONFIG_ERROR",
        )
    engine = str(viewport_cfg.get("engine") or "chromium")
    if engine not in {"chromium", "firefox", "webkit"}:
        raise BrowserConfigError(
            f"unsupported browser engine: {engine}", category="BROWSER_CONFIG_ERROR"
        )

    site_config = load_site_config(site_name)
    try:
        resolve_url(site_config.get("base_url"), "site.base_url")
    except CliConfigError as exc:
        raise BrowserConfigError(str(exc), category=exc.category) from exc
    access_policy = create_site_access_policy(site_name, site_config)
    proxy = resolve_proxy(settings)
    proxy_server = _safe_proxy_server(proxy["server"]) if proxy else None

    pw = sync_playwright().start()
    browser = None
    context = None
    try:
        launcher = getattr(pw, engine)
        browser = launcher.launch(
            timeout=BROWSER_LAUNCH_TIMEOUT_MS,
            proxy=proxy,
        )
        device = None
        size = None
        if viewport_key == "desktop":
            vp = viewport_cfg.get("viewport") or {}
            width = int(vp.get("width") or 1440)
            height = int(vp.get("height") or 900)
            size = (width, height)
            context = browser.new_context(viewport={"width": width, "height": height})
        else:
            device_name = str(viewport_cfg.get("device") or "").strip()
            if not device_name:
                raise BrowserConfigError(
                    "mobile Playwright device is missing", category="BROWSER_CONFIG_ERROR"
                )
            try:
                profile = dict(pw.devices[device_name])
            except KeyError as exc:
                raise BrowserConfigError(
                    f"Playwright device config not found: {device_name}",
                    category="BROWSER_CONFIG_ERROR",
                ) from exc
            profile.pop("default_browser_type", None)  # new_context 不接受该键
            context = browser.new_context(**profile)
            size = (profile["viewport"]["width"], profile["viewport"]["height"])
            device = device_name

        access_policy.attach(context)  # 路由级 Signed Request 注入

        page = context.new_page()
        page.set_default_timeout(PAGE_NAV_TIMEOUT_MS)
        return BrowserRuntime(
            viewport=viewport_key,
            engine=engine,
            page=page,
            context=context,
            browser=browser,
            playwright=pw,
            device=device,
            viewport_size=size,
            proxy_server=proxy_server,
            access_policy=access_policy,
            site_name=site_name,
            site_config=site_config,
        )
    except Exception:
        if context is not None:
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            pw.stop()
        except Exception:  # noqa: BLE001
            pass
        raise


def close_browser(runtime: Optional[BrowserRuntime]) -> None:
    """统一关闭 BrowserRuntime（允许传 None）。"""
    if runtime is None:
        return
    try:
        runtime.close()
    except Exception:  # noqa: BLE001
        pass
