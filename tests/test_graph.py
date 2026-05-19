"""Unit tests for graph.py — PipelineGraph, GraphResolver, and YAML parsing."""

import json
import tempfile
from pathlib import Path

import pytest

from stepflow.graph import (
    PipelineGraph,
    StepNode,
    Transition,
    EndCondition,
    EndConditions,
    EndResult,
    GraphResolver,
    _flags_match,
)
from stepflow.exceptions import CycleLimitExceeded, GraphValidationError


# ── Helpers ──────────────────────────────────────────────────────────

def _make_node(
    id: str,
    step_type: str = "agent",
    transitions: list[Transition] | None = None,
    checkpoint: bool = False,
    checkpoint_label: str = "",
    max_retries: int = 3,
    config: dict | None = None,
    output_schema: str | None = None,
    output_schema_retries: int = 0,
) -> StepNode:
    return StepNode(
        id=id,
        step_type=step_type,
        transitions=transitions or [],
        checkpoint=checkpoint,
        checkpoint_label=checkpoint_label,
        max_retries=max_retries,
        config=config or {},
        output_schema=output_schema,
        output_schema_retries=output_schema_retries,
    )


def _gate(id: str, transitions: list[Transition]) -> StepNode:
    return StepNode(id=id, step_type="gate", transitions=transitions)


def _trans(to: str, match: dict | None = None, max_loop: int | None = None) -> Transition:
    return Transition(to=to, match=match, max_loop=max_loop)


# ── Transition matching ──────────────────────────────────────────────

def test_flags_match_exact():
    assert _flags_match({"a": 1}, {"a": 1, "b": 2}) is True


def test_flags_match_subset():
    assert _flags_match({"a": 1}, {"a": 1}) is True


def test_flags_match_false_value():
    assert _flags_match({"a": False}, {"a": False, "b": True}) is True


def test_flags_match_missing_key():
    assert _flags_match({"a": 1}, {"b": 2}) is False


def test_flags_match_wrong_value():
    assert _flags_match({"a": 1}, {"a": 2}) is False


def test_flags_match_none():
    assert _flags_match({}, {"a": 1}) is True


# ── StepNode validation ──────────────────────────────────────────────

def test_step_node_invalid_type():
    with pytest.raises(ValueError, match="step_type"):
        StepNode(id="x", step_type="invalid")


def test_step_node_defaults():
    node = StepNode(id="test")
    assert node.step_type == "agent"
    assert node.max_retries == 3
    assert node.transitions == []
    assert node.checkpoint is False
    assert node.config == {}


# ── GraphResolver: basic ─────────────────────────────────────────────

def test_resolver_begin_node():
    graph = PipelineGraph(
        name="test", begin="a",
        steps=[_make_node("a"), _make_node("b")],
    )
    r = GraphResolver(graph)
    assert r.begin_node() == "a"


def test_resolver_is_gate():
    graph = PipelineGraph(
        name="test", begin="a",
        steps=[_make_node("a"), _gate("b", [_trans("a")])],
    )
    r = GraphResolver(graph)
    assert r.is_gate("a") is False
    assert r.is_gate("b") is True
    assert r.is_gate("nonexistent") is False


def test_resolver_find_error_transition():
    node = _make_node("a", transitions=[
        _trans("b"),
        _trans("error_handler", match={"_error": True}),
        _trans("c"),
    ])
    graph = PipelineGraph(name="test", begin="a", steps=[node, _make_node("b"), _make_node("c"), _make_node("error_handler")])
    r = GraphResolver(graph)
    assert r.find_error_transition("a") == "error_handler"
    assert r.find_error_transition("b") is None
    assert r.find_error_transition("nonexistent") is None


# ── GraphResolver: transition resolution ─────────────────────────────

def test_next_node_first_match():
    node = _make_node("a", transitions=[
        _trans("b", match={"x": True}),
        _trans("c"),
    ])
    graph = PipelineGraph(name="test", begin="a", steps=[node, _make_node("b"), _make_node("c")])
    r = GraphResolver(graph)
    assert r.next_node("a", {"x": True}, {}) == "b"


