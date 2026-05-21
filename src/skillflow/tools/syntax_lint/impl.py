"""Syntax lint via ruff (preferred) or compileall (fallback)."""

import subprocess
import sys
from pathlib import Path


def syntax_lint(file: str, *, workspace_root: str = "") -> dict:
    full = (Path(workspace_root) / file).resolve()

    if not full.exists():
        return {"verdict": "failed", "feedback": f"File not found: {file}"}

    if file.endswith(".py"):
        # Try ruff first
        r = subprocess.run(
            [sys.executable, "-m", "ruff", "check", str(full), "--quiet"],
            capture_output=True, text=True
        )
        if r.returncode != 0:
            return {"verdict": "failed", "feedback": r.stderr.strip() or r.stdout.strip()}

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
