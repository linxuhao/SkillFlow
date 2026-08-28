"""A run that declares no code repository must not be given one.

`code_path_resolver` returning None means "no opinion, use your default", so a
host had no way to say "this project owns no repository at all". A repo-less run
therefore had `projects_base/<project_id>` invented for it, and `build_source_map`
attached that path as a `repo` read layer on nothing more than an `is_dir()`
check. Its repo access thus depended on whether that directory happened to
exist — an accident, not a decision, and the accident was already being made:
the directory was there (empty) in the run where this was found.

The resolver can now answer `False`. These tests pin the difference, and they
CREATE the directory with a file in it on purpose: a test that relied on the
path being absent would pass just as happily with the old behaviour.
"""

import pytest

from skillflow.core import SkillFlow
from skillflow.graph import (
    PipelineGraph, StepNode, Transition, EndConditions, EndCondition,
)
from skillflow.tool_loader import ToolLoader
from skillflow.workspace import WorkspaceManager


def _graph() -> PipelineGraph:
    return PipelineGraph(
        name="g", begin="work",
        steps=[
            StepNode(id="work", step_type="agent", agent_config="worker",
                     context=[{"source": {"step": "prior"}}],
                     transitions=[Transition(to="done")]),
            StepNode(id="done", step_type="gate", transitions=[]),
        ],
        end_conditions=EndConditions(combinator="or", conditions=[
            EndCondition(type="node_reached", node="done", result="completed")]))


def _engine(tmp_path, resolver):
    # The default path is POPULATED, so nothing here passes merely because a
    # directory is missing.
    d = tmp_path / "projects" / "p1"
    d.mkdir(parents=True, exist_ok=True)
    (d / "IN_THE_DEFAULT_REPO.txt").write_text("x", encoding="utf-8")

    sf = SkillFlow(":memory:", tool_loader=ToolLoader(),
                   workspace_base=str(tmp_path / "workspaces"),
                   projects_base=str(tmp_path / "projects"),
                   code_path_resolver=resolver)
    sf.register_agent_config("worker", tools=["read_file"])
    sf.register_graph(_graph())
    return sf


def _listing(sf) -> str:
    run_id = sf.create_run("g", project_id="p1")
    sf.start_run(run_id)
    sf.advance_run(run_id)
    sf.claim_next_step(run_id)
    return str(sf.execute_tool("list", {}, run_id=run_id, step_id="work"))


# ── the resolver contract ────────────────────────────────────────────────

def test_false_means_no_repo_and_none_still_means_no_opinion(tmp_path):
    (tmp_path / "projects" / "p1").mkdir(parents=True)
    ws = WorkspaceManager(base_path=str(tmp_path / "workspaces"),
                          projects_base=str(tmp_path / "projects"),
                          code_path_resolver=lambda pid: False)
    assert ws.get_project_code_path("p1") is None

    ws_default = WorkspaceManager(base_path=str(tmp_path / "workspaces"),
                                  projects_base=str(tmp_path / "projects"),
                                  code_path_resolver=lambda pid: None)
    assert ws_default.get_project_code_path("p1") == \
        (tmp_path / "projects" / "p1").resolve()

    ws_linked = WorkspaceManager(base_path=str(tmp_path / "workspaces"),
                                 projects_base=str(tmp_path / "projects"),
                                 code_path_resolver=lambda pid: str(tmp_path / "elsewhere"))
    assert ws_linked.get_project_code_path("p1") == (tmp_path / "elsewhere").resolve()


# ── what the step can actually read ──────────────────────────────────────

def test_a_repoless_run_gets_no_repo_read_layer(tmp_path):
    sf = _engine(tmp_path, resolver=lambda pid: False)
    assert "IN_THE_DEFAULT_REPO.txt" not in _listing(sf)


def test_the_control_proves_the_test_can_tell_the_difference(tmp_path):
    """Same populated directory, resolver merely has 'no opinion' — the default
    applies and the file IS readable. Without this, the test above would pass on
    a build that had simply broken the repo layer for everyone."""
    sf = _engine(tmp_path, resolver=lambda pid: None)
    assert "IN_THE_DEFAULT_REPO.txt" in _listing(sf)


def test_a_tool_step_is_handed_an_empty_project_root_not_the_string_none(tmp_path):
    """`str(None)` is "None", a path-shaped lie that a tool would try to open.
    An empty root is refusable — tools already check for it."""
    sf = _engine(tmp_path, resolver=lambda pid: False)
    ws = sf._workspace
    assert ws.get_project_code_path("p1") is None
    cp = ws.get_project_code_path("p1")
    assert (str(cp) if cp else "") == ""
