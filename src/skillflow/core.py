"""SkillFlow main class.

Provides the full run lifecycle: create, claim, execute (application),
confirm/fail, advance, checkpoint, and recovery. Uses a persistent
SQLite connection (single-worker model) with WAL mode for safety.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
import warnings
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from skillflow.tool_loader import ToolLoader

from skillflow.schema import ALL_DDL
from skillflow.graph import (
    EndConditions,
    EndResult,
    GraphResolver,
    PipelineGraph,
    StepNode,
    Transition,
)
from skillflow.exceptions import (
    CycleLimitExceeded,
    GraphValidationError,
    NoMatchingTransition,
    OutputValidationError,
    StepVersionConflict,
    SkillFlowError,
)


# ── Internal abort signal for intentional rollback within _tx ────────

class _TxRollback(Exception):
    """Raised inside a _tx block to intentionally roll back."""
    pass


@dataclass(frozen=True)
class ClaimToken:
    step_id: str
    run_id: str
    step_instance_id: int
    version: int
    claimed_at: float


@dataclass(frozen=True)
class ClaimedStep:
    token: ClaimToken
    step_id: str
    step_config: dict
    run_context: dict
    inputs: dict[str, dict]
    validation_error: str | None = None
    error_context: dict | None = None
    emit: Callable[[str, dict], Any] = field(
        default=lambda event_type, payload: _noop_emit(event_type, payload)
    )
    # Durable trace sink — bound by claim_next_step to SkillFlow.trace with the
    # run/step/instance ids prefilled. Lets the host record full prompts,
    # responses and actions to the append-only trace. No-op by default so a
    # ClaimedStep built in isolation (tests) still works.
    trace: Callable[..., None] = field(default=lambda category, event, payload=None: None)

    @property
    def step_instance_id(self) -> int:
        """FW-7: top-level convenience accessor for token.step_instance_id."""
        return self.token.step_instance_id

    def flat_inputs(self) -> dict:
        result: dict = {}
        for step_outputs in self.inputs.values():
            result.update(step_outputs)
        return result


def _noop_emit(event_type: str, payload: dict) -> None:
    pass


# Labels under which the framework surfaces re-open context (reject/loop-back
# feedback and validation errors) into a claimed step's _resolved_context, so
# the host renders them into the prompt in any tool mode.
_FEEDBACK_CONTEXT_LABEL = (
    "⚠️ Reviewer / User Feedback — MUST ADDRESS before resubmitting"
)
_VALIDATION_ERROR_CONTEXT_LABEL = (
    "⚠️ Previous attempt failed validation — MUST FIX"
)


@dataclass(frozen=True)
class StepResult:
    outputs: dict = field(default_factory=dict)
    flags: dict = field(default_factory=dict)


@dataclass(frozen=True)
class OutboxEvent:
    id: int
    event_type: str
    payload_json: str
    stream_target: str


class StepRunner(Protocol):
    async def execute(self, step: ClaimedStep) -> StepResult: ...


class SkillFlow:
    """Transactional graph orchestrator with embedded SQLite."""

    def __init__(self, db_path: str = ":memory:", *,
                 tool_loader: "ToolLoader | None" = None,
                 stale_threshold_seconds: float = 300,
                 notification_bus: "NotificationBus | None" = None,
                 workspace_base: str = "",
                 projects_base: str = "",
                 code_dir: str = "",
                 code_path_resolver: "Callable[[str], str | None] | None" = None,
                 delegate_tools_to_agent: bool = False,
                 trace_enabled: bool = True,
                 trace_db_path: str | None = None):
        self._db_path = db_path
        self._graphs: dict[str, PipelineGraph] = {}
        self._resolvers: dict[str, GraphResolver] = {}
        self._lock = threading.RLock()
        self._tool_loader = tool_loader
        # Durable run trace. Per-run seq is computed atomically inside each
        # INSERT (the (run_id, seq) index makes it an O(log n) seek) so the
        # "unique per run" contract holds across concurrent PROCESSES too.
        # Set trace_enabled=False to disable the trace entirely (zero write
        # overhead) for latency-sensitive hosts.
        self._trace_enabled = trace_enabled
        # Per-project trace DB: when set, trace records are written to
        # {trace_db_path}/{project_id}/trace.db instead of the shared DB.
        # None (default) = backward-compat: trace goes into the shared
        # skillflow_trace table in self._conn.
        self._trace_db_path = trace_db_path
        self._trace_conns: dict[str, sqlite3.Connection] = {}
        self._load_native_tools()
        self._stale_threshold = stale_threshold_seconds
        self._workspace = None
        self.delegate_tools_to_agent = delegate_tools_to_agent
        if workspace_base:
            from skillflow.workspace import WorkspaceManager
            self._workspace = WorkspaceManager(workspace_base, projects_base=projects_base,
                                                 code_dir=code_dir,
                                                 code_path_resolver=code_path_resolver)

        from skillflow.agent_registry import AgentRegistry
        self.agent_registry = AgentRegistry()

        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys = ON;")
        self._conn.execute("PRAGMA busy_timeout = 5000;")
        # Main DDL (CREATE TABLE IF NOT EXISTS — always safe)
        for stmt in ALL_DDL:
            self._conn.execute(stmt)
        # Indexes
        from skillflow.schema import SKILLFLOW_INDEXES
        for stmt in SKILLFLOW_INDEXES:
            self._conn.execute(stmt)
        # Migrations — idempotent DDL (skip if already applied)
        from skillflow.schema import SKILLFLOW_MIGRATIONS
        for stmt in SKILLFLOW_MIGRATIONS:
            try:
                self._conn.execute(stmt)
            except sqlite3.OperationalError:
                # Column/index already exists or DB locked — fine
                pass
        self._conn.commit()

        # Notification bus — shared with host app for real-time push
        if notification_bus is not None:
            self.notifications = notification_bus
        else:
            from skillflow.notifications import NotificationBus
            self.notifications = NotificationBus(db_path=db_path)
        self.notifications.set_connection(self._conn)

    def _load_native_tools(self):
        """Ensure the built-in tools directory is loaded as the native source."""
        native_dir = Path(__file__).parent / "tools"
        if self._tool_loader is None:
            from skillflow.tool_loader import ToolLoader
            self._tool_loader = ToolLoader(native_dir)
        elif hasattr(self._tool_loader, '_tools_dirs'):
            # Only manipulate real ToolLoader instances, not duck-typed mocks
            if native_dir not in self._tool_loader._tools_dirs:
                self._tool_loader._tools_dirs.insert(0, native_dir)
                self._tool_loader._cache.clear()
                self._tool_loader._tool_dir_cache.clear()
        # Register plugin tools (e.g. skillflow_lint)
        linter_dir = Path(__file__).parent / "plugins" / "linter" / "tools"
        if linter_dir.exists() and hasattr(self._tool_loader, '_tools_dirs'):
            if linter_dir not in self._tool_loader._tools_dirs:
                self._tool_loader._tools_dirs.append(linter_dir)
                self._tool_loader._cache.clear()
                self._tool_loader._tool_dir_cache.clear()

    def _should_delegate_tool(self, tool_name: str) -> bool:
        """Return True if this tool should be delegated to the agent.

        In framework mode (delegate_tools_to_agent=False), never delegate.
        In runner mode (delegate_tools_to_agent=True), only native tools
        are auto-executed; everything else goes to the agent.
        """
        if not self.delegate_tools_to_agent:
            return False
        if self._tool_loader is None:
            return True
        return not self._tool_loader.is_native(tool_name)

    # ── Per-project trace DB helpers ──────────────────────────────────

    def _trace_db_path_for(self, project_id: str) -> Path | None:
        """Return the per-project trace DB path, or None if not configured."""
        if not self._trace_db_path or not project_id:
            return None
        return Path(self._trace_db_path) / project_id / "trace.db"

    def _get_trace_conn(self, project_id: str) -> sqlite3.Connection | None:
        """Get or create a cached SQLite connection for a project's trace.db.

        Returns None when per-project trace DBs are not configured (backward
        compat — caller should fall back to self._conn).
        """
        if not self._trace_db_path:
            return None
        if project_id in self._trace_conns:
            return self._trace_conns[project_id]
        db_path = self._trace_db_path_for(project_id)
        if db_path is None:
            return None
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        self._ensure_trace_table(conn)
        self._trace_conns[project_id] = conn
        return conn

    @staticmethod
    def _ensure_trace_table(conn: sqlite3.Connection) -> None:
        """Create the skillflow_trace table in a per-project DB if missing."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS skillflow_trace (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id           TEXT NOT NULL,
                step_id          TEXT,
                step_instance_id INTEGER,
                seq              INTEGER NOT NULL,
                category         TEXT NOT NULL,
                event            TEXT NOT NULL,
                payload_json     TEXT NOT NULL DEFAULT '{}',
                created_at       TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_skillflow_trace_run "
            "ON skillflow_trace(run_id, seq)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_skillflow_trace_step "
            "ON skillflow_trace(step_instance_id)"
        )
        conn.commit()

    def _close_trace_conn(self, project_id: str) -> None:
        """Close and evict a cached per-project trace connection."""
        conn = self._trace_conns.pop(project_id, None)
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    @contextmanager
    def _tx(self):
        """Serialised transaction context.

        Yields the persistent connection with BEGIN IMMEDIATE already
        started.  Commits on clean exit, rolls back on any exception
        (including _TxRollback, which is used for intentional abort).
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE;")
            try:
                yield self._conn
            except _TxRollback:
                self._conn.rollback()
            except Exception:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    @staticmethod
    def _serialize(obj: dict) -> str:
        return json.dumps(obj, ensure_ascii=False)

    @staticmethod
    def _deserialize(text: str) -> dict:
        if not text:
            return {}
        if isinstance(text, dict):
            return text  # SQLite json_set may return pre-parsed dict
        return json.loads(text)

    # ── Graph management ──────────────────────────────────────────

    def register_graph(self, graph: PipelineGraph) -> None:
        issues = graph.validate()
        if issues:
            raise GraphValidationError(issues)
        # Validate agent_config references exist in registry
        missing = self._check_agent_configs(graph)
        if missing:
            raise GraphValidationError([
                f"Agent config '{name}' referenced in graph but not registered"
                for name in missing
            ])
        resolver = GraphResolver(graph)
        self._graphs[graph.name] = graph
        self._resolvers[graph.name] = resolver
        with self._tx() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO skillflow_graphs (name, yaml_text, version, updated_at)
                VALUES (?, ?, COALESCE((SELECT version + 1 FROM skillflow_graphs WHERE name=?), 1),
                        datetime('now'))
                """,
                (graph.name, json.dumps(graph.to_dict()), graph.name),
            )

    def list_graphs(self) -> list[dict]:
        """Return all registered graphs as ``{name, version, description}``.

        ``description`` is parsed out of the stored ``yaml_text`` JSON (there is
        no dedicated column for it). Used by hosts to enumerate available
        configs for a picker / dashboard without knowing them ahead of time.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT name, yaml_text, version FROM skillflow_graphs ORDER BY name ASC"
            ).fetchall()
        out: list[dict] = []
        for r in rows:
            description = None
            try:
                description = json.loads(r["yaml_text"]).get("description")
            except (ValueError, TypeError):
                pass
            out.append({"name": r["name"], "version": r["version"], "description": description})
        return out

    def register_agent_config(self, name: str, **kwargs) -> None:
        """Register an agent config so graph validation can check references."""
        self.agent_registry.register(name, **kwargs)
        if self._tool_loader:
            self.agent_registry.resolve_tool_schemas(self._tool_loader)

    def register_agent_config_from_dict(self, name: str, d: dict) -> None:
        """Register from a flat dict (convenience for YAML-loaded configs)."""
        self.agent_registry.register_dict(name, d)
        if self._tool_loader:
            self.agent_registry.resolve_tool_schemas(self._tool_loader)

    def _check_agent_configs(self, graph: PipelineGraph) -> list[str]:
        """Return names of agent_configs referenced in graph but not registered."""
        missing: list[str] = []
        for node in graph.steps:
            if node.agent_config and node.agent_config not in self.agent_registry:
                missing.append(node.agent_config)
        return missing

    def _get_resolver(self, graph_name: str) -> GraphResolver:
        resolver = self._resolvers.get(graph_name)
        if resolver is not None:
            return resolver
        with self._lock:
            row = self._conn.execute(
                "SELECT yaml_text FROM skillflow_graphs WHERE name = ?", (graph_name,)
            ).fetchone()
        if not row:
            raise SkillFlowError(f"Graph '{graph_name}' not registered")
        data = json.loads(row["yaml_text"])
        graph = PipelineGraph._from_dict(data)
        resolver = GraphResolver(graph)
        self._graphs[graph_name] = graph
        self._resolvers[graph_name] = resolver
        return resolver

    def _get_resolver_for_run(self, run_id: str) -> GraphResolver:
        with self._lock:
            row = self._conn.execute(
                "SELECT graph_name FROM skillflow_runs WHERE id = ?", (run_id,)
            ).fetchone()
        if not row:
            raise SkillFlowError(f"Run '{run_id}' not found")
        return self._get_resolver(row["graph_name"])

    # ── Run lifecycle ──────────────────────────────────────────────

    def create_run(self, graph_name: str, context: dict | None = None,
                   project_id: str = None, *,
                   graph_path: str | None = None) -> str:
        resolver = self._get_resolver(graph_name)
        graph = resolver.graph
        run_id = str(uuid.uuid4())
        ctx = context or {}

        # Extract project_id from context if not explicitly given
        if project_id is None:
            project_id = ctx.get("project_id")

        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO skillflow_runs (id, graph_name, graph_path, project_id, context_json, current_node, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
                """,
                (run_id, graph_name, graph_path, project_id, self._serialize(ctx), graph.begin),
            )
            for node in graph.steps:
                conn.execute(
                    """
                    INSERT INTO skillflow_steps
                        (run_id, step_id, step_config_json, max_retries, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'pending', datetime('now'), datetime('now'))
                    """,
                    (run_id, node.id, self._serialize(node.config), node.max_retries),
                )
            for node in graph.steps:
                for trans in node.transitions:
                    if trans.max_loop is not None:
                        conn.execute(
                            """
                            INSERT INTO skillflow_edge_counts
                                (run_id, from_step, to_step, count, max_loop)
                            VALUES (?, ?, ?, 0, ?)
                            """,
                            (run_id, node.id, trans.to, trans.max_loop),
                        )
            self.notifications.publish_sync(
                "run_created",
                {"run_id": run_id, "graph_name": graph_name, "project_id": project_id},
                run_id=run_id,
            )
        return run_id

    def start_run(self, run_id: str) -> None:
        with self._tx() as conn:
            cur = conn.execute(
                """
                UPDATE skillflow_runs SET status = 'running', started_at = datetime('now'),
                    updated_at = datetime('now')
                WHERE id = ? AND status = 'pending'
                """,
                (run_id,),
            )
            if cur.rowcount == 0:
                raise SkillFlowError(f"Run '{run_id}' not found or not in 'pending' status")
            _proj = conn.execute(
                "SELECT project_id FROM skillflow_runs WHERE id = ?", (run_id,)
            ).fetchone()
            self.notifications.publish_sync(
                "run_started",
                {"run_id": run_id, "project_id": _proj["project_id"] if _proj else None},
                run_id=run_id,
            )

    def pause_run(self, run_id: str) -> None:
        self._update_run_state(run_id, "paused")

    def resume_run(self, run_id: str) -> None:
        self._update_run_state(run_id, "running")

    def reactivate_run(self, run_id: str) -> None:
        """Reactivate a failed run back to running state.

        Resets the step that caused the failure to pending so it gets
        re-executed, and points current_node at it. If the failure
        reason can't be mapped to a specific step, falls back to
        re-resolving from the graph start.
        """
        with self._tx() as conn:
            run = conn.execute(
                "SELECT status, error_reason, graph_name FROM skillflow_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if not run:
                raise ValueError(f"Run not found: {run_id}")
            if run["status"] == "completed":
                raise ValueError(
                    f"Run {run_id} is already completed. "
                    f"Use re_run() to explicitly re-run a completed pipeline."
                )

            # Try to find which step caused the failure
            error_reason = run["error_reason"] or ""
            retry_step_id = self._extract_step_from_error(error_reason)
            if not retry_step_id:
                # Fallback: use the last completed step
                last = conn.execute(
                    "SELECT step_id FROM skillflow_steps WHERE run_id = ? "
                    "AND status = 'completed' ORDER BY id DESC LIMIT 1",
                    (run_id,),
                ).fetchone()
                if last:
                    retry_step_id = last["step_id"]

            # Guard: the resume step must still exist in the (possibly changed)
            # graph. If the graph was edited since this run started — e.g. a node
            # was removed — pointing current_node at a now-missing step makes
            # advance_run() return None forever (a silent deadlock). Fail loudly
            # so the caller can tell the user to start a fresh run. Raising here
            # rolls back the surrounding transaction, so no partial state lands.
            if retry_step_id and self._get_resolver(
                    run["graph_name"]).get_node(retry_step_id) is None:
                raise ValueError(
                    f"Cannot reactivate run {run_id}: its resume step "
                    f"'{retry_step_id}' no longer exists in graph "
                    f"'{run['graph_name']}' (the graph changed since this run "
                    f"started). Start a new run instead."
                )

            if retry_step_id:
                # Reset the latest instance of the failed step to pending
                conn.execute(
                    """UPDATE skillflow_steps SET status = 'pending',
                       version = version + 1,
                       outputs_json = '{}', result_flags_json = '{}',
                       updated_at = datetime('now')
                    WHERE id = (
                        SELECT id FROM skillflow_steps
                        WHERE run_id = ? AND step_id = ? AND status = 'completed'
                        ORDER BY id DESC LIMIT 1
                    )""",
                    (run_id, retry_step_id),
                )

            conn.execute(
                """UPDATE skillflow_runs SET status = 'running',
                   error_reason = NULL,
                   current_node = ?,
                   updated_at = datetime('now') WHERE id = ?""",
                (retry_step_id, run_id),
            )

    @staticmethod
    def _extract_step_from_error(error: str) -> str | None:
        """Extract a step_id from a transition error message like
        \"No matching transition from 't_impl_review' with flags {}\"."""
        import re
        m = re.search(r"from '(\w+)'", error)
        if m:
            return m.group(1)
        # Also try: "Lifecycle hook failed: ..." — can't extract step, return None
        return None

    def re_run(self, run_id: str) -> str:
        """Explicitly restart a completed/failed run as a fresh run.

        Creates a NEW run_id with the same graph and project,
        resetting all step state. Returns the new run_id.
        """
        with self._tx() as conn:
            old = conn.execute(
                "SELECT graph_name, graph_path, project_id, context_json "
                "FROM skillflow_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if not old:
                raise ValueError(f"Run not found: {run_id}")

        import json
        ctx = json.loads(old["context_json"]) if old["context_json"] else {}
        new_id = self.create_run(
            old["graph_name"],
            context=ctx,
            project_id=old["project_id"],
            graph_path=old["graph_path"],
        )
        self.start_run(new_id)
        return new_id

    def fail_run(self, run_id: str, reason: str) -> None:
        with self._tx() as conn:
            self._fail_run_in_tx(conn, run_id, reason)

    def complete_run(self, run_id: str) -> None:
        with self._tx() as conn:
            self._complete_run_in_tx(conn, run_id, "Run completed")

    def _update_run_state(self, run_id: str, status: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE skillflow_runs SET status = ?, updated_at = datetime('now') WHERE id = ?",
                (status, run_id),
            )

    def get_run(self, run_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM skillflow_runs WHERE id = ?", (run_id,)
            ).fetchone()
            return dict(row) if row else None

    # ── Project CRUD (Wolverine-style: framework owns project state) ─

    def create_project(self, project_id: str, name: str = "",
                       meta: dict | None = None) -> None:
        with self._tx() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO skillflow_projects (id, name, meta_json, created_at, updated_at)
                   VALUES (?, ?, ?, datetime('now'), datetime('now'))""",
                (project_id, name, self._serialize(meta or {})),
            )

    def get_project(self, project_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM skillflow_projects WHERE id = ?", (project_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_projects(self, status: str = None) -> list[dict]:
        with self._lock:
            if status:
                rows = self._conn.execute(
                    "SELECT * FROM skillflow_projects WHERE status = ? ORDER BY created_at DESC",
                    (status,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM skillflow_projects ORDER BY created_at DESC"
                ).fetchall()
            return [dict(r) for r in rows]

    def update_project_status(self, project_id: str, status: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE skillflow_projects SET status = ?, updated_at = datetime('now') WHERE id = ?",
                (status, project_id),
            )

    def delete_project(self, project_id: str) -> None:
        """Delete all skillflow state for a project.

        Removes runs, steps, edge counts, loop state, outbox events,
        trace records, and the project row itself.  When per-project trace
        DBs are active, the cached connection is closed so the caller can
        safely delete the workspace directory (including ``trace.db``)
        from the filesystem; shared-DB trace rows are deleted inline.
        Safe to call even if the project has no runs.
        """
        with self._tx() as conn:
            # Collect all run IDs for this project
            run_ids = [
                r["id"] for r in conn.execute(
                    "SELECT id FROM skillflow_runs WHERE project_id = ?",
                    (project_id,),
                ).fetchall()
            ]
            for run_id in run_ids:
                conn.execute("DELETE FROM skillflow_steps WHERE run_id = ?", (run_id,))
                conn.execute("DELETE FROM skillflow_edge_counts WHERE run_id = ?", (run_id,))
                conn.execute("DELETE FROM skillflow_loop_state WHERE run_id = ?", (run_id,))
                conn.execute("DELETE FROM skillflow_outbox WHERE payload_json LIKE ?",
                             (f"%{run_id}%",))
                # Shared-DB mode: delete trace rows from the main DB.
                # Per-project mode: the caller handles trace.db via filesystem.
                if not self._trace_db_path:
                    conn.execute(
                        "DELETE FROM skillflow_trace WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM skillflow_runs WHERE project_id = ?", (project_id,))
            conn.execute("DELETE FROM skillflow_projects WHERE id = ?", (project_id,))
        # Per-project mode: close the cached connection so the workspace
        # directory (including trace.db) can be safely deleted.
        if self._trace_db_path:
            self._close_trace_conn(project_id)

    # ── Query APIs ──────────────────────────────────────────────────

    def list_runs(self, project_id: str = None, status: str = None) -> list[dict]:
        with self._lock:
            clauses = []
            params: list = []
            if project_id:
                clauses.append("project_id = ?")
                params.append(project_id)
            if status:
                clauses.append("status = ?")
                params.append(status)
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            rows = self._conn.execute(
                f"SELECT * FROM skillflow_runs {where} ORDER BY created_at DESC",
                params,
            ).fetchall()
            return [dict(r) for r in rows]

    def get_steps(self, run_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM skillflow_steps
                   WHERE run_id = ? ORDER BY id ASC""",
                (run_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_run_by_project(self, project_id: str,
                           graph_name: str | None = None) -> dict | None:
        """Latest non-completed run for ``project_id``.

        When ``graph_name`` is given, the lookup is scoped to that config so a
        single ``project_id`` can carry one live run per config without them
        colliding. ``graph_name=None`` preserves the original behaviour (any
        non-completed run, newest first).
        """
        with self._lock:
            if graph_name is not None:
                row = self._conn.execute(
                    """SELECT * FROM skillflow_runs
                       WHERE project_id = ? AND graph_name = ?
                         AND status NOT IN ('completed')
                       ORDER BY created_at DESC LIMIT 1""",
                    (project_id, graph_name),
                ).fetchone()
            else:
                row = self._conn.execute(
                    """SELECT * FROM skillflow_runs
                       WHERE project_id = ? AND status NOT IN ('completed')
                       ORDER BY created_at DESC LIMIT 1""",
                    (project_id,),
                ).fetchone()
            return dict(row) if row else None

    def get_or_create_run(self, graph_name: str, project_id: str,
                          context: dict | None = None) -> str:
        # Scope the reuse lookup to this graph so a project that runs more than
        # one config (e.g. a meta_conversation run and its DPE run) gets a
        # distinct run per config instead of accidentally reusing another.
        existing = self.get_run_by_project(project_id, graph_name=graph_name)
        if existing:
            return existing["id"]
        return self.create_run(graph_name, context, project_id=project_id)

    def start_project(self, project_id: str, graph_name: str,
                      context: dict | None = None) -> str:
        self.create_project(project_id)
        run_id = self.create_run(graph_name, context, project_id=project_id)
        self.start_run(run_id)
        return run_id

    # ── Claim / Confirm / Fail ─────────────────────────────────────

    def claim_next_step(self, run_id: str) -> ClaimedStep | None:
        with self._tx() as conn:
            run = conn.execute(
                "SELECT * FROM skillflow_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if not run or run["status"] not in ("running",) or not run["current_node"]:
                raise _TxRollback()

            graph_name = run["graph_name"]
            resolver = self._get_resolver(graph_name)

            if resolver.is_gate(run["current_node"]):
                raise _TxRollback()

            node = resolver.get_node(run["current_node"])
            if not node:
                raise _TxRollback()

            current_version = conn.execute(
                "SELECT version FROM skillflow_steps WHERE run_id = ? AND step_id = ? AND status = 'pending'",
                (run_id, run["current_node"]),
            ).fetchone()
            if not current_version:
                # For cyclic graphs: if the step has already been executed
                # (completed/failed), create a new instance for the next iteration
                # For cyclic graphs: create a new instance if the step was
                # previously completed or failed (not if it's currently claimed)
                existing = conn.execute(
                    "SELECT id, status FROM skillflow_steps WHERE run_id = ? AND step_id = ?",
                    (run_id, run["current_node"]),
                ).fetchone()
                if existing and existing["status"] in ("completed", "failed"):
                    node = resolver.get_node(run["current_node"])
                    if node:
                        conn.execute(
                            """
                            INSERT INTO skillflow_steps
                                (run_id, step_id, step_config_json, max_retries, status, created_at, updated_at)
                            VALUES (?, ?, ?, ?, 'pending', datetime('now'), datetime('now'))
                            """,
                            (run_id, run["current_node"], self._serialize(node.config), node.max_retries),
                        )
                        current_version = conn.execute(
                            "SELECT version FROM skillflow_steps WHERE run_id = ? AND step_id = ? AND status = 'pending'",
                            (run_id, run["current_node"]),
                        ).fetchone()
                if not current_version:
                    raise _TxRollback()

            ver = current_version["version"]
            claimed_at = time.time()
            claimed_at_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(claimed_at))

            cursor = conn.execute(
                """
                UPDATE skillflow_steps SET status = 'claimed', version = version + 1,
                    claimed_at = ?, claimed_by = ?, updated_at = datetime('now')
                WHERE run_id = ? AND step_id = ? AND version = ? AND status = 'pending'
                """,
                (claimed_at_str, "worker", run_id, run["current_node"], ver),
            )
            if cursor.rowcount == 0:
                raise _TxRollback()

            step_row = conn.execute(
                "SELECT id FROM skillflow_steps WHERE run_id = ? AND step_id = ? AND status = 'claimed'",
                (run_id, run["current_node"]),
            ).fetchone()

            completed_steps = conn.execute(
                """
                SELECT step_id, outputs_json FROM skillflow_steps
                WHERE run_id = ? AND status = 'completed'
                ORDER BY completed_at ASC
                """,
                (run_id,),
            ).fetchall()

            inputs: dict[str, dict] = {}
            for cs in completed_steps:
                inputs[cs["step_id"]] = self._deserialize(cs["outputs_json"])

            error_context = None
            validation_error = None
            feedback = None
            existing = conn.execute(
                "SELECT inputs_json FROM skillflow_steps WHERE run_id = ? AND step_id = ?",
                (run_id, run["current_node"]),
            ).fetchone()
            if existing:
                existing_inputs = self._deserialize(existing["inputs_json"])
                if "_error" in existing_inputs:
                    error_context = existing_inputs["_error"]
                if "_validation_error" in existing_inputs:
                    validation_error = existing_inputs["_validation_error"]
                if "_feedback" in existing_inputs:
                    feedback = existing_inputs["_feedback"]

            # Emit via notification bus (real-time push + durable outbox).
            # publish_sync schedules an async task; outbox write happens
            # after this _tx transaction commits, avoiding premature commit.
            self.notifications.publish_sync(
                "step_claimed",
                {
                    "run_id": run_id, "step_id": run["current_node"],
                    "step_instance_id": step_row["id"] if step_row else None,
                    "project_id": run["project_id"],
                },
                step_id=run["current_node"],
                run_id=run_id,
            )

            token = ClaimToken(
                step_id=run["current_node"], run_id=run_id,
                step_instance_id=step_row["id"] if step_row else 0,
                version=ver + 1, claimed_at=claimed_at,
            )

            # Inject resolved tool schemas if agent config is registered
            tool_schemas: dict = {}
            agent_cfg = None
            if node.agent_config and node.agent_config in self.agent_registry:
                agent_cfg = self.agent_registry.get(node.agent_config)
                if agent_cfg and agent_cfg.tool_schemas:
                    tool_schemas = agent_cfg.tool_schemas
            inputs_with_tools = dict(inputs)
            if tool_schemas:
                inputs_with_tools["_tool_schemas"] = tool_schemas
            if agent_cfg:
                inputs_with_tools["_agent_config"] = agent_cfg.to_dict()

            # Extract loop item context FIRST so context resolution can reference it
            loop_context: dict[str, str] = {}
            loop_row = conn.execute(
                "SELECT current_item, item_context_key "
                "FROM skillflow_loop_state WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if loop_row:
                item = loop_row["current_item"]
                key = loop_row["item_context_key"] or "loop_item"
                if item:
                    val = self._serialize(item) if not isinstance(item, str) else item
                    loop_context[f"[{key}]"] = val
                    loop_context[key] = val

            # Resolve context specs from the graph step node (loop vars available as $var)
            if self._workspace and node.context:
                try:
                    from skillflow.context import ContextResolver
                    config_path = self._workspace.get_project_path(
                        run["project_id"]
                    )
                    resolver = ContextResolver(config_path, self._tool_loader)
                    resolved = resolver.resolve(
                        node.context,
                        current_config=run["graph_name"],
                        loop_context=loop_context if loop_context else None,
                    )
                    if resolved:
                        inputs_with_tools["_resolved_context"] = resolved
                except Exception:
                    pass  # Context resolution is best-effort

            # Also inject loop variables directly for prompt-level access
            if loop_context:
                if "_resolved_context" not in inputs_with_tools:
                    inputs_with_tools["_resolved_context"] = {}
                for k, v in loop_context.items():
                    if k.startswith("[") and k not in inputs_with_tools["_resolved_context"]:
                        inputs_with_tools["_resolved_context"][k] = v

            # Merge dynamic write tool schemas derived from the step's output
            # contract (mode). Write-mode without fixed slots → create/edit
            # (+ write only when allow_full_write); content-mode → write_/create_/
            # edit_ per slot. Single source of truth for mutation tools.
            if node.output_mode:
                from skillflow.write_tools import generate_write_tool_schemas
                for ws in generate_write_tool_schemas(
                        node.output_mode, node.output_fixed,
                        allow_full_write=node.output_allow_full_write):
                    tool_schemas[ws["name"]] = ws

            # Merge dynamic read tool schemas from graph's context specs
            if self._workspace and node.context:
                try:
                    from skillflow.read_tools import (
                        generate_read_tool_schemas,
                        make_read_tool_fns,
                    )
                    ws_root = str(self._workspace.get_project_path(
                        run["project_id"]
                    ))
                    code_root = str(self._workspace.get_project_code_path(
                        run["project_id"]
                    )) if self._workspace else ""
                    read_schemas = generate_read_tool_schemas(
                        node.context,
                        workspace_root=ws_root,
                        current_config=run["graph_name"],
                        code_root=code_root,
                        loop_context=loop_context if loop_context else None,
                    )
                    if read_schemas and self._tool_loader:
                        # Clear stale dynamic read tools from previous task
                        # iterations (e.g. list_step_3_$current_task from an
                        # earlier claim before the $var was resolved).
                        read_fns = make_read_tool_fns(
                            node.context,
                            workspace_root=ws_root,
                            current_config=run["graph_name"],
                            code_root=code_root,
                            loop_context=loop_context if loop_context else None,
                        )
                        stale = [n for n in self._tool_loader._cache
                                 if any(n.startswith(p) for p in
                                        ("list_step_", "read_step_", "search_step_",
                                         "list_repo_", "read_repo_", "search_repo_",
                                         "list_config_", "read_config_", "search_config_",
                                         "list_workspace_", "read_workspace_", "search_workspace_"))
                                 and self._tool_loader.is_dynamic(n)]
                        for n in stale:
                            del self._tool_loader._cache[n]
                        for rs in read_schemas:
                            name = rs["name"]
                            fn = read_fns.get(name)
                            if fn:
                                tool_schemas[name] = rs
                                self._tool_loader.register_dynamic_tool(name, rs, fn)
                except Exception:
                    pass  # Read tool generation is best-effort

            inputs_with_tools["_tool_schemas"] = tool_schemas

            # Step-level max_tool_turns overrides agent config default (0 = use agent default)
            if node.max_tool_turns:
                inputs_with_tools["_max_tool_turns"] = node.max_tool_turns

            # Provide output directory + expected files
            if self._workspace:
                tmp_dir = self._workspace.get_step_tmp_dir(
                    run["project_id"], run["graph_name"], node.id
                )
                # Staging PERSISTS across retries (do not wipe). A retry inherits
                # the prior attempt's prompt (KV-cache reuse), so the agent issues
                # follow-up edits against the state it already produced — staging
                # must match that accumulated state. Wiping would both lose files
                # a prior attempt created and break follow-up edit() calls
                # (old_str reverted to the repo baseline → "not found"). A
                # successful step consumes tmp via promotion (tmp→step_dir rename),
                # so the next step still starts clean without an explicit wipe.
                tmp_dir.mkdir(parents=True, exist_ok=True)
                inputs_with_tools["_output_dir"] = str(tmp_dir)
                if node.output_fixed:
                    from skillflow.write_tools import _get_pattern
                    inputs_with_tools["_expected_files"] = [
                        _get_pattern(s, node.output_fixed) for s in node.output_fixed
                    ]

            # Preserve injected context from previous attempts.
            #
            # Reject / loop-back feedback and validation errors are produced by
            # the framework when a step is re-opened (reject_checkpoint, feedback
            # transitions, validation retries). We surface them into the resolved
            # context so the host renders them into the prompt for free, in BOTH
            # tool modes (native tool-calling and JSON-prompt tooling), without
            # any host-side special-casing. The dedicated keys are also kept for
            # hosts/runners that read them directly.
            if feedback is not None:
                inputs_with_tools["_feedback"] = feedback
                rc = inputs_with_tools.setdefault("_resolved_context", {})
                rc[_FEEDBACK_CONTEXT_LABEL] = feedback
            if validation_error is not None:
                inputs_with_tools["_validation_error"] = validation_error
                rc = inputs_with_tools.setdefault("_resolved_context", {})
                rc[_VALIDATION_ERROR_CONTEXT_LABEL] = validation_error
            if error_context is not None:
                inputs_with_tools["_error"] = error_context

            # Persist enriched inputs so DB state reflects claim-time resolution
            conn.execute(
                "UPDATE skillflow_steps SET inputs_json = ?, updated_at = datetime('now') WHERE id = ?",
                (self._serialize(inputs_with_tools), step_row["id"]),
            )

            claimed_step_id = run["current_node"]
            claimed_instance_id = token.step_instance_id

            def _trace(category: str, event: str, payload: dict | None = None,
                       _rid=token.run_id, _sid=claimed_step_id, _inst=claimed_instance_id):
                self.trace(_rid, category, event, payload,
                           step_id=_sid, step_instance_id=_inst)

            # Record the claim itself, so the trace shows step boundaries +
            # any reopen reason (reject feedback / validation error).
            _trace("step", "claimed", {
                "attempt_feedback": bool(inputs_with_tools.get("_feedback")),
                "validation_error": validation_error,
            })

            # Wire emit to notification bus so host-internal events
            # (agent_message, files_written, etc.) flow through the
            # same pub/sub channel as framework events.
            _notifications = self.notifications
            def _emit(event_type, payload,
                      _rid=token.run_id, _sid=claimed_step_id,
                      _n=_notifications):
                _n.publish_sync(event_type, payload,
                                step_id=_sid, run_id=_rid)

            return ClaimedStep(
                token=token, step_id=claimed_step_id,
                step_config=node.config,
                run_context=self._deserialize(run["context_json"]),
                inputs=inputs_with_tools,
                validation_error=validation_error,
                error_context=error_context,
                trace=_trace,
                emit=_emit,
            )

    def confirm_step(self, token: ClaimToken, result: StepResult) -> None:
        resolver = self._get_resolver_for_run(token.run_id)
        node = resolver.get_node(token.step_id)

        if node and node.output_schema and node.output_schema_retries > 0:
            from skillflow.validation import OutputValidator
            try:
                validator = OutputValidator(node.output_schema)
                validator.validate(result.outputs)
            except OutputValidationError as e:
                self._handle_validation_failure(token, str(e))
                return
            except ImportError as e:
                self._handle_validation_failure(
                    token, f"Schema import failed: {e}"
                )
                return

        # Validation specs from graph (syntax_lint, py_compile, json_schema, etc.)
        if node and node.validation:
            val_result = self._validate_outputs(token, node)
            if not val_result.get("passed", False):
                errors = val_result.get("errors", [])
                error_msg = "Validation failed:\n" + "\n".join(
                    e.get("error", str(e)) for e in errors
                )
                self._handle_validation_failure(token, error_msg)
                return

        # ── Lifecycle hooks ──────────────────────────────────────────
        if node and self._workspace:
            lifecycle = self._resolve_lifecycle(node)
            for hook_name, hook_spec in lifecycle.items():
                hook_result = self._execute_lifecycle_hook(
                    token, node, hook_name, hook_spec
                )
                # Emit warnings (non-fatal) from per-check on_failure: "warn"
                warnings = hook_result.get("warnings", [])
                if warnings:
                    warn_msg = "; ".join(
                        w.get("error", str(w)) if isinstance(w, dict) else str(w)
                        for w in warnings
                    )
                    self._emit_lifecycle_event(token, hook_name, "warned", warn_msg)

                if not hook_result.get("passed", False):
                    error = hook_result.get("error", f"Lifecycle hook '{hook_name}' failed")
                    # A tool-hook sequence bubbles the failing item's on_failure
                    # in the result; fall back to the spec-level value otherwise.
                    on_failure = hook_result.get("on_failure") or (
                        hook_spec.get("on_failure", "fail")
                        if isinstance(hook_spec, dict) else "fail")
                    if on_failure == "retry":
                        self._emit_lifecycle_event(token, hook_name, "retry", error)
                        self._handle_lifecycle_retry(token, error)
                        return
                    elif on_failure == "skip":
                        self._emit_lifecycle_event(token, hook_name, "skipped", error)
                        continue
                    elif on_failure == "warn":
                        self._emit_lifecycle_event(token, hook_name, "warned", error)
                        continue
                    else:
                        self._emit_lifecycle_event(token, hook_name, "failed", error)
                        self._handle_lifecycle_failure(token, error)
                        return
                # Success: emit the terminal event the trace was missing. Surface
                # any useful detail the hook returned (e.g. files applied count).
                detail = ""
                files = hook_result.get("files")
                if isinstance(files, list):
                    detail = f"{len(files)} file(s)"
                elif hook_result.get("committed"):
                    detail = "committed"
                self._emit_lifecycle_event(token, hook_name, "completed", detail)

        with self._tx() as conn:
            cursor = conn.execute(
                """
                UPDATE skillflow_steps
                SET status = 'completed', version = version + 1,
                    outputs_json = ?, result_flags_json = ?,
                    completed_at = datetime('now'), updated_at = datetime('now')
                WHERE id = ? AND version = ?
                """,
                (
                    self._serialize(result.outputs),
                    self._serialize(result.flags),
                    token.step_instance_id, token.version,
                ),
            )
            if cursor.rowcount == 0:
                raise StepVersionConflict(
                    f"Step '{token.step_id}' (instance {token.step_instance_id}) "
                    f"version mismatch: expected {token.version}"
                )

            # Resolve next transition inline to close the atomicity gap
            # between confirm_step and advance_run. If process dies here,
            # the run already knows its next step.
            _cycle_exceeded: str | None = None
            try:
                next_node = self._resolve_next_in_tx(
                    conn, token.run_id, token.step_id, result.flags, resolver
                )
            except CycleLimitExceeded as e:
                self._fail_run_in_tx(conn, token.run_id, f"Cycle limit exceeded: {e}")
                # Step completed but the run is now failed — still emit the
                # step_completed event so the host sees the terminal state.
                self.notifications.publish_sync(
                    "step_completed",
                    {
                        "run_id": token.run_id, "step_id": token.step_id,
                        "step_instance_id": token.step_instance_id,
                    },
                    step_id=token.step_id, run_id=token.run_id,
                )
                _cycle_exceeded = str(e)
                # Trace is deferred to after the _tx block to avoid a nested
                # commit on the same connection.
                next_node = None  # suppress UnboundLocalError below

            if _cycle_exceeded:
                pass  # run is already failed; fall through to trace below
            elif next_node:
                conn.execute(
                    "UPDATE skillflow_runs SET current_node = ?, updated_at = datetime('now') WHERE id = ?",
                    (next_node, token.run_id),
                )
            else:
                conn.execute(
                    "UPDATE skillflow_runs SET current_node = NULL, updated_at = datetime('now') WHERE id = ?",
                    (token.run_id,),
                )

            if not _cycle_exceeded:
                _proj_id = conn.execute(
                    "SELECT project_id FROM skillflow_runs WHERE id = ?",
                    (token.run_id,),
                ).fetchone()
                self.notifications.publish_sync(
                    "step_completed",
                    {
                        "run_id": token.run_id, "step_id": token.step_id,
                        "step_instance_id": token.step_instance_id,
                        "project_id": _proj_id["project_id"] if _proj_id else None,
                    },
                    step_id=token.step_id, run_id=token.run_id,
                )
        if _cycle_exceeded:
            self.trace(token.run_id, "step", "completed",
                       {"flags": result.flags, "cycle_limit_exceeded": _cycle_exceeded},
                       step_id=token.step_id, step_instance_id=token.step_instance_id)
            return
        self.trace(token.run_id, "step", "completed",
                   {"flags": result.flags, "next_node": next_node},
                   step_id=token.step_id, step_instance_id=token.step_instance_id)

    def _handle_validation_failure(self, token: ClaimToken, error: str) -> None:
        self.trace(token.run_id, "step", "validation_failed", {"error": error},
                   step_id=token.step_id, step_instance_id=token.step_instance_id)
        resolver = self._get_resolver_for_run(token.run_id)
        node = resolver.get_node(token.step_id)
        if not node:
            return
        with self._tx() as conn:
            row = conn.execute(
                "SELECT retry_count, validation_retry_count, max_retries FROM skillflow_steps WHERE id = ?",
                (token.step_instance_id,),
            ).fetchone()
            # Share retry budget between LLM retries and validation retries
            total_retries = (row["retry_count"] if row else 0) + (row["validation_retry_count"] if row else 0)
            max_allowed = row["max_retries"] if row else node.max_retries
            if row and total_retries < max_allowed:
                conn.execute(
                    """
                    UPDATE skillflow_steps
                    SET status = 'pending', version = version + 1,
                        validation_retry_count = validation_retry_count + 1,
                        inputs_json = json_set(inputs_json, '$._validation_error', ?),
                        updated_at = datetime('now')
                    WHERE id = ? AND version = ?
                    """,
                    (error, token.step_instance_id, token.version),
                )
                self.notifications.publish_sync(
                    "step_validation_failed",
                    {
                        "run_id": token.run_id, "step_id": token.step_id, "error": error,
                        "retry_count": row["retry_count"],
                        "validation_retry_count": row["validation_retry_count"] + 1,
                        "max_retries": max_allowed,
                    },
                    step_id=token.step_id, run_id=token.run_id,
                )
            else:
                # Retry budget exhausted — permanent failure
                self._fail_step_in_tx(conn, token, f"Output validation failed: {error}", retryable=False)

    def _validate_outputs(self, token: ClaimToken, node: StepNode) -> dict:
        """Run graph validation specs against draft outputs. Returns {passed, errors}."""
        if not self._workspace:
            return {"passed": True}
        pid = self._get_project_id(token.run_id)
        gname = self._get_graph_name(token.run_id)
        tmp_dir = self._workspace.get_step_tmp_dir(pid, gname, token.step_id)
        from skillflow.step_validation import StepValidator
        validator = StepValidator(self._tool_loader, tmp_dir,
                                  trace_sink=self._validation_trace_sink(token))
        return validator.validate(node.validation)

    def _validation_trace_sink(self, token: ClaimToken):
        """Pre-bound (event, payload) sink so validation/check tools land in
        the run trace under category 'tool_call' with source='validation'."""
        def sink(event: str, payload: dict):
            self.trace(token.run_id, "tool_call", event, payload,
                       step_id=token.step_id,
                       step_instance_id=token.step_instance_id)
        return sink

    # ── Lifecycle hooks ─────────────────────────────────────────────

    def _resolve_lifecycle(self, node: StepNode) -> dict:
        """Resolve lifecycle hooks with correct execution order.

        Order: after_validate → on_deliver → after_deliver.
        If after_validate is not declared but the step produces output,
        default to built-in step_commit.
        """
        declared = dict(node.lifecycle) if node.lifecycle else {}
        has_output = bool(node.output_fixed or node.output_mode)

        lifecycle: dict = {}
        if has_output:
            lifecycle["after_validate"] = declared.pop(
                "after_validate", {"tool": "step_commit"})
        if "on_deliver" in declared:
            lifecycle["on_deliver"] = declared.pop("on_deliver")
        if "after_deliver" in declared:
            lifecycle["after_deliver"] = declared.pop("after_deliver")
        lifecycle.update(declared)  # any unknown hooks
        return lifecycle

    def _execute_lifecycle_hook(self, token: ClaimToken, node: StepNode,
                                 hook_name: str, hook_spec) -> dict:
        """Execute a single lifecycle hook.

        hook_spec can be:
        - A dict with 'tool' (single tool call): used for after_validate, on_deliver
        - A list of validation specs (multi-check): used for after_deliver
        - A list of {'tool', 'params'} dicts (sequential tool hooks): used for
          on_deliver when several repo-mutating tools must run in order (e.g.
          repo_apply then repo_delete). Each runs via _execute_tool_hook, so
          $STEP_DIR is resolved and project_root injected — unlike check specs,
          which run through StepValidator and receive neither.

        Returns {passed: bool, error?: str}.
        """
        self._emit_lifecycle_event(token, hook_name, "started")

        if isinstance(hook_spec, list):
            # after_deliver runs validation checks against the project repo;
            # other slots (on_deliver) run a sequence of tool hooks with full
            # variable resolution.
            if hook_name == "after_deliver":
                return self._execute_check_hook(token, node, hook_name, hook_spec)
            return self._execute_tool_hook_sequence(token, node, hook_name, hook_spec)
        elif isinstance(hook_spec, dict) and "tool" in hook_spec:
            return self._execute_tool_hook(token, node, hook_name, hook_spec)
        else:
            return {"passed": False, "error": f"Invalid hook spec for '{hook_name}'"}

    def _execute_tool_hook_sequence(self, token: ClaimToken, node: StepNode,
                                     hook_name: str, items: list) -> dict:
        """Run a list of tool hooks in order (e.g. repo_apply then repo_delete).

        Each item is a ``{'tool', 'params', 'on_failure'?, 'max_retries'?}`` dict
        executed via :meth:`_execute_tool_hook` (full variable resolution +
        project_root injection). Per-item policy is honored IN PLACE:
        ``on_failure: retry`` re-runs THAT item up to its own ``max_retries``
        (without re-running earlier, already-succeeded items or the agent step);
        ``warn``/``skip`` log and continue to the next item; ``fail`` (or a retry
        item that exhausts its retries) stops the sequence and fails the step.
        The sequence never bubbles ``retry`` to the caller — that would reset the
        whole agent step and re-execute earlier, already-committed items.
        """
        detail_files = None
        for item in items:
            if not (isinstance(item, dict) and "tool" in item):
                return {"passed": False,
                        "error": f"Invalid tool hook in '{hook_name}': {item!r}"}
            on_failure = item.get("on_failure", "fail")
            max_retries = int(item.get("max_retries", 0) or 0)
            res = {"passed": False}
            for attempt in range(max_retries + 1):
                res = self._execute_tool_hook(token, node, hook_name, item)
                if res.get("passed", True):
                    break
                if on_failure == "retry" and attempt < max_retries:
                    # Retry THIS item in place — do not re-run earlier items.
                    self._emit_lifecycle_event(
                        token, hook_name, "retry", res.get("error", ""))
                    continue
                break
            if not res.get("passed", True):
                if on_failure in ("warn", "skip"):
                    self._emit_lifecycle_event(
                        token, hook_name,
                        "warned" if on_failure == "warn" else "skipped",
                        res.get("error", ""))
                    continue
                # 'fail', or a 'retry' item that exhausted max_retries → fail the
                # step (never bubble 'retry', which re-runs the whole sequence).
                return {**res, "on_failure": "fail"}
            files = res.get("files")
            if isinstance(files, list):
                detail_files = files
        out = {"passed": True}
        if detail_files is not None:
            out["files"] = detail_files
        return out

    def _execute_tool_hook(self, token: ClaimToken, node: StepNode,
                            hook_name: str, hook_spec: dict) -> dict:
        """Execute a tool-type lifecycle hook (single tool call)."""
        tool_name = hook_spec["tool"]
        params = dict(hook_spec.get("params", {}))

        # Resolve variables
        if self._workspace:
            row = self._conn.execute(
                "SELECT project_id, graph_name FROM skillflow_runs WHERE id = ?",
                (token.run_id,),
            ).fetchone()
            if row:
                params = self._workspace.resolve_variables(
                    row["project_id"], row["graph_name"], token.step_id, params
                )
                params.setdefault("workspace_root",
                                  str(self._workspace.get_project_path(row["project_id"])))
                params.setdefault("project_root",
                                  str(self._workspace.get_project_code_path(row["project_id"])))

        # Built-in step_commit: move tmp→step_dir atomically
        if tool_name == "step_commit":
            return self._step_commit(token)

        # Backward compat: draft_promote
        if tool_name == "draft_promote":
            return self._draft_promote(token)

        # External tool via ToolLoader
        if self._tool_loader:
            try:
                fn = self._tool_loader.load_fn(tool_name)
                params.setdefault("run_id", token.run_id)
                params.setdefault("step_id", token.step_id)
                # Filter kwargs to only what the function accepts
                import inspect as _inspect
                try:
                    sig = _inspect.signature(fn)
                    filtered = {k: v for k, v in params.items()
                               if k in sig.parameters}
                except (ValueError, TypeError):
                    filtered = params
                result = fn(**filtered)
                if isinstance(result, dict):
                    # Determine success: explicit "passed" key > no "error" key
                    # OR error is falsy.  repo_apply returns committed=False to
                    # signal "nothing to commit" (success, not failure).
                    if "passed" in result:
                        passed = result["passed"]
                    elif "error" in result and result["error"]:
                        passed = False
                    else:
                        passed = True
                    return {"passed": bool(passed), "error": result.get("error", ""),
                            **result}
                return {"passed": True}
            except Exception as e:
                return {"passed": False, "error": str(e)}

        return {"passed": False, "error": f"Tool '{tool_name}' not available"}

    def _execute_check_hook(self, token: ClaimToken, node: StepNode,
                             hook_name: str, check_specs: list[dict]) -> dict:
        """Execute a check-type lifecycle hook (list of validation specs)."""
        if not self._workspace:
            return {"passed": True}
        pid = self._get_project_id(token.run_id)
        gname = self._get_graph_name(token.run_id)

        # after_deliver checks against the project repo, not step output
        if hook_name == "after_deliver":
            check_dir = self._workspace.get_project_code_path(pid)
        else:
            check_dir = self._workspace.get_step_dir(pid, gname, token.step_id)

        from skillflow.step_validation import StepValidator
        validator = StepValidator(self._tool_loader, check_dir,
                                  trace_sink=self._validation_trace_sink(token))
        result = validator.validate(check_specs)
        # Normalize: StepValidator returns "errors" (plural list),
        # but callers expect "error" (singular string).
        if "errors" in result and "error" not in result:
            err_list = result["errors"]
            if err_list:
                parts = []
                for e in err_list:
                    if isinstance(e, dict):
                        parts.append(f"{e.get('tool','?')}: {e.get('error', str(e))}")
                    else:
                        parts.append(str(e))
                result["error"] = "; ".join(parts)
        # Preserve warnings for callers that handle on_failure: "warn"
        return result

    def _step_commit(self, token: ClaimToken) -> dict:
        """Built-in: atomic rename tmp_dir → step_dir."""
        if not self._workspace:
            return {"passed": True}
        pid = self._get_project_id(token.run_id)
        gname = self._get_graph_name(token.run_id)
        tmp_dir = self._workspace.get_step_tmp_dir(pid, gname, token.step_id)
        step_dir = self._workspace.get_step_dir(pid, gname, token.step_id)

        if not tmp_dir.exists() or not any(tmp_dir.iterdir()):
            return {"passed": True, "files": []}

        import shutil
        # Collect files before moving
        moved_files = []
        for item in sorted(tmp_dir.rglob("*")):
            if item.is_file():
                rel = item.relative_to(tmp_dir)
                moved_files.append(str(rel))

        # Atomic: remove old step dir, rename tmp → step
        if step_dir.exists():
            shutil.rmtree(str(step_dir))
        os.rename(str(tmp_dir), str(step_dir))

        return {"passed": True, "files": moved_files}

    def _draft_promote(self, token: ClaimToken) -> dict:
        """Deprecated: use _step_commit instead. Kept for backward compat."""
        # Delegate to _step_commit which uses the new .tmp → step_dir paths
        return self._step_commit(token)

    def _handle_lifecycle_retry(self, token: ClaimToken, error: str) -> None:
        """Reset step to pending so it retries with feedback injected."""
        with self._tx() as conn:
            conn.execute(
                """
                UPDATE skillflow_steps
                SET status = 'pending', version = version + 1,
                    retry_count = retry_count + 1,
                    last_error = ?, claimed_at = NULL, claimed_by = NULL,
                    inputs_json = json_set(
                        COALESCE(inputs_json, '{}'),
                        '$._feedback',
                        json(?)
                    ),
                    updated_at = datetime('now')
                WHERE id = ? AND version = ?
                """,
                (error, json.dumps({"lifecycle_error": error}),
                 token.step_instance_id, token.version),
            )
            conn.execute(
                "UPDATE skillflow_runs SET current_node = NULL, updated_at = datetime('now') WHERE id = ?",
                (token.run_id,),
            )

    def _handle_lifecycle_failure(self, token: ClaimToken, error: str) -> None:
        """Permanently fail the step due to lifecycle hook failure."""
        with self._tx() as conn:
            self._fail_step_in_tx(conn, token,
                f"Lifecycle hook failed: {error}", retryable=False)

    def _emit_lifecycle_event(self, token: ClaimToken, hook_name: str,
                               status: str, detail: str = ""):
        """Emit a lifecycle hook event to the outbox."""
        # SF-6: resolve project_id so downstream consumers don't need to
        # cross-reference the runs table.
        project_id = ""
        try:
            row = self._conn.execute(
                "SELECT project_id FROM skillflow_runs WHERE id = ?",
                (token.run_id,),
            ).fetchone()
            if row:
                project_id = row["project_id"]
        except Exception:
            pass
        payload = {
            "run_id": token.run_id,
            "step_id": token.step_id,
            "project_id": project_id,
            "hook": hook_name,
            "status": status,
        }
        if detail:
            payload["detail"] = detail
        self.notifications.publish_sync(
            "lifecycle_hook", payload,
            step_id=token.step_id, run_id=token.run_id,
        )
        # Mirror to the durable trace (outbox rows are drained + deleted).
        self.trace(token.run_id, "lifecycle", hook_name,
                   {"status": status, "detail": detail},
                   step_id=token.step_id,
                   step_instance_id=token.step_instance_id)

    def fail_step(self, token: ClaimToken, error: str, retryable: bool = True) -> None:
        with self._tx() as conn:
            self._fail_step_in_tx(conn, token, error, retryable)

    def _fail_step_in_tx(self, conn: sqlite3.Connection, token: ClaimToken,
                         error: str, retryable: bool) -> None:
        """Fail a step within an already-open transaction."""
        step_row = conn.execute(
            "SELECT retry_count, max_retries, version FROM skillflow_steps WHERE id = ?",
            (token.step_instance_id,),
        ).fetchone()
        if not step_row:
            raise _TxRollback()

        retry_count = step_row["retry_count"]
        max_retries = step_row["max_retries"]
        current_version = step_row["version"]

        if retryable and retry_count < max_retries:
            cursor = conn.execute(
                """
                UPDATE skillflow_steps
                SET status = 'pending', version = version + 1,
                    retry_count = retry_count + 1,
                    last_error = ?, claimed_at = NULL, claimed_by = NULL,
                    updated_at = datetime('now')
                WHERE id = ? AND version = ?
                """,
                (error, token.step_instance_id, current_version),
            )
            if cursor.rowcount == 0:
                raise StepVersionConflict(
                    f"Step instance {token.step_instance_id} version mismatch in fail_step"
                )
            conn.execute(
                "UPDATE skillflow_runs SET current_node = NULL, updated_at = datetime('now') WHERE id = ?",
                (token.run_id,),
            )
            self.notifications.publish_sync(
                "step_failed",
                {
                    "run_id": token.run_id, "step_id": token.step_id,
                    "step_instance_id": token.step_instance_id,
                    "error": error, "retryable": True, "retry_count": retry_count + 1,
                },
                step_id=token.step_id, run_id=token.run_id,
            )
            return

        # Retries exhausted
        resolver = self._get_resolver_for_run(token.run_id)
        error_handler = resolver.find_error_transition(token.step_id)

        if error_handler:
            conn.execute(
                """
                UPDATE skillflow_steps
                SET status = 'failed', version = version + 1,
                    last_error = ?, claimed_at = NULL, claimed_by = NULL,
                    updated_at = datetime('now')
                WHERE id = ? AND version = ?
                """,
                (error, token.step_instance_id, current_version),
            )
            error_context = {
                "_error": {
                    "source_step": token.step_id,
                    "error_type": "MaxRetriesExceeded",
                    "error_message": error,
                    "retry_count": retry_count,
                }
            }
            conn.execute(
                """
                UPDATE skillflow_steps
                SET inputs_json = ?, updated_at = datetime('now')
                WHERE run_id = ? AND step_id = ? AND status = 'pending'
                """,
                (self._serialize(error_context), token.run_id, error_handler),
            )
            # If no pending row was found (shouldn't happen since create_run
            # creates one for every step, but guard anyway), insert one.
            if conn.execute(
                "SELECT changes()"
            ).fetchone()[0] == 0:
                node = resolver.get_node(error_handler)
                conn.execute(
                    """
                    INSERT INTO skillflow_steps
                        (run_id, step_id, step_config_json, max_retries, status,
                         inputs_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'pending', ?, datetime('now'), datetime('now'))
                    """,
                    (token.run_id, error_handler,
                     self._serialize(node.config if node else {}),
                     node.max_retries if node else 3,
                     self._serialize(error_context)),
                )
            conn.execute(
                "UPDATE skillflow_runs SET current_node = ?, updated_at = datetime('now') WHERE id = ?",
                (error_handler, token.run_id),
            )
            self.notifications.publish_sync(
                "step_failed",
                {
                    "run_id": token.run_id, "step_id": token.step_id,
                    "step_instance_id": token.step_instance_id,
                    "error": error, "retryable": False, "routed_to": error_handler,
                },
                step_id=token.step_id, run_id=token.run_id,
            )
            # If the failed step had a checkpoint, emit a checkpoint-skipped event
            node = resolver.get_node(token.step_id)
            if node and node.checkpoint:
                self.notifications.publish_sync(
                    "checkpoint_skipped",
                    {
                        "run_id": token.run_id,
                        "step_id": token.step_id,
                        "step_label": node.checkpoint_label or node.name or token.step_id,
                        "error": error,
                        "routed_to": error_handler,
                    },
                    step_id=token.step_id, run_id=token.run_id,
                )
        else:
            conn.execute(
                """
                UPDATE skillflow_steps
                SET status = 'failed', version = version + 1,
                    last_error = ?, claimed_at = NULL, claimed_by = NULL,
                    updated_at = datetime('now')
                WHERE id = ? AND version = ?
                """,
                (error, token.step_instance_id, current_version),
            )
            self._fail_run_in_tx(conn, token.run_id, error)

    def _fail_step_timeout_in_tx(self, conn: sqlite3.Connection, run_id: str,
                                  step_id: str, claimed_at: str,
                                  timeout_seconds: int) -> None:
        """Fail a claimed step that exceeded its timeout_seconds.

        Called from advance_run when a step has been claimed longer than
        its configured timeout.  Marks the step as failed and emits a
        'step_timeout' outbox event so the host can notify the user.
        """
        error = (
            f"Step '{step_id}' timed out after {timeout_seconds}s "
            f"(claimed at {claimed_at})"
        )
        conn.execute(
            """UPDATE skillflow_steps
               SET status = 'failed', version = version + 1,
                   last_error = ?, claimed_at = NULL, claimed_by = NULL,
                   updated_at = datetime('now')
               WHERE run_id = ? AND step_id = ? AND status = 'claimed'""",
            (error, run_id, step_id),
        )
        conn.execute(
            "UPDATE skillflow_runs SET current_node = NULL, "
            "updated_at = datetime('now') WHERE id = ?",
            (run_id,),
        )
        self.notifications.publish_sync(
            "step_timeout",
            {
                "run_id": run_id, "step_id": step_id,
                "error": error,
                "timeout_seconds": timeout_seconds,
                "claimed_at": claimed_at,
            },
            step_id=step_id, run_id=run_id,
        )

    # ── Tool node helpers ───────────────────────────────────────────

    def _execute_tool_inline(self, tool_node: StepNode, *,
                              run_id: str = "",
                              graph_name: str = "") -> dict:
        """Execute a tool node synchronously and return the result dict.

        Auto-injects context fields so tools like ``notify`` can enrich
        messages without the agent passing them explicitly.
        """
        if self._tool_loader is None:
            raise SkillFlowError(
                f"Cannot execute tool node '{tool_node.id}': "
                "no ToolLoader configured on SkillFlow"
            )
        fn = self._tool_loader.load_fn(tool_node.tool_name)
        kwargs = dict(tool_node.tool_params)
        kwargs.setdefault("workspace_root", "")
        kwargs.setdefault("project_root", "")
        # Auto-inject context
        kwargs.setdefault("run_id", run_id)
        kwargs.setdefault("step_id", tool_node.id)
        kwargs.setdefault("config_name", graph_name)
        kwargs.setdefault("step_name", tool_node.tool_name or tool_node.agent_config or tool_node.id)
        kwargs.setdefault("step_type", tool_node.step_type)
        # Resolve $STEP_DRAFT_DIR etc. via workspace
        if self._workspace and run_id:
            try:
                row = self._conn.execute(
                    "SELECT project_id FROM skillflow_runs WHERE id = ?", (run_id,)
                ).fetchone()
                if row and row["project_id"]:
                    pid = row["project_id"]
                    kwargs.setdefault("project_id", pid)
                    # Look up current task name from loop state (for commit messages)
                    try:
                        lr = self._conn.execute(
                            "SELECT current_item FROM skillflow_loop_state WHERE run_id = ? LIMIT 1",
                            (run_id,),
                        ).fetchone()
                        if lr and lr["current_item"]:
                            kwargs.setdefault("task_name", lr["current_item"])
                    except Exception:
                        pass
                    kwargs = self._workspace.resolve_variables(
                        pid, graph_name, tool_node.id, kwargs
                    )
                    # Fill workspace_root / project_root with the project's real
                    # paths. The setdefault("") placeholders above would defeat a
                    # plain setdefault here, so assign when still empty. Mirrors the
                    # agent-tool path (execute_tool) and uses get_project_code_path
                    # so a tool step (run_tests / unity_compile / …) operates on the
                    # delivered repo (projects_base/<id>, or the linked repo for
                    # existing-repo projects) — NOT the staging workspace.
                    if not kwargs.get("workspace_root"):
                        kwargs["workspace_root"] = str(
                            self._workspace.get_project_path(pid))
                    if not kwargs.get("project_root"):
                        kwargs["project_root"] = str(
                            self._workspace.get_project_code_path(pid))
            except Exception:
                pass  # variable resolution is best-effort
        # Trace tool-type STEP nodes (e.g. repo_apply/repo_validate/notify as
        # whole steps) the same way agent-invoked tools are traced.
        param_summary = {k: (f"<{len(v)} chars>" if isinstance(v, str) and len(v) > 200 else v)
                         for k, v in kwargs.items()
                         if k not in ("run_id", "workspace_root", "project_root")}
        self.trace(run_id, "tool_call", tool_node.tool_name,
                   {"source": "tool_step", "params": param_summary},
                   step_id=tool_node.id)
        # Filter injected context kwargs to what the tool actually accepts
        # (consistent with _execute_tool_hook / execute_tool). Without this,
        # a tool-step tool that doesn't declare e.g. project_root crashes.
        import inspect as _inspect
        try:
            sig = _inspect.signature(fn)
            if not any(p.kind == _inspect.Parameter.VAR_KEYWORD
                       for p in sig.parameters.values()):
                kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
        except (ValueError, TypeError):
            pass
        result = fn(**kwargs)
        if not isinstance(result, dict):
            result = {"output": result}
        res_summary = {k: result[k] for k in ("written", "error", "applied", "files", "passed")
                       if k in result}
        if isinstance(res_summary.get("files"), list):
            res_summary["files"] = len(res_summary["files"])
        self.trace(run_id, "tool_result", tool_node.tool_name,
                   {"source": "tool_step", **(res_summary or {"keys": sorted(result.keys())})},
                   step_id=tool_node.id)
        return result

    def _confirm_tool_in_tx(self, conn, run_id: str, step_id: str,
                            result: dict) -> None:
        """Confirm a tool node execution in the database."""
        # Create step instance if not exists
        step_row = conn.execute(
            "SELECT id, version FROM skillflow_steps WHERE run_id = ? AND step_id = ? ORDER BY id DESC LIMIT 1",
            (run_id, step_id),
        ).fetchone()
        if not step_row:
            conn.execute(
                """
                INSERT INTO skillflow_steps (run_id, step_id, step_config_json, status, version,
                    inputs_json, outputs_json, result_flags_json, created_at, updated_at)
                VALUES (?, ?, '{}', 'completed', 1, '{}', ?, ?, datetime('now'), datetime('now'))
                """,
                (run_id, step_id, self._serialize(result), self._serialize(result)),
            )
        else:
            conn.execute(
                """
                UPDATE skillflow_steps
                SET status = 'completed', version = version + 1,
                    outputs_json = ?, result_flags_json = ?,
                    completed_at = datetime('now'), updated_at = datetime('now')
                WHERE id = ? AND version = ?
                """,
                (self._serialize(result), self._serialize(result),
                 step_row["id"], step_row["version"]),
            )
        self.notifications.publish_sync(
            "step_completed",
            {
                "run_id": run_id, "step_id": step_id,
                "step_instance_id": step_row["id"] if step_row else None,
            },
            step_id=step_id, run_id=run_id,
        )

    def _inject_feedback_in_tx(self, conn, run_id: str, target_step_id: str,
                               feedback: dict) -> None:
        """Inject feedback into a pending step's inputs for the target."""
        conn.execute(
            """
            UPDATE skillflow_steps
            SET inputs_json = json_set(inputs_json, '$._feedback', ?),
                updated_at = datetime('now')
            WHERE run_id = ? AND step_id = ? AND status = 'pending'
            """,
            (self._serialize(feedback), run_id, target_step_id),
        )

    # ── Graph traversal ─────────────────────────────────────────────

    def _read_loop_items(self, loop_cfg, pid, gname, run, loop_step_id):
        """Read + flatten a loop's source manifest from disk.

        Returns (items, missing): ``missing`` is True when the source file is
        unavailable (caller falls back to the cached item list), so an empty
        manifest (→ done) is distinguishable from a transiently missing file.
        """
        source = loop_cfg.source
        source_step = source.get("step", "")
        source_file = source.get("file", "")
        source_field = source.get("field", "")
        if not self._workspace:
            return [], True
        step_dir = self._workspace.get_step_dir(pid, gname, source_step)
        file_path = step_dir / source_file
        if not file_path.exists():
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                step_dir = self._workspace.get_final_dir(pid, gname, source_step)
            file_path = step_dir / source_file
        if not file_path.exists():
            self.notifications.publish_sync(
                "loop_source_missing",
                {
                    "run_id": run["id"], "loop_step_id": loop_step_id,
                    "source_step": source_step, "source_file": source_file,
                },
                run_id=run["id"],
            )
            return [], True
        try:
            import json
            data = json.loads(file_path.read_text(encoding="utf-8"))
            items = data.get(source_field, [])
            if not isinstance(items, list):
                items = []
        except Exception:
            items = []
        # Flatten if items are lists (e.g. execution_order is list of lists)
        if items and isinstance(items[0], list):
            flat: list = []
            for group in items:
                if isinstance(group, list):
                    flat.extend(group)
                else:
                    flat.append(group)
            items = flat
        return items, False

    def _loop_body_nodes(self, resolver, loop_step_id) -> set[str]:
        """The set of node ids that form a loop's body: reachable from the
        loop's body transition, following transitions, excluding the loop node
        itself. Purely topological — derived from the graph, no config-specific
        names — so it stays config-agnostic.
        """
        node = resolver.get_node(loop_step_id)
        if not node:
            return set()
        body_target = None
        for t in node.transitions:
            if t.to:
                body_target = t.to
                break
        body_nodes: set[str] = set()
        stack = [body_target]
        while stack:
            nid = stack.pop()
            if not nid or nid in body_nodes or nid == loop_step_id:
                continue
            body_nodes.add(nid)
            n = resolver.get_node(nid)
            if not n:
                continue
            for t in n.transitions:
                if t.to and t.to != loop_step_id:
                    stack.append(t.to)
        return body_nodes

    def _reset_loop_body_edge_counts(self, conn, run_id, resolver,
                                     loop_step_id, body_target):
        """Clear edge counts for the loop body so each iteration gets a fresh
        retry budget.

        Inner review/verify loops (e.g. t_impl_review→t_impl, max_loop=3) are
        counted per (run, from, to) — i.e. shared across every iteration. That
        starves later tasks of retries. Resetting the body's edge counts when a
        new item is dispatched scopes the budget to the current iteration.
        """
        body_nodes = self._loop_body_nodes(resolver, loop_step_id)
        if not body_nodes:
            return
        placeholders = ",".join("?" for _ in body_nodes)
        conn.execute(
            f"DELETE FROM skillflow_edge_counts "
            f"WHERE run_id = ? AND from_step IN ({placeholders})",
            (run_id, *sorted(body_nodes)),
        )

    def _credit_loop_current_item(self, conn, run_id: str, loop_step_id: str) -> None:
        """Progression (write): mark the loop's current_item completed.

        Called from confirm_step when a body cycle's terminal step routes back to
        the loop node — exactly once per completed body cycle, atomic with the
        terminal step's completion. _resolve_loop performs NO crediting (it only
        reads completed_items to pick the next item), so resolution is idempotent
        and the separate advance/claim transactions can't cause a spurious
        advance or skipped item.
        """
        row = conn.execute(
            "SELECT completed_items, current_item FROM skillflow_loop_state "
            "WHERE run_id = ? AND loop_step_id = ?",
            (run_id, loop_step_id),
        ).fetchone()
        if not row or not row["current_item"]:
            return
        completed: set[str] = set()
        if row["completed_items"]:
            try:
                completed = set(self._deserialize(row["completed_items"]))
            except Exception:
                pass
        if row["current_item"] in completed:
            return  # idempotent — already credited
        completed.add(row["current_item"])
        conn.execute(
            "UPDATE skillflow_loop_state SET completed_items = ?, "
            "updated_at = datetime('now') WHERE run_id = ? AND loop_step_id = ?",
            (self._serialize(sorted(completed)), run_id, loop_step_id),
        )

    def _resolve_loop(self, conn, run: dict, resolver, loop_step_id: str) -> str | None:
        """Resolve a loop step to either its body or done transition.

        Tracks completed items as a SET (not a numeric index), so PM can
        add, remove, reorder, or replace items in the manifest between
        goal-loop retries and the loop picks up whatever isn't done yet.

        State columns:
          - completed_items (JSON array of strings): task names already dispatched
          - items_json (JSON array): cached manifest, always kept in sync with the
            live manifest so context resolution finds the right item.

        First-uncompleted-item order follows the manifest's list-of-lists
        structure (groups sequential, items within groups parallel).
        """
        node = resolver.get_node(loop_step_id)
        if not node or not node.loop:
            return None

        loop_cfg = node.loop
        pid = run["project_id"]
        gname = run["graph_name"]

        # Identify body vs done transitions.
        body_target: str | None = None
        for t in node.transitions:
            if t.to:
                body_target = t.to
                break

        # ── Read the source manifest on EVERY resolve (dynamic) ──────────
        items, missing = self._read_loop_items(
            loop_cfg, pid, gname, run, loop_step_id
        )
        if not self._workspace:
            return None

        row = conn.execute(
            "SELECT items_json, completed_items, current_item FROM skillflow_loop_state "
            "WHERE run_id = ? AND loop_step_id = ?",
            (run["id"], loop_step_id),
        ).fetchone()

        if missing and row:
            items = self._deserialize(row["items_json"]) if row["items_json"] else []

        def _route_done():
            conn.execute(
                "UPDATE skillflow_steps SET status = 'completed', "
                "completed_at = datetime('now'), updated_at = datetime('now') "
                "WHERE run_id = ? AND step_id = ? AND status = 'pending'",
                (run["id"], loop_step_id),
            )
            for t in node.transitions:
                if t.to and t.to != body_target:
                    return t.to
            return None

        # ── Completed set — scoped to the LIVE manifest ───────────────────
        # Resolution is READ-ONLY w.r.t. progression: it never credits
        # completion (confirm_step does that via _credit_loop_current_item when a
        # body cycle returns to the loop). So re-entry — e.g. an extra scheduler
        # tick in the dispatch→claim gap — re-picks the SAME item with
        # current_item unchanged, never spuriously advancing or skipping work.
        completed: set[str] = set()
        current_item: str | None = None
        if row:
            if row["completed_items"]:
                try:
                    completed = set(self._deserialize(row["completed_items"]))
                except Exception:
                    pass
            current_item = row["current_item"] or None
        # Drop superseded names from prior goal-loop rounds so completed_items
        # reflects only the active manifest (prevents the len(completed) overcount
        # and the scheduler's idx-out-of-range).
        completed &= set(items)

        if row is None:
            if not items:
                return _route_done()
            conn.execute(
                "INSERT INTO skillflow_loop_state (run_id, loop_step_id, "
                "items_json, completed_items, current_item, item_context_key, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, '[]', NULL, ?, datetime('now'), datetime('now'))",
                (run["id"], loop_step_id, self._serialize(items),
                 loop_cfg.item_as or "loop_item"),
            )

        # ── Find first uncompleted item (manifest order) ────────────────
        if not items:
            return _route_done()
        next_item: str | None = None
        for item in items:
            if item not in completed:
                next_item = item
                break
        if next_item is None:
            return _route_done()

        # ── Dispatch (idempotent) ─────────────────────────────────────────
        # Persist the live manifest + scoped completed set + the item to run.
        new_dispatch = (next_item != current_item)
        conn.execute(
            "UPDATE skillflow_loop_state SET items_json = ?, completed_items = ?, "
            "current_item = ?, updated_at = datetime('now') "
            "WHERE run_id = ? AND loop_step_id = ?",
            (self._serialize(items), self._serialize(sorted(completed)),
             next_item, run["id"], loop_step_id),
        )
        # Reset the body's per-iteration retry budget ONLY for a genuinely new
        # item — never on an idempotent re-entry for the same in-flight item,
        # which would wipe the body's mid-cycle edge counts.
        if new_dispatch:
            self._reset_loop_body_edge_counts(
                conn, run["id"], resolver, loop_step_id, body_target
            )
        return body_target

    def _resolve_next_in_tx(self, conn, run_id: str, step_id: str,
                            flags: dict, resolver) -> str | None:
        """Resolve the immediate next step from transitions, within a transaction.

        Returns the next node ID, or None to let advance_run handle the full
        resolution (checkpoints, gates, loops, max_loop tracking).

        Only resolves simple agent→agent transitions:
        - No checkpoint steps (need user approval)
        - No gate or loop targets (need edge count / iteration tracking)
        - No checkpoint-guarded transitions

        Increments edge counts atomically and enforces max_loop so that
        review→parent loop-back counts are tracked correctly even when
        advance_run later takes the pre-resolved fast path.

        Raises CycleLimitExceeded when all matching transitions are exhausted
        by max_loop (caller must fail the run).
        """
        node = resolver.get_node(step_id)
        if not node or not node.transitions:
            return None

        if node.checkpoint:
            return None

        run = conn.execute(
            "SELECT project_id, graph_name FROM skillflow_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        fr = self._make_file_reader(
            run["project_id"], run["graph_name"], step_id
        ) if run else None

        # Read current edge counts to enforce max_loop.  We also increment
        # the count inline for the chosen edge so that subsequent calls
        # (including the next _resolve_next_in_tx) see the updated count.
        edge_counts = self._read_edge_counts(conn, run_id)

        from skillflow.graph import _flags_match
        exhausted_reasons: list[str] = []
        for t in node.transitions:
            if t.match is not None:
                if t.match.get("from") == "checkpoint":
                    continue
                if not _flags_match(t.match, flags, file_reader=fr):
                    continue
            # Don't resolve to gates, loops, native tools, or terminal
            # transitions (None) — advance_run handles them.
            if t.to is None:
                return None
            skip_tool = False
            if resolver.is_tool(t.to):
                tool_node = resolver.get_node(t.to)
                if tool_node and not self._should_delegate_tool(tool_node.tool_name):
                    skip_tool = True
            if resolver.is_loop(t.to):
                # PROGRESSION: credit the loop's current_item — but ONLY when
                # THIS completing step is inside the loop body (a body cycle
                # returning). An EXTERNAL step transitioning INTO the loop
                # (entry / goal-loop re-entry) must NOT credit: that would mark
                # the in-flight item complete and skip it. Topological check
                # (no config-specific names). Fires once per body cycle; a stray
                # re-tick at the loop node goes through advance_run only (no
                # credit), so resolution stays idempotent.
                if step_id in self._loop_body_nodes(resolver, t.to):
                    self._credit_loop_current_item(conn, run_id, t.to)
                return None
            if resolver.is_gate(t.to) or skip_tool:
                return None

            # Check max_loop on this edge
            if t.max_loop is not None:
                key = (step_id, t.to)
                if edge_counts.get(key, 0) >= t.max_loop:
                    exhausted_reasons.append(
                        f"'{step_id}' -> '{t.to}' (max_loop={t.max_loop} reached)"
                    )
                    continue

            # SF-22: Atomically increment the edge count so subsequent calls
            # see it. This is the ONLY increment for THIS transition — when
            # we return a target, advance_run takes the fast-path at the
            # pre-resolved current_node check and does NOT walk edges_taken,
            # so there is no double-count. When we return None (gates, loops,
            # checkpoints), the edge is NOT counted here — advance_run will
            # count it via edges_taken later.
            conn.execute(
                """
                INSERT INTO skillflow_edge_counts (run_id, from_step, to_step, count, max_loop)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(run_id, from_step, to_step)
                DO UPDATE SET count = count + 1
                """,
                (run_id, step_id, t.to, t.max_loop),
            )
            return t.to

        if exhausted_reasons:
            raise CycleLimitExceeded(
                f"All transitions from '{step_id}' are exhausted: "
                + "; ".join(exhausted_reasons)
            )
        return None

    def _make_file_reader(self, project_id: str, graph_name: str,
                          step_id: str) -> callable | None:
        """Return a callable for resolving from_file match conditions.

        Reads from the step's promoted output directory ({step_id}/)
        where _step_commit has atomically moved validated outputs.
        """
        if not self._workspace:
            return None
        step_dir = self._workspace.get_step_dir(project_id, graph_name, step_id)
        def read(path: str) -> str:
            f = step_dir / path
            if not f.exists():
                raise FileNotFoundError(f"Output file not found: {path}")
            return f.read_text(encoding="utf-8")
        return read

    def _complete_tool_step(self, run_id: str, step_id: str,
                            tool_result: dict, run_row: dict,
                            resolver) -> str | None:
        """Confirm an inline tool execution and resolve its transition.

        Called AFTER _execute_tool_inline returns, inside a fresh _tx()
        so the write lock is only held during the fast DB update, not
        during the (potentially slow) tool itself.
        """
        with self._tx() as conn:
            self._confirm_tool_in_tx(conn, run_id, step_id, tool_result)
            step_flags = tool_result
            fr = self._make_file_reader(
                run_row["project_id"], run_row["graph_name"], step_id)
            edge_counts = self._read_edge_counts(conn, run_id)
            try:
                _t, target = resolver.resolve_transition(
                    step_id, step_flags, edge_counts, file_reader=fr)
            except CycleLimitExceeded:
                self._fail_run_in_tx(conn, run_id, "Cycle limit exceeded")
                return None
            if _t and _t.feedback and _t.to:
                error_str = tool_result.get("error", "Tool failed")
                self._inject_feedback_in_tx(conn, run_id, _t.to, error_str)
            if target:
                # Count this traversal so max_loop is enforced on a TOOL step's
                # OUTGOING edge, exactly as advance_run's main path does for
                # agent-originated edges. Without it a tool step that loops back
                # (e.g. a run_tests gate → implementer) would never trip
                # max_loop → the loop runs unbounded. Once the count reaches
                # max_loop, resolve_transition above raises CycleLimitExceeded
                # (caught → run fails) on the next pass.
                conn.execute(
                    """
                    INSERT INTO skillflow_edge_counts (run_id, from_step, to_step, count, max_loop)
                    VALUES (?, ?, ?, 1, NULL)
                    ON CONFLICT(run_id, from_step, to_step)
                    DO UPDATE SET count = count + 1
                    """,
                    (run_id, step_id, target),
                )
                ec = resolver.graph.end_conditions
                if ec and ec.conditions:
                    end_result = self._evaluate_end_conditions(
                        conn, run_id, ec, target)
                    if end_result:
                        if end_result.status == "completed":
                            self._complete_run_in_tx(
                                conn, run_id, end_result.reason)
                        else:
                            self._fail_run_in_tx(
                                conn, run_id, end_result.reason)
                        return None
                conn.execute(
                    "UPDATE skillflow_runs SET current_node = ?,"
                    " updated_at = datetime('now') WHERE id = ?",
                    (target, run_id),
                )
                return target
            # No target — check end_conditions against the current node
            ec = resolver.graph.end_conditions
            if ec and ec.conditions:
                end_result = self._evaluate_end_conditions(
                    conn, run_id, ec, step_id)
                if end_result:
                    if end_result.status == "completed":
                        self._complete_run_in_tx(
                            conn, run_id, end_result.reason)
                    else:
                        self._fail_run_in_tx(
                            conn, run_id, end_result.reason)
                    return None
            self._fail_run_in_tx(
                conn, run_id,
                f"No matching transition from '{step_id}'"
                f" with flags {step_flags}"
            )
            return None

    def _claim_tool_step_in_tx(self, run_id: str, step_id: str, node) -> int | None:
        """Atomically claim a tool step (pending→claimed) for inline execution.

        Returns the claimed step-instance id, or None if the step is not
        claimable — i.e. a concurrent advance_run() already claimed it. Mirrors
        claim_next_step's CAS so the in-flight guard + runaway valve (which key
        on the 'claimed' status/trace) see inline tool steps too, making
        execution idempotent under concurrent drivers sharing this DB.
        """
        with self._tx() as conn:
            row = conn.execute(
                "SELECT version FROM skillflow_steps "
                "WHERE run_id = ? AND step_id = ? AND status = 'pending'",
                (run_id, step_id),
            ).fetchone()
            if not row:
                existing = conn.execute(
                    "SELECT status FROM skillflow_steps "
                    "WHERE run_id = ? AND step_id = ? ORDER BY id DESC LIMIT 1",
                    (run_id, step_id),
                ).fetchone()
                if existing and existing["status"] == "claimed":
                    return None  # a concurrent driver owns it
                # First run with no row, or cyclic re-entry (prev completed/
                # failed): open a fresh pending instance to claim.
                conn.execute(
                    "INSERT INTO skillflow_steps (run_id, step_id, "
                    "step_config_json, max_retries, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 'pending', datetime('now'), datetime('now'))",
                    (run_id, step_id, self._serialize(node.config), node.max_retries),
                )
                row = conn.execute(
                    "SELECT version FROM skillflow_steps "
                    "WHERE run_id = ? AND step_id = ? AND status = 'pending'",
                    (run_id, step_id),
                ).fetchone()
                if not row:
                    return None
            ver = row["version"]
            claimed_at_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time()))
            cur = conn.execute(
                "UPDATE skillflow_steps SET status = 'claimed', version = version + 1, "
                "claimed_at = ?, claimed_by = ?, updated_at = datetime('now') "
                "WHERE run_id = ? AND step_id = ? AND version = ? AND status = 'pending'",
                (claimed_at_str, "tool-inline", run_id, step_id, ver),
            )
            if cur.rowcount == 0:
                return None  # lost the race
            inst = conn.execute(
                "SELECT id FROM skillflow_steps "
                "WHERE run_id = ? AND step_id = ? AND status = 'claimed' "
                "ORDER BY id DESC LIMIT 1",
                (run_id, step_id),
            ).fetchone()
            return inst["id"] if inst else None

    def _reopen_tool_step_in_tx(self, run_id: str, step_id: str) -> None:
        """Release a claimed tool step back to pending after a CRASHED execution,
        so it retries promptly instead of stalling until its claim times out.

        Capped at 3 crashes: a tool that crashes deterministically is broken,
        not transient. On the 3rd crash the RUN is failed (not just the step) —
        marking only the step failed + current_node=NULL would let advance_run
        re-resolve back to the tool and open a FRESH instance with a reset
        counter, so the crash loop never actually stopped (only the host's
        step-count valve caught it). The SF-20 stale-recovery cap does NOT cover
        this path (a crash reopens directly, never via recover_stale_claims).
        """
        with self._tx() as conn:
            row = conn.execute(
                "SELECT id, inputs_json FROM skillflow_steps "
                "WHERE run_id = ? AND step_id = ? AND status = 'claimed' "
                "ORDER BY id DESC LIMIT 1",
                (run_id, step_id),
            ).fetchone()
            if not row:
                return
            inputs = self._deserialize(row["inputs_json"])
            reopen_count = inputs.get("_tool_reopen_count", 0) + 1
            if reopen_count >= 3:
                error_msg = (
                    f"Tool step '{step_id}' crashed {reopen_count} times — "
                    f"failing (likely a bug in the tool, not a transient error)."
                )
                conn.execute(
                    "UPDATE skillflow_steps SET status = 'failed', "
                    "version = version + 1, last_error = ?, claimed_at = NULL, "
                    "claimed_by = NULL, updated_at = datetime('now') WHERE id = ?",
                    (error_msg, row["id"]),
                )
                # Fail the RUN so advance_run returns None instead of re-resolving
                # the predecessor's transition and opening a fresh tool instance.
                self._fail_run_in_tx(conn, run_id, error_msg)
                self.notifications.publish_sync(
                    "step_failed",
                    {"run_id": run_id, "step_id": step_id,
                     "error": error_msg, "retryable": False},
                    step_id=step_id, run_id=run_id,
                )
                return
            inputs["_tool_reopen_count"] = reopen_count
            conn.execute(
                "UPDATE skillflow_steps SET status = 'pending', claimed_at = NULL, "
                "claimed_by = NULL, version = version + 1, inputs_json = ?, "
                "updated_at = datetime('now') "
                "WHERE id = ? AND status = 'claimed'",
                (self._serialize(inputs), row["id"]),
            )

    def advance_run(self, run_id: str) -> str | None:
        # Recover stale claims before any traversal
        self.recover_stale_claims(self._stale_threshold)

        resolver = self._get_resolver_for_run(run_id)

        # ── Tool fast-path: execute OUTSIDE any write transaction ──
        # Long-running tools (e.g. run_tests) must not hold the SQLite write
        # lock — that blocks agent trace writes and other scheduler ticks,
        # causing SQLITE_BUSY → crashed agents → stale claims → infinite loops.
        #
        # CLAIM-GUARDED (1.3.2): a CAS pending→claimed precedes execution, so
        # only one advance_run() call runs the tool. This is REQUIRED for
        # correctness when more than one driver advances the same run — e.g. a
        # host CLI + a Docker container sharing this DB, or the wake-on-confirm
        # and interval jobs overlapping. Without it, an unclaimed slow tool gets
        # re-launched by every tick → dozens of concurrent run_tests pile up and
        # mutually starve (the step-5 rampage). Losers return None and back off;
        # a crashed tool reopens to pending; a dead driver's claim is reclaimed
        # via recover_stale_claims once the node's timeout_seconds elapses.
        run_row = self._conn.execute(
            "SELECT * FROM skillflow_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if (run_row and run_row["status"] == "running"
                and run_row["current_node"]):
            current = run_row["current_node"]
            if resolver.is_tool(current):
                tool_node = resolver.get_node(current)
                if tool_node and not self._should_delegate_tool(
                        tool_node.tool_name):
                    inst_id = self._claim_tool_step_in_tx(
                        run_id, current, tool_node)
                    if inst_id is None:
                        return None  # another driver owns this tool step
                    self.trace(run_id, "step", "claimed",
                               {"tool": tool_node.tool_name, "inline": True},
                               step_id=current, step_instance_id=inst_id)
                    try:
                        # Execute tool WITHOUT holding any lock/transaction
                        tool_result = self._execute_tool_inline(
                            tool_node, run_id=run_id,
                            graph_name=run_row["graph_name"])
                    except Exception:
                        # Don't leave a crashed tool wedged in 'claimed'.
                        self._reopen_tool_step_in_tx(run_id, current)
                        raise
                    return self._complete_tool_step(
                        run_id, current, tool_result, run_row, resolver)

        # ── Full resolution (gate, loop, agent, or current_node=None) ──
        with self._tx() as conn:
            run = conn.execute(
                "SELECT * FROM skillflow_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if not run:
                return None
            if run["status"] in ("completed", "failed", "paused"):
                return None

            if run["current_node"]:
                claimed_row = conn.execute(
                    "SELECT step_id, claimed_at FROM skillflow_steps "
                    "WHERE run_id = ? AND status = 'claimed' LIMIT 1",
                    (run_id,),
                ).fetchone()
                if claimed_row:
                    # Fix 1.4: if the claimed step is different from
                    # current_node, block (alien claim). If it IS
                    # current_node, check timeout before blocking.
                    if claimed_row["step_id"] != run["current_node"]:
                        return None
                    # Same step — check timeout
                    node = resolver.get_node(claimed_row["step_id"])
                    if node and node.timeout_seconds > 0:
                        threshold = time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ",
                            time.gmtime(time.time() - node.timeout_seconds),
                        )
                        if claimed_row["claimed_at"] < threshold:
                            self._fail_step_timeout_in_tx(
                                conn, run_id, claimed_row["step_id"],
                                claimed_row["claimed_at"], node.timeout_seconds,
                            )
                            # Step timed out and was failed — don't return
                            # None; continue below to re-resolve current_node
                            # (it may route through error transition).
                        else:
                            return None  # within timeout, wait
                    else:
                        return None  # no timeout configured, wait indefinitely
                # If current_node is a loop step, resolve its iteration
                current = run["current_node"]
                if resolver.is_loop(current):
                    current = self._resolve_loop(conn, run, resolver, current)
                    if current is None:
                        return None
                    conn.execute(
                        "UPDATE skillflow_runs SET current_node = ?, updated_at = datetime('now') WHERE id = ?",
                        (current, run_id),
                    )
                    return current

                # If current_node is a gate, resolve through it
                if resolver.is_gate(current):
                    gate_depth = 0
                    edge_counts = self._read_edge_counts(conn, run_id)
                    # Merge flags from all completed steps for gate resolution
                    all_rows = conn.execute(
                        "SELECT result_flags_json FROM skillflow_steps "
                        "WHERE run_id = ? AND status = 'completed'",
                        (run_id,),
                    ).fetchall()
                    flags: dict = {}
                    for row in all_rows:
                        flags.update(self._deserialize(row["result_flags_json"]))
                    while resolver.is_gate(current) and gate_depth < 1000:
                        gate_depth += 1
                        # SF-23 (gate pre-resolved): Use resolve_transition so
                        # we can distinguish "no match" from "terminal (to: null)".
                        try:
                            gt, gtarget = resolver.resolve_transition(
                                current, flags, edge_counts)
                        except CycleLimitExceeded:
                            self._fail_run_in_tx(conn, run_id,
                                                 f"Gate '{current}': cycle limit exceeded")
                            return None
                        if gt is None:
                            self._fail_run_in_tx(conn, run_id,
                                                 f"Gate '{current}': no matching transition")
                            return None
                        if gtarget is None:
                            # Terminal transition (to: null) — pipeline ends
                            ec_gate = resolver.graph.end_conditions
                            if ec_gate and ec_gate.conditions:
                                end_result = self._evaluate_end_conditions(
                                    conn, run_id, ec_gate, current)
                                if end_result:
                                    if end_result.status == "completed":
                                        self._complete_run_in_tx(conn, run_id, end_result.reason)
                                    else:
                                        self._fail_run_in_tx(conn, run_id, end_result.reason)
                                    return None
                            self._complete_run_in_tx(
                                conn, run_id,
                                f"Pipeline completed at gate '{current}'")
                            return None
                        # Count this gate traversal so max_loop is enforced on a
                        # gate's outgoing edge in the pre-resolved path too (a
                        # gate reached FROM a tool step resolves here, not in the
                        # main path). Update the in-memory dict as well so a gate
                        # chain that loops within this single pass sees its own
                        # increments.
                        conn.execute(
                            """
                            INSERT INTO skillflow_edge_counts (run_id, from_step, to_step, count, max_loop)
                            VALUES (?, ?, ?, 1, NULL)
                            ON CONFLICT(run_id, from_step, to_step)
                            DO UPDATE SET count = count + 1
                            """,
                            (run_id, current, gtarget),
                        )
                        edge_counts[(current, gtarget)] = \
                            edge_counts.get((current, gtarget), 0) + 1
                        current = gtarget
                    if gate_depth >= 1000:
                        self._fail_run_in_tx(conn, run_id, "Gate resolution exceeded 1000 iterations")
                        return None
                    conn.execute(
                        "UPDATE skillflow_runs SET current_node = ?, updated_at = datetime('now') WHERE id = ?",
                        (current, run_id),
                    )
                    return current

                # If current_node is a tool, hand it to the top fast-path,
                # which executes it OUTSIDE _tx() on the next advance_run pass.
                # Executing it inline here would hold self._lock for the tool's
                # whole duration, blocking concurrent ticks (→ SQLITE_BUSY /
                # stale-claim re-spawn loop, the step-5 run_tests bug).
                # current_node is already this tool, so no DB update is needed;
                # returning None lets the caller re-enter and the top fast-path
                # run it lock-free.
                if resolver.is_tool(current):
                    tool_node = resolver.get_node(current)
                    if tool_node and self._should_delegate_tool(tool_node.tool_name):
                        return current  # agent claims and executes the tool
                    return None

                # Check end conditions when current_node was pre-resolved
                # (e.g., by confirm_step inline transition resolution).
                # SF-24: _resolve_next_in_tx (called by confirm_step) already
                # returns None for checkpoint steps, so a pre-resolved step can
                # never be a checkpoint. If future code lifts that guard, add a
                # safety check here before accepting the pre-resolved node.
                ec = resolver.graph.end_conditions
                if ec and ec.conditions:
                    end_result = self._evaluate_end_conditions(
                        conn, run_id, ec, run["current_node"]
                    )
                    if end_result:
                        if end_result.status == "completed":
                            self._complete_run_in_tx(conn, run_id, end_result.reason)
                        else:
                            self._fail_run_in_tx(conn, run_id, end_result.reason)
                        return None
                return run["current_node"]

            claimed_row = conn.execute(
                "SELECT step_id, claimed_at FROM skillflow_steps "
                "WHERE run_id = ? AND status = 'claimed' LIMIT 1",
                (run_id,),
            ).fetchone()
            if claimed_row:
                node = resolver.get_node(claimed_row["step_id"])
                if node and node.timeout_seconds > 0:
                    threshold = time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ",
                        time.gmtime(time.time() - node.timeout_seconds),
                    )
                    if claimed_row["claimed_at"] < threshold:
                        self._fail_step_timeout_in_tx(
                            conn, run_id, claimed_row["step_id"],
                            claimed_row["claimed_at"], node.timeout_seconds,
                        )
                        # Fall through — continue resolving
                    else:
                        return None  # within timeout, wait
                else:
                    return None  # no timeout, wait indefinitely

            last = conn.execute(
                """
                SELECT step_id, result_flags_json FROM skillflow_steps
                WHERE run_id = ? AND status = 'completed'
                ORDER BY id DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()

            edges_taken: list[tuple[str, str]] = []
            fr = self._make_file_reader(
                run["project_id"], run["graph_name"],
                last["step_id"] if last else "")
            if last is None:
                next_node = resolver.begin_node()
            else:
                flags = self._deserialize(last["result_flags_json"])
                edge_counts = self._read_edge_counts(conn, run_id)
                try:
                    matched_t, first_target = resolver.resolve_transition(
                        last["step_id"], flags, edge_counts, file_reader=fr
                    )
                except CycleLimitExceeded:
                    self._fail_run_in_tx(conn, run_id, "Cycle limit exceeded")
                    return None
                if first_target is None:
                    last_node = resolver.get_node(last["step_id"])
                    # SF-23: A transition with to:null (terminal) matched —
                    # this means the pipeline should end. Check end_conditions
                    # and complete the run instead of failing.
                    if matched_t is not None and matched_t.to is None:
                        ec = resolver.graph.end_conditions
                        if ec and ec.conditions:
                            end_result = self._evaluate_end_conditions(
                                conn, run_id, ec, last["step_id"]
                            )
                            if end_result:
                                if end_result.status == "completed":
                                    self._complete_run_in_tx(conn, run_id, end_result.reason)
                                else:
                                    self._fail_run_in_tx(conn, run_id, end_result.reason)
                                return None
                        # Terminal transition with no end_conditions or
                        # no matching condition — complete as success.
                        self._complete_run_in_tx(
                            conn, run_id,
                            f"Pipeline completed at '{last['step_id']}'"
                        )
                        return None
                    # Check if this is a checkpoint step whose transition requires
                    # checkpoint approval. If so, pause instead of failing.
                    if last_node and last_node.checkpoint:
                        # Find the first checkpoint-guarded transition as the pending target
                        for t in last_node.transitions:
                            if t.match and t.match.get("from") == "checkpoint":
                                first_target = t.to
                                break
                    if first_target is None:
                        self._fail_run_in_tx(
                            conn, run_id,
                            f"No matching transition from '{last['step_id']}' with flags {flags}"
                        )
                        return None
                    # Fall through — first_target set from checkpoint transition
                edges_taken.append((last["step_id"], first_target))
                next_node = first_target

            # Checkpoint — pause BEFORE auto-advancing through gates/tools
            if last:
                last_node = resolver.get_node(last["step_id"])
                if last_node and last_node.checkpoint:
                    conn.execute(
                        "UPDATE skillflow_runs SET current_node = ?, updated_at = datetime('now') WHERE id = ?",
                        (next_node, run_id),
                    )
                    conn.execute(
                        "UPDATE skillflow_runs SET status = 'paused', updated_at = datetime('now') WHERE id = ?",
                        (run_id,),
                    )
                    # Emit checkpoint_paused via notification bus so
                    # TUI/SSE consumers see it without polling.
                    _chk_label = last_node.checkpoint_label or last_node.name or last["step_id"]
                    self.notifications.publish_sync(
                        "checkpoint_paused",
                        {
                            "step_id": last["step_id"],
                            "label": _chk_label,
                            "next_node": next_node,
                            "project_id": run["project_id"],
                            "graph_name": run["graph_name"],
                        },
                        step_id=last["step_id"], run_id=run_id,
                    )
                    # SF-3: record checkpoint pause in durable trace.
                    self.trace(run_id, "step", "checkpoint_paused", {
                        "step_id": last["step_id"],
                        "label": _chk_label,
                        "next_node": next_node,
                    }, step_id=last["step_id"])
                    return None

            # Auto-advance through gates AND auto-execute tool nodes
            # Merge flags from ALL completed steps so gates see flags
            # produced by earlier steps (e.g. task_gate needs step 3's
            # has_tasks, even though the last step is a _review step).
            all_completed = conn.execute(
                "SELECT step_id, result_flags_json FROM skillflow_steps "
                "WHERE run_id = ? AND status = 'completed'",
                (run_id,),
            ).fetchall()
            last_flags_for_gate: dict = {}
            for cs in all_completed:
                last_flags_for_gate.update(
                    self._deserialize(cs["result_flags_json"]))
            gate_depth = 0
            defer_tool = False  # set when we stop at a native tool to run it
                                # lock-free via the top fast-path next pass
            while gate_depth < 1000:
                if resolver.is_gate(next_node):
                    gate_depth += 1
                    edge_counts = self._read_edge_counts(conn, run_id)
                    # SF-23 (gate): Use resolve_transition directly so we can
                    # distinguish "no match" from "matched to terminal (to: null)".
                    # resolve_gate_transitions → next_node returns the target or
                    # None — but None is also the valid terminal sentinel.
                    try:
                        gt, gtarget = resolver.resolve_transition(
                            next_node, last_flags_for_gate, edge_counts,
                            file_reader=fr)
                    except CycleLimitExceeded:
                        self._fail_run_in_tx(conn, run_id,
                                             f"Gate '{next_node}': cycle limit exceeded")
                        return None
                    if gt is None:
                        self._fail_run_in_tx(conn, run_id,
                                             f"Gate '{next_node}': no matching transition")
                        return None
                    if gtarget is None:
                        # Terminal transition (to: null) — gate matched, pipeline ends
                        ec_gate = resolver.graph.end_conditions
                        if ec_gate and ec_gate.conditions:
                            end_result = self._evaluate_end_conditions(
                                conn, run_id, ec_gate, next_node)
                            if end_result:
                                if end_result.status == "completed":
                                    self._complete_run_in_tx(conn, run_id, end_result.reason)
                                else:
                                    self._fail_run_in_tx(conn, run_id, end_result.reason)
                                return None
                        self._complete_run_in_tx(
                            conn, run_id,
                            f"Pipeline completed at gate '{next_node}'")
                        return None
                    edges_taken.append((next_node, gtarget))
                    next_node = gtarget
                elif resolver.is_tool(next_node):
                    tool_node = resolver.get_node(next_node)
                    if tool_node and self._should_delegate_tool(tool_node.tool_name):
                        break  # return the tool node for the agent
                    # Native tool: do NOT execute inline. _execute_tool_inline
                    # holds self._lock for the tool's whole duration, blocking
                    # concurrent ticks (→ SQLITE_BUSY / stale-claim re-spawn
                    # loop, the step-5 run_tests bug). Stop here, commit
                    # current_node = this tool below, and let the top fast-path
                    # run it OUTSIDE _tx() on the next advance_run pass.
                    defer_tool = True
                    break
                elif resolver.is_loop(next_node):
                    resolved = self._resolve_loop(conn, run, resolver, next_node)
                    if resolved is None:
                        self._fail_run_in_tx(conn, run_id, f"Loop '{next_node}': failed to resolve")
                        return None
                    edges_taken.append((next_node, resolved))
                    next_node = resolved
                else:
                    break  # Agent node — needs external runner

            if gate_depth >= 1000:
                self._fail_run_in_tx(conn, run_id, "Gate/tool resolution exceeded 1000 iterations")
                return None

            # Increment edge counts for all traversed transitions
            for from_step, to_step in edges_taken:
                conn.execute(
                    """
                    INSERT INTO skillflow_edge_counts (run_id, from_step, to_step, count, max_loop)
                    VALUES (?, ?, ?, 1, NULL)
                    ON CONFLICT(run_id, from_step, to_step)
                    DO UPDATE SET count = count + 1
                    """,
                    (run_id, from_step, to_step),
                )

            # End conditions
            ec = resolver.graph.end_conditions
            if ec and ec.conditions:
                end_result = self._evaluate_end_conditions(conn, run_id, ec, next_node)
                if end_result:
                    if end_result.status == "completed":
                        self._complete_run_in_tx(conn, run_id, end_result.reason)
                    else:
                        self._fail_run_in_tx(conn, run_id, end_result.reason)
                    return None

            conn.execute(
                "UPDATE skillflow_runs SET current_node = ?, updated_at = datetime('now') WHERE id = ?",
                (next_node, run_id),
            )
            # When we stopped at a native tool, current_node now points at it;
            # return None so the caller re-enters and the top fast-path executes
            # it OUTSIDE _tx() (lock-free). Returning the tool node would make
            # the host try to claim it as an agent step.
            if defer_tool:
                return None
            return next_node

    def reject_checkpoint(self, run_id: str, step_id: str, feedback: str,
                          redirect_to: str = "") -> None:
        with self._tx() as conn:
            run = conn.execute(
                "SELECT * FROM skillflow_runs WHERE id = ?", (run_id,)
            ).fetchone()
            # A checkpoint may be rejected while the run is paused (the normal
            # case) or after it failed downstream of the checkpoint (host wants
            # to redo the checkpoint step). The true safety invariant is that
            # the named checkpoint step is in 'completed' status (checked
            # below) — not the run-level status. Rejecting a 'completed' run
            # would silently re-open finished work, so that is still refused.
            if not run or run["status"] not in ("paused", "failed"):
                raise SkillFlowError(
                    f"Run '{run_id}' is not in a rejectable state (expected "
                    f"paused or failed, got "
                    f"'{run['status'] if run else 'missing'}')"
                )

            step_row = conn.execute(
                "SELECT id, version FROM skillflow_steps WHERE run_id = ? AND step_id = ? AND status = 'completed'",
                (run_id, step_id),
            ).fetchone()
            if not step_row:
                raise SkillFlowError(f"Step '{step_id}' not found in completed status")

            conn.execute(
                """
                UPDATE skillflow_steps
                SET status = 'pending', version = version + 1,
                    retry_count = 0,
                    updated_at = datetime('now')
                WHERE id = ? AND version = ?
                """,
                (step_row["id"], step_row["version"]),
            )
            # Inject the rejection feedback so the re-run sees it. We write the
            # `_feedback` channel (the same one loop-back transitions use, see
            # the redirect branch below) because that is the key the claim path
            # preserves across re-claim and the runner reads into the prompt.
            # `_rejection` is kept too for host display / back-compat, but it is
            # `_feedback` that actually reaches the agent. Without this the
            # rejected step re-runs with no knowledge of why it was rejected.
            conn.execute(
                """
                UPDATE skillflow_steps
                SET inputs_json = json_set(
                        json_set(inputs_json, '$._rejection', ?),
                        '$._feedback', ?),
                    updated_at = datetime('now')
                WHERE id = ?
                """,
                (feedback, feedback, step_row["id"]),
            )
            conn.execute(
                "UPDATE skillflow_runs SET current_node = ?, status = 'running', updated_at = datetime('now') WHERE id = ?",
                (redirect_to or step_id, run_id),
            )
            # When redirecting, inject feedback into the redirect target
            if redirect_to:
                conn.execute(
                    """
                    UPDATE skillflow_steps
                    SET inputs_json = json_set(inputs_json, '$._feedback', ?),
                        updated_at = datetime('now')
                    WHERE run_id = ? AND step_id = ? AND status = 'pending'
                    """,
                    (feedback, run_id, redirect_to),
                )
            self.notifications.publish_sync(
                "step_checkpoint_rejected",
                {"run_id": run_id, "step_id": step_id},
                step_id=step_id, run_id=run_id,
            )

    def approve_checkpoint(self, run_id: str) -> str:
        """Approve the current checkpoint and advance the pipeline.

        The run must be in 'paused' status on a checkpoint step.  This method
        resumes execution and emits a ``checkpoint_approved`` outbox event so
        downstream consumers (TUI, SSE) can react without polling.

        Returns the next node id (the review step) so the host can surface it
        in the response without an extra DB round-trip.

        Raises SkillFlowError if the run is not paused, or if the last
        completed step is not a checkpoint.
        """
        with self._tx() as conn:
            run = conn.execute(
                "SELECT * FROM skillflow_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if not run:
                raise SkillFlowError(f"Run not found: {run_id}")
            if run["status"] != "paused":
                raise SkillFlowError(
                    f"Run '{run_id}' is not paused (status: '{run['status']}')"
                )

            resolver = self._get_resolver(run["graph_name"])

            # Find the last completed checkpoint step
            steps = conn.execute(
                "SELECT step_id FROM skillflow_steps "
                "WHERE run_id = ? AND status = 'completed' "
                "ORDER BY completed_at DESC",
                (run_id,),
            ).fetchall()

            checkpoint_step_id = ""
            checkpoint_node = None
            for s in steps:
                node = resolver.get_node(s["step_id"])
                if node and node.checkpoint:
                    checkpoint_step_id = s["step_id"]
                    checkpoint_node = node
                    break

            if not checkpoint_step_id:
                raise SkillFlowError(
                    f"No checkpoint step found in completed steps for run '{run_id}'"
                )

            # The run's current_node was already set to the review step when
            # advance_run paused the run.  Just resume — advance_run on the
            # next tick will claim the review step.
            next_node = run["current_node"] or ""
            conn.execute(
                "UPDATE skillflow_runs SET status = 'running', "
                "updated_at = datetime('now') WHERE id = ?",
                (run_id,),
            )

            # Emit via notification bus for real-time TUI/SSE notification
            self.notifications.publish_sync(
                "checkpoint_approved",
                {
                    "run_id": run_id,
                    "step_id": checkpoint_step_id,
                    "project_id": run["project_id"],
                    "label": checkpoint_node.checkpoint_label if checkpoint_node else "",
                    "next_node": next_node,
                },
                step_id=checkpoint_step_id, run_id=run_id,
            )

            # Durable trace record
            self.trace(run_id, "step", "checkpoint_approved", {
                "step_id": checkpoint_step_id,
                "next_node": next_node,
            })

            return next_node

    # ── Recovery ──────────────────────────────────────────────────

    def recover_stale_claims(self, stale_threshold_seconds: float = 300) -> list[str]:
        now_epoch = time.time()
        with self._tx() as conn:
            claimed = conn.execute(
                """
                SELECT id, run_id, step_id, inputs_json, claimed_at
                FROM skillflow_steps WHERE status = 'claimed'
                """,
            ).fetchall()
            # For a TOOL step, a claim is stale only once it is older than the
            # LONGER of the caller's flat threshold and the node's own
            # timeout_seconds. A slow-but-alive tool (e.g. run_tests, whose node
            # declares timeout_seconds=1200) must NOT be reclaimed at the flat
            # threshold — reclaiming it relaunches the tool concurrently with
            # itself, piling up mutually-starving copies (the step-5 rampage).
            # Only a claim older than the tool's max legitimate runtime is
            # presumed dead. Scoped to tool steps on purpose: agent-step
            # reclaim timing is unchanged (it feeds a separate live
            # investigation), and this is the only path prone to the rampage
            # because inline tools re-launch on every advance_run.
            stale = []
            for row in claimed:
                window = stale_threshold_seconds
                try:
                    node = self._get_resolver_for_run(
                        row["run_id"]).get_node(row["step_id"])
                    if node and node.step_type == "tool":
                        if node.timeout_seconds == 0:
                            # 0 = "no timeout": a live tool may run arbitrarily
                            # long, so it is NEVER stale — reclaiming it would
                            # relaunch it concurrently with itself (the rampage).
                            continue
                        if node.timeout_seconds > window:
                            window = float(node.timeout_seconds)
                except Exception:
                    pass
                threshold = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_epoch - window))
                if row["claimed_at"] and row["claimed_at"] < threshold:
                    stale.append(row)
            run_ids: set[str] = set()
            for row in stale:
                # SF-20: track stale recovery count to detect crash loops.
                # If the same step instance has been recovered twice already,
                # the worker keeps dying on it — fail it permanently.
                inputs = self._deserialize(row["inputs_json"])
                stale_count = inputs.get("_stale_recovery_count", 0) + 1
                if stale_count >= 3:
                    error_msg = (
                        f"Step '{row['step_id']}' worker crashed 3 times — "
                        f"likely a code bug or OOM in this step."
                    )
                    conn.execute(
                        """
                        UPDATE skillflow_steps
                        SET status = 'failed', version = version + 1,
                            last_error = ?, claimed_at = NULL, claimed_by = NULL,
                            updated_at = datetime('now')
                        WHERE id = ?
                        """,
                        (error_msg, row["id"]),
                    )
                    conn.execute(
                        "UPDATE skillflow_runs SET current_node = NULL, "
                        "updated_at = datetime('now') WHERE id = ?",
                        (row["run_id"],),
                    )
                    self.notifications.publish_sync(
                        "step_failed",
                        {
                            "run_id": row["run_id"], "step_id": row["step_id"],
                            "error": error_msg, "retryable": False,
                        },
                        step_id=row["step_id"], run_id=row["run_id"],
                    )
                    run_ids.add(row["run_id"])
                    continue

                # Store recovery count in inputs so we can detect repeated crashes
                inputs["_stale_recovery_count"] = stale_count
                conn.execute(
                    """
                    UPDATE skillflow_steps
                    SET status = 'pending', version = version + 1,
                        claimed_at = NULL, claimed_by = NULL,
                        inputs_json = ?,
                        updated_at = datetime('now')
                    WHERE id = ?
                    """,
                    (self._serialize(inputs), row["id"]),
                )
                # Keep current_node — the step was claimed but the worker
                # died before confirm.  advance_run will re-claim the same step.

                run_ids.add(row["run_id"])
            if stale:
                self.notifications.publish_sync(
                    "stale_claims_recovered",
                    {"count": len(stale), "run_ids": list(run_ids)},
                )
            return list(run_ids)

    # ── Outbox ────────────────────────────────────────────────────

    def drain_outbox(self, batch_size: int = 100) -> list[OutboxEvent]:
        with self._tx() as conn:
            rows = conn.execute(
                """
                SELECT id, event_type, payload_json, stream_target FROM skillflow_outbox
                WHERE status = 'pending'
                ORDER BY id ASC LIMIT ?
                """,
                (batch_size,),
            ).fetchall()
            events = []
            for row in rows:
                conn.execute(
                    "UPDATE skillflow_outbox SET status = 'draining', drain_started_at = datetime('now') WHERE id = ?",
                    (row["id"],),
                )
                events.append(OutboxEvent(
                    id=row["id"], event_type=row["event_type"],
                    payload_json=row["payload_json"],
                    stream_target=row["stream_target"],
                ))
            return events

    def ack_outbox(self, event_ids: list[int]) -> None:
        if not event_ids:
            return
        with self._tx() as conn:
            placeholders = ",".join("?" * len(event_ids))
            conn.execute(
                f"UPDATE skillflow_outbox SET status = 'delivered' WHERE id IN ({placeholders})",
                event_ids,
            )

    # ── Durable run trace (append-only audit log) ───────────────────
    # Unlike the outbox (drained + ack'd for SSE delivery), the trace is
    # never deleted. It records every event/prompt/tool-action/lifecycle
    # outcome keyed by step_instance_id, so loop iterations never overwrite
    # one another and a finished run can be reconstructed offline.

    # Truncate oversized payload strings so a giant prompt/response doesn't
    # bloat the DB. Full content over this is clipped with a marker.
    _TRACE_MAX_FIELD = 20000

    def _clip(self, value):
        if isinstance(value, str) and len(value) > self._TRACE_MAX_FIELD:
            return value[: self._TRACE_MAX_FIELD] + f"\n…[clipped {len(value) - self._TRACE_MAX_FIELD} chars]"
        return value

    def trace(self, run_id: str, category: str, event: str,
              payload: dict | None = None, *, step_id: str = "",
              step_instance_id: int | None = None,
              project_id: str = "") -> None:
        """Append one durable trace record for a run.

        category: one of 'event' | 'prompt' | 'response' | 'tool_call' |
                  'tool_result' | 'lifecycle' | 'step'.
        event:    a short verb/name (e.g. 'tool_call', 'on_deliver',
                  'agent_response').
        payload:  arbitrary JSON-able detail; long strings are clipped.

        When ``trace_db_path`` was set at construction and ``project_id`` is
        provided, the record is written to ``{trace_db_path}/{project_id}/trace.db``
        instead of the shared DB.  Falls back to the shared ``skillflow_trace``
        table otherwise (backward-compat).
        """
        if not run_id or not self._trace_enabled:
            return
        clean = {k: self._clip(v) for k, v in (payload or {}).items()}
        try:
            # Resolve target connection: per-project DB when configured,
            # otherwise the shared DB (backward-compat).
            conn = self._get_trace_conn(project_id) if project_id else None
            target = conn or self._conn
            with self._lock:
                # seq is computed INSIDE the insert, atomically per statement.
                # The old in-process counter (seeded once from MAX) was only
                # race-free within ONE SkillFlow instance: every additional
                # process sharing the DB seeded its own counter and minted
                # DUPLICATE seq values — breaking the "unique per run"
                # contract keyset pagination and trace consumers rely on.
                # The (run_id, seq) index makes the MAX an O(log n) seek, and
                # the per-record commit fsync dwarfs it anyway.
                target.execute(
                    """
                    INSERT INTO skillflow_trace
                        (run_id, step_id, step_instance_id, seq, category, event, payload_json)
                    SELECT ?, ?, ?, COALESCE(MAX(seq), 0) + 1, ?, ?, ?
                    FROM skillflow_trace WHERE run_id = ?
                    """,
                    (run_id, step_id or None, step_instance_id,
                     category, event, self._serialize(clean), run_id),
                )
                target.commit()
        except Exception:
            # Tracing must never break a run.
            pass

    def prune_trace(self, run_id: str | None = None, *,
                    keep_last_runs: int | None = None) -> int:
        """Delete trace records to bound growth.

        - run_id: when per-project trace DBs are active, this resolves the
          project from the run and closes/removes its cached connection so
          the caller can delete ``trace.db`` from the filesystem. When
          using the shared DB, deletes rows from ``skillflow_trace``.
        - keep_last_runs: only meaningful with the shared DB; with per-project
          DBs this is a no-op (each project has its own file — bound by
          filesystem lifecycle, not row count).
        Returns the number of rows deleted (shared-DB mode) or 0 otherwise.
        """
        # Per-project DB mode: close the cached connection so the file can
        # be safely removed from the filesystem.
        if self._trace_db_path:
            if run_id is not None:
                pid = self._get_project_id(run_id)
                if pid:
                    self._close_trace_conn(pid)
            if keep_last_runs is not None:
                import logging
                logging.getLogger("skillflow").warning(
                    "prune_trace(keep_last_runs=…) is a no-op with per-project "
                    "trace DBs — each project has its own trace.db file")
            return 0

        # Shared-DB mode (backward compat).
        deleted = 0
        with self._lock:
            if run_id is not None:
                cur = self._conn.execute(
                    "DELETE FROM skillflow_trace WHERE run_id = ?", (run_id,))
                deleted += cur.rowcount
            if keep_last_runs is not None:
                keep = [r[0] for r in self._conn.execute(
                    "SELECT run_id FROM skillflow_trace GROUP BY run_id "
                    "ORDER BY MAX(id) DESC LIMIT ?", (keep_last_runs,)).fetchall()]
                if keep:
                    ph = ",".join("?" * len(keep))
                    cur = self._conn.execute(
                        f"DELETE FROM skillflow_trace WHERE run_id NOT IN ({ph})", keep)
                    deleted += cur.rowcount
            self._conn.commit()
        return deleted

    def get_trace(self, run_id: str, *, step_instance_id: int | None = None,
                  category: str | None = None, after_seq: int | None = None,
                  before_seq: int | None = None, order: str = "asc",
                  limit: int | None = None) -> list[dict]:
        """Return trace records for a run ordered by ``seq``.

        Keyset pagination, stateless (``seq`` is monotonic and unique per run):

        * ``order="asc"`` (default, oldest first): pass ``after_seq`` (the last
          ``seq`` seen) to fetch the next page; rows have ``seq > after_seq``.
        * ``order="desc"`` (newest first): pass ``before_seq`` (the last ``seq``
          seen) to fetch the next page; rows have ``seq < before_seq``.

        ``limit`` bounds the page. With no cursor/limit the full ordered trace is
        returned (original behavior).
        """
        # Resolve target connection: per-project trace DB when active.
        conn = self._conn
        if self._trace_db_path:
            pid = self._get_project_id(run_id)
            if pid:
                pconn = self._get_trace_conn(pid)
                if pconn:
                    conn = pconn

        descending = str(order).lower() == "desc"
        q = "SELECT seq, step_id, step_instance_id, category, event, payload_json, created_at " \
            "FROM skillflow_trace WHERE run_id = ?"
        args: list = [run_id]
        if step_instance_id is not None:
            q += " AND step_instance_id = ?"
            args.append(step_instance_id)
        if category is not None:
            q += " AND category = ?"
            args.append(category)
        if after_seq is not None:
            q += " AND seq > ?"
            args.append(after_seq)
        if before_seq is not None:
            q += " AND seq < ?"
            args.append(before_seq)
        q += " ORDER BY seq DESC" if descending else " ORDER BY seq ASC"
        if limit is not None:
            q += " LIMIT ?"
            args.append(limit)
        out = []
        for r in conn.execute(q, args).fetchall():
            out.append({
                "seq": r["seq"], "step_id": r["step_id"],
                "step_instance_id": r["step_instance_id"],
                "category": r["category"], "event": r["event"],
                "payload": self._deserialize(r["payload_json"]),
                "created_at": r["created_at"],
            })
        return out

    def trace_query(self, run_id: str, sql: str,
                    params: tuple = ()) -> list[sqlite3.Row]:
        """Run a raw SELECT query against the trace DB for a run.

        Resolves the correct database (per-project ``trace.db`` when active,
        shared DB otherwise) so callers like cache-stats aggregators can
        run custom aggregations without knowing the storage layout.
        Only SELECT queries are allowed.
        """
        if not sql.strip().upper().lstrip().startswith("SELECT"):
            raise ValueError("trace_query only supports SELECT statements")

        conn = self._conn
        if self._trace_db_path:
            pid = self._get_project_id(run_id)
            if pid:
                pconn = self._get_trace_conn(pid)
                if pconn:
                    conn = pconn
        return conn.execute(sql, params).fetchall()

    def _get_project_id(self, run_id: str) -> str:
        row = self._conn.execute(
            "SELECT project_id FROM skillflow_runs WHERE id = ?", (run_id,)
        ).fetchone()
        return row["project_id"] if row else ""

    def get_project_id(self, run_id: str) -> str:
        """Public accessor for the project_id of a run."""
        return self._get_project_id(run_id)

    def _get_graph_name(self, run_id: str) -> str:
        row = self._conn.execute(
            "SELECT graph_name FROM skillflow_runs WHERE id = ?", (run_id,)
        ).fetchone()
        return row["graph_name"] if row else ""

    # ── Host tool execution API ─────────────────────────────────────

    def execute_tool(self, name: str, params: dict, *,
                     run_id: str = "", step_id: str = "",
                     step_instance_id: int | None = None,
                     project_root: str = "") -> dict:
        """Execute a tool on behalf of the host's agent loop.

        Resolves the allowed tool list from the graph node internally.
        Write tools write to the skillflow-managed draft directory.
        Read/exploration tools receive ``project_root`` as their workspace.

        Every call + result is recorded to the durable run trace. Pass
        ``step_instance_id`` (from the claimed step's token) so each tool call
        correlates to its exact step instance — essential for loop iterations
        where the same step_id runs many times.
        """
        # Trace the call (params summarized — content fields can be huge).
        param_summary = {k: (f"<{len(v)} chars>" if isinstance(v, str) and len(v) > 200 else v)
                         for k, v in (params or {}).items()}
        self.trace(run_id, "tool_call", name,
                   {"source": "agent", "params": param_summary},
                   step_id=step_id, step_instance_id=step_instance_id)
        result = self._execute_tool_impl(name, params, run_id=run_id,
                                         step_id=step_id, project_root=project_root)
        # Trace the result (key fields only).
        res_summary: dict = {"source": "agent"}
        if isinstance(result, dict):
            for k in ("written", "error", "applied", "size"):
                if k in result:
                    res_summary[k] = result[k]
            if len(res_summary) == 1:
                # Read/search tools (web_search, web_fetch, read_file,
                # list_files) carry their payload in non-write keys. Keep a
                # bounded, readable preview instead of just listing key names.
                blob = json.dumps(result, ensure_ascii=False, default=str)
                res_summary["preview"] = (
                    blob if len(blob) <= 2000
                    else blob[:2000] + f"… <+{len(blob) - 2000} chars>"
                )
        self.trace(run_id, "tool_result", name, res_summary,
                   step_id=step_id, step_instance_id=step_instance_id)
        return result

    def _execute_tool_impl(self, name: str, params: dict, *,
                           run_id: str = "", step_id: str = "",
                           project_root: str = "") -> dict:
        if self._tool_loader is None:
            return {"error": "No ToolLoader configured"}

        # Resolve graph node for allowlist + output.fixed
        node = None
        if run_id and step_id:
            try:
                node = self._get_resolver_for_run(run_id).get_node(step_id)
            except Exception:
                pass

        # Build allowed tool set from agent config + write tool schemas + read tools
        allowed: set[str] = set()
        if node:
            if node.agent_config and node.agent_config in self.agent_registry:
                ac = self.agent_registry.get(node.agent_config)
                if ac:
                    allowed.update(ac.tools)
            if node.output_mode:
                from skillflow.write_tools import generate_write_tool_schemas
                for ws in generate_write_tool_schemas(
                        node.output_mode, node.output_fixed,
                        allow_full_write=node.output_allow_full_write):
                    allowed.add(ws["name"])
            # Add read tool names from context specs (mode ∈ {tool, both})
            if node.context:
                from skillflow.read_tools import get_read_tool_names
                allowed.update(get_read_tool_names(node.context))

        if allowed and name not in allowed:
            return {"error": f"Tool '{name}' not allowed. Allowed: {sorted(allowed)}"}

        fixed = node.output_fixed if node else {}

        # Write/create/edit tools — write to step tmp directory (atomic staging)
        if (name.startswith("write_") or name.startswith("create_")
                or name.startswith("edit_")):
            if not self._workspace:
                return {"error": "No workspace configured for write tool"}
            pid = self._get_project_id(run_id)
            gname = self._get_graph_name(run_id)
            tmp_dir = self._workspace.get_step_tmp_dir(pid, gname, step_id)
            from skillflow.write_tools import (execute_write, execute_create,
                                               execute_edit)
            slot = name[name.index("_") + 1:]  # everything after first _
            if name.startswith("create_"):
                return execute_create(slot, fixed, params, str(tmp_dir))
            elif name.startswith("edit_"):
                # Edit the EXISTING file from the consolidated repo (project_root),
                # writing the result into staging for promotion + repo_apply.
                return execute_edit(slot, fixed, params, str(tmp_dir),
                                    source_dir=project_root or "")
            else:
                return execute_write(slot, fixed, params, str(tmp_dir))

        # Generic write-mode tools (mode: write, no fixed slots): create new
        # files / edit existing ones surgically. edit reads its baseline from
        # the consolidated repo (project_root) but writes the whole result into
        # staging — the repo is only ever mutated by on_deliver:repo_apply.
        if name in ("create", "edit", "write"):
            if not self._workspace:
                return {"error": "No workspace configured for write tool"}
            pid = self._get_project_id(run_id)
            gname = self._get_graph_name(run_id)
            tmp_dir = self._workspace.get_step_tmp_dir(pid, gname, step_id)
            from skillflow.write_tools import (execute_generic_create,
                                               execute_generic_edit,
                                               execute_generic_write)
            if name == "create":
                return execute_generic_create(params, str(tmp_dir),
                                              source_dir=project_root or "")
            if name == "edit":
                return execute_generic_edit(params, str(tmp_dir),
                                            source_dir=project_root or "")
            return execute_generic_write(params, str(tmp_dir))

        # finish_step — no-op completion signal; the host runner detects it and
        # breaks the tool-calling loop after the current turn completes
        if name == "finish_step":
            return {"status": "completed", "summary": params.get("summary", "")}

        # Read/exploration/validation tools via ToolLoader
        fn = self._tool_loader.load_fn(name)
        kwargs = dict(params)
        kwargs.setdefault("workspace_root", project_root or "")
        kwargs.setdefault("project_root", project_root or "")
        # Forward step/run identity so tools that want per-step state (e.g.
        # scratch-file tools) can isolate by step. Signature-filtered below, so
        # tools that don't declare these params are unaffected.
        kwargs.setdefault("step_id", step_id or "")
        kwargs.setdefault("run_id", run_id or "")
        # SF-10: pass step staging/output dirs so read_file (and similar tools)
        # can find files the agent just wrote (in .tmp) or files from previous
        # retries (in the step's final dir). write_* tools write to .tmp; without
        # these fallback paths the agent can't verify its own output within a step.
        if name in ("read_file", "list_tree"):
            try:
                if run_id and step_id and self._workspace:
                    pid = self._get_project_id(run_id)
                    gname = self._get_graph_name(run_id)
                    kwargs.setdefault("step_tmp_dir",
                                      str(self._workspace.get_step_tmp_dir(pid, gname, step_id)))
                    kwargs.setdefault("step_dir",
                                      str(self._workspace.get_step_dir(pid, gname, step_id)))
            except Exception:
                pass
        # Filter kwargs to only what the function accepts
        import inspect as _inspect
        try:
            sig = _inspect.signature(fn)
            kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
        except (ValueError, TypeError):
            pass
        result = fn(**kwargs)
        return result if isinstance(result, dict) else {"output": result}

    def _read_edge_counts(self, conn: sqlite3.Connection, run_id: str) -> dict[tuple[str, str], int]:
        result: dict[tuple[str, str], int] = {}
        for er in conn.execute(
            "SELECT from_step, to_step, count FROM skillflow_edge_counts WHERE run_id = ?",
            (run_id,),
        ).fetchall():
            result[(er["from_step"], er["to_step"])] = er["count"]
        return result

    # ── Internal helpers ───────────────────────────────────────────

    def _evaluate_end_conditions(self, conn: sqlite3.Connection, run_id: str,
                                  ec: EndConditions, next_node: str) -> EndResult | None:
        results: list[EndResult] = []
        for cond in ec.conditions:
            if cond.type == "node_reached":
                if next_node == cond.node:
                    if cond.require_completed:
                        step_row = conn.execute(
                            "SELECT status FROM skillflow_steps "
                            "WHERE run_id = ? AND step_id = ?",
                            (run_id, cond.node),
                        ).fetchone()
                        if not step_row or step_row["status"] != "completed":
                            continue  # step hasn't executed yet, skip
                    results.append(EndResult(status=cond.result, reason=f"Node '{cond.node}' reached"))
            elif cond.type == "max_total_steps":
                total = conn.execute(
                    "SELECT COUNT(*) as cnt FROM skillflow_steps WHERE run_id = ? AND status IN ('completed', 'failed')",
                    (run_id,),
                ).fetchone()
                if total and total["cnt"] >= cond.limit:
                    results.append(EndResult(status="failed", reason=f"Max total steps ({cond.limit}) exceeded"))
            elif cond.type in ("max_run_duration", "max_run_duration_seconds"):
                run = conn.execute(
                    "SELECT started_at FROM skillflow_runs WHERE id = ?", (run_id,)
                ).fetchone()
                if run and run["started_at"]:
                    try:
                        import datetime as dt
                        # started_at is written via SQLite datetime('now'), which
                        # is space-separated ('2026-06-20 18:40:51'). Tolerate a
                        # 'T' separator too. The old parser only accepted 'T', so
                        # every parse raised ValueError and this universal
                        # runaway cap was silently dead (a 1h cap let a 3h loop
                        # run).
                        started_dt = dt.datetime.strptime(
                            run["started_at"].replace("T", " "),
                            "%Y-%m-%d %H:%M:%S")
                        elapsed = (dt.datetime.utcnow() - started_dt).total_seconds()
                        if elapsed >= cond.limit:
                            results.append(EndResult(status="failed", reason=f"Max run duration ({cond.limit}s) exceeded"))
                    except (ValueError, OverflowError):
                        pass
            elif cond.type == "flag_match":
                last = conn.execute(
                    """
                    SELECT result_flags_json FROM skillflow_steps
                    WHERE run_id = ? AND status = 'completed'
                    ORDER BY completed_at DESC LIMIT 1
                    """,
                    (run_id,),
                ).fetchone()
                if last:
                    flags = self._deserialize(last["result_flags_json"])
                    if _flags_match(cond.flag, flags):
                        results.append(EndResult(status="failed", reason=f"Flag match: {cond.flag}"))
        if not results:
            return None
        if ec.combinator == "or":
            return results[0]
        else:
            return results[0] if len(results) == len(ec.conditions) else None

    def _fail_run_in_tx(self, conn: sqlite3.Connection, run_id: str, reason: str):
        conn.execute(
            """
            UPDATE skillflow_runs SET status = 'failed', error_reason = ?,
                completed_at = datetime('now'), updated_at = datetime('now')
            WHERE id = ?
            """,
            (reason, run_id),
        )
        self.notifications.publish_sync(
            "run_failed",
            {"run_id": run_id, "reason": reason},
            run_id=run_id,
        )

    def _complete_run_in_tx(self, conn: sqlite3.Connection, run_id: str, reason: str):
        conn.execute(
            """
            UPDATE skillflow_runs SET status = 'completed',
                completed_at = datetime('now'), updated_at = datetime('now')
            WHERE id = ?
            """,
            (run_id,),
        )
        self.notifications.publish_sync(
            "run_completed",
            {"run_id": run_id, "reason": reason},
            run_id=run_id,
        )


def _flags_match(match: dict, flags: dict) -> bool:
    for key, expected in match.items():
        if key not in flags:
            return False
        if flags[key] != expected:
            return False
    return True
