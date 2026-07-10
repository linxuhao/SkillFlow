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
