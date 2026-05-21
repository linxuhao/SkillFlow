"""Unit tests for skillflow exceptions."""

import pytest
from skillflow.exceptions import (
    SkillFlowError,
    StepVersionConflict,
    CycleLimitExceeded,
    GraphValidationError,
    NoMatchingTransition,
    OutputValidationError,
)


def test_skillflow_error_base():
    err = SkillFlowError("base error")
    assert str(err) == "base error"
    assert isinstance(err, Exception)


def test_step_version_conflict():
    err = StepVersionConflict("version mismatch")
    assert isinstance(err, SkillFlowError)
    assert "version mismatch" in str(err)


def test_cycle_limit_exceeded():
    err = CycleLimitExceeded("max_loop=5 reached")
    assert isinstance(err, SkillFlowError)


def test_graph_validation_error():
    err = GraphValidationError(["issue 1", "issue 2"])
    assert isinstance(err, SkillFlowError)
    assert err.issues == ["issue 1", "issue 2"]
    assert "issue 1" in str(err)
    assert "issue 2" in str(err)


def test_graph_validation_error_empty():
    err = GraphValidationError([])
    assert err.issues == []


def test_no_matching_transition():
    err = NoMatchingTransition("no match")
    assert isinstance(err, SkillFlowError)


def test_output_validation_error():
    err = OutputValidationError("schema mismatch")
    assert isinstance(err, SkillFlowError)
