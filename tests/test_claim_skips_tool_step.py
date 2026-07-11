"""Regression: claim_next_step must NOT hand an inline tool step to the host.

A tool step has no agent_config; a host that claims it and routes it to its
agent runner raises "Agent config '' not found". Inline (non-delegated) tools
are executed by advance_run's fast-path instead. claim_next_step already skips
gates the same way; this guards the tool case.

Surfaced by an AItelier dpe_game run: an addon spliced a `scaffold` tool step
right after `git_sync_pre`; on the first tick a claim reached it before the
scheduler drained it, failing the step until it self-healed on a later tick.
"""

from skillflow.core import SkillFlow, StepResult
from skillflow.graph import (
    PipelineGraph, StepNode, Transition, EndConditions, EndCondition,
)
from tests.mocks import MockToolLoader


def _graph_with_adjacent_tools():
    return PipelineGraph(
        name="tooltest",
        begin="gen",
        steps=[
            StepNode(id="gen", step_type="agent", agent_config="noop_agent",
                     transitions=[Transition(to="t1")]),
            # two adjacent tool steps (the spliced-addon shape)
            StepNode(id="t1", step_type="tool", tool_name="noop_tool",
                     transitions=[Transition(to="t2")]),
            StepNode(id="t2", step_type="tool", tool_name="noop_tool",
                     transitions=[Transition(to="done")]),
            StepNode(id="done", step_type="agent", agent_config="noop_agent",
                     transitions=[]),
        ],
        end_conditions=EndConditions(
            combinator="or",
            conditions=[EndCondition(type="node_reached", node="done",
                                     result="completed")],
        ),
    )


def test_claim_never_returns_an_inline_tool_step(tmp_path):
    tools = MockToolLoader()
    tools.register("noop_tool", lambda **k: {"passed": True})
    sf = SkillFlow(str(tmp_path / "t.db"), tool_loader=tools)
    sf.register_agent_config("noop_agent")
    sf.register_graph(_graph_with_adjacent_tools())
    run_id = sf.create_run("tooltest")
    sf.start_run(run_id)

    # Run the first agent step.
    sf.advance_run(run_id)
    claimed = sf.claim_next_step(run_id)
    assert claimed.step_id == "gen"
    sf.confirm_step(claimed.token, StepResult(outputs={}, flags={}))

    # Now current_node walks onto the tool steps t1/t2. Even if a driver calls
    # claim_next_step while current_node points at a tool, it must refuse to
    # claim it (returns None) — never hand a tool step to the agent runner.
    for _ in range(10):
        run = sf.get_run(run_id)
        cur = run["current_node"]
        if cur in ("t1", "t2"):
            assert sf.claim_next_step(run_id) is None, (
                f"claim_next_step wrongly claimed tool step {cur!r}")
        c = sf.claim_next_step(run_id)
        if c is not None:
            # the only claimable steps are agent steps
            assert c.step_id in ("gen", "done")
            sf.confirm_step(c.token, StepResult(outputs={}, flags={}))
        sf.advance_run(run_id)
        if sf.get_run(run_id)["status"] == "completed":
            break

    assert sf.get_run(run_id)["status"] == "completed"
