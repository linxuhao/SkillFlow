"""Test fixtures for stepflow.

Provides isolated in-memory SQLite databases, mock StepRunners,
mock ToolLoaders, and helper factories for building PipelineGraphs.
"""

import pytest

from stepflow.core import StepFlow
from mocks import (
    MockStepRunner,
    MockToolLoader,
    STANDARD_MOCK_TOOLS,
    create_standard_mock_tools,
)


@pytest.fixture
def sf():
    """StepFlow instance backed by an in-memory SQLite database.

    Each test gets a fresh, isolated instance. All stepflow_* tables
    are created automatically.
    """
    return StepFlow(":memory:")


@pytest.fixture
def sf_tmp(tmp_path):
    """StepFlow instance backed by a file-based SQLite database.

    Use when you need to simulate process crashes by re-opening the
    database from the same file path.
    """
    db_path = str(tmp_path / "test.db")
    return StepFlow(db_path)


@pytest.fixture
def mock_tools():
    """Pre-built MockToolLoader with all standard validation/lifecycle tools."""
    return create_standard_mock_tools()


@pytest.fixture
def sf_with_tools(mock_tools):
    """StepFlow with mock ToolLoader, no workspace.

    Use for graph traversal, checkpoints, gates, error routing tests.
    """
    return StepFlow(":memory:", tool_loader=mock_tools)


@pytest.fixture
def sf_with_workspace(tmp_path, mock_tools):
    """StepFlow with mock ToolLoader + tmp_path workspace.

    Use for lifecycle hook and output validation tests that need
    filesystem directories for step outputs.
    """
    return StepFlow(
        ":memory:",
        tool_loader=mock_tools,
        workspace_base=str(tmp_path / "workspaces"),
        projects_base=str(tmp_path / "projects"),
    )


# ── Agent config names used in dpe_full.yaml ──────────────────────

DPE_AGENT_CONFIGS = [
    "researcher",
    "researcher_reviewer",
    "architect",
    "architect_reviewer",
    "pm",
    "pm_reviewer",
    "task_planner",
    "task_planner_reviewer",
    "task_implementer",
    "task_implementer_reviewer",
    "task_verifier",
    "task_verifier_reviewer",
    "final_verifier",
    "final_verifier_reviewer",
]


def register_dpe_agent_configs(sf: StepFlow, tools: list[str] | None = None):
    """Register all agent configs referenced by dpe_full.yaml."""
    tool_list = tools or ["file_exists", "json_schema", "syntax_lint",
                          "py_compile", "pytest", "repo_apply", "dir_tree"]
    for name in DPE_AGENT_CONFIGS:
        sf.register_agent_config(name, model="mock", tools=tool_list)
