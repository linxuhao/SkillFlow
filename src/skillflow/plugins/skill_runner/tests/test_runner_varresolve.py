"""Regression guard: runner mode resolves path variables in delegated
tool-node params before handing them to the driving agent.

In runner mode (delegate_tools_to_agent=True) a NON-native tool node is
delegated to the external agent — and its params must arrive with $STEP_DIR /
$PROJECT_ROOT expanded to real paths, not literal placeholders. That is exactly
the bug the skill_runner runner.py fix closed (a delegated lint/apply step
pointed at "$CONFIG_DIR/.../foo.yaml" used to get the unexpanded string).

Uses the real ToolLoader (the mock loader reports every tool as native, so
nothing delegates).
"""

from pathlib import Path

import yaml

import skillflow
from skillflow.core import SkillFlow
from skillflow.graph import PipelineGraph
from skillflow.tool_loader import ToolLoader
from skillflow.plugins.skill_runner import SkillTool

_NATIVE_TOOLS_DIR = Path(skillflow.__file__).parent / "tools"

# 'host_lint' is NOT a native tool, so in runner mode it is delegated to the
# agent (surfaced with resolved params) rather than auto-executed.
_GRAPH = """
name: toolvar_skill
begin: plan
end_conditions:
  combinator: or
  conditions:
    - type: node_reached
      node: done
      result: "completed"
steps:
  - id: plan
    step_type: agent
    agent_config: analyst
    transitions: [{to: lint_step}]
  - id: lint_step
    step_type: tool
    tool_name: host_lint
    tool_params:
      manifest: "$STEP_DIR/manifest.yaml"
      root: "$PROJECT_ROOT"
    transitions: [{to: done}]
  - id: done
    step_type: agent
    agent_config: analyst
"""


def _make_sf(tmp_path, *, with_workspace: bool):
    kw = dict(tool_loader=ToolLoader(_NATIVE_TOOLS_DIR),
              delegate_tools_to_agent=True)
    if with_workspace:
        kw.update(workspace_base=str(tmp_path / "ws"),
                  projects_base=str(tmp_path / "proj"))
    sf = SkillFlow(":memory:", **kw)
    sf.register_agent_config("analyst", model="mock")
    sf.register_graph(PipelineGraph._from_dict(yaml.safe_load(_GRAPH)))
    return sf


def test_delegated_tool_node_params_resolve_path_variables(tmp_path):
    sf = _make_sf(tmp_path, with_workspace=True)
    tool = SkillTool(sf, "toolvar_skill", project_id="tv")

    assert tool(action="next").step == "plan"
    resp = tool(action="submit", result={})  # plan done -> delegate lint_step

    assert resp.step == "lint_step"
    assert resp.tool_name == "host_lint"
    src = resp.tool_params["manifest"]
    root = resp.tool_params["root"]
    # The fix: real absolute paths, no literal placeholders.
    assert "$STEP_DIR" not in src and src.startswith("/") and src.endswith("manifest.yaml"), src
    assert "$PROJECT_ROOT" not in root and root.startswith("/"), root
    assert "lint_step" in src  # $STEP_DIR points at this step's dir


def test_delegated_tool_node_params_literal_without_workspace(tmp_path):
    """No workspace configured → nothing to resolve against; params pass through
    unchanged rather than crashing (defensive branch in _make_response)."""
    sf = _make_sf(tmp_path, with_workspace=False)
    tool = SkillTool(sf, "toolvar_skill", project_id="tv")

    assert tool(action="next").step == "plan"
    resp = tool(action="submit", result={})

    assert resp.step == "lint_step"
    assert resp.tool_params["manifest"] == "$STEP_DIR/manifest.yaml"
