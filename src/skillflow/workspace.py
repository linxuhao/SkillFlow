"""Configurable workspace manager for skillflow.

Provides per-step atomic staging directories (tmp → step_dir) with
config-specific subdirectories. Host applications configure the base path.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path


def _sanitize_item(item: str) -> str:
    """Filesystem-safe, COLLISION-FREE folder name for a loop item key.

    Collapses anything not alnum/._- to '_' and caps length so an item value can
    never escape the step dir. When that transform is LOSSY (the sanitized text
    differs from the raw item, e.g. 'api/auth', 'Task A: setup', or any CJK item
    name), a short hash of the raw value is appended — distinct raw items always
    map to distinct folders. Without it, 'api/auth' collided with 'api_auth' and
    ALL pure-CJK names collapsed to 'item', so the second item's promotion
    rmtree'd the first item's output.
    """
    raw = str(item)
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._-")[:100]
    if s == raw:
        return s
    import hashlib
    suffix = hashlib.sha1(raw.encode("utf-8", "surrogatepass")).hexdigest()[:8]
    return f"{s}-{suffix}" if s else f"item-{suffix}"


def route_step_read_dir(step_dir: Path, producer_id: str, scope: str,
                        loop_context: dict | None) -> Path:
    """THE single routing rule for reading a step's promoted output.

    A loop-body producer's output lives per-item at ``{step}/{item}/``. A reader
    in the SAME loop (same item in flight) gets that item's folder — unless it
    asked for ``scope: all``. Every other reader (outside any loop, or in a
    different loop) gets the ``{step}/`` parent, i.e. ALL items — a drained
    loop's stale ``current_item`` must never route an aggregator to one item.
    Non-loop producers are untouched. Used by read_tools, context resolution,
    and any other consumer; do not reimplement this rule inline.
    """
    lc = loop_context or {}
    producer_loop = (lc.get("_loop_of") or {}).get(producer_id)
    if not producer_loop or scope == "all":
        return step_dir
    if lc.get("_reader_loop") != producer_loop:
        return step_dir  # outside reader → all items
    item = (lc.get("_loop_items") or {}).get(producer_loop)
    if not item:
        return step_dir
    return step_dir / _sanitize_item(item)



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
                 projects_base: str = "", code_dir: str = "",
                 code_path_resolver: Callable[[str], str | None] | None = None):
        self.base_path = Path(base_path).expanduser().resolve()
        self.base_path.mkdir(parents=True, exist_ok=True)
        self.projects_base = Path(projects_base).expanduser().resolve() if projects_base else self.base_path / "projects"
        self.projects_base.mkdir(parents=True, exist_ok=True)
        self._code_dir = Path(code_dir).expanduser().resolve() if code_dir else None
        # Optional host-provided callback mapping a project_id to its code
        # repository root — lets a host point individual projects at arbitrary
        # paths (e.g. an existing repo) that the default project_id-keyed
        # layout cannot express. Returning None falls back to the default.
        self._code_path_resolver = code_path_resolver

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

        Resolution order:
        1. ``code_path_resolver(project_id)`` if it was provided and returns a
           non-empty path — lets a host map a project to an arbitrary repo
           (e.g. an 'existing' repo the project was created against).
        2. ``_code_dir / project_id`` if ``code_dir`` was set.
        3. ``projects_base / project_id`` (default).
        """
        if self._code_path_resolver is not None:
            resolved = self._code_path_resolver(project_id)
            if resolved:
                return Path(resolved).expanduser().resolve()
        if self._code_dir:
            return (self._code_dir / project_id).resolve()
        return (self.projects_base / project_id).resolve()

    # ── Step-level paths ─────────────────────────────────────────────

    # ── New: per-step atomic directories ───────────────────────────────

    def get_step_tmp_dir(self, project_id: str, config_name: str,
                         step_id: str) -> Path:
        """Agent writes here during execution. Invisible to context resolver.

        Staging is FLAT even for loop-body steps: one iteration stages at a time,
        and promotion renames the whole ``.tmp`` onto the per-item target
        ``{step}/{item}/`` (see ``get_step_dir``'s ``item`` param).
        """
        p = self.get_config_path(project_id, config_name) / f"{step_id}.tmp"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def get_step_dir(self, project_id: str, config_name: str,
                     step_id: str, item: str | None = None) -> Path:
        """Atomic rename target. Context resolver reads from here.

        For a loop-body step, pass ``item`` (the loop's current item) to get the
        per-item folder ``{step}/{item}/`` so each iteration's output survives
        instead of the shared ``{step}/`` being replaced every iteration. Non-loop
        steps pass ``item=None`` → unchanged ``{step}/``.
        """
        p = self.get_config_path(project_id, config_name) / step_id
        if item:
            p = p / _sanitize_item(item)
        return p  # created by _step_commit, not here


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
                          step_id: str, params: dict,
                          item: str | None = None) -> dict:
        """Resolve ``$STEP_TMP_DIR``, ``$STEP_DIR`` etc. in param values.

        ``item``: the step's current loop item, when it is a loop-body step —
        $STEP_DIR then points at the per-item promotion target ``{step}/{item}/``
        (where the files ACTUALLY are). Without it, lifecycle hooks like
        ``repo_apply(source_dir=$STEP_DIR)`` read the flat parent and would
        commit item-named subfolders into the repo.
        """
        resolved = {}
        for key, value in params.items():
            if isinstance(value, str):
                value = (value
                         .replace("$STEP_TMP_DIR",
                                  str(self.get_step_tmp_dir(project_id, config_name, step_id)))
                         .replace("$STEP_DIR",
                                  str(self.get_step_dir(project_id, config_name, step_id,
                                                        item=item)))
                         # backward compat aliases
                         .replace("$STEP_DRAFT_DIR",
                                  str(self.get_step_tmp_dir(project_id, config_name, step_id)))
                         .replace("$STEP_FINAL_DIR",
                                  str(self.get_step_dir(project_id, config_name, step_id,
                                                        item=item)))
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
