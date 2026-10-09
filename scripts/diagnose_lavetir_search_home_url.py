"""Capture a safe, readonly trace of Lavetir mobile Search entry.

This diagnostic intentionally stops after ``SearchPage.ensure_search_session_open``.
It does not enter a query or submit Search and never stores URL query/fragment
values, request headers, cookies, or exception messages.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlsplit

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pages.home_page import HomePage
from pages.navigation import NavigationPage
from pages.search_page import SearchPage
from utils.browser import BrowserRuntime, close_browser, create_browser
from utils.readonly_mutation_guard import ReadonlyMutationGuard
from utils.site_access import SiteAccessPolicy


SITE_NAME = "lavetir"
VIEWPORT = "mobile"
EXPECTED_BROWSER = "webkit"
EXPECTED_DEVICE = "iPhone 14"
DIAGNOSTIC_NAME = "lavetir-mobile-search-home-url"
READONLY_CASE_ID = "LAVETIR_SEARCH_HOME_URL_DIAGNOSTIC"
SIGNED_REQUEST_ENV_NAMES = (
    "LAVETIR_US_SHOPIFY_SIGNATURE",
    "LAVETIR_US_SHOPIFY_SIGNATURE_INPUT",
    "LAVETIR_US_SHOPIFY_SIGNATURE_AGENT",
)
EMAIL_RE = re.compile(r"(?i)(?<![\w.+-])[\w.+-]+@[\w.-]+\.[a-z]{2,}(?![\w.-])")
OPAQUE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]{32,}$")
SAFE_CATEGORY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
SAFE_BUILD_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _secret_values() -> tuple[str, ...]:
    """Return configured credentials only for in-memory output redaction."""
    return tuple(value for name in SIGNED_REQUEST_ENV_NAMES if (value := os.environ.get(name)))


def _redact_known_secrets(value: str) -> str:
    for secret in _secret_values():
        value = value.replace(secret, "[REDACTED]")
    return value


def _safe_pathname(pathname: str) -> str:
    """Keep useful route structure while masking email and opaque path tokens."""
    parts = pathname.split("/")
    safe_parts = []
    for part in parts:
        decoded = unquote(part)
        if EMAIL_RE.search(decoded) or OPAQUE_SEGMENT_RE.fullmatch(decoded):
            safe_parts.append("[REDACTED]")
        else:
            safe_parts.append(_redact_known_secrets(part))
    return "/".join(safe_parts) or "/"


def safe_url_fields(url: str) -> dict[str, Any]:
    """Return URL origin/path and presence bits without query or fragment data."""
    try:
        source = str(url or "")
        parsed = urlsplit(source)
        scheme = parsed.scheme.lower()
        hostname = (parsed.hostname or "").lower()
        port = parsed.port
        if scheme in {"http", "https"} and hostname:
            # hostname excludes URL userinfo; never use parsed.netloc here.
            host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
            is_default_port = (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
            origin = f"{scheme}://{host}" + (f":{port}" if port and not is_default_port else "")
        else:
            origin = None
        pathname = _safe_pathname(parsed.path or "/")
        return {
            "origin": _redact_known_secrets(origin) if origin else None,
            "pathname": pathname,
            # urlsplit() does not distinguish a missing delimiter from an
            # explicitly empty ``?`` or ``#`` component.
            "query_present": "?" in source.split("#", 1)[0],
            "fragment_present": "#" in source,
            "hostname": hostname or None,
        }
    except (TypeError, ValueError):
        return {
            "origin": None,
            "pathname": "[UNPARSEABLE]",
            "query_present": False,
            "fragment_present": False,
            "hostname": None,
        }


def homepage_url_check_result(actual_url: str, configured_base_url: str) -> str:
    """Mirror SearchPage's exact precondition without accepting aliases."""
    return (
        "PASS"
        if str(actual_url or "").rstrip("/") == str(configured_base_url or "").rstrip("/")
        else "FAIL"
    )


