"""`to_dict()` must lose nothing, because pinning executes its output.

A run pinned to a version rebuilds its resolver from the STORED JSON
(`_get_resolver` → `PipelineGraph._from_dict(json.loads(yaml_text))`), and that
JSON is `to_dict()`'s. So any field `_from_dict` reads and `to_dict` forgets to
emit is silently dropped from what a pinned run executes — and the digest that
decides whether a version is new is computed over the same dict, so the loss is
invisible from both directions.

That was not hypothetical: `output_fixed`, `output_allow_full_write` and
`output_carry_forward` were nested under `if s.output_mode`, so a step
declaring `output: {fixed: …}` with no `mode:` round-tripped to a step
declaring nothing. It was found by reading, not by a test, and every existing
round-trip test checks exactly one field (`test_carry_forward.py`,
`test_capability_registry.py`, `test_per_item_loop.py`).

This one sets every field of every dataclass to a NON-DEFAULT and compares the
whole object, so the next field added to `_from_dict` and forgotten in
`to_dict` fails here instead of in a pinned run.
"""

import dataclasses

import pytest

from skillflow.core import graph_digest
from skillflow.graph import (
    PipelineGraph, StepNode, Transition, EndCondition, EndConditions, LoopConfig,
)


def _loaded_graph() -> PipelineGraph:
    """One graph carrying a non-default value in every field we can set."""
    return PipelineGraph(
        name="g",
        description="a description",
        begin="a",
        capabilities=["stateful"],
        anchors={"after_plan": "a"},
        steps=[
            StepNode(
                id="a", name="Step A", step_type="agent",
                agent_config="host", capability="stateful",
                max_retries=7, timeout_seconds=123, max_tool_turns=9,
                checkpoint=True, checkpoint_label="Look", checkpoint_reject_to="a",
                context=[{"step": "a", "mode": "both"}],
                config={"extra_templates": ["x.md"]},
                output_mode="fixed",
                output_fixed={"design": "d.md"},
                output_allow_full_write=True,
                output_carry_forward=True,
                output_schema={"type": "object"},
                output_schema_retries=2,
                validation=[{"tool": "file_exists", "files": ["d.md"]}],
                lifecycle={"on_deliver": [{"tool": "repo_apply"}]},
                notify=["done"],
                loop=LoopConfig(source={"step": "a", "file": "m.json",
                                        "field": "order"},
                                item_as="task", max_iterations=4),
                transitions=[Transition(to="done", match={"value": True},
                                        max_loop=3, label="ok", feedback=True)],
            ),
            StepNode(id="done", step_type="gate", transitions=[]),
        ],
        end_conditions=EndConditions(
            combinator="or",
            conditions=[EndCondition(type="node_reached", node="done",
                                     result="completed", require_completed=True,
                                     limit=5, flag={"k": "v"})]),
    )


def _fields(obj):
    return {f.name: getattr(obj, f.name) for f in dataclasses.fields(obj)}


# Each field set ALONE, on an otherwise default step.
#
# One maximal fixture is not enough, and I proved it: with `output_mode` also
# set, re-nesting the `output` block under `if s.output_mode` — the exact bug
# this file exists for — still emitted it, and the test stayed green. A field
# is dropped by a condition on some OTHER field, so each has to be exercised
# without its neighbours.
_STEP_FIELDS = {
    "name": "Step A",
    "agent_config": "host",
    "capability": "stateful",
    "max_retries": 7,
    "timeout_seconds": 123,
    "max_tool_turns": 9,
    "checkpoint": True,
    "context": [{"step": "a", "mode": "both"}],
    "config": {"extra_templates": ["x.md"]},
    "output_mode": "fixed",
    "output_fixed": {"design": "d.md"},
    "output_allow_full_write": True,
    "output_carry_forward": True,
    "output_schema": {"type": "object"},
    "output_schema_retries": 2,
    "validation": [{"tool": "file_exists", "files": ["d.md"]}],
    "lifecycle": {"on_deliver": [{"tool": "repo_apply"}]},
    "notify": ["done"],
    "tool_name": "run_tests",
    "tool_params": {"path": "tests"},
    "tool_error": "route",
}


def _graph_with(**step_kwargs) -> PipelineGraph:
    return PipelineGraph(
        name="g", begin="a",
        steps=[StepNode(id="a", step_type="agent",
                        transitions=[Transition(to="done")], **step_kwargs),
               StepNode(id="done", step_type="gate", transitions=[])],
        end_conditions=EndConditions(combinator="or", conditions=[
            EndCondition(type="node_reached", node="done", result="completed")]))


