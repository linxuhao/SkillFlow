"""A step's read tools belong to that step, not to whoever claimed last.

`read`/`search`/`list` close over the source map of the step that built them —
its workspace, its staging dir, its code repo. They used to be registered in the
ToolLoader under those three fixed names, in one process-wide slot each, and
resolved by name at call time. With one project advancing at a time that was
safe only by accident: no second claim could land between a claim and its
execution. Once a host advances several projects concurrently the accident is
gone, and the last claim's closures answer every in-flight step.

Observed 2026-08-28 in AItelier: a code-review step in a repo-less project
listed an unrelated project's git repository (character art from a game the
reviewer then wrote findings about), because that project claimed a step while
the reviewer was still running.
"""

import pytest

from skillflow.core import SkillFlow, StepResult
from skillflow.graph import (
    PipelineGraph, StepNode, Transition, EndConditions, EndCondition,
)
from skillflow.tool_loader import ToolLoader


def _graph(name: str) -> PipelineGraph:
    return PipelineGraph(
        name=name, begin="review",
        steps=[
            StepNode(id="review", step_type="agent", agent_config="reviewer",
                     context=[{"source": {"step": "prior"}}],
                     transitions=[Transition(to="done")]),
            StepNode(id="done", step_type="gate", transitions=[]),
        ],
        end_conditions=EndConditions(combinator="or", conditions=[
            EndCondition(type="node_reached", node="done", result="completed")]))


@pytest.fixture
def sf(tmp_path):
    # The REAL ToolLoader, not a mock. The whole defect lives in how a callable
    # is stored and resolved there, so a double that lacks that machinery would
    # make this suite pass (or fail) for reasons unrelated to the bug.
    engine = SkillFlow(":memory:", tool_loader=ToolLoader(),
                       workspace_base=str(tmp_path / "workspaces"),
                       projects_base=str(tmp_path / "projects"))
    engine.register_agent_config("reviewer", tools=["read_file"])
    return engine


def _repo(tmp_path, project_id: str, marker: str) -> None:
    """A code repo for *project_id* holding one identifying file."""
    d = tmp_path / "projects" / project_id
    d.mkdir(parents=True, exist_ok=True)
    (d / marker).write_text("x", encoding="utf-8")


def _claim(sf, graph_name: str, project_id: str):
    run_id = sf.create_run(graph_name, project_id=project_id)
    sf.start_run(run_id)
    sf.advance_run(run_id)
    return run_id, sf.claim_next_step(run_id)


def _listing(sf, run_id: str) -> str:
    out = sf.execute_tool("list", {}, run_id=run_id, step_id="review")
    return str(out)


def test_a_second_projects_claim_does_not_take_over_the_first_ones_reads(
        sf, tmp_path):
    """The bug, exactly: A claims, B claims, A reads — and must see A's repo."""
    _repo(tmp_path, "proj_a", "ONLY_IN_A.txt")
    _repo(tmp_path, "proj_b", "ONLY_IN_B.txt")
    sf.register_graph(_graph("ga"))
    sf.register_graph(_graph("gb"))

    run_a, claimed_a = _claim(sf, "ga", "proj_a")
    assert "list" in claimed_a.inputs.get("_tool_schemas", {}), \
        "the step must actually be granted the read surface, or this proves nothing"

    # B claims while A is still executing — the moment that used to overwrite
    # A's closures.
    run_b, _ = _claim(sf, "gb", "proj_b")

    listing_a = _listing(sf, run_a)

    assert "ONLY_IN_A.txt" in listing_a
    assert "ONLY_IN_B.txt" not in listing_a, \
        "A's step read B's repository — the read tools are shared again"


def test_each_project_keeps_reading_its_own_repo_after_the_other_claims(
        sf, tmp_path):
    """Both directions: B must not inherit A's either."""
    _repo(tmp_path, "proj_a", "ONLY_IN_A.txt")
    _repo(tmp_path, "proj_b", "ONLY_IN_B.txt")
    sf.register_graph(_graph("ga"))
    sf.register_graph(_graph("gb"))

    run_a, _ = _claim(sf, "ga", "proj_a")
    run_b, _ = _claim(sf, "gb", "proj_b")

    assert "ONLY_IN_B.txt" in _listing(sf, run_b)
    assert "ONLY_IN_A.txt" not in _listing(sf, run_b)
    # …and A still works after B's claim, i.e. the fix isn't "last claim loses".
    assert "ONLY_IN_A.txt" in _listing(sf, run_a)


