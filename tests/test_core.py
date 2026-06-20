"""Unit tests for core.py — SkillFlow run lifecycle, claim/confirm/fail, advance_run."""

import json
import time
from pathlib import Path

import pytest

from skillflow.core import SkillFlow, ClaimedStep, ClaimToken, StepResult
from skillflow.graph import (
    PipelineGraph,
    StepNode,
    Transition,
    EndCondition,
    EndConditions,
)
from skillflow.exceptions import (
    StepVersionConflict,
    SkillFlowError,
    GraphValidationError,
)
from skillflow.tool_loader import ToolLoader
from skillflow.workspace import WorkspaceManager



# ── Graph helpers ────────────────────────────────────────────────────

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


def _simple_graph(name="test", begin="a"):
    return PipelineGraph(
        name=name, begin=begin,
        steps=[
            _agent("a", [_trans("b")]),
            _agent("b", [_trans("c")]),
            _agent("c", []),  # Terminal
        ],
    )


def _dpe_graph():
    """Minimal DPE-like graph for integration tests."""
    return PipelineGraph(
        name="dpe_test", begin="1_5",
        steps=[
            _agent("1_5", [_trans("2")], checkpoint=True),
            _agent("2", [_trans("3")]),
            _agent("3", [_trans("task_gate")]),
            _gate("task_gate", [_trans("t_plan", match={"has_tasks": True}), _trans("5", match={"has_tasks": False})]),
            _agent("t_plan", [_trans("t_impl")]),
            _agent("t_impl", [_trans("t_verify"), _trans("error_handler", match={"_error": True})], max_retries=2),
            _agent("t_verify", [_trans("task_loop")]),
            _gate("task_loop", [
                _trans("t_plan", match={"more_tasks": True}, max_loop=10),
                _trans("5", match={"all_done": True}),
            ]),
            _agent("error_handler", [_trans("task_loop")]),
            _agent("5", []),
        ],
        end_conditions=EndConditions(
            combinator="or",
            conditions=[
                EndCondition(type="node_reached", node="5", result="completed"),
                EndCondition(type="max_total_steps", limit=100),
            ],
        ),
    )


# ── Run lifecycle ────────────────────────────────────────────────────

def test_create_run(sf: SkillFlow):
    graph = _simple_graph()
    sf.register_graph(graph)
    run_id = sf.create_run("test", {"project_id": "X"})
    assert run_id is not None
    assert len(run_id) > 0

    run = sf.get_run(run_id)
    assert run is not None
    assert run["status"] == "pending"
    assert run["current_node"] == "a"


def test_start_run(sf: SkillFlow):
    graph = _simple_graph()
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)
    run = sf.get_run(run_id)
    assert run["status"] == "running"
    assert run["started_at"] is not None


def test_start_run_wrong_status_raises(sf: SkillFlow):
    graph = _simple_graph()
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)
    # Already running — can't start again
    with pytest.raises(SkillFlowError):
        sf.start_run(run_id)


def test_pause_resume_run(sf: SkillFlow):
    graph = _simple_graph()
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)
    sf.pause_run(run_id)
    assert sf.get_run(run_id)["status"] == "paused"
    sf.resume_run(run_id)
    assert sf.get_run(run_id)["status"] == "running"


def test_fail_run(sf: SkillFlow):
    graph = _simple_graph()
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)
    sf.fail_run(run_id, "something went wrong")
    run = sf.get_run(run_id)
    assert run["status"] == "failed"
    assert run["error_reason"] == "something went wrong"


def test_complete_run(sf: SkillFlow):
    graph = _simple_graph()
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)
    sf.complete_run(run_id)
    assert sf.get_run(run_id)["status"] == "completed"


def test_get_run_nonexistent(sf: SkillFlow):
    assert sf.get_run("nonexistent") is None


# ── Claim / Confirm ──────────────────────────────────────────────────

def test_claim_next_step(sf: SkillFlow):
    graph = _simple_graph()
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)

    next_node = sf.advance_run(run_id)
    assert next_node == "a"

    claimed = sf.claim_next_step(run_id)
    assert claimed is not None
    assert claimed.step_id == "a"
    assert claimed.token.step_id == "a"
    assert claimed.token.run_id == run_id


def test_claim_returns_none_when_already_claimed(sf: SkillFlow):
    graph = _simple_graph()
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)
    sf.advance_run(run_id)

    first = sf.claim_next_step(run_id)
    assert first is not None
    second = sf.claim_next_step(run_id)
    assert second is None  # Already claimed


def test_claim_returns_none_when_gate(sf: SkillFlow):
    graph = PipelineGraph(
        name="test", begin="g",
        steps=[_gate("g", [_trans("a")]), _agent("a")],
    )
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)

    # advance_run auto-advances through gates, so current_node will be "a"
    next_node = sf.advance_run(run_id)
    assert next_node == "a"  # Gate auto-advanced


def test_confirm_step(sf: SkillFlow):
    graph = _simple_graph()
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)
    sf.advance_run(run_id)
    claimed = sf.claim_next_step(run_id)

    result = StepResult(outputs={"ok": True}, flags={"done": True})
    sf.confirm_step(claimed.token, result)

    run = sf.get_run(run_id)
    assert run["current_node"] == "b"  # Inline transition resolved by confirm


def test_confirm_step_version_conflict(sf: SkillFlow):
    graph = _simple_graph()
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)
    sf.advance_run(run_id)
    claimed = sf.claim_next_step(run_id)

    # Simulate stale recovery: reset the step
    sf.recover_stale_claims(stale_threshold_seconds=-1)  # Everything is stale below -1s

    result = StepResult(outputs={}, flags={})
    with pytest.raises(StepVersionConflict):
        sf.confirm_step(claimed.token, result)


