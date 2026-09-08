"""Capture post-selection variant state without changing business behavior.

This diagnostic deliberately observes the live DOM after ``ProductPage.select_size``
returns.  It does not retry, reselect, reload, click ATC, or write formal smoke
results.  The output is evidence-only JSON under ``artifacts/variant-diagnostics``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlparse, urlunparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pages.home_page import HomePage
from pages.product_page import ProductPage
from pages.search_page import SearchPage
from utils.browser import close_browser, create_browser, load_site_config, load_settings
from utils.config import resolve_url
from utils.errors import CliConfigError, sanitize_message
from utils.result import make_run_id, write_results_json
from utils.suite_runner import guarded_main


ARTIFACT_ROOT = PROJECT_ROOT / "artifacts" / "variant-diagnostics"
POLL_INTERVAL_MS = 100

CLASSIFICATIONS = (
    "SELECTION_STABLE",
    "POST_SELECTION_THEME_STATE_RESET",
    "SIZE_GROUP_RERENDER_STATE_LOSS",
    "VARIANT_ATC_STATE_FAILURE",
    "SIZE_AVAILABILITY_RERENDER_FAILURE",
    "SEARCH_ENTRY_CONTEXT_DEPENDENT_STATE_RESET",
)


def _safe_url(raw_url: str) -> str:
    """Return an origin/path URL without query, fragment, or userinfo."""

    parsed = urlparse(str(raw_url or ""))
    if not parsed.scheme or not parsed.hostname:
        return parsed.path or ""
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunparse((parsed.scheme, host, parsed.path, "", "", ""))


def _safe_path(raw_url: str) -> str:
    return urlparse(str(raw_url or "")).path or "/"


def _safe_bool(callable_, default: bool = False) -> bool:
    try:
        return bool(callable_())
    except Exception:
        return default


def _safe_text(callable_, default: Optional[str] = None) -> Optional[str]:
    try:
        value = callable_()
    except Exception:
        return default
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _requested_size_state(product: ProductPage, requested_size: str) -> dict[str, Any]:
    """Read only aggregate metadata for the requested option from current DOM."""

    state: dict[str, Any] = {
        "requested_size_present": False,
        "requested_size_available": False,
        "requested_size_checked": False,
    }
    try:
        options = product._size_resolver().options()
        for option in options:
            if option.value == requested_size:
                state.update(
                    requested_size_present=True,
                    requested_size_available=bool(option.available),
                    requested_size_checked=bool(option.selected),
                )
                break
    except Exception:
        pass
    return state


DOM_SNAPSHOT_SCRIPT = r"""
(({selectors, requestedSize}) => {
    const visible = el => {
        if (!el) return false;
        const rect = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        return style.display !== 'none' && style.visibility !== 'hidden' &&
            rect.width > 0 && rect.height > 0;
    };
    const all = (root, selector) => {
        if (!root || !selector) return [];
        try { return [...root.querySelectorAll(selector)]; } catch (_) { return []; }
    };
    const first = (root, selector) => all(root, selector)[0] || null;
    const text = el => (el && (el.textContent || '')).trim().replace(/\s+/g, ' ');
    const normalized = value => String(value || '').trim().toLowerCase();
    const hasToken = (el, tokens) => {
        if (!el) return false;
        const classes = String(el.className || '').toLowerCase().split(/\s+/);
        return (tokens || []).some(token => classes.includes(String(token).toLowerCase()));
    };
    const labelFor = radio => {
        if (!radio) return null;
        const wrapping = radio.closest('label');
        if (wrapping) return wrapping;
        const id = radio.getAttribute('id');
        if (id) {
            try { return document.querySelector('label[for="' + CSS.escape(id) + '"]'); }
            catch (_) { return null; }
        }
        return null;
    };
    const valueOf = radio => {
        const value = String(radio && radio.getAttribute('value') || '').trim();
        return value || text(labelFor(radio));
    };
    const controlAvailable = (radio, disabledTokens) => {
        const control = labelFor(radio) || radio;
        const parent = radio && radio.parentElement;
        const disabled = !!(radio && radio.disabled) ||
            normalized(radio && radio.getAttribute('aria-disabled')) === 'true' ||
            hasToken(radio, disabledTokens) || hasToken(parent, disabledTokens) ||
            normalized(control && control.getAttribute('aria-disabled')) === 'true' ||
            hasToken(control, disabledTokens);
        return !disabled && visible(control);
    };

    const purchase = first(document, selectors.purchase_selector);
    const title = first(document, selectors.title_selector);
    const atc = first(document, selectors.atc_selector);
    const colorGroup = first(document, selectors.color_selector);
    const colorRadios = all(colorGroup, 'input[type="radio"]');
    const colors = colorRadios.filter(radio => controlAvailable(radio, []));
    const selectedColorRadio = colorRadios.find(radio => radio.checked);

    let candidateGroupCount = 0;
    let selectedGroup = null;
    let selectedModel = null;
    outer:
    for (const model of (selectors.size_models || [])) {
        const groups = all(document, model.group_selector);
        candidateGroupCount += groups.length;
        for (const group of groups) {
            const options = all(group, model.option_selector);
            const groupForm = group.closest('form');
            const purchaseId = purchase && purchase.getAttribute('id');
            const associated = (purchase && (groupForm === purchase ||
                (purchaseId && groupForm && groupForm.getAttribute('id') === purchaseId) ||
                options.some(option => {
                    const optionForm = option.getAttribute('form');
                    return (purchaseId && optionForm === purchaseId) ||
                        option.closest('form') === purchase;
                })));
            if (associated) {
                selectedGroup = group;
                selectedModel = model;
                break outer;
            }
        }
    }

    const sizeRadios = selectedGroup && selectedModel
        ? all(selectedGroup, selectedModel.option_selector) : [];
    const sizeOptions = sizeRadios.map(radio => {
        const value = valueOf(radio);
        return {
            value,
            selected: !!radio.checked,
            available: !!value && controlAvailable(radio, selectedModel.disabled_class_tokens || []),
            custom: normalized(value) === normalized(selectedModel.custom_size_value),
        };
    }).filter(option => option.value);
    const availableSizes = sizeOptions.filter(option => option.available);
    const selectedSize = sizeOptions.find(option => option.selected);
    const requested = sizeOptions.find(option => option.value === requestedSize);
    const atcVisible = visible(atc);
    const atcEnabled = atcVisible && !atc.disabled &&
        normalized(atc.getAttribute('aria-disabled')) !== 'true';

    return {
        pathname: location.pathname || '/',
        selected_color: selectedColorRadio ? valueOf(selectedColorRadio) : null,
        selected_size: selectedSize ? selectedSize.value : null,
        model: selectedModel ? selectedModel.id : null,
        group_detected: !!selectedGroup,
        option_total: sizeOptions.length,
        available_count: availableSizes.length,
        normal_available: availableSizes.filter(option => !option.custom).length,
        custom_present: sizeOptions.some(option => option.custom),
        requested_size_present: !!requested,
        requested_size_available: !!(requested && requested.available),
        requested_size_checked: !!(requested && requested.selected),
        atc_visible: atcVisible,
        atc_enabled: atcEnabled,
        purchase_attached: !!purchase,
        title_visible: visible(title),
        color_count: colors.length,
        candidate_group_count: candidateGroupCount,
    };
})
"""


def _css_selector(product: ProductPage, name: str) -> Optional[str]:
    try:
        selector = product.resolve_selector(name)
    except Exception:
        return None
    if str(selector.get("by") or "css").lower() != "css":
        return None
    value = str(selector.get("value") or "").strip()
    return value or None


def _diagnostic_selectors(product: ProductPage) -> dict[str, Any]:
    config = product.page_config()
    models = []
    resolver = config.get("size_resolver") or {}
    for model in resolver.get("models") or []:
        if not isinstance(model, dict):
            continue
        group_selector = str(model.get("group_selector") or "").strip()
        option_selector = str(model.get("option_selector") or "").strip()
        if not group_selector or not option_selector:
            continue
        models.append(
            {
                "id": str(model.get("id") or ""),
                "group_selector": group_selector,
                "option_selector": option_selector,
                "disabled_class_tokens": list(model.get("disabled_class_tokens") or []),
                "custom_size_value": str(
                    model.get("custom_size_value")
                    or (resolver.get("custom_measurement") or {}).get("trigger_value")
                    or "free custom size"
                ),
            }
        )
    return {
        "purchase_selector": _css_selector(product, "purchase_area"),
        "title_selector": _css_selector(product, "title"),
        "atc_selector": _css_selector(product, "add_to_cart"),
        "color_selector": _css_selector(product, "color"),
        "size_models": models,
    }


def snapshot_product_state(
    product: ProductPage,
    requested_size: str,
    elapsed_ms: int,
    selectors: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Return one fast, configuration-driven, aggregate DOM snapshot."""

    if selectors is not None:
        try:
            raw = product.page.evaluate(
                DOM_SNAPSHOT_SCRIPT,
                {"selectors": selectors, "requestedSize": requested_size},
            )
            snapshot = {
                "elapsed_ms": int(max(0, elapsed_ms)),
                "pathname": str(raw.get("pathname") or "/"),
                "selected_color": raw.get("selected_color"),
                "selected_size": raw.get("selected_size"),
                "model": raw.get("model"),
                "group_detected": bool(raw.get("group_detected", False)),
                "option_total": int(raw.get("option_total", 0) or 0),
                "available_count": int(raw.get("available_count", 0) or 0),
                "normal_available": int(raw.get("normal_available", 0) or 0),
                "custom_present": bool(raw.get("custom_present", False)),
                "atc_visible": bool(raw.get("atc_visible", False)),
                "atc_enabled": bool(raw.get("atc_enabled", False)),
                "purchase_attached": bool(raw.get("purchase_attached", False)),
                "requested_size_present": bool(raw.get("requested_size_present", False)),
                "requested_size_available": bool(raw.get("requested_size_available", False)),
                "requested_size_checked": bool(raw.get("requested_size_checked", False)),
            }
            return snapshot
        except Exception:
            pass

    # Fallback is only for a non-CSS site configuration or a transient
    # evaluate failure; it remains read-only and is not used by current sites.
    try:
        readiness = product._readiness_snapshot()
    except Exception:
        readiness = {}
    snapshot = {
        "elapsed_ms": int(max(0, elapsed_ms)),
        "pathname": _safe_path(product.page.url),
        "selected_color": _safe_text(product.get_selected_color),
        "selected_size": _safe_text(product.get_selected_size),
        "model": readiness.get("size_model"),
        "group_detected": bool(readiness.get("size_group_detected", False)),
        "option_total": int(readiness.get("size_option_total", 0) or 0),
        "available_count": int(readiness.get("size_count", 0) or 0),
        "normal_available": int(readiness.get("normal_size_available", 0) or 0),
        "custom_present": bool(readiness.get("custom_size_present", False)),
        "atc_visible": bool(readiness.get("atc_visible", False)),
        "atc_enabled": bool(readiness.get("atc_enabled", False)),
        "purchase_attached": bool(readiness.get("purchase_area_attached", False)),
    }
    snapshot.update(_requested_size_state(product, requested_size))
    return snapshot


