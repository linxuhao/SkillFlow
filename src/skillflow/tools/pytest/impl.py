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

    # Run pytest FROM the repo root (workspace_root), not the test file's
    # directory. A test like tests/test_pkg.py that does `from pkg.mod import x`
    # needs the repo root on sys.path so the `pkg/` package (at the repo root)
    # resolves — running from tests/ put only tests/ on the path and caused
    # `ModuleNotFoundError: No module named 'pkg'` (AT-9 fallout). We still add
    # the test file's own dir to PYTHONPATH for tests that shell out to sibling
    # files by relative path (the prior NB-4 case).
    repo_root = str(Path(workspace_root).resolve()) if workspace_root else str(full.parent)
    test_parent = str(full.parent)
    cwd = repo_root
    pp_parts = [repo_root, test_parent, os.environ.get("PYTHONPATH", "")]
    env = {**os.environ,
           "PYTHONPATH": os.pathsep.join(p for p in pp_parts if p)}
    r = subprocess.run(
        [sys.executable, "-m", "pytest", str(full), "-q", "--tb=short"],
        capture_output=True, text=True, timeout=60, cwd=cwd, env=env
    )
    # pytest exit codes: 0=passed, 1=failures, 2=interrupted, 3=internal,
    # 4=usage, 5=no tests collected. The lifecycle hook runs pytest on EVERY
    # written .py file (incl. non-test modules), so exit 5 ("no tests ran") is
    # expected and must NOT fail the build — only real test failures (exit 1)
    # and infra errors (2/3) should.
    if r.returncode in (0, 5):
        return {"verdict": "passed", "feedback": ""}
    return {"verdict": "failed",
            "feedback": r.stdout.strip()[-500:] or r.stderr.strip()[-500:]}
