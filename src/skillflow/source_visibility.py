"""Shared visibility rules for source trees exposed to agents.

Only engine-owned storage is hidden here. Dot-directories such as ``.github``
are ordinary project source and deliberately remain visible.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator


INTERNAL_SOURCE_STORAGE_DIRS = frozenset({".zvec-grep"})


def is_internal_source_storage(path: str | Path) -> bool:
    """Return whether a relative path enters engine-owned source storage."""
    return any(part in INTERNAL_SOURCE_STORAGE_DIRS for part in Path(path).parts)


def prune_internal_source_storage(dirnames: list[str]) -> None:
    """Prune engine-owned storage in-place for an ``os.walk`` traversal."""
    dirnames[:] = sorted(
        name for name in dirnames if name not in INTERNAL_SOURCE_STORAGE_DIRS
    )


def iter_visible_source_paths(root: str | Path) -> Iterator[Path]:
    """Yield a deterministic tree without descending into internal storage."""
    base = Path(root)
    if is_internal_source_storage(base):
        return
    visible: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        prune_internal_source_storage(dirnames)
        current = Path(dirpath)
        for name in dirnames:
            visible.append(current / name)
        for name in sorted(filenames):
            visible.append(current / name)
    yield from sorted(visible)


def iter_visible_source_files(root: str | Path, glob: str | None = None) -> Iterator[Path]:
    """Yield visible files, optionally applying pathlib-compatible matching."""
    base = Path(root)
    for path in iter_visible_source_paths(base):
        if not path.is_file():
            continue
        rel = path.relative_to(base)
        if glob and not rel.match(glob):
            continue
        yield path
