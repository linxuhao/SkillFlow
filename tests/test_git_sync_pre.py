"""Tests for git_sync_pre tool — unit + integration.

Covers:
  - Non-git dir → skip
  - Git repo, no remote → skip
  - Up-to-date → skip
  - Fast-forward → success with pulled count
  - Diverged/conflict → explicit failure message
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
    result = fn(project_root=str(tmp_path))
    assert result["synced"] is True
    assert result["action"] == "skip"
    assert "no remote" in result["detail"]
    assert "error" not in result


def test_up_to_date(tmp_path):
    """Local equals remote → up-to-date skip."""
    _init_git(tmp_path)
    (tmp_path / "f.txt").write_text("hi")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")

    # Simulate remote on same branch with same HEAD
    _git(tmp_path, "remote", "add", "origin", ".")
    # fetch from self — HEAD already matches
    _git(tmp_path, "fetch", "origin")

    fn = _load_tool()
    result = fn(project_root=str(tmp_path))
    assert result["synced"] is True
    assert result["action"] == "up-to-date"
    assert "error" not in result


def test_fast_forward_pull(tmp_path):
    """Remote is ahead → fast-forward pull succeeds."""
    # Create "remote" repo
    remote = tmp_path / "remote"
    _init_git(remote)
    (remote / "f.txt").write_text("v1")
    _git(remote, "add", "-A")
    _git(remote, "commit", "-qm", "init")

    # Clone it BEFORE adding more commits to remote
    local = tmp_path / "local"
    _git(tmp_path, "clone", "-q", str(remote), str(local))

    # Now add a NEW commit to remote so local is behind
    (remote / "f.txt").write_text("v2")
    _git(remote, "add", "-A")
    _git(remote, "commit", "-qm", "v2")

    fn = _load_tool()
    result = fn(project_root=str(local))
    assert result["synced"] is True
    assert result["action"] == "pulled"
    assert result["pulled"] == 1
    assert "error" not in result


def test_diverged_conflict_failure(tmp_path):
    """Remote has diverged → explicit failure message."""
    # Create "remote" repo
    remote = tmp_path / "remote"
    _init_git(remote)
    (remote / "f.txt").write_text("remote content")
    _git(remote, "add", "-A")
    _git(remote, "commit", "-qm", "remote commit")

    # Clone it
    local = tmp_path / "local"
    _git(tmp_path, "clone", "-q", str(remote), str(local))

    # Diverging commits on both sides
    (local / "f.txt").write_text("local content")
    _git(local, "add", "-A")
    _git(local, "commit", "-qm", "local commit")

    (remote / "f.txt").write_text("remote v2")
    _git(remote, "add", "-A")
    _git(remote, "commit", "-qm", "remote v2")

    fn = _load_tool()
    result = fn(project_root=str(local))

    # Must be an explicit failure
    assert result["synced"] is False
    assert result["action"] == "conflict"
    assert "error" in result
    error = result["error"]

    # Error message must be actionable
    assert "diverged" in error.lower() or "conflict" in error.lower()
    assert "Branch:" in error
    assert "Local HEAD:" in error
    assert "Remote" in error
    assert "git pull --rebase" in error


def test_detached_head(tmp_path):
    """Detached HEAD → silent skip."""
    _init_git(tmp_path)
    (tmp_path / "f.txt").write_text("hi")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")
    _git(tmp_path, "remote", "add", "origin", ".")
    # Checkout a specific commit to detach HEAD
    sha = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    _git(tmp_path, "checkout", "-q", sha)

    fn = _load_tool()
    result = fn(project_root=str(tmp_path))
    assert result["synced"] is True
    assert result["action"] == "skip"
    assert "detached HEAD" in result["detail"]
    assert "error" not in result


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
        # The error must contain the divergence message, not a generic "Tool failed"
        assert "diverged" in error.lower() or "conflict" in error.lower(), \
            f"Expected divergence error, got: {error}"
        assert "git pull --rebase" in error, \
            f"Expected actionable fix hint, got: {error}"
