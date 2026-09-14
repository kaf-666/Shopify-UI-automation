"""Lightweight, fail-fast validation for versioned site profiles.

The validator intentionally stays close to the existing YAML shape.  It is
used before Playwright starts so an incomplete profile cannot turn into a
late-page-object ``KeyError`` or accidentally inherit another site's access
policy.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

from utils.config import SITE_NAME_RE, load_yaml_mapping, resolve_url, site_config_path
from utils.errors import CliConfigError


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SITES_DIR = PROJECT_ROOT / "configs" / "sites"

CURRENT_SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = {CURRENT_SCHEMA_VERSION}

WEBSITE_SMOKE_READONLY_V1 = "website_smoke_readonly_v1"
WEBSITE_SMOKE_V1 = "website_smoke_v1"
KNOWN_SUITES = (WEBSITE_SMOKE_READONLY_V1, WEBSITE_SMOKE_V1)

SUPPORTED_NAVIGATION_STRATEGIES = {
    "mega_menu_hover",
    "direct_link",
    "drawer_accordion",
    "drawer_direct",
}
SUPPORTED_NAVIGATION_STRATEGIES_BY_VIEWPORT = {
    "desktop": {"mega_menu_hover", "direct_link"},
    "mobile": {"drawer_accordion", "drawer_direct"},
}
SUPPORTED_SELECTOR_TYPES = {"css", "xpath", "testid", "label", "placeholder", "text", "role"}
SIGNATURE_HEADERS = ("Signature", "Signature-Input", "Signature-Agent")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
HOST_RE = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)


class SiteConfigValidationError(CliConfigError):
    """A site-profile error with a stable category for CLI and Jenkins logs."""


def _fail(category: str, message: str) -> None:
    raise SiteConfigValidationError(message, category=category)


def _as_mapping(value: Any, field: str, *, category: str = "INVALID_SITE_CONFIG") -> dict:
    if not isinstance(value, Mapping):
        _fail(category, f"{field} must be a mapping")
    return dict(value)


def _required_text(mapping: Mapping[str, Any], field: str, *, category: str = "INVALID_SITE_CONFIG") -> str:
    value = str(mapping.get(field) or "").strip()
    if not value:
        _fail(category, f"missing required field: {field}")
    return value


def _require_bool(mapping: Mapping[str, Any], field: str, *, section: str) -> bool:
    value = mapping.get(field)
    if type(value) is not bool:  # bool only; YAML integers are not capability declarations.
        _fail("INVALID_SITE_CONFIG", f"{section}.{field} must be boolean")
    return value


def _validate_schema_version(config: Mapping[str, Any]) -> None:
    version = config.get("schema_version")
    if type(version) is not int:
        _fail("INVALID_SITE_CONFIG", "missing or invalid schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        _fail("UNSUPPORTED_SCHEMA_VERSION", f"schema_version={version} is not supported")


def _validate_identity(
    config: Mapping[str, Any], *, requested_site: str, config_path: Optional[Path]
) -> str:
    declared = _required_text(config, "site")
    if not SITE_NAME_RE.fullmatch(declared):
        _fail("INVALID_SITE_CONFIG", "site contains unsupported characters")
    requested = str(requested_site or "").strip()
    if not SITE_NAME_RE.fullmatch(requested):
        _fail("INVALID_SITE_CONFIG", "requested site contains unsupported characters")
    expected_name = config_path.stem if config_path is not None else requested
    if declared.casefold() != expected_name.casefold():
        _fail(
            "INVALID_SITE_CONFIG",
            f"site identity mismatch: declared={declared} filename={expected_name}",
        )
    return declared


def _validate_base_url(config: Mapping[str, Any]) -> str:
    if "base_url" not in config:
        _fail("INVALID_SITE_CONFIG", "missing required field: base_url")
    try:
        return resolve_url(config.get("base_url"), "site.base_url")
    except CliConfigError as exc:
        _fail("INVALID_SITE_CONFIG", str(exc))
    raise AssertionError("unreachable")  # pragma: no cover - makes type checkers happy


def _access_mode(access: Mapping[str, Any]) -> str:
    mode = str(access.get("mode") or "").strip().lower()
    legacy_type = str(access.get("type") or "").strip().lower()
    if mode and legacy_type and mode != legacy_type:
        _fail("INVALID_ACCESS_CONFIG", "access.mode and access.type disagree")
    selected = mode or legacy_type
    if not selected:
        _fail("INVALID_ACCESS_CONFIG", "access.mode is required")
    if selected not in {"none", "signed_request"}:
        _fail("INVALID_ACCESS_CONFIG", f"unsupported access mode: {selected}")
    return selected


def _validate_allowed_hosts(raw_hosts: Any, *, base_url: str) -> list[str]:
    if not isinstance(raw_hosts, (list, tuple)) or not raw_hosts:
        _fail("INVALID_ACCESS_CONFIG", "signed_request requires a non-empty allowed_hosts list")
    hosts: list[str] = []
    for raw in raw_hosts:
        host = str(raw or "").strip().lower()
        if not host or not HOST_RE.fullmatch(host) or host.startswith(".") or host.endswith("."):
            _fail("INVALID_ACCESS_CONFIG", "allowed_hosts contains an invalid exact hostname")
        if host not in hosts:
            hosts.append(host)
    base_host = (urlparse(base_url).hostname or "").lower()
    if base_host not in hosts:
        _fail("INVALID_ACCESS_CONFIG", "base_url host is not included in allowed_hosts")
    return hosts


def _validate_access(config: Mapping[str, Any], *, base_url: str) -> str:
    access = _as_mapping(config.get("access"), "access", category="INVALID_ACCESS_CONFIG")
    mode = _access_mode(access)
    if mode == "none":
        return mode

    _validate_allowed_hosts(access.get("allowed_hosts"), base_url=base_url)
    source = _required_text(access, "source", category="INVALID_ACCESS_CONFIG").lower()
    if source not in {"env", "environment", "file", "secret_file", "env_or_file", "environment_or_file"}:
        _fail("INVALID_ACCESS_CONFIG", f"unsupported signed_request source: {source}")
    env = _as_mapping(access.get("env") or access.get("environment"), "access.env", category="INVALID_ACCESS_CONFIG")
    for header in SIGNATURE_HEADERS:
        name = str(env.get(header) or "").strip()
        if not ENV_NAME_RE.fullmatch(name):
            _fail("INVALID_ACCESS_CONFIG", f"access.env.{header} must name an environment variable")
    if source in {"file", "secret_file", "env_or_file", "environment_or_file"} and not str(
        access.get("secret_file") or ""
    ).strip():
        _fail("INVALID_ACCESS_CONFIG", "file-backed signed_request requires secret_file")
    return mode


def _validate_selector_leaf(entry: Mapping[str, Any], *, field: str) -> None:
    selector_type = str(entry.get("by") or "").strip().lower()
    if selector_type not in SUPPORTED_SELECTOR_TYPES:
        _fail("INVALID_SITE_CONFIG", f"{field}.by is unsupported or missing")
    if selector_type == "role":
        if not str(entry.get("role") or entry.get("value") or "").strip():
            _fail("INVALID_SITE_CONFIG", f"{field} role selector needs role or value")
        return
    if not str(entry.get("value") or entry.get("name") or "").strip():
        _fail("INVALID_SITE_CONFIG", f"{field} selector value is missing")


def _validate_selector_entry(entry: Any, *, field: str) -> None:
    if entry is None:
        return  # Optional selector slots remain allowed; required slots are checked separately.
    selector = _as_mapping(entry, field)
    has_viewport_branches = "desktop" in selector or "mobile" in selector
    if not has_viewport_branches:
        _validate_selector_leaf(selector, field=field)
        return
    if "by" in selector or "value" in selector:
        _fail("INVALID_SITE_CONFIG", f"{field} cannot mix viewport branches with a leaf selector")
    for viewport in ("desktop", "mobile"):
        branch = selector.get(viewport)
        if not isinstance(branch, Mapping):
            _fail("INVALID_SITE_CONFIG", f"{field}.{viewport} must be a selector mapping")
        _validate_selector_leaf(branch, field=f"{field}.{viewport}")


def _page(config: Mapping[str, Any], name: str) -> dict:
    pages = _as_mapping(config.get("pages"), "pages")
    page = pages.get(name)
    if not isinstance(page, Mapping):
        _fail("MISSING_CAPABILITY_REQUIREMENT", f"missing required page: {name}")
    return dict(page)


def _selectors(page: Mapping[str, Any], page_name: str) -> dict:
    return _as_mapping(page.get("selectors"), f"pages.{page_name}.selectors")


def _require_page_url(page: Mapping[str, Any], page_name: str) -> None:
    value = str(page.get("url") or "").strip()
    if not value:
        _fail("MISSING_CAPABILITY_REQUIREMENT", f"pages.{page_name}.url is required")
    if not value.startswith("/") and not value.startswith(("http://", "https://")):
        _fail("INVALID_SITE_CONFIG", f"pages.{page_name}.url must be an absolute URL or root-relative path")


def _require_selectors(page: Mapping[str, Any], page_name: str, names: tuple[str, ...]) -> None:
    selectors = _selectors(page, page_name)
    for name in names:
        if selectors.get(name) is None:
            _fail("MISSING_CAPABILITY_REQUIREMENT", f"missing selector: pages.{page_name}.selectors.{name}")


def _validate_all_selector_groups(config: Mapping[str, Any]) -> None:
    pages = _as_mapping(config.get("pages"), "pages")
    for page_name, raw_page in pages.items():
        if not isinstance(raw_page, Mapping):
            _fail("INVALID_SITE_CONFIG", f"pages.{page_name} must be a mapping")
        raw_selectors = raw_page.get("selectors")
        if raw_selectors is None:
            continue
        selectors = _as_mapping(raw_selectors, f"pages.{page_name}.selectors")
        for selector_name, entry in selectors.items():
            _validate_selector_entry(entry, field=f"pages.{page_name}.selectors.{selector_name}")


def _capability_groups(config: Mapping[str, Any]) -> dict[str, dict]:
    capabilities = _as_mapping(config.get("capabilities"), "capabilities")
    groups = {
        "suites": (WEBSITE_SMOKE_READONLY_V1, WEBSITE_SMOKE_V1),
        "navigation": ("desktop", "mobile"),
        "collection": ("product_cards", "filters", "sort"),
        "product": ("color", "size", "custom_size"),
    }
    resolved: dict[str, dict] = {}
    for group_name, required_names in groups.items():
        group = _as_mapping(capabilities.get(group_name), f"capabilities.{group_name}")
        if group_name == "navigation":
            for viewport in required_names:
                strategy = str(group.get(viewport) or "").strip().lower()
                if not strategy:
                    _fail("INVALID_SITE_CONFIG", f"capabilities.navigation.{viewport} is required")
                if strategy not in SUPPORTED_NAVIGATION_STRATEGIES:
                    _fail("UNKNOWN_NAVIGATION_STRATEGY", f"navigation.{viewport}={strategy}")
                if strategy not in SUPPORTED_NAVIGATION_STRATEGIES_BY_VIEWPORT[viewport]:
                    _fail(
                        "UNKNOWN_NAVIGATION_STRATEGY",
                        f"navigation.{viewport} does not support strategy={strategy}",
                    )
                group[viewport] = strategy
        else:
            for name in required_names:
                _require_bool(group, name, section=f"capabilities.{group_name}")
        resolved[group_name] = group
    return resolved


def _validate_navigation_requirements(config: Mapping[str, Any], navigation_caps: Mapping[str, Any]) -> None:
    navigation = _page(config, "navigation")
    _require_selectors(navigation, "navigation", ("header", "target_collection"))
    desktop_strategy = str(navigation_caps["desktop"])
    mobile_strategy = str(navigation_caps["mobile"])

    if desktop_strategy in {"mega_menu_hover", "direct_link"}:
        _require_selectors(navigation, "navigation", ("desktop_menu", "desktop_menu_item"))
    if desktop_strategy == "mega_menu_hover":
        _require_selectors(navigation, "navigation", ("desktop_target_parent",))

    if mobile_strategy in {"drawer_accordion", "drawer_direct"}:
        _require_selectors(
            navigation,
            "navigation",
            ("mobile_trigger", "mobile_drawer", "mobile_drawer_open", "mobile_menu", "mobile_menu_item", "mobile_close"),
        )
    if mobile_strategy == "drawer_accordion":
        _require_selectors(navigation, "navigation", ("mobile_target_parent",))


def _validate_readonly_requirements(config: Mapping[str, Any], groups: Mapping[str, Mapping[str, Any]]) -> None:
    product_caps = groups["product"]
    for capability in ("color", "size"):
        if not product_caps[capability]:
            _fail("MISSING_CAPABILITY_REQUIREMENT", f"readonly suite requires product.{capability}=true")
    if not groups["collection"]["product_cards"]:
        _fail("MISSING_CAPABILITY_REQUIREMENT", "readonly suite requires collection.product_cards=true")

    home = _page(config, "home")
    _require_page_url(home, "home")
    _require_selectors(home, "home", ("logo", "search"))

    search = _page(config, "search")
    _require_page_url(search, "search")
    _require_selectors(
        search,
        "search",
        ("container", "input", "submit", "predictive_results", "predictive_product", "result_card", "result_link"),
    )

    _validate_navigation_requirements(config, groups["navigation"])

    collection = _page(config, "collection")
    _require_page_url(collection, "collection")
    _require_selectors(
        collection,
        "collection",
        ("product_grid", "product_card", "product_link", "product_title"),
    )

    product = _page(config, "product")
    _require_page_url(product, "product")
    _require_selectors(product, "product", ("purchase_area", "title", "price", "color", "add_to_cart"))
    resolver = product.get("size_resolver")
    if not isinstance(resolver, Mapping) or not isinstance(resolver.get("models"), list) or not resolver["models"]:
        _fail("MISSING_CAPABILITY_REQUIREMENT", "readonly suite requires product.size_resolver.models")
    for index, model in enumerate(resolver["models"]):
        if not isinstance(model, Mapping):
            _fail("MISSING_CAPABILITY_REQUIREMENT", f"product.size_resolver.models[{index}] must be a mapping")
        for field in ("id", "group_selector", "option_selector", "wait_option_selector"):
            if not str(model.get(field) or "").strip():
                _fail(
                    "MISSING_CAPABILITY_REQUIREMENT",
                    f"product.size_resolver.models[{index}].{field} is required",
                )
    if product_caps["custom_size"]:
        custom_measurement = resolver.get("custom_measurement")
        if not isinstance(custom_measurement, Mapping):
            _fail("MISSING_CAPABILITY_REQUIREMENT", "product.custom_size=true requires size_resolver.custom_measurement")
        for field in ("id", "trigger_value", "field_selector"):
            if not str(custom_measurement.get(field) or "").strip():
                _fail(
                    "MISSING_CAPABILITY_REQUIREMENT",
                    f"product.size_resolver.custom_measurement.{field} is required",
                )


def _validate_full_requirements(config: Mapping[str, Any], groups: Mapping[str, Mapping[str, Any]]) -> None:
    _validate_readonly_requirements(config, groups)
    cart = _page(config, "cart")
    _require_selectors(
        cart,
        "cart",
        ("drawer", "cart_item", "quantity_input", "quantity_plus", "quantity_minus", "subtotal", "checkout_button"),
    )
    checkout = _page(config, "checkout")
    _require_selectors(checkout, "checkout", ("root", "contact", "delivery", "shipping_form", "email", "express"))


def validate_site_config_mapping(
    config: Mapping[str, Any],
    *,
    site_name: str,
    config_path: Optional[Path] = None,
    suite: Optional[str] = None,
) -> dict:
    """Validate one already-loaded site profile and return a normal dict.

    ``suite`` is optional for static profile validation.  When supplied it
    additionally enforces the capability gate for that execution request.
    """

    if not isinstance(config, Mapping):
        _fail("INVALID_SITE_CONFIG", "site config must be a mapping")
    normalized = dict(config)
    _validate_schema_version(normalized)
    _validate_identity(normalized, requested_site=site_name, config_path=config_path)
    base_url = _validate_base_url(normalized)
    _validate_access(normalized, base_url=base_url)
    groups = _capability_groups(normalized)
    _validate_all_selector_groups(normalized)

    if groups["suites"][WEBSITE_SMOKE_READONLY_V1]:
        _validate_readonly_requirements(normalized, groups)
    if groups["suites"][WEBSITE_SMOKE_V1]:
        _validate_full_requirements(normalized, groups)

    if suite is not None:
        requested_suite = str(suite).strip()
        if requested_suite not in KNOWN_SUITES:
            _fail("INVALID_SITE_CONFIG", f"unknown suite: {requested_suite}")
        if not groups["suites"][requested_suite]:
            _fail(
                "SITE_CAPABILITY_UNSUPPORTED",
                f"site={normalized['site']} suite={requested_suite}",
            )
    return normalized


def validate_site_config(
    site_name: str,
    *,
    suite: Optional[str] = None,
    sites_dir: Path = SITES_DIR,
) -> dict:
    """Load and validate a named profile without starting a browser or reading secrets."""

    try:
        path = site_config_path(Path(sites_dir), site_name)
    except CliConfigError as exc:
        _fail("INVALID_SITE_CONFIG", str(exc))
    try:
        config = load_yaml_mapping(path, "site config")
    except CliConfigError as exc:
        _fail(getattr(exc, "category", "INVALID_SITE_CONFIG"), str(exc))
    return validate_site_config_mapping(config, site_name=site_name, config_path=path, suite=suite)


def configured_site_names(*, sites_dir: Path = SITES_DIR) -> list[str]:
    """Return deterministic configured YAML stems for the ``--all`` CLI mode."""

    root = Path(sites_dir)
    try:
        paths = sorted(path for path in root.glob("*.yaml") if path.is_file())
    except OSError as exc:
        _fail("INVALID_SITE_CONFIG", "unable to inspect site config directory")
    if not paths:
        _fail("INVALID_SITE_CONFIG", "no site config files found")
    return [path.stem for path in paths]