# These two ARE emitted only under `if s.checkpoint:` — the same shape as the
# `output` bug, but defensible: a step that never pauses cannot use a label or a
# reject target, so nothing reachable is lost. Paired here rather than skipped,
# so the pairing is a stated decision and not an absence.
_NEEDS_CHECKPOINT = {"checkpoint_label": "Look", "checkpoint_reject_to": "a"}


@pytest.mark.parametrize("field,value",
                         sorted({**_STEP_FIELDS, **_NEEDS_CHECKPOINT}.items()))
def test_a_step_field_set_alone_survives_the_round_trip(field, value):
    kwargs = {field: value}
    if field in _NEEDS_CHECKPOINT:
        kwargs["checkpoint"] = True
    g = _graph_with(**kwargs)
    back = PipelineGraph._from_dict(g.to_dict())

    # Against the CONSTRUCTED object, not the raw input: `__post_init__`
    # normalises `context`, so comparing to what was passed in fails on a
    # transform that is not a loss.
    assert getattr(back.steps[0], field) == getattr(g.steps[0], field), (
        f"to_dict() dropped `{field}` when it was the only field set. A pinned "
        f"run rebuilds its resolver from that dict, so it would execute the "
        f"lossy copy — and the digest is computed over the same dict, so "
        f"nothing else would notice.")


def test_the_loaded_graph_survives_as_a_whole():
    """The per-field cases cannot see an interaction; this can."""
    g = _loaded_graph()
    back = PipelineGraph._from_dict(g.to_dict())

    before, after = _fields(g.steps[0]), _fields(back.steps[0])
    lost = {k: (before[k], after[k]) for k in before
            if before[k] != after[k] and k not in ("transitions", "loop")}
    assert not lost, f"to_dict() dropped or changed: {lost}"


def test_no_transition_loop_or_end_condition_field_is_dropped():
    g = _loaded_graph()
    back = PipelineGraph._from_dict(g.to_dict())

    for label, a, b in (
        ("Transition", g.steps[0].transitions[0], back.steps[0].transitions[0]),
        ("LoopConfig", g.steps[0].loop, back.steps[0].loop),
        ("EndCondition", g.end_conditions.conditions[0],
         back.end_conditions.conditions[0]),
        ("EndConditions", g.end_conditions, back.end_conditions),
    ):
        before, after = _fields(a), _fields(b)
        lost = {k: (before[k], after[k]) for k in before
                if before[k] != after[k] and k != "conditions"}
        assert not lost, f"to_dict() dropped {label} fields: {lost}"


def test_no_graph_level_field_is_dropped():
    g = _loaded_graph()
    back = PipelineGraph._from_dict(g.to_dict())

    for k in ("name", "description", "begin", "capabilities", "anchors"):
        assert getattr(g, k) == getattr(back, k), f"to_dict() dropped {k}"


def test_the_round_trip_reaches_a_fixpoint():
    """Not just "nothing lost" but "stable": `register_graph` mints a version
    when the digest changes, so a dict that keeps drifting on re-serialisation
    would mint one on every boot — the defect versioning replaced."""
    g = _loaded_graph()
    once = PipelineGraph._from_dict(g.to_dict()).to_dict()
    twice = PipelineGraph._from_dict(once).to_dict()

    assert graph_digest(once) == graph_digest(twice), \
        "re-serialising a stored graph changes its digest"


@pytest.mark.parametrize("cls", [StepNode, Transition, LoopConfig,
                                 EndCondition, EndConditions, PipelineGraph])
def test_every_field_of_every_dataclass_is_exercised(cls):
    """The property tests above are only as good as the fixture. If a field is
    added to one of these dataclasses, this fails until the fixture sets it —
    which is what keeps the round-trip check exhaustive rather than merely
    old."""
    known = {
        StepNode: {"id", "name", "step_type", "loop", "transitions", "checkpoint",
                   "checkpoint_label", "checkpoint_reject_to", "config",
                   "max_retries", "timeout_seconds", "max_tool_turns",
                   "output_schema", "output_schema_retries", "tool_name",
                   "tool_params", "tool_error", "agent_config", "capability",
                   "context", "output_mode", "output_fixed",
                   "output_allow_full_write", "output_carry_forward",
                   "validation", "notify", "lifecycle"},
        Transition: {"to", "match", "max_loop", "label", "feedback"},
        LoopConfig: {"source", "item_as", "max_iterations"},
        EndCondition: {"type", "node", "result", "require_completed", "limit",
                       "flag"},
        EndConditions: {"combinator", "conditions"},
        PipelineGraph: {"name", "description", "begin", "steps",
                        "end_conditions", "capabilities", "anchors"},
    }[cls]
    actual = {f.name for f in dataclasses.fields(cls)}
    assert actual == known, (
        f"{cls.__name__} gained or lost fields: {actual ^ known}. Set any new "
        f"one in `_loaded_graph()` and add it here, or the round-trip tests "
        f"silently stop covering it.")
