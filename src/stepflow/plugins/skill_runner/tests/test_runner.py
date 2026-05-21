"""Tests for SkillTool — simulating an agent calling the tool facade."""

import pytest

from stepflow.core import StepFlow
from stepflow.graph import PipelineGraph
from stepflow.plugins.skill_runner import SkillTool, SkillResponse
from tests.mocks import create_standard_mock_tools


# ── Helper: build simple graphs ─────────────────────────────────────

def _simple_skill_graph():
    """analyze → gate → branch_a or branch_b → done (end_cond on done)."""
    import yaml
    yaml_text = """
name: simple_skill
begin: analyze
end_conditions:
  combinator: or
  conditions:
    - type: node_reached
      node: done
      result: "completed"
steps:
  - id: analyze
    step_type: agent
    agent_config: analyst
    transitions: [{to: router}]
  - id: router
    step_type: gate
    transitions:
      - {to: branch_a, match: {route: a}}
      - {to: branch_b, match: {route: b}}
  - id: branch_a
    step_type: agent
    agent_config: analyst
    transitions: [{to: done}]
  - id: branch_b
    step_type: agent
    agent_config: analyst
    transitions: [{to: done}]
  - id: done
    step_type: agent
    agent_config: analyst
"""
    return PipelineGraph._from_dict(yaml.safe_load(yaml_text))


def _checkpoint_skill_graph():
    """draft (checkpoint) → publish → done (end_cond on done)."""
    import yaml
    yaml_text = """
name: checkpoint_skill
begin: draft
end_conditions:
  combinator: or
  conditions:
    - type: node_reached
      node: done
      result: "completed"
steps:
  - id: draft
    step_type: agent
    agent_config: writer
    checkpoint: true
    checkpoint_label: "Review Draft"
    transitions:
      - to: publish
        match: {from: checkpoint, value: approved}
  - id: publish
    step_type: agent
    agent_config: writer
    transitions: [{to: done}]
  - id: done
    step_type: agent
    agent_config: writer
"""
    return PipelineGraph._from_dict(yaml.safe_load(yaml_text))


# ── Fixtures ────────────────────────────────────────────────────────

@pytest.fixture
def sf():
    tools = create_standard_mock_tools()
    sf = StepFlow(":memory:", tool_loader=tools)
    sf.register_agent_config("analyst", model="mock")
    sf.register_agent_config("writer", model="mock")
    return sf


# ── Basic flow ──────────────────────────────────────────────────────

def test_next_starts_run_and_returns_instruction(sf):
    sf.register_graph(_simple_skill_graph())
    tool = SkillTool(sf, "simple_skill")

    resp = tool(action="next")
    assert resp.status == "in_progress"
    assert resp.step == "analyze"
    assert "analyze" in resp.instruction.lower()


def test_submit_advances_through_gate(sf):
    sf.register_graph(_simple_skill_graph())
    tool = SkillTool(sf, "simple_skill")

    tool(action="next")
    resp = tool(action="submit", result={"route": "a"})
    assert resp.status == "in_progress"
    assert resp.step == "branch_a"


def test_full_run_to_completion(sf):
    sf.register_graph(_simple_skill_graph())
    tool = SkillTool(sf, "simple_skill")

    resp = tool(action="next")
    assert resp.status == "in_progress"
    assert resp.step == "analyze"

    # Submit with route=a → gate resolves to branch_a
    resp = tool(action="submit", result={"route": "a"})
    assert resp.status == "in_progress"
    assert resp.step == "branch_a"

    # Submit branch_a → done → end_condition triggers completion
    resp = tool(action="submit", result={})
    assert resp.status == "completed"


def test_double_next_is_idempotent(sf):
    sf.register_graph(_simple_skill_graph())
    tool = SkillTool(sf, "simple_skill")

    r1 = tool(action="next")
    r2 = tool(action="next")
    assert r1.step == r2.step
    assert r1.status == r2.status


# ── Checkpoint flow ─────────────────────────────────────────────────

def test_checkpoint_pause_and_approve(sf):
    sf.register_graph(_checkpoint_skill_graph())
    tool = SkillTool(sf, "checkpoint_skill")

    # Execute draft → checkpoint pauses
    tool(action="next")
    resp = tool(action="submit", result={"content": "draft v1"})
    assert resp.status == "paused"
    assert resp.checkpoint_label == "Review Draft"

    # Approve → publish → done → complete (end_condition on done)
    resp = tool(action="approve")
    assert resp.status == "in_progress"
    assert resp.step == "publish"


def test_checkpoint_reject_and_redo(sf):
    sf.register_graph(_checkpoint_skill_graph())
    tool = SkillTool(sf, "checkpoint_skill")

    # Execute draft
    tool(action="next")
    resp = tool(action="submit", result={"content": "draft v1"})
    assert resp.status == "paused"

    # Reject with feedback
    resp = tool(action="reject", feedback="Needs more detail")
    assert resp.status == "in_progress"
    assert resp.step == "draft"  # re-offered

    # Redo draft → paused again
    resp = tool(action="submit", result={"content": "draft v2"})
    assert resp.status == "paused"

    # Approve → publish → done → complete
    tool(action="approve")
    resp = tool(action="submit", result={})  # publish → done → complete
    assert resp.status == "completed"


# ── Prompt assembler ────────────────────────────────────────────────

def test_prompt_includes_context_and_feedback():
    from stepflow.plugins.skill_runner.runner import PromptAssembler
    from stepflow.core import ClaimedStep, ClaimToken

    assembler = PromptAssembler()
    token = ClaimToken(step_id="test", run_id="r1",
                        step_instance_id=1, version=1, claimed_at=0)
    step = ClaimedStep(
        token=token, step_id="test",
        step_config={}, run_context={},
        inputs={
            "_resolved_context": {"Source": "some context"},
            "_feedback": "Previous attempt was incomplete",
        },
    )
    instruction = assembler.assemble(step)
    assert "some context" in instruction
    assert "Previous attempt was incomplete" in instruction


# ── Tool response is JSON-serializable ──────────────────────────────

def test_response_is_serializable(sf):
    import json
    sf.register_graph(_simple_skill_graph())
    tool = SkillTool(sf, "simple_skill")
    resp = tool(action="next")

    d = {"status": resp.status, "step": resp.step,
         "instruction": resp.instruction, "tools": resp.tools}
    serialized = json.dumps(d)
    assert "in_progress" in serialized
    assert "analyze" in serialized


# ── Completed run returns completed on next calls ───────────────────

def test_next_on_completed_run(sf):
    sf.register_graph(_simple_skill_graph())
    tool = SkillTool(sf, "simple_skill")

    tool(action="next")
    tool(action="submit", result={"route": "a"})  # → branch_a
    resp = tool(action="submit", result={})        # → completed

    assert resp.status == "completed"

    # Further calls stay completed
    resp = tool(action="next")
    assert resp.status == "completed"