def _install_mutation_observer(page) -> bool:
    """Install a readonly observer; it records counts only, never markup."""

    try:
        return bool(
            page.evaluate(
                """(() => {
                    const previous = window.__variantStabilityDiagnostic;
                    if (previous && previous.observer) previous.observer.disconnect();
                    if (!document.body) return false;
                    const state = {
                        startedAt: performance.now(),
                        count: 0,
                        lastMutationMs: null,
                    };
                    const observer = new MutationObserver(mutations => {
                        state.count += mutations.length;
                        state.lastMutationMs = Math.round(
                            performance.now() - state.startedAt
                        );
                    });
                    observer.observe(document.body, {
                        subtree: true,
                        childList: true,
                        attributes: true,
                        attributeFilter: ["checked", "class", "aria-disabled", "disabled"],
                    });
                    window.__variantStabilityDiagnostic = {observer, state};
                    return true;
                })()"""
            )
        )
    except Exception:
        return False


def _start_mutation_observation(page) -> None:
    try:
        page.evaluate(
            """(() => {
                const diagnostic = window.__variantStabilityDiagnostic;
                if (!diagnostic) return false;
                diagnostic.state.startedAt = performance.now();
                diagnostic.state.count = 0;
                diagnostic.state.lastMutationMs = null;
                return true;
            })()"""
        )
    except Exception:
        pass


