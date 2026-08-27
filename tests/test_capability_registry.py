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


def test_a_card_cannot_grant_what_the_graph_does_not_offer(tmp_path):
    """The offer list bounds what DATA may grant.

    A task card is agent-authored: without this gate, writing a JSON file grants
    tools. Note the asymmetry — a name written into the GRAPH is the author's own
    declaration and is honoured (a graph with no offer list keeps working), while
    a name arriving from a card is refused unless advertised. An empty offer list
    therefore refuses every card-declared name rather than waving them through,
    which an earlier `and offers` short-circuit got backwards.
    """
    import json as _json
    tools = MockToolLoader()
    sf = SkillFlow(str(tmp_path / "t.db"), tool_loader=tools,
                   workspace_base=str(tmp_path / "ws"))
    sf.register_agent_config("builder", tools=["read_file"])
    sf.register_capability("tool_creation", tools=["register_tool"])
    g = _cap_graph({"from_item": "capabilities", "card": "3/card.json"},
                   offers=["something_else"], name="cardgate")
    sf.register_graph(g)
    run_id = sf.create_run("cardgate", {"project_id": "p"})
    d = sf._workspace.get_step_dir("p", "cardgate", "3")
    d.mkdir(parents=True, exist_ok=True)
    (d / "card.json").write_text(_json.dumps({"capabilities": ["tool_creation"]}))
    sf.start_run(run_id)
    sf.advance_run(run_id)
    claimed = sf.claim_next_step(run_id)
    assert "register_tool" not in claimed.inputs.get("_tool_schemas", {})


def test_a_graph_with_no_offer_list_still_honours_its_own_declaration(tmp_path):
    """Backwards compatibility, and the reason the gate is provenance-aware:
    every graph that already declares `capability:` (pipeline_forge does) has no
    offer list, and requiring one would revoke their grants."""
    tools = MockToolLoader()
    sf = SkillFlow(str(tmp_path / "t.db"), tool_loader=tools)
    sf.register_agent_config("builder", tools=["read_file"])
    sf.register_capability("tool_creation", tools=["register_tool"])
    claimed = _claim(sf, _cap_graph("tool_creation", name="nolist"))
    assert "register_tool" in claimed.inputs.get("_tool_schemas", {})


def test_a_static_capability_outside_the_offer_list_fails_registration(tmp_path):
    """Knowable statically → rejected at register_graph, not once per claim."""
    import pytest as _pytest
    from skillflow.exceptions import GraphValidationError
    sf = SkillFlow(str(tmp_path / "t.db"), tool_loader=MockToolLoader())
    sf.register_agent_config("builder", tools=["read_file"])
    with _pytest.raises(GraphValidationError):
        sf.register_graph(_cap_graph("tool_creation", offers=["other"],
                                     name="contradiction"))


def test_a_granted_tool_can_actually_be_called(tmp_path):
    """Offered-then-denied is worse than never offered.

    claim_next_step SHOWS the agent every tool a capability grants; the execution
    allowlist decides whether the call is honoured, and it never consulted
    capabilities — so `capability: "tool_creation"` advertised register_tool and
    then answered "Tool 'register_tool' not allowed". The same bug had already
    been fixed once for the addon `add_tools` channel, one lane over.
    """
    tools = MockToolLoader()
    sf = SkillFlow(str(tmp_path / "t.db"), tool_loader=tools)
    sf.register_agent_config("builder", tools=["read_file"])
    sf.register_capability("tool_creation", tools=["register_tool"])
    claimed = _claim(sf, _cap_graph("tool_creation", name="callable"))
    assert "register_tool" in claimed.inputs.get("_tool_schemas", {})
    # The mock loader has no fn for it, so the call gets as far as loading and
    # raises — which is the point: it is no longer REFUSED by the gate.
    try:
        out = sf.execute_tool("register_tool", {}, run_id=claimed.token.run_id,
                              step_id="build")
    except ImportError:
        out = {}
    assert "not allowed" not in str(out.get("error", ""))


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


