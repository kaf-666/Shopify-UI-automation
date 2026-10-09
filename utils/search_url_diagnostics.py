"""Safe, failure-only URL evidence for the Search home-session assertion.

Only parsed URL metadata is persisted. Query and fragment values are never
returned, and path segments that can contain identities or credentials are
redacted before a diagnostic is attached to an exception or written to disk.
"""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

from utils.errors import sanitize_message
from utils.mutation_fingerprint import sanitize_pathname
from utils.result import write_results_json


DIAGNOSTIC_ATTRIBUTE = "_search_home_url_diagnostic"
DIAGNOSTIC_FILENAME_SUFFIX = "-search-home-url.json"
_CASE_ID_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,63}$")
_HOST_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))*$", re.I)
_EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
_OPAQUE_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_-]{24,}(?![A-Za-z0-9])")
_LONG_NUMBER_RE = re.compile(r"(?<!\d)\d{7,}(?!\d)")
_SENSITIVE_TITLE_VALUE_RE = re.compile(
    r"\b(cookie|authorization|token|signature|secret|password|email|phone)\s*[:=]\s*[^\s,;#&]+",
    re.I,
)
_FORMATTED_PHONE_RE = re.compile(r"(?<!\d)\+?\d[\d ().-]{6,}\d(?!\d)")
_SENSITIVE_PATH_MARKERS = frozenset(
    {
        "account", "accounts", "customer", "customers", "user", "users",
        "profile", "profiles", "order", "orders", "email", "emails",
        "phone", "phones", "session", "sessions", "token", "tokens",
        "signature", "signatures", "authorization", "address", "addresses",
    }
)
_MISMATCH_TYPES = frozenset(
    {
        "MATCH",
        "SCHEME_MISMATCH",
        "HOST_MISMATCH",
        "PORT_MISMATCH",
        "PATH_MISMATCH",
        "QUERY_MISMATCH",
        "FRAGMENT_MISMATCH",
        "URL_MISMATCH_UNCLASSIFIED",
        "INVALID_URL",
    }
)


def _parsed_url(raw_url: object):
    raw = str(raw_url or "")
    try:
        return raw, urlsplit(raw)
    except (TypeError, ValueError):
        return raw, None


def _safe_host(host: str) -> str:
    value = str(host or "").strip().lower()
    try:
        value = ipaddress.ip_address(value).compressed
    except ValueError:
        value = sanitize_message(value)
        if not _HOST_RE.fullmatch(value):
            return "[REDACTED_HOST]"
        # A syntactically valid DNS label can still be a bound credential.
        if "[REDACTED]" in value:
            return "[REDACTED_HOST]"
    return value or "[REDACTED_HOST]"


def _safe_origin(raw_url: object) -> str:
    _raw, parsed = _parsed_url(raw_url)
    if parsed is None or parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return "[REDACTED_ORIGIN]"
    host = _safe_host(parsed.hostname)
    if host == "[REDACTED_HOST]":
        return "[REDACTED_ORIGIN]"
    try:
        port = parsed.port
    except ValueError:
        return "[REDACTED_ORIGIN]"
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    port_part = f":{port}" if port is not None and port != default_port else ""
    host_part = f"[{host}]" if ":" in host else host
    origin = f"{parsed.scheme.lower()}://{host_part}{port_part}"
    safe_origin = sanitize_message(origin)
    return safe_origin if "[REDACTED]" not in safe_origin else "[REDACTED_ORIGIN]"


def _safe_pathname(raw_url: object) -> str:
    _raw, parsed = _parsed_url(raw_url)
    if parsed is None:
        return "/{token}"
    path = sanitize_pathname(parsed.path or "/")
    raw_segments = (parsed.path or "/").split("/")
    safe_segments = path.split("/")
    redact_remainder = False
    for index, raw_segment in enumerate(raw_segments):
        decoded = unquote(raw_segment).strip().lower()
        if redact_remainder and index < len(safe_segments):
            safe_segments[index] = "{token}"
        if decoded in _SENSITIVE_PATH_MARKERS:
            redact_remainder = True
    safe = "/".join(safe_segments) or "/"
    # Also protect any configured Signed Request value that happens to occur
    # in an otherwise ordinary-looking route segment.
    safe = sanitize_message(safe)
    if not safe.startswith("/"):
        safe = "/{token}"
    return safe


def _query_present(raw_url: str) -> bool:
    before_fragment = raw_url.split("#", 1)[0]
    return "?" in before_fragment


def _fragment_present(raw_url: str) -> bool:
    return "#" in raw_url


def _effective_port(parsed) -> int | None:
    if parsed is None:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if port is not None:
        return port
    return 443 if parsed.scheme.lower() == "https" else 80 if parsed.scheme.lower() == "http" else None