def test_next_node_fallthrough():
    node = _make_node("a", transitions=[
        _trans("b", match={"x": True}),
        _trans("c"),
    ])
    graph = PipelineGraph(name="test", begin="a", steps=[node, _make_node("b"), _make_node("c")])
    r = GraphResolver(graph)
    # x is False, so first transition doesn't match; second has no match → matches
    assert r.next_node("a", {"x": False}, {}) == "c"


def test_next_node_fallback_match():
    node = _make_node("a", transitions=[
        _trans("b", match={"x": True}),
        _trans("c", match=None),  # Always matches
    ])
    graph = PipelineGraph(name="test", begin="a", steps=[node, _make_node("b"), _make_node("c")])
    r = GraphResolver(graph)
    assert r.next_node("a", {"y": True}, {}) == "c"


def test_next_node_no_match():
    node = _make_node("a", transitions=[
        _trans("b", match={"x": True}),
    ])
    graph = PipelineGraph(name="test", begin="a", steps=[node, _make_node("b")])
    r = GraphResolver(graph)
    assert r.next_node("a", {"x": False}, {}) is None


def test_next_node_max_loop_enforced():
    node = _make_node("a", transitions=[
        _trans("b", max_loop=3),
        _trans("c"),
    ])
    graph = PipelineGraph(name="test", begin="a", steps=[node, _make_node("b"), _make_node("c")])
    r = GraphResolver(graph)
    # Edge count at limit → first transition blocked
    assert r.next_node("a", {}, {("a", "b"): 3}) == "c"


def test_next_node_cycle_limit_exceeded():
    node = _make_node("a", transitions=[
        _trans("b", max_loop=1),
    ])
    graph = PipelineGraph(name="test", begin="a", steps=[node, _make_node("b")])
    r = GraphResolver(graph)
    with pytest.raises(CycleLimitExceeded, match="max_loop=1 reached"):
        r.next_node("a", {}, {("a", "b"): 1})


def test_next_node_nonexistent():
    graph = PipelineGraph(name="test", begin="a", steps=[_make_node("a")])
    r = GraphResolver(graph)
    assert r.next_node("nonexistent", {}, {}) is None


# ── GraphResolver: gate resolution ───────────────────────────────────

def test_resolve_gate_transitions_match():
    gate = _gate("g", [_trans("a", match={"go": True}), _trans("b")])
    graph = PipelineGraph(name="test", begin="g", steps=[gate, _make_node("a"), _make_node("b")])
    r = GraphResolver(graph)
    assert r.resolve_gate_transitions("g", {"go": True}, {}) == "a"
    assert r.resolve_gate_transitions("g", {"go": False}, {}) == "b"


# ── GraphResolver: validation ────────────────────────────────────────

def test_validate_valid_graph():
    graph = PipelineGraph(
        name="valid", begin="a",
        steps=[
            _make_node("a", transitions=[_trans("b")]),
            _make_node("b", transitions=[_trans("c")]),
            _make_node("c"),
        ],
    )
    issues = graph.validate()
    assert issues == []


def test_validate_missing_begin():
    graph = PipelineGraph(name="test", begin="nonexistent", steps=[_make_node("a")])
    issues = graph.validate()
    assert any("Begin node" in i for i in issues)


def test_validate_missing_name():
    graph = PipelineGraph(name="", begin="a", steps=[_make_node("a")])
    issues = graph.validate()
    assert any("name" in i.lower() for i in issues)


def test_validate_no_steps():
    graph = PipelineGraph(name="test", begin="a")
    issues = graph.validate()
    assert len(issues) > 0


def test_validate_duplicate_step_ids():
    graph = PipelineGraph(name="test", begin="a", steps=[_make_node("a"), _make_node("a")])
    issues = graph.validate()
    assert any("Duplicate" in i for i in issues)


