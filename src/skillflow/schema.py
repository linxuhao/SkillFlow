"""SQL DDL for skillflow tables.

All table names are prefixed ``skillflow_`` to coexist safely with
application tables in the same SQLite database.

Usage:
    from skillflow.schema import ALL_DDL
    for stmt in ALL_DDL:
        conn.execute(stmt)
"""

# ── Tables ──────────────────────────────────────────────────────────

SKILLFLOW_GRAPHS = """
CREATE TABLE IF NOT EXISTS skillflow_graphs (
    name          TEXT PRIMARY KEY,
    yaml_text     TEXT NOT NULL,
    version       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# `skillflow_graphs` holds the CURRENT definition and is INSERT OR REPLACE'd, so
# every re-registration destroyed the content it replaced. This is the history:
# append-only, one row per distinct graph CONTENT, keyed by a digest of it.
#
# It exists so a run can be pinned. Before it, a run stored only `graph_name`,
# and `_get_resolver_for_run` resolved that name to whatever was registered
# *now* — an edit mid-run silently retargeted the run's remaining steps, and an
# edit after it made the trace of a finished run describe a graph that no longer
# existed. The version column on `skillflow_graphs` did not help: it was bumped
# blindly on every registration, so it counted process restarts (312 of them in
# one live deployment) rather than edits.
SKILLFLOW_GRAPH_VERSIONS = """
CREATE TABLE IF NOT EXISTS skillflow_graph_versions (
    name        TEXT NOT NULL,
    version     INTEGER NOT NULL,
    -- Canonical (sort_keys) JSON of graph.to_dict(), so `digest` is verifiable
    -- against what is stored here rather than being an unfalsifiable label.
    yaml_text   TEXT NOT NULL,
    digest      TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (name, version)
);
"""

SKILLFLOW_PROJECTS = """
CREATE TABLE IF NOT EXISTS skillflow_projects (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'active',
    meta_json   TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

SKILLFLOW_RUNS = """
CREATE TABLE IF NOT EXISTS skillflow_runs (
    id              TEXT PRIMARY KEY,
    graph_name      TEXT NOT NULL,
    graph_path      TEXT,
    project_id      TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    context_json    TEXT NOT NULL DEFAULT '{}',
    current_node    TEXT,
    started_at      TEXT,
    completed_at    TEXT,
    error_reason    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

SKILLFLOW_STEPS = """
CREATE TABLE IF NOT EXISTS skillflow_steps (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                  TEXT NOT NULL,
    step_id                 TEXT NOT NULL,
    step_config_json        TEXT NOT NULL DEFAULT '{}',
    status                  TEXT NOT NULL DEFAULT 'pending',
    version                 INTEGER NOT NULL DEFAULT 1,
    retry_count             INTEGER NOT NULL DEFAULT 0,
    validation_retry_count  INTEGER NOT NULL DEFAULT 0,
    max_retries             INTEGER NOT NULL DEFAULT 3,
    inputs_json             TEXT NOT NULL DEFAULT '{}',
    outputs_json            TEXT NOT NULL DEFAULT '{}',
    result_flags_json       TEXT NOT NULL DEFAULT '{}',
    last_error              TEXT,
    claimed_at              TEXT,
    -- WHO holds the claim: an identity, not a literal. See skillflow.identity.
    -- "worker host=box pid=4711 boot=… ns=… start=…". A reaper that can ask
    -- "is that pid alive?" separates a crashed owner from a quiet one; the
    -- literal "worker" this used to hold made the two indistinguishable.
    claimed_by              TEXT,
    -- Fencing token: bumped by EVERY claim of this step instance, carried by
    -- the ClaimToken, and checked on the write paths. `version` answers
    -- "reload and re-decide"; this answers "stop — you are not the executor
    -- any more", which is the only correct answer for a zombie that would
    -- otherwise run on_deliver (repo_apply, real git commits) beside its
    -- replacement. 0 = pre-migration/unfenced.
    claim_epoch             INTEGER NOT NULL DEFAULT 0,
    completed_at            TEXT,
    -- Per-run monotonic COMPLETION order (1, 2, 3 … assigned when a step
    -- instance is marked completed). `id` is CREATION order — the two diverge
    -- permanently once a loop/reject re-run appends new instances after later
    -- steps were instantiated. Position reconstruction ("which step finished
    -- last?") must sort by this, never by id: sorting by id sent a live run
    -- back to an hours-old reviewer instance and re-ran its transition.
    completion_seq          INTEGER,
    -- WHICH loop item this instance ran for, stamped at claim from
    -- skillflow_loop_state.current_item. NULL when the step is not in a loop
    -- body, and on every row written before this column existed.
    --
    -- The loop body is the only place where step_id is NOT enough to say what
    -- an instance did: a fan-out over six tasks runs t_impl six times (plus
    -- retries, plus review loop-backs, so nine rows for six items is normal),
    -- and nothing else on the row distinguishes them. Reconstructing it from
    -- completion order does not work — retries and loop-backs interleave. The
    -- per-item OUTPUT was always recoverable ({step}/{item}/ in the
    -- workspace), but that is project-scoped and replaced in place, so it
    -- cannot answer "what happened during THIS run".
    loop_item               TEXT,
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at              TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (run_id) REFERENCES skillflow_runs(id)
);
"""

SKILLFLOW_EDGE_COUNTS = """
CREATE TABLE IF NOT EXISTS skillflow_edge_counts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL,
    from_step       TEXT NOT NULL,
    to_step         TEXT NOT NULL,
    count           INTEGER NOT NULL DEFAULT 0,
    max_loop        INTEGER,
    FOREIGN KEY (run_id) REFERENCES skillflow_runs(id),
    UNIQUE(run_id, from_step, to_step)
);
"""

SKILLFLOW_LOOP_STATE = """
CREATE TABLE IF NOT EXISTS skillflow_loop_state (
    run_id          TEXT NOT NULL,
    loop_step_id    TEXT NOT NULL,
    current_index   INTEGER NOT NULL DEFAULT 0,
    items_json      TEXT NOT NULL DEFAULT '[]',
    completed_items TEXT,
    current_item    TEXT,
    item_context_key TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (run_id) REFERENCES skillflow_runs(id),
    PRIMARY KEY (run_id, loop_step_id)
);
"""

# SF-24: migration from index-based to set-based loop tracking
SKILLFLOW_LOOP_STATE_MIGRATION = [
    # Add set-based columns if missing (older skillflow DBs)
    "ALTER TABLE skillflow_loop_state ADD COLUMN completed_items TEXT",
    "ALTER TABLE skillflow_loop_state ADD COLUMN current_item TEXT",
]

SKILLFLOW_OUTBOX = """
CREATE TABLE IF NOT EXISTS skillflow_outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type      TEXT NOT NULL,
    payload_json    TEXT NOT NULL DEFAULT '{}',
    stream_target   TEXT NOT NULL DEFAULT '__global__',
    status          TEXT NOT NULL DEFAULT 'pending',
    drain_started_at TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

SKILLFLOW_TRACE = """
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
);
"""

# ── Indexes ─────────────────────────────────────────────────────────

SKILLFLOW_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_skillflow_runs_status ON skillflow_runs(status);",
    "CREATE INDEX IF NOT EXISTS idx_skillflow_runs_project ON skillflow_runs(project_id);",
    "CREATE INDEX IF NOT EXISTS idx_skillflow_steps_run ON skillflow_steps(run_id);",
    "CREATE INDEX IF NOT EXISTS idx_skillflow_steps_status ON skillflow_steps(status);",
    "CREATE INDEX IF NOT EXISTS idx_skillflow_steps_claimed ON skillflow_steps(claimed_at);",
    "CREATE INDEX IF NOT EXISTS idx_skillflow_edge_counts_run ON skillflow_edge_counts(run_id);",
    "CREATE INDEX IF NOT EXISTS idx_skillflow_outbox_status ON skillflow_outbox(status);",
    "CREATE INDEX IF NOT EXISTS idx_skillflow_loop_state_run ON skillflow_loop_state(run_id);",
    "CREATE INDEX IF NOT EXISTS idx_skillflow_trace_run ON skillflow_trace(run_id, seq);",
    "CREATE INDEX IF NOT EXISTS idx_skillflow_trace_step ON skillflow_trace(step_instance_id);",
]