def _mismatch(actual_raw: str, actual, configured_raw: str, configured) -> tuple[str, dict]:
    if actual is None or configured is None:
        return "INVALID_URL", {
            "scheme": False,
            "host": False,
            "port": False,
            "path": False,
            "query": False,
            "fragment": False,
        }
    components = {
        "scheme": actual.scheme.lower() != configured.scheme.lower(),
        "host": (actual.hostname or "").lower() != (configured.hostname or "").lower(),
        "port": _effective_port(actual) != _effective_port(configured),
        "path": (actual.path or "/").rstrip("/") != (configured.path or "/").rstrip("/"),
        "query": actual.query != configured.query,
        "fragment": actual.fragment != configured.fragment,
    }
    for key, kind in (
        ("scheme", "SCHEME_MISMATCH"),
        ("host", "HOST_MISMATCH"),
        ("port", "PORT_MISMATCH"),
        ("path", "PATH_MISMATCH"),
        ("query", "QUERY_MISMATCH"),
        ("fragment", "FRAGMENT_MISMATCH"),
    ):
        if components[key]:
            return kind, components
    if actual_raw.rstrip("/") != configured_raw.rstrip("/"):
        return "URL_MISMATCH_UNCLASSIFIED", components
    return "MATCH", components


def _safe_title(value: object) -> str:
    title = sanitize_message(value)
    title = _EMAIL_RE.sub("[REDACTED_EMAIL]", title)
    title = _LONG_NUMBER_RE.sub("[REDACTED]", title)
    title = _OPAQUE_RE.sub("[REDACTED]", title)
    title = _SENSITIVE_TITLE_VALUE_RE.sub(r"\1=[REDACTED]", title)
    title = _FORMATTED_PHONE_RE.sub("[REDACTED_PHONE]", title)
    # A title may itself contain a URL; preserve useful surrounding text but
    # never copy URL query or fragment values into the artifact.
    title = re.sub(r"https?://[^\s]+", "[REDACTED_URL]", title, flags=re.I)
    return title[:160]


def _safe_case_id(value: object) -> str:
    candidate = str(value or "UNKNOWN")
    return candidate if _CASE_ID_RE.fullmatch(candidate) else "UNKNOWN"


def _safe_browser(value: object) -> str:
    candidate = str(value or "").strip().lower()
    return candidate if candidate in {"chromium", "firefox", "webkit"} else "UNKNOWN"


def _safe_viewport(value: object) -> str:
    candidate = str(value or "").strip().lower()
    return candidate if candidate in {"desktop", "mobile"} else "UNKNOWN"


def _page_value(getter, fallback=None):
    try:
        return getter(), None
    except Exception as exc:  # observation must never replace the Search error
        return fallback, type(exc).__name__


def build_search_home_url_diagnostic(
    *,
    page,
    actual_url: str,
    configured_url: str,
    viewport: str,
    browser: str | None,
    case_id: str | None,
    home,
) -> dict:
    """Collect a fixed, allowlisted snapshot without changing page state."""
    actual_raw, actual = _parsed_url(actual_url)
    configured_raw, configured = _parsed_url(configured_url)
    mismatch_type, components = _mismatch(actual_raw, actual, configured_raw, configured)

    title, title_error = _page_value(page.title, "")
    ready_state, ready_error = _page_value(
        lambda: page.evaluate("document.readyState"), "UNKNOWN"
    )
    trigger_visible, trigger_error = _page_value(
        lambda: bool(home.search_trigger().is_visible()), None
    )
    ready_state = ready_state if ready_state in {"loading", "interactive", "complete"} else "UNKNOWN"

    result = {
        "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "case_id": _safe_case_id(case_id),
        "viewport": _safe_viewport(viewport),
        "browser": _safe_browser(browser),
        "actual_origin": _safe_origin(actual_raw),
        "actual_pathname": _safe_pathname(actual_raw),
        "configured_origin": _safe_origin(configured_raw),
        "configured_pathname": _safe_pathname(configured_raw),
        "query_present": _query_present(actual_raw),
        "fragment_present": _fragment_present(actual_raw),
        "url_mismatch_type": mismatch_type,
        "mismatch_components": components,
        "current_page_title": _safe_title(title),
        "document_ready_state": ready_state,
        "search_trigger_visible": trigger_visible,
        "diagnostic_collection_status": "COLLECTED",
    }
    errors = {
        key: error
        for key, error in (
            ("title", title_error),
            ("ready_state", ready_error),
            ("search_trigger", trigger_error),
        )
        if error
    }
    if errors:
        result["observation_error_types"] = errors
    return result


