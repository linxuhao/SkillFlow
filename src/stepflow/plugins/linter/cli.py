"""CLI entry point for stepflow-lint."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure package is importable when run from repo without pip install
_src = Path(__file__).parent.parent.parent.parent
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from stepflow.plugins.linter import lint_config, LintIssue


def main(argv: list[str] | None = None):
    """Validate stepflow pipeline YAML files.

    Usage:
        stepflow-lint config.yaml
        stepflow-lint configs/*.yaml
    """
    args = argv or sys.argv[1:]
    if not args:
        print("Usage: stepflow-lint <config.yaml> [...]")
        sys.exit(1)

    exit_code = 0
    for file_pattern in args:
        p = Path(file_pattern)
        candidates = [p] if p.is_absolute() else list(Path(".").glob(file_pattern))
        if not candidates:
            print(f"⚠  No files matched: {file_pattern}")
            exit_code = 1
            continue
        for path in candidates:
            issues = lint_config(path)
            if not issues:
                print(f"✓ {path} — OK")
                continue

            print(f"\n✗ {path} — {len(issues)} issue(s):\n")
            for issue in issues:
                tag = "ERROR" if issue.severity == "error" else "WARN"
                loc = f" ({issue.location})" if issue.location else ""
                print(f"  {tag}{loc}: {issue.message}")
                if issue.suggestion:
                    print(f"    → {issue.suggestion}")
                print()
            exit_code = 1

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
