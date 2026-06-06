"""Syntax lint via ruff (preferred) or compileall (fallback)."""

import subprocess
import sys
from pathlib import Path


def syntax_lint(file: str, *, workspace_root: str = "") -> dict:
    full = (Path(workspace_root) / file).resolve()

    if not full.exists():
        return {"verdict": "failed", "feedback": f"File not found: {file}"}

    if file.endswith(".py"):
        # Auto-fix trivially-fixable issues (unused imports F401, etc.) instead
        # of failing the whole task/run on cosmetic lint. Models routinely leave
        # a stray import; failing a multi-task build on F401 is far too strict.
        subprocess.run(
            [sys.executable, "-m", "ruff", "check", str(full), "--fix", "--quiet"],
            capture_output=True, text=True
        )
        # After auto-fix, only fail on REAL problems that break execution:
        # syntax errors (E9), undefined names / bad imports (F4xx undefined,
        # F7xx, F82x). Style/cosmetic lints never block.
        r = subprocess.run(
            [sys.executable, "-m", "ruff", "check", str(full), "--quiet",
             "--select", "E9,F63,F7,F82"],
            capture_output=True, text=True
        )
        if r.returncode != 0:
            return {"verdict": "failed", "feedback": r.stdout.strip() or r.stderr.strip()}

        # Also compile check
        r = subprocess.run(
            [sys.executable, "-c",
             f"compile(open({str(full)!r}).read(), {str(full)!r}, 'exec')"],
            capture_output=True, text=True
        )
        if r.returncode != 0:
            return {"verdict": "failed", "feedback": r.stderr.strip()}

    elif file.endswith((".js", ".ts")):
        # Basic JS syntax check (no external tool dependency)
        try:
            content = full.read_text(encoding="utf-8")
            if len(content.strip()) < 10:
                return {"verdict": "failed", "feedback": "File too short"}
        except Exception as e:
            return {"verdict": "failed", "feedback": str(e)}

    elif file.endswith(".html"):
        # Basic HTML check: has html tag
        try:
            content = full.read_text(encoding="utf-8")
            if "<html" not in content.lower():
                return {"verdict": "failed",
                        "feedback": "Missing <html> tag"}
        except Exception as e:
            return {"verdict": "failed", "feedback": str(e)}

    return {"verdict": "passed", "feedback": ""}