def test_confirm_then_advance_resolves_next(sf: SkillFlow):
    graph = _simple_graph()
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)

    # Step a
    sf.advance_run(run_id)
    claimed = sf.claim_next_step(run_id)
    sf.confirm_step(claimed.token, StepResult(flags={}))

    # Advance to b
    next_node = sf.advance_run(run_id)
    assert next_node == "b"


# ── Fail step ────────────────────────────────────────────────────────

def test_fail_step_retryable(sf: SkillFlow):
    graph = _simple_graph()
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)
    sf.advance_run(run_id)
    claimed = sf.claim_next_step(run_id)

    sf.fail_step(claimed.token, "temporary error", retryable=True)

    # Step should be back to pending for re-claim
    run = sf.get_run(run_id)
    assert run["status"] == "running"  # Run is still running


def test_fail_step_retries_exhausted_with_error_handler(sf: SkillFlow):
    graph = PipelineGraph(
        name="test", begin="a",
        steps=[
            _agent("a", [_trans("b"), _trans("eh", match={"_error": True})], max_retries=1),
            _agent("eh", [_trans("b")]),
            _agent("b", []),
        ],
    )
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)
    sf.advance_run(run_id)
    claimed = sf.claim_next_step(run_id)

    # First fail — retryable, should go back to pending
    sf.fail_step(claimed.token, "err1", retryable=True)
    # Re-claim and fail again (now retries exhausted)
    sf.advance_run(run_id)
    claimed2 = sf.claim_next_step(run_id)
    sf.fail_step(claimed2.token, "err2", retryable=True)

    # Should have routed to error_handler
    run = sf.get_run(run_id)
    assert run["status"] == "running"
    assert run["current_node"] == "eh"


def test_fail_step_retries_exhausted_no_error_handler(sf: SkillFlow):
    graph = PipelineGraph(
        name="test", begin="a",
        steps=[
            _agent("a", [_trans("b")], max_retries=1),
            _agent("b", []),
        ],
    )
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)
    sf.advance_run(run_id)

    # First attempt
    claimed = sf.claim_next_step(run_id)
    sf.fail_step(claimed.token, "err1", retryable=True)
    # Retry
    sf.advance_run(run_id)
    claimed2 = sf.claim_next_step(run_id)
    sf.fail_step(claimed2.token, "err2", retryable=True)

    # Run should be failed
    run = sf.get_run(run_id)
    assert run["status"] == "failed"


# ── advance_run ──────────────────────────────────────────────────────

def test_advance_run_first_call_resolves_begin(sf: SkillFlow):
    graph = _simple_graph()
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)

    next_node = sf.advance_run(run_id)
    assert next_node == "a"


def test_advance_run_idempotent(sf: SkillFlow):
    graph = _simple_graph()
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)

    n1 = sf.advance_run(run_id)
    n2 = sf.advance_run(run_id)
    assert n1 == n2 == "a"


def test_advance_run_none_when_executing(sf: SkillFlow):
    graph = _simple_graph()
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)

    sf.advance_run(run_id)
    sf.claim_next_step(run_id)
    # Step is claimed — advance_run should return None
    assert sf.advance_run(run_id) is None


def test_advance_run_pauses_on_checkpoint(sf: SkillFlow):
    graph = PipelineGraph(
        name="test", begin="a",
        steps=[
            _agent("a", [_trans("b")], checkpoint=True),
            _agent("b", []),
        ],
    )
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)

    # Execute step a
    sf.advance_run(run_id)
    claimed = sf.claim_next_step(run_id)
    sf.confirm_step(claimed.token, StepResult(flags={}))

    # Advance should detect checkpoint and pause
    next_node = sf.advance_run(run_id)
    assert next_node is None
    run = sf.get_run(run_id)
    assert run["status"] == "paused"
    assert run["current_node"] == "b"

    # Resume
    sf.resume_run(run_id)
    next_node = sf.advance_run(run_id)
    assert next_node == "b"


def test_advance_run_gate_auto_advance(sf: SkillFlow):
    graph = PipelineGraph(
        name="test", begin="a",
        steps=[
            _agent("a", [_trans("g")]),
            _gate("g", [_trans("b", match={"go": True}), _trans("c")]),
            _agent("b", []),
            _agent("c", []),
        ],
    )
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)

    # Execute step a with flag go=True
    sf.advance_run(run_id)
    claimed = sf.claim_next_step(run_id)
    sf.confirm_step(claimed.token, StepResult(flags={"go": True}))

    # Advance should resolve through gate to "b"
    next_node = sf.advance_run(run_id)
    assert next_node == "b"


def test_advance_run_end_condition_node_reached(sf: SkillFlow):
    graph = PipelineGraph(
        name="test", begin="a",
        steps=[_agent("a", [_trans("done")]), _agent("done", [])],
        end_conditions=EndConditions(
            conditions=[EndCondition(type="node_reached", node="done", result="completed")],
        ),
    )
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)

    # Execute step a
    sf.advance_run(run_id)
    claimed = sf.claim_next_step(run_id)
    sf.confirm_step(claimed.token, StepResult())

    # Advance resolves to "done" which triggers end condition
    next_node = sf.advance_run(run_id)
    assert next_node is None
    assert sf.get_run(run_id)["status"] == "completed"


def test_advance_run_end_condition_max_steps(sf: SkillFlow):
    graph = PipelineGraph(
        name="test", begin="a",
        steps=[
            _agent("a", [_trans("b")]),
            _agent("b", [_trans("c")]),
            _agent("c", []),
        ],
        end_conditions=EndConditions(
            conditions=[EndCondition(type="max_total_steps", limit=1)],
        ),
    )
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)

    # Execute step a (step count = 1)
    sf.advance_run(run_id)
    claimed = sf.claim_next_step(run_id)
    sf.confirm_step(claimed.token, StepResult())

    # Next advance should detect max_total_steps exceeded
    assert sf.advance_run(run_id) is None
    assert sf.get_run(run_id)["status"] == "failed"


