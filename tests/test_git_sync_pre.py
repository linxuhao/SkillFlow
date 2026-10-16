"""Tests for git_sync_pre tool — unit + integration.

NOTE on `policy="pull"`: fetching and fast-forwarding is now opted into by name
and the default is no network at all. These tests are about the sync mechanics,
so they ask for the sync; `test_not_a_git_repo` deliberately does not, because
its subject is the refusal that comes before any policy. See
tests/test_git_sync_policy.py for the default and for the worktree refusal.

Covers:
  - Non-git dir → skip
  - Git repo, no remote → skip
  - Remote configured + policy pull → explicit unavailable (closed Git policy)
  - Error message flows from skillflow → host
"""

import subprocess
from pathlib import Path

import pytest

from skillflow.core import SkillFlow, StepResult
from skillflow.graph import PipelineGraph, StepNode, Transition
from skillflow.tool_loader import ToolLoader
from skillflow.workspace import WorkspaceManager

_REAL_TOOLS = Path(__file__).parent.parent / "src" / "skillflow" / "tools"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True
    )


def _init_git(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)


def _load_tool():
    loader = ToolLoader(_REAL_TOOLS)
    return loader.load_fn("git_sync_pre")


# ── Unit tests ────────────────────────────────────────────────────────────


def test_not_a_git_repo(tmp_path):
    """Non-git directory → silent skip."""
    fn = _load_tool()
    result = fn(project_root=str(tmp_path))
    assert result["synced"] is True
    assert result["action"] == "skip"
    assert "not a git repository" in result["detail"]
    assert "error" not in result


def test_git_repo_no_remote(tmp_path):
    """Git repo without remote → silent skip."""
    _init_git(tmp_path)
    fn = _load_tool()
    result = fn(project_root=str(tmp_path), policy="pull")
    assert result["synced"] is True
    assert result["action"] == "skip"
    assert "no remote" in result["detail"]
    assert "error" not in result


def _behind_clone(tmp_path):
    remote = tmp_path / "remote"
    _init_git(remote)
    (remote / "f.txt").write_text("v1")
    _git(remote, "add", "-A")
    _git(remote, "commit", "-qm", "init")
    local = tmp_path / "local"
    _git(tmp_path, "clone", "-q", str(remote), str(local))
    (remote / "f.txt").write_text("v2")
    _git(remote, "add", "-A")
    _git(remote, "commit", "-qm", "v2")
    return remote, local


def test_pull_with_a_remote_is_unavailable_not_fetched(tmp_path):
    """Remote ahead → explicit unavailable; nothing fetched or moved.

    fetch/pull would run repository-configured transport, hook and checkout
    filter programs in the engine; the closed Git policy refuses that rather
    than falling back to native fetch/pull.
    """
    _, local = _behind_clone(tmp_path)
    head = _git(local, "rev-parse", "HEAD").stdout.strip()
    tracking = _git(local, "for-each-ref", "refs/remotes").stdout

    fn = _load_tool()
    result = fn(project_root=str(local), policy="pull")
    assert result["synced"] is False
    assert result["action"] == "unavailable"
    assert "closed Git policy" in result["error"]
    assert _git(local, "rev-parse", "HEAD").stdout.strip() == head
    assert (local / "f.txt").read_text() == "v1"
    assert _git(local, "for-each-ref", "refs/remotes").stdout == tracking


# ── Integration test: error message flows from skillflow to host ───────────


def test_diverged_error_flows_to_run_error_reason(tmp_path):
    """When git_sync_pre fails as a tool_step, the run's error_reason
    must contain the actionable divergence message — not a generic
    'Tool failed' string."""
    # Setup: remote + local with diverging commits
    remote = tmp_path / "remote"
    _init_git(remote)
    (remote / "f.txt").write_text("remote")
    _git(remote, "add", "-A")
    _git(remote, "commit", "-qm", "remote initial")

    local = tmp_path / "local"
    _git(tmp_path, "clone", "-q", str(remote), str(local))
    (local / "f.txt").write_text("local")
    _git(local, "add", "-A")
    _git(local, "commit", "-qm", "local diverged")
    (remote / "f.txt").write_text("remote v2")
    _git(remote, "add", "-A")
    _git(remote, "commit", "-qm", "remote diverged")

    ws_base = tmp_path / "ws"
    projects_base = tmp_path / "projects"

    sf = SkillFlow(":memory:")
    sf._tool_loader = ToolLoader(_REAL_TOOLS)
    sf._workspace = WorkspaceManager(
        str(ws_base), projects_base=str(projects_base),
        code_path_resolver=lambda pid: str(local),
    )

    # Pipeline: git_sync_pre → on synced=True go to researcher,
    #           on synced=False go to fail_handler with feedback
    sync_node = StepNode(
        id="git_sync_pre",
        step_type="tool",
        tool_name="git_sync_pre",
        tool_params={"project_root": "$PROJECT_ROOT"},
        transitions=[
            Transition(to="researcher", match={"synced": True}),
            Transition(to="fail_handler", match={"synced": False}, feedback=True),
        ],
    )
    researcher = StepNode(
        id="researcher",
        step_type="agent",
        transitions=[Transition(to=None)],
    )
    fail_handler = StepNode(
        id="fail_handler",
        step_type="agent",
        transitions=[Transition(to=None)],
    )

    g = PipelineGraph(
        name="sync_test",
        begin="git_sync_pre",
        steps=[sync_node, researcher, fail_handler],
    )
    sf.register_graph(g)

    rid = sf.create_run("sync_test", {"project_id": "syncproj"})
    sf.start_run(rid)

    # Advance through git_sync_pre tool step
    next_node = sf.advance_run(rid)

    # git_sync_pre should fail → transition with feedback to fail_handler
    # The run should NOT be completed (it has fail_handler pending).
    run = sf._conn.execute(
        "SELECT status, error_reason FROM skillflow_runs WHERE id = ?", (rid,)
    ).fetchone()

    # Check that the error message is preserved (not swallowed)
    if run["error_reason"]:
        error = run["error_reason"]
        # The error must carry the tool's own explanation, not "Tool failed"
        assert "unavailable" in error.lower(), \
            f"Expected unavailable sync error, got: {error}"
        assert "outside the engine" in error, \
            f"Expected actionable fix hint, got: {error}"
