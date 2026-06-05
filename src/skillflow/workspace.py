"""Configurable workspace manager for skillflow.

Provides per-step atomic staging directories (tmp → step_dir) with
config-specific subdirectories. Host applications configure the base path.
"""

from __future__ import annotations

import json
from pathlib import Path


class WorkspaceManager:
    """Manages workspace directories for skillflow pipeline runs.

    Layout::

        {base_path}/{project_id}/
        ├── {config_name}/
        │   ├── {step_id}.tmp/       ← agent writes (atomic staging)
        │   ├── {step_id}/           ← promoted on step commit
        │   └── Trace_{step_id}/
        ├── project/
        │   └── project_brief.md
        └── tasks/

    Usage::

        ws = WorkspaceManager(base_path="~/.skillflow/workspaces")
        ws.setup_step("my-project", "dpe_default", "1")
        draft = ws.get_step_tmp_dir("my-project", "dpe_default", "1")
        final = ws.get_step_dir("my-project", "dpe_default", "1")
    """

    def __init__(self, base_path: str = "~/.skillflow/workspaces",
                 projects_base: str = "", code_dir: str = ""):
        self.base_path = Path(base_path).expanduser().resolve()
        self.base_path.mkdir(parents=True, exist_ok=True)
        self.projects_base = Path(projects_base).expanduser().resolve() if projects_base else self.base_path / "projects"
        self.projects_base.mkdir(parents=True, exist_ok=True)
        self._code_dir = Path(code_dir).expanduser().resolve() if code_dir else None

    # ── Project-level paths ──────────────────────────────────────────

    def get_project_path(self, project_id: str) -> Path:
        """Return the workspace root for a project."""
        p = (self.base_path / project_id).resolve()
        if not str(p).startswith(str(self.base_path)):
            raise PermissionError(f"Path traversal denied: {project_id}")
        return p

    def get_config_path(self, project_id: str, config_name: str) -> Path:
        """Return the config subdirectory for a project."""
        p = self.get_project_path(project_id) / config_name
        p.mkdir(parents=True, exist_ok=True)
        return p

    def get_project_brief_dir(self, project_id: str) -> Path:
        p = self.get_project_path(project_id) / "project"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def get_tasks_dir(self, project_id: str) -> Path:
        p = self.get_project_path(project_id) / "tasks"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def get_project_code_path(self, project_id: str) -> Path:
        """Return the project's code repository root path.

        Default: _code_dir / project_id if code_dir was set,
        otherwise projects_base / project_id.
        Hosts with custom repo paths (e.g. 'existing' repos) should
        pass code_dir to __init__.
        """
        if self._code_dir:
            return (self._code_dir / project_id).resolve()
        return (self.projects_base / project_id).resolve()

    # ── Step-level paths ─────────────────────────────────────────────

    # ── New: per-step atomic directories ───────────────────────────────

    def get_step_tmp_dir(self, project_id: str, config_name: str,
                         step_id: str) -> Path:
        """Agent writes here during execution. Invisible to context resolver."""
        p = self.get_config_path(project_id, config_name) / f"{step_id}.tmp"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def get_step_dir(self, project_id: str, config_name: str,
                     step_id: str) -> Path:
        """Atomic rename target. Context resolver reads from here."""
        p = self.get_config_path(project_id, config_name) / step_id
        return p  # created by _step_commit, not here

    # ── Deprecated: kept for backward compat ────────────────────────────

    def get_inbox_dir(self, project_id: str, config_name: str,
                      step_id: str) -> Path:
        import warnings
        warnings.warn(
            "get_inbox_dir() is deprecated; use get_step_dir() instead. "
            "The legacy Inbox_* paths are no longer used by skillflow internally.",
            DeprecationWarning, stacklevel=2
        )
        p = self.get_config_path(project_id, config_name) / f"Inbox_{step_id}"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def get_draft_dir(self, project_id: str, config_name: str,
                      step_id: str) -> Path:
        import warnings
        warnings.warn(
            "get_draft_dir() is deprecated; use get_step_tmp_dir() instead. "
            "The legacy Outbox_Draft_* paths are no longer used by skillflow internally.",
            DeprecationWarning, stacklevel=2
        )
        p = self.get_config_path(project_id, config_name) / f"Outbox_Draft_{step_id}"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def get_final_dir(self, project_id: str, config_name: str,
                      step_id: str) -> Path:
        import warnings
        warnings.warn(
            "get_final_dir() is deprecated; use get_step_dir() instead. "
            "The legacy Outbox_Final_* paths are no longer used by skillflow internally.",
            DeprecationWarning, stacklevel=2
        )
        p = self.get_config_path(project_id, config_name) / f"Outbox_Final_{step_id}"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def get_trace_dir(self, project_id: str, config_name: str,
                      step_id: str) -> Path:
        p = self.get_config_path(project_id, config_name) / f"Trace_{step_id}"
        p.mkdir(parents=True, exist_ok=True)
        return p

    # ── Resolve step variables ──────────────────────────────────────

    def resolve_variables(self, project_id: str, config_name: str,
                          step_id: str, params: dict) -> dict:
        """Resolve ``$STEP_TMP_DIR``, ``$STEP_DIR`` etc. in param values."""
        resolved = {}
        for key, value in params.items():
            if isinstance(value, str):
                value = (value
                         .replace("$STEP_TMP_DIR",
                                  str(self.get_step_tmp_dir(project_id, config_name, step_id)))
                         .replace("$STEP_DIR",
                                  str(self.get_step_dir(project_id, config_name, step_id)))
                         # backward compat aliases
                         .replace("$STEP_DRAFT_DIR",
                                  str(self.get_step_tmp_dir(project_id, config_name, step_id)))
                         .replace("$STEP_FINAL_DIR",
                                  str(self.get_step_dir(project_id, config_name, step_id)))
                         .replace("$TASK_DIR",
                                  str(self.get_tasks_dir(project_id)))
                         .replace("$CONFIG_DIR",
                                  str(self.get_config_path(project_id, config_name)))
                         .replace("$PROJECT_ROOT",
                                  str(self.projects_base / project_id)))
            resolved[key] = value
        return resolved

    # ── Cross-config context ────────────────────────────────────────

    def read_output(self, project_id: str, config_name: str,
                    step_id: str, filename: str) -> str | None:
        """Read a file from a step's output directory."""
        path = self.get_step_dir(project_id, config_name, step_id) / filename
        if path.exists():
            return path.read_text(encoding="utf-8", errors="replace")
        return None

    def read_cross_config(self, project_id: str, source_config: str,
                          output_filename: str) -> str | None:
        """Read a file from another config's output (any step)."""
        config_dir = self.get_project_path(project_id) / source_config
        if not config_dir.exists():
            return None
        # Search new-style step dirs first, then legacy Outbox_Final_*
        for d in sorted(config_dir.glob("*")):
            if d.name.endswith(".tmp") or d.name.startswith("Outbox_Draft"):
                continue
            if d.is_dir():
                f = d / output_filename
                if f.exists():
                    return f.read_text(encoding="utf-8", errors="replace")
        return None

    # ── Brief access ─────────────────────────────────────────────────

    def read_brief(self, project_id: str) -> str | None:
        path = self.get_project_brief_dir(project_id) / "project_brief.md"
        if path.exists():
            return path.read_text(encoding="utf-8", errors="replace")
        return None

    def write_brief(self, project_id: str, content: str) -> Path:
        path = self.get_project_brief_dir(project_id) / "project_brief.md"
        path.write_text(content, encoding="utf-8")
        return path
