"""Reserve this Jenkins invocation before site access or smoke can fail."""
import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.run_context import prepare_context, SUITE_ROOTS


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", required=True)
    parser.add_argument("--suite", required=True, choices=SUITE_ROOTS)
    parser.add_argument("--viewport", required=True, choices=("both", "desktop", "mobile"))
    args = parser.parse_args(argv)
    try:
        context = prepare_context(suite=args.suite, site=args.site, viewport=args.viewport,
                                  workspace=PROJECT_ROOT)
    except Exception:
        print("Run context: FAIL (RUN_CONTEXT_PREPARATION_FAILED)")
        return 2
    print(f"Run context: {context['context_path']}")
    print(f"Current results: {context['results_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
