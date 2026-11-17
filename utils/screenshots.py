"""轻量失败证据截图。

只负责截图捕获、路径生成与目录创建。
由 Case Runner 决定何时调用；捕获失败被隔离
（返回 None，绝不把异常抛进 Case 逻辑）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional
from utils.safe_artifact_write import write_run_bytes


def capture_case_failure(
    page,
    artifact_dir,
    viewport: str,
    case_id: str,
) -> Optional[str]:
    """为失败的 Case 保存截图，返回相对产物目录的路径（如 desktop/SMOKE-PDP-02-failure.png），捕获失败返回 None。

    优先整页截图，失败时回退为视口截图。
    """
    try:
        rel = f"{viewport}/{case_id}-failure.png"
        try:
            data = page.screenshot(full_page=True)
        except Exception:
            data = page.screenshot(full_page=False)
        write_run_bytes(data, artifact_dir, rel)
        return rel
    except Exception:
        return None
