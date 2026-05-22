"""Tests for skill converter — validates YAML and the converter pipeline config."""

from pathlib import Path

import pytest

from skillflow.core import SkillFlow
from skillflow.graph import PipelineGraph
from skillflow.plugins.linter import lint_config, skillflow_lint
from skillflow.plugins.skill_runner import SkillTool
from tests.mocks import MockToolLoader, create_standard_mock_tools


_CONVERTER_DIR = Path(__file__).parent.parent


# A minimal valid skillflow YAML the mock LLM would produce
VALID_SKILL_YAML = """
name: generated_skill
description: "Auto-generated skill pipeline"
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
    transitions:
      - to: router

  - id: router
    step_type: gate
    transitions:
      - to: branch_a
        match: {route: a}
      - to: branch_b
        match: {route: b}

  - id: branch_a
    step_type: agent
    agent_config: analyst
    transitions:
      - to: done

  - id: branch_b
    step_type: agent
    agent_config: analyst
    transitions:
      - to: done

  - id: done
    step_type: agent
    agent_config: analyst
"""

INVALID_SKILL_YAML = """
name: broken_skill
begin: analyze
steps:
  - id: analyze
    step_type: agent
    agent_config: analyst
    transitions:
      - to: missing_target
"""


@pytest.fixture
def sf():
    mt = MockToolLoader()
    # Register skillflow_lint so converter can use it as a tool
    mt.register("skillflow_lint", skillflow_lint)
    # Also register standard mock tools
    for name, fn in create_standard_mock_tools()._tools.items():
        mt.register(name, fn)
    return SkillFlow(":memory:", tool_loader=mt)


def test_linter_validates_generated_yaml():
    """The linter correctly validates YAML the converter would produce."""
    result = skillflow_lint(content=VALID_SKILL_YAML)
    assert result["passed"] is True
    assert result["errors"] == 0


def test_linter_detects_errors():
    """The linter catches broken YAML."""
    result = skillflow_lint(content=INVALID_SKILL_YAML)
    assert result["passed"] is False
    assert result["errors"] > 0
    assert any("missing_target" in i["message"] for i in result["issues"])


def test_converter_pipeline_config_is_valid():
    """The bundled skill_converter.yaml is itself a valid skillflow config."""
    converter_yaml = _CONVERTER_DIR / "skill_converter.yaml"
    issues = lint_config(converter_yaml)
    errors = [i for i in issues if i.severity == "error"]
    assert len(errors) == 0, f"Converter config has errors: {errors}"


def test_converter_pipeline_registers(sf):
    """The converter pipeline graph can be registered on SkillFlow."""
    from skillflow.plugins.skill_converter.converter import _register_converter_agents

    _register_converter_agents(sf)

    converter_graph = PipelineGraph.from_yaml(
        str(_CONVERTER_DIR / "skill_converter.yaml"))
    sf.register_graph(converter_graph)

    tool = SkillTool(sf, "skill_converter")
    assert tool.graph_name == "skill_converter"


def test_converter_yaml_loads_as_pipeline_graph():
    """The bundled converter YAML parses to a valid PipelineGraph."""
    graph = PipelineGraph.from_yaml(str(_CONVERTER_DIR / "skill_converter.yaml"))
    assert graph.name == "skill_converter"
    assert graph.begin == "analyze_skill"
    assert len(graph.steps) == 7  # analyze, design, explain, validate, fix, validate_fix, done
