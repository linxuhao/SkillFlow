"""Tests for graph composition / addon overlays (skillflow.compose)."""

import pytest

from skillflow.compose import ComposeError, compose_graph
from skillflow.graph import PipelineGraph


def _base():
    # a -> b -> c(terminal)
    return {
        "name": "base",
        "begin": "a",
        "anchors": {"post_b": "b"},
        "steps": [
            {"id": "a", "step_type": "agent", "transitions": [{"to": "b"}]},
            {"id": "b", "step_type": "agent", "transitions": [{"to": "c"}]},
            {"id": "c", "step_type": "agent", "transitions": [{"to": None}]},
        ],
    }


def _harness_overlay(anchor="@post_b"):
    return {
        "name": "harness",
        "overlay": [
            {"insert_after": anchor,
             "steps": [{"id": "compile", "step_type": "tool", "tool_name": "godot_compile"}]},
        ],
    }


def test_insert_after_splices_into_edge():
    merged = compose_graph(_base(), [_harness_overlay()])
    by_id = {s["id"]: s for s in merged["steps"]}
    # b now points at the injected step, which points at b's original target c.
    assert by_id["b"]["transitions"] == [{"to": "compile"}]
    assert by_id["compile"]["transitions"] == [{"to": "c"}]
    # anchors metadata is stripped from the result.
    assert "anchors" not in merged


def test_raw_step_id_anchor_works_without_at():
    merged = compose_graph(_base(), [_harness_overlay(anchor="b")])
    by_id = {s["id"]: s for s in merged["steps"]}
    assert by_id["b"]["transitions"] == [{"to": "compile"}]


def test_multi_step_chain_wires_sequentially():
    ov = {"name": "h", "overlay": [{"insert_after": "b", "steps": [
        {"id": "s1", "step_type": "tool"},
        {"id": "s2", "step_type": "tool"},
    ]}]}
    by_id = {s["id"]: s for s in compose_graph(_base(), [ov])["steps"]}
    assert by_id["b"]["transitions"] == [{"to": "s1"}]
    assert by_id["s1"]["transitions"] == [{"to": "s2"}]
    assert by_id["s2"]["transitions"] == [{"to": "c"}]  # tail inherits original edge


def test_explicit_transitions_on_injected_step_are_kept():
    ov = {"name": "h", "overlay": [{"insert_after": "b", "steps": [
        {"id": "gate", "step_type": "gate",
         "transitions": [{"to": "c"}, {"to": "b", "match": {"retry": True}, "max_loop": 2}]},
    ]}]}
    by_id = {s["id"]: s for s in compose_graph(_base(), [ov])["steps"]}
    # gate kept its own (loop-back) wiring; not auto-rewired to the tail.
    assert {"to": "b", "match": {"retry": True}, "max_loop": 2} in by_id["gate"]["transitions"]


def test_insert_after_terminal_node_extends_tail():
    ov = {"name": "h", "overlay": [{"insert_after": "c", "steps": [
        {"id": "post", "step_type": "tool"}]}]}
    by_id = {s["id"]: s for s in compose_graph(_base(), [ov])["steps"]}
    assert by_id["c"]["transitions"] == [{"to": "post"}]
    assert by_id["post"]["transitions"] == [{"to": None}]


def test_branching_anchor_requires_after_match():
    base = _base()
    # give b two transitions
    b = next(s for s in base["steps"] if s["id"] == "b")
    b["transitions"] = [{"to": "c", "match": {"passed": True}}, {"to": "a", "match": {"passed": False}}]
    ov = {"name": "h", "overlay": [{"insert_after": "b", "steps": [{"id": "x", "step_type": "tool"}]}]}
    with pytest.raises(ComposeError, match="disambiguate"):
        compose_graph(base, [ov])
    # with after_match it reroutes only the matched edge
    ov2 = {"name": "h", "overlay": [{"insert_after": "b", "after_match": {"passed": True},
                                     "steps": [{"id": "x", "step_type": "tool"}]}]}
    by_id = {s["id"]: s for s in compose_graph(base, [ov2])["steps"]}
    assert {"to": "x", "match": {"passed": True}} in by_id["b"]["transitions"]
    assert by_id["x"]["transitions"] == [{"to": "c"}]


