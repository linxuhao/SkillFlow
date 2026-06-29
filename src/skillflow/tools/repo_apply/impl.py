"""Apply files from draft directory to project repo."""

import shutil
import subprocess
import sys
from pathlib import Path


def _probe(msg: str) -> None:
    """Diagnostic log for the step-dir-contains-whole-repo investigation.

    Prints to stderr with a greppable marker so it shows in `docker logs`.
    """
    print(f"[repo_apply.probe] {msg}", file=sys.stderr, flush=True)


def repo_apply(source_dir: str, *, workspace_root: str = "",
               project_root: str = "",
               step_id: str = "", project_id: str = "",
               task_name: str = "") -> dict:
    src = Path(source_dir)
    if not src.is_absolute():
        src = Path(workspace_root) / source_dir
    src = src.resolve()
    dst = Path(project_root).resolve()

    # PROBE: what is repo_apply actually reading from, and is it the repo itself?
    _probe(f"step={step_id} project={project_id} task={task_name!r} "
           f"source_dir_param={source_dir!r} resolved_src={src} "
           f"project_root={dst} workspace_root={workspace_root!r} "
           f"src==dst? {src == dst}")

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

    # PROBE: how many files did we just apply? A handful = normal surgical edit;
    # dozens/hundreds = the leak. Dump a sample so we can see WHAT leaked.
    if len(applied_files) > 15:
        _probe(f"LARGE APPLY step={step_id}: {len(applied_files)} files from "
               f"{src} — first 15: {applied_files[:15]}")
    else:
        _probe(f"step={step_id}: applied {len(applied_files)} file(s): {applied_files}")

    if not applied_files:
        return {"applied": False, "files": [],
                "error": "No files to apply (empty or all filtered)"}

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
