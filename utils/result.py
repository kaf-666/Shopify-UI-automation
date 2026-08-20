"""结构化结果模型与 JSON 持久化。

提供统一的 Case 结果模型（SmokeCaseRunner 与后续 Case 集共用）、
视口级与运行级聚合、ISO 8601 时间戳与原子 JSON 写入。
本模块不包含任何业务 / 依赖判断逻辑。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

SCHEMA_VERSION = "1.0"


def iso_now() -> str:
    """ISO 8601 with timezone, millisecond precision."""
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def make_run_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


@dataclass
class CaseResult:
    """单个 Case 的执行结果（含时间、证据与失败分类）。"""
    case_id: str
    name: str
    status: str  # PASS | FAIL | BLOCKED
    started_at: str
    finished_at: str
    duration_ms: int
    detail: str = ""
    failure_classification: Optional[str] = None
    blocked_by: List[str] = field(default_factory=list)
    evidence: List[dict] = field(default_factory=list)
    evidence_capture_error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "name": self.name,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "detail": self.detail,
            "failure_classification": self.failure_classification,
            "blocked_by": list(self.blocked_by),
            "evidence": list(self.evidence),
        }


@dataclass
class ViewportResult:
    """单个视口（desktop / mobile）的聚合结果。"""
    viewport: str
    browser: dict  # {"engine": ..., "device": ...}
    status: str
    started_at: str
    finished_at: str
    duration_ms: int
    summary: dict
    pre_clean: dict
    cleanup: dict
    cases: List[CaseResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "viewport": self.viewport,
            "browser": dict(self.browser),
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "summary": dict(self.summary),
            "pre_clean": dict(self.pre_clean),
            "cleanup": dict(self.cleanup),
            "cases": [c.to_dict() for c in self.cases],
        }


@dataclass
class RunResult:
    """一次完整运行的顶层结果（含运行时元数据与汇总）。"""
    run_id: str
    site: str
    base_url: str
    started_at: str
    finished_at: str
    duration_ms: int
    overall_status: str
    runtime: dict
    summary: dict
    viewports: List[ViewportResult] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "site": self.site,
            "base_url": self.base_url,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "overall_status": self.overall_status,
            "runtime": dict(self.runtime),
            "summary": dict(self.summary),
            "viewports": [v.to_dict() for v in self.viewports],
        }


class ResultWriteError(Exception):
    """结果 JSON 写入失败（分类 RESULT_WRITE_FAILURE）。"""


def write_results_json(data: dict, path: Path) -> None:
    """Atomic JSON write (tmp file then os.replace), UTF-8, indent=2."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError as exc:
        raise ResultWriteError(str(exc)) from exc
