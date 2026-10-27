"""Tool checkpoints pause on matching edges, using actual loader callbacks."""

import pytest

from skillflow.core import SkillFlow, SkillFlowError, StepResult
from skillflow.graph import EndCondition, EndConditions, PipelineGraph, StepNode, Transition
from skillflow.tool_loader import ToolLoader


ROUTES = [None, {"ready": True}, {"from": "checkpoint", "value": "approved"}]


def _start(tmp_path, *, match=None, checkpoint=True, terminal_target=False,
           end_at_current=False, after_checkpoint=False, reply=None, tool_error="fail"):
    calls = []
    loader = ToolLoader()
    sf = SkillFlow(str(tmp_path / "checkpoint.db"), tool_loader=loader)

    def before(**_kwargs):
        calls.append("before")
        return reply if reply is not None else {"ready": True, "sequence": len(calls)}

    def after(**_kwargs):
        calls.append("after")
        return {"ready": True, "sequence": len(calls)}

    for name, function in [("before_tool", before), ("after_tool", after)]:
        loader.register_dynamic_tool(name, {"name": name, "description": "Owned test callback",
            "parameters": {"type": "object", "properties": {}}}, function)
    target = "done" if terminal_target else "after"
    steps = [StepNode(id="before", step_type="tool", tool_name="before_tool",
        checkpoint=checkpoint, tool_error=tool_error,
        transitions=[Transition(to=target, match=match)])]
    if not terminal_target:
        steps.append(StepNode(id="after", step_type="tool", tool_name="after_tool",
            checkpoint=after_checkpoint, transitions=[Transition(to="done")]))
    steps.append(StepNode(id="done", step_type="gate", transitions=[Transition(to=None)]))
    end = EndCondition(type="node_reached", node="before" if end_at_current else "done",
                       require_completed=end_at_current)
    graph = PipelineGraph(name="tool_checkpoint", begin="before", steps=steps,
                          end_conditions=EndConditions(conditions=[end]))
    assert graph.validate() == []
    sf.register_graph(graph)
    run = sf.create_run(graph.name)
    sf.start_run(run)
    sf.advance_run(run)
    assert calls == ["before"]
    return sf, run, calls


@pytest.mark.parametrize("match", ROUTES, ids=["unconditional", "matched", "guarded"])
@pytest.mark.parametrize("terminal_target", [False, True], ids=["successor_tool", "end_gate"])
def test_tool_checkpoint_flag_pauses_before_any_successor(tmp_path, match, terminal_target):
    sf, run, calls = _start(tmp_path, match=match, terminal_target=terminal_target)
    target = "done" if terminal_target else "after"
    assert sf.get_run(run)["status"] == "paused"
    assert sf.get_run(run)["current_node"] == target
    for _ in range(2):
        sf.advance_run(run)
    assert calls == ["before"]
    retained = sf.get_checkpoint(run)
    assert retained["step_id"] == "before" and retained["next_node"] == target
    assert retained["expected_checkpoint"]["step_instance_id"] > 0
    assert sf.approve_checkpoint(run, expected_checkpoint=retained["expected_checkpoint"],
                                 decision_id="approve-before") == target
    for _ in range(3):
        sf.advance_run(run)
    assert sf.get_run(run)["status"] == "completed"
    assert calls == (["before"] if terminal_target else ["before", "after"])
    assert sf.approve_checkpoint(run, expected_checkpoint=retained["expected_checkpoint"],
                                 decision_id="approve-before") == target
    assert sf.get_run(run)["status"] == "completed"
    assert calls == (["before"] if terminal_target else ["before", "after"])


