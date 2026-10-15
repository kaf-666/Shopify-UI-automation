"""Scan CI output for injected values without printing those values."""
import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.errors import sensitive_env_names, sanitize_message
from utils.run_context import load_context, confined_path, ordinary_files

SKIP_PARTS = {".git", ".venv", "node_modules", "__pycache__"}
OUTPUT_DIRS = {"artifacts", "playwright-report", "test-results"}
ROOT_OUTPUT_NAMES = {"console.log", "error.json", "error.log", "error.txt", "metadata.json", "results.json"}
TEXT_SUFFIXES = {".csv", ".html", ".json", ".jsonl", ".ndjson", ".log", ".txt", ".xml",
                 ".md", ".har", ".yaml", ".yml", ".svg", ".js", ".css"}


def text_files(root: Path):
    for path in root.rglob("*"):
        if not path.is_file() or any(part in SKIP_PARTS for part in path.parts):
            continue
        relative = path.relative_to(root)
        in_output_dir = any(part in OUTPUT_DIRS for part in relative.parts)
        is_root_output = len(relative.parts) == 1 and (
            path.name.lower() in ROOT_OUTPUT_NAMES
            or path.name.lower().startswith(("error-", "metadata-", "result-")))
        if (in_output_dir or is_root_output) and path.suffix.lower() in TEXT_SUFFIXES:
            yield path


def scan_files(paths, environ=None):
    """Stream exact bytes across chunk boundaries; unreadable files fail closed.

    Explicit CI publication scans every candidate, including unknown suffixes.
    Historical workspace scanning retains its output-directory selection.
    """
    env = os.environ if environ is None else environ
    values = {name: str(env[name]).encode("utf-8") for name in sensitive_env_names(env) if env.get(name)}
    overlap = max((len(value) for value in values.values()), default=1) - 1
    hits, errors, count = [], [], 0
    for path in paths:
        count += 1
        found = set()
        try:
            with Path(path).open("rb") as stream:
                tail = b""
                while chunk := stream.read(64 * 1024):
                    block = tail + chunk
                    found.update(name for name, value in values.items() if value in block)
                    tail = block[-overlap:] if overlap else b""
        except OSError:
            errors.append(Path(path))
        hits.extend((name, Path(path)) for name in sorted(found))
    return hits, errors, count


def main(argv=()):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-context", help="scan only this CI invocation")
    args = parser.parse_args(argv)
    try:
        if args.run_context:
            context = load_context(args.run_context, workspace=PROJECT_ROOT)
            root = confined_path(context["run_dir"], PROJECT_ROOT, must_exist=True, directory=True)
            paths = list(ordinary_files(root))
        else:
            paths = list(text_files(PROJECT_ROOT))
        hits, errors, count = scan_files(paths)
    except Exception:
        print("Secret leakage scan: FAIL (OUTPUT_SCAN_UNAVAILABLE)")
        return 1
    if hits or errors:
        print("Secret leakage scan: FAIL")
        for name, path in hits:
            relative = path.relative_to(PROJECT_ROOT).as_posix()
            print(f"  matched injected value for {name} in {sanitize_message(relative)}")
        if errors:
            print(f"  unreadable outputs: {len(errors)}")
        return 1
    print(f"Secret leakage scan: PASS (scanned {count} files; values not printed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