def test_the_tools_are_released_when_the_step_ends(sf, tmp_path):
    """Reclaimed at step end: nothing may call a finished step's read surface.

    Without this the map grows for the life of the process and a late call from
    an abandoned executor still reaches a live source map.
    """
    _repo(tmp_path, "proj_a", "ONLY_IN_A.txt")
    sf.register_graph(_graph("ga"))
    run_a, claimed_a = _claim(sf, "ga", "proj_a")
    assert "ONLY_IN_A.txt" in _listing(sf, run_a)

    sf.confirm_step(claimed_a.token, StepResult(outputs={}, flags={}))

    out = sf.execute_tool("list", {}, run_id=run_a, step_id="review")
    assert "ONLY_IN_A.txt" not in str(out)
    # A legible refusal, not "tool not found in any tools directory": the tool
    # exists, this step just no longer owns one.
    assert "per step" in out.get("error", "")


def test_a_failed_step_also_gives_its_tools_back(sf, tmp_path):
    _repo(tmp_path, "proj_a", "ONLY_IN_A.txt")
    sf.register_graph(_graph("ga"))
    run_a, claimed_a = _claim(sf, "ga", "proj_a")
    sf.fail_step(claimed_a.token, "boom")
    assert "ONLY_IN_A.txt" not in _listing(sf, run_a)


def test_the_read_trio_is_never_left_in_the_shared_loader(sf, tmp_path):
    """The structural claim, not just the observable one.

    A test that only compares listings would still pass if the closures went
    back into a shared slot under different names. Nothing per-step may be
    reachable through the loader at all — only the NAME is global, so that
    is_native/is_dynamic keep classifying these tools correctly.
    """
    _repo(tmp_path, "proj_a", "ONLY_IN_A.txt")
    sf.register_graph(_graph("ga"))
    _claim(sf, "ga", "proj_a")

    for name in ("read", "search", "list"):
        assert name not in sf._tool_loader._cache, \
            f"{name!r} put a callable in the shared loader"
        # …but the NAME is still known, so is_native keeps classifying it the
        # way it did when the callable lived there (runner mode branches on it).
        assert sf._tool_loader.is_native(name)
    assert sf._step_scoped_names >= {"read", "search", "list"}


# ── Release is a compare-and-delete, not a blind pop ──────────────────────

def test_a_stale_executor_cannot_release_its_replacements_tools(sf, tmp_path):
    """`(run_id, step_id)` names the STEP, not the claim.

    `_assert_epoch` narrows the window in which a reclaimed executor can reach
    the release, but assert and release are separate operations — a stall
    between them lets the reaper hand the step to a replacement whose entry sits
    under the same key. A blind pop would delete it, and the replacement would
    run its whole step with no read surface: a silent degrade, which is the
    exact failure shape this area exists to stop producing.
    """
    _repo(tmp_path, "proj_a", "ONLY_IN_A.txt")
    sf.register_graph(_graph("ga"))
    run_a, claimed = _claim(sf, "ga", "proj_a")
    live_epoch = claimed.token.claim_epoch

    # A stale claim generation tries to give the tools back.
    sf._release_step_tools(run_a, "review", live_epoch - 1)
    assert "ONLY_IN_A.txt" in _listing(sf, run_a), \
        "a stale epoch released the live claim's tools"

    # The owner's own release still works.
    sf._release_step_tools(run_a, "review", live_epoch)
    assert "ONLY_IN_A.txt" not in _listing(sf, run_a)


# ── The loader keeps only NAMES, and only the right ones ──────────────────

def test_adding_a_tools_dir_forgets_registered_callables_but_not_step_names(
        tmp_path):
    """`add_tools_dir` clears `_cache`, which is where a registered dynamic
    tool's callable lives — so that name must stop counting as native, exactly
    as before. A step-owned name has no callable in this loader to invalidate,
    so it must survive: runner mode branches on `is_native` to decide between
    executing a tool and delegating it to the agent.
    """
    from skillflow.tool_loader import ToolLoader
    loader = ToolLoader(tmp_path / "native")
    (tmp_path / "native").mkdir()

    loader.register_dynamic_tool("registered", {"name": "registered"},
                                 lambda **kw: {})
    loader.declare_dynamic("list")
    assert loader.is_native("registered") and loader.is_native("list")

    loader.add_tools_dir(tmp_path / "extra")

    assert not loader.is_native("registered"), \
        "its callable was just discarded; it must not still read as native"
    assert loader.is_native("list"), \
        "a step-owned name has nothing in this loader to invalidate"
