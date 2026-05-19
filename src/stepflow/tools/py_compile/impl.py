"""Python compile check."""

import py_compile as _pyc
from pathlib import Path
import tempfile
import os


def py_compile(file: str, *, workspace_root: str = "") -> dict:
    full = (Path(workspace_root) / file).resolve()

    if not full.exists():
        return {"verdict": "failed", "feedback": f"File not found: {file}"}

    if not file.endswith(".py"):
        return {"verdict": "passed", "feedback": ""}

    try:
        with tempfile.NamedTemporaryFile(suffix=".pyc", delete=False) as tmp:
            tmp_path = tmp.name
        _pyc.compile(str(full), cfile=tmp_path, doraise=True)
        os.unlink(tmp_path)
    except _pyc.PyCompileError as e:
        return {"verdict": "failed", "feedback": str(e)}
    except Exception as e:
        return {"verdict": "failed", "feedback": str(e)}

    return {"verdict": "passed", "feedback": ""}
