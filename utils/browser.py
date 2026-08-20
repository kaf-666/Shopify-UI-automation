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

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml
from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

from utils.site_access import SiteAccessPolicy, create_site_access_policy

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = PROJECT_ROOT / "configs" / "settings.yaml"
SITES_DIR = PROJECT_ROOT / "configs" / "sites"
PAGE_NAV_TIMEOUT_MS = 45_000
BROWSER_LAUNCH_TIMEOUT_MS = 30_000


class BrowserConfigError(Exception):
    """浏览器配置缺失或非法。"""


def load_settings() -> dict:
    """读取 configs/settings.yaml 为字典。"""
    if not SETTINGS_PATH.exists():
        raise BrowserConfigError(f"settings not found: {SETTINGS_PATH}")
    with open(SETTINGS_PATH, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data


def resolve_template(raw: str, field: str = "value") -> str:
    """解析 @url:`...` 模板 → 实际值。"""
    if raw is None or not str(raw).strip():
        raise BrowserConfigError(f"empty config value for '{field}'")
    text = str(raw)
    prefix = "@url:`"
    if text.startswith(prefix) and text.endswith("`"):
        return text[len(prefix):-1]
    return text


def load_site_config(site_name: str) -> dict:
    """读取 configs/sites/<site_name>.yaml 为字典。"""
    path = SITES_DIR / f"{site_name}.yaml"
    if not path.exists():
        raise BrowserConfigError(f"site config not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data


def resolve_base_url(raw: str) -> str:
    """从 settings.yaml 解析 base_url 模板。"""
    return resolve_template(raw, "base_url")


def resolve_proxy(settings: dict) -> Optional[dict]:
    """从全局 proxy 配置构建 Playwright proxy 字典。"""
    proxy = settings.get("proxy") or {}
    if not proxy.get("enabled"):
        return None
    server = resolve_template(str(proxy.get("server") or ""), "proxy.server")
    result = {"server": server}
    username = proxy.get("username")
    password = proxy.get("password")
    if username and password:
        result["username"] = username
        result["password"] = password
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
    site_config = load_site_config(site_name)
    access_policy = create_site_access_policy(site_name, site_config)
    proxy = resolve_proxy(settings)
    proxy_server = proxy["server"] if proxy else None

    viewport_key = str(viewport).lower()
    browsers = settings.get("browsers") or {}
    if viewport_key not in browsers:
        raise BrowserConfigError(
            f"unsupported viewport: {viewport} (supported: {', '.join(browsers)})"
        )
    viewport_cfg = browsers[viewport_key]
    engine = str(viewport_cfg.get("engine") or "chromium")

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
            device_name = str(viewport_cfg.get("device") or "iPhone 14")
            profile = dict(pw.devices[device_name])
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
