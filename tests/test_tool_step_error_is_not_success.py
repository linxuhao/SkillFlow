"""A tool step whose tool reported an error must not be recorded 'completed'.

`_confirm_tool_in_tx` writes ``status = 'completed'`` unconditionally, and a tool
step's result IS its routing flags, so before this the engine took whichever edge
matched and carried on. For a plumbing tool that is the malignant shape:
``repo_apply`` returns ``{"applied": False, "error": …}`` when it has no project
root to commit into (its own guard, added because the engine now OMITS
``project_root`` for a run that declares no code repository), the step was marked
completed, the run took this node's first edge, and it reported success having
applied nothing.

The signature-level guard does not reach this. ``ToolArgumentsUnavailable`` is
raised from ``signature.bind``, so it fires only when the tool declares the
parameter REQUIRED — ``git_sync_pre(project_root: str)`` does, while
``repo_apply``/``repo_validate``/``pytest`` all default theirs and bind fine.

The other half of the contract is that this must NOT break a gate tool step,
whose ``error`` is its verdict and is meant to be routed back to a maker
(``forge_lint`` → ``emit_graph``), nor a step whose author deliberately tolerates
an error (``git_push_post``: a failed push must not fail a run whose work is
already committed). Those declare ``tool_error: "route"``.
"""

from pathlib import Path

import pytest

from skillflow.core import SkillFlow, StepResult
from skillflow.graph import (
    PipelineGraph, StepNode, Transition, EndConditions, EndCondition,
)
from tests.mocks import MockToolLoader


def _graph(tool_error: str = "fail", transitions=None):
    """gen (agent) → t (tool) → done (agent)."""
    return PipelineGraph(
        name="toolerr",
        begin="gen",
        steps=[
            StepNode(id="gen", step_type="agent", agent_config="noop_agent",
                     transitions=[Transition(to="t")]),
            StepNode(id="t", step_type="tool", tool_name="plumbing",
                     tool_error=tool_error,
                     transitions=transitions or [Transition(to="done")]),
            StepNode(id="done", step_type="agent", agent_config="noop_agent",
                     transitions=[]),
        ],
        end_conditions=EndConditions(
            combinator="or",
            conditions=[EndCondition(type="node_reached", node="done",
                                     result="completed")],
        ),
    )


def _run_to_the_tool(tmp_path, graph, fn):
    tmp_path = Path(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    tools = MockToolLoader()
    tools.register("plumbing", fn)
    sf = SkillFlow(str(tmp_path / "t.db"), tool_loader=tools)
    sf.register_agent_config("noop_agent")
    sf.register_graph(graph)
    run_id = sf.create_run("toolerr")
    sf.start_run(run_id)
    sf.advance_run(run_id)
    claimed = sf.claim_next_step(run_id)
    assert claimed.step_id == "gen"
    sf.confirm_step(claimed.token, StepResult(outputs={}, flags={}))
    # advance onto the tool node, then let the fast-path execute it
    for _ in range(4):
        sf.advance_run(run_id)
        if sf.get_run(run_id)["status"] != "running":
            break
        if sf.get_run(run_id)["current_node"] != "t":
            break
    return sf, run_id


def _step_rows(sf, run_id, step_id):
    with sf._ro() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT status, last_error FROM skillflow_steps "
            "WHERE run_id = ? AND step_id = ? ORDER BY id", (run_id, step_id))]


# ── The bug ───────────────────────────────────────────────────────────────────

def test_a_refusing_repo_tool_fails_the_step_and_the_run(tmp_path):
    """The exact repo_apply shape: applied nothing, said so, defaulted arg."""
    sf, run_id = _run_to_the_tool(
        tmp_path, _graph(),
        lambda **k: {"applied": False, "files": [],
                     "error": "repo_apply: project_root must be an absolute "
                              "path (got '') — refusing to resolve against the "
                              "process CWD"})

    run = sf.get_run(run_id)
    assert run["status"] == "failed", (
        "a tool step that applied nothing reported success: " + str(dict(run)))
    assert "refusing to resolve" in (run["error_reason"] or ""), dict(run)

    rows = _step_rows(sf, run_id, "t")
    assert rows and rows[-1]["status"] == "failed", rows
    assert "repo_apply" in (rows[-1]["last_error"] or ""), rows


def test_the_failed_tool_step_never_reaches_the_next_node(tmp_path):
    """The step after the failure must never be entered.

    Asserted on the EDGE, not on `current_node`: the tool node's edge to `done`
    trips the graph's `node_reached` end condition, which completes the run
    without ever writing `current_node`. So "current_node is still 't'" is true
    whether the run failed here or sailed through — the edge count is what
    distinguishes them.
    """
    sf, run_id = _run_to_the_tool(
        tmp_path, _graph(), lambda **k: {"error": "nothing was applied"})
    assert sf.get_run(run_id)["status"] == "failed"
    with sf._ro() as conn:
        edges = [dict(r) for r in conn.execute(
            "SELECT from_step, to_step, count FROM skillflow_edge_counts "
            "WHERE run_id = ?", (run_id,))]
    assert not [e for e in edges if e["from_step"] == "t"], edges
    assert all(r["status"] == "pending" for r in _step_rows(sf, run_id, "done"))


