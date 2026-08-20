"""页面对象基类。

职责：持有 page、加载站点配置（configs/sites/mondressy.yaml）、
解析页面级选择器配置（含 desktop / mobile 分端）、拼接页面 URL、
统一创建 Locator 并提供 open() / wait 辅助。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml

from utils.browser import PAGE_NAV_TIMEOUT_MS, resolve_template

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SITE_CFG_PATH = PROJECT_ROOT / "configs" / "sites" / "mondressy.yaml"


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
    def load_site_config(path: Optional[str] = None) -> dict:
        cfg_path = Path(path) if path else SITE_CFG_PATH
        if not cfg_path.exists():
            raise FileNotFoundError(f"site config not found: {cfg_path}")
        with open(cfg_path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            raise ValueError(f"site config must be a mapping: {cfg_path}")
        return data

    def base_url(self) -> str:
        return resolve_template(str(self.site_config.get("base_url") or ""), "base_url")

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
        if by == "text":
            return self.page.get_by_text(value)
        if by == "role":
            parts = str(value).split(" ", 1)
            kwargs = {"name": parts[1]} if len(parts) > 1 else {}
            return self.page.get_by_role(parts[0], **kwargs)
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
