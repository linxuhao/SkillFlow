"""Registry for host-registered custom lint backends.

The generic ``lint`` tool dispatches per-extension via a linter manifest
(extension → backend name). Built-in backends are ``ruff``, ``djlint``,
``basic`` and ``skip``. A host application can extend the set at startup:

    from skillflow.lint_backends import register_backend

    def eslint_backend(fp: Path) -> dict:
        ...
        return {"file": str(fp), "passed": True, "error_message": ""}

    register_backend("eslint", eslint_backend)

A backend is a callable ``(Path) -> dict`` returning at least
``{"passed": bool, "error_message": str}``. Custom backends are consulted
before built-ins, so a host may also override a built-in name.

This module (not the tool's ``impl.py``) holds the registry because
ToolLoader loads ``impl.py`` standalone via ``spec_from_file_location`` —
state defined there would not be shared with the host's imports. Both
sides import this module normally, so they see the same singleton.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

LintBackend = Callable[[Path], dict]

_backends: dict[str, LintBackend] = {}


def register_backend(name: str, fn: LintBackend) -> None:
    """Register (or replace) a custom lint backend under ``name``."""
    _backends[name] = fn


def get_backend(name: str) -> LintBackend | None:
    """Return the custom backend registered under ``name``, if any."""
    return _backends.get(name)


def registered_backends() -> list[str]:
    """Sorted names of all registered custom backends."""
    return sorted(_backends)
