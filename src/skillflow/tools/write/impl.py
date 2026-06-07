"""Write content to a file in the workspace."""

import json
from pathlib import Path


def _ensure_str(value, default: str = "") -> str:
    """Coerce value to string — LLM native tool calling may pass dict for string params."""
    if isinstance(value, str):
        return value
    if value is None:
        return default
    return json.dumps(value, ensure_ascii=False)


def write(file: str, content: str, *, workspace_root: str = "") -> dict:
    content = _ensure_str(content)
    root = Path(workspace_root)
    root.mkdir(parents=True, exist_ok=True)
    # AT-9: strip a leading 'project/' phantom-root component so the same file
    # isn't written under both project/pkg/x.py and pkg/x.py.
    parts = list(Path(file).parts)
    if parts and parts[0] == "project":
        parts = parts[1:]
    rel = str(Path(*parts)) if parts else file
    target = (root / rel).resolve()
    if not str(target).startswith(str(root.resolve())):
        return {"error": f"Path traversal denied: {file}"}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"written": rel, "size": len(content)}