def collection_failure_diagnostic(*, viewport: str, browser: str | None, case_id: str | None, error: Exception) -> dict:
    """Minimal safe evidence when the best-effort collector itself fails."""
    return {
        "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "case_id": _safe_case_id(case_id),
        "viewport": _safe_viewport(viewport),
        "browser": _safe_browser(browser),
        "actual_origin": "UNKNOWN",
        "actual_pathname": "UNKNOWN",
        "configured_origin": "UNKNOWN",
        "configured_pathname": "UNKNOWN",
        "query_present": None,
        "fragment_present": None,
        "url_mismatch_type": "INVALID_URL",
        "mismatch_components": {},
        "current_page_title": "UNKNOWN",
        "document_ready_state": "UNKNOWN",
        "search_trigger_visible": None,
        "diagnostic_collection_status": "FAILED",
        "diagnostic_collection_error_type": type(error).__name__,
    }


def safe_search_home_url_diagnostic(payload: object) -> dict:
    """Rebuild a diagnostic from a strict allowlist before JSON persistence."""
    if not isinstance(payload, dict):
        payload = {}
    mismatch = payload.get("url_mismatch_type")
    if mismatch not in _MISMATCH_TYPES:
        mismatch = "INVALID_URL"
    components_value = payload.get("mismatch_components")
    components = {}
    if isinstance(components_value, dict):
        components = {
            key: bool(components_value.get(key))
            for key in ("scheme", "host", "port", "path", "query", "fragment")
            if key in components_value
        }
    errors_value = payload.get("observation_error_types")
    observation_errors = {}
    if isinstance(errors_value, dict):
        observation_errors = {
            key: str(value)[:64]
            for key, value in errors_value.items()
            if key in {"title", "ready_state", "search_trigger"}
            and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", str(value))
        }
    query = payload.get("query_present")
    fragment = payload.get("fragment_present")
    trigger_visible = payload.get("search_trigger_visible")
    timestamp = str(payload.get("timestamp") or "")
    try:
        timestamp = datetime.fromisoformat(timestamp).isoformat(timespec="milliseconds")
    except ValueError:
        timestamp = "UNKNOWN"
    output = {
        "timestamp": timestamp,
        "case_id": _safe_case_id(payload.get("case_id")),
        "viewport": _safe_viewport(payload.get("viewport")),
        "browser": _safe_browser(payload.get("browser")),
        "actual_origin": _safe_origin(payload.get("actual_origin")),
        "actual_pathname": _safe_pathname(payload.get("actual_pathname")),
        "configured_origin": _safe_origin(payload.get("configured_origin")),
        "configured_pathname": _safe_pathname(payload.get("configured_pathname")),
        "query_present": query if isinstance(query, bool) else None,
        "fragment_present": fragment if isinstance(fragment, bool) else None,
        "url_mismatch_type": mismatch,
        "mismatch_components": components,
        "current_page_title": _safe_title(payload.get("current_page_title")),
        "document_ready_state": payload.get("document_ready_state")
        if payload.get("document_ready_state") in {"loading", "interactive", "complete"}
        else "UNKNOWN",
        "search_trigger_visible": trigger_visible if isinstance(trigger_visible, bool) else None,
        "diagnostic_collection_status": payload.get("diagnostic_collection_status")
        if payload.get("diagnostic_collection_status") in {"COLLECTED", "FAILED"}
        else "UNKNOWN",
    }
    error_type = payload.get("diagnostic_collection_error_type")
    if isinstance(error_type, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", error_type):
        output["diagnostic_collection_error_type"] = error_type
    if observation_errors:
        output["observation_error_types"] = observation_errors
    return output


def find_search_home_url_diagnostic(error: BaseException | None) -> dict | None:
    """Find the attached Search diagnostic through FlowError cause chains."""
    pending = [error] if error is not None else []
    seen: set[int] = set()
    while pending:
        current = pending.pop(0)
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        payload = getattr(current, DIAGNOSTIC_ATTRIBUTE, None)
        if isinstance(payload, dict):
            return safe_search_home_url_diagnostic(payload)
        pending.extend((getattr(current, "__cause__", None), getattr(current, "__context__", None)))
    return None


def write_search_home_url_diagnostic(
    payload: dict, artifact_dir: Path, viewport: str, case_id: str
) -> str:
    """Write a safe sidecar and return its relative path for results.json."""
    safe_viewport = _safe_viewport(viewport)
    safe_case_id = _safe_case_id(case_id)
    if safe_viewport == "UNKNOWN" or safe_case_id == "UNKNOWN":
        raise ValueError("invalid diagnostic artifact identity")
    relative = f"{safe_viewport}/{safe_case_id}{DIAGNOSTIC_FILENAME_SUFFIX}"
    write_results_json(
        safe_search_home_url_diagnostic(payload),
        Path(artifact_dir) / relative,
    )
    return relative