def test_a_card_declared_capability_still_hands_its_tools_the_state_dir(tmp_path):
    """The kwargs half of a `{from_item:}` grant.

    The three kwarg paths (context-source tool, tool node, agent-invoked tool)
    re-derived the capability from the NODE, which for a card declaration needs
    the loop item's card they do not have — so they resolved to nothing. The
    task got the tools and then let them pick their own directories, which is
    the `Path.home()/".aitelier"` failure the whole mechanism exists to prevent.
    """
    import json as _json
    tools = MockToolLoader()
    sf = SkillFlow(str(tmp_path / "t.db"), tool_loader=tools,
                   workspace_base=str(tmp_path / "ws"))
    sf.register_agent_config("builder", tools=["read_file"])
    sf.register_capability(
        "stateful", tools=["write"],
        context_provider=lambda cfg: {"state_dir": f"/state/{cfg}"})
    g = _cap_graph({"from_item": "capabilities", "card": "3/card.json"},
                   offers=["stateful"], name="kwargs")
    sf.register_graph(g)
    run_id = sf.create_run("kwargs", {"project_id": "p"})
    d = sf._workspace.get_step_dir("p", "kwargs", "3")
    d.mkdir(parents=True, exist_ok=True)
    (d / "card.json").write_text(_json.dumps({"capabilities": ["stateful"]}))
    sf.start_run(run_id)
    sf.advance_run(run_id)
    claimed = sf.claim_next_step(run_id)
    assert claimed.inputs.get("_capabilities") == ["stateful"]
    node = sf._get_resolver("kwargs").get_node("build")
    kw = sf._capability_context(node, "kwargs",
                                names=sf._granted_capabilities(run_id, "build"))
    assert kw == {"state_dir": "/state/kwargs"}


def test_a_capability_card_cannot_escape_the_config_directory(tmp_path):
    """`card:` interpolates a loop item read out of an LLM-authored manifest."""
    import json as _json
    tools = MockToolLoader()
    sf = SkillFlow(str(tmp_path / "t.db"), tool_loader=tools,
                   workspace_base=str(tmp_path / "ws"))
    sf.register_agent_config("builder", tools=["read_file"])
    sf.register_capability("stateful", tools=["write"])
    outside = tmp_path / "outside.json"
    outside.write_text(_json.dumps({"capabilities": ["stateful"]}))
    g = _cap_graph({"from_item": "capabilities",
                    "card": "3/../../../../outside.json"},
                   offers=["stateful"], name="escape")
    sf.register_graph(g)
    run_id = sf.create_run("escape", {"project_id": "p"})
    d = sf._workspace.get_step_dir("p", "escape", "3")
    d.mkdir(parents=True, exist_ok=True)
    sf.start_run(run_id)
    sf.advance_run(run_id)
    claimed = sf.claim_next_step(run_id)
    assert claimed.inputs.get("_capabilities") == []
    assert "write" not in claimed.inputs.get("_tool_schemas", {})


def test_conflicting_capability_kwargs_do_not_wedge_the_run(tmp_path):
    """Two capabilities disagreeing about a key is an author error; it must not
    take the run down. Raising did: the tool-node path let it escape advance_run
    (the run then sat at its node forever, failing nothing), and the claim path
    swallowed it into a step with no context at all."""
    tools = MockToolLoader()
    sf = SkillFlow(str(tmp_path / "t.db"), tool_loader=tools)
    sf.register_agent_config("builder", tools=["read_file"])
    sf.register_capability("a", tools=["write"],
                           context_provider=lambda cfg: {"state_dir": "/A"})
    sf.register_capability("b", tools=["pytest"],
                           context_provider=lambda cfg: {"state_dir": "/B"})
    g = _cap_graph(["a", "b"], offers=["a", "b"], name="clash")
    sf.register_graph(g)
    node = sf._get_resolver("clash").get_node("build")
    kw = sf._capability_context(node, "clash", offers=["a", "b"])
    assert "state_dir" not in kw          # omitted, not guessed


def test_the_capability_table_has_a_public_accessor(tmp_path):
    """The host reads this table in six places. Reaching into `_capabilities`
    means a rename here degrades every one of them silently — the emit gate
    returns no violations and goes green."""
    sf = SkillFlow(str(tmp_path / "t.db"), tool_loader=MockToolLoader())
    sf.register_capability("x", tools=["write"], briefing="b", owner="host")
    got = sf.capabilities()
    assert got["x"]["tools"] == ["write"]
    assert got["x"]["owner"] == "host" and got["x"]["briefing"] == "b"
    assert "context_provider" not in got["x"], "the callable is the engine's"
    got["x"]["tools"].append("leak")
    assert sf._capabilities["x"]["tools"] == ["write"], "accessor returned a live ref"


def test_graph_offers_have_a_public_accessor(tmp_path):
    sf = SkillFlow(str(tmp_path / "t.db"), tool_loader=MockToolLoader())
    sf.register_agent_config("builder", tools=["read_file"])
    sf.register_capability("tool_creation", tools=["register_tool"])
    sf.register_graph(_cap_graph("tool_creation", offers=["tool_creation"],
                                 name="offers"))
    assert sf.graph_capabilities("offers") == ["tool_creation"]
    assert sf.graph_capabilities("nope") == []
