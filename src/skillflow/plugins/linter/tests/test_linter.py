"""Regression tests for the linter plugin (skillflow-lint).

The linter is not agent-driven, so there's no agent to mock — instead we
assert structural validation directly: a well-formed graph lints clean, and
common authoring mistakes are flagged as errors. Guards against regressions in
lint_config / lint_content and the underlying graph.validate().
"""

from pathlib import Path

from skillflow.plugins.linter import lint_config, lint_content, LintIssue


_VALID = """
name: lint_ok
begin: analyze
end_conditions:
  combinator: or
  conditions:
    - type: node_reached
      node: done
      result: "completed"
steps:
  - id: analyze
    step_type: agent
    agent_config: analyst
    transitions: [{to: done}]
  - id: done
    step_type: agent
    agent_config: analyst
"""

# Transition points at a node that doesn't exist — a classic authoring bug.
_UNDEFINED_TARGET = """
name: lint_bad
begin: analyze
steps:
  - id: analyze
    step_type: agent
    agent_config: analyst
    transitions: [{to: nonexistent_node}]
"""


def test_valid_graph_lints_clean():
    issues = lint_content(_VALID)
    errors = [i for i in issues if i.severity == "error"]
    assert errors == [], f"expected no errors, got: {[e.message for e in errors]}"


def test_undefined_transition_target_is_flagged():
    issues = lint_content(_UNDEFINED_TARGET)
    assert any(i.severity == "error" for i in issues), \
        "expected an error for a transition to an undefined node"
    assert any("nonexistent_node" in i.message for i in issues), \
        f"expected the undefined node named in an issue, got: {[i.message for i in issues]}"


def test_lint_config_roundtrip_and_missing_file(tmp_path):
    # Valid file lints clean.
    good = tmp_path / "good.yaml"
    good.write_text(_VALID, encoding="utf-8")
    assert [i for i in lint_config(good) if i.severity == "error"] == []

    # Missing file is reported as an error (not a crash).
    issues = lint_config(tmp_path / "does_not_exist.yaml")
    assert len(issues) == 1 and issues[0].severity == "error"
    assert "not found" in issues[0].message.lower()


def test_lint_issue_shape():
    """A flagged issue carries the fields the CLI prints."""
    issue = lint_content(_UNDEFINED_TARGET)[0]
    assert isinstance(issue, LintIssue)
    assert issue.severity in ("error", "warning")
    assert isinstance(issue.message, str) and issue.message
