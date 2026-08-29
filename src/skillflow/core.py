"""SkillFlow main class.

Provides the full run lifecycle: create, claim, execute (application),
confirm/fail, advance, checkpoint, and recovery. Uses a persistent
SQLite connection (single-worker model) with WAL mode for safety.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import logging
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
    RequiredContextMissing,
    StepVersionConflict,
    StaleClaimFenced,
    SkillFlowError,
    ToolArgumentsUnavailable,
)
from skillflow.identity import owner_is_dead, worker_identity


# ── Graph content identity ───────────────────────────────────────────

def canonical_graph_json(graph: dict) -> str:
    """One text form per graph CONTENT, whatever the authoring formatting.

    `sort_keys` is what makes the digest stable across boots: without it a graph
    that round-trips through YAML in a different key order hashes differently
    and mints a version every restart, which is the failure this replaced.
    """
    return json.dumps(graph, sort_keys=True, ensure_ascii=False)


def graph_digest(graph: dict) -> str:
    return "sha256:" + hashlib.sha256(
        canonical_graph_json(graph).encode("utf-8")).hexdigest()


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
    # Fencing token — the value of skillflow_steps.claim_epoch at claim time.
    # Checked by the write paths (confirm_step, fail_step, execute_tool) so a
    # reclaimed-but-still-running executor is refused rather than racing its
    # replacement. 0 = unfenced (a hand-built token, or a row claimed before
    # the column existed); the check is skipped when either side is 0 so the
    # migration cannot reject a claim that is legitimately in flight.
    claim_epoch: int = 0


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


def _describe_tool_failure(tool_result: dict) -> str:
    """What to put in the maker's feedback banner when a tool gate rejects.

    The banner is the most prominent thing in the retried step's prompt — it is
    headed "MUST ADDRESS before resubmitting". It used to read, verbatim,
    "Tool failed" for any tool that reports its outcome WITHOUT an `error` key.
    `run_tests` is exactly that shape: it returns `{"written": ..., "passed":
    false}` and puts the pytest output in the report file. So a maker looping back
    from a failed test gate was told, in the loudest place available, nothing at
    all — and, worse, was told a TOOL had failed when what had failed was the
    TESTS. Observed for three consecutive fix rounds before the loop exhausted.

    Prefer `error`; otherwise say which fields the tool did report, and point at
    the artifact, so the banner sends the agent to the detail instead of competing
    with it.
    """
    if not isinstance(tool_result, dict):
        return "Tool failed"
    err = tool_result.get("error")
    if isinstance(err, str) and err.strip():
        return err
    parts = []
    for key in ("summary", "message", "reason", "detail"):
        val = tool_result.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(val.strip())
            break
    written = tool_result.get("written")
    facts = ", ".join(f"{k}={tool_result[k]!r}" for k in sorted(tool_result)
                      if k not in ("error", "summary", "message", "reason", "detail")
                      and not isinstance(tool_result[k], (dict, list)))
    if facts:
        parts.append(f"The gate returned {facts}.")
    if isinstance(written, str) and written.strip():
        parts.append(f"The full detail is in '{written}' — read it before retrying.")
    return " ".join(parts) if parts else "Tool failed"


_ROUTING_REASON_FIELDS = ("feedback", "error", "errors", "reason",
                          "violations", "summary", "message", "detail")
_ROUTING_REASON_MAX_FILES = 3
_ROUTING_REASON_MAX_CHARS = 300


def _readable_reason_value(value) -> str:
    """Flatten a JSON value into one line a human can read."""
    if isinstance(value, str):
        text = value
    elif isinstance(value, (list, tuple)):
        text = "; ".join(_readable_reason_value(v) for v in value
                         if v not in (None, "", [], {}))
    elif isinstance(value, dict):
        text = "; ".join(f"{k}: {_readable_reason_value(v)}"
                         for k, v in value.items())
    else:
        text = str(value)
    return " ".join(text.split())


def _routing_reason(node, file_reader) -> str:
    """Why the edges did not route, read out of the files they route ON.

    A run that dies on an exhausted `max_loop` or an unmatched transition
    reports the EDGE ("Cycle limit exceeded") while the REASON is sitting in
    the very file the matcher just read — `match: {from_file:
    continuity_report.json, field: passed}` failed because that file says
    `{"passed": false, "violations": ["字数超限: 5662 字（上限 4500）"]}`. The
    engine had the fact and discarded it; the user saw only the edge.

    Re-read the `from_file` targets of this node's transitions and pull out
    the first human-readable field. Bounded (at most 3 files, truncated) and
    total: a diagnostic must never turn a clean failure into a crash.
    """
    if node is None or file_reader is None:
        return ""
    try:
        from skillflow.graph import _parse_json_extract_last
        paths: list[str] = []
        for t in getattr(node, "transitions", None) or []:
            path = (t.match or {}).get("from_file")
            if path and path not in paths:
                paths.append(path)
        parts: list[str] = []
        for path in paths[:_ROUTING_REASON_MAX_FILES]:
            try:
                data = _parse_json_extract_last(file_reader(path))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            for field in _ROUTING_REASON_FIELDS:
                value = data.get(field)
                if value is None:
                    # JSON `null` is how a checker says "no error" — rendering it
                    # would print the literal "None" AND stop the search, hiding
                    # the `violations` two keys down. That is this very defect in
                    # miniature: {"passed": false, "error": null, "violations": [...]}.
                    continue
                text = _readable_reason_value(value)
                if text:
                    parts.append(f"{path} {field}: {text}")
                    break
        if not parts:
            return ""
        detail = " | ".join(parts)
        if len(detail) > _ROUTING_REASON_MAX_CHARS:
            detail = detail[:_ROUTING_REASON_MAX_CHARS - 1] + "…"
        return detail
    except Exception:
        return ""


def _flatten_loop_items(items) -> list:
    """Flatten a loop's item manifest to a flat list of names, at ANY depth.

    The manifest's usual shape carries one level of nesting — `execution_order`
    is a list of parallel waves — and the old flatten unwrapped exactly one
    level. An LLM-authored manifest wrapped every wave once more
    (``[[["a", "b"]], [["c"]]]``): one unwrap left lists in place and the
    `set(items)` in `_resolve_loop` raised `TypeError: unhashable type: 'list'`
    on every scheduler tick, wedging the worker for hours. Depth is not
    something to assume, so descend until only leaves remain. Recursion is
    bounded by json.loads, which is itself recursive and rejects anything
    deeper than it can parse.

    Order and duplicates are preserved exactly (depth-first, left to right), so
    a manifest that iterates correctly today yields an identical sequence.
    A non-string leaf (number, dict, null) is COERCED to text, never dropped:
    items name directories, dropping one would silently run less work than the
    manifest declares, and raising would re-create the very crash loop this
    replaces. The coerced name then fails in that one body step, per item,
    where it is visible and retryable.
    """
    flat: list = []
    seen: set[int] = set()   # ids on the CURRENT path — a self-referential list
                             # (never from JSON, but callers vary) stops here

    def _walk(value) -> None:
        if isinstance(value, list):
            if id(value) in seen:
                return
            seen.add(id(value))
            for entry in value:
                _walk(entry)
            seen.discard(id(value))
            return
        if isinstance(value, str):
            flat.append(value)
            return
        try:
            text = json.dumps(value, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(value)
        logging.getLogger("skillflow").warning(
            "loop manifest item %r is not a string; iterating it as %r",
            value, text)
        flat.append(text)

    _walk(items)
    return flat


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
                 trace_db_path: str | None = None,
                 artifact_history: bool = True):
        self._db_path = db_path
        self._graphs: dict[str, PipelineGraph] = {}
        # Named overlay specs (base binding + overlay ops) that compose onto a
        # base graph — the mechanical half of the "addon" registry (the host
        # keeps manifests/prompt-fragments). See register_overlay/compose_config.
        self._overlays: dict[str, dict] = {}
        self._resolvers: dict[str, GraphResolver] = {}
        # Resolvers for PINNED historical versions, keyed (name, version). Kept
        # apart from `_resolvers` on purpose: that one is keyed by name alone and
        # `register_graph` overwrites it, which is exactly the aliasing a pinned
        # run has to be immune to.
        self._pinned_resolvers: dict[tuple[str, int], GraphResolver] = {}
        # (name, version) pairs already reported as missing from the history, so
        # the fallback warns once instead of once per resolver lookup.
        self._pinned_missing: set[tuple[str, int]] = set()
        # run_id → (graph_name, graph_version). A run's pin is immutable except
        # through `repin_run`, which evicts. Keeps `_get_resolver_for_run` off
        # the engine lock on the agent tool-call hot path.
        self._run_pin_cache: dict[str, tuple[str, int | None]] = {}
        # Capability registry: a step's `capability` keyword → a curated
        # toolset + a context provider the FRAMEWORK injects (folders, dirs).
        # Lets the host provision tools/state locations by declared purpose so
        # neither the pipeline author nor the agent picks them (least privilege).
        #   name -> {"tools": [str, ...], "context_provider": callable|None}
        self._capabilities: dict[str, dict] = {}
        # Tool names that failed to resolve, keyed by who asked for them
        # ("capability:tool_creation"). The agent-config half lives in
        # AgentRegistry.unknown_tools(); `unresolved_tools()` merges both.
        self._unresolved_tools: dict[str, set] = {}
        # run_id -> graph_name is immutable for a run; memoized so the hot paths
        # (notably the per-tool-call capability lookup) skip a locked DB query.
        self._graph_name_cache: dict[str, str] = {}
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
        # Activity-based liveness: each trace() marks the claimed step alive by
        # bumping its updated_at (throttled via this map: (run_id, step_id) ->
        # last-heartbeat epoch). recover_stale_claims then measures SILENCE
        # (now - updated_at), not total runtime, so a slow-but-active step is
        # never reaped while a truly dead/hung one still is.
        self._hb_last: dict[tuple[str, str], float] = {}
        self._hb_min_interval = 25.0  # seconds between heartbeat writes per step
        # Tool callables owned by a CLAIM rather than by the shared ToolLoader:
        # the unified read/search/list trio, whose closures capture that step's
        # source map (its workspace, staging dir and code repo). Held in one
        # process-wide, name-keyed slot they were last-writer-wins across
        # projects: with several projects advancing concurrently a review step in
        # one project listed another project's git repository (observed
        # 2026-08-28).
        #
        # LOOKUP key is (run_id, step_id): that is all a tool call carries, and
        # skillflow allows at most one live claim per run, so the pair names the
        # step whose claim is in flight. IDENTITY, stored in the entry, is the
        # PAIR (step_instance_id, claim_epoch) — neither half identifies a claim
        # on its own:
        #
        #   * `step_instance_id` names a `skillflow_steps` ROW, not a claim.
        #     EIGHT UPDATEs in this file reset a row to 'pending'
        #     (`_handle_validation_failure`, `_handle_lifecycle_retry`,
        #     `_fail_step_in_tx`'s retry branch, `_reopen_tool_step_in_tx`,
        #     `reject_checkpoint`, `recover_stale_claims`, `reactivate_run`,
        #     `release_claim`) and
        #     `claim_next_step` then re-claims the SAME row, bumping only
        #     `claim_epoch` — none of the eight writes `claim_epoch` at all; the
        #     only two statements that do are the two claim paths, and both
        #     increment. Successive claims of one row therefore share a row id.
        #   * `claim_epoch` restarts per row. Graph re-entry (a loop body, a
        #     Green/Red re-run) INSERTs a FRESH row whose first claim also yields
        #     `claim_epoch == 1`, so consecutive instances of one step share an
        #     epoch.
        #
        # The pair is unique: the row id separates instances, the epoch separates
        # re-claims within an instance. Entry shape:
        # (step_instance_id, claim_epoch, {name: fn}).
        self._step_tools: dict[tuple[str, str], tuple] = {}
        # Its OWN lock, not the engine RLock. `_step_tool_fn` runs on the hot
        # path of every tool call, and `_tx` holds the engine lock for whole
        # transactions — sharing it would make one project's tool calls wait on
        # another project's claim, i.e. re-introduce cross-project coupling in
        # the very change that exists to remove it. Held across dict operations
        # only — never across a log call or any other I/O — so it can be taken
        # while holding the engine lock (claim_next_step does) and never the
        # other way round.
        self._step_tools_lock = threading.Lock()
        # Names handed out that way. Kept HERE rather than asked of the tool
        # loader: the loader is a duck-typed injected dependency, and making a
        # boolean query part of its contract breaks every custom implementation
        # (and silently mis-answers for a MagicMock, which returns truthy for
        # any attribute). skillflow declared these names, so skillflow knows.
        self._step_scoped_names: set[str] = set()
        self._load_native_tools()
        self._stale_threshold = stale_threshold_seconds
        self._workspace = None
        # Artifact history (ON by default): _step_commit commits each promoted
        # step output dir to a git repo at the workspace root, so a goal-loop
        # re-run (which rmtree-wipes the prior {step}/ before renaming the new
        # one in) no longer destroys the previous output — every iteration is
        # recoverable via `git log`/`git show` for tracing. Best-effort; git
        # failures never break a run (and it no-ops without a workspace, so
        # :memory:/workspace-less hosts are unaffected). Pass
        # artifact_history=False to disable. See _artifact_commit /
        # step_output_versions.
        self._artifact_history = artifact_history
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

    @contextmanager
    def _ro(self):
        """Read-only access to the persistent connection.

        Same lock (the connection is shared and not thread-safe), but no
        `BEGIN IMMEDIATE`: a pure SELECT on a hot path should not take the
        write lock, which is what turns into SQLITE_BUSY and reaped claims
        elsewhere.
        """
        with self._lock:
            yield self._conn

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

    def register_graph(self, graph: PipelineGraph) -> int:
        """Register (or update) a graph and return its content version.

        A version is minted only when the CONTENT changes. Re-registering the
        same graph — which every host does on every boot scan — returns the
        existing version and writes no history row. That idempotence is what
        makes the number mean "how many times this graph was edited" instead of
        "how many times this process started".
        """
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
        canonical = canonical_graph_json(graph.to_dict())
        digest = graph_digest(graph.to_dict())
        with self._tx() as conn:
            latest = conn.execute(
                "SELECT version, digest FROM skillflow_graph_versions "
                "WHERE name = ? ORDER BY version DESC LIMIT 1",
                (graph.name,),
            ).fetchone()
            if latest and latest["digest"] == digest:
                version = latest["version"]
            else:
                version = (latest["version"] + 1) if latest else 1
                conn.execute(
                    "INSERT INTO skillflow_graph_versions "
                    "(name, version, yaml_text, digest) VALUES (?, ?, ?, ?)",
                    (graph.name, version, canonical, digest),
                )
            conn.execute(
                """
                INSERT OR REPLACE INTO skillflow_graphs (name, yaml_text, version, updated_at)
                VALUES (?, ?, ?, datetime('now'))
                """,
                (graph.name, canonical, version),
            )
        return version

    # ── Graph version history ─────────────────────────────────────────

    def list_graph_versions(self, name: str) -> list[dict]:
        """Every recorded content version of *name*, newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT version, digest, created_at FROM skillflow_graph_versions "
                "WHERE name = ? ORDER BY version DESC",
                (name,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_graph_version(self, name: str, version: int) -> dict | None:
        """One historical version as ``{version, digest, created_at, graph}``."""
        with self._lock:
            row = self._conn.execute(
                "SELECT version, digest, created_at, yaml_text "
                "FROM skillflow_graph_versions WHERE name = ? AND version = ?",
                (name, int(version)),
            ).fetchone()
        if not row:
            return None
        return {"version": row["version"], "digest": row["digest"],
                "created_at": row["created_at"],
                "graph": json.loads(row["yaml_text"])}

    def graph_version_for_run(self, run_id: str) -> dict:
        """Which graph content a run is pinned to, and whether it is still latest.

        ``version`` is None for a run created before pinning existed; such a run
        resolves against the current definition, so ``is_latest`` is meaningless
        and is reported as None rather than True.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT graph_name, graph_version, graph_digest "
                "FROM skillflow_runs WHERE id = ?", (run_id,),
            ).fetchone()
            if not row:
                raise SkillFlowError(f"Run '{run_id}' not found")
            cur = self._conn.execute(
                "SELECT MAX(version) AS v FROM skillflow_graph_versions WHERE name = ?",
                (row["graph_name"],),
            ).fetchone()
        latest = cur["v"] if cur else None
        pinned = row["graph_version"]
        return {"graph_name": row["graph_name"], "version": pinned,
                "digest": row["graph_digest"], "latest_version": latest,
                "is_latest": None if pinned is None else pinned == latest}

    def repin_run(self, run_id: str, version: int | None = None) -> dict:
        """Move a run onto another graph version (default: the latest).

        Pinning takes away the ability to hot-patch a run in flight by editing
        its graph and re-registering — which was a real recovery action, not
        only an accident: a node added mid-flight is how one live run was
        unwedged. This gives that back as a deliberate, recorded operation
        rather than a side effect of any edit landing anywhere.

        The step rows are untouched: a node the new version adds simply has no
        row yet and `claim_next_step` opens one.

        REFUSES a target that does not contain the run's `current_node`. That is
        not a theoretical guard — it is the only thing standing between this call
        and a silent permanent wedge. `claim_next_step` resolves an unknown
        `current_node` to `None` and rolls back, `advance_run` keeps returning
        the same node, and the run stays `running` with nothing logged and no
        event: indistinguishable from idle, forever. (`reactivate_run` has a
        resume-step guard, but it only fires when reactivating a FAILED run, so
        it never sees this.)
        """
        with self._tx() as conn:
            row = conn.execute(
                "SELECT graph_name, graph_version, current_node "
                "FROM skillflow_runs WHERE id = ?",
                (run_id,)).fetchone()
            if not row:
                raise SkillFlowError(f"Run '{run_id}' not found")
            name = row["graph_name"]
            if version is None:
                cur = conn.execute(
                    "SELECT MAX(version) AS v FROM skillflow_graph_versions "
                    "WHERE name = ?", (name,)).fetchone()
                version = cur["v"] if cur else None
                if version is None:
                    raise SkillFlowError(
                        f"Graph '{name}' has no version history to re-pin to")
            target = conn.execute(
                "SELECT version, digest FROM skillflow_graph_versions "
                "WHERE name = ? AND version = ?", (name, int(version))).fetchone()
            if not target:
                raise SkillFlowError(
                    f"Graph '{name}' has no version {version}")
            node_now = row["current_node"]
            if not node_now:
                # NULL is routine, not exotic: every retryable `_fail_step_in_tx`
                # clears it, and `recover_stale_claims` clears it unconditionally
                # — so it is the state an operator is MOST likely re-pinning
                # from. Skipping the guard there let a re-pin return success and
                # kill the run on the next advance. `advance_run` re-derives the
                # position from the last completed step, so that is what the
                # target has to still contain.
                last = conn.execute(
                    "SELECT step_id FROM skillflow_steps WHERE run_id = ? "
                    "AND status = 'completed' ORDER BY completion_seq DESC, "
                    "id DESC LIMIT 1", (run_id,)).fetchone()
                node_now = last["step_id"] if last else None
            if node_now:
                probe = self._get_resolver(name, version=target["version"])
                if probe.get_node(node_now) is None:
                    raise SkillFlowError(
                        f"Refusing to re-pin run {run_id} to '{name}' v"
                        f"{target['version']}: it has no step '{node_now}', "
                        f"which is where the run is (or where it would resume "
                        f"from). The run would stop advancing, or fail on its "
                        f"next advance. Re-pin to a version that still has that "
                        f"step, or start a fresh run.")
            conn.execute(
                "UPDATE skillflow_runs SET graph_version = ?, graph_digest = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (target["version"], target["digest"], run_id))
            # The only place a pin changes, so the only place that must evict.
            self._run_pin_cache.pop(run_id, None)
            self.notifications.publish_sync(
                "run_repinned",
                {"run_id": run_id, "graph_name": name,
                 "from_version": row["graph_version"], "to_version": target["version"]},
                run_id=run_id)
        return {"run_id": run_id, "graph_name": name,
                "from_version": row["graph_version"],
                "to_version": target["version"], "digest": target["digest"]}

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

    # ── Overlay registry (composable addons) ──────────────────────────
    def register_overlay(self, name: str, spec: dict) -> None:
        """Register a named overlay spec so it can be composed onto a base graph.

        ``spec`` is the raw overlay dict — ``base`` (the graph it targets),
        optional ``alias`` (the blessed composed name for base+this), optional
        ``description``/``whenToUse``, and ``overlay: [op, ...]`` ops. This is the
        mechanical half of "addons": the host still owns prompt fragments,
        manifests, and assets.
        """
        self._overlays[name] = dict(spec)

    def list_overlays(self) -> list[dict]:
        """Return registered overlays as ``{name, base, alias, description, whenToUse}``."""
        return [{"name": n, "base": s.get("base", ""), "alias": s.get("alias", ""),
                 "description": s.get("description", ""),
                 "whenToUse": s.get("whenToUse", "")}
                for n, s in self._overlays.items()]

    def composed_config_name(self, base_name: str, overlay_names: list[str]) -> str:
        """The name a base+overlays combo composes to: a single overlay's
        ``alias`` when set (the blessed combo), else emergent ``base__a+b``."""
        if len(overlay_names) == 1:
            alias = self._overlays.get(overlay_names[0], {}).get("alias")
            if alias:
                return alias
        return f"{base_name}__{'+'.join(sorted(overlay_names))}"

    def compose_config(self, base_name: str, overlay_names: list[str], *,
                       name: str | None = None, register: bool = True) -> str:
        """Compose a base graph + named overlays into a runnable graph.

        Sources the base from the graph registry (its ``anchors`` are preserved
        so overlay ``@anchor`` targets resolve) and the overlays from the overlay
        registry. Validates each overlay's declared ``base`` matches. Registers
        the composed graph (re-validating reachability/cycles/agent refs) unless
        ``register=False``. Returns the composed config name.
        """
        from skillflow.compose import compose_graph
        if base_name not in self._graphs:
            raise SkillFlowError(f"compose_config: unknown base graph '{base_name}'")
        base_dict = self._graphs[base_name].to_dict()
        overlays: list[dict] = []
        for on in overlay_names:
            spec = self._overlays.get(on)
            if spec is None:
                raise SkillFlowError(f"compose_config: unknown overlay '{on}'")
            declared = spec.get("base", "")
            if declared and declared != base_name:
                raise SkillFlowError(
                    f"overlay '{on}' binds to base '{declared}', not '{base_name}'")
            overlays.append(spec)
        cfg_name = name or self.composed_config_name(base_name, overlay_names)
        merged = compose_graph(base_dict, overlays)
        merged["name"] = cfg_name
        graph = PipelineGraph._from_dict(merged)
        if register:
            self.register_graph(graph)
        return cfg_name

    def describe_config(self, config_name: str) -> dict:
        """Decompose a config name into ``{base, addons}``: an overlay alias →
        its base + [that overlay]; an emergent ``base__a+b`` → parsed; otherwise
        the name itself as a plain base with no addons."""
        for n, s in self._overlays.items():
            if s.get("alias") and s["alias"] == config_name:
                return {"base": s.get("base", ""), "addons": [n]}
        if "__" in config_name:
            base, _, rest = config_name.partition("__")
            return {"base": base, "addons": [a for a in rest.split("+") if a]}
        return {"base": config_name, "addons": []}

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

    # ── Capability registry ─────────────────────────────────────────
    def register_capability(self, name: str, *, tools=(),
                            context_provider=None, briefing: str = "",
                            owner: str = "host") -> None:
        """Register a capability: a step keyword the FRAMEWORK expands into a
        curated toolset + injected context.

        A step declares ``capability: <name>``. At execution the engine then
        - grants an AGENT step every tool in ``tools`` (merged into its tool
          schemas), and
        - injects ``context_provider(config_name)`` — a dict of extra kwargs —
          into every tool call the step makes (both a ``tool`` step and an
          agent-invoked tool). Use it to hand a tool a framework-selected
          directory (a durable ``state_dir``, a ``tools_dir``) so the tool never
          picks its own path.

        ``context_provider`` is ``callable(config_name: str) -> dict`` (the host
        typically closes over the workspace). ``tools`` is a list of tool names.
        Either may be omitted.
        """
        prev = self._capabilities.get(name)
        if prev is not None and prev.get("owner", "host") != owner:
            # Re-registering under the SAME owner is how an edit lands (the
            # generate → drive → fix loop). A different owner is not an edit: it
            # silently changes what every step holding this capability is
            # granted, with nothing in the log to read afterwards. In a registry
            # fed by the base, addons and generated artifacts, that is the one
            # write that must never be quiet.
            raise ValueError(
                f"capability {name!r} is already registered by "
                f"{prev.get('owner', 'host')!r}; {owner!r} cannot redefine it. "
                "Pick another name, or archive the existing one first.")
        self._capabilities[name] = {
            "tools": list(tools or ()),
            "context_provider": context_provider,
            "briefing": briefing or "",
            "owner": owner,
        }

    def _resolve_tool_schema(self, name: str, *, owner: str):
        """Turn a tool NAME into a schema, RECORDING a miss instead of hiding it.

        Every path that resolves a tool name goes through here. That is the point:
        the same swallow was written twice, in two different places, with the same
        false comment — "graph validation will catch it". It does not. `graph.validate()`
        sees only the YAML, so it cannot see an agent config's `tools:` list, and it
        cannot see a capability's grant at all. A role granted `write_file` registered
        clean, ran without it, produced nothing and reported success; a capability whose
        tool is missing grants nothing just as quietly.

        Returns None on a miss rather than raising: hosts legitimately register agents
        and capabilities before tools, and the record clears itself on the next resolve.
        """
        if not self._tool_loader:
            return None
        try:
            schema = self._tool_loader.load_schema(name)
        except Exception:
            missing = self._unresolved_tools.setdefault(owner, set())
            if name not in missing:
                missing.add(name)
                logging.getLogger("skillflow").warning(
                    "%s declares tool %r, which does not resolve — it is silently "
                    "unavailable to that step", owner, name)
            return None
        self._unresolved_tools.get(owner, set()).discard(name)
        return schema

    def unresolved_tools(self) -> dict[str, list[str]]:
        """``{owner: [tool names that do not resolve]}`` across EVERY grant path.

        Owners are ``agent_config:<name>`` and ``capability:<name>``. A host can
        surface this after registration; otherwise a missing tool shows up only as a
        step that mysteriously produces nothing.
        """
        out = {f"agent_config:{k}": v
               for k, v in self.agent_registry.unknown_tools().items()}
        for owner, names in self._unresolved_tools.items():
            if names:
                out[owner] = sorted(names)
        return out

    @staticmethod
    def _declared_capability_names(node, item_card: dict | None = None,
                                   with_source: bool = False):
        """The capability names a step instance declares, and where each came from.

        Three forms, all explicit: a name, a list of names, or
        ``{from_item: "<field>", card: "<path>"}`` which reads the list off the
        loop item's card. `from_item` is written down rather than inferred for
        the same reason `repo_mode` is: a grant that appears from nowhere cannot
        be traced back when it is missing.

        The SOURCE matters downstream: a name written into the graph is the
        author's; a name from a card is agent-authored data, and only the second
        kind is bounded by the graph's offer list.
        """
        cap = getattr(node, "capability", "") or ""
        names: list[tuple[str, str]] = []
        if isinstance(cap, str):
            names = [(cap, "graph")] if cap else []
        elif isinstance(cap, list):
            names = [(c, "graph") for c in cap if c]
        elif isinstance(cap, dict):
            field = cap.get("from_item") or ""
            if not field:
                # The one shape that used to grant nothing in total silence: an
                # object declaration with a typo'd key.
                logging.getLogger("skillflow").warning(
                    "step %s declares a capability object with no `from_item` "
                    "(keys: %s) — granting nothing",
                    getattr(node, "id", "?"), sorted(cap))
            elif isinstance(item_card, dict):
                got = item_card.get(field) or []
                if isinstance(got, str):
                    got = [got]
                names = [(c, "item") for c in got if isinstance(c, str) and c]
        return names if with_source else [n for n, _ in names]

    def _capabilities_for(self, node, item_card: dict | None = None,
                          offers: list[str] | None = None) -> list[tuple[str, dict]]:
        """Registered ``(name, capability)`` pairs this step instance holds.

        A name the graph does not OFFER is refused here as well as at whatever
        gate produced it: the offer list is the engine's own check, so a
        hand-edited task card cannot grant itself tools that the pipeline never
        advertised. A name that is offered but not registered is a deployment
        gap, and is warned about rather than silently skipped.
        """
        out: list[tuple[str, dict]] = []
        offered = set(offers or ())
        for name, src in self._declared_capability_names(node, item_card,
                                                         with_source=True):
            # The offer list bounds what DATA may grant. A name written into the
            # graph is the author's own declaration and needs no second list to
            # authorise it — requiring one would break every graph that already
            # declares `capability:`. A name arriving from a task card is
            # agent-authored input, and a JSON file must not be able to grant
            # tools the pipeline never advertised, so an empty/absent offer list
            # refuses ALL of those rather than (as an earlier `and offers` did)
            # waving them all through. A graph that does declare an offer list
            # binds both, so an author contradicting themselves is caught too.
            if src == "item" and name not in offered:
                logging.getLogger("skillflow").warning(
                    "step %s: task card declares capability %r, which this "
                    "graph does not offer — refused", getattr(node, "id", "?"),
                    name)
                continue
            if src == "graph" and offered and name not in offered:
                logging.getLogger("skillflow").warning(
                    "step %s declares capability %r, absent from this graph's "
                    "own offer list — refused", getattr(node, "id", "?"), name)
                continue
            cap = self._capabilities.get(name)
            if cap is None:
                logging.getLogger("skillflow").warning(
                    "step %s declares capability %r, which is not registered on "
                    "this deployment — granting nothing",
                    getattr(node, "id", "?"), name)
                continue
            out.append((name, cap))
        return out

    def _capability_item_card(self, node, run, loop_context) -> dict | None:
        """The loop item's card, for a ``{from_item: ...}`` declaration.

        Two shapes, because loops carry two: an item that IS a dict answers
        directly; a loop over names (DPE's task manifest is a list of task ids)
        needs the file that names it, which the declaration points at:

            capability: { from_item: "capabilities", card: "3/tasks/$current_task.json" }

        The path is interpolated from the same loop variables the context sources
        use, so there is one substitution rule in the config, not two.
        """
        cap = getattr(node, "capability", "") or ""
        if not isinstance(cap, dict) or not cap.get("from_item"):
            return None
        # (A loop item is always a string by the time it reaches loop state —
        # `_flatten_loop_items` serialises every non-string leaf — so the card
        # path is the only way in, and `card:` is REQUIRED, not optional.)
        card = cap.get("card") or ""
        if not card:
            logging.getLogger("skillflow").warning(
                "step %s declares `from_item` with no `card:` — there is no "
                "other way to reach the item's fields, so nothing is granted",
                getattr(node, "id", "?"))
            return None
        if not card or not self._workspace:
            return None
        import re as _re
        if "$" in card and loop_context:
            def _sub(m):
                v = m.group(1)
                return str(loop_context.get(f"[{v}]", loop_context.get(v, m.group(0))))
            card = _re.sub(r"\$(\w+)", _sub, card)
        try:
            step_part, _, file_part = card.partition("/")
            base = self._workspace.get_config_path(
                run["project_id"], run["graph_name"]).resolve()
            path = (base / step_part / file_part).resolve()
            # Both halves are agent-reachable: the declaration is a config, but
            # `$current_task` interpolates a loop item read out of an LLM-written
            # manifest, so an item named "../../../x" would otherwise read any
            # JSON file on the box and grant from it. state_dir jails traversal
            # for the same reason; this path had no containment check at all.
            if base not in path.parents:
                logging.getLogger("skillflow").warning(
                    "step %s: capability card %r escapes the config directory — "
                    "refused", getattr(node, "id", "?"), card)
                return None
            if not path.is_file():
                logging.getLogger("skillflow").warning(
                    "step %s reads capabilities from %s, which does not exist — "
                    "granting nothing", getattr(node, "id", "?"), card)
                return None
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logging.getLogger("skillflow").warning(
                "step %s could not read its capability card %s",
                getattr(node, "id", "?"), card, exc_info=True)
            return None

    def _granted_capabilities(self, run_id: str, step_id: str) -> list[str]:
        """Capability names this step instance was granted at claim time."""
        if not run_id or not step_id:
            return []
        try:
            # Read-only: `_tx()` opens BEGIN IMMEDIATE, and this runs twice per
            # agent tool call (allowlist + kwargs). Taking the write lock on the
            # hottest path is what the tool fast-path's own comment warns about —
            # SQLITE_BUSY there turns into reaped claims.
            with self._ro() as conn:
                row = conn.execute(
                    "SELECT inputs_json FROM skillflow_steps WHERE run_id = ? "
                    "AND step_id = ? ORDER BY id DESC LIMIT 1",
                    (run_id, step_id)).fetchone()
            if not row:
                return []
            got = (self._deserialize(row["inputs_json"]) or {}).get("_capabilities")
            return [c for c in (got or []) if isinstance(c, str)]
        except Exception:
            logging.getLogger("skillflow").warning(
                "could not read granted capabilities for %s/%s",
                run_id, step_id, exc_info=True)
            return []

    def capabilities(self) -> dict[str, dict]:
        """Registered capabilities as ``{name: {tools, briefing, owner}}``.

        Public because the host reads this table in six places — a palette, an
        emit gate, a registry module, a catalog row — and was reaching into
        `_capabilities` to do it. A private name that gets renamed here degrades
        every one of those SILENTLY: the gate returns no violations and goes
        green, the palette says the deployment registers nothing. An accessor
        turns that skew into an AttributeError at the first call.

        The `context_provider` callable is deliberately not exposed: it is the
        engine's to invoke, not a caller's to inspect.
        """
        return {name: {"tools": list(cap.get("tools") or ()),
                       "briefing": cap.get("briefing", ""),
                       "owner": cap.get("owner", "host")}
                for name, cap in self._capabilities.items()}

    def graph_capabilities(self, graph_name: str) -> list[str]:
        """What a registered graph OFFERS, or [] if it is not registered."""
        graph = self._graphs.get(graph_name)
        return list(getattr(graph, "capabilities", []) or [])

    def _capability_of(self, node) -> dict | None:
        """First registered capability of a STATIC declaration (legacy readers)."""
        names = self._declared_capability_names(node)
        for n in names:
            cap = self._capabilities.get(n)
            if cap is not None:
                return cap
        return None

    def _capability_context(self, node, config_name: str,
                            item_card: dict | None = None,
                            offers: list[str] | None = None,
                            names: list[str] | None = None) -> dict:
        """Extra kwargs a step's capabilities inject into its tool calls.

        Several capabilities may apply to one step, so their kwargs are merged —
        and a KEY COLLISION RAISES. Two capabilities disagreeing about
        `state_dir` is not a preference to resolve by ordering; letting the last
        writer win hands a tool a directory chosen by registration order, which
        is exactly the class of bug `stateful` exists to prevent.
        """
        merged: dict = {}
        source: dict[str, str] = {}
        # `names` — what the CLAIM resolved — is preferred over re-deriving from
        # the node: a `{from_item: ...}` declaration needs the loop item's card,
        # which these call sites do not have, so re-deriving silently produced an
        # empty list and the step's tools were handed no state_dir at all.
        pairs = ([(n, self._capabilities[n]) for n in names
                  if n in self._capabilities] if names is not None
                 else self._capabilities_for(node, item_card, offers))
        for name, cap in pairs:
            provider = cap.get("context_provider")
            if not provider:
                continue
            try:
                out = provider(config_name) or {}
            except Exception:
                logging.getLogger("skillflow").warning(
                    "capability %r context_provider for step %s failed",
                    name, getattr(node, "id", "?"), exc_info=True)
                continue
            if not isinstance(out, dict):
                continue
            for k, v in out.items():
                if k in merged and merged[k] != v:
                    # NOT raised. This is called from three places and none of
                    # them turns an exception into a failed step: the tool-node
                    # path lets it escape `advance_run` (the run then wedges at
                    # its current node, failing nothing, forever), the claim path
                    # swallows it and claims the step with NO resolved context at
                    # all, and the agent-invoked path drops it silently. Loud and
                    # deterministic beats any of those: the key is omitted, so the
                    # tool errors on its own missing argument instead of being
                    # handed a value chosen by registration order.
                    logging.getLogger("skillflow").error(
                        "capabilities %r and %r both inject %r with different "
                        "values on step %s — omitting it; fix the pair",
                        source[k], name, k, getattr(node, "id", "?"))
                    merged.pop(k, None)
                    source[k] = "__conflict__"
                    continue
                if source.get(k) == "__conflict__":
                    continue
                merged[k] = v
                source[k] = name
        return merged

    def _check_agent_configs(self, graph: PipelineGraph) -> list[str]:
        """Return names of agent_configs referenced in graph but not registered."""
        missing: list[str] = []
        for node in graph.steps:
            if node.agent_config and node.agent_config not in self.agent_registry:
                missing.append(node.agent_config)
        return missing

    def _get_resolver(self, graph_name: str,
                      version: int | None = None) -> GraphResolver:
        if version is not None:
            resolver = self._pinned_resolvers.get((graph_name, version))
            if resolver is not None:
                return resolver
            with self._lock:
                row = self._conn.execute(
                    "SELECT yaml_text FROM skillflow_graph_versions "
                    "WHERE name = ? AND version = ?", (graph_name, version)
                ).fetchone()
            if row:
                graph = PipelineGraph._from_dict(json.loads(row["yaml_text"]))
                resolver = GraphResolver(graph)
                self._pinned_resolvers[(graph_name, version)] = resolver
                return resolver
            # The pinned version is missing (a restored or hand-edited DB).
            # Falling through to the current definition is wrong, but refusing
            # strands the run with no way to finish; say so and continue.
            #
            # Once per (name, version), not once per lookup. This runs several
            # times per tick against a run that will keep missing forever, which
            # is ~100 identical WARNINGs a minute — the volume that evicts real
            # events from the log window. The resolver itself is deliberately
            # NOT cached under the pinned key: the fallback is "whatever is
            # current", and current can change.
            if (graph_name, version) not in self._pinned_missing:
                self._pinned_missing.add((graph_name, version))
                logging.getLogger("skillflow").warning(
                    "graph '%s' version %s is not in the version history — "
                    "resolving against the CURRENT definition instead. Runs "
                    "pinned to it are executing a graph they did not start "
                    "with.", graph_name, version)
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
        """The graph a run is PINNED to — not whatever is registered right now.

        Resolving by name alone meant an edit landing mid-run retargeted the
        run's remaining steps: its finished steps had been validated against one
        set of rules and its next ones against another, silently.
        """
        # Memoized like `_get_graph_name`, and for the same reason: this is now
        # on the agent tool-call path (twice per call) and on every advance_run,
        # where it replaced a lock-free dict lookup. Taking the engine RLock and
        # running a SELECT there makes one project's tool calls wait on another
        # project's transaction — the exact contention `_step_tools_lock` was
        # given its own lock to avoid. A run's pin changes only in `repin_run`,
        # which evicts this.
        pin = self._run_pin_cache.get(run_id)
        if pin is None:
            # Read AND memoize under the same lock. `repin_run` evicts from
            # inside its transaction, which also holds this lock, so the write
            # must be in here too — otherwise: this thread reads v1, releases
            # the lock, `repin_run` UPDATEs to v2 and pops a key that is not
            # there yet, and then this thread installs the stale v1. Nothing
            # evicts it again, because the only evictor has already run. The run
            # executes v1 forever while the DB and the tool's return value both
            # say v2 — the recovery reports success and does nothing.
            with self._lock:
                row = self._conn.execute(
                    "SELECT graph_name, graph_version FROM skillflow_runs "
                    "WHERE id = ?", (run_id,)
                ).fetchone()
                if not row:
                    raise SkillFlowError(f"Run '{run_id}' not found")
                pin = (row["graph_name"], row["graph_version"])
                self._run_pin_cache[run_id] = pin
        return self._get_resolver(pin[0], version=pin[1])

    def _graph_for_run(self, run_id: str) -> PipelineGraph | None:
        """The pinned graph, for callers that need the graph and not a resolver."""
        try:
            return self._get_resolver_for_run(run_id).graph
        except Exception:
            return None

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

        # Pin the version whose content IS `graph` — the one the step rows below
        # are built from — never merely "the latest row".
        #
        # `register_graph` publishes to `self._graphs` BEFORE its transaction, so
        # "latest row" and "the graph _get_resolver just handed us" can disagree:
        # durably if that transaction failed (the in-memory graph stays, no
        # version row is ever written), and transiently because scheduler ticks
        # run in threads and can land between the two. Pinning the wrong one puts
        # a run's step rows and its resolver on different graphs — precisely the
        # disagreement this whole mechanism exists to prevent, and if the two
        # differ on `begin` or a node id the run wedges on a node that looks
        # perfectly valid in the DB.
        _digest = graph_digest(graph.to_dict())
        with self._tx() as conn:
            # No row for this exact content means it was never versioned (an
            # older build, or a registration whose transaction failed). NULL is
            # then the RIGHT pin, not a fallback: it resolves by name, which
            # returns the very graph these rows are being built from.
            _v = conn.execute(
                "SELECT version, digest FROM skillflow_graph_versions "
                "WHERE name = ? AND digest = ? ORDER BY version DESC LIMIT 1",
                (graph_name, _digest)
            ).fetchone()
            conn.execute(
                """
                INSERT INTO skillflow_runs (id, graph_name, graph_path, graph_version, graph_digest, project_id, context_json, current_node, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
                """,
                (run_id, graph_name, graph_path,
                 _v["version"] if _v else None, _v["digest"] if _v else None,
                 project_id, self._serialize(ctx), graph.begin),
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
                # Fallback: use the last completed step — by COMPLETION order,
                # not id (same divergence as advance_run: a looped run's
                # highest-id completed row can be an hours-old early step, and
                # resuming from it replays already-passed ground).
                last = conn.execute(
                    "SELECT step_id FROM skillflow_steps WHERE run_id = ? "
                    "AND status = 'completed' "
                    "ORDER BY completion_seq DESC, id DESC LIMIT 1",
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
            if retry_step_id and self._get_resolver_for_run(
                    run_id).get_node(retry_step_id) is None:
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
                # A deleted run must stop answering from the memo — otherwise
                # `_get_resolver_for_run` keeps serving its pin instead of
                # raising "Run not found", and the dict grows without bound.
                self._run_pin_cache.pop(run_id, None)
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

    # Everything about a step EXCEPT the four JSON payload columns. Those four
    # carry a step's whole resolved context and its whole output, and on a real
    # run they are enormous: measured on one host, a single `5_review` row is
    # 89 MB and one run's rows total 260 MB.
    _STEP_SUMMARY_COLUMNS = (
        "id, run_id, step_id, status, version, retry_count, "
        "validation_retry_count, max_retries, last_error, claimed_at, "
        "claimed_by, claim_epoch, completed_at, completion_seq, loop_item, "
        "created_at, updated_at"
    )

    def get_steps(self, run_id: str, *,
                  include_payloads: bool = False) -> list[dict]:
        """All step instances of a run, in GRAPH declaration order.

        Raw id order is creation order: loop/reject re-runs append new
        instances of EARLY steps after the whole graph was instantiated, so a
        UI rendering id order shows a re-run outline AFTER still-pending
        downstream steps — the pipeline appears to run backwards. Sorting by
        the node's declared position keeps every instance under its step
        (multiple attempts adjacent, in id order); steps not in the graph
        (renamed/removed mid-flight) sink to the end rather than erroring.

        The payload columns (``step_config_json``, ``inputs_json``,
        ``outputs_json``, ``result_flags_json``) are LEFT OUT unless you ask
        for them. This was `SELECT *`, and every caller that wanted to know
        "which steps of this run are done" — a dashboard listing, a status
        sync running on every scheduler tick — read hundreds of megabytes of
        agent context and output off disk and decoded it into Python strings
        to look at ``step_id`` and ``status``. A six-project listing on one
        host moved over a gigabyte that way and took a minute, on the event
        loop, with the whole API dark behind it. The keys are OMITTED rather
        than blanked, so a caller that actually needs a payload fails on the
        missing key instead of quietly reading an empty one.
        """
        columns = "*" if include_payloads else self._STEP_SUMMARY_COLUMNS
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT {columns} FROM skillflow_steps
                   WHERE run_id = ? ORDER BY id ASC""",
                (run_id,),
            ).fetchall()
            steps = [dict(r) for r in rows]
            run = self._conn.execute(
                "SELECT graph_name FROM skillflow_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        if not run:
            return steps
        try:
            graph = self._get_resolver_for_run(run_id).graph
            node_pos = {node.id: i for i, node in enumerate(graph.steps)}
        except Exception:
            return steps  # graph gone/unloadable — creation order is still usable
        return sorted(steps,
                      key=lambda s: (node_pos.get(s["step_id"], len(node_pos)),
                                     s["id"]))

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

    # ── Fencing ────────────────────────────────────────────────────

    def _epoch_holds(self, step_instance_id: int, claim_epoch: int,
                     conn: "sqlite3.Connection | None" = None) -> bool:
        """Is `claim_epoch` still this step instance's claim?

        False means the step was reclaimed and the caller is a zombie. Either
        side being 0 means "unfenced" and always holds: a hand-built token, or
        a row that predates the column, must not be locked out by a migration.
        """
        if not claim_epoch:
            return True
        sql = "SELECT claim_epoch FROM skillflow_steps WHERE id = ?"
        if conn is not None:
            row = conn.execute(sql, (step_instance_id,)).fetchone()
        else:
            # Own the lock for the read: the connection is shared across the
            # host's threads and another one may be mid-_tx.
            with self._lock:
                row = self._conn.execute(sql, (step_instance_id,)).fetchone()
        if row is None:
            return True          # unknown instance — not our call to refuse
        current = row["claim_epoch"] or 0
        return current == 0 or current == claim_epoch

    def _assert_epoch(self, token: "ClaimToken", what: str) -> None:
        """Refuse a write from an executor that no longer holds the step."""
        if self._epoch_holds(token.step_instance_id, token.claim_epoch):
            return
        raise StaleClaimFenced(
            f"Step '{token.step_id}' (instance {token.step_instance_id}) was "
            f"reclaimed: {what} refused for claim_epoch {token.claim_epoch}."
        )

    # ── Claim / Confirm / Fail ─────────────────────────────────────

    def claim_next_step(self, run_id: str) -> ClaimedStep | None:
        with self._tx() as conn:
            run = conn.execute(
                "SELECT * FROM skillflow_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if not run or run["status"] not in ("running",) or not run["current_node"]:
                raise _TxRollback()

            graph_name = run["graph_name"]
            resolver = self._get_resolver(graph_name, version=run["graph_version"])
            # The graph THIS run is pinned to — not `self._graphs[graph_name]`,
            # which register_graph overwrites. `resolver` is rebound to a
            # ContextResolver further down, so keep the graph itself.
            pinned_graph = resolver.graph

            if resolver.is_gate(run["current_node"]):
                raise _TxRollback()

            # A LOOP node is a control node too — no agent_config — so claiming
            # it hands the host's runner a step it can only fail with "Agent
            # config '' not found". `advance_run` leaves `current_node` on one
            # whenever `_resolve_loop` returns None (a loop whose transitions all
            # target its own body, so `_route_done` finds no exit), and unlike
            # its sibling at the tool fast-path that branch fails nothing — the
            # run simply sits there, still `running`. That was harmless while a
            # silent advance ended the tick; a caller that reacts to the silence
            # by asking us to claim (AItelier's wedge recovery, 2026-08-29) turns
            # an idle wait into a failed run. Refuse, as for a gate: the caller's
            # own "nothing claimable" branch then says so with a reason.
            if resolver.is_loop(run["current_node"]):
                raise _TxRollback()

            node = resolver.get_node(run["current_node"])
            if not node:
                raise _TxRollback()

            # An inline (non-delegated) tool step is executed via advance_run's
            # fast-path, NOT claimed and handed to the host's agent runner — a
            # tool step has no agent_config, so the runner would raise
            # "Agent config '' not found". Mirror the is_gate guard above.
            # Delegated tools (runner mode, delegate_tools_to_agent=True) still
            # fall through so the agent claims and executes them. Without this,
            # a tool step spliced right after another tool step (e.g. an addon's
            # scaffold after git_sync_pre, or 5_compile after 5_test) can be
            # claimed as an agent step if a tick reaches claim before the host
            # drains it, failing the step until it self-heals on a later tick.
            if resolver.is_tool(run["current_node"]) and not \
                    self._should_delegate_tool(node.tool_name):
                raise _TxRollback()

            current_version = conn.execute(
                "SELECT version FROM skillflow_steps WHERE run_id = ? AND step_id = ? AND status = 'pending'",
                (run_id, run["current_node"]),
            ).fetchone()
            if not current_version:
                # Open a fresh pending instance when there is no claimable row:
                # - cyclic re-entry (prior instance completed/failed), or
                # - NO row at all: a node added to the graph AFTER this run was
                #   instantiated (mid-flight graph extension). The tool-claim
                #   path already treats no-row as "open a fresh instance"; the
                #   agent path refusing it made every added agent node
                #   unclaimable in existing runs (claim → None forever, run
                #   wedged at current_node with the scheduler ticking in vain).
                # Never when a row is currently claimed (a concurrent driver
                # owns it).
                existing = conn.execute(
                    "SELECT id, status, inputs_json FROM skillflow_steps "
                    "WHERE run_id = ? AND step_id = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (run_id, run["current_node"]),
                ).fetchone()
                if existing is None or existing["status"] in ("completed", "failed"):
                    node = resolver.get_node(run["current_node"])
                    if node:
                        # Carry `_feedback` onto the fresh instance. A loop-back
                        # writes it to the instance that just finished (see
                        # _inject_feedback_in_tx); without this the re-run starts
                        # from a blank inputs_json and the maker never learns why it
                        # was sent back. Only `_feedback` — `_validation_error`
                        # belongs to one instance's own retry cycle, and resurrecting
                        # it here would report an error the re-run has not made yet.
                        carried = {}
                        if existing is not None:
                            prev = self._deserialize(existing["inputs_json"])
                            if isinstance(prev, dict) and "_feedback" in prev:
                                carried["_feedback"] = prev["_feedback"]
                        conn.execute(
                            """
                            INSERT INTO skillflow_steps
                                (run_id, step_id, step_config_json, max_retries, status,
                                 inputs_json, created_at, updated_at)
                            VALUES (?, ?, ?, ?, 'pending', ?, datetime('now'), datetime('now'))
                            """,
                            (run_id, run["current_node"], self._serialize(node.config),
                             node.max_retries, self._serialize(carried)),
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
                    claimed_at = ?, claimed_by = ?,
                    claim_epoch = COALESCE(claim_epoch, 0) + 1,
                    loop_item = ?,
                    updated_at = datetime('now')
                WHERE run_id = ? AND step_id = ? AND version = ? AND status = 'pending'
                """,
                (claimed_at_str, worker_identity("worker"),
                 self._loop_item_in_tx(conn, resolver, run_id, run["current_node"]),
                 run_id, run["current_node"], ver),
            )
            if cursor.rowcount == 0:
                raise _TxRollback()

            step_row = conn.execute(
                "SELECT id, claim_epoch FROM skillflow_steps "
                "WHERE run_id = ? AND step_id = ? AND status = 'claimed'",
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
            # Read from the instance JUST CLAIMED, not "some row with this step_id".
            # A step that runs more than once — every maker in a Green/Red loop after
            # its first rejection — has several instances, and this query used to have
            # no ORDER BY or LIMIT, so fetchone() returned the OLDEST one: the very
            # rows `_handle_validation_failure` and `_inject_feedback_in_tx` write to
            # are the newest. Observed: a step with validation_retry_count=3 re-ran
            # four times with a byte-identical prompt, never once told what was wrong,
            # while a single-instance step in the same run got its feedback fine.
            existing = conn.execute(
                "SELECT inputs_json FROM skillflow_steps WHERE id = ?",
                (step_row["id"],),
            ).fetchone() if step_row else None
            if existing:
                existing_inputs = self._deserialize(existing["inputs_json"])
                if "_error" in existing_inputs:
                    error_context = existing_inputs["_error"]
                if "_validation_error" in existing_inputs:
                    validation_error = existing_inputs["_validation_error"]
                if "_feedback" in existing_inputs:
                    feedback = existing_inputs["_feedback"]
            # Prefer the accumulated checkpoint-feedback log (ALL rounds) over the
            # scalar _feedback (latest only), so a re-run honors every round of
            # user feedback instead of drifting toward the most recent one.
            _fb_log = self._read_feedback_log(
                run["project_id"], run["graph_name"], run["current_node"])
            if _fb_log:
                feedback = _fb_log

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
                claim_epoch=(step_row["claim_epoch"] or 0) if step_row else 0,
            )

            # Drop whatever the PREVIOUS claim of this step left behind, before
            # anything that can skip the re-bind below. That bind sits under
            # three conditions (the step has context specs; the read-tool build
            # did not raise; it yielded at least one callable), and on every one
            # of those skip paths the old closures used to stay in the slot and
            # serve this claim — their 'self'/'promoted' layer pointing at the
            # previous LOOP ITEM's directory, so item B read item A's output as
            # its own. Unconditional: this claim owns the slot from here on.
            self._clear_step_tools(run_id, run["current_node"])

            # Inject resolved tool schemas if agent config is registered
            tool_schemas: dict = {}
            agent_cfg = None
            if node.agent_config and node.agent_config in self.agent_registry:
                agent_cfg = self.agent_registry.get(node.agent_config)
                if agent_cfg and agent_cfg.tool_schemas:
                    # COPY here, not later. This dict belongs to the shared agent
                    # config cache, and everything below (write-tool schemas,
                    # dynamic read tools, capability grants) writes into it. A
                    # copy taken further down leaves the earlier writers mutating
                    # the role itself: a reviewer sharing a role with a write-mode
                    # implementer was handed create/edit/write, and a read tool
                    # whose fn is later evicted stayed advertised on the role.
                    tool_schemas = dict(agent_cfg.tool_schemas)
            # (Capability toolset is merged further down, once the loop item is
            # resolved — a `{from_item: ...}` declaration cannot be read here.)
            # Addon toolset: compose's `add_tools` op parks extra tool names in
            # the step's opaque config. Merge them exactly like a capability
            # grant — copy first, or the grant leaks into every other step that
            # shares this role.
            _extra = node.config.get("extra_tools") if isinstance(node.config, dict) else None
            if _extra and self._tool_loader:
                for _tn in _extra:
                    if _tn in tool_schemas:
                        continue
                    _schema = self._resolve_tool_schema(_tn, owner="addon:add_tools")
                    if _schema is not None:
                        tool_schemas[_tn] = _schema
            inputs_with_tools = dict(inputs)
            if tool_schemas:
                inputs_with_tools["_tool_schemas"] = tool_schemas
            if agent_cfg:
                inputs_with_tools["_agent_config"] = agent_cfg.to_dict()

            # Extract loop item context FIRST so context resolution can reference
            # it. JSON-safe, per-loop, reader-aware routing info (underscore keys
            # are ignored by $var interpolation and the [..]-only prompt injection):
            #   _loop_of:     {step_id: loop_id} for every loop-body step (cached
            #                 reach-back topology — drain/give-up targets are NOT body)
            #   _reader_loop: the claimed step's own loop id ("" when not in a loop)
            #   _loop_items:  {loop_id: current raw item} per loop-state row
            # Read routing (workspace.route_step_read_dir): a source reading a
            # loop-body producer resolves to {step}/{item}/ ONLY when the reader is
            # in the SAME loop (its current item); any outside reader gets the
            # {step}/ parent (all items) — a drained loop's stale current_item can
            # never leak into an aggregator.
            loop_context: dict = {}
            # Routing map covers AGENT body steps only — the per-item producers.
            # Tool body steps write flat (no promotion) and stay flat-read.
            _loop_of = {}
            for _lid, _body in resolver.loop_bodies().items():
                for _n in _body:
                    _bn = resolver.get_node(_n)
                    if _bn is not None and _bn.step_type == "agent":
                        _loop_of.setdefault(_n, _lid)
            if _loop_of:
                loop_context["_loop_of"] = _loop_of
                loop_context["_reader_loop"] = _loop_of.get(node.id, "")
                _rows = conn.execute(
                    "SELECT loop_step_id, current_item, item_context_key "
                    "FROM skillflow_loop_state WHERE run_id = ?",
                    (run_id,),
                ).fetchall()
                _items: dict[str, str] = {}
                for _r in _rows:
                    _it = _r["current_item"]
                    if _it:
                        _items[_r["loop_step_id"]] = (
                            self._serialize(_it) if not isinstance(_it, str) else _it)
                if _items:
                    loop_context["_loop_items"] = _items
                # Legacy $var interpolation keys — from the READER's own loop only
                # (the old unfiltered fetchone could inject ANOTHER loop's item in
                # a multi-loop graph).
                _rl = loop_context["_reader_loop"]
                if _rl and _rl in _items:
                    _krow = next((r for r in _rows if r["loop_step_id"] == _rl), None)
                    _key = (_krow["item_context_key"] if _krow else "") or "loop_item"
                    loop_context[f"[{_key}]"] = _items[_rl]
                    loop_context[_key] = _items[_rl]

            # Resolve context specs from the graph step node (loop vars available as $var)
            if self._workspace and node.context:
                try:
                    from skillflow.context import ContextResolver
                    config_path = self._workspace.get_project_path(
                        run["project_id"]
                    )
                    # code_root: the real code repo, so `from: repository`
                    # inline reads and context-source tools see the SAME tree
                    # the read tools serve (not the workspace's brief dir).
                    # None from get_project_code_path means the run declares no
                    # repo — forwarded as False, because ContextResolver reads
                    # None as "not supplied" and falls back to
                    # workspace_root/"project", the populated project BRIEF dir,
                    # which it then labels "Repository".
                    _ctx_code = self._workspace.get_project_code_path(
                        run["project_id"])
                    resolver = ContextResolver(
                        config_path, self._tool_loader,
                        code_root=_ctx_code if _ctx_code is not None else False,
                        # a `{source: {tool: X}}` context tool runs on behalf of
                        # THIS step → hand it the step's capability context too.
                        extra_tool_kwargs=self._capability_context(
                            node, run["graph_name"],
                            item_card=self._capability_item_card(
                                node, run, loop_context),
                            offers=getattr(pinned_graph, "capabilities", None)))
                    resolved = resolver.resolve(
                        node.context,
                        current_config=run["graph_name"],
                        loop_context=loop_context if loop_context else None,
                    )
                    if resolved:
                        inputs_with_tools["_resolved_context"] = resolved
                except RequiredContextMissing:
                    # A `required: true` source is missing → this is a REAL failure
                    # (the step would otherwise run on absent context). Propagate so
                    # the step fails loudly instead of being swallowed like a
                    # best-effort miss.
                    raise
                except Exception:
                    # Best-effort, but an exception here (vs a clean "no match")
                    # is a resolver bug that silently drops the agent's injected
                    # context — leave a breadcrumb.
                    import logging
                    logging.getLogger("skillflow").warning(
                        "context resolution failed for step %s",
                        getattr(node, "id", "?"), exc_info=True)

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
                        build_source_map,
                        generate_read_tool_schemas,
                        make_read_tool_fns,
                    )
                    ws_root = str(self._workspace.get_project_path(
                        run["project_id"]
                    ))
                    # None when the run declares no code repo — the read surface
                    # then attaches no `repo` layer at all, by decision rather
                    # than by whether that directory happens to exist.
                    _code_path = (self._workspace.get_project_code_path(
                        run["project_id"]) if self._workspace else None)
                    code_root = str(_code_path) if _code_path else ""
                    # Staging-first working tree: the current step's .tmp
                    # (create/edit output) shadows the repo baseline so a
                    # write-mode agent sees its own pending edits (no read
                    # pristine → re-edit stale old_str → thrash). Build the
                    # source map ONCE and share it with both the schema and the
                    # fn builders (avoids re-resolving every context spec twice).
                    _step_tmp = str(self._workspace.get_step_tmp_dir(
                        run["project_id"], run["graph_name"], node.id))
                    # Own promoted layer: per-item for a loop-body step, so the
                    # 'self'/'promoted' read tier sees THIS item's prior round
                    # (not the flat parent listing every sibling item).
                    _own_lid = loop_context.get("_reader_loop") if loop_context else ""
                    _own_item = ((loop_context.get("_loop_items") or {}).get(_own_lid)
                                 if _own_lid else None)
                    _step_dir = str(self._workspace.get_step_dir(
                        run["project_id"], run["graph_name"], node.id,
                        item=_own_item))
                    _smap = build_source_map(
                        node.context,
                        workspace_root=ws_root,
                        current_config=run["graph_name"],
                        code_root=code_root,
                        loop_context=loop_context if loop_context else None,
                        step_tmp_dir=_step_tmp,
                        step_dir=_step_dir,
                    )
                    read_schemas = generate_read_tool_schemas(
                        node.context,
                        step_tmp_dir=_step_tmp,
                        step_dir=_step_dir,
                        _smap=_smap,
                    )
                    if read_schemas and self._tool_loader:
                        # Unified read/search/list closures over this step's
                        # source map. Re-registered fresh each step (names are
                        # stable); the stale-clear below removes any old-style
                        # per-label dynamic read tools left in the cache.
                        read_fns = make_read_tool_fns(
                            node.context,
                            step_tmp_dir=_step_tmp,
                            step_dir=_step_dir,
                            _smap=_smap,
                        )
                        # Legacy per-label dynamic read tools (`read_step_2`, …)
                        # that a host may have registered. Guarded on the private
                        # cache being present: the tool loader is duck-typed, and
                        # reaching into an attribute only the built-in class has
                        # raised straight into the `except` below — which silently
                        # left the step with NO read surface at all.
                        _cache = getattr(self._tool_loader, "_cache", None)
                        if isinstance(_cache, dict):
                            stale = [n for n in _cache
                                     if any(n.startswith(p) for p in
                                            ("list_step_", "read_step_", "search_step_",
                                             "list_repo_", "read_repo_", "search_repo_",
                                             "list_config_", "read_config_", "search_config_",
                                             "list_workspace_", "read_workspace_", "search_workspace_"))
                                     and self._tool_loader.is_dynamic(n)]
                            for n in stale:
                                del _cache[n]
                        step_fns: dict = {}
                        for rs in read_schemas:
                            name = rs["name"]
                            fn = read_fns.get(name)
                            if fn:
                                tool_schemas[name] = rs
                                # Declare the NAME globally (so is_native /
                                # is_dynamic keep classifying it) but hand the
                                # closure to this claim: it captures this step's
                                # source map, and a shared slot let whichever
                                # project claimed last answer everyone's reads.
                                _decl = getattr(self._tool_loader,
                                                "declare_dynamic", None)
                                if _decl:
                                    _decl(name)
                                self._step_scoped_names.add(name)
                                step_fns[name] = fn
                        if step_fns:
                            self._set_step_tools(
                                run_id, node.id, step_fns,
                                step_row["id"] if step_row else 0,
                                (step_row["claim_epoch"] or 0) if step_row else 0)
                except Exception:
                    # Best-effort: the step runs with NO read tools. Not "the
                    # last claim's" — the slot was cleared unconditionally at
                    # claim, above. build_source_map handles missing paths
                    # gracefully, so an exception here is an unexpected wiring
                    # bug, not a routine miss — log it (a silent pass once hid a
                    # TypeError that dropped read tools for every step).
                    import logging
                    logging.getLogger("skillflow").warning(
                        "read tool registration failed for step %s; agent will "
                        "run without read/search/list",
                        getattr(node, "id", "?"), exc_info=True)

            # ── Capability provisioning ──────────────────────────────────
            # Late on purpose: a `{from_item: "..."}` declaration reads the loop
            # item's card, which does not exist until loop_context is built. The
            # toolset merge COPIES first — a grant written into the shared agent
            # config cache leaks into every other step using that role.
            _item_card = self._capability_item_card(node, run, loop_context)
            _offers = getattr(pinned_graph, "capabilities", []) or []
            _caps = self._capabilities_for(node, _item_card, _offers)
            if _caps and self._tool_loader:
                for _cap_name, _cap in _caps:
                    for _tn in _cap.get("tools") or ():
                        if _tn in tool_schemas:
                            continue
                        _schema = self._resolve_tool_schema(
                            _tn, owner=f"capability:{_cap_name}")
                        if _schema is not None:
                            tool_schemas[_tn] = _schema
            # A capability's briefing is the ONLY channel that teaches a step how
            # to use what it was just handed. It rides the step's context (not the
            # system preamble): the preamble is project-global and byte-identical
            # because that is what makes it cacheable, and a per-step block landing
            # there breaks the property it exists for.
            _briefs = [(n, c.get("briefing")) for n, c in _caps if c.get("briefing")]
            if _briefs:
                _rc = inputs_with_tools.setdefault("_resolved_context", {})
                for _n, _b in _briefs:
                    _rc[f"[capability: {_n}]"] = _b
            inputs_with_tools["_capabilities"] = [n for n, _ in _caps]

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
                # carry_forward: seed an EMPTY staging from this step's own
                # promoted output. Promotion is a replace (rmtree + rename), so a
                # re-run after a rejection starts blank and an agent that writes
                # only what it changed destroys the rest — which is precisely the
                # reading its briefing invites ("step output — files from previous
                # retries", searched after staging). Live 2026-08-26: a re-planned
                # PM emitted 2 of 9 task cards and promotion deleted the other 8,
                # while the manifest still named them.
                # Empty-only: a retry mid-step already has its accumulated state
                # here (see the note above) and must not be overwritten by the
                # older promoted copy.
                if getattr(node, "output_carry_forward", False) and not any(
                        tmp_dir.iterdir()):
                    _prior = self._workspace.get_step_dir(
                        run["project_id"], run["graph_name"], node.id)
                    if _prior.exists():
                        import shutil as _shutil
                        for _src in sorted(_prior.rglob("*")):
                            if not _src.is_file():
                                continue
                            _dst = tmp_dir / _src.relative_to(_prior)
                            _dst.parent.mkdir(parents=True, exist_ok=True)
                            _shutil.copy2(_src, _dst)
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

            # How many instances of this step the run has already created.
            # `attempt_feedback` answers a NARROWER question than it reads as:
            # it is true only when `_feedback` was INJECTED (a checkpoint
            # rejection, or a tool edge declaring `feedback: true`). A reviewer
            # that rejects through a verdict file injects nothing — the maker
            # re-reads the verdict through its own `{step: <reviewer>}` context
            # source — so a loop body can re-run three times over one item with
            # `attempt_feedback` false on every claim, and the trace shows no
            # rework at all. Counting instances is how "this step ran again"
            # becomes answerable without reconstructing it from loop items.
            _instance_n = conn.execute(
                "SELECT COUNT(*) FROM skillflow_steps WHERE run_id = ? AND step_id = ?",
                (token.run_id, claimed_step_id),
            ).fetchone()[0]
            # Record the claim itself, so the trace shows step boundaries +
            # any reopen reason (reject feedback / validation error).
            _trace("step", "claimed", {
                "attempt_feedback": bool(inputs_with_tools.get("_feedback")),
                "validation_error": validation_error,
                "instance_n": _instance_n,
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
        # BEFORE the lifecycle hooks, not after. The version guard at the
        # bottom of this method fires only once every side effect has already
        # landed: on_deliver runs repo_apply, which makes real git commits in
        # the user's repository, and after_validate promotes {step}.tmp/ over
        # {step}/. A reclaimed executor reaching here would do both alongside
        # its replacement and only then be told it had lost the step.
        self._assert_epoch(token, "confirm_step")
        # The claim is over — drop the read tools it owned. Identity-guarded on
        # (instance id, claim epoch), so neither a zombie executor of an earlier
        # instance nor a superseded re-claim of the same row can release the
        # entry belonging to the claim that replaced it.
        self._release_step_tools(token.run_id, token.step_id,
                                 token.step_instance_id, token.claim_epoch)
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
                if not self._handle_validation_failure(
                        token, error_msg, promote_on_exhaustion=True):
                    return
                # Budget exhausted. Fall THROUGH to the lifecycle hooks so the
                # staged output is promoted, carrying a flag the graph can route
                # on — see _handle_validation_failure for why.
                result.flags["validation_failed"] = True

        # ── Lifecycle hooks ──────────────────────────────────────────
        if node and self._workspace:
            lifecycle = self._resolve_lifecycle(node)
            promoted = None   # what after_validate promoted; None = it didn't say
            for hook_name, hook_spec in lifecycle.items():
                # Nothing was promoted → there is nothing to deliver, and running
                # the deliver hooks on it is worse than not running them at all.
                # Live, NL2Repo task `funcy`: a t_impl agent answered in 92 tokens
                # and wrote no file, so `_step_commit` promoted nothing (it never
                # even created {step}/{item}/) and `repo_apply` then failed on
                # "Source dir not found" — retrying a TOOL for output only the
                # AGENT could write — after which the step hard-failed into the
                # graph's `_error` edge and landed an empty implementation on a
                # reviewer with nothing to review. Re-ask the agent instead, on the
                # step's own (shared) retry budget, exactly as a failed validator
                # does. It also stops a re-run that wrote nothing from delivering
                # the PREVIOUS attempt's step dir, which _step_commit leaves in
                # place when it promotes nothing.
                if hook_name == "on_deliver" and promoted == []:
                    error = ("Nothing to deliver: the step promoted no files, so "
                             "its on_deliver hooks were skipped. Write the files "
                             "this step is required to produce.")
                    self._emit_lifecycle_event(token, hook_name, "skipped", error)
                    logging.getLogger("skillflow").warning(
                        "step %r (run %s) promoted no files — on_deliver skipped, "
                        "re-asking the step instead of delivering nothing",
                        token.step_id, token.run_id)
                    self._handle_validation_failure(token, error)
                    return
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
                    if hook_name == "after_validate":
                        promoted = files
                    # A free-form write step that promoted NOTHING is almost always a
                    # maker that described its files instead of writing them — and it
                    # used to complete green, hand an empty result downstream, and be
                    # discovered only by a reviewer whose bounded reject loop then
                    # burned out. Observed four rounds running, with "0 file(s)" sitting
                    # right here in the trace the whole time. Surface it as a flag the
                    # graph CAN route on, and say so in the log.
                    if node is not None and getattr(node, "output_mode", "") == "write":
                        result.flags.setdefault("wrote_files", bool(files))
                        # …unless the step also DELIVERS: an empty delivery is
                        # re-asked above, so the step does not complete at all and
                        # this line would be a lie about what happens next.
                        if not files and "on_deliver" not in lifecycle:
                            logging.getLogger("skillflow").warning(
                                "step %r (run %s) declares `output.mode: write` but "
                                "promoted no files — the step will complete with an "
                                "empty output", token.step_id, token.run_id)
                elif hook_result.get("committed"):
                    detail = "committed"
                self._emit_lifecycle_event(token, hook_name, "completed", detail)

        with self._tx() as conn:
            # completion_seq: per-run monotonic COMPLETION order. `id` is
            # creation order — loop/reject re-runs append high-id instances,
            # so after any loop the two orders diverge permanently and
            # position reconstruction must sort by THIS, never by id.
            cursor = conn.execute(
                """
                UPDATE skillflow_steps
                SET status = 'completed', version = version + 1,
                    outputs_json = ?, result_flags_json = ?,
                    completion_seq = (SELECT COALESCE(MAX(completion_seq), 0) + 1
                                      FROM skillflow_steps WHERE run_id = ?),
                    completed_at = datetime('now'), updated_at = datetime('now')
                WHERE id = ? AND version = ?
                """,
                (
                    self._serialize(result.outputs),
                    self._serialize(result.flags),
                    token.run_id,
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
                # Reason FIRST, exhausted edges after: hosts truncate this
                # string for status chips (the one that prompted this change
                # cuts at 160), and the edge list alone runs ~150 chars on real
                # step ids — a reason appended after it is out of sight again.
                self._fail_run_in_tx(
                    conn, token.run_id,
                    "Cycle limit exceeded"
                    + self._routing_reason_suffix(
                        conn, token.run_id, token.step_id, resolver)
                    + f" (edges: {e})")
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

    def _handle_validation_failure(self, token: ClaimToken, error: str,
                                   *, promote_on_exhaustion: bool = False) -> bool:
        """Retry the step on a validation failure; decide what the LAST one means.

        Returns True only when the retry budget is spent AND the caller asked for
        ``promote_on_exhaustion`` — the caller must then let the step complete
        (lifecycle hooks promote ``{step}.tmp/``) with ``validation_failed: true``
        in its result flags. Every earlier attempt behaves exactly as before:
        status back to 'pending' with ``_validation_error`` in the inputs.

        Exhaustion used to be a permanent failure, which threw away everything the
        step had staged — the tmp dir is never promoted, so a hundred correct files
        die with the one file a validator objected to. Measured: a task card that
        required reproducing a test suite VERBATIM, fixtures with deliberate syntax
        errors included, made ``tool: lint`` unsatisfiable by construction — 16
        attempts over 4 step instances, 145 minutes, 74 correct files written and
        zero bytes delivered. Feeding the error back cannot help when the task
        requires the file the linter hates.

        So the engine stops making that call alone: it keeps the work, records the
        failure (flag + trace + notification + ``last_error``), and lets the graph
        decide. A reviewer with the task in hand can tell a defect from a required
        fixture; a linter cannot. Nothing is swallowed — a graph that wants the old
        hard stop matches the flag (``match: {validation_failed: true}`` to a repair
        step, or an ``end_conditions`` ``flag_match`` to fail the run).

        ``output_schema`` failures do NOT promote (caller passes the default): those
        are malformed step OUTPUTS, which downstream context resolution reads
        directly — there is no reviewer in between to judge them.
        """
        self.trace(token.run_id, "step", "validation_failed", {"error": error},
                   step_id=token.step_id, step_instance_id=token.step_instance_id)
        resolver = self._get_resolver_for_run(token.run_id)
        node = resolver.get_node(token.step_id)
        if not node:
            return False
        promoting = False
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
            elif promote_on_exhaustion:
                # Keep the work, record the failure. No version bump — confirm_step
                # is about to complete this same row with the token's version.
                promoting = True
                conn.execute(
                    """
                    UPDATE skillflow_steps
                    SET last_error = ?, updated_at = datetime('now')
                    WHERE id = ?
                    """,
                    (f"Output validation failed after {total_retries} retries "
                     f"(output promoted, flagged): {error}",
                     token.step_instance_id),
                )
                self.notifications.publish_sync(
                    "step_validation_exhausted",
                    {
                        "run_id": token.run_id, "step_id": token.step_id,
                        "error": error, "retry_count": total_retries,
                        "max_retries": max_allowed, "promoted": True,
                    },
                    step_id=token.step_id, run_id=token.run_id,
                )
            else:
                # Retry budget exhausted — permanent failure
                self._fail_step_in_tx(conn, token, f"Output validation failed: {error}", retryable=False)

        if promoting:
            # Trace AFTER the tx — self.trace commits on the same connection.
            self.trace(token.run_id, "step", "validation_exhausted",
                       {"error": error, "retry_count": total_retries,
                        "promoted": True, "flags": {"validation_failed": True}},
                       step_id=token.step_id, step_instance_id=token.step_instance_id)
            logging.getLogger("skillflow").warning(
                "step %r (run %s) still fails validation after %d retries — promoting "
                "its output with flag validation_failed=true instead of discarding it; "
                "route on that flag if this step must not proceed unchecked. %s",
                token.step_id, token.run_id, total_retries, error)
        return promoting

    def _validate_outputs(self, token: ClaimToken, node: StepNode) -> dict:
        """Run graph validation specs against draft outputs. Returns {passed, errors}."""
        if not self._workspace:
            return {"passed": True}
        pid = self._get_project_id(token.run_id)
        gname = self._get_graph_name(token.run_id)
        tmp_dir = self._workspace.get_step_tmp_dir(pid, gname, token.step_id)
        from skillflow.step_validation import StepValidator
        validator = StepValidator(self._tool_loader, tmp_dir, config_name=gname,
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
        row = None
        if self._workspace:
            row = self._conn.execute(
                "SELECT project_id, graph_name FROM skillflow_runs WHERE id = ?",
                (token.run_id,),
            ).fetchone()
            if row:
                # A loop-body step's output was just promoted to {step}/{item}/ —
                # $STEP_DIR must point THERE for on_deliver hooks (repo_apply /
                # repo_delete), or the repo would receive item-named subfolders
                # and _deletions.json would never be found.
                try:
                    _item = self._loop_item_for_step(
                        token.run_id, self._get_resolver_for_run(token.run_id),
                        token.step_id)
                except Exception:
                    _item = None
                params = self._workspace.resolve_variables(
                    row["project_id"], row["graph_name"], token.step_id, params,
                    item=_item,
                )
                params.setdefault("workspace_root",
                                  str(self._workspace.get_project_path(row["project_id"])))
                _cp = self._workspace.get_project_code_path(row["project_id"])
                # A repo-less run gets NO project_root, not an empty one.
                # `Path("").resolve()` is the process CWD, and the repo tools
                # resolve straight from this value: `repo_apply` does
                # `dst = Path(project_root).resolve()` (tools/repo_apply/impl.py),
                # as do `git_sync_pre` and `repo_validate`. So the value must not
                # arrive as "" — the danger is the empty STRING, not the key.
                #
                # Omitting buys exactly one thing, and it is worth being precise
                # about which: a tool whose parameter has NO DEFAULT can then not
                # be called at all, and `signature.bind` below turns that into a
                # named `ToolArgumentsUnavailable` (`git_sync_pre(project_root:
                # str)` is the one such tool here). For a tool that DEFAULTS the
                # parameter to "" — `repo_apply`, `repo_validate`, `pytest` —
                # omitting and passing "" are byte-identical inside the function,
                # and the refusal comes entirely from the tool's own guard. Those
                # guards are the safety; this line only avoids stepping on them.
                if _cp:
                    params.setdefault("project_root", str(_cp))

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
                # Inject run context so tools like repo_apply can build a
                # traceable commit message (config/graph + project + task). The
                # tool-STEP path injects these too; the lifecycle-hook path (this
                # one, used by on_deliver → repo_apply) historically did not, so
                # commits read a bare "step: work N file(s)". The kwarg filter
                # below drops any a given tool doesn't accept.
                if row:
                    if row["project_id"]:
                        params.setdefault("project_id", row["project_id"])
                    if row["graph_name"]:
                        params.setdefault("config_name", row["graph_name"])
                    try:
                        # Filtered by THIS step's own loop (any step type) — the
                        # old unfiltered LIMIT 1 could hand another loop's item
                        # to a multi-loop graph's hook.
                        _res = self._get_resolver_for_run(token.run_id)
                        _lid = _res.loop_of(token.step_id)
                        _it = self._current_item_of_loop(token.run_id, _lid) if _lid else None
                        if _it:
                            params.setdefault("task_name", _it)
                    except Exception:
                        pass
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

        if check_dir is None:
            # A run that owns no repository has nothing for after_deliver to
            # check. Reported per spec rather than as one blanket refusal: only
            # StepValidator honours a spec's own `on_failure`, and the caller
            # resolves the HOOK-level policy from `hook_spec` — which for
            # after_deliver is always a LIST, so `isinstance(hook_spec, dict)`
            # is False and it resolves to "fail". A single {"passed": False}
            # here therefore turns every after_deliver spec, including the ones
            # that declare `on_failure: "warn"`, into an unretryable step
            # failure. Routing each spec through its own declared policy keeps
            # that promise while still saying why nothing was checked.
            reason = ("after_deliver validation checks the code repository, "
                      "and this run declares none")
            errors, warnings = [], []
            for spec in check_specs:
                item = {"tool": spec.get("tool", "?"),
                        "files": spec.get("files", []), "error": reason}
                (warnings if spec.get("on_failure") == "warn"
                 else errors).append(item)
            result = {"passed": not errors, "errors": errors,
                      "warnings": warnings}
        else:
            from skillflow.step_validation import StepValidator
            validator = StepValidator(self._tool_loader, check_dir,
                                      config_name=gname,
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
        """Built-in: atomic rename tmp_dir → step_dir.

        A loop-body step promotes into a PER-ITEM folder ``{step}/{item}/`` so each
        iteration's output survives (the shared ``{step}/`` was replaced every
        iteration → an aggregator saw only the last item). Only THIS item's folder
        is removed on re-run, so sibling items are preserved. Non-loop steps keep
        the plain ``{step}/`` — byte-identical to before.
        """
        if not self._workspace:
            return {"passed": True}
        pid = self._get_project_id(token.run_id)
        gname = self._get_graph_name(token.run_id)
        resolver = self._get_resolver_for_run(token.run_id)
        item = self._loop_item_for_step(token.run_id, resolver, token.step_id)
        tmp_dir = self._workspace.get_step_tmp_dir(pid, gname, token.step_id)
        step_dir = self._workspace.get_step_dir(pid, gname, token.step_id, item=item)

        if not tmp_dir.exists() or not any(tmp_dir.iterdir()):
            return {"passed": True, "files": []}

        import shutil
        from skillflow.workspace import _sanitize_item
        # Collect files before moving; a loop-body step reports the item-prefixed
        # path so the write feedback / trace shows exactly where it landed. The
        # prefix uses the SANITIZED folder name — the path that actually exists
        # on disk (the raw item may contain '/'/CJK/etc. and never names a folder).
        prefix = f"{_sanitize_item(item)}/" if item else ""
        moved_files = []
        for f in sorted(tmp_dir.rglob("*")):
            if f.is_file():
                moved_files.append(prefix + str(f.relative_to(tmp_dir)))

        # Atomic: remove old {step}[/item] dir, rename tmp → it. The prior dir was
        # already committed to the artifact-history git by its own _step_commit,
        # so this rmtree loses no history when artifact_history is on.
        if step_dir.exists():
            shutil.rmtree(str(step_dir))
        step_dir.parent.mkdir(parents=True, exist_ok=True)
        if item:
            # GC pre-per-item leftovers: files sitting FLAT in {step}/ came from a
            # promotion before this step was item-keyed (old layout wiped the whole
            # dir each round, so flat files are always stale). Left in place they'd
            # be double-read by every all-items aggregation. Item FOLDERS are kept —
            # sibling items' outputs are exactly what per-item promotion preserves.
            for stale in step_dir.parent.iterdir() if step_dir.parent.is_dir() else ():
                if stale.is_file():
                    try:
                        stale.unlink()
                    except OSError:
                        pass
        os.rename(str(tmp_dir), str(step_dir))

        if self._artifact_history:
            self._artifact_commit(pid, gname, token.step_id, token.run_id)

        return {"passed": True, "files": moved_files}

    # ── Artifact history (opt-in git versioning of promoted step outputs) ──
    def _artifact_commit(self, pid: str, config_name: str, step_id: str,
                         run_id: str = "", *, rel: str = "",
                         msg_prefix: str = "") -> None:
        """Commit a just-promoted step output dir to the workspace git repo.

        Preserves every iteration of a step's output across goal-loop re-runs
        (which overwrite {step}/) so they stay recoverable for tracing. The repo
        lives at the workspace root; volatile files (staging {*.tmp}/, trace DBs)
        are gitignored. Entirely best-effort — never raises into the run path.

        ``rel`` overrides the staged path (defaults to ``{config}/{step}``) so the
        same plumbing can version a sibling artifact — e.g. the accumulating
        checkpoint-feedback log at ``{config}/_feedback/{step}.md``.
        """
        import subprocess
        try:
            root = self._workspace.get_project_path(pid)
            if not root or not root.is_dir():
                return
            env = {**os.environ, "GIT_TERMINAL_PROMPT": "0",
                   "GIT_AUTHOR_NAME": "skillflow", "GIT_AUTHOR_EMAIL": "skillflow@localhost",
                   "GIT_COMMITTER_NAME": "skillflow", "GIT_COMMITTER_EMAIL": "skillflow@localhost"}
            def _git(*args, check=False):
                return subprocess.run(["git", *args], cwd=str(root), env=env,
                                      capture_output=True, text=True, check=check)
            if not (root / ".git").is_dir():
                _git("init", "-q")
            # Keep volatile/large files out of history (idempotent).
            gi = root / ".gitignore"
            want = ["*.tmp/", "trace.db", "trace.db-*", "*.db-wal", "*.db-shm",
                    "__pycache__/", "*.pyc"]
            have = gi.read_text(encoding="utf-8").splitlines() if gi.is_file() else []
            missing = [w for w in want if w not in have]
            if missing:
                gi.write_text("\n".join(have + missing).strip() + "\n", encoding="utf-8")
                _git("add", ".gitignore")
            # Stage just this artifact (one commit == one step output / feedback round).
            rel = rel or f"{config_name}/{step_id}"
            _git("add", "--", rel)
            # Skip an empty commit (nothing staged → step wrote nothing new).
            if _git("diff", "--cached", "--quiet").returncode == 0:
                return
            msg = f"{msg_prefix}{rel}" + (f" @ {run_id[:8]}" if run_id else "")
            _git("commit", "-q", "-m", msg)
        except Exception:
            pass  # best-effort: artifact history must never break a run

    # ── Checkpoint-feedback log (persisted, appended, git-historized) ──────
    # Reject feedback used to be a SCALAR (_feedback in inputs_json), overwritten
    # on every reject — so consecutive rounds of user feedback drifted: the step
    # re-ran seeing only the LATEST round and silently dropped earlier requests.
    # Instead, each round is APPENDED to a per-step log that lives BESIDE the step
    # dir (a step dir is rmtree'd on every re-run, so an in-dir log would be
    # wiped). The full log is injected into the re-run's prompt, and versioned in
    # the artifact-history git repo for an audit trail of what was asked, when.
    def _feedback_log_path(self, pid: str, config_name: str, step_id: str):
        if not self._workspace:
            return None
        from skillflow.context import feedback_log_path
        return feedback_log_path(
            self._workspace.get_config_path(pid, config_name), step_id)

    def _read_feedback_log(self, pid: str, config_name: str, step_id: str):
        """Full accumulated log WITH the read-contract preamble (see
        context.FEEDBACK_LOG_PREAMBLE — quotes are the complained-about OLD
        text, not text to reproduce). Shared with the ``{feedback_of: step}``
        context source so both injection paths carry the same contract."""
        if not self._workspace:
            return None
        from skillflow.context import read_feedback_log
        return read_feedback_log(
            self._workspace.get_config_path(pid, config_name), step_id)

    def _append_feedback_log(self, pid: str, config_name: str, step_id: str,
                             feedback, run_id: str = "") -> None:
        """Append one round of checkpoint feedback to the step's persisted log.

        Best-effort — feedback history must never break a run. Called AFTER the
        reject transaction commits (git is a subprocess; keep it off the write
        lock)."""
        p = self._feedback_log_path(pid, config_name, step_id)
        if not p:
            return
        try:
            import datetime as dt
            text = feedback if isinstance(feedback, str) else self._serialize(feedback)
            if not (text or "").strip():
                return
            p.parent.mkdir(parents=True, exist_ok=True)
            existing = p.read_text(encoding="utf-8") if p.is_file() else ""
            n = existing.count("## 反馈轮 #") + 1
            ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            entry = f"## 反馈轮 #{n} · {ts}\n\n{text.strip()}\n"
            p.write_text((existing + ("\n\n" if existing.strip() else "") + entry),
                         encoding="utf-8")
            if self._artifact_history:
                self._artifact_commit(pid, config_name, step_id, run_id,
                                      rel=f"{config_name}/_feedback/{step_id}.md",
                                      msg_prefix="feedback: ")
        except Exception:
            pass

    def step_output_versions(self, project_id: str, config_name: str,
                             step_id: str) -> list[dict]:
        """Return the artifact-history commits that touched a step's output dir,
        newest first: ``[{commit, timestamp, message}]``. Empty if history is
        off / no git / no versions. Recover a version with
        ``git show <commit>:<config_name>/<step_id>/<file>`` at the workspace root.
        """
        import subprocess
        out: list[dict] = []
        try:
            if not self._workspace:
                return out
            root = self._workspace.get_project_path(project_id)
            if not root or not (root / ".git").is_dir():
                return out
            rel = f"{config_name}/{step_id}"
            res = subprocess.run(
                ["git", "log", "--pretty=%H%x1f%cI%x1f%s", "--", rel],
                cwd=str(root), capture_output=True, text=True)
            for line in res.stdout.splitlines():
                parts = line.split("\x1f")
                if len(parts) == 3:
                    out.append({"commit": parts[0], "timestamp": parts[1],
                                "message": parts[2]})
        except Exception:
            pass
        return out

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
        # The step-level failure itself left NO mark on the durable trace (no
        # failure path traces; outbox rows are drained + deleted), so a run read
        # as "hook failed … next step claimed" — the step silently never
        # completing, and the `_error` edge it took, both invisible. Traced AFTER
        # the tx: self.trace commits on the same connection.
        try:
            routed_to = self._get_resolver_for_run(
                token.run_id).find_error_transition(token.step_id)
        except Exception:
            routed_to = None
        self.trace(token.run_id, "step", "failed",
                   {"error": error, "routed_to": routed_to},
                   step_id=token.step_id, step_instance_id=token.step_instance_id)

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
        # A zombie must not spend the replacement's retry budget, nor overwrite
        # a live claim with `pending`. (The CAS in _fail_step_in_tx re-reads
        # `version` from the row it is about to write, so it never detects a
        # reclaim on its own.)
        self._assert_epoch(token, "fail_step")
        self._release_step_tools(token.run_id, token.step_id,
                                 token.step_instance_id, token.claim_epoch)
        with self._tx() as conn:
            self._fail_step_in_tx(conn, token, error, retryable)

    # How many times one step instance may be released back to pending before
    # the releases are treated as the problem. Same shape and same reason as the
    # reaper's `_stale_recovery_count`: a cause that recurs forever would
    # otherwise re-run the step forever, at full LLM cost, with nothing said.
    MAX_CLAIM_RELEASES = 3

    def release_claim(self, token: ClaimToken, reason: str) -> dict:
        """Return a claimed step to `pending` WITHOUT spending its retry budget.

        For when the step did not fail — its EXECUTOR went away. A driver whose
        task is cancelled (a disconnected client, an ended session) has to hand
        the claim back or the row stays `claimed` forever: the stale-claim reaper
        refuses to reclaim a claim whose owner PROCESS is alive, and a server's
        own driver dies without the server dying.

        `fail_step(retryable=True)` is the wrong tool for that. It increments
        `retry_count`, so three cancellations exhaust a healthy step's budget and
        kill it with an error that blames the step for something the client did.
        Retries are for a step that ran and was wrong; this is for a step that
        never got its answer heard.

        **A cancellation never touches `retry_count` and never ends a run.**
        The retry budget is for failures that are the STEP's; spending it on
        client disconnects means a step quietly loses its resilience to real
        ones, and then dies on the next genuine transient error — destroying the
        staged output this method exists to protect. Two earlier shapes of this
        code did exactly that, by different routes.

        Releases are counted in the `release_count` COLUMN, monotonically. That
        count is the RECORD: it is what tells an operator afterwards that nine
        disconnects, not one bad step, is what happened here. Every
        `MAX_CLAIM_RELEASES`-th release logs a warning, because re-running a
        step is expensive and a repeatedly-killed driver needs looking at — but
        a warning is how you say that, not a dead run.

        A column rather than a key in `inputs_json`: a re-claim
        rebuilds that dict from freshly resolved context and preserves only
        `_error`/`_validation_error`/`_feedback`, so a counter kept there is
        erased by the very event it is counting.

        Returns ``{released, releases, failed}``.
        """
        self._assert_epoch(token, "release_claim")
        self._release_step_tools(token.run_id, token.step_id,
                                 token.step_instance_id, token.claim_epoch)
        with self._tx() as conn:
            row = conn.execute(
                "SELECT status, release_count FROM skillflow_steps WHERE id = ?",
                (token.step_instance_id,)).fetchone()
            if not row or row["status"] != "claimed":
                # Already resolved by someone else — nothing to hand back, and
                # rewriting it to pending would undo their work.
                return {"released": False, "releases": 0, "failed": False,
                        "note": f"step is {row['status'] if row else 'missing'}"}
            releases = (row["release_count"] or 0) + 1
            # A cancellation NEVER touches `retry_count`, and never ends the
            # run. Two earlier shapes of this branch both did, by different
            # routes: `retryable=False` failed the run outright, and
            # `retryable=True` still failed it once the budget was gone,
            # because `_fail_step_in_tx` reads `if retryable and retry_count <
            # max_retries`. Charging them to the budget at all is the mistake
            # under both — nine client disconnects would spend a step's whole
            # allowance, and the next GENUINE transient failure killed the run
            # and destroyed the staged output this method exists to protect.
            # Worse, the reset that protected the run also erased the evidence:
            # the row afterwards read `retry_count 3/3, release_count 0` with
            # one real error, and nothing recorded the nine disconnects.
            #
            # So: release, always. `release_count` is monotonic and is the
            # record. The budget stays for failures that are actually the
            # step's. Re-running is expensive, so every cap-multiple says so
            # loudly — an operator with a repeatedly-cancelled step needs to
            # know, and losing a run is never the way to tell them.
            if releases % self.MAX_CLAIM_RELEASES == 0:
                logging.getLogger("skillflow").warning(
                    "step '%s' (instance %s) has now been released by its "
                    "executor %s times without completing. Something is "
                    "killing this step's driver; each release re-runs the step "
                    "in full. The step's retry budget is deliberately NOT "
                    "being charged for this.",
                    token.step_id, token.step_instance_id, releases)
            conn.execute(
                """
                UPDATE skillflow_steps
                SET status = 'pending', version = version + 1,
                    claimed_at = NULL, claimed_by = NULL,
                    last_error = ?, release_count = ?,
                    updated_at = datetime('now')
                WHERE id = ? AND status = 'claimed'
                """,
                (reason, releases, token.step_instance_id))
        return {"released": True, "releases": releases, "failed": False}

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
        # Capability context: a `capability` keyword on this tool step hands the
        # tool framework-selected values (e.g. a durable state_dir) so the tool
        # never picks its own path. setdefault → explicit tool_params still win.
        # Prefer what the claim resolved; fall back to the node's own static
        # declaration for a tool step that never went through an agent claim.
        for _ck, _cv in self._capability_context(
                tool_node, graph_name,
                offers=getattr(self._graph_for_run(run_id) if run_id
                               else self._graphs.get(graph_name),
                               "capabilities", None),
                names=self._granted_capabilities(run_id or "", tool_node.id)
                or None).items():
            kwargs.setdefault(_ck, _cv)
        # Resolve $STEP_DRAFT_DIR etc. via workspace
        if self._workspace and run_id:
            try:
                row = self._conn.execute(
                    "SELECT project_id FROM skillflow_runs WHERE id = ?", (run_id,)
                ).fetchone()
                if row and row["project_id"]:
                    pid = row["project_id"]
                    kwargs.setdefault("project_id", pid)
                    # Look up current task name from loop state (for commit
                    # messages / register_tool). Filtered by THIS tool step's own
                    # loop — the old unfiltered LIMIT 1 was multi-loop-ambiguous.
                    try:
                        _lid = (self._get_resolver_for_run(run_id) if run_id
                                else self._get_resolver(graph_name)).loop_of(tool_node.id)
                        _it = self._current_item_of_loop(run_id, _lid) if _lid else None
                        if _it:
                            kwargs.setdefault("task_name", _it)
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
                        _cp = self._workspace.get_project_code_path(pid)
                        if _cp:
                            kwargs["project_root"] = str(_cp)
                        else:
                            # Repo-less: drop the "" placeholder seeded above
                            # rather than pass it on. `Path("").resolve()` is the
                            # process CWD and repo_apply/git_sync_pre/
                            # repo_validate each resolve this value directly.
                            #
                            # What the pop actually buys: a tool whose parameter
                            # is REQUIRED cannot be called, and the caller sees
                            # that as a TypeError instead of running against the
                            # CWD. For a tool that defaults it to "" the pop
                            # changes nothing the function can observe — those
                            # refuse by their own guard, which is where the
                            # safety on this path lives.
                            kwargs.pop("project_root", None)
            except Exception:
                # Best-effort default-filling, but a failure here leaves a tool
                # without workspace_root/project_root → it misfires later with a
                # confusing error. Log so the real cause is visible.
                import logging
                logging.getLogger("skillflow").warning(
                    "tool arg resolution (workspace/project root) failed",
                    exc_info=True)
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
        sig = None
        try:
            sig = _inspect.signature(fn)
            if not any(p.kind == _inspect.Parameter.VAR_KEYWORD
                       for p in sig.parameters.values()):
                kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
        except (ValueError, TypeError):
            sig = None
        if sig is not None:
            # Does the call even BIND? A tool that declares an argument the
            # engine could not supply — `git_sync_pre(project_root: str)` on a
            # run whose code-path resolver answers "no code repository", where
            # project_root is deliberately omitted above — would otherwise raise
            # a bare TypeError out of `fn(**kwargs)` below, and the caller's
            # generic `except` would reopen the step to pending and re-raise. It
            # is bounded (`_reopen_tool_step_in_tx` fails the run on the third
            # crash) but it spends three ticks reproducing a failure that cannot
            # change, and the diagnosis lands in a traceback rather than in the
            # run's error.
            #
            # So: name the missing argument, and raise a DETERMINISTIC-failure
            # exception the caller fails the step and run on directly. NOT an
            # error dict — `_confirm_tool_in_tx` marks a tool step 'completed'
            # whatever the result contains and nothing turns an `error` key into
            # a routing flag, so returning one records a step that never ran as
            # completed and sends the run down this node's first edge.
            try:
                sig.bind(**kwargs)
            except TypeError as exc:
                err = (f"{tool_node.tool_name}: cannot be called with the "
                       f"arguments available at this step ({exc}). Supplied: "
                       f"{sorted(kwargs)}.")
                self.trace(run_id, "tool_result", tool_node.tool_name,
                           {"source": "tool_step", "error": err},
                           step_id=tool_node.id)
                raise ToolArgumentsUnavailable(err)
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
                    inputs_json, outputs_json, result_flags_json, completion_seq,
                    completed_at, created_at, updated_at)
                VALUES (?, ?, '{}', 'completed', 1, '{}', ?, ?,
                    (SELECT COALESCE(MAX(completion_seq), 0) + 1
                     FROM skillflow_steps WHERE run_id = ?),
                    datetime('now'), datetime('now'), datetime('now'))
                """,
                (run_id, step_id, self._serialize(result), self._serialize(result),
                 run_id),
            )
        else:
            conn.execute(
                """
                UPDATE skillflow_steps
                SET status = 'completed', version = version + 1,
                    outputs_json = ?, result_flags_json = ?,
                    completion_seq = (SELECT COALESCE(MAX(completion_seq), 0) + 1
                                      FROM skillflow_steps WHERE run_id = ?),
                    completed_at = datetime('now'), updated_at = datetime('now')
                WHERE id = ? AND version = ?
                """,
                (self._serialize(result), self._serialize(result), run_id,
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
        """Inject feedback into the target step's NEWEST instance.

        This used to require ``status = 'pending'``, which silently dropped the
        feedback on the case that needs it most: a BACKWARD loop-back (a tool gate
        routing to a maker that already ran). That maker's instance is `completed`
        and its next one does not exist yet — the claim path inserts it afterwards —
        so the UPDATE matched zero rows and the maker re-ran blind. Observed on a
        pipeline_forge run: seven emit attempts producing three identical gate
        failures, and not one of the maker's four step instances carrying `_feedback`.

        Targeting the newest instance always hits a row (``create_run`` pre-creates
        one per step) and pairs with the carry-forward in the claim path, which hands
        ``_feedback`` to the fresh instance created for the re-run.
        """
        conn.execute(
            """
            UPDATE skillflow_steps
            SET inputs_json = json_set(inputs_json, '$._feedback', ?),
                updated_at = datetime('now')
            WHERE id = (SELECT id FROM skillflow_steps
                        WHERE run_id = ? AND step_id = ?
                        ORDER BY id DESC LIMIT 1)
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
        # Flatten at any depth (e.g. execution_order is a list of lists)
        items = _flatten_loop_items(items)
        return items, False

    def _loop_body_nodes(self, resolver, loop_step_id) -> set[str]:
        """The set of node ids that form a loop's body. Delegates to the
        resolver's cached reach-back topology (graph.loop_body_map): a body node
        must be reachable from the loop's body entry AND able to reach back to
        the loop — give-up/drain targets are NOT body (they run once, post-loop).
        """
        return set(resolver.loop_bodies().get(loop_step_id, ()))

    def _current_item_of_loop(self, run_id: str, loop_id: str) -> str | None:
        """The named loop's current item (filtered — multi-loop graphs never leak
        another loop's item). RLock is reentrant: safe inside a transaction."""
        with self._lock:
            row = self._conn.execute(
                "SELECT current_item FROM skillflow_loop_state "
                "WHERE run_id = ? AND loop_step_id = ?",
                (run_id, loop_id)).fetchone()
        return (row["current_item"] if row else None) or None

    def _loop_item_for_step(self, run_id: str, resolver, step_id: str) -> str | None:
        """Current loop item if ``step_id`` is an AGENT body step of a loop, else
        None. Used to key a body step's output dir per-item (``{step}/{item}/``) so
        each iteration's output SURVIVES — the shared ``{step}/`` was replaced every
        iteration, so an aggregator saw only the last item.

        AGENT-only on purpose: only agent steps go through tmp→promotion, so only
        they get per-item folders. A loop-body TOOL step writes flat via $STEP_DIR
        with no promotion — keying its reads/transition-matching per-item would
        point at folders that never exist. Tool body steps keep the pre-1.5.23
        flat, overwritten-per-iteration semantics. O(1) via the resolver's cached
        step→loop map.
        """
        loop_id = resolver.loop_of(step_id)
        if not loop_id:
            return None
        node = resolver.get_node(step_id)
        if not node or node.step_type != "agent":
            return None
        return self._current_item_of_loop(run_id, loop_id)

    def _gc_dropped_item_dirs(self, pid: str, gname: str, loop_step_id: str,
                              items: list, run_id: str | None = None) -> None:
        """Remove this loop's AGENT body steps' per-item folders whose item is no
        longer in the manifest (best-effort; artifact history keeps every promoted
        iteration recoverable). Called only when the manifest actually changed."""
        import shutil
        from skillflow.workspace import _sanitize_item
        try:
            # Which steps are loop BODY steps decides what gets deleted, so read
            # it from the graph this run is pinned to.
            resolver = (self._get_resolver_for_run(run_id) if run_id
                        else self._get_resolver(gname))
        except Exception:
            return
        keep = {_sanitize_item(i) for i in items}
        for sid in resolver.loop_bodies().get(loop_step_id, ()):
            n = resolver.get_node(sid)
            if not n or n.step_type != "agent":
                continue
            d = self._workspace.get_step_dir(pid, gname, sid)
            if not d.is_dir():
                continue
            for sub in d.iterdir():
                if sub.is_dir() and sub.name not in keep:
                    shutil.rmtree(str(sub), ignore_errors=True)

    def _enrich_write_path(self, run_id: str, step_id: str, result):
        """Add a workspace-relative ``path`` to a write-tool result so the agent
        (and the trace) sees WHERE the file actually landed —
        ``{step}[/{item}]/{file}`` — the location later steps read it from. Once
        write-location and read-location diverge (per-item folders, staging→
        promotion, repo vs step dir), an opaque ``{"written": "x.json"}`` hides a
        persist-here/load-there mismatch; this makes it visible.
        """
        if not isinstance(result, dict) or result.get("error"):
            return result
        name = (result.get("written") or result.get("edited")
                or result.get("created"))
        if not name:
            return result
        try:
            resolver = self._get_resolver_for_run(run_id)
            item = self._loop_item_for_step(run_id, resolver, step_id)
        except Exception:
            item = None
        if item:
            # Report the SANITIZED folder — the path that actually exists (the
            # raw item may contain '/'/CJK/etc. and never names a folder).
            from skillflow.workspace import _sanitize_item
            result["path"] = f"{step_id}/{_sanitize_item(item)}/{name}"
        else:
            result["path"] = f"{step_id}/{name}"
        result.setdefault("note", "staged; readable by later steps at this path")
        return result

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
                # A silent empty set here re-runs already-completed loop items.
                import logging
                logging.getLogger("skillflow").warning(
                    "failed to deserialize loop completed_items; "
                    "completed items may re-run", exc_info=True)
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
            # Flattened on the way out too: a cache written by a pre-1.5.33
            # engine can still hold the nested shape that crashed `set(items)`.
            items = (_flatten_loop_items(self._deserialize(row["items_json"]))
                     if row["items_json"] else [])

        def _route_done():
            conn.execute(
                "UPDATE skillflow_steps SET status = 'completed', "
                "completion_seq = (SELECT COALESCE(MAX(completion_seq), 0) + 1 "
                "                  FROM skillflow_steps WHERE run_id = ?), "
                "completed_at = datetime('now'), updated_at = datetime('now') "
                "WHERE run_id = ? AND step_id = ? AND status = 'pending'",
                (run["id"], run["id"], loop_step_id),
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
                    # A silent empty set here re-runs already-completed items.
                    import logging
                    logging.getLogger("skillflow").warning(
                        "failed to deserialize loop completed_items; "
                        "completed items may re-run", exc_info=True)
            current_item = row["current_item"] or None
        # Drop superseded names from prior goal-loop rounds so completed_items
        # reflects only the active manifest (prevents the len(completed) overcount
        # and the scheduler's idx-out-of-range).
        completed &= set(items)
        # Disk GC mirroring that DB reconciliation: when the manifest CHANGED (a
        # goal-loop round regenerated it), remove body steps' per-item folders for
        # items no longer in it — otherwise an all-items aggregation keeps
        # reporting on dropped items forever (per-item promotion removed the old
        # wipe-the-whole-dir GC).
        if row is not None and self._workspace and row["items_json"]:
            try:
                _old = set(_flatten_loop_items(
                    self._deserialize(row["items_json"]) or []))
            except Exception:
                _old = set()
            if _old and _old - set(items):
                self._gc_dropped_item_dirs(run["project_id"], run["graph_name"],
                                           loop_step_id, items, run["id"])

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
            run["project_id"], run["graph_name"], step_id, run_id=run_id
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

    def _routing_reason_suffix(self, conn, run_id: str, step_id: str,
                               resolver, file_reader=None) -> str:
        """Suffix for a terminal routing failure: the reason, not just the edge.

        Appended to "Cycle limit exceeded" / "No matching transition …" so the
        error_reason carries what the routing file says instead of leaving it
        in the workspace for nobody to find. Returns "" when there is nothing
        to add, and never raises.
        """
        try:
            node = resolver.get_node(step_id)
            if node is None:
                return ""
            if file_reader is None:
                run = conn.execute(
                    "SELECT project_id, graph_name FROM skillflow_runs WHERE id = ?",
                    (run_id,),
                ).fetchone()
                if not run:
                    return ""
                file_reader = self._make_file_reader(
                    run["project_id"], run["graph_name"], step_id, run_id=run_id)
            detail = _routing_reason(node, file_reader)
            return f" — {detail}" if detail else ""
        except Exception:
            return ""

    def _make_file_reader(self, project_id: str, graph_name: str,
                          step_id: str, run_id: str = "") -> callable | None:
        """Return a callable for resolving from_file match conditions.

        Reads from the step's promoted output directory ({step_id}/) where
        _step_commit has atomically moved validated outputs. For a loop-body step
        (whose output is per-item at {step_id}/{item}/) the CURRENT item's folder
        is read — so a transition matching a looped reviewer's `review_verdict.json`
        finds this iteration's verdict, not a sibling's. (RLock is reentrant, so
        the loop-state lookup is safe even inside a transaction.)
        """
        if not self._workspace:
            return None
        item = None
        if run_id:
            try:
                resolver = self._get_resolver_for_run(run_id)
                item = self._loop_item_for_step(run_id, resolver, step_id)
            except Exception:
                item = None
        step_dir = self._workspace.get_step_dir(project_id, graph_name, step_id,
                                                item=item)
        def read(path: str) -> str:
            f = step_dir / path
            if not f.exists():
                raise FileNotFoundError(f"Output file not found: {path}")
            return f.read_text(encoding="utf-8")
        return read

    @staticmethod
    def _tool_step_errored(node, tool_result: dict) -> str | None:
        """The message a tool step must FAIL on, or None to carry on.

        A tool step's result is also its routing flags, so without this the
        engine takes whatever edge matched and records the step 'completed'
        whatever the tool said. For a plumbing tool that is the malignant shape:
        a refusal returns ``{"applied": False, "error": …}``, which is a
        perfectly serviceable set of routing flags, so the run goes down this
        node's first edge and reports success having done none of the work.

        This is a DEFENCE for a shape a graph author can write, not a failure
        that was observed. `repo_apply` and `repo_validate` — the two tools whose
        refusal would look like that — appear in every shipped config only as
        ``lifecycle: on_deliver`` hooks, and `_execute_tool_hook` has mapped a
        truthy ``error`` to ``passed: False`` since long before either tool grew
        a refusal branch. A ``step_type: tool`` node naming one is legal, and
        nothing stopped it from being written.

        The signature-level guard (`ToolArgumentsUnavailable`) does not cover it.
        That fires on `signature.bind`, so it only catches a tool whose parameter
        is REQUIRED — `git_sync_pre(project_root: str)`. The three tools that
        default it (`repo_apply`, `repo_validate`, `pytest`) bind fine and refuse
        in their own body instead, which is a returned dict, not a raise.

        DECLARED, never inferred, because each inference rule mis-classifies a
        real config — both of these are shipped and deliberate:

        * "truthy error always fails" would kill a GATE tool step whose verdict is
          an error. `forge_lint` returns ``{passed: false, error: <lint issues>}``
          and pipeline_forge routes it back to the emitter on ``passed: false``.
        * "fails only if the edge it took carried no ``match``" would kill a step
          whose author tolerates the error on purpose. AItelier's `git_push_post`
          has one unconditional edge to `done` because a failed push must not fail
          a run whose work `repo_apply` already committed locally — and its target
          is single, so the tolerance cannot be spelled as a second matched edge
          either (two edges A→B are legal, but say nothing the author means).

        So the NODE says: ``tool_error: "route"`` for both of those, the "fail"
        default for everything else. `passed` is deliberately NOT consulted — a
        gate returning ``passed: false`` is routing, not failing, and the node
        already had to opt into "route" to return an error at all.
        """
        if not isinstance(tool_result, dict):
            return None
        if getattr(node, "tool_error", "fail") == "route":
            return None
        error = tool_result.get("error")
        if not error:
            return None
        return (f"{getattr(node, 'tool_name', '?')}: {error}"
                if getattr(node, "tool_name", "") else str(error))

    def _complete_tool_step(self, run_id: str, step_id: str,
                            tool_result: dict, run_row: dict,
                            resolver) -> str | None:
        """Confirm an inline tool execution and resolve its transition.

        Called AFTER _execute_tool_inline returns, inside a fresh _tx()
        so the write lock is only held during the fast DB update, not
        during the (potentially slow) tool itself.

        A truthy ``error`` in the result is a STEP FAILURE unless the node
        declares ``tool_error: "route"`` — see `_tool_step_errored`. Checked
        BEFORE `_confirm_tool_in_tx`, which writes ``status = 'completed'``
        unconditionally: a tool step that did none of its work would otherwise
        be recorded completed, the run would take this node's first edge, and it
        would report success having shipped nothing. A defence for a writable
        shape — no shipped config puts a refusing plumbing tool on a
        ``step_type: tool`` node; see `_tool_step_errored`.
        """
        node = resolver.get_node(step_id)
        err = self._tool_step_errored(node, tool_result)
        if err is not None:
            self.trace(run_id, "tool_result", getattr(node, "tool_name", ""),
                       {"source": "tool_step", "error": err,
                        "tool_error": "fail"},
                       step_id=step_id)
            self._fail_tool_step_in_tx(run_id, step_id, err)
            return None
        with self._tx() as conn:
            self._confirm_tool_in_tx(conn, run_id, step_id, tool_result)
            step_flags = tool_result
            if node is not None and getattr(node, "tool_error", "fail") == "route":
                # The node said an error is a legitimate outcome here, so make it
                # ROUTABLE. Matching `error` itself is not usable: it holds a
                # free-text message, so a graph cannot write an edge for "it
                # errored" without knowing the wording. A copy — never mutate the
                # result the row already stores.
                step_flags = dict(tool_result)
                step_flags["_tool_error"] = bool(tool_result.get("error"))
            fr = self._make_file_reader(
                run_row["project_id"], run_row["graph_name"], step_id,
                run_id=run_id)
            edge_counts = self._read_edge_counts(conn, run_id)
            try:
                _t, target = resolver.resolve_transition(
                    step_id, step_flags, edge_counts, file_reader=fr)
            except CycleLimitExceeded:
                self._fail_run_in_tx(
                    conn, run_id,
                    "Cycle limit exceeded"
                    + self._routing_reason_suffix(
                        conn, run_id, step_id, resolver, file_reader=fr))
                return None
            if _t and _t.feedback and _t.to:
                self._inject_feedback_in_tx(
                    conn, run_id, _t.to, _describe_tool_failure(tool_result))
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
            # Checkpoint on a TOOL step: the `from: checkpoint` edge matches on
            # _checkpoint_approved, which the tool's own flags never carry, so
            # `target` is None here — but this is a pause point, not a dead end.
            # The agent completion path (advance_run) pauses on checkpoints;
            # mirror it here so a tool step can carry a checkpoint too (e.g. a
            # `restage` gate that stages a maker's output for human review after
            # an intervening reviewer). Without this the run would fall through to
            # _fail_run_in_tx("No matching transition …") below.
            ckpt_node = resolver.get_node(step_id)
            if ckpt_node and ckpt_node.checkpoint:
                ckpt_target = next(
                    (t.to for t in ckpt_node.transitions
                     if t.match and t.match.get("from") == "checkpoint"), None)
                if ckpt_target is not None:
                    conn.execute(
                        "UPDATE skillflow_runs SET current_node = ?, status = 'paused',"
                        " updated_at = datetime('now') WHERE id = ?",
                        (ckpt_target, run_id))
                    _lbl = ckpt_node.checkpoint_label or ckpt_node.name or step_id
                    self.notifications.publish_sync(
                        "checkpoint_paused",
                        {"step_id": step_id, "label": _lbl, "next_node": ckpt_target,
                         "project_id": run_row["project_id"],
                         "graph_name": run_row["graph_name"]},
                        step_id=step_id, run_id=run_id)
                    self.trace(run_id, "step", "checkpoint_paused",
                               {"step_id": step_id, "label": _lbl,
                                "next_node": ckpt_target}, step_id=step_id)
                    return None
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
                + self._routing_reason_suffix(
                    conn, run_id, step_id, resolver, file_reader=fr)
            )
            return None

    def _loop_item_in_tx(self, conn, resolver, run_id: str, step_id: str) -> str | None:
        """The loop item this step is being claimed FOR, or None outside a loop.

        Read at claim, which is when it is true: the loop advances
        ``current_item`` before releasing its body, and the same value already
        drives per-item write promotion and read routing. Stamping it on the row
        is what makes a body instance attributable afterwards — see
        SKILLFLOW_STEPS.loop_item.

        Unlike the read-routing map in ``claim_next_step``, this does NOT filter
        to agent steps. That filter is about promotion (tool body steps write
        flat), which has nothing to do with which item a tool step ran for.

        Never raises: attribution is metadata, and a claim must not fail because
        of it.
        """
        try:
            loop_id = resolver.loop_of(step_id)
            if not loop_id:
                return None
            row = conn.execute(
                "SELECT current_item FROM skillflow_loop_state "
                "WHERE run_id = ? AND loop_step_id = ?",
                (run_id, loop_id),
            ).fetchone()
            item = row["current_item"] if row else None
            if item is None or item == "":
                return None
            return item if isinstance(item, str) else self._serialize(item)
        except Exception:
            logging.getLogger("skillflow").debug(
                "loop_item not resolved for %s/%s", run_id, step_id, exc_info=True)
            return None

    def _claim_tool_step_in_tx(self, run_id: str, step_id: str, node,
                               resolver=None) -> int | None:
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
                "claimed_at = ?, claimed_by = ?, "
                "claim_epoch = COALESCE(claim_epoch, 0) + 1, "
                "loop_item = ?, "
                "updated_at = datetime('now') "
                "WHERE run_id = ? AND step_id = ? AND version = ? AND status = 'pending'",
                (claimed_at_str, worker_identity("tool-inline"),
                 self._loop_item_in_tx(conn, resolver, run_id, step_id)
                 if resolver is not None else None,
                 run_id, step_id, ver),
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

    def _fail_tool_step_in_tx(self, run_id: str, step_id: str,
                              error: str) -> None:
        """Fail a claimed tool step AND its run, with no retry.

        The sibling of `_reopen_tool_step_in_tx`, for a failure that a retry
        cannot change. Two callers, and both are about a tool that did not do the
        step's work:

        * the tool's signature cannot bind the arguments this step has
          (`ToolArgumentsUnavailable`) — the same graph, step and signature next
          tick, so three reopens reproduce it exactly three times;
        * the tool ran and reported a truthy ``error`` on a node that did not
          declare ``tool_error: "route"`` (`_tool_step_errored`). The errors that
          reach here are of the kind a re-tick cannot change — a missing project
          root, a git command that refuses — which is why this path retries
          nothing. No shipped config reaches it: it is a defence for a shape a
          graph author can write (see `_tool_step_errored`), not a failure that
          was observed.

        Confirming either would mark a step that did nothing 'completed':
        `_confirm_tool_in_tx` writes that status unconditionally, so the run
        would take this node's first edge and report success.

        The RUN is failed, not just the step: marking only the step failed leaves
        `advance_run` free to re-resolve the predecessor's transition and open a
        fresh instance of the same tool node (the reasoning `_reopen_tool_step_in_tx`
        records for its own 3-crash cap).
        """
        with self._tx() as conn:
            row = conn.execute(
                "SELECT id FROM skillflow_steps WHERE run_id = ? AND "
                "step_id = ? AND status = 'claimed' ORDER BY id DESC LIMIT 1",
                (run_id, step_id),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE skillflow_steps SET status = 'failed', "
                    "version = version + 1, last_error = ?, claimed_at = NULL, "
                    "claimed_by = NULL, updated_at = datetime('now') "
                    "WHERE id = ?",
                    (error, row["id"]),
                )
            self._fail_run_in_tx(conn, run_id, error)
            self.notifications.publish_sync(
                "step_failed",
                {"run_id": run_id, "step_id": step_id, "error": error,
                 "retryable": False},
                step_id=step_id, run_id=run_id,
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
                        run_id, current, tool_node, resolver)
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
                    except ToolArgumentsUnavailable as exc:
                        # The tool could not be CALLED. Deterministic — the next
                        # tick binds the same arguments against the same
                        # signature — so reopening it only reproduces the
                        # failure, and confirming it would record a step that
                        # never ran as completed. Fail the step and the run.
                        self._fail_tool_step_in_tx(run_id, current, str(exc))
                        return None
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
                    # A gate reached on THIS path used to resolve with no
                    # file_reader, so a `from_file` edge could never match here
                    # — the same gate routed one way through advance_run's main
                    # loop and dead-ended when a tick found it pre-resolved.
                    # Read against the last completed step, exactly as the main
                    # loop does.
                    gate_last = conn.execute(
                        "SELECT step_id FROM skillflow_steps WHERE run_id = ?"
                        " AND status = 'completed'"
                        " ORDER BY completion_seq DESC, id DESC LIMIT 1",
                        (run_id,),
                    ).fetchone()
                    gfr = self._make_file_reader(
                        run["project_id"], run["graph_name"],
                        gate_last["step_id"] if gate_last else "", run_id=run_id)
                    while resolver.is_gate(current) and gate_depth < 1000:
                        gate_depth += 1
                        # SF-23 (gate pre-resolved): Use resolve_transition so
                        # we can distinguish "no match" from "terminal (to: null)".
                        try:
                            gt, gtarget = resolver.resolve_transition(
                                current, flags, edge_counts, file_reader=gfr)
                        except CycleLimitExceeded:
                            self._fail_run_in_tx(
                                conn, run_id,
                                f"Gate '{current}': cycle limit exceeded"
                                + self._routing_reason_suffix(
                                    conn, run_id, current, resolver, file_reader=gfr))
                            return None
                        if gt is None:
                            self._fail_run_in_tx(
                                conn, run_id,
                                f"Gate '{current}': no matching transition"
                                + self._routing_reason_suffix(
                                    conn, run_id, current, resolver, file_reader=gfr))
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

            # "Which step finished LAST?" — order by completion_seq, never by
            # id: id is creation order, and loop/reject re-runs append high-id
            # instances of EARLY steps. Sorting by id here once sent a run that
            # had just finished 'humanize' back to an hours-old, higher-id
            # 'outline_review' instance and re-took ITS transition (re-staging
            # and re-pausing an already-approved checkpoint). id DESC only
            # breaks ties among pre-migration NULL rows.
            last = conn.execute(
                """
                SELECT step_id, result_flags_json FROM skillflow_steps
                WHERE run_id = ? AND status = 'completed'
                ORDER BY completion_seq DESC, id DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()

            edges_taken: list[tuple[str, str]] = []
            fr = self._make_file_reader(
                run["project_id"], run["graph_name"],
                last["step_id"] if last else "", run_id=run_id)
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
                    self._fail_run_in_tx(
                        conn, run_id,
                        "Cycle limit exceeded"
                        + self._routing_reason_suffix(
                            conn, run_id, last["step_id"], resolver,
                            file_reader=fr))
                    return None
                if first_target is None:
                    last_node = resolver.get_node(last["step_id"])
                    # SF-23: A transition with to:null (terminal) matched —
                    # this means the pipeline should end. Check end_conditions
                    # and complete the run instead of failing. A node with NO
                    # transitions at all is terminal by the same logic (before
                    # completion_seq, this path was masked: id-order picked a
                    # stale earlier instance and re-resolved FROM it instead).
                    if ((matched_t is not None and matched_t.to is None)
                            or (last_node is not None
                                and not last_node.transitions)):
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
                            + self._routing_reason_suffix(
                                conn, run_id, last["step_id"], resolver,
                                file_reader=fr)
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
                        self._fail_run_in_tx(
                            conn, run_id,
                            f"Gate '{next_node}': cycle limit exceeded"
                            + self._routing_reason_suffix(
                                conn, run_id, next_node, resolver, file_reader=fr))
                        return None
                    if gt is None:
                        self._fail_run_in_tx(
                            conn, run_id,
                            f"Gate '{next_node}': no matching transition"
                            + self._routing_reason_suffix(
                                conn, run_id, next_node, resolver, file_reader=fr))
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
            # The redirect target must exist in the graph THIS RUN is pinned to.
            #
            # Hosts compute `redirect_to` from the graph by NAME — i.e. from the
            # CURRENT definition — so once a run is pinned the two can name
            # different graphs. Reachable in one sitting: a run pauses at a
            # checkpoint, the config is edited and re-registered, the user
            # clicks Reject, and `redirect_to` is a node that exists only in the
            # newer version. Writing it would leave `claim_next_step` resolving
            # `current_node` to None and rolling back on every tick — the run
            # stays `running` forever with nothing logged, which is exactly what
            # an idle run looks like. Fail loudly instead; the caller can show it.
            _target = redirect_to or step_id
            if self._get_resolver_for_run(run_id).get_node(_target) is None:
                raise SkillFlowError(
                    f"Cannot reject checkpoint '{step_id}' of run {run_id} into "
                    f"'{_target}': that step is not in the graph version this "
                    f"run is pinned to. The config changed since the run "
                    f"started — re-pin the run or start a fresh one.")
            conn.execute(
                "UPDATE skillflow_runs SET current_node = ?, status = 'running', updated_at = datetime('now') WHERE id = ?",
                (_target, run_id),
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

        # Append this round of feedback to the redirect target's persisted,
        # git-historized log — AFTER the tx so the git subprocess never holds the
        # write lock. The step re-runs seeing the FULL history (all rounds), not
        # just this latest one, so consecutive requests no longer drift.
        self._append_feedback_log(
            run["project_id"], run["graph_name"], redirect_to or step_id,
            feedback, run_id)

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

            resolver = self._get_resolver(run["graph_name"],
                                          version=run["graph_version"])

            # Find the last completed checkpoint step (completion order;
            # completed_at alone has 1s resolution so same-second completions
            # would tie-break by nothing — completion_seq is a strict order)
            steps = conn.execute(
                "SELECT step_id FROM skillflow_steps "
                "WHERE run_id = ? AND status = 'completed' "
                "ORDER BY completion_seq DESC, id DESC",
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

    # ── Liveness heartbeat ────────────────────────────────────────

    def _heartbeat_step(self, run_id: str, step_id: str) -> None:
        """Mark a claimed step alive by bumping its updated_at (throttled).

        Called from trace() on every worker action. recover_stale_claims reads
        updated_at as the activity clock, so a step is reaped for SILENCE, not
        total runtime — a slow-but-active agent is never falsely recovered.
        Best-effort: never raise into the caller's hot path.
        """
        if not run_id or not step_id:
            return
        key = (run_id, step_id)
        now = time.time()
        last = self._hb_last.get(key, 0.0)
        if now - last < self._hb_min_interval:
            return
        self._hb_last[key] = now
        try:
            with self._lock:
                self._conn.execute(
                    "UPDATE skillflow_steps SET updated_at = datetime('now') "
                    "WHERE run_id = ? AND step_id = ? AND status = 'claimed'",
                    (run_id, step_id),
                )
                self._conn.commit()
        except Exception:
            pass  # liveness must never break a run

    @staticmethod
    def _ts_to_epoch(ts: str | None) -> float | None:
        """Parse a stored UTC timestamp to epoch seconds, accepting both the
        strftime ``YYYY-MM-DDTHH:MM:SSZ`` (claimed_at) and SQLite
        ``datetime('now')`` ``YYYY-MM-DD HH:MM:SS`` (updated_at) formats."""
        if not ts:
            return None
        s = ts.strip().replace("T", " ")
        if s.endswith("Z"):
            s = s[:-1]
        s = s.split(".", 1)[0]  # drop any fractional seconds
        try:
            return calendar.timegm(time.strptime(s, "%Y-%m-%d %H:%M:%S"))
        except Exception:
            return None

    # ── Recovery ──────────────────────────────────────────────────

    def recover_stale_claims(self, stale_threshold_seconds: float = 300) -> list[str]:
        now_epoch = time.time()
        with self._tx() as conn:
            claimed = conn.execute(
                """
                SELECT id, run_id, step_id, inputs_json, claimed_at, updated_at,
                       claimed_by
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
            # presumed dead.
            #
            # Staleness is measured against the ACTIVITY clock (updated_at),
            # NOT the claim START (claimed_at). An agent step heartbeats
            # updated_at on every trace() action, so a slow-but-active reviewer
            # (many turns over minutes) is never reaped — only SILENCE longer
            # than the window means the worker is dead or hung. A tool step
            # emits no intra-run activity, so its updated_at == claimed_at and
            # its window is its timeout_seconds (0 = never stale), unchanged.
            stale = []
            for row in claimed:
                # Ownership decides. The lease only answers where ownership
                # cannot — these are different questions and only the first has
                # a definite answer:
                #
                #   dead    the process that made this claim is gone. No amount
                #           of waiting brings it back, so reclaim NOW instead of
                #           serving out a window. This also overrides the
                #           timeout_seconds == 0 exemption below: that rule
                #           exists because reclaiming a LIVE tool relaunches it
                #           beside itself, and a dead owner runs nothing.
                #   alive   observed running by the OS, right now — so it is
                #           still working and there is nothing to recover.
                #           Silence is not death: an agent step spends minutes
                #           inside a single LLM call and emits no trace while it
                #           waits, so its activity clock goes quiet WHILE IT
                #           WORKS. Reaping it there killed real work (8 reclaims
                #           against 13 t_impl executions on one run) and then
                #           failed the returning executor's confirm on `version
                #           mismatch`, leaving the step recorded as failed after
                #           its output had been promoted. A live owner is never
                #           reclaimed. If it is also hung, that is tardiness,
                #           and tardiness is for its own timeout and for the
                #           host's hung-step warning — not for a reaper that
                #           cannot tell the two apart.
                #   unknown a legacy `claimed_by` ("worker"), another kernel
                #           boot, or no /proc to read. Nothing was observed, so
                #           the lease below is still the only answer there —
                #           which is also why this change is inert on a platform
                #           where liveness cannot be seen.
                owner_dead = owner_is_dead(row["claimed_by"])
                if owner_dead is True:
                    stale.append(row)
                    continue
                if owner_dead is False:
                    continue
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
                # Activity clock: updated_at (heartbeated) falling back to
                # claimed_at. Epoch comparison is format-robust (claimed_at is
                # ISO-Z, updated_at is SQLite space-format).
                activity = row["updated_at"] or row["claimed_at"]
                ep = self._ts_to_epoch(activity)
                if ep is not None and (now_epoch - ep) > window:
                    stale.append(row)
            run_ids: set[str] = set()
            for row in stale:
                # SF-20: track stale recovery count to detect crash loops.
                # If the same step instance has been recovered twice already,
                # the worker keeps dying on it — fail it permanently.
                #
                # Deliberately does NOT touch `_step_tools`. That map is
                # in-memory and per SkillFlow instance: an entry exists only for
                # a claim THIS process made. This reaper, by contrast, scans
                # every claimed row in a shared DB, and it reaps for two reasons
                # that both point away from us — the OS reported the owning
                # process gone (so the owner was some OTHER process; ours is
                # demonstrably alive and running this code) or the lease expired
                # (and a lease-condemned worker may still be executing: the reset
                # UPDATE below rewrites status/version/claimed_at/claimed_by/
                # inputs_json but NOT claim_epoch, which only claim_next_step and
                # _claim_tool_step_in_tx bump, so the falsely reaped worker still
                # passes `_epoch_holds`, its tool calls are not fenced, and
                # stripping its entry would leave it reading nothing for the rest
                # of the step while believing it had read everything). Our own
                # entries are released at confirm/fail, overwritten by the next
                # claim of the same step, and bounded by the eviction cap.
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
                    # NOT on a paused run. For a paused run `current_node` is
                    # not a position to be re-derived — it is the checkpoint's
                    # RESUME TARGET, written by the pause and read verbatim by
                    # approve_checkpoint. Clearing it makes approval return an
                    # empty next_node, after which advance_run re-resolves from
                    # the last completed step, hits that step's checkpoint edge
                    # again and PAUSES THE RUN A SECOND TIME: the user approves,
                    # nothing happens, the run sits at the same checkpoint.
                    #
                    # This reaper is the only one of the five pointer-clearing
                    # sites that can reach a paused run at all: the other four
                    # run on a worker holding a claim token, and a paused run has
                    # no live claim. It fires on a timer against every run.
                    cur = conn.execute(
                        "UPDATE skillflow_runs SET current_node = NULL, "
                        "updated_at = datetime('now') "
                        "WHERE id = ? AND status != 'paused'",
                        (row["run_id"],),
                    )
                    if cur.rowcount == 0:
                        logging.getLogger("skillflow").warning(
                            "run %s is paused; step %s failed permanently but "
                            "its resume target was kept — approving the "
                            "checkpoint will route into a failed step",
                            row["run_id"], row["step_id"])
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
        if not run_id:
            return
        # A trace call is proof the step's worker is alive → refresh its
        # liveness heartbeat (throttled). Done before the trace_enabled gate so
        # liveness holds even for hosts that disable the durable trace.
        self._heartbeat_step(run_id, step_id)
        if not self._trace_enabled:
            return
        clean = {k: self._clip(v) for k, v in (payload or {}).items()}
        try:
            # Resolve target connection: per-project DB when configured,
            # otherwise the shared DB (backward-compat).
            # Auto-resolve project_id from run_id when not explicitly passed,
            # so callers that only have a run_id (e.g. the _trace closure in
            # claim_next_step) still route to the per-project trace.db.
            if not project_id:
                project_id = self._get_project_id(run_id)
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
        with self._lock:
            row = self._conn.execute(
                "SELECT project_id FROM skillflow_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return row["project_id"] if row else ""

    def get_project_id(self, run_id: str) -> str:
        """Public accessor for the project_id of a run."""
        return self._get_project_id(run_id)

    def _get_graph_name(self, run_id: str) -> str:
        cached = self._graph_name_cache.get(run_id)
        if cached is not None:
            return cached
        with self._lock:
            row = self._conn.execute(
                "SELECT graph_name FROM skillflow_runs WHERE id = ?", (run_id,)
            ).fetchone()
        name = row["graph_name"] if row else ""
        if name:                       # cache only a resolved name (a not-yet-
            self._graph_name_cache[run_id] = name   # visible run is re-queried)
        return name

    def _edit_fallback_dir(self, run_id: str, pid: str, gname: str,
                           step_id: str) -> str:
        """Edit-baseline fallback for outputs that never reach the repo: the
        step's own promoted dir — but ONLY when this RUN has already completed
        an instance of this step (a revision loop: checkpoint reject or
        review-fail edge). Step dirs are shared across runs of one config, so
        without the gate a fresh run's first attempt would silently edit a
        PREVIOUS run's promoted output (e.g. a chapter-2 outline "editing"
        chapter 1's) instead of erroring toward create."""
        if not (run_id and self._workspace):
            return ""
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT 1 FROM skillflow_steps WHERE run_id = ? AND "
                    "step_id = ? AND status = 'completed' LIMIT 1",
                    (run_id, step_id),
                ).fetchone()
            if row is None:
                return ""
            # A loop-body step's promoted baseline lives at {step}/{item}/ — the
            # flat parent would pass is_dir() (it holds sibling item folders) but
            # contain none of THIS item's files, breaking revision-round edits.
            try:
                item = self._loop_item_for_step(
                    run_id, self._get_resolver_for_run(run_id), step_id)
            except Exception:
                item = None
            final_dir = self._workspace.get_step_dir(pid, gname, step_id, item=item)
            return str(final_dir) if final_dir.is_dir() else ""
        except Exception:
            return ""  # best-effort: no fallback beats a wrong baseline

    # ── Host tool execution API ─────────────────────────────────────

    # ── Step-owned tool callables ─────────────────────────────────────
    # Bound at claim, resolved by the owning step, dropped when the step ends.

    _STEP_TOOL_CAP = 512

    def _set_step_tools(self, run_id: str, step_id: str, fns: dict,
                        step_instance_id: int, claim_epoch: int) -> None:
        """Bind *fns* to the claim identified by (instance id, claim epoch).

        Both halves are needed: the row id separates two INSTANCES of one step
        (a loop body, a Green/Red re-run — each re-entry INSERTs a fresh row
        whose first claim is epoch 1), and the epoch separates two CLAIMS of one
        row (a validation failure or lifecycle retry resets the same row to
        'pending' and `claim_next_step` re-claims it, bumping only the epoch).
        """
        with self._step_tools_lock:
            self._step_tools[(run_id, step_id)] = (
                int(step_instance_id or 0), int(claim_epoch or 0), fns)
            over_cap = len(self._step_tools) > self._STEP_TOOL_CAP
        if over_cap:
            # Outside the lock: eviction reads the DB, and holding a lock every
            # tool call takes across I/O would couple one step's reads to
            # another step's bookkeeping.
            self._evict_ended_step_tools()

    def _clear_step_tools(self, run_id: str, step_id: str) -> None:
        """Drop whatever (run_id, step_id) holds, unconditionally.

        Called at CLAIM, before anything that can fail. The bind below happens
        only when the step has context specs, the read-tool build succeeds and it
        yields at least one callable — three ways to skip it, and on every one of
        them the PREVIOUS claim's entry used to survive into the new claim. Those
        closures capture the previous claim's source map, whose 'self'/'promoted'
        layer is the previous LOOP ITEM's directory: item B would read item A's
        output as its own and produce silently wrong work.

        Unconditional and identity-free on purpose — but NOT because "no earlier
        claim exists here", which was the justification written first and is
        false: `recover_stale_claims`' lease branch resets a row to 'pending'
        without touching `claim_epoch`, so a worker it condemned while still
        alive can be holding this slot when the replacement claim arrives.
        `_evict_ended_step_tools` spares exactly that entry, and this drops it.

        The two are consistent even so, and it is the ORDER that makes them so.
        The evictor runs while the row is still 'pending' at the reaped worker's
        epoch — nobody has replaced it, `_epoch_holds` still admits its calls, and
        blinding it would strand a step that is going to confirm. This runs after
        `claim_next_step` has bumped `claim_epoch` for the new owner, at which
        point the old worker is fenced: `_assert_epoch` rejects its confirm, its
        output is discarded, and its remaining reads decide nothing. Robbing a
        claim that can no longer commit costs nothing; blinding a claim that
        still can is the bug the evictor avoids.
        """
        with self._step_tools_lock:
            self._step_tools.pop((run_id, step_id), None)

    def _evict_ended_step_tools(self) -> None:
        """Last-resort bound, and it must never cost a LIVE step its tools.

        Entries are dropped at confirm/fail and overwritten at the next claim of
        the same step, so exceeding the cap means claims are being abandoned
        without ever reaching any of those. The only safe thing to drop is an
        entry whose claim is provably OVER, and the DB is the one place that
        knows: the step row is gone, it has been re-claimed under a LATER epoch,
        or it reached a terminal status ('completed' / 'failed') at this entry's
        epoch.

        'pending' at the SAME epoch is NOT over, and this is the case that costs
        a live step its eyes. `recover_stale_claims`' lease branch resets a row
        to 'pending' precisely when it CANNOT prove the owner is dead, and it
        rewrites status/version/claimed_at/claimed_by/inputs_json but NOT
        `claim_epoch` — so a worker the lease condemned while it was still alive
        keeps an epoch that satisfies `_epoch_holds`, `execute_tool` does not
        fence its calls, and they arrive here expecting to be served. The reaper
        spares that entry deliberately (`test_a_lease_reaped_claim_keeps_its_read_surface`);
        a `status != 'claimed'` test here would delete the very entry it spared.

        Any other status is spared too, for the same reason: this decides whether
        to BLIND a step, so an unrecognised status must not read as "over". The
        cost of sparing is bounded by the cap and reported by the warning below.

        Deliberately NOT an idle clock. An earlier version evicted entries idle
        longer than the reap threshold and claimed that matched
        `recover_stale_claims`; it does not. That reaper decides on OWNERSHIP
        first — a claim whose owning process the OS reports alive is never
        reaped, however long it has been silent — and an agent inside one long
        generation turn traces nothing, so neither its lease clock nor a
        read-tool clock moves while it works. An idle-clock evictor therefore
        blinds exactly the step the reaper protects.

        Best-effort: if the DB cannot be read, nothing is evicted. A few hundred
        leaked closures are a smaller failure than a running step losing its
        read surface.
        """
        with self._step_tools_lock:
            snapshot = [(k, v[0], v[1]) for k, v in self._step_tools.items()]
        live: dict[int, tuple[str, int]] = {}
        ids = sorted({sid for _k, sid, _e in snapshot if sid})
        try:
            # `_ro`, never `_tx`. The only caller is `_set_step_tools`, and it
            # runs lexically inside `claim_next_step`'s `with self._tx()` block.
            # Measured: a nested `_tx` there happens to SUCCEED today, because
            # something between the BEGIN and this point commits the connection
            # — so this is not a live bug, it is a dependency on where a commit
            # happens to fall. Were the transaction still open, the nested
            # `BEGIN IMMEDIATE` would raise "cannot start a transaction within a
            # transaction" (verified separately), the `except` below would
            # swallow it, and the evictor would evict nothing forever while
            # logging that the table could not be read. `_ro` takes the same
            # RLock (re-entrant on this thread) and issues a plain SELECT, so it
            # is correct either way.
            with self._ro() as conn:
                for i in range(0, len(ids), 400):
                    chunk = ids[i:i + 400]
                    qs = ",".join("?" * len(chunk))
                    for row in conn.execute(
                            f"SELECT id, status, claim_epoch FROM "
                            f"skillflow_steps WHERE id IN ({qs})", chunk):
                        live[row["id"]] = (row["status"],
                                           row["claim_epoch"] or 0)
        except Exception:
            logging.getLogger("skillflow").warning(
                "step tool map is over its cap but the step table could not be "
                "read; evicting nothing", exc_info=True)
            return
        dead = []
        for key, sid, epoch in snapshot:
            row = live.get(sid)
            if row is None or row[1] != epoch or row[0] in ("completed", "failed"):
                dead.append((key, sid, epoch))
        remaining = 0
        with self._step_tools_lock:
            for key, sid, epoch in dead:
                entry = self._step_tools.get(key)
                # Compare-and-delete: the slot may have been rebound by a new
                # claim while the DB was being read.
                if entry is not None and entry[0] == sid and entry[1] == epoch:
                    del self._step_tools[key]
            remaining = len(self._step_tools)
        if remaining > self._STEP_TOOL_CAP:
            logging.getLogger("skillflow").warning(
                "step tool map holds %d entries and %d of them still name a "
                "live claim — steps are being claimed but never confirmed, "
                "failed or reclaimed", remaining, remaining - len(dead))

    def _step_tool_fn(self, run_id: str, step_id: str, name: str):
        """The callable *this* step owns for *name*, or None.

        Not identity-checked: a tool call carries no step_instance_id down this
        path, and `execute_tool` already fences a reclaimed executor's calls when
        the host passes the claim's epoch. The identity stored beside the
        callables exists for RELEASE.
        """
        if not (run_id and step_id):
            return None
        with self._step_tools_lock:
            entry = self._step_tools.get((run_id, step_id))
        return entry[2].get(name) if entry is not None else None

    def _release_step_tools(self, run_id: str, step_id: str,
                            step_instance_id: int | None,
                            claim_epoch: int | None) -> None:
        """Drop this claim's callables — only if the entry is still ITS entry.

        Compare-and-delete on the PAIR (instance id, claim epoch), because
        neither half names a claim alone. `(run_id, step_id)` names the step; the
        row id names one INSTANCE of it but is shared by every re-claim of that
        row (eight sites reset a row to 'pending' — enumerated where
        `_step_tools` is declared — and `claim_next_step` re-claims the same row,
        bumping only `claim_epoch`; none of the seven writes the epoch at all);
        the epoch distinguishes those re-claims but restarts at 1 on
        every fresh row, so it is shared by consecutive instances of one step.

        What each half actually buys, measured against the fence rather than
        assumed:

        * The INSTANCE id is what stops a zombie executor of instance N from
          releasing instance N+1's entry. `_assert_epoch` does not: it re-reads
          the zombie's OWN row, which nothing resets, so its token holds forever.
        * The EPOCH separates two CLAIMS of one row, and matters wherever
          `_assert_epoch` lets a superseded token through. `_epoch_holds` admits
          three shapes, not one: the token's own `claim_epoch` is 0/None (a
          hand-built token, or a host that does not forward epochs); the row it
          names is gone; or the row's stored `claim_epoch` is 0/NULL. The first
          never reaches the comparison — the guard below returns early on an
          incomplete identity — so the epoch's real work is the remaining two,
          plus every case where the fence was not consulted at all. With real
          forwarded epochs on both sides the fence refuses the call before it
          gets here, so there the pair guard is belt to that brace.

          It is a CHEAP belt, and that is the argument for keeping it: this
          comparison is the difference between releasing an entry and blinding
          whichever claim currently owns the slot, and it costs one integer
          compare.

        A token carrying an incomplete identity — instance id or epoch 0/None —
        therefore releases NOTHING: it cannot prove which claim it is. That is
        safe rather than leaky because `_clear_step_tools` drops the slot
        unconditionally at the NEXT claim of the same step, so an un-released
        entry survives only until the step runs again, and the cap bounds the
        rest.
        """
        if not step_instance_id or not claim_epoch:
            return
        key = (run_id, step_id)
        with self._step_tools_lock:
            entry = self._step_tools.get(key)
            if (entry is not None and entry[0] == int(step_instance_id)
                    and entry[1] == int(claim_epoch)):
                del self._step_tools[key]

    def execute_tool(self, name: str, params: dict, *,
                     run_id: str = "", step_id: str = "",
                     step_instance_id: int | None = None,
                     claim_epoch: int = 0,
                     project_root: str = "") -> dict:
        """Execute a tool on behalf of the host's agent loop.

        Resolves the allowed tool list from the graph node internally.
        Write tools write to the skillflow-managed draft directory.
        Read/exploration tools receive ``project_root`` as their workspace.

        ``project_root=""`` means "the host has no opinion", never "the process
        CWD". For a tool reached through the ToolLoader the code-path resolver is
        consulted, and if it answers that the run owns no code repository,
        ``project_root``/``workspace_root`` are OMITTED from the tool call rather
        than passed as empty strings.

        Omission is not itself a safety mechanism, and the comments here used to
        claim it was. ``Path("")`` is ``Path(".")``, so a tool that DEFAULTS the
        parameter to ``""`` cannot tell an omitted argument from an empty one —
        the two are byte-identical inside the function. Omitting buys exactly one
        thing: a tool whose parameter has no default becomes uncallable, which
        the tool-STEP path turns into a named `ToolArgumentsUnavailable` and this
        path into a TypeError. Everything else is the tools' own guards
        (``repo_apply``, ``repo_validate``, ``git_sync_pre``, ``pytest`` each
        refuse a non-absolute root in their first lines).

        Two skillflow tools still fall back to the process CWD when the value is
        missing — ``tools/lint`` (``Path.cwd()``) and ``tools/file_exists``
        (``Path("")``). Neither is reachable that way today: they appear only in
        ``validation:`` / ``after_deliver`` specs, which run through
        ``StepValidator`` with an explicit ``workspace_root``, and no shipped
        agent config grants either to an agent. Pinned in
        ``tests/test_repo_tools_refuse_the_process_cwd.py`` so it stays a
        theoretical hazard rather than becoming a live one unnoticed.

        That applies to the LOADER branch only. The write families handled first
        — ``write_*``/``create_*``/``edit_*``/``delete_*`` and the generic
        ``create``/``edit``/``write`` — return before the resolver is reached and
        pass ``project_root or ""`` on as an edit BASELINE (``source_dir``). An
        empty baseline there is not a CWD hazard: ``skillflow.write_tools``
        treats it as "no source", and every write still lands in the step's tmp
        directory, which comes from the workspace manager.

        Every call + result is recorded to the durable run trace. Pass
        ``step_instance_id`` (from the claimed step's token) so each tool call
        correlates to its exact step instance — essential for loop iterations
        where the same step_id runs many times.

        Pass ``claim_epoch`` (also from the token) to fence the call: once the
        step has been reclaimed, the tool is refused with an error dict instead
        of running beside the replacement executor. Omitted (0) it is unfenced,
        which is what a host that has not been taught to forward the epoch
        gets — the same behaviour as before.
        """
        if (claim_epoch and step_instance_id
                and not self._epoch_holds(step_instance_id, claim_epoch)):
            msg = (f"Step '{step_id}' was reclaimed; tool '{name}' refused "
                   f"(stale claim_epoch {claim_epoch}).")
            self.trace(run_id, "tool_call", name,
                       {"source": "agent", "fenced": msg},
                       step_id=step_id, step_instance_id=step_instance_id)
            return {"error": msg}
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
                # node drives the tool allowlist + output.fixed; a silent None
                # here degrades tool gating for this call.
                import logging
                logging.getLogger("skillflow").warning(
                    "failed to resolve node for %s/%s; tool gating degraded",
                    run_id, step_id, exc_info=True)

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
                        allow_full_write=node.output_allow_full_write,
                        carry_forward=getattr(node, "output_carry_forward", False)):
                    allowed.add(ws["name"])
            # Add read tool names from context specs (mode ∈ {tool, both})
            if node.context:
                from skillflow.read_tools import get_read_tool_names
                allowed.update(get_read_tool_names(node.context))
            # Addon toolset: compose's `add_tools` op parks extra tool names in
            # the step's opaque config, and claim_next_step merges them into the
            # schemas the agent is SHOWN. This allowlist never consulted them, so
            # `add_tools` advertised a tool and then refused the call —
            # `{"error": "Tool 'X' not allowed. Allowed: [...]"}`. Offered-then-
            # denied is worse than never offered: it burns the agent's turn and
            # reads to the model as a broken environment, which is exactly what a
            # tool grant is supposed to prevent.
            _extra = node.config.get("extra_tools") if isinstance(node.config, dict) else None
            if _extra:
                allowed.update(_extra)
            # Capability grants, for the SAME reason as add_tools directly above:
            # claim_next_step shows the agent every tool a capability grants, and
            # this list decided whether the call was honoured. It never consulted
            # capabilities, so `capability: "tool_creation"` advertised
            # register_tool and then answered "Tool 'register_tool' not allowed" —
            # the offered-then-denied failure this block was already fixed for
            # once, one lane over. The card-declared form goes through the same
            # resolution as the claim path, so a task granted its tools can call
            # them.
            # Read what the CLAIM actually granted rather than re-resolving it:
            # claim_next_step records the resolved names in `_capabilities`, and
            # re-deriving them here would mean two code paths that can disagree
            # about a task card edited mid-step.
            for _cn in self._granted_capabilities(run_id, step_id):
                _cap = self._capabilities.get(_cn) or {}
                allowed.update(_cap.get("tools") or ())

        if allowed and name not in allowed:
            return {"error": f"Tool '{name}' not allowed. Allowed: {sorted(allowed)}"}

        fixed = node.output_fixed if node else {}

        # Write/create/edit tools — write to step tmp directory (atomic staging)
        if (name.startswith("write_") or name.startswith("create_")
                or name.startswith("edit_") or name.startswith("delete_")):
            if not self._workspace:
                return {"error": "No workspace configured for write tool"}
            pid = self._get_project_id(run_id)
            gname = self._get_graph_name(run_id)
            tmp_dir = self._workspace.get_step_tmp_dir(pid, gname, step_id)
            from skillflow.write_tools import (execute_write, execute_create,
                                               execute_edit, execute_delete)
            slot = name[name.index("_") + 1:]  # everything after first _
            if name.startswith("delete_"):
                res = execute_delete(slot, fixed, params, str(tmp_dir))
            elif name.startswith("create_"):
                res = execute_create(slot, fixed, params, str(tmp_dir))
            elif name.startswith("edit_"):
                # Edit the EXISTING file from the consolidated repo (project_root),
                # writing the result into staging for promotion + repo_apply.
                # For outputs that never reach the repo, the step's own promoted
                # dir is the baseline — same-run gated (see helper).
                res = execute_edit(slot, fixed, params, str(tmp_dir),
                                   source_dir=project_root or "",
                                   fallback_source_dir=self._edit_fallback_dir(
                                       run_id, pid, gname, step_id))
            else:
                res = execute_write(slot, fixed, params, str(tmp_dir))
            return self._enrich_write_path(run_id, step_id, res)

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
                res = execute_generic_create(params, str(tmp_dir),
                                             source_dir=project_root or "")
            elif name == "edit":
                res = execute_generic_edit(params, str(tmp_dir),
                                           source_dir=project_root or "",
                                           fallback_source_dir=self._edit_fallback_dir(
                                               run_id, pid, gname, step_id))
            else:
                res = execute_generic_write(params, str(tmp_dir))
            return self._enrich_write_path(run_id, step_id, res)

        # finish_step — no-op completion signal; the host runner detects it and
        # breaks the tool-calling loop after the current turn completes
        if name == "finish_step":
            return {"status": "completed", "summary": params.get("summary", "")}

        # Read/exploration/validation tools. The read/search/list trio belongs to
        # the claim that built it, so ask that step first — the shared loader has
        # no callable for it and must never be allowed to answer with another
        # step's.
        fn = self._step_tool_fn(run_id, step_id, name)
        if fn is None:
            # Refuse only if the name is still DYNAMIC — i.e. nothing on disk
            # answers to it. `_step_scoped_names` only ever grows, so a bare
            # membership test would shadow a real tool directory named `read` /
            # `search` / `list` for the rest of the process's life if one were
            # ever added. `is_dynamic` re-checks the disk, so on THIS branch an
            # on-disk tool wins and falls through to the loader below.
            #
            # Only on this branch. A step that HOLDS an entry for the name never
            # gets here, so its closure shadows an on-disk tool of the same name
            # unconditionally. That is the intended precedence — the step's own
            # read surface is what the step was granted — but it is a shadow, not
            # a negotiation, and adding a `read`/`search`/`list` tool directory
            # would not take it back.
            _is_dyn = getattr(self._tool_loader, "is_dynamic", None)
            if name in self._step_scoped_names and (
                    _is_dyn is None or _is_dyn(name)):
                return {"error": f"Tool '{name}' is provided per step, and this "
                                 f"step has no read surface (or its claim has "
                                 f"already ended)."}
            fn = self._tool_loader.load_fn(name)
        # `project_root=""` from the host means "no opinion", NOT "the process
        # CWD" — so ask the code-path resolver, as the tool-STEP path
        # (`_execute_tool_inline`) and the lifecycle-hook path
        # (`_execute_tool_hook`) do. (`_claim_tool_step_in_tx` is a pure
        # status CAS and never touches tool arguments.) This path was the only
        # one of the three that never consulted the resolver, and it is the one
        # agents call: the host sends "" for a repo-less run, `setdefault`
        # forwarded it into both roots, and a tool doing
        # `Path(project_root or workspace_root).resolve()` would get the process
        # CWD — for a hosted engine, the server's own checkout.
        #
        # The three paths agree about `project_root` ONLY. They deliberately
        # disagree about `workspace_root`: the two step paths set it to the DPS
        # workspace (`get_project_path`) and this one sets it to the code repo,
        # because an agent-invoked tool is handed one root and read tools take
        # their tree from it. A tool that reads `workspace_root` therefore gets
        # different trees depending on which path invoked it — recorded here
        # because it is a live inconsistency, not a thing this change fixed.
        if not project_root and run_id and self._workspace is not None:
            try:
                _pid = self._get_project_id(run_id)
                _cp = (self._workspace.get_project_code_path(_pid)
                       if _pid else None)
                project_root = str(_cp) if _cp else ""
            except Exception:
                logging.getLogger("skillflow").warning(
                    "could not resolve the code path for run %s; tool %r runs "
                    "without a project root", run_id, name, exc_info=True)
                project_root = ""
        kwargs = dict(params)
        # Omitted, never "". A tool that REQUIRES either name then raises
        # TypeError rather than running; a tool that defaults it to "" sees
        # exactly what a `setdefault("")` would have given it — `Path("")` is
        # `Path(".")` — and refuses (or does not) on its own guard alone. So this
        # branch is not the safety; it is the absence of an argument the engine
        # has no answer for. See the docstring for which tools still fall back to
        # the CWD when the value is missing, and why none is reachable here.
        if project_root:
            kwargs.setdefault("workspace_root", project_root)
            kwargs.setdefault("project_root", project_root)
        if run_id:
            try:
                kwargs.setdefault("config_name", self._get_graph_name(run_id))
            except Exception:
                pass
        # Forward step/run identity so tools that want per-step state (e.g.
        # scratch-file tools) can isolate by step. Signature-filtered below, so
        # tools that don't declare these params are unaffected.
        kwargs.setdefault("step_id", step_id or "")
        kwargs.setdefault("run_id", run_id or "")
        # Capability context (agent-invoked tool): the agent's STEP may carry a
        # `capability` that hands its tools framework-selected values (e.g. a
        # durable state_dir, a tools_dir for register_tool). Same injection as
        # the tool-node path so an agent-invoked tool is provisioned identically.
        if run_id and step_id:
            try:
                _cgn = self._get_graph_name(run_id)
                _cres = self._get_resolver_for_run(run_id)
                _cnode = _cres.get_node(step_id)
                if _cnode is not None:
                    for _ck, _cv in self._capability_context(
                            _cnode, _cgn,
                            offers=getattr(_cres.graph, "capabilities", None),
                            names=self._granted_capabilities(run_id, step_id)
                            or None).items():
                        kwargs.setdefault(_ck, _cv)
            except Exception:
                pass
        # SF-10: pass step staging/output dirs so read_file (and similar tools)
        # can find files the agent just wrote (in .tmp) or files from previous
        # retries (in the step's final dir). write_* tools write to .tmp; without
        # these fallback paths the agent can't verify its own output within a step.
        #
        # Offered to EVERY tool, not a name allowlist: kwargs are signature-
        # filtered below, so a tool that doesn't declare these is unaffected, and
        # a tool that PRODUCES a file needs the staging dir just as much as one
        # that reads it. Writing an artefact straight into the working tree
        # instead looks like it worked and is then deleted by the step's delivery
        # reconciliation as "a file this step did not deliver".
        if True:
            try:
                if run_id and step_id and self._workspace:
                    pid = self._get_project_id(run_id)
                    gname = self._get_graph_name(run_id)
                    # step_dir = this step's own PRIOR promoted output; for a
                    # loop-body step that is the per-item folder (SF-10 fallback
                    # reads must see the previous round of THIS item, not the
                    # flat parent full of sibling items).
                    try:
                        _item = self._loop_item_for_step(
                            run_id, self._get_resolver_for_run(run_id), step_id)
                    except Exception:
                        _item = None
                    kwargs.setdefault("step_tmp_dir",
                                      str(self._workspace.get_step_tmp_dir(pid, gname, step_id)))
                    kwargs.setdefault("step_dir",
                                      str(self._workspace.get_step_dir(pid, gname, step_id,
                                                                       item=_item)))
            except Exception:
                # Without these, read_file loses the staging/step dirs and can't
                # see files the agent just wrote (breaks staging-first reads).
                import logging
                logging.getLogger("skillflow").warning(
                    "failed to attach step staging dirs for read_file/list_tree",
                    exc_info=True)
        # Filter kwargs to only what the function accepts
        import inspect as _inspect
        sig = None
        dropped: list[str] = []
        try:
            sig = _inspect.signature(fn)
            # Only the CALLER's own arguments count as dropped. The engine
            # setdefault()s workspace_root/project_root/step_id/run_id onto every
            # call, and most tools declare none of them — counting those would both
            # blame the agent for arguments it never sent and make the rebinding
            # below (which needs exactly one unrecognised name) never fire.
            dropped = [k for k in (params or {}) if k not in sig.parameters]
            kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
        except (ValueError, TypeError):
            pass
        if sig is not None:
            kwargs, dropped = _rebind_unambiguous_param(sig, kwargs, dropped, params or {})
        try:
            result = fn(**kwargs)
        except Exception as e:
            # A tool that RETURNS {"error": ...} is fed back to the agent and is
            # survivable; a tool that RAISES used to propagate all the way to a
            # failed run. So an agent that mistyped one argument name could not
            # recover from its own typo — the typo never came back to it. Observed:
            # `read_file() missing 1 required positional argument: 'path'` killed a
            # whole test-drive. Turn it into a tool result the ReAct loop can act on,
            # and name the real parameters so the next turn gets them right.
            msg = f"{name}() failed: {type(e).__name__}: {e}"
            if sig is not None:
                expected = ", ".join(
                    p for p in sig.parameters
                    if p not in ("workspace_root", "project_root", "step_id", "run_id",
                                 "kwargs", "args"))
                msg += f". Accepted parameters: {expected}"
            if dropped:
                msg += (f". These arguments were not recognised and were ignored: "
                        f"{', '.join(sorted(dropped))}")
            return {"error": msg}
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


def _rebind_unambiguous_param(sig, kwargs: dict, dropped: list,
                              original: dict) -> tuple[dict, list]:
    """Bind a single unrecognised argument to a single missing required one.

    The registry names "the file to operate on" six different ways — `path`
    (read_file, forge_lint, list_tree), `file` (write, pytest), `files`, `filename`,
    `file_path`, `graph_path`. The two most-used tools disagree, so an agent that
    just called `write(file=…)` and then reads the file back with
    `read_file(file=…)` is following the most recent example it saw. The signature
    filter then DROPS `file` and the call fails on a missing `path` — the filter
    turns a recoverable "unexpected keyword" into a hard "missing argument".

    When exactly one argument was dropped and exactly one required parameter is
    unfilled, the intended mapping is not a guess: there is only one of each. Bind
    them. With more than one on either side it IS a guess, so leave it alone and let
    the caller report the accepted parameter names instead.
    """
    if len(dropped) != 1:
        return kwargs, dropped
    import inspect as _inspect
    missing = [n for n, p in sig.parameters.items()
               if p.default is _inspect.Parameter.empty
               and p.kind in (_inspect.Parameter.POSITIONAL_OR_KEYWORD,
                              _inspect.Parameter.KEYWORD_ONLY)
               and n not in kwargs]
    if len(missing) != 1:
        return kwargs, dropped
    return {**kwargs, missing[0]: original[dropped[0]]}, []


def _flags_match(match: dict, flags: dict) -> bool:
    for key, expected in match.items():
        if key not in flags:
            return False
        if flags[key] != expected:
            return False
    return True
