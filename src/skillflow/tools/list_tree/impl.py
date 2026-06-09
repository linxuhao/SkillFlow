"""List directory structure of the workspace.

Searches the same multi-directory order as read_file:
  1. ``workspace_root`` — project code repo
  2. ``step_tmp_dir``   — current step's .tmp staging
  3. ``step_dir``       — current step's final dir
"""

from pathlib import Path

BLOCKED = {".git", "__pycache__", ".venv", "node_modules", ".gitkeep", "_snapshot.json"}


def list_tree(path: str = ".", depth: int = 3, *,
              workspace_root: str = "",
              step_tmp_dir: str = "", step_dir: str = "") -> dict:
    # Build search path list
    search_roots: list[tuple[str, str]] = []
    if workspace_root:
        search_roots.append(("project", workspace_root))
    if step_tmp_dir:
        search_roots.append(("step staging", step_tmp_dir))
    if step_dir:
        search_roots.append(("step output", step_dir))

    target = None
    found_root = ""
    found_label = ""
    for label, root_dir in search_roots:
        root = Path(root_dir)
        candidate = (root / path).resolve()
        if not str(candidate).startswith(str(root.resolve())):
            continue
        if candidate.exists():
            target = candidate
            found_root = root_dir
            found_label = label
            break

    if target is None:
        return {"error": f"Directory not found: {path}"}

    max_depth = min(depth, 4)
    max_entries = 200
    entries: list[str] = []
    count = 0

    for item in sorted(target.rglob("*")):
        if count >= max_entries:
            entries.append(f"... [truncated at {max_entries} entries]")
            break
        rel = item.relative_to(target)
        parts = rel.parts
        if len(parts) > max_depth:
            continue
        if any(p in BLOCKED for p in parts):
            continue
        indent = "  " * len(parts)
        if item.is_dir():
            entries.append(f"{indent}{parts[-1]}/")
        else:
            size = item.stat().st_size
            size_str = f"{size}b" if size < 1024 else f"{size // 1024}kb"
            entries.append(f"{indent}{parts[-1]}  ({size_str})")
        count += 1

    return {
        "tree": "\n".join(entries),
        "entry_count": count,
        "found_in": found_label,
    }
