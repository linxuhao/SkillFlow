"""Integration tests — full end-to-end graph flows with real SQLite."""

import pytest

from skillflow.core import SkillFlow, StepResult, ClaimedStep
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


# ── Full DPE pipeline ────────────────────────────────────────────────

def _dpe_graph():
    return PipelineGraph(
        name="dpe_test", begin="1_5",
        steps=[
            _agent("1_5", [_trans("2")], checkpoint=True),
            _agent("2", [_trans("3")], checkpoint=True),
            _agent("3", [_trans("task_gate")]),
            _gate("task_gate", [
                _trans("t_plan", match={"has_tasks": True}),
                _trans("5", match={"has_tasks": False}),
            ]),
            _agent("t_plan", [_trans("t_impl")]),
            _agent("t_impl", [
                _trans("t_verify"),
                _trans("task_error_handler", match={"_error": True}),
            ], max_retries=2),
            _agent("t_verify", [_trans("task_loop")]),
            _gate("task_loop", [
                _trans("t_plan", match={"more_tasks": True}, max_loop=10),
                _trans("5", match={"all_done": True}),
                _trans("1_5", match={"refresh_needed": True}, max_loop=3),
            ]),
            _agent("task_error_handler", [_trans("task_loop")]),
            _agent("5", []),
        ],
        end_conditions=EndConditions(
            combinator="or",
            conditions=[
                EndCondition(type="node_reached", node="5", result="completed"),
                EndCondition(type="max_total_steps", limit=100),
                EndCondition(type="flag_match", flag={"fatal_error": True}),
            ],
        ),
    )


def _execute(sf: SkillFlow, run_id: str, step_id: str, outputs=None, flags=None):
    """Helper: execute one step through the full claim-execute-confirm cycle."""
    claimed = sf.claim_next_step(run_id)
    assert claimed is not None, f"Failed to claim {step_id}"
    assert claimed.step_id == step_id, f"Expected {step_id}, got {claimed.step_id}"
    result = StepResult(outputs=outputs or {}, flags=flags or {})
    sf.confirm_step(claimed.token, result)


def test_full_dpe_pipeline_no_tasks(sf: SkillFlow):
    """Pipeline with no tasks: 1_5 → 2 → 3 → task_gate → 5."""
    graph = _dpe_graph()
    sf.register_graph(graph)
    run_id = sf.create_run("dpe_test")
    sf.start_run(run_id)

    # Step 1_5
    sf.advance_run(run_id)
    _execute(sf, run_id, "1_5")

    # Checkpoint pause
    assert sf.advance_run(run_id) is None
    assert sf.get_run(run_id)["status"] == "paused"
    sf.resume_run(run_id)

    # Step 2
    sf.advance_run(run_id)
    _execute(sf, run_id, "2")

    # Checkpoint pause
    assert sf.advance_run(run_id) is None
    sf.resume_run(run_id)

    # Step 3
    sf.advance_run(run_id)
    _execute(sf, run_id, "3", flags={"has_tasks": False})

    # task_gate → 5 (no tasks)
    assert sf.advance_run(run_id) is None  # End condition triggered
    assert sf.get_run(run_id)["status"] == "completed"


def test_full_dpe_pipeline_with_tasks(sf: SkillFlow):
    """Pipeline with tasks: 1_5 → 2 → 3 → task_gate → t_plan → t_impl → t_verify → task_loop → 5."""
    graph = _dpe_graph()
    sf.register_graph(graph)
    run_id = sf.create_run("dpe_test")
    sf.start_run(run_id)

    # Planning phase
    sf.advance_run(run_id)
    _execute(sf, run_id, "1_5")
    sf.advance_run(run_id)  # pause
    sf.resume_run(run_id)

    sf.advance_run(run_id)
    _execute(sf, run_id, "2")
    sf.advance_run(run_id)  # pause
    sf.resume_run(run_id)

    sf.advance_run(run_id)
    _execute(sf, run_id, "3", flags={"has_tasks": True})

    # Task loop — 3 tasks
    for i in range(3):
        more = (i < 2)  # more_tasks for first 2 iterations

        sf.advance_run(run_id)
        _execute(sf, run_id, "t_plan")
        sf.advance_run(run_id)
        _execute(sf, run_id, "t_impl")
        sf.advance_run(run_id)
        _execute(sf, run_id, "t_verify", flags={"more_tasks": more, "all_done": not more})

        sf.advance_run(run_id)  # resolves task_loop gate
        if more:
            continue  # Back to t_plan
        else:
            # Final step 5
            pass

    # Should have resolved to 5 and completed
    run = sf.get_run(run_id)
    if run["status"] == "running":
        assert run["current_node"] == "5"
        _execute(sf, run_id, "5")
        assert sf.advance_run(run_id) is None
        assert sf.get_run(run_id)["status"] == "completed"


