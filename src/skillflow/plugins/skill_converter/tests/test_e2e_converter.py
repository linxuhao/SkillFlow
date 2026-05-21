"""End-to-end converter test: skill description → broken YAML → linter → fix → valid.

Simulates a real debug session where the converter pipeline runs with
an LLM that makes mistakes on the first design pass, the linter catches them,
and the fix loop corrects them. Exercises all three plugins together.
"""

import json
from pathlib import Path

import pytest

from skillflow.core import SkillFlow, StepResult
from skillflow.graph import PipelineGraph
from skillflow.plugins.linter import lint_config, skillflow_lint
from skillflow.plugins.skill_runner import SkillTool
from skillflow.plugins.skill_converter.converter import _register_converter_agents
from tests.mocks import MockToolLoader, create_standard_mock_tools


_CONVERTER_DIR = Path(__file__).parent.parent
_FIXTURES_DIR = Path(__file__).parent.parent.parent.parent.parent.parent / "tests" / "fixtures"


# ── Canned LLM responses ────────────────────────────────────────────

ANALYSIS_JSON = json.dumps({
    "phases": ["analyze_diff", "review", "done"],
    "decisions": [
        {"condition": "Review passed", "branches": ["done", "analyze_diff"]}
    ],
    "terminal_condition": "All review items resolved",
    "tools_per_phase": {
        "analyze_diff": ["read_file", "grep"],
        "review": ["read_file", "write"],
    },
    "checkpoints": [],
})

# Intentional errors: missing end_conditions, missing agent_config, cycle
BROKEN_YAML = """
name: broken_skill
begin: analyze
steps:
  - id: analyze
    step_type: agent
    agent_config: analyst
    transitions:
      - to: review
  - id: review
    step_type: agent
    agent_config: reviewer
    transitions:
      - to: analyze
"""

# Corrected version with end_conditions and max_loop
CORRECTED_YAML = """
name: review_skill
description: "Code review skill with fix loop"
begin: analyze_diff

end_conditions:
  combinator: or
  conditions:
    - type: node_reached
      node: done
      result: "completed"

steps:
  - id: analyze_diff
    step_type: agent
    agent_config: analyst
    transitions:
      - to: review

  - id: review
    step_type: agent
    agent_config: reviewer
    transitions:
      - to: done
        match: {approved: true}
      - to: analyze_diff
        match: {approved: false}
        max_loop: 3

  - id: done
    step_type: agent
    agent_config: analyst
"""


# ── Mock LLM Agent ──────────────────────────────────────────────────

class MockLLMAgent:
    """Simulates an LLM agent calling SkillTool.

    Before submitting a step, writes any output_fixed files to the
    step's tmp_dir so lifecycle hooks (step_commit) can promote them
    and downstream from_file / tool reads work correctly.
    """

    def __init__(self, tool: SkillTool, sf: SkillFlow, responses: dict[str, dict]):
        self.tool = tool
        self.sf = sf
        self.responses = responses
        self.call_count: dict[str, int] = {}

    def run(self) -> dict:
        resp = self.tool(action="next")

        while resp.status not in ("completed", "failed"):
            if resp.status == "paused":
                resp = self.tool(action="approve")
                continue

            if resp.status != "in_progress":
                break

            step = resp.step
            self.call_count[step] = self.call_count.get(step, 0) + 1
            attempt = self.call_count[step]

            # Get canned response for this step attempt
            step_responses = self.responses.get(step, {})
            result = step_responses.get(attempt, step_responses.get(0, {}))

            # Write output files to tmp_dir before confirm
            self._write_output_files(step, result)

            resp = self.tool(action="submit", result=result)

        return {
            "status": resp.status,
            "outputs": resp.outputs,
            "error": resp.error,
            "steps_completed": resp.steps_completed,
        }

    def _write_output_files(self, step_id: str, result: dict):
        """If the step has output_fixed entries like 'pipeline', write them."""
        if not self.sf._workspace:
            return

        run_id = self.tool.run_id
        if not run_id:
            return

        pid = self.sf._get_project_id(run_id)
        gname = self.sf._get_graph_name(run_id)

        # Check what output_fixed files the step expects
        try:
            resolver = self.sf._get_resolver(self.tool.graph_name)
            node = resolver.get_node(step_id)
            if not node or not node.output_fixed:
                return
        except Exception:
            return

        # Write each fixed output file to tmp_dir
        tmp_dir = self.sf._workspace.get_step_tmp_dir(pid, gname, step_id)
        tmp_dir.mkdir(parents=True, exist_ok=True)

        for slot, spec in node.output_fixed.items():
            filename = spec if isinstance(spec, str) else spec.get("file", f"{slot}.json")
            content = result.get(slot, "")
            if isinstance(content, (dict, list)):
                content = json.dumps(content, indent=2)
            (tmp_dir / filename).write_text(str(content), encoding="utf-8")


# ── Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def sf_no_ws():
    """SkillFlow without workspace — for linter-only tests."""
    mt = MockToolLoader()
    mt.register("skillflow_lint", skillflow_lint)
    for name, fn in create_standard_mock_tools()._tools.items():
        mt.register(name, fn)
    return SkillFlow(":memory:", tool_loader=mt)


@pytest.fixture
def sf_with_ws(tmp_path):
    """SkillFlow with workspace — for converter e2e tests."""
    mt = MockToolLoader()
    mt.register("skillflow_lint", skillflow_lint)
    for name, fn in create_standard_mock_tools()._tools.items():
        mt.register(name, fn)
    return SkillFlow(
        ":memory:",
        tool_loader=mt,
        workspace_base=str(tmp_path / "workspaces"),
        projects_base=str(tmp_path / "projects"),
    )


