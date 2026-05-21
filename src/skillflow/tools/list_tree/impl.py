"""List directory structure of the workspace."""

from pathlib import Path

BLOCKED = {".git", "__pycache__", ".venv", "node_modules", ".gitkeep", "_snapshot.json"}


def list_tree(path: str = ".", depth: int = 3, *,
              workspace_root: str = "") -> dict:
    root = Path(workspace_root)
    target = (root / path).resolve()
    if not str(target).startswith(str(root.resolve())):
        return {"error": f"Path traversal denied: {path}"}
    if not target.exists():
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

    return {"tree": "\n".join(entries), "entry_count": count}