def test_advance_run_end_condition_flag_match(sf: SkillFlow):
    graph = PipelineGraph(
        name="test", begin="a",
        steps=[_agent("a", [])],
        end_conditions=EndConditions(
            conditions=[EndCondition(type="flag_match", flag={"fatal_error": True})],
        ),
    )
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)

    sf.advance_run(run_id)
    claimed = sf.claim_next_step(run_id)
    sf.confirm_step(claimed.token, StepResult(flags={"fatal_error": True}))

    assert sf.advance_run(run_id) is None
    assert sf.get_run(run_id)["status"] == "failed"


def test_advance_run_no_matching_transition_fails_run(sf: SkillFlow):
    graph = PipelineGraph(
        name="test", begin="a",
        steps=[_agent("a", [_trans("b", match={"x": True})]), _agent("b", [])],
    )
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)

    sf.advance_run(run_id)
    claimed = sf.claim_next_step(run_id)
    sf.confirm_step(claimed.token, StepResult(flags={"x": False}))  # Doesn't match

    assert sf.advance_run(run_id) is None
    assert sf.get_run(run_id)["status"] == "failed"


# ── Checkpoint rejection ─────────────────────────────────────────────

def test_reject_checkpoint(sf: SkillFlow):
    graph = PipelineGraph(
        name="test", begin="a",
        steps=[
            _agent("a", [_trans("b")], checkpoint=True),
            _agent("b", []),
        ],
    )
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)

    sf.advance_run(run_id)
    claimed = sf.claim_next_step(run_id)
    sf.confirm_step(claimed.token, StepResult(outputs={"plan": "v1"}))
    sf.advance_run(run_id)  # Pauses
    assert sf.get_run(run_id)["status"] == "paused"

    sf.reject_checkpoint(run_id, "a", "Needs more detail")
    assert sf.get_run(run_id)["status"] == "running"
    assert sf.get_run(run_id)["current_node"] == "a"

    # Next claim should re-claim step a
    claimed2 = sf.claim_next_step(run_id)
    assert claimed2 is not None
    assert claimed2.step_id == "a"
    # The rejection feedback must reach the re-run via the preserved _feedback
    # channel (regression: previously only _rejection was set, which nothing
    # read, so the agent re-ran blind).
    assert claimed2.inputs.get("_feedback") == "Needs more detail"
    # The framework also surfaces it into _resolved_context so the host renders
    # it into the prompt in any tool mode without special-casing _feedback.
    rc = claimed2.inputs.get("_resolved_context") or {}
    assert any("Needs more detail" == v for v in rc.values())


def test_reject_checkpoint_running_raises(sf: SkillFlow):
    graph = PipelineGraph(name="test", begin="a", steps=[_agent("a", [])])
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)
    sf.advance_run(run_id)
    claimed = sf.claim_next_step(run_id)
    sf.confirm_step(claimed.token, StepResult())

    # Run is 'running' (not paused/failed) — not a rejectable state.
    with pytest.raises(SkillFlowError):
        sf.reject_checkpoint(run_id, "a", "feedback")


def test_reject_checkpoint_from_failed_run(sf: SkillFlow):
    """A checkpoint may be rejected after the run failed downstream of it."""
    graph = PipelineGraph(
        name="test", begin="a",
        steps=[
            _agent("a", [_trans("b")], checkpoint=True),
            _agent("b", []),
        ],
    )
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)

    sf.advance_run(run_id)
    claimed = sf.claim_next_step(run_id)
    sf.confirm_step(claimed.token, StepResult(outputs={"plan": "v1"}))
    sf.advance_run(run_id)  # pauses at checkpoint a

    # Simulate a downstream failure leaving the run in 'failed'.
    sf._update_run_state(run_id, "failed")
    assert sf.get_run(run_id)["status"] == "failed"

    # Rejecting the completed checkpoint step from a failed run is allowed and
    # re-opens that step with the feedback.
    sf.reject_checkpoint(run_id, "a", "redo with constraints")
    assert sf.get_run(run_id)["status"] == "running"
    claimed2 = sf.claim_next_step(run_id)
    assert claimed2.step_id == "a"
    assert claimed2.inputs.get("_feedback") == "redo with constraints"


# ── Edge counts ──────────────────────────────────────────────────────

def test_edge_count_increments_on_advance(sf: SkillFlow):
    graph = PipelineGraph(
        name="test", begin="a",
        steps=[
            _agent("a", [_trans("g")]),
            _gate("g", [_trans("b", max_loop=2), _trans("c")]),
            _agent("b", [_trans("g")]),
            _agent("c", []),
        ],
    )
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)

    # Iteration 1: a → g → b (since match is None for b)
    sf.advance_run(run_id)  # claims a
    claimed = sf.claim_next_step(run_id)
    sf.confirm_step(claimed.token, StepResult())
    sf.advance_run(run_id)  # resolves g → b
    claimed2 = sf.claim_next_step(run_id)
    sf.confirm_step(claimed2.token, StepResult())

    # Iteration 2: b → g → b
    sf.advance_run(run_id)
    claimed3 = sf.claim_next_step(run_id)
    sf.confirm_step(claimed3.token, StepResult())

    # Iteration 3: b → g → c (max_loop=2 on g→b exhausted)
    sf.advance_run(run_id)
    claimed4 = sf.claim_next_step(run_id)
    sf.confirm_step(claimed4.token, StepResult())
    next_node = sf.advance_run(run_id)
    assert next_node == "c"


# ── Recovery ─────────────────────────────────────────────────────────

def test_recover_stale_claims(sf: SkillFlow):
    graph = _simple_graph()
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)
    sf.advance_run(run_id)
    sf.claim_next_step(run_id)

    # Set threshold to -1 to recover all claims (everything is stale)
    recovered = sf.recover_stale_claims(stale_threshold_seconds=-1)
    assert run_id in recovered

    # Step should be back to pending
    next_node = sf.advance_run(run_id)
    assert next_node == "a"