def safe_exception_category(exc: BaseException) -> str:
    """Expose a short category or exception class, never its message."""
    category = getattr(exc, "category", None)
    if isinstance(category, str) and SAFE_CATEGORY_RE.fullmatch(category):
        return category
    name = type(exc).__name__
    return name if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name) else "UNCLASSIFIED_ERROR"


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class NavigationObserver:
    """Record main-frame navigation facts with query and fragment values removed."""

    def __init__(self, page, started_at: float) -> None:
        self.page = page
        self.started_at = started_at
        self.timeline: list[dict[str, Any]] = []
        self._last_url_fields: Optional[dict[str, Any]] = None

    def _append(self, event_name: str, url: str, **extra: Any) -> None:
        fields = safe_url_fields(url)
        previous = self._last_url_fields
        event = {
            "event": event_name,
            "timestamp": _timestamp(),
            "elapsed_ms": round((time.perf_counter() - self.started_at) * 1000),
            "origin": fields["origin"],
            "pathname": fields["pathname"],
            "query_present": fields["query_present"],
            "fragment_present": fields["fragment_present"],
        }
        if event_name == "main_frame_navigation":
            event["previous_origin"] = previous.get("origin") if previous else None
            event["previous_pathname"] = previous.get("pathname") if previous else None
            self._last_url_fields = fields
        event.update(extra)
        self.timeline.append(event)

    def _on_frame_navigated(self, frame) -> None:
        try:
            if frame != self.page.main_frame:
                return
            self._append("main_frame_navigation", frame.url)
        except Exception:
            # Browser event callbacks must not mask the actual navigation.
            self.timeline.append(
                {
                    "event": "main_frame_navigation_observer_error",
                    "timestamp": _timestamp(),
                    "elapsed_ms": round((time.perf_counter() - self.started_at) * 1000),
                    "error_category": "BROWSER_EVENT_ERROR",
                }
            )

    def _on_response(self, response) -> None:
        try:
            request = response.request
            if request.resource_type != "document" or request.frame != self.page.main_frame:
                return
            status = int(response.status)
            self._append("main_document_response", response.url, status=status)
        except Exception:
            return

    def _on_request_failed(self, request) -> None:
        try:
            if request.resource_type != "document" or request.frame != self.page.main_frame:
                return
            self._append(
                "main_document_request_failed",
                request.url,
                error_category="NETWORK_REQUEST_FAILED",
            )
        except Exception:
            return

    def attach(self) -> None:
        self.page.on("framenavigated", self._on_frame_navigated)
        self.page.on("response", self._on_response)
        self.page.on("requestfailed", self._on_request_failed)

    def detach(self) -> None:
        for event_name, handler in (
            ("framenavigated", self._on_frame_navigated),
            ("response", self._on_response),
            ("requestfailed", self._on_request_failed),
        ):
            try:
                self.page.remove_listener(event_name, handler)
            except Exception:
                pass


def _safe_visible(locator) -> Optional[bool]:
    if locator is None:
        return None
    try:
        return bool(locator.is_visible())
    except Exception:
        return None


def _safe_title(page) -> Optional[str]:
    try:
        title = " ".join(str(page.title() or "").split())
    except Exception:
        return None
    title = _redact_known_secrets(title)
    title = EMAIL_RE.sub("[REDACTED]", title)
    title = re.sub(r"(?i)\b[A-Za-z0-9_-]{32,}\b", "[REDACTED]", title)
    return title[:160]


def _cloudflare_challenge_detected(page, title: Optional[str]) -> bool:
    title_hint = bool(
        re.search(r"cloudflare|just a moment|attention required|checking your browser", title or "", re.I)
    )
    try:
        marker = page.locator(
            "#challenge-form, .cf-challenge, [data-cf-challenge], iframe[src*='challenges.cloudflare.com']"
        )
        return title_hint or marker.count() > 0
    except Exception:
        return title_hint


def _search_open_state(search: SearchPage) -> Optional[bool]:
    try:
        classes = search.container().get_attribute("class") or ""
        return "is-active" in classes.split()
    except Exception:
        return None


def _ready_state(page) -> Optional[str]:
    try:
        value = page.evaluate("document.readyState")
        return value if value in {"loading", "interactive", "complete"} else None
    except Exception:
        return None


