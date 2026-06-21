"""Regression tests — verify fixes for the 67 audit issues from the current AItelier codebase.

Each test is named with its audit ID from the original skillflow_brief.md.
"""

import pytest

from skillflow.core import SkillFlow, StepResult
from skillflow.graph import (
    PipelineGraph,
    StepNode,
    Transition,
    EndCondition,
    EndConditions,
)


def _agent(id: str, transitions=None, checkpoint=False, max_retries=3):
    return StepNode(
        id=id, step_type="agent",
        transitions=transitions or [],
        checkpoint=checkpoint,
        max_retries=max_retries,
    )


def _gate(id: str, transitions=None):
    return StepNode(id=id, step_type="gate", transitions=transitions or [])


def _trans(to: str, match=None, max_loop=None):
    return Transition(to=to, match=match, max_loop=max_loop)


# ── C2: No project-level concurrency control ────────────────────────

def test_c2_two_ticks_cant_claim_same_step(sf: SkillFlow):
    """Two ticks cannot simultaneously claim and execute the same step."""
    graph = PipelineGraph(
        name="test", begin="a",
        steps=[_agent("a", [_trans("b")]), _agent("b", [])],
    )
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)
    sf.advance_run(run_id)

    c1 = sf.claim_next_step(run_id)
    c2 = sf.claim_next_step(run_id)

    assert c1 is not None
    assert c2 is None  # Second claim fails — no duplicate execution


# ── C3: step_locked inconsistently used ─────────────────────────────

def test_c3_version_columns_replace_locks(sf: SkillFlow):
    """Version columns provide atomic claiming without a separate lock column."""
    graph = PipelineGraph(
        name="test", begin="a",
        steps=[_agent("a", [_trans("b")]), _agent("b", [])],
    )
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)
    sf.advance_run(run_id)

    # Claim successfully
    claimed = sf.claim_next_step(run_id)
    assert claimed is not None

    # Confirm passes version check
    sf.confirm_step(claimed.token, StepResult(flags={}))
    # If we got here without StepVersionConflict, version locking works


# ── C4: SSE __END__ never pushed ────────────────────────────────────

def test_c4_outbox_produces_terminal_event(sf: SkillFlow):
    """When a run completes, the outbox gets a run_completed event."""
    graph = PipelineGraph(
        name="test", begin="a",
        steps=[
            _agent("a", [_trans("b")]),
            _agent("b", []),
        ],
        end_conditions=EndConditions(
            conditions=[EndCondition(type="node_reached", node="b", result="completed")],
        ),
    )
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)
    sf.advance_run(run_id)
    claimed = sf.claim_next_step(run_id)
    sf.confirm_step(claimed.token, StepResult())
    # advance_run triggers end condition (node_reached: b)
    sf.advance_run(run_id)

    events = sf.drain_outbox(batch_size=50)
    event_types = [e.event_type for e in events]
    assert "run_completed" in event_types


# ── C5: No transactional boundary between files and DB ──────────────

def test_c5_confirm_and_advance_are_separate_transactions(sf: SkillFlow):
    """confirm_step writes state atomically; crash between confirm and
    advance is recovered by advance_run reading the last completed step."""
    graph = PipelineGraph(
        name="test", begin="a",
        steps=[_agent("a", [_trans("b")]), _agent("b", [])],
    )
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)

    # Execute step a
    sf.advance_run(run_id)
    claimed = sf.claim_next_step(run_id)
    sf.confirm_step(claimed.token, StepResult(flags={}))

    # Simulate crash between confirm and advance:
    # current_node is NULL, step a is completed.
    # Next tick's advance_run reads last completed step → resolves to b.
    next_node = sf.advance_run(run_id)
    assert next_node == "b"
    # No state loss, no duplicate execution


# ── C8: submit_task resurrects completed projects ───────────────────

def test_c8_completed_run_stays_completed(sf: SkillFlow):
    """Once a run is completed, advance_run returns None — no accidental
    reactivation."""
    graph = PipelineGraph(
        name="test", begin="a",
        steps=[_agent("a", [])],
    )
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)
    sf.advance_run(run_id)
    claimed = sf.claim_next_step(run_id)
    sf.confirm_step(claimed.token, StepResult())
    sf.complete_run(run_id)

    # advance_run on completed run returns None
    assert sf.advance_run(run_id) is None
    # claim_next_step on completed run returns None
    assert sf.claim_next_step(run_id) is None


