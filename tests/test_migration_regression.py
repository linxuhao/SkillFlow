"""Regression tests for AItelier migration to skillflow.

Verifies the 67 audit issues are addressed by the skillflow architecture.
"""

import pytest

from skillflow.core import SkillFlow, StepResult
from skillflow.graph import PipelineGraph, StepNode, Transition, EndCondition, EndConditions


def _agent(id: str, transitions=None, max_retries=3):
    return StepNode(
        id=id, step_type="agent",
        transitions=transitions or [],
        max_retries=max_retries,
    )


def _trans(to: str, match=None, max_loop=None):
    return Transition(to=to, match=match, max_loop=max_loop)


# ── C2: No project-level concurrency control ────────────────────────

def test_c2_no_duplicate_project_execution(sf: SkillFlow):
    """Two concurrent scheduler ticks can't run the same step twice."""
    graph = PipelineGraph(
        name="test", begin="a",
        steps=[_agent("a")],
    )
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)
    sf.advance_run(run_id)

    # Two claims — only one succeeds
    c1 = sf.claim_next_step(run_id)
    c2 = sf.claim_next_step(run_id)
    assert c1 is not None
    assert c2 is None


# ── C3: step_locked inconsistent ────────────────────────────────────

def test_c3_version_columns_replace_step_locked(sf: SkillFlow):
    """Version columns provide atomicity, no step_locked column needed."""
    graph = PipelineGraph(
        name="test", begin="a",
        steps=[_agent("a", [_trans("b")]), _agent("b", [])],
    )
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)
    sf.advance_run(run_id)

    claimed = sf.claim_next_step(run_id)
    assert claimed is not None

    # Confirm with correct version
    sf.confirm_step(claimed.token, StepResult(flags={}))

    # Try to confirm again with stale token — must fail
    with pytest.raises(Exception):
        sf.confirm_step(claimed.token, StepResult())


# ── C5: No transactional boundary between files and DB ──────────────

def test_c5_atomic_state_transitions(sf: SkillFlow):
    """confirm_step is atomic — no partial state between files and DB."""
    graph = PipelineGraph(
        name="test", begin="a",
        steps=[_agent("a", [_trans("b")]), _agent("b", [])],
    )
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)
    sf.advance_run(run_id)

    claimed = sf.claim_next_step(run_id)
    sf.confirm_step(claimed.token, StepResult(flags={}))

    # Between confirm and advance, step is completed, current_node is NULL.
    # Next advance_run reads the last completed step — no state loss.
    next_node = sf.advance_run(run_id)
    assert next_node == "b"


# ── C4: SSE __END__ never pushed ────────────────────────────────────

def test_c4_terminal_events_produced(sf: SkillFlow):
    """run_completed event is emitted to outbox."""
    graph = PipelineGraph(
        name="test", begin="a",
        steps=[_agent("a", [_trans("b")]), _agent("b", [])],
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
    sf.advance_run(run_id)  # Triggers end condition

    events = sf.drain_outbox(batch_size=50)
    event_types = [e.event_type for e in events]
    assert "run_completed" in event_types


# ── C8: submit_task resurrects completed projects ───────────────────

def test_c8_completed_run_cannot_be_reactivated(sf: SkillFlow):
    """A completed skillflow run cannot be accidentally restarted."""
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

    # advance_run on completed returns None
    assert sf.advance_run(run_id) is None
    assert sf.get_run(run_id)["status"] == "completed"


def test_reactivate_rejects_resume_step_removed_from_graph(sf: SkillFlow):
    """Reactivating a failed run whose resume step was removed from the graph
    must fail loudly, not silently wedge.

    If a node is deleted from the graph after a run started, pointing
    current_node at it makes advance_run() return None forever (a silent
    deadlock). reactivate_run must raise instead so the caller can tell the
    user to start a fresh run.
    """
    graph = PipelineGraph(
        name="t", begin="a",
        steps=[_agent("a", [_trans("b")]), _agent("b", [])],
    )
    sf.register_graph(graph)
    run_id = sf.create_run("t")
    sf.start_run(run_id)
    sf.advance_run(run_id)
    ca = sf.claim_next_step(run_id)
    sf.confirm_step(ca.token, StepResult())   # 'a' completed → current_node 'b'

    # Drive 'b' to failure (exhaust retries) so the run ends up 'failed'.
    for i in range(5):
        sf.advance_run(run_id)
        cb = sf.claim_next_step(run_id)
        if cb is None:
            break
        sf.fail_step(cb.token, f"Error {i + 1}", retryable=True)
    assert sf.get_run(run_id)["status"] == "failed"

    # Graph changes underneath the run: the resume step (last-completed 'a') is
    # gone. Re-registering overwrites the cached resolver, exactly as a process
    # restart would after the YAML changed.
    sf.register_graph(
        PipelineGraph(name="t", begin="x", steps=[_agent("x", [])]))

    with pytest.raises(ValueError, match="no longer exists in graph"):
        sf.reactivate_run(run_id)
    # Transaction rolled back — the run is left cleanly failed, not wedged.
    assert sf.get_run(run_id)["status"] == "failed"


# ── Hardcoded step sequences → YAML graph ───────────────────────────

def test_pipeline_in_yaml_not_code(sf: SkillFlow):
    """Custom pipeline works without changing Python constants."""
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

    steps = []
    while True:
        n = sf.advance_run(run_id)
        if n is None:
            break
        claimed = sf.claim_next_step(run_id)
        if claimed is None:
            break
        steps.append(claimed.step_id)
        sf.confirm_step(claimed.token, StepResult())

    assert steps == ["lint", "test", "deploy"]