def _url_relation(configured_base_url: str, actual_url: str, allowed_hosts: list[str]) -> dict[str, Any]:
    configured = safe_url_fields(configured_base_url)
    actual = safe_url_fields(actual_url)
    configured_host = configured.get("hostname")
    actual_host = actual.get("hostname")
    normalized_apex = lambda host: host[4:] if isinstance(host, str) and host.startswith("www.") else host
    if configured_host == actual_host:
        origin_relation = "SAME_HOST"
    elif configured_host and actual_host and normalized_apex(configured_host) == normalized_apex(actual_host):
        origin_relation = "APEX_WWW_VARIANT"
    elif configured_host and actual_host:
        origin_relation = "DIFFERENT_HOST"
    else:
        origin_relation = "ORIGIN_UNAVAILABLE"
    return {
        "homepage_url_check_result": homepage_url_check_result(actual_url, configured_base_url),
        "origin_relation": origin_relation,
        "actual_host_is_configured_allowed_host": actual_host in {str(host).lower() for host in allowed_hosts},
        "configured_pathname": configured["pathname"],
        "actual_pathname": actual["pathname"],
        "path_relation": "SAME_PATH" if configured["pathname"] == actual["pathname"] else "DIFFERENT_PATH",
        "query_present": actual["query_present"],
        "fragment_present": actual["fragment_present"],
    }


def build_stage_snapshot(
    name: str,
    started_at: float,
    page,
    home: HomePage,
    search: SearchPage,
    navigation: NavigationPage,
) -> dict[str, Any]:
    actual_url = str(getattr(page, "url", "") or "")
    configured = safe_url_fields(home.base_url())
    actual = safe_url_fields(actual_url)
    access_cfg = home.site_config.get("access") or {}
    allowed_hosts = access_cfg.get("allowed_hosts") or []
    relation = _url_relation(home.base_url(), actual_url, allowed_hosts)
    title = _safe_title(page)
    try:
        ready_state = _ready_state(page)
    except Exception:
        ready_state = None
    return {
        "stage": name,
        "timestamp": _timestamp(),
        "elapsed_ms": round((time.perf_counter() - started_at) * 1000),
        "configured_base_origin": configured["origin"],
        "configured_base_path": configured["pathname"],
        "actual_origin": actual["origin"],
        "actual_pathname": actual["pathname"],
        "query_present": actual["query_present"],
        "fragment_present": actual["fragment_present"],
        "origin_relation": relation["origin_relation"],
        "actual_host_is_configured_allowed_host": relation["actual_host_is_configured_allowed_host"],
        "path_relation": relation["path_relation"],
        "document_ready_state": ready_state,
        "page_title": title,
        "logo_visible": _safe_visible(home.logo()),
        "header_visible": _safe_visible(navigation.header()),
        "mobile_navigation_trigger_visible": _safe_visible(navigation.menu_trigger()),
        "search_trigger_visible": _safe_visible(home.search_trigger()),
        "search_container_open_state": _search_open_state(search),
        "cloudflare_challenge_detected": _cloudflare_challenge_detected(page, title),
        "homepage_url_check_result": homepage_url_check_result(actual_url, home.base_url()),
    }


