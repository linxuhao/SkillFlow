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


def test_state_dir_create_false_does_not_provision(tmp_path):
    """A read-only caller (catalog listing) must be able to resolve the path
    WITHOUT creating it — a GET that mkdirs makes reads side-effecting and
    litters the state root with empty dirs for configs that never wrote."""
    ws = WorkspaceManager(base_path=str(tmp_path / "workspaces"))
    d = ws.state_dir("gen_never_ran", create=False)
    assert not d.exists(), "create=False must not provision the directory"
    # same path as the creating call, so callers agree on location
    assert d == ws.state_dir("gen_never_ran")
    assert d.is_dir()          # the default call DID provision it


def test_state_dir_create_false_still_jails(tmp_path):
    ws = WorkspaceManager(base_path=str(tmp_path / "workspaces"))
    root = (tmp_path / "pipeline_state").resolve()
    assert ws.state_dir("../../etc", create=False).is_relative_to(root)


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


# ── Batch 1: declaration forms, offer lists, briefings, ownership ─────────
def _cap_graph(cap, *, offers=None, name="captest"):
    g = PipelineGraph(
        name=name, begin="build",
        steps=[
            StepNode(id="build", step_type="agent", agent_config="builder",
                     capability=cap, transitions=[Transition(to="done")]),
            StepNode(id="done", step_type="gate", transitions=[]),
        ],
        end_conditions=_end())
    if offers is not None:
        g.capabilities = list(offers)
    return g


def _claim(sf, graph):
    sf.register_graph(graph)
    run_id = sf.create_run(graph.name)
    sf.start_run(run_id)
    sf.advance_run(run_id)
    return sf.claim_next_step(run_id)


def test_capability_survives_serialization(tmp_path):
    """A step's `capability` must round-trip through to_dict/from_dict.

    It never did: `to_dict` simply did not emit the field, and registration
    persists `graph.to_dict()` while `compose_config` round-trips through it.
    So the declaration lived only in the object parsed straight from YAML in
    that process — every composed config, and every graph reloaded from the DB,
    silently lost it. Verified on the live deployment: pipeline_forge declares
    `capability: "tool_creation"` in its YAML and the stored graph had the key
    on no step at all.
    """
    g = _cap_graph("tool_creation", offers=["tool_creation"])
    back = PipelineGraph._from_dict(g.to_dict())
    assert back.steps[0].capability == "tool_creation"
    assert back.capabilities == ["tool_creation"]


def test_a_list_of_capabilities_grants_the_union(tmp_path):
    tools = MockToolLoader()
    sf = SkillFlow(str(tmp_path / "t.db"), tool_loader=tools)
    sf.register_agent_config("builder", tools=["read_file"])
    sf.register_capability("a", tools=["write"])
    sf.register_capability("b", tools=["pytest"])
    claimed = _claim(sf, _cap_graph(["a", "b"], offers=["a", "b"]))
    schemas = claimed.inputs.get("_tool_schemas", {})
    assert "write" in schemas and "pytest" in schemas


def test_a_capability_the_graph_does_not_offer_is_refused(tmp_path):
    """The offer list is the engine's own gate, not just the palette's filter.

    Whatever produced the name — a PM's task card, a hand edit — a pipeline
    grants only what it advertises. Without this check the card is the only
    authority, and editing a JSON file grants tools.
    """
    tools = MockToolLoader()
    sf = SkillFlow(str(tmp_path / "t.db"), tool_loader=tools)
    sf.register_agent_config("builder", tools=["read_file"])
    sf.register_capability("tool_creation", tools=["register_tool"])
    claimed = _claim(sf, _cap_graph("tool_creation", offers=["something_else"]))
    assert "register_tool" not in claimed.inputs.get("_tool_schemas", {})


