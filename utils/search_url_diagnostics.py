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
from utils.result import write_results_json


DIAGNOSTIC_ATTRIBUTE = "_search_home_url_diagnostic"
DIAGNOSTIC_FILENAME_SUFFIX = "-search-home-url.json"
_CASE_ID_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,63}$")
_HOST_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))*$", re.I)
_TITLE_CATEGORIES = frozenset(
    {"HOME", "SEARCH", "COLLECTION", "PRODUCT", "CHALLENGE", "ERROR", "UNKNOWN"}
)
_PATH_CATEGORIES = frozenset(
    {
        "HOME", "SEARCH", "COLLECTION", "PRODUCT", "ACCOUNT", "CART",
        "CHALLENGE", "ERROR", "PAGE", "BLOG", "POLICY", "UNKNOWN",
    }
)
_PATH_ROUTE_CATEGORIES = {
    "search": "SEARCH",
    "collection": "COLLECTION",
    "collections": "COLLECTION",
    "product": "PRODUCT",
    "products": "PRODUCT",
    "account": "ACCOUNT",
    "accounts": "ACCOUNT",
    "customer": "ACCOUNT",
    "customers": "ACCOUNT",
    "member": "ACCOUNT",
    "members": "ACCOUNT",
    "user": "ACCOUNT",
    "users": "ACCOUNT",
    "profile": "ACCOUNT",
    "profiles": "ACCOUNT",
    "order": "ACCOUNT",
    "orders": "ACCOUNT",
    "cart": "CART",
    "challenge": "CHALLENGE",
    "cdn-cgi": "CHALLENGE",
    "error": "ERROR",
    "404": "ERROR",
    "500": "ERROR",
    "page": "PAGE",
    "pages": "PAGE",
    "blog": "BLOG",
    "blogs": "BLOG",
    "policies": "POLICY",
}
_CHALLENGE_TITLE_MARKERS = (
    "cloudflare",
    "just a moment",
    "checking your browser",
    "verify you are human",
    "attention required",
    "challenge",
)
_ERROR_TITLE_MARKERS = (
    "page not found",
    "not found",
    "access denied",
    "service unavailable",
    "temporarily unavailable",
    "error",
    "404",
    "500",
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


def _decoded_path(path: str) -> str:
    """Decode only for classification; decoded path text is never persisted."""
    value = path
    for _ in range(4):
        decoded = unquote(value)
        if decoded == value:
            break
        value = decoded
    return value


def _path_metadata(raw_url: object) -> dict:
    """Return route metadata while dropping every non-root pathname value."""
    _raw, parsed = _parsed_url(raw_url)
    if parsed is None:
        return {
            "pathname": "/[REDACTED]",
            "path_is_root": False,
            "path_category": "UNKNOWN",
            "path_segment_count": None,
            "pathname_redacted": True,
        }

    path = parsed.path or "/"
    if path == "/":
        return {
            "pathname": "/",
            "path_is_root": True,
            "path_category": "HOME",
            "path_segment_count": 0,
            "pathname_redacted": False,
        }

    decoded = _decoded_path(path)
    segments = [segment for segment in decoded.split("/") if segment]
    first_segment = segments[0].strip().casefold() if segments else ""
    return {
        "pathname": "/[REDACTED]",
        "path_is_root": False,
        "path_category": _PATH_ROUTE_CATEGORIES.get(first_segment, "UNKNOWN"),
        "path_segment_count": min(len(segments), 1000),
        "pathname_redacted": True,
    }


def _safe_pathname(raw_url: object) -> str:
    """Expose only the root path; replace every other pathname wholesale."""
    return _path_metadata(raw_url)["pathname"]


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


def _title_category(value: object, *, path_is_root: bool) -> tuple[str, bool]:
    """Classify a title using fixed markers; never return title text."""
    title = str(value or "").strip()
    if not title:
        return "UNKNOWN", False
    normalized = sanitize_message(title).casefold()
    if any(marker in normalized for marker in _CHALLENGE_TITLE_MARKERS):
        return "CHALLENGE", True
    if any(marker in normalized for marker in _ERROR_TITLE_MARKERS):
        return "ERROR", True
    if re.search(r"\bsearch\b", normalized):
        return "SEARCH", True
    if re.search(r"\bcollections?\b", normalized):
        return "COLLECTION", True
    if re.search(r"\bproducts?\b", normalized):
        return "PRODUCT", True
    if path_is_root:
        return "HOME", True
    return "UNKNOWN", True


def _safe_path_metadata(payload: dict, field: str, prefix: str = "") -> dict:
    """Re-sanitize a path, retaining only bounded metadata from our redacted form."""
    raw_path = payload.get(field)
    metadata = _path_metadata(raw_path)
    if raw_path != "/[REDACTED]":
        return metadata

    keys = {
        "path_category": f"{prefix}path_category",
        "path_segment_count": f"{prefix}path_segment_count",
    }
    category = payload.get(keys["path_category"])
    count = payload.get(keys["path_segment_count"])
    safe_category = (
        category
        if isinstance(category, str) and category in _PATH_CATEGORIES
        else "UNKNOWN"
    )
    if safe_category == "HOME":
        safe_category = "UNKNOWN"
    return {
        "pathname": "/[REDACTED]",
        "path_is_root": False,
        "path_category": safe_category,
        "path_segment_count": min(count, 1000)
        if type(count) is int and count >= 0
        else None,
        "pathname_redacted": True,
    }


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
    actual_path = _path_metadata(actual_raw)
    configured_path = _path_metadata(configured_raw)

    title, title_error = _page_value(page.title, "")
    title_category, title_present = _title_category(
        title, path_is_root=actual_path["path_is_root"]
    )
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
        "actual_pathname": actual_path["pathname"],
        "path_is_root": actual_path["path_is_root"],
        "path_category": actual_path["path_category"],
        "path_segment_count": actual_path["path_segment_count"],
        "pathname_redacted": actual_path["pathname_redacted"],
        "configured_origin": _safe_origin(configured_raw),
        "configured_pathname": configured_path["pathname"],
        "configured_path_is_root": configured_path["path_is_root"],
        "configured_path_category": configured_path["path_category"],
        "configured_path_segment_count": configured_path["path_segment_count"],
        "configured_pathname_redacted": configured_path["pathname_redacted"],
        "query_present": _query_present(actual_raw),
        "fragment_present": _fragment_present(actual_raw),
        "url_mismatch_type": mismatch_type,
        "mismatch_components": components,
        "page_title_category": title_category,
        "title_present": title_present,
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
        "actual_pathname": "/[REDACTED]",
        "path_is_root": False,
        "path_category": "UNKNOWN",
        "path_segment_count": None,
        "pathname_redacted": True,
        "configured_origin": "UNKNOWN",
        "configured_pathname": "/[REDACTED]",
        "configured_path_is_root": False,
        "configured_path_category": "UNKNOWN",
        "configured_path_segment_count": None,
        "configured_pathname_redacted": True,
        "query_present": None,
        "fragment_present": None,
        "url_mismatch_type": "INVALID_URL",
        "mismatch_components": {},
        "page_title_category": "UNKNOWN",
        "title_present": False,
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
    if not isinstance(mismatch, str) or mismatch not in _MISMATCH_TYPES:
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
    actual_path = _safe_path_metadata(payload, "actual_pathname")
    configured_path = _safe_path_metadata(
        payload, "configured_pathname", prefix="configured_"
    )
    title_present = payload.get("title_present")
    if not isinstance(title_present, bool):
        title_present = bool(str(payload.get("current_page_title") or "").strip())
    page_title_category = payload.get("page_title_category")
    if not isinstance(page_title_category, str) or page_title_category not in _TITLE_CATEGORIES:
        page_title_category, inferred_present = _title_category(
            payload.get("current_page_title"),
            path_is_root=actual_path["path_is_root"],
        )
        title_present = title_present or inferred_present
    if not actual_path["path_is_root"] and page_title_category == "HOME":
        page_title_category = "UNKNOWN"
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
        "actual_pathname": actual_path["pathname"],
        "path_is_root": actual_path["path_is_root"],
        "path_category": actual_path["path_category"],
        "path_segment_count": actual_path["path_segment_count"],
        "pathname_redacted": actual_path["pathname_redacted"],
        "configured_origin": _safe_origin(payload.get("configured_origin")),
        "configured_pathname": configured_path["pathname"],
        "configured_path_is_root": configured_path["path_is_root"],
        "configured_path_category": configured_path["path_category"],
        "configured_path_segment_count": configured_path["path_segment_count"],
        "configured_pathname_redacted": configured_path["pathname_redacted"],
        "query_present": query if isinstance(query, bool) else None,
        "fragment_present": fragment if isinstance(fragment, bool) else None,
        "url_mismatch_type": mismatch,
        "mismatch_components": components,
        "page_title_category": page_title_category,
        "title_present": title_present,
        "document_ready_state": payload.get("document_ready_state")
        if payload.get("document_ready_state") in {"loading", "interactive", "complete"}
        else "UNKNOWN",
        "search_trigger_visible": trigger_visible if isinstance(trigger_visible, bool) else None,
        "diagnostic_collection_status": payload.get("diagnostic_collection_status")
        if payload.get("diagnostic_collection_status") in {"COLLECTED", "FAILED"}
        else "UNKNOWN",
    }
    recovery_context = payload.get("recovery_context")
    if (
        isinstance(recovery_context, dict)
        and recovery_context.get("url_mismatch_observed") is True
        and recovery_context.get("observation_stage")
        == "ENSURE_SEARCH_SESSION_OPEN_URL_ASSERTION"
        and recovery_context.get("final_error_category") == "TIMEOUT_ERROR"
        and recovery_context.get("root_cause_confirmed") is False
        and type(recovery_context.get("observation_attempt")) is int
        and 1 <= recovery_context["observation_attempt"] <= 1000
    ):
        output["recovery_context"] = {
            "url_mismatch_observed": True,
            "observation_timestamp": output["timestamp"],
            "observation_stage": "ENSURE_SEARCH_SESSION_OPEN_URL_ASSERTION",
            "observation_attempt": recovery_context["observation_attempt"],
            "last_relevant_url_diagnostic": dict(output),
            "final_error_category": "TIMEOUT_ERROR",
            "root_cause_confirmed": False,
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
