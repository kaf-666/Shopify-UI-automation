"""Safe site-scoped artifact path helpers.

New smoke runs keep the configured site as an explicit path dimension so a
shared workspace cannot overwrite or aggregate artifacts from another site.
"""

from __future__ import annotations

import re
from pathlib import Path

from utils.config import site_config_path
from utils.errors import CliConfigError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SITES_DIR = PROJECT_ROOT / "configs" / "sites"
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def canonical_site_name(site_name: str, *, sites_dir: Path = SITES_DIR) -> str:
    """Return the configured site's canonical filename stem.

    ``site_config_path`` supplies the existing identifier and traversal
    validation. Resolving the directory entry also prevents spelling variants
    from creating separate artifact roots on case-insensitive workspaces.
    """
    configured_path = site_config_path(sites_dir, site_name)
    root = Path(sites_dir).resolve()
    try:
        matches = sorted(
            candidate
            for candidate in root.glob("*.yaml")
            if candidate.stem.casefold() == configured_path.stem.casefold()
            and candidate.is_file()
        )
    except OSError as exc:
        raise CliConfigError(
            "unable to inspect configured sites", category="SITE_CONFIG_ERROR"
        ) from exc
    if len(matches) != 1:
        raise CliConfigError("site config not found", category="SITE_CONFIG_ERROR")
    return matches[0].stem


def site_scoped_artifact_root(artifact_root: Path, site_name: str) -> Path:
    """Return a validated ``<artifact_root>/<site>`` directory path."""
    root = Path(artifact_root).resolve()
    site = canonical_site_name(site_name)
    target = (root / site).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise CliConfigError(
            "artifact site path escapes configured root", category="ARTIFACT_PATH_ERROR"
        ) from exc
    return target


def site_scoped_artifact_dir(artifact_root: Path, site_name: str, run_id: str) -> Path:
    """Return a validated ``<artifact_root>/<site>/<run_id>`` directory path."""
    normalized_run_id = str(run_id or "").strip()
    if not RUN_ID_RE.fullmatch(normalized_run_id):
        raise CliConfigError("invalid artifact run id", category="ARTIFACT_PATH_ERROR")
    root = site_scoped_artifact_root(artifact_root, site_name)
    target = (root / normalized_run_id).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise CliConfigError(
            "artifact run path escapes configured root", category="ARTIFACT_PATH_ERROR"
        ) from exc
    return target


def artifact_display_path(path: Path) -> str:
    """Return a project-relative artifact path when it is safe to do so."""
    try:
        return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return path.as_posix()
