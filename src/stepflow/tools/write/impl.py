"""Write content to a file in the workspace."""

from pathlib import Path


def write(file: str, content: str, *, workspace_root: str = "") -> dict:
    outbox = Path(workspace_root)
    outbox.mkdir(parents=True, exist_ok=True)
    target = (outbox / file).resolve()
    if not str(target).startswith(str(outbox.resolve())):
        return {"error": f"Path traversal denied: {file}"}
    target.write_text(content, encoding="utf-8")
    return {"written": str(file), "size": len(content)}
