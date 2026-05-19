"""Stale claim recovery.

Detects and recovers steps that were claimed but the claiming process
crashed before confirming or failing. These "stale" claims are reset
to pending so the step can be re-claimed on the next scheduler tick.
"""

from __future__ import annotations

import sqlite3
import time

from stepflow.schema import ALL_DDL


def recover_stale_claims(
    db_path: str, stale_threshold_seconds: float = 300
) -> list[str]:
    """Reset stale claimed steps to pending.

    A step is stale if it has status 'claimed' and its ``claimed_at``
    timestamp is older than ``stale_threshold_seconds`` from now.

    Also clears ``current_node`` on affected runs so ``advance_run`` can
    re-resolve the next step.

    Returns the list of affected run_ids.
    """
    threshold = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ",
        time.gmtime(time.time() - stale_threshold_seconds),
    )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")

    try:
        # Ensure tables exist
        for stmt in ALL_DDL:
            conn.execute(stmt)

        conn.execute("BEGIN IMMEDIATE;")

        stale_rows = conn.execute(
            """
            SELECT id, run_id, step_id FROM stepflow_steps
            WHERE status = 'claimed' AND claimed_at < ?
            """,
            (threshold,),
        ).fetchall()

        run_ids: set[str] = set()
        for row in stale_rows:
            conn.execute(
                """
                UPDATE stepflow_steps
                SET status = 'pending', version = version + 1,
                    claimed_at = NULL, claimed_by = NULL,
                    updated_at = datetime('now')
                WHERE id = ?
                """,
                (row["id"],),
            )
            conn.execute(
                "UPDATE stepflow_runs SET current_node = NULL WHERE id = ?",
                (row["run_id"],),
            )
            run_ids.add(row["run_id"])

        conn.commit()
        return list(run_ids)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