def _finish_mutation_observation(page) -> dict[str, Any]:
    try:
        state = page.evaluate(
            """(() => {
                const diagnostic = window.__variantStabilityDiagnostic;
                if (!diagnostic) return {count: 0, lastMutationMs: null};
                return {
                    count: Number(diagnostic.state.count) || 0,
                    lastMutationMs: diagnostic.state.lastMutationMs === null
                        ? null
                        : Number(diagnostic.state.lastMutationMs),
                };
            })()"""
        )
        result = {
            "count": int(state.get("count", 0) or 0),
            "last_mutation_ms": state.get("lastMutationMs"),
        }
    except Exception:
        result = {"count": 0, "last_mutation_ms": None}
    try:
        page.evaluate(
            """(() => {
                const diagnostic = window.__variantStabilityDiagnostic;
                if (diagnostic && diagnostic.observer) diagnostic.observer.disconnect();
                delete window.__variantStabilityDiagnostic;
                return true;
            })()"""
        )
    except Exception:
        pass
    return result


def _first_reset_event(
    requested_size: str, timeline: list[dict[str, Any]]
) -> Optional[dict[str, Any]]:
    for before, after in zip(timeline, timeline[1:]):
        if (
            before.get("selected_size") == requested_size
            and after.get("selected_size") is None
        ):
            return {
                "at_ms": int(after.get("elapsed_ms", 0) or 0),
                "state_before": dict(before),
                "state_after": dict(after),
            }
    return None


