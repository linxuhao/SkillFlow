"""Generate a directory tree summary for context injection."""

from pathlib import Path

BLOCKED = {".git", "__pycache__", ".venv", "node_modules", ".gitkeep", "_snapshot.json"}


def dir_tree(config_name: str = "", *, workspace_root: str = "",
             project_root: str = "") -> dict:
    """Return the project repository's tree, or an empty tree if it has none.

    ``workspace_root`` is accepted (every context/tool call supplies it) but is
    NOT a fallback root. It used to be: ``project_root or workspace_root``. On a
    run that declares no code repository the engine omits ``project_root``, so
    that fallback rendered the DPS WORKSPACE under the "repo root (write paths
    are relative to here)" header — telling the agent its own step directories
    were the repository.
    """
    parts = []

    # Project repo tree. The header is a COMMENT, not a path component: an
    # earlier version emitted a bare "project/" line, which models read as a
    # real directory and mirrored into write paths (e.g. project/pkg/x.py
    # alongside pkg/x.py) — see AT-9. Render entries as "./"-rooted, repo-
    # relative paths so there is exactly one unambiguous root to write to.
    proj = Path(project_root) if project_root else None
    if proj is not None and proj.exists():
        parts.append("# repo root (write paths are relative to here, e.g. ./pkg/mod.py):")
        parts.append("./")
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
