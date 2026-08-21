"""Website Smoke V1 stability records, history and summary calculations.

This module is deliberately independent of browser execution.  It consumes the
existing ``results.json`` contract and keeps stability state out of the
functional runner's exit-code path.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections import Counter
from importlib import metadata as importlib_metadata
from pathlib import Path
from platform import python_version
from typing import Any, Iterable, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEBSITE_ARTIFACT_ROOT = PROJECT_ROOT / "artifacts" / "website-smoke-v1"
HISTORY_FILENAME = "stability-history.jsonl"
STABILITY_SCHEMA_VERSION = 1
VALID_TRIGGERS = {"TIMER", "MANUAL", "SCM", "OTHER", "UNKNOWN"}
VALID_STABILITY_STATUSES = {
    "COLLECTING",
    "STABLE",
    "ACCESS_UNSTABLE",
    "FLAKY",
    "UNSTABLE",
    "MIXED_BASELINE",
}
GATE_STATES = {"PASS", "FAIL", "BLOCKED", "NOT_RUN", "PENDING", "UNKNOWN"}
FORBIDDEN_MARKERS = (
    ".shopify-monitor",
    "MONDRESSY_US_SHOPIFY_SIGNATURE=",
    "MONDRESSY_US_SHOPIFY_SIGNATURE_INPUT=",
    "MONDRESSY_US_SHOPIFY_SIGNATURE_AGENT=",
    "Signature: ",
    "Signature-Input: ",
    "Signature-Agent: ",
    "Proxy Password",
    "Cookie: ",
    "Set-Cookie: ",
)


def _int_value(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _float_value(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _counts(value: Any) -> dict[str, int]:
    value = value if isinstance(value, dict) else {}
    return {
        "pass": _int_value(value.get("pass")),
        "fail": _int_value(value.get("fail")),
        "blocked": _int_value(value.get("blocked")),
    }


def _normalize_gate(value: Any, default: str = "NOT_RUN") -> str:
    normalized = str(value or default).strip().upper()
    return normalized if normalized in GATE_STATES else "UNKNOWN"


def _normalize_result(value: Any, overall_status: str = "") -> str:
    normalized = str(value or "").strip().upper()
    if normalized in {"SUCCESS", "FAILURE", "UNSTABLE", "ABORTED"}:
        return normalized
    return "SUCCESS" if overall_status == "PASS" else "FAILURE"


def _normalize_trigger(value: Any) -> str:
    normalized = str(value or "").strip().upper()
    return normalized if normalized in VALID_TRIGGERS else "UNKNOWN"


def trigger_from_environment(environ: Optional[dict[str, str]] = None) -> str:
    """Safely map common Jenkins cause markers to the stable trigger enum."""
    env = environ if environ is not None else os.environ
    explicit = env.get("STABILITY_TRIGGER") or env.get("BUILD_TRIGGER")
    if explicit:
        return _normalize_trigger(explicit)

    cause_text = " ".join(
        str(env.get(name) or "")
        for name in ("BUILD_CAUSE", "BUILD_CAUSE_TIMERTRIGGER", "JENKINS_BUILD_CAUSE")
    ).upper()
    if "TIMER" in cause_text:
        return "TIMER"
    if "SCM" in cause_text or "GIT" in cause_text:
        return "SCM"
    if "USER" in cause_text or "MANUAL" in cause_text:
        return "MANUAL"
    return "UNKNOWN"


def _safe_playwright_version() -> str:
    try:
        return importlib_metadata.version("playwright")
    except importlib_metadata.PackageNotFoundError:
        return "unknown"


def _read_commit(environ: Optional[dict[str, str]] = None) -> str:
    env = environ if environ is not None else os.environ
    for name in ("GIT_COMMIT", "GIT_COMMIT_SHA", "CHANGE_SHA"):
        value = str(env.get(name) or "").strip()
        if value:
            return value
    return ""


def _build_number(environ: Optional[dict[str, str]] = None) -> int:
    env = environ if environ is not None else os.environ
    return _int_value(env.get("BUILD_NUMBER"))


def _source_viewport(results: dict, requested_viewport: Optional[str]) -> str:
    requested = str(requested_viewport or "").strip().lower()
    if requested in {"desktop", "mobile", "both"}:
        return requested
    viewports = results.get("viewports") or []
    names = {str(item.get("viewport") or "").lower() for item in viewports if isinstance(item, dict)}
    if names == {"desktop", "mobile"}:
        return "both"
    if names == {"desktop"}:
        return "desktop"
    if names == {"mobile"}:
        return "mobile"
    return "unknown"


def _viewport_by_name(results: dict, name: str) -> Optional[dict]:
    for viewport in results.get("viewports") or []:
        if isinstance(viewport, dict) and viewport.get("viewport") == name:
            return viewport
    return None


def _aggregate_gate(results: dict, field: str) -> str:
    viewports = results.get("viewports") or []
    statuses = [
        str((viewport.get(field) or {}).get("status") or "NOT_RUN").upper()
        for viewport in viewports
        if isinstance(viewport, dict)
    ]
    if not statuses:
        return "NOT_RUN"
    if "FAIL" in statuses:
        return "FAIL"
    if "BLOCKED" in statuses:
        return "BLOCKED"
    return "PASS" if all(status == "PASS" for status in statuses) else "NOT_RUN"


def _failure_classification(case: dict) -> str:
    value = str(case.get("failure_classification") or "").strip()
    return value or "UNKNOWN_FAILURE"


def _failure_entries(results: dict) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for viewport in results.get("viewports") or []:
        if not isinstance(viewport, dict):
            continue
        viewport_name = str(viewport.get("viewport") or "unknown")
        for case in viewport.get("cases") or []:
            if not isinstance(case, dict) or case.get("status") == "PASS":
                continue
            entries.append(
                {
                    "viewport": viewport_name,
                    "case_id": str(case.get("case_id") or "UNKNOWN_CASE"),
                    "status": str(case.get("status") or "UNKNOWN"),
                    "classification": _failure_classification(case),
                }
            )

    fatal = results.get("fatal_error")
    if isinstance(fatal, dict) and fatal.get("classification"):
        entries.append(
            {
                "viewport": "run",
                "case_id": "__RUN__",
                "status": "FAIL",
                "classification": str(fatal.get("classification")),
            }
        )
    return entries


def _http_statuses(text: str) -> set[str]:
    patterns = (
        r"\b(?:http(?:[_\s-]?status)?|status(?:[_\s-]?code)?|response[_\s-]?status)\D{0,14}(403|429|5\d{2})\b",
        r"\b(403|429|5\d{2})\s+(?:Forbidden|Too\s+Many\s+Requests|Server\s+Error)\b",
    )
    hits: set[str] = set()
    for pattern in patterns:
        hits.update(re.findall(pattern, text, flags=re.IGNORECASE))
    return hits


def _http_metrics(results: dict) -> dict[str, int]:
    """Count status indicators once per result section/status.

    The source is only the existing result artifact.  No network request is
    made for telemetry.
    """
    counts = {"403": 0, "429": 0, "5xx": 0}
    sections: list[Any] = []
    for viewport in results.get("viewports") or []:
        if not isinstance(viewport, dict):
            continue
        sections.extend([viewport.get("pre_clean"), viewport.get("cleanup")])
        sections.extend(viewport.get("cases") or [])
    if results.get("fatal_error"):
        sections.append(results.get("fatal_error"))

    for section in sections:
        text = json.dumps(section, ensure_ascii=False, sort_keys=True)
        for status in _http_statuses(text):
            if status == "403":
                counts["403"] += 1
            elif status == "429":
                counts["429"] += 1
            elif status.startswith("5"):
                counts["5xx"] += 1
    return counts


def _case_statuses(results: dict) -> list[dict[str, str]]:
    values: list[dict[str, str]] = []
    for viewport in results.get("viewports") or []:
        if not isinstance(viewport, dict):
            continue
        viewport_name = str(viewport.get("viewport") or "unknown")
        for case in viewport.get("cases") or []:
            if not isinstance(case, dict):
                continue
            values.append(
                {
                    "viewport": viewport_name,
                    "case_id": str(case.get("case_id") or "UNKNOWN_CASE"),
                    "status": str(case.get("status") or "UNKNOWN"),
                }
            )
    return values


def _eligible(results: dict, viewport: str) -> tuple[bool, str]:
    if viewport != "both":
        return False, "viewport is not both"
    if results.get("fatal_error") is not None:
        return False, "fatal_error is present"
    viewports = results.get("viewports") or []
    names = {str(item.get("viewport") or "") for item in viewports if isinstance(item, dict)}
    if names != {"desktop", "mobile"}:
        return False, "desktop and mobile results are not both present"
    if any(
        not isinstance(item, dict)
        or _int_value((item.get("summary") or {}).get("total"), -1) != 15
        for item in viewports
    ):
        return False, "one or more viewport gates are incomplete"
    return True, "complete both run"


def _relative_source_path(result_path: Path) -> str:
    try:
        return result_path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return result_path.name


def build_stability_record(
    results: dict,
    result_path: Path,
    *,
    requested_viewport: Optional[str] = None,
    environ: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Convert an existing Website Smoke V1 result into a safe record."""
    env = environ if environ is not None else os.environ
    viewport = _source_viewport(results, requested_viewport)
    eligible, eligibility_reason = _eligible(results, viewport)
    overall_status = str(results.get("overall_status") or "FAIL").upper()
    vps = {
        name: _counts((_viewport_by_name(results, name) or {}).get("summary"))
        for name in ("desktop", "mobile")
    }
    combined = _counts(results.get("summary"))
    failures = _failure_entries(results)
    failure_classifications = [entry["classification"] for entry in failures]
    http = _http_metrics(results)
    gate_default = "NOT_RUN"
    # Gate values are supplied by Jenkins after the corresponding gate has
    # completed.  Local record generation remains useful without Jenkins.
    schema_gate = _normalize_gate(env.get("STABILITY_SCHEMA_GATE"), gate_default)
    secret_gate = _normalize_gate(env.get("STABILITY_SECRET_GATE"), gate_default)
    artifact_gate = _normalize_gate(env.get("STABILITY_ARTIFACT_GATE"), "PASS")

    duration_ms = _int_value(results.get("duration_ms"))
    return {
        "schema_version": STABILITY_SCHEMA_VERSION,
        "suite": "website-smoke-v1",
        "run_id": str(results.get("run_id") or result_path.parent.name),
        "eligible": eligible,
        "eligibility_reason": eligibility_reason,
        "build_number": _build_number(env),
        "commit_sha": _read_commit(env),
        "viewport": viewport,
        "trigger": trigger_from_environment(env),
        "started_at": str(results.get("started_at") or ""),
        "finished_at": str(results.get("finished_at") or ""),
        "duration_seconds": round(duration_ms / 1000, 3),
        "python_version": python_version(),
        "playwright_version": _safe_playwright_version(),
        "desktop": vps["desktop"],
        "mobile": vps["mobile"],
        "combined": combined,
        "pre_clean": _aggregate_gate(results, "pre_clean"),
        "cleanup": _aggregate_gate(results, "cleanup"),
        "http": http,
        "failure_cases": failures,
        "failure_classifications": failure_classifications,
        "case_statuses": _case_statuses(results),
        "automation_defect_count": sum(
            1 for entry in failures if _is_automation_defect(entry["classification"])
        ),
        "schema_gate": schema_gate,
        "secret_gate": secret_gate,
        "artifact_gate": artifact_gate,
        "python_exit_code": _int_value(
            env.get("STABILITY_PYTHON_EXIT_CODE"),
        )
        if str(env.get("STABILITY_PYTHON_EXIT_CODE") or "").strip().isdigit()
        else (0 if overall_status == "PASS" else 1),
        "jenkins_result": _normalize_result(
            env.get("STABILITY_JENKINS_RESULT"), overall_status
        ),
        "overall_status": overall_status,
        "source_results": _relative_source_path(result_path),
    }


