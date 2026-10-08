"""One byte snapshot for text readers after their existing path checks."""
import hashlib
import io
from pathlib import Path


def read_text_snapshot(path: Path, *, raw: bool = False) -> tuple[str, dict]:
    data = path.read_bytes()
    # Match the readers' existing UTF-8 replacement and universal-newline
    # behavior, with raw reads preserving CR/LF exactly.
    with io.TextIOWrapper(io.BytesIO(data), encoding="utf-8", errors="replace",
                          newline="" if raw else None) as stream:
        text = stream.read()
    return text, {"file_byte_sha256": hashlib.sha256(data).hexdigest(),
                  "byte_size": len(data)}
