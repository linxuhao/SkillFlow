"""Tests for the addon_converter pipeline + compose_validate tool.

addon_converter is the sibling of skill_converter that authors an OVERLAY (ops
against a base graph's anchors) instead of a standalone graph. Its acceptance
test is compose_validate: the overlay must actually compose onto the real base
and yield a valid graph.

Covered here:
  * compose_validate passes a good overlay, fails a bad one (unknown anchor).
  * the addon_converter graph loads + validates.
  * end-to-end drive of the graph with MOCKED agent outputs → composed overlay.
"""

import json
from pathlib import Path

import pytest
import yaml

from skillflow.core import SkillFlow
from skillflow.graph import PipelineGraph
from skillflow.tool_loader import ToolLoader
from skillflow.plugins.skill_runner import SkillTool
from skillflow.plugins.skill_converter.converter import (
    _register_addon_converter_agents, get_addon_output_file)
from tests.mocks import MockToolLoader, create_standard_mock_tools


_ADDON_CONVERTER_YAML = (Path(__file__).parent.parent / "src" / "skillflow"
                         / "plugins" / "skill_converter" / "addon_converter.yaml")
_NATIVE_TOOLS_DIR = Path(__file__).parent.parent / "src" / "skillflow" / "tools"


# ── A tiny base graph with anchors (self-contained; no host dep) ─────

BASE_GRAPH_YAML = """
name: mini_base
description: "minimal base with an anchor"
begin: a
anchors:
  mid: b
end_conditions:
  combinator: or
  conditions:
    - type: node_reached
      node: c
      result: "completed"
steps:
  - id: a
    step_type: agent
    agent_config: r
    transitions:
      - to: b
  - id: b
    step_type: agent
    agent_config: r
    transitions:
      - to: c
  - id: c
    step_type: agent
    agent_config: r
"""

GOOD_OVERLAY_YAML = """
name: mini_addon
base: mini_base
alias: mini_plus
description: "adds a gate after the mid anchor"
whenToUse: "testing composition"
overlay:
  - insert_after: "@mid"
    steps:
      - id: mid_gate
        step_type: tool
        tool_name: some_tool
        tool_params: { out_dir: "$STEP_DIR" }
"""

# References an anchor that does not exist in the base → must fail composition.
BAD_OVERLAY_YAML = """
name: bad_addon
base: mini_base
description: "targets a non-existent anchor"
overlay:
  - insert_after: "@nonexistent"
    steps:
      - id: ghost
        step_type: tool
        tool_name: some_tool
"""


def _compose_validate():
    """The real native tool fn via the ToolLoader (tests the on-disk wiring)."""
    return ToolLoader(_NATIVE_TOOLS_DIR).load_fn("compose_validate")


def _base_graph_dict() -> str:
    """Base graph as a to_dict() YAML (anchors intact), like the host seeds it."""
    g = PipelineGraph._from_dict(yaml.safe_load(BASE_GRAPH_YAML))
    return yaml.safe_dump(g.to_dict(), sort_keys=False)


# ── compose_validate tool ────────────────────────────────────────────

def test_compose_validate_passes_good_overlay():
    fn = _compose_validate()
    result = fn(overlay_content=GOOD_OVERLAY_YAML, base_content=_base_graph_dict())
    assert result["passed"] is True, result["errors"]
    assert result["errors"] == []
    assert "composes cleanly" in result["summary"]


def test_compose_validate_fails_unknown_anchor():
    fn = _compose_validate()
    result = fn(overlay_content=BAD_OVERLAY_YAML, base_content=_base_graph_dict())
    assert result["passed"] is False
    assert any("anchor" in e.lower() for e in result["errors"]), result["errors"]


def test_compose_validate_fails_base_mismatch():
    fn = _compose_validate()
    mism = GOOD_OVERLAY_YAML.replace("base: mini_base", "base: other_base")
    result = fn(overlay_content=mism, base_content=_base_graph_dict())
    assert result["passed"] is False
    assert any("binds to base" in e for e in result["errors"]), result["errors"]


def test_compose_validate_writes_report(tmp_path):
    fn = _compose_validate()
    result = fn(overlay_content=GOOD_OVERLAY_YAML, base_content=_base_graph_dict(),
                out_dir=str(tmp_path))
    report = tmp_path / "compose_report.json"
    assert report.exists()
    on_disk = json.loads(report.read_text())
    assert on_disk == result


# ── addon_converter graph ─────────────────────────────────────────────

def test_addon_converter_graph_loads_and_validates():
    sf = SkillFlow(":memory:")
    _register_addon_converter_agents(sf)
    graph = PipelineGraph.from_yaml(str(_ADDON_CONVERTER_YAML))
    # register_graph re-validates reachability/cycles/agent refs.
    sf.register_graph(graph)
    assert "addon_converter" in sf._graphs
    assert graph.validate() == []


# ── End-to-end (mocked agents) ────────────────────────────────────────