def test_validate_missing_transition_target():
    graph = PipelineGraph(
        name="test", begin="a",
        steps=[_make_node("a", transitions=[_trans("nonexistent")])],
    )
    issues = graph.validate()
    assert any("nonexistent" in i for i in issues)


def test_validate_unreachable_node():
    graph = PipelineGraph(
        name="test", begin="a",
        steps=[
            _make_node("a", transitions=[_trans("b")]),
            _make_node("b"),
            _make_node("orphan"),  # No incoming edges
        ],
    )
    issues = graph.validate()
    assert any("orphan" in i and "unreachable" in i.lower() for i in issues)


def test_validate_cycle_with_safety():
    graph = PipelineGraph(
        name="test", begin="a",
        steps=[
            _make_node("a", transitions=[_trans("b")]),
            _make_node("b", transitions=[_trans("a", max_loop=5)]),
        ],
    )
    issues = graph.validate()
    assert issues == []


def test_validate_cycle_without_safety():
    graph = PipelineGraph(
        name="test", begin="a",
        steps=[
            _make_node("a", transitions=[_trans("b")]),
            _make_node("b", transitions=[_trans("a")]),  # No max_loop
        ],
    )
    issues = graph.validate()
    assert any("max_loop" in i.lower() for i in issues)


def test_validate_self_loop():
    graph = PipelineGraph(
        name="test", begin="a",
        steps=[_make_node("a", transitions=[_trans("a", max_loop=10)])],
    )
    issues = graph.validate()
    assert issues == []


def test_validate_self_loop_no_safety():
    graph = PipelineGraph(
        name="test", begin="a",
        steps=[_make_node("a", transitions=[_trans("a")])],  # No max_loop
    )
    issues = graph.validate()
    assert any("max_loop" in i.lower() for i in issues)


# ── EndConditions ────────────────────────────────────────────────────

def test_end_conditions_empty():
    ec = EndConditions()
    assert ec.combinator == "or"
    assert ec.conditions == []


def test_end_condition_fields():
    cond = EndCondition(type="node_reached", node="5", result="completed")
    assert cond.type == "node_reached"
    assert cond.node == "5"
    assert cond.result == "completed"
    assert cond.limit == 0
    assert cond.flag == {}


def test_end_condition_flag_match():
    cond = EndCondition(type="flag_match", flag={"fatal_error": True})
    assert cond.flag == {"fatal_error": True}


# ── YAML round-trip ──────────────────────────────────────────────────

def test_to_dict_and_back():
    graph = PipelineGraph(
        name="test_pipeline",
        description="A test",
        begin="start",
        steps=[
            _make_node("start", transitions=[_trans("middle")]),
            _make_node("middle", transitions=[_trans("end")]),
            _make_node("end"),
        ],
        end_conditions=EndConditions(
            combinator="or",
            conditions=[EndCondition(type="node_reached", node="end", result="completed")],
        ),
    )
    d = graph.to_dict()
    graph2 = PipelineGraph._from_dict(d)
    assert graph2.name == graph.name
    assert graph2.begin == graph.begin
    assert len(graph2.steps) == 3
    assert graph2.end_conditions is not None
    assert graph2.end_conditions.combinator == "or"

# ── v2 extended tests ──