def _root_cause_candidates(report: dict[str, Any], site_config: dict[str, Any]) -> dict[str, Any]:
    """Summarize observations without asserting an unobserved root cause."""
    snapshots = report.get("stage_snapshots", [])
    timeline = report.get("navigation_timeline", [])
    home_cfg = (site_config.get("pages") or {}).get("home") or {}
    configured_home_path = str(home_cfg.get("url") or "/")
    aliases = home_cfg.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    configured_paths = {configured_home_path, *(str(path) for path in aliases)}
    navigation_events = [event for event in timeline if event.get("event") == "main_frame_navigation"]
    redirects = []
    for event in navigation_events:
        old_origin = event.get("previous_origin")
        new_origin = event.get("origin")
        old_host = (urlsplit(old_origin).hostname or "").lower() if old_origin else ""
        new_host = (urlsplit(new_origin).hostname or "").lower() if new_origin else ""
        strip_www = lambda host: host[4:] if host.startswith("www.") else host
        same_path = event.get("previous_pathname") == event.get("pathname")
        if (
            old_host
            and new_host
            and old_host != new_host
            and strip_www(old_host) == strip_www(new_host)
            and same_path
        ):
            redirects.append({"from_origin": old_origin, "to_origin": new_origin})

    last = snapshots[-1] if snapshots else {}
    observed_path = last.get("actual_pathname")
    observed_navigation_paths = {
        event.get("pathname")
        for event in navigation_events
        if event.get("pathname") and event.get("pathname") not in configured_paths
    }
    non_root = bool(
        (observed_path and observed_path not in configured_paths)
        or observed_navigation_paths
    )
    locale_candidate = bool(
        observed_path
        and re.match(r"^/[a-z]{2}(?:-[a-z]{2})?(?:/|$)", observed_path, re.I)
        and observed_path not in configured_paths
    )
    error_responses = [
        event.get("status")
        for event in timeline
        if event.get("event") == "main_document_response" and event.get("status", 0) >= 400
    ]
    challenge = any(snapshot.get("cloudflare_challenge_detected") for snapshot in snapshots)
    query_or_fragment = any(
        snapshot.get("actual_pathname") == "/"
        and (snapshot.get("query_present") or snapshot.get("fragment_present"))
        for snapshot in snapshots
    )
    nav_after_ready = False
    apex_www_variant = any(
        snapshot.get("origin_relation") == "APEX_WWW_VARIANT" for snapshot in snapshots
    )
    ready = next((s for s in snapshots if s.get("stage") == "after_home_ready"), None)
    before_assert = next((s for s in snapshots if s.get("stage") == "before_home_url_assertion"), None)
    if ready and before_assert:
        ready_elapsed = ready.get("elapsed_ms", -1)
        assert_elapsed = before_assert.get("elapsed_ms", -1)
        nav_after_ready = any(
            ready_elapsed <= event.get("elapsed_ms", -1) <= assert_elapsed
            for event in navigation_events
        ) or ready.get("actual_pathname") != before_assert.get("actual_pathname")
    return {
        "canonical_apex_www_transition_observed": bool(redirects),
        "apex_www_variant_observed": apex_www_variant,
        "apex_www_transition_evidence": redirects,
        "root_path_query_or_fragment_observed": query_or_fragment,
        "configured_home_path": configured_home_path,
        "configured_home_aliases": [str(path) for path in aliases],
        "non_configured_home_path_observed": non_root,
        "non_configured_navigation_paths": sorted(observed_navigation_paths),
        "locale_path_candidate_unverified": locale_candidate,
        "navigation_after_home_ready_candidate": nav_after_ready,
        "cloudflare_challenge_indicator_observed": challenge,
        "main_document_http_error_statuses": error_responses,
        "other_url_delta_observed": bool(
            last
            and last.get("homepage_url_check_result") == "FAIL"
            and not redirects
            and not apex_www_variant
            and not query_or_fragment
            and not non_root
            and not challenge
        ),
        "classification_note": "Evidence flags only; no root cause is confirmed by this report alone.",
    }


def _new_report() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "diagnostic": DIAGNOSTIC_NAME,
        "started_at": _timestamp(),
        "site": SITE_NAME,
        "viewport": VIEWPORT,
        "browser": EXPECTED_BROWSER,
        "device": EXPECTED_DEVICE,
        "status": "RUNNING",
        "signed_request_policy_active": False,
        "readonly_mutation_guard_attached": False,
        "readonly_mutation_violation_count": 0,
        "stage_snapshots": [],
        "navigation_timeline": [],
        "error_category": None,
    }


def _write_report(report: dict[str, Any], output_dir: Optional[Path] = None) -> Path:
    destination = output_dir or (PROJECT_ROOT / "artifacts" / "diagnostics" / DIAGNOSTIC_NAME)
    destination.mkdir(parents=True, exist_ok=True)
    build_id = os.environ.get("BUILD_NUMBER", "")
    if not SAFE_BUILD_ID_RE.fullmatch(build_id):
        build_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = destination / f"run-{build_id}.json"
    serialized = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    for secret in _secret_values():
        serialized = serialized.replace(secret, "[REDACTED]")
    path.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    print(f"Diagnostic report: {path.relative_to(PROJECT_ROOT).as_posix() if path.is_relative_to(PROJECT_ROOT) else path.name}")
    return path


def _attach_readonly_guard(runtime: BrowserRuntime) -> ReadonlyMutationGuard:
    """Attach after create_browser has installed the Signed Request route."""
    guard = ReadonlyMutationGuard()
    guard.set_scope(VIEWPORT, "search_url_diagnostic", READONLY_CASE_ID)
    guard.attach(runtime.context)
    return guard