class MockLLMAgent:
    """Drives a SkillTool with canned per-step results, writing output_fixed
    files to tmp_dir before submit (so lifecycle/promotion + tool reads work).
    Adapted from the skill_converter e2e mock."""

    def __init__(self, tool, sf, responses):
        self.tool = tool
        self.sf = sf
        self.responses = responses
        self.call_count: dict[str, int] = {}

    def run(self):
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
            step_responses = self.responses.get(step, {})
            result = step_responses.get(attempt, step_responses.get(0, {}))
            self._write_output_files(step, result)
            resp = self.tool(action="submit", result=result)
        return {"status": resp.status, "outputs": resp.outputs, "error": resp.error}

    def _write_output_files(self, step_id, result):
        if not self.sf._workspace or not self.tool.run_id:
            return
        pid = self.sf._get_project_id(self.tool.run_id)
        gname = self.sf._get_graph_name(self.tool.run_id)
        try:
            resolver = self.sf._get_resolver(self.tool.graph_name)
            node = resolver.get_node(step_id)
            if not node or not node.output_fixed:
                return
        except Exception:
            return
        tmp_dir = self.sf._workspace.get_step_tmp_dir(pid, gname, step_id)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        for slot, spec in node.output_fixed.items():
            filename = spec if isinstance(spec, str) else spec.get("file", f"{slot}.json")
            content = result.get(slot, "")
            if isinstance(content, (dict, list)):
                content = json.dumps(content, indent=2)
            (tmp_dir / filename).write_text(str(content), encoding="utf-8")


@pytest.fixture
def sf_with_ws(tmp_path):
    mt = MockToolLoader()
    mt.register("compose_validate", _compose_validate())
    for name, fn in create_standard_mock_tools()._tools.items():
        mt.register(name, fn)
    return SkillFlow(
        ":memory:",
        tool_loader=mt,
        workspace_base=str(tmp_path / "workspaces"),
        projects_base=str(tmp_path / "projects"),
    )


def _seed_base(sf, pid):
    """Seed base_graph.yaml + base_spec.json + addon_description.md into the
    config's _seed dir, exactly like the host's generate_addon tool does."""
    seed_dir = sf._workspace.get_config_path(pid, "addon_converter") / "_seed"
    seed_dir.mkdir(parents=True, exist_ok=True)
    base = yaml.safe_load(BASE_GRAPH_YAML)
    g = PipelineGraph._from_dict(base)
    (seed_dir / "base_graph.yaml").write_text(
        yaml.safe_dump(g.to_dict(), sort_keys=False), encoding="utf-8")
    spec = {"name": g.name, "anchors": g.anchors,
            "steps": [s.id for s in g.steps], "anchor_targets": {}}
    (seed_dir / "base_spec.json").write_text(json.dumps(spec), encoding="utf-8")
    (seed_dir / "addon_description.md").write_text(
        "Add a gate after the mid anchor.", encoding="utf-8")


ANALYSIS = {"intent": "add a gate", "injections": [{"anchor": "mid",
            "what": "gate", "kind": "insert_after"}], "tools": ["some_tool"],
            "context_additions": [], "template_fragments": []}
EXPLAIN = "# Overlay\n\nInserts mid_gate after @mid. Ready to compose-validate."


def test_addon_converter_e2e_good_overlay(sf_with_ws):
    """Full drive: analyze → design (good) → explain (checkpoint) → validate → done."""
    sf = sf_with_ws
    _register_addon_converter_agents(sf)
    sf.register_graph(PipelineGraph.from_yaml(str(_ADDON_CONVERTER_YAML)))

    pid = "addon-e2e"
    _seed_base(sf, pid)
    tool = SkillTool(sf, "addon_converter", project_id=pid)

    responses = {
        "analyze_addon": {1: {"analysis": ANALYSIS}},
        "design_overlay": {1: {"overlay": GOOD_OVERLAY_YAML}},
        "explain_overlay": {1: {"explanation": EXPLAIN}},
    }
    agent = MockLLMAgent(tool, sf, responses)
    result = agent.run()

    assert result["status"] == "completed", result.get("error")
    # No fix loop needed for a good overlay.
    assert agent.call_count.get("fix_overlay", 0) == 0
    # The composed overlay is recoverable + re-composes cleanly.
    out = get_addon_output_file(sf, tool.run_id)
    assert out is not None and out.exists()
    spec = yaml.safe_load(out.read_text())
    assert spec["name"] == "mini_addon"


def test_addon_converter_e2e_bad_then_fixed(sf_with_ws):
    """A bad first overlay fails compose_validate → fix_overlay corrects it."""
    sf = sf_with_ws
    _register_addon_converter_agents(sf)
    sf.register_graph(PipelineGraph.from_yaml(str(_ADDON_CONVERTER_YAML)))

    pid = "addon-e2e-fix"
    _seed_base(sf, pid)
    tool = SkillTool(sf, "addon_converter", project_id=pid)

    responses = {
        "analyze_addon": {1: {"analysis": ANALYSIS}},
        "design_overlay": {1: {"overlay": BAD_OVERLAY_YAML}},   # unknown anchor
        "explain_overlay": {1: {"explanation": EXPLAIN}},
        "fix_overlay": {1: {"overlay": GOOD_OVERLAY_YAML}},     # corrected
    }
    agent = MockLLMAgent(tool, sf, responses)
    result = agent.run()

    assert result["status"] == "completed", result.get("error")
    assert agent.call_count.get("fix_overlay", 0) >= 1
    out = get_addon_output_file(sf, tool.run_id)
    assert out is not None and yaml.safe_load(out.read_text())["name"] == "mini_addon"
