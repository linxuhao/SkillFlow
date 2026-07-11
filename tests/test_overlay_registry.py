"""Overlay registry: skillflow owns named overlays + compose/describe.

The mechanical half of "addons" — register named overlay specs, compose them
onto a base graph (whose anchors are preserved through the registry), and
decompose a composed name back to {base, addons}. The host keeps manifests,
prompt fragments, and assets.
"""

import pytest

from skillflow.core import SkillFlow
from skillflow.exceptions import SkillFlowError
from skillflow.graph import PipelineGraph


BASE = {
    "name": "base1", "begin": "a",
    "anchors": {"post_a": "a"},
    "steps": [
        {"id": "a", "step_type": "agent", "agent_config": "noop",
         "transitions": [{"to": "b"}]},
        {"id": "b", "step_type": "agent", "agent_config": "noop", "transitions": []},
    ],
}

OVERLAY = {
    "name": "ov1", "base": "base1", "alias": "base1_ov",
    "description": "adds a tool step after a",
    "overlay": [
        {"insert_after": "@post_a",
         "steps": [{"id": "injected", "step_type": "tool", "tool_name": "noop_tool"}]},
    ],
}


def _sf():
    sf = SkillFlow(":memory:")
    sf.register_agent_config("noop")
    sf.register_graph(PipelineGraph._from_dict(BASE))
    sf.register_overlay("ov1", OVERLAY)
    return sf


def test_anchors_survive_registration_roundtrip():
    g = PipelineGraph._from_dict(BASE)
    assert g.anchors == {"post_a": "a"}
    assert g.to_dict().get("anchors") == {"post_a": "a"}      # to_dict emits them
    assert PipelineGraph._from_dict(g.to_dict()).anchors == {"post_a": "a"}


def test_compose_uses_alias_and_splices_at_anchor():
    sf = _sf()
    name = sf.compose_config("base1", ["ov1"])
    assert name == "base1_ov"                                 # single overlay → alias
    g = sf._graphs["base1_ov"]
    ids = [n.id for n in g.steps]
    assert "injected" in ids
    # spliced right after the anchor: a → injected → b
    a = next(n for n in g.steps if n.id == "a")
    assert a.transitions[0].to == "injected"


def test_emergent_name_for_multi_or_aliasless():
    sf = _sf()
    sf.register_overlay("ov2", {"name": "ov2", "base": "base1", "overlay": []})
    name = sf.compose_config("base1", ["ov1", "ov2"])
    assert name == "base1__ov1+ov2"                           # sorted emergent name


def test_describe_config():
    sf = _sf()
    assert sf.describe_config("base1_ov") == {"base": "base1", "addons": ["ov1"]}
    assert sf.describe_config("base1__ov1+ov2") == {"base": "base1", "addons": ["ov1", "ov2"]}
    assert sf.describe_config("base1") == {"base": "base1", "addons": []}


def test_base_mismatch_rejected():
    sf = _sf()
    sf.register_overlay("wrong", {"name": "wrong", "base": "other_base", "overlay": []})
    with pytest.raises(SkillFlowError, match="binds to base"):
        sf.compose_config("base1", ["wrong"])


def test_unknown_base_or_overlay_rejected():
    sf = _sf()
    with pytest.raises(SkillFlowError, match="unknown base"):
        sf.compose_config("nope", ["ov1"])
    with pytest.raises(SkillFlowError, match="unknown overlay"):
        sf.compose_config("base1", ["nope"])


def test_list_overlays():
    sf = _sf()
    ov = next(o for o in sf.list_overlays() if o["name"] == "ov1")
    assert ov["base"] == "base1" and ov["alias"] == "base1_ov"
