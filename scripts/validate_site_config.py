"""Validate versioned site profiles before browser startup.

Examples:
    python scripts/validate_site_config.py --site mondressy
    python scripts/validate_site_config.py --site lavetir
    python scripts/validate_site_config.py --all
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.errors import CliConfigError, sanitize_message
from utils.site_config_validator import KNOWN_SUITES, configured_site_names, validate_site_config


def _validate_one(site: str, suite: Optional[str]) -> tuple[bool, str]:
    try:
        config = validate_site_config(site, suite=suite)
    except CliConfigError as exc:
        category = getattr(exc, "category", "INVALID_SITE_CONFIG")
        return False, f"SITE_CONFIG_VALIDATION FAIL [{category}] site={site} detail={sanitize_message(exc)}"
    return (
        True,
        "SITE_CONFIG_VALIDATION PASS "
        f"site={config['site']} schema_version={config['schema_version']}",
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a multi-site profile without starting Playwright")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--site", help="configured site identifier")
    selection.add_argument("--all", action="store_true", help="validate every configs/sites/*.yaml profile")
    parser.add_argument(
        "--suite",
        choices=KNOWN_SUITES,
        default=None,
        help="also enforce the requested suite capability gate",
    )
    args = parser.parse_args(argv)

    try:
        sites = configured_site_names() if args.all else [str(args.site)]
    except CliConfigError as exc:
        category = getattr(exc, "category", "INVALID_SITE_CONFIG")
        print(f"SITE_CONFIG_VALIDATION FAIL [{category}] detail={sanitize_message(exc)}")
        return 2

    success = True
    for site in sites:
        passed, line = _validate_one(site, args.suite)
        print(line)
        success = success and passed
    return 0 if success else 2


if __name__ == "__main__":
    raise SystemExit(main())
