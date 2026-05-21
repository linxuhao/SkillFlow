"""SkillFlow config linter — validates pipeline YAML files.

Provides both a programmatic API and a skillflow-compatible tool function
so the linter can be used standalone (CLI) or inside a converter pipeline
(as a tool node).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from skillflow.graph import PipelineGraph, GraphValidationError


@dataclass
class LintIssue:
    """A single lint finding with location and fix suggestion."""

    severity: str       # "error" | "warning"
    message: str        # human-readable description
    location: str = ""  # e.g. "steps[3]" or "transitions[0].to"
    suggestion: str = ""


def _parse_yaml_issues(exc: Exception, path: str) -> list[LintIssue]:
    """Extract structured issues from YAML parse / graph validation errors."""
    issues: list[LintIssue] = []

    if isinstance(exc, GraphValidationError):
        for msg in exc.issues:
            issue = LintIssue(severity="error", message=msg)
            # Try to extract location from the message
            if "unreachable" in msg:
                issue.suggestion = "Add a transition to this step or remove it"
            if "max_loop" in msg.lower():
                issue.suggestion = "Add max_loop: N to at least one edge in the cycle"
            if "duplicate" in msg.lower():
                issue.suggestion = "Ensure every step has a unique id"
            issues.append(issue)

    elif hasattr(exc, "__cause__") and exc.__cause__ is not None:
        # PyYAML parse error
        cause = exc.__cause__
        issues.append(LintIssue(
            severity="error",
            message=f"YAML parse error: {cause}",
            suggestion="Check YAML syntax — ensure proper indentation and quoting",
        ))

    else:
        issues.append(LintIssue(
            severity="error",
            message=str(exc),
            suggestion="",
        ))

    return issues


def lint_config(path: str | Path) -> list[LintIssue]:
    """Validate a skillflow YAML file.

    Args:
        path: Path to a skillflow pipeline YAML file.

    Returns:
        List of LintIssue objects. Empty list means the config is valid.
    """
    path = Path(path)
    if not path.exists():
        return [LintIssue(
            severity="error",
            message=f"File not found: {path}",
            suggestion="Check the file path",
        )]

    try:
        graph = PipelineGraph.from_yaml(str(path))
    except Exception as exc:
        return _parse_yaml_issues(exc, str(path))

    # Run graph structural validation
    try:
        issues = graph.validate()
    except Exception as exc:
        return _parse_yaml_issues(exc, str(path))

    return [
        LintIssue(severity="error", message=msg,
                  suggestion=_suggest(msg))
        for msg in issues
    ]


def lint_content(yaml_text: str) -> list[LintIssue]:
    """Validate a raw YAML string as a skillflow pipeline config.

    Args:
        yaml_text: Raw YAML content.

    Returns:
        List of LintIssue objects.
    """
    try:
        import yaml
        data = yaml.safe_load(yaml_text)
        graph = PipelineGraph._from_dict(data)
    except Exception as exc:
        return _parse_yaml_issues(exc, "<content>")

    try:
        issues = graph.validate()
    except Exception as exc:
        return _parse_yaml_issues(exc, "<content>")

    return [
        LintIssue(severity="error", message=msg,
                  suggestion=_suggest(msg))
        for msg in issues
    ]


def _suggest(msg: str) -> str:
    """Heuristic suggestion based on validation message."""
    if "unreachable" in msg.lower():
        return "Add a transition to this step or remove it"
    if "max_loop" in msg.lower() or "no max_loop" in msg.lower():
        return "Add max_loop: N to at least one edge in the cycle"
    if "duplicate" in msg.lower():
        return "Ensure every step has a unique id"
    if "begin" in msg.lower():
        return "Add a 'begin' field with the entry step id"
    if "transition" in msg.lower():
        return "Ensure the 'to' target exists as a step id"
    return ""


def skillflow_lint(**kwargs) -> dict:
    """SkillFlow tool function — validates a pipeline YAML file.

    Accepts keyword arguments (standard skillflow tool convention).
    Can be loaded via ToolLoader as tool_name="skillflow_lint".

    Parameters (from tool schema):
        path: Path to the YAML file to validate.
        content: Raw YAML string (alternative to path).

    Returns:
        {"passed": bool, "errors": int, "warnings": int, "issues": [...]}
    """
    path = kwargs.get("path", "")
    content = kwargs.get("content", "")

    if path:
        issues = lint_config(path)
    elif content:
        issues = lint_content(content)
    else:
        return {
            "passed": False,
            "errors": 1,
            "warnings": 0,
            "issues": [{
                "severity": "error",
                "message": "Neither 'path' nor 'content' provided",
                "location": "",
                "suggestion": "Provide 'path' (file path) or 'content' (YAML string)",
            }],
        }

    errors = [i for i in issues if i.severity == "error"]
    warnings = [i for i in issues if i.severity == "warning"]

    return {
        "passed": len(errors) == 0,
        "errors": len(errors),
        "warnings": len(warnings),
        "issues": [
            {
                "severity": i.severity,
                "message": i.message,
                "location": i.location,
                "suggestion": i.suggestion,
            }
            for i in issues
        ],
    }