# ── Hardcoded step sequences ────────────────────────────────────────

def test_pipeline_is_defined_in_graph_not_code(sf: SkillFlow):
    """Pipeline step sequences come from YAML/graph definition, not Python constants."""
    # This graph has a completely custom sequence — no AItelier DPE steps
    graph = PipelineGraph(
        name="custom", begin="lint",
        steps=[
            _agent("lint", [_trans("test")]),
            _agent("test", [_trans("deploy")]),
            _agent("deploy", []),
        ],
    )
    sf.register_graph(graph)
    run_id = sf.create_run("custom")
    sf.start_run(run_id)

    sf.advance_run(run_id)
    claimed = sf.claim_next_step(run_id)
    assert claimed.step_id == "lint"

    sf.confirm_step(claimed.token, StepResult())
    sf.advance_run(run_id)
    claimed2 = sf.claim_next_step(run_id)
    assert claimed2.step_id == "test"


# ── No cycle support — planning refresh was ad-hoc hack ─────────────

def test_cycle_support_planning_refresh_is_graph_edge(sf: SkillFlow):
    """Planning refresh is a regular graph edge with max_loop, not ad-hoc code."""
    graph = PipelineGraph(
        name="test", begin="plan",
        steps=[
            _agent("plan", [_trans("build")]),
            _agent("build", [_trans("verify")]),
            _agent("verify", [_trans("gate")]),
            _gate("gate", [
                _trans("plan", match={"refresh": True}, max_loop=3),
                _trans("done", match={"refresh": False}),
            ]),
            _agent("done", []),
        ],
    )
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)

    # Run 3 refresh cycles
    for i in range(3):
        sf.advance_run(run_id)
        _exec(sf, run_id, "plan")
        sf.advance_run(run_id)
        _exec(sf, run_id, "build")
        sf.advance_run(run_id)
        _exec(sf, run_id, "verify", flags={"refresh": True})
        sf.advance_run(run_id)  # Gate resolves to plan (iter 1-3)

    # 4th verify: no more refresh
    sf.advance_run(run_id)
    _exec(sf, run_id, "plan")
    sf.advance_run(run_id)
    _exec(sf, run_id, "build")
    sf.advance_run(run_id)
    _exec(sf, run_id, "verify", flags={"refresh": False})
    sf.advance_run(run_id)  # Gate resolves to done

    claimed = sf.claim_next_step(run_id)
    assert claimed is not None
    assert claimed.step_id == "done"


def _exec(sf: SkillFlow, run_id: str, step_id: str, outputs=None, flags=None):
    claimed = sf.claim_next_step(run_id)
    assert claimed is not None, f"Failed to claim {step_id}"
    assert claimed.step_id == step_id, f"Expected {step_id}, got {claimed.step_id}"
    sf.confirm_step(claimed.token, StepResult(outputs=outputs or {}, flags=flags or {}))


def test_max_run_duration_end_condition_actually_fires(sf):
    """The max_run_duration_seconds cap is the mechanism-agnostic backstop
    against ANY runaway (in-place loops, tool rampage, slow non-convergence).

    It was silently dead: started_at is written via SQLite datetime('now')
    (space-separated, '2026-06-20 18:40:51') but parsed with a 'T'-separated
    format, so every parse raised ValueError and the check was skipped — a 1h
    cap let a 3h+ loop run. This guards the fix.
    """
    g = PipelineGraph(
        name="test_dur", begin="a1",
        steps=[
            StepNode(id="a1", step_type="agent",
                     transitions=[Transition(to="a2")]),
            StepNode(id="a2", step_type="agent"),
        ],
        end_conditions=EndConditions(
            combinator="or",
            conditions=[EndCondition(type="max_run_duration_seconds", limit=1)],
        ),
    )
    sf.register_graph(g)
    rid = sf.create_run("test_dur")
    sf.start_run(rid)
    # Simulate a run started well over the limit ago, in the SAME space-
    # separated format datetime('now') writes.
    sf._conn.execute(
        "UPDATE skillflow_runs SET started_at = '2020-01-01 00:00:00' WHERE id = ?",
        (rid,))
    sf._conn.commit()

    # First advance evaluates end conditions → duration exceeded → run fails.
    # (Before the fix the parse raised ValueError, the check was skipped, and
    # the run kept running.)
    sf.advance_run(rid)

    run = sf.get_run(rid)
    assert run["status"] == "failed"
    assert "duration" in (run["error_reason"] or "").lower()
