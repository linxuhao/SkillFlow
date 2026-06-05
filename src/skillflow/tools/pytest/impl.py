"""Run pytest on test files."""

import os
import subprocess
import sys
from pathlib import Path


def pytest(file: str, *, workspace_root: str = "") -> dict:
    full = (Path(workspace_root) / file).resolve()

    if not full.exists():
        return {"verdict": "failed", "feedback": f"File not found: {file}"}

    if not file.endswith(".py"):
        return {"verdict": "passed", "feedback": ""}

    # NB-4: run pytest FROM the project root (the dir holding the test file, where
    # the implementation is assembled), not the server CWD. Tests that shell out
    # to the CLI by relative path (subprocess.run([sys.executable, "add.py", ...]))
    # or import the module need to resolve it relative to that dir.
    cwd = str(full.parent)
    env = {**os.environ,
           "PYTHONPATH": cwd + os.pathsep + os.environ.get("PYTHONPATH", "")}
    r = subprocess.run(
        [sys.executable, "-m", "pytest", str(full), "-q", "--tb=short"],
        capture_output=True, text=True, timeout=60, cwd=cwd, env=env
    )
    if r.returncode != 0:
        return {"verdict": "failed",
                "feedback": r.stdout.strip()[-500:] or r.stderr.strip()[-500:]}

    return {"verdict": "passed", "feedback": ""}
