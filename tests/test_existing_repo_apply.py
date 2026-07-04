"""Existing-repo apply: a project mapped (via code_path_resolver) to an
arbitrary repo must have its repo_apply lifecycle commit INTO that repo, not
into the default projects_base/<id> workspace location.

Regression test for AItelier's "work on an existing repo as a first-class
project" flow: skillflow's default code path is keyed by project_id, which
cannot point at an existing repo. The host passes a code_path_resolver; this
test proves the whole chain (resolver → get_project_code_path → lifecycle
project_root → repo_apply git commit) lands the change in the real repo.
"""

import subprocess
from pathlib import Path

from skillflow.core import SkillFlow, StepResult
from skillflow.graph import PipelineGraph, StepNode, Transition
from skillflow.tool_loader import ToolLoader
from skillflow.workspace import WorkspaceManager

_REAL_TOOLS = Path(__file__).parent.parent / "src" / "skillflow" / "tools"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True
    ).stdout


def test_repo_apply_lands_in_existing_repo(tmp_path):
    # An existing git repo, pre-seeded and committed (the "previous project").
    existing = tmp_path / "existing_repo"
    existing.mkdir()
    (existing / "server.py").write_text("# original\nBUG = True\n")
    subprocess.run(["git", "init", "-q"], cwd=existing, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=existing, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=existing, check=True)
    subprocess.run(["git", "add", "-A"], cwd=existing, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=existing, check=True)

    ws_base = tmp_path / "ws"
    projects_base = tmp_path / "projects"

    # Host resolver: the project "fixproj" lives in the existing repo; anything
    # else falls back to the default layout (returns None).
    def resolver(pid: str):
        return str(existing) if pid == "fixproj" else None

    sf = SkillFlow(":memory:")
    sf._tool_loader = ToolLoader(_REAL_TOOLS)
    sf._workspace = WorkspaceManager(
        str(ws_base), projects_base=str(projects_base), code_path_resolver=resolver
    )

    node = StepNode(
        id="s1",
        step_type="agent",
        output_mode="content",
        output_fixed={"out": "server.py"},
        lifecycle={"on_deliver": {"tool": "repo_apply",
                                  "params": {"source_dir": "$STEP_DIR"}}},
        transitions=[Transition(to=None)],
    )
    g = PipelineGraph(name="existing_apply", begin="s1", steps=[node])
    sf.register_graph(g)

    # Sanity: the resolver is consulted for the code path.
    assert sf._workspace.get_project_code_path("fixproj") == existing.resolve()

    rid = sf.create_run("existing_apply", {"project_id": "fixproj"})
    sf.start_run(rid)
    sf.advance_run(rid)
    token = sf.claim_next_step(rid)

    # The agent "writes" the fix into the step staging dir.
    tmp = sf._workspace.get_step_tmp_dir("fixproj", "existing_apply", "s1")
    (tmp / "server.py").write_text("# patched\nBUG = False\n")

    sf.confirm_step(token.token, StepResult(outputs={}, flags={}))

    # 1. The fix landed in the EXISTING repo's working tree.
    assert (existing / "server.py").read_text() == "# patched\nBUG = False\n"

    # 2. It was committed there (repo_apply runs git add + commit).
    log = _git(existing, "log", "--oneline")
    assert "/s1:" in log, f"no apply commit in existing repo:\n{log}"

    # 3. It did NOT leak into the default workspace location.
    assert not (projects_base / "fixproj" / "server.py").exists()


def test_repo_apply_default_location_when_resolver_returns_none(tmp_path):
    """A new/clone project (resolver returns None) still applies to the
    default projects_base/<id> repo — the existing behavior is preserved."""
    ws_base = tmp_path / "ws"
    projects_base = tmp_path / "projects"
    default_repo = projects_base / "newproj"
    default_repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=default_repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=default_repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=default_repo, check=True)

    sf = SkillFlow(":memory:")
    sf._tool_loader = ToolLoader(_REAL_TOOLS)
    sf._workspace = WorkspaceManager(
        str(ws_base), projects_base=str(projects_base),
        code_path_resolver=lambda pid: None,
    )

    node = StepNode(
        id="s1", step_type="agent", output_mode="content",
        output_fixed={"out": "main.py"},
        lifecycle={"on_deliver": {"tool": "repo_apply",
                                  "params": {"source_dir": "$STEP_DIR"}}},
        transitions=[Transition(to=None)],
    )
    g = PipelineGraph(name="default_apply", begin="s1", steps=[node])
    sf.register_graph(g)

    rid = sf.create_run("default_apply", {"project_id": "newproj"})
    sf.start_run(rid)
    sf.advance_run(rid)
    token = sf.claim_next_step(rid)
    tmp = sf._workspace.get_step_tmp_dir("newproj", "default_apply", "s1")
    (tmp / "main.py").write_text("print('hi')\n")
    sf.confirm_step(token.token, StepResult(outputs={}, flags={}))

    assert (default_repo / "main.py").read_text() == "print('hi')\n"