def test_it_does_not_retry_a_failure_that_cannot_change(tmp_path):
    """Deterministic: the same arguments meet the same tool next tick.

    The tool must be called exactly ONCE — and the run must be `failed`, or the
    single call proves nothing (a run that sailed through also calls it once).
    """
    calls = []

    def fn(**k):
        calls.append(k)
        return {"error": "no project root"}

    sf, run_id = _run_to_the_tool(tmp_path, _graph(), fn)
    assert sf.get_run(run_id)["status"] == "failed"
    for _ in range(3):
        sf.advance_run(run_id)
    assert len(calls) == 1, f"re-ran a deterministic failure {len(calls)} times"


# ── What it must NOT break ────────────────────────────────────────────────────

def test_a_falsy_error_key_is_not_a_failure(tmp_path):
    """`{"error": None}` is a tool initialising its result, not failing."""
    sf, run_id = _run_to_the_tool(
        tmp_path, _graph(), lambda **k: {"passed": True, "error": None})
    assert sf.get_run(run_id)["status"] == "completed"


def test_a_gate_tool_step_routes_its_verdict_instead_of_dying(tmp_path):
    """The forge_lint shape: `{passed: false, error: <issues>}` → back to gen."""
    graph = _graph(tool_error="route", transitions=[
        Transition(to="done", match={"passed": True}),
        Transition(to="gen", match={"passed": False}, max_loop=3),
    ])
    sf, run_id = _run_to_the_tool(
        tmp_path, graph,
        lambda **k: {"passed": False, "error": "lint failed:\n- bad edge"})

    assert sf.get_run(run_id)["status"] == "running"
    assert sf.get_run(run_id)["current_node"] == "gen"
    rows = _step_rows(sf, run_id, "t")
    assert rows[-1]["status"] == "completed", rows


def test_a_tolerated_error_completes_the_run(tmp_path):
    """The git_push_post shape: one unconditional edge, error is an outcome."""
    sf, run_id = _run_to_the_tool(
        tmp_path, _graph(tool_error="route"),
        lambda **k: {"pushed": False, "action": "error",
                     "error": "git push origin main failed: auth"})
    assert sf.get_run(run_id)["status"] == "completed"
    assert _step_rows(sf, run_id, "t")[-1]["status"] == "completed"


def test_route_makes_the_error_matchable_as__tool_error(tmp_path):
    """`error` holds free text, so a graph needs a boolean to route on."""
    graph = _graph(tool_error="route", transitions=[
        Transition(to="gen", match={"_tool_error": True}, max_loop=3),
        Transition(to="done"),
    ])
    sf, run_id = _run_to_the_tool(
        tmp_path, graph, lambda **k: {"error": "the remote said no"})
    assert sf.get_run(run_id)["current_node"] == "gen"

    # …and the same node with no error takes the unconditional edge.
    graph2 = _graph(tool_error="route", transitions=[
        Transition(to="gen", match={"_tool_error": True}, max_loop=3),
        Transition(to="done"),
    ])
    sf2, run2 = _run_to_the_tool(tmp_path / "b", graph2,
                                 lambda **k: {"pushed": True})
    assert sf2.get_run(run2)["status"] == "completed"


def test_the_stored_result_is_not_mutated_by_the_synthesised_flag(tmp_path):
    """`_tool_error` is a routing flag, not something the tool returned."""
    sf, run_id = _run_to_the_tool(
        tmp_path, _graph(tool_error="route"),
        lambda **k: {"pushed": False, "error": "auth"})
    with sf._ro() as conn:
        row = conn.execute(
            "SELECT outputs_json FROM skillflow_steps WHERE run_id = ? AND "
            "step_id = 't' ORDER BY id DESC LIMIT 1", (run_id,)).fetchone()
    assert "_tool_error" not in row["outputs_json"], row["outputs_json"]


# ── The declaration survives a round-trip through the graph schema ────────────

def test_tool_error_is_read_from_yaml_and_written_back(tmp_path):
    from skillflow.graph import PipelineGraph as G
    data = {
        "name": "rt", "begin": "t",
        "steps": [
            {"id": "t", "step_type": "tool", "tool_name": "x",
             "tool_error": "route", "transitions": [{"to": "u"}]},
            {"id": "u", "step_type": "gate", "transitions": [{"to": None}]},
        ],
    }
    g = G._from_dict(data)
    steps = {s.id: s for s in g.steps}
    assert steps["t"].tool_error == "route"
    assert steps["u"].tool_error == "fail"  # the default, for every node
    back = g.to_dict()
    t = [s for s in back["steps"] if s["id"] == "t"][0]
    assert t["tool_error"] == "route"
    u = [s for s in back["steps"] if s["id"] == "u"][0]
    assert "tool_error" not in u, "the default must not be serialised as noise"


@pytest.mark.parametrize("cfg", ["fail", "route"])
def test_a_clean_result_is_unaffected_either_way(tmp_path, cfg):
    sf, run_id = _run_to_the_tool(
        tmp_path / cfg, _graph(tool_error=cfg),
        lambda **k: {"passed": True, "files": ["a.py"]})
    assert sf.get_run(run_id)["status"] == "completed"
