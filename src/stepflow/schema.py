"""SQL DDL for stepflow tables.

All table names are prefixed ``stepflow_`` to coexist safely with
application tables in the same SQLite database.

Usage:
    from stepflow.schema import ALL_DDL
    for stmt in ALL_DDL:
        conn.execute(stmt)
"""

# ── Tables ──────────────────────────────────────────────────────────

STEPFLOW_GRAPHS = """
CREATE TABLE IF NOT EXISTS stepflow_graphs (
    name          TEXT PRIMARY KEY,
    yaml_text     TEXT NOT NULL,
    version       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

STEPFLOW_PROJECTS = """
CREATE TABLE IF NOT EXISTS stepflow_projects (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'active',
    meta_json   TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

STEPFLOW_RUNS = """
CREATE TABLE IF NOT EXISTS stepflow_runs (
    id              TEXT PRIMARY KEY,
    graph_name      TEXT NOT NULL,
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

STEPFLOW_STEPS = """
CREATE TABLE IF NOT EXISTS stepflow_steps (
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
    claimed_by              TEXT,
    completed_at            TEXT,
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at              TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (run_id) REFERENCES stepflow_runs(id)
);
"""

STEPFLOW_EDGE_COUNTS = """
CREATE TABLE IF NOT EXISTS stepflow_edge_counts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL,
    from_step       TEXT NOT NULL,
    to_step         TEXT NOT NULL,
    count           INTEGER NOT NULL DEFAULT 0,
    max_loop        INTEGER,
    FOREIGN KEY (run_id) REFERENCES stepflow_runs(id),
    UNIQUE(run_id, from_step, to_step)
);
"""

STEPFLOW_LOOP_STATE = """
CREATE TABLE IF NOT EXISTS stepflow_loop_state (
    run_id          TEXT NOT NULL,
    loop_step_id    TEXT NOT NULL,
    current_index   INTEGER NOT NULL DEFAULT 0,
    items_json      TEXT NOT NULL DEFAULT '[]',
    item_context_key TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (run_id) REFERENCES stepflow_runs(id),
    PRIMARY KEY (run_id, loop_step_id)
);
"""

STEPFLOW_OUTBOX = """
CREATE TABLE IF NOT EXISTS stepflow_outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type      TEXT NOT NULL,
    payload_json    TEXT NOT NULL DEFAULT '{}',
    stream_target   TEXT NOT NULL DEFAULT '__global__',
    status          TEXT NOT NULL DEFAULT 'pending',
    drain_started_at TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# ── Indexes ─────────────────────────────────────────────────────────

STEPFLOW_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_stepflow_runs_status ON stepflow_runs(status);",
    "CREATE INDEX IF NOT EXISTS idx_stepflow_runs_project ON stepflow_runs(project_id);",
    "CREATE INDEX IF NOT EXISTS idx_stepflow_steps_run ON stepflow_steps(run_id);",
    "CREATE INDEX IF NOT EXISTS idx_stepflow_steps_status ON stepflow_steps(status);",
    "CREATE INDEX IF NOT EXISTS idx_stepflow_steps_claimed ON stepflow_steps(claimed_at);",
    "CREATE INDEX IF NOT EXISTS idx_stepflow_edge_counts_run ON stepflow_edge_counts(run_id);",
    "CREATE INDEX IF NOT EXISTS idx_stepflow_outbox_status ON stepflow_outbox(status);",
    "CREATE INDEX IF NOT EXISTS idx_stepflow_loop_state_run ON stepflow_loop_state(run_id);",
]

# ── Idempotent migrations (run after CREATE TABLE IF NOT EXISTS) ─────

STEPFLOW_MIGRATIONS = [
    # v2: add project_id to runs for Wolverine-style project-scoped queries
    "ALTER TABLE stepflow_runs ADD COLUMN project_id TEXT",
    # v3: stepflow_loop_state may fail on existing DBs if already created
]
# These may throw on existing databases if column already exists.
# Run inside try/except OperationalError at connection init time.

# ── Ordered DDL list ────────────────────────────────────────────────

ALL_DDL: list[str] = [
    STEPFLOW_GRAPHS,
    STEPFLOW_PROJECTS,
    STEPFLOW_RUNS,
    STEPFLOW_STEPS,
    STEPFLOW_EDGE_COUNTS,
    STEPFLOW_LOOP_STATE,
    STEPFLOW_OUTBOX,
]
