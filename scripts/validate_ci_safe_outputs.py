"""Scan persisted CI outputs for values of injected CI secrets.

This validator intentionally prints only variable names and safe file counts.
It never prints the value that matched. Jenkins credential masking protects the
console; this scan covers result, metadata, error and artifact files persisted
by a build. Source and test fixtures are not output surfaces and are excluded.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.errors import SENSITIVE_ENV_NAMES


SKIP_PARTS = {".git", ".venv", "node_modules", "__pycache__"}
OUTPUT_DIRS = {"artifacts", "playwright-report", "test-results"}
ROOT_OUTPUT_NAMES = {
    "console.log",
    "error.json",
    "error.log",
    "error.txt",
    "metadata.json",
    "results.json",
}
TEXT_SUFFIXES = {
    ".csv", ".html", ".json", ".log", ".txt", ".xml",
}


def text_files(root: Path):
    for path in root.rglob("*"):
        if not path.is_file() or any(part in SKIP_PARTS for part in path.parts):
            continue
        relative = path.relative_to(root)
        in_output_dir = any(part in OUTPUT_DIRS for part in relative.parts)
        is_root_output = len(relative.parts) == 1 and (
            path.name.lower() in ROOT_OUTPUT_NAMES
            or path.name.lower().startswith(("error-", "metadata-", "result-"))
        )
        if (in_output_dir or is_root_output) and path.suffix.lower() in TEXT_SUFFIXES:
            yield path


def main() -> int:
    values = {
        name: os.environ.get(name, "")
        for name in SENSITIVE_ENV_NAMES
    }
    values = {name: value for name, value in values.items() if value}
    scanned = 0
    hits = []
    for path in text_files(PROJECT_ROOT):
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        scanned += 1
        for name, value in values.items():
            if value in content:
                hits.append((name, path.relative_to(PROJECT_ROOT).as_posix()))

    if hits:
        print("Secret leakage scan: FAIL")
        for name, relative_path in hits:
            print(f"  matched injected value for {name} in {relative_path}")
        return 1

    print(f"Secret leakage scan: PASS (scanned {scanned} text files; values not printed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