def _is_automation_defect(value: Any) -> bool:
    return bool(re.search(r"AUTOMATION[\s_-]+DEFECT", str(value or "").upper()))


def _is_access_failure(entry: dict[str, Any]) -> bool:
    text = " ".join(str(entry.get(key) or "") for key in ("classification", "detail")).upper()
    return bool(
        re.search(r"ACCESS|CLOUDFLARE|HTTP[_ -]?(403|429|5\d{2})|\b(403|429|5\d{2})\b", text)
    )


def _record_failures(record: dict) -> list[dict[str, str]]:
    raw = record.get("failure_cases") or []
    values: list[dict[str, str]] = []
    for item in raw:
        if isinstance(item, dict):
            values.append(
                {
                    "viewport": str(item.get("viewport") or "unknown"),
                    "case_id": str(item.get("case_id") or "UNKNOWN_CASE"),
                    "status": str(item.get("status") or "FAIL"),
                    "classification": str(item.get("classification") or "UNKNOWN_FAILURE"),
                }
            )
        elif item:
            values.append(
                {
                    "viewport": "unknown",
                    "case_id": str(item),
                    "status": "FAIL",
                    "classification": "UNKNOWN_FAILURE",
                }
            )
    return values


def is_record_eligible(record: dict) -> bool:
    """Return whether a record may enter the stability window."""
    if "eligible" in record:
        return bool(record.get("eligible")) and record.get("viewport") == "both"
    return record.get("viewport") == "both" and record.get("suite") == "website-smoke-v1"


