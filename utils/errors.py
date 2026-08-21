"""统一运行时异常与敏感信息脱敏工具。

Exit code contract:
    0 = PASS
    1 = functional / blocked / infrastructure failure
    2 = CLI or runtime configuration error

异常消息会进入 Jenkins console 或 results.json，因而本模块不允许把
环境变量值、代理认证信息或本机绝对路径直接暴露给调用方。
"""

from __future__ import annotations

import os
import re
from typing import Optional


class CliConfigError(Exception):
    """命令行、配置或运行时配置错误，应映射为 Exit 2。"""

    def __init__(self, message: str, category: str = "CONFIG_ERROR"):
        super().__init__(message)
        self.category = category


class FrameworkFatalError(Exception):
    """框架级 fatal error，和业务 Case FAIL 分开记录。"""

    def __init__(self, message: str, classification: str = "RUNTIME_ERROR"):
        super().__init__(message)
        self.classification = classification


SENSITIVE_ENV_NAMES = (
    "MONDRESSY_US_SHOPIFY_SIGNATURE",
    "MONDRESSY_US_SHOPIFY_SIGNATURE_INPUT",
    "MONDRESSY_US_SHOPIFY_SIGNATURE_AGENT",
    "SHOPIFY_PROXY_PASSWORD",
    "PLAYWRIGHT_PROXY_PASSWORD",
)


def sanitize_message(message: object) -> str:
    """脱敏后返回可安全写入日志 / JSON 的错误消息。

    这里不删除变量名本身（例如缺失哪个环境变量仍需可诊断），只替换
    已存在的值、URI userinfo 和 Windows / Unix 绝对路径。
    """

    text = str(message or "")
    for name in SENSITIVE_ENV_NAMES:
        value = os.environ.get(name)
        if value:
            text = text.replace(value, "[REDACTED]")

    # 任意 URL userinfo 都不应出现在 Jenkins 日志中。
    text = re.sub(r"(https?://|socks5://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", text, flags=re.I)
    text = re.sub(r"(https?://|socks5://)[^\s/@]+@", r"\1[REDACTED]@", text, flags=re.I)

    # 避免 secret_file / 用户目录等本机绑定进入 results.json；不要把
    # 正常的 https:// URL 的斜杠误识别为本机路径。
    text = re.sub(r"(?i)(?<![A-Za-z])[A-Z]:[\\/][^\s'\"\n]+", "[REDACTED_PATH]", text)
    text = re.sub(
        r"(?i)(?<![A-Za-z0-9:])/(?:Users|home|tmp|var|opt|workspace|private|root)/[^\s'\"\n]+",
        "[REDACTED_PATH]",
        text,
    )
    return text


def config_category(exc: BaseException) -> Optional[str]:
    """读取配置异常分类；普通异常返回 None。"""

    return getattr(exc, "category", None) if isinstance(exc, CliConfigError) else None
