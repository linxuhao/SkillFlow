"""Compose a complete artifact candidate before exposing its owning claim."""
from __future__ import annotations

import os
import shutil
from pathlib import Path


def _regular_files(root: Path):
    """Reject links and special files instead of following them."""
    if root.is_symlink():
        raise ValueError(f"Artifact directory must be a regular directory: {root}")
    if not root.exists():
        return
    if not root.is_dir():
        raise ValueError(f"Artifact root must be a directory: {root}")
    for directory, dirs, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        for name in dirs:
            if (parent / name).is_symlink():
                raise ValueError(f"Artifact directory is a symlink: {parent / name}")
        for name in files:
            path = parent / name
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"Artifact is not a regular file: {path}")
            yield path


def merge_candidate(candidate: Path, baseline: Path | None) -> list[str]:
    """Fill missing files from baseline; current-attempt files take precedence.

    SkillFlow calls this only during candidate initialization and persists
    readiness in the owning step's inputs. Reclaims keep the ready candidate,
    including explicit deletions and an empty file set.
    """
    candidate = Path(candidate)
    sources = list(_regular_files(Path(baseline))) if baseline is not None else []
    list(_regular_files(candidate))
    candidate.mkdir(parents=True, exist_ok=True)
    inherited = []
    for source in sources:
        relative = source.relative_to(baseline)
        target = candidate / relative
        if target.exists():
            if not target.is_file():
                raise ValueError(f"Artifact file conflicts with a directory: {relative}")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        inherited.append(relative.as_posix())
    return inherited
