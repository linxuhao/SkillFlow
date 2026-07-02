"""Generic linter — dispatch to ruff, djlint, or basic checks per extension.

Reads an optional linter_manifest.json (extension → linter mapping).
Auto-installs missing linters via pip (cached per process).

Hosts may register additional backends via
``skillflow.lint_backends.register_backend`` — custom backends are
consulted before built-ins, so the manifest can name them directly.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from skillflow.lint_backends import get_backend

# ── Install cache (process lifetime) ────────────────────────────────────
_installed: dict[str, bool] = {}

# ── Default manifest (built-in) ─────────────────────────────────────────
_DEFAULT_MANIFEST: dict[str, str] = {
    ".py": "ruff",
    ".html": "djlint",
    ".js": "basic",
    ".css": "basic",
}


# ── Public entry point ──────────────────────────────────────────────────

def lint(files: list[str], *, workspace_root: str = "",
         manifest_path: str | None = None) -> dict:
    """Run linters on files matching patterns.

    Args:
        files: Glob patterns relative to workspace_root.
        workspace_root: Root directory for file resolution.
        manifest_path: Optional path to linter_manifest.json relative
                       to workspace_root. Uses built-in defaults if omitted.

    Returns:
        {all_passed: bool, results: [{file, passed, error_message}]}
    """
    root = Path(workspace_root) if workspace_root else Path.cwd()
    manifest = _load_manifest(root, manifest_path)

    results: list[dict] = []
    for pattern in files:
        matches = sorted(root.rglob(pattern)) if "*" in pattern else [root / pattern]
        for fp in matches:
            if not fp.is_file():
                continue
            ext = fp.suffix.lower()
            backend = manifest.get(ext, "skip")
            results.append(_run_backend(backend, fp))

    all_passed = all(r.get("passed", False) for r in results)
    return {"all_passed": all_passed, "results": results}


# ── Manifest loading ────────────────────────────────────────────────────

def _load_manifest(root: Path, manifest_path: str | None) -> dict[str, str]:
    if manifest_path:
        mp = (root / manifest_path).resolve()
        try:
            if mp.exists():
                data = json.loads(mp.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return {str(k): str(v) for k, v in data.items()}
        except (json.JSONDecodeError, OSError):
            pass
    return dict(_DEFAULT_MANIFEST)


# ── Backend dispatch ────────────────────────────────────────────────────

def _run_backend(backend: str, fp: Path) -> dict:
    custom = get_backend(backend)
    if custom is not None:
        try:
            res = custom(fp)
        except Exception as e:  # a buggy backend fails the file, not the process
            return {"file": str(fp), "passed": False,
                    "error_message": f"Custom linter '{backend}' crashed: {e}"}
        return {"file": str(fp),
                "passed": bool(res.get("passed", False)),
                "error_message": str(res.get("error_message", ""))}
    if backend == "ruff":
        return _lint_ruff(fp)
    elif backend == "djlint":
        return _lint_djlint(fp)
    elif backend == "basic":
        return _lint_basic(fp)
    elif backend == "skip":
        return {"file": str(fp), "passed": True,
                "error_message": f"No linter configured for {fp.suffix}"}
    else:
        return {"file": str(fp), "passed": True,
                "error_message": f"Unknown linter '{backend}' for {fp.suffix}"}


# ── ruff backend ────────────────────────────────────────────────────────

def _lint_ruff(fp: Path) -> dict:
    if not fp.suffix == ".py":
        return {"file": str(fp), "passed": True, "error_message": ""}
    _ensure_installed("ruff", "ruff check --help")

    # Auto-fix trivial issues first
    subprocess.run(
        [sys.executable, "-m", "ruff", "check", str(fp), "--fix", "--quiet"],
        capture_output=True, text=True,
    )
    # Strict: only syntax/name errors that break execution
    r = subprocess.run(
        [sys.executable, "-m", "ruff", "check", str(fp), "--quiet",
         "--select", "E9,F63,F7,F82"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return {"file": str(fp), "passed": False,
                "error_message": r.stdout.strip() or r.stderr.strip()}

    # Also compile check
    try:
        compile(fp.read_text(encoding="utf-8"), str(fp), "exec")
    except SyntaxError as e:
        return {"file": str(fp), "passed": False,
                "error_message": f"SyntaxError: {e}"}

    return {"file": str(fp), "passed": True, "error_message": ""}


# ── djlint backend ──────────────────────────────────────────────────────

def _lint_djlint(fp: Path) -> dict:
    if not fp.suffix == ".html":
        return {"file": str(fp), "passed": True, "error_message": ""}
    _ensure_installed("djlint", "python -m djlint --version")

    # If djlint is unavailable after install attempt, fall back to basic
    if not _installed.get("djlint"):
        return _lint_basic(fp)

    r = subprocess.run(
        [sys.executable, "-m", "djlint", str(fp), "--lint", "--warn", "--quiet"],
        capture_output=True, text=True,
    )
    # --warn downgrades style hints (H030, H031 etc.) to warnings so
    # they don't cause non-zero exit. Real template syntax errors still fail.
    if r.returncode != 0:
        errors = r.stdout.strip() or r.stderr.strip()
        return {"file": str(fp), "passed": False,
                "error_message": errors}
    return {"file": str(fp), "passed": True, "error_message": ""}


# ── basic backend ───────────────────────────────────────────────────────

def _lint_basic(fp: Path) -> dict:
    try:
        content = fp.read_text(encoding="utf-8")
        if len(content.strip()) < 3:
            return {"file": str(fp), "passed": False,
                    "error_message": "File is empty or too short"}
    except Exception as e:
        return {"file": str(fp), "passed": False,
                "error_message": f"Cannot read file: {e}"}
    return {"file": str(fp), "passed": True, "error_message": ""}


# ── Install helper ──────────────────────────────────────────────────────

def _ensure_installed(name: str, check_cmd: str) -> None:
    """Install a pip package if not already cached."""
    if _installed.get(name):
        return

    # Quick check: is it already importable / runnable?
    try:
        # Build check command: use python -m prefix for module checks
        cmd = [sys.executable, "-m"] + check_cmd.split() if check_cmd.startswith("python -m ") else check_cmd.split()
        r = subprocess.run(cmd[:4], capture_output=True, text=True)
        if r.returncode == 0:
            _installed[name] = True
            return
    except FileNotFoundError:
        pass

    # Attempt pip install
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", name, "--quiet"],
            capture_output=True, text=True, timeout=120,
        )
        _installed[name] = r.returncode == 0
    except Exception:
        _installed[name] = False
