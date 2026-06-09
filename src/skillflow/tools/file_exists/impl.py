"""Check that files matching patterns exist in the workspace."""

from pathlib import Path


def file_exists(files: list[str], *, workspace_root: str = "") -> dict:
    root = Path(workspace_root)
    results = []
    all_passed = True
    all_found: list[str] = []

    for pattern in files:
        matches = list(root.rglob(pattern)) if "*" in pattern else [root / pattern]
        for f in matches:
            passed = f.exists() and f.is_file()
            if not passed:
                all_passed = False
                # SF-23: include actionable context so the agent knows what
                # was expected and what actually exists in the output directory.
                # List sibling files so the agent can see what IS there.
                parent = f.parent
                siblings = (
                    sorted(p.name for p in parent.iterdir() if p.is_file())
                    if parent.exists() and parent.is_dir()
                    else []
                )
                error_msg = (
                    f"File not found: {f.name} (expected in {parent}). "
                    + (f"Files present: {', '.join(siblings)}" if siblings
                       else f"Directory is empty or missing: {parent}")
                )
                results.append({
                    "file": str(f.relative_to(root) if f.is_relative_to(root) else f),
                    "passed": False,
                    "error_message": error_msg
                })
            else:
                all_found.append(str(f.relative_to(root)))
                results.append({
                    "file": str(f.relative_to(root)),
                    "passed": True,
                    "error_message": ""
                })

    return {"all_passed": all_passed, "results": results}