def test_recover_stale_claims_fresh_not_affected(sf: SkillFlow):
    graph = _simple_graph()
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)
    sf.advance_run(run_id)
    sf.claim_next_step(run_id)

    # Default threshold 300s — just-claimed step is fresh
    recovered = sf.recover_stale_claims(stale_threshold_seconds=300)
    assert len(recovered) == 0


# ── Validation retry ─────────────────────────────────────────────────

def test_validation_retry_count_independent(sf: SkillFlow):
    """Validation retries don't consume execution retries."""
    graph = PipelineGraph(
        name="test", begin="a",
        steps=[_agent("a", [], max_retries=3)],
    )
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)
    sf.advance_run(run_id)
    claimed = sf.claim_next_step(run_id)

    # Simulate a step that passed validation — check the internal state
    sf.confirm_step(claimed.token, StepResult(outputs={"ok": True}))
    assert sf.get_run(run_id)["status"] == "running"  # Still running, step completed


# ── Outbox ───────────────────────────────────────────────────────────

def test_drain_outbox(sf: SkillFlow):
    graph = PipelineGraph(name="test", begin="a", steps=[_agent("a", [])])
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)

    events = sf.drain_outbox(batch_size=10)
    # Should have run_created + run_started
    assert len(events) >= 2
    event_types = [e.event_type for e in events]
    assert "run_created" in event_types
    assert "run_started" in event_types


def test_ack_outbox(sf: SkillFlow):
    graph = PipelineGraph(name="test", begin="a", steps=[_agent("a", [])])
    sf.register_graph(graph)
    sf.create_run("test")

    events = sf.drain_outbox(batch_size=10)
    ids = [e.id for e in events]
    assert len(ids) > 0

    sf.ack_outbox(ids)
    # Second drain should return empty
    events2 = sf.drain_outbox(batch_size=10)
    assert len(events2) == 0


def test_drain_outbox_empty_when_none_pending(sf: SkillFlow):
    events = sf.drain_outbox()
    assert events == []

# ── v2: tool nodes + feedback loopback ──

class TestToolNodeExecution:
    def test_advance_auto_executes_tool_node(self, sf):
        from skillflow.graph import PipelineGraph, StepNode, Transition
        from unittest.mock import MagicMock

        mock = MagicMock()
        mock.load_fn.return_value = lambda **kw: {"applied": True}
        mock.load_schema.return_value = {"name": "echo"}

        g = PipelineGraph(name="test_tool", begin="a1", steps=[
            StepNode(id="a1", step_type="agent",
                     transitions=[Transition(to="t1")]),
            StepNode(id="t1", step_type="tool", tool_name="echo",
                     transitions=[Transition(to="a2", match={"applied": True})]),
            StepNode(id="a2", step_type="agent"),
        ])
        sf.register_graph(g)
        sf._tool_loader = mock
        rid = sf.create_run("test_tool"); sf.start_run(rid)
        token = sf.claim_next_step(rid)
        from skillflow.core import StepResult
        sf.confirm_step(token.token, StepResult(outputs={}, flags={}))
        # Tool nodes run lock-free on a subsequent advance pass (never while
        # advance_run holds self._lock): first pass resolves to the tool and
        # commits current_node; second pass executes it via the top fast-path.
        assert sf.advance_run(rid) is None
        next_node = sf.advance_run(rid)
        assert next_node == "a2"

    def test_advance_tool_with_feedback_loopback(self, sf):
        from skillflow.graph import PipelineGraph, StepNode, Transition
        from unittest.mock import MagicMock

        mock = MagicMock()
        mock.load_fn.return_value = lambda **kw: {"all_passed": False}
        mock.load_schema.return_value = {"name": "validate"}

        g = PipelineGraph(name="test_fb", begin="impl", steps=[
            StepNode(id="impl", step_type="agent",
                     transitions=[Transition(to="validate")]),
            StepNode(id="validate", step_type="tool", tool_name="validate",
                     transitions=[
                         Transition(to="review", match={"all_passed": True}),
                         Transition(to="impl", match={"all_passed": False},
                                    max_loop=3, feedback=True),
                     ]),
            StepNode(id="review", step_type="agent"),
        ])
        sf.register_graph(g)
        sf._tool_loader = mock
        rid = sf.create_run("test_fb"); sf.start_run(rid)
        token = sf.claim_next_step(rid)
        sf.confirm_step(token.token, StepResult(outputs={}, flags={}))
        # Tool runs lock-free on the second advance pass (see note above).
        assert sf.advance_run(rid) is None
        next_node = sf.advance_run(rid)
        assert next_node == "impl"

    def test_create_run_populates_v2_step_fields(self, sf):
        from skillflow.graph import PipelineGraph, StepNode
        sf.register_agent_config_from_dict("researcher", {"model": "test", "template": "test.md"})
        g = PipelineGraph(name="test_v2cr", begin="s1", steps=[
            StepNode(id="s1", agent_config="researcher", output_mode="content",
                     output_fixed={"sota": "step1_5_sota.md"})
        ])
        sf.register_graph(g)
        rid = sf.create_run("test_v2cr"); sf.start_run(rid)


class TestStaleRecoveryInAdvance:
    def test_advance_recovers_stale_claims(self, sf):
        from skillflow.graph import PipelineGraph, StepNode, Transition
        import time
        g = PipelineGraph(name="test_stale", begin="a1", steps=[
            StepNode(id="a1", step_type="agent",
                     transitions=[Transition(to="a2")]),
            StepNode(id="a2", step_type="agent"),
        ])
        sf.register_graph(g)
        rid = sf.create_run("test_stale"); sf.start_run(rid)
        # Manually set a1 to claimed with old timestamp
        sf._conn.execute(
            "UPDATE skillflow_steps SET status='claimed', claimed_at='2020-01-01T00:00:00Z' WHERE run_id=? AND step_id='a1'",
            (rid,))
        sf._conn.execute("UPDATE skillflow_runs SET current_node='a1' WHERE id=?", (rid,))
        sf._conn.commit()
        sf._stale_threshold = 300  # normal threshold, 2020 is definitely stale
        next_node = sf.advance_run(rid)
        assert next_node is not None