def test_add_context_appends_source():
    ov = {"name": "h", "overlay": [
        {"insert_after": "b", "steps": [{"id": "compile", "step_type": "tool"}]},
        {"add_context": "c", "source": {"step": "compile"}},
    ]}
    by_id = {s["id"]: s for s in compose_graph(_base(), [ov])["steps"]}
    assert {"source": {"step": "compile"}} in by_id["c"]["context"]


def test_add_context_is_idempotent():
    ov = {"name": "h", "overlay": [
        {"add_context": "c", "source": {"step": "x"}},
        {"add_context": "c", "source": {"step": "x"}},
    ]}
    by_id = {s["id"]: s for s in compose_graph(_base(), [ov])["steps"]}
    assert by_id["c"]["context"].count({"source": {"step": "x"}}) == 1


def test_add_template_attaches_fragment_to_step_config():
    ov = {"name": "h", "overlay": [
        {"add_template": "@post_b", "fragment": "addons/game/architect.md"},
    ]}
    by_id = {s["id"]: s for s in compose_graph(_base(), [ov])["steps"]}
    assert by_id["b"]["config"]["extra_templates"] == ["addons/game/architect.md"]


def test_add_template_is_idempotent_and_stacks():
    ov = {"name": "h", "overlay": [
        {"add_template": "b", "fragment": "a.md"},
        {"add_template": "b", "fragment": "a.md"},
        {"add_template": "b", "fragment": "b.md"},
    ]}
    by_id = {s["id"]: s for s in compose_graph(_base(), [ov])["steps"]}
    assert by_id["b"]["config"]["extra_templates"] == ["a.md", "b.md"]


def test_add_tools_grants_tools_to_step_config():
    ov = {"name": "h", "overlay": [
        {"add_tools": "@post_b", "tools": ["gen_image_asset", "gen_audio_asset"]},
    ]}
    by_id = {s["id"]: s for s in compose_graph(_base(), [ov])["steps"]}
    assert by_id["b"]["config"]["extra_tools"] == ["gen_image_asset", "gen_audio_asset"]


def test_add_tools_is_idempotent_and_stacks():
    ov = {"name": "h", "overlay": [
        {"add_tools": "b", "tools": ["a"]},
        {"add_tools": "b", "tools": ["a", "c"]},
    ]}
    by_id = {s["id"]: s for s in compose_graph(_base(), [ov])["steps"]}
    assert by_id["b"]["config"]["extra_tools"] == ["a", "c"]


def test_add_tools_rejects_unknown_target():
    ov = {"name": "h", "overlay": [{"add_tools": "nope", "tools": ["a"]}]}
    with pytest.raises(ComposeError, match="add_tools target"):
        compose_graph(_base(), [ov])


def test_add_tools_rejects_non_list_tools():
    # A bare string is the easy mistake, and it would silently grant one tool per
    # CHARACTER if it were merely iterated.
    ov = {"name": "h", "overlay": [{"add_tools": "b", "tools": "gen_image_asset"}]}
    with pytest.raises(ComposeError, match="list of tool names"):
        compose_graph(_base(), [ov])


def test_add_tools_coexists_with_add_template_on_one_step():
    # The pairing an addon actually uses: domain guidance plus the tools to act
    # on it, both riding on the same step's opaque config.
    ov = {"name": "h", "overlay": [
        {"add_template": "b", "fragment": "addons/game/implementer.md"},
        {"add_tools": "b", "tools": ["gen_image_asset"]},
    ]}
    cfg = {s["id"]: s for s in compose_graph(_base(), [ov])["steps"]}["b"]["config"]
    assert cfg["extra_templates"] == ["addons/game/implementer.md"]
    assert cfg["extra_tools"] == ["gen_image_asset"]


