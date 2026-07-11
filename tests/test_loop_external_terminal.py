"""Behavioral proof of the loop-external terminal-gate pattern.

A verifier step lives INSIDE a goal loop: passed:false loops back to fix,
passed:true ends the run. If the end condition fires on the verifier node
itself (`node_reached review`), the verifier's `completed` row latches after
the FIRST (failed) pass, so the run terminates on the next loop iteration
before re-verifying the fixed work — shipping a still-failing result.

The fix: route passed:true to a loop-EXTERNAL `done` gate and fire the end
condition on `done`. A gate has no completed step row, so it's reached at most
once, only on a real pass.

This mirrors the AItelier dpe_default_v2 bug where a game with playtest
passed:false shipped as `completed`.
"""

from skillflow.core import SkillFlow, StepResult
from skillflow.graph import (
    PipelineGraph, StepNode, Transition, EndConditions, EndCondition,
)


def _agent(id, transitions):
    return StepNode(id=id, step_type="agent", transitions=transitions)


def _gate(id, transitions):
    return StepNode(id=id, step_type="gate", transitions=transitions)


def _graph(*, terminal_node):
    """work → review; review passed→done, failed→work(loop). End at terminal_node."""
    return PipelineGraph(
        name=f"loop_{terminal_node}", begin="work",
        steps=[
            _agent("work", [Transition(to="review")]),
            _agent("review", [
                Transition(to="done", match={"passed": True}),
                Transition(to="work", match={"passed": False}, max_loop=5),
            ]),
            _gate("done", [Transition(to=None)]),
        ],
        end_conditions=EndConditions(
            combinator="or",
            conditions=[
                EndCondition(type="node_reached", node=terminal_node, result="completed"),
                EndCondition(type="max_total_steps", limit=50),
            ],
        ),
    )


def _step(sf, run_id, expect, flags):
    c = sf.claim_next_step(run_id)
    assert c is not None and c.step_id == expect, f"expected {expect}, got {c and c.step_id}"
    sf.confirm_step(c.token, StepResult(outputs={}, flags=flags))


def _drain_gates(sf, run_id):
    for _ in range(10):
        if sf.advance_run(run_id) is None:
            break


def test_reject_then_pass_reaches_done_not_early_terminate():
    """Terminal on `done`: a failed verify loops back; the run completes only
    after the SECOND verify passes — the fixed work is really re-verified."""
    sf = SkillFlow(":memory:")
    sf.register_agent_config("noop")
    g = _graph(terminal_node="done")
    # agent steps need an agent_config for the host, but this engine test drives
    # them directly; set one so validation passes.
    for n in g.steps:
        if n.step_type == "agent":
            n.agent_config = "noop"
    sf.register_graph(g)
    rid = sf.create_run(g.name)
    sf.start_run(rid)

    _drain_gates(sf, rid)
    _step(sf, rid, "work", {})           # first attempt
    _drain_gates(sf, rid)
    _step(sf, rid, "review", {"passed": False})   # reject → loop back
    assert sf.get_run(rid)["status"] == "running", "must NOT terminate on a failed review"
    _drain_gates(sf, rid)
    _step(sf, rid, "work", {})           # fix
    _drain_gates(sf, rid)
    _step(sf, rid, "review", {"passed": True})    # pass → done
    _drain_gates(sf, rid)

    assert sf.get_run(rid)["status"] == "completed", "should complete after passing verify"
    # The verify ran TWICE — the fixed work was really re-verified, not shipped
    # after the first failed pass.
    reviews = [s for s in sf.get_steps(rid)
               if s["step_id"] == "review" and s["status"] == "completed"]
    assert len(reviews) == 2, f"verify should run twice (reject then pass), ran {len(reviews)}"
