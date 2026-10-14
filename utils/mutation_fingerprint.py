"""Safe, policy-independent mutation fingerprint helpers.

Only endpoint metadata is retained: request bodies and headers are never read.
Classification remains owned by ``TransactionalMutationPolicy``.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import unquote, unquote_plus, urlsplit


UNKNOWN = "UNKNOWN"
HOST_CLASSES = frozenset({"FIRST_PARTY", "THIRD_PARTY", UNKNOWN})
CLASSIFICATIONS = frozenset(
    {
        "EXPECTED_MUTATION",
        "KNOWN_BLOCKED_SIDE_EFFECT",
        "UNEXPECTED_MUTATION",
        "HIGH_RISK_MUTATION",
    }
)
GATING_FAILURE_CLASSIFICATIONS = frozenset(
    {"UNEXPECTED_MUTATION", "HIGH_RISK_MUTATION"}
)
REASONS = frozenset(
    {
        "API_CLEANUP_EXPECTED_MUTATION",
        "EXPECTED_CART_MUTATION",
        "EXPECTED_TRANSACTIONAL_MUTATION",
        "FIRST_PARTY_WATI_ADD_TO_CART_EVENT",
        "FIRST_PARTY_LOCALIZATION_CONTEXT",
        "HIGH_RISK_PRECEDENCE",
        "PATH_NOT_ALLOWED",
        "REDACTED",
    }
)
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_LONG_HEX_RE = re.compile(r"^[0-9a-f]{16,}$", re.IGNORECASE)
_LONG_ID_RE = re.compile(r"^\d{7,}$")
_EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
_TOKEN_CHARS_RE = re.compile(r"^[A-Z0-9_-]{16,}$", re.IGNORECASE)
_METHOD_RE = re.compile(r"^[A-Z]{1,12}$")
_HASH_RE = re.compile(r"^[0-9a-f]{12}$")

_DYNAMIC_MARKERS = frozenset(
    {"checkout", "checkouts", "session", "sessions", "cart", "customer", "customers"}
)
_CART_STATIC_SEGMENTS = frozenset(
    {"add", "add.js", "change", "change.js", "update", "update.js", "clear", "clear.js"}
)
_CHECKOUT_STATIC_SEGMENTS = frozenset(
    {"information", "shipping", "payment", "processing", "thank_you", "thank-you", "complete"}
)
_CUSTOMER_STATIC_SEGMENTS = frozenset(
    {"account", "login", "logout", "register", "recover", "activate", "addresses", "orders"}
)
_SESSION_STATIC_SEGMENTS = frozenset({"new", "create", "refresh", "validate", "token"})
LOCALIZATION_QUERY_KEY_ALLOWLIST = frozenset(
    {
        "country",
        "country_code",
        "currency",
        "currency_code",
        "language",
        "language_code",
        "locale",
        "locale_code",
    }
)


def _is_locale_segment(value: str) -> bool:
    return bool(re.fullmatch(r"[a-z]{2}(?:-[a-z]{2})?", value, re.IGNORECASE))


def _looks_like_random_token(value: str) -> bool:
    if not _TOKEN_CHARS_RE.fullmatch(value):
        return False
    # Lowercase hyphenated Shopify handles are ordinary static slugs, not
    # opaque tokens. Require upper-case or underscore structure as an extra
    # token-like signal so long product handles remain diagnosable.
    if not any(char.isupper() for char in value) and "_" not in value:
        return False
    categories = sum(
        bool(re.search(pattern, value))
        for pattern in (r"[a-z]", r"[A-Z]", r"\d", r"[_-]")
    )
    unique_ratio = len(set(value.lower())) / max(1, len(value))
    return categories >= 3 and unique_ratio >= 0.45


def _sanitize_segment(segment: str, previous: str = "", before_previous: str = "") -> str:
    if not segment:
        return segment

    decoded = segment
    # Detect common multiply-encoded route identifiers without ever emitting
    # the decoded sensitive value.
    for _ in range(8):
        next_decoded = unquote(decoded)
        if next_decoded == decoded:
            break
        decoded = next_decoded
    if re.search(r"%[0-9a-f]{2}", decoded, re.IGNORECASE):
        return "{token}"
    lowered = decoded.lower()

    # Encoded separators can hide additional path components. Mask the whole
    # segment rather than emitting a value whose structure is ambiguous.
    if "/" in decoded or "\\" in decoded:
        return "{token}"
    if _EMAIL_RE.search(decoded):
        return "{email}"
    if _UUID_RE.fullmatch(decoded):
        return "{uuid}"
    if _LONG_HEX_RE.fullmatch(decoded):
        return "{hex}"
    if _LONG_ID_RE.fullmatch(decoded):
        return "{id}"

    if previous in _DYNAMIC_MARKERS:
        if previous == "cart" and lowered in _CART_STATIC_SEGMENTS:
            return segment
        if previous in {"checkout", "checkouts"}:
            if lowered in _CHECKOUT_STATIC_SEGMENTS or _is_locale_segment(decoded):
                return segment
        if previous in {"customer", "customers"} and lowered in _CUSTOMER_STATIC_SEGMENTS:
            return segment
        if previous in {"session", "sessions"} and lowered in _SESSION_STATIC_SEGMENTS:
            return segment
        return "{token}"

    # Shopify's hosted checkout URLs commonly include a short locale segment
    # before the opaque checkout identifier.
    if before_previous in {"checkout", "checkouts"} and _is_locale_segment(previous):
        if lowered not in _CHECKOUT_STATIC_SEGMENTS:
            return "{token}"

    if _looks_like_random_token(decoded):
        return "{token}"
    return segment


def sanitize_pathname(pathname: object) -> str:
    """Strip query/fragment and replace dynamic path segments safely."""
    try:
        path = urlsplit(str(pathname or "")).path or "/"
    except (TypeError, ValueError):
        return "/{token}"
    if not path.startswith("/"):
        path = "/" + path

    raw_segments = path.split("/")
    safe_segments = []
    for index, segment in enumerate(raw_segments):
        previous = unquote(raw_segments[index - 1]).lower() if index else ""
        before_previous = unquote(raw_segments[index - 2]).lower() if index > 1 else ""
        safe_segments.append(_sanitize_segment(segment, previous, before_previous))
    return "/".join(safe_segments) or "/"


def _origin(url: str):
    try:
        parsed = urlsplit(str(url or ""))
        host = (parsed.hostname or "").lower()
        scheme = parsed.scheme.lower()
        if not scheme or not host:
            return None
        port = parsed.port
        if port is None:
            port = 443 if scheme == "https" else 80 if scheme == "http" else None
        return (scheme, host, port)
    except (TypeError, ValueError):
        return None


def safe_endpoint_fingerprint(
    url: object,
    method: object,
    first_party_hosts,
    canonical_origin: object = None,
) -> dict:
    """Build safe endpoint metadata without retaining a URL or query value."""
    raw_url = str(url or "")
    try:
        parsed = urlsplit(raw_url)
        pathname = parsed.path or "/"
        hostname = (parsed.hostname or "").lower()
        before_fragment = raw_url.split("#", 1)[0]
        query_present = "?" in before_fragment
    except (TypeError, ValueError):
        pathname = "/{token}"
        hostname = ""
        query_present = UNKNOWN

    try:
        allowed = {str(host).strip().lower() for host in (first_party_hosts or ()) if str(host).strip()}
    except TypeError:
        allowed = set()
    if not hostname:
        host_class = UNKNOWN
    else:
        host_class = "FIRST_PARTY" if hostname in allowed else "THIRD_PARTY"

    request_origin = _origin(raw_url)
    expected_origin = _origin(str(canonical_origin or ""))
    if request_origin is None or expected_origin is None:
        same_origin = UNKNOWN
    else:
        same_origin = request_origin == expected_origin

    normalized_method = str(method or "").upper()
    if not _METHOD_RE.fullmatch(normalized_method):
        normalized_method = UNKNOWN

    try:
        path_hash = hashlib.sha256(pathname.encode("utf-8", errors="strict")).hexdigest()[:12]
    except (UnicodeError, TypeError):
        path_hash = UNKNOWN

    return {
        "method": normalized_method,
        "host_class": host_class,
        "same_origin": same_origin,
        "sanitized_path": sanitize_pathname(pathname),
        "path_hash": path_hash,
        "query_present": query_present,
    }


def safe_localization_query_structure(raw_query: object) -> dict:
    """Summarize localization query keys without retaining raw keys or values.

    Parameter names are data too, so only names from the fixed allowlist are
    returned. Values are never decoded or copied. Unknown keys contribute
    only to a count.
    """
    if not isinstance(raw_query, str) or not raw_query:
        return {
            "query_parameter_count": 0,
            "recognized_query_key_names": [],
            "unknown_query_key_count": 0,
            "duplicate_query_key_present": False,
            "empty_query_key_present": False,
        }

    components = raw_query.split("&")
    recognized = set()
    seen = set()
    unknown_count = 0
    duplicate_present = False
    empty_present = False

    for component in components:
        raw_key = component.split("=", 1)[0]
        decoded = raw_key
        try:
            for _ in range(8):
                next_decoded = unquote_plus(decoded, errors="replace")
                if next_decoded == decoded:
                    break
                decoded = next_decoded
        except (UnicodeError, ValueError):
            # An unparseable name is unknown; never include its contents in
            # diagnostic output or an error message.
            decoded = ""

        normalized = decoded.casefold()
        if not normalized:
            empty_present = True
        if normalized in seen:
            duplicate_present = True
        seen.add(normalized)
        if normalized in LOCALIZATION_QUERY_KEY_ALLOWLIST:
            recognized.add(normalized)
        elif normalized:
            unknown_count += 1

    return {
        "query_parameter_count": len(components),
        "recognized_query_key_names": sorted(recognized),
        "unknown_query_key_count": unknown_count,
        "duplicate_query_key_present": duplicate_present,
        "empty_query_key_present": empty_present,
    }


def _safe_count(value: object) -> str:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return str(value)
    return UNKNOWN


def format_mutation_fingerprint_report(summary: object) -> list[str]:
    """Render allowlisted fields only, and only when the mutation gate fails."""
    if not isinstance(summary, dict):
        return []
    try:
        unexpected = int(summary.get("unexpected_mutation", 0) or 0)
        high_risk = int(summary.get("high_risk_mutation", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return []
    if unexpected <= 0 and high_risk <= 0:
        return []

    lines = ["MUTATION_UNEXPECTED_FINGERPRINT_BEGIN"]
    rows = summary.get("by_path")
    emitted = 0
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            classification = row.get("classification")
            if classification not in GATING_FAILURE_CLASSIFICATIONS:
                continue
            method = str(row.get("method") or "UNKNOWN").upper()
            if not _METHOD_RE.fullmatch(method):
                method = UNKNOWN
            host_class = row.get("host_class")
            if host_class not in HOST_CLASSES:
                host_class = UNKNOWN
            same_origin = row.get("same_origin")
            if not isinstance(same_origin, bool) and same_origin != UNKNOWN:
                same_origin = UNKNOWN
            query_present = row.get("query_present")
            if not isinstance(query_present, bool) and query_present != UNKNOWN:
                query_present = UNKNOWN
            path = sanitize_pathname(row.get("sanitized_path", row.get("path", "/")))
            path_hash = str(row.get("path_hash") or UNKNOWN).lower()
            if not _HASH_RE.fullmatch(path_hash):
                path_hash = UNKNOWN
            reason = row.get("reason", row.get("classification_reason"))
            if reason not in REASONS:
                reason = "REDACTED"
            lines.extend(
                [
                    f"method={method}",
                    f"host_class={host_class}",
                    f"same_origin={str(same_origin).lower() if isinstance(same_origin, bool) else same_origin}",
                    f"sanitized_path={path}",
                    f"path_hash={path_hash}",
                    f"query_present={str(query_present).lower() if isinstance(query_present, bool) else query_present}",
                    f"classification={classification}",
                    f"reason={reason}",
                    f"total_count={_safe_count(row.get('count'))}",
                    f"desktop_count={_safe_count(row.get('desktop_count'))}",
                    f"mobile_count={_safe_count(row.get('mobile_count'))}",
                    f"blocked_count={_safe_count(row.get('blocked_count'))}",
                ]
            )
            emitted += 1
    diagnostics = summary.get("localization_query_diagnostics")
    if isinstance(diagnostics, list):
        for diagnostic in diagnostics:
            if not isinstance(diagnostic, dict):
                continue
            viewport = diagnostic.get("viewport")
            if not isinstance(viewport, str) or viewport not in {"desktop", "mobile", UNKNOWN}:
                viewport = UNKNOWN
            journey = diagnostic.get("journey")
            if not isinstance(journey, str) or journey not in {"direct", "search", "browse", "infrastructure", UNKNOWN}:
                journey = UNKNOWN
            case_id = diagnostic.get("case_id")
            if not isinstance(case_id, str) or not re.fullmatch(r"[RW]SMOKE-[A-Z0-9-]{1,48}", case_id):
                case_id = UNKNOWN
            scope_name = diagnostic.get("scope_name")
            if not isinstance(scope_name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", scope_name):
                scope_name = UNKNOWN
            origin_match = diagnostic.get("origin_match")
            if not isinstance(origin_match, bool) and origin_match != UNKNOWN:
                origin_match = UNKNOWN
            source = diagnostic.get("request_source_category")
            if not isinstance(source, str) or source not in {"NAVIGATION", "XHR", "FETCH", "DOCUMENT", "OTHER", UNKNOWN}:
                source = UNKNOWN
            frame_type = diagnostic.get("frame_type")
            if not isinstance(frame_type, str) or frame_type not in {"MAIN_FRAME", "CHILD_FRAME", UNKNOWN}:
                frame_type = UNKNOWN
            frame_path_category = diagnostic.get("frame_path_category")
            if not isinstance(frame_path_category, str) or frame_path_category not in {
                "HOME_ROOT", "PRODUCT", "COLLECTION", "SEARCH", "CART",
                "CHECKOUT", "OTHER_FIRST_PARTY", "OTHER_ORIGIN", UNKNOWN,
            }:
                frame_path_category = UNKNOWN
            navigation = diagnostic.get("navigation_request")
            if not isinstance(navigation, bool) and navigation != UNKNOWN:
                navigation = UNKNOWN
            classification = diagnostic.get("classification")
            if not isinstance(classification, str) or classification not in CLASSIFICATIONS:
                classification = UNKNOWN
            reason = diagnostic.get("reason")
            if not isinstance(reason, str) or reason not in REASONS:
                reason = "REDACTED"
            blocked = diagnostic.get("blocked")
            if not isinstance(blocked, bool):
                blocked = True
            elapsed = _safe_count(diagnostic.get("relative_elapsed_ms"))
            query_count = _safe_count(diagnostic.get("query_parameter_count"))
            unknown_count = _safe_count(diagnostic.get("unknown_query_key_count"))
            recognized = diagnostic.get("recognized_query_key_names")
            if not isinstance(recognized, list):
                recognized = []
            recognized = sorted(
                {
                    key for key in recognized
                    if isinstance(key, str) and key in LOCALIZATION_QUERY_KEY_ALLOWLIST
                }
            )
            duplicate = diagnostic.get("duplicate_query_key_present") is True
            empty = diagnostic.get("empty_query_key_present") is True
            lines.extend(
                [
                    "LOCALIZATION_QUERY_DIAGNOSTIC_BEGIN",
                    f"viewport={viewport}",
                    f"journey={journey}",
                    f"case_id={case_id}",
                    f"scope_name={scope_name}",
                    f"origin_match={str(origin_match).lower() if isinstance(origin_match, bool) else origin_match}",
                    f"query_parameter_count={query_count}",
                    f"recognized_query_key_names={','.join(recognized) if recognized else 'NONE'}",
                    f"unknown_query_key_count={unknown_count}",
                    f"duplicate_query_key_present={str(duplicate).lower()}",
                    f"empty_query_key_present={str(empty).lower()}",
                    f"request_source_category={source}",
                    f"frame_type={frame_type}",
                    f"frame_path_category={frame_path_category}",
                    f"navigation_request={str(navigation).lower() if isinstance(navigation, bool) else navigation}",
                    f"relative_elapsed_ms={elapsed}",
                    f"classification={classification}",
                    f"reason={reason}",
                    f"blocked={str(blocked).lower()}",
                    "LOCALIZATION_QUERY_DIAGNOSTIC_END",
                ]
            )
    if emitted == 0:
        if not diagnostics:
            lines.append("fingerprint=UNAVAILABLE")
    lines.append("MUTATION_UNEXPECTED_FINGERPRINT_END")
    return lines