def _record_sort_key(record: dict) -> tuple[str, int, str]:
    return (
        str(record.get("finished_at") or record.get("started_at") or ""),
        _int_value(record.get("build_number")),
        str(record.get("run_id") or ""),
    )


def _record_commits(records: Iterable[dict]) -> set[str]:
    return {str(record.get("commit_sha") or "").strip() for record in records if record.get("commit_sha")}


def _status_for_records(records: list[dict], target: int, strict_mixed: bool = False) -> str:
    if strict_mixed:
        commits = _record_commits(records)
        if len(commits) > 1:
            return "MIXED_BASELINE"
    if len(records) < target:
        return "COLLECTING"

    stable = True
    any_access_signal = False
    any_non_access_failure = False
    any_gate_failure = False
    case_failure_counts: Counter[str] = Counter()
    for record in records:
        combined = _counts(record.get("combined"))
        if not (
            record.get("jenkins_result") == "SUCCESS"
            and combined == {"pass": 30, "fail": 0, "blocked": 0}
            and _counts(record.get("desktop")) == {"pass": 15, "fail": 0, "blocked": 0}
            and _counts(record.get("mobile")) == {"pass": 15, "fail": 0, "blocked": 0}
            and record.get("pre_clean") == "PASS"
            and record.get("cleanup") == "PASS"
            and record.get("schema_gate") == "PASS"
            and record.get("secret_gate") == "PASS"
            and record.get("artifact_gate") == "PASS"
            and _int_value((record.get("http") or {}).get("403")) == 0
            and _int_value((record.get("http") or {}).get("429")) == 0
            and _int_value((record.get("http") or {}).get("5xx")) == 0
            and _int_value(record.get("automation_defect_count")) == 0
            and not _record_failures(record)
        ):
            stable = False

        gate_values = (record.get("schema_gate"), record.get("secret_gate"), record.get("artifact_gate"))
        any_gate_failure = any_gate_failure or any(value != "PASS" for value in gate_values)
        http = record.get("http") or {}
        any_access_signal = any_access_signal or any(_int_value(http.get(key)) > 0 for key in ("403", "429", "5xx"))
        for entry in _record_failures(record):
            if _is_access_failure(entry):
                any_access_signal = True
            else:
                any_non_access_failure = True
                case_failure_counts[entry["case_id"]] += 1
        if record.get("jenkins_result") not in {"SUCCESS", "UNSTABLE"} and not _record_failures(record):
            any_non_access_failure = True

    if stable:
        return "STABLE"
    if any_non_access_failure or any_gate_failure:
        if any(count >= 2 for count in case_failure_counts.values()):
            return "FLAKY"
        return "UNSTABLE"
    if any_access_signal:
        return "ACCESS_UNSTABLE"
    return "UNSTABLE"


