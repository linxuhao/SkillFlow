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
    outbox = Path(workspace_root)
    outbox.mkdir(parents=True, exist_ok=True)
    target = (outbox / file).resolve()
    if not str(target).startswith(str(outbox.resolve())):
        return {"error": f"Path traversal denied: {file}"}
    target.write_text(content, encoding="utf-8")
    return {"written": str(file), "size": len(content)}