class TestStepNodeV2:
    def test_parses_tool_type(self):
        data = {"name": "t", "begin": "t1", "steps": [
            {"id": "t1", "step_type": "tool", "tool_name": "repo_apply",
             "tool_params": {"source_dir": "$STEP_DRAFT_DIR"},
             "transitions": [{"to": None, "match": {"applied": True}}]}
        ]}
        from stepflow.graph import PipelineGraph
        g = PipelineGraph._from_dict(data)
        n = g.steps[0]
        assert n.step_type == "tool"
        assert n.tool_name == "repo_apply"
        assert n.tool_params == {"source_dir": "$STEP_DRAFT_DIR"}

    def test_parses_agent_config_and_context(self):
        data = {"name": "t", "begin": "s1", "steps": [{
            "id": "s1", "agent_config": "researcher",
            "context": [{"source": {"config": "meta_conversation", "output": "brief.md"}}],
            "output_mode": "content",
            "output": {"fixed": {"sota": "step1_5_sota.md"}},
            "validation": [{"files": ["*.json"], "tool": "json_schema"}],
        }]}
        from stepflow.graph import PipelineGraph
        g = PipelineGraph._from_dict(data)
        n = g.steps[0]
        assert n.agent_config == "researcher"
        assert len(n.context) == 1
        assert n.output_mode == "content"
        assert n.output_fixed == {"sota": "step1_5_sota.md"}
        assert len(n.validation) == 1

    def test_to_dict_roundtrip_v2_fields(self):
        from stepflow.graph import PipelineGraph, StepNode, Transition
        g = PipelineGraph(
            name="test", begin="s1",
            steps=[StepNode(
                id="s1", step_type="agent", agent_config="pm",
                output_mode="content", output_fixed={"m": "manifest.json"},
                context=[{"source": {"config": "meta", "output": "brief.md"}}],
                validation=[{"files": ["*.json"], "tool": "json_schema"}],
                transitions=[Transition(to="s2", match={"passed": True}, feedback=True)]
            ), StepNode(id="s2")]
        )
        d = g.to_dict()
        g2 = PipelineGraph._from_dict(d)
        assert g2.steps[0].agent_config == "pm"
        assert g2.steps[0].output_fixed == {"m": "manifest.json"}
        assert g2.steps[0].transitions[0].feedback is True

    def test_transition_to_none_serialization(self):
        from stepflow.graph import PipelineGraph, StepNode, Transition
        g = PipelineGraph(name="t", begin="s1", steps=[
            StepNode(id="s1", transitions=[Transition(to=None)])
        ])
        d = g.to_dict()
        assert d["steps"][0]["transitions"][0]["to"] is None

    def test_feedback_default_false(self):
        from stepflow.graph import Transition
        t = Transition(to="x")
        assert t.feedback is False

    def test_tool_node_defaults(self):
        from stepflow.graph import StepNode
        n = StepNode(id="t", step_type="tool", tool_name="repo_apply")
        assert n.tool_name == "repo_apply"
        assert n.tool_params == {}
        assert n.agent_config == ""


class TestGraphResolverV2:
    def test_is_tool(self):
        from stepflow.graph import PipelineGraph, StepNode, GraphResolver
        g = PipelineGraph(name="t", begin="s1", steps=[
            StepNode(id="s1", step_type="tool", tool_name="echo")
        ])
        r = GraphResolver(g)
        assert r.is_tool("s1") is True
        assert r.is_agent("s1") is False

    def test_is_agent(self):
        from stepflow.graph import PipelineGraph, StepNode, GraphResolver
        g = PipelineGraph(name="t", begin="s1", steps=[
            StepNode(id="s1", step_type="agent")
        ])
        r = GraphResolver(g)
        assert r.is_agent("s1") is True
        assert r.is_tool("s1") is False

    def test_resolve_transition_with_checkpoint_approved(self):
        from stepflow.graph import PipelineGraph, StepNode, Transition, GraphResolver
        g = PipelineGraph(name="t", begin="s1", steps=[
            StepNode(id="s1", transitions=[
                Transition(to="s2", match={"from": "checkpoint", "value": "approved"})
            ]),
            StepNode(id="s2"),
        ])
        r = GraphResolver(g)
        t, target = r.resolve_transition("s1", {}, {}, checkpoint_approved=True)
        assert target == "s2"

    def test_resolve_transition_checkpoint_rejected_no_match(self):
        from stepflow.graph import PipelineGraph, StepNode, Transition, GraphResolver
        g = PipelineGraph(name="t", begin="s1", steps=[
            StepNode(id="s1", transitions=[
                Transition(to="s2", match={"from": "checkpoint", "value": "approved"})
            ]),
            StepNode(id="s2"),
        ])
        r = GraphResolver(g)
        t, target = r.resolve_transition("s1", {}, {}, checkpoint_approved=False)
        assert target is None

    def test_flags_match_from_checkpoint_approved(self):
        from stepflow.graph import _flags_match
        assert _flags_match({"from": "checkpoint", "value": "approved"},
                            {"_checkpoint_approved": True}) is True
        assert _flags_match({"from": "checkpoint", "value": "approved"},
                            {"_checkpoint_approved": False}) is False

    def test_validate_allows_terminal_to_none(self):
        from stepflow.graph import PipelineGraph, StepNode, Transition
        g = PipelineGraph(name="t", begin="s1", steps=[
            StepNode(id="s1", transitions=[Transition(to=None)])
        ])
        issues = g.validate()
        assert "transition to 'None'" not in str(issues)

    def test_next_node_backward_compat_still_works(self):
        from stepflow.graph import PipelineGraph, StepNode, Transition, GraphResolver
        g = PipelineGraph(name="t", begin="s1", steps=[
            StepNode(id="s1", transitions=[Transition(to="s2", match={"done": True})]),
            StepNode(id="s2"),
        ])
        r = GraphResolver(g)
        assert r.next_node("s1", {"done": True}, {}) == "s2"