def summarize_records(
    records: Iterable[dict],
    *,
    last: int = 10,
    baseline_commit: Optional[str] = None,
    strict_mixed: bool = False,
) -> dict[str, Any]:
    """Summarize the latest N eligible records on one code baseline."""
    target = max(1, int(last))
    all_records = [record for record in records if isinstance(record, dict)]
    all_records.sort(key=_record_sort_key)
    commits = _record_commits(all_records)
    requested_baseline = str(baseline_commit or "").strip()
    if requested_baseline:
        baseline = requested_baseline
    else:
        baseline = ""
        for record in reversed(all_records):
            candidate = str(record.get("commit_sha") or "").strip()
            if candidate:
                baseline = candidate
                break

    baseline_records = [record for record in all_records if str(record.get("commit_sha") or "").strip() == baseline]
    eligible = [record for record in baseline_records if is_record_eligible(record)]
    selected = eligible[-target:]
    mixed_baseline = strict_mixed and not requested_baseline and len(commits) > 1
    status = (
        "MIXED_BASELINE"
        if mixed_baseline
        else _status_for_records(selected, target, strict_mixed=False)
    )
    return {
        "baseline_commit": baseline,
        "all_commits": sorted(commits),
        "mixed_commits_ignored": max(0, len(commits) - (1 if baseline else 0)),
        "eligible_builds": len(selected),
        "eligible_builds_on_baseline": len(eligible),
        "target_builds": target,
        "records": selected,
        "status": status,
    }