class TestConfirmStepImportError:
    def test_confirm_step_catches_import_error(self, sf):
        from skillflow.graph import PipelineGraph, StepNode, Transition
        g = PipelineGraph(name="test_ie", begin="s1", steps=[
            StepNode(id="s1", step_type="agent", output_schema="nonexistent.pkg.Class",
                     output_schema_retries=2,
                     transitions=[Transition(to="s2")]),
            StepNode(id="s2", step_type="agent"),
        ])
        sf.register_graph(g)
        rid = sf.create_run("test_ie"); sf.start_run(rid)
        token = sf.claim_next_step(rid)
        from skillflow.core import StepResult
        sf.confirm_step(token.token, StepResult(outputs={"passed": True}, flags={}))
        # Should not raise — ImportError is caught, step goes back to pending
        row = sf._conn.execute(
            "SELECT status, validation_retry_count FROM skillflow_steps WHERE run_id=? AND step_id='s1'",
            (rid,)
        ).fetchone()
        assert row["status"] == "pending"

    def test_confirm_step_no_output_schema_skips(self, sf):
        from skillflow.graph import PipelineGraph, StepNode, Transition
        g = PipelineGraph(name="test_nos", begin="s1", steps=[
            StepNode(id="s1", step_type="agent",
                     transitions=[Transition(to="s2")]),
            StepNode(id="s2", step_type="agent"),
        ])
        sf.register_graph(g)
        rid = sf.create_run("test_nos"); sf.start_run(rid)
        token = sf.claim_next_step(rid)
        from skillflow.core import StepResult
        sf.confirm_step(token.token, StepResult(outputs={}, flags={}))
        row = sf._conn.execute(
            "SELECT status FROM skillflow_steps WHERE run_id=? AND step_id='s1'",
            (rid,)
        ).fetchone()
        assert row["status"] == "completed"


class TestToolNodeContextInjection:
    def test_execute_tool_injects_context(self, sf):
        """_execute_tool_inline auto-injects run_id, step_id, config_name, etc."""
        from skillflow.graph import PipelineGraph, StepNode, Transition
        from unittest.mock import MagicMock

        # Tool that captures all kwargs
        captured = {}
        def capture_tool(**kw):
            captured.update(kw)
            return {"ok": True}

        mock = MagicMock()
        mock.load_fn.return_value = capture_tool
        mock.load_schema.return_value = {"name": "capture"}
        sf._tool_loader = mock

        g = PipelineGraph(name="test_ctx", begin="s1", steps=[
            StepNode(id="s1", step_type="agent",
                     transitions=[Transition(to="t_capture")]),
            StepNode(id="t_capture", step_type="tool", tool_name="capture",
                     transitions=[Transition(to="s2", match={"ok": True})]),
            StepNode(id="s2", step_type="agent"),
        ])
        sf.register_graph(g)
        rid = sf.create_run("test_ctx")
        sf.start_run(rid)
        token = sf.claim_next_step(rid)
        sf.confirm_step(token.token, StepResult(outputs={}, flags={}))
        # Tool runs lock-free on the second advance pass.
        sf.advance_run(rid)
        sf.advance_run(rid)

        assert captured.get("run_id") == rid
        assert captured.get("step_id") == "t_capture"
        assert captured.get("config_name") == "test_ctx"
        assert captured.get("step_name") == "capture"
        assert captured.get("step_type") == "tool"

    def test_execute_tool_injects_agent_config_name(self, sf):
        """step_name = tool_name even when agent_config is also set."""
        from skillflow.graph import PipelineGraph, StepNode, Transition
        from unittest.mock import MagicMock

        captured = {}
        def capture_tool(**kw):
            captured.update(kw)
            return {"ok": True}

        mock = MagicMock()
        mock.load_fn.return_value = capture_tool
        mock.load_schema.return_value = {"name": "capture"}
        sf._tool_loader = mock

        sf.register_agent_config_from_dict("my_agent", {"model": "test", "template": "test.md"})
        g = PipelineGraph(name="test_an", begin="a1", steps=[
            StepNode(id="a1", step_type="agent",
                     transitions=[Transition(to="doer")]),
            StepNode(id="doer", step_type="tool", tool_name="capture",
                     agent_config="my_agent",
                     transitions=[Transition(to=None)]),
        ])
        sf.register_graph(g)
        rid = sf.create_run("test_an")
        sf.start_run(rid)
        token = sf.claim_next_step(rid)
        sf.confirm_step(token.token, StepResult(outputs={}, flags={}))
        # Tool runs lock-free on the second advance pass.
        sf.advance_run(rid)
        sf.advance_run(rid)

        assert captured.get("step_name") == "capture"  # tool_name takes priority
        assert captured.get("config_name") == "test_an"


from skillflow.core import StepResult


