"""Read a file from the project workspace.

Searches multiple directories in order:
  1. ``workspace_root`` — current run worktree, including uncommitted edits
  2. ``step_tmp_dir``   — current step's artifact files
  3. ``step_dir``       — current step's published artifacts
"""
from pathlib import Path


def read_file(path: str, start_line: int = 0, end_line: int | None = None,
              *, workspace_root: str = "",
              step_tmp_dir: str = "", step_dir: str = "",
              artifact_candidate: bool = False,
              artifact_revision: bool = False, output_target: str = "artifact") -> dict:
    # Build search path list: candidate first for carry-forward artifacts.
    if (artifact_candidate or artifact_revision) and output_target == "artifact":
        search_roots = ([("artifact candidate", step_tmp_dir)]
                        if step_tmp_dir else [])
        if not artifact_revision and workspace_root:
            search_roots.append(("project", workspace_root))
    else:
        search_roots = []
        if workspace_root:
            search_roots.append(("project", workspace_root))
        if step_tmp_dir:
            search_roots.append(("step staging", step_tmp_dir))
        if step_dir:
            search_roots.append(("step output", step_dir))

    full = None
    found_label = ""
    for label, root in search_roots:
        candidate = (Path(root) / path).resolve()
        ws = Path(root).resolve()
        if not candidate.is_relative_to(ws):
            continue  # traversal denied for this root
        if candidate.is_file():
            full = candidate
            found_label = label
            break

    if full is None:
        return {"error": f"File not found: {path}"}

    content = full.read_text(encoding="utf-8", errors="replace")
    lines = content.splitlines()
    if end_line is None:
        end_line = len(lines)
    result = lines[start_line:end_line]
    return {
        "content": "\n".join(
            f"{start_line + i + 1}\t{line}" for i, line in enumerate(result)
        ),
        "total_lines": len(lines),
        "returned_lines": len(result),
        "found_in": found_label,
    }
