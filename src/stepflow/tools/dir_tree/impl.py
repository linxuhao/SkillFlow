"""Generate a directory tree summary for context injection."""

from pathlib import Path

BLOCKED = {".git", "__pycache__", ".venv", "node_modules", ".gitkeep", "_snapshot.json"}


def dir_tree(config_name: str = "", *, workspace_root: str = "",
             project_root: str = "") -> dict:
    """Return a combined tree of workspace + project directories."""
    parts = []

    # Project repo tree
    proj = Path(project_root) if project_root else Path(workspace_root)
    if proj.exists():
        parts.append("project/")
        for item in sorted(proj.rglob("*"))[:100]:
            rel = item.relative_to(proj)
            if len(rel.parts) > 3:
                continue
            if any(p in BLOCKED for p in rel.parts):
                continue
            indent = "  " * len(rel.parts)
            name = rel.parts[-1]
            if item.is_dir():
                parts.append(f"{indent}{name}/")
            else:
                size = item.stat().st_size
                sz = f"{size}b" if size < 1024 else f"{size // 1024}kb"
                parts.append(f"{indent}{name}  ({sz})")

    return {"tree": "\n".join(parts), "format": "plain"}
