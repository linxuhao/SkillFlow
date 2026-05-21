"""Run pytest on test files."""

import subprocess
import sys
from pathlib import Path


def pytest(file: str, *, workspace_root: str = "") -> dict:
    full = (Path(workspace_root) / file).resolve()

    if not full.exists():
        return {"verdict": "failed", "feedback": f"File not found: {file}"}

    if not file.endswith(".py"):
        return {"verdict": "passed", "feedback": ""}

    r = subprocess.run(
        [sys.executable, "-m", "pytest", str(full), "-q", "--tb=short"],
        capture_output=True, text=True, timeout=60
    )
    if r.returncode != 0:
        return {"verdict": "failed",
                "feedback": r.stdout.strip()[-500:] or r.stderr.strip()[-500:]}

    return {"verdict": "passed", "feedback": ""}
