"""Test fixtures for skillflow.

Provides isolated in-memory SQLite databases, mock StepRunners,
mock ToolLoaders, and helper factories for building PipelineGraphs.
"""

import pytest

from skillflow.core import SkillFlow
from skillflow.identity import worker_identity
from mocks import (
    MockStepRunner,
    MockToolLoader,
    STANDARD_MOCK_TOOLS,
    create_standard_mock_tools,
)


# A `start=` value that cannot be a real one: /proc reports process start as a
# decimal tick count, so no live process ever matches this.
_OWNER_IS_GONE = "start=gone"


@pytest.fixture
def crash_the_owner():
    """Simulate the crash these tests describe, then reap what it left behind.

    Recovery is decided by ownership: a claim whose owner is observably alive is
    never reclaimed, however long it has been quiet. So a test can no longer
    fake a crash by handing the reaper ``stale_threshold_seconds=-1`` — the
    claim it planted is owned by the very pytest process doing the asking, and
    that process is manifestly running.

    Rewrite the owner to one that provably is not: our own pid, carrying a
    process-start marker that is not ours. That is exactly the recycled pid a
    container restart produces, and it is deterministic — no spawning a process
    and hoping its pid is not reused. Where liveness cannot be observed at all
    (no /proc) the identity reads as *unknown* instead, and the -1 lease still
    fires, so this reads the same on every platform.
    """
    def _crash(sf: SkillFlow, run_id: str | None = None) -> list[str]:
        gone = f"{worker_identity('worker')} {_OWNER_IS_GONE}"
        where = "status = 'claimed'"
        params: tuple = (gone,)
        if run_id is not None:
            where += " AND run_id = ?"
            params = (gone, run_id)
        with sf._lock:
            sf._conn.execute(
                f"UPDATE skillflow_steps SET claimed_by = ? WHERE {where}",
                params)
            sf._conn.commit()
        return sf.recover_stale_claims(stale_threshold_seconds=-1)
    return _crash


@pytest.fixture
def sf():
    """SkillFlow instance backed by an in-memory SQLite database.

    Each test gets a fresh, isolated instance. All skillflow_* tables
    are created automatically.
    """
    return SkillFlow(":memory:")


@pytest.fixture
def sf_tmp(tmp_path):
    """SkillFlow instance backed by a file-based SQLite database.

    Use when you need to simulate process crashes by re-opening the
    database from the same file path.
    """
    db_path = str(tmp_path / "test.db")
    return SkillFlow(db_path)


@pytest.fixture
def mock_tools():
    """Pre-built MockToolLoader with all standard validation/lifecycle tools."""
    return create_standard_mock_tools()


@pytest.fixture
def sf_with_tools(mock_tools):
    """SkillFlow with mock ToolLoader, no workspace.

    Use for graph traversal, checkpoints, gates, error routing tests.
    """
    return SkillFlow(":memory:", tool_loader=mock_tools)


@pytest.fixture
def sf_with_workspace(tmp_path, mock_tools):
    """SkillFlow with mock ToolLoader + tmp_path workspace.

    Use for lifecycle hook and output validation tests that need
    filesystem directories for step outputs.
    """
    return SkillFlow(
        ":memory:",
        tool_loader=mock_tools,
        workspace_base=str(tmp_path / "workspaces"),
        projects_base=str(tmp_path / "projects"),
    )


@pytest.fixture
def sf_with_trace_db(tmp_path, mock_tools):
    """SkillFlow with mock ToolLoader + per-project trace DB.

    Use for testing trace writes/reads against per-project trace.db files
    instead of the shared skillflow_trace table.
    """
    return SkillFlow(
        ":memory:",
        tool_loader=mock_tools,
        workspace_base=str(tmp_path / "workspaces"),
        trace_db_path=str(tmp_path / "workspaces"),
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


def register_dpe_agent_configs(sf: SkillFlow, tools: list[str] | None = None):
    """Register all agent configs referenced by dpe_full.yaml."""
    tool_list = tools or ["file_exists", "json_schema", "syntax_lint",
                          "py_compile", "pytest", "repo_apply", "dir_tree"]
    for name in DPE_AGENT_CONFIGS:
        sf.register_agent_config(name, model="mock", tools=tool_list)