def _change_events(
    timeline: list[dict[str, Any]], key: str
) -> list[dict[str, Any]]:
    changes = []
    for before, after in zip(timeline, timeline[1:]):
        if before.get(key) != after.get(key):
            changes.append(
                {
                    "at_ms": int(after.get("elapsed_ms", 0) or 0),
                    "before": before.get(key),
                    "after": after.get(key),
                }
            )
    return changes


def classify_timeline(
    entry: str,
    requested_size: str,
    timeline: list[dict[str, Any]],
) -> tuple[list[str], Optional[dict[str, Any]], dict[str, Any]]:
    """Classify synthetic or real evidence without taking browser actions."""

    if not timeline:
        return ["VARIANT_ATC_STATE_FAILURE"], None, {
            "group_presence_changes": [],
            "option_total_changes": [],
            "atc_changes": [],
        }

    first = timeline[0]
    final = timeline[-1]
    group_changes = _change_events(timeline, "group_detected")
    option_changes = _change_events(timeline, "option_total")
    atc_changes = _change_events(timeline, "atc_enabled") + _change_events(
        timeline, "atc_visible"
    )
    dom_events = {
        "group_presence_changes": group_changes,
        "option_total_changes": option_changes,
        "atc_changes": atc_changes,
    }
    reset = _first_reset_event(requested_size, timeline)
    classifications: list[str] = []

    if reset:
        if group_changes or option_changes:
            classifications.append("SIZE_GROUP_RERENDER_STATE_LOSS")
        else:
            classifications.append("POST_SELECTION_THEME_STATE_RESET")

    if (
        not bool(final.get("atc_visible"))
        or not bool(final.get("atc_enabled"))
    ):
        classifications.append("VARIANT_ATC_STATE_FAILURE")

    if (
        bool(first.get("requested_size_available"))
        and (
            not bool(final.get("requested_size_present"))
            or not bool(final.get("requested_size_available"))
        )
    ):
        classifications.append("SIZE_AVAILABILITY_RERENDER_FAILURE")

    if not classifications:
        if (
            final.get("selected_size") == requested_size
            and bool(final.get("atc_visible"))
            and bool(final.get("atc_enabled"))
            and int(final.get("available_count", 0) or 0) > 0
        ):
            classifications.append("SELECTION_STABLE")
        else:
            classifications.append("VARIANT_ATC_STATE_FAILURE")

    # Keep entry in the pure function signature so callers can compare Search
    # and Direct evidence without encoding a site-specific rule here.
    _ = entry
    return classifications, reset, dom_events


