"""共享配置解析工具。

业务层只通过这里解析配置模板和 YAML，避免各模块自行 replace URL 模板
或以不同方式处理配置错误。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import yaml

from utils.errors import CliConfigError

URL_PREFIX = "@url:`"
SITE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def resolve_config_value(raw: Any, field: str = "value") -> str:
    """解析 ``@url:`...` `` 包装，返回实际配置值。"""

    if raw is None or not str(raw).strip():
        raise CliConfigError(f"missing required config: {field}")
    text = str(raw).strip()
    if text.startswith(URL_PREFIX):
        if not text.endswith("`"):
            raise CliConfigError(f"invalid template for {field}")
        text = text[len(URL_PREFIX) : -1]
    if not text:
        raise CliConfigError(f"empty config value for '{field}'")
    return text


def resolve_url(raw: Any, field: str = "url") -> str:
    """解析并校验 HTTP(S) URL；相对页面 path 不应调用此函数。"""

    value = resolve_config_value(raw, field)
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise CliConfigError(f"invalid URL for {field}", category="CONFIG_ERROR")
    if parsed.username or parsed.password:
        raise CliConfigError(f"URL userinfo is not allowed for {field}")
    return value.rstrip("/")


def load_yaml_mapping(path: Path, kind: str) -> dict:
    """读取并校验 mapping YAML，统一转换为 CliConfigError。"""

    if not path.exists():
        raise CliConfigError(f"{kind} not found", category=f"{kind.upper()}_NOT_FOUND")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:
        raise CliConfigError(f"invalid YAML in {kind}", category="YAML_PARSE_ERROR") from exc
    except (OSError, UnicodeError) as exc:
        raise CliConfigError(f"unable to read {kind}", category="CONFIG_ERROR") from exc
    if not isinstance(data, Mapping):
        raise CliConfigError(f"{kind} must be a mapping", category="CONFIG_SCHEMA_ERROR")
    return dict(data)


def site_config_path(sites_dir: Path, site_name: str) -> Path:
    """解析 site 文件名并阻止路径穿越。"""

    name = str(site_name or "").strip()
    if not SITE_NAME_RE.fullmatch(name):
        raise CliConfigError("invalid site name", category="SITE_CONFIG_ERROR")
    root = sites_dir.resolve()
    path = (root / f"{name}.yaml").resolve()
    if root not in path.parents:
        raise CliConfigError("site config path escapes configured directory", category="SITE_CONFIG_ERROR")
    return path
