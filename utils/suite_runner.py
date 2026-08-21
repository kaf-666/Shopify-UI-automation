"""各正式 Suite Runner 共用的轻量运行契约工具。

不改变各 Suite 的 Case 类和 CLI 文件，只集中处理 Exit Code、fatal artifact
和真实 BrowserRuntime metadata。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import List, Optional

from utils.errors import CliConfigError, sanitize_message
from utils.result import RunResult, ResultWriteError, ViewportResult, iso_now, write_results_json


def exit_code_for_exception(exc: BaseException) -> int:
    """统一 Exit Code：配置错误 2，其余 Suite / 业务 / 环境失败 1。"""
    return 2 if isinstance(exc, CliConfigError) else 1


def exception_classification(exc: BaseException) -> str:
    if isinstance(exc, CliConfigError):
        return getattr(exc, "category", "CONFIG_ERROR")
    return "RUNTIME_ERROR"


def runtime_metadata(runtime) -> dict:
    """只从 BrowserRuntime 读取报告元数据，避免手工写假设备信息。"""
    return runtime.metadata()


def write_fatal_results(
    artifact_dir: Path,
    run_id: str,
    site: str = "",
    base_url: str = "",
    started_at: Optional[str] = None,
    started: Optional[float] = None,
    viewports: Optional[List[ViewportResult]] = None,
    runtime: Optional[dict] = None,
    exc: Optional[BaseException] = None,
    classification: Optional[str] = None,
) -> bool:
    """写入 framework fatal 结果；artifact 目录不可用时由调用方打印 console。"""
    viewports = list(viewports or [])
    counts = {
        "pass": sum(v.summary.get("pass", 0) for v in viewports),
        "fail": sum(v.summary.get("fail", 0) for v in viewports),
        "blocked": sum(v.summary.get("blocked", 0) for v in viewports),
    }
    result = RunResult(
        run_id=run_id,
        site=site,
        base_url=base_url,
        started_at=started_at or iso_now(),
        finished_at=iso_now(),
        duration_ms=int(((time.perf_counter() - started) if started else 0) * 1000),
        overall_status="FAIL",
        runtime=runtime or {},
        summary={**counts, "total": sum(v.summary.get("total", 0) for v in viewports)},
        viewports=viewports,
        fatal_error={
            "classification": classification or exception_classification(exc or RuntimeError()),
            "message": sanitize_message(exc or "framework fatal error"),
        },
    )
    try:
        write_results_json(result.to_dict(), artifact_dir / "results.json")
        return True
    except ResultWriteError as write_exc:
        print(f"RESULT_WRITE_FAILURE: {sanitize_message(write_exc)}")
        return False


def report_fatal(exc: BaseException) -> int:
    """统一 console fatal 摘要与退出码。"""
    classification = exception_classification(exc)
    print(f"FATAL_ERROR [{classification}]: {sanitize_message(exc)}")
    return exit_code_for_exception(exc)


def guarded_main(main_func, argv=None) -> int:
    """CLI 入口保护层：未捕获配置异常统一 Exit 2，其他异常 Exit 1。"""
    try:
        return main_func(argv)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - final CLI boundary
        # Formal runners expose ARTIFACT_ROOT. If the failure happened after
        # CLI entry, preserve the fatal result contract even when their legacy
        # orchestration code did not reach its normal JSON write block.
        try:
            root = main_func.__globals__.get("ARTIFACT_ROOT")
            if root:
                root = Path(root)
                run_id = f"fatal_{time.strftime('%Y%m%d_%H%M%S')}"
                artifact_dir = root / run_id
                artifact_dir.mkdir(parents=True, exist_ok=True)
                write_fatal_results(artifact_dir, run_id, exc=exc)
        except Exception:
            # The console classification remains authoritative if artifact
            # creation itself is unavailable.
            pass
        return report_fatal(exc)
