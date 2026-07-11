"""Tests for the `required: true` context flag (fail-loud on empty input)."""

import pytest

from skillflow.context import ContextResolver
from skillflow.exceptions import RequiredContextMissing
from skillflow.graph import _normalize_context_spec


def test_normalize_carries_required_from_wrapper_or_sibling():
    # written inside the source: wrapper
    a = _normalize_context_spec({"source": {"step": "1", "required": True}})
    assert a["required"] is True
    # written as a sibling of source:
    b = _normalize_context_spec({"source": {"step": "1"}, "required": True})
    assert b["required"] is True
    # default false
    c = _normalize_context_spec({"source": {"step": "1"}})
    assert c["required"] is False


def test_required_missing_raises(tmp_path):
    # a step source with no step dir → resolves to empty content
    r = ContextResolver(tmp_path)
    specs = [_normalize_context_spec({"source": {"step": "1", "output": "goals.json"}, "required": True})]
    with pytest.raises(RequiredContextMissing, match="cannot run without it"):
        r.resolve(specs, current_config="dpe")


def test_non_required_missing_is_silent(tmp_path):
    r = ContextResolver(tmp_path)
    specs = [_normalize_context_spec({"source": {"step": "1", "output": "goals.json"}})]
    assert r.resolve(specs, current_config="dpe") == {}  # dropped, no raise


def test_required_present_does_not_raise(tmp_path):
    # create the step output the required source points at
    (tmp_path / "dpe" / "1").mkdir(parents=True)
    (tmp_path / "dpe" / "1" / "goals.json").write_text('{"user_stories": ["x"]}')
    r = ContextResolver(tmp_path)
    specs = [_normalize_context_spec(
        {"source": {"config": "dpe", "step": "1", "output": "goals.json"}, "required": True})]
    out = r.resolve(specs, current_config="dpe")
    assert any("goals.json" in k for k in out)