# ── Ordered DDL list ────────────────────────────────────────────────

ALL_DDL: list[str] = [
    SKILLFLOW_GRAPHS,
    SKILLFLOW_GRAPH_VERSIONS,
    SKILLFLOW_PROJECTS,
    SKILLFLOW_RUNS,
    SKILLFLOW_STEPS,
    SKILLFLOW_EDGE_COUNTS,
    SKILLFLOW_LOOP_STATE,
    SKILLFLOW_OUTBOX,
    SKILLFLOW_TRACE,
]

# ── Migrations (run after DDL, errors are non-fatal) ──────────────────

SKILLFLOW_MIGRATIONS: list[str] = [
    "ALTER TABLE skillflow_runs ADD COLUMN graph_path TEXT;",
    # SF-24: set-based loop tracking
    "ALTER TABLE skillflow_loop_state ADD COLUMN completed_items TEXT",
    "ALTER TABLE skillflow_loop_state ADD COLUMN current_item TEXT",
    # SF-25: per-run completion order (see SKILLFLOW_STEPS.completion_seq)
    "ALTER TABLE skillflow_steps ADD COLUMN completion_seq INTEGER",
    # SF-26: fencing token (see SKILLFLOW_STEPS.claim_epoch). Existing rows
    # backfill to 0, which reads as "unfenced" on both sides of every check —
    # a claim already in flight when this ships is never falsely rejected.
    "ALTER TABLE skillflow_steps ADD COLUMN claim_epoch INTEGER NOT NULL DEFAULT 0",
    # Backfill historical rows by their best available approximation:
    # (completed_at, id). Idempotent — the IS NULL guard makes every re-run
    # after the first a no-op (migrations execute on each boot).
    """UPDATE skillflow_steps SET completion_seq = (
         SELECT COUNT(*) FROM skillflow_steps s2
         WHERE s2.run_id = skillflow_steps.run_id
           AND s2.status = 'completed'
           AND (s2.completed_at < skillflow_steps.completed_at
                OR (s2.completed_at = skillflow_steps.completed_at
                    AND s2.id <= skillflow_steps.id))
       )
       WHERE status = 'completed' AND completion_seq IS NULL""",
    # SF-27: loop-item attribution (see SKILLFLOW_STEPS.loop_item). Historical
    # rows stay NULL on purpose — the information was never recorded, and
    # guessing it from completion order would be wrong exactly where it matters
    # (a retried or looped-back body step).
    "ALTER TABLE skillflow_steps ADD COLUMN loop_item TEXT",
    # SF-28: pin a run to the graph CONTENT it started with (see
    # SKILLFLOW_GRAPH_VERSIONS). Rows written before this column existed stay
    # NULL and keep resolving by name — the content they ran is not recoverable,
    # and inventing a version for them would claim otherwise.
    #
    # First boot after this ships mints version 1 for every registered graph and
    # writes it back to skillflow_graphs.version, so that column DROPS (312 → 1
    # in the deployment above). That is the correction, not a loss: the old value
    # counted registrations of content that was never kept.
    "ALTER TABLE skillflow_runs ADD COLUMN graph_version INTEGER",
    "ALTER TABLE skillflow_runs ADD COLUMN graph_digest TEXT",
]
