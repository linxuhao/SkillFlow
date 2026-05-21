"""Apply files from draft directory to project repo."""

import shutil
import subprocess
from pathlib import Path


def repo_apply(source_dir: str, *, workspace_root: str = "",
               project_root: str = "") -> dict:
    src = Path(source_dir)
    if not src.is_absolute():
        src = Path(workspace_root) / source_dir
    src = src.resolve()
    dst = Path(project_root).resolve()

    if not src.exists():
        return {"applied": False, "files": [],
                "error": f"Source dir not found: {source_dir}"}

    applied_files = []
    for f in sorted(src.rglob("*")):
        if not f.is_file():
            continue
        if f.name in (".gitkeep", "_snapshot.json"):
            continue
        if ".git/" in str(f):
            continue
        rel = f.relative_to(src)
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)
        applied_files.append(str(rel))

    if not applied_files:
        return {"applied": False, "files": [],
                "error": "No files to apply (empty or all filtered)"}

    # git add + commit
    r = subprocess.run(["git", "add", "-A"], cwd=dst,
                       capture_output=True, text=True)
    if r.returncode != 0:
        return {"applied": False, "files": applied_files,
                "error": f"git add failed: {r.stderr.strip()}"}

    r = subprocess.run(
        ["git", "commit", "-m", f"step: apply {len(applied_files)} file(s)"],
        cwd=dst, capture_output=True, text=True
    )
    if r.returncode != 0:
        return {"applied": False, "files": applied_files,
                "error": f"git commit failed: {r.stderr.strip()}"}

    return {"applied": True, "files": applied_files}