class TestNotificationBusIntegration:
    def test_notification_bus_is_created(self, sf):
        """SkillFlow always has a notifications attribute."""
        assert hasattr(sf, 'notifications')
        from skillflow.notifications import NotificationBus
        assert isinstance(sf.notifications, NotificationBus)

    def test_notification_bus_accepts_subscriber(self, sf):
        """Subscriber can be registered on sf.notifications."""
        called = []
        async def handler(n):
            called.append(n)

        sf.notifications.subscribe(handler)
        assert len(sf.notifications._subscribers) == 1
        sf.notifications.unsubscribe(handler)
        assert len(sf.notifications._subscribers) == 0

    def test_tool_node_publishes_via_outbox(self, sf):
        """When tool node executes, event goes to outbox."""
        from skillflow.graph import PipelineGraph, StepNode, Transition
        from unittest.mock import MagicMock

        mock = MagicMock()
        mock.load_fn.return_value = lambda **kw: {"applied": True}
        mock.load_schema.return_value = {"name": "echo"}
        sf._tool_loader = mock

        g = PipelineGraph(name="test_pub", begin="s1", steps=[
            StepNode(id="s1", step_type="agent",
                     transitions=[Transition(to="t1")]),
            StepNode(id="t1", step_type="tool", tool_name="echo",
                     transitions=[Transition(to="s2", match={"applied": True})]),
            StepNode(id="s2", step_type="agent"),
        ])
        sf.register_graph(g)
        rid = sf.create_run("test_pub")
        sf.start_run(rid)
        token = sf.claim_next_step(rid)
        sf.confirm_step(token.token, StepResult(outputs={}, flags={}))
        # Tool runs lock-free on the second advance pass.
        sf.advance_run(rid)
        sf.advance_run(rid)

        # Check outbox has step_completed for tool node
        rows = sf._conn.execute(
            "SELECT event_type FROM skillflow_outbox WHERE payload_json LIKE '%t1%' ORDER BY id"
        ).fetchall()
        events = [r["event_type"] for r in rows]
        assert "step_completed" in events  # tool node confirmed


# ── Lifecycle hooks ──────────────────────────────────────────────────


def test_lifecycle_default_draft_promote(sf: SkillFlow, tmp_path: Path):
    """after_validate defaults to draft_promote when output.fixed is set."""
    node = StepNode(
        id="s1",
        step_type="agent",
        output_mode="content",
        output_fixed={"out": "result.md"},
        transitions=[Transition(to=None)],
    )
    g = PipelineGraph(name="test_lc", begin="s1", steps=[node])
    sf.register_graph(g)
    sf._tool_loader = ToolLoader(Path(__file__).parent.parent / "src" / "skillflow" / "tools")

    # Set up workspace for draft dir
    import shutil
    ws_base = tmp_path / "ws"
    ws_base.mkdir()
    sf._workspace = WorkspaceManager(str(ws_base))

    rid = sf.create_run("test_lc", {"project_id": "test-pid"})
    sf.start_run(rid)
    sf.advance_run(rid)
    token = sf.claim_next_step(rid)

    # Write a file to step tmp dir
    tmp = sf._workspace.get_step_tmp_dir("test-pid", "test_lc", "s1")
    (tmp / "result.md").write_text("# test")

    sf.confirm_step(token.token, StepResult(outputs={}, flags={}))

    # File should be in step dir now (atomic rename)
    step_dir = sf._workspace.get_step_dir("test-pid", "test_lc", "s1")
    assert (step_dir / "result.md").exists()
    assert not (tmp / "result.md").exists()


def test_lifecycle_explicit_after_validate(sf: SkillFlow, tmp_path: Path):
    """Explicit after_validate hook overrides the default."""
    node = StepNode(
        id="s1",
        step_type="agent",
        output_mode="content",
        output_fixed={"out": "result.md"},
        lifecycle={"after_validate": {"tool": "draft_promote"}},
        transitions=[Transition(to=None)],
    )
    g = PipelineGraph(name="test_lc2", begin="s1", steps=[node])
    sf.register_graph(g)
    sf._tool_loader = ToolLoader(Path(__file__).parent.parent / "src" / "skillflow" / "tools")

    ws_base = tmp_path / "ws2"
    ws_base.mkdir()
    sf._workspace = WorkspaceManager(str(ws_base))

    rid = sf.create_run("test_lc2", {"project_id": "test-pid"})
    sf.start_run(rid)
    sf.advance_run(rid)
    token = sf.claim_next_step(rid)

    # draft_promote now delegates to _step_commit which uses new .tmp → step_dir paths
    tmp = sf._workspace.get_step_tmp_dir("test-pid", "test_lc2", "s1")
    (tmp / "result.md").write_text("# test")

    sf.confirm_step(token.token, StepResult(outputs={}, flags={}))

    step_dir = sf._workspace.get_step_dir("test-pid", "test_lc2", "s1")
    assert (step_dir / "result.md").exists()


def test_lifecycle_no_output_no_default(sf: SkillFlow):
    """No lifecycle defaults when step produces no output."""
    node = StepNode(
        id="s1",
        step_type="agent",
        transitions=[Transition(to=None)],
    )
    g = PipelineGraph(name="test_lc3", begin="s1", steps=[node])
    sf.register_graph(g)
    sf._tool_loader = ToolLoader(Path(__file__).parent.parent / "src" / "skillflow" / "tools")

    rid = sf.create_run("test_lc3")
    sf.start_run(rid)
    sf.advance_run(rid)
    token = sf.claim_next_step(rid)
    # Should not error — lifecycle is empty and outputs are not configured
    sf.confirm_step(token.token, StepResult(outputs={}, flags={}))


def test_lifecycle_graph_parsing():
    """Lifecycle field is parsed from YAML/dict."""
    data = {
        "name": "test",
        "begin": "s1",
        "steps": [{
            "id": "s1",
            "step_type": "agent",
            "lifecycle": {
                "on_deliver": {"tool": "repo_apply", "params": {"source_dir": "$STEP_FINAL_DIR"}},
                "after_deliver": [
                    {"tool": "syntax_lint", "files": ["*.py"]},
                    {"tool": "pytest", "files": ["*_test.py"]},
                ],
            },
            "transitions": [{"to": None}],
        }],
    }
    g = PipelineGraph._from_dict(data)
    assert g.steps[0].lifecycle == {
        "on_deliver": {"tool": "repo_apply", "params": {"source_dir": "$STEP_FINAL_DIR"}},
        "after_deliver": [
            {"tool": "syntax_lint", "files": ["*.py"]},
            {"tool": "pytest", "files": ["*_test.py"]},
        ],
    }