def test_briefing_reaches_the_step_that_holds_the_capability(tmp_path):
    tools = MockToolLoader()
    sf = SkillFlow(str(tmp_path / "t.db"), tool_loader=tools)
    sf.register_agent_config("builder", tools=["read_file"])
    sf.register_capability("game_assets", tools=["write"],
                           briefing="transparent=true is mandatory")
    claimed = _claim(sf, _cap_graph("game_assets", offers=["game_assets"]))
    rc = claimed.inputs.get("_resolved_context") or {}
    assert any("transparent=true is mandatory" in str(v) for v in rc.values())
    assert claimed.inputs.get("_capabilities") == ["game_assets"]


def test_a_step_without_the_capability_carries_no_briefing(tmp_path):
    """The whole point: an unheld capability costs nothing, not even its text."""
    tools = MockToolLoader()
    sf = SkillFlow(str(tmp_path / "t.db"), tool_loader=tools)
    sf.register_agent_config("builder", tools=["read_file"])
    sf.register_capability("game_assets", tools=["write"], briefing="…")
    claimed = _claim(sf, _cap_graph("", offers=["game_assets"]))
    rc = claimed.inputs.get("_resolved_context") or {}
    assert not any("capability:" in str(k) for k in rc)
    assert "write" not in claimed.inputs.get("_tool_schemas", {})


def test_redefinition_by_another_owner_is_refused(tmp_path):
    """Same owner re-registering is an edit; a different owner is a silent
    substitution of what every holder is granted."""
    sf = SkillFlow(str(tmp_path / "t.db"))
    sf.register_capability("game_assets", tools=["a"], owner="addon:game_harness")
    sf.register_capability("game_assets", tools=["a", "b"],
                           owner="addon:game_harness")          # edit: fine
    import pytest
    with pytest.raises(ValueError) as e:
        sf.register_capability("game_assets", tools=["x"], owner="gen:evil")
    assert "already registered" in str(e.value)


def test_from_item_reads_the_loop_item_card(tmp_path):
    """`{from_item: ...}` grants per TASK, not per step.

    The card is named by the declaration (`card:`), interpolated with the same
    loop variables the context sources use.
    """
    tools = MockToolLoader()
    sf = SkillFlow(str(tmp_path / "t.db"), tool_loader=tools,
                   workspace_base=str(tmp_path / "ws"))
    ws = sf._workspace
    sf.register_agent_config("builder", tools=["read_file"])
    sf.register_capability("game_assets", tools=["write"],
                           briefing="pin the subject")

    import json as _json
    g = _cap_graph({"from_item": "capabilities",
                    "card": "3/tasks/card.json"},
                   offers=["game_assets"], name="fromitem")
    sf.register_graph(g)
    run_id = sf.create_run("fromitem", {"project_id": "p"})
    card_dir = ws.get_step_dir("p", "fromitem", "3") / "tasks"
    card_dir.mkdir(parents=True, exist_ok=True)
    (card_dir / "card.json").write_text(
        _json.dumps({"id": "t1", "capabilities": ["game_assets"]}))
    sf.start_run(run_id)
    sf.advance_run(run_id)
    claimed = sf.claim_next_step(run_id)
    assert "write" in claimed.inputs.get("_tool_schemas", {})
    assert claimed.inputs.get("_capabilities") == ["game_assets"]


def test_from_item_with_a_card_that_declares_nothing_grants_nothing(tmp_path):
    tools = MockToolLoader()
    sf = SkillFlow(str(tmp_path / "t.db"), tool_loader=tools,
                   workspace_base=str(tmp_path / "ws"))
    ws = sf._workspace
    sf.register_agent_config("builder", tools=["read_file"])
    sf.register_capability("game_assets", tools=["write"])
    import json as _json
    g = _cap_graph({"from_item": "capabilities", "card": "3/tasks/card.json"},
                   offers=["game_assets"], name="fromitem2")
    sf.register_graph(g)
    run_id = sf.create_run("fromitem2", {"project_id": "p"})
    d = ws.get_step_dir("p", "fromitem2", "3") / "tasks"
    d.mkdir(parents=True, exist_ok=True)
    (d / "card.json").write_text(_json.dumps({"id": "t1"}))
    sf.start_run(run_id)
    sf.advance_run(run_id)
    claimed = sf.claim_next_step(run_id)
    assert "write" not in claimed.inputs.get("_tool_schemas", {})