def load_history(path: Path) -> tuple[list[dict], list[str]]:
    """Load JSONL records, returning valid records and non-fatal parse errors."""
    if not path.exists():
        return [], []
    records: list[dict] = []
    errors: list[str] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                errors.append(f"line {line_number} is not valid JSON")
                continue
            if not isinstance(value, dict):
                errors.append(f"line {line_number} is not a JSON object")
                continue
            records.append(value)
    return records, errors


def load_archived_records(root: Path) -> list[dict]:
    if not root.exists():
        return []
    records: list[dict] = []
    for path in sorted(root.rglob("stability_record.json")):
        try:
            with path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
            if isinstance(value, dict):
                records.append(value)
        except (OSError, json.JSONDecodeError):
            continue
    return records


def _record_key(record: dict) -> tuple[str, str, str]:
    return (
        str(record.get("run_id") or ""),
        str(record.get("source_results") or ""),
        f"{record.get('build_number', 0)}:{record.get('commit_sha', '')}:{record.get('finished_at', '')}",
    )


def merge_records(*record_groups: Iterable[dict]) -> list[dict]:
    merged: dict[tuple[str, str, str], dict] = {}
    for group in record_groups:
        for record in group:
            if isinstance(record, dict):
                merged[_record_key(record)] = record
    values = list(merged.values())
    values.sort(key=_record_sort_key)
    return values


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def atomic_write_json(path: Path, value: dict) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n")


def write_history_record(path: Path, record: dict) -> None:
    """Atomically append or replace one run's JSONL history line."""
    existing_lines: list[str] = []
    if path.exists():
        existing_lines = path.read_text(encoding="utf-8").splitlines()

    key = _record_key(record)
    output: list[str] = []
    replaced = False
    for line in existing_lines:
        if not line.strip():
            continue
        try:
            previous = json.loads(line)
        except json.JSONDecodeError:
            output.append(line)
            continue
        if isinstance(previous, dict) and _record_key(previous) == key:
            output.append(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            replaced = True
        else:
            output.append(line)
    if not replaced:
        output.append(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
    atomic_write_text(path, "\n".join(output) + "\n")


def serialize_without_forbidden_markers(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def contains_forbidden_marker(value: Any) -> Optional[str]:
    blob = serialize_without_forbidden_markers(value)
    for marker in FORBIDDEN_MARKERS:
        if marker in blob:
            return marker
    return None


def duration_stats(records: Iterable[dict]) -> dict[str, float]:
    values = sorted(_float_value(record.get("duration_seconds")) for record in records)
    if not values:
        return {"average": 0.0, "p95": 0.0, "min": 0.0, "max": 0.0}
    rank = max(0, int((len(values) * 0.95) - 1))
    return {
        "average": round(sum(values) / len(values), 3),
        "p95": round(values[rank], 3),
        "min": round(values[0], 3),
        "max": round(values[-1], 3),
    }


def aggregate_status_counts(records: Iterable[dict], viewport: str) -> dict[str, int]:
    total = {"pass": 0, "fail": 0, "blocked": 0}
    for record in records:
        values = _counts(record.get(viewport))
        for key in total:
            total[key] += values[key]
    return total


def gate_failure_count(records: Iterable[dict], field: str) -> int:
    return sum(1 for record in records if record.get(field) != "PASS")


def http_totals(records: Iterable[dict]) -> dict[str, int]:
    total = {"403": 0, "429": 0, "5xx": 0}
    for record in records:
        values = record.get("http") or {}
        for key in total:
            total[key] += _int_value(values.get(key))
    return total


def failure_frequencies(records: Iterable[dict]) -> tuple[Counter[str], Counter[str]]:
    case_counts: Counter[str] = Counter()
    classification_counts: Counter[str] = Counter()
    for record in records:
        for entry in _record_failures(record):
            case_counts[entry["case_id"]] += 1
            classification_counts[entry["classification"]] += 1
    return case_counts, classification_counts


def case_stability(records: Iterable[dict]) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for record in records:
        for item in record.get("case_statuses") or []:
            if not isinstance(item, dict):
                continue
            case_id = str(item.get("case_id") or "UNKNOWN_CASE")
            status = str(item.get("status") or "UNKNOWN").lower()
            if status not in {"pass", "fail", "blocked"}:
                continue
            counts.setdefault(case_id, {"pass": 0, "fail": 0, "blocked": 0})[status] += 1
    return counts
