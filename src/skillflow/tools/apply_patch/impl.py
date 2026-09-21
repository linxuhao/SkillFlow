"""Strict text patches in the host-selected code worktree."""
from pathlib import Path

from skillflow.strict_patch import apply_code_patch


def apply_patch(patch: str = "", references=None, *, project_root: str = "",
                output_dir: str = "", output_target: str = "",
                run_id: str = "") -> dict:
    if output_target != "code":
        return {"error": "apply_patch requires code outputs; artifacts use their own tools"}
    if (not project_root or not output_dir
            or not Path(project_root).is_absolute() or not Path(output_dir).is_absolute()):
        return {"error": "apply_patch requires injected absolute code and output roots"}
    root = Path(project_root).resolve()
    if Path(output_dir).resolve() != root:
        return {"error": "apply_patch output root differs from the run's code worktree"}
    if references and not run_id:
        # The ledger of issued digests is per run. Without one there is nothing
        # a citation could be checked against, and accepting it would mean
        # editing coordinates on trust.
        return {"error": "apply_patch references require a run; none was injected"}
    return apply_code_patch(patch, root, references=references, run_id=run_id)