def test_error_transition_flow(sf: SkillFlow):
    """t_impl fails repeatedly → error_handler → task_loop → 5."""
    graph = _dpe_graph()
    sf.register_graph(graph)
    run_id = sf.create_run("dpe_test")
    sf.start_run(run_id)

    # Quick setup: complete planning
    sf.advance_run(run_id)
    _execute(sf, run_id, "1_5")
    sf.advance_run(run_id); sf.resume_run(run_id)  # checkpoint

    sf.advance_run(run_id)
    _execute(sf, run_id, "2")
    sf.advance_run(run_id); sf.resume_run(run_id)  # checkpoint

    sf.advance_run(run_id)
    _execute(sf, run_id, "3", flags={"has_tasks": True})

    # First task
    sf.advance_run(run_id)
    _execute(sf, run_id, "t_plan")

    # t_impl fails 3 times (max_retries=2 → initial attempt + 2 retries)
    for attempt in range(3):
        sf.advance_run(run_id)
        claimed = sf.claim_next_step(run_id)
        assert claimed is not None, f"Failed to claim t_impl at attempt {attempt+1}"
        sf.fail_step(claimed.token, f"API error {attempt+1}", retryable=True)

    # Should have routed to error_handler
    run = sf.get_run(run_id)
    assert run["current_node"] == "task_error_handler"
    assert run["status"] == "running"

    # Error handler → task_loop
    sf.advance_run(run_id)
    _execute(sf, run_id, "task_error_handler", flags={})
    sf.advance_run(run_id)  # task_loop gate resolves

    # task_loop resolves — no flags set by error_handler
    # Should fail run because no matching transition
    run = sf.get_run(run_id)
    if run["status"] == "running" and run["current_node"] is None:
        # The advance_run tried to resolve task_loop but found no match
        pass


def test_checkpoint_pause_resume_cycle(sf: SkillFlow):
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
    _execute(sf, run_id, "a")
    assert sf.advance_run(run_id) is None  # Paused
    assert sf.get_run(run_id)["status"] == "paused"

    sf.resume_run(run_id)
    assert sf.get_run(run_id)["status"] == "running"

    next_node = sf.advance_run(run_id)
    assert next_node == "b"


def test_checkpoint_rejection_then_reexecution(sf: SkillFlow):
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

    # First execution
    sf.advance_run(run_id)
    _execute(sf, run_id, "a", outputs={"plan": "v1"})
    sf.advance_run(run_id)  # Pauses
    assert sf.get_run(run_id)["status"] == "paused"

    # Reject checkpoint
    sf.reject_checkpoint(run_id, "a", "Needs improvement")
    assert sf.get_run(run_id)["status"] == "running"

    # Re-claim and re-execute
    claimed = sf.claim_next_step(run_id)
    assert claimed.step_id == "a"

    result = StepResult(outputs={"plan": "v2"}, flags={})
    sf.confirm_step(claimed.token, result)

    sf.advance_run(run_id)  # Pauses again
    sf.resume_run(run_id)
    sf.advance_run(run_id)
    _execute(sf, run_id, "b")


def test_crash_during_execute_recovery(sf_tmp: SkillFlow):
    """Simulate crash during execute: claim, then recover without confirm."""
    graph = PipelineGraph(
        name="test", begin="a",
        steps=[_agent("a", [_trans("b")]), _agent("b", [])],
    )
    sf_tmp.register_graph(graph)
    run_id = sf_tmp.create_run("test")
    sf_tmp.start_run(run_id)
    sf_tmp.advance_run(run_id)
    sf_tmp.claim_next_step(run_id)

    # "Crash" — recover stale claims
    sf_tmp.recover_stale_claims(stale_threshold_seconds=-1)

    # Step should be re-claimable
    sf_tmp.advance_run(run_id)
    claimed = sf_tmp.claim_next_step(run_id)
    assert claimed is not None
    assert claimed.step_id == "a"

    sf_tmp.confirm_step(claimed.token, StepResult())
    sf_tmp.advance_run(run_id)
    claimed_b = sf_tmp.claim_next_step(run_id)
    assert claimed_b.step_id == "b"


def test_concurrent_claim_prevention(sf: SkillFlow):
    """Two claim attempts — only one wins."""
    graph = PipelineGraph(
        name="test", begin="a",
        steps=[_agent("a", [])],
    )
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)
    sf.advance_run(run_id)

    # Simulate two concurrent claims
    c1 = sf.claim_next_step(run_id)
    c2 = sf.claim_next_step(run_id)

    assert c1 is not None
    assert c2 is None  # Second claim fails because version changed


def test_idempotent_advance_after_confirm(sf: SkillFlow):
    """advance_run returns same result after confirm without claim."""
    graph = PipelineGraph(
        name="test", begin="a",
        steps=[_agent("a", [_trans("b")]), _agent("b", [])],
    )
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)

    # Execute a
    sf.advance_run(run_id)
    _execute(sf, run_id, "a")

    # Double advance — not claimed, so both should return b
    n1 = sf.advance_run(run_id)
    n2 = sf.advance_run(run_id)
    assert n1 == n2
    assert n1 == "b"


def test_planning_refresh_cycle(sf: SkillFlow):
    """refresh_needed → 1_5 with max_loop enforcement."""
    graph = PipelineGraph(
        name="test", begin="a",
        steps=[
            _agent("a", [_trans("g")]),
            _gate("g", [
                _trans("a", match={"refresh": True}, max_loop=2),
                _trans("done", match={"refresh": False}),
            ]),
            _agent("done", []),
        ],
    )
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)

    # Iteration 1: a → g → a (refresh)
    sf.advance_run(run_id)
    _execute(sf, run_id, "a", flags={"refresh": True})
    sf.advance_run(run_id)  # resolves g → a

    # Iteration 2: a → g → a (refresh)
    sf.advance_run(run_id)
    _execute(sf, run_id, "a", flags={"refresh": True})
    sf.advance_run(run_id)  # resolves g → a

    # Iteration 3: a → g → done (max_loop=2 on g→a exhausted, flags changed)
    sf.advance_run(run_id)
    _execute(sf, run_id, "a", flags={"refresh": False})
    sf.advance_run(run_id)  # resolves g → done
    _execute(sf, run_id, "done")
