"""Check that files matching patterns exist in the workspace."""

from pathlib import Path


def file_exists(files: list[str], *, workspace_root: str = "") -> dict:
    root = Path(workspace_root)
    results = []
    all_passed = True

    for pattern in files:
        matches = list(root.rglob(pattern)) if "*" in pattern else [root / pattern]
        for f in matches:
            passed = f.exists() and f.is_file()
            if not passed:
                all_passed = False
            results.append({
                "file": str(f.relative_to(root) if f.is_relative_to(root) else f),
                "passed": passed,
                "error_message": "" if passed else "File not found"
            })

    return {"all_passed": all_passed, "results": results}