@pytest.mark.parametrize("match", ROUTES, ids=["unconditional", "matched", "guarded"])
def test_replayed_approval_cannot_approve_the_next_tool_checkpoint(tmp_path, match):
    sf, run, calls = _start(tmp_path, match=match, after_checkpoint=True)
    first = sf.get_checkpoint(run)["expected_checkpoint"]
    sf.approve_checkpoint(run, expected_checkpoint=first, decision_id="first")
    sf.advance_run(run)
    second = sf.get_checkpoint(run)["expected_checkpoint"]
    assert sf.get_run(run)["status"] == "paused" and calls == ["before", "after"]
    assert second != first
    assert sf.approve_checkpoint(run, expected_checkpoint=first, decision_id="first") == "after"
    assert sf.get_run(run)["status"] == "paused"
    assert sf.get_checkpoint(run)["expected_checkpoint"] == second
    with pytest.raises(SkillFlowError, match="Stale checkpoint"):
        sf.approve_checkpoint(run, expected_checkpoint=first, decision_id="different")
    assert sf.get_checkpoint(run)["expected_checkpoint"] == second
    sf.approve_checkpoint(run, expected_checkpoint=second, decision_id="second")
    sf.advance_run(run)
    assert sf.get_run(run)["status"] == "completed" and calls == ["before", "after"]


def test_rejection_reexecutes_only_the_tool_before_a_fresh_approval(tmp_path):
    sf, run, calls = _start(tmp_path)
    first = sf.get_checkpoint(run)["expected_checkpoint"]
    sf.reject_checkpoint(run, "before", "Revise owned fixture output")
    assert sf.get_run(run)["status"] == "running"
    sf.advance_run(run)
    revised = sf.get_checkpoint(run)["expected_checkpoint"]
    assert sf.get_run(run)["status"] == "paused" and calls == ["before", "before"]
    assert revised != first
    with pytest.raises(SkillFlowError, match="Stale checkpoint"):
        sf.approve_checkpoint(run, expected_checkpoint=first, decision_id="old")
    sf.approve_checkpoint(run, expected_checkpoint=revised, decision_id="revised")
    sf.advance_run(run)
    assert calls == ["before", "before", "after"]


def test_noncheckpoint_still_continues_one_tool_per_advance(tmp_path):
    sf, run, calls = _start(tmp_path, checkpoint=False)
    assert sf.get_run(run)["status"] == "running"
    sf.advance_run(run)
    assert calls == ["before", "after"] and sf.get_run(run)["status"] == "completed"


def test_refusing_tool_fails_without_publishing_a_checkpoint(tmp_path):
    sf, run, calls = _start(tmp_path, reply={"error": "owned fixture refusal"})
    assert sf.get_run(run)["status"] == "failed" and calls == ["before"]
    with pytest.raises(SkillFlowError):
        sf.get_checkpoint(run)
    sf.advance_run(run)
    assert calls == ["before"]


def test_routable_error_still_pauses_when_its_step_completed(tmp_path):
    sf, run, calls = _start(tmp_path, reply={"error": "owned routed result"}, tool_error="route")
    assert sf.get_run(run)["status"] == "paused" and calls == ["before"]
    retained = sf.get_checkpoint(run)["expected_checkpoint"]
    sf.approve_checkpoint(run, expected_checkpoint=retained, decision_id="route")
    sf.advance_run(run)
    assert calls == ["before", "after"] and sf.get_run(run)["status"] == "completed"


def test_nonterminal_tool_checkpoint_matches_agent_end_condition_order(tmp_path):
    reference = SkillFlow(str(tmp_path / "agent.db"))
    reference.register_graph(PipelineGraph(name="agent_reference", begin="before", steps=[
        StepNode(id="before", step_type="agent", checkpoint=True,
                 transitions=[Transition(to="after")]),
        StepNode(id="after", step_type="agent", transitions=[])],
        end_conditions=EndConditions(conditions=[EndCondition(
            type="node_reached", node="before", require_completed=True)])))
    agent_run = reference.create_run("agent_reference")
    reference.start_run(agent_run)
    reference.advance_run(agent_run)
    claim = reference.claim_next_step(agent_run)
    reference.confirm_step(claim.token, StepResult(outputs={"fixture": "complete"}))
    reference.advance_run(agent_run)
    assert reference.get_run(agent_run)["status"] == "paused"
    assert reference.get_run(agent_run)["current_node"] == "after"

    sf, run, calls = _start(tmp_path, end_at_current=True)
    assert sf.get_run(run)["status"] == "paused" and calls == ["before"]
    assert sf.get_checkpoint(run)["next_node"] == "after"
