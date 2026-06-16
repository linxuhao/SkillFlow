#!/usr/bin/env python3
"""Thin shim — the converter CLI now lives in the package.

Real implementation: ``skillflow.convert_cli`` (pip console script: ``skillflow-convert``).
This shim is kept so the clone + ``scripts/install.sh`` flow and direct
``python scripts/skill_convert.py`` invocation keep working.
"""
import sys
from pathlib import Path

# Allow running straight from a repo checkout without `pip install`.
_src = Path(__file__).resolve().parent.parent / "src"
if _src.is_dir() and str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from skillflow.convert_cli import main

if __name__ == "__main__":
    main()
