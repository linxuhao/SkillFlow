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
    claimed_by              TEXT,
    completed_at            TEXT,
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
    item_context_key TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (run_id) REFERENCES skillflow_runs(id),
    PRIMARY KEY (run_id, loop_step_id)
);
"""

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
]

# ── Ordered DDL list ────────────────────────────────────────────────

ALL_DDL: list[str] = [
    SKILLFLOW_GRAPHS,
    SKILLFLOW_PROJECTS,
    SKILLFLOW_RUNS,
    SKILLFLOW_STEPS,
    SKILLFLOW_EDGE_COUNTS,
    SKILLFLOW_LOOP_STATE,
    SKILLFLOW_OUTBOX,
]

# ── Migrations (run after DDL, errors are non-fatal) ──────────────────

SKILLFLOW_MIGRATIONS: list[str] = [
    "ALTER TABLE skillflow_runs ADD COLUMN graph_path TEXT;",
]