# ── Static lint tests (no workspace needed) ──────────────────────────

def test_skill_review_fixture_is_valid():
    """The skill_review fixture is a realistic valid config."""
    issues = lint_config(str(_FIXTURES_DIR / "skill_review.yaml"))
    errors = [i for i in issues if i.severity == "error"]
    assert len(errors) == 0, f"skill_review.yaml has errors: {errors}"


def test_linter_detects_errors_in_broken():
    result = skillflow_lint(content=BROKEN_YAML)
    assert result["passed"] is False
    messages = [i["message"] for i in result["issues"]]
    assert any("max_loop" in m.lower() or "cycle" in m.lower() for m in messages), (
        f"Expected cycle error, got: {messages}")


def test_linter_validates_corrected():
    result = skillflow_lint(content=CORRECTED_YAML)
    assert result["passed"] is True
    assert result["errors"] == 0


# ── E2E converter tests (need workspace) ─────────────────────────────

def test_converter_e2e_broken_then_fixed(sf_with_ws):
    """Full debug session: broken YAML → linter catches → LLM fixes → valid.

    The converter pipeline: analyze → design (broken) → validate (fails) →
    fix_issues → validate (passes) → completed.
    """
    sf = sf_with_ws
    _register_converter_agents(sf)

    graph = PipelineGraph.from_yaml(str(_CONVERTER_DIR / "skill_converter.yaml"))
    sf.register_graph(graph)

    tool = SkillTool(sf, "skill_converter")

    responses = {
        "analyze_skill": {
            1: {"analysis": json.loads(ANALYSIS_JSON)},
        },
        "design_graph": {
            1: {"pipeline": BROKEN_YAML},
        },
        "fix_issues": {
            1: {"pipeline": CORRECTED_YAML},
        },
    }

    agent = MockLLMAgent(tool, sf, responses)
    result = agent.run()

    assert result["status"] == "completed", (
        f"Converter failed: {result.get('error')}")
    assert agent.call_count.get("design_graph", 0) >= 1
    # fix_issues must have been called — broken YAML triggers the loop
    assert agent.call_count.get("fix_issues", 0) >= 1


def test_converter_valid_first_attempt_skips_fix(sf_with_ws):
    """When design_graph produces valid YAML, fix_issues is never called."""
    sf = sf_with_ws
    _register_converter_agents(sf)

    graph = PipelineGraph.from_yaml(str(_CONVERTER_DIR / "skill_converter.yaml"))
    sf.register_graph(graph)

    tool = SkillTool(sf, "skill_converter")

    responses = {
        "analyze_skill": {
            1: {"analysis": json.loads(ANALYSIS_JSON)},
        },
        "design_graph": {
            1: {"pipeline": CORRECTED_YAML},
        },
    }

    agent = MockLLMAgent(tool, sf, responses)
    result = agent.run()

    assert result["status"] == "completed"
    assert agent.call_count.get("fix_issues", 0) == 0


def test_converter_multiple_fix_attempts(sf_with_ws):
    """Converter: design produces broken, fix_issues also broken first time,
    then fix_issues succeeds on second attempt (max_loop=3)."""
    sf = sf_with_ws
    _register_converter_agents(sf)

    graph = PipelineGraph.from_yaml(str(_CONVERTER_DIR / "skill_converter.yaml"))
    sf.register_graph(graph)

    tool = SkillTool(sf, "skill_converter")

    # Fix still broken on attempt 1, correct on attempt 2
    responses = {
        "analyze_skill": {
            1: {"analysis": json.loads(ANALYSIS_JSON)},
        },
        "design_graph": {
            1: {"pipeline": BROKEN_YAML},
        },
        "fix_issues": {
            1: {"pipeline": BROKEN_YAML},     # first fix attempt: still broken
            2: {"pipeline": CORRECTED_YAML},   # second fix attempt: correct
        },
    }

    agent = MockLLMAgent(tool, sf, responses)
    result = agent.run()

    assert result["status"] == "completed"
    # fix_issues was called twice (first fix broken, second succeeds)
    assert agent.call_count.get("fix_issues", 0) == 2


def test_linter_feedback_in_fix_step(sf_with_ws):
    """Verify linter feedback is injected into fix_issues via feedback: true."""
    sf = sf_with_ws
    _register_converter_agents(sf)

    graph = PipelineGraph.from_yaml(str(_CONVERTER_DIR / "skill_converter.yaml"))
    sf.register_graph(graph)

    tool = SkillTool(sf, "skill_converter")

    # Track what the fix_issues step receives
    fix_instruction = []

    class TrackingTool(SkillTool):
        def _advance_and_respond(self):
            resp = super()._advance_and_respond()
            if resp.step == "fix_issues" and resp.status == "in_progress":
                fix_instruction.append(resp.instruction)
            return resp

    tracking = TrackingTool(sf, "skill_converter")

    responses = {
        "analyze_skill": {1: {"analysis": json.loads(ANALYSIS_JSON)}},
        "design_graph": {1: {"pipeline": BROKEN_YAML}},
        "fix_issues": {1: {"pipeline": CORRECTED_YAML}},
    }

    agent = MockLLMAgent(tracking, sf, responses)
    result = agent.run()

    assert result["status"] == "completed"
    assert len(fix_instruction) >= 1
    # The fix instruction should mention linter feedback
    fix_text = fix_instruction[0].lower()
    assert any(term in fix_text for term in ["feedback", "linter", "error", "issue"]), (
        f"Fix step didn't receive linter feedback: {fix_instruction[0][:200]}")
