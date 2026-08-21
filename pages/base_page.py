"""页面对象基类。

职责：持有 page、加载 settings.default_site 指定的站点配置、
解析页面级选择器配置（含 desktop / mobile 分端）、拼接页面 URL、
统一创建 Locator 并提供 open() / wait 辅助。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from utils.browser import PAGE_NAV_TIMEOUT_MS, SITES_DIR, load_settings
from utils.config import load_yaml_mapping, resolve_url, site_config_path
from utils.errors import CliConfigError

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class SelectorNotFoundError(KeyError):
    """当前页面未配置该选择器名称。"""


class BasePage:
    """页面对象基类：统一持有 page、站点配置与视口，并提供选择器解析与基础导航。"""
    PAGE_NAME: str = ""

    def __init__(self, page, site_config: Optional[dict] = None, viewport: str = "desktop"):
        self.page = page
        self.viewport = viewport
        self.site_config = site_config if site_config is not None else self.load_site_config()

    # ------------------------------------------------------------------- 配置
    @staticmethod
    def load_site_config(path: Optional[str] = None, site_name: Optional[str] = None) -> dict:
        """加载显式 site；未传 site 时遵循 settings.default_site。"""
        if path:
            return load_yaml_mapping(Path(path), "site config")
        settings = load_settings()
        selected = site_name or str(settings.get("default_site") or "")
        try:
            cfg_path = site_config_path(SITES_DIR, selected)
            return load_yaml_mapping(cfg_path, "site config")
        except CliConfigError:
            raise

    def base_url(self) -> str:
        return resolve_url(self.site_config.get("base_url"), "site.base_url")

    def page_config(self) -> dict:
        pages = self.site_config.get("pages") or {}
        if self.PAGE_NAME not in pages:
            raise KeyError(f"no page config for '{self.PAGE_NAME}' in site config")
        return pages[self.PAGE_NAME]

    def page_url(self) -> str:
        url = str(self.page_config().get("url") or "")
        return url if url.startswith("http") else self.base_url() + url

    # -------------------------------------------------------------- 选择器
    def resolve_selector(self, name: str) -> dict:
        selectors = self.page_config().get("selectors") or {}
        if name not in selectors:
            raise SelectorNotFoundError(
                f"selector '{name}' not configured for page '{self.PAGE_NAME}'"
            )
        entry = selectors[name]
        # desktop/mobile 分端配置：{desktop: {by,value}, mobile: {by,value}}
        if isinstance(entry, dict) and "desktop" in entry and isinstance(entry["desktop"], dict):
            return entry.get(self.viewport) or entry["desktop"]
        return entry

    def locator(self, name: str):
        sel = self.resolve_selector(name)
        by = str(sel.get("by") or "css").lower()
        value = sel.get("value", "")
        if by == "css":
            return self.page.locator(value)
        if by == "xpath":
            return self.page.locator(f"xpath={value}")
        if by == "testid":
            return self.page.get_by_test_id(sel.get("value") or sel.get("name"))
        if by == "label":
            return self.page.get_by_label(
                sel.get("value") or sel.get("name"),
                exact=bool(sel.get("exact", False)),
            )
        if by == "placeholder":
            return self.page.get_by_placeholder(
                sel.get("value") or sel.get("name"),
                exact=bool(sel.get("exact", False)),
            )
        if by == "text":
            return self.page.get_by_text(
                sel.get("value") or sel.get("text"),
                exact=bool(sel.get("exact", False)),
            )
        if by == "role":
            role = sel.get("role")
            name = sel.get("name")
            if not role:
                # backward compatibility for the old "button Checkout" form
                parts = str(value).split(" ", 1)
                role = parts[0]
                name = name or (parts[1] if len(parts) > 1 else None)
            kwargs = {"name": name} if name else {}
            if "exact" in sel:
                kwargs["exact"] = bool(sel["exact"])
            return self.page.get_by_role(str(role), **kwargs)
        raise ValueError(f"unsupported selector 'by' type: {by}")

    def wait_visible(self, name: str, timeout: Optional[int] = None) -> None:
        self.locator(name).first.wait_for(
            state="visible", timeout=timeout or PAGE_NAV_TIMEOUT_MS
        )

    # -------------------------------------------------------------------- 打开
    def open(self, ready_selector: Optional[str] = None) -> None:
        self.page.goto(
            self.page_url(), wait_until="domcontentloaded", timeout=PAGE_NAV_TIMEOUT_MS
        )
        if ready_selector:
            self.wait_visible(ready_selector)
