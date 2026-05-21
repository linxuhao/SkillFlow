"""Move all files from draft dir to final dir, then git commit."""

import shutil
import subprocess
from pathlib import Path


def draft_commit(source_dir: str = "", *, workspace_root: str = "",
                 step_id: str = "", run_id: str = "") -> dict:
    """Move Draw→Final and git commit in the workspace.

    If source_dir is empty/unset, expects $STEP_TMP_DIR to have been
    resolved by skillflow's _execute_tool_inline before calling.
    """
    src = Path(source_dir)
    if not src.is_absolute():
        src = Path(workspace_root) / source_dir
    src = src.resolve()

    if not src.exists() or not src.is_dir():
        return {"committed": False, "files": [],
                "error": f"Source dir not found: {source_dir}"}

    # New path: .tmp → step_dir (atomic rename, same as _step_commit)
    src_str = str(src)
    if src_str.endswith(".tmp"):
        dst = Path(src_str[:-4])  # strip .tmp suffix → step_dir
    else:
        # Legacy: Outbox_Draft_ → Outbox_Final_
        dst = Path(src_str.replace("Outbox_Draft_", "Outbox_Final_"))
    dst.mkdir(parents=True, exist_ok=True)

    # Move files preserving relative structure
    moved_files: list[str] = []
    for item in sorted(src.rglob("*")):
        if not item.is_file():
            continue
        rel = item.relative_to(src)
        dest = dst / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(item), str(dest))
        moved_files.append(str(rel))

    if not moved_files:
        return {"committed": False, "files": [],
                "error": "No files in draft directory"}

    # Git commit in workspace root (grandparent of draft dir)
    repo_root = src.parent.parent
    r = subprocess.run(["git", "add", "-A"], cwd=repo_root,
                       capture_output=True, text=True)
    if r.returncode != 0:
        return {"committed": False, "files": moved_files,
                "error": f"git add failed: {r.stderr.strip()}"}

    msg = f"step: commit {len(moved_files)} file(s) — {', '.join(moved_files[:5])}"
    r = subprocess.run(["git", "commit", "-m", msg], cwd=repo_root,
                       capture_output=True, text=True)
    if r.returncode != 0:
        return {"committed": False, "files": moved_files,
                "error": f"git commit failed: {r.stderr.strip()}"}

    return {"committed": True, "files": moved_files}