def build_diagnostic_payload(
    *,
    run_id: str,
    site: str,
    viewport: str,
    entry: str,
    query: Optional[str],
    observe_ms: int,
    runs: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Build a JSON-safe evidence envelope with no request/session data."""

    return {
        "schema_version": "variant_diagnostics_v1",
        "run_id": run_id,
        "site": site,
        "viewport": viewport,
        "entry": entry,
        "query": query if entry == "search" else None,
        "observe_ms": int(observe_ms),
        "poll_interval_ms": POLL_INTERVAL_MS,
        "runs": list(runs),
    }


def _observe_after_selection(
    product: ProductPage,
    requested_size: str,
    observe_ms: int,
    selectors: Optional[dict[str, Any]] = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Poll current DOM only; no recovery action is allowed in this window."""

    observer_attached = _install_mutation_observer(product.page)
    _start_mutation_observation(product.page)
    started = time.monotonic()
    timeline: list[dict[str, Any]] = [
        snapshot_product_state(product, requested_size, 0, selectors)
    ]
    try:
        while True:
            elapsed = int((time.monotonic() - started) * 1000)
            if elapsed >= observe_ms:
                break
            product.page.wait_for_timeout(
                min(POLL_INTERVAL_MS, max(1, observe_ms - elapsed))
            )
            elapsed = int((time.monotonic() - started) * 1000)
            timeline.append(
                snapshot_product_state(product, requested_size, elapsed, selectors)
            )
    finally:
        mutation = _finish_mutation_observation(product.page)
    mutation["observer_attached"] = observer_attached
    return timeline, mutation


def _open_product(
    runtime,
    site_config: dict,
    viewport: str,
    entry: str,
    query: str,
    product_url: Optional[str],
) -> tuple[ProductPage, Optional[str], Optional[str]]:
    """Navigate through the requested generic entry and return ProductPage."""

    if entry == "search":
        search = SearchPage(runtime.page, site_config, viewport)
        search.open_from_home()
        search.submit_query(query)
        search.wait_predictive_ready()
        if search.result_count() <= 0:
            raise RuntimeError("Search returned no product results")
        product_page = search.open_result(0)
        product = ProductPage(product_page, site_config, viewport)
        return product, query, _safe_url(product_page.url)

    product = ProductPage(runtime.page, site_config, viewport)
    target = product_url or product.page_url()
    if not str(target).strip():
        raise CliConfigError("direct entry requires a product URL", category="CLI_CONFIG_ERROR")
    parsed = urlparse(str(target))
    if not parsed.path.startswith("/products/"):
        raise CliConfigError(
            "product URL must point to a /products/ path",
            category="CLI_CONFIG_ERROR",
        )
    runtime.page.goto(str(target), wait_until="domcontentloaded", timeout=45_000)
    return product, None, _safe_url(runtime.page.url)


def run_viewport(
    *,
    site: str,
    site_config: dict,
    viewport: str,
    entry: str,
    query: str,
    product_url: Optional[str],
    observe_ms: int,
) -> dict[str, Any]:
    """Run one independent browser session and close it unconditionally."""

    runtime = create_browser(viewport, site_name=site)
    try:
        product, used_query, navigated_url = _open_product(
            runtime,
            site_config,
            viewport,
            entry,
            query,
            product_url,
        )
        product.wait_purchase_ready()
        title = _safe_text(product.get_title, default="") or ""
        selectors = _diagnostic_selectors(product)
        requested_color = product.first_available_color()
        product.select_color(requested_color)
        requested_size = product.first_available_size()
        pre_selection = snapshot_product_state(product, requested_size, 0, selectors)

        # This is the final business action.  Once it returns, the remainder
        # of this function only reads state and waits for the observation window.
        product.select_size(requested_size)
        selected_size_after_selection = product.get_selected_size()
        timeline, mutation = _observe_after_selection(
            product, requested_size, observe_ms, selectors
        )
        classifications, reset, dom_events = classify_timeline(
            entry, requested_size, timeline
        )
        return {
            "entry": entry,
            "query": used_query,
            "product_url": navigated_url,
            "product_path": _safe_path(product.page.url),
            "title": title,
            "requested_color": requested_color,
            "requested_size": requested_size,
            "selected_size_after_selection": selected_size_after_selection,
            "pre_selection": pre_selection,
            "timeline": timeline,
            "reset_observed": reset is not None,
            "first_reset_ms": reset.get("at_ms") if reset else None,
            "reset": reset,
            "dom_events": dom_events,
            "mutation": mutation,
            "final": timeline[-1] if timeline else {},
            "classification": classifications[0],
            "classifications": classifications,
        }
    finally:
        close_browser(runtime)


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if value < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return value


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Readonly post-selection variant stability diagnostic")
    parser.add_argument("--site", default=None, help="site name (default: settings.default_site)")
    parser.add_argument("--viewport", choices=["desktop", "mobile", "both"], default="mobile")
    parser.add_argument("--entry", choices=["search", "direct"], default="search")
    parser.add_argument("--query", default="dress")
    parser.add_argument("--product-url", default=None)
    parser.add_argument("--observe-ms", type=_positive_int, default=5_000)
    args = parser.parse_args(argv)

    run_id = make_run_id()
    artifact_dir = ARTIFACT_ROOT / run_id
    try:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        settings = load_settings()
        site = args.site or str(settings.get("default_site") or "")
        site_config = load_site_config(site)
        base_url = resolve_url(site_config.get("base_url"), "site.base_url")
    except Exception as exc:
        print(f"FATAL_ERROR [{getattr(exc, 'category', 'RUNTIME_ERROR')}]: {sanitize_message(exc)}")
        return 2 if isinstance(exc, CliConfigError) else 1

    viewports = ["desktop", "mobile"] if args.viewport == "both" else [args.viewport]
    runs: list[dict[str, Any]] = []
    exit_code = 0
    for viewport in viewports:
        try:
            run = run_viewport(
                site=site,
                site_config=site_config,
                viewport=viewport,
                entry=args.entry,
                query=args.query,
                product_url=args.product_url,
                observe_ms=args.observe_ms,
            )
            runs.append({"viewport": viewport, **run})
            print(
                f"[Variant Diagnostic] viewport={viewport} "
                f"product_path={run['product_path']} "
                f"classification={run['classification']} "
                f"reset_observed={run['reset_observed']} "
                f"first_reset_ms={run['first_reset_ms']} "
                f"mutation_count={run['mutation']['count']}"
            )
        except Exception as exc:  # retain an evidence envelope for setup failures
            exit_code = 1
            error = sanitize_message(exc)
            runs.append(
                {
                    "viewport": viewport,
                    "entry": args.entry,
                    "query": args.query if args.entry == "search" else None,
                    "classification": "VARIANT_ATC_STATE_FAILURE",
                    "classifications": ["VARIANT_ATC_STATE_FAILURE"],
                    "error": error,
                }
            )
            print(f"[Variant Diagnostic] viewport={viewport} ERROR={error}")

    payload = build_diagnostic_payload(
        run_id=run_id,
        site=site,
        viewport=args.viewport,
        entry=args.entry,
        query=args.query,
        observe_ms=args.observe_ms,
        runs=runs,
    )
    # Keep base_url out of the evidence envelope: product path/title and
    # current state are sufficient, and the payload must remain session-safe.
    _ = base_url
    try:
        write_results_json(payload, artifact_dir / "diagnostic.json")
    except Exception as exc:
        print(f"RESULT_WRITE_FAILURE: {sanitize_message(exc)}")
        return 1

    print(f"Diagnostic: artifacts/variant-diagnostics/{run_id}/diagnostic.json")
    return exit_code


if __name__ == "__main__":
    sys.exit(guarded_main(main))