class TestNotifyField:
    def test_notify_field_parsed_from_yaml(self):
        from stepflow.graph import PipelineGraph
        data = {"name": "t", "begin": "s1", "steps": [{
            "id": "s1",
            "notify": ["step_started", "agent_response"],
        }]}
        g = PipelineGraph._from_dict(data)
        assert g.steps[0].notify == ["step_started", "agent_response"]

    def test_notify_field_none_by_default(self):
        from stepflow.graph import PipelineGraph
        data = {"name": "t", "begin": "s1", "steps": [{"id": "s1"}]}
        g = PipelineGraph._from_dict(data)
        assert g.steps[0].notify is None

    def test_notify_field_roundtrip(self):
        from stepflow.graph import PipelineGraph, StepNode
        g = PipelineGraph(name="t", begin="s1", steps=[
            StepNode(id="s1", notify=["*"]),
        ])
        d = g.to_dict()
        assert d["steps"][0]["notify"] == ["*"]
        g2 = PipelineGraph._from_dict(d)
        assert g2.steps[0].notify == ["*"]

    def test_notify_field_none_not_serialized(self):
        from stepflow.graph import PipelineGraph, StepNode
        g = PipelineGraph(name="t", begin="s1", steps=[
            StepNode(id="s1"),  # notify=None by default
        ])
        d = g.to_dict()
        assert "notify" not in d["steps"][0]

    def test_from_file_match_resolves_correctly(self):
        """from_file reads the output file and checks a field."""
        import json, tempfile, os
        from stepflow.graph import _flags_match

        d = tempfile.mkdtemp()
        verdict_path = os.path.join(d, "review_verdict.json")
        with open(verdict_path, "w") as f:
            json.dump({"passed": True, "feedback": "ok"}, f)

        def reader(path):
            return open(os.path.join(d, path)).read()

        match = {"from_file": "review_verdict.json", "field": "passed", "value": True}
        assert _flags_match(match, {}, file_reader=reader) is True

        match_false = {"from_file": "review_verdict.json", "field": "passed", "value": False}
        assert _flags_match(match_false, {}, file_reader=reader) is False

    def test_from_file_match_missing_file_returns_false(self):
        from stepflow.graph import _flags_match
        match = {"from_file": "nonexistent.json", "field": "passed", "value": True}
        assert _flags_match(match, {}, file_reader=lambda p: (_ for _ in ()).throw(FileNotFoundError())) is False

    def test_from_file_match_no_reader_returns_false(self):
        from stepflow.graph import _flags_match
        match = {"from_file": "x.json", "field": "passed", "value": True}
        assert _flags_match(match, {}) is False
