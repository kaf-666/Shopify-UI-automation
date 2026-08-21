"""Scan workspace text outputs for values of injected CI secrets.

This validator intentionally prints only variable names and safe file counts.
It never prints the value that matched. Jenkins credential masking protects the
console; this scan covers checked-out text, configuration, results and artifact
metadata that may be persisted by a build.
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
TEXT_SUFFIXES = {
    ".csv", ".groovy", ".html", ".ini", ".json", ".log", ".md", ".py",
    ".rst", ".txt", ".yaml", ".yml", ".xml", ".properties",
}
TEXT_NAMES = {"Jenkinsfile", "Dockerfile", "Makefile"}


def text_files(root: Path):
    for path in root.rglob("*"):
        if not path.is_file() or any(part in SKIP_PARTS for part in path.parts):
            continue
        if path.name in TEXT_NAMES or path.suffix.lower() in TEXT_SUFFIXES:
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
