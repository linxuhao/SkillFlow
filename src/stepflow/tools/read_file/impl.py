"""Read a file from the project workspace."""

from pathlib import Path


def read_file(path: str, start_line: int = 0, end_line: int | None = None,
              *, workspace_root: str = "") -> dict:
    full = (Path(workspace_root) / path).resolve()
    ws = Path(workspace_root).resolve()
    if not str(full).startswith(str(ws)):
        return {"error": f"Path traversal denied: {path}"}
    if not full.is_file():
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
    }