def run_diagnostic(output_dir: Optional[Path] = None) -> tuple[int, dict[str, Any]]:
    started_at = time.perf_counter()
    report = _new_report()
    runtime = None
    guard = None
    observer = None
    home = None
    search = None
    navigation = None
    site_config = None
    try:
        # create_browser validates the Lavetir configuration, selects WebKit +
        # iPhone 14, and attaches the configured Signed Request policy before
        # creating the page. No request headers are inspected here.
        runtime = create_browser(VIEWPORT, site_name=SITE_NAME)
        if runtime.engine != EXPECTED_BROWSER or runtime.device != EXPECTED_DEVICE:
            raise RuntimeError("configured browser does not match diagnostic target")
        if not isinstance(runtime.access_policy, SiteAccessPolicy):
            raise RuntimeError("site access policy was not installed")
        if runtime.access_policy.type_name != "signed_request":
            raise RuntimeError("Signed Request policy is not active")
        report["signed_request_policy_active"] = True
        site_config = runtime.site_config or {}
        guard = _attach_readonly_guard(runtime)
        report["readonly_mutation_guard_attached"] = True

        home = HomePage(runtime.page, site_config, VIEWPORT)
        search = SearchPage(runtime.page, site_config, VIEWPORT)
        navigation = NavigationPage(runtime.page, site_config, VIEWPORT)
        observer = NavigationObserver(runtime.page, started_at)
        observer.attach()

        report["stage_snapshots"].append(
            build_stage_snapshot("before_home_open", started_at, runtime.page, home, search, navigation)
        )
        try:
            home.open()
        except Exception as exc:
            report["error_category"] = safe_exception_category(exc)
            report["failed_stage"] = "home_open"
            report["stage_snapshots"].append(
                build_stage_snapshot("after_home_navigation", started_at, runtime.page, home, search, navigation)
            )
            raise
        report["stage_snapshots"].append(
            build_stage_snapshot("after_home_navigation", started_at, runtime.page, home, search, navigation)
        )
        try:
            home.wait_core_ready()
        except Exception as exc:
            report["error_category"] = safe_exception_category(exc)
            report["failed_stage"] = "home_ready"
            report["stage_snapshots"].append(
                build_stage_snapshot("after_home_ready", started_at, runtime.page, home, search, navigation)
            )
            raise
        report["stage_snapshots"].append(
            build_stage_snapshot("after_home_ready", started_at, runtime.page, home, search, navigation)
        )
        report["stage_snapshots"].append(
            build_stage_snapshot("before_home_url_assertion", started_at, runtime.page, home, search, navigation)
        )
        try:
            # This is the existing business behavior under investigation.
            # It opens Search but does not fill or submit a query.
            search.ensure_search_session_open(home)
        except Exception as exc:
            report["error_category"] = safe_exception_category(exc)
            report["failed_stage"] = "search_entry"
            report["stage_snapshots"].append(
                build_stage_snapshot("search_entry_exception", started_at, runtime.page, home, search, navigation)
            )
            raise
        report["stage_snapshots"].append(
            build_stage_snapshot("after_search_entry", started_at, runtime.page, home, search, navigation)
        )
        report["status"] = "PASS"
    except Exception as exc:
        report["status"] = "FAIL"
        if not report.get("error_category"):
            report["error_category"] = safe_exception_category(exc)
        if not report.get("failed_stage"):
            report["failed_stage"] = "diagnostic_setup_or_execution"
    finally:
        if observer is not None:
            report["navigation_timeline"] = observer.timeline
            observer.detach()
        if guard is not None:
            report["readonly_mutation_violation_count"] = guard.violation_count()
            guard.detach()
        close_browser(runtime)

    if site_config is not None:
        report["url_classification"] = _root_cause_candidates(report, site_config)
    if report["readonly_mutation_violation_count"]:
        report["status"] = "FAIL"
        report.setdefault("error_category", "READONLY_MUTATION_BLOCKED")
    _write_report(report, output_dir)
    return (0 if report["status"] == "PASS" else 1), report


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Optional report directory; Jenkins uses the fixed artifacts/diagnostics path by default.",
    )
    args = parser.parse_args(argv)
    exit_code, _ = run_diagnostic(args.output_dir)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
