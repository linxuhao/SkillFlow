"""Capability registry: a step's `capability` keyword → framework-provisioned
toolset + injected context (durable state_dir, tools_dir).

The point is LEAST PRIVILEGE: neither the pipeline author nor the agent picks a
tool's write folder or its extra tools — the FRAMEWORK hands them over, keyed on
the step's declared purpose. Regression cover for the AItelier CAC40 finding
where a generated stateful tool hardcoded ``Path.home()/".aitelier"`` (an
un-mounted, jail-escaping path) instead of receiving one.
"""

from skillflow.core import SkillFlow, StepResult
from skillflow.graph import (
    PipelineGraph, StepNode, Transition, EndConditions, EndCondition,
)
from skillflow.workspace import WorkspaceManager
from tests.mocks import MockToolLoader


# -- workspace.state_dir --------------------------------------------------
def test_state_dir_is_durable_per_config_and_jailed(tmp_path):
    ws = WorkspaceManager(base_path=str(tmp_path / "workspaces"))
    d = ws.state_dir("gen_cac40_daily")
    # lives BESIDE the workspaces root (not under a per-project/per-run dir), so
    # it survives across separate runs of the config and, for a mounted data
    # root, across container recreation.
    assert d == (tmp_path / "pipeline_state" / "gen_cac40_daily").resolve()
    assert d.is_dir()
    assert ws.state_dir("gen_other") != d
    assert str(ws.state_dir("gen_cac40_daily", item="MSFT")).startswith(str(d))


def test_state_dir_rejects_traversal(tmp_path):
    ws = WorkspaceManager(base_path=str(tmp_path / "workspaces"))
    root = (tmp_path / "pipeline_state").resolve()
    assert str(ws.state_dir("../../etc").resolve()).startswith(str(root))


# -- engine graphs --------------------------------------------------------
def _end(done="done"):
    return EndConditions(combinator="or", conditions=[
        EndCondition(type="node_reached", node=done, result="completed")])


def _agent_with_capability(cap):
    return PipelineGraph(
        name="captest", begin="build",
        steps=[
            StepNode(id="build", step_type="agent", agent_config="builder",
                     capability=cap, transitions=[Transition(to="done")]),
            StepNode(id="done", step_type="gate", transitions=[]),
        ],
        end_conditions=_end())


def test_capability_grants_agent_extra_tools(tmp_path):
    tools = MockToolLoader()
    sf = SkillFlow(str(tmp_path / "t.db"), tool_loader=tools)
    sf.register_agent_config("builder", tools=["read_file"])
    sf.register_capability("tool_creation",
                           tools=["write", "pytest", "register_tool"])
    sf.register_graph(_agent_with_capability("tool_creation"))
    run_id = sf.create_run("captest")
    sf.start_run(run_id)
    sf.advance_run(run_id)
    claimed = sf.claim_next_step(run_id)
    schemas = claimed.inputs.get("_tool_schemas", {})
    for t in ("write", "pytest", "register_tool"):
        assert t in schemas, f"capability tool {t!r} not granted: {sorted(schemas)}"


def test_capability_toolset_does_not_leak_into_shared_agent_config(tmp_path):
    tools = MockToolLoader()
    sf = SkillFlow(str(tmp_path / "t.db"), tool_loader=tools)
    sf.register_agent_config("builder", tools=["read_file"])
    sf.register_capability("tool_creation", tools=["register_tool"])
    sf.register_graph(_agent_with_capability("tool_creation"))
    run_id = sf.create_run("captest")
    sf.start_run(run_id)
    sf.advance_run(run_id)
    sf.claim_next_step(run_id)
    assert "register_tool" not in sf.agent_registry.get("builder").tool_schemas


def _tool_with_capability(cap):
    return PipelineGraph(
        name="captool", begin="gen",
        steps=[
            StepNode(id="gen", step_type="agent", agent_config="noop",
                     transitions=[Transition(to="persist")]),
            StepNode(id="persist", step_type="tool", tool_name="recorder",
                     capability=cap, transitions=[Transition(to="done")]),
            StepNode(id="done", step_type="gate", transitions=[]),
        ],
        end_conditions=_end())


def _drive_to_tool(sf, run_id, captured):
    sf.advance_run(run_id)
    claimed = sf.claim_next_step(run_id)
    sf.confirm_step(claimed.token, StepResult(outputs={}, flags={}))
    for _ in range(12):
        sf.advance_run(run_id)
        if captured:
            break


def test_capability_injects_state_dir_into_tool_step(tmp_path):
    captured = {}
    tools = MockToolLoader()
    tools.register("recorder", lambda **k: captured.update(k) or {"passed": True})
    sf = SkillFlow(str(tmp_path / "t.db"), tool_loader=tools,
                   workspace_base=str(tmp_path / "ws"))
    sf.register_agent_config("noop")
    sf.register_capability(
        "stateful",
        context_provider=lambda cfg: {"state_dir": str(sf._workspace.state_dir(cfg))})
    sf.register_graph(_tool_with_capability("stateful"))
    run_id = sf.create_run("captool", {"project_id": "p"})
    sf.start_run(run_id)
    _drive_to_tool(sf, run_id, captured)
    assert "state_dir" in captured, captured
    assert captured["state_dir"] == str(sf._workspace.state_dir("captool"))


def test_no_capability_means_no_injected_state_dir(tmp_path):
    captured = {}
    tools = MockToolLoader()
    tools.register("recorder", lambda **k: captured.update(k) or {"passed": True})
    sf = SkillFlow(str(tmp_path / "t.db"), tool_loader=tools,
                   workspace_base=str(tmp_path / "ws"))
    sf.register_agent_config("noop")
    sf.register_capability(
        "stateful",
        context_provider=lambda cfg: {"state_dir": "X"})
    sf.register_graph(_tool_with_capability(""))
    run_id = sf.create_run("captool", {"project_id": "p"})
    sf.start_run(run_id)
    _drive_to_tool(sf, run_id, captured)
    assert "state_dir" not in captured


def test_capability_injects_state_dir_into_context_source_tool(tmp_path):
    """A `{source: {tool: X}}` context tool runs on behalf of its reading step,
    so it must receive that step's capability context too (the 4th tool path)."""
    captured = {}
    tools = MockToolLoader()
    tools.register("loader", lambda **k: (captured.update(k) or {"content": "data"}))
    sf = SkillFlow(str(tmp_path / "t.db"), tool_loader=tools,
                   workspace_base=str(tmp_path / "ws"))
    sf.register_agent_config("dec")
    sf.register_capability(
        "stateful",
        context_provider=lambda cfg: {"state_dir": str(sf._workspace.state_dir(cfg))})
    g = PipelineGraph(
        name="ctxtool", begin="decide",
        steps=[
            StepNode(id="decide", step_type="agent", agent_config="dec",
                     capability="stateful",
                     context=[{"source": {"tool": "loader"}}],
                     transitions=[Transition(to="done")]),
            StepNode(id="done", step_type="gate", transitions=[]),
        ],
        end_conditions=_end())
    sf.register_graph(g)
    run_id = sf.create_run("ctxtool", {"project_id": "p"})
    sf.start_run(run_id)
    sf.advance_run(run_id)
    sf.claim_next_step(run_id)          # claim resolves context → invokes loader
    assert captured.get("state_dir") == str(sf._workspace.state_dir("ctxtool")), captured
