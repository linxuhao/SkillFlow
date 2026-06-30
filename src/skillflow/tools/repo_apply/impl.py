"""Apply files from draft directory to project repo."""

import shutil
import subprocess
from pathlib import Path


def repo_apply(source_dir: str, *, workspace_root: str = "",
               project_root: str = "",
               step_id: str = "", project_id: str = "",
               task_name: str = "") -> dict:
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
        # Empty source = a legitimate no-op step (the agent determined no change
        # was needed and produced no output). Nothing to copy or commit; report
        # success so the on_deliver hook doesn't retry/fail a clean no-op.
        return {"applied": True, "files": [], "committed": False}

    # git add + commit
    r = subprocess.run(["git", "add", "-A"], cwd=dst,
                       capture_output=True, text=True)
    if r.returncode != 0:
        return {"applied": False, "files": applied_files,
                "error": f"git add failed: {r.stderr.strip()}"}

    # Build descriptive commit message
    parts = [f"step: {step_id}"] if step_id else ["step: apply"]
    if project_id:
        parts.append(f"[{project_id}]")
    if task_name:
        parts.append(f"{task_name}")
    parts.append(f"{len(applied_files)} file(s)")
    msg = " ".join(parts)

    r = subprocess.run(
        ["git", "commit", "-m", msg],
        cwd=dst, capture_output=True, text=True
    )
    if r.returncode != 0:
        # "nothing to commit" is not a failure: the files were copied but are
        # byte-identical to what's already committed (idempotent re-apply, e.g.
        # a t_impl retry). The desired state is present, so report success
        # rather than triggering a wasteful on_deliver retry loop.
        combined = (r.stdout + r.stderr).lower()
        if "nothing to commit" in combined or "no changes added" in combined:
            return {"applied": True, "files": applied_files, "committed": False}
        return {"applied": False, "files": applied_files,
                "error": f"git commit failed: {(r.stderr or r.stdout).strip()}"}

    return {"applied": True, "files": applied_files, "committed": True}
