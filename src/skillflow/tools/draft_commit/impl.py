"""Move all files from draft dir to final dir, then git commit."""

import shutil
import subprocess
from pathlib import Path

from skillflow import closed_git


def draft_commit(source_dir: str = "", *, workspace_root: str = "",
                 step_id: str = "", run_id: str = "",
                 project_id: str = "", task_name: str = "") -> dict:
    """Move staging ({step}.tmp) → step dir and git commit in the workspace.

    Callers pass source_dir="$STEP_TMP_DIR" (alias $STEP_DRAFT_DIR), which
    skillflow resolves before calling. Nothing injects a default: an empty
    source_dir resolves to workspace_root itself (dst == src), so every
    workspace file is moved onto itself and the workspace root is git-added.
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
    # Build descriptive commit message
    parts = [f"step: {step_id}"] if step_id else ["step: commit"]
    if project_id:
        parts.append(f"[{project_id}]")
    if task_name:
        parts.append(f"{task_name}")
    parts.append(f"{len(moved_files)} file(s) — {', '.join(moved_files[:5])}")
    msg = " ".join(parts)
    # `git add -A` + `git commit`, published raw by closed_git (no repository
    # hook/filter/fsmonitor/signing program runs).
    try:
        with closed_git.open_repo(repo_root) as repo:
            result = repo.publish(lambda path: True, msg)
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        return {"committed": False, "files": moved_files,
                "error": f"git commit failed: {exc}"}
    if not result["committed"]:
        return {"committed": False, "files": moved_files,
                "error": "git commit failed: nothing to commit"}

    return {"committed": True, "files": moved_files}