def test_multiple_addons_compose_in_order():
    a1 = {"name": "a1", "overlay": [{"insert_after": "b",
          "steps": [{"id": "x", "step_type": "tool"}]}]}
    a2 = {"name": "a2", "overlay": [{"insert_after": "b",
          "steps": [{"id": "y", "step_type": "tool"}]}]}
    by_id = {s["id"]: s for s in compose_graph(_base(), [a1, a2])["steps"]}
    # a1 first: b->x->c ; then a2 inserts after b: b->y->x->c
    assert by_id["b"]["transitions"] == [{"to": "y"}]
    assert by_id["y"]["transitions"] == [{"to": "x"}]
    assert by_id["x"]["transitions"] == [{"to": "c"}]


def test_unknown_anchor_raises():
    with pytest.raises(ComposeError, match="unknown anchor"):
        compose_graph(_base(), [_harness_overlay(anchor="@nope")])


def test_id_collision_raises():
    ov = {"name": "h", "overlay": [{"insert_after": "b", "steps": [{"id": "a", "step_type": "tool"}]}]}
    with pytest.raises(ComposeError, match="collides"):
        compose_graph(_base(), [ov])


def test_inputs_not_mutated():
    base = _base()
    import copy
    snapshot = copy.deepcopy(base)
    compose_graph(base, [_harness_overlay()])
    assert base == snapshot


def test_composed_graph_passes_validation():
    # The whole point: the merged graph is a valid PipelineGraph (reachability,
    # cycle-safety) — the injected node is reachable and terminates.
    merged = compose_graph(_base(), [_harness_overlay()])
    g = PipelineGraph._from_dict(merged)
    from skillflow.graph import GraphResolver
    GraphResolver(g).validate()  # raises if invalid
    assert any(s.id == "compile" for s in g.steps)


def test_add_tools_grant_is_offered_and_executable(tmp_path):
    """An `add_tools` grant has to survive BOTH gates, not just the first.

    Claim time merges `config.extra_tools` into the schemas the agent is SHOWN.
    The execution allowlist in `_execute_tool_impl` was built from the agent
    config, the write-tool schemas and the context read tools only — it never
    read `extra_tools` — so the agent was offered the tool and then told
    `Tool 'X' not allowed` when it called it. Offered-then-denied is worse than
    never offered: it burns the turn and reads to the model as a broken
    environment.

    The older tests here stop at `step_config["extra_tools"]`, i.e. at "was it
    granted" — which is exactly why the second half stayed broken.
    """
    from skillflow import SkillFlow
    from tests.mocks import MockToolLoader

    tools = MockToolLoader()
    tools.register("gen_image_asset", lambda **kw: {"generated": True},
                   schema={"name": "gen_image_asset", "description": "d",
                           "parameters": {}})
    tools.register("read_file", lambda **kw: {"content": ""},
                   schema={"name": "read_file", "description": "d",
                           "parameters": {}})
    sf = SkillFlow(str(tmp_path / "t.db"), tool_loader=tools)
    sf.register_agent_config("builder", model="mock", tools=["read_file"])

    base = {"name": "grant", "begin": "a",
            "steps": [{"id": "a", "step_type": "agent", "agent_config": "builder",
                       "transitions": [{"to": None}]}]}
    merged = compose_graph(base, [{"name": "h", "overlay": [
        {"add_tools": "a", "tools": ["gen_image_asset"]}]}])
    sf.register_graph(PipelineGraph._from_dict(merged))

    run_id = sf.create_run("grant")
    sf.start_run(run_id)
    sf.advance_run(run_id)
    claimed = sf.claim_next_step(run_id)

    # Half one: the agent is shown the tool.
    assert "gen_image_asset" in claimed.inputs.get("_tool_schemas", {})

    # Half two: and calling it is not refused.
    res = sf.execute_tool("gen_image_asset", {}, run_id=run_id, step_id="a")
    assert "not allowed" not in str(res.get("error", "")), res
    assert res.get("generated") is True, res


def test_addon_capabilities_union_with_the_base():
    """An addon adds to the base's offer list; it never replaces it."""
    from skillflow.compose import compose_graph
    base = {"name": "b", "begin": "a", "steps": [{"id": "a", "step_type": "agent"}],
            "capabilities": ["stateful"]}
    out = compose_graph(base, [{"name": "ov", "capabilities": ["game_assets"],
                                "overlay": []}])
    assert out["capabilities"] == ["game_assets", "stateful"]