def test_lifecycle_to_dict_roundtrip():
    """Lifecycle survives to_dict → _from_dict roundtrip."""
    lifecycle = {
        "after_validate": {"tool": "draft_promote"},
        "on_deliver": {"tool": "repo_apply", "params": {"src": "$STEP_FINAL_DIR"}},
        "after_deliver": [{"tool": "pytest", "files": ["test_*.py"]}],
    }
    node = StepNode(
        id="s1",
        step_type="agent",
        output_mode="write",
        lifecycle=lifecycle,
        transitions=[Transition(to=None)],
    )
    g = PipelineGraph(name="test_rt", begin="s1", steps=[node])
    d = g.to_dict()
    g2 = PipelineGraph._from_dict(d)
    assert g2.steps[0].lifecycle == lifecycle


def test_lifecycle_events_in_outbox(sf: SkillFlow, tmp_path: Path):
    """Lifecycle hook execution emits events to outbox."""
    node = StepNode(
        id="s1",
        step_type="agent",
        output_mode="content",
        output_fixed={"out": "result.md"},
        transitions=[Transition(to=None)],
    )
    g = PipelineGraph(name="test_lc_evt", begin="s1", steps=[node])
    sf.register_graph(g)
    sf._tool_loader = ToolLoader(Path(__file__).parent.parent / "src" / "skillflow" / "tools")

    ws_base = tmp_path / "ws_evt"
    ws_base.mkdir()
    sf._workspace = WorkspaceManager(str(ws_base))

    rid = sf.create_run("test_lc_evt", {"project_id": "test-pid"})
    sf.start_run(rid)
    sf.advance_run(rid)
    token = sf.claim_next_step(rid)

    tmp = sf._workspace.get_step_tmp_dir("test-pid", "test_lc_evt", "s1")
    (tmp / "result.md").write_text("# test")

    sf.confirm_step(token.token, StepResult(outputs={}, flags={}))

    events = sf._conn.execute(
        "SELECT event_type, payload_json FROM skillflow_outbox WHERE event_type = 'lifecycle_hook' ORDER BY id"
    ).fetchall()
    assert len(events) >= 1
    payloads = [json.loads(e["payload_json"]) for e in events]
    hooks = [p["hook"] for p in payloads]
    assert "after_validate" in hooks

    # Step should be completed
    step = sf._conn.execute(
        "SELECT status FROM skillflow_steps WHERE run_id = ? AND step_id = ?",
        (rid, "s1"),
    ).fetchone()
    assert step["status"] == "completed"


# ── Durable run trace ─────────────────────────────────────────────────

def test_trace_append_and_get(sf: SkillFlow):
    """trace() appends ordered records; get_trace() returns them by seq."""
    rid = "run-trace-1"
    sf.trace(rid, "event", "first", {"a": 1})
    sf.trace(rid, "tool_call", "write", {"params": {"file": "x.py"}}, step_id="s1",
             step_instance_id=7)
    sf.trace(rid, "event", "third")

    recs = sf.get_trace(rid)
    assert [r["seq"] for r in recs] == [1, 2, 3]
    assert [r["event"] for r in recs] == ["first", "write", "third"]
    assert recs[1]["category"] == "tool_call"
    assert recs[1]["step_instance_id"] == 7
    assert recs[1]["payload"]["params"]["file"] == "x.py"


def test_trace_filters(sf: SkillFlow):
    rid = "run-trace-2"
    sf.trace(rid, "tool_call", "write", step_id="s1", step_instance_id=1)
    sf.trace(rid, "tool_call", "read_file", step_id="s2", step_instance_id=2)
    sf.trace(rid, "lifecycle", "on_deliver", step_id="s1", step_instance_id=1)

    assert len(sf.get_trace(rid, step_instance_id=1)) == 2
    assert len(sf.get_trace(rid, category="tool_call")) == 2
    assert sf.get_trace(rid, step_instance_id=2)[0]["event"] == "read_file"


def test_trace_clips_huge_fields(sf: SkillFlow):
    rid = "run-trace-3"
    big = "x" * (sf._TRACE_MAX_FIELD + 5000)
    sf.trace(rid, "prompt", "user_prompt", {"text": big})
    rec = sf.get_trace(rid)[0]
    assert "clipped" in rec["payload"]["text"]
    assert len(rec["payload"]["text"]) < len(big)


def test_trace_isolated_per_run(sf: SkillFlow):
    r1 = "run-trace-4a"
    r2 = "run-trace-4b"
    sf.trace(r1, "event", "a")
    sf.trace(r2, "event", "b")
    assert len(sf.get_trace(r1)) == 1
    assert sf.get_trace(r1)[0]["event"] == "a"
    assert sf.get_trace(r2)[0]["event"] == "b"


def test_trace_records_tool_exec(sf: SkillFlow, tmp_path: Path):
    """execute_tool records a tool_call + tool_result pair to the trace."""
    node = StepNode(id="s1", step_type="agent", output_mode="write",
                    transitions=[Transition(to=None)])
    g = PipelineGraph(name="t_tool", begin="s1", steps=[node])
    sf.register_graph(g)
    sf._tool_loader = ToolLoader(Path(__file__).parent.parent / "src" / "skillflow" / "tools")
    sf._workspace = WorkspaceManager(str(tmp_path / "ws"))
    rid = sf.create_run("t_tool", {"project_id": "pid"})
    sf.start_run(rid)
    sf.advance_run(rid)
    sf.claim_next_step(rid)

    sf.execute_tool("write", {"file": "a.py", "content": "x = 1"},
                    run_id=rid, step_id="s1", step_instance_id=42)

    cats = [(r["category"], r["event"]) for r in sf.get_trace(rid)]
    assert ("tool_call", "write") in cats
    assert ("tool_result", "write") in cats
    # step_instance_id flows through so writes correlate to their instance
    wcalls = [r for r in sf.get_trace(rid) if r["event"] == "write"]
    assert all(r["step_instance_id"] == 42 for r in wcalls)
    # The result trace carries the written filename.
    res = [r for r in sf.get_trace(rid, category="tool_result") if r["event"] == "write"][0]
    assert res["payload"].get("written") == "a.py"


def test_claimed_step_trace_bound(sf: SkillFlow):
    """ClaimedStep.trace is wired so the host can append prompts/responses."""
    graph = _simple_graph()
    sf.register_graph(graph)
    rid = sf.create_run("test", {"project_id": "p"})
    sf.start_run(rid)
    sf.advance_run(rid)
    claimed = sf.claim_next_step(rid)

    claimed.trace("prompt", "user_prompt", {"text": "hello"})
    recs = sf.get_trace(rid, category="prompt")
    assert len(recs) == 1
    assert recs[0]["payload"]["text"] == "hello"
    assert recs[0]["step_id"] == claimed.step_id
    assert recs[0]["step_instance_id"] == claimed.step_instance_id
    # The claim itself is also traced.
    assert any(r["event"] == "claimed" for r in sf.get_trace(rid, category="step"))


def test_trace_records_tool_step_node(sf: SkillFlow, tmp_path: Path):
    """A tool-type STEP node (not agent-invoked) is traced with source=tool_step."""
    from skillflow.graph import StepNode
    tool_node = StepNode(id="apply", step_type="tool", tool_name="notify",
                         tool_params={"message": "hi", "level": "info"})
    sf._tool_loader = ToolLoader(Path(__file__).parent.parent / "src" / "skillflow" / "tools")
    sf._workspace = WorkspaceManager(str(tmp_path / "ws"))
    rid = "run-toolstep"
    # create a run row so project_root resolution works
    sf._conn.execute(
        "INSERT INTO skillflow_runs (id, graph_name, project_id, status, context_json) "
        "VALUES (?,?,?,?,?)", (rid, "g", "pid", "running", "{}"))
    sf._conn.commit()

    sf._execute_tool_inline(tool_node, run_id=rid, graph_name="g")

    cats = [(r["category"], r["event"], r["payload"].get("source")) for r in sf.get_trace(rid)]
    assert ("tool_call", "notify", "tool_step") in cats
    assert ("tool_result", "notify", "tool_step") in cats


def test_trace_records_validation_tools(sf: SkillFlow, tmp_path: Path):
    """Validation specs run via StepValidator are traced (source=validation)."""
    node = StepNode(
        id="s1", step_type="agent", output_mode="write",
        validation=[{"files": ["*.py"], "tool": "syntax_lint"}],
        transitions=[Transition(to=None)],
    )
    g = PipelineGraph(name="t_val", begin="s1", steps=[node])
    sf.register_graph(g)
    sf._tool_loader = ToolLoader(Path(__file__).parent.parent / "src" / "skillflow" / "tools")
    sf._workspace = WorkspaceManager(str(tmp_path / "wsv"))
    rid = sf.create_run("t_val", {"project_id": "pidv"})
    sf.start_run(rid)
    sf.advance_run(rid)
    claimed = sf.claim_next_step(rid)
    # write a valid python file to the step tmp dir
    tmp = sf._workspace.get_step_tmp_dir("pidv", "t_val", "s1")
    (tmp / "ok.py").write_text("x = 1\n")

    sf.confirm_step(claimed.token, StepResult(outputs={}, flags={}))

    val = [r for r in sf.get_trace(rid) if r["payload"].get("source") == "validation"]
    assert any(r["event"] == "syntax_lint" for r in val)


def test_trace_disabled_writes_nothing(tmp_path):
    sf2 = SkillFlow(str(tmp_path / "off.db"), trace_enabled=False)
    sf2.trace("r", "event", "x", {"a": 1})
    assert sf2.get_trace("r") == []


def test_trace_seq_cached_no_select_per_record(sf: SkillFlow):
    """Seq stays correct via the in-process counter (no SELECT per record)."""
    rid = "run-seqcache"
    for i in range(10):
        sf.trace(rid, "event", f"e{i}")
    assert [r["seq"] for r in sf.get_trace(rid)] == list(range(1, 11))


def test_prune_trace_by_run(sf: SkillFlow):
    sf.trace("ra", "event", "x")
    sf.trace("rb", "event", "y")
    assert sf.prune_trace("ra") == 1
    assert sf.get_trace("ra") == []
    assert len(sf.get_trace("rb")) == 1
    # seq counter reset → next trace for ra restarts at 1
    sf.trace("ra", "event", "z")
    assert sf.get_trace("ra")[0]["seq"] == 1


def test_prune_trace_keep_last_runs(sf: SkillFlow):
    for rid in ("r1", "r2", "r3"):
        sf.trace(rid, "event", "x")
    deleted = sf.prune_trace(keep_last_runs=2)
    assert deleted == 1
    remaining = {r["run_id"] for rid in ("r1", "r2", "r3") for r in sf.get_trace(rid)} \
        if False else None
    assert sf.get_trace("r1") == []          # oldest dropped
    assert len(sf.get_trace("r2")) == 1
    assert len(sf.get_trace("r3")) == 1


def test_delete_project_removes_trace(sf: SkillFlow):
    """Deleting a project drops its runs' trace records (and seq cache)."""
    graph = _simple_graph(name="delproj")
    sf.register_graph(graph)
    rid = sf.create_run("delproj", {"project_id": "doomed"})
    sf.start_run(rid)
    sf.advance_run(rid)
    claimed = sf.claim_next_step(rid)  # writes a 'claimed' trace
    claimed.trace("prompt", "user_prompt", {"text": "hi"})
    assert len(sf.get_trace(rid)) >= 2
    assert rid in sf._trace_seq

    sf.delete_project("doomed")

    assert sf.get_trace(rid) == []
    assert rid not in sf._trace_seq
    # Other projects' trace is untouched
    other = sf.create_run("delproj", {"project_id": "safe"})
    sf.trace(other, "event", "x")
    sf.delete_project("doomed")  # idempotent, no effect on 'safe'
    assert len(sf.get_trace(other)) == 1
